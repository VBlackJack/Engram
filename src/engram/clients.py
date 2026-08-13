# Copyright 2026 Julien Bombled
# SPDX-License-Identifier: Apache-2.0

"""Connect an MCP client to this Engram without hand-editing a vendor file.

Connecting used to mean opening a vendor configuration in an editor, typing an
endpoint that had to match a port nobody had printed, and then pasting a
twenty-five line protocol into a second file. Two hand transcriptions stand
between an installed Engram and a working one, and the first symptom of getting
either wrong is a client that simply never mentions memory.

The endpoint is therefore read from the configuration the daemon itself loads,
never assumed, and every write merges into whatever the file already contains: a
user with three MCP servers keeps three.
"""

from __future__ import annotations

import json
import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .config import format_endpoint
from .resources import client_protocol_text

if TYPE_CHECKING:  # pragma: no cover
    from .config import AppConfig

SERVER_KEY = "engram"


class ClientKind(StrEnum):
    """The MCP clients this project documents a configuration for."""

    CLAUDE = "claude"
    CODEX = "codex"
    GEMINI = "gemini"


class ClientConfigError(RuntimeError):
    """Raised when a vendor configuration cannot be read or would be clobbered."""


@dataclass(frozen=True, slots=True)
class ClientPlan:
    """What connecting one client would write, and where."""

    kind: ClientKind
    config_path: Path
    instructions_path: Path
    block: str
    already_correct: bool


def endpoint_url(config: AppConfig) -> str:
    """Return the endpoint a client must be pointed at for this configuration."""
    return format_endpoint(config.server.host, config.server.port, config.server.path)


def plan_client(kind: ClientKind, config: AppConfig, *, home: Path | None = None) -> ClientPlan:
    """Describe the change connecting one client would make, without making it."""
    root = Path.home() if home is None else home
    url = endpoint_url(config)
    if kind is ClientKind.CLAUDE:
        # Project scope: a file beside the work, which is also the form the
        # vendor documents for sharing a server with a repository.
        path = Path.cwd() / ".mcp.json"
        return ClientPlan(
            kind=kind,
            config_path=path,
            instructions_path=Path.cwd() / "CLAUDE.md",
            block=_render_json_block({"type": "http", "url": url}),
            already_correct=_json_entry(path, ("mcpServers", SERVER_KEY))
            == {"type": "http", "url": url},
        )
    if kind is ClientKind.GEMINI:
        path = root / ".gemini" / "settings.json"
        return ClientPlan(
            kind=kind,
            config_path=path,
            instructions_path=Path.cwd() / "GEMINI.md",
            block=_render_json_block({"httpUrl": url}),
            already_correct=_json_entry(path, ("mcpServers", SERVER_KEY)) == {"httpUrl": url},
        )
    path = root / ".codex" / "config.toml"
    return ClientPlan(
        kind=kind,
        config_path=path,
        instructions_path=Path.cwd() / "AGENTS.md",
        block=_render_codex_block(url),
        already_correct=_codex_declares(path, url),
    )


def connect(plan: ClientPlan, *, force: bool) -> bool:
    """Write the endpoint into the vendor configuration, and report whether it changed."""
    if plan.already_correct:
        return False
    if plan.kind is ClientKind.CODEX:
        _write_codex(plan, force=force)
    else:
        _write_json_client(plan, force=force)
    return True


def install_protocol(plan: ClientPlan) -> bool:
    """Append the session protocol to the client's instructions, once."""
    protocol = client_protocol_text()
    existing = (
        plan.instructions_path.read_text(encoding="utf-8")
        if plan.instructions_path.is_file()
        else ""
    )
    if protocol.strip() in existing:
        return False
    separator = "" if not existing or existing.endswith("\n\n") else "\n"
    plan.instructions_path.parent.mkdir(parents=True, exist_ok=True)
    plan.instructions_path.write_text(
        f"{existing}{separator}\n{protocol.strip()}\n",
        encoding="utf-8",
    )
    return True


def _render_json_block(entry: dict[str, str]) -> str:
    return json.dumps({"mcpServers": {SERVER_KEY: entry}}, indent=2) + "\n"


