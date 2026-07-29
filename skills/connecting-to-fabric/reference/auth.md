# Authentication and permissions

## Contents

- Token audiences per plane
- Client credentials flow
- Inspecting a token before blaming permissions
- Other credential types (certificate, managed identity, user)
- Permission matrix — which role unlocks which plane
- Tenant prerequisites
- Secret hygiene

## Token audiences per plane

| Plane | Scope | Used by |
|---|---|---|
| Fabric REST (control) | `https://api.fabric.microsoft.com/.default` | workspace/item discovery, connection strings |
| OneLake (data) | `https://storage.azure.com/.default` | DFS list-paths, Delta file reads |
| Power BI legacy REST | `https://analysis.windows.net/powerbi/api/.default` | older Power BI endpoints only |
| SQL / TDS | *no bearer token* | driver performs its own Entra handshake |

One token never covers two planes. Request each audience separately; caching a single
token and reusing it everywhere produces `401 Audience validation failed`.

## Client credentials flow

```python
import base64, json, httpx

def get_token(tenant: str, client_id: str, secret: str, scope: str) -> str:
    r = httpx.post(
        f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
        data={"grant_type": "client_credentials", "client_id": client_id,
              "client_secret": secret, "scope": scope},
        timeout=30)
    r.raise_for_status()
    return r.json()["access_token"]
```

Use the **tenant GUID**, not `organizations` or `common`. Those multi-tenant
authorities are invalid for client credentials and produce a token whose `tid` does
not match the workspace, which then looks like a permission problem.

Tokens last about an hour. For long jobs, re-request rather than refresh — the
client-credentials grant issues no refresh token.

## Inspecting a token before blaming permissions

```python
def claims(tok: str) -> dict:
    p = tok.split(".")[1]
    p += "=" * (-len(p) % 4)          # JWT strips base64 padding
    return json.loads(base64.urlsafe_b64decode(p))
```

Check three fields:

- `aud` — matches the plane you are about to call
- `tid` — matches the tenant that owns the workspace
- `appid` — matches the app registration you intended

`roles` being `null` is normal and expected. Fabric workspace access is not an Entra
app role; it is granted inside Fabric. An empty `roles` claim says nothing about
whether the SP can see the workspace.

## Other credential types

**Certificate** — preferred for production; nothing to rotate on a schedule and no
secret to leak into logs.

```bash
az login --service-principal -u <appId> --certificate /path/cert.pem --tenant <tenantId>
```

**Managed identity** — best inside Azure compute; no credential material at all.
`az login --identity`, or `DefaultAzureCredential()` from `azure-identity`.

**User (interactive / device code)** — for local exploration. Note that a *user* often
sees workspaces a *service principal* cannot, so verifying with your own login can
produce a false positive. Always verify with the identity the job will actually use.

If the tenant has no Azure subscription, add `--allow-no-subscriptions` to `az login`
or it fails with "No subscriptions found" before Fabric is ever contacted.

## Permission matrix

Workspace roles do not grant planes uniformly. This is the single most surprising
part of Fabric access control:

| Workspace role | REST discovery | SQL endpoint SELECT | Direct OneLake file read | Write |
|---|---|---|---|---|
| Viewer | yes | yes | **no** | no |
| Contributor | yes | yes | yes | yes |
| Member / Admin | yes | yes | yes | yes |

**Viewer is enough to query data through SQL but not to read the Delta files behind
it.** Direct OneLake reads need `ReadAll`, which Contributor implies.

If you need file-level reads *without* granting write, do not escalate to
Contributor. Use a
[OneLake data access role](https://learn.microsoft.com/en-us/fabric/onelake/security/get-started-data-access-roles)
scoped to specific folders with read-only permission — that is the only combination
that gives Delta access while keeping the identity unable to write.

Diagnostic value: if DFS listing succeeds but `DeltaTable(...)` returns 403, the
identity is almost certainly Viewer. Route through SQL or add a data access role.

## Tenant prerequisites

Both must be true before *any* service principal can call Fabric APIs, and neither is
visible from the failing request:

1. Admin portal → **"Service principals can use Fabric APIs"** enabled.
2. The app is a member of a security group allowed by that setting (unless it is
   enabled for the whole organisation).

See [enabling service principal APIs](https://learn.microsoft.com/en-us/fabric/admin/enable-service-principal-admin-apis).

When either is off, `GET /v1/workspaces` returns HTTP 200 with `{"value": []}` — not
an error. An empty list is therefore ambiguous: it means "no access" *or* "tenant
switch off". Confirm with a tenant admin instead of guessing.

Adding the SP to the workspace is separate again: Workspace → Manage access → add the
app registration by name.

## Secret hygiene

- `.env` in `.gitignore` **before** the first commit.
- Print `len(secret)`, never the secret.
- Pass secrets to child processes via environment (`SQLCMDPASSWORD`), never argv —
  argv is visible to other processes and lands in shell history.
- Prefer certificate or managed identity over a secret wherever the runtime allows.
- A secret's *ID* (the registration identifier shown in the portal) is not the secret
  and is never used in auth. Storing it is harmless but it authenticates nothing.
