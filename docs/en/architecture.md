# Architecture

[Francais](../fr/architecture.md) | [English](architecture.md)

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
to avoid inode replacement races. Pure `list` reads use SQLite `mode=ro` without this writer lock.

## SQLite storage

SQLite opens in WAL mode with foreign keys, a busy timeout, and transactional migrations. The
3.51.3 floor avoids the WAL-reset bug. The `entries` table is canonical; FTS and vector tables are
derived and reconstructible with `engram reindex`. `consolidation_plans` anchors immutable plan
snapshots and their single-use state outside the editable review artifact.

`audit_log` is append-only. It stores actor, action, entry identifier, and a detail fingerprint,
never the statement or conversation payload.

## HTTP MCP server

FastMCP exposes a Streamable HTTP endpoint (`127.0.0.1:8377/mcp` by default) and two strict-schema
tools. `remember` uses the write queue. `recall` performs retrieval and assembly in a worker without
mutating trust state.

MCP receives tool calls only. It does not observe the client conversation, which is why the
[client protocol](client-protocol.md) is part of the product.

## Retrieval

`fts` mode queries FTS5 with BM25 and uses recency as a tie-breaker. A bounded substring search is
the fallback when FTS returns nothing. The configured `hybrid` mode combines FTS and embeddings
through reciprocal rank fusion (`rrf_k`). The embeddings endpoint is local and OpenAI-compatible;
when it is unavailable, recall explicitly falls back to FTS.

## Recall capsule

The D6/D7 policy separates ranking from trust. The capsule is filled in this order:

1. `current`: trusted active preferences, decisions, and facts;
2. `next_action`: current project states;
3. `relevant`: relevant episodes;
4. `conflicts`: unresolved symmetric versions, only when requested;
5. `own_pending`: quarantined candidates belonging to the calling client only;
6. `sources` and `notes`: identifiers and selection rationale.

The budget is bounded by `[capsule]`. Lower-priority items are omitted first with an explicit note;
a stale, superseded, expired, or other-client quarantined entry cannot enter `current`.

## Datacron consolidation

The gateway talks to the Datacron server over stdio MCP and enforces configured allowlists. The
flow is deliberately split:

1. `--plan` searches neighboring sections, classifies create/patch/skip, anchors an immutable SQLite
   snapshot, and writes JSON + Markdown;
2. a human changes only each approve/reject decision;
3. `--apply` verifies the artifact against the snapshot, consumes the plan, rereads the note,
   compares the CAS hash, writes through MCP, then rereads;
4. Engram marks `promoted` only when the reread confirms the mutation;
5. `--check-freshness` later compares hashes without rewriting the vault.

A failure on one proposition does not authorize forcing another. An apply report containing
`failed` or `stale` exits with code 6. The consumed plan cannot be replayed, so unresolved
propositions must be replanned from current Datacron state.

Only `plan_id` and decisions cross the review boundary as inputs. Path, heading, heading level,
content, hashes, neighbors, classification, and action must exactly match the trusted snapshot;
manual retargeting is rejected. Apply still regenerates the current neighbor set and rechecks the
live target before writing. Paths use canonical forward-slash syntax and headings are one line. The
exact patched section must match the canonical candidate body after reread; finding that body
elsewhere in the note is not sufficient verification. After all propositions, apply rereads every
potentially written path and marks any promotion whose whole-note hash diverged as stale.
