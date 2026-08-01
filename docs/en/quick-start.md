# Five-minute quick start

[Francais](../fr/quick-start.md) | [English](quick-start.md)

> **Goal:** start Engram and verify one memory.<br>
> **Time:** 5 to 10 minutes, excluding Python installation.<br>
> **Risk:** low with a new database. For an existing database, use the
> [operator guide](operator-guide.md).<br>
> **End result:** one MCP client can call `recall` and `remember`.

Keep only the current step open. Detailed explanations are linked and are not required to finish
this path.

## 1. Install

This path requires Git and `uv`. Check them first:

```powershell
git --version
uv --version
```

**You should see:** both version numbers. Otherwise install
[Git](https://git-scm.com/downloads) or
[`uv`](https://docs.astral.sh/uv/getting-started/installation/) before continuing.

In PowerShell, for a new installation:

> **STOP for recovery or upgrade:** when reusing an Engram checkout, configuration, or database,
> do not run this block. Use the
> [operator guide](operator-guide.md#migrate-an-existing-database).

```powershell
git clone https://github.com/VBlackJack/Engram.git
Set-Location Engram
uv sync --python 3.14.6
uv run --python 3.14.6 python -c "import sqlite3; print(sqlite3.sqlite_version)"
if (Test-Path -LiteralPath "engram.toml") { throw "Existing Engram configuration: stop" }
if (Test-Path -LiteralPath "engram.db") { throw "Existing Engram database: stop" }
Copy-Item engram.example.toml engram.toml -ErrorAction Stop
```

**You should see:** SQLite `3.51.3` or newer, followed by an `engram.toml` file.

The `3.14.6` pin is not a stylistic choice. The SQLite a runtime links is decided per build, not per
Python version, and most distributions link one too old to run Engram; `3.14.6` is measured to clear
the requirement on Windows and Linux. Substituting an interpreter you already have will most likely
fail the version check on the next line. See
[Windows and SQLite](installation-windows.md) for the measurements. Installing it needs `uv` 0.12.1
or newer.

**If not:** follow only the
[Windows and SQLite troubleshooting path](installation-windows.md).

## 2. Start

```powershell
uv run --python 3.14.6 engram serve
```

**You should see:** the process stays active and the local endpoint is
`http://127.0.0.1:8377/mcp`.

Keep that terminal open. A browser is not an MCP test.

**If not:** go to [The client cannot connect](faq.md#the-client-cannot-connect).

## 3. Connect one client

Choose exactly one option in the [setup guide](setup.md#3-choose-one-client):

- Claude Code;
- Codex;
- Gemini CLI or Gemini Code Assist.

**You should see:** a server named `engram` with two tools, `recall` and `remember`.

You do not need to configure all three clients before continuing.

## 4. Install the memory behavior

Copy the **Ready-to-paste instruction** block from the
[client protocol](client-protocol.md#ready-to-paste-instruction) into the selected client's
instructions.

**Why:** MCP carries calls, but Engram cannot observe the conversation passively. Without this
protocol, the server works while the client may simply forget to use it.

## 5. Verify

Ask the client to call `recall` with:

```text
query = "Engram startup and next action"
scope = "project/engram"
```

**You should see:**

- a structured capsule;
- `notes.recall_complete = true`, or a warning explaining why recall is incomplete;
- probably empty lists on a new database.

Then request one non-sensitive test `remember`:

```text
statement = "The Engram connection test is complete."
kind = "episode"
scope = "project/engram"
subject_keys = ["engram:connection-test"]
```

Recall the same query from the same client.

**You should see:** the candidate in `own_pending`. It must not appear in `current`; that is the
normal quarantine policy.

## Done

- For daily use: [user guide](user-guide.md).
- To choose between Engram, Datacron, and Cortex:
  [trilogy guide](datacron-cortex.md).
- To attest, migrate, reindex, or consolidate:
  [operator guide](operator-guide.md).
- For a specific symptom: [FAQ](faq.md).
