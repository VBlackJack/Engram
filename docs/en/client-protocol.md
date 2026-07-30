# Engram client protocol

[Francais](../fr/client-protocol.md) | [English](client-protocol.md)

## Why this protocol exists

MCP carries tool calls; Engram cannot passively observe the conversation, open files, or the end of
a session. Each client must decide when to recall and when to propose a memory. This contract keeps
that behavior consistent across Claude, Codex, and Gemini.

## The three moments

### 1. At startup

Call `recall` before substantive work with a short query naming the project, task, and relevant
subjects. Use the narrowest known scope. Read `current` and `next_action` before acting; treat
`conflicts` and `own_pending` as unresolved.

### 2. During the session

Call `remember` only after durable, explicit information: a decision, correction, confirmed
preference, verified fact, or state change. Choose the kind and stable `subject_keys`. Do not store
intermediate reasoning, assumptions, secrets, transcripts, large outputs, or facts already present.

## Incomplete recall

Always inspect `notes.recall_complete`. When it is `false`, do not infer that an absent memory does
not exist. `notes.warnings` contains stable, bounded codes:

| Code | Meaning | Client action |
| --- | --- | --- |
| `query_too_long` | The query exceeded the configured character cap and was not searched. | Shorten the query and retry. |
| `query_too_many_terms` | The query exceeded the configured term cap and was not searched. | Use a smaller, focused set of subjects and retry. |
| `query_has_no_search_terms` | Normalization produced no searchable lexical term. | Provide at least one word or number. |
| `fts_query_timeout` | The absolute lexical plan deadline expired; the public result is empty to avoid unbounded or partial revalidation. | Do not infer absence. Retry with fewer, more specific lexical terms; contact the operator if it persists. |
| `capsule_budget_overflow` | Whole entries were omitted to respect the serialized payload cap. | Retry with a larger allowed budget or narrower query; do not infer absence. |
| `unclassified_claim_omitted` | A trusted legacy claim lacked explicit proposition identity and was omitted fail-closed. | Ask the operator to classify the legacy entry before relying on recall completeness. |
| `conflicts_hidden_by_request` | Conflicting versions matched but `include_conflicts` was false. | Retry with `include_conflicts=true` and handle every version symmetrically. |
| `conflict_family_overflow` | A complete conflict family did not fit the retrieval cap and was omitted. | Narrow the request or ask the operator to review `fts_top_k`; never choose a version implicitly. |
| `project_state_overflow` | Scoped project-state history exceeded the retrieval cap and was omitted. | Do not infer that there is no next action; narrow the scope or use an operator inventory. |
| `hybrid_provider_unavailable` | Embeddings failed and recall used lexical FTS only. | Treat semantic recall as incomplete; retry later or continue with explicit lexical limits. |
| `hybrid_provider_invalid_vector` | The embedding provider returned the wrong count or an empty, non-numeric, non-finite, non-float32, oversized, or zero-norm query vector. | Treat semantic recall as unavailable and repair or replace the provider. |
| `hybrid_candidate_overflow` | The exact vector scan exceeded `hybrid_max_candidates` and recall used lexical FTS only. | Narrow scope/kinds or have the operator review the bounded hybrid cap. |
| `hybrid_vector_budget_exceeded` | Visible vector dimensions or bytes exceeded the fixed in-memory scan budget, so recall used lexical FTS only. | Narrow scope/kinds or remain in FTS mode; do not raise the fixed safety budget. |
| `hybrid_vector_coverage_incomplete` | At least one visible entry had no vector, or an incompatible vector dimension, for the configured model. | Reindex vectors or treat semantic absence as unknown. |

### 3. At the end

When state changed in a way useful to the next session, call `remember` with a concise
`project_state`: completed work, current state, any confirmed blocker, and the next concrete action.
Add an `episode` only when the event itself has short-term future value.

## Ready-to-paste instruction

```text
Engram session protocol

Engram is an MCP memory broker. It does not observe the conversation unless you call its tools.

At the start of a substantive task, call recall with a concise query naming the project, task,
and relevant subjects. Use the narrowest known scope. Read current and next_action before acting.
Treat conflicts and own_pending as unresolved, never as verified truth.

During the session, call remember only for durable, explicit information: a confirmed preference,
a decision and useful rationale, a verified fact, an important correction, or a meaningful project
state change. Choose one of preference, decision, fact, project_state, or episode. Use a small set
of stable subject_keys. Never store secrets, credentials, private transcripts, speculation,
intermediate reasoning, large tool output, or information already present.

Before ending a session whose state materially changed, call remember with one concise
project_state containing the completed work, current state, any confirmed blocker, and the next
concrete action. Store an episode only when the event itself has short-term future value.

The remember tool reports created, retry, corroborated, existing_trusted, or renewed. New and
renewed generations are unconfirmed quarantined candidates; existing_trusted is already trusted
content, while own_pending remains unresolved and must never justify an irreversible action.
If recall returns a conflict, surface every version in the exact kind/scope/claim_key family
symmetrically and ask for resolution when it matters. subject_keys are discovery hints, not
conflict identity. If notes.recall_complete is false, never infer absence from omitted results;
inspect notes.warnings and follow the documented retry or operator action for each code.
```

This text grants no authority to attest or consolidate; those actions remain human and separate.

## Install by client

- **Claude Code**: user instructions or a local uncommitted `CLAUDE.md`.
- **Codex**: user instructions or a local `AGENTS.md` at the desired scope.
- **Gemini CLI / Code Assist**: user or project `GEMINI.md`.
- **Claude Desktop**: project/custom instructions associated with connector use.

See [setup.md](setup.md) for MCP configuration files and transport limitations.
