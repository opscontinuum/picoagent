---
name: es-ilm-and-retention
description: Runbook for Elasticsearch index lifecycle management - indices stuck in ERROR, rollover that is not happening, and disk filling because retention is not deleting
---
# ILM and retention

Two questions live here: *why is this index stuck* and *why is the disk filling*. They meet
in the same place, because an index that never rolls over never ages into the delete phase.

## 1. State

`es_ilm` reports whether ILM is running
([ILM status](https://www.elastic.co/guide/en/elasticsearch/reference/current/ilm-get-status.html):
`RUNNING`, `STOPPING`, `STOPPED`) and one row per managed index with its
phase/action/step, age, failing step and reason
([explain lifecycle](https://www.elastic.co/guide/en/elasticsearch/reference/current/ilm-explain-lifecycle.html)).
Errors come first. `es_ilm only_errors=true` narrows to just those.

Check three things before diagnosing anything:

- **Is ILM running at all?** `STOPPED` means somebody stopped it, usually for an upgrade and
  then forgot. No index will progress. Restarting it is a cluster change (`es_request`,
  user's decision).
- **How many indices are unmanaged?** The tool counts them. An index nobody's policy covers
  will grow until the disk does. This is the most common cause of "retention isn't working":
  the policy is fine, the index was never attached to it.
- **Is an index stuck in `ERROR`?** `step: "ERROR"` with `failed_step` and
  `step_info.reason`. Read the reason - it is specific and usually names the fix.

## 2. The reasons you will actually see

- **`index.lifecycle.rollover_alias [x] does not point to index [y]`** - the index has a
  policy with a rollover action but its write alias is missing, points somewhere else, or no
  index is marked `is_write_index`. Fix the alias, then retry. This is the single most common
  ILM error and it is a configuration mistake, not a cluster fault.
- **Rollover conditions never met** - the index sits in `hot/rollover/check-rollover-ready`
  forever because the policy's `max_age` / `max_primary_shard_size` / `max_docs` are larger
  than the data will ever reach. Not an error; the step is simply waiting. `es_ilm
  policy=<name>` prints the conditions so you can compare them with
  `es_index_inspect view=stats`.
- **`illegal_argument_exception` after a policy edit** - a phase or action was changed in a
  way the index's cached phase definition cannot execute. The index carries the policy
  version it entered the phase with (`phase_execution.version`), so an edit does not
  retroactively apply.
- **`shrink` or `forcemerge` stuck** - these need free space and a node that can hold every
  shard. A cluster near a watermark cannot shrink. Check `es-node-pressure` first.
- **`wait-for-active-shards` / allocation steps not completing** - the lifecycle step is
  waiting on allocation, which means this is really an `es-unassigned-shards` problem.
  Do not retry ILM; fix the allocation.
- **Searchable-snapshot or frozen-tier steps failing** - the repository or the tier is
  missing. `es-snapshot-and-restore`.

## 3. The fix ladder

Always in this order. Skipping a rung moves the index without fixing anything, and the next
step fails the same way.

1. **Fix the cause.** The alias, the missing node, the free space, the repository.
2. **Retry the step**: `POST /<index>/_ilm/retry`, through `es_request`, with the user's
   confirmation. This re-runs the failed step from the beginning. It is safe and it is the
   normal remedy - but only after step 1, or it fails again immediately.
3. **Move the index**: `POST /_ilm/move/<index>` with an explicit target step. Last resort.
   It skips work the policy intended to do, so say what is being skipped before proposing
   it. User's decision, always.

`es_ilm` itself never calls retry, move or stop. It reads.

## 4. Disk filling because retention is not deleting

Work backwards from the space:

1. `es_indices` - which indices are large, and are they what you expect? A data stream that
   never rolled over shows up as one enormous backing index.
2. `es_ilm` - is that index managed? By which policy?
3. `es_ilm policy=<name>` - what is `delete.min_age`, and is there a `delete` phase at all?
   A policy with hot and warm phases and no delete keeps everything forever, correctly.
4. `es_index_inspect index=<index> view=stats` - actual size and document count, to compare
   against what the policy assumed.

The arithmetic that matters: an index is deleted `delete.min_age` after its *rollover*, not
after it was created. If rollover is stuck, `min_age` never starts counting, and a 30-day
retention policy will hold data forever. That is why a stuck rollover and a full disk are
usually the same incident.

Deleting data to reclaim space is the user's decision, every time, including when a policy
would have deleted it anyway.

## Report

Name the indices, whether they are managed and by what, the exact `step_info.reason` quoted
from the API, which rung of the fix ladder applies, and what you need approved. When the
cause is an unmanaged index or a policy with no delete phase, say that the system is doing
what it was told to do - the fix is a decision about retention, not a repair.
