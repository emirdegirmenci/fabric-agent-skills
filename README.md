# Fabric Agent Skills

**Agent Skills for Microsoft Fabric — connect, load, and model, with rules measured against a live environment rather than copied from documentation.**

[![Claude Code Plugin](https://img.shields.io/badge/Claude_Code-Plugin-D97757?logo=anthropic&logoColor=white)](https://docs.claude.com/en/docs/claude-code)
[![Agent Skills](https://img.shields.io/badge/Agent_Skills-standard-1f6feb)](https://agentskills.io)
[![Microsoft Fabric](https://img.shields.io/badge/Microsoft-Fabric-117865?logo=microsoft&logoColor=white)](https://www.microsoft.com/microsoft-fabric)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](./LICENSE)
[![Version](https://img.shields.io/badge/version-1.0.0-blue)](https://github.com/emirdegirmenci/fabric-agent-skills/releases)

`#microsoft-fabric` · `#onelake` · `#delta-lake` · `#lakehouse` · `#data-engineering` · `#agent-skills` · `#claude-code`

Three skills that teach a coding agent (Claude Code, Cursor, GitHub Copilot CLI, Codex, or anything supporting the [Agent Skills](https://agentskills.io) standard) how to work with Microsoft Fabric without the usual two days of trial and error.

```
skills/
├── connecting-to-fabric/        auth, OneLake Delta, SQL endpoint, REST, error triage
├── loading-fabric-tables/       INSERT/MERGE/CTAS, incremental loads, OPTIMIZE/VACUUM
└── modeling-fabric-warehouses/  schema layering, naming, types, keys
```

## Why this exists

Fabric's failure modes are mostly silent. A timezone-naive timestamp column vanishes from every SQL consumer with no error. A `sleep(30)` after a load passes while the table is still invisible. A token that works on one plane returns 401 on another and looks like a permissions problem.

None of that is obvious from the documentation. These skills encode what was actually measured.

## Highlights — things you probably do not know yet

| Finding | Why it matters |
|---|---|
| A Delta `timestamp_ntz` column is **completely invisible** to the SQL analytics endpoint — no error, column simply absent | PyArrow's default timestamp is naive, so **the wrong thing is the default**. Fix: `pa.timestamp("us", tz="UTC")` |
| SQL endpoint metadata lag was **measured at ~116 seconds** (absent at 5s / 22s / 54s) | The instinctive `sleep(30)` passes while the table is still missing, and the pipeline continues as if the load landed |
| Every Delta `string` surfaces as `varchar(8000)`, whatever the content length | Column-width discipline is a Warehouse-DDL lever, not a Lakehouse one. And the 8,060-byte row limit does not constrain the endpoint's projection |
| All-NULL columns **do** appear (contrary to popular belief) | If a column is missing, suspect the timestamp type, not the NULLs |
| Identifiers are case-sensitive: `[t_types]` works, `[T_TYPES]` → `Msg 208` | The default collation is `Latin1_General_100_BIN2_UTF8`, byte-ordered |
| Writing to `Tables/<new_schema>/<table>` **creates the schema** — no DDL, no portal step | A typo silently creates a second schema instead of failing |
| A Lakehouse SQL analytics endpoint rejects **all** DDL/DML by design | `Msg 368 ... external policy action ... denied` is architectural, not a role problem — stop escalating permissions |
| Viewer can query through SQL but **cannot** read OneLake files | Use a OneLake data access role for read-only file access instead of granting Contributor |
| `sqlcmd` needs **no ODBC driver and no admin rights** | [go-sqlcmd](https://github.com/microsoft/go-sqlcmd) is a single static binary with built-in Entra service-principal auth |
| `OPTIMIZE` works from `delta-rs`, no Spark needed | Measured: 4 files / 1,944 bytes → 1 file / 513 bytes |

Every claim marked **measured** in the skills was produced by writing to and reading from a real Fabric lakehouse, not inferred from docs.

## Install

### Claude Code — plugin marketplace (one command)

```
/plugin marketplace add emirdegirmenci/fabric-agent-skills
/plugin install fabric-agent-skills@fabric-agent-skills
```

### Any agent — copy the skill folders

```bash
git clone https://github.com/emirdegirmenci/fabric-agent-skills.git

# Claude Code, personal (all projects)
cp -r fabric-agent-skills/skills/* ~/.claude/skills/

# Claude Code, one project
cp -r fabric-agent-skills/skills/* /path/to/project/.claude/skills/

# Cross-runtime alias recognised by Codex, Copilot CLI and Gemini CLI
cp -r fabric-agent-skills/skills/* ~/.agents/skills/
```

Skills load on demand: only the `name` and `description` sit in context until one becomes relevant, so the reference material costs nothing until it is needed.

## What each skill covers

### `connecting-to-fabric`

Fabric has three independent planes, each needing a different token audience and granting different permissions. Getting this wrong is the most common source of wasted debugging.

| Plane | Reaches | Audience |
|---|---|---|
| Control (REST) | workspaces, items, connection strings | `https://api.fabric.microsoft.com/.default` |
| Data (OneLake) | Delta files, `Files/` | `https://storage.azure.com/.default` |
| SQL (TDS :1433) | Warehouse, Lakehouse SQL endpoint | no bearer token — the driver authenticates |

References: token audiences and the permission matrix · REST discovery · abfss URIs and Delta metadata · `sqlcmd`/`pyodbc` and T-SQL surface limits · an error-to-cause decision tree.

Includes `scripts/fabric_probe.py` — walks all three planes and continues past failures, so one run shows every rung's status instead of stopping at the first.

### `loading-fabric-tables`

Where you can write, four load patterns (full reload, incremental append, upsert, history+current), watermarks and idempotency, the audit-column standard, `COPY INTO`, Spark and `delta-rs` writers, partitioning mistakes, and the maintenance that keeps the SQL endpoint fast.

Includes the measured Delta → SQL type mapping and the five hard rules derived from it.

### `modeling-fabric-warehouses`

Landing-versus-curated layering, one schema per domain, load pattern encoded in the table name, the audit block, the type and key policy Fabric forces on you, and anti-patterns observed in a real production warehouse.

Includes `scripts/inventory_schemas.py` — extracts an existing warehouse's real layout to markdown, one file per schema, plus a **convention report**: casing, suffixes, view-prefix consistency, audit-column coverage, the de-facto join keys, type distribution, and suspicious `Test*`/`_bkp`/non-ASCII names.

Run it before adding anything. Matching a warehouse's existing conventions beats importing correct ones.

## Requirements

Nothing is required to *read* the skills. The bundled scripts need:

```bash
pip install httpx                # REST and OneLake listing
pip install deltalake pyarrow    # Delta read/write
```

For SQL access, prefer [go-sqlcmd](https://github.com/microsoft/go-sqlcmd) — single binary, no ODBC driver, no admin rights, and it accepts a service-principal secret directly:

```bash
sqlcmd -S "$SERVER" -d "$DATABASE" \
  --authentication-method=ActiveDirectoryServicePrincipal \
  -U "$CLIENT_ID@$TENANT_ID" -Q "SELECT TOP 5 * FROM [schema].[table]"
```

`pyodbc` also works but needs the `msodbcsql18` system driver.

Configuration comes from environment variables or a `.env` — see [`.env.example`](.env.example). Never commit real values.

## Design notes

- **Progressive disclosure.** Each `SKILL.md` is navigation plus hard rules; depth lives in `reference/` files loaded only when needed. No `SKILL.md` exceeds 500 lines.
- **Rules, not suggestions.** Each skill opens with numbered hard rules whose violation causes silent data loss or silently wrong results.
- **Measured, not assumed.** Where a claim came from experiment it says **measured** and gives the number.
- **Portable.** No tenant, workspace, or organisation identifiers anywhere. All configuration is read from the environment.
- **Read-only by default.** Bundled probes perform only token requests, GETs and SELECTs. Write paths are documented but never executed by the scripts.

## Contributing

Fabric changes between releases, and behaviour differs between a Warehouse and a Lakehouse endpoint. If a measured claim no longer holds, please open an issue with:

1. what you ran,
2. what you expected from this repo,
3. what actually happened, and the Fabric item type involved.

Re-measure rather than trusting the table — `skills/loading-fabric-tables/reference/delta-to-sql-types.md` explains how, using a scratch schema you own.

## Keywords

Microsoft Fabric · OneLake · Delta Lake · Lakehouse · Data Warehouse · SQL analytics endpoint · Fabric REST API · service principal · Microsoft Entra ID · abfss · delta-rs · go-sqlcmd · pyodbc · T-SQL · medallion architecture · incremental load · upsert · MERGE · OPTIMIZE · VACUUM · V-Order · data modeling · naming conventions · ETL · agent skills · Claude Code · Cursor · GitHub Copilot · Codex · MCP alternative

## License

[MIT](LICENSE). Not affiliated with or endorsed by Microsoft.
