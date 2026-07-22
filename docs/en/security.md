# Security and privacy

[Francais](../fr/security.md) | [English](security.md)

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
and attestation remain explicit.

## Single writer and SQLite

Run one Engram instance per database file. The application lock serializes mutations, SQLite uses
WAL and `BEGIN IMMEDIATE`, and a short timeout asks the client to retry. The guard rejects SQLite
< 3.51.3. Do not put the database on a network share whose SQLite locking semantics are uncertain.
Stop the daemon before `attest` or `supersede`, then restart it after the trusted mutation.

## Network

The `127.0.0.1` default is a security boundary. Engram implements no account, token, or TLS. Do not
bind directly to `0.0.0.0`. For a remote client, place an authenticated HTTPS proxy in front of
Engram, restrict origins and network access, and monitor requests.

## Datacron confinement

Reads and writes go through Datacron MCP. `vault_root`, `read_paths`, and `write_paths` constrain
targets; `new_note_directory` must remain under `_memory/`. An empty write list closes the path. A
mutation requires the expected CAS hash and a reread before promotion is recorded.

`contradiction_scan` is a read-only signal; it grants no write authority.

## Data and external calls

- No telemetry.
- No cloud LLM call is implemented.
- FTS5 stays entirely local.
- Hybrid mode sends statements to the configured embeddings endpoint. Keep it on loopback for a
  local guarantee; a remote URL is an operator-chosen data export.
- Logs intentionally omit memory content, but still protect `logs/` and the database as user data.

## Bounded capsule

`token_budget` is constrained by server-side minimum and maximum values. The builder removes
lower-priority sections before exceeding the budget and reports omissions. This bound limits
accidental exfiltration and context flooding; it does not replace access control.

## Backup and incident response

Back up coherently: while Engram runs, use a SQLite-consistent backup rather than a simple copy of
the database/WAL/SHM files. If corruption is suspected, stop the writer, preserve the files, work
on a copy, and do not run purge.
