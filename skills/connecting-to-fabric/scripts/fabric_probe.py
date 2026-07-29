#!/usr/bin/env python3
"""Microsoft Fabric three-plane connectivity probe. READ-ONLY.

Walks the localisation ladder and continues past failures, so one run shows every
rung's status instead of stopping at the first problem:

  1. Entra token, storage.azure.com      -> credential valid?
  2. Entra token, api.fabric.microsoft.com
  3. GET /v1/workspaces                  -> does this identity see anything?
  4. GET items / lakehouses / warehouses -> SQL endpoint FQDN discovery
  5. OneLake DFS list-paths              -> data plane reachable?
  6. Delta read via delta-rs             -> file-level read allowed?
  7. SELECT over TDS                     -> SQL plane reachable?

The only POST goes to the Entra token endpoint. Nothing else mutates state: no
POST/PUT/PATCH/DELETE against Fabric, no DDL, no DML.

Config: a .env beside this script or one directory up, or --env PATH, or environment
variables. Environment always wins over the file, so the same code runs in CI.

  AZURE_TENANT, AZURE_CLIENT_ID, AZURE_CLIENT_SECRET   (required)
  FABRIC_WORKSPACE, FABRIC_LAKEHOUSE                   (steps 4-6)
  FABRIC_SCHEMA        schema-enabled lakehouse only; omit for a classic lakehouse
  FABRIC_TABLE         defaults to the first table discovered in step 5
  FABRIC_SQL_SERVER    defaults to the FQDN discovered in step 4
  FABRIC_SQL_DB        defaults to the lakehouse display name
  SQLCMD_PATH          defaults to ./tools/sqlcmd[.exe] then PATH

Dependencies:
  pip install httpx              # required, steps 1-5
  pip install deltalake pyarrow  # step 6
  sqlcmd (github.com/microsoft/go-sqlcmd) or pyodbc + ODBC Driver 18  # step 7

Usage:
  python fabric_probe.py [--env PATH]
"""
from __future__ import annotations

import base64
import json
import os
import pathlib
import shutil
import subprocess
import sys

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

HERE = pathlib.Path(__file__).resolve().parent

if "--env" in sys.argv:
    ENV_FILE = pathlib.Path(sys.argv[sys.argv.index("--env") + 1]).expanduser().resolve()
else:
    # Walk up from the script: scripts/ -> skill/ -> skills/ -> repo root.
    _candidates = [HERE / ".env"] + [p / ".env" for p in list(HERE.parents)[:3]]
    ENV_FILE = next((p for p in _candidates if p.is_file()), HERE / ".env")

if ENV_FILE.is_file():
    # utf-8-sig: a BOM from a Windows editor would otherwise corrupt the first key.
    for line in ENV_FILE.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        os.environ.setdefault(k.strip(), v)   # setdefault => real environment wins

TENANT = os.environ.get("AZURE_TENANT", "").strip()
CLIENT_ID = os.environ.get("AZURE_CLIENT_ID", "").strip()
SECRET = os.environ.get("AZURE_CLIENT_SECRET", "").strip()
WORKSPACE = os.environ.get("FABRIC_WORKSPACE", "").strip()
LAKEHOUSE = os.environ.get("FABRIC_LAKEHOUSE", "").strip()
SCHEMA = os.environ.get("FABRIC_SCHEMA", "").strip()
TABLE = os.environ.get("FABRIC_TABLE", "").strip()
SQL_SERVER = os.environ.get("FABRIC_SQL_SERVER", "").strip()
SQL_DB = os.environ.get("FABRIC_SQL_DB", "").strip()

DFS_HOST = os.environ.get("ONELAKE_DFS_HOST", "onelake.dfs.fabric.microsoft.com").strip()
API = os.environ.get("FABRIC_API_HOST", "https://api.fabric.microsoft.com").strip()
LOGIN = os.environ.get("AZURE_LOGIN_HOST", "https://login.microsoftonline.com").strip()
# 30s covers Entra and control-plane calls comfortably. OneLake listings on large
# lakehouses get 60s below, and the SQL step gets 180s for a cold endpoint.
TIMEOUT = float(os.environ.get("FABRIC_TIMEOUT", "30"))

missing = [k for k, v in (("AZURE_TENANT", TENANT), ("AZURE_CLIENT_ID", CLIENT_ID),
                          ("AZURE_CLIENT_SECRET", SECRET)) if not v]
if missing:
    sys.exit(f"ERROR: missing config: {', '.join(missing)}   (looked in {ENV_FILE})")

try:
    import httpx
except ImportError:
    sys.exit("ERROR: httpx is required ->  pip install httpx")


def hdr(n: int, title: str) -> None:
    print(f"\n{'=' * 76}\n{n}) {title}\n{'=' * 76}")


def ok(msg: str) -> None:
    print(f"  [ OK ] {msg}")


