# Delta → SQL endpoint type mapping (measured)

## Contents

- The mapping table
- RULE 1: never write a timezone-naive timestamp
- RULE 2: every string becomes varchar(8000)
- RULE 3: all-NULL columns do surface
- RULE 4: identifiers are case-sensitive over SQL
- RULE 5: expect ~2 minutes of metadata lag
- Verifying the mapping in your own environment

Everything below was measured by writing Delta tables and reading the resulting
`INFORMATION_SCHEMA` through a Lakehouse SQL analytics endpoint. Where a claim is
measured rather than documented, it is marked **measured**.

## The mapping table

| Delta type | SQL analytics endpoint | Note |
|---|---|---|
| `long` | `bigint` | |
| `integer` | `int` | |
| `string` | `varchar(8000)` | always 8000, regardless of content — see RULE 2 |
| `boolean` | `bit` | |
| `double` | `float` | |
| `decimal(p,s)` | `decimal(p,s)` | precision and scale preserved |
| `date` | `date` | |
| `timestamp` | `datetime2` | written as timezone-**aware** |
| `timestamp_ntz` | **column does not appear at all** | see RULE 1 — **measured** |
| `binary` | `varbinary(8000)` | |

## RULE 1: never write a timezone-naive timestamp

**A `timestamp_ntz` column is invisible to the SQL analytics endpoint.** The table
appears, the other columns appear, and that column is silently absent from
`INFORMATION_SCHEMA.COLUMNS` and from `SELECT *`. No error, no warning.

This is the worst failure mode in this document, because everything looks like it
worked. A pipeline can load a timestamp for months while every SQL consumer and every
Power BI report silently lacks the column.

PyArrow's default timestamp type is timezone-naive, so **the wrong thing is the
default**:

```python
# WRONG — becomes timestamp_ntz, column vanishes from SQL
pa.array(values, pa.timestamp("us"))

# RIGHT — becomes timestamp, appears as datetime2
pa.array(values, pa.timestamp("us", tz="UTC"))
```

Measured, same table, same write:

| Column | PyArrow type | Delta type | SQL endpoint |
|---|---|---|---|
| `ts_ntz` | `timestamp("us")` | `timestamp_ntz` | **missing** |
| `ts_utc` | `timestamp("us", tz="UTC")` | `timestamp` | `datetime2` |
| `ts_ms_utc` | `timestamp("ms", tz="UTC")` | `timestamp` | `datetime2` |

Millisecond and microsecond precision both work; only the timezone matters.

Do not "fix" this by storing timestamps as strings. That trades an invisible column for
`varchar(8000)` that cannot be filtered by range or used in a date hierarchy. Attach
UTC and keep the type.

**Verification step after any load that includes a timestamp:**

```sql
SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS
WHERE TABLE_SCHEMA = 'your_schema' AND TABLE_NAME = 'your_table';
```

Compare the count against the Delta schema. A mismatch means a naive timestamp.

## RULE 2: every string becomes varchar(8000)

**measured** — a 2-character column and a 5,000-character column both surface as
`varchar(8000)`. Delta's `string` carries no length, so the endpoint cannot infer one.

Consequences:

- **You cannot tighten column widths from the writer side.** Width discipline (see the
  modeling skill) applies when you author `CREATE TABLE` in a **Warehouse**, not to a
  Lakehouse table projected through the endpoint.
- Do not conclude your table breaks the 8,060-byte row limit because it has many
  `varchar(8000)` columns. The limit constrains **declared widths in Warehouse DDL**;
  the endpoint's projection of Delta strings is not subject to it — tables with 15+
  string columns work fine.
- If you need real width limits, enforced types, or narrower storage, the table belongs
  in a Warehouse, not a Lakehouse.
- Downstream tools that size UI columns or allocate buffers from declared width will
  assume 8,000 characters for every string. Where that matters, cast in a view:
  `CAST(code AS varchar(20)) AS code`.

## RULE 3: all-NULL columns do surface

**measured** — a `string` column containing only NULLs appears normally as
`varchar(8000)`.

This contradicts a common belief that all-NULL columns are dropped by the endpoint. If
a column is missing, look for RULE 1 (naive timestamp) or an unsupported Delta type
before suspecting NULLs.

## RULE 4: identifiers are case-sensitive over SQL

**measured**, with the default `Latin1_General_100_BIN2_UTF8` collation:

```sql
SELECT COUNT_BIG(*) FROM [zz_test].[t_types];   -- 3
SELECT COUNT_BIG(*) FROM [zz_test].[T_TYPES];   -- Msg 208: Invalid object name
```

So the case you use when creating a Delta table is the only case that works in SQL
forever after. Pick one convention per schema and never rely on a case-insensitive
match — including in `WHERE` clauses on data, where `'active' <> 'Active'`.

## RULE 5: expect ~2 minutes of metadata lag

**measured** — a newly written table was absent from `INFORMATION_SCHEMA` at 5s, 22s and
54s after the write, and present at 116s.

Therefore:

- **Never `sleep(30)` and assume the table is there.** It was not, in this measurement.
- Never sleep at all as the primary mechanism. Call the Refresh SQL Endpoint Metadata
  REST API, or use the *Refresh SQL Endpoint* pipeline activity, as the final step.
- When polling is unavoidable, poll for the *condition* (does the column exist? does the
  row count match?) with a generous ceiling — not for a fixed duration.

The same lag applies to **new columns** on an existing table, which is why a schema
change can appear to have half-landed.

## Verifying the mapping in your own environment

Mappings can change between Fabric releases, and a Warehouse behaves differently from a
Lakehouse endpoint. Re-measure rather than trusting this table:

1. Write a small Delta table with one column per type you care about, into a scratch
   schema you own.
2. Read `dt.schema().fields` for the Delta types.
3. Read `INFORMATION_SCHEMA.COLUMNS` for the SQL types.
4. Diff the two column *name sets* first — a missing name is the finding that matters
   most, and comparing types alone hides it.

Use a scratch schema named so nobody mistakes it for real data (`zz_*` sorts last and
reads as deliberate), and write **only** inside it.
