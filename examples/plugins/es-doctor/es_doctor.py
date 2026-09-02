"""es-doctor - an Elasticsearch / Elastic Stack diagnostics plugin.

What the model gets
-------------------
Tools
  es_cluster_health   status, shard counts, and (when not green) the allocation explanation
  es_indices          indices grouped by signal: logs / metrics / traces / other
  es_logs             dig through logs (filebeat, Elastic Agent logs-*) by service, host, level,
                      free text and time window, with a dataset breakdown and optional timeline
  es_metrics          time-bucketed averages of a metric (metricbeat / metrics-*), with friendly aliases
  es_correlate        one table aligning error counts, metrics and APM latency/failures per time
                      bucket, Pearson correlation of errors vs each metric, spike detection, and
                      the top error messages inside the spike
  es_search           raw query DSL passthrough for anything the helpers don't cover
  es_request          raw REST call; destructive ones are blocked unless ``allow_destructive = true``
  es_shards, es_recovery, es_nodes, es_hot_threads, es_ilm, es_snapshots, es_index_inspect,
  es_templates, es_slowlog - the cluster-administration half, in ``es_admin.py``
Skills
  es-triage, es-log-dig, es-correlate and six administration runbooks - see ``skills/``
Command
  /es                 quick cluster summary for the human

Everything talks plain HTTP with ``urllib`` - no elasticsearch client library needed.

Configuration (``[plugins.es-doctor]`` or env vars)::

    url = "https://es.example.com:9200"     # ELASTICSEARCH_URL
    api_key = "base64=="                      # ELASTICSEARCH_API_KEY  (sent as `Authorization: ApiKey`)
    username = "elastic"; password = "..."    # or basic auth
    ca_cert = "/etc/pki/es-ca.pem"            # trust this CA (a self-signed cluster's
                                              # secure answer; prefer over verify_tls=false)
    verify_tls = true                         # false disables cert AND hostname checks
    allow_destructive = false
    logs_index = "logs-*,filebeat-*"          # override the default patterns if your naming differs
    metrics_index = "metrics-*,metricbeat-*"
    traces_index = "traces-apm*,apm-*"
"""
from __future__ import annotations

import json
import math
import os
import re
import urllib.parse
from typing import Any

from es_client import (DEFAULT_LOGS_INDEX, DEFAULT_METRICS_INDEX, DEFAULT_TRACES_INDEX,
                       ESClient, ESError, Settings, _ESTool, result, text_table)

# ------------------------------------------------------------------ Elastic knowledge
# Beats and Elastic Agent write ECS documents into these data streams; the default patterns
# live in es_client.py, so a user with legacy indices overrides three config keys.

#: Friendly names -> ECS / Beats metric fields.
METRIC_ALIASES = {
    "cpu": "system.cpu.total.norm.pct",
    "memory": "system.memory.actual.used.pct",
    "load": "system.load.norm.5",
    "disk": "system.filesystem.used.pct",
    "net_in": "host.network.ingress.bytes",
    "net_out": "host.network.egress.bytes",
    "container_cpu": "docker.cpu.total.pct",
    "container_memory": "docker.memory.usage.pct",
    "k8s_cpu": "kubernetes.pod.cpu.usage.node.pct",
    "k8s_memory": "kubernetes.pod.memory.usage.node.pct",
    "jvm_heap": "jolokia.jvm.memory.heap.used.pct",
}
ERROR_LEVELS = ["error", "err", "fatal", "critical", "crit", "emerg", "alert", "panic"]
DESTRUCTIVE = re.compile(r"(_delete_by_query|_close|_shrink|_forcemerge|_reindex|_update_by_query|/_settings|_ilm)", re.I)

PROMPT_NOTE = """# Elasticsearch / Elastic Stack
You have es_* tools. Data from Beats and Elastic Agent follows ECS (Elastic Common Schema):
- data streams: logs-*, metrics-*, traces-apm* (Elastic Agent); filebeat-*, metricbeat-*, packetbeat-*,
  heartbeat-*, auditbeat-*, winlogbeat-* (standalone Beats)
- key fields: @timestamp, host.name, service.name, log.level, message, event.dataset, data_stream.dataset,
  agent.type, container.id, kubernetes.pod.name, trace.id, transaction.id, http.response.status_code,
  event.duration, event.outcome, error.message, url.path
- metricbeat: system.cpu.total.norm.pct, system.memory.actual.used.pct, system.load.norm.5,
  system.filesystem.used.pct, docker.*, kubernetes.*; APM: transaction.duration.us, transaction.name
Workflow for "why is X broken": es_cluster_health -> es_logs (errors, narrow time window) ->
es_correlate (errors vs cpu/memory/latency, same window, same host/service) -> read matching skill."""


