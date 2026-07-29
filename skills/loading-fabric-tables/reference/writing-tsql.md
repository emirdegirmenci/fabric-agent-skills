# Writing with T-SQL (Warehouse)

## Contents

- Where T-SQL writes work
- CREATE TABLE AS SELECT
- INSERT / UPDATE / DELETE
- Transactions
- Temp tables — the DISTRIBUTION requirement
- COPY INTO
- What is missing and what to use instead
- Type and width traps
- Running statements from scripts

## Where T-SQL writes work

**Warehouse only.** A Lakehouse SQL analytics endpoint rejects DDL and DML:

```
Msg 368 ... The external policy action
'Microsoft.Sql/Sqlservers/Databases/Schemas/Tables/Create' was denied
```

No role changes this. Use a Warehouse, or write the Lakehouse via Spark.

## CREATE TABLE AS SELECT

CTAS is the primary way to build derived tables — one statement, no separate DDL to
keep in sync with the query:

```sql
CREATE TABLE dbo.FactOrderDaily AS
SELECT e.RegionShortCode,
       CAST(e.OrderDate AS date)      AS OrderDate,
       COUNT_BIG(*)                        AS Orders,
       CAST('etl' AS varchar(200))         AS InsertedUser,
       SYSUTCDATETIME()                    AS InsertedDate,
       CAST(0 AS bit)                      AS IsDeleted,
       SYSUTCDATETIME()                    AS EtlDate
FROM   dbo.Order AS e
GROUP BY e.RegionShortCode, CAST(e.OrderDate AS date);
```

The column types come from the SELECT, so **cast literals explicitly** — an
uncast `'etl'` becomes an oddly sized `varchar` and an uncast `0` becomes `int`
where you wanted `bit`.

To rebuild: create with a temporary name, then swap. `DROP` then `CREATE` leaves the
table missing if the create fails.

## INSERT / UPDATE / DELETE

```sql
INSERT INTO dbo.Target (ColA, ColB, InsertedUser, InsertedDate, IsDeleted, EtlDate)
SELECT s.ColA, s.ColB, 'etl', @RunTime, 0, @RunTime
FROM   staging.Source AS s
WHERE  s.SourceModifiedDate > @Watermark;
```

Always list target columns. `INSERT INTO t SELECT *` breaks the day someone adds a
column, and it breaks by inserting data into the wrong column rather than by failing.

Set-based only. Row-by-row loops in a distributed engine are pathologically slow —
express the operation as one statement over the set.

## Transactions

```sql
BEGIN TRANSACTION;
    TRUNCATE TABLE dbo.Target;
    INSERT INTO dbo.Target (...) SELECT ... FROM staging.Source;
COMMIT TRANSACTION;
```

Wrap any multi-statement load whose intermediate state is invalid — truncate+insert,
delete+insert. Without it, a failure between statements leaves an empty or partial
table that reports will happily query.

`SET TRANSACTION ISOLATION LEVEL` is unsupported; snapshot isolation is implicit.
Keep transactions short: a long-running one holds resources and increases the cost of
a failure.

## Temp tables — the DISTRIBUTION requirement

```sql
CREATE TABLE #changed WITH (DISTRIBUTION = ROUND_ROBIN) AS
SELECT * FROM staging.Source WHERE SourceModifiedDate > @Watermark;
```

Without `WITH (DISTRIBUTION = ROUND_ROBIN)`, `INSERT INTO #temp SELECT ...` fails.
This is the single most common first-time error when staging intermediate results.

`#temp` tables are session-scoped and are lost on a front-end failover, which
manifests as a query "killed" mid-pipeline. Design so a rerun recreates them.

## COPY INTO

Bulk load from external files (Parquet, CSV) into a Warehouse:

```sql
COPY INTO dbo.Staging_Person
FROM 'https://<account>.dfs.core.windows.net/<container>/customer/*.parquet'
WITH (FILE_TYPE = 'PARQUET');
```

