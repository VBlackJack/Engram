# FAQ and troubleshooting

[Français](../fr/faq.md) | [English](faq.md)

## Start here, whatever the symptom

```text
uv run --python 3.14.6 engram doctor
```

It measures, in one pass: the interpreter, the SQLite version against the floor, **which**
configuration file was resolved and whether it loads, the database and its schema version, what
owns the database lock, whether the endpoint accepts connections, and whether the log file can be
written. Every failing line prints the command that repairs it. It exits `0` unless a check
failed; warnings are not failures. `--json` emits the same report as one document for a script.

Read the whole report before changing anything: the diagnosis does not stop at the first problem,
because the second one is often what explains the first.

Quick access:

- [The client cannot connect](#the-client-cannot-connect)
- [The daemon will not stop](#the-daemon-will-not-stop)
- [The candidate stays in own_pending](#the-candidate-is-in-own_pending-not-current)
- [Consolidation reports stale](#consolidation-reports-stale)
- [Cortex cannot see a recent note](#cortex-cannot-see-a-recent-datacron-note)

## `Configuration file does not exist`

Run `engram init` to write a starting configuration where the loader looks for it, or set
`ENGRAM_CONFIG` to an absolute path. Relative TOML paths resolve from the selected file's
directory, and `engram doctor` prints the exact path it tried.

`engram init` writes the copy packaged inside the distribution, so it needs no checkout beside it.
It refuses to replace an existing file; `engram init --force` replaces one deliberately.

## `SQLite 3.51.3 or newer is required`

The active Python's `sqlite3` is too old, even if a newer `sqlite3` command is on PATH. The message
names a documentation URL and `engram doctor` rather than a file inside a checkout; run the latter,
which prints the version found, the floor it must clear, and the interpreter it came from:

```text
uv run --python 3.14.6 engram doctor
```

Repair it by running Engram on an interpreter whose `sqlite3` links a newer library — for example
the one `uv python install 3.14.6` provides — or follow
[Windows and SQLite](installation-windows.md) to replace the DLL of a runtime you must keep.

## The client cannot connect

Run `engram doctor` first. Its `daemon` and `endpoint` lines separate three causes that look
identical from inside the client:

- `daemon` warns that nothing owns the database: Engram is not running. Start it, or install the
  startup integration ([Windows](setup.md#2-configure-and-start),
  [macOS and Linux](installation-unix.md)).
- `endpoint` fails while `daemon` reports `serving`: the daemon bound a different address from the
  one the client is pointed at. Compare `[server].host` and `[server].port` with the log.
- `endpoint` warns that the URL accepts but no daemon owns this database: **another process is
  listening there**, and the client is reaching it instead of your memory. Give this installation
  its own port, or stop the other one.

Otherwise verify the URL ends in `/mcp` and matches what the client's file contains.
`engram setup client claude --print` — or `codex`, or `gemini` — shows the block built from your
own configuration, so it is also the fastest way to see the endpoint Engram believes in. A browser
is not an MCP test. Claude Desktop's remote connector cannot reach localhost; use Claude Code or an
authenticated HTTPS proxy.

## The daemon will not stop

```text
uv run --python 3.14.6 engram stop
```

This works for every installation, including the Windows logon task and a systemd or launchd
service, none of which have a console to interrupt. It waits on the ownership lock and reports
whether the daemon actually stopped, rather than assuming it did.

Do not create the `<database>.stop` sentinel by hand. The command resolves that path from the
configuration the daemon itself loaded; a path typed from memory that matches no shipped
configuration writes the request where nothing is watching, and looks like it worked.

If `engram stop` fails, it names the pid still holding the database and leaves the request in
place. Read the log before terminating that process: killing a daemon mid-write is what leaves a
write-ahead log behind.

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

## Engram is gone after a reboot or logoff

Nothing installed a startup integration. `engram serve` in a terminal lasts exactly as long as that
terminal.

- **Windows:** `uv run --python 3.14.6 engram setup autostart --install` registers a logon task
  that runs the daemon with no console window. Check it with `--status`.
- **macOS / Linux:** `engram setup autostart` is Windows-only and exits `2` elsewhere. Install the
  systemd unit or launchd agent in
  [Install as a service on macOS and Linux](installation-unix.md).

## Cortex cannot see a recent Datacron note

Cortex has no watcher and Datacron does not call it. Run:

```text
cortex sync
```

Then check `cortex_freshness` from the MCP client. The Datacron note remains canonical while the
Cortex index lags. See the [trilogy guide](datacron-cortex.md).

## The CLI returns code `4`

An external dependency is unavailable: Datacron during consolidation, or the embedding endpoint
during a hybrid operation. Use the global `--debug` flag only after checking the relevant
dependency's configuration and availability.

## The CLI returns code `2` and I do not know which setting is wrong

Code `2` is usage or configuration. `engram doctor` names the configuration file it resolved and
the first key that stops it from loading:

```text
uv run --python 3.14.6 engram doctor
```

`engram setup autostart` also exits `2` on macOS and Linux by design — it is Windows-only. Use
[Install as a service on macOS and Linux](installation-unix.md) there.
