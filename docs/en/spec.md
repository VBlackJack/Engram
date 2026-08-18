# Data contract

[Français](../fr/spec.md) | [English](spec.md)

> **Reference document:** useful when implementing or auditing a client. For daily use, read the
> [user guide](user-guide.md).

This document defines Engram's persistent contract. Trust and provenance fields are server
decisions, not free-form client claims.

## Kinds

| Kind | Use | Default TTL |
| --- | --- | --- |
| `preference` | Explicit durable preference | No expiry |
| `decision` | A decision and its useful rationale | No expiry |
| `project_state` | Current state and next action | 30 days |
| `fact` | Stable verified fact | No expiry |
| `episode` | Short-lived useful session event | 7 days |

TTLs are configurable in `[ttl_days]`. A value of `0` disables expiry for a **trusted** entry.
An unattested candidate is additionally bounded by `candidate_max_days`, 90 by default, so a kind
set to `0` never grants a model's unreviewed guess the lifetime of a human-verified fact; the
ceiling only ever shortens, and attesting a candidate lifts it. Expiring a candidate removes it
from recall without deleting it: `engram list --status expired` still shows the statement, and
attesting it returns it to trusted as a new entry. Recall excludes entries at
or past `expires_at` immediately, while the daemon periodically changes their status to `expired`.
Business validity is inclusive: an entry is recallable and consolidatable only when the store's UTC
date satisfies `valid_from <= today <= valid_until`; either missing bound is open-ended.

## Entry schema

| Field | Type | Rule |
| --- | --- | --- |
| `id` | string | Canonical server ULID |
| `kind` | enum | One of the five kinds |
| `scope` | string | Normalized logical space, `user` by default |
| `statement` | string | Content bounded by `max_statement_chars` |
| `subject_keys` | string list | Bounded, normalized retrieval/topic keys |
| `status` | enum | `active`, `superseded`, `quarantined`, `expired` |
| `promotion_state` | enum | `candidate`, `approved`, `rejected`, `promoted` |
| `source_type` | enum | `human`, `tool_verified`, `model_inferred`, `session_summary` |
| `writer_model` | string or null | MCP identity of the writing client |
| `confidence` | enum | `high`, `medium`, `low` |
| `observed_at` | datetime or null | Supplied observation time when known |
| `recorded_at` | datetime | Server UTC timestamp |
| `valid_from` | date or null | Business validity start |
| `valid_until` | date or null | Business validity end |
| `expires_at` | datetime or null | Expiry computed by kind |
| `canonical_key` | string | SHA-256 identity of normalized kind, scope, and statement |
| `idempotency_key` | string | SHA-256 fingerprint of the canonical key and entry ULID |
| `claim_key` | string or null | Normalized semantic conflict family for trusted claims |
| `supersedes` | identifier list | Replaced versions |
| `evidence` | `{type, ref}` list | Opaque references, never the source payload |
| `stale` | boolean | Datacron promotion whose freshness diverged |
| `datacron_ref` | string or null | Promoted target path and section |
| `datacron_hash` | string or null | Hash reread after the write |
| `synced_at` | datetime or null | Last confirmed synchronization |

## Provenance and confidence

For new or renewed content, `remember` creates a `model_inferred`, `quarantined`, `candidate`
entry. An exact retry by the same writer returns that generation; materially different metadata
from that writer is retained as a corroborating observation. Canonically identical active trusted
content is returned without creating a candidate. The explicit outcome is `created`, `retry`,
`corroborated`, `existing_trusted`, or `renewed`. Writer identity comes from MCP initialization,
not from an argument. A requested `high` confidence is stored as `medium`, and the cap event is
audited.

Every candidate generation owns at least one row in `entry_observations`. Additional observations
retain their writer, confidence, dates, subject keys, and evidence without silently merging
different writers into one provenance claim.

Only the trusted local CLI path accepts `human` or `tool_verified`. An entry is eligible for
consolidation only when it is `active`, `approved`, not stale, inside its business-validity window,
and attested by one of those sources. Apply checks the window before a Datacron write, and the store
checks it again inside the promotion transaction, so an entry that became invalid cannot be marked
promoted.

