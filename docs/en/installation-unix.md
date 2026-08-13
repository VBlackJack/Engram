# Install as a service on macOS and Linux

[Français](../fr/installation-unix.md) | [English](installation-unix.md)

> **Use this page when** you have finished the [quick start](quick-start.md) and want Engram to
> keep running without a terminal.<br>
> **Time:** 10 minutes.<br>
> **Result:** `engram serve` starts at login and is supervised by the system.<br>
> **Verified with:** Engram `2026.0730.02` on 2026-08-13.

## Why this page exists

`engram setup autostart` registers a Windows scheduled task. It is **Windows-only**: on any other
platform it refuses with exit code `2` and changes nothing, rather than reporting an installation
it did not perform. macOS and Linux already have a supervisor per user session, so Engram uses
theirs instead of inventing one.

The engine itself is portable. Everything below runs the same `engram serve` the Windows task runs.

## Before you start

Collect three absolute paths; every unit file below needs them and none of them may be relative:

```text
uv run --python 3.14.6 engram doctor
```

**Expected result:** the `configuration` line prints the absolute path of the `engram.toml` the
loader resolved, and the `database` line prints the absolute database path it points at. Note them.

Then find the executable to run. With a `uv sync` checkout it is inside the project's virtual
environment:

```text
cd /path/to/Engram
uv sync --python 3.14.6
readlink -f .venv/bin/engram
```

**Expected result:** an absolute path such as `/home/you/Engram/.venv/bin/engram`. Use that path in
the unit files. Calling the `.venv` executable directly, rather than going through `uv run`, keeps
the supervised process free of a parent that may itself resolve or lock the project.

Throughout this page, replace:

- `/home/you/Engram/.venv/bin/engram` with the path you just printed;
- `/home/you/Engram/engram.toml` with your configuration path;
- `/home/you/Engram` with the directory that configuration lives in.

`--config` is a global option and comes **before** the subcommand: `engram --config <path> serve`.

## Linux: a systemd user unit

A **user** unit, not a system one. Engram writes into your home directory, holds one exclusive lock
on a database owned by you, and listens only on loopback; running it as root buys nothing and makes
the database unreadable by the account that uses it.

Create `~/.config/systemd/user/engram.service`:

```ini
[Unit]
Description=Engram local MCP memory broker
Documentation=https://github.com/VBlackJack/Engram
After=default.target

[Service]
Type=simple
WorkingDirectory=/home/you/Engram
ExecStart=/home/you/Engram/.venv/bin/engram --config /home/you/Engram/engram.toml serve
ExecStop=/home/you/Engram/.venv/bin/engram --config /home/you/Engram/engram.toml stop
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
```

`ExecStop` runs `engram stop`, which asks the daemon to close the database and waits until the
ownership lock is released. That is what turns a stop into a clean SQLite shutdown instead of a
signal that may leave the write-ahead log behind.

Enable and start it:

```bash
systemctl --user daemon-reload
systemctl --user enable --now engram.service
```

**Expected result:** the next command reports `active (running)`.

| I want to... | Command |
| --- | --- |
| Check status | `systemctl --user status engram.service` |
| Start now | `systemctl --user start engram.service` |
| Stop now | `systemctl --user stop engram.service` |
| Restart | `systemctl --user restart engram.service` |
| Start at login | `systemctl --user enable engram.service` |
| Stop starting at login | `systemctl --user disable engram.service` |
| Read the service log | `journalctl --user -u engram.service -f` |

Verify with Engram's own diagnosis rather than with systemd's opinion of the process:

```bash
/home/you/Engram/.venv/bin/engram --config /home/you/Engram/engram.toml doctor
```

**Expected result:** `daemon` reports `serving`, and `endpoint` reports that the URL accepts
connections.

A user unit stops when your last session closes unless lingering is enabled. To keep Engram running
between logins on a machine you administer:

```bash
loginctl enable-linger "$USER"
```

Engram's own log file stays where `[logging].path` points; `journalctl` only shows what the process
wrote to its console.

## macOS: a launchd LaunchAgent

A **LaunchAgent** in your home directory, not a LaunchDaemon: the same reasoning as above, and
`~/Library/LaunchAgents` is the location that runs as you.

Create `~/Library/LaunchAgents/com.github.vblackjack.engram.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.github.vblackjack.engram</string>

    <key>ProgramArguments</key>
    <array>
        <string>/Users/you/Engram/.venv/bin/engram</string>
        <string>--config</string>
        <string>/Users/you/Engram/engram.toml</string>
        <string>serve</string>
    </array>

    <key>WorkingDirectory</key>
    <string>/Users/you/Engram</string>

    <key>RunAtLoad</key>
    <true/>

    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
    </dict>

    <key>StandardOutPath</key>
    <string>/Users/you/Engram/logs/launchd.out.log</string>

    <key>StandardErrorPath</key>
    <string>/Users/you/Engram/logs/launchd.err.log</string>
</dict>
</plist>
```

`KeepAlive`/`SuccessfulExit=false` restarts Engram when it crashes but leaves it stopped after a
deliberate `engram stop`, which exits `0`. Without that dictionary, launchd would immediately
restart the daemon you just asked to stop.

Load and start it:

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.github.vblackjack.engram.plist
launchctl kickstart -k gui/$(id -u)/com.github.vblackjack.engram
```

| I want to... | Command |
| --- | --- |
| Check status | `launchctl print gui/$(id -u)/com.github.vblackjack.engram` |
| List briefly | `launchctl list \| grep engram` |
| Start / restart now | `launchctl kickstart -k gui/$(id -u)/com.github.vblackjack.engram` |
| Stop the process | `/Users/you/Engram/.venv/bin/engram --config /Users/you/Engram/engram.toml stop` |
| Enable at login | `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.github.vblackjack.engram.plist` |
| Disable and unload | `launchctl bootout gui/$(id -u)/com.github.vblackjack.engram` |

On macOS releases that predate `bootstrap`, the equivalents are `launchctl load -w <plist>` and
`launchctl unload -w <plist>`. Prefer `bootstrap`/`bootout` where both exist.

Verify the same way:

```bash
/Users/you/Engram/.venv/bin/engram --config /Users/you/Engram/engram.toml doctor
```

## Stopping and restarting for an operator procedure

Every procedure in the [operator guide](operator-guide.md) that requires the daemon to be stopped
is satisfied by:

```text
engram --config /absolute/path/engram.toml stop
```

Restart it afterwards with `systemctl --user start engram.service` or
`launchctl kickstart -k gui/$(id -u)/com.github.vblackjack.engram`. Under systemd, `systemctl
--user stop` already runs `engram stop` through `ExecStop`; either entry point is correct.

## If it does not start

1. Run `engram --config <absolute path> doctor` by hand, as the same user. It names the repair for
   every failing check.
2. Confirm the paths in the unit file are absolute and still exist. A unit inherits almost none of
   your shell environment, so `~`, `$HOME`, and a relative `engram.toml` are the usual cause.
3. Read the service log: `journalctl --user -u engram.service -n 50` or the `StandardErrorPath`
   file on macOS.
4. Check that nothing else already owns the database. `engram doctor` reports the owning pid, and a
   second daemon fails on the lock by design.
5. For a symptom the diagnosis does not explain, use the [FAQ](faq.md).
