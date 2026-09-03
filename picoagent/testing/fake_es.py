"""A tiny fake Elasticsearch for tests and offline demos. Standard library only.

It holds a deterministic 30-minute incident in memory - a checkout service whose
error rate, CPU and request latency all spike between minute 15 and 20 - spread
across Elastic Agent / Beats data streams:

    logs-checkout-default        filebeat-style app logs (ECS: log.level, message, service.name)
    logs-nginx.error-default     web-server error log
    metrics-system.cpu-default   metricbeat system.cpu.total.norm.pct
    metrics-system.memory-default metricbeat system.memory.actual.used.pct
    traces-apm-default           APM transactions (transaction.duration.us, event.outcome)

Next to the incident it holds a deterministic *cluster* (``build_cluster``) for the
administration tools: three nodes with one under memory and disk pressure, two unassigned
shards with different reasons, a relocating shard, an ILM policy stuck in ERROR, three
snapshots in three states, templates and a data stream. Every response shape here was
copied from the Elasticsearch reference pages listed in ``build_cluster``'s docstring; the
point of a fake is to be wrong in the same places production is, so invented field names
would defeat it.

Supported endpoints (enough for the es-doctor plugin):
    GET  /_cluster/health                 GET  /_cat/indices?format=json
    GET  /_cluster/allocation/explain     POST /<pattern>/_search
    GET  /_cat/{shards,recovery,nodes,thread_pool}   GET /_nodes/[<node>/]stats[/<metrics>]
    GET  /_nodes/[<node>/]hot_threads (text)         GET /_tasks, /_cluster/pending_tasks
    GET  /_ilm/status, /<index>/_ilm/explain, /_ilm/policy/<name>
    GET  /_snapshot[/<repo>[/<snap>[/_status]]]      GET /_slm/{policy,stats}
    GET  /<index>/_{settings,mapping,stats}          PUT  /<index>/_settings
    GET  /_index_template, /_component_template, /_template, /_data_stream
    POST /_index_template/_simulate_index/<name>, /_snapshot/<repo>/_verify
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


# ------------------------------------------------------------------ cluster state

#: Leaf fields of the canned ``logs-checkout-default`` mapping, as dotted ECS paths. Kept as a
#: flat list because the interesting number - how close the index is to its field limit - is a
#: count, and a list is the only shape where that count is obvious to a reader.
MAPPING_FIELDS: list[tuple[str, str]] = [
    ("@timestamp", "date"), ("message", "text"),
    ("log.level", "keyword"), ("log.logger", "keyword"),
    ("log.origin.file.name", "keyword"), ("log.origin.file.line", "long"),
    ("service.name", "keyword"), ("service.version", "keyword"), ("service.environment", "keyword"),
    ("host.name", "keyword"), ("host.ip", "ip"), ("host.os.name", "keyword"),
    ("host.os.version", "keyword"), ("host.architecture", "keyword"),
    ("event.dataset", "keyword"), ("event.module", "keyword"), ("event.outcome", "keyword"),
    ("event.duration", "long"), ("event.category", "keyword"), ("event.action", "keyword"),
    ("error.message", "text"), ("error.type", "keyword"), ("error.stack_trace", "text"),
    ("error.id", "keyword"),
    ("trace.id", "keyword"), ("transaction.id", "keyword"), ("span.id", "keyword"),
    ("http.request.method", "keyword"), ("http.response.status_code", "long"),
    ("http.response.body.bytes", "long"),
    ("url.path", "keyword"), ("url.query", "keyword"), ("url.domain", "keyword"),
    ("user.name", "keyword"), ("user.id", "keyword"),
    ("container.id", "keyword"), ("kubernetes.pod.name", "keyword"), ("kubernetes.namespace", "keyword"),
    ("agent.type", "keyword"), ("agent.version", "keyword"), ("data_stream.dataset", "keyword"),
]

HOT_THREADS_TEXT = """::: {node-1}{fUOnV3xmQmuY9dnEHJb2rQ}{127.0.0.1}{127.0.0.1:9300}{m}
   Hot threads at 2026-09-02T10:20:00.512Z, interval=500ms, busiestThreads=3, ignoreIdleThreads=true:

    0.4% [cpu=0.4%, other=0.0%] (2ms out of 500ms) cpu usage by thread 'elasticsearch[node-1][masterService#updateTask][T#1]'
     10/10 snapshots sharing following 2 elements
       java.base@21/java.lang.Thread.run(Thread.java:1583)

::: {node-2}{h1yPzZ1nRJ-GCPnh1p1Q5w}{127.0.0.1}{127.0.0.1:9301}{d}
   Hot threads at 2026-09-02T10:20:00.512Z, interval=500ms, busiestThreads=3, ignoreIdleThreads=true:

   91.3% [cpu=91.3%, other=0.0%] (456.5ms out of 500ms) cpu usage by thread 'elasticsearch[node-2][search][T#3]'
     10/10 snapshots sharing following 18 elements
       app//org.apache.lucene.search.PointRangeQuery$1$1.intersect(PointRangeQuery.java:214)
       app//org.apache.lucene.codecs.lucene90.LuceneBKDReader.intersect(LuceneBKDReader.java:398)
       app//org.elasticsearch.search.query.QueryPhase.executeQuery(QueryPhase.java:154)

   74.8% [cpu=71.2%, other=3.6%] (374ms out of 500ms) cpu usage by thread 'elasticsearch[node-2][write][T#2]'
     8/10 snapshots sharing following 12 elements
       app//org.elasticsearch.index.engine.InternalEngine.index(InternalEngine.java:952)

::: {node-3}{Rk8QyfmvT3ib0hVsY1Wp2A}{127.0.0.1}{127.0.0.1:9302}{d}
   Hot threads at 2026-09-02T10:20:00.512Z, interval=500ms, busiestThreads=3, ignoreIdleThreads=true:

    2.1% [cpu=2.1%, other=0.0%] (10.5ms out of 500ms) cpu usage by thread 'elasticsearch[node-3][refresh][T#1]'
     4/10 snapshots sharing following 6 elements
       app//org.elasticsearch.index.shard.IndexShard.refresh(IndexShard.java:1206)