def bad(msg: str) -> None:
    print(f"  [FAIL] {msg}")


def claims(tok: str) -> dict:
    p = tok.split(".")[1]
    p += "=" * (-len(p) % 4)          # JWT omits base64 padding
    return json.loads(base64.urlsafe_b64decode(p))


def get_token(scope: str) -> str | None:
    """Client-credentials token. The secret is never printed."""
    try:
        r = httpx.post(f"{LOGIN}/{TENANT}/oauth2/v2.0/token",
                       data={"grant_type": "client_credentials", "client_id": CLIENT_ID,
                             "client_secret": SECRET, "scope": scope}, timeout=TIMEOUT)
    except Exception as e:  # noqa: BLE001
        bad(f"NETWORK ({LOGIN}): {type(e).__name__}: {e}")
        return None
    if r.status_code != 200:
        try:
            b = r.json()
        except Exception:  # noqa: BLE001
            b = {"error": r.status_code, "error_description": r.text[:300]}
        bad(f"HTTP {r.status_code} {b.get('error')}: "
            f"{str(b.get('error_description'))[:400]}")
        return None
    tok = r.json()["access_token"]
    c = claims(tok)
    ok(f"appid={c.get('appid')} tid={c.get('tid')} aud={c.get('aud')} "
       f"roles={c.get('roles')}")
    return tok


def safe_get(url: str, **kw):
    try:
        return httpx.get(url, **kw)
    except Exception as e:  # noqa: BLE001
        bad(f"NETWORK {url.split('?')[0]} -> {type(e).__name__}: {e}")
        return None


def show(label: str, r, body_chars: int = 500) -> bool:
    """Print status, x-ms-error-code and a body excerpt. True if 2xx."""
    if r is None:
        return False
    if r.status_code < 300:
        ok(f"{label} -> HTTP {r.status_code}")
        return True
    bad(f"{label} -> HTTP {r.status_code}")
    for h in ("x-ms-error-code", "x-ms-request-id", "requestId"):
        if r.headers.get(h):
            print(f"         {h}: {r.headers[h]}")
    if r.text.strip():
        print(f"         {r.text.strip()[:body_chars]}")
    return False


print("=" * 76)
print("FABRIC CONNECTIVITY PROBE - read-only (no writes are performed)")
print("=" * 76)
print(f"  env file  : {ENV_FILE if ENV_FILE.is_file() else '(none - environment only)'}")
print(f"  platform  : {sys.platform} | python {sys.version.split()[0]}")
print(f"  tenant    : {TENANT}")
print(f"  client_id : {CLIENT_ID}")
print(f"  secret    : {'*' * 8} ({len(SECRET)} chars)")
print(f"  workspace : {WORKSPACE or '(unset)'}")
print(f"  lakehouse : {LAKEHOUSE or '(unset)'}")
print(f"  schema    : {SCHEMA or '(none - classic lakehouse)'}")

hdr(1, "Entra token - OneLake data plane (https://storage.azure.com/.default)")
storage_tok = get_token("https://storage.azure.com/.default")

hdr(2, "Entra token - Fabric REST (https://api.fabric.microsoft.com/.default)")
fabric_tok = get_token("https://api.fabric.microsoft.com/.default")

if not storage_tok and not fabric_tok:
    print("\nRESULT: no token at all. Verify tenant GUID, client id and secret value.")
    sys.exit(1)

sql_server, sql_db = SQL_SERVER, SQL_DB

