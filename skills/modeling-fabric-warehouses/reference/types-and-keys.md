# Types and keys

## Contents

- Supported types
- Unsupported types and their substitutes
- Why surrogate keys are varchar(36)
- The two-key pattern: surrogate + ShortCode
- Natural key naming
- Sizing varchar and the row limit
- Timestamps and timezones
- Booleans and status
- Constraints are documentation
- Type policy in one table

## Supported types

| Category | Types |
|---|---|
| Exact numeric | `bigint`, `int`, `smallint`, `bit`, `decimal(p,s)` / `numeric(p,s)` |
| Approximate | `float`, `real` |
| Date / time | `date`, `time(n)`, `datetime2(n)` — at most 6 fractional digits |
| Character | `char(n)`, `varchar(n)`, `varchar(max)` |
| Binary | `varbinary(n)`, `varbinary(max)` |
| Other | `uniqueidentifier` |

Reference: [Data types](https://learn.microsoft.com/en-us/fabric/data-warehouse/data-types).

## Unsupported types and their substitutes

| Unsupported | Substitute | Consequence |
|---|---|---|
| `nvarchar`, `nchar` | `varchar`, `char` | UTF-8 collation covers Unicode; multi-byte text costs more bytes |
| `datetime`, `smalldatetime` | `datetime2(6)` | none |
| `datetimeoffset` | `datetime2(6)` | **offset is lost** — normalise to UTC first |
| `money`, `smallmoney` | `decimal(19,4)` | none; the currency was never stored anyway |
| `xml` | `varchar(max)` | XML functions unavailable; parse in the pipeline |
| `text`, `ntext` | `varchar(max)` | none |
| `image` | `varbinary(max)` | none |
| `tinyint` | `smallint` | one extra byte |
| `geometry`, `geography` | `varbinary` (WKB) or `varchar` (WKT) | no spatial functions |
| `sql_variant`, `hierarchyid` | none | redesign the column |
| `vector` | none | not supported |

The `datetimeoffset` gap is the one that causes data loss rather than inconvenience. If
a source sends local times with offsets, convert to UTC at ingestion and keep the
original offset in its own column if it has business meaning. Discovering this after a
year of loads is expensive.

`nvarchar` is worth stating twice because it is the most frequent porting error:
scripts brought from SQL Server fail with "type not found", and the fix is mechanical —
replace with `varchar` and confirm the collation is UTF-8.

## Why surrogate keys are varchar(36)

Store GUID surrogate keys as `varchar(36)`, not `uniqueidentifier`.

This looks like a mistake and is a deliberate trade. `uniqueidentifier` **does not
round-trip reliably between Warehouse and Lakehouse** — Delta stores it as binary, and
cross-database joins on such a column produce wrong or empty results. Since three-part
naming across Lakehouse and Warehouse is a normal thing to do in Fabric, a key type
that breaks those joins is not usable as a key type.

Costs, stated honestly: 36 bytes instead of 16, string comparison instead of binary, and
no type-level guarantee the value is a GUID. Buys: joins that work across every plane
Fabric has. Take the trade.

If you never join across databases and never will, `uniqueidentifier` is fine — but
that is a bet on the future shape of the platform.

## The two-key pattern: surrogate + ShortCode

Give entities two identifiers with different jobs:

```sql
ProductCategoryId        varchar(36),    -- surrogate: stable, opaque, for joins
ShortCode               varchar(20),    -- business: human-readable, appears in reports
```

- **Surrogate** — never reused, never reinterpreted, meaningless to users. Survives
  source-system renumbering.
- **ShortCode** — what users recognise and type. Survives system migrations because it
  belongs to the business, not the system.

Reports filter and display `ShortCode`; joins use the surrogate. The pattern generalises
as `<Entity>ShortCode` on referencing tables (`RegionShortCode`,
`WarehouseShortCode`), which makes many report queries readable without a single
dimension join — a real usability win.

Keep `ShortCode` values stable. A "code" that changes is not a code.

## Natural key naming

Pick one name per concept and use it warehouse-wide. In the reference warehouse
`CustomerNo` appears on 241 columns and `RegionShortCode` on 123.

That repetition is the point. An analyst who learns `CustomerNo` once can join any two
tables that involve a customer. The alternative — `CustomerNo`, `PersonNumber`, `person_id`,
`EmpID` for the same concept in four schemas — forces a data dictionary lookup for
every query, and produces wrong joins when someone guesses.

Where a legacy source forces a different name, add a consistently named column
alongside it rather than propagating the source's vocabulary.

## Sizing varchar and the row limit

The row width limit is **8,060 bytes**. Exceeding it fails on insert with error 511 or
611.

`varchar(8000)` for everything is therefore not a safe default — a table with a handful
of such columns cannot accept a row. Size to the data:

| Content | Width |
|---|---|
| codes, short identifiers | `varchar(20)` |
| GUID as text | `varchar(36)` |
| names, usernames, actors | `varchar(200)` |
| descriptions | `varchar(500)` – `varchar(1000)` |
| free text, JSON, XML | `varchar(max)` |

Note that under UTF-8, `varchar(n)` is `n` *bytes*, not characters. Non-Latin text
consumes 2–4 bytes per character, so a `varchar(20)` column holds fewer than 20
characters of Turkish, Greek or Arabic text. Size accordingly, or truncation shows up as
a data-quality bug months later.

`varchar(max)` is not free — it is stored and scanned differently. Use it for genuinely
unbounded text, not as a way to avoid deciding.

## Timestamps and timezones

- Always `datetime2(6)`. `datetime` does not exist; more than 6 fractional digits is
  rejected.
- **Store UTC.** With no `datetimeoffset`, a local time is indistinguishable from a UTC
  time in the column, and mixing them loses or duplicates rows twice a year.
- Use `date` when there is no time component. It is smaller and it stops spurious
  time-of-day values appearing in reports.
- Convert to local time in the presentation layer, where the user's timezone is known.

Column naming: `*Date` for both dates and timestamps is acceptable if consistent;
`*DateTime` or `*Utc` is clearer. Pick one.

## Booleans and status

- `bit` for genuine two-state flags, named `Is<Something>`: `IsDeleted`, `IsCurrent`,
  `IsActive`.
- Never a `varchar` `'Y'`/`'N'`/`'true'`. Under case-sensitive collation `'Y'` and `'y'`
  are different values, which produces filters that silently miss rows.
- For multi-state status, an `int` or `varchar(20)` code plus a `lookup` table entry —
  not a growing set of `bit` columns. Three booleans encode eight states, of which
  usually only three are legal.

A caution on `bit` and nullability: a nullable `bit` has three states, and
`WHERE IsDeleted = 0` silently excludes the `NULL` rows. Either default it to `0` at
load, or write `WHERE ISNULL(IsDeleted, 0) = 0` — consistently.

## Constraints are documentation

Fabric accepts `PRIMARY KEY`, `UNIQUE` and `FOREIGN KEY` **only with `NOT ENFORCED`**:

```sql
ALTER TABLE Customer.Customer
    ADD CONSTRAINT PK_Person PRIMARY KEY NONCLUSTERED (CustomerNo) NOT ENFORCED;
```

Declare them anyway — they inform the optimiser and tell the next engineer what the
grain is. But nothing prevents a duplicate, so validate in the load and fail the job:

```sql
SELECT COUNT_BIG(*) AS duplicate_keys FROM (
    SELECT CustomerNo FROM Customer.Customer WHERE ISNULL(IsDeleted, 0) = 0
    GROUP BY CustomerNo HAVING COUNT_BIG(*) > 1
) d;
```

One caveat: an unenforced FK on a **SQL analytics endpoint** can block automatic schema
updates when new Delta columns appear. Drop FKs on tables whose schema evolves.

`ALTER TABLE` is narrow — `ADD` nullable columns and `DROP COLUMN` are supported,
`ALTER COLUMN` is in preview. Adding a `NOT NULL` column to a populated table is not
possible, so plan for nullable-then-backfill.

## Type policy in one table

| Purpose | Type |
|---|---|
| Surrogate key | `varchar(36)` |
| Business code | `varchar(20)` – `varchar(200)` |
| Customer / entity number | `varchar(20)` or `bigint`, one choice warehouse-wide |
| Counter, quantity | `int`, or `bigint` above 2 billion |
| Small enumeration | `smallint` or `int` + `lookup` |
| Money, rate | `decimal(19,4)` |
| Ratio, score | `decimal(p,s)`; `float` only when precision genuinely does not matter |
| Flag | `bit` |
| Timestamp | `datetime2(6)`, UTC |
| Date only | `date` |
| Name, actor | `varchar(200)` |
| Description | `varchar(500)` – `varchar(1000)` |
| JSON, XML, free text | `varchar(max)` |
| Binary payload | `varbinary(max)` |

Prefer `decimal` over `float` for anything anyone will sum or reconcile. Floating-point
totals that differ in the last digit between two reports generate more support work than
the storage saving is worth.
