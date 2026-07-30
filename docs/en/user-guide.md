# User guide

[Francais](../fr/user-guide.md) | [English](user-guide.md)

## Daily use

Engram becomes useful when each client follows three simple moments:

1. **Start**: call `recall` with the project, task, and useful keywords.
2. **During**: call `remember` after a decision, explicit correction, confirmed preference, or
   durable state change.
3. **End**: store a `project_state` describing the exit state and next action.

Ready-to-install text is in [client-protocol.md](client-protocol.md).

### Choose the kind

| Situation | Suggested kind |
| --- | --- |
| "Always use absolute paths in this project" | `preference` |
| "SQLite was selected for these reasons" | `decision` |
| "Migration is complete; next action: deploy" | `project_state` |
| "The service listens on port 8377" | `fact` |
| "The test failed once after a timeout" | `episode` |

Use a small number of stable `subject_keys`, such as `engram:storage` or `project:release`. Do not
store secrets, transcripts, hypotheses, duplicates, or ephemeral detail.

### Read a capsule

- `current` contains stable context usable now.
- `next_action` contains relevant project states and next steps.
- `relevant` contains recent episodes.
- `conflicts` stays empty unless `include_conflicts=true`.
- `own_pending` contains only quarantined candidates written by this MCP client.
- `sources` lists cited identifiers; `notes` explains selection, budget omissions, and whether
  recall was complete. Never infer absence when `notes.recall_complete` is false.

Treat `own_pending` as unconfirmed. A candidate must not guide irreversible action.

## Attest trusted memory

Set the default audit identity in `engram.toml`:

```toml
[attestation]
default_actor = "local-operator"
```

Stop `engram serve` before running a trusted mutation so only one process writes the database. The
CLI enforces this boundary: `migrate`, `classify`, `attest`, `supersede`, `reindex`, and every
`consolidate` mode fail with the daemon PID and corrective action while it is active. `list` remains
available read-only. Inspect pending candidates, then attest reviewed content:

```powershell
engram list --status quarantined
engram attest "The service listens on port 8377." fact user `
  --subject-key "engram/server-port" `
  --claim-key "engram/server-port" `
  --evidence "review=change-42"
```

If the canonical kind, scope, and statement match a candidate, attestation promotes that same
entry ID to `active`/`approved`. For a corrected statement, pass the replaced ID explicitly:

```powershell
engram attest "The service listens on port 9000." fact user `
  --subject-key "engram/server-port" `
  --claim-key "engram/server-port" `
  --supersedes 01AAAAAAAAAAAAAAAAAAAAAAAA
```

To link two entries that already exist, use `engram supersede --old OLD_ID --new NEW_ID`. Trusted
commands emit machine-readable JSON. Restart the daemon before recalling the newly active memory.
Use `engram attest --help` for provenance, confidence, validity, observation, evidence, and actor
options.

## Migrate and classify an existing database

Stop the daemon and take a SQLite-consistent backup before migrating. During a recovery or incident,
exercise the workflow on a copy first, then run:

```powershell
engram preflight
engram migrate
engram list --unclassified
```

`preflight` requires the daemon to be stopped and holds the offline-writer OS lock. It opens the
source database read-only, pins one SQLite snapshot, copies that snapshot to a temporary on-disk
database, then executes the complete migration, derived-index reconstruction, and integrity checks
on the disposable copy. The source bytes are not changed. Ensure temporary storage can hold one
database copy. Schemas 3 through 5 are supported; an older schema requires the staged
2026.0721.04 upgrade named by the diagnostic.

Version 2026.0730.02 adds fixed ceilings for statements, subject keys, client/audit identities,
evidence, and durable references. It never truncates legacy content. If preflight reports an
incompatible row, keep the verified backup and use 2026.0730.01 to review or export that row before
retrying. In the JSON report, `vector_rebuild_required: true` means the old derived vector table
will be replaced; run `engram reindex` after migration when hybrid mode is enabled.
`fts_rebuild_required: true` means the FTS schema must be recreated; `null` means its schema matched
and startup will still validate its external content.

Inventory pending candidates before upgrading if an R2 MCP client identity was missing or empty, or
if its name/version contained `%`, `/`, surrounding whitespace, more than 128 characters,
control/bidi characters, line separators, or Unicode surrogates. R3 preserves ordinary legacy
`name/version` owners, but reserved separators use a domain-separated SHA-256 `mcp-v2:` identity
and invalid components are rejected. Use `engram list --status quarantined` with the previous
release to review or export affected candidates, and attest one only after human verification;
otherwise let its prior TTL policy apply. Preflight cannot distinguish generic Store writer names
from MCP owners.

`migrate` applies every pending schema step in one transaction and rejects malformed historical rows without
leaving a partial migration. Project states receive their reserved family; episodes do not use one.
For every preference, decision, or fact in the inventory, review the content and assign a stable
semantic family manually:

