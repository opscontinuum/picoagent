# es-doctor

An Elasticsearch / Elastic Stack diagnostics plugin for picoagent. It gives the agent a
working knowledge of how Beats and Elastic Agent lay data out (ECS fields, `logs-*`,
`metrics-*`, `traces-apm*` data streams), tools to dig through it, and a correlation tool
that puts log errors, host metrics and APM latency on the same timeline.

No client library: everything is plain HTTP through the standard library.

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

Three skills tell the model *how* to use them: `es-triage` (unhealthy cluster),
`es-log-dig` (wide-to-narrow log investigation), `es-correlate` (reading the correlation
table and telling cause from symptom). `/es` prints a one-line cluster summary for you.

## Try it without a cluster

```bash
python -m picoagent.testing.fake_es --port 9200      # canned incident: 2026-09-02 10:15-10:20 UTC
ELASTICSEARCH_URL=http://localhost:9200 picoagent -e examples/plugins/es-doctor
› why did checkout fail this morning? look between 10:00 and 10:30 UTC on 2026-09-02
```

The fake holds a checkout service whose `db connection pool exhausted` errors, nginx
upstream timeouts, CPU, memory and APM latency all spike together for five minutes.
`es_correlate` on that window reports r≈0.99 for CPU and latency and names the spike.

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
