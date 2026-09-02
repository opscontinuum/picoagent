"""A tiny fake Elasticsearch for tests and offline demos. Standard library only.

It holds a deterministic 30-minute incident in memory - a checkout service whose
error rate, CPU and request latency all spike between minute 15 and 20 - spread
across Elastic Agent / Beats data streams:

    logs-checkout-default        filebeat-style app logs (ECS: log.level, message, service.name)
    logs-nginx.error-default     web-server error log
    metrics-system.cpu-default   metricbeat system.cpu.total.norm.pct
    metrics-system.memory-default metricbeat system.memory.actual.used.pct
    traces-apm-default           APM transactions (transaction.duration.us, event.outcome)

Supported endpoints (enough for the es-doctor plugin):
    GET  /_cluster/health                 GET /_cat/indices?format=json
    GET  /_cluster/allocation/explain     POST /<pattern>/_search
    DELETE anything                       -> recorded, returns 200 (so tests can prove it was blocked upstream)

_search understands: bool.filter with range/term/match/wildcard/simple_query_string,
size, sort on @timestamp, and aggs date_histogram(fixed_interval) with sub-aggs
terms / avg / filter(term). Everything else is ignored. Requests are recorded on
``server.requests``.

Standalone:  python -m picoagent.testing.fake_es --port 9200
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import random
import threading
import urllib.parse
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

INCIDENT_START = datetime(2026, 9, 2, 10, 0, tzinfo=timezone.utc)   # data spans 10:00-10:30 UTC
SPIKE = range(15, 20)                                                  # minutes 15..19 are bad


# ------------------------------------------------------------------ dataset

def build_dataset(seed: int = 7) -> list[tuple[str, dict]]:
    """Return ``[(index, doc), ...]`` for the incident. Deterministic for a given seed."""
    rng = random.Random(seed)
    docs: list[tuple[str, dict]] = []
    base = {"host": {"name": "web-01"}, "agent": {"type": "filebeat"}, "service": {"name": "checkout"}}

    for minute in range(30):
        ts = INCIDENT_START + timedelta(minutes=minute)
        bad = minute in SPIKE
        for i in range(8):                                      # app logs
            level = "error" if bad and i < 6 else ("warn" if i == 7 else "info")
            msg = {"error": "db connection pool exhausted: timeout acquiring connection",
                   "warn": "slow query 850ms on orders", "info": "order placed"}[level]
            docs.append(("logs-checkout-default", {**base, "@timestamp": _iso(ts, i * 6),
                         "log": {"level": level}, "message": msg, "event": {"dataset": "checkout.app"},
                         "data_stream": {"type": "logs", "dataset": "checkout"}, "trace": {"id": f"t{minute}{i}"}}))
        if bad:                                                 # nginx upstream errors
            for i in range(3):
                docs.append(("logs-nginx.error-default", {**base, "agent": {"type": "filebeat"},
                             "@timestamp": _iso(ts, 10 + i * 15), "log": {"level": "error"},
                             "message": "upstream timed out (110: Connection timed out) while reading response",
                             "event": {"dataset": "nginx.error"}, "data_stream": {"type": "logs", "dataset": "nginx.error"}}))
        cpu = 0.93 + rng.random() * 0.05 if bad else 0.25 + rng.random() * 0.1
        mem = 0.88 if bad else 0.55 + rng.random() * 0.05
        for sec in (0, 30):                                     # metricbeat every 30s
            docs.append(("metrics-system.cpu-default", {**base, "agent": {"type": "metricbeat"}, "@timestamp": _iso(ts, sec),
                         "system": {"cpu": {"total": {"norm": {"pct": round(cpu, 3)}}}},
                         "event": {"dataset": "system.cpu"}, "data_stream": {"type": "metrics", "dataset": "system.cpu"}}))
            docs.append(("metrics-system.memory-default", {**base, "agent": {"type": "metricbeat"}, "@timestamp": _iso(ts, sec),
                         "system": {"memory": {"actual": {"used": {"pct": round(mem, 3)}}}},
                         "event": {"dataset": "system.memory"}, "data_stream": {"type": "metrics", "dataset": "system.memory"}}))
        for i in range(5):                                      # APM transactions
            docs.append(("traces-apm-default", {**base, "agent": {"type": "apm"}, "@timestamp": _iso(ts, i * 11),
                         "transaction": {"name": "POST /checkout", "duration": {"us": 4_800_000 if bad else 120_000}},
                         "event": {"outcome": "failure" if bad and i < 4 else "success"},
                         "data_stream": {"type": "traces", "dataset": "apm"}}))
    return docs


def _iso(ts: datetime, seconds: int) -> str:
    return (ts + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _get(doc: dict, dotted: str) -> Any:
    """``_get(doc, "system.cpu.total.norm.pct")`` walks nested dicts; None if missing."""
    cur: Any = doc
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


# ------------------------------------------------------------------ query engine

def _matches(doc: dict, clause: dict) -> bool:
    """Evaluate one query clause against a doc. Unknown clause types match everything."""
    if "bool" in clause:
        b = clause["bool"]
        return (all(_matches(doc, c) for c in b.get("filter", []) + b.get("must", []))
                and not any(_matches(doc, c) for c in b.get("must_not", []))
                and (not b.get("should") or any(_matches(doc, c) for c in b["should"])))
    if "range" in clause:
        field, bounds = next(iter(clause["range"].items()))
        value = _get(doc, field)
        return value is not None and (bounds.get("gte") is None or value >= bounds["gte"]) \
            and (bounds.get("lte") is None or value <= bounds["lte"]) and (bounds.get("lt") is None or value < bounds["lt"])
    if "term" in clause:
        field, value = next(iter(clause["term"].items()))
        value = value.get("value") if isinstance(value, dict) else value
        return _get(doc, field) == value
    if "match" in clause:
        field, value = next(iter(clause["match"].items()))
        value = value.get("query") if isinstance(value, dict) else value
        return str(value).lower() in str(_get(doc, field) or "").lower()
    if "wildcard" in clause:
        field, value = next(iter(clause["wildcard"].items()))
        value = value.get("value") if isinstance(value, dict) else value
        return fnmatch.fnmatch(str(_get(doc, field) or ""), value)
    if "simple_query_string" in clause or "query_string" in clause:
        q = (clause.get("simple_query_string") or clause["query_string"])["query"]
        return all(word.lower() in json.dumps(doc).lower() for word in q.split())
    if "exists" in clause:
        return _get(doc, clause["exists"]["field"]) is not None
    return True


def _interval_seconds(text: str) -> int:
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    return int(text[:-1]) * units[text[-1]]


def _run_aggs(docs: list[dict], aggs: dict) -> dict:
    """Compute the supported aggregation subset."""
    out: dict = {}
    for name, spec in aggs.items():
        sub = spec.get("aggs") or spec.get("aggregations") or {}
        if "date_histogram" in spec:
            dh = spec["date_histogram"]
            step = _interval_seconds(dh.get("fixed_interval") or dh.get("calendar_interval") or "1m")
            buckets: dict[int, list[dict]] = {}
            for d in docs:
                ts = datetime.strptime(d["@timestamp"], "%Y-%m-%dT%H:%M:%S.000Z").replace(tzinfo=timezone.utc)
                key = int(ts.timestamp()) // step * step
                buckets.setdefault(key, []).append(d)
            out[name] = {"buckets": [
                {"key": k * 1000, "key_as_string": datetime.fromtimestamp(k, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                 "doc_count": len(v), **_run_aggs(v, sub)} for k, v in sorted(buckets.items())]}
        elif "terms" in spec:
            field = spec["terms"]["field"]
            counts: dict[str, int] = {}
            for d in docs:
                value = _get(d, field)
                if value is not None:
                    counts[str(value)] = counts.get(str(value), 0) + 1
            top = sorted(counts.items(), key=lambda kv: -kv[1])[: spec["terms"].get("size", 10)]
            out[name] = {"buckets": [{"key": k, "doc_count": n} for k, n in top]}
        elif "avg" in spec or "max" in spec:
            kind = "avg" if "avg" in spec else "max"
            values = [v for v in (_get(d, spec[kind]["field"]) for d in docs) if isinstance(v, (int, float))]
            out[name] = {"value": (sum(values) / len(values) if kind == "avg" else max(values)) if values else None}
        elif "filter" in spec:
            kept = [d for d in docs if _matches(d, spec["filter"])]
            out[name] = {"doc_count": len(kept), **_run_aggs(kept, sub)}
        elif "value_count" in spec:
            out[name] = {"value": sum(1 for d in docs if _get(d, spec["value_count"]["field"]) is not None)}
    return out


# ------------------------------------------------------------------ server

class ESHandler(BaseHTTPRequestHandler):
    server: "FakeES"

    def log_message(self, *a: Any) -> None:
        pass

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(n) or b"{}")

    def _reply(self, obj: Any, code: int = 200) -> None:
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(obj).encode())

    def _record(self, body: dict | None = None) -> None:
        self.server.requests.append({"method": self.command, "path": self.path,
                                     "headers": dict(self.headers), "body": body})

    def do_GET(self) -> None:
        self._record()
        path = urllib.parse.urlparse(self.path).path
        if path == "/_cluster/health":
            return self._reply({"cluster_name": "fake", "status": self.server.health, "number_of_nodes": 3,
                                "active_shards": 42, "unassigned_shards": 2 if self.server.health != "green" else 0,
                                "active_shards_percent_as_number": 95.0})
        if path.startswith("/_cat/indices"):
            pattern = urllib.parse.unquote(path[len("/_cat/indices/"):]) or "*"
            names = sorted({idx for idx, _ in self.server.docs if any(fnmatch.fnmatch(idx, p) for p in pattern.split(","))})
            return self._reply([{"health": "yellow" if "nginx" in n else "green", "status": "open", "index": n,
                                 "docs.count": str(sum(1 for i, _ in self.server.docs if i == n)),
                                 "store.size": "1.2mb", "pri": "1", "rep": "1"} for n in names])
        if path == "/_cluster/allocation/explain":
            return self._reply({"index": "logs-nginx.error-default", "shard": 0, "primary": False,
                                "current_state": "unassigned",
                                "unassigned_info": {"reason": "INDEX_CREATED"},
                                "can_allocate": "no",
                                "allocate_explanation": "cannot allocate because allocation is not permitted to any of the nodes",
                                "node_allocation_decisions": [{"node_name": "node-2", "deciders": [
                                    {"decider": "disk_threshold", "decision": "NO",
                                     "explanation": "the node is above the high watermark cluster setting [90%]"}]}]})
        return self._reply({"error": f"unsupported GET {path}"}, 404)

    def do_POST(self) -> None:
        body = self._body()
        self._record(body)
        path = urllib.parse.urlparse(self.path).path
        if path.endswith("/_search"):
            pattern = urllib.parse.unquote(path[1:-len("/_search")])
            return self._reply(self._search(pattern, body))
        return self._reply({"error": f"unsupported POST {path}"}, 404)

    def do_DELETE(self) -> None:
        self._record()
        self._reply({"acknowledged": True})

    def _search(self, pattern: str, body: dict) -> dict:
        patterns = pattern.split(",")
        docs = [d for idx, d in self.server.docs if any(fnmatch.fnmatch(idx, p) for p in patterns)]
        docs = [d for d in docs if _matches(d, body.get("query", {}))]
        sort = body.get("sort", [])
        desc = bool(sort) and next(iter(sort[0].values())).get("order", "asc") == "desc" if isinstance(sort[0] if sort else None, dict) else False
        hits = sorted(docs, key=lambda d: d["@timestamp"], reverse=desc)[: body.get("size", 10)]
        result = {"took": 1, "hits": {"total": {"value": len(docs), "relation": "eq"},
                                      "hits": [{"_index": "fake", "_source": h} for h in hits]}}
        if body.get("aggs") or body.get("aggregations"):
            result["aggregations"] = _run_aggs(docs, body.get("aggs") or body["aggregations"])
        return result


class FakeES(ThreadingHTTPServer):
    def __init__(self, port: int = 0, health: str = "yellow", docs: list[tuple[str, dict]] | None = None):
        super().__init__(("127.0.0.1", port), ESHandler)
        self.requests: list[dict] = []
        self.health = health
        self.docs = docs if docs is not None else build_dataset()
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"

    def start(self) -> "FakeES":
        self._thread = threading.Thread(target=self.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self.shutdown()
        self.server_close()

    def __enter__(self):
        return self.start()

    def __exit__(self, *a):
        self.stop()


def main() -> None:
    ap = argparse.ArgumentParser(description="fake Elasticsearch with a canned incident")
    ap.add_argument("--port", type=int, default=9200)
    ap.add_argument("--health", default="yellow")
    args = ap.parse_args()
    server = FakeES(port=args.port, health=args.health)
    print(f"fake Elasticsearch on {server.url}  ({len(server.docs)} docs, incident 10:15-10:20 UTC on 2026-09-02)")
    server.serve_forever()


if __name__ == "__main__":
    main()
