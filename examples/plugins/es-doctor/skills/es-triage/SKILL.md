---
name: es-triage
description: Runbook for an unhealthy Elasticsearch cluster (yellow/red, unassigned shards, disk watermarks, slow queries)
---
# Elasticsearch cluster triage

1. `es_cluster_health`. Red = a primary is missing (data unavailable); yellow = replicas missing (degraded resilience).
2. Read the allocation explanation it includes. Common deciders:
   - `disk_threshold` → a node crossed the 85%/90%/95% watermarks. Free space, delete old indices via ILM, or add nodes. Do not just raise the watermark.
   - `same_shard` / `awareness` → not enough nodes/zones for the replica count. Lower `number_of_replicas` on that index or add nodes.
   - `filter` / `node_version` → allocation filtering or mixed versions.
3. `es_indices` and look for red/yellow rows and unexpectedly huge indices (runaway data stream, missing ILM rollover).
4. If the cluster is healthy but slow, use `es_request GET /_nodes/hot_threads` and `es_request GET /_cat/thread_pool?v&h=node_name,name,active,rejected,completed` to spot rejected search/write queues.
5. Check the cluster's own logs too: `es_logs` with `service=elasticsearch` or `query="elasticsearch"` on `logs-*`.
6. Report: what is broken, the decider/reason, the safest fix, and what you would need approval for (anything under `es_request` DELETE/PUT settings is blocked by default).

Never run `_delete_by_query`, close indices, or change cluster settings without explicit user confirmation.
