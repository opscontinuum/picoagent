---
name: es-unassigned-shards
description: Runbook for a red or yellow Elasticsearch cluster - reading the allocation deciders for an unassigned shard and choosing the fix that does not lose data
---
# Unassigned shards

A shard is unassigned because a *decider* said no. The whole job is to find out which one
and address that reason. Guessing here loses data.

## Procedure

1. `es_shards` - the table puts unassigned shards first and gives each one an
   `unassigned.reason`. Note the per-node shard counts at the bottom: a node holding three
   times its share is its own problem.
2. `es_shards index=<the index> explain=true` (add `primary=true` when the row said `p`).
   This runs `_cluster/allocation/explain` for that shard and prints every node's decision
   as `node: [decider] DECISION - explanation`.
3. Read `can_allocate` first, then the deciders. `can_allocate: allocation_delayed` means
   nothing is wrong yet - Elasticsearch is waiting for a node that left to come back, and
   `remaining_delay` says for how long.

If the cluster is healthy the API answers 400 and the tool says so; that is an answer.

## The deciders, and what each one means

Names as they appear in `deciders[].decider`
([allocation explain API](https://www.elastic.co/guide/en/elasticsearch/reference/current/cluster-allocation-explain.html),
[worked examples](https://www.elastic.co/docs/troubleshoot/elasticsearch/cluster-allocation-api-examples)).

- **`disk_threshold`** - the node is over a watermark. Defaults
  ([cluster-level shard allocation settings](https://www.elastic.co/docs/reference/elasticsearch/configuration-reference/cluster-level-shard-allocation-routing-settings)):
  low **85%** (no new shards allocated here), high **90%** (shards relocate away),
  flood stage **95%** (every index with a shard on that node gets
  `index.blocks.read_only_allow_delete`, so writes are rejected). Fix by freeing space -
  delete or roll over old indices (`es-ilm-and-retention`), or add capacity. The read-only
  block clears itself once usage drops back under the high watermark on recent versions; on
  older ones it must be removed explicitly with a settings change, which needs the user.
  **Do not raise the watermarks.** That hides the alarm and the next stop is a full disk,
  which is an unclean shutdown.
- **`same_shard`** - a copy of this shard is already on that node. Elasticsearch will never
  put a primary and its replica together. Cause: `number_of_replicas` >= number of data
  nodes. Fix: add a data node, or lower the replica count on that index (a settings change -
  ask; and it lowers resilience).
- **`awareness`** - shard-allocation awareness attributes (racks, zones) leave no legal node.
  Same shape as `same_shard`: too many copies for the number of distinct zones.
- **`filter`** - `index.routing.allocation.include/exclude/require` (or the cluster-level
  equivalent) excludes every candidate. The explanation quotes the filter. Common after a
  node was drained for maintenance and the exclusion was never removed, and after a tier
  change (`_tier_preference` naming a tier with no nodes).
- **`node_version`** - the target node runs an older version than the node that holds the
  shard. Shards only move to equal-or-newer nodes. Finish the rolling upgrade.
- **`replica_after_primary_active`** - the replica cannot start until its primary does.
  Not the problem: find the primary and explain that instead.
- **`max_retry`** - allocation failed repeatedly (`unassigned.reason: ALLOCATION_FAILED`) and
  Elasticsearch stopped retrying. The retries are exhausted, not the cause. Find the
  underlying failure first - `es_logs query="failed to create shard OR CorruptIndexException"`
  and the `unassigned.details` field - then the retry counter is cleared with
  `POST /_cluster/reroute?retry_failed=true` through `es_request`, which needs the user's
  confirmation. Retrying without fixing the cause just burns the counter again.
- **`throttling`** - concurrent-recovery limits. Transient; the shard is queued, not stuck.
  Confirm with `es_recovery`.

## `unassigned.reason` values, and what they tell you

- `INDEX_CREATED` - never allocated since the index was made. Nothing was lost; a decider is
  refusing. Go to the decider.
- `NODE_LEFT` - the node holding it went away. With a replica elsewhere this heals itself
  after `index.unassigned.node_left.delayed_timeout`. With no other copy and the node gone
  for good, this is the data-loss case below.
- `ALLOCATION_FAILED` - see `max_retry`.
- `CLUSTER_RECOVERED`, `REPLICA_ADDED`, `INDEX_REOPENED` - normal transitional states; check
  again in a minute before treating them as a problem.

## When the only copy is gone

If an unassigned **primary** has `no_valid_shard_copy`, no node holds a good copy. The
options are, in order:

1. Bring the missing node back. Its data directory is the copy.
2. Restore that index from a snapshot (`es-snapshot-and-restore`).
3. `POST /_cluster/reroute` with `allocate_stale_primary` or `allocate_empty_primary`.
   **These lose data** - stale promotes an out-of-date copy, empty creates an empty shard and
   throws the contents away. Never propose either without saying plainly what is lost, and
   never run it without the user's explicit agreement. It is not reversible.

## Report

Name the index and shard, whether it is a primary or a replica, the `unassigned.reason`, the
decider and its explanation quoted from the API, the fix, and - if the fix is a cluster
change - exactly what you need the user to approve. If the answer is "add a node" or "free
disk", say that rather than proposing a setting that papers over it.
