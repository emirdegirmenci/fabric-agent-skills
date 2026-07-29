---
name: modeling-fabric-warehouses
description: Use when designing or extending a Microsoft Fabric data warehouse or lakehouse — deciding how to lay out schemas, name tables and columns, choose data types and keys, separate landing from curated layers, or audit an existing warehouse's conventions before adding to it. Covers schema-per-domain layering, load-pattern-in-the-name, audit column standards, and the varchar/datetime2 type policy Fabric forces.
---

# Modeling Fabric warehouses

## Hard rules

1. **Audit the existing layout before adding anything.** Run
   `scripts/inventory_schemas.py` and match what you find — consistency inside a
   warehouse beats correctness imported from outside.
2. **Every curated table belongs to exactly one domain schema.** If it seems to belong to
   two, the boundary is wrong or the table does two jobs.
3. **Never let `dbo` be the destination by default.** Assign a domain at creation;
   moving a table later is a breaking change for every consumer.
4. **Retire by moving to an archive schema**, never by renaming with a suffix.
5. **One case convention per schema, one view prefix per warehouse, ASCII identifiers
   only.** Under the case-sensitive default collation, mixed casing means no single
   search finds everything.
6. **Surrogate keys are `varchar(36)`, not `uniqueidentifier`** — the latter does not
   round-trip between Warehouse and Lakehouse.
7. **Timestamps are `datetime2(6)` holding UTC.** `datetimeoffset` does not exist in
   Fabric, so an offset cannot be stored — normalise at ingestion or lose it.
8. **Reuse one column name per concept warehouse-wide.** A single well-known join key
   does more for usability than any documentation.
9. **Constraints are documentation.** Fabric accepts them only as `NOT ENFORCED`;
   validate uniqueness in the load.
10. **No `Test*`, `uat_*`, `tmp_*`, `_Yedek` names in a production warehouse.** Use
    source control and Delta time travel.

Column widths are a Warehouse-only lever: a Lakehouse table read through the SQL
endpoint reports **every** string as `varchar(8000)` regardless of content — see the
**loading-fabric-tables** skill's measured type mapping.

---

Conventions matter more in Fabric than in a single-database world, because one
workspace mixes a Lakehouse and a Warehouse that expose the *same* entities to the
*same* consumers. Without a rule for what lives where, you get two competing versions
of every table and no way to tell which one to trust.

The conventions below are drawn from a production warehouse of ~630 tables and ~12,700
columns across 36 schemas. They are stated as defaults, not laws — but adopt them as a
set. Half-applied conventions are worse than none, because consumers can no longer
predict anything.

## Before you add anything: audit what exists

Never design against an assumed layout. Extract the real one first:

```bash
python scripts/inventory_schemas.py            # writes inventory/<db>/<schema>.md
```

One file per schema, one row per column, plus a machine-readable summary. Read the
schema you are about to extend and match it. If the existing convention differs from
this skill, **follow the existing one** and note the divergence — consistency inside a
warehouse beats correctness imported from outside.

## Layer 1: land flat, curate by domain

Two layers, and the split is by *provenance*, not by cleanliness:

| Layer | Where | Organisation | Purpose |
|---|---|---|---|
| **Landing** | Lakehouse, one flat schema (`dbo`) | mirrors the source, flat | ingest cheaply, keep source shape |
| **Curated** | Warehouse, many schemas | grouped by business domain | the model consumers query |

In practice the same entity exists in both — landing holds `ProductCategory` as
delivered, and the curated layer holds it reshaped and grouped under a domain. That
duplication is deliberate: landing absorbs source churn so the curated model does not
have to.

Land flat. Resist organising the landing zone — every schema you invent there is a
decision you must revisit when the source changes, and the landing zone's only job is
to receive data without argument.

## Layer 2: one schema per domain, not one schema for everything

The curated layer's schemas are the model's table of contents. Three legitimate kinds,
and mixing their naming is what makes a warehouse unnavigable:

