#!/usr/bin/env python3
"""Extract a Fabric warehouse / lakehouse schema inventory to markdown. READ-ONLY.

Answers "what conventions does this warehouse already use?" before you add to it:
one markdown file per schema listing every table and column with its type, plus a
machine-readable summary and a convention report (case style, suffixes, most frequent
column names, type distribution).

Runs SELECT against INFORMATION_SCHEMA only. No DDL, no DML.

Connects with go-sqlcmd (no ODBC driver, no admin rights):
    https://github.com/microsoft/go-sqlcmd
Looked up at ./tools/sqlcmd[.exe], then PATH, or set SQLCMD_PATH.

Config: a .env beside this script or one directory up, or environment variables.
Environment wins over the file.

    AZURE_TENANT, AZURE_CLIENT_ID, AZURE_CLIENT_SECRET
    FABRIC_SQL_SERVER      the shared TDS FQDN for the workspace
    FABRIC_DATABASES       comma-separated database (item display) names

Usage:
    python inventory_schemas.py                    # databases from FABRIC_DATABASES
    python inventory_schemas.py SalesLakehouse SalesWarehouse       # explicit databases
    python inventory_schemas.py --out ./inventory
"""
from __future__ import annotations

import collections
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

HERE = pathlib.Path(__file__).resolve().parent
for cand in (HERE / ".env", HERE.parent / ".env", HERE.parent.parent / ".env"):
    if cand.is_file():
        for line in cand.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip("\"'"))
        break

TENANT = os.environ.get("AZURE_TENANT", "")
CLIENT_ID = os.environ.get("AZURE_CLIENT_ID", "")
SECRET = os.environ.get("AZURE_CLIENT_SECRET", "")
SERVER = os.environ.get("FABRIC_SQL_SERVER", "")

if not all((TENANT, CLIENT_ID, SECRET, SERVER)):
    sys.exit("ERROR: need AZURE_TENANT, AZURE_CLIENT_ID, AZURE_CLIENT_SECRET, "
             "FABRIC_SQL_SERVER (see the connecting-to-fabric skill to discover the "
             "server FQDN — it cannot be guessed)")

# Parse flags and positionals in one pass, so a flag's VALUE is never mistaken for a
# database name.
OUT = HERE / "inventory"
positional: list[str] = []
_args = sys.argv[1:]
_i = 0
while _i < len(_args):
    if _args[_i] == "--out":
        if _i + 1 >= len(_args):
            sys.exit("ERROR: --out requires a path")
        OUT = pathlib.Path(_args[_i + 1]).expanduser()
        _i += 2
    elif _args[_i].startswith("--"):
        sys.exit(f"ERROR: unknown option {_args[_i]}")
    else:
        positional.append(_args[_i])
        _i += 1

DATABASES = positional or [d.strip() for d in
                           os.environ.get("FABRIC_DATABASES", "").split(",") if d.strip()]
if not DATABASES:
    sys.exit("ERROR: no databases given. Pass them as arguments or set FABRIC_DATABASES.")

# Fabric owns these; they are telemetry and engine metadata, not part of the model.
SYSTEM_SCHEMAS = {"sys", "INFORMATION_SCHEMA", "queryinsights"}

# Unit separator: cannot occur in a SQL identifier, unlike '|' or ',' which appear in
# real data and would corrupt the split.
SEP = "\x1f"


def sqlcmd_path() -> str:
    if os.environ.get("SQLCMD_PATH"):
        return os.environ["SQLCMD_PATH"]
    for c in (HERE / "tools" / "sqlcmd.exe", HERE / "tools" / "sqlcmd",
              HERE.parent / "tools" / "sqlcmd.exe", HERE.parent / "tools" / "sqlcmd"):
        if c.is_file():
            return str(c)
    found = shutil.which("sqlcmd")
    if found:
        return found
    sys.exit("ERROR: sqlcmd not found. Install from "
             "https://github.com/microsoft/go-sqlcmd (single binary, no ODBC driver "
             "and no admin rights needed), or set SQLCMD_PATH.")


SQLCMD = sqlcmd_path()

