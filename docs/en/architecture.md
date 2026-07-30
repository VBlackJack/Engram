# Architecture

[Francais](../fr/architecture.md) | [English](architecture.md)

> **Reference document:** not required to start. Use the
> [quick start](quick-start.md) or the
> [Engram-Datacron-Cortex diagram](datacron-cortex.md) for a short path.

In five points: Engram is a stateful local HTTP server, SQLite is canonical, one process writes,
search indexes are derived, and every Datacron consolidation remains reviewed. Cortex is not an
internal Engram component.

## Overview

```text
Claude Code / Codex / Gemini
             |
             | MCP Streamable HTTP
             v
     Engram (single writer)
       |        |        |
     SQLite   Retrieval  Capsule
       |                   |
       +--- reviewed plan -+----> Datacron MCP ----> Markdown vault
```

Engram is a local stateful process. Multiple MCP clients may use it, but only one Engram instance
must write the database. The server serializes mutations and returns `server busy, retry` when the
lock cannot be acquired within the configured timeout.

A database-adjacent coordination file carries an OS-level exclusive lock. The daemon owns it for
its lifetime; each offline writer owns it for the whole command. Windows uses a locked byte through
the CRT, while POSIX uses `flock`. PID and command metadata are diagnostic only, so a file left by a
dead process is safely reclaimed when the kernel lock is absent. The file is not unlinked on release
to avoid inode replacement races. The public `EngramStore` owns the same lease for its lifetime;
multiple connections in that one process share the lease, while another writer process is rejected.
Pure `list` reads use SQLite `mode=ro` without this writer lock.

## SQLite storage

SQLite opens in WAL mode with foreign keys, a busy timeout, and transactional migrations. The
3.51.3 floor avoids the WAL-reset bug. The `entries` table is canonical; FTS and vector tables are
derived and reconstructible with `engram reindex`. Startup compares the external-content FTS index
with canonical rows and rebuilds it when it is missing or inconsistent. `consolidation_plans`
anchors immutable plan snapshots and their single-use state outside the editable review artifact.
Every connection first loads `sqlite_schema` under a temporary 256 KiB ceiling, then applies a
permanent 8 MiB ceiling to SQLite values and rows. Consolidation snapshots are rejected before
mutation above 4 MiB of UTF-8.
Upgrade preflight holds the writer lease, reads one source snapshot, and proves the full migration
on a disposable on-disk copy. Canonical table definitions are checked exactly; derived objects may
be absent or rebuildable tables, but a colliding index, trigger, or view is rejected.

`audit_log` is append-only. It stores actor, action, entry identifier, and a detail fingerprint,
never the statement or conversation payload.

The configured log rotates at 10 MiB with five backups. Every Engram process closes the file between
records and serializes write/rollover through a separate OS lock, including on Windows where an open
handle would otherwise make rename-based rotation fail.

## HTTP MCP server

FastMCP exposes a Streamable HTTP endpoint (`127.0.0.1:8377/mcp` by default) and two strict-schema
tools. `remember` uses the write queue. `recall` performs retrieval and assembly in a worker without
mutating trust state.

MCP receives tool calls only. It does not observe the client conversation, which is why the
[client protocol](client-protocol.md) is part of the product.

## Retrieval

`fts` mode derives operator-neutral terms from bounded NFKC input. It ranks an exact phrase and an
all-term query first, then fills every remaining top-K slot from fairly interleaved disjunction and
controlled-prefix rankings. Strict hits keep priority without suppressing morphological matches.
Every stage applies visibility filters and a hard top-K in SQL, with BM25 followed by recency and
identifier tie-breaks. One absolute monotonic `fts_query_timeout_ms` deadline covers lock wait and
all progressive SQLite stages. SQLite's progress handler interrupts an over-budget scan; only
fully completed stages exist in the internal rank, but the public result is empty and marked
incomplete to avoid partial revalidation after expiry. The configured `hybrid` mode
combines FTS and embeddings through reciprocal
rank fusion (`rrf_k`). It computes an exact semantic top-K only while all visible vectors fit under
`hybrid_max_candidates` and fixed dimension/byte budgets. The scan returns IDs and vectors only;
at most the fused top-K payloads are materialized and revalidated. An overflow, malformed provider
result, or unavailable embedding endpoint degrades explicitly to FTS and marks the capsule
incomplete. Vector rebuilds use bounded pages and a temporary stage, preserve the live index until
the atomic swap, and compare SQLite `data_version` before that swap to reject an intervening commit.

## Recall capsule

The D6/D7 policy separates ranking from trust. The capsule is filled in this order:

1. `current`: trusted active preferences, decisions, and facts;
2. `next_action`: current project states;
3. `relevant`: relevant episodes;
4. `conflicts`: unresolved symmetric versions, only when requested;
5. `own_pending`: quarantined candidates belonging to the calling client only;
6. `sources` and `notes`: identifiers, selection rationale, `recall_complete`, and bounded warning
   codes for any fail-closed omission.

The budget is bounded by `[capsule]` against the serialized fallback plus structured payload. The
serialized UTF-8 byte count is used as a one-byte-per-token conservative ceiling and an absolute
payload-size cap. Lower-priority items are omitted first with an explicit note, and oversized scope
metadata is represented by a bounded digest; a stale, superseded, expired, or other-client
quarantined entry cannot enter `current`.

## Datacron consolidation

The gateway talks to the Datacron server over stdio MCP and enforces configured allowlists. The
flow is deliberately split:

The Datacron subprocess lives in a non-daemon owner thread. `startup_timeout_ms`,
`request_timeout_ms`, and `shutdown_timeout_ms` bound each boundary. On close, the transport closes
stdin and then terminates the process tree through a Windows Job Object or a POSIX process group; a
timeout poisons the session and forbids implicit replay.

1. `--plan` rereads the canonical path, searches several neighboring-section variants, classifies
   create/link/skip, anchors an immutable SQLite snapshot, and writes JSON + Markdown;
2. a human changes only each approve/reject decision;
3. `--apply` verifies the artifact against the snapshot and consumes the plan; `new` creates one
   canonical note and rereads it, `redundant` reverifies and links without a write, while `update`
   and `contradictory` remain `skip`;
4. Engram marks `promoted` only when the exact create or exact link is reverified;
5. `--check-freshness` later compares hashes without rewriting the vault.

A failure on one proposition does not authorize forcing another. An apply report containing
`failed` or `stale` exits with code 6. The consumed plan cannot be replayed, so unresolved
propositions must be replanned from current Datacron state.

Only `plan_id` and decisions cross the review boundary as inputs. Path, heading, heading level,
content, hashes, neighbors, classification, and action must exactly match the trusted snapshot;
manual retargeting is rejected. Apply still regenerates the current neighbor set and rechecks the
live target before creating or linking. Paths use canonical forward-slash syntax and headings are
one line. A create path contains the candidate ID and has no alternate. After an ambiguous response,
only a note whose full canonical content is identical, apart from line-ending and final-newline
normalization, becomes `redundant`; any extra content remains `update/skip`. After all propositions,
apply rereads every
potentially written path and marks any promotion whose whole-note hash diverged as stale.
