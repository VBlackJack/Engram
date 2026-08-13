# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the
project uses date-derived CalVer releases in the form `YYYY.MMDD.NN`, written in the PEP 440
normal form that packaging tools stamp on the built artifacts, so a release never carries two
different version strings.

## [Unreleased]

## [2026.813.2] - 2026-08-13

### Fixed

- Close the session leak for every request that cannot open a session, not only for the four shapes
  2026.813.1 guarded. That release checked that a session-opening body parsed as JSON, which a
  well-formed body that is not an MCP request passes: `{}`, `[]`, `null`, a request without its
  JSON-RPC version, a request without a method, a method other than `initialize`, and a notification
  each still reached the SDK, which registered a transport before answering `400`. Measured on the
  published wheel: 500 requests of each shape, 500 sessions each. Two methods the guard never saw
  were open as well, because it only screened `POST`: a `GET` or a `DELETE` carrying no session id
  registered one transport per request too.

  The rule the transport actually enforces is narrower than a parse check, and it is now the rule
  applied: without a session id the only request that can be accepted is a `POST` carrying a valid
  JSON-RPC `initialize`, validated with the SDK's own message model so the guard and the transport
  cannot disagree about what valid means. Every refusal reproduces the status code, the JSON-RPC
  error code and the wording the transport would have sent, so a client cannot tell which layer
  answered. Requests that carry a session id are untouched; one the server never issued was already
  answered `404` without allocating anything.

  Measured after the change: 500 requests of each of the eleven refused shapes, and of each refused
  method, grow the session table by zero, while a session initialising alongside the flood lists
  both tools, remembers and recalls without noticing it.

## [2026.813.1] - 2026-08-13

### Added

- Refuse a session-opening request the MCP transport would reject anyway. The SDK registers a
  transport and starts its task before it validates anything, and nothing reclaims the entry, so
  every rejected POST that carried no session id left a live session and a task for the lifetime of
  the process: measured, 500 rejected requests grew the table by exactly 500. The checks now run one
  layer earlier, with the transport's own rules and status codes, and 2000 rejected requests grow it
  by zero while a real session still initialises, lists both tools, remembers and recalls.
  Corrected in 2026.813.2: that measurement covered the four shapes this release guarded, and the
  guard left the rest of the class open.

- `engram init` writes the starting configuration where the loader will look for it. The template
  now ships inside the distribution, so an installation that is not a checkout can be configured
  at all: before this, the wheel contained no template and the documented remedy named a file only
  a git clone has. It replaces a PowerShell block whose two guard lines do not stop the copy when
  pasted into a live session, and it behaves identically on every operating system.
- `engram stop` asks the daemon that owns the database to exit and waits on the ownership lock to
  confirm it did. A windowless daemon can receive no console control event, so the only lever was a
  sentinel file built by hand at a path the documentation published wrongly: the request landed
  where nothing was watching, the command reported success, and every offline write stayed blocked
  behind a daemon that never stopped.
- `engram doctor` reports the interpreter, the SQLite floor, which configuration was resolved and
  whether it loads, the database and its schema version, the ownership lock, the endpoint and the
  log file, each beside the command that repairs it. The endpoint is judged against the lock,
  because a port that accepts is not proof that this Engram is behind it: a second installation
  holds the same loopback port and a client pointed there reaches whatever answers.
- `engram setup client claude|codex|gemini` writes the vendor MCP configuration from the endpoint
  this installation actually serves, and `--protocol` appends the session protocol to CLAUDE.md,
  AGENTS.md or GEMINI.md. Every write merges, so other MCP servers and Codex comments survive. The
  protocol text moved into the package, where a command can deliver it and a test can pin it
  against the published page.


- Add `engram setup autostart`, which registers the daemon as a Windows logon task run by the
  windowed interpreter beside the running one. The absence of a console window is a property of
  that interpreter rather than of a flag asking a console to hide itself, so it can be proven from
  outside the process. Installing twice converges on one task instead of adding a second, removing
  an absent task succeeds, and a host without a logon scheduler is refused explicitly instead of
  reporting a success it did not achieve.
- Add a module entry point, so an interpreter that cannot run a console launcher can still start
  Engram. It binds the standard streams a windowed interpreter leaves unset, because the transport
  stack asks standard output whether it is a terminal while configuring its own logging: without
  this, the daemon announced its endpoint and then died on the next statement, which is a worse
  failure than the window it replaces.
- Add a `--config` option that names the configuration file explicitly. A scheduled task inherits
  no environment variable, so the file a normal invocation would discover has to be written into
  the task rather than assumed.
- Add a way to ask the daemon to stop. A windowless process can receive no console control event,
  so every stop so far was a termination, and a terminated daemon never closes its last SQLite
  connection: the write-ahead log and its shared index survived every restart. The request is an
  empty file beside the ownership lock, which makes the right to stop Engram exactly the right to
  write in its database directory — already the right to corrupt it — and exposes nothing on the
  unauthenticated loopback port. The daemon now exits on its own with code 0, and the disappearance
  of `-wal` and `-shm` is the observable proof the shutdown was clean.
