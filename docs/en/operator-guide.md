# Operator guide

[Français](../fr/operator-guide.md) | [English](operator-guide.md)

> **Goal:** administer Engram without mixing these actions into daily use.<br>
> **Audience:** the person responsible for the database, trust, or Datacron.<br>
> **Risk:** medium to high; backup and daemon shutdown are mandatory where stated.<br>
> **Version:** Engram `2026.0730.02`, reviewed 2026-08-13.

Only need to recall or propose a memory? Return to the [user guide](user-guide.md).

Before any procedure on this page, `uv run --python 3.14.6 engram doctor` tells you which
configuration file is in effect, which database it resolves to, its schema version, and what
currently owns it. Every step below assumes those are the ones you meant.

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

<a id="stop-the-daemon"></a>

One command works for every installation, whatever started the daemon:

```text
uv run --python 3.14.6 engram stop
```

**You should see:** JSON with `"stopped": true`, and both `engram.db-wal` and `engram.db-shm`
gone. `engram stop` asks the daemon that owns the configured database to close it and exit, then
waits on the ownership lock and reports whether it actually stopped. When nothing holds the
database it reports `"requested": false, "stopped": true`.

This matters most for the installations that have no terminal to interrupt:

| How Engram was started | How to stop it |
| --- | --- |
| Windows logon task (`engram setup autostart --install`) | `engram stop` — there is no console to send `Ctrl+C` to |
| systemd user unit | `engram stop`, or `systemctl --user stop engram.service`, which runs it |
| launchd LaunchAgent | `engram stop` |
| A terminal running `engram serve` | `engram stop` from another terminal, or `Ctrl+C` in that one |

**If `engram stop` fails:** it names the pid still holding the database and leaves the request in
place. Do not delete the lock file and do not kill the process before reading the log; terminating
a daemon mid-write is what leaves a write-ahead log behind. `uv run --python 3.14.6 engram doctor`
reports the owner and whether it is a daemon or an offline writer.

### 2. Take a consistent SQLite backup

Replace the path with the effective `[database].path`, which `engram doctor` prints. `ENGRAM_CONFIG`
may select a different file. Both variants reject a missing source and an existing destination.

#### Windows (PowerShell)

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

#### macOS / Linux

```bash
ENGRAM_BACKUP_SOURCE="$(cd "$(dirname /absolute/path/engram.db)" && pwd)/$(basename /absolute/path/engram.db)"
ENGRAM_BACKUP_DIR="$(dirname "$ENGRAM_BACKUP_SOURCE")/backups"
mkdir -p "$ENGRAM_BACKUP_DIR"
ENGRAM_BACKUP_DESTINATION="$ENGRAM_BACKUP_DIR/engram-$(date +%Y%m%d-%H%M%S).db"
export ENGRAM_BACKUP_SOURCE ENGRAM_BACKUP_DESTINATION
uv run --python 3.14.6 python -c "from os import environ; from pathlib import Path; import sqlite3; source=Path(environ['ENGRAM_BACKUP_SOURCE']); destination=Path(environ['ENGRAM_BACKUP_DESTINATION']); assert source.is_file(), f'source missing: {source}'; assert not destination.exists(), f'destination exists: {destination}'; source_db=sqlite3.connect(source.resolve().as_uri() + '?mode=ro', uri=True); assert source_db.execute('PRAGMA quick_check').fetchone()[0] == 'ok'; backup_db=sqlite3.connect(destination); source_db.backup(backup_db); assert backup_db.execute('PRAGMA quick_check').fetchone()[0] == 'ok'; backup_db.close(); source_db.close(); print(destination)"
unset ENGRAM_BACKUP_SOURCE ENGRAM_BACKUP_DESTINATION
```

**You should see:** a timestamped backup path and no `quick_check` failure. Keep a copy outside the
working folder for a critical operation.

### 3. Know how you will restart

Every procedure below ends with a restart. Use the one that matches how Engram was installed:

| Installation | Restart |
| --- | --- |
| Windows logon task | `uv run --python 3.14.6 engram setup autostart --status` shows it is registered; start it with `uv run --python 3.14.6 engram setup autostart --install`, which converges and starts the daemon when the database is free |
| systemd user unit | `systemctl --user start engram.service` |
| launchd LaunchAgent | `launchctl kickstart -k gui/$(id -u)/com.github.vblackjack.engram` |
| Foreground | `uv run --python 3.14.6 engram serve` |

Unit files and the full command tables are in
[Install as a service on macOS and Linux](installation-unix.md).

Confirm with `uv run --python 3.14.6 engram doctor`: `daemon` must report `serving` and `endpoint`
must report that the URL accepts connections.

## Attest a candidate

### 1. Inventory

```text
uv run --python 3.14.6 engram list --status quarantined
```

