# Load patterns

## Contents

- Choosing a pattern
- Pattern 1: full reload (`*Truncate`)
- Pattern 2: incremental append
- Pattern 3: upsert (delete-then-insert)
- Pattern 4: history + current (`*History` / `*Current`)
- Watermarks
- Idempotency
- Late-arriving and out-of-order data
- Deletes
- Orchestration cadence

## Choosing a pattern

Ask two questions about the source, in this order:

1. **Can I detect what changed, reliably?** If no → full reload. Everything else
   depends on a trustworthy change marker, and a marker you only *believe* is
   trustworthy produces silent data loss that surfaces months later.
2. **Do rows mutate after first delivery?** No → append. Yes → upsert, or
   history+current if the old values matter.

Prefer the simplest pattern the source permits. Full reload of a ten-million-row table
is often cheaper in engineering time and far cheaper in incident time than an
incremental load whose watermark is subtly wrong.

## Pattern 1: full reload (`*Truncate`)

Truncate and reinsert. Naturally idempotent — rerun as often as you like.

```sql
BEGIN TRANSACTION;
    TRUNCATE TABLE dbo.InvoiceTruncate;
    INSERT INTO dbo.InvoiceTruncate
        (InvoiceId, ..., InsertedUser, InsertedDate, UpdatedUser, UpdatedDate,
         IsDeleted, EtlDate)
    SELECT s.InvoiceId, ..., 'etl', @RunTime, 'etl', @RunTime, 0, @RunTime
    FROM   staging.Invoice AS s;
COMMIT TRANSACTION;
```

The `Truncate` suffix is the contract: readers know the table is fully rebuilt each
run, so a missing row means it is missing at the source, not that a delta was skipped.

Use when: reference and lookup data, small dimensions, sources with no change marker,
or any source whose change marker you have not yet proven.

Cost: full read every run. Trade CPU for correctness knowingly.

## Pattern 2: incremental append

Only for append-only sources with a monotonic marker.

```sql
DECLARE @Watermark datetime2(6) =
    (SELECT ISNULL(MAX(SourceModifiedDate), '1900-01-01') FROM dbo.Order);

INSERT INTO dbo.Order (..., InsertedUser, InsertedDate, IsDeleted, EtlDate)
SELECT ..., 'etl', @RunTime, 0, @RunTime
FROM   staging.Order
WHERE  SourceModifiedDate > @Watermark;
```

Use `>` with a watermark stored to full precision, or `>=` combined with a key-based
anti-join. `>=` alone re-inserts boundary rows on every run.

Failure mode to design against: a source that updates rows in place without advancing
the marker. Then this pattern silently misses changes. Verify before choosing it —
compare a full count and a checksum against the source at least once.

## Pattern 3: upsert (delete-then-insert)

**In a Warehouse, use `MERGE`** — it is generally available and expresses the intent
directly:

```sql
MERGE dbo.Customer AS t
USING #chg AS s ON s.CustomerNo = t.CustomerNo
WHEN MATCHED THEN UPDATE SET
    t.FullName = s.FullName, t.UpdatedUser = 'etl',
    t.UpdatedDate = @RunTime, t.EtlDate = @RunTime
WHEN NOT MATCHED THEN INSERT
    (CustomerNo, FullName, InsertedUser, InsertedDate, UpdatedUser, UpdatedDate,
     IsDeleted, EtlDate)
    VALUES (s.CustomerNo, s.FullName, 'etl', @RunTime, 'etl', @RunTime, 0, @RunTime);
```

`MERGE` preserves `InsertedDate` on matched rows, which the delete-then-insert
alternative below does not.

Delete-then-insert remains useful when the target is not a Warehouse, or when the
change set is large enough that a rewrite beats a row-wise match:

```sql
CREATE TABLE #chg WITH (DISTRIBUTION = ROUND_ROBIN) AS
SELECT * FROM staging.Customer WHERE SourceModifiedDate > @Watermark;

BEGIN TRANSACTION;
    DELETE t FROM dbo.Customer AS t
    INNER JOIN #chg AS c ON c.CustomerNo = t.CustomerNo;

    INSERT INTO dbo.Customer (..., InsertedUser, InsertedDate, UpdatedUser, UpdatedDate,
                            IsDeleted, EtlDate)
    SELECT ..., 'etl', @RunTime, 'etl', @RunTime, 0, @RunTime
    FROM   #chg;
COMMIT TRANSACTION;
```

Both statements in one transaction, or a mid-run failure leaves rows deleted and not
reinserted.

The `CREATE TABLE ... WITH (DISTRIBUTION = ROUND_ROBIN)` is required —
`INSERT INTO #temp SELECT` fails on a non-distributed temp table.

