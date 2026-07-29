#!/usr/bin/env python3
"""Measure Delta -> SQL analytics endpoint behaviour in YOUR Fabric environment.

Fabric changes between releases, and a Warehouse behaves differently from a Lakehouse
endpoint. Rather than trusting a published table, re-measure. This script writes a few
tiny tables into a scratch schema you name, reads them back through both planes, and
reports:

  1. Does writing an abfss path create the schema?
  2. Delta type -> SQL endpoint type, per column.
  3. Which columns DISAPPEAR over SQL. (Timezone-naive timestamps do. Silently.)
  4. How long the SQL endpoint metadata lag actually is.
  5. Do append / overwrite / replaceWhere work?
  6. Does OPTIMIZE (compact) work through delta-rs, and by how much?
  7. Are identifiers case-sensitive over SQL?

SAFETY
------
Every write goes through _uri(), which asserts the path stays inside
Tables/<SCRATCH_SCHEMA>/. Anything else raises AssertionError before a request is made.
sql() refuses any statement containing a DDL/DML keyword, so this script can only ever
SELECT over TDS. It never calls POST/PUT/PATCH/DELETE against the Fabric REST API.

Even so: point it at a scratch schema in a NON-PRODUCTION workspace if you have one,
and use a name nobody could mistake for real data. The default sorts last and reads as
deliberate.

USAGE
-----
    pip install httpx deltalake pyarrow
    # plus sqlcmd: https://github.com/microsoft/go-sqlcmd

    python type_mapping_probe.py                       # schema: zz_scratch_probe
    python type_mapping_probe.py --schema zz_my_test
    python type_mapping_probe.py --cleanup             # delete the scratch schema

Config: environment variables or a .env (see .env.example at the repo root).
Requires AZURE_TENANT, AZURE_CLIENT_ID, AZURE_CLIENT_SECRET, FABRIC_WORKSPACE,
FABRIC_LAKEHOUSE, FABRIC_SQL_SERVER, FABRIC_SQL_DB, and OneLake WRITE permission
(Viewer is not enough -- Contributor, or a OneLake data access role granting write).
"""
from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import sys
import time
from decimal import Decimal

import httpx
import pyarrow as pa
from deltalake import DeltaTable, write_deltalake

HERE = pathlib.Path(__file__).resolve().parent
for cand in (HERE / ".env", HERE.parent / ".env", HERE.parent.parent / ".env",
             HERE.parent.parent.parent / ".env"):
    if cand.is_file():
        for line in cand.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip("\"'"))
        break


def _need(name: str) -> str:
    v = os.environ.get(name, "").strip()
    if not v:
        sys.exit(f"ERROR: {name} is required (see .env.example)")
    return v


TENANT, CLIENT_ID, SECRET = _need("AZURE_TENANT"), _need("AZURE_CLIENT_ID"), _need("AZURE_CLIENT_SECRET")
WORKSPACE, LAKEHOUSE = _need("FABRIC_WORKSPACE"), _need("FABRIC_LAKEHOUSE")
SQL_SERVER, SQL_DB = _need("FABRIC_SQL_SERVER"), _need("FABRIC_SQL_DB")
DFS_HOST = os.environ.get("ONELAKE_DFS_HOST", "onelake.dfs.fabric.microsoft.com")

args = sys.argv[1:]
SCRATCH = (args[args.index("--schema") + 1] if "--schema" in args
           else "zz_scratch_probe")
CLEANUP = "--cleanup" in args

# ------------------------------------------------------------------ SAFETY LOCK
PREFIX = f"{LAKEHOUSE}/Tables/{SCRATCH}/"
if len(SCRATCH) < 4 or "/" in SCRATCH or ".." in SCRATCH:
    sys.exit(f"ERROR: refusing an unsafe scratch schema name: {SCRATCH!r}")


def _uri(table: str) -> str:
    """Only paths inside the scratch schema. Anything else raises."""
    if not table or "/" in table or ".." in table:
        raise AssertionError(f"invalid table name: {table!r}")
    path = f"{LAKEHOUSE}/Tables/{SCRATCH}/{table}"
    if not path.startswith(PREFIX):
        raise AssertionError(f"SAFETY LOCK: {path} is outside {PREFIX}")
    return f"abfss://{WORKSPACE}@{DFS_HOST}/{path}"


