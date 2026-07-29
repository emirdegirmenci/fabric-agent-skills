# Schema layering

## Contents

- The two layers
- Why the same table exists twice
- Kinds of schema in the curated layer
- Keeping dbo from becoming the warehouse
- Retiring tables
- Cross-database access
- When to split a schema
- Lakehouse schema-enablement

## The two layers

| | Landing | Curated |
|---|---|---|
| Item | Lakehouse | Warehouse |
| Schemas | one, flat (`dbo`) | many, by domain |
| Shape | as delivered by the source | modelled for consumers |
| Writes | Spark / pipeline | T-SQL |
| Audience | data engineers | analysts, reports, applications |
| Churn | absorbs source changes | changes only on a modelling decision |

The Lakehouse is chosen for landing because ingestion is cheap there and Spark handles
messy input. The Warehouse is chosen for curation because T-SQL, `MERGE`, and
transactions make modelling work tractable.

## Why the same table exists twice

Expect heavy overlap between landing and curated names — in the reference warehouse,
**261 of 371** landing tables also exist in the curated layer. That is not redundancy
to eliminate; it is the layer boundary doing its job:

- A source column rename breaks one landing table, not every downstream report.
- Reprocessing does not require re-fetching from the source system.
- The curated shape can differ (renames, splits, type normalisation) without
  negotiating with the source owner.

Keep the *names* aligned across the boundary even when the shape differs. An analyst
tracing `ProductCategory` back to its landing table should not have to guess.

What must **not** happen is consumers querying the landing layer. If they do, the
boundary has failed and every source change becomes a production incident. Enforce it
with permissions, not documentation.

## Kinds of schema in the curated layer

**Domain schemas** — the default and the bulk. Named for a business concept in the
singular: `Order`, `Billing`, `Customer`, `Inventory`, `Exam`. An analyst should
be able to guess the schema from the question they are asking.

**Source-system schemas** — `SAP`, `Crm`, and similar. Legitimate only while the data
still carries that system's shape and vocabulary. They are a staging area with a nicer
address, and they should shrink over time as entities are properly curated. A
source-system schema that has existed unchanged for years is really an unmodelled
domain.

**Consumer schemas** — `GoldLayer_<Report>`. Denormalised, purpose-built, owned by the
report. Their virtue is that they are *allowed* to be redundant and disposable: rebuild
or delete them without touching the domain model. Do not let other consumers depend on
one, or it quietly becomes a domain schema with a misleading name.

**Technical schemas** — `lookup` for reference codes and enumerations, an archive schema
for retired tables, `dbo` for the not-yet-classified.

Excluded from all of this: `sys`, `INFORMATION_SCHEMA`, and `queryinsights`, which
Fabric owns. Filter them out of inventories and documentation — `queryinsights` is
telemetry, not data.

## Keeping dbo from becoming the warehouse

`dbo` is where tables land when nobody decides where they belong. In the reference
warehouse it holds 150 tables, more than any real domain — the accumulated cost of
deferring that decision 150 times.

Two habits prevent it:

- **Assign a domain at creation.** A five-second decision then; an archaeology project
  later, once reports depend on the `dbo` name.
- **Track it as a metric.** Count `dbo` tables per quarter. Flat or falling is healthy;
  rising means the convention exists only on paper.

Moving a table out of `dbo` later is a breaking change for every consumer, which is
exactly why it does not happen. Decide up front.

## Retiring tables

Move retired tables to a dedicated **archive schema**. Do not rename them with a
suffix.

The schema boundary earns its keep three ways: it can be permissioned separately, it is
trivially excluded from discovery and documentation, and a table's status is visible
from its address rather than from a naming convention the reader has to know.

Suffix-based retirement (`_Yedek`, `_old`, `_bkp`) leaves retired tables sitting in the
consumer's namespace, sorted next to the live ones, indistinguishable at a glance. The
reference warehouse has 143 objects with backup, temp or test names — none of which
anyone will ever confidently delete, because nobody can prove they are unused.

For point-in-time recovery use Delta time travel and source control, not a copied
table.

## Cross-database access

Three-part naming works between any Lakehouse endpoint and Warehouse **in the same
workspace** — same server, no linked servers, no configuration:

```sql
SELECT a.login_name, s.Total
FROM   LandingLakehouse.app_schema.account AS a
JOIN   CuratedWarehouse.Customer.Summary   AS s ON s.account_id = a.id;
```

Two constraints shape design around this:

- **Cross-workspace queries are not supported.** Use a OneLake shortcut to bring remote
  data into range instead of splitting a model across workspaces.
- **`uniqueidentifier` does not round-trip** between Warehouse and Lakehouse (Delta
  stores it as binary). Join on `varchar(36)` or an integer key. This single constraint
  is why surrogate keys are stored as text — see
  [types-and-keys.md](types-and-keys.md).

Being able to join across the boundary is convenient for engineers and a trap for
consumers: it lets a report reach into landing. Grant on the curated database only.

## When to split a schema

Split when a schema stops answering a single question — the signals are a name needing
"and" to describe it, or two teams changing it for unrelated reasons.

Do not split by size. Sixty tables in one coherent domain is fine; six tables split
across three schemas because they felt crowded is worse than either.

Cheap intermediate step: a naming prefix inside the schema. If the prefix hardens and
stays stable, it has earned a schema of its own. Splitting is a breaking change for
consumers, so let the evidence accumulate first.

## Lakehouse schema-enablement

A [schema-enabled lakehouse](https://learn.microsoft.com/en-us/fabric/data-engineering/lakehouse-schemas)
supports `Tables/<schema>/<table>`; a classic one is flat, `Tables/<table>`.

**This is fixed at creation and cannot be changed.** Enable schemas for anything
expected to grow, even if the landing layer starts with one schema — retrofitting means
creating a new lakehouse and migrating.

It also changes every abfss path (an extra segment) and how tables appear over SQL, so
code written against one shape does not work against the other. Detect the shape by
listing `Tables/` rather than assuming.