- Clear a stop request only after taking the ownership lock, never before. A second `serve` started
  by mistake fails on that lock; had it cleared first, it would have cancelled a stop meant for the
  daemon that owns the database, leaving an operator who asked for a shutdown watching nothing
  happen. A request left behind by an earlier run is cleared at startup, so a stale file cannot
  stop the next daemon either.
- Escalate `--replace` from asking to ending to terminating, and verify each rung against the
  ownership lock. Ending a task only terminates the process the scheduler started: in production
  that was a wrapper script, and the daemon it had launched survived, kept the lock and kept
  serving while the scheduler reported the task stopped. The payload now names which rung released
  the database.
- Report `interpreter_present` and `registered_command` in `--status`, read back from the task the
  scheduler holds rather than recomputed from the running interpreter. The recomputed one always
  exists; the one written into the task at install time is the one that can be deleted, and its
  absence is why a task can be registered, enabled, and unable to start.
- Refuse to install the logon task while another registered task would open the same database, and
  add `--replace` to take that installation over. The test is the database each task resolves to,
  never the name of a task: a literal name would be hardcoding, and would miss any installation
  that chose a different one. A task launched through a wrapper script does not announce its
  configuration; that answer is reported as undetermined and still refuses, because installing over
  an unknown produces a task that looks registered and never serves. `--replace` disables the
  competing task rather than deleting it, stops the daemon it started, and waits for the ownership
  lock itself rather than for a fixed delay. `--force` remains for an operator who knows better.
  `--status` now also lists the competing tasks; the fields it already published are unchanged.
- Add task-oriented French and English quick-start, operator, and
  Engram-Datacron-Cortex guides, with short ADHD-friendly paths and expected results.
- Add contract tests binding every published input constraint to the enforcement the server
  performs, including a proof that each published keyword is what rejects the invalid value.
- Add a turnkey contract job that installs the distribution on an unmodified Windows runtime and
  asserts the outcome that runtime actually produces: a working command when its SQLite clears the
  fail-closed minimum, and the documented exit code and message when it does not.

### Changed

- `capsule.py` and `retrieval.py` join the per-module coverage floors. Reaching a memory is part of
  holding it: an entry a capsule drops under budget costs the reader what a lost row costs.
- The client configuration write is in place and, deliberately, not atomic. Writing through the
  existing file is what preserves its permissions, owner, access control list, extended attributes,
  NTFS alternate data streams and hard links; the price is that an interrupted write can leave the
  file partly written. Both documentation sets say so, and present `--print` as the path that writes
  nothing.

- The continuous-integration quality gate no longer lets a formatting difference decide whether the
  tests run. Lint, format and types report their verdict and the run continues to pytest; each
  still fails the build, at the end of the job. A red build and a failing test suite had become the
  same statement, and four commits reached production through that gap.
- The documented Codex block no longer sets `required = true`, which fails Codex startup when the
  server cannot initialise and made a memory broker that is merely down take the whole assistant
  with it.



- Run continuous integration and release builds on the uv-managed interpreter the documentation
  tells users to install, instead of building or replacing SQLite on the runner. A gate that
  installs the version it is meant to require measures the runner, not the product.
- Exercise the documented Windows DLL repair where it can be observed to repair something, on a
  runtime that has just been proven to fail, and report explicitly when a run leaves that path
  unexercised.
- State one specific uv-managed build as the turnkey Windows installation instead of one
  recommendation among two, publish the measurements behind that claim, and reframe the DLL
  replacement as the repair of a runtime already in place. The guarantee belongs to the build, not
  to the installer: asking for a minor version leaves the choice of build to whichever `uv` release
  is installed, and some of those builds link a SQLite below the floor.
- Warn in both quick starts that a successful `pip install` is not evidence, and that substituting
  an interpreter for the pinned one is expected to fail the version check.
- Measure test coverage on every run and fail below a whole-project floor of 85 percent, applied
  once to the union of both operating-system legs rather than to each leg in isolation, so a module
  that branches on the platform is not judged on code the other leg is the one to execute.
- Require each module whose failure would lose or corrupt stored memory to clear 90 percent on its
  own, and treat one of them going missing from the report as a violation rather than as a pass.
- Cover the branches that decide whether memory survives: every failure path of both database open
  routines closes its connection, migration to schema five refuses an unusable supersession edge and
  carries a valid one forward, and the ownership lock refuses a second acquisition, survives an
  operating-system refusal while releasing, and states who holds it for every role.
- Audit the locked dependency set the run installed, rather than a fresh resolution describing a
  dependency tree the tested artifact never had.
- Publish a CycloneDX software bill of materials alongside every released distribution, generated
  from the lock so its component list does not depend on which platform the release was built on.
- Attest the provenance of the exact files a release publishes.
- Validate the MCP registry manifest against the schema it declares, offline and on both operating
  systems, and require a bumped `$schema` to bring its schema with it rather than be checked against
  the copy it was not written for.