| Kind | Named after | Example | Contains |
|---|---|---|---|
| **Domain** | a business concept | `Order`, `Billing`, `Customer` | curated entities |
| **Source system** | the system of record | `SAP`, `Crm` | data still shaped like that system |
| **Consumer** | who reads it | `GoldLayer_SalesReport` | report-serving, denormalised |

Plus technical schemas: `dbo` for the unclassified remainder, `lookup` for reference
codes, an archive schema for retired tables.

Rules that keep this working:

- A table belongs to **exactly one** domain schema. If it seems to belong to two, the
  domain boundary is wrong or the table does two jobs.
- Name a schema after a **source system** only while the data still has that system's
  shape. Once curated, it belongs in a domain schema.
- Keep `dbo` shrinking. A growing `dbo` means nobody is deciding — in the reference
  warehouse `dbo` is the largest schema by far, which is the honest cost of deferring
  that decision 150 times.
- Retire by **moving to an archive schema**, not by renaming with a suffix. A schema
  boundary can be permissioned and excluded from discovery; a suffix cannot.

Detailed layering, cross-database access, and when to split a schema:
[reference/schema-layering.md](reference/schema-layering.md).

## Layer 3: put the load pattern in the table name

A reader must be able to tell how a table is maintained without opening the pipeline:

| Suffix | Meaning | Rerun safe? |
|---|---|---|
| `<Entity>Truncate` | fully rebuilt every run | yes, always |
| `<Entity>` | incremental or upsert | only under its watermark |
| `<Entity>History` | append-only, every version kept | append |
| `<Entity>Current` | derived latest-row snapshot | yes, rebuilt from History |
| `<Entity>Translation` | localised text sidecar, keyed to the base table | follows base |
| `<Entity>MapTable` | source code → warehouse code mapping | reference |

This is the highest-value naming convention in the set, because it encodes an
*operational* fact. `InvoiceTruncate` tells an on-call engineer a rerun is
harmless; a bare `Invoice` warns them it is not.

Mechanics of each pattern live in the **loading-fabric-tables** skill.

## Layer 4: the audit block, identical everywhere

Business columns first, then this block in this order, on every loaded table:

```sql
InsertedUser  varchar(200),
InsertedDate  datetime2(6),
UpdatedUser   varchar(200),
UpdatedDate   datetime2(6),
IsDeleted     bit,
EtlDate       datetime2(6)
```

Uniformity is the whole value. When the block is identical, freshness monitoring,
reconciliation, and "did last night's load land?" are written once and work against
every table. In the reference warehouse **200 of 392 base tables** carry this block —
and the tables that skip it are precisely the ones nobody can monitor.

Add `IsCurrent bit` and `EffectiveDate datetime2(6)` when a table keeps versions.

Rationale per column and the soft-delete rule: **loading-fabric-tables**.

## Layer 5: types and keys — Fabric constrains you

Fabric's type system is narrower than SQL Server's, and the substitutions have real
consequences:

- **`varchar`, never `nvarchar`.** `nvarchar` does not exist; UTF-8 collation covers
  Unicode. Size to the data — `varchar(8000)` everywhere hits the 8,060-byte row limit.
- **`datetime2(6)`, never `datetime`.** And `datetimeoffset` does not exist, so
  **timezone offsets cannot be stored** — normalise to UTC at ingestion.
- **`decimal(p,s)` for money**, never `money`.
- **Surrogate keys as `varchar(36)`, not `uniqueidentifier`.** This looks wrong and is
  right: `uniqueidentifier` does not round-trip reliably between Warehouse and
  Lakehouse (stored as binary in Delta), so cross-database joins on it break. Storing
  the GUID as text costs bytes and buys joins that work everywhere.
- **Give every entity a stable natural key** and reuse one name for it across the
  warehouse (`CustomerNo` appears on 241 columns in the reference warehouse). A single
  well-known join key does more for usability than any amount of documentation.
