# Setup

[Francais](../fr/setup.md) | [English](setup.md)

## 1. Install Engram

Engram requires Python 3.13+ and SQLite 3.51.3+ inside that Python's `sqlite3` module. Install with
the Python 3.14.3 runtime managed by `uv`, then verify SQLite: a Python build may still embed an
older library. CI explicitly replaces it with official SQLite 3.53.3.

```powershell
git clone https://github.com/VBlackJack/Engram.git
Set-Location Engram
uv sync --extra dev --python 3.14.3
uv run --python 3.14.3 python -c "import sqlite3; print(sqlite3.sqlite_version)"
Copy-Item engram.example.toml engram.toml
```

If the displayed version is below `3.51.3`, follow
[installation-windows.md](installation-windows.md) or choose a newer Python runtime.

## 2. Configure and start

Edit `engram.toml`. The server accepts only loopback IP literals (`127.0.0.1` or `::1`), stores the
database in `engram.db`, uses FTS, and disables Datacron writes by default.

When upgrading an existing configuration, update the capsule limits before restarting:

```toml
[capsule]
default_token_budget = 4800
min_token_budget = 1200
max_token_budget = 6000
```

The minimum must be at least 1200 and the maximum must be at least the default. Engram rejects
older, smaller limits at startup because they cannot hold the mandatory bounded response envelope.

```powershell
uv run --python 3.14.3 engram serve
```

The endpoint is `http://127.0.0.1:8377/mcp`. Only one Engram process should use this database as a
writer. Test with an MCP client, not a browser: the endpoint speaks MCP Streamable HTTP.

## 3. Connect Claude

### Claude Code

The official CLI supports HTTP transport:

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

Add the text from [client-protocol.md](client-protocol.md) to Claude Code user instructions or to a
local uncommitted `CLAUDE.md`. Syntax is documented in the
[Claude Code MCP guide](https://code.claude.com/docs/en/mcp).

### Claude Desktop

Claude Desktop configures remote HTTP servers under **Settings > Connectors > Add custom
connector**. The connector is resolved by Claude's remote infrastructure, so `127.0.0.1` on your
computer is unreachable. For Desktop, publish Engram behind an authenticated HTTPS proxy (private
tunnel/VPN with access control), then register `https://engram.example/mcp`. The historical
`claude_desktop_config.json` file is not the remote-connector mechanism. See the
[remote MCP connector guide](https://support.claude.com/en/articles/11503834-build-custom-connectors-via-remote-mcp-servers).

Claude Code is the direct path for strictly local use.

## 4. Connect Codex

Add the server to `~/.codex/config.toml`:

```toml
[mcp_servers.engram]
url = "http://127.0.0.1:8377/mcp"
enabled = true
required = true
startup_timeout_sec = 10
tool_timeout_sec = 30
```

Restart Codex. In the desktop app, the equivalent is under **Settings > MCP servers > Add server >
Streamable HTTP**. Add [client-protocol.md](client-protocol.md) to user instructions or a suitable
local `AGENTS.md`. The official reference is the [Codex MCP guide](https://developers.openai.com/codex/mcp/).

## 5. Connect Gemini

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

Run `/mcp` in Gemini CLI. Reload the VS Code window if the server is missing. Put the text from
[client-protocol.md](client-protocol.md) in the user or project `GEMINI.md`. Gemini Code Assist
agent mode for IntelliJ does not currently support these MCP tools; use Gemini CLI or VS Code. See
the [Gemini Code Assist documentation](https://developers.google.com/gemini-code-assist/docs/use-agentic-chat-pair-programmer).

## 6. Functional verification

In each client:

1. call `recall` with a context query and `scope="user"`;
2. call `remember` with a non-sensitive test fact;
3. recall the same query from the same client and verify `own_pending`;
4. recall from another client and verify that the candidate is absent from its `own_pending`.

The candidate does not enter `current` until it follows the attestation path.