```powershell
engram classify ENTRY_ID --claim-key "engram/server-port"
engram list --unclassified
```

Repeat until the inventory is empty. Never derive `claim_key` automatically from `subject_keys`:
the latter support retrieval and do not identify a conflict. Then restart the daemon.

## Diagnose CLI failures

Expected local failures use stable process exit codes and one actionable stderr message:

| Code | Meaning | Corrective action |
| --- | --- | --- |
| `2` | Invalid usage or configuration | Fix the command or `engram.toml` value |
| `3` | Local resource unavailable | Free the port/lock, repair the database, or upgrade SQLite |
| `4` | External provider unavailable | Repair Datacron or the configured embedding endpoint |
| `5` | Transient store contention | Retry after the current write finishes |
| `6` | Apply report contains failed or stale propositions | Inspect the report and generate a new plan |
| `130` | Operator interruption reached the CLI | No recovery required |

Tracebacks are disabled for these failures. For diagnosis, place the global flag before the
command (`engram --debug serve`) or set `ENGRAM_DEBUG=1`. The SQLite runtime guard points directly
to [installation-windows.md](installation-windows.md) when the loaded version predates `3.51.3`.

## Reindex

FTS5 and vectors are derived:

```powershell
uv run --python 3.14.3 engram reindex
```

In `fts` mode, only FTS indexes are rebuilt. In `hybrid` mode, the configured model is called to
recreate vectors. The live vector index is swapped only after every bounded batch succeeds and no
other SQLite connection has committed during the rebuild. The `entries` table and audit log are
unchanged.

## Evaluate retrieval

```powershell
uv run --python 3.14.3 engram eval --mode fts --out local/eval
uv run --python 3.14.3 engram eval --mode both --out local/eval
```

The seeded corpus loads 72 entries and grades 88 queries with deterministic graders. The FTS
release contract always uses its checked-in retrieval settings and a conservative 4800-byte UTF-8
capsule cap, independently from runtime retrieval and capsule defaults. `both` also measures
hybrid retrieval when the endpoint responds. `metrics.json` and `rapport-eval.md` stay under
`local/` and are not published.

## Consolidate into Datacron

Configure `[datacron]` with a vault plus explicit read and write paths. An empty allowlist forbids
writes, including when the parent environment already defines `DATACRON_WRITE_PATHS`. The default
CLI transport is `command = "datacron"` with `args = ["mcp", "serve"]`.
`startup_timeout_ms`, `request_timeout_ms`, and `shutdown_timeout_ms` bound each subprocess
boundary. A timeout poisons the session: the plan must never be replayed. The runtime pins
`mcp==1.28.1`: its stdio context manager closes stdin and then terminates the process tree (Windows
Job Object or POSIX process group) across two bounded 2 s waits. The default 5 s shutdown timeout
covers that cleanup, and the non-daemon owner thread prevents process exit before its `finally`.

The automated end-to-end gate must use an initialized disposable vault and a test-only Engram
configuration. It must never target the durable vault. Running `consolidate --plan` against a real
vault remains an explicit manual operator action; no smoke command includes that step.

### 1. Generate the plan

```powershell
uv run --python 3.14.3 engram consolidate --plan --out local/consolidation/plan.json
```

This command is read-only for Datacron. It stores an immutable plan snapshot in Engram SQLite, then
writes the review artifacts. Read the companion Markdown file, then edit the JSON. Every proposition
is `pending` by default. The complete canonical snapshot must fit within 4 MiB of UTF-8; reduce the
batch before generating another plan when it does not.

### 2. Review manually

For each proposition:

- verify `classification`, `proposed_action`, `rel_path`, `heading`, `heading_level`, `new_content`,
  `expected_hash`, and the neighbors;
- for `update`, inspect the before/after diff; the current action is `skip` and no existing section
  is changed without an independently verified durable identity anchor;
- edit only `decision`, choosing `"approve"` or `"reject"`.

Do not retarget a proposition or edit any generated field. Engram compares every immutable field
with its SQLite snapshot and refuses a modified plan. Apply also refuses any decision still marked
`pending` without consuming the plan.

### 3. Apply

```powershell
uv run --python 3.14.3 engram consolidate --apply local/consolidation/plan.json
```

An outcome may be `applied`, `skipped`, `stale`, or `failed`. A plan is consumed before any external
write attempt and cannot be replayed. If any outcome is `stale` or `failed`, the report is preserved
and the command exits with code 6. Regenerate a plan from the current note.
A create uses one canonical path containing the candidate ID. If the write response is lost after
creation, the next plan rereads that same path and classifies only its identical full canonical
content (apart from line endings and the final newline) as `redundant`, instead of creating a
duplicate.

### 4. Check freshness

```powershell
uv run --python 3.14.3 engram consolidate --check-freshness
```

The check compares promoted hashes. It marks divergences stale in Engram without rewriting
Datacron. A stale promotion is removed from current recall until reviewed again.
