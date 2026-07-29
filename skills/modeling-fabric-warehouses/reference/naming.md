# Naming conventions

## Contents

- The collation consequence
- Schemas
- Tables
- Load-pattern suffixes
- Sidecar suffixes
- Columns
- Views
- Identifier characters
- Notebooks and jobs
- Auditing an existing convention

## The collation consequence

Fabric's default collation is `Latin1_General_100_BIN2_UTF8` — **case-sensitive,
accent-sensitive, byte-ordered**. Verify per database:

```sql
SELECT DATABASEPROPERTYEX(DB_NAME(), 'Collation');
```

This turns casing from a style preference into a correctness concern:

- `Status = 'active'` does not match `'Active'`. Normalise case at load rather than
  defending against it in every query.
- Two objects differing only in case are two different objects. `vw_Sales` and
  `VW_Sales` can coexist, and both will be missed by a search for the other.
- `ORDER BY` on text sorts by byte value, not alphabetically. Turkish `ı/İ/i/I`, German
  `ä`, Scandinavian `å/ø` land in positions users read as broken. Sort in the
  presentation layer, or store an explicit sort-key column.

Reference: [Collation](https://learn.microsoft.com/en-us/fabric/data-warehouse/collation).

## Schemas

- PascalCase, **singular**, business vocabulary: `Order`, `Customer`, `Billing`.
- Source-system schemas keep the system's own name: `SAP`, `Crm`.
- Consumer schemas name the consumer: `GoldLayer_SalesReport`.
- Technical schemas lowercase to mark them as infrastructure: `dbo`, `lookup`,
  `staging`, `archive`.

Lowercase-for-technical is a small thing that pays off constantly: a reader scanning a
schema list sees immediately which entries are model and which are plumbing.

Schema and table names cannot contain `/` or `\`.

## Tables

PascalCase, singular entity name: `ProductCategory`, `CustomerInvoice`.

Singular because a row is one thing. `Customer` reads correctly in `Customer.CustomerNo`,
where `Customers.CustomerNo` does not. Whichever you choose, do not mix — mixed number is
one of the most visible signs of an unmanaged warehouse.

Do not encode the schema in the table name: `Order.Order` is worse than
`Order.Registration`. The schema already qualifies it.

Tables owned by an application may use snake_case (`order_items`, `audit_events`) — an
app's own migrations legitimately follow that app's language conventions. Keep it
**consistent within a schema**: one case convention per schema, no exceptions.

## Load-pattern suffixes

The highest-value convention in this file, because it encodes an operational fact:

| Suffix | Contract |
|---|---|
| `<Entity>Truncate` | fully rebuilt each run; rerunning is always safe |
| `<Entity>` (no suffix) | incremental or upsert; rerun only under its watermark |
| `<Entity>History` | append-only, all versions retained |
| `<Entity>Current` | derived latest-row snapshot; rebuildable from History |

`InvoiceTruncate` tells an on-call engineer at 3 a.m. that a rerun is harmless. A
bare `Invoice` tells them to check the watermark first. No documentation lookup, no
pipeline reading.

## Sidecar suffixes

| Suffix | Meaning | Shape |
|---|---|---|
| `<Entity>Translation` | localised text | base key + language code + translated columns |
| `<Entity>MapTable` | source code → warehouse code | source value, target value, source system |
| `<Entity>Rule` | business rules for the entity | rule key + parameters |

The `Translation` sidecar keeps multilingual text out of the base table, so the base
stays one-row-per-entity and joins stay predictable. In the reference warehouse ~46
entities have one. The alternative — `NameTR`, `NameEN`, `NameDE` columns — requires
DDL for every new language and spreads `COALESCE` through every query.

`MapTable` makes source-to-warehouse code translation data rather than code, so a new
source value is an insert instead of a deployment.

## Columns

PascalCase. Names that read as the thing they hold, not their type.

Consistent patterns worth adopting wholesale:

| Pattern | Use | Type |
|---|---|---|
| `<Entity>Id` | surrogate key | `varchar(36)` |
| `<Entity>ShortCode` | business/natural code | `varchar(20..200)` |
| `Is<Something>` | boolean | `bit` |
| `<Something>Date` | date or timestamp | `datetime2(6)` or `date` |
| `<Something>User` | actor | `varchar(200)` |
| `EffectiveDate` | validity start | `datetime2(6)` |
| `IsCurrent` | current-version flag | `bit` |

**Use one name for one concept, warehouse-wide.** In the reference warehouse `CustomerNo`
appears on 241 columns and `RegionShortCode` on 123 — that repetition is the feature. An
analyst who learns the join key once can join anything. A warehouse where the same
concept is `CustomerNo`, `PersonNumber`, `person_id` and `EmpID` in four schemas needs a
data dictionary to do what a naming convention could have done for free.

The `*ShortCode` convention deserves attention: a stable, human-readable business code
alongside the opaque surrogate key. It survives system migrations, appears in reports
without a lookup, and is what users actually recognise.

Keep the audit block's order fixed:
`InsertedUser, InsertedDate, UpdatedUser, UpdatedDate, IsDeleted, EtlDate`.

## Views

**Pick one prefix and use only it.** `vw_` is a reasonable default.

The reference warehouse has `vw_`, `v_` and `VW_` in simultaneous use, and under
case-sensitive collation that means no single search finds all views. This is a small
inconsistency with an ongoing cost.

Name the view for what it returns, not how it is built:
`vw_ActiveCustomerOrder` over `vw_Order_Join_Customer_Filtered`.

Views are the right place to enforce the soft-delete filter. Expose
`WHERE IsDeleted = 0` in a view and grant consumers access to the view rather than the
table — a structural guarantee instead of a rule every report author must remember.

## Identifier characters

**ASCII letters, digits and underscore only.**

Fabric accepts non-ASCII object names, and the reference warehouse contains a few — they
break scripts, mangle in non-UTF-8 consoles, corrupt CSV round-trips, and require
quoting in every tool. Column *values* are Unicode text and that is fine; identifiers
should not be.

Also forbidden: `/` and `\` in schema and table names.

Use `sp_rename` to rename a column — `ALTER TABLE` cannot.

## Notebooks and jobs

Name the job for what it does and how often, so a failure in the Monitoring hub is
legible without opening it:

```
CreateDeltaTable_Truncate            full reload
CreateDeltaTable_Incremental         incremental, event-driven
CreateDeltaTable_Incremental_Daily   incremental, daily
CreateDelta_Hourly                   hourly
InitialInsert                        one-off seed — must never be scheduled
RefreshMetadataNotebook              SQL endpoint metadata refresh
```

`<Action>_<Pattern>_<Cadence>` is enough. The pattern and cadence in the name are what
let an operator judge severity immediately.

## Auditing an existing convention

Before adding tables, extract the real conventions — assumptions about a warehouse you
did not build are usually wrong:

```bash
python scripts/inventory_schemas.py
```

Then check: which case convention dominates per schema, which suffixes are in use, what
the most frequent column names are (those are the real join keys), and which type is
used for identifiers.

**Match what you find, even where this file disagrees.** Consistency within a warehouse
beats correctness imported from outside. Note divergences and fix them deliberately, as
their own piece of work — not as a side effect of adding a table.
