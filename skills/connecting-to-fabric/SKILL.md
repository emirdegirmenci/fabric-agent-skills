---
name: connecting-to-fabric
description: Use when connecting code to Microsoft Fabric — authenticating a service principal, reading or writing OneLake Delta tables, querying a Lakehouse SQL analytics endpoint or Warehouse over TDS, or calling the Fabric REST API. Covers abfss URIs, token audiences, ODBC-free SQL access, and diagnosing 401/403/404 and "table not visible" failures.
---

# Connecting to Microsoft Fabric

## Hard rules

1. **One token per plane.** A token minted for one audience returns
   `401 Audience validation failed` on another. This is not a credentials problem.
2. **Getting a token proves nothing about access.** Workspace permission is granted
   inside Fabric, separately. An empty `GET /v1/workspaces` is HTTP **200**, not an error.
3. **Never hardcode a SQL endpoint FQDN or an item GUID.** The FQDN is a generated
   string; discover both from the control plane at startup.
4. **Print `status_code`, `x-ms-error-code`, and a body excerpt on every failure.** The
   status code alone is ambiguous on all three planes.
5. **Never print the secret.** Print `len(secret)`. Pass it to child processes through
   the environment (`SQLCMDPASSWORD`), never in `argv`.
6. **Default to the SQL endpoint for reads.** It needs the least permission (Viewer) and
   no Spark. Reach for OneLake Delta only for metadata, time travel, or `Files/`.
7. **`403` is never fixed by editing a URI, and `404` is never fixed by changing a role.**
   Fabric returns 403 for denied access and 404 for a wrong path.
8. **Identifiers are case-sensitive** under the default collation — verified: `[t_types]`
   works, `[T_TYPES]` returns `Msg 208 Invalid object name`.

---

Fabric has **three independent planes**. Picking the wrong one is the most common
source of wasted debugging, because each needs a *different token audience* and
grants *different permissions*.

| Plane | What it reaches | Token audience | Tool |
|---|---|---|---|
| **Control plane** (REST) | Workspaces, items, connection strings, capacities | `https://api.fabric.microsoft.com/.default` | `httpx` / `curl` / `az rest` |
| **Data plane** (OneLake) | Delta files under `Tables/`, raw files under `Files/` | `https://storage.azure.com/.default` | `deltalake`, DFS REST |
| **SQL plane** (TDS :1433) | Warehouse + Lakehouse SQL analytics endpoint | *no bearer token* — driver authenticates | `sqlcmd`, `pyodbc`, JDBC |

**A token for one plane returns 401 on another.** If you get `401 Audience validation
failed`, you used the wrong scope — not the wrong credentials.

## Quick start

Run the bundled probe before writing any integration code. It exercises all three
planes and prints HTTP status, `x-ms-error-code`, and a body excerpt for each step,
continuing past failures so one broken plane doesn't hide the others.

```bash
pip install httpx deltalake pyarrow
python scripts/fabric_probe.py --env /path/to/.env
```

It answers, in order: is the credential valid → does the SP see the workspace →
can it list OneLake paths → can it read a Delta table → can it run SELECT.
Whichever step first fails tells you which permission is missing; see
[reference/troubleshooting.md](reference/troubleshooting.md).

## Configuration

Never hardcode secrets. Read them from a `.env` next to the script or from the
environment, and let the environment win so the same code runs in CI:

```python
for line in pathlib.Path(".env").read_text(encoding="utf-8-sig").splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip("\"'"))
```

`utf-8-sig` matters: a `.env` saved by a Windows editor carries a BOM that otherwise
becomes part of the first key name. `setdefault` (not assignment) is what makes the
real environment take precedence.

Required keys: `AZURE_TENANT`, `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET`,
`FABRIC_WORKSPACE`, `FABRIC_LAKEHOUSE`. Add `.env` to `.gitignore` before the first
commit, and never print the secret — print `len(secret)` when you need to confirm it loaded.