"""

#: cat APIs return every value as a JSON *string*, including numbers - see the example
#: responses on the cat pages. Tools therefore have to parse; the fake keeps the quirk.
_NODE_IDS = {"node-1": "fUOnV3xmQmuY9dnEHJb2rQ", "node-2": "h1yPzZ1nRJ-GCPnh1p1Q5w",
             "node-3": "Rk8QyfmvT3ib0hVsY1Wp2A"}


def build_cluster() -> dict:
    """The canned administrator's view of the cluster, keyed by endpoint family.

    Response shapes follow the Elasticsearch reference (accessed 2026-09-02):
    cat shards/recovery/nodes/thread_pool ``cat-shards.html``, ``cat-recovery.html``,
    ``cat-nodes.html``, ``cat-thread-pool.html``; ``cluster-allocation-explain.html`` and
    ``docs/troubleshoot/elasticsearch/cluster-allocation-api-examples``;
    ``ilm-explain-lifecycle.html``; ``get-snapshot-api.html`` and
    ``get-snapshot-status-api.html``; ``slm-api-get-policy.html``;
    ``indices-simulate-index.html``; ``indices-get-data-stream.html``; ``tasks.html``;
    ``cluster-pending.html``; ``cluster-nodes-stats.html``.

    The scenario is chosen so every admin tool has something to find: node-2 is under heap,
    disk and breaker pressure and rejecting searches; one replica was never allocated
    (INDEX_CREATED, blocked by the disk decider) and one lost its node (NODE_LEFT); an ILM
    policy is stuck on a rollover alias mismatch; one snapshot is PARTIAL and one is running.
    """
    return {
        "shards": _cat_shards(),
        "recovery": _cat_recovery(),
        "nodes": _cat_nodes(),
        "thread_pools": _cat_thread_pools(),
        "node_stats": _node_stats(),
        "hot_threads": HOT_THREADS_TEXT,
        "tasks": _tasks(),
        "pending_tasks": _pending_tasks(),
        "ilm_status": {"operation_mode": "RUNNING"},
        "ilm_explain": _ilm_explain(),
        "ilm_policies": _ilm_policies(),
        "repositories": {"backups": {"type": "fs", "uuid": "0JLknrXbSUiVPuLakHjBrQ",
                                     "settings": {"location": "/mnt/backups"}}},
        "snapshots": _snapshots(),
        "snapshot_status": _snapshot_status(),
        "slm_policies": _slm_policies(),
        "slm_stats": _slm_stats(),
        "settings": _index_settings(),
        "mappings": _index_mappings(),
        "index_stats": _index_stats(),
        "index_templates": _index_templates(),
        "component_templates": _component_templates(),
        "legacy_templates": _legacy_templates(),
        "simulate_index": _simulate_index(),
        "data_streams": _data_streams(),
        "allocation_explain": _allocation_explain(),
        "verify_repository": {"nodes": {nid: {"name": name} for name, nid in _NODE_IDS.items()}},
    }


def _cat_shards() -> list[dict]:
    """``GET /_cat/shards?format=json&bytes=b&h=...`` - unassigned rows carry empty node/docs."""
    def row(index: str, shard: str, prirep: str, state: str, docs: str, store: str, node: str,
            reason: str = "", at: str = "", details: str = "") -> dict:
        return {"index": index, "shard": shard, "prirep": prirep, "state": state, "docs": docs,
                "store": store, "node": node, "unassigned.reason": reason, "unassigned.at": at,
                "unassigned.details": details}
    return [
        row("logs-checkout-default", "0", "p", "STARTED", "240000", "412000000", "node-1"),
        row("logs-checkout-default", "0", "r", "STARTED", "240000", "412000000", "node-3"),
        row("logs-nginx.error-default", "0", "p", "STARTED", "18000", "24000000", "node-2"),
        row("logs-nginx.error-default", "0", "r", "UNASSIGNED", "", "", "",
            "INDEX_CREATED", "2026-09-02T09:41:03.117Z", ""),
        row("metrics-system.cpu-default", "0", "p", "STARTED", "86400", "51000000", "node-3"),
        row("metrics-system.cpu-default", "0", "r", "UNASSIGNED", "", "", "",
            "NODE_LEFT", "2026-09-02T10:02:44.900Z", "node_left [Ux1cWq0RQ2yGnR7f0oq3Zg]"),
        row("metrics-system.memory-default", "0", "p", "STARTED", "86400", "49000000", "node-2"),
        row("traces-apm-default", "0", "p", "RELOCATING", "512000", "1980000000",
            "node-2 -> 127.0.0.1 Rk8QyfmvT3ib0hVsY1Wp2A node-3"),
    ]


def _cat_recovery() -> list[dict]:
    """``GET /_cat/recovery?format=json&bytes=b&h=...``; the second row is finished."""
    return [
        {"index": "traces-apm-default", "shard": "0", "time": "48.3s", "type": "peer", "stage": "index",
         "source_node": "node-2", "target_node": "node-3", "files_percent": "62.5%",
         "bytes_percent": "40.0%", "translog_ops_percent": "0.0%", "_active": True},
        {"index": "logs-checkout-default", "shard": "0", "time": "13ms", "type": "snapshot", "stage": "done",
         "source_node": "n/a", "target_node": "node-1", "files_percent": "100.0%",
         "bytes_percent": "100.0%", "translog_ops_percent": "100.0%", "_active": False},
    ]


def _cat_nodes() -> list[dict]:
    """``GET /_cat/nodes?format=json&h=...``. node-2 is over the heap and disk thresholds."""
    return [
        {"name": "node-1", "node.role": "hilmrstw", "master": "*", "heap.percent": "61",
         "ram.percent": "72", "cpu": "9", "load_5m": "0.94", "disk.used_percent": "44.20",
         "disk.avail": "112.4gb", "uptime": "6.1d", "version": "8.14.3"},
        {"name": "node-2", "node.role": "dhirstw", "master": "-", "heap.percent": "82",
         "ram.percent": "94", "cpu": "88", "load_5m": "7.31", "disk.used_percent": "91.30",
         "disk.avail": "17.4gb", "uptime": "6.1d", "version": "8.14.3"},
        {"name": "node-3", "node.role": "dhirstw", "master": "-", "heap.percent": "48",
         "ram.percent": "70", "cpu": "21", "load_5m": "1.12", "disk.used_percent": "51.80",
         "disk.avail": "96.1gb", "uptime": "2.4d", "version": "8.14.3"},
    ]


def _cat_thread_pools() -> list[dict]:
    """``GET /_cat/thread_pool/<patterns>?format=json&h=...``; node-2's search pool is rejecting."""
    pools = {"search": ("fixed", "13", "1000"), "write": ("fixed", "8", "10000"),
             "get": ("fixed", "8", "1000"), "management": ("scaling", "5", "-1"),
             "snapshot": ("scaling", "4", "-1"), "force_merge": ("fixed", "1", "-1"),
             "refresh": ("scaling", "4", "-1"), "flush": ("scaling", "4", "-1")}
    rows = []
    for node in ("node-1", "node-2", "node-3"):
        for name, (kind, size, queue_size) in pools.items():
            busy = node == "node-2" and name == "search"
            rows.append({"node_name": node, "name": name, "type": kind, "size": size,
                         "queue_size": queue_size, "active": "6" if busy else "0",
                         "queue": "48" if busy else "0", "rejected": "17" if busy else "0",
                         "completed": "918233" if busy else "51204"})
    return rows


