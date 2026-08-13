# Setup

[Français](../fr/setup.md) | [English](setup.md)

> **Goal:** install Engram and connect one MCP client.<br>
> **Time:** 10 to 20 minutes.<br>
> **Result:** the client displays `recall` and `remember`.<br>
> **Verified with:** Engram `2026.0730.02` on 2026-08-13.

Every command in sections 1 and 3 is identical on Windows, macOS, and Linux. Section 2 is the only
place where the operating systems differ, and it is signposted.

## 1. Install Engram

Engram requires Git, `uv` 0.12.1 or newer, Python 3.13+, and SQLite 3.51.3+ inside that Python's
`sqlite3` module.

```text
git --version
uv --version
```

**Expected result:** both commands print a version. Otherwise install
[Git](https://git-scm.com/downloads) or
[`uv`](https://docs.astral.sh/uv/getting-started/installation/). `uv` 0.12.1 is the first release
that knows the `3.14.6` build; earlier releases only know builds up to `3.14.3`.

```text
git clone https://github.com/VBlackJack/Engram.git
cd Engram
uv sync --python 3.14.6
uv run --python 3.14.6 engram init
```

**Expected result:** `engram init` prints the file it wrote, the database path that file resolves
to, the endpoint, and `Next: engram doctor`.

`engram init` writes the starting configuration from the copy packaged inside the distribution. It
needs no checkout, no shell-specific syntax, and no `engram.example.toml` beside it, so it behaves
the same from a wheel install and on every operating system. It refuses to replace an existing
`engram.toml`; `--force` replaces one deliberately.

Then verify the installation before starting anything:

```text
uv run --python 3.14.6 engram doctor
```

**Expected result:** `[ ok ]` for `python`, `sqlite`, and `configuration`. `database` and `daemon`
warn until the daemon runs for the first time. Each failing line prints the command that repairs
it, and `engram doctor` exits non-zero only when a check failed.

If SQLite is too old, stop and follow
[Windows and SQLite](installation-windows.md). A recent `sqlite3` command on PATH does not replace
the library loaded by Python.

## 2. Configure and start

For a new installation, the values `engram init` wrote are enough:

- loopback endpoint `127.0.0.1:8377/mcp`;
- local `engram.db` database;
- FTS retrieval;
- Datacron writes disabled.

> **STOP for recovery or upgrade:** when upgrading or reusing an earlier database, do not run the
> next command. The new `engram.toml` created in step 1 is expected. For an earlier database, back
> up and follow
> [Migrate an existing database](operator-guide.md#migrate-an-existing-database).

### Windows: the logon task

For a new database only, install the logon autostart:

```text
uv run --python 3.14.6 engram setup autostart --install
```

**Expected result:** the command exits 0 and prints JSON whose `started` field is `true`. Engram is
running now and will start again at every logon. **No terminal has to stay open:** the task runs the
windowed interpreter, so there is no window left to close by accident.

Verify:

```text
uv run --python 3.14.6 engram setup autostart --status
uv run --python 3.14.6 engram doctor
```

**Expected result:** `installed` is `true` and `daemon_running` is `true`, and `engram doctor`
reports `[ ok ] daemon: serving, pid ...` and `[ ok ] endpoint: http://127.0.0.1:8377/mcp accepts`.

What the command does, and what it does not:

| Action | Effect | Exit code |
|---|---|---|
| `--install` | Registers or updates **one** task for this `engram.toml`, then starts the daemon when the database is free. Repeating the command converges instead of adding a second task. | `0` |
| `--status` | Writes nothing. The answer is in the JSON, never in the exit code. The `interpreter_present` field says whether the interpreter **recorded in the task** still exists: `installed: true` with `interpreter_present: false` describes a task that will no longer start. | `0` in every case |
| `--uninstall` | Removes the task. On an already absent task, `removed` is `false`. | `0` |

#### Taking over an earlier installation

If Engram was already started at logon by something else — a scheduled task created by hand, a
launch script — `--install` **refuses** and names the task it found:

```text
uv run --python 3.14.6 engram setup autostart --install
```

**Expected result:** a non-zero exit code and a message of the form
`Another registered task would open this database: 'Engram Local Daemon' ...`.

Detection compares **the database being targeted**, not the name of the task. A task that goes
through a wrapper script does not announce its configuration: in that case the command does not
conclude that there is no conflict, it reports the uncertainty and refuses anyway. An unknown
answer is not a negative one.

To take the installation over:

```text
uv run --python 3.14.6 engram setup autostart --install --replace
```

**Expected result:** exit 0, and JSON whose `disabled` field names the task that was taken over.

```text
uv run --python 3.14.6 engram setup autostart --status
```

**Expected result:** `installed` is `true`, `daemon_running` is `true`, and `conflicts` is empty.

> **The task taken over is disabled, not deleted.** Its definition stays intact in the scheduler.
> To go back, re-enable it from PowerShell and then disable Engram's own:
>
> ```powershell
> Enable-ScheduledTask -TaskName "<name reported in disabled>"
> ```
>
> Deleting it for good stays your gesture, never the command's.

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
  operating system, use
  [Install as a service on macOS and Linux](installation-unix.md);
- to target one specific configuration file, add `--config <path>` before the subcommand. The task
  records that path literally; it inherits no environment variable.

### macOS and Linux: systemd or launchd

`engram setup autostart` builds a Windows scheduled task and nothing else; on any other platform it
refuses with exit code `2` rather than pretending to have installed something. The equivalent unit
files, ready to fill in, are in
[Install as a service on macOS and Linux](installation-unix.md): a systemd **user** unit for Linux
and a launchd **LaunchAgent** for macOS, both running `engram serve` with an absolute `--config`
path.

Until you install one of those, run the daemon in a terminal:

```text
uv run --python 3.14.6 engram serve
```

### Stopping the daemon cleanly

Whatever started it — logon task, systemd, launchd, or a terminal — one command asks the daemon
that owns this database to close it and exit, then waits and reports what actually happened:

```text
uv run --python 3.14.6 engram stop
```

**Expected result:** JSON with `"stopped": true`, and **both `engram.db-wal` and `engram.db-shm`
disappear**. That is the observable proof of a clean shutdown: SQLite only removes those two files
when the last connection closes. When no process holds the database, the command reports
`"requested": false, "stopped": true` and changes nothing.

`engram stop` does not report success it did not achieve. It waits on the ownership lock, and if
the daemon still holds the database when the wait runs out it fails and tells you so, leaving the
request in place.

Underneath, the mechanism is a sentinel file: a daemon with no console can receive neither `Ctrl+C`
nor `Ctrl+Break`, so `engram stop` touches an empty `<database>.stop` beside the configured
`[database].path` — the same directory as the `<database>.lock` — and the daemon clears it on the
way out. Use the command rather than the file: the command resolves that path from the very
configuration the daemon loaded, while a hand-typed path that matches no shipped configuration
writes the request where nothing is watching and looks like it worked.

The right to stop Engram is therefore exactly the right to write in its database directory, which
is already the right to corrupt it. No port exposes that capability.

A forgotten stop request does not prevent the next start: the daemon clears the one it finds
**after** taking the lock. A second `serve` started by mistake fails on the lock without touching a
stop request meant for the daemon that owns it.

For foreground diagnostics, `engram serve` is still available and behaves as before: it holds the
terminal and stops when it closes or on `Ctrl+C`. Stop the daemon first, because only one Engram
process may write this database.

Test with an MCP client, not a browser.

## 3. Choose one client

### The one command that configures any of them

```text
uv run --python 3.14.6 engram setup client claude
uv run --python 3.14.6 engram setup client codex
uv run --python 3.14.6 engram setup client gemini
```

Run exactly one. Each writes that vendor's MCP configuration using **the endpoint from the
configuration you loaded**, not the `8377` this page prints, so an installation that moved its port
stays correct without anyone noticing the difference:

| Client | File written | Instructions file for `--protocol` |
| --- | --- | --- |
| `claude` | `.mcp.json` in the current directory | `CLAUDE.md` in the current directory |
| `codex` | `~/.codex/config.toml` | `AGENTS.md` in the current directory |
| `gemini` | `~/.gemini/settings.json` | `GEMINI.md` in the current directory |

| Option | Effect |
| --- | --- |
| `--protocol` | Also appends the [client protocol](client-protocol.md) to that client's instructions file, once. Running it again changes nothing. |
| `--print` | Shows the block instead of writing it. Nothing is modified. |
| `--force` | Replaces an existing `engram` entry that names a **different** endpoint. Without it, the command refuses rather than silently repointing a working client. |

It merges, it does not clobber: other MCP servers, unrelated keys, and TOML comments in those files
survive. Re-running it when the entry is already correct writes nothing and says so.

> **The write is not atomic, deliberately.** Engram writes *through* your existing file rather than
> replacing it, which is what keeps its permissions, its owner, its access control list, its
> extended attributes, its NTFS alternate data streams and every hard link pointing at it — a
> replacement would silently drop all of those. The price is that a machine that loses power, or a
> process killed, during the write can leave the file partly written. The window is one small write
> to a local file, and the next run refuses a file it cannot parse rather than appending to it.
>
> If you cannot accept even that, use `--print` and paste the block yourself: it writes nothing at
> all. Copy the file first if it is precious and not under version control.

**Expected result:** the file exists and contains your endpoint. Restart the client, then go to
[verification](#4-functional-verification).

The rest of this section is the hand-edited fallback, for a client this command does not cover or a
file you would rather write yourself. The options are independent; configure one.

### Option A: Claude

#### Claude Code

```text
claude mcp add --transport http engram http://127.0.0.1:8377/mcp --scope user
claude mcp list
```

Equivalent project `.mcp.json` — this is what `engram setup client claude` writes:

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
startup_timeout_sec = 10
tool_timeout_sec = 30
```

**Never add the `required` key to this block.** OpenAI defines it as failing Codex startup when the
server cannot initialise, so a memory broker that is merely down would take your whole assistant
with it. The block above is exactly what `engram setup client codex` writes, and it omits that key
deliberately.

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

Before opening the client, confirm the server side once:

```text
uv run --python 3.14.6 engram doctor
```

**Expected result:** no `[fail]` line, `daemon` reports `serving`, and `endpoint` reports that your
URL accepts connections. If the client still cannot connect after that, the problem is in the
client's own configuration file, not in Engram.

In the selected client:

1. call `recall` with a context query and `scope="user"`;
2. call `remember` with a non-sensitive test episode;
3. recall the same query from the same client;
4. verify that the candidate appears in `own_pending`, not `current`.

The candidate is deliberately quarantined. Another client must not see it in its own
`own_pending`.

**Next step:** follow the [user guide](user-guide.md). If an expected result is missing, open the
[FAQ](faq.md) before changing several settings.
