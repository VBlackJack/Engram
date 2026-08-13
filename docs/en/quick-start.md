# Five-minute quick start

[Français](../fr/quick-start.md) | [English](quick-start.md)

> **Goal:** start Engram and verify one memory.<br>
> **Time:** 5 to 10 minutes, excluding Python installation.<br>
> **Risk:** low with a new database. For an existing database, use the
> [operator guide](operator-guide.md).<br>
> **End result:** one MCP client can call `recall` and `remember`.

Keep only the current step open. Detailed explanations are linked and are not required to finish
this path.

## 1. Install

This path requires Git and `uv` 0.12.1 or newer. Check them first:

```text
git --version
uv --version
```

**You should see:** both version numbers. Otherwise install
[Git](https://git-scm.com/downloads) or
[`uv`](https://docs.astral.sh/uv/getting-started/installation/) before continuing.

Every command on this page is identical on Windows, macOS, and Linux.

> **STOP for recovery or upgrade:** when reusing an Engram checkout, configuration, or database,
> do not run this block. `engram init` refuses to overwrite an existing `engram.toml`, but the
> safe path for an earlier database is the
> [operator guide](operator-guide.md#migrate-an-existing-database).

```text
git clone https://github.com/VBlackJack/Engram.git
cd Engram
uv sync --python 3.14.6
uv run --python 3.14.6 engram init
```

**You should see:** `Wrote .../engram.toml`, the database path it resolves to, the endpoint, and
`Next: engram doctor`.

`engram init` writes the starting configuration from the copy packaged inside the distribution, so
it works from a wheel install as well as from this checkout, and on every operating system. It
refuses to replace an existing `engram.toml` unless you pass `--force`.

The `3.14.6` pin is not a stylistic choice. The SQLite a runtime links is decided per build, not per
Python version, and most distributions link one too old to run Engram; `3.14.6` is measured to clear
the requirement on Windows and Linux. Substituting an interpreter you already have will most likely
fail the SQLite check `engram doctor` runs in the next step. See
[Windows and SQLite](installation-windows.md) for the measurements. Installing it requires `uv`
0.12.1 or newer.

## 2. Check the installation

```text
uv run --python 3.14.6 engram doctor
```

**You should see:** an `[ ok ]` line for `python`, for `sqlite` (`3.51.3` or newer), and for
`configuration`. `database` and `daemon` warn until the next step; a warning is not a failure.
Every failing line prints the command that repairs it, and the command exits non-zero only when
something failed.

**If not:** apply the repair the failing line names. For SQLite specifically, follow
[Windows and SQLite](installation-windows.md).

## 3. Start

```text
uv run --python 3.14.6 engram serve
```

**You should see:** the process stays active and the local endpoint is
`http://127.0.0.1:8377/mcp`.

Keep that terminal open. A browser is not an MCP test. `engram serve` in a terminal is the right
shape for a first run; to keep Engram running without one, install the startup integration once
you have finished this page:

- **Windows:** `uv run --python 3.14.6 engram setup autostart --install` registers a logon task
  that starts the daemon with no console window. Without it, Engram stops at the next logoff.
  Details in the [setup guide](setup.md#2-configure-and-start).
- **macOS / Linux:** `engram setup autostart` is Windows-only and exits `2` elsewhere. Use the
  ready-made systemd unit or launchd agent in
  [Install as a service on macOS and Linux](installation-unix.md).

To stop the daemon later, from any installation: `uv run --python 3.14.6 engram stop`.

**If not:** go to [The client cannot connect](faq.md#the-client-cannot-connect).

## 4. Connect one client

The fastest path writes the vendor file for you, using the endpoint from your own configuration:

```text
uv run --python 3.14.6 engram setup client claude --protocol
```

Replace `claude` with `codex` or `gemini`. `--protocol` also appends the session protocol of
step 5 to `CLAUDE.md`, `AGENTS.md`, or `GEMINI.md`. Add `--print` to see the block without writing
anything.

The hand-written equivalents, and the Claude Desktop limitation, are in the
[setup guide](setup.md#3-choose-one-client).

**You should see:** a server named `engram` with two tools, `recall` and `remember`.

You do not need to configure all three clients before continuing.

## 5. Install the memory behavior

`engram setup client --protocol` already did this. If you configured the client by hand, copy the
**Ready-to-paste instruction** block from the
[client protocol](client-protocol.md#ready-to-paste-instruction) into the selected client's
instructions.

**Why:** MCP carries calls, but Engram cannot observe the conversation passively. Without this
protocol, the server works while the client may simply forget to use it.

## 6. Verify

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

- To keep Engram running across reboots: [setup guide](setup.md#2-configure-and-start) on Windows,
  [macOS and Linux service install](installation-unix.md) elsewhere.
- For daily use: [user guide](user-guide.md).
- To choose between Engram, Datacron, and Cortex:
  [trilogy guide](datacron-cortex.md).
- To attest, migrate, reindex, or consolidate:
  [operator guide](operator-guide.md).
- For a specific symptom: [FAQ](faq.md). Run `engram doctor` first; it names the repair.
