# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the
project uses date-derived CalVer releases in the form `YYYY.MMDD.NN`.

## [Unreleased]

### Added

- Add task-oriented French and English quick-start, operator, and
  Engram-Datacron-Cortex guides, with short ADHD-friendly paths and expected results.
- Add contract tests binding every published input constraint to the enforcement the server
  performs, including a proof that each published keyword is what rejects the invalid value.

### Changed

- Separate daily memory use from privileged maintenance, route both READMEs through goal-based
  documentation, and clarify that Cortex synchronization is explicit rather than automatic.
- Publish both tool schemas without local references so a client that does not dereference still
  sees the `kind` enum and the `evidence` object structure instead of unknown types.
- Publish the configured `token_budget` bounds and default on the `recall` schema, and flatten
  optional fields so their format and length limits stay visible at one level.
- Reject an `observed_at` value carrying no UTC offset, and a non-textual instant, at argument
  validation instead of deeper in storage.

## [2026.0730.02] - 2026-07-30

### Added

- Add schema-v5 claim families, canonical content identities, retained observation evidence,
  relational supersession integrity, explicit remember outcomes, and fail-closed startup checks.
- Add offline `migrate` and `classify` workflows, including retry-safe legacy classification and
  `list --unclassified` inventory.
- Add trusted local `attest`, `supersede`, and status-filtered `list` commands with configurable
  audit identity and stable JSON output.
- Anchor consolidation plans as immutable SQLite snapshots with generated single-use identifiers.
- Add a versioned lexical/adversarial recall contract while preserving the historical semantic
  paraphrase benchmark as a separate measurement.
- Add progressive, operator-neutral FTS queries with configurable query, term, prefix, and SQL
  top-K bounds.
- Add a pre-parser HTTP request-body ceiling, an absolute SQLite FTS deadline, and stable
  incomplete-recall signalling when that deadline expires.
- Add a deterministic hybrid retrieval contract covering semantic-only wins, lexical preservation,
  provider ordering, and repeatable fused ranks without approving a production embedding model.
- Add a source-read-only upgrade preflight that proves schemas 3-5 on a disposable snapshot and
  reports required FTS/vector reconstruction before production migration.

### Changed

- Trusted `preference`, `decision`, and `fact` attestations now require `--claim-key`;
  `project_state` uses the reserved `project_state/current` family and `episode` has no claim key.
- `remember` now reports `created`, `retry`, `corroborated`, `existing_trusted`, or `renewed`.
  Equivalent retries share one generation while independent writers retain separate observations.
- The public `Entry` constructor remains source compatible: new `canonical_key` and `claim_key`
  fields are trailing optional fields. This release still changes persisted and CLI contracts.
- `engram eval` now writes its artifacts and exits nonzero when the checked-in FTS quality,
  latency, or capsule-budget contract fails; CI runs that contract on Linux and Windows.
- Pin the FTS gate to versioned retrieval settings and a 4800-byte conservative capsule cap,
  fingerprint every seed and recall-task field with that configuration, and retain CI
  metrics/reports even when the gate fails.
- Version the R3 evaluation corpus and schema, and require the FTS gate to prove that no evaluated
  recall exceeded its absolute query deadline.
- Upgrade note: before restarting an existing installation, set `[capsule]`
  `default_token_budget = 4800`, `min_token_budget = 1200`, and
  `max_token_budget = 6000` (or another maximum at least as large as the default). Older
  `min_token_budget` values below 1200 are rejected at startup because they cannot contain the
  mandatory bounded response envelope.
- Upgrade note: take a SQLite-consistent backup and run read-only `engram preflight` before
  restarting 2026.0730.02. New fixed content ceilings and domain-separated SHA-256 `mcp-v2:`
  identities for reserved `%`/`/` components are never applied by truncation or guessed ownership
  aliases. Inventory pending R2 owners that violate the new component policy. A failed preflight
  names the first row to review or export with 2026.0730.01 before retrying.

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
  read-only status listing path; the exported Store API now holds the same writer lease.
- Preserve Datacron search rank while selecting consolidation targets and bind redundant
  propositions to their exact normalized neighbor.
- Launch the installed Datacron CLI shape by default and prevent an empty write allowlist from
  inheriting parent-process write permissions.
- Map expected CLI failures to stable exit codes and actionable stderr without tracebacks, with an
  explicit debug opt-in.
- Check HTTP port availability before opening storage, close startup resources on every path, and
  wrap Datacron stdio startup failures at the gateway boundary.
- Bound recall ranking in SQL, neutralize FTS control syntax, and keep deterministic progressive
  fallback ordering across exact, conjunction, disjunction, and controlled-prefix stages.
- Preserve strict FTS hits as the highest-priority results while still filling unused top-K
  capacity from fairly interleaved disjunction and prefix stages.
- Verify the external-content FTS index against canonical rows at startup and rebuild derived state
  automatically when it is missing, partial, or inconsistent.
- Enforce the capsule budget against the combined structured and fallback payload, including
  adversarial scope metadata, using the complete serialized UTF-8 size as both a conservative
  one-byte-per-token ceiling and an absolute payload cap.
- Reject every wildcard, hostname, LAN, or public listening address at configuration construction;
  the unauthenticated MCP daemon now accepts only loopback IP literals.
- Reject missing, empty, oversized, control-bearing, or non-UTF-8 MCP client identities; preserve
  safe legacy owners and map reserved separators into a collision-resistant `mcp-v2:` namespace.
- Apply fixed ceilings to persisted text, evidence, audit identities, vector dimensions, embedding
  batches, inputs, and streamed response bodies, including startup validation of existing data.
- Preserve only fully completed lexical stages on timeout, bound lock wait and SQLite execution
  under one deadline, and retain the previous vector index when a batched rebuild is incomplete.
- Stream upgrade/integrity scans, precheck persisted allocation bounds, validate canonical table
  definitions, and prove full migrations without modifying the source database.
- Load SQLite schemas under a 256 KiB bootstrap ceiling, retain an 8 MiB value/row ceiling, and
  reject consolidation snapshots above 4 MiB before mutation or upgrade.
- Reject malformed embedding URLs and model identifiers during configuration and normalize every
  defensive HTTPX URL failure into the documented hybrid fallback path.
- Serialize rotating-log writes across processes and reject a staged vector swap after any
  intervening SQLite commit.

### Backlog

- Evaluate stemming only if real usage with weaker clients exposes morphological misses. Hybrid
  retrieval remains the existing opt-in extension path for semantic paraphrases.

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

[Unreleased]: https://github.com/VBlackJack/Engram/compare/v2026.0730.02...HEAD
[2026.0730.02]: https://github.com/VBlackJack/Engram/compare/v2026.0721.04...v2026.0730.02
[2026.0721.04]: https://github.com/VBlackJack/Engram/releases/tag/v2026.0721.04
