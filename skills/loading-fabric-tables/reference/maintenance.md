# Maintenance: OPTIMIZE, VACUUM, statistics, metadata refresh

## Contents

- The small-file problem
- OPTIMIZE (Lakehouse)
- V-Order and its cost
- VACUUM and the seven-day floor
- Deletion vectors
- Warehouse: statistics, not file layout
- Refreshing SQL endpoint metadata
- Scheduling maintenance
- Diagnosing "it got slow"

## The small-file problem

Every Delta write adds files; nothing removes them. Frequent small loads leave hundreds
or thousands of small Parquet files per table, and each query pays per-file overhead.

This is the number one cause of "the report used to be fast". It cannot be fixed with
query hints or a bigger capacity, because the cost is in file enumeration and metadata,
not compute. Fix it at the table.

Cheapest possible health check — no data transfer, straight from the transaction log.
Note that `deltalake` 1.x returns an **arro3** table here, so convert before use;
`.to_pylist()` on the raw result raises `AttributeError`:

```python
import pyarrow as pa

adds = pa.table(dt.get_add_actions(flatten=True))
print(adds.num_rows, "files,",
      sum(x or 0 for x in adds.column("size_bytes").to_pylist()) / 1e6, "MB")
```

A few hundred files on a table of a few hundred megabytes means it needs `OPTIMIZE`.
Average file size well under ~100 MB is the signal.

Maintenance applies to **Delta tables only**. Legacy Hive-format tables (Parquet, ORC,
AVRO, CSV) are not supported.

## OPTIMIZE (Lakehouse)

Compacts small files into larger ones. Three ways to run it:

```sql
-- Spark SQL, in a notebook
OPTIMIZE schema_name.table_name;
```

- **Portal**, ad hoc: Lakehouse Explorer → right-click table → **Maintenance**.
- **Pipeline**, recurring: the *Lakehouse Maintenance* activity in Data Factory
  exposes OPTIMIZE (with optional V-Order) and VACUUM, so maintenance can be chained
  onto the load that made the mess.
- **`delta-rs`**, from any Python process — no Spark session needed:

```python
metrics = dt.optimize.compact()        # numFilesAdded / numFilesRemoved
dt.vacuum(retention_hours=168, dry_run=True)   # inspect before deleting
```

Measured on a table built from four single-row appends: **4 files → 1 file,
1,944 bytes → 513 bytes**, row count unchanged. Compaction is worth running even on
tiny tables that are appended to frequently.

`delta-rs` compaction does **not** apply V-Order. For read-heavy tables prefer the
Spark/pipeline route so V-Order is applied; use `delta-rs` for ad-hoc cleanup and for
environments without Spark.

Prefer the pipeline route for anything recurring. Portal clicks are not reproducible
and do not survive the person who knew to click them.

## V-Order and its cost

V-Order applies additional sorting, encoding and compression tuned for Fabric's read
engines (Direct Lake, SQL endpoint, Power BI).

The trade-off is documented and worth stating plainly: about **15% slower average
writes**, in exchange for **up to 50% better compression**. Enable it on tables that
are read far more often than written — which is most warehouse tables — and consider
leaving it off on high-frequency staging tables that are rewritten constantly and
queried rarely.

## VACUUM and the seven-day floor

`VACUUM` deletes files no longer referenced by the Delta log and older than the
retention threshold. **Default retention is seven days.**

```sql
VACUUM schema_name.table_name;              -- uses the default retention
```

Retention is exactly the time-travel window: vacuuming to one day means you can no
longer read yesterday's version, and any long-running reader or writer holding an older
snapshot can fail.

Fabric protects you from this by default — **portal and API maintenance requests fail
for retention intervals under seven days**. Overriding requires setting
`spark.databricks.delta.retentionDurationCheck.enabled` to `false` in the Spark
properties of the workspace environment.

Treat that override as a deliberate decision with a written reason, not a way to
reclaim storage. If storage is the problem, the answer is usually OPTIMIZE plus a
sane load pattern, not a shorter safety window.

## Deletion vectors

Updates and deletes can be recorded as deletion-vector files rather than rewriting
data files — fast to write, slower to read as they accumulate. Maintenance can merge
these back into the Parquet files. Include it when the table takes frequent updates.

## Warehouse: statistics, not file layout

A Fabric **Warehouse** compacts data automatically. There is no user-invokable
`OPTIMIZE`, and `sp_showspaceused` does not exist — use the Capacity Metrics app for
space and usage.

Your lever is statistics. Fabric creates them automatically, and you can add
single-column statistics manually; **manually created multi-column statistics are not
supported**.

```sql
CREATE STATISTICS stat_Person_CustomerNo ON dbo.Customer (CustomerNo);
UPDATE STATISTICS dbo.Customer;
```

After a large load that shifts the data distribution, updating statistics on the
columns used in joins and filters is the cheapest available win. Reference:
[Statistics in Fabric DW](https://learn.microsoft.com/en-us/fabric/data-warehouse/statistics).

## Refreshing SQL endpoint metadata

The SQL analytics endpoint caches Delta metadata. Immediately after a load, a query can
return stale rows or miss the table entirely.

**Measured**: a newly written table was absent from `INFORMATION_SCHEMA` at 5s, 22s and
54s after the write, and present at 116s. So a `sleep(30)` — the instinctive fix — passes
while the table is still invisible, and the pipeline continues as if the load landed.
The same lag applies to new *columns* on an existing table, which is why a schema change
can look half-applied.

Make the refresh an explicit final step:

- **Pipeline**: the *Refresh SQL Endpoint* activity in Data Factory — chain it after
  the load and after maintenance.
- **Code**: the Refresh SQL Endpoint Metadata REST API.

**Do not use sleeps.** The lag is variable, so a fixed sleep is simultaneously slower
than needed and sometimes too short — which is exactly why this class of bug looks
intermittent and gets blamed on the network.

## Scheduling maintenance

A workable default:

| Table profile | OPTIMIZE | VACUUM |
|---|---|---|
| Frequently loaded (hourly) | daily | weekly |
| Daily loaded | weekly | weekly |
| Full-reload (`*Truncate`) | after each load | weekly |
| Rarely written reference data | monthly | monthly |

Pipeline order per run: **load → maintenance → refresh endpoint metadata**. Refresh
last, so consumers never see a half-maintained state.

Run maintenance after major ingestion, or when you observe many small files and
slower reads — not on a fixed schedule you never revisit.

## Diagnosing "it got slow"

Work in this order; each step is cheaper than the one after it:

1. **File count per table** — `get_add_actions()`. Many small files → OPTIMIZE. This is
   the answer most of the time.
2. **Stale metadata** — do Delta and SQL row counts agree? If not, refresh metadata.
3. **Statistics** (Warehouse) — did a large load change the distribution?
4. **The query itself** — `queryinsights.exec_requests_history` ranks by
   `allocated_cpu_time_ms` and `data_scanned_remote_storage_mb`. Add a query label so
   your pipeline's statements are findable:
   `OPTION (LABEL = 'etl:customer:daily')`.
5. **Partitioning** — over-partitioning manufactures small files. Removing a bad
   partition column often beats adding hardware.

Note that `queryinsights` views can be empty for a couple of minutes after a warehouse
is created; that is not a permission problem.

Reference:
[SQL endpoint performance](https://learn.microsoft.com/en-us/fabric/data-warehouse/sql-analytics-endpoint-performance) ·
[Query insights](https://learn.microsoft.com/en-us/fabric/data-warehouse/query-insights) ·
[Table maintenance](https://learn.microsoft.com/en-us/fabric/data-engineering/lakehouse-table-maintenance)
