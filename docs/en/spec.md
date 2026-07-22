# Data contract

[Francais](../fr/spec.md) | [English](spec.md)

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

TTLs are configurable in `[ttl_days]`. A value of `0` disables expiry. Recall excludes entries at
or past `expires_at` immediately, while the daemon periodically changes their status to `expired`.

## Entry schema

| Field | Type | Rule |
| --- | --- | --- |
| `id` | string | Stable server identifier |
| `kind` | enum | One of the five kinds |
| `scope` | string | Normalized logical space, `user` by default |
| `statement` | string | Content bounded by `max_statement_chars` |
| `subject_keys` | string list | Bounded and normalized subject identities |
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
| `idempotency_key` | string | Unique deterministic fingerprint |
| `supersedes` | identifier list | Replaced versions |
| `evidence` | `{type, ref}` list | Opaque references, never the source payload |
| `stale` | boolean | Datacron promotion whose freshness diverged |
| `datacron_ref` | string or null | Promoted target path and section |
| `datacron_hash` | string or null | Hash reread after the write |
| `synced_at` | datetime or null | Last confirmed synchronization |

## Provenance and confidence

`remember` always creates a `model_inferred`, `quarantined`, `candidate` entry. Writer identity
comes from MCP initialization, not from an argument. A requested `high` confidence is stored as
`medium`, and the cap event is audited.

Only the trusted local CLI path accepts `human` or `tool_verified`. An entry is eligible for
consolidation only when it is `active`, `approved`, not stale, and attested by one of those sources.

## Lifecycle

1. A `remember` call creates or idempotently finds a `quarantined` candidate.
2. Explicit attestation produces an `active`, `approved` entry. Canonically identical candidate
   content is promoted in place and retains its identifier.
3. A new version can mark previous versions `superseded` without erasing history.
4. TTL marks an entry `expired`; physical purge is separate and audited.
5. Reviewed consolidation changes the state to `promoted` only after a CAS write and reread.

Active conflicts sharing `subject_keys` are symmetric: no version is arbitrarily placed in
`current`. They appear in `conflicts` only when requested.

## Process ownership

The daemon and every command that can mutate the configured database acquire the same exclusive OS
lock before opening the store. Contention fails immediately with owner diagnostics. An unlocked
coordination file is not ownership, so stale PID metadata cannot block recovery. Status listing uses
an existing migrated database in SQLite read-only mode and remains available while the daemon runs.

## Idempotency

The key is computed from the relevant canonical content. An identical retry returns the same entry
with `idempotent=true` and creates an `idempotent_noop` event; it does not duplicate memory.

## Datacron freshness

After promotion, Engram stores `datacron_ref`, `datacron_hash`, and `synced_at`. The freshness
check rereads the note. If its hash differs, the entry becomes `stale` and is hidden from `current`
until review. History is retained and this check never rewrites Datacron.