def _render_codex_block(url: str) -> str:
    # `required` is deliberately absent. The vendor defines it as failing Codex
    # startup when the server cannot initialise, which would make a memory
    # broker that is merely down take the whole assistant with it.
    return (
        f"[mcp_servers.{SERVER_KEY}]\n"
        f'url = "{url}"\n'
        "enabled = true\n"
        "startup_timeout_sec = 10\n"
        "tool_timeout_sec = 30\n"
    )


def _load_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8") or "{}")
    except (OSError, json.JSONDecodeError) as exc:
        raise ClientConfigError(f"{path} is not readable JSON: {exc}") from exc
    if not isinstance(loaded, dict):
        raise ClientConfigError(f"{path} does not contain a JSON object")
    return loaded


def _json_entry(path: Path, keys: tuple[str, ...]) -> object:
    try:
        current: object = _load_json(path)
    except ClientConfigError:
        return None
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _write_json_client(plan: ClientPlan, *, force: bool) -> None:
    document = _load_json(plan.config_path)
    servers = document.get("mcpServers")
    if servers is None:
        servers = {}
    if not isinstance(servers, dict):
        raise ClientConfigError(f"{plan.config_path} has a non-object 'mcpServers' entry")
    if SERVER_KEY in servers and not force:
        raise ClientConfigError(
            f"{plan.config_path} already configures '{SERVER_KEY}' with a different endpoint. "
            "Pass --force to replace it, or --print to see the block and merge it yourself."
        )
    servers[SERVER_KEY] = json.loads(plan.block)["mcpServers"][SERVER_KEY]
    document["mcpServers"] = servers
    plan.config_path.parent.mkdir(parents=True, exist_ok=True)
    plan.config_path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")


def _codex_declares(path: Path, url: str) -> bool:
    """Return whether Codex already names exactly this endpoint."""
    entry = _codex_entry(path)
    return entry is not None and entry.get("url") == url


def _codex_entry(path: Path) -> Mapping[str, object] | None:
    """Return the table Codex already declares for Engram, or None when it has none.

    Reading only the url conflated three different states: no table, a table
    naming another endpoint, and a table that exists without a url. The last one
    read as "nothing is configured", so a second table of the same name was
    appended -- which TOML forbids -- and the command reported success over a
    file Codex can no longer parse. A document that does not parse is likewise
    not an absent table: appending to it would confirm a configuration nobody
    can load.
    """
    if not path.is_file():
        return None
    try:
        document = tomllib.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ClientConfigError(f"{path} cannot be read: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ClientConfigError(
            f"{path} is not valid TOML: {exc}. Repair it before connecting a client; "
            "appending to it would leave Codex unable to start."
        ) from exc
    servers = document.get("mcp_servers")
    if not isinstance(servers, dict):
        return None
    entry = servers.get(SERVER_KEY)
    return entry if isinstance(entry, Mapping) else None


def _write_codex(plan: ClientPlan, *, force: bool) -> None:
    """Append the table, because a TOML document has no safe in-place editor here.

    Appending a uniquely named table is always valid TOML whatever precedes it,
    so the rest of the file is never reparsed or rewritten. Replacing one that
    already exists is refused rather than attempted: a textual substitution
    inside a document this project does not own is how comments and ordering get
    silently destroyed.
    """
    del force
    if _codex_entry(plan.config_path) is not None:
        raise ClientConfigError(
            f"{plan.config_path} already declares [mcp_servers.{SERVER_KEY}]. TOML forbids the "
            "same table twice, so appending a second one would leave a file Codex cannot parse. "
            "It is not rewritten even with --force, because substituting a table textually in a "
            "document this project does not own loses comments and ordering. Edit the url in "
            "place, or run with --print and merge the block yourself."
        )
    existing = plan.config_path.read_text(encoding="utf-8") if plan.config_path.is_file() else ""
    separator = "" if not existing or existing.endswith("\n\n") else "\n"
    plan.config_path.parent.mkdir(parents=True, exist_ok=True)
    plan.config_path.write_text(f"{existing}{separator}{plan.block}", encoding="utf-8")


def default_home() -> Path:
    """Return the home directory vendor configurations are resolved against."""
    override = os.environ.get("ENGRAM_CLIENT_HOME")
    return Path(override) if override else Path.home()
