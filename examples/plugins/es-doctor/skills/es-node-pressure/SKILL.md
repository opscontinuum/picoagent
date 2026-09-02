---
name: es-node-pressure
description: Runbook for an Elasticsearch node under heap, GC, disk, circuit-breaker or master-queue pressure - which symptoms follow from shard count, which from queries, which from hardware
---
# A node is under pressure

Nodes fail in a small number of ways and they cascade: disk pressure moves shards, moving
shards costs heap and CPU, heap pressure trips breakers, tripped breakers fail requests, and
a busy master makes all of it slower to resolve. Find the first link.

## 1. Look at every node together

`es_nodes`. The table gives heap %, RAM %, CPU, 5-minute load, disk used %, free space, old-GC
count and time ([cat nodes](https://www.elastic.co/guide/en/elasticsearch/reference/current/cat-nodes.html),
[node stats](https://www.elastic.co/guide/en/elasticsearch/reference/current/cluster-nodes-stats.html)).
The Warnings section flags heap at or above 75%, disk at or above the 85% low watermark, any
circuit breaker with `tripped > 0`, and old-GC time over a minute.

Compare nodes before judging one. Three nodes at 80% heap is a sizing question. One node at
80% while two sit at 45% is a distribution question, and `es_shards`' per-node counts will
usually show it.

## 2. Heap and garbage collection

Heap percentage is a snapshot; the old-generation GC counters are the trend. A node whose
old-GC *time* keeps climbing is spending real seconds not answering queries, and each of
those seconds is a stop.

What holds heap, in the order worth checking:

- **Fielddata** - aggregating or sorting on an analysed `text` field loads its terms into
  heap. `es_index_inspect view=stats` shows `fielddata` memory per index. The fix is a
  `keyword` field, which means a mapping change (`es-mappings-and-templates`).
- **Per-request memory** - a large `size`, deep pagination, or an aggregation with a high
  cardinality field. The `request` breaker trips on these.
- **Cluster state** - many indices, many fields, many templates. Master-node heap.
  Elasticsearch's own guidance is to stay under **3000 indices per GB of heap on master
  nodes**, and to keep shards between **10GB and 50GB** with fewer than **200 million
  documents** each ([size your shards](https://www.elastic.co/guide/en/elasticsearch/reference/current/size-your-shards.html)).
  Very many small shards is the most common self-inflicted heap problem, and its cause is
  usually a rollover policy that fires too often or an index-per-day habit on low-volume
  data.

## 3. Circuit breakers

`es_nodes view=breakers` prints each breaker's estimate, limit and trip count. A breaker
trip is Elasticsearch protecting the node: the request failed so the JVM would not.

- `parent` - the node is over its total budget. Nothing specific is to blame; the node is
  simply doing too much for its heap.
- `fielddata` - see above. This one has a code fix.
- `request` - one request asked for too much. Find it with `es_nodes view=tasks` and the
  slow log.
- `in_flight_requests` - too much data in flight, often oversized bulk bodies.

Non-zero trip counts are cumulative since node start. Ask whether they are still increasing
before treating them as current.

## 4. Disk

Watermarks and their consequences are in `es-unassigned-shards`; the short version is 85% no
new shards, 90% shards move away, 95% indices go read-only
([cluster-level shard allocation settings](https://www.elastic.co/docs/reference/elasticsearch/configuration-reference/cluster-level-shard-allocation-routing-settings)).
Note that a node crossing 90% causes shard *relocation*, which costs CPU, heap and network on
both nodes and can look like a completely different incident. `es_recovery` shows it
happening. Do not treat a relocation storm as the cause when a full disk started it.

Where the space went is a retention question: `es-ilm-and-retention`.

## 5. The master and the cluster state

`es_nodes view=tasks` also lists pending cluster tasks. Pending tasks queued for more than
30 seconds mean the master cannot apply cluster-state changes fast enough
([pending cluster tasks](https://www.elastic.co/guide/en/elasticsearch/reference/current/cluster-pending.html)).
Causes: very many indices or fields, mapping updates arriving continuously (dynamic mapping
on high-cardinality keys), or a master node that is also carrying data. Symptom to watch for
alongside it: repeated master elections in the Elasticsearch logs
(`es_logs query="master_left OR elected-as-master" level=any`).

## 6. Sorting cause from symptom

| Observation | Follows from |
|---|---|
| heap high on every node, similar shard counts | sizing: the cluster is doing more than it has heap for |
| heap high on one node, shard count high there | distribution: shards, not queries |
| fielddata memory non-zero, `fielddata` breaker trips | a mapping problem, fixable in the index |
| `request` breaker trips, one long task | one client's query |
| disk warnings then relocation then CPU | retention started it |
| pending tasks queued, many indices | cluster-state size; master heap |
| load high, CPU low | I/O or lock contention - `es_hot_threads type=block` |

## What the user has to decide

Everything that actually relieves a pressured node is a decision, not a fix you apply:

- **Add nodes** - the answer when the cluster is genuinely too small. Say so plainly rather
  than proposing settings that trade one resource for another.
- **Reduce replicas** - frees disk and indexing work immediately and reduces resilience
  immediately. It is a trade, not a win.
- **Force-merge cold indices** - reclaims tombstone space and cuts segment count, but is
  expensive and effectively one-way. Only on indices that will never be written again, and
  only through `es_request` with the user's agreement.
- **Delete or roll over old data** - `es-ilm-and-retention`, and it needs the user.
- **Change a mapping** - never in place; a new index and a reindex.

## Report

Name the node, the specific pressure (quote the number and the breaker or GC counter), what
you believe started the chain and why, what will relieve it now, and what would keep it from
recurring. Say explicitly when the honest answer is "this cluster needs more hardware".