Caveat this pattern hides: it overwrites `InsertedDate` with the current run time,
losing first-seen. If first-seen matters, carry the existing value forward from the
row being replaced, or use history+current instead.

In Spark, `DeltaTable.merge(...)` is available and is the better tool — the
delete-then-insert dance is a T-SQL workaround.

## Pattern 4: history + current

Keep every version, and materialise "latest" for consumers.

```sql
-- 1. append every delivered version, never update
INSERT INTO dbo.InvoiceHistory (..., EtlDate) SELECT ..., @RunTime FROM #chg;

-- 2. rebuild the current view of the world
BEGIN TRANSACTION;
    TRUNCATE TABLE dbo.InvoiceCurrent;
    INSERT INTO dbo.InvoiceCurrent
    SELECT * FROM (
        SELECT *, ROW_NUMBER() OVER (PARTITION BY InvoiceId
                                     ORDER BY EtlDate DESC) AS rn
        FROM dbo.InvoiceHistory
    ) x WHERE rn = 1;
COMMIT TRANSACTION;
```

`*Current` is derived and disposable — it can always be rebuilt from `*History`, which
makes it safe to truncate. This is why the split is worth the storage: history is the
source of truth, current is a cache.

A lighter variant flags the winner in place with `IsCurrent bit` instead of keeping a
second table. Cheaper on storage, but every reader must remember
`WHERE IsCurrent = 1`, and the ones who forget produce quietly wrong numbers. Prefer
the separate `*Current` table, or expose an `IsCurrent = 1` view and grant access only
to the view.

## Watermarks

Store watermarks where they survive a failed run and can be inspected:

- **Derived** — `MAX(marker)` from the target. No extra state, self-healing, but reads
  the target every run and cannot express "reload the last 3 days".
- **Explicit control table** — one row per table with `LastWatermark`, `LastRunStatus`,
  `RowsLoaded`. More moving parts, but it makes reprocessing a data change rather than
  a code change, and it gives operators something to look at.

Rules that prevent the classic bugs:

- Capture `@RunTime` **once** at the start of the run and reuse it. Per-row
  `SYSUTCDATETIME()` splits one batch across timestamps and breaks reconciliation.
- Advance the watermark only after the load commits.
- Compare in one timezone. Mixing local and UTC markers loses or duplicates rows
  twice a year, and `datetimeoffset` is unsupported in Fabric so the offset is not
  there to save you. Normalise to UTC at ingestion.

## Idempotency

Any load may run twice — retries, manual reruns, overlapping schedules. Make the
second run harmless:

| Pattern | Idempotent? | What makes it safe |
|---|---|---|
| Full reload | yes | truncate discards the previous attempt |
| Append | only with a watermark | strict `>` against a committed watermark |
| Upsert | yes | delete-then-insert converges on the same result |
| History+current | history no, current yes | dedupe history on (key, source version) |

Test it deliberately: run the load twice against unchanged source and assert the row
count did not move. This one check catches most watermark errors before production.

## Late-arriving and out-of-order data

A strict watermark permanently skips rows that arrive stamped earlier than the
watermark. Two defences:

- **Lookback window** — reload `watermark - N` instead of `watermark`, sized to the
  worst observed lateness. Requires upsert semantics, since rows will be re-delivered.
- **Periodic reconciliation** — a scheduled full reload (weekly, monthly) that repairs
  whatever the incremental path missed. Cheap insurance, and it also catches
  in-place source updates that never advanced the marker.

## Deletes

Prefer `IsDeleted = 1` over `DELETE`:

```sql
UPDATE t SET IsDeleted = 1, UpdatedUser = 'etl', UpdatedDate = @RunTime, EtlDate = @RunTime
FROM   dbo.Customer AS t
WHERE  NOT EXISTS (SELECT 1 FROM staging.Customer s WHERE s.CustomerNo = t.CustomerNo);
```

Reasons this is the default: a source-side bug that drops rows should not destroy
warehouse history; downstream reports keep referential integrity; and someone can
answer "when did this disappear?".

The cost is that every consumer must filter. Enforce it structurally — expose views
that already apply `WHERE IsDeleted = 0` and point consumers at the views, rather than
trusting each report author to remember.

Detecting deletes requires a full key list from the source. With a purely incremental
feed you cannot see absence, which is another reason for periodic full reconciliation.

## Orchestration cadence

Name the schedule into the job, not just the table — `CreateDeltaTable_Incremental_Daily`
and `CreateDelta_Hourly` tell an operator the blast radius of a failure before they
open anything. Keep one job per cadence rather than one job with branching logic;
a failed hourly run should not block the daily one.

Ordering: staging load → transform → dependent tables → **metadata refresh** →
maintenance. The metadata refresh belongs at the end of the chain, not after each
table, so consumers see a consistent set.