# ------------------------------------------------------------------ query helpers

_DURATION = re.compile(r"^\d+[smhd]$")


def time_bound(value: str | None, default: str) -> str:
    """Accept ISO timestamps, ES date-math (``now-1h``) or bare durations (``15m`` -> ``now-15m``)."""
    if not value:
        return default
    return f"now-{value}" if _DURATION.match(value) else value


def time_filters(since: str | None, until: str | None) -> list[dict]:
    return [{"range": {"@timestamp": {"gte": time_bound(since, "now-1h"), "lte": time_bound(until, "now")}}}]


def entity_filters(host: str | None = None, service: str | None = None, container: str | None = None) -> list[dict]:
    """Filters on the ECS fields that identify *what* we're looking at."""
    filters = []
    if host:
        filters.append({"term": {"host.name": host}})
    if service:
        filters.append({"term": {"service.name": service}})
    if container:
        filters.append({"bool": {"should": [{"term": {"container.id": container}},
                                             {"term": {"kubernetes.pod.name": container}}], "minimum_should_match": 1}})
    return filters


def level_filter(level: str | None) -> list[dict]:
    """``level="error"`` matches all the ways Beats spell an error level."""
    if not level:
        return []
    levels = ERROR_LEVELS if level.lower() == "error" else [level.lower(), level.upper(), level.capitalize()]
    return [{"bool": {"should": [{"term": {"log.level": lv}} for lv in levels], "minimum_should_match": 1}}]


def metric_field(name: str) -> str:
    return METRIC_ALIASES.get(name, name)


def metric_label(field: str) -> str:
    """Short column header: the alias if there is one, else the last two path segments."""
    for alias, ecs in METRIC_ALIASES.items():
        if ecs == field:
            return alias
    return ".".join(field.split(".")[-2:])


def bucket_label(bucket: dict) -> str:
    return bucket.get("key_as_string", str(bucket.get("key")))[:16].replace("T", " ")


def pearson(xs: list[float], ys: list[float]) -> float | None:
    """Correlation coefficient, or ``None`` when either series is constant."""
    pairs = [(x, y) for x, y in zip(xs, ys) if x is not None and y is not None]
    if len(pairs) < 3:
        return None
    mx = sum(p[0] for p in pairs) / len(pairs)
    my = sum(p[1] for p in pairs) / len(pairs)
    sxx = sum((x - mx) ** 2 for x, _ in pairs)
    syy = sum((y - my) ** 2 for _, y in pairs)
    if sxx == 0 or syy == 0:
        return None
    return sum((x - mx) * (y - my) for x, y in pairs) / math.sqrt(sxx * syy)


def spike_indices(values: list[float], sigma: float = 2.0) -> list[int]:
    """Indices where the value exceeds mean + ``sigma`` standard deviations."""
    clean = [v for v in values if v is not None]
    if len(clean) < 4:
        return []
    mean = sum(clean) / len(clean)
    std = math.sqrt(sum((v - mean) ** 2 for v in clean) / len(clean))
    if std == 0:
        return []
    return [i for i, v in enumerate(values) if v is not None and v > mean + sigma * std]


def fmt(value: float | None, digits: int = 3) -> str:
    return "-" if value is None else (f"{value:.{digits}f}" if isinstance(value, float) else str(value))


# ------------------------------------------------------------------ tools

class ClusterHealthTool(_ESTool):
    name = "es_cluster_health"
    description = "Elasticsearch cluster status, node/shard counts; includes allocation explanation when not green."
    parameters = {"type": "object", "properties": {}}

    def run(self, args, ctx):
        health = self.es.request("GET", "/_cluster/health")
        lines = [f"cluster {health.get('cluster_name')}: status {health.get('status')}",
                 f"nodes: {health.get('number_of_nodes')}  active shards: {health.get('active_shards')}  "
                 f"unassigned: {health.get('unassigned_shards')}  active%: {health.get('active_shards_percent_as_number')}"]
        if health.get("status") != "green":
            try:
                explain = self.es.request("GET", "/_cluster/allocation/explain")
                lines.append(f"\nallocation explain for {explain.get('index')} shard {explain.get('shard')}: "
                             f"{explain.get('allocate_explanation') or explain.get('current_state')}")
                for node in explain.get("node_allocation_decisions", []):
                    for decider in node.get("deciders", []):
                        lines.append(f"  {node.get('node_name')}: [{decider.get('decider')}] {decider.get('decision')} - {decider.get('explanation')}")
            except ESError as exc:
                lines.append(f"(allocation explain unavailable: {exc})")
        return result(ctx, "\n".join(lines), health=health)


