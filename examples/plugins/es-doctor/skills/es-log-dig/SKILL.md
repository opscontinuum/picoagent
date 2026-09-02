---
name: es-log-dig
description: How to dig through application and system logs shipped by Filebeat / Elastic Agent to find the cause of an incident
---
# Digging through logs

Data lives in `logs-*` (Elastic Agent) and `filebeat-*` (standalone Filebeat) and follows ECS:
`@timestamp`, `log.level`, `message`, `service.name`, `host.name`, `container.id`, `kubernetes.pod.name`,
`event.dataset` (e.g. `nginx.error`, `postgresql.log`, `kubernetes.container_logs`), `trace.id`, `error.message`.

Work from wide to narrow:

1. **When did it start?** `es_logs level=error histogram=true interval=5m since=6h` for the affected `service` or `host`. The timeline shows the first bucket where errors jump. Narrow `since`/`until` to a few minutes around it.
2. **What is failing?** With the narrow window, read the error messages. Group mentally by template (collapse ids and numbers). The most frequent message is usually the symptom; the *first* distinct message is usually closer to the cause.
3. **Who else was affected?** Drop the `service` filter and look at the `By dataset` breakdown: errors in `nginx.error` plus `postgresql.log` at the same time point at the database, not the web tier.
4. **Follow a request.** If messages carry `trace.id` or a request id, `es_search` on `logs-*` with `term trace.id` to see every log line for one failing request across services.
5. **Check warnings before the errors.** `es_logs level=warn` in the 10 minutes before the first error often shows the precursor (slow queries, retries, pool nearly full).
6. **Then correlate.** Hand the window and host/service to `es_correlate` to test whether the errors line up with CPU, memory, disk or latency (see skill es-correlate).

Tips: `query` uses simple_query_string, so `"pool exhausted" -retry` works. Beats spell error levels differently (`error`, `ERR`, `fatal`, `crit`); `level=error` covers them. Use `level=any` when the app logs errors at INFO.