**Duplicate keys silently break `setdefault` parsing**: the *first* occurrence wins, so
an empty `FABRIC_SQL_SERVER=` earlier in the file overrides a filled one later. Keep
one definition per key.

## Authentication

Client credentials, one POST per audience:

```python
r = httpx.post(
    f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/token",
    data={"grant_type": "client_credentials", "client_id": CLIENT_ID,
          "client_secret": SECRET, "scope": scope},
    timeout=30)
token = r.json()["access_token"]
```

Decode the JWT payload and check `aud`, `tid`, `appid` before blaming permissions —
a token that returns 200 here can still be for the wrong tenant.

**Getting a token proves the credential is valid and nothing else.** Workspace access
is granted separately, in Fabric, and is the usual cause of an empty
`GET /v1/workspaces`. See [reference/auth.md](reference/auth.md) for the permission
matrix (which role unlocks which plane), certificate auth, managed identity, and the
tenant-level switch that must be on before any SP can call Fabric APIs.

## Reading data: pick a plane

**Default to the SQL endpoint.** It needs the least permission (Viewer), needs no
Spark session, and gives you T-SQL over both Lakehouse and Warehouse tables.

Reach for OneLake Delta only when you need something SQL cannot give you: Delta
metadata, time travel, partition layout, or reading files under `Files/`.

### SQL endpoint / Warehouse — no ODBC driver required