class IndicesTool(_ESTool):
    name = "es_indices"
    description = "List indices/data streams grouped by signal (logs, metrics, traces, other) with health and doc counts."
    parameters = {"type": "object", "properties": {"pattern": {"type": "string", "description": "e.g. logs-* (default all)"}}}

    def run(self, args, ctx):
        pattern = args.get("pattern") or "*"
        rows = self.es.request("GET", f"/_cat/indices/{urllib.parse.quote(pattern, safe='*,-.')}?format=json&s=index")
        groups: dict[str, list[str]] = {"logs": [], "metrics": [], "traces": [], "other": []}
        for row in rows:
            name = row["index"]
            signal = next((s for s in ("logs", "metrics", "traces") if name.startswith((f"{s}-", f".ds-{s}-"))), None)
            if signal is None and any(name.startswith(b) for b in ("filebeat", "winlogbeat", "auditbeat")):
                signal = "logs"
            elif signal is None and any(name.startswith(b) for b in ("metricbeat", "packetbeat", "heartbeat")):
                signal = "metrics"
            elif signal is None and name.startswith("apm"):
                signal = "traces"
            groups[signal or "other"].append(f"  {row.get('health', '?'):7} {name:45} docs={row.get('docs.count', '?'):>8} size={row.get('store.size', '?')}")
        text = "\n".join(f"{signal} ({len(rows)}):\n" + "\n".join(rows) for signal, rows in groups.items() if rows)
        return result(ctx, text or "(no indices)")


class LogsTool(_ESTool):
    name = "es_logs"
    description = ("Search logs (Elastic Agent logs-*, filebeat-*). Filters: service, host, container, level "
                   "(default error), free-text query, time window (since/until: ISO, now-1h, or 15m). "
                   "Returns matching lines newest-last plus a breakdown by event.dataset; histogram=true adds a timeline.")
    parameters = {"type": "object", "properties": {
        "query": {"type": "string", "description": "free text (simple_query_string)"},
        "service": {"type": "string"}, "host": {"type": "string"}, "container": {"type": "string"},
        "level": {"type": "string", "description": "error (default) | warn | info | any"},
        "since": {"type": "string"}, "until": {"type": "string"},
        "size": {"type": "integer", "description": "max lines (default 40)"},
        "index": {"type": "string"}, "histogram": {"type": "boolean"}, "interval": {"type": "string", "description": "e.g. 5m"}}}

    def run(self, args, ctx):
        level = args.get("level", "error")
        filters = time_filters(args.get("since"), args.get("until")) + entity_filters(args.get("host"), args.get("service"), args.get("container"))
        if level and level.lower() != "any":
            filters += level_filter(level)
        if args.get("query"):
            filters.append({"simple_query_string": {"query": args["query"], "default_operator": "and"}})
        body: dict[str, Any] = {"size": int(args.get("size") or 40), "query": {"bool": {"filter": filters}},
                                "sort": [{"@timestamp": {"order": "desc"}}],
                                "_source": ["@timestamp", "log.level", "service.name", "host.name", "message", "error.message", "event.dataset"],
                                "aggs": {"datasets": {"terms": {"field": "event.dataset", "size": 10}}}}
        if args.get("histogram"):
            body["aggs"]["timeline"] = {"date_histogram": {"field": "@timestamp", "fixed_interval": args.get("interval") or "5m"}}
        data = self.es.search(args.get("index") or self.settings.logs_index, body)

        hits = list(reversed(data["hits"]["hits"]))
        total = data["hits"]["total"]["value"] if isinstance(data["hits"]["total"], dict) else data["hits"]["total"]
        lines = [f"{total} matching log events; showing {len(hits)} (oldest first)"]
        for hit in hits:
            src = hit["_source"]
            msg = src.get("message") or (src.get("error") or {}).get("message") or json.dumps(src)[:200]
            lines.append(f"{src.get('@timestamp', '')[:19]} {str((src.get('log') or {}).get('level', '')):5} "
                         f"{(src.get('service') or {}).get('name', '-')}@{(src.get('host') or {}).get('name', '-')}  {msg[:300]}")
        aggs = data.get("aggregations", {})
        if aggs.get("datasets", {}).get("buckets"):
            lines.append("\nBy dataset: " + ", ".join(f"{b['key']}={b['doc_count']}" for b in aggs["datasets"]["buckets"]))
        if aggs.get("timeline"):
            lines.append("\nTimeline:")
            lines += [f"  {bucket_label(b)}  {b['doc_count']:>6}  {'#' * min(60, b['doc_count'])}"
                      for b in aggs["timeline"]["buckets"] if b["doc_count"]]
        return result(ctx, "\n".join(lines), total=total)