Review the statement, type (`kind`), scope, subjects, and evidence. Do not automatically copy a
batch into the trusted area.

### 2. Attest the exact content

`attest` matches on canonical content, not on an identifier. Retyping a statement with a different
wording creates a **new** entry instead of promoting the one you meant, and still exits `0`. Copy
the statement from the inventory rather than retyping it.

#### Windows (PowerShell)

```powershell
uv run --python 3.14.6 engram attest "The service listens on port 8377." fact user `
  --subject-key "engram/server-port" `
  --claim-key "engram/server-port" `
  --evidence "review=change-42"
```

#### macOS / Linux

```bash
uv run --python 3.14.6 engram attest "The service listens on port 8377." fact user \
  --subject-key "engram/server-port" \
  --claim-key "engram/server-port" \
  --evidence "review=change-42"
```

**You should see:** an `active` / `approved` JSON result. When kind, scope, and canonical content
match the candidate, Engram promotes its existing identifier.

`--claim-key` is **mandatory** for `preference`, `decision`, and `fact`: it is the conflict-family
identity, and an entry without one is omitted from recall fail-closed. `--subject-key` is a
discovery hint and is not a substitute.

### Options that control trust and lifetime

| Option | Accepted values | Default | What it changes |
| --- | --- | --- | --- |
| `--source-type` | `human`, `tool_verified` | `human` | Provenance recorded for the entry. `tool_verified` is for a statement a tool proved, not one a person judged. A client can never assert either through MCP; only this command can. |
| `--confidence` | `high`, `medium`, `low` | `high` | Confidence stored with the entry. Lower it deliberately for something reviewed but not certain; a trusted entry at `high` outranks the same claim at `low`. |
| `--valid-from` | `YYYY-MM-DD` | unset | First day the statement holds. Before it, the entry exists but is not current. Use it to attest a decision that takes effect later. |
| `--valid-until` | `YYYY-MM-DD` | unset | Last day the statement holds. After it, the entry stops being current. This is the honest way to record something you already know will expire. |
| `--observed-at` | ISO-8601 with a UTC offset, e.g. `2026-08-13T09:00:00+02:00` | now | When the fact was observed. Backdate it when you attest today something that was true earlier; recency tie-breaks read this, not the moment you typed the command. |

`--valid-from` and `--valid-until` are calendar days; `--observed-at` is an instant and requires an
explicit offset. `--valid-until` is a lifetime bound on one statement and is unrelated to the
`[ttl_days]` policy, which applies per kind.

### Accepted evidence types

`--evidence` takes `TYPE=REF` and is repeatable. Only four types are accepted; anything else is
refused:

| Type | Use it for |
| --- | --- |
| `tool_result` | The identifier or reference of a tool run that produced the statement |
| `datacron_note` | The Datacron note that carries the durable version |
| `human_message` | The message in which a person stated or confirmed it |
| `review` | The review, change, or ticket in which it was validated |

The reference itself is opaque to Engram: it is stored and returned, never resolved or fetched.

### Correcting an entry

To correct an entry, pass the replaced identifier:

```text
uv run --python 3.14.6 engram attest "The service listens on port 9000." fact user --subject-key "engram/server-port" --claim-key "engram/server-port" --supersedes 01AAAAAAAAAAAAAAAAAAAAAAAA
```

To link two existing entries:

```text
uv run --python 3.14.6 engram supersede --old OLD_ID --new NEW_ID
```

### 3. Restart and verify

Restart the daemon the way it was installed — see
[Know how you will restart](#3-know-how-you-will-restart) — then recall the subject.

**Where to look depends on the kind you attested.** The capsule does not put everything in
`current`:

| Kind attested | Capsule section it appears in |
| --- | --- |
| `preference` | `current` |
| `decision` | `current` |
| `fact` | `current` |
| `project_state` | `next_action` |
| `episode` | `relevant` |

A `project_state` or `episode` that never shows up in `current` is behaving correctly; checking
`current` for either one is how a successful attestation gets mistaken for a failed one. An entry
in an unresolved conflict family appears under `conflicts` instead, whatever its kind, and one
still lacking a `claim_key` is omitted entirely with an `unclassified_claim_omitted` warning.

Human attestation does not remove the need to review a conflict returned by Engram.

## Migrate an existing database

### 1. Inventory R2 identities

Before replacing the `2026.0730.01` environment, preserve it and run:

```text
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
max_token_budget = 32768
```

`max_token_budget` is the ceiling a client may ask for, not what a recall usually costs: raising it
does not make any recall larger, because `default_token_budget` still decides that. It was `6000`
until 2026.813.1, and a conflict family of six recorded versions needs more than 6000 serialized
bytes, so those families were unreachable through the MCP tools at any budget a client was allowed
to request.

### 3. Prove the migration without touching the source

After backup and daemon shutdown:

```text
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