- Separate daily memory use from privileged maintenance, route both READMEs through goal-based
  documentation, and clarify that Cortex synchronization is explicit rather than automatic.
- Publish both tool schemas without local references so a client that does not dereference still
  sees the `kind` enum and the `evidence` object structure instead of unknown types.
- Publish the configured `token_budget` bounds and default on the `recall` schema, and flatten
  optional fields so their format and length limits stay visible at one level.
- Reject an `observed_at` value carrying no UTC offset, and a non-textual instant, at argument
  validation instead of deeper in storage.

### Fixed

- Re-read the vendor file before reporting that a client needs no change. `connect` trusted the
  plan, which describes the file as it was when the plan was made, so an entry pointed elsewhere or
  a file that had stopped parsing both answered "Already correct": nothing written, success
  reported, and the client left aimed at an endpoint that is not this Engram.
- Restart the lifetime when a candidate is attested. Promoting in place kept the candidate's expiry,
  so a human attestation made shortly before it inherited what remained and then left recall, while
  the same attestation with no candidate present lived its full term. This releases `expires_at`
  from the identity trigger, in migration 6: an entry's expiry is a lifecycle attribute rather than
  part of what makes it that entry. Migration 5 is unchanged, so every database converges through a
  numbered step.
- Refuse a re-attestation that carries metadata a trusted entry cannot be given. Different subject
  keys, confidence, evidence or validity window were discarded while the call reported success, the
  audit row is content-free so nothing recorded the loss, and no path amends trusted content in
  place to undo it. An identical repeat is still the idempotent no-op it was.
- Store the subject keys a corroboration adds. They were written to the observation, which nothing
  reads, so the entry and the lexical index kept the original set and a key added to make a memory
  findable did not: measured, zero hits. A union above the configured maximum is refused rather than
  truncated.
- Publish the input bounds the server enforces rather than the ceilings a configuration may set.
  `remember` advertised 16384 characters and 64 subject keys while refusing anything over 2000 and
  8, so a client composing from the schema alone failed on exactly the long rationales worth
  keeping.

- Stop reading a scheduled task's command and arguments as paths relative to the directory Engram
  happens to be invoked from. The scheduler stores those tokens as free text, routinely relative or
  with an environment variable left unexpanded, and resolving them against the current directory
  placed them inside the configuration directory, which the documented installation makes the
  current directory. Measured on one ordinary machine: 89 of 109 registered tasks were reported as
  competing for the database, so the single documented Windows install command refused, and the
  remedy it printed would have disabled disk cleanup, Office updates and a browser updater. A
  relative token now belongs to the working directory of its own task, and a refusal names the
  first few conflicts rather than all of them.
- Stop spending trusted content on a conflict family the capsule then drops. A conflict group is
  removed whole and eviction reaches it last, so a family too large to fit on its own made the
  builder empty every other section to make room and then remove the family as well: asking for
  conflicts returned strictly less than not asking, while the capsule's own advice on the other
  path is to retry with the flag set. Measured over 100 combinations of family size, statement
  width and budget, 71 lost content and delivered no conflict in exchange. Groups are now measured
  before anything is evicted, so only the ones the capsule can deliver are kept.
- Refuse a configuration key Engram does not read instead of running on the default it hides. The
  loader validated types and never names, so `[server] prot` and `[servr] port` both left the
  endpoint on 8377 without a word, and a misspelt `[database] path` opened a different database
  from the configured one. A near miss now names the key it resembles.
- Correct the version to its own PEP 440 normal form. `2026.0730.02` was stamped `2026.730.2` on
  every built artifact while the tag, the documentation and `engram --version` said otherwise, so
  a release carried two version strings and nothing failed. A test now refuses a version that is
  not canonical, and a second one refuses a version the release workflow's tag pattern would not
  build.
- Name a URL and `engram doctor` in the SQLite floor refusal instead of a repository path, which
  someone who installed a distribution does not have.
- Explain the six `attest` options that decide what a memory means and how long it lives, and the
  identifiers `classify` and `supersede` take, none of which carried help text.


- Pin the interpreter to a build measured to link a recent enough SQLite on Windows **and** Linux.
  The previously pinned build cleared the requirement on one platform only, so the Linux leg would
  have refused every storage operation.
- Assert the shared `OSError` contract, not the Windows-specific subclass, when configuration
  refuses an unusable rotation lock. The narrower assertion failed on Linux, where the same
  condition is reported as `IsADirectoryError`.

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

[Unreleased]: https://github.com/VBlackJack/Engram/compare/v2026.813.2...HEAD
[2026.813.2]: https://github.com/VBlackJack/Engram/compare/v2026.813.1...v2026.813.2
[2026.813.1]: https://github.com/VBlackJack/Engram/compare/v2026.0730.02...v2026.813.1
[2026.0730.02]: https://github.com/VBlackJack/Engram/compare/v2026.0721.04...v2026.0730.02
[2026.0721.04]: https://github.com/VBlackJack/Engram/releases/tag/v2026.0721.04
