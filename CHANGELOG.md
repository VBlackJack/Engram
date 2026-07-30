# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the
project uses date-derived CalVer releases in the form `YYYY.MMDD.NN`.

## [Unreleased]

### Added

- Add schema-v5 claim families, canonical content identities, retained observation evidence,
  relational supersession integrity, explicit remember outcomes, and fail-closed startup checks.
- Add offline `migrate` and `classify` workflows, including retry-safe legacy classification and
  `list --unclassified` inventory.
- Add trusted local `attest`, `supersede`, and status-filtered `list` commands with configurable
  audit identity and stable JSON output.
- Anchor consolidation plans as immutable SQLite snapshots with generated single-use identifiers.

### Changed

- Trusted `preference`, `decision`, and `fact` attestations now require `--claim-key`;
  `project_state` uses the reserved `project_state/current` family and `episode` has no claim key.
- `remember` now reports `created`, `retry`, `corroborated`, `existing_trusted`, or `renewed`.
  Equivalent retries share one generation while independent writers retain separate observations.
- The public `Entry` constructor remains source compatible: new `canonical_key` and `claim_key`
  fields are trailing optional fields. This release still changes persisted and CLI contracts.

### Fixed

- Reject malformed v4 data before migration, forged normalized identities, drifted triggers or
  indexes, missing candidate observations, unsafe supersession links, and invisible trusted-write
  successes.
- Make attestation plus multi-entry supersession atomic and preserve relation/legacy JSON coherence
  through expiry and purge.
- Reject any reviewed-plan retargeting, content substitution, or pending decision; refuse
  consumed-plan replay; and return a distinct nonzero exit after persisting apply reports with
  failed or stale outcomes.
- Bind reviewed targets to both the reviewed and current neighbor sets, keep NEW targets immutable,
  reject non-canonical Windows paths and multiline headings, and reconcile freshness after batches.
- Enforce business validity inside the promotion transaction and tolerate eligibility changes while
  hybrid retrieval waits for embeddings.
- Enforce inclusive business-validity windows at recall, plan, and apply time.
- Keep reviewed `update` propositions visible for audit while forcing `skip` until Datacron
  provides an independently verified durable section identity.
- Enforce TTL at recall time and run a configurable logical-expiry sweep for the HTTP daemon.
- Promote canonically identical quarantined content in place when it receives trusted attestation.
- Enforce one cross-process database writer with an OS lock, stale-owner recovery, and a truly
  read-only status listing path.
- Preserve Datacron search rank while selecting consolidation targets and bind redundant
  propositions to their exact normalized neighbor.
- Launch the installed Datacron CLI shape by default and prevent an empty write allowlist from
  inheriting parent-process write permissions.
- Map expected CLI failures to stable exit codes and actionable stderr without tracebacks, with an
  explicit debug opt-in.
- Check HTTP port availability before opening storage, close startup resources on every path, and
  wrap Datacron stdio startup failures at the gateway boundary.

### Backlog

- Evaluate Porter stemming and prefix search only if real usage with weaker clients exposes
  morphological misses. Hybrid retrieval remains the existing opt-in extension path.

## [2026.0721.04] - 2026-07-21

### Added

- SQLite WAL storage with migrations, bounded inputs, deterministic idempotency, TTL lifecycle,
  supersession, and a content-free append-only audit log.
- Streamable HTTP MCP server exposing the strict `remember` and `recall` tools.
- Trust-aware recall capsules with quarantine, conflict symmetry, provenance, freshness, and token
  budget policies.
- FTS5/BM25 retrieval with recency tie-breaking and optional local hybrid embeddings.
- Seeded deterministic evaluation corpus, graders, reports, and code-owned P2 decision.
- Human-reviewed consolidation to Datacron through MCP, with allowlists, CAS writes, rereads, and
  stale-promotion detection.
- Mirrored French and English product documentation, CI gates, release artifacts, and MCP Registry
  metadata.

[Unreleased]: https://github.com/VBlackJack/Engram/compare/v2026.0721.04...HEAD
[2026.0721.04]: https://github.com/VBlackJack/Engram/releases/tag/v2026.0721.04