Faster than row-oriented inserts for large volumes. Prefer Parquet over CSV: types
are carried in the file, so no parsing ambiguity and no locale surprises with decimals
or dates.

Not available on a Lakehouse SQL analytics endpoint (nor is `OPENROWSET`).

## What is missing and what to use instead

`MERGE` **is** generally available in Warehouse — prefer it over hand-rolled upserts
there. It is unavailable on a Lakehouse SQL analytics endpoint only because that
endpoint permits no DML at all.

| Missing | Use instead |
|---|---|
| `IDENTITY` / sequences | source key, `ROW_NUMBER()`, or a hash of the business key |
| Triggers | do it in the pipeline; it is visible there |
| Recursive CTEs | flatten, or iterate in the orchestrator |
| `SET ROWCOUNT` | `TOP` |
| `FOR XML` | `FOR JSON` (last operator only) |
| Materialized views | a CTAS table refreshed by the pipeline |
| Synonyms | a view |
| `BULK LOAD` | `COPY INTO` (`bcp` exists as a preview) |
| Manual multi-column statistics | single-column statistics |
| Enforced keys | constraints must be `NOT ENFORCED` — validate in the load |

Two consequences worth planning around:

**Constraints are declarations, not guarantees.** `PRIMARY KEY`, `UNIQUE` and
`FOREIGN KEY` are accepted **only with `NOT ENFORCED`**; they inform the optimiser and
document intent, and nothing stops a duplicate from being inserted. Assert uniqueness
in the load and fail the job on a non-zero result:

```sql
-- orchestrator asserts this returns 0; portable, no error-handling syntax needed
SELECT COUNT_BIG(*) AS duplicate_keys FROM (
    SELECT CustomerNo FROM dbo.Customer WHERE IsDeleted = 0
    GROUP BY CustomerNo HAVING COUNT_BIG(*) > 1
) d;
```

Also: an unenforced FK on a SQL analytics endpoint can block automatic schema updates
when new Delta columns appear. Drop it if the table's schema evolves.

**`ALTER TABLE` is narrow.** You can `ADD` nullable columns of supported types and
`DROP COLUMN`; `ALTER COLUMN` is in preview. Adding a `NOT NULL` column to a populated
table is not an option, so plan for nullable-and-backfill. Rename a column with
`sp_rename`, not `ALTER TABLE`.

## Type and width traps

- `nvarchar`, `datetime`, `datetimeoffset`, `money`, `xml`, `tinyint` do not exist —
  substitute per the **connecting-to-fabric** skill's type table.
- `datetimeoffset` being unavailable means **timezone offsets are lost**. Normalise to
  UTC at ingestion; do not store local times and hope.
- Row width limit is 8,060 bytes. A wide table of `varchar(8000)` columns fails on
  insert with error 511 or 611. Size columns to the data.
- Default collation `Latin1_General_100_BIN2_UTF8` is **case- and accent-sensitive**.
  `'active' <> 'Active'`. Normalise case during load rather than in every query.

## Running statements from scripts

Use `sqlcmd` ([go-sqlcmd](https://github.com/microsoft/go-sqlcmd)) — no ODBC driver,
no admin rights, Linux and Windows binaries:

```bash
export SQLCMDPASSWORD="$AZURE_CLIENT_SECRET"
sqlcmd -S "$SERVER" -d "$WAREHOUSE" \
  --authentication-method=ActiveDirectoryServicePrincipal \
  -U "$CLIENT_ID@$TENANT_ID" \
  -b -i load_person.sql
```

`-b` makes sqlcmd exit non-zero on SQL error — **without it a failed load reports
success** and the orchestrator moves on. Use `-i file.sql` for multi-statement scripts
rather than embedding SQL in shell quoting.

Verify after loading, in the same run, and fail the job on mismatch:

```sql
SELECT COUNT_BIG(*) AS rows_loaded, MAX(EtlDate) AS batch_stamp
FROM   dbo.Customer WHERE EtlDate = @RunTime;
```