COLUMNS_SQL = """
SELECT c.TABLE_SCHEMA, c.TABLE_NAME, t.TABLE_TYPE, c.ORDINAL_POSITION,
       c.COLUMN_NAME, c.DATA_TYPE,
       ISNULL(CAST(c.CHARACTER_MAXIMUM_LENGTH AS VARCHAR(20)), ''),
       ISNULL(CAST(c.NUMERIC_PRECISION AS VARCHAR(10)), ''),
       ISNULL(CAST(c.NUMERIC_SCALE AS VARCHAR(10)), ''),
       c.IS_NULLABLE
FROM INFORMATION_SCHEMA.COLUMNS c
JOIN INFORMATION_SCHEMA.TABLES  t
  ON t.TABLE_SCHEMA = c.TABLE_SCHEMA AND t.TABLE_NAME = c.TABLE_NAME
ORDER BY c.TABLE_SCHEMA, c.TABLE_NAME, c.ORDINAL_POSITION
"""


def query(db: str, sql: str) -> list[list[str]]:
    """Run a SELECT and return rows as lists of trimmed fields."""
    proc = subprocess.run(
        [SQLCMD, "-S", SERVER, "-d", db,
         "--authentication-method=ActiveDirectoryServicePrincipal",
         "-U", f"{CLIENT_ID}@{TENANT}",
         # -W trim, -h -1 no header block, -Y/-y 0 no truncation of wide columns
         "-s", SEP, "-W", "-h", "-1", "-Y", "0", "-y", "0",
         "-b",                                   # non-zero exit on SQL error
         "-Q", f"SET NOCOUNT ON; {sql}"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env={**os.environ, "SQLCMDPASSWORD": SECRET},   # keep the secret out of argv
        timeout=900)
    if proc.returncode != 0:
        raise RuntimeError(f"[{db}] sqlcmd exit {proc.returncode}: "
                           f"{(proc.stderr or proc.stdout).strip()[:400]}")
    rows = []
    for line in proc.stdout.splitlines():
        if not line.strip() or line.startswith("(") or set(line.strip()) <= {"-", SEP}:
            continue
        rows.append([c.strip() for c in line.split(SEP)])
    return rows


def full_type(dtype: str, clen: str, prec: str, scale: str) -> str:
    if clen:
        return f"{dtype}({'max' if clen == '-1' else clen})"
    if dtype in ("decimal", "numeric") and prec:
        return f"{dtype}({prec},{scale})"
    return dtype


def collect(db: str) -> dict:
    print(f"[{db}] reading INFORMATION_SCHEMA ...")
    rows = query(db, COLUMNS_SQL)
    print(f"[{db}] {len(rows)} column rows")
    schemas: dict[str, dict] = {}
    for r in rows:
        if len(r) < 10:
            continue
        sch, tbl, ttype, ordi, col, dtype, clen, prec, scale, nullable = r[:10]
        if sch in SYSTEM_SCHEMAS:
            continue
        t = schemas.setdefault(sch, {"tables": {}})["tables"].setdefault(
            tbl, {"type": ttype, "columns": []})
        t["columns"].append({"pos": int(ordi), "name": col,
                             "type": full_type(dtype, clen, prec, scale),
                             "nullable": nullable == "YES"})
    return schemas


def schema_markdown(db: str, sch: str, data: dict) -> str:
    tables = data["tables"]
    views = [t for t, v in tables.items() if v["type"] != "BASE TABLE"]
    out = [f"# {db}.{sch}", "",
           f"{len(tables)} objects ({len(tables) - len(views)} tables, "
           f"{len(views)} views). Qualify as `[{sch}].[<table>]`, connect with "
           f"`-d {db}`.", "",
           "## Contents", "",
           ", ".join(f"`{t}`" for t in sorted(tables)), ""]
    for tbl in sorted(tables):
        v = tables[tbl]
        tag = "" if v["type"] == "BASE TABLE" else f"  _({v['type']})_"
        out += [f"## {sch}.{tbl}{tag}", "", "| # | column | type | null |",
                "|---|---|---|---|"]
        for c in sorted(v["columns"], key=lambda x: x["pos"]):
            out.append(f"| {c['pos']} | `{c['name']}` | {c['type']} | "
                       f"{'Y' if c['nullable'] else 'N'} |")
        out.append("")
    return "\n".join(out)


def conventions_markdown(db: str, schemas: dict) -> str:
    tables = [(s, t, v) for s, d in schemas.items() for t, v in d["tables"].items()]
    names = [t for _, t, _ in tables]
    cols = [c for _, _, v in tables for c in v["columns"]]

    case = collections.Counter()
    for n in names:
        if re.fullmatch(r"[a-z0-9_]+", n):
            case["snake_case"] += 1
        elif re.fullmatch(r"[A-Z][A-Za-z0-9]*", n):
            case["PascalCase"] += 1
        else:
            case["mixed/other"] += 1

    suffixes = {sfx: [n for n in names if n.endswith(sfx)]
                for sfx in ("Truncate", "Current", "History", "Translation",
                            "MapTable", "Rule")}
    view_prefixes = collections.Counter(
        m.group(0) for n in names if (m := re.match(r"(?i)^(vw_?|v_)", n)))
    suspicious = sorted(n for n in names
                        if re.search(r"(?i)(yedek|backup|bkp|_old|^test|^tmp|^temp|^uat)", n))
    non_ascii = sorted(n for n in names if not n.isascii())

    audit = {"InsertedUser", "InsertedDate", "UpdatedUser", "UpdatedDate",
             "IsDeleted", "EtlDate"}
    base = [(s, t, v) for s, t, v in tables if v["type"] == "BASE TABLE"]
    with_audit = sum(1 for _, _, v in base
                     if len(audit & {c["name"] for c in v["columns"]}) >= 5)

    out = [f"# {db} — convention report", "",
           f"{len(schemas)} schemas, {len(tables)} objects, {len(cols)} columns.", "",
           "## Schemas by size", "",
           "| schema | objects | columns |", "|---|---|---|"]
    for s, d in sorted(schemas.items(), key=lambda x: -len(x[1]["tables"])):
        ncol = sum(len(t["columns"]) for t in d["tables"].values())
        out.append(f"| `{s}` | {len(d['tables'])} | {ncol} |")

    out += ["", "## Table name casing", "",
            ", ".join(f"{k}: {v}" for k, v in case.most_common()), "",
            "## Load-pattern and sidecar suffixes", ""]
    for sfx, hits in suffixes.items():
        if hits:
            out.append(f"- `*{sfx}`: {len(hits)} — e.g. {', '.join(sorted(hits)[:3])}")
    if not any(suffixes.values()):
        out.append("- none found: load patterns are not visible in table names")

    out += ["", "## View prefixes", "",
            (", ".join(f"`{k}` x{v}" for k, v in view_prefixes.most_common())
             or "none found"),
            ("" if len(view_prefixes) <= 1 else
             "\n**Inconsistent.** Under the case-sensitive default collation no single "
             "search finds all views. Standardise on one prefix."),
            "", "## Audit columns", "",
            f"{with_audit} of {len(base)} base tables carry at least 5 of "
            f"`InsertedUser, InsertedDate, UpdatedUser, UpdatedDate, IsDeleted, EtlDate`.",
            "", "## Most frequent column names", "",
            "These are the de facto join keys — reuse them rather than inventing "
            "synonyms.", ""]
    for n, c in collections.Counter(c["name"] for c in cols).most_common(20):
        out.append(f"- `{n}` x{c}")

    out += ["", "## Type distribution", ""]
    for t, c in collections.Counter(
            c["type"].split("(")[0] for c in cols).most_common(15):
        out.append(f"- `{t}` x{c}")

    if suspicious:
        out += ["", "## Backup / test / temporary names", "",
                "Indistinguishable from live tables. Move to an archive schema or "
                "delete; rely on source control and Delta time travel instead.", "",
                ", ".join(f"`{n}`" for n in suspicious[:40])]
        if len(suspicious) > 40:
            out.append(f"\n...and {len(suspicious) - 40} more.")
    if non_ascii:
        out += ["", "## Non-ASCII identifiers", "",
                "These break scripts, non-UTF-8 consoles and CSV round-trips.", "",
                ", ".join(f"`{n}`" for n in non_ascii)]
    return "\n".join(out) + "\n"


OUT.mkdir(parents=True, exist_ok=True)
summary: dict = {}

for db in DATABASES:
    try:
        schemas = collect(db)
    except Exception as e:  # noqa: BLE001
        print(f"[{db}] SKIPPED: {e}")
        continue
    db_dir = OUT / db
    db_dir.mkdir(parents=True, exist_ok=True)
    summary[db] = {}
    for sch, data in sorted(schemas.items()):
        (db_dir / f"{sch}.md").write_text(schema_markdown(db, sch, data),
                                          encoding="utf-8")
        ncol = sum(len(t["columns"]) for t in data["tables"].values())
        summary[db][sch] = {"objects": len(data["tables"]), "columns": ncol,
                            "table_names": sorted(data["tables"])}
        print(f"  {db}/{sch}.md  ({len(data['tables'])} objects, {ncol} columns)")
    (db_dir / "CONVENTIONS.md").write_text(conventions_markdown(db, schemas),
                                           encoding="utf-8")
    print(f"  {db}/CONVENTIONS.md")

(OUT / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2),
                                  encoding="utf-8")
print(f"\nWrote {OUT}")
print("Read CONVENTIONS.md first, then the schema you are about to extend.")