def _node_stats() -> dict:
    """``GET /_nodes/stats/jvm,fs,breaker``: heap, GC counters, disk and circuit breakers."""
    def node(name: str, heap_pct: int, old_count: int, old_ms: int, avail: int, total: int,
             parent_tripped: int, fielddata_tripped: int) -> dict:
        gigabyte = 1024 ** 3
        return {
            "name": name, "roles": ["master"] if name == "node-1" else ["data"],
            "jvm": {"mem": {"heap_used_in_bytes": int(30 * gigabyte * heap_pct / 100),
                            "heap_used_percent": heap_pct, "heap_max_in_bytes": 30 * gigabyte},
                    "gc": {"collectors": {"young": {"collection_count": 90210,
                                                    "collection_time_in_millis": 412_000},
                                          "old": {"collection_count": old_count,
                                                  "collection_time_in_millis": old_ms}}}},
            "fs": {"total": {"total_in_bytes": total, "free_in_bytes": avail,
                             "available_in_bytes": avail}},
            "breakers": {
                "request": {"limit_size_in_bytes": 18 * gigabyte, "limit_size": "18gb",
                            "estimated_size_in_bytes": 0, "estimated_size": "0b",
                            "overhead": 1.0, "tripped": 0},
                "fielddata": {"limit_size_in_bytes": 12 * gigabyte, "limit_size": "12gb",
                              "estimated_size_in_bytes": 8 * gigabyte, "estimated_size": "8gb",
                              "overhead": 1.03, "tripped": fielddata_tripped},
                "inflight_requests": {"limit_size_in_bytes": 30 * gigabyte, "limit_size": "30gb",
                                      "estimated_size_in_bytes": 1024, "estimated_size": "1kb",
                                      "overhead": 2.0, "tripped": 0},
                "parent": {"limit_size_in_bytes": 28 * gigabyte, "limit_size": "28gb",
                           "estimated_size_in_bytes": int(28 * gigabyte * heap_pct / 100),
                           "estimated_size": f"{int(28 * heap_pct / 100)}gb",
                           "overhead": 1.0, "tripped": parent_tripped}},
        }
    terabyte = 1024 ** 4
    return {"cluster_name": "fake", "nodes": {
        _NODE_IDS["node-1"]: node("node-1", 61, 12, 4_100, int(0.56 * terabyte), terabyte, 0, 0),
        _NODE_IDS["node-2"]: node("node-2", 82, 331, 96_400, int(0.087 * terabyte), terabyte, 2, 1),
        _NODE_IDS["node-3"]: node("node-3", 48, 9, 3_050, int(0.48 * terabyte), terabyte, 0, 0)}}


def _tasks() -> dict:
    """``GET /_tasks?detailed=true&group_by=parents`` - keyed by task id, children nested."""
    return {"tasks": {
        f"{_NODE_IDS['node-2']}:88231": {
            "node": _NODE_IDS["node-2"], "id": 88231, "type": "transport",
            "action": "indices:data/read/search",
            "description": "indices[logs-checkout-default], search_type[QUERY_THEN_FETCH], "
                           "source[{\"size\":10000}]",
            "start_time_in_millis": 1_788_000_000_000, "running_time_in_nanos": 400_000_000_000,
            "cancellable": True, "children": []},
        f"{_NODE_IDS['node-1']}:1204": {
            "node": _NODE_IDS["node-1"], "id": 1204, "type": "transport",
            "action": "cluster:monitor/tasks/lists",
            "start_time_in_millis": 1_788_000_399_000, "running_time_in_nanos": 293_139,
            "cancellable": False, "children": []}}}


def _pending_tasks() -> dict:
    """``GET /_cluster/pending_tasks`` - one task has been queued far too long."""
    return {"tasks": [
        {"insert_order": 8811, "priority": "HIGH",
         "source": "shard-started StartedShardEntry{shardId [[traces-apm-default][0]]}",
         "executing": True, "time_in_queue_millis": 45_200, "time_in_queue": "45.2s"},
        {"insert_order": 8812, "priority": "NORMAL",
         "source": "put-mapping [logs-checkout-default]", "executing": False,
         "time_in_queue_millis": 1_200, "time_in_queue": "1.2s"}]}