## Lifecycle

1. A `remember` call creates, renews, retries, or corroborates a `quarantined` candidate, or
   returns canonically identical active trusted content.
2. Explicit attestation produces an `active`, `approved` entry. Canonically identical candidate
   content is promoted in place and retains its identifier.
3. A new version can mark previous versions `superseded` without erasing history.
4. TTL marks an entry `expired`; physical purge is separate and audited.
5. Reviewed consolidation changes the state to `promoted` only after an exactly reread create or
   an exactly reverified `redundant` link.

Active trusted conflicts are grouped only by the exact `(kind, scope, claim_key)` tuple. Every
version in such a family is returned symmetrically in `conflicts`; none is arbitrarily placed in
`current`. `subject_keys` improve topic discovery but never define semantic conflict identity.
Legacy trusted rows without a `claim_key` remain readable through the explicit unclassified
inventory, but are hidden from `current` until an operator classifies them.

## Process ownership

The daemon and every command that can mutate the configured database acquire the same exclusive OS
lock before opening the store. Contention fails immediately with owner diagnostics. An unlocked
coordination file is not ownership, so stale PID metadata cannot block recovery. Status listing uses
an existing migrated database in SQLite read-only mode and remains available while the daemon runs.

The CLI error contract reserves exit code `2` for usage/configuration, `3` for unavailable local
resources, `4` for unavailable external dependencies (Datacron transport or the embedding
endpoint), `5` for transient store contention, `6` for an apply report containing failed or stale
propositions, and `130` for an operator interruption propagated to the CLI. Known failures omit
tracebacks unless the global `--debug` flag or `ENGRAM_DEBUG=1` is active. Port availability is
checked before SQLite or the process lock is opened.

## Idempotency

`canonical_key` identifies exact normalized content. `idempotency_key` identifies one stable
generation by hashing that canonical key with its ULID, so it survives attestation in place.
Retry detection uses the canonical identity, writer, and retained observation: an exact retry
returns `retry` and records an `idempotent_noop`; a new observation returns `corroborated`.

## Datacron freshness

After promotion, Engram stores `datacron_ref`, `datacron_hash`, and `synced_at`. The freshness
check rereads the note. If its hash differs, the entry becomes `stale` and is hidden from `current`
until review. History is retained and this check never rewrites Datacron.

Datacron search does not provide durable subject keys. The gateway therefore keeps those keys empty
instead of copying them from the candidate. Search combines the full AND query with per-term
variants. Work is bounded to three full queries plus at most eight single-term variants, and
`neighbor_limit` remains between 1 and 64. Any hit without a path, unreadable, empty, or lacking a
unique section selector fails the plan; it can never cause a `new` classification followed by a
create. `get_note(full)` validates
the returned `rel_path`, removes only the canonical Datacron sandbox envelope, and rejects
truncated content. The server `content_hash`, computed over the file, stays intact for freshness
and verification; the `freshness-contract-v1` contract is mandatory. A `redundant` proposition
always targets the neighbor whose normalized statement exactly matches the candidate. An `update`
proposition shows the classified target, its `H1` through `H6` level, and its diff in the report,
but its current action is `skip`: section patching is not allowed without an independently verified
durable identity anchor.

A new note path is deterministic, bounds its ASCII slug to 64 characters, and always contains the
candidate ID. Before each plan, Engram rereads that canonical path. A note whose full content
exactly matches the expected rendering
becomes `redundant`, including after a lost create response; only line endings and final-newline
presence are normalized. Any other existing note becomes `update/skip`. No alternate path may
create a duplicate. Planning persists a canonical
immutable proposition snapshot under a generated `plan_id`. The review artifact may change only
the per-proposition decision. Apply rejects any other divergence or remaining `pending` decision
without consuming the plan. Once every decision is approve/reject, apply consumes the plan before
external writes and refuses replay. A final batch pass reconciles the whole-note hash of every
promotion on a potentially written path before recall can expose it. Any `failed` or `stale` apply
outcome produces exit code 6 after the report is written.
