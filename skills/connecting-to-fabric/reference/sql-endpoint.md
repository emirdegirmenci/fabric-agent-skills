# SQL analytics endpoint and Warehouse (TDS)

## Contents

- Which database, which server
- sqlcmd (no ODBC)
- Parsing sqlcmd output in scripts
- pyodbc
- JDBC
- Read-only reality of the SQL analytics endpoint
- T-SQL surface area
- Data types
- Collation — the case-sensitivity trap
- Cross-database queries
- Metadata discovery queries
- Metadata staleness after ETL

## Which database, which server

One workspace exposes **one server FQDN**; every Lakehouse endpoint and Warehouse in
it is a database on that server. Discover the FQDN from the control plane
([rest-api.md](rest-api.md)) — it is a generated string, not derivable from names.

Database name = the item's **display name**, not its GUID.

## sqlcmd (no ODBC)

[go-sqlcmd](https://github.com/microsoft/go-sqlcmd) is a single static binary built on
`go-mssqldb`, so it needs no `msodbcsql18` install and no admin rights. Releases ship
for Windows, Linux (amd64/arm64/s390x) and macOS. This is the portable default.

```bash
export SQLCMDPASSWORD="$AZURE_CLIENT_SECRET"
sqlcmd -S "$SERVER" -d "$DATABASE" \
  --authentication-method=ActiveDirectoryServicePrincipal \
  -U "$CLIENT_ID@$TENANT_ID" \
  -Q "SELECT TOP 5 * FROM [schema].[table]"
```

`SQLCMDPASSWORD` keeps the secret out of `argv`. Username is `clientId@tenantId`.

Other auth methods: `ActiveDirectoryDefault` (picks up `az login` / managed identity),
`ActiveDirectoryInteractive`, `ActiveDirectoryManagedIdentity`.

## Parsing sqlcmd output in scripts

Default output is fixed-width and pads every column to its declared width, which for
`varchar(8000)` is unusable. Use these flags together:

```bash
sqlcmd ... -s$'\x1f' -W -h -1 -Y 0 -y 0 -Q "SET NOCOUNT ON; SELECT ..."
```

| Flag | Effect |
|---|---|
| `-s<sep>` | column separator; use `\x1f` (unit separator) — cannot occur in SQL identifiers |
| `-W` | trim trailing whitespace |
| `-h -1` | suppress the header/underline block |
| `-Y 0` / `-y 0` | no truncation of fixed/variable-length columns |
| `SET NOCOUNT ON` | suppress "(N rows affected)" |

Pipe-`|` is a poor separator: it appears in real data. Then drop blank lines and
lines consisting only of separators and dashes.

For anything beyond simple extraction, prefer a real driver — parsing text output is
brittle by nature.

## pyodbc

Needs the system driver **ODBC Driver 18 for SQL Server** (`msodbcsql18`), which
requires admin on Windows and a Microsoft package repo on Linux. Use it when you want
a DB-API interface or pandas integration; otherwise prefer `sqlcmd`.

```python
conn_str = (
    f"Driver={{ODBC Driver 18 for SQL Server}};Server={server},1433;Database={db};"
    f"Authentication=ActiveDirectoryServicePrincipal;"
    f"UID={client_id};PWD={secret};"
    f"Encrypt=yes;TrustServerCertificate=no;Connection Timeout=60;"
)
```

Check availability before assuming: `pyodbc.drivers()`. Driver 17 also works but
lacks the newer Entra modes — prefer 18.

## JDBC

`com.microsoft.sqlserver:mssql-jdbc`, with
`authentication=ActiveDirectoryServicePrincipal`, `user=<clientId>`,
`password=<secret>`, `encrypt=true`.

## Read-only reality of the SQL analytics endpoint

A **Lakehouse SQL analytics endpoint rejects all DDL and DML**, regardless of role:

```
Msg 368 ... The external policy action
'Microsoft.Sql/Sqlservers/Databases/Schemas/Tables/Create' was denied
```

This is architectural, not a permission gap. The endpoint is a read-only T-SQL
projection over Delta files. To write, use Spark against the lakehouse, or use a
**Warehouse**, which is a real read-write engine over the same TDS protocol.

Useful as a guardrail: pointing an integration at a Lakehouse endpoint makes
accidental writes impossible.

## T-SQL surface area

Supported: CTEs, window functions, `CASE`, correlated subqueries, `UNION`/`INTERSECT`/
`EXCEPT`, `EXISTS`, `TOP`, `OFFSET-FETCH`, `CROSS/OUTER APPLY`, `PIVOT`/`UNPIVOT`,
`FOR JSON` (last operator only).

Not supported — these fail rather than degrade:

| Feature | Note |
|---|---|
| `FOR XML` | use `FOR JSON` |
| Recursive CTEs | flatten or iterate in the client |
| `SET TRANSACTION ISOLATION LEVEL` | snapshot isolation is implicit |
| `SET ROWCOUNT` | use `TOP` |
| Triggers, materialized views | not available |
| `OPENROWSET` on a Lakehouse endpoint | Warehouse only |
| `MERGE` | not available in the SQL analytics endpoint |
| `sp_showspaceused` | use the Capacity Metrics app |

Current list:
[T-SQL surface area](https://learn.microsoft.com/en-us/fabric/data-warehouse/tsql-surface-area).

## Data types

Supported: `bigint`, `int`, `smallint`, `bit`, `decimal`/`numeric`, `float`, `real`,
`date`, `time(n)`, `datetime2(n)` (≤6 fractional digits), `char`/`varchar`
(incl. `max`), `varbinary`, `uniqueidentifier`.

Not supported, with the substitution to use:

| Unsupported | Use | Cost |
|---|---|---|
| `nvarchar` / `nchar` | `varchar` | UTF-8 collation covers Unicode; multi-byte text uses more bytes |
| `datetime` / `smalldatetime` | `datetime2(6)` | — |
| `datetimeoffset` | `datetime2(6)` | **offset is lost** — normalise to UTC before load |
| `money` | `decimal(19,4)` | — |
| `xml` | `varchar(max)` | XML functions lost |
| `text` / `ntext` / `image` | `varchar(max)` / `varbinary(max)` | — |
| `tinyint` | `smallint` | — |
| `geometry` / `geography` | `varbinary` (WKB) or `varchar` (WKT) | cast at use |
| `sql_variant`, `hierarchyid` | no equivalent | redesign |

Reference: [Data types](https://learn.microsoft.com/en-us/fabric/data-warehouse/data-types).

Row width limit is 8,060 bytes; wide tables of `varchar(8000)` columns hit errors 511
and 611 on insert. Size columns deliberately.

## Collation — the case-sensitivity trap

Default is `Latin1_General_100_BIN2_UTF8`: **case-sensitive and accent-sensitive**,
compared byte-wise.

```sql
WHERE Status = 'active'   -- does NOT match 'Active'
```

This surprises everyone arriving from a case-insensitive SQL Server. Consequences:

- Normalise case on load, or compare with an explicit `COLLATE`, or `UPPER()` both
  sides — knowing that wrapping a column in a function prevents statistics from being
  used well.
- For non-English text, binary ordering is not alphabetical ordering. Turkish
  `ı/İ/i/I`, German `ä`, Scandinavian `å/ø` sort by byte value, so `ORDER BY` looks
  wrong to users. Sort in the presentation layer, or store a normalised sort key
  column.

Verify per database — it can differ:

```sql
SELECT DATABASEPROPERTYEX(DB_NAME(), 'Collation');
```

Details: [Collation](https://learn.microsoft.com/en-us/fabric/data-warehouse/collation).

## Cross-database queries

Three-part naming works across Lakehouse endpoints and Warehouses **in the same
workspace**:

```sql
SELECT a.login_name, s.total
FROM   OtherLakehouse.app_schema.account AS a
JOIN   ThisWarehouse.dbo.Summary        AS s ON s.account_id = a.id;
```

No linked servers, no configuration. Cross-*workspace* is not supported — use a
shortcut to bring the data into range instead.

Caveat: `uniqueidentifier` does not round-trip reliably between Warehouse and
Lakehouse (stored as binary in Delta). Avoid joining on GUID columns across
databases; join on a text or integer key.

## Metadata discovery queries

```sql
-- schemas with object counts
SELECT TABLE_SCHEMA, COUNT(*) AS objects
FROM INFORMATION_SCHEMA.TABLES
GROUP BY TABLE_SCHEMA ORDER BY objects DESC;

-- full column inventory
SELECT c.TABLE_SCHEMA, c.TABLE_NAME, t.TABLE_TYPE, c.ORDINAL_POSITION,
       c.COLUMN_NAME, c.DATA_TYPE, c.CHARACTER_MAXIMUM_LENGTH,
       c.NUMERIC_PRECISION, c.NUMERIC_SCALE, c.IS_NULLABLE
FROM INFORMATION_SCHEMA.COLUMNS c
JOIN INFORMATION_SCHEMA.TABLES  t
  ON t.TABLE_SCHEMA = c.TABLE_SCHEMA AND t.TABLE_NAME = c.TABLE_NAME
ORDER BY 1, 2, 4;
```

Exclude the system schemas `sys`, `INFORMATION_SCHEMA` and `queryinsights` from
inventories — `queryinsights` is Fabric's own telemetry, not user data.

## Metadata staleness after ETL

The endpoint caches Delta metadata, so a table written seconds ago can be missing or
short of rows. Do not paper over this with sleeps: call the Refresh SQL Endpoint
Metadata REST API after the load, then query. Background sync also runs on its own
schedule, which is why the problem looks intermittent.

**Measured lag**: a table written to OneLake was absent from `INFORMATION_SCHEMA` at 5s,
22s and 54s, and present at 116s. A `sleep(30)` therefore *passes* while the table is
still invisible.

If a column rather than a table is missing, suspect the Delta type before suspecting
the cache — a timezone-naive `timestamp_ntz` column never appears at all. See the
**loading-fabric-tables** skill's measured type mapping.

Performance guidance:
[SQL analytics endpoint performance](https://learn.microsoft.com/en-us/fabric/data-warehouse/sql-analytics-endpoint-performance).
The usual root cause of slowness is many small Delta files — fix it at the writer with
`OPTIMIZE` (see the **loading-fabric-tables** skill), not with query hints.
