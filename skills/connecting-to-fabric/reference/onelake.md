# OneLake data plane

## Contents

- abfss URI anatomy
- Listing paths over DFS REST
- Reading Delta with delta-rs
- Cheap metadata (row counts without reading data)
- Files/ vs Tables/
- Shortcuts

Audience: `https://storage.azure.com/.default`. Host:
`onelake.dfs.fabric.microsoft.com` (sovereign clouds differ — never hardcode; make it
configurable).

Official: [OneLake access API](https://learn.microsoft.com/en-us/fabric/onelake/onelake-access-api).

## abfss URI anatomy

```
abfss://<workspace>@onelake.dfs.fabric.microsoft.com/<item>/Tables/<schema>/<table>
        └─ GUID or name ─┘                            └ GUID or name ┘
```

Two addressing forms, never mixed:

| Form | Workspace | Item | Notes |
|---|---|---|---|
| GUID | `11111111-2222-...` | `aaaaaaaa-bbbb-...` | **no `.Lakehouse` suffix**; stable across renames |
| Friendly | `My Workspace` | `My Lakehouse.Lakehouse` | suffix **required**; breaks on rename |

Prefer GUIDs in code. Names are for humans and change without warning; a renamed
lakehouse silently 404s every hardcoded path.

The `<schema>` segment exists only on a
[schema-enabled lakehouse](https://learn.microsoft.com/en-us/fabric/data-engineering/lakehouse-schemas).
On a classic lakehouse the path is `Tables/<table>`. Schema-enablement is chosen at
creation and cannot be toggled later, so the path shape is a property of the item —
detect it by listing `Tables/` and seeing whether the children are tables or schemas.

## Listing paths over DFS REST

The workspace is the filesystem; the item GUID is the first directory.

```python
r = httpx.get(
    f"https://onelake.dfs.fabric.microsoft.com/{WORKSPACE}",
    params={"resource": "filesystem", "recursive": "false",
            "directory": f"{LAKEHOUSE}/Tables/{SCHEMA}"},
    headers={"Authorization": f"Bearer {storage_token}",
             "x-ms-version": "2023-11-03"},
    timeout=60)
names = [p["name"].rsplit("/", 1)[-1] for p in r.json().get("paths", [])]
```

`x-ms-version` is required; without it the service may reject or behave
inconsistently. `recursive=false` keeps responses small — recurse deliberately, one
level at a time, or a large lakehouse returns tens of thousands of file entries.

`name` in the response is the **full path**, not the leaf; split it.

Useful probe order — the first level that fails localises the problem:
`{item}/Tables` → `{item}/Tables/{schema}` → `{item}/Files`.

## Reading Delta with delta-rs

```python
from deltalake import DeltaTable

dt = DeltaTable(uri, storage_options={"bearer_token": storage_token,
                                      "use_fabric_endpoint": "true"})
tbl = dt.to_pyarrow_table()
```

Both keys are mandatory; omitting `use_fabric_endpoint` makes
[delta-rs](https://delta-io.github.io/delta-rs/) treat OneLake as generic ADLS Gen2
and authentication fails with an opaque error.

Requires `deltalake` and `pyarrow`. The bearer token expires in ~1 hour — construct
`DeltaTable` per unit of work in long jobs rather than holding one open.

Useful properties:

```python
dt.version()                      # Delta version
dt.schema().fields                # name / type / nullable
dt.metadata().partition_columns
dt.history(5)                     # recent commits
DeltaTable(uri, version=N, ...)   # time travel
```

## Cheap metadata

`to_pyarrow_table()` downloads everything. To profile many tables, read the
transaction log instead.

In `deltalake` 1.x, `get_add_actions()` returns an **arro3** table, not a PyArrow one.
Calling `.to_pylist()` on it raises `AttributeError: 'arro3.core._core.Table' object has
no attribute 'to_pylist'` — convert first:

```python
import pyarrow as pa

adds  = pa.table(dt.get_add_actions(flatten=True))   # arro3 -> pyarrow
files = adds.num_rows
rows  = sum(x or 0 for x in adds.column("num_records").to_pylist())
size  = sum(x or 0 for x in adds.column("size_bytes").to_pylist())
```

Watch for this inside a `try/except`: a broad handler swallows the `AttributeError` and
the profile silently reports no row count instead of failing. If an inventory shows `?`
for every row count, this is why.

`len(dt.file_uris())` is a simpler file count when that is all you need.

This is orders of magnitude cheaper than reading data and is the right way to inventory
a lakehouse. `num_records` comes from optional per-file statistics — treat it as absent,
not zero, when writers omit stats.

`files` is itself a health signal: many small files per table means the table needs
`OPTIMIZE` (see the **loading-fabric-tables** skill) and that the SQL endpoint will
read it slowly.

## Files/ vs Tables/

- `Tables/` — Delta tables. The only thing the SQL analytics endpoint exposes.
- `Files/` — arbitrary files (CSV, JSON, XML, Parquet, checkpoints, exports).

**A Delta table outside `Tables/` is invisible to SQL.** If a table exists in OneLake
but not in `INFORMATION_SCHEMA`, check its location before suspecting permissions.

Read `Files/` content with a plain authenticated GET on the DFS path; the SQL
endpoint has no `OPENROWSET` over these.

## Shortcuts

[Shortcuts](https://learn.microsoft.com/en-us/fabric/onelake/onelake-shortcuts)
mount external storage (ADLS, S3, another lakehouse) under `Tables/` or `Files/`
without copying. They read like native paths, so most code needs no change.

Two consequences worth knowing before debugging: permissions are evaluated at the
*target*, so a shortcut can 403 while its siblings work; and latency follows the
target, so a shortcut to another region is slow for physical reasons, not
misconfiguration.
