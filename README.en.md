# Engram

> The local hippocampus of the trilogy: shared operational memory that remains explainable,
> bounded, and consolidated into Datacron after human review.

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)
[![Python 3.13+](https://img.shields.io/badge/Python-3.13%2B-3776AB.svg)](pyproject.toml)
[![MCP Streamable HTTP](https://img.shields.io/badge/MCP-Streamable%20HTTP-5A45FF.svg)](server.json)
[![CI](https://github.com/VBlackJack/Engram/actions/workflows/ci.yml/badge.svg)](https://github.com/VBlackJack/Engram/actions/workflows/ci.yml)

[Francais](README.md) | English

Engram is a local-first MCP server that captures working memories and returns compact capsules
ranked by trust. In the trilogy, **Datacron** is the durable Markdown notebook and source of truth,
**Cortex** is the librarian for broad documentation, and **Engram** is the hippocampus: it keeps
operational memory across clients and proposes reviewed consolidation into Datacron.

## What is in place

| Capability | Status |
| --- | --- |
| Storage | SQLite WAL, migrations, TTL, idempotency, supersession |
| Writes | One Engram process is the single writer |
| Audit | Content-free append-only log |
| MCP | Streamable HTTP, strict `remember` and `recall` tools |
| Retrieval | FTS5/BM25 by default, optional local hybrid mode behind a flag |
| Trust | Server-side provenance, confidence cap, anti-poisoning quarantine |
| Recall | Bounded capsule: current, next_action, relevant, conflicts, own_pending, sources |
| Consolidation | Human plan, verified Datacron create/link, reread, freshness check |
| Evaluation | Seeded corpus and deterministic graders, without Datacron vault access |

## Installation

Requirements:

- Python 3.13 or newer;
- `uv` 0.11.3 or newer recommended;
- **SQLite 3.51.3 or newer in the Python runtime**.

The SQLite floor is mandatory. Versions 3.7.0 through 3.51.2 are affected by SQLite's documented
WAL-reset bug. Engram checks `sqlite3.sqlite_version` when opening a database and rejects an older
runtime. See [Windows installation](docs/en/installation-windows.md) for the official SQLite 3.53.x
DLL method. SQLite documents the [WAL-reset bug](https://sqlite.org/wal.html#walreset) and publishes
the [3.53.3 binaries](https://www.sqlite.org/download.html).

```powershell
git clone https://github.com/VBlackJack/Engram.git
cd Engram
uv sync --extra dev --python 3.14.3
uv run --python 3.14.3 python -c "import sqlite3; print(sqlite3.sqlite_version)"
Copy-Item engram.example.toml engram.toml
```

The PyPI package is not published in this release. Install from source or from the wheel/sdist
attached to the GitHub release.

## Quick start

```powershell
uv run --python 3.14.3 engram serve
```

The default MCP endpoint is `http://127.0.0.1:8377/mcp`. Keep this loopback address: the server
does not implement network authentication.

Add the server to Claude Code, Codex, or Gemini, then install the
[client protocol](docs/en/client-protocol.md). Exact configuration blocks are in the
[setup guide](docs/en/setup.md).

## Configuration

Engram loads `engram.toml`. `ENGRAM_CONFIG` can select another file. Every TOML key can be
overridden as `ENGRAM_<SECTION>_<KEY>`; relative paths resolve from the TOML file directory.

| TOML section | Main variables | Purpose |
| --- | --- | --- |
| `[database]` | `ENGRAM_DATABASE_PATH`, `ENGRAM_DATABASE_BUSY_TIMEOUT_MS` | Database and SQLite wait |
| `[ttl_days]` | `ENGRAM_TTL_DAYS_PREFERENCE`, `_DECISION`, `_FACT`, `_PROJECT_STATE`, `_EPISODE` | Lifetime by kind; `0` disables expiry |
| `[limits]` | `ENGRAM_LIMITS_MAX_STATEMENT_CHARS`, `ENGRAM_LIMITS_MAX_SUBJECT_KEYS` | Input bounds |
| `[logging]` | `ENGRAM_LOGGING_PATH`, `_FILE_LEVEL`, `_CONSOLE_LEVEL` | Log file and levels |
| `[attestation]` | `ENGRAM_ATTESTATION_DEFAULT_ACTOR` | Default actor for trusted local mutations |
| `[server]` | `ENGRAM_SERVER_HOST`, `_PORT`, `_PATH`, `_WRITE_WAIT_TIMEOUT_MS`, `_TTL_SWEEP_INTERVAL_SECONDS` | HTTP endpoint, backpressure, and logical-expiry sweep |
| `[capsule]` | `ENGRAM_CAPSULE_DEFAULT_TOKEN_BUDGET`, `_MIN_TOKEN_BUDGET`, `_MAX_TOKEN_BUDGET` | Recall budget |
| `[retrieval]` | `ENGRAM_RETRIEVAL_MODE`, `_EMBEDDINGS_ENDPOINT`, `_EMBEDDINGS_MODEL`, `_EMBEDDINGS_TIMEOUT_MS`, `_RRF_K` | FTS or local hybrid |
| `[datacron]` | `ENGRAM_DATACRON_COMMAND`, `_ARGS`, `_VAULT_ROOT`, `_READ_PATHS`, `_WRITE_PATHS`, `_NEW_NOTE_DIRECTORY`, `_NEIGHBOR_LIMIT`, `_STARTUP_TIMEOUT_MS`, `_REQUEST_TIMEOUT_MS`, `_SHUTDOWN_TIMEOUT_MS` | Gateway, timeouts, and Datacron confinement |

For list variables, `ARGS` uses shell parsing and `READ_PATHS`/`WRITE_PATHS` use the OS path
separator. Safe defaults are in [`engram.example.toml`](engram.example.toml). Datacron writes stay
disabled while `write_paths` is empty, even if the parent process defines
`DATACRON_WRITE_PATHS`. The default local transport launches `datacron mcp serve`.

## MCP tools

| Tool | Essential inputs | Result and policy |
| --- | --- | --- |
| `remember` | `statement`, `kind`, `scope`, `subject_keys`, `observed_at`, `evidence` | Returns `created`, `retry`, `corroborated`, `existing_trusted`, or `renewed`; new/renewed content remains quarantined |
| `recall` | `query`, `scope`, `kinds`, `include_conflicts`, `token_budget` | Returns a trust-aware capsule; only the current client's candidates appear in `own_pending` |

Accepted kinds: `preference`, `decision`, `project_state`, `fact`, `episode`. The server assigns
provenance; a client can never declare itself a `human` source.

## Security and privacy

- All data, lexical indexes, audit records, and logs remain local.
- No cloud LLM call or telemetry is implemented.
- Client candidates are quarantined so an unattested assertion cannot become shared truth.
- Hybrid mode contacts only the explicitly configured embeddings endpoint; FTS is the default.
- Datacron writes use allowlists under `_memory/`, one deterministic canonical path, and an exact
  reread.
- Do not bind to `0.0.0.0` without an authentication proxy and network controls.

See the complete [security model](docs/en/security.md).

## CLI commands

```text
engram --version
engram --debug serve
engram serve
engram migrate
engram reindex
engram list --status quarantined
engram list --unclassified
engram classify ENTRY_ID --claim-key "topic/claim"
engram attest "Reviewed statement" fact user --subject-key "topic/key" --claim-key "topic/claim"
engram supersede --old OLD_ID --new NEW_ID
engram eval --mode both --out local/eval
engram consolidate --plan --out local/consolidation/plan.json
engram consolidate --apply local/consolidation/plan.json
engram consolidate --check-freshness
```

`consolidate --plan` remains read-only for Datacron, but anchors the immutable propositions in the
Engram database. Edit only each JSON `decision` (`approve` or `reject`) before `--apply`. The plan is
single-use: changing any other field or replaying it after apply is refused and requires a new plan.
A diverged Datacron hash yields `stale`, preserves the report, and returns exit code 6; it is never
forced. Currently, an `update` result remains visible with its target and diff in the report, but
always produces `skip`: Engram does not patch a section until Datacron supplies an independently
verified durable identity anchor.
Every create targets one canonical path containing the candidate ID. After an ambiguous write
response, a new plan reconciles only identical full canonical content at that path instead of
creating a duplicate.
Stop the daemon before `migrate`, `classify`, `attest`, `supersede`, `reindex`, or `consolidate`,
then restart it before recall. These commands acquire the same OS lock as the daemon and fail
clearly while it is active; `list` remains available through a read-only SQLite connection. For an
existing database, first take a SQLite-consistent backup, run `engram migrate`, then inventory
`engram list --unclassified`. Review every historical preference, decision, or fact and assign its
family explicitly with `engram classify ENTRY_ID --claim-key "topic/claim"`; never infer these keys
in bulk. Trusted commands use `[attestation].default_actor` unless `--actor` is provided.

Known CLI failures never print a traceback by default. Exit code `2` means usage or configuration,
`3` means a local resource is unavailable (port, process lock, database, or SQLite runtime), `4`
means Datacron could not be reached, `5` means transient store contention, and `6` means an apply
report contains failed or stale propositions. Use global `--debug` before the command, or
`ENGRAM_DEBUG=1`, only when a traceback is needed.

## Current limitations

- Engram cannot passively observe conversations: each client must call `recall` and `remember`
  according to the documented protocol.
- The transport is local HTTP. Claude Desktop remote connectors require a public HTTPS URL;
  Claude Code connects directly to localhost.
- Hybrid mode is experimental and depends on a local OpenAI-compatible endpoint. It explicitly
  falls back to FTS when unavailable.
- PyPI publication and MCP Registry submission are deferred. The manifest describes the package
  and its local HTTP endpoint.
- Porter stemming and prefix search will only be evaluated if real use exposes morphological
  misses.

## Documentation

| Start | Understand | Operate safely |
| --- | --- | --- |
| [Installation](docs/en/setup.md) | [Data contract](docs/en/spec.md) | [Security](docs/en/security.md) |
| [Windows and SQLite](docs/en/installation-windows.md) | [Architecture](docs/en/architecture.md) | [FAQ](docs/en/faq.md) |
| [User guide](docs/en/user-guide.md) | [Client protocol](docs/en/client-protocol.md) | [Documentation hub](docs/en/index.md) |

## Development

```powershell
uv sync --extra dev --python 3.14.3
uv run --python 3.14.3 ruff check .
uv run --python 3.14.3 ruff format --check .
uv run --python 3.14.3 mypy
uv run --python 3.14.3 pytest
uv build --python 3.14.3
```

## License

Apache License 2.0. Copyright 2026 Julien Bombled. See [LICENSE](LICENSE) and the
[third-party notices](THIRD_PARTY_NOTICES.md).
