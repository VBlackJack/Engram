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
conflict identity.
```

This text grants no authority to attest or consolidate; those actions remain human and separate.

## Install by client

- **Claude Code**: user instructions or a local uncommitted `CLAUDE.md`.
- **Codex**: user instructions or a local `AGENTS.md` at the desired scope.
- **Gemini CLI / Code Assist**: user or project `GEMINI.md`.
- **Claude Desktop**: project/custom instructions associated with connector use.

See [setup.md](setup.md) for MCP configuration files and transport limitations.
