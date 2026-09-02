---
name: es-slow-cluster
description: Runbook for a green but slow Elasticsearch cluster - thread-pool rejections, hot threads, heap, segments, slow logs and mapping problems, in the order that separates cause from symptom
---
# The cluster is green and slow

Green means allocation is fine, so the latency is coming from work, memory or layout. Walk
the steps in order; each one narrows what the next has to explain.

## 1. Is work being dropped or just delayed?

`es_nodes view=thread_pools` - rows with rejections and queue depth come first
([cat thread pool](https://www.elastic.co/guide/en/elasticsearch/reference/current/cat-thread-pool.html)).

- `rejected > 0` on **search**: queries were *refused*, not slowed. The client saw an error.
  Either too many concurrent searches, or each one is expensive enough to hold a thread.
- `rejected > 0` on **write**: bulk requests were refused. Almost always bulk sizing - too
  many small bulks, or too many concurrent writers - not a cluster that is "too small".
- `queue` high with `rejected = 0`: work is queuing. Latency without errors. Same causes,
  earlier stage.
- Rejections on one node only: that node is the problem (`es-node-pressure`), not the query.

## 2. What is the CPU actually doing?

`es_hot_threads` (add `node=` for the busy node). Read the per-node header lines and the top
frames of each hot thread:

- `...search.query.QueryPhase...`, `PointRangeQuery`, `TermsQuery` - query execution.
  Expensive queries, or too many.
- `...index.engine.InternalEngine.index...` - indexing pressure.
- `...index.engine.*.merge...` or `TieredMergePolicy` - merging. Go to step 4.
- `GC`, `G1`, `ParallelGC` frames - memory, not CPU. Go to step 3.
- `type=wait` or `type=block` instead of the default `type=cpu` finds threads that are
  *blocked* rather than busy - the right sample when CPU is low but latency is high.

## 3. Memory

`es_nodes` (summary). Look at heap %, the old-GC column and any tripped breaker.
Heap that stays high with old-GC time climbing means the node is spending its time
collecting rather than searching. Circuit breaker trips
([node stats](https://www.elastic.co/guide/en/elasticsearch/reference/current/cluster-nodes-stats.html))
name the culprit: `fielddata` means aggregating or sorting on `text` fields, `request` means
a single request asked for too much (huge `size`, deep pagination, wide aggregations),
`parent` means the node is simply over its total budget. Details in `es-node-pressure`.

## 4. Index shape

`es_index_inspect index=<the slow index> view=stats`:

- **Segment count** high relative to the index size: many small segments, each searched
  separately. Causes: a very short `refresh_interval`, continuous small writes, merging that
  cannot keep up (step 2 will have shown merge frames). A read-only index that will not
  change again is a candidate for a force-merge - which is expensive and one-way, so it goes
  through `es_request` with the user's agreement.
- **Deleted-document percentage** high: updates and deletes leave tombstones that are still
  searched until merged away.
- **Merge time** large and growing: writes are outrunning merging.
- **Fielddata memory** non-zero: something is aggregating or sorting on an analysed `text`
  field. That loads every term into heap. The fix is a `keyword` sub-field, which means a
  mapping change and a new index - see `es-mappings-and-templates`.
- **Query cache hit ratio** low: queries are not repeating, or they contain a `now`
  timestamp that makes every one unique. Round date ranges (`now/m`) to make them cacheable.

## 5. Which queries?

`es_slowlog index=<index>` reports the warn thresholds and looks for slow-log events that
were shipped into Elasticsearch. The slow log is written to files on each node
(`*_index_search_slowlog.json`,
[slow log settings](https://www.elastic.co/docs/reference/elasticsearch/index-settings/slow-log),
[slow logs](https://www.elastic.co/docs/deploy-manage/monitor/logging-configuration/slow-logs)),
so unless Filebeat or Elastic Agent ships them there is nothing for an API to return - the
tool says so rather than pretending the index is fast.

If thresholds are unset, propose `es_slowlog action=enable query_warn=2s fetch_warn=1s`,
which asks the user before writing. Start generous: a 100ms threshold on a busy index
produces a log that is itself a performance problem. Come back after real traffic has run.

`es_nodes view=tasks` shows what is running *now* - a long `indices:data/read/search` is the
slow query, with its source in the description.

## 6. Mapping

`es_index_inspect index=<index> view=mappings`. A field count near the limit, or
`dynamic: true` on data with unpredictable keys, means every new document can add fields.
That inflates the cluster state, slows every mapping update and eventually fails indexing.
`es-mappings-and-templates` has the fix.

## Reading the combination

| What you see | What it usually is |
|---|---|
| write rejections, indexing frames hot | bulk sizing or too many writers, not cluster size |
| search rejections, few slow queries | many concurrent cheap queries; look at the client |
| high fetch time, low query time | large `_source` documents, or a big `size` |
| high query time, low fetch time | expensive queries: wildcards, scripts, deep aggregations |
| fielddata memory, breaker trips | aggregating on `text`; needs a mapping change |
| many segments, merge frames hot | refresh interval or write pattern |
| everything slow on one node only | `es-node-pressure` |

## Report

Say which step produced the evidence, quote the number (rejected count, segment count, heap
percent, the hot-thread frame), name the change you would make, and say which changes need
the user: force-merge, replica count, refresh interval, mapping and any bulk-client change
are all decisions, not fixes you apply.
