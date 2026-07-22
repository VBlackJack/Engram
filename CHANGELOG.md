# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the
project uses date-derived CalVer releases in the form `YYYY.MMDD.NN`.

## [Unreleased]

### Added

- Add trusted local `attest`, `supersede`, and status-filtered `list` commands with configurable
  audit identity and stable JSON output.

### Fixed

- Enforce TTL at recall time and run a configurable logical-expiry sweep for the HTTP daemon.
- Promote canonically identical quarantined content in place when it receives trusted attestation.
- Enforce one cross-process database writer with an OS lock, stale-owner recovery, and a truly
  read-only status listing path.

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