def _ilm_explain() -> dict:
    """``GET /<index>/_ilm/explain`` - one healthy index, one in ERROR, one unmanaged."""
    phase_execution = {"policy": "logs",
                       "phase_definition": {"min_age": "0ms",
                                            "actions": {"rollover": {"max_age": "30d",
                                                                     "max_primary_shard_size": "50gb"}}},
                       "version": 3, "modified_date": "2026-08-01T09:00:00.000Z",
                       "modified_date_in_millis": 1_785_920_400_000}
    return {"indices": {
        "logs-checkout-default": {
            "index": "logs-checkout-default", "managed": True, "policy": "logs",
            "index_creation_date_millis": 1_787_961_600_000, "time_since_index_creation": "11.4d",
            "lifecycle_date_millis": 1_787_961_600_000, "lifecycle_date": "2026-08-22T00:00:00.000Z",
            "age": "11.4d", "phase": "hot", "phase_time_millis": 1_787_961_600_100,
            "phase_time": "2026-08-22T00:00:00.100Z", "action": "rollover",
            "action_time_millis": 1_787_961_600_100, "action_time": "2026-08-22T00:00:00.100Z",
            "step": "check-rollover-ready", "step_time_millis": 1_787_961_600_100,
            "step_time": "2026-08-22T00:00:00.100Z", "phase_execution": phase_execution},
        "logs-nginx.error-default": {
            "index": "logs-nginx.error-default", "managed": True, "policy": "logs",
            "index_creation_date_millis": 1_787_875_200_000, "time_since_index_creation": "12.4d",
            "lifecycle_date_millis": 1_787_875_200_000, "lifecycle_date": "2026-08-21T00:00:00.000Z",
            "age": "12.4d", "phase": "hot", "phase_time_millis": 1_787_875_200_100,
            "phase_time": "2026-08-21T00:00:00.100Z", "action": "rollover",
            "action_time_millis": 1_787_875_200_100, "action_time": "2026-08-21T00:00:00.100Z",
            "step": "ERROR", "step_time_millis": 1_787_961_000_000,
            "step_time": "2026-08-21T23:50:00.000Z",
            "failed_step": "check-rollover-ready", "is_auto_retryable_error": True,
            "failed_step_retry_count": 5,
            "step_info": {"type": "illegal_argument_exception",
                          "reason": "index.lifecycle.rollover_alias [logs-nginx] does not point to index "
                                    "[logs-nginx.error-default]"},
            "phase_execution": phase_execution},
        "metrics-system.memory-default": {"index": "metrics-system.memory-default", "managed": False}}}


def _ilm_policies() -> dict:
    """``GET /_ilm/policy/<name>``."""
    return {"logs": {"version": 3, "modified_date": "2026-08-01T09:00:00.000Z",
                     "policy": {"phases": {
                         "hot": {"min_age": "0ms",
                                 "actions": {"rollover": {"max_age": "30d", "max_primary_shard_size": "50gb"},
                                             "set_priority": {"priority": 100}}},
                         "warm": {"min_age": "7d",
                                  "actions": {"forcemerge": {"max_num_segments": 1},
                                              "set_priority": {"priority": 50}}},
                         "delete": {"min_age": "90d", "actions": {"delete": {}}}}},
                     "in_use_by": {"indices": ["logs-checkout-default", "logs-nginx.error-default"],
                                   "data_streams": ["logs-checkout-default"],
                                   "composable_templates": ["logs-checkout"]}}}


def _snapshots() -> dict:
    """``GET /_snapshot/<repo>/<pattern>`` - three snapshots, one PARTIAL, one running."""
    def snap(name: str, uuid: str, state: str, start: str, end: str, duration: int,
             failures: list, shards: dict) -> dict:
        return {"snapshot": name, "uuid": uuid, "repository": "backups", "version_id": 8_140_399,
                "version": "8.14.3", "indices": ["logs-checkout-default", "logs-nginx.error-default",
                                                 "metrics-system.cpu-default"],
                "data_streams": ["logs-checkout-default"], "feature_states": [],
                "include_global_state": True, "state": state,
                "start_time": start, "start_time_in_millis": 1_787_961_600_000,
                "end_time": end, "end_time_in_millis": 1_787_961_600_000 + duration,
                "duration_in_millis": duration, "failures": failures, "shards": shards}
    return {"backups": [
        snap("daily-1", "vdRctLCxSketdKb54xw67g", "SUCCESS", "2026-08-31T01:00:00.000Z",
             "2026-08-31T01:04:11.000Z", 251_000, [], {"total": 8, "failed": 0, "successful": 8}),
        snap("daily-2", "lNeQD1SvTQCqqJUMQSwmGg", "PARTIAL", "2026-09-01T01:00:00.000Z",
             "2026-09-01T01:06:40.000Z", 400_000,
             [{"index": "logs-nginx.error-default", "index_uuid": "9Kq2WvJmTb2Kk3Yw0nR4Aw", "shard_id": 0,
               "reason": "IndexShardSnapshotFailedException[Failed to snapshot]; nested: "
                         "NoSuchFileException[/mnt/backups/indices/9Kq2/0/__f1]",
               "node_id": "h1yPzZ1nRJ-GCPnh1p1Q5w", "status": "INTERNAL_SERVER_ERROR"}],
             {"total": 8, "failed": 1, "successful": 7}),
        snap("daily-3", "Q4mYs7hZQtieKgwqrl9tXA", "IN_PROGRESS", "2026-09-02T01:00:00.000Z",
             "1970-01-01T00:00:00.000Z", 0, [], {"total": 8, "failed": 0, "successful": 3})]}


