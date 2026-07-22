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

Stop `engram serve` before running a trusted mutation so only one process writes the database.
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
writes.

### 1. Generate the plan

```powershell
uv run --python 3.14.3 engram consolidate --plan --out local/consolidation/plan.json
```

This command is read-only for Datacron and Engram. Read the companion Markdown file, then edit the
JSON. Every proposition is `pending` by default.

### 2. Review manually

For each proposition:

- choose `decision: "approve"` or `"reject"`;
- verify `classification`, `proposed_action`, `rel_path`, `heading`, and `new_content`;
- preserve `expected_hash`: it carries the CAS protection.

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
