# Troubleshooting

## Contents

- Localise before diagnosing
- Always print these three things
- Symptom table
- "Empty list" is the ambiguous case
- Permission vs path — telling them apart
- Network-restricted environments

## Localise before diagnosing

Fabric failures are cheap to misdiagnose because the same HTTP code means different
things on each plane. Walk the ladder and stop at the first rung that fails — that
rung, not the symptom you started with, is the problem:

1. Token for `storage.azure.com` — credential valid?
2. Token for `api.fabric.microsoft.com` — same, second audience
3. `GET /v1/workspaces` — does the SP see anything at all?
4. `GET /v1/workspaces/{ws}/items` — is *this* workspace reachable?
5. DFS list `{item}/Tables` — is the data plane reachable?
6. `DeltaTable(...)` — is file-level read allowed?
7. `SELECT TOP 1` — is the SQL plane reachable?

`scripts/fabric_probe.py` runs exactly this ladder and continues past failures, so one
run shows every rung's status instead of stopping at the first.

## Always print these three things

```python
print(r.status_code, r.headers.get("x-ms-error-code"), r.text[:500])
```

Fabric puts the actionable detail in `x-ms-error-code`; the status code alone is
usually ambiguous. Truncate the body — error pages can be enormous.

## Symptom table

| Symptom | Cause | Fix |
|---|---|---|
| `AADSTS7000215` on token request | Wrong client secret, or the secret's *ID* used instead of its *value* | Use the secret value; it is shown once at creation |
| `AADSTS700016` | App not found in this tenant | Wrong `AZURE_TENANT` |
| `AADSTS900023` | Invalid tenant identifier | Use the tenant GUID, not `organizations`/`common` |
| Token OK, `/v1/workspaces` → `{"value": []}` | SP not in any workspace, **or** tenant SP-API switch off | See [auth.md](auth.md) — both must be checked |
| `401 Audience validation failed` | Token for the wrong plane | Request the audience that plane needs |
| `403` on `/items` but `/workspaces` lists it | Role too low for that item type | Raise role, or use the SQL plane |
| DFS list OK, `DeltaTable(...)` 403 | Viewer role — no `ReadAll` | Use SQL endpoint, or add a OneLake data access role |
| DFS list `404 PathNotFound` | Wrong URI shape | See below |
| `Msg 368 ... external policy action ... denied` on DDL | Lakehouse SQL endpoint is read-only by design | Write via Spark, or target a Warehouse |
| Table in OneLake, absent from `INFORMATION_SCHEMA` | Metadata lag, or table outside `Tables/` | Refresh endpoint metadata; verify location |
| `Invalid object name` for a table you can see | Case-sensitive collation, or missing schema qualifier | Match case exactly; use `[schema].[table]` |
| Rows present in Delta, missing over SQL | Endpoint metadata stale | Refresh metadata; do not add sleeps |
| `ConnectTimeout` to `api.fabric.microsoft.com` | Egress blocked | See network section below |
| `sqlcmd` connect timeout | Port 1433 blocked, or endpoint still provisioning | Check `provisioningStatus`; check firewall |
| `nvarchar` / `datetime` "type not found" | Unsupported in Fabric | Substitute per [sql-endpoint.md](sql-endpoint.md) |
| Error 511 / 611 on insert | Row exceeds 8,060 bytes | Narrow columns; split the table |
| `429` | Rate limited | Honour `Retry-After`; batch calls |

## "Empty list" is the ambiguous case

`GET /v1/workspaces` returning HTTP 200 with `{"value": []}` is the single most
misleading response in Fabric. It means *at least one* of:

- the SP is not added to any workspace
- the tenant setting "Service principals can use Fabric APIs" is off
- the app is not in the security group that setting allows
- the token is for a different tenant than the workspace

You cannot distinguish these from the response. Check `tid` in the token first (it is
free), then ask a Fabric admin to confirm the tenant switch and the workspace role.

## Permission vs path — telling them apart

A 404 on OneLake usually means the *path* is wrong, not that access is denied —
Fabric returns 403 for access. So when a Delta read 404s:

1. List the parent directory. If the parent lists, the path shape is the problem.
2. Check `.Lakehouse` suffix usage — required with names, forbidden with GUIDs.
3. Check whether the lakehouse is schema-enabled. A schema segment on a classic
   lakehouse (or its absence on a schema-enabled one) 404s.
4. Confirm the table is under `Tables/`, not `Files/`.

Conversely, a 403 is never fixed by editing the URI.

## Network-restricted environments

Corporate networks often allow some Fabric hosts and not others, which produces
confusing partial failures. The planes use different hosts:

| Host | Port | Needed for |
|---|---|---|
| `login.microsoftonline.com` | 443 | all authentication |
| `api.fabric.microsoft.com` | 443 | control plane |
| `onelake.dfs.fabric.microsoft.com` | 443 | OneLake data |
| `*.datawarehouse.fabric.microsoft.com` | **1433** | SQL/TDS |

Port 1433 is blocked far more often than 443, and `ConnectTimeout` on the control
plane does not imply the SQL plane is down — test each separately before concluding
anything. Report which hosts and ports are needed rather than asking for blanket
access.