class MetricsTool(_ESTool):
    name = "es_metrics"
    description = ("Time-bucketed average of a metric from metricbeat / metrics-*. field accepts aliases: "
                   + ", ".join(METRIC_ALIASES) + " - or any ECS field. Filter by host/service/container.")
    parameters = {"type": "object", "properties": {
        "field": {"type": "string"}, "host": {"type": "string"}, "service": {"type": "string"}, "container": {"type": "string"},
        "since": {"type": "string"}, "until": {"type": "string"}, "interval": {"type": "string", "description": "default 1m"},
        "index": {"type": "string"}}, "required": ["field"]}

    def run(self, args, ctx):
        field = metric_field(args["field"])
        body = {"size": 0, "query": {"bool": {"filter": time_filters(args.get("since"), args.get("until"))
                                              + entity_filters(args.get("host"), args.get("service"), args.get("container"))
                                              + [{"exists": {"field": field}}]}},
                "aggs": {"over_time": {"date_histogram": {"field": "@timestamp", "fixed_interval": args.get("interval") or "1m"},
                                       "aggs": {"value": {"avg": {"field": field}}, "peak": {"max": {"field": field}}}}}}
        data = self.es.search(args.get("index") or self.settings.metrics_index, body)
        buckets = data.get("aggregations", {}).get("over_time", {}).get("buckets", [])
        lines = [f"{field}  (avg per {args.get('interval') or '1m'}, {len(buckets)} buckets)"]
        lines += [f"{bucket_label(b)}  avg={fmt(b['value']['value'])}  max={fmt(b['peak']['value'])}" for b in buckets]
        if not buckets:
            lines.append("no data - check the field name (try es_search with size=1 on the metrics index) or the time window")
        return result(ctx, "\n".join(lines), field=field)


