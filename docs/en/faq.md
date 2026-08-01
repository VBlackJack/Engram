# FAQ and troubleshooting

[Francais](../fr/faq.md) | [English](faq.md)

Quick access:

- [The client cannot connect](#the-client-cannot-connect)
- [The candidate stays in own_pending](#the-candidate-is-in-own_pending-not-current)
- [Consolidation reports stale](#consolidation-reports-stale)
- [Cortex cannot see a recent note](#cortex-cannot-see-a-recent-datacron-note)

## `Configuration file does not exist`

Copy `engram.example.toml` to `engram.toml`, or set `ENGRAM_CONFIG` to an absolute path. Relative
TOML paths resolve from the selected file's directory.

## `SQLite 3.51.3 or newer is required`

The active Python's `sqlite3` is too old, even if a newer `sqlite3.exe` is on PATH. Verify with:

```powershell
uv run --python 3.14.6 python -c "import sys, sqlite3; print(sys.executable); print(sqlite3.sqlite_version)"
```

Use the `uv`-managed Python or follow [installation-windows.md](installation-windows.md).

## The client cannot connect

Verify Engram is running, the URL ends in `/mcp`, and no other process owns the port. A browser is
not an MCP test. Claude Desktop's remote connector cannot reach localhost; use Claude Code or an
authenticated HTTPS proxy.

## My candidate is missing from `own_pending`

`own_pending` is isolated by MCP identity `clientInfo.name/clientInfo.version`. A new client version
or a different client is a different writer. Also verify `scope`, `kinds`, query, TTL, and budget.

## The candidate is in `own_pending`, not `current`

This is the normal security policy for that entry: anything visible in `own_pending` is an
unconfirmed quarantined candidate. It needs explicit attestation before becoming active and shared.
Canonically identical content that is already active and trusted would return
`existing_trusted` without creating this candidate.

## `server busy, retry`

A write is already running or multiple instances use the same database. Ensure one Engram process
is the writer, then retry with backoff. Increase `write_wait_timeout_ms` only after diagnosis.

## The hybrid endpoint is unreachable

Engram logs the degradation and uses FTS. Verify `embeddings_endpoint`, the exact
`embeddings_model`, timeout, and server availability. Return to `mode = "fts"` to operate without
embeddings.

## FTS misses a morphological variant

Try words from the statement or `subject_keys`. Engram applies controlled prefixes after exact
phrase, all-term, and any-term stages, but it is not a stemmer or fuzzy spell checker. Use hybrid
mode for paraphrases with no shared vocabulary.

## Consolidation reports `stale`

The Datacron note changed after planning. Do not force it or replace the hash. Generate `--plan`
again, review and approve the new proposition, then run `--apply`.

## Consolidation rejects a path

The path is outside `read_paths`/`write_paths`, the write allowlist is empty, or the new-note
directory is not under `_memory/`. Correct `engram.toml`; do not bypass validation.

## A promotion disappears from `current`

`--check-freshness` may have found a diverged Datacron hash and marked the entry stale. Inspect the
JSON/Markdown report under `local/consolidation`, then perform a fresh review.

## The capsule omits results

Read `notes.why_returned`. If it reports budget omissions, request a larger `token_budget` within
the `[capsule]` bounds, or narrow `scope`, `kinds`, and `query`.

## Cortex cannot see a recent Datacron note

Cortex has no watcher and Datacron does not call it. Run:

```powershell
cortex sync
```

Then check `cortex_freshness` from the MCP client. The Datacron note remains canonical while the
Cortex index lags. See the [trilogy guide](datacron-cortex.md).

## The CLI returns code `4`

An external dependency is unavailable: Datacron during consolidation, or the embedding endpoint
during a hybrid operation. Use the global `--debug` flag only after checking the relevant
dependency's configuration and availability.
