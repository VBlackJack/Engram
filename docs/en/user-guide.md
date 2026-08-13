# User guide

[Français](../fr/user-guide.md) | [English](user-guide.md)

> **Goal:** use Engram every day without administering the database.<br>
> **Time:** less than one minute at the start and end of a task.<br>
> **Result:** the session recovers useful context and leaves one clear next action.

For installation, start with the [five-minute quick start](quick-start.md). Advanced operations are
separated into the [operator guide](operator-guide.md).

## The three-moment routine

### 1. At the start: recall

Ask the client to call `recall` before substantive work:

```text
query = "Engram documentation audit Datacron Cortex"
scope = "project/engram"
```

A useful query names:

- the project;
- the current task;
- two or three relevant subjects.

**Read first:** `current`, `next_action`, and `notes.recall_complete`.

### 2. During: work, then remember only durable information

Call `remember` after explicit information that will help a future session:

- a confirmed preference;
- a decision and its useful rationale;
- a verified fact;
- an important correction;
- a project-state change.

Example:

```text
statement = "User documentation now separates daily use from operator tasks."
kind = "project_state"
scope = "project/engram"
subject_keys = ["engram:documentation"]
```

Do not store secrets, transcripts, hypotheses, intermediate reasoning, large tool outputs, or
ephemeral details.

### 3. At the end: leave the next step

When state changed, store one concise `project_state`:

```text
statement = "The user guide is complete. State: links need validation. Next action: run documentation tests."
kind = "project_state"
scope = "project/engram"
subject_keys = ["engram:documentation", "engram:release"]
```

It should state:

1. what is complete;
2. the current state;
3. any confirmed blocker;
4. the next concrete action.

## Choose the type (`kind`)

| Situation | Type |
| --- | --- |
| "Always use absolute paths in this project" | `preference` |
| "SQLite was selected for these reasons" | `decision` |
| "Migration is complete; next action: deploy" | `project_state` |
| "The service listens on port 8377" | `fact` |
| "The test failed once after a timeout" | `episode` |

Use a small number of stable, descriptive `subject_keys`, such as `engram:storage` or
`project:release`.

## Read a capsule without getting lost

Read in this order:

| Area | Question to ask |
| --- | --- |
| `current` | Which trusted information can I use now? |
| `next_action` | Which project state or next step is still useful? |
| `conflicts` | Are several trusted versions unresolved? |
| `own_pending` | What did I propose from this same client without validation? |
| `relevant` | Which recent episode may help? |
| `notes` | Is recall complete, and why were these items returned? |
| `sources` | Which identifiers support the capsule? |

### Two safety rules

1. `own_pending` means **unconfirmed candidate**. Never use it to justify an irreversible action.
2. When `notes.recall_complete` is `false`, a missing memory proves nothing. Read
   `notes.warnings`, then narrow the query or request operator action.

When `conflicts` contains several versions, present them symmetrically. Do not silently choose the
most convenient one.

## Understand the `remember` result

| Outcome | Meaning |
| --- | --- |
| `created` | New quarantined candidate |
| `retry` | Same observation returned without a new generation |
| `corroborated` | New observation of candidate content already present |
| `existing_trusted` | Canonically identical content is already trusted |
| `renewed` | New generation of an expired memory, still quarantined |

New or renewed content does not enter `current` automatically.

## When to ask an operator

Move to the [operator guide](operator-guide.md) to:

- attest or correct memory;
- migrate a database;
- rebuild FTS or vectors;
- consolidate into Datacron;
- diagnose a CLI exit code.

For an error visible in the client, start with the [FAQ](faq.md).

## Which tool after Engram?

- Need a canonical note or durable write: **Datacron**.
- Need to find an idea across many documents: **Cortex**.
- Need to recover session context: **Engram**.

The full flow is in [Engram, Datacron, and Cortex](datacron-cortex.md).
