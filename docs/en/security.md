# Security and privacy

[Francais](../fr/security.md) | [English](security.md)

> **Reference document:** to start, remember only that the server stays local, `own_pending` is
> untrusted, and human review precedes every promotion. See the [user guide](user-guide.md).

## Trust boundary

The MCP client is untrusted for provenance. `remember` accepts neither `source_type`,
`writer_model`, nor a privileged status. The server derives identity from the MCP session, enforces
`model_inferred`, caps confidence at `medium`, and quarantines the entry.

`human` and `tool_verified` provenance is available only through the local `engram attest` command.
The audit actor comes from configuration or an explicit operator flag. An opaque evidence reference
does not by itself turn a candidate into a verified fact.

## Anti-poisoning quarantine

A quarantined candidate:

- does not enter `current`, `next_action`, or `relevant`;
- is visible only in `own_pending` for the same MCP client name/version pair;
- is labeled `unconfirmed candidate`;
- cannot be consolidated into Datacron.

This policy limits cross-client propagation of injected instructions or false inference. Review
and attestation remain explicit. The MCP client name/version pair is self-declared. It is a
convenience namespace for pending observations, not authentication, authorization, or a
confidentiality boundary. Any process that can reach the endpoint can claim the same pair.

## Single writer and SQLite

Run one Engram instance per database file. The application lock serializes mutations, SQLite uses
WAL and `BEGIN IMMEDIATE`, and a short timeout asks the client to retry. The guard rejects SQLite
< 3.51.3. Do not put the database on a network share whose SQLite locking semantics are uncertain.

The daemon holds an exclusive OS lock derived from the database path. Offline writers (`migrate`,
`classify`, `attest`, `supersede`, `reindex`, and `consolidate`) hold that same lock for their
complete operation and fail before opening SQLite when another owner exists. The coordination file
persists, but owner
metadata alone never grants ownership: Windows byte-range locking or POSIX `flock` is authoritative
and is released automatically when a process dies. `list` uses SQLite read-only mode and takes no
writer lock. Stop the daemon before an offline write, then restart it afterward.

## Network

The listening address is a security boundary. Engram accepts only unambiguous loopback IP literals
such as `127.0.0.1` or `::1`; hostnames, wildcard, LAN, and public addresses fail configuration.
Engram implements no account, token, or TLS. For a remote client, place an authenticated HTTPS
proxy in front of the loopback endpoint, restrict origins and network access, and monitor requests.

## Datacron confinement

Reads and writes go through Datacron MCP. `vault_root`, `read_paths`, and `write_paths` constrain
targets; `new_note_directory` must remain under `_memory/`. An empty write list closes the path. A
parent-process `DATACRON_WRITE_PATHS` value is explicitly cleared in that case rather than
inherited. A mutation requires the expected CAS hash and a reread before promotion is recorded.

`contradiction_scan` is a read-only signal; it grants no write authority.

## Data and external calls

- No telemetry.
- No cloud LLM call is implemented.
- FTS5 stays entirely local.
- Hybrid mode sends statements to the configured embeddings endpoint. Keep it on loopback for a
  local guarantee; a remote URL is an operator-chosen data export.
- Logs intentionally omit memory content, but still protect `logs/` and the database as user data.

## Bounded capsule

`token_budget` is constrained by server-side minimum and maximum values, published as `minimum`
and `maximum` on the `recall` tool schema so a client sees the bound before calling. Engram
interprets it
conservatively as the maximum UTF-8 byte count of the complete serialized tool result: one byte per
possible byte-level subword token. This is an absolute payload-size cap, not a promise of exact
tokenization for every model. The builder measures fallback and structured content together,
replaces oversized scope metadata with a bounded digest, removes lower-priority sections before
the cap, and exposes fail-closed omissions through `notes.recall_complete` and bounded warning
codes. This bound limits accidental exfiltration and context flooding; it does not replace access
control.

## Backup and incident response

Back up coherently: while Engram runs, use a SQLite-consistent backup rather than a simple copy of
the database/WAL/SHM files. If corruption is suspected, stop the writer, preserve the files, work
on a copy, and do not run purge.