class CorrelateTool(_ESTool):
    name = "es_correlate"
    description = ("Correlate log errors with metrics and APM over a time window: one row per bucket with error count, "
                   "total logs, each metric's avg, APM p50 latency and failure count; Pearson r of errors vs each series; "
                   "spike buckets; top error messages inside the spike. metrics: aliases or ECS fields (default cpu, memory).")
    parameters = {"type": "object", "properties": {
        "since": {"type": "string"}, "until": {"type": "string"}, "interval": {"type": "string", "description": "default 1m"},
        "host": {"type": "string"}, "service": {"type": "string"}, "container": {"type": "string"},
        "metrics": {"type": "array", "items": {"type": "string"}},
        "include_apm": {"type": "boolean", "description": "default true"},
        "logs_index": {"type": "string"}, "metrics_index": {"type": "string"}, "traces_index": {"type": "string"}}}

    def run(self, args, ctx):
        interval = args.get("interval") or "1m"
        window = time_filters(args.get("since"), args.get("until"))
        who = entity_filters(args.get("host"), args.get("service"), args.get("container"))
        metrics = [metric_field(m) for m in (args.get("metrics") or ["cpu", "memory"])]

        errors_by_bucket, total_by_bucket = self._log_series(args, window, who, interval)
        metric_series = {m: self._metric_series(args, window, entity_filters(args.get("host"), None, args.get("container")), interval, m) for m in metrics}
        apm = self._apm_series(args, window, who, interval) if args.get("include_apm", True) else None

        keys = sorted(set(total_by_bucket) | {k for s in metric_series.values() for k in s} | set(apm["p50"] if apm else []))
        if not keys:
            return result(ctx, "no data in window; widen since/until or drop host/service filters", is_error=True)

        errors = [errors_by_bucket.get(k, 0) for k in keys]
        header = ["bucket", "errors", "logs"] + [metric_label(m) for m in metrics]
        header += ["apm_p50_ms", "apm_fail"] if apm else []
        rows = []
        for i, k in enumerate(keys):
            row = [k[:16].replace("T", " "), errors[i], total_by_bucket.get(k, 0)]
            row += [fmt(metric_series[m].get(k)) for m in metrics]
            if apm:
                p50 = apm["p50"].get(k)
                row += [fmt(p50 / 1000, 0) if p50 else "-", apm["fail"].get(k, 0)]
            rows.append(row)

        lines = [f"errors vs metrics per {interval}, {len(keys)} buckets", text_table(header, rows), "", "Correlation of error count with:"]
        for m in metrics:
            r = pearson(errors, [metric_series[m].get(k) for k in keys])
            lines.append(f"  {m:40} r={fmt(r, 2)} {_strength(r)}")
        if apm:
            r_lat = pearson(errors, [apm['p50'].get(k) for k in keys])
            r_fail = pearson(errors, [apm['fail'].get(k, 0) for k in keys])
            lines.append(f"  {'apm transaction.duration.us (p50)':40} r={fmt(r_lat, 2)} {_strength(r_lat)}")
            lines.append(f"  {'apm event.outcome=failure count':40} r={fmt(r_fail, 2)} {_strength(r_fail)}")

        spikes = spike_indices(errors)
        if spikes:
            first, last = keys[spikes[0]], keys[spikes[-1]]
            lines.append(f"\nError spike: {len(spikes)} bucket(s) from {first[:16]} to {last[:16]} (>2σ above mean)")
            lines.append("Top error messages during the spike:")
            for msg, count in self._top_errors(args, first, last, who):
                lines.append(f"  {count:>5}  {msg[:160]}")
        else:
            lines.append("\nNo error spike (>2σ) detected in this window.")
        return result(ctx, "\n".join(lines), buckets=len(keys), spike_buckets=len(spikes))

    # ---- the three series -------------------------------------------------
    def _log_series(self, args, window, who, interval) -> tuple[dict[str, int], dict[str, int]]:
        body = {"size": 0, "query": {"bool": {"filter": window + who}},
                "aggs": {"t": {"date_histogram": {"field": "@timestamp", "fixed_interval": interval},
                               "aggs": {"errors": {"filter": level_filter("error")[0]}}}}}
        buckets = self.es.search(args.get("logs_index") or self.settings.logs_index, body).get("aggregations", {}).get("t", {}).get("buckets", [])
        return ({b["key_as_string"]: b["errors"]["doc_count"] for b in buckets}, {b["key_as_string"]: b["doc_count"] for b in buckets})

    def _metric_series(self, args, window, who, interval, field) -> dict[str, float]:
        body = {"size": 0, "query": {"bool": {"filter": window + who + [{"exists": {"field": field}}]}},
                "aggs": {"t": {"date_histogram": {"field": "@timestamp", "fixed_interval": interval}, "aggs": {"v": {"avg": {"field": field}}}}}}
        buckets = self.es.search(args.get("metrics_index") or self.settings.metrics_index, body).get("aggregations", {}).get("t", {}).get("buckets", [])
        return {b["key_as_string"]: b["v"]["value"] for b in buckets if b["v"]["value"] is not None}

    def _apm_series(self, args, window, who, interval) -> dict[str, dict]:
        body = {"size": 0, "query": {"bool": {"filter": window + who + [{"exists": {"field": "transaction.duration.us"}}]}},
                "aggs": {"t": {"date_histogram": {"field": "@timestamp", "fixed_interval": interval},
                               "aggs": {"p50": {"avg": {"field": "transaction.duration.us"}},
                                        "fail": {"filter": {"term": {"event.outcome": "failure"}}}}}}}
        try:
            buckets = self.es.search(args.get("traces_index") or self.settings.traces_index, body).get("aggregations", {}).get("t", {}).get("buckets", [])
        except ESError:
            return {"p50": {}, "fail": {}}
        return {"p50": {b["key_as_string"]: b["p50"]["value"] for b in buckets if b["p50"]["value"]},
                "fail": {b["key_as_string"]: b["fail"]["doc_count"] for b in buckets}}

    def _top_errors(self, args, since, until, who) -> list[tuple[str, int]]:
        body = {"size": 200, "_source": ["message", "error.message"],
                "query": {"bool": {"filter": [{"range": {"@timestamp": {"gte": since, "lte": until}}}] + who + level_filter("error")}}}
        counts: dict[str, int] = {}
        for hit in self.es.search(args.get("logs_index") or self.settings.logs_index, body)["hits"]["hits"]:
            src = hit["_source"]
            msg = re.sub(r"\d+", "N", src.get("message") or (src.get("error") or {}).get("message") or "")   # collapse ids/numbers
            counts[msg] = counts.get(msg, 0) + 1
        return sorted(counts.items(), key=lambda kv: -kv[1])[:5]


