# Operator guide

[Francais](../fr/operator-guide.md) | [English](operator-guide.md)

> **Goal:** administer Engram without mixing these actions into daily use.<br>
> **Audience:** the person responsible for the database, trust, or Datacron.<br>
> **Risk:** medium to high; backup and daemon shutdown are mandatory where stated.<br>
> **Version:** Engram `2026.0730.02`.

Only need to recall or propose a memory? Return to the [user guide](user-guide.md).

## Choose a procedure

| I want to... | Procedure | Daemon |
| --- | --- | --- |
| View candidates | `uv run --python 3.14.6 engram list --status quarantined` | May stay active |
| Trust a reviewed candidate | [Attest](#attest-a-candidate) | Stopped |
| Upgrade a database | [Migrate](#migrate-an-existing-database) | Stopped |
| Rebuild Engram indexes | [Reindex](#reindex-engram) | Stopped |
| Measure retrieval | [Evaluate](#evaluate-retrieval) | May stay active |
| Promote into Datacron | [Consolidate](#consolidate-into-datacron) | Stopped |

`migrate`, `classify`, `attest`, `supersede`, `reindex`, and every `consolidate` mode acquire the
same writer lock as the daemon. They refuse to start while it is running.

## Before any mutation

### 1. Stop the daemon

Cleanly interrupt the terminal running `uv run --python 3.14.6 engram serve`.

**You should see:** the process exits. If a command still reports an owner PID, do not delete the
lock file; identify that process first.

### 2. Take a consistent SQLite backup

Replace the first path with the effective `[database].path`, resolved from the `engram.toml`
directory. `ENGRAM_CONFIG` may select a different file. The command rejects a missing source and an
existing destination:

```powershell
$engramDbPath = (Resolve-Path "G:/ABSOLUTE/PATH/engram.db").Path
$engramBackupDir = Join-Path (Split-Path -Parent $engramDbPath) "backups"
New-Item -ItemType Directory -Force -Path $engramBackupDir | Out-Null
$engramBackupPath = Join-Path $engramBackupDir ("engram-" + (Get-Date -Format "yyyyMMdd-HHmmss") + ".db")
if (Test-Path -LiteralPath $engramBackupPath) { throw "Backup destination already exists" }
$env:ENGRAM_BACKUP_SOURCE = $engramDbPath
$env:ENGRAM_BACKUP_DESTINATION = $engramBackupPath
uv run --python 3.14.6 python -c "from os import environ; from pathlib import Path; import sqlite3; source=Path(environ['ENGRAM_BACKUP_SOURCE']); destination=Path(environ['ENGRAM_BACKUP_DESTINATION']); assert source.is_file(), f'source missing: {source}'; assert not destination.exists(), f'destination exists: {destination}'; source_db=sqlite3.connect(source.resolve().as_uri() + '?mode=ro', uri=True); assert source_db.execute('PRAGMA quick_check').fetchone()[0] == 'ok'; backup_db=sqlite3.connect(destination); source_db.backup(backup_db); assert backup_db.execute('PRAGMA quick_check').fetchone()[0] == 'ok'; backup_db.close(); source_db.close(); print(destination)"
Remove-Item Env:ENGRAM_BACKUP_SOURCE
Remove-Item Env:ENGRAM_BACKUP_DESTINATION
```

**You should see:** a timestamped backup path and no `quick_check` failure. Keep a copy outside the
working folder for a critical operation.

## Attest a candidate

### 1. Inventory

```powershell
uv run --python 3.14.6 engram list --status quarantined
```

Review the statement, type (`kind`), scope, subjects, and evidence. Do not automatically copy a
batch into the trusted area.

### 2. Attest the exact content

```powershell
uv run --python 3.14.6 engram attest "The service listens on port 8377." fact user `
  --subject-key "engram/server-port" `
  --claim-key "engram/server-port" `
  --evidence "review=change-42"
```

**You should see:** an `active` / `approved` JSON result. When kind, scope, and canonical content
match the candidate, Engram promotes its existing identifier.

To correct an entry, pass the replaced identifier:

```powershell
uv run --python 3.14.6 engram attest "The service listens on port 9000." fact user `
  --subject-key "engram/server-port" `
  --claim-key "engram/server-port" `
  --supersedes 01AAAAAAAAAAAAAAAAAAAAAAAA
```

To link two existing entries:

```powershell
uv run --python 3.14.6 engram supersede --old OLD_ID --new NEW_ID
```

### 3. Restart

```powershell
uv run --python 3.14.6 engram serve
```

Recall the subject and verify it appears in `current`. Human attestation does not remove the need
to review a conflict returned by Engram.

## Migrate an existing database

### 1. Inventory R2 identities

Before replacing the `2026.0730.01` environment, preserve it and run:

```powershell
uv run --python 3.14.6 engram list --status quarantined
```

Review or export candidates whose MCP client identity was missing/empty, contained `%`, `/`, outer
whitespace, more than 128 characters, controls or bidi, line separators, or Unicode surrogates. R3
preserves ordinary `name/version` owners, uses a SHA-256 domain-separated `mcp-v2:` namespace for
reserved separators, and rejects invalid components.

Preflight cannot distinguish every generic Store API owner from an MCP owner. Do not guess their
identity: review them manually or leave their previous TTL policy in effect.

### 2. Update the R3 configuration

```toml
[capsule]
default_token_budget = 4800
min_token_budget = 1200
max_token_budget = 6000
```

### 3. Prove the migration without touching the source

After backup and daemon shutdown:

```powershell
uv run --python 3.14.6 engram preflight
```

`preflight` opens the source read-only, pins one snapshot, and tests the full migration on a
temporary copy. Schemas 3 through 5 are supported. Engram never truncates an old value that exceeds
a new limit. Keep at least the database size plus working headroom free on the temporary volume.

**Continue only when:** the report declares compatibility. When it names a row, review or export
that row with the version named by the diagnostic.

Also interpret the derived indexes:

- `fts_rebuild_required: true`: the FTS schema must be recreated;
- `fts_rebuild_required: null`: the schema matches; external-content rows are still validated at
  startup;
- `vector_rebuild_required: true`: old vectors must be rebuilt after migration when hybrid mode is
  enabled.

### 4. Migrate and classify

```powershell
uv run --python 3.14.6 engram migrate
uv run --python 3.14.6 engram list --unclassified
```

For each historical `preference`, `decision`, or `fact`, choose a semantic family manually:

```powershell
uv run --python 3.14.6 engram classify ENTRY_ID --claim-key "engram/server-port"
uv run --python 3.14.6 engram list --unclassified
```

Do not derive `claim_key` in bulk from `subject_keys`: subjects aid search, while the claim key
defines conflict identity.

### 5. Rebuild when requested

When `vector_rebuild_required` is `true` and hybrid mode is enabled:

```powershell
uv run --python 3.14.6 engram reindex
```

Then restart the daemon and test one known recall.

## Reindex Engram

Stop the daemon, then run:

```powershell
uv run --python 3.14.6 engram reindex
```

- In `fts` mode, Engram rebuilds FTS only.
- In `hybrid` mode, it also recreates vectors with the configured endpoint.
- `entries` and `audit_log` are unchanged.
- The live index is replaced only after a successful rebuild.

Restart `uv run --python 3.14.6 engram serve`, then recall a known query.

## Evaluate retrieval

```powershell
uv run --python 3.14.6 engram eval --mode fts --out local/eval
uv run --python 3.14.6 engram eval --mode both --out local/eval
```

**You should see:** `metrics.json` and `rapport-eval.md` under `local/eval`. The seeded corpus and
graders are deterministic and do not access the Datacron vault.

## Consolidate into Datacron

Before starting:

- configure the gateway as shown in
  [Set up a shared vault](datacron-cortex.md#set-up-a-shared-vault);
- stop the daemon;
- never target the durable vault from an automated test.

### 1. Generate a plan

```powershell
uv run --python 3.14.6 engram consolidate --plan --out local/consolidation/plan.json
```

**You should see:** a JSON file and Markdown report. This step reads Datacron but does not write to
it. Engram also anchors an immutable plan snapshot in SQLite.

### 2. Review

For every proposition, verify:

- classification and action;
- `rel_path`, heading, and heading level;
- new content, expected hash, and neighbors;
- the diff when present.

Change only `decision` to `"approve"` or `"reject"`. Any remaining `"pending"` proposition blocks
apply. Do not change the target, generated content, or hashes.

### 3. Apply once

```powershell
uv run --python 3.14.6 engram consolidate --apply local/consolidation/plan.json
```

The plan is consumed before writes and cannot be replayed. A result can be `applied`, `skipped`,
`stale`, or `failed`.

- `stale`: Datacron changed; generate and review a new plan.
- `failed`: preserve the report and repair the dependency.
- exit code `6`: at least one proposition is `stale` or `failed`.
- an `update` proposition is currently review-only and yields `skip`.

### 4. Check freshness

```powershell
uv run --python 3.14.6 engram consolidate --check-freshness
```

A divergence removes the promotion from current recall. Engram does not rewrite Datacron to hide
the problem.

### 5. Restart and synchronize Cortex

```powershell
uv run --python 3.14.6 engram serve
```

In another terminal, when Cortex indexes this vault:

```powershell
cortex sync
```

Datacron consolidation never synchronizes Cortex automatically.

## Exit codes

| Code | Meaning | First action |
| --- | --- | --- |
| `2` | Invalid usage or configuration | Correct the command or `engram.toml` |
| `3` | Unavailable local resource | Check port, lock, database, and SQLite runtime |
| `4` | Unavailable external dependency | Check Datacron or the embedding endpoint |
| `5` | Transient store contention | Wait for the active write and retry |
| `6` | Apply with `failed` or `stale` result | Read the report and generate a new plan |
| `130` | Operator interruption | Check state, then resume explicitly |

Known failures do not display tracebacks. For a focused diagnostic:

```powershell
uv run --python 3.14.6 engram --debug COMMAND
```

or set `ENGRAM_DEBUG=1`.

## Incident recovery

1. Do not launch multiple writers to "unlock" the database.
2. Preserve the database, possible WAL/SHM files, logs, and command report.
3. Work on a copy and run `uv run --python 3.14.6 engram preflight`.
4. Restore a backup only after stopping every Engram process and identifying the changes that
   would be lost.
5. Use the [FAQ](faq.md) for the exact symptom.