def _snapshot_status() -> dict:
    """``GET /_snapshot/<repo>/<snapshot>/_status`` - only ever asked for the running one."""
    return {"daily-3": {"snapshots": [{
        "snapshot": "daily-3", "repository": "backups", "uuid": "Q4mYs7hZQtieKgwqrl9tXA",
        "state": "STARTED", "include_global_state": True,
        "shards_stats": {"initializing": 0, "started": 2, "finalizing": 0, "done": 3,
                         "failed": 0, "total": 8},
        "stats": {"incremental": {"file_count": 118, "size_in_bytes": 1_204_887_331},
                  "total": {"file_count": 402, "size_in_bytes": 3_918_442_100},
                  "start_time_in_millis": 1_788_048_000_000, "time_in_millis": 214_000},
        "indices": {}}]}}


def _slm_policies() -> dict:
    """``GET /_slm/policy`` - the nightly policy failed last night and succeeded the night before."""
    return {"nightly": {
        "version": 4, "modified_date": "2026-07-14T11:02:31.000Z", "modified_date_millis": 1_784_030_551_000,
        "policy": {"name": "<daily-{now/d}>", "schedule": "0 0 1 * * ?", "repository": "backups",
                   "config": {"indices": ["logs-*", "metrics-*"], "ignore_unavailable": False,
                              "include_global_state": True},
                   "retention": {"expire_after": "30d", "min_count": 5, "max_count": 50}},
        "last_success": {"snapshot_name": "daily-1", "start_time": 1_787_878_800_000,
                         "time": 1_787_879_051_000},
        "last_failure": {"snapshot_name": "daily-2", "time": 1_787_965_600_000,
                         "details": "{\"type\":\"snapshot_exception\",\"reason\":\"[backups:daily-2] Indices "
                                    "don't have primary shards [logs-nginx.error-default]\"}"},
        "next_execution": "2026-09-03T01:00:00.000Z", "next_execution_millis": 1_788_138_000_000,
        "stats": {"policy": "nightly", "snapshots_taken": 41, "snapshots_failed": 3,
                  "snapshots_deleted": 12, "snapshot_deletion_failures": 0}}}


def _slm_stats() -> dict:
    """``GET /_slm/stats``."""
    return {"retention_runs": 31, "retention_failed": 0, "retention_timed_out": 0,
            "retention_deletion_time": "1.4s", "retention_deletion_time_millis": 1_404,
            "total_snapshots_taken": 41, "total_snapshots_failed": 3, "total_snapshots_deleted": 12,
            "total_snapshot_deletion_failures": 0,
            "policy_stats": [{"policy": "nightly", "snapshots_taken": 41, "snapshots_failed": 3,
                              "snapshots_deleted": 12, "snapshot_deletion_failures": 0}]}


def _index_settings() -> dict:
    """``GET /<index>/_settings?flat_settings=true&include_defaults=true``.

    Mutable: ``PUT /<index>/_settings`` writes into the ``settings`` sub-dict, so enabling the
    slow log and then reading it back round-trips the way a real cluster does.
    """
    return {"logs-checkout-default": {
        "settings": {"index.number_of_shards": "1", "index.number_of_replicas": "1",
                     "index.refresh_interval": "1s", "index.lifecycle.name": "logs",
                     "index.lifecycle.rollover_alias": "logs-checkout",
                     "index.codec": "best_compression",
                     "index.provided_name": "logs-checkout-default",
                     "index.uuid": "8pQ3vXfLRlm0nEtQ0nR4Aw",
                     "index.creation_date": "1787961600000",
                     "index.version.created": "8140399"},
        "defaults": {"index.mapping.total_fields.limit": "50", "index.max_result_window": "10000",
                     "index.routing.allocation.include._tier_preference": "data_content",
                     "index.blocks.read_only_allow_delete": "false",
                     "index.search.slowlog.threshold.query.warn": "-1",
                     "index.search.slowlog.threshold.fetch.warn": "-1",
                     "index.indexing.slowlog.threshold.index.warn": "-1",
                     "index.number_of_routing_shards": "1"}}}


def _index_mappings() -> dict:
    """``GET /<index>/_mapping`` - 41 leaf fields against a total_fields.limit of 50."""
    properties: dict = {}
    for path, kind in MAPPING_FIELDS:
        node = properties
        parts = path.split(".")
        for part in parts[:-1]:
            node = node.setdefault(part, {"properties": {}})["properties"]
        node[parts[-1]] = {"type": kind}
    return {"logs-checkout-default": {"mappings": {"dynamic": "true", "properties": properties}}}


def _index_stats() -> dict:
    """``GET /<index>/_stats/<metrics>`` - a segment-heavy index with a cold query cache."""
    def body(scale: int) -> dict:
        return {"docs": {"count": 240_000 * scale, "deleted": 31_000 * scale},
                "store": {"size_in_bytes": 412_000_000 * scale},
                "indexing": {"index_total": 1_204_000 * scale, "index_time_in_millis": 88_400 * scale,
                             "index_current": 0, "index_failed": 12},
                "search": {"query_total": 512_000 * scale, "query_time_in_millis": 941_000 * scale,
                           "fetch_total": 128_000 * scale, "fetch_time_in_millis": 402_000 * scale},
                "segments": {"count": 210 * scale, "memory_in_bytes": 41_000_000 * scale},
                "merges": {"total": 3_100 * scale, "total_time_in_millis": 1_204_000 * scale},
                "refresh": {"total": 88_000 * scale, "total_time_in_millis": 204_000 * scale},
                "fielddata": {"memory_size_in_bytes": 8_589_934_592, "evictions": 41},
                "query_cache": {"memory_size_in_bytes": 12_000_000, "hit_count": 31_000,
                                "miss_count": 480_000},
                "request_cache": {"memory_size_in_bytes": 2_400_000, "hit_count": 9_100,
                                  "miss_count": 22_000}}
    return {"logs-checkout-default": {"_shards": {"total": 2, "successful": 2, "failed": 0},
                                      "_all": {"primaries": body(1), "total": body(2)},
                                      "indices": {"logs-checkout-default": {
                                          "uuid": "8pQ3vXfLRlm0nEtQ0nR4Aw", "health": "green",
                                          "status": "open",
                                          "primaries": body(1), "total": body(2)}}}}