```text
uv run --python 3.14.6 engram migrate
uv run --python 3.14.6 engram list --unclassified
```

For each historical `preference`, `decision`, or `fact`, choose a semantic family manually:

```text
uv run --python 3.14.6 engram classify ENTRY_ID --claim-key "engram/server-port"
uv run --python 3.14.6 engram list --unclassified
```

Do not derive `claim_key` in bulk from `subject_keys`: subjects aid search, while the claim key
defines conflict identity.

### 5. Rebuild when requested

When `vector_rebuild_required` is `true` and hybrid mode is enabled:

```text
uv run --python 3.14.6 engram reindex
```

Then restart the daemon the way it was installed — see
[Know how you will restart](#3-know-how-you-will-restart) — and test one known recall.

### 6. Going back to the previous build

A migration is one-way. An older Engram refuses a database whose schema is newer than the one it
knows — `Database schema version 6 is newer than supported version 5` — so returning to the
previous build is a **restore**, not a checkout. Reinstalling the old version alone leaves a daemon
that will not open the database at all.

1. Stop the daemon: `uv run --python 3.14.6 engram stop`.
2. Back up the current, migrated database, the same way as in
   [Take a consistent SQLite backup](#2-take-a-consistent-sqlite-backup). Keep it: it is the only
   copy of everything written since the migration.
3. Put the pre-migration backup back in place, and delete any `-wal` and `-shm` beside it. A
   write-ahead log belongs to the database it was written for; leaving one next to a restored file
   is how a rollback loses data it appeared to keep.
4. Install the previous version.
5. Restart, then verify with `engram doctor`, `PRAGMA integrity_check`, and the entry count you
   recorded before the migration.

**Everything written after the migration is lost by this procedure**, because the backup restored
in step 3 predates it. The recovery point is the moment the backup was taken, so take it
immediately before migrating, and treat step 2 as the record of what a rollback would discard.

## Reindex Engram

Stop the daemon with `uv run --python 3.14.6 engram stop`, then run:

```text
uv run --python 3.14.6 engram reindex
```

- In `fts` mode, Engram rebuilds FTS only.
- In `hybrid` mode, it also recreates vectors with the configured endpoint.
- `entries` and `audit_log` are unchanged.
- The live index is replaced only after a successful rebuild.

Restart the daemon the way it was installed — see
[Know how you will restart](#3-know-how-you-will-restart) — then recall a known query.

## Evaluate retrieval

```text
uv run --python 3.14.6 engram eval --mode fts --out local/eval
uv run --python 3.14.6 engram eval --mode both --out local/eval
```

**You should see:** `metrics.json` and `rapport-eval.md` under `local/eval`. The seeded corpus and
graders are deterministic and do not access the Datacron vault.

## Consolidate into Datacron

Before starting:

- configure the gateway as shown in
  [Set up a shared vault](datacron-cortex.md#set-up-a-shared-vault);
- stop the daemon with `uv run --python 3.14.6 engram stop`;
- never target the durable vault from an automated test.

### 1. Generate a plan

```text
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

```text
uv run --python 3.14.6 engram consolidate --apply local/consolidation/plan.json
```

The plan is consumed before writes and cannot be replayed. A result can be `applied`, `skipped`,
`stale`, or `failed`.

- `stale`: Datacron changed; generate and review a new plan.
- `failed`: preserve the report and repair the dependency.
- exit code `6`: at least one proposition is `stale` or `failed`.
- an `update` proposition is currently review-only and yields `skip`.

### 4. Check freshness

```text
uv run --python 3.14.6 engram consolidate --check-freshness
```

A divergence removes the promotion from current recall. Engram does not rewrite Datacron to hide
the problem.

### 5. Restart and synchronize Cortex

Restart the daemon the way it was installed — see
[Know how you will restart](#3-know-how-you-will-restart). For a foreground run:

```text
uv run --python 3.14.6 engram serve
```

In another terminal, when Cortex indexes this vault:

```text
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

```text
uv run --python 3.14.6 engram --debug COMMAND
```

or set `ENGRAM_DEBUG=1`.

## Incident recovery

1. Run `uv run --python 3.14.6 engram doctor` first. It reports the interpreter, the SQLite
   version, the configuration file actually resolved, the database and its schema, the lock owner,
   the endpoint, and the log file — and names a repair for each failure.
2. Do not launch multiple writers to "unlock" the database.
3. Preserve the database, possible WAL/SHM files, logs, and command report.
4. Work on a copy and run `uv run --python 3.14.6 engram preflight`.
5. Restore a backup only after stopping every Engram process with
   `uv run --python 3.14.6 engram stop` and identifying the changes that would be lost.
6. Use the [FAQ](faq.md) for the exact symptom.