def token(scope: str) -> str:
    r = httpx.post(f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/token",
                   data={"grant_type": "client_credentials", "client_id": CLIENT_ID,
                         "client_secret": SECRET, "scope": scope}, timeout=30)
    r.raise_for_status()
    return r.json()["access_token"]


STOK = token("https://storage.azure.com/.default")
OPTS = {"bearer_token": STOK, "use_fabric_endpoint": "true"}
DFS_HDR = {"Authorization": f"Bearer {STOK}", "x-ms-version": "2023-11-03"}
SEP = "\x1f"   # unit separator: cannot occur in a SQL identifier


def find_sqlcmd() -> str:
    if os.environ.get("SQLCMD_PATH"):
        return os.environ["SQLCMD_PATH"]
    for c in (HERE / "tools" / "sqlcmd.exe", HERE / "tools" / "sqlcmd"):
        if c.is_file():
            return str(c)
    found = shutil.which("sqlcmd")
    if found:
        return found
    sys.exit("ERROR: sqlcmd not found. Install from "
             "https://github.com/microsoft/go-sqlcmd, or set SQLCMD_PATH.")


SQLCMD = find_sqlcmd()


def sql(q: str) -> list[list[str]]:
    """SELECT only. This script never issues DDL or DML over TDS."""
    low = q.lower()
    for banned in ("insert ", "update ", "delete ", "create ", "drop ", "alter ",
                   "truncate ", "merge "):
        if banned in low:
            raise AssertionError(f"SAFETY LOCK: refused SQL write ({banned.strip()})")
    p = subprocess.run(
        [SQLCMD, "-S", SQL_SERVER, "-d", SQL_DB,
         "--authentication-method=ActiveDirectoryServicePrincipal",
         "-U", f"{CLIENT_ID}@{TENANT}", "-s", SEP, "-W", "-h", "-1",
         "-Y", "0", "-y", "0", "-Q", f"SET NOCOUNT ON; {q}"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env={**os.environ, "SQLCMDPASSWORD": SECRET}, timeout=300)
    if p.returncode != 0:
        return [["ERR", (p.stderr or p.stdout).strip()[:200]]]
    return [ln.split(SEP) for ln in p.stdout.splitlines()
            if ln.strip() and not ln.startswith("(")]


def hdr(t: str) -> None:
    print(f"\n{'=' * 76}\n{t}\n{'=' * 76}")


def files_summary(d: DeltaTable) -> str:
    # deltalake 1.x returns an arro3 table here, NOT PyArrow: .to_pylist() is absent.
    a = pa.table(d.get_add_actions(flatten=True))
    return (f"{a.num_rows} file(s), "
            f"{sum(x or 0 for x in a.column('num_records').to_pylist())} rows, "
            f"{sum(x or 0 for x in a.column('size_bytes').to_pylist())} bytes")


def list_schemas() -> list[str]:
    r = httpx.get(f"https://{DFS_HOST}/{WORKSPACE}",
                  params={"resource": "filesystem", "recursive": "false",
                          "directory": f"{LAKEHOUSE}/Tables"},
                  headers=DFS_HDR, timeout=60)
    r.raise_for_status()
    return [p["name"].rsplit("/", 1)[-1] for p in r.json().get("paths", [])]


# ------------------------------------------------------------------- cleanup mode
if CLEANUP:
    target = f"{LAKEHOUSE}/Tables/{SCRATCH}"
    if not target.endswith(f"/Tables/{SCRATCH}"):
        sys.exit("SAFETY LOCK: refusing to delete an unexpected path")
    print(f"Deleting scratch schema: {target}")
    r = httpx.delete(f"https://{DFS_HOST}/{WORKSPACE}/{target}",
                     params={"recursive": "true"}, headers=DFS_HDR, timeout=120)
    print(f"  DELETE -> HTTP {r.status_code} "
          f"{r.headers.get('x-ms-error-code', '')} {r.text[:200]}")
    print(f"  remaining schemas: {list_schemas()}")
    sys.exit(0)

print(f"scratch schema : {SCRATCH}   (the ONLY place this script writes)")
print(f"safety prefix  : {PREFIX}")

# ------------------------------------------- 1) does writing create the schema?
hdr("1) Does writing an abfss path create the schema?")
before = list_schemas()
t0 = time.time()

types_tbl = pa.table({
    "c_long": pa.array([1, 2, 3], pa.int64()),
    "c_int": pa.array([1, 2, 3], pa.int32()),
    "c_string": pa.array(["a", "b", "c"], pa.string()),
    "c_string_long": pa.array(["x" * 5000] * 3, pa.string()),
    "c_bool": pa.array([True, False, True], pa.bool_()),
    "c_double": pa.array([1.5, 2.5, 3.5], pa.float64()),
    "c_decimal": pa.array([Decimal("1.2300"), Decimal("4.5600"), Decimal("7.8900")],
                          pa.decimal128(18, 4)),
    "c_date": pa.array([19000, 19001, 19002], pa.date32()),
    "c_binary": pa.array([b"\x01", b"\x02", b"\x03"], pa.binary()),
    "c_all_null": pa.array([None, None, None], pa.string()),
    # The critical pair: identical values, one naive, one UTC-aware.
    "c_ts_naive": pa.array([1700000000000000] * 3, pa.timestamp("us")),
    "c_ts_utc": pa.array([1700000000000000] * 3, pa.timestamp("us", tz="UTC")),
})
try:
    write_deltalake(_uri("t_types"), types_tbl, mode="overwrite", storage_options=OPTS)
except Exception as e:  # noqa: BLE001
    print(f"  [FAIL] write failed: {type(e).__name__}: {str(e)[:400]}")
    print("  OneLake write permission is missing. Viewer grants reads only; you need")
    print("  Contributor, or a OneLake data access role that grants write.")
    sys.exit(1)

after = list_schemas()
print(f"  wrote t_types in {time.time() - t0:.1f}s")
print(f"  schemas before : {before}")
print(f"  schemas after  : {after}")
print(f"  schema created by the write alone: {SCRATCH in after and SCRATCH not in before}")

# ------------------------------------------------------ 2) metadata lag
hdr("2) How long is the SQL endpoint metadata lag?")
seen_at = None
for wait in (0, 15, 30, 60, 120, 240):
    if wait:
        time.sleep(wait)
    rows = sql(f"SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES "
               f"WHERE TABLE_SCHEMA='{SCRATCH}'")
    names = [r[0].strip() for r in rows if r and r[0] != "ERR" and r[0].strip()]
    elapsed = int(time.time() - t0)
    print(f"  t+{elapsed:>4}s -> {names or '(not visible yet)'}")
    if names:
        seen_at = elapsed
        break
print(f"  MEASURED LAG: ~{seen_at}s" if seen_at else
      "  Not visible within ~7 min -> a metadata refresh is required")

# ------------------------------------------- 3) type mapping + missing columns
hdr("3) Delta type -> SQL type, and which columns disappear")
dt = DeltaTable(_uri("t_types"), storage_options=OPTS)
delta_types = {f.name: str(f.type) for f in dt.schema().fields}
rows = sql(f"SELECT COLUMN_NAME, DATA_TYPE, "
           f"ISNULL(CAST(CHARACTER_MAXIMUM_LENGTH AS VARCHAR(20)),''), "
           f"ISNULL(CAST(NUMERIC_PRECISION AS VARCHAR(10)),''), "
           f"ISNULL(CAST(NUMERIC_SCALE AS VARCHAR(10)),'') "
           f"FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA='{SCRATCH}' "
           f"AND TABLE_NAME='t_types' ORDER BY ORDINAL_POSITION")
sql_types: dict[str, str] = {}
for r in rows:
    if len(r) >= 5 and r[0] != "ERR":
        col, dtyp, clen, prec, scale = (x.strip() for x in r[:5])
        if clen:
            dtyp += f"({'max' if clen == '-1' else clen})"
        elif prec and dtyp in ("decimal", "numeric"):
            dtyp += f"({prec},{scale})"
        sql_types[col] = dtyp

print(f"  {'column':<16} {'Delta':<26} {'SQL endpoint'}")
for k, v in delta_types.items():
    print(f"  {k:<16} {v:<26} {sql_types.get(k, '*** MISSING ***')}")

missing = set(delta_types) - set(sql_types)
print(f"\n  COLUMNS INVISIBLE OVER SQL: {missing or '(none)'}")
if missing:
    print("  A timezone-naive timestamp becomes Delta timestamp_ntz, which the SQL")
    print("  analytics endpoint omits entirely -- no error, no warning. Always write")
    print('  timestamps as pa.timestamp("us", tz="UTC").')

# ------------------------------------------- 4) append / overwrite / replaceWhere
hdr("4) append / overwrite / replaceWhere")
base = pa.table({"id": pa.array([1, 2, 3], pa.int64()),
                 "batch": pa.array(["b1"] * 3, pa.string())})
extra = pa.table({"id": pa.array([4, 5], pa.int64()),
                  "batch": pa.array(["b2"] * 2, pa.string())})
write_deltalake(_uri("t_load"), base, mode="overwrite", storage_options=OPTS)
d = DeltaTable(_uri("t_load"), storage_options=OPTS)
print(f"  overwrite    -> {d.to_pyarrow_table().num_rows} rows, v{d.version()}")
write_deltalake(_uri("t_load"), extra, mode="append", storage_options=OPTS)
d = DeltaTable(_uri("t_load"), storage_options=OPTS)
print(f"  append       -> {d.to_pyarrow_table().num_rows} rows, v{d.version()}")
try:
    write_deltalake(_uri("t_load"), extra, mode="overwrite",
                    predicate="batch = 'b2'", storage_options=OPTS)
    d = DeltaTable(_uri("t_load"), storage_options=OPTS)
    print(f"  replaceWhere -> {d.to_pyarrow_table().num_rows} rows, v{d.version()}")
except Exception as e:  # noqa: BLE001
    print(f"  replaceWhere -> FAILED: {type(e).__name__}: {str(e)[:200]}")

# ---------------------------------------------------- 5) OPTIMIZE / VACUUM
hdr("5) OPTIMIZE (compact) and VACUUM through delta-rs")
for i in range(4):        # deliberately create small files
    write_deltalake(_uri("t_maint"), pa.table({"id": pa.array([i], pa.int64())}),
                    mode="append" if i else "overwrite", storage_options=OPTS)
d = DeltaTable(_uri("t_maint"), storage_options=OPTS)
print(f"  before compact -> {files_summary(d)}, v{d.version()}")
try:
    m = d.optimize.compact()
    d = DeltaTable(_uri("t_maint"), storage_options=OPTS)
    print(f"  after compact  -> {files_summary(d)}, v{d.version()}")
    print(f"  metrics        -> added={m.get('numFilesAdded')} "
          f"removed={m.get('numFilesRemoved')}")
except Exception as e:  # noqa: BLE001
    print(f"  compact FAILED: {type(e).__name__}: {str(e)[:200]}")
try:
    # 168h = the 7-day default; Fabric rejects shorter retention by design.
    print(f"  vacuum dry-run -> {len(d.vacuum(retention_hours=168, dry_run=True))} "
          f"file(s) eligible")
except Exception as e:  # noqa: BLE001
    print(f"  vacuum FAILED: {type(e).__name__}: {str(e)[:200]}")

# ------------------------------------------------- 6) case sensitivity
hdr("6) Are identifiers case-sensitive over SQL?")
for q in (f"SELECT COUNT_BIG(*) FROM [{SCRATCH}].[t_types]",
          f"SELECT COUNT_BIG(*) FROM [{SCRATCH}].[T_TYPES]"):
    rows = sql(q)
    val = rows[0][0].strip() if rows and rows[0][0] != "ERR" else "ERROR (see below)"
    if rows and rows[0][0] == "ERR":
        val = rows[0][1][:70]
    print(f"  {q.split('FROM ')[1]:<34} -> {val}")

hdr("Summary")
rows = sql(f"SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES "
           f"WHERE TABLE_SCHEMA='{SCRATCH}' ORDER BY TABLE_NAME")
print(f"  tables in {SCRATCH}: {[r[0].strip() for r in rows if r[0] != 'ERR']}")
print(f"  wrote only under: {PREFIX}")
print(f"\n  Clean up with:  python {pathlib.Path(__file__).name} "
      f"--schema {SCRATCH} --cleanup")