if fabric_tok:
    h = {"Authorization": f"Bearer {fabric_tok}"}

    hdr(3, "Fabric REST: workspaces visible to this identity")
    r = safe_get(f"{API}/v1/workspaces", headers=h, timeout=TIMEOUT)
    if show("GET /v1/workspaces", r):
        ws = r.json().get("value", [])
        if not ws:
            print("         EMPTY. Ambiguous - it means at least one of:")
            print("           - the SP is in no workspace (Workspace > Manage access)")
            print("           - tenant setting 'Service principals can use Fabric APIs' off")
            print("           - the app is outside the security group that setting allows")
            print("           - the token tid does not match the workspace tenant")
        for w in ws:
            mark = "   <<< target" if w.get("id") == WORKSPACE else ""
            print(f"         - {w.get('displayName')}  ({w.get('id')}){mark}")
        if WORKSPACE and ws and not any(w.get("id") == WORKSPACE for w in ws):
            print("         WARNING: target workspace is not in the list")

    if WORKSPACE:
        hdr(4, "Fabric REST: items and SQL endpoint discovery")
        r = safe_get(f"{API}/v1/workspaces/{WORKSPACE}/items", headers=h, timeout=TIMEOUT)
        if show("GET .../items", r):
            items = r.json().get("value", [])
            counts: dict[str, int] = {}
            for it in items:
                counts[str(it.get("type"))] = counts.get(str(it.get("type")), 0) + 1
            print(f"         {len(items)} items: "
                  + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
            for it in items:
                if str(it.get("type")) in ("Lakehouse", "Warehouse", "SQLEndpoint"):
                    print(f"         - {str(it.get('type')):<12} {it.get('displayName')} "
                          f"({it.get('id')})")

        if LAKEHOUSE:
            r = safe_get(f"{API}/v1/workspaces/{WORKSPACE}/lakehouses/{LAKEHOUSE}",
                         headers=h, timeout=TIMEOUT)
            if show("GET .../lakehouses/<id>", r):
                body = r.json()
                props = (body.get("properties") or {}).get("sqlEndpointProperties") or {}
                if props.get("connectionString"):
                    sql_server = sql_server or props["connectionString"]
                    sql_db = sql_db or body.get("displayName", "")
                    print(f"         SQL endpoint : {sql_server}")
                    print(f"         database     : {sql_db}  "
                          f"(provisioning={props.get('provisioningStatus')})")

        r = safe_get(f"{API}/v1/workspaces/{WORKSPACE}/warehouses", headers=h,
                     timeout=TIMEOUT)
        if show("GET .../warehouses", r):
            whs = r.json().get("value", [])
            if not whs:
                print("         (none) - lakehouse SQL endpoint only")
            for w in whs:
                cs = ((w.get("properties") or {}).get("connectionString")) or "?"
                print(f"         - {w.get('displayName')} ({w.get('id')})  server={cs}")
                if cs != "?" and not sql_server:
                    sql_server, sql_db = cs, w.get("displayName", "")

tables: list[str] = []
if storage_tok and WORKSPACE:
    hdr(5, "OneLake DFS: list-paths")
    h = {"Authorization": f"Bearer {storage_tok}", "x-ms-version": "2023-11-03"}
    base = f"https://{DFS_HOST}/{WORKSPACE}"

    r = safe_get(base, params={"resource": "filesystem", "recursive": "false"},
                 headers=h, timeout=60)
    if show("workspace root", r):
        for p in r.json().get("paths", []):
            print(f"         - {p.get('name')}")

    if LAKEHOUSE:
        table_dir = f"{LAKEHOUSE}/Tables/{SCHEMA}" if SCHEMA else f"{LAKEHOUSE}/Tables"
        for d in dict.fromkeys([f"{LAKEHOUSE}/Tables", table_dir, f"{LAKEHOUSE}/Files"]):
            r = safe_get(base, params={"resource": "filesystem", "recursive": "false",
                                       "directory": d}, headers=h, timeout=60)
            if show(f"dir {d}", r):
                names = [str(p.get("name", "")).rsplit("/", 1)[-1]
                         for p in r.json().get("paths", [])]
                print("         " + (", ".join(names) if names else "(empty)"))
                if d == table_dir:
                    tables = names

hdr(6, "Delta read via delta-rs (read-only)")
target = TABLE or (tables[0] if tables else "")
if not (storage_tok and WORKSPACE and LAKEHOUSE and target):
    print("  (skipped) need token + workspace + lakehouse + a table; step 5 found none "
          "and FABRIC_TABLE is unset")
else:
    try:
        from deltalake import DeltaTable
    except ImportError:
        print("  (skipped) deltalake not installed ->  pip install deltalake pyarrow")
    else:
        path = f"{LAKEHOUSE}/Tables/{SCHEMA}/{target}" if SCHEMA \
            else f"{LAKEHOUSE}/Tables/{target}"
        uri = f"abfss://{WORKSPACE}@{DFS_HOST}/{path}"
        print(f"  uri: {uri}")
        try:
            dt = DeltaTable(uri, storage_options={"bearer_token": storage_tok,
                                                  "use_fabric_endpoint": "true"})
            # Transaction-log stats first: no data transfer.
            # deltalake 1.x returns an arro3 table here, not a PyArrow one, so it must
            # be converted; calling .to_pylist() on the raw result raises AttributeError.
            try:
                import pyarrow as pa

                adds = pa.table(dt.get_add_actions(flatten=True))
                nrec = [x for x in adds.column("num_records").to_pylist()
                        if x is not None]
                size = sum(x or 0 for x in adds.column("size_bytes").to_pylist())
                ok(f"metadata: delta v{dt.version()}, {adds.num_rows} file(s), "
                   f"{size / 1e6:.1f} MB"
                   + (f", {sum(nrec)} rows" if nrec else ", row count not in stats"))
            except Exception as e:  # noqa: BLE001
                # Report rather than swallow: a silent skip here is how a broken
                # profile ends up showing "?" for every row count.
                ok(f"metadata: delta v{dt.version()} "
                   f"(add-action stats unavailable: {type(e).__name__})")
            print("         columns: "
                  + ", ".join(f.name for f in dt.schema().fields))
            tbl = dt.to_pyarrow_table()
            ok(f"data read: {tbl.num_rows} rows x {tbl.num_columns} cols")
            for i, row in enumerate(tbl.slice(0, 3).to_pylist(), 1):
                short = {k: (str(v)[:40] if v is not None else None)
                         for k, v in list(row.items())[:6]}
                print(f"         row{i}: {short}")
        except Exception as e:  # noqa: BLE001
            bad(f"{type(e).__name__}: {str(e)[:400]}")
            print("         403 here while step 5 succeeded => Viewer role. Use the SQL")
            print("         endpoint, or add a OneLake data access role for file reads.")

hdr(7, "SQL over TDS (SELECT only)")


def find_sqlcmd() -> str | None:
    if os.environ.get("SQLCMD_PATH"):
        return os.environ["SQLCMD_PATH"]
    for c in (HERE / "tools" / "sqlcmd.exe", HERE / "tools" / "sqlcmd"):
        if c.is_file():
            return str(c)
    return shutil.which("sqlcmd")


SEP = "\x1f"          # unit separator: cannot occur in SQL identifiers
METADATA_SQL = ("SELECT TOP 20 TABLE_SCHEMA, TABLE_NAME "
                "FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_TYPE='BASE TABLE' "
                "AND TABLE_SCHEMA NOT IN ('sys','queryinsights') "
                "ORDER BY TABLE_SCHEMA, TABLE_NAME")

if not sql_server:
    print("  (skipped) SQL server FQDN unknown - set FABRIC_SQL_SERVER, or let step 4")
    print("  discover it. It is a generated string and cannot be guessed.")
elif not sql_db:
    print("  (skipped) database name unknown - set FABRIC_SQL_DB to the item's display")
    print("  name (not its GUID).")
else:
    print(f"  server: {sql_server}")
    print(f"  database: {sql_db}")
    exe = find_sqlcmd()
    if exe:
        try:
            proc = subprocess.run(
                [exe, "-S", sql_server, "-d", sql_db,
                 "--authentication-method=ActiveDirectoryServicePrincipal",
                 "-U", f"{CLIENT_ID}@{TENANT}",
                 "-s", SEP, "-W", "-h", "-1", "-Y", "0", "-y", "0",
                 "-Q", f"SET NOCOUNT ON; {METADATA_SQL}"],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                env={**os.environ, "SQLCMDPASSWORD": SECRET},  # keep secret out of argv
                timeout=180)
            if proc.returncode != 0:
                bad(f"sqlcmd exit {proc.returncode}: "
                    f"{(proc.stderr or proc.stdout).strip()[:400]}")
            else:
                rows = [ln.split(SEP) for ln in proc.stdout.splitlines()
                        if ln.strip() and not ln.startswith("(")]
                ok(f"connected via sqlcmd - {len(rows)} table(s) listed (top 20)")
                for r_ in rows[:10]:
                    print(f"         - {'.'.join(c.strip() for c in r_)}")
        except Exception as e:  # noqa: BLE001
            bad(f"sqlcmd: {type(e).__name__}: {str(e)[:300]}")
    else:
        print("  sqlcmd not found. Preferred client - no ODBC driver, no admin rights:")
        print("  https://github.com/microsoft/go-sqlcmd")
        try:
            import pyodbc
        except ImportError:
            print("  (skipped) pyodbc not installed either ->  pip install pyodbc")
        else:
            drv = next((d for d in ("ODBC Driver 18 for SQL Server",
                                    "ODBC Driver 17 for SQL Server")
                        if d in pyodbc.drivers()), None)
            if not drv:
                print(f"  (skipped) no suitable ODBC driver. Installed: {pyodbc.drivers()}")
                print("  Linux: install msodbcsql18 from the Microsoft package repo")
            else:
                conn_str = (
                    f"Driver={{{drv}}};Server={sql_server},1433;Database={sql_db};"
                    f"Authentication=ActiveDirectoryServicePrincipal;"
                    f"UID={CLIENT_ID};PWD={SECRET};"
                    f"Encrypt=yes;TrustServerCertificate=no;Connection Timeout=60;")
                try:
                    with pyodbc.connect(conn_str, timeout=60) as cn:
                        cur = cn.cursor()
                        cur.execute(METADATA_SQL)
                        rows = cur.fetchall()
                        ok(f"connected via pyodbc ({drv}) - {len(rows)} table(s)")
                        for s, t in rows[:10]:
                            print(f"         - {s}.{t}")
                except Exception as e:  # noqa: BLE001
                    bad(f"pyodbc: {type(e).__name__}: {str(e)[:400]}")

print("\n" + "=" * 76)
print("Done - read-only (token + GET + SELECT). No writes were performed.")
print("=" * 76)
