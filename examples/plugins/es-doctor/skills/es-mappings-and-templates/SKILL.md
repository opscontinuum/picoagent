---
name: es-mappings-and-templates
description: Runbook for Elasticsearch mapping and template problems - which template an index actually won, mapping explosion, text vs keyword, and why the fix is always a new index
---
# Mappings and templates

Symptoms that land here: a field cannot be aggregated on, a new index has the wrong shard
count, documents are being rejected, a field is missing from search results, or the cluster
state has grown enormous. They all come from *what was decided when the index was created*,
which is why they cannot be fixed in place.

## 1. Which template did this index actually get?

`es_templates simulate_index=<the index name>` resolves it. The tool prints the resulting
settings, the winning template by priority, and the templates that also matched and lost
([simulate index template](https://www.elastic.co/guide/en/elasticsearch/reference/current/indices-simulate-index.html)).

How Elasticsearch picks:

- Composable index templates win by **`priority`**; highest wins, and only one applies.
- A composable template pulls in its `composed_of` component templates in order, later ones
  overriding earlier, and the template's own `template` block overriding those.
- **Legacy (v1) templates lose to any composable template that matches.** They only apply
  when no composable template does. A leftover `filebeat-*` v1 template with a broad pattern
  is the usual explanation for "my index has three shards and I never asked for three".
- Two composable templates with the same priority and overlapping patterns is a
  configuration error; the `overlapping` list is where you see it.

`es_templates` with no arguments lists all three kinds so you can see the overlaps at once.

## 2. Data streams with no template

The tool flags data streams with no template recorded. Their backing indices were created
from something else - a legacy template or cluster defaults - so their mappings are not what
the data stream's owner thinks they are. Every future rollover repeats it.

## 3. Mapping explosion

`es_index_inspect index=<index> view=mappings` gives the leaf-field count, the count by type,
the `dynamic` setting and the field limit. The tool warns at 80% of
`index.mapping.total_fields.limit`.

What causes it: `dynamic: true` (the default) over documents whose *keys* carry data -
per-user fields, per-request-id fields, flattened JSON from an unknown producer. Every new
key becomes a new field, forever, in the cluster state, on the master node.

Raising `index.mapping.total_fields.limit` is not the fix. It postpones the failure and
enlarges the cluster state, which is master-node heap (`es-node-pressure`). The fixes are:

- `dynamic: false` - unknown fields are stored in `_source` and returned, but not indexed or
  searchable. Usually the right default for data you do not control.
- `dynamic: strict` - a document with an unknown field is rejected. Right when the schema is
  a contract and a violation should be loud.
- The `flattened` field type - one field holding an arbitrary object, searchable as
  key/value pairs, with no mapping growth. Right for genuinely open-ended keys.
- Fix the producer so keys stop being data.

## 4. `text` versus `keyword`

- `text` is analysed: broken into terms, searchable by phrase and relevance, **not**
  aggregatable or sortable without loading fielddata into heap (and the `fielddata` circuit
  breaker exists precisely to stop that).
- `keyword` is exact: aggregatable, sortable, usable in `term` filters, not analysed.

"I can't aggregate on this field" and a tripped `fielddata` breaker are the same problem.
The standard shape is a `text` field with a `keyword` sub-field, which is what dynamic
mapping produces by default (`field` and `field.keyword`) - so check whether the sub-field
already exists before proposing a mapping change.

`ignore_malformed: true` lets a document with one bad value index anyway rather than failing
the whole document. Useful on ingest you do not control; it also silently hides bad data, so
say that when proposing it.

## 5. Why the fix is a new index

An existing field's mapping cannot be changed. Not the type, not the analyser, not `text` to
`keyword`. The data is already indexed in the old structure and Elasticsearch will not
rewrite it. You can only **add** new fields to an existing mapping.

So every real fix is one of:

1. **Change the template, then roll over.** For data streams and rolling indices this is the
   whole answer: update the component or index template, then roll over so the next backing
   index is created with the new mapping. Old data keeps the old mapping and ages out.
   Nothing is reindexed, nothing is lost.
2. **New index plus reindex.** When the existing data has to be queryable the new way.
   Create the index with the mapping you want, reindex into it, swap the alias. The reindex
   is expensive and goes through `es_request` with the user's agreement.
3. **Add a sub-field or a new field** and populate it going forward, if history does not
   matter.

Never propose editing a mapping in place, and when you propose a rollover or reindex, say
which data will and will not have the new mapping afterwards.

## Report

Name the index, which template it won and which ones overlapped, the specific mapping fact
(field count against the limit, the field's actual type, `dynamic` setting), which of the
three fixes applies, and what it costs: a rollover costs nothing and only helps new data; a
reindex costs time and disk and needs approval.
