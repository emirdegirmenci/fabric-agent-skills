---
name: loading-fabric-tables
description: Use when writing data into Microsoft Fabric — running INSERT/UPDATE/DELETE or CTAS against a Warehouse, loading Delta tables in a Lakehouse, building full-reload or incremental or upsert pipelines, stamping ETL audit columns, or fixing slow queries caused by unmaintained tables (OPTIMIZE, VACUUM, statistics, SQL endpoint metadata refresh).
---

# Loading Fabric tables

## Hard rules

Violating any of these produces silent data loss or silently wrong results — no error,
no warning. They are measured, not stylistic.

1. **Write timestamps timezone-aware, always.** A `timestamp_ntz` column is
   **completely invisible** to the SQL analytics endpoint. PyArrow's default is naive,
   so the wrong thing is the default: use `pa.timestamp("us", tz="UTC")`.
2. **Never `sleep()` to wait for the SQL endpoint.** Measured lag was ~116 seconds — a
   30-second sleep passes and the table is still absent. Refresh endpoint metadata as
   the pipeline's final step instead.
3. **Verify the column set after every load**, not just the row count. Compare
   `INFORMATION_SCHEMA.COLUMNS` against the Delta schema; a missing name is how rule 1
   fails.
4. **Stamp `EtlDate` once per run**, captured before the first write. Per-row
   `SYSUTCDATETIME()` splits one batch across timestamps and breaks reconciliation.
5. **Soft-delete (`IsDeleted = 1`), never `DELETE`,** for source-driven removals.
6. **Put the load pattern in the table name** (`*Truncate`, `*History`, `*Current`) so a
   rerun's safety is visible without reading the pipeline.
7. **Never target a Lakehouse SQL analytics endpoint for writes.** It rejects all DDL and
   DML by design; no role changes that.
8. **Run OPTIMIZE after loads that append small batches.** Unmaintained tables are the
   normal cause of "the report got slow", and no query tuning fixes it.

Measured type mapping and the rules behind 1–3:
[reference/delta-to-sql-types.md](reference/delta-to-sql-types.md).

---

Two decisions determine everything else, and getting them wrong is expensive to undo.

## Decision 1: where can you even write?

| Target | Writes? | How |
|---|---|---|
| **Warehouse** | yes | T-SQL: `CREATE TABLE AS SELECT`, `INSERT`, `UPDATE`, `DELETE`, `COPY INTO` |
| **Lakehouse** | yes | Spark / notebook / pipeline / `deltalake` writer — **never T-SQL** |
| **Lakehouse SQL analytics endpoint** | **no** | read-only by design; DDL fails with `Msg 368 ... external policy action ... denied` |

The endpoint's refusal is architectural, not a permission gap. Do not chase it with
role changes: choose a Warehouse for T-SQL writes, or write the Lakehouse through
Spark.

Reading and connecting is a separate concern — see the **connecting-to-fabric** skill.

## Decision 2: which load pattern per table?

Declare this per table and put it in the table's name, so the pattern is visible
without reading the pipeline. Four patterns cover nearly everything:

| Pattern | When | Name it | Mechanics |
|---|---|---|---|
| **Full reload** | small or unreliable source; no dependable change marker | `<Entity>Truncate` | truncate + insert, one transaction |
| **Incremental append** | append-only source with a monotonic marker | `<Entity>` | insert rows where `marker > last watermark` |
| **Upsert** | source rows mutate and you keep one current row | `<Entity>` | delete-then-insert the changed keys |
| **Snapshot / current** | you keep history *and* need "latest" fast | `<Entity>History` + `<Entity>Current` | append to history, rebuild current |

Naming the pattern is a real engineering practice, not cosmetics: an operator seeing
`InvoiceTruncate` knows a rerun is safe and idempotent, while a bare
`Invoice` implies a watermark that a blind rerun could double-count.

Full mechanics, watermark handling, and idempotency rules:
[reference/load-patterns.md](reference/load-patterns.md).

## Always stamp audit columns

Every loaded table carries the same block, in the same order, at the end of the
column list — after the business columns:

```sql
InsertedUser  varchar(200),   -- who/what created the row
InsertedDate  datetime2(6),   -- when it first arrived
UpdatedUser   varchar(200),   -- who/what last changed it
UpdatedDate   datetime2(6),   -- when it last changed
IsDeleted     bit,            -- soft delete; never DELETE source-driven rows
EtlDate       datetime2(6)    -- when this ETL run touched the row
```

Why each one earns its place:

- `EtlDate` is the batch marker. It answers "did last night's run reach this table?"
  without joining to a log, and it makes a partial reload provable.
- `Inserted*` / `Updated*` separate first-seen from last-changed. Without both, you
  cannot tell a new row from a re-delivered one.
- `IsDeleted` keeps deletes reversible. A source that drops a row for a bad reason
  should not silently destroy warehouse history — filter `WHERE IsDeleted = 0` in
  consuming views instead.

Consistency is the point. When the block is identical everywhere, monitoring,
freshness checks, and reconciliation queries are written once and work on every table.

Stamp `EtlDate` with a **single value per run**, captured once at the start —
`SYSUTCDATETIME()` evaluated per row makes rows from the same batch look different
and breaks batch-level reconciliation.

## After loading, refresh endpoint metadata

The SQL analytics endpoint caches Delta metadata. A table written seconds ago can be
missing rows, or missing entirely, when queried.