- **Constraints are documentation.** Fabric accepts `PRIMARY KEY`/`FOREIGN KEY`/`UNIQUE`
  only as `NOT ENFORCED`. Declare them for the optimiser and for readers, and validate
  uniqueness in the load.

Full type table, key strategy, and the `*ShortCode` pattern for business codes:
[reference/types-and-keys.md](reference/types-and-keys.md).

## Naming, briefly

- **One case convention per schema.** PascalCase for warehouse-owned tables and
  columns; snake_case is fine for tables an application owns, but do not mix inside one
  schema.
- **The default collation is case- and accent-sensitive**
  (`Latin1_General_100_BIN2_UTF8`). `Status = 'active'` does not match `'Active'`, and
  non-English text sorts by byte value, not alphabetically. Normalise case at load;
  store a sort key if users need alphabetical order.
- **One view prefix, chosen once.** The reference warehouse has `vw_`, `v_` and `VW_`
  in use simultaneously, which with case-sensitive collation makes views genuinely hard
  to find.
- **ASCII identifiers only.** Non-ASCII characters in object names survive Fabric but
  break tooling, scripts, and anything that round-trips through a non-UTF-8 console.

Full conventions with examples: [reference/naming.md](reference/naming.md).

## Anti-patterns, observed in production

These are not hypothetical — all of them exist in the reference warehouse, and each one
costs someone time:

| Anti-pattern | Why it hurts | Instead |
|---|---|---|
| `_Yedek`, `_YEDEK2`, `_bkp` copies | indistinguishable from live tables; never cleaned up | archive schema, or source control |
| `Test*`, `uat_*`, `tmp_*` in production | consumers cannot tell what is real | separate workspace |
| Mixed view prefixes | undiscoverable under case-sensitive collation | one prefix |
| `dbo` as the default destination | 150 tables with no domain; discovery becomes grep | assign a domain on creation |
| Non-ASCII object names | breaks tooling and scripts | ASCII identifiers |
| Suffix-based retirement | retired tables stay in the consumer's namespace | archive schema |

A backup table created "just for today" is indistinguishable from a real one six months
later. Use source control and time travel, which Delta gives you for free.

## Design checklist

```
- [ ] Existing conventions audited (scripts/inventory_schemas.py) and matched
- [ ] Landing stays flat; curated table assigned to exactly one domain schema
- [ ] Load pattern reflected in the table name
- [ ] Audit block present, in the standard order
- [ ] Types from Fabric's supported set; UTC normalised; widths sized to data
- [ ] Surrogate keys varchar(36), natural key named consistently
- [ ] Constraints declared NOT ENFORCED; uniqueness validated in the load
- [ ] One case convention; one view prefix; ASCII identifiers
- [ ] No Test/uat/tmp/backup names
```

## Reference

- [reference/schema-layering.md](reference/schema-layering.md) — landing vs curated, schema kinds, cross-database access
- [reference/naming.md](reference/naming.md) — tables, columns, views, collation consequences
- [reference/types-and-keys.md](reference/types-and-keys.md) — type policy, keys, `*ShortCode`, constraints
- `scripts/inventory_schemas.py` — run it; extracts the real schema layout to markdown

### Official documentation

- [Data types](https://learn.microsoft.com/en-us/fabric/data-warehouse/data-types) ·
  [T-SQL surface area](https://learn.microsoft.com/en-us/fabric/data-warehouse/tsql-surface-area)
- [Collation](https://learn.microsoft.com/en-us/fabric/data-warehouse/collation) ·
  [Statistics](https://learn.microsoft.com/en-us/fabric/data-warehouse/statistics)
- [Lakehouse schemas](https://learn.microsoft.com/en-us/fabric/data-engineering/lakehouse-schemas) ·
  [Medallion architecture in OneLake](https://learn.microsoft.com/en-us/fabric/onelake/onelake-medallion-lakehouse-architecture)
- [OneLake shortcuts](https://learn.microsoft.com/en-us/fabric/onelake/onelake-shortcuts)
