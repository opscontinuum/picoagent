# es-doctor

An Elasticsearch / Elastic Stack plugin for picoagent, in two halves.

The **data** half gives the agent a working knowledge of how Beats and Elastic Agent lay data
out (ECS fields, `logs-*`, `metrics-*`, `traces-apm*` data streams), tools to dig through it,
and a correlation tool that puts log errors, host metrics and APM latency on the same
timeline.

The **administration** half looks at the cluster itself the way an administrator does:
shards and why they are unassigned, node heap/disk/breaker pressure, thread-pool rejections,
hot threads, index lifecycle management, snapshots, index internals, templates and the slow
log. Six runbook skills turn those tools into procedures.

No client library: everything is plain HTTP through the standard library. Everything reads,
with exactly one exception (`es_slowlog enable|disable`, three named settings, after you
confirm); every other write goes through `es_request` and its `allow_destructive` gate.

## Install

```bash
picoagent plugin add ./examples/plugins/es-doctor        # or git:github.com/you/es-doctor@v0.1.0
```

```toml
# ~/.picoagent/config.toml or <project>/.picoagent/config.toml
[plugins]
enabled = ["./examples/plugins/es-doctor"]

[plugins.es-doctor]
url = "https://es.example.com:9200"     # or ELASTICSEARCH_URL
api_key = "..."                          # or ELASTICSEARCH_API_KEY; username/password also work
# logs_index = "logs-*,filebeat-*"       # override if your naming differs
# metrics_index = "metrics-*,metricbeat-*"
# traces_index = "traces-apm*,apm-*"
# allow_destructive = false
```

## What the model can do

| Tool | Purpose |
|---|---|
| `es_cluster_health` | status, shards, and the allocation explanation when not green |
| `es_indices` | indices grouped as logs / metrics / traces / other |
| `es_logs` | search logs by service, host, container, level, free text, time window; dataset breakdown; optional timeline |
| `es_metrics` | bucketed avg/max of a metric; aliases `cpu`, `memory`, `load`, `disk`, `net_in`, `net_out`, `container_*`, `k8s_*`, `jvm_heap` |
| `es_correlate` | errors + metrics + APM per bucket, Pearson r, spike detection, top error messages in the spike |
| `es_search` | raw query DSL |
| `es_request` | raw REST; DELETE, `_delete_by_query`, `_close`, settings changes etc. are blocked unless `allow_destructive = true` |

### Cluster administration

| Tool | Purpose |
|---|---|
| `es_shards` | shard table with unassigned shards first and their reasons, counts by state, shards per node; `explain=true` runs the allocation explain for one shard and prints every decider |
| `es_recovery` | active and recent recoveries (snapshot restores, relocations, replica builds) with stage and percentage, grouped by type |
| `es_nodes` | heap, GC, CPU, load, disk and circuit breakers with thresholds flagged; `view=thread_pools` for queue depth and rejections, `view=breakers`, `view=tasks` for long-running tasks and a backed-up master queue |
| `es_hot_threads` | `_nodes/hot_threads` passed through as plain text; `type=cpu|wait|block|mem` |
| `es_ilm` | ILM status, per-index phase/action/step, indices stuck in ERROR with the failing step and its reason, count of unmanaged indices; `policy=` summarises a policy |
| `es_snapshots` | repositories, recent snapshots and their state, progress of a running snapshot, SLM policies with last success and failure; `verify=true` asks first |
| `es_index_inspect` | one index in depth: the settings that matter, mapping size against the field limit, doc/store/segment/merge/search/cache stats |
| `es_templates` | index, component and legacy templates; `simulate_index=` resolves which template an index name wins and what overlaps it; flags data streams with no template |
| `es_slowlog` | show or set the three slow-log warn thresholds, and look for slow-log events that were shipped in |

