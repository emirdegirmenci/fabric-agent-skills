# Fabric REST API — discovery

## Contents

- Base URL and headers
- Listing workspaces
- Listing items
- Getting a Lakehouse SQL endpoint
- Listing Warehouses
- Pagination
- Long-running operations
- Rate limiting

Base: `https://api.fabric.microsoft.com/v1` · audience
`https://api.fabric.microsoft.com/.default` · header `Authorization: Bearer <token>`.

Full reference: [Fabric REST API](https://learn.microsoft.com/en-us/rest/api/fabric/articles/).

## Listing workspaces

```
GET /v1/workspaces
```

The single most useful diagnostic call: it shows exactly what the identity can see.

```python
r = httpx.get("https://api.fabric.microsoft.com/v1/workspaces",
              headers={"Authorization": f"Bearer {fabric_token}"}, timeout=30)
for w in r.json()["value"]:
    print(w["displayName"], w["id"])
```

HTTP 200 with an empty `value` is the normal shape of "no access" — see
[auth.md](auth.md) for the two tenant prerequisites that also produce it.

[API docs](https://learn.microsoft.com/en-us/rest/api/fabric/core/workspaces/list-workspaces)

## Listing items

```
GET /v1/workspaces/{workspaceId}/items
```

Returns every item with `type`, `displayName`, `id`. Useful `type` values:
`Lakehouse`, `Warehouse`, `SQLEndpoint`, `Notebook`, `SemanticModel`, `Report`,
`DataPipeline`, `Eventhouse`.

Filter client-side by `type` and match `displayName` to resolve a name to a GUID.
Do this once at startup and cache — never hardcode GUIDs in code that ships to
another tenant.

In a mature workspace this response is large (hundreds of items). Request it once and
index it rather than re-querying per lookup.

## Getting a Lakehouse SQL endpoint

The TDS server FQDN is a generated string; it cannot be derived from the workspace or
item name and must be read from the API (or copied from the portal).

```
GET /v1/workspaces/{workspaceId}/lakehouses/{lakehouseId}
```

```python
body = r.json()
props = body["properties"]["sqlEndpointProperties"]
server   = props["connectionString"]          # ...datawarehouse.fabric.microsoft.com
database = body["displayName"]                # item display name, NOT the GUID
status   = props["provisioningStatus"]        # must be "Success"
```

If `provisioningStatus` is not `Success`, the endpoint is still being created and
connections fail with a generic timeout. Poll rather than retry the TDS connection.

[API docs](https://learn.microsoft.com/en-us/rest/api/fabric/lakehouse/items/get-lakehouse)

## Listing Warehouses

```
GET /v1/workspaces/{workspaceId}/warehouses
```

Each entry carries `properties.connectionString`. **All Lakehouse endpoints and
Warehouses in one workspace share the same server FQDN** — they are separate
databases on it. So one connection string plus `-d <database>` reaches everything,
and cross-database queries with three-part naming work between them:

```sql
SELECT * FROM OtherDatabase.some_schema.some_table;
```

Fabric also auto-creates staging warehouses (names containing
`StagingWarehouseForDataflows`). They are Dataflow internals — skip them when
enumerating real targets.

## Pagination

List endpoints return `continuationToken` / `continuationUri` when truncated. Loop
until absent:

```python
items, url = [], f"{BASE}/workspaces/{ws}/items"
while url:
    r = httpx.get(url, headers=hdr, timeout=30); r.raise_for_status()
    body = r.json()
    items += body.get("value", [])
    url = body.get("continuationUri")
```

Never assume the first page is complete. A workspace that fits on one page today will
not after the next project lands.

## Long-running operations

Creation and refresh calls return `202 Accepted` with a `Location` header. Poll it
until `status` is `Succeeded` or `Failed`; honour `Retry-After`.

Read-only workflows do not hit this path — if a GET returns 202, you called a
different endpoint than you intended.

## Rate limiting

`429 Too Many Requests` carries `Retry-After` (seconds). Respect it and add jitter.
Prefer one broad call plus client-side filtering over many narrow calls — enumerating
items once beats one lookup per table.