Make a metadata refresh the last step of the pipeline. **Do not insert sleeps** — the
lag is not a fixed duration, so a sleep is both slower than necessary and
occasionally too short. This is why mature Fabric workspaces contain an explicit
refresh step at the end of every load.

## Then maintain the table

Unmaintained Delta tables are the usual root cause of "the report got slow", and no
amount of query tuning fixes it. Frequent small writes leave many small files; each
query then pays per-file overhead.

- **Lakehouse**: run `OPTIMIZE` (compaction, plus V-Order) and `VACUUM` (remove
  obsolete files) on a schedule.
- **Warehouse**: compaction is automatic. Your lever is statistics, not file layout.

OPTIMIZE is available from `delta-rs` too, not only Spark and the portal — measured on a
4-file table: **4 files → 1 file, 1,944 → 513 bytes**, same row count.

```python
dt.optimize.compact()          # returns metrics: numFilesAdded / numFilesRemoved
dt.vacuum(retention_hours=168, dry_run=True)   # list first; 168h = the 7-day default
```

File count per table is the cheapest health signal. Note the API shape in
`deltalake` 1.x — `get_add_actions()` returns an **arro3** table, not a PyArrow one, so
`.to_pylist()` does not exist and a naive call fails:

```python
import pyarrow as pa
adds = pa.table(dt.get_add_actions(flatten=True))   # convert first
files = adds.num_rows
rows  = sum(x or 0 for x in adds.column("num_records").to_pylist())
bytes_ = sum(x or 0 for x in adds.column("size_bytes").to_pylist())
```

Hundreds of files on a small table means it needs `OPTIMIZE`.

Details, retention rules, and the time-travel trade-off:
[reference/maintenance.md](reference/maintenance.md).

## Writing checklist

```
- [ ] Target accepts writes (Warehouse, or Lakehouse via Spark)
- [ ] Load pattern chosen and reflected in the table name
- [ ] Rerunning the load is safe (idempotent, or watermark-guarded)
- [ ] All timestamps written timezone-aware (tz="UTC") — rule 1
- [ ] Audit block present, EtlDate stamped once per run
- [ ] Soft delete, not hard delete, for source-driven removals
- [ ] Types conform to Fabric's supported set (no nvarchar/datetime/money)
- [ ] Column set verified against the Delta schema after load, not just row count
- [ ] Endpoint metadata refreshed as the final step (no sleeps)
- [ ] OPTIMIZE/VACUUM scheduled (Lakehouse) or statistics reviewed (Warehouse)
```

## T-SQL specifics

`CREATE TABLE AS SELECT` is the workhorse for building derived tables, and `MERGE` is
generally available in Warehouse — use it for upserts rather than hand-rolling one.
`#temp` tables need `WITH (DISTRIBUTION = ROUND_ROBIN)` before
`INSERT INTO #t SELECT` works. Constraints are accepted only as `NOT ENFORCED`, so
uniqueness must be asserted in the load, and `ALTER TABLE` is limited to adding
nullable columns and dropping columns.

Statements, transaction semantics, `COPY INTO`, and the surface limits that bite
during loads: [reference/writing-tsql.md](reference/writing-tsql.md).

## Spark / Delta specifics

Partition deliberately — partitioning a small table by a high-cardinality column is
the most common self-inflicted performance wound, because it manufactures the
small-file problem you then have to `OPTIMIZE` away. A table under a few GB usually
wants no partitioning at all.

Writer options, overwrite vs append vs `replaceWhere`, schema evolution, and why
ad-hoc laptop writes are discouraged:
[reference/writing-spark.md](reference/writing-spark.md).

## Reference

- [reference/delta-to-sql-types.md](reference/delta-to-sql-types.md) — **measured** Delta→SQL type mapping and the five hard rules
- `scripts/type_mapping_probe.py` — run it; re-measures all of the above in *your* environment, writing only inside a scratch schema you name
- [reference/load-patterns.md](reference/load-patterns.md) — the four patterns, watermarks, idempotency
- [reference/writing-tsql.md](reference/writing-tsql.md) — CTAS, INSERT/UPDATE/DELETE, transactions, COPY INTO
- [reference/writing-spark.md](reference/writing-spark.md) — Delta writes, partitioning, schema evolution
- [reference/maintenance.md](reference/maintenance.md) — OPTIMIZE, VACUUM, statistics, metadata refresh

### Official documentation

- [Load data into a Lakehouse](https://learn.microsoft.com/en-us/fabric/data-engineering/lakehouse-notebook-load-data)
- [Lakehouse table maintenance](https://learn.microsoft.com/en-us/fabric/data-engineering/lakehouse-table-maintenance)
- [T-SQL surface area](https://learn.microsoft.com/en-us/fabric/data-warehouse/tsql-surface-area) ·
  [Data types](https://learn.microsoft.com/en-us/fabric/data-warehouse/data-types)
- [Statistics in Fabric DW](https://learn.microsoft.com/en-us/fabric/data-warehouse/statistics) ·
  [SQL endpoint performance](https://learn.microsoft.com/en-us/fabric/data-warehouse/sql-analytics-endpoint-performance)
- [Query insights](https://learn.microsoft.com/en-us/fabric/data-warehouse/query-insights)
- [delta-rs writer](https://delta-io.github.io/delta-rs/)