def _strength(r: float | None) -> str:
    if r is None:
        return "(no variation)"
    a = abs(r)
    label = "strong" if a >= 0.7 else "moderate" if a >= 0.4 else "weak"
    return f"{label} {'positive' if r > 0 else 'negative'}"


class SearchTool(_ESTool):
    name = "es_search"
    description = "Raw Elasticsearch query DSL: POST /<index>/_search with the given body. Use for anything es_logs/es_metrics can't express."
    parameters = {"type": "object", "properties": {"index": {"type": "string"}, "body": {"type": "object"}}, "required": ["index", "body"]}

    def run(self, args, ctx):
        data = self.es.search(args["index"], args["body"])
        return result(ctx, json.dumps(data, indent=1))


class RequestTool(_ESTool):
    name = "es_request"
    description = "Raw REST call to Elasticsearch (GET/POST/PUT/DELETE + path + optional JSON body). Destructive calls are blocked unless configured."
    parameters = {"type": "object", "properties": {"method": {"type": "string"}, "path": {"type": "string"}, "body": {"type": "object"}},
                  "required": ["method", "path"]}

    def run(self, args, ctx):
        data = self.es.request(args["method"].upper(), args["path"], args.get("body"))
        return result(ctx, json.dumps(data, indent=1) if not isinstance(data, str) else data)


def is_destructive(method: str, path: str) -> bool:
    return method.upper() in ("DELETE",) or bool(DESTRUCTIVE.search(path))


# ------------------------------------------------------------------ registration

def register(api):
    import es_admin       # sibling module; the loader puts the plugin root on sys.path

    cfg = api.plugin_config()
    es = ESClient(url=cfg.get("url") or os.environ.get("ELASTICSEARCH_URL", "http://localhost:9200"),
                  api_key=cfg.get("api_key") or os.environ.get("ELASTICSEARCH_API_KEY", ""),
                  username=cfg.get("username", ""), password=cfg.get("password", ""),
                  verify_tls=cfg.get("verify_tls", True),
                  ca_cert=cfg.get("ca_cert", ""))
    settings = Settings(logs_index=cfg.get("logs_index", DEFAULT_LOGS_INDEX), metrics_index=cfg.get("metrics_index", DEFAULT_METRICS_INDEX),
                        traces_index=cfg.get("traces_index", DEFAULT_TRACES_INDEX), allow_destructive=bool(cfg.get("allow_destructive", False)))

    for tool_class in (ClusterHealthTool, IndicesTool, LogsTool, MetricsTool, CorrelateTool, SearchTool, RequestTool):
        api.register_tool(tool_class(es, settings))
    for tool_class in es_admin.TOOL_CLASSES:
        api.register_tool(tool_class(es, settings))
    api.register_system_prompt_section("es-doctor", lambda: PROMPT_NOTE + "\n" + es_admin.ES_ADMIN_PROMPT_NOTE)

    async def guard(event, rt):
        """Block destructive es_request calls unless the user opted in."""
        if event["name"] == "es_request" and not settings.allow_destructive \
                and is_destructive(event["args"].get("method", "GET"), event["args"].get("path", "")):
            return {"block": True, "reason": "destructive Elasticsearch call; set allow_destructive = true in [plugins.es-doctor] to permit"}
        return None
    api.on("tool_call", guard)

    async def es_command(args, rt):
        try:
            health = es.request("GET", "/_cluster/health")
            return f"{es.url}: {health.get('status')} - {health.get('number_of_nodes')} nodes, {health.get('unassigned_shards')} unassigned shards"
        except ESError as exc:
            return str(exc)
    api.register_command("es", es_command, "Elasticsearch cluster summary")
