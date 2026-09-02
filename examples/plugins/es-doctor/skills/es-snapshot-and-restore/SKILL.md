---
name: es-snapshot-and-restore
description: Runbook for Elasticsearch snapshots - reading repository and SLM state, PARTIAL vs FAILED, the pre-checks a restore needs, and watching a restore finish
---
# Snapshots and restore

Answer two questions in order: *do we have a good backup of this data*, and only then *how
do we put it back*. Restores are one-way from the user's point of view and they overwrite,
so nothing here happens without them.

## 1. What exists

`es_snapshots` lists repositories. `es_snapshots repository=<name>` adds the most recent
snapshots newest-first, whatever is running now, and SLM policies with their last success and
last failure ([get snapshot](https://www.elastic.co/guide/en/elasticsearch/reference/current/get-snapshot-status-api.html),
[SLM get policy](https://www.elastic.co/guide/en/elasticsearch/reference/current/slm-api-get-policy.html)).

No repository registered means there are no backups. Say that first and plainly; nothing
else on this page applies.

Repository types tell you where the data is: `fs` (a shared filesystem every node must
mount), `s3`, `gcs`, `azure`, `hdfs`, `url` (read-only). A `fs` repository that only some
nodes can reach produces snapshots that fail on those nodes' shards - that is the classic
PARTIAL.

## 2. Reading snapshot state

- **`SUCCESS`** - every shard was snapshotted. Restorable.
- **`PARTIAL`** - some shards failed. The snapshot exists and can be restored, but the
  indices whose shards failed will come back incomplete. The tool prints the failures with
  the index, shard and reason. A PARTIAL snapshot is not a backup of the indices it failed
  on; treat it as a backup of the rest.
- **`FAILED`** - not usable.
- **`IN_PROGRESS`** - still running. Progress comes from
  `_status`, which the tool asks for **only** on a running snapshot: on a finished one it
  reads every shard of every snapshot in the repository, which on object storage means one
  request per shard per snapshot and can take a very long time
  ([get snapshot status](https://www.elastic.co/guide/en/elasticsearch/reference/current/get-snapshot-status-api.html)).
  Do not reach for `_status` to answer "was last night's backup good" - the snapshot list
  already says.

`es_snapshots repository=<repo> verify=true` asks the user first, then makes every node write
a test blob to the repository. It changes no snapshot, but it is real I/O from every node.
Use it when the state is inconsistent or a repository was just registered - not routinely.

## 3. SLM

The policy line gives the schedule, repository, retention and next run; `last_success` and
`last_failure` say what actually happened. A policy whose `last_failure` is more recent than
its `last_success` has not backed anything up since, no matter how healthy it looks. The
failure `details` is a serialised exception and usually names the cause directly (a missing
index, a repository permission, an unassigned primary at snapshot time).

Retention (`expire_after`, `min_count`, `max_count`) is why old snapshots disappear. If
someone expected a snapshot to still be there, check retention before suspecting deletion.

## 4. Before restoring anything

A restore writes into the cluster. Check all of these first:

- **The target index must not exist, or must be closed.** Restoring over an open index is
  refused. So the real decision is: restore under a new name (`rename_pattern` /
  `rename_replacement`), or close and overwrite the existing one - which destroys what is
  there now.
- **Is the snapshot's state `SUCCESS` for the indices you need?** Check the per-index
  failures on a PARTIAL.
- **`include_global_state`** - restoring it replaces cluster settings, templates and ILM
  policies. Almost never what is wanted in a partial recovery, and it can silently change how
  *other* indices behave. Default to leaving it off and say why.
- **Version** - a snapshot restores into the same or a newer major version, within the
  supported range. An older cluster cannot read a newer snapshot.
- **Space and allocation** - the restored index needs shards allocated. A cluster near a disk
  watermark will restore into unassigned shards (`es-unassigned-shards`).
- **Data streams** - restoring a backing index without its data stream leaves an index that
  nothing writes to.

The restore call itself is `POST /_snapshot/<repo>/<snapshot>/_restore` through `es_request`,
which is blocked unless the user set `allow_destructive` - and it still needs their explicit
agreement on this specific restore, with the target names spelled out.

## 5. Watching it finish

`es_recovery` shows the restore as recoveries of `type: snapshot`, with stage and byte
percentage ([cat recovery](https://www.elastic.co/guide/en/elasticsearch/reference/current/cat-recovery.html)).
`stage: done` on every shard means the data is in.

**"Restore succeeded but the cluster is yellow"** is normal and expected: a restore recreates
primaries, and the replicas are then built from them. `es_recovery` will show `type: peer`
recoveries for the replicas. Yellow that does not clear is a separate allocation problem -
go to `es-unassigned-shards`; it is not a failed restore.

## Report

Say which repository and snapshot, its state, exactly which indices it covers and which (on a
PARTIAL) it does not, what the restore would overwrite or create, and what you need approved
before running it. When the answer is "there is no usable backup of this data", say it in the
first sentence.
