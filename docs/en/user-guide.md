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
- `sources` lists cited identifiers; `notes` explains selection and budget omissions.

Treat `own_pending` as unconfirmed. A candidate must not guide irreversible action.

## Attest trusted memory

Set the default audit identity in `engram.toml`:

```toml
[attestation]
default_actor = "local-operator"
```

Stop `engram serve` before running a trusted mutation so only one process writes the database. The
CLI enforces this boundary: `attest`, `supersede`, `reindex`, and every `consolidate` mode fail with
the daemon PID and corrective action while it is active. `list` remains available read-only.
Inspect pending candidates, then attest reviewed content:

```powershell
engram list --status quarantined
engram attest "The service listens on port 8377." fact user `
  --subject-key "engram/server-port" `
  --evidence "review=change-42"
```

If the canonical kind, scope, and statement match a candidate, attestation promotes that same
entry ID to `active`/`approved`. For a corrected statement, pass the replaced ID explicitly:

```powershell
engram attest "The service listens on port 9000." fact user `
  --subject-key "engram/server-port" `
  --supersedes 01AAAAAAAAAAAAAAAAAAAAAAAA
```

To link two entries that already exist, use `engram supersede --old OLD_ID --new NEW_ID`. Trusted
commands emit machine-readable JSON. Restart the daemon before recalling the newly active memory.
Use `engram attest --help` for provenance, confidence, validity, observation, evidence, and actor
options.

## Diagnose CLI failures

Expected local failures use stable process exit codes and one actionable stderr message:

| Code | Meaning | Corrective action |
| --- | --- | --- |
| `2` | Invalid usage or configuration | Fix the command or `engram.toml` value |
| `3` | Local resource unavailable | Free the port/lock, repair the database, or upgrade SQLite |
| `4` | Datacron unavailable | Install Datacron or fix its command and arguments |
| `5` | Transient store contention | Retry after the current write finishes |
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
recreate vectors. The `entries` table and audit log are unchanged.

## Evaluate retrieval

```powershell
uv run --python 3.14.3 engram eval --mode fts --out local/eval
uv run --python 3.14.3 engram eval --mode both --out local/eval
```

The seeded corpus loads 72 entries and grades 64 queries with deterministic graders. `both` also
measures hybrid retrieval when the endpoint responds. `metrics.json` and `rapport-eval.md` stay
under `local/` and are not published.

## Consolidate into Datacron

Configure `[datacron]` with a vault plus explicit read and write paths. An empty allowlist forbids
writes, including when the parent environment already defines `DATACRON_WRITE_PATHS`. The default
CLI transport is `command = "datacron"` with `args = ["mcp", "serve"]`.

The automated end-to-end gate must use an initialized disposable vault and a test-only Engram
configuration. It must never target the durable vault. Running `consolidate --plan` against a real
vault remains an explicit manual operator action; no smoke command includes that step.

### 1. Generate the plan

```powershell
uv run --python 3.14.3 engram consolidate --plan --out local/consolidation/plan.json
```

This command is read-only for Datacron and Engram. Read the companion Markdown file, then edit the
JSON. Every proposition is `pending` by default.

### 2. Review manually

For each proposition:

- choose `decision: "approve"` or `"reject"`;
- verify `classification`, `proposed_action`, `rel_path`, `heading`, `heading_level`, and
  `new_content`;
- preserve `expected_hash`: it carries the CAS protection.

You may select another patch target only when its path, heading, heading level, and hash already
appear together in `neighbors`. Apply regenerates those neighbors and rejects any other retargeting.

### 3. Apply

```powershell
uv run --python 3.14.3 engram consolidate --apply local/consolidation/plan.json
```

An outcome may be `applied`, `skipped`, `stale`, or `failed`. Do not edit a hash to bypass
`stale`; regenerate the plan from the current note.

### 4. Check freshness

```powershell
uv run --python 3.14.3 engram consolidate --check-freshness
```

The check compares promoted hashes. It marks divergences stale in Engram without rewriting
Datacron. A stale promotion is removed from current recall until reviewed again.
