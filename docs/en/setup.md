# Setup

[Francais](../fr/setup.md) | [English](setup.md)

> **Goal:** install Engram and connect one MCP client.<br>
> **Time:** 10 to 20 minutes.<br>
> **Result:** the client displays `recall` and `remember`.<br>
> **Verified with:** Engram `2026.0730.02` on 2026-07-30.

## 1. Install Engram

Engram requires Git, `uv`, Python 3.13+, and SQLite 3.51.3+ inside that Python's `sqlite3` module.

```powershell
git --version
uv --version
```

**Expected result:** both commands print a version. Otherwise install
[Git](https://git-scm.com/downloads) or
[`uv`](https://docs.astral.sh/uv/getting-started/installation/).

```powershell
git clone https://github.com/VBlackJack/Engram.git
Set-Location Engram
uv sync --python 3.14.6
uv run --python 3.14.6 python -c "import sqlite3; print(sqlite3.sqlite_version)"
if (Test-Path -LiteralPath "engram.toml") { throw "Existing Engram configuration: stop" }
if (Test-Path -LiteralPath "engram.db") { throw "Existing Engram database: stop" }
Copy-Item engram.example.toml engram.toml -ErrorAction Stop
```

**Expected result:** SQLite reports `3.51.3` or newer and `engram.toml` exists.

If the version is too old, stop and follow
[Windows and SQLite](installation-windows.md). A recent `sqlite3.exe` on PATH does not replace the
library loaded by Python.

## 2. Configure and start

For a new installation, the safe values in `engram.example.toml` are enough:

- loopback endpoint `127.0.0.1:8377/mcp`;
- local `engram.db` database;
- FTS retrieval;
- Datacron writes disabled.

> **STOP for recovery or upgrade:** when upgrading or reusing an earlier database, do not run the
> next command. The new `engram.toml` created in step 1 is expected. For an earlier database, back
> up and follow
> [Migrate an existing database](operator-guide.md#migrate-an-existing-database).

For a new database only, install the logon autostart:

```powershell
uv run --python 3.14.6 engram setup autostart --install
```

**Expected result:** the command exits 0 and prints JSON whose `started` field is `true`. Engram is
running now and will start again at every logon. **No terminal has to stay open:** the task runs the
windowed interpreter, so there is no window left to close by accident.

Verify:

```powershell
uv run --python 3.14.6 engram setup autostart --status
```

**Expected result:** `installed` is `true` and `daemon_running` is `true`.

What the command does, and what it does not:

| Action | Effect | Exit code |
|---|---|---|
| `--install` | Registers or updates **one** task for this `engram.toml`, then starts the daemon when the database is free. Repeating the command converges instead of adding a second task. | `0` |
| `--status` | Writes nothing. The answer is in the JSON, never in the exit code. | `0` in every case |
| `--uninstall` | Removes the task. On an already absent task, `removed` is `false`. | `0` |

### Taking over an earlier installation

If Engram was already started at logon by something else — a scheduled task created by hand, a
launch script — `--install` **refuses** and names the task it found:

```powershell
uv run --python 3.14.6 engram setup autostart --install
```

**Expected result:** a non-zero exit code and a message of the form
`Another registered task would open this database: 'Engram Local Daemon' ...`.

Detection compares **the database being targeted**, not the name of the task. A task that goes
through a wrapper script does not announce its configuration: in that case the command does not
conclude that there is no conflict, it reports the uncertainty and refuses anyway. An unknown
answer is not a negative one.

To take the installation over:

```powershell
uv run --python 3.14.6 engram setup autostart --install --replace
```

**Expected result:** exit 0, and JSON whose `disabled` field names the task that was taken over.

```powershell
uv run --python 3.14.6 engram setup autostart --status
```

**Expected result:** `installed` is `true`, `daemon_running` is `true`, and `conflicts` is empty.

> **The task taken over is disabled, not deleted.** Its definition stays intact in the scheduler.
> To go back: `Enable-ScheduledTask -TaskName "<name reported in disabled>"`, then disable
> Engram's own. Deleting it for good stays your gesture, never the command's.

`--replace` stops the daemon belonging to the task it takes over and **waits for the database lock
to be released**, not for a fixed delay. Replaying `--install --replace` on an already converged
system exits 0 and changes nothing.

As a last resort, `--force` installs despite a conflict or an unresolved answer. Use it only when
you know the other task will not open this database: with two daemons on one database, the second
is the one that dies on the lock.

Worth knowing:

- the task is named after the path of your `engram.toml`. Two separate installations own two
  separate tasks, and neither replaces the other silently;
- `--install` does not start a second daemon when the database is already held. It says so in
  `start_skipped_reason` rather than reporting a success it did not achieve;
- off Windows the command fails explicitly with exit code `2` and changes nothing. On another
  operating system, supervise `engram serve` with the local service manager;
- to target one specific configuration file, add `--config <path>` before the subcommand. The task
  records that path literally; it inherits no environment variable.

For foreground diagnostics, `engram serve` is still available and behaves as before: it holds the
terminal and stops when it closes. Stop the daemon first, because only one Engram process may write
this database.

Test with an MCP client, not a browser.

## 3. Choose one client

The following options are independent. Configure one, then go directly to
[verification](#4-functional-verification).

### Option A: Claude

#### Claude Code

```powershell
claude mcp add --transport http engram http://127.0.0.1:8377/mcp --scope user
claude mcp list
```

Equivalent project `.mcp.json`:

```json
{
  "mcpServers": {
    "engram": {
      "type": "http",
      "url": "http://127.0.0.1:8377/mcp"
    }
  }
}
```

Add the [client protocol](client-protocol.md) to Claude Code user instructions or a local
uncommitted `CLAUDE.md`.

**Expected result:** `claude mcp list` shows `engram` as connected. Reference:
[Claude Code MCP guide](https://code.claude.com/docs/en/mcp).

#### Claude Desktop

Claude Desktop resolves HTTP connectors from remote infrastructure, so `127.0.0.1` on your computer
is unreachable. For Desktop, place Engram behind an authenticated HTTPS proxy, then add that URL
under **Settings > Connectors > Add custom connector**.

Desktop extensions and local MCP servers are a separate mechanism. Engram does not yet ship a
Desktop extension or stdio transport; for this HTTP release, use Claude Code locally or a secured
remote connector. References:
[remote MCP connectors](https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp) and
[desktop versus web connectors](https://support.claude.com/en/articles/11725091-when-to-use-desktop-and-web-connectors).

### Option B: Codex

Add to `~/.codex/config.toml`:

```toml
[mcp_servers.engram]
url = "http://127.0.0.1:8377/mcp"
enabled = true
required = true
startup_timeout_sec = 10
tool_timeout_sec = 30
```

Restart Codex, then add the [client protocol](client-protocol.md) to user instructions or an
`AGENTS.md` at the appropriate scope.

**Expected result:** Codex sees `recall` and `remember` under `engram`. In the desktop app, the same
setting is under **Settings > MCP servers > Add server > Streamable HTTP**. Reference:
[Codex MCP guide](https://developers.openai.com/codex/mcp/).

### Option C: Gemini

Gemini CLI and Gemini Code Assist for VS Code share `~/.gemini/settings.json`:

```json
{
  "mcpServers": {
    "engram": {
      "httpUrl": "http://127.0.0.1:8377/mcp"
    }
  }
}
```

Put the [client protocol](client-protocol.md) in the user or project `GEMINI.md`, then run `/mcp`.
Reload VS Code if needed.

**Expected result:** `/mcp` displays Engram and its two tools. Gemini Code Assist for IntelliJ also
supports MCP, but uses a separate `mcp.json` file in the IDE configuration directory; do not
automatically reuse `~/.gemini/settings.json`. Reference:
[Gemini Code Assist documentation](https://developers.google.com/gemini-code-assist/docs/use-agentic-chat-pair-programmer).

## 4. Functional verification

In the selected client:

1. call `recall` with a context query and `scope="user"`;
2. call `remember` with a non-sensitive test episode;
3. recall the same query from the same client;
4. verify that the candidate appears in `own_pending`, not `current`.

The candidate is deliberately quarantined. Another client must not see it in its own
`own_pending`.

**Next step:** follow the [user guide](user-guide.md). If an expected result is missing, open the
[FAQ](faq.md) before changing several settings.