`sqlcmd` ([go-sqlcmd](https://github.com/microsoft/go-sqlcmd)) is a single static
binary with built-in Entra auth. It removes the `msodbcsql18` system dependency
that otherwise needs admin rights, which makes it the portable choice for CI,
containers, and locked-down laptops.

```bash
export SQLCMDPASSWORD="$AZURE_CLIENT_SECRET"      # keeps the secret out of argv
sqlcmd -S "$FABRIC_SQL_SERVER" -d "$FABRIC_SQL_DB" \
  --authentication-method=ActiveDirectoryServicePrincipal \
  -U "$AZURE_CLIENT_ID@$AZURE_TENANT" \
  -s"|" -W -h -1 -Q "SET NOCOUNT ON; SELECT TOP 5 * FROM [schema].[table]"
```

The server FQDN is not guessable — discover it from the control plane
([reference/rest-api.md](reference/rest-api.md)). A Lakehouse and a Warehouse in the
same workspace share one server; they differ only by `-d <database>`, and the database
name is the *item display name*, not its GUID.

For `pyodbc`, JDBC, connection pooling, parsing `sqlcmd` output reliably, and the
T-SQL surface that Fabric does **not** support, see
[reference/sql-endpoint.md](reference/sql-endpoint.md).

### OneLake Delta

```python
uri = f"abfss://{WORKSPACE}@onelake.dfs.fabric.microsoft.com/{LAKEHOUSE}/Tables/{SCHEMA}/{TABLE}"
dt = DeltaTable(uri, storage_options={"bearer_token": storage_token,
                                      "use_fabric_endpoint": "true"})
table = dt.to_pyarrow_table()
```

Both `storage_options` keys are mandatory. Without `use_fabric_endpoint`,
[delta-rs](https://delta-io.github.io/delta-rs/) treats the host as generic ADLS and
authentication fails.

URI rules that cause silent 404s when broken:

- Workspace GUID goes **before** the `@`, item GUID **after** the host.
- Using GUIDs means **no `.Lakehouse` suffix**. The suffix is only for the
  friendly-name form (`ws-name@.../lh-name.Lakehouse/...`). Never mix the two.
- Schema segment exists only on a
  [schema-enabled lakehouse](https://learn.microsoft.com/en-us/fabric/data-engineering/lakehouse-schemas).
  On a classic lakehouse the path is `Tables/<table>` with no schema level.

`get_add_actions()` gives row counts, file counts and sizes from the transaction log
without reading any data — use it instead of `to_pyarrow_table()` when profiling many
tables. In `deltalake` 1.x it returns an **arro3** table, not a PyArrow one, so convert
before use (`.to_pylist()` on the raw result raises `AttributeError`):

```python
import pyarrow as pa
adds = pa.table(dt.get_add_actions(flatten=True))
files = adds.num_rows
rows  = sum(x or 0 for x in adds.column("num_records").to_pylist())
```

Directory listing, `Files/` access, and the raw DFS REST API are in
[reference/onelake.md](reference/onelake.md).

## Writing data

This skill's examples are read-only by default because read paths are what you
verify first. When you do write:

- **Write through Spark or a pipeline, not `write_deltalake` from a laptop.** Ad-hoc
  writers skip the table maintenance
  ([OPTIMIZE / VACUUM](https://learn.microsoft.com/en-us/fabric/data-engineering/lakehouse-table-maintenance))
  that keeps the SQL endpoint fast, and produce the small-file problem that later
  shows up as slow reports.
- **The SQL analytics endpoint is read-only by design.** DDL there fails with
  `Msg 368 ... external policy action ... was denied` no matter what role you hold.
  That is not a permission bug — write to the Lakehouse via Spark, or use a
  Warehouse, which does accept DDL/DML.
- After an ETL run, the endpoint can lag the Delta files. Force a metadata refresh
  rather than adding sleeps; see [reference/sql-endpoint.md](reference/sql-endpoint.md).

For how tables and schemas should be *shaped* once you can write them — layering,
naming, types, load patterns — use the **modeling-fabric-warehouses** skill.

## Diagnosing failures

Always print `status_code`, the `x-ms-error-code` header, and the first ~500 chars of
the body. Fabric puts the actionable detail in that header, and a bare status code is
usually ambiguous.

| Symptom | Most likely cause |
|---|---|
| Token OK, `GET /v1/workspaces` returns `{"value": []}` | SP not added to any workspace, or tenant-level SP API switch off |
| `401 Audience validation failed` | Token minted for the wrong plane |
| DFS list works, Delta read 403 | Viewer role — enough for SQL, not for direct OneLake file reads |
| Table in OneLake but missing from `INFORMATION_SCHEMA` | Endpoint metadata lag, or table outside `Tables/` |
| `ConnectTimeout` to `api.fabric.microsoft.com` | Egress blocked — the SQL plane on :1433 may still work |

Full decision tree, including how to tell "no permission" apart from "wrong path":
[reference/troubleshooting.md](reference/troubleshooting.md).

## Reference

- [reference/auth.md](reference/auth.md) — token audiences, permission matrix, tenant prerequisites
- [reference/rest-api.md](reference/rest-api.md) — discovering workspaces, items, SQL endpoints
- [reference/onelake.md](reference/onelake.md) — DFS listing, abfss URIs, Delta metadata
- [reference/sql-endpoint.md](reference/sql-endpoint.md) — sqlcmd/pyodbc, T-SQL surface limits, collation
- [reference/troubleshooting.md](reference/troubleshooting.md) — error-to-cause decision tree
- `scripts/fabric_probe.py` — run it; three-plane connectivity probe

### Official documentation

- [Fabric REST API](https://learn.microsoft.com/en-us/rest/api/fabric/articles/) ·
  [OneLake access API](https://learn.microsoft.com/en-us/fabric/onelake/onelake-access-api)
- [Entra auth for Fabric DW](https://learn.microsoft.com/en-us/fabric/data-warehouse/entra-id-authentication) ·
  [Service principals in Fabric DW](https://learn.microsoft.com/en-us/fabric/data-warehouse/service-principals)
- [T-SQL surface area](https://learn.microsoft.com/en-us/fabric/data-warehouse/tsql-surface-area) ·
  [Data types](https://learn.microsoft.com/en-us/fabric/data-warehouse/data-types)
- [OneLake data access roles](https://learn.microsoft.com/en-us/fabric/onelake/security/get-started-data-access-roles)
- [delta-rs](https://delta-io.github.io/delta-rs/) ·
  [go-sqlcmd](https://github.com/microsoft/go-sqlcmd) ·
  [fabric-cli](https://microsoft.github.io/fabric-cli/)
