---
name: es-correlate
description: Correlate log errors with Metricbeat/Elastic Agent metrics and APM data to separate cause from symptom
---
# Correlating logs with metrics and APM

`es_correlate` builds one table per time bucket: error count, total logs, the average of each metric, APM p50 latency and failure count, then reports Pearson r between errors and each series and flags spike buckets. Use it *after* es-log-dig has given you a window and a host/service.

## Choosing metrics
Defaults are `cpu` (`system.cpu.total.norm.pct`) and `memory` (`system.memory.actual.used.pct`). Add what the error messages hint at:
- timeouts / slow responses → `load`, `cpu`, APM latency
- connection errors, "too many open files" → `net_in`, `net_out`, and `es_logs query="ulimit OR EMFILE"`
- OOM kills, restarts → `memory`, `container_memory` / `k8s_memory`, and `es_logs query="OOMKilled OR Killed process"`
- database pool exhausted → APM `transaction.duration.us` on the *database* service, `postgresql.*` datasets

## Reading the result
- **r ≥ 0.7** with errors: the metric moves with the errors. That does not say which is cause: CPU saturation can cause timeouts, or timeouts can cause retry storms that saturate CPU. Look at *which rose first* in the table (1m buckets help).
- **Latency up, CPU flat**: the wait is downstream (database, external API, lock). Dig that service's logs.
- **CPU up, latency flat, errors up**: likely a crash loop or a batch job on the same host; check `es_logs host=... level=any` for restarts.
- **Errors up, nothing else moves**: a logic or config change. Check deploy times: `es_logs query="deploy OR release OR config reload" level=any`.
- The "Top error messages during the spike" list has numbers collapsed to `N`; the first distinct message is usually the root symptom.

## Widening scope
Run once with `host` set (machine-level resources) and once with `service` only (all replicas). If a service-wide spike shows on one host only, it is that host; if it shows on all hosts, it is a shared dependency.

Report: timeline (first error → peak → recovery), the correlated series with r values, your best cause hypothesis, and the evidence that would confirm it.
