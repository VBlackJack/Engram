# FAQ and troubleshooting

[Francais](../fr/faq.md) | [English](faq.md)

## `Configuration file does not exist`

Copy `engram.example.toml` to `engram.toml`, or set `ENGRAM_CONFIG` to an absolute path. Relative
TOML paths resolve from the selected file's directory.

## `SQLite 3.51.3 or newer is required`

The active Python's `sqlite3` is too old, even if a newer `sqlite3.exe` is on PATH. Verify with:

```powershell
uv run --python 3.14.3 python -c "import sys, sqlite3; print(sys.executable); print(sqlite3.sqlite_version)"
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

Try words from the statement or `subject_keys`. The substring fallback is not a stemmer. Porter
and prefix search are deferred until real evidence warrants them; hybrid mode is the current
extension path.

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