Nine skills tell the model *how* to use all of this. `es-triage` is the entry point and
routes to the rest: `es-unassigned-shards`, `es-slow-cluster`, `es-node-pressure`,
`es-ilm-and-retention`, `es-snapshot-and-restore`, `es-mappings-and-templates` for the
cluster, and `es-log-dig` / `es-correlate` for the data in it. `/es` prints a one-line
cluster summary for you.

### What it will not do

No SSH and no log-file access: everything is an API call. No restore, reroute, ILM
retry/move, index deletion or settings change other than the three slow-log keys - those
remain `es_request` plus `allow_destructive`, and the skills say so at every branch. No
Kibana, Fleet or Watcher APIs, no cross-cluster search or CCR, no security diagnostics, and
no "auto-fix" mode: the tools gather evidence and the user decides.

### Versions

Targets Elasticsearch 7.10+ and 8.x. `_index_template`, `_component_template` and
`_simulate_index` exist from 7.8, `_slm` from 7.4, `_ilm/explain` from 6.6. Elasticsearch 9.0
removed the `?time` and `?local` query parameters from the cat APIs; this plugin never sent
either, and none of the cat columns it asks for were removed. On Elastic Cloud Serverless
most administration endpoints return 400 or 404 - the tool hands that back as an error
result rather than raising, and the skills note what serverless does not expose.

## Try it without a cluster

```bash
python -m picoagent.testing.fake_es --port 9200      # canned incident: 2026-09-02 10:15-10:20 UTC
ELASTICSEARCH_URL=http://localhost:9200 picoagent -e examples/plugins/es-doctor
› why did checkout fail this morning? look between 10:00 and 10:30 UTC on 2026-09-02
```

The fake holds a checkout service whose `db connection pool exhausted` errors, nginx
upstream timeouts, CPU, memory and APM latency all spike together for five minutes.
`es_correlate` on that window reports r≈0.99 for CPU and latency and names the spike.

It also serves a canned *cluster* for the administration tools: three nodes, one of them
over the heap and disk thresholds with a tripped breaker and a rejecting search pool; two
unassigned shards with different reasons and different deciders; a relocating shard; an ILM
policy stuck on a rollover-alias mismatch; three snapshots (SUCCESS, PARTIAL, IN_PROGRESS)
and an SLM policy that failed last night; overlapping templates and a data stream with no
template. Try `why is my cluster yellow?` or `is anything stuck in ILM?`.

Every response shape it serves was copied from the Elasticsearch reference rather than
invented, because a fake with made-up field names makes the tools wrong in production while
the tests stay green. The pages are listed in the docstrings of `build_cluster` and
`es_admin.py`.

## Sample correlation output

```
errors vs metrics per 1m, 30 buckets
bucket            errors  logs  cpu    memory  apm_p50_ms  apm_fail
----------------  ------  ----  -----  ------  ----------  --------
2026-09-02 10:14  0       8     0.268  0.579   120         0
2026-09-02 10:15  9       11    0.962  0.880   4800        4
...
Correlation of error count with:
  system.cpu.total.norm.pct                r=0.99 strong positive
  apm transaction.duration.us (p50)        r=1.00 strong positive

Error spike: 5 bucket(s) from 2026-09-02T10:15 to 2026-09-02T10:19 (>2σ above mean)
Top error messages during the spike:
     25  db connection pool exhausted: timeout acquiring connection
     12  upstream timed out (N: Connection timed out) while reading response
```

## Layout

```
plugin.toml
es_client.py    ESClient, ESError, Settings, the tool base class and the two output helpers
es_doctor.py    the data tools (logs, metrics, correlation, search, raw request) and register()
es_admin.py     the nine cluster-administration tools
skills/         nine runbooks
```

`es_client.py` exists because the plugin loader imports the entry module under a mangled
name and rebuilds it on every load: a sibling that imported `ESError` from `es_doctor`
would get a second, unrelated class, and `except ESError` would silently stop catching.
A module both sides import by its plain name keeps one identity.