def _index_templates() -> dict:
    """``GET /_index_template[/<name>]``."""
    return {"index_templates": [{"name": "logs-checkout", "index_template": {
        "index_patterns": ["logs-checkout-*"],
        "template": {"settings": {"index.number_of_shards": "1", "index.number_of_replicas": "1",
                                  "index.lifecycle.name": "logs"},
                     "mappings": {"properties": {"@timestamp": {"type": "date"}}}, "aliases": {}},
        "composed_of": ["ecs-base"], "priority": 200, "version": 2,
        "data_stream": {"hidden": False, "allow_custom_routing": False},
        "_meta": {"managed_by": "platform-team"}}}]}


def _component_templates() -> dict:
    """``GET /_component_template[/<name>]``."""
    return {"component_templates": [{"name": "ecs-base", "component_template": {
        "template": {"settings": {"index.codec": "best_compression"},
                     "mappings": {"properties": {"host": {"properties": {"name": {"type": "keyword"}}}}}},
        "version": 1, "_meta": {"description": "ECS base fields"}}}]}


def _legacy_templates() -> dict:
    """``GET /_template[/<name>]`` - the v1 templates that still shadow index names."""
    return {"filebeat-7": {"order": 1, "index_patterns": ["filebeat-7*", "logs-checkout-*"],
                           "settings": {"index": {"number_of_shards": "3", "number_of_replicas": "1"}},
                           "mappings": {"properties": {"@timestamp": {"type": "date"}}}, "aliases": {}}}


def _simulate_index() -> dict:
    """``POST /_index_template/_simulate_index/<name>``."""
    return {"logs-checkout-2026.09.02": {
        "template": {"settings": {"index": {"number_of_shards": "1", "number_of_replicas": "1",
                                            "lifecycle": {"name": "logs"},
                                            "codec": "best_compression"}},
                     "mappings": {"properties": {"@timestamp": {"type": "date"},
                                                 "host": {"properties": {"name": {"type": "keyword"}}}}},
                     "aliases": {}},
        "overlapping": [{"name": "filebeat-7", "index_patterns": ["filebeat-7*", "logs-checkout-*"]}]}}


def _data_streams() -> dict:
    """``GET /_data_stream``."""
    return {"data_streams": [
        {"name": "logs-checkout-default", "timestamp_field": {"name": "@timestamp"},
         "indices": [{"index_name": ".ds-logs-checkout-default-2026.08.22-000001",
                      "index_uuid": "8pQ3vXfLRlm0nEtQ0nR4Aw"}],
         "generation": 4, "status": "GREEN", "template": "logs-checkout", "ilm_policy": "logs",
         "hidden": False, "system": False, "allow_custom_routing": False, "replicated": False},
        {"name": "metrics-system.cpu-default", "timestamp_field": {"name": "@timestamp"},
         "indices": [{"index_name": ".ds-metrics-system.cpu-default-2026.08.30-000001",
                      "index_uuid": "T1oWq8LmSk2Gn0Rb4wQ9Zg"}],
         "generation": 2, "status": "YELLOW", "hidden": False, "system": False,
         "allow_custom_routing": False, "replicated": False}]}


def _allocation_explain() -> dict:
    """``POST/GET /_cluster/allocation/explain``, keyed ``index/shard/primary``.

    ``_default`` is what an explain with no body returns: Elasticsearch picks an unassigned
    shard itself, and here that is the nginx replica the disk decider is refusing.
    """
    nginx_replica = {
        "index": "logs-nginx.error-default", "shard": 0, "primary": False,
        "current_state": "unassigned",
        "unassigned_info": {"reason": "INDEX_CREATED", "at": "2026-09-02T09:41:03.117Z",
                            "last_allocation_status": "no_attempt"},
        "can_allocate": "no",
        "allocate_explanation": "cannot allocate because allocation is not permitted to any of the nodes",
        "node_allocation_decisions": [
            {"node_id": _NODE_IDS["node-2"], "node_name": "node-2", "transport_address": "127.0.0.1:9301",
             "roles": ["data"], "node_attributes": {}, "node_decision": "no", "weight_ranking": 1,
             "deciders": [{"decider": "disk_threshold", "decision": "NO",
                           "explanation": "the node is above the high watermark cluster setting "
                                          "[cluster.routing.allocation.disk.watermark.high=90%], having "
                                          "less than the minimum required [17.4gb] free space, "
                                          "actual free: [8.7gb]"}]},
            {"node_id": _NODE_IDS["node-3"], "node_name": "node-3", "transport_address": "127.0.0.1:9302",
             "roles": ["data"], "node_attributes": {}, "node_decision": "no", "weight_ranking": 2,
             "deciders": [{"decider": "same_shard", "decision": "NO",
                           "explanation": "a copy of this shard is already allocated to this node "
                                          "[[logs-nginx.error-default][0], node[Rk8QyfmvT3ib0hVsY1Wp2A]]"}]}]}
    cpu_replica = {
        "index": "metrics-system.cpu-default", "shard": 0, "primary": False,
        "current_state": "unassigned",
        "unassigned_info": {"reason": "NODE_LEFT", "at": "2026-09-02T10:02:44.900Z",
                            "details": "node_left [Ux1cWq0RQ2yGnR7f0oq3Zg]",
                            "last_allocation_status": "no_attempt"},
        "can_allocate": "allocation_delayed",
        "allocate_explanation": "The node containing this shard copy recently left the cluster. "
                                "Elasticsearch is waiting for it to return.",
        "configured_delay": "1m", "configured_delay_in_millis": 60_000,
        "remaining_delay": "42.1s", "remaining_delay_in_millis": 42_100,
        "node_allocation_decisions": [
            {"node_id": _NODE_IDS["node-3"], "node_name": "node-3", "transport_address": "127.0.0.1:9302",
             "roles": ["data"], "node_decision": "yes"}]}
    return {"_default": nginx_replica,
            "logs-nginx.error-default/0/False": nginx_replica,
            "metrics-system.cpu-default/0/False": cpu_replica}


