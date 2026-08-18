# Engram

> The local hippocampus of the trilogy: shared operational memory that remains explainable,
> bounded, and consolidated into Datacron after human review.

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)
[![Python 3.13+](https://img.shields.io/badge/Python-3.13%2B-3776AB.svg)](pyproject.toml)
[![MCP Streamable HTTP](https://img.shields.io/badge/MCP-Streamable%20HTTP-5A45FF.svg)](server.json)
[![CI](https://github.com/VBlackJack/Engram/actions/workflows/ci.yml/badge.svg)](https://github.com/VBlackJack/Engram/actions/workflows/ci.yml)

[Français](README.md) | English

Engram is a local-first MCP server that captures working memories and returns compact capsules
ranked by trust. In the trilogy, **Datacron** is the durable Markdown notebook and source of truth,
**Cortex** is the librarian for broad documentation, and **Engram** is the hippocampus: it keeps
operational memory across clients and proposes reviewed consolidation into Datacron.

## Choose your path

| I want to... | Guide |
| --- | --- |
| Start Engram now | [Five-minute quick start](docs/en/quick-start.md) |
| Use memory every day | [User guide](docs/en/user-guide.md) |
| Understand Engram, Datacron, and Cortex | [Trilogy guide](docs/en/datacron-cortex.md) |
| Administer, migrate, or consolidate | [Operator guide](docs/en/operator-guide.md) |
| Keep Engram running after a logoff | [Windows logon task](docs/en/setup.md#windows-the-logon-task) or [systemd / launchd](docs/en/installation-unix.md) |
| Find out why Engram is not working | `engram doctor`, then the [FAQ](docs/en/faq.md) |

The rest of this README is a release reference. You do not need to read it all before starting.
Documentation verified with Engram `2026.0730.02` on 2026-08-13.

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

- Git for source installation;
- Python 3.13 or newer;
- **`uv` 0.12.1 or newer** — required, not merely advised: it is the first release that knows the
  `3.14.6` build, and continuous integration pins `uv==0.12.1` on both the Windows and Linux legs;
- **SQLite 3.51.3 or newer in the Python runtime**.

The SQLite floor is mandatory. Versions 3.7.0 through 3.51.2 are affected by SQLite's documented
WAL-reset bug. Engram checks `sqlite3.sqlite_version` when opening a database and rejects an older
runtime; the refusal names `engram doctor` and the documentation URL, both of which report the
repair for the machine in front of you. See
[Windows installation](docs/en/installation-windows.md) for the official SQLite 3.53.x DLL method.
SQLite documents the [WAL-reset bug](https://sqlite.org/wal.html#walreset) and publishes the
[3.53.3 binaries](https://www.sqlite.org/download.html).

Identical on Windows, macOS, and Linux:

```text
git clone https://github.com/VBlackJack/Engram.git
cd Engram
uv sync --python 3.14.6
uv run --python 3.14.6 engram init
uv run --python 3.14.6 engram doctor
```

`engram init` writes `engram.toml` from the copy packaged inside the distribution, so it works from
a wheel install as well as from a checkout, and refuses to replace an existing file unless you pass
`--force`. `engram doctor` then reports the interpreter, the SQLite floor, the configuration that
was resolved, the database, the lock, the endpoint, and the log file — each with the command that
repairs it.

The PyPI package is not published. Before this GitHub release is published, use the current source
checkout; after publication, the wheel/sdist attached to the release can also be used.

## Quick start

```text
uv run --python 3.14.6 engram serve
```

The default MCP endpoint is `http://127.0.0.1:8377/mcp`. Keep this loopback address: the server
does not implement network authentication.

`engram serve` lasts exactly as long as its terminal. To keep Engram after a logoff:

| Platform | Command |
| --- | --- |
| Windows | `uv run --python 3.14.6 engram setup autostart --install` registers a logon task that runs the daemon with no console window. `--status` reports it, `--uninstall` removes it. |
| macOS / Linux | `engram setup autostart` is Windows-only and exits `2` elsewhere. Use the systemd user unit or launchd LaunchAgent in [Install as a service on macOS and Linux](docs/en/installation-unix.md). |

Stop the daemon from any installation with `uv run --python 3.14.6 engram stop`, which asks it to
close the database, waits on the ownership lock, and reports whether it actually stopped.

Connect a client with one command, using the endpoint from your own configuration:

```text
uv run --python 3.14.6 engram setup client claude --protocol
```

Replace `claude` with `codex` or `gemini`. It writes `.mcp.json`, `~/.codex/config.toml`, or
`~/.gemini/settings.json`, merging rather than overwriting, and `--protocol` appends the
[client protocol](docs/en/client-protocol.md) to `CLAUDE.md`, `AGENTS.md`, or `GEMINI.md`. Exact
hand-written blocks are in the [setup guide](docs/en/setup.md).

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
| `[server]` | `ENGRAM_SERVER_HOST`, `_PORT`, `_PATH`, `_WRITE_WAIT_TIMEOUT_MS`, `_TTL_SWEEP_INTERVAL_SECONDS`, `_MAX_REQUEST_BODY_BYTES` | Loopback HTTP endpoint, backpressure, a hard 512 KiB request-body ceiling, and logical-expiry sweep |
| `[capsule]` | `ENGRAM_CAPSULE_DEFAULT_TOKEN_BUDGET`, `_MIN_TOKEN_BUDGET`, `_MAX_TOKEN_BUDGET` | Recall budget |
| `[retrieval]` | `ENGRAM_RETRIEVAL_MODE`, `_FTS_TOP_K`, `_FTS_MAX_QUERY_CHARS`, `_FTS_MAX_QUERY_TERMS`, `_FTS_MIN_PREFIX_CHARS`, `_FTS_QUERY_TIMEOUT_MS`, `_HYBRID_MAX_CANDIDATES`, `_EMBEDDINGS_ENDPOINT`, `_EMBEDDINGS_MODEL`, `_EMBEDDINGS_TIMEOUT_MS`, `_RRF_K` | Bounded FTS with one absolute deadline, or local hybrid |
| `[datacron]` | `ENGRAM_DATACRON_COMMAND`, `_ARGS`, `_VAULT_ROOT`, `_READ_PATHS`, `_WRITE_PATHS`, `_NEW_NOTE_DIRECTORY`, `_NEIGHBOR_LIMIT`, `_STARTUP_TIMEOUT_MS`, `_REQUEST_TIMEOUT_MS`, `_SHUTDOWN_TIMEOUT_MS` | Gateway, timeouts, and Datacron confinement |

For list variables, `ARGS` uses shell parsing and `READ_PATHS`/`WRITE_PATHS` use the OS path
separator. Safe defaults are in [`engram.example.toml`](engram.example.toml). Datacron writes stay
disabled while `write_paths` is empty, even if the parent process defines
`DATACRON_WRITE_PATHS`. The default local transport launches `datacron mcp serve`.

## MCP tools

| Tool | Essential inputs | Result and policy |
| --- | --- | --- |
| `remember` | `statement`, `kind`, `scope`, `subject_keys`, `observed_at`, `evidence` | Returns `created`, `retry`, `corroborated`, `existing_trusted`, or `renewed`; new/renewed content remains quarantined |
| `recall` | `query`, `scope`, `kinds`, `include_conflicts`, `token_budget` | Returns a trust-aware capsule; always inspect `notes.recall_complete` and warning codes |

Accepted kinds: `preference`, `decision`, `project_state`, `fact`, `episode`. The server assigns
provenance; a client can never declare itself a `human` source.

## Security and privacy

- All data, lexical indexes, audit records, and logs remain local.
- No cloud LLM call or telemetry is implemented.
- Client candidates are quarantined so an unattested assertion cannot become shared truth.
- The MCP client name/version is a self-declared namespace, not authentication or a confidentiality
  boundary.
- Hybrid mode contacts only the explicitly configured embeddings endpoint; FTS is the default.
- Datacron writes use allowlists under `_memory/`, one deterministic canonical path, and an exact
  reread.
- Direct listening accepts only loopback IP literals. A remote proxy must connect locally and
  provide authentication, TLS, and network controls itself.

See the complete [security model](docs/en/security.md).

## CLI commands

```text
engram --version
engram --debug serve
engram init
engram init --force
engram doctor
engram doctor --json
engram serve
engram stop
engram setup autostart --install
engram setup autostart --status
engram setup autostart --uninstall
engram setup client claude --protocol
engram setup client codex --print
engram setup client gemini --force
engram migrate
engram preflight
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

`--config <path>` is global and goes **before** the subcommand.

| Command | What it is for |
| --- | --- |
| `engram init` | Writes the starting `engram.toml` from the copy packaged in the distribution — no checkout, no shell syntax, no platform difference. Refuses to overwrite; `--force` replaces deliberately. |
| `engram doctor` | The single diagnosis to run before anything else, and the one to send anyone whose client cannot connect. Reports the interpreter, the SQLite floor, the resolved configuration and whether it loads, the database and schema version, the ownership lock, the endpoint, and the log file, each with its repair. Exits `0` unless something failed; `--json` for scripts. |
| `engram stop` | Asks the daemon owning this database to close it and exit, waits on the lock, and reports whether it stopped. The only way to stop a windowless logon task or a supervised service cleanly. |
| `engram setup autostart` | **Windows only.** Registers, inspects, or removes the logon task that starts the daemon without a console window. Exits `2` on any other platform and changes nothing; use [systemd or launchd](docs/en/installation-unix.md) there. Without it, Engram stops at the next logoff. |
| `engram setup client` | Writes `.mcp.json` (Claude Code, current directory), `~/.codex/config.toml` (Codex), or `~/.gemini/settings.json` (Gemini) using the endpoint from the loaded configuration. Merges instead of clobbering: other servers, keys, and TOML comments survive. `--protocol` appends the session protocol to `CLAUDE.md` / `AGENTS.md` / `GEMINI.md`; `--print` writes nothing; `--force` replaces an entry naming a different endpoint. |

The Codex block this command writes deliberately omits the `required` key: OpenAI defines it as
failing Codex startup when the server cannot initialise, so a memory broker that is merely down
would take the whole assistant with it.

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
Stop the daemon with `engram stop` before `migrate`, `classify`, `attest`, `supersede`, `reindex`,
or `consolidate`, then restart it before recall. These commands acquire the same OS lock as the daemon and fail
clearly while it is active; `list` remains available through a read-only SQLite connection. For an
existing database, first take a SQLite-consistent backup, stop the daemon, and run
`engram preflight`. It holds the offline-writer lock, keeps the source database read-only, copies its
snapshot to temporary storage, and proves the complete migration/rebuild there before reporting
compatibility. Then run `engram migrate` and inventory
`engram list --unclassified`. Review every historical preference, decision, or fact and assign its
family explicitly with `engram classify ENTRY_ID --claim-key "topic/claim"`; never infer these keys
in bulk. Trusted commands use `[attestation].default_actor` unless `--actor` is provided.
R3 never truncates data that exceeds its new fixed bounds: a failed preflight names the first row
to review with 2026.0730.01 before retrying. If preflight reports
`vector_rebuild_required: true` and hybrid mode is enabled, run `engram reindex` after migration.
SQLite first loads the schema under a temporary 256 KiB ceiling, then retains an 8 MiB
value/row ceiling; consolidation snapshots have an explicit 4 MiB UTF-8 limit. Preflight rejects
incompatible historical data without truncating it.

Known CLI failures never print a traceback by default. Exit code `2` means usage or configuration,
`3` means a local resource is unavailable (port, process lock, database, or SQLite runtime), `4`
means an external dependency is unavailable (Datacron or the embedding endpoint), `5` means
transient store contention, and `6` means an apply report contains failed or stale propositions.
Use global `--debug` before the command, or `ENGRAM_DEBUG=1`, only when a traceback is needed.

## Current limitations

- Engram cannot passively observe conversations: each client must call `recall` and `remember`
  according to the documented protocol.
- The transport is local HTTP. Claude Desktop remote connectors require a public HTTPS URL;
  Claude Code connects directly to localhost.
- Hybrid mode is experimental and depends on a local OpenAI-compatible endpoint. It explicitly
  falls back to FTS when the provider is unavailable or returns an invalid vector, or when the
  exact scan exceeds fixed candidate, dimension, or byte budgets. Incomplete vector coverage marks
  recall incomplete.
- PyPI publication and MCP Registry submission are deferred. The manifest describes the package
  and its local HTTP endpoint.
- FTS remains lexical: bounded fallbacks handle noise, term order, and controlled prefixes, but
  cannot recover paraphrases with no shared vocabulary. The evaluation report keeps historical
  semantic paraphrases separate from the lexical release contract.

## Documentation

| Start | Use | Operate safely |
| --- | --- | --- |
| [Five-minute path](docs/en/quick-start.md) | [User guide](docs/en/user-guide.md) | [Operator guide](docs/en/operator-guide.md) |
| [Installation](docs/en/setup.md) | [Engram, Datacron, and Cortex](docs/en/datacron-cortex.md) | [Security](docs/en/security.md) |
| [Client protocol](docs/en/client-protocol.md) | [Architecture](docs/en/architecture.md) | [FAQ and hub](docs/en/index.md) |
| [Windows and SQLite](docs/en/installation-windows.md) | [Data contract](docs/en/spec.md) | [macOS and Linux service install](docs/en/installation-unix.md) |

## Development

```text
uv sync --extra dev --python 3.14.6
uv run --python 3.14.6 ruff check .
uv run --python 3.14.6 ruff format --check .
uv run --python 3.14.6 mypy
uv run --python 3.14.6 pytest
uv build --python 3.14.6
```

## Contributing

The gates, the commit convention and the FR/EN documentation mirroring rule are described in
[CONTRIBUTING.md](CONTRIBUTING.md). Report a vulnerability privately, never in a public issue: see
[SECURITY.md](SECURITY.md), which also states the threat model — a loopback-only endpoint, no
authentication on the port, and trust granted only by a human gesture.

## License

Apache License 2.0. Copyright 2026 Julien Bombled. See [LICENSE](LICENSE) and the
[third-party notices](THIRD_PARTY_NOTICES.md).
