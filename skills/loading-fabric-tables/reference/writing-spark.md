# Writing Delta tables (Spark and delta-rs)

## Contents

- Why writes belong in Spark, not on a laptop
- Where the table must live
- Spark write modes
- Upsert with MERGE
- Partitioning — when not to
- Schema evolution
- Writing with delta-rs
- Notebook naming as documentation
- After the write

## Why writes belong in Spark, not on a laptop

A Lakehouse accepts writes from anything that can speak Delta, including a local
Python process. Prefer Spark inside Fabric anyway:

- Maintenance is attached. A notebook or pipeline can chain OPTIMIZE, VACUUM, and the
  endpoint metadata refresh onto the load; a laptop script leaves the table degrading.
- It is observable. Pipeline runs appear in the Monitoring hub with history; an ad-hoc
  local run leaves no trace when someone asks what happened last Tuesday.
- Small-file behaviour is better, and the write is where that problem is created.

Local `deltalake` writes are legitimate for backfills, one-off fixes, and tests. Do not
build a production pipeline on them.

## Where the table must live

Under `Tables/` — the SQL analytics endpoint exposes nothing else. A Delta table under
`Files/` works for Spark and is invisible to every SQL consumer, which is a confusing
failure to debug later.

On a
[schema-enabled lakehouse](https://learn.microsoft.com/en-us/fabric/data-engineering/lakehouse-schemas)
the path is `Tables/<schema>/<table>`; on a classic lakehouse it is `Tables/<table>`.
Schema-enablement is fixed at creation.

**Writing to a new path creates the schema.** There is no `CREATE SCHEMA` step and no
portal action — measured: a single `write_deltalake` to
`Tables/<new_schema>/<table>` made `<new_schema>` appear in the `Tables/` listing
immediately, and in `INFORMATION_SCHEMA` after the usual metadata lag.

Convenient, and it means **a typo in the schema name silently creates a second schema**
rather than failing. Derive the schema name from configuration, never from an inline
string literal at the call site, and list `Tables/` after a first-time write to confirm
you created what you intended.

## Spark write modes

```python
# append — incremental loads
df.write.format("delta").mode("append").saveAsTable("schema_name.table_name")

# overwrite — full reload
df.write.format("delta").mode("overwrite").saveAsTable("schema_name.table_name")

# replaceWhere — overwrite one partition only
(df.write.format("delta").mode("overwrite")
   .option("replaceWhere", "EtlDate >= '2026-01-01'")
   .saveAsTable("schema_name.table_name"))
```

`replaceWhere` is the right tool for reprocessing a date range: idempotent for that
range, and it does not touch the rest of the table. Reprocessing by
`DELETE` + `append` costs two commits and leaves a window where the data is missing.

## Upsert with MERGE

In Spark, `MERGE` is the natural upsert and it preserves first-seen values:

```python
from delta.tables import DeltaTable

target = DeltaTable.forName(spark, "schema_name.customer")
(target.alias("t")
   .merge(source.alias("s"), "t.CustomerNo = s.CustomerNo")
   .whenMatchedUpdate(set={
       "FullName":    "s.FullName",
       "UpdatedUser": "'etl'",
       "UpdatedDate": "current_timestamp()",
       "EtlDate":     "lit_run_time"})
   .whenNotMatchedInsertAll()
   .execute())
```

Note what is *not* in `whenMatchedUpdate`: `InsertedDate` and `InsertedUser`. Leaving
them out is the point — they must keep their original values, and this is the main
advantage over delete-then-insert.

Frequent MERGEs generate deletion vectors and small files. Schedule maintenance on
tables that are merged often.

## Partitioning — when not to

The most common self-inflicted performance wound is partitioning a small table by a
high-cardinality column. Each partition becomes a directory with its own small files,
and you manufacture exactly the problem `OPTIMIZE` exists to clean up.

Guidance:

- Under a few GB: **do not partition**. Delta statistics and file skipping are enough.
- Larger: partition on a **low-cardinality** column that queries actually filter on —
  typically a date at day or month granularity, or a tenant/term code.
- Never partition on an identifier, a timestamp with time-of-day, or anything with
  thousands of distinct values.

Target roughly 100 MB–1 GB per partition. Fewer, bigger files beat more, smaller ones
on every read path Fabric has.

```python
(df.write.format("delta").mode("overwrite")
   .partitionBy("RegionShortCode")
   .saveAsTable("schema_name.order"))
```

Partitioning is baked in at creation; changing it means rewriting the table. Decide
deliberately, and default to not partitioning.

## Schema evolution

```python
(df.write.format("delta").mode("append")
   .option("mergeSchema", "true")
   .saveAsTable("schema_name.table_name"))
```

`mergeSchema` adds new columns automatically. Convenient, and it will silently absorb
an upstream typo as a brand-new column — so treat schema drift as something to alert
on, not something to accept quietly. Compare the column set against the previous run
and report differences.

New columns appear in the SQL endpoint after a metadata refresh. An **unenforced
foreign key on the endpoint can block that automatic schema update** — drop the FK on
tables whose schema evolves.

Type changes are not schema evolution. Widening `int` to `bigint` requires a rewrite,
and the SQL endpoint's type mapping (no `nvarchar`, no `datetime`, no
`datetimeoffset`) constrains what Delta types are usable in the first place.

## Writing with delta-rs

For backfills and fixes outside Spark:

```python
from deltalake import write_deltalake

write_deltalake(
    uri, arrow_table, mode="append",
    storage_options={"bearer_token": storage_token, "use_fabric_endpoint": "true"})
```

Same `storage_options` as reading. Requires OneLake write permission, which Viewer does
not grant. No V-Order, and no automatic maintenance — run `dt.optimize.compact()`
afterwards (see [maintenance.md](maintenance.md)).

Verified working through `delta-rs` against a Fabric lakehouse:
`mode="overwrite"`, `mode="append"`, and the `replaceWhere` equivalent
`predicate="col = 'value'"` with `mode="overwrite"`.

**The timestamp trap lives here.** PyArrow's default timestamp is timezone-naive, which
Delta records as `timestamp_ntz`, which the SQL analytics endpoint **omits entirely** —
no error, column simply absent. Always attach a timezone:

```python
pa.array(values, pa.timestamp("us", tz="UTC"))     # not pa.timestamp("us")
```

Full measured mapping: [delta-to-sql-types.md](delta-to-sql-types.md).

Docs: [delta-rs](https://delta-io.github.io/delta-rs/).

## Notebook naming as documentation

Name the load job for its pattern and cadence, so an operator reading a failed run in
the Monitoring hub knows the blast radius without opening anything:

```
CreateDeltaTable_Truncate            full reload
CreateDeltaTable_Incremental         incremental, event-driven
CreateDeltaTable_Incremental_Daily   incremental, daily
CreateDelta_Hourly                   hourly
InitialInsert                        one-off seed — should never run on a schedule
RefreshMetadataNotebook              endpoint metadata refresh
```

One job per cadence, not one job with branching. A failed hourly run should not block
the daily one, and separate jobs give separate run histories and separate alerts.

## After the write

Always, in this order:

1. Verify — row count and `MAX(EtlDate)` for the batch you just wrote.
2. Maintain — OPTIMIZE (V-Order for read-heavy tables), VACUUM on schedule.
3. Refresh SQL endpoint metadata, as the final step of the chain.

See [reference/maintenance.md](maintenance.md).