def _flatten_settings(body: dict, prefix: str = "") -> dict:
    """``PUT _settings`` accepts flat (``{"index.x.y": v}``) or nested bodies; store one shape."""
    flat: dict = {}
    for key, value in body.items():
        path = f"{prefix}{key}"
        if isinstance(value, dict):
            flat.update(_flatten_settings(value, f"{path}."))
        else:
            flat[path] = value
    return flat


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

    def _reply_text(self, text: str, code: int = 200) -> None:
        """Hot threads is the one endpoint that answers plain text, not JSON."""
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=UTF-8")
        self.end_headers()
        self.wfile.write(text.encode())

    def _record(self, body: dict | None = None) -> None:
        self.server.requests.append({"method": self.command, "path": self.path,
                                     "headers": dict(self.headers), "body": body})

    @property
    def _query(self) -> dict[str, str]:
        raw = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        return {k: v[0] for k, v in raw.items()}

    def do_GET(self) -> None:
        self._record()
        path = urllib.parse.urlparse(self.path).path
        admin = self._admin_get(path)
        if admin is not None:
            obj, code = admin
            return self._reply_text(obj, code) if isinstance(obj, str) else self._reply(obj, code)
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
        return self._reply({"error": f"unsupported GET {path}"}, 404)

    def do_POST(self) -> None:
        body = self._body()
        self._record(body)
        path = urllib.parse.urlparse(self.path).path
        if path.endswith("/_search"):
            pattern = urllib.parse.unquote(path[1:-len("/_search")])
            return self._reply(self._search(pattern, body))
        if path == "/_cluster/allocation/explain":
            return self._reply(*self._explain(body))
        if path.startswith("/_index_template/_simulate_index/"):
            name = urllib.parse.unquote(path[len("/_index_template/_simulate_index/"):])
            answer = self.server.cluster["simulate_index"].get(name)
            return self._reply(answer) if answer else self._reply(
                {"error": {"type": "resource_not_found_exception", "reason": f"no template for [{name}]"}}, 404)
        if path.startswith("/_snapshot/") and path.endswith("/_verify"):
            return self._reply(self.server.cluster["verify_repository"])
        return self._reply({"error": f"unsupported POST {path}"}, 404)

    def do_PUT(self) -> None:
        body = self._body()
        self._record(body)
        path = urllib.parse.urlparse(self.path).path
        if path.endswith("/_settings"):
            index = urllib.parse.unquote(path[1:-len("/_settings")])
            stored = self.server.cluster["settings"].setdefault(index, {"settings": {}, "defaults": {}})["settings"]
            for key, value in _flatten_settings(body).items():
                if value is None:
                    stored.pop(key, None)                    # a null value resets a setting, as in a real cluster
                else:
                    stored[key] = value
            return self._reply({"acknowledged": True})
        return self._reply({"error": f"unsupported PUT {path}"}, 404)

    def do_DELETE(self) -> None:
        self._record()
        self._reply({"acknowledged": True})

    # ---- the administrator's endpoints ------------------------------------
    def _admin_get(self, path: str) -> tuple[Any, int] | None:
        """Answer one of ``build_cluster``'s endpoints, or ``None`` to fall through.

        Returns ``(body, status)``; a ``str`` body is sent as text/plain (hot threads).
        """
        cluster, query = self.server.cluster, self._query
        if path.startswith("/_cat/shards"):
            return self._cat_rows(cluster["shards"], path, "/_cat/shards"), 200
        if path.startswith("/_cat/recovery"):
            rows = self._cat_rows(cluster["recovery"], path, "/_cat/recovery")
            if query.get("active_only", "false").lower() == "true":
                rows = [r for r in rows if r["_active"]]
            return [{k: v for k, v in r.items() if not k.startswith("_")} for r in rows], 200
        if path == "/_cat/nodes":
            return cluster["nodes"], 200
        if path.startswith("/_cat/thread_pool"):
            wanted = urllib.parse.unquote(path[len("/_cat/thread_pool/"):]) if len(path) > len("/_cat/thread_pool") else ""
            names = [p for p in wanted.split(",") if p]
            return [r for r in cluster["thread_pools"] if not names or r["name"] in names], 200
        if "/hot_threads" in path:
            return cluster["hot_threads"], 200
        if "/stats" in path and path.startswith("/_nodes"):
            return self._node_stats_for(path), 200
        if path == "/_tasks":
            return cluster["tasks"], 200
        if path == "/_cluster/pending_tasks":
            return cluster["pending_tasks"], 200
        if path == "/_cluster/allocation/explain":
            return self._explain({})
        if path == "/_ilm/status":
            return cluster["ilm_status"], 200
        if path.endswith("/_ilm/explain"):
            return self._ilm_explain_for(path, query)
        if path.startswith("/_ilm/policy"):
            name = urllib.parse.unquote(path[len("/_ilm/policy/"):]) if len(path) > len("/_ilm/policy") else ""
            found = {k: v for k, v in cluster["ilm_policies"].items() if not name or k == name}
            return (found, 200) if found else ({"error": {"type": "resource_not_found_exception",
                                                          "reason": f"policy [{name}] does not exist"}}, 404)
        if path == "/_slm/policy":
            return cluster["slm_policies"], 200
        if path == "/_slm/stats":
            return cluster["slm_stats"], 200
        if path == "/_snapshot" or path.startswith("/_snapshot/"):
            return self._snapshot_get(path, query)
        if path.startswith("/_index_template"):
            return self._template_get(path, "/_index_template", cluster["index_templates"], "index_templates")
        if path.startswith("/_component_template"):
            return self._template_get(path, "/_component_template", cluster["component_templates"], "component_templates")
        if path.startswith("/_template"):
            name = urllib.parse.unquote(path[len("/_template/"):]) if len(path) > len("/_template") else ""
            return {k: v for k, v in cluster["legacy_templates"].items() if not name or fnmatch.fnmatch(k, name)}, 200
        if path.startswith("/_data_stream"):
            return cluster["data_streams"], 200
        for suffix, key in (("/_settings", "settings"), ("/_mapping", "mappings")):
            if path.endswith(suffix):
                return self._index_scoped(path[1:-len(suffix)], cluster[key])
        if "/_stats" in path:
            # Unlike _settings and _mapping, the stats response is not keyed by index name:
            # it is {"_shards", "_all", "indices": {...}} for whatever the pattern matched.
            found, code = self._index_scoped(path[1:path.index("/_stats")], cluster["index_stats"])
            return (next(iter(found.values())), code) if code == 200 else (found, code)
        return None

    def _cat_rows(self, rows: list[dict], path: str, prefix: str) -> list[dict]:
        """cat endpoints take the index pattern in the path; no pattern means every row."""
        pattern = urllib.parse.unquote(path[len(prefix) + 1:]) if len(path) > len(prefix) else ""
        if not pattern:
            return list(rows)
        return [r for r in rows if any(fnmatch.fnmatch(r["index"], p) for p in pattern.split(","))]

    def _node_stats_for(self, path: str) -> dict:
        """``/_nodes/stats/...`` or ``/_nodes/<name>/stats/...`` - the filter is a node name here."""
        stats = self.server.cluster["node_stats"]
        parts = [p for p in path.split("/") if p]
        node = parts[1] if len(parts) > 1 and parts[1] != "stats" else ""
        if not node:
            return stats
        kept = {nid: body for nid, body in stats["nodes"].items() if node in (body["name"], nid)}
        return {**stats, "nodes": kept}

    def _ilm_explain_for(self, path: str, query: dict) -> tuple[dict, int]:
        pattern = urllib.parse.unquote(path[1:-len("/_ilm/explain")])
        indices = {name: body for name, body in self.server.cluster["ilm_explain"]["indices"].items()
                   if any(fnmatch.fnmatch(name, p) for p in pattern.split(","))}
        if query.get("only_managed", "false").lower() == "true":
            indices = {n: b for n, b in indices.items() if b.get("managed")}
        if query.get("only_errors", "false").lower() == "true":
            indices = {n: b for n, b in indices.items() if b.get("step") == "ERROR"}
        return {"indices": indices}, 200

    def _snapshot_get(self, path: str, query: dict) -> tuple[dict, int]:
        cluster = self.server.cluster
        parts = [urllib.parse.unquote(p) for p in path.split("/") if p][1:]      # drop "_snapshot"
        if not parts:
            return cluster["repositories"], 200
        repo = parts[0]
        if repo not in cluster["repositories"]:
            return {"error": {"type": "repository_missing_exception", "reason": f"[{repo}] missing"}}, 404
        if len(parts) == 1:
            return {repo: cluster["repositories"][repo]}, 200
        if len(parts) >= 3 and parts[2] == "_status":
            status = cluster["snapshot_status"].get(parts[1])
            return (status, 200) if status else ({"snapshots": []}, 200)
        snapshots = cluster["snapshots"].get(repo, [])
        if parts[1] == "_current":
            found = [s for s in snapshots if s["state"] == "IN_PROGRESS"]
        else:
            found = [s for s in snapshots if any(fnmatch.fnmatch(s["snapshot"], p) for p in parts[1].split(","))]
        found = sorted(found, key=lambda s: s["start_time"], reverse=query.get("order") == "desc")
        size = int(query.get("size") or 0)
        return {"snapshots": found[:size] if size else found, "total": len(found),
                "remaining": max(0, len(found) - size) if size else 0}, 200

    def _template_get(self, path: str, prefix: str, stored: dict, key: str) -> tuple[dict, int]:
        name = urllib.parse.unquote(path[len(prefix) + 1:]) if len(path) > len(prefix) else ""
        return {key: [t for t in stored[key] if not name or fnmatch.fnmatch(t["name"], name)]}, 200

    def _index_scoped(self, pattern: str, stored: dict) -> tuple[dict, int]:
        found = {name: body for name, body in stored.items()
                 if any(fnmatch.fnmatch(name, p) for p in urllib.parse.unquote(pattern).split(","))}
        return (found, 200) if found else ({"error": {"type": "index_not_found_exception",
                                                      "reason": f"no such index [{pattern}]"},
                                            "status": 404}, 404)

    def _explain(self, body: dict) -> tuple[dict, int]:
        """No body means "pick an unassigned shard yourself", which is what ``_default`` is.

        With nothing unassigned Elasticsearch answers 400, not an empty 200; an empty
        ``allocation_explain`` in the fake reproduces that, message included.
        """
        explains = self.server.cluster["allocation_explain"]
        key = f"{body.get('index')}/{body.get('shard', 0)}/{bool(body.get('primary'))}"
        found = explains.get(key) or explains.get("_default")
        if found is None:
            return {"error": {"type": "illegal_argument_exception",
                              # 7.16 dropped "unassigned" from this message and it has stayed
                              # that way through 8.x and 9.x. Serving the <=7.15 wording made
                              # the plugin's matcher look correct against a version nobody runs.
                              "reason": "unable to find any shards to explain "
                                        "[ClusterAllocationExplainRequest[useAnyUnassignedShard=true]] "
                                        "in the routing table"},
                    "status": 400}, 400
        return found, 200

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
    def __init__(self, port: int = 0, health: str = "yellow", docs: list[tuple[str, dict]] | None = None,
                 cluster: dict | None = None):
        super().__init__(("127.0.0.1", port), ESHandler)
        self.requests: list[dict] = []
        self.health = health
        self.docs = docs if docs is not None else build_dataset()
        self.cluster = cluster if cluster is not None else build_cluster()
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
