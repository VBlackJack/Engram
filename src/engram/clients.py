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
import tempfile
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
    protocol = client_protocol_text().strip()
    existing = plan.instructions_path.read_bytes() if plan.instructions_path.is_file() else b""
    if protocol.encode("utf-8") in existing.replace(b"\r\n", b"\n"):
        return False
    _replace_atomically(
        plan.instructions_path,
        existing + _line_separated_appendix(existing, f"{protocol}\n"),
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
    _replace_atomically(
        plan.config_path,
        (json.dumps(document, indent=2) + "\n").encode("utf-8"),
    )


def _codex_declares(path: Path, url: str) -> bool:
    """Return whether Codex already names exactly this endpoint."""
    entry = _codex_entry(path)
    return entry is not None and entry.get("url") == url


def _codex_entry(path: Path) -> Mapping[str, object] | None:
    """Return the table Codex already declares for Engram, or None when it has none.

    Absent and structurally invalid are answered separately, because collapsing
    them is what produced the defect twice. Reading only the url made a table
    without one read as "nothing configured"; testing the container with
    isinstance made `mcp_servers = "legacy"` and `mcp_servers.engram = "legacy"`
    read the same way. Each time, a second table of the same name was appended --
    which TOML forbids -- and the command reported success over a file Codex can
    no longer parse. A key that is missing returns None; a value of the wrong
    shape, and a document that does not parse at all, raise.
    """
    if not path.is_file():
        return None
    try:
        document = tomllib.loads(path.read_bytes().decode("utf-8"))
    except (OSError, UnicodeDecodeError) as exc:
        raise ClientConfigError(f"{path} cannot be read: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ClientConfigError(
            f"{path} is not valid TOML: {exc}. Repair it before connecting a client; "
            "appending to it would leave Codex unable to start."
        ) from exc
    if "mcp_servers" not in document:
        return None
    servers = document["mcp_servers"]
    if not isinstance(servers, Mapping):
        raise ClientConfigError(
            f"{path} declares mcp_servers as a {type(servers).__name__} rather than a table, "
            f"so no [mcp_servers.{SERVER_KEY}] table can be added beside it. "
            "Repair that entry, or run with --print and merge the block yourself."
        )
    if SERVER_KEY not in servers:
        return None
    entry = servers[SERVER_KEY]
    if not isinstance(entry, Mapping):
        raise ClientConfigError(
            f"{path} declares mcp_servers.{SERVER_KEY} as a {type(entry).__name__} rather than a "
            "table. Appending the block would leave a file Codex cannot parse. "
            "Repair that entry, or run with --print and merge the block yourself."
        )
    return entry


def _write_codex(plan: ClientPlan, *, force: bool) -> None:
    """Append the table, then prove the file still parses, or put it back untouched.

    Replacing a table that already exists is refused rather than attempted: a
    textual substitution inside a document this project does not own is how
    comments and ordering get silently destroyed.

    Appending is not unconditionally safe either, and reasoning about when it is
    safe is what produced two defects in a row. "A uniquely named table is always
    valid" is false whenever something already occupies that name in a form a
    header cannot extend: a string, an inline table, a dotted key. Each of those
    parses into exactly what a well-formed table parses into, so no inspection
    separates them, and every missed shape reported success over a file Codex can
    no longer read. The candidate bytes are therefore assembled and parsed
    without the destination being touched at all, and only a document that loads
    is moved into place.
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
    existing = plan.config_path.read_bytes() if plan.config_path.is_file() else b""
    candidate = existing + _line_separated_appendix(existing, plan.block)
    try:
        tomllib.loads(candidate.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ClientConfigError(
            f"{plan.config_path} cannot carry a [mcp_servers.{SERVER_KEY}] table: {exc}. "
            "The file is untouched. Repair the conflicting entry, or run with --print and "
            "merge the block yourself."
        ) from exc
    _replace_atomically(plan.config_path, candidate)


def _line_separated_appendix(existing: bytes, block: str) -> bytes:
    """Return the bytes to append, separated from what is there and matching its newlines.

    Nothing already in the file is rewritten: a user's line endings are theirs,
    and normalising them is a change to a document this project does not own.
    """
    newline = b"\r\n" if b"\r\n" in existing else b"\n"
    encoded = newline.join(block.encode("utf-8").split(b"\n"))
    if not existing or existing.endswith((b"\n\n", b"\r\n\r\n")):
        return encoded
    if existing.endswith((b"\n", b"\r")):
        return newline + encoded
    return newline + newline + encoded


def _require_written(temporary: Path, path: Path, data: bytes) -> None:
    """Compare what reached the disk with what was checked, not with what was meant."""
    if temporary.read_bytes() != data:
        raise ClientConfigError(
            f"{path} was not written: the bytes on disk differ from the ones checked"
        )


def _replace_atomically(path: Path, data: bytes) -> None:
    """Put bytes at a path without the destination ever holding a partial state.

    The destination is never opened for writing. The candidate is written beside
    it, read back so that what landed on disk is what was checked rather than
    what was intended, and moved into place by a single rename. An interrupted
    run therefore leaves either the previous file or a stray temporary one, never
    a half-written configuration a client cannot parse.

    The earlier attempt wrote the destination first and undid it afterwards,
    which is not the same thing. It validated a variable rather than a file, and
    the undo went through a text write that rewrote every line ending on Windows:
    a refusal that announced the file was unchanged had turned it from LF to
    CRLF. Bytes are read and written as bytes here for that reason.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".part")
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        _require_written(temporary, path, data)
        temporary.replace(path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def default_home() -> Path:
    """Return the home directory vendor configurations are resolved against."""
    override = os.environ.get("ENGRAM_CLIENT_HOME")
    return Path(override) if override else Path.home()
