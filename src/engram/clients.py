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
import stat
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import IO, TYPE_CHECKING, Any

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
class _Snapshot:
    """What a file held, and which file that was.

    The content alone is not enough to identify what was read. Two files can
    hold identical bytes, so comparing only the bytes accepted a destination
    that had been repointed at somebody else's file in the meantime: the
    comparison passed and the write went to the wrong inode. The identity
    travels with the bytes so the writer can require both.
    """

    data: bytes
    identity: tuple[int, int]


@dataclass(frozen=True, slots=True)
class ClientPlan:
    """What connecting one client would write, and where.

    `already_correct` describes the file as it was when the plan was made, which
    is what `--print` reports. It is deliberately not what `connect` acts on: a
    plan is a description, not a promise about a file somebody else can still
    edit.
    """

    kind: ClientKind
    config_path: Path
    instructions_path: Path
    endpoint: str
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
        plan = ClientPlan(
            kind=kind,
            config_path=path,
            instructions_path=Path.cwd() / "CLAUDE.md",
            endpoint=url,
            block=_render_json_block({"type": "http", "url": url}),
            already_correct=False,
        )
    elif kind is ClientKind.GEMINI:
        path = root / ".gemini" / "settings.json"
        plan = ClientPlan(
            kind=kind,
            config_path=path,
            instructions_path=Path.cwd() / "GEMINI.md",
            endpoint=url,
            block=_render_json_block({"httpUrl": url}),
            already_correct=False,
        )
    else:
        path = root / ".codex" / "config.toml"
        plan = ClientPlan(
            kind=kind,
            config_path=path,
            instructions_path=Path.cwd() / "AGENTS.md",
            endpoint=url,
            block=_render_codex_block(url),
            already_correct=False,
        )
    return replace(plan, already_correct=_declares_endpoint(plan))


def _declares_endpoint(plan: ClientPlan) -> bool:
    """Read the vendor file now and report whether it already names this endpoint.

    Called again by `connect`, never inherited from the plan. A plan describes
    the file at the moment it was made, and acting on that description reported
    "already correct" over an endpoint somebody had since changed, or over a file
    that had since stopped parsing -- a no-op announced as a success, which is
    the one outcome this command must never produce.
    """
    if plan.kind is ClientKind.CODEX:
        return _codex_declares(plan.config_path, plan.endpoint)
    expected: object = json.loads(plan.block)["mcpServers"][SERVER_KEY]
    return _json_entry(plan.config_path, ("mcpServers", SERVER_KEY)) == expected


def connect(plan: ClientPlan, *, force: bool) -> bool:
    """Write the endpoint into the vendor configuration, and report whether it changed."""
    if _declares_endpoint(plan):
        return False
    if plan.kind is ClientKind.CODEX:
        _write_codex(plan, force=force)
    else:
        _write_json_client(plan, force=force)
    return True


def install_protocol(plan: ClientPlan) -> bool:
    """Append the session protocol to the client's instructions, once."""
    protocol = client_protocol_text().strip()
    existing = _read_optional_bytes(plan.instructions_path)
    seen = b"" if existing is None else existing.data
    if protocol.encode("utf-8") in seen.replace(b"\r\n", b"\n"):
        return False
    _write_preserving_identity(
        plan.instructions_path,
        seen + _line_separated_appendix(seen, f"{protocol}\n"),
        expected=existing,
    )
    return True


def _read_optional_bytes(path: Path) -> _Snapshot | None:
    """Return what a file held and which file held it, or None when there is none.

    Both halves are taken from one open descriptor. Reading the bytes and asking
    the path about itself separately would describe two different files if the
    name were repointed in between, which is the whole thing the writer checks.
    """
    if not path.is_file():
        # A stat before the open, so a FIFO is refused by the writer rather than
        # blocking here. Correctness does not rest on it: the identity below
        # comes from the descriptor actually opened.
        return None
    try:
        stream = path.open("rb")
    except OSError as exc:
        raise ClientConfigError(f"{path} cannot be read: {exc}") from exc
    with stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ClientConfigError(
                f"{path} is {_describe_file_type(info.st_mode)}, not a regular file."
            )
        return _Snapshot(data=stream.read(), identity=(info.st_dev, info.st_ino))


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
    snapshot = _read_optional_bytes(path)
    return _parse_json(path, None if snapshot is None else snapshot.data)


def _parse_json(path: Path, raw: bytes | None) -> dict[str, Any]:
    """Parse the bytes the caller read, so the write can be checked against them."""
    if raw is None:
        return {}
    try:
        loaded = json.loads(raw.decode("utf-8") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
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
    raw = _read_optional_bytes(plan.config_path)
    document = _parse_json(plan.config_path, None if raw is None else raw.data)
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
    _write_preserving_identity(
        plan.config_path,
        (json.dumps(document, indent=2) + "\n").encode("utf-8"),
        expected=raw,
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
    existing = _read_optional_bytes(plan.config_path)
    seen = b"" if existing is None else existing.data
    candidate = seen + _line_separated_appendix(seen, plan.block)
    try:
        tomllib.loads(candidate.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ClientConfigError(
            f"{plan.config_path} cannot carry a [mcp_servers.{SERVER_KEY}] table: {exc}. "
            "The file is untouched. Repair the conflicting entry, or run with --print and "
            "merge the block yourself."
        ) from exc
    _write_preserving_identity(plan.config_path, candidate, expected=existing)


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


def _write_preserving_identity(path: Path, data: bytes, *, expected: _Snapshot | None) -> None:
    """Write bytes to a path while keeping everything about the file that is not content.

    Five review rounds chased metadata off the edge of a rename: the permission
    bits, then the symlink, then the hard links, then the extended attributes,
    and on Windows the security descriptor, the file attributes and the alternate
    data streams. The edge is structural, not a run of oversights. Renaming swaps
    a directory entry, so the destination inode is discarded with everything only
    it carried, and each of those properties then has to be collected and
    reapplied by hand through a different native interface per platform, or it is
    lost in silence. Enumerating them one review at a time was always going to
    leave the next one out.

    Writing through the existing file removes the class instead of enumerating
    it. The inode is never replaced, so the security descriptor, the alternate
    streams, the file attributes, the owner, the mode, the extended attributes
    and every hard link survive because nothing discarded them -- on both
    platforms, with no platform-specific code. A symlink is followed by the open
    itself, so the link keeps pointing at the file that receives the change.

    **This write is not atomic, and does not pretend to be.** A process killed
    during it can leave the file partly written. That is stated rather than
    papered over, because the attempt to paper over it was worse than the gap.

    An earlier version staged the content in a sidecar beside the destination and
    claimed the guarantee was bought back. It was not. A hard kill still left the
    destination half written, and nothing survived the kill to report where the
    good copy was. What the sidecar did add was real: a predictable path in a
    directory the user does not solely control, which a pre-created hard link
    turned into a write through to an unrelated file, and a second copy of the
    content at default permissions, which carried a secret out of a `0400`
    destination. Three defects for no guarantee, so no auxiliary file is created
    at all now.

    What is guaranteed instead: nothing else on disk is touched, the bytes are
    read back and compared before the call returns, and a file left unparseable
    by an interrupted run is refused loudly by the next one rather than extended.
    """
    _require_writable_target(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if expected is None:
        _create_exclusively(path, data)
    else:
        _rewrite_unchanged(path, data, expected)
    landed = path.read_bytes()
    if landed != data:
        raise ClientConfigError(
            f"{path} holds {len(landed)} bytes after a {len(data)}-byte write; "
            "the file system did not store what was written"
        )


def _create_exclusively(path: Path, data: bytes) -> None:
    """Create the file, refusing if anything already occupies the name.

    The caller found no file and built its content on that basis. Asking again
    whether the path exists would let a file created in between be adopted and
    overwritten; creating exclusively makes that a refusal instead, including
    when the newcomer is a hard link to somebody else's file.
    """
    try:
        with path.open("xb") as stream:
            _store(stream, data)
    except FileExistsError as exc:
        raise ClientConfigError(
            f"{path} appeared after Engram read the directory and found nothing there, so the "
            "block was prepared against a state that no longer holds. Nothing was written; "
            "run the command again."
        ) from exc
    except OSError as exc:
        raise ClientConfigError(f"{path} could not be created: {exc}") from exc


def _rewrite_unchanged(path: Path, data: bytes, expected: _Snapshot) -> None:
    """Rewrite the file only if it still holds the bytes the content was built from.

    The content is a function of what was read. If the file changed in between,
    writing would discard somebody else's edit without a word, so the bytes are
    compared through the descriptor that will do the writing -- not through a
    second look at the path, which is what let a concurrent file be adopted.
    """
    try:
        stream = path.open("r+b")
    except OSError as exc:
        raise ClientConfigError(f"{path} could not be opened for writing: {exc}") from exc
    with stream:
        info = os.fstat(stream.fileno())
        _require_regular(path, info)
        if (info.st_dev, info.st_ino) != expected.identity:
            raise ClientConfigError(
                f"{path} is no longer the file Engram read: the name now leads to a different "
                "one. Nothing was written, so whatever it points at is intact; run the command "
                "again to work from the file that is there now."
            )
        current = stream.read()
        if current != expected.data:
            raise ClientConfigError(
                f"{path} changed after Engram read it: it held {len(expected.data)} bytes and "
                f"now holds {len(current)}. Nothing was written, so the other change is intact; "
                "run the command again to build the block from the current file."
            )
        stream.seek(0)
        _store(stream, data)


def _store(stream: IO[bytes], data: bytes) -> None:
    """Put the bytes in the open file and make the file system commit to them."""
    stream.write(data)
    stream.truncate()
    stream.flush()
    os.fsync(stream.fileno())


def _require_regular(path: Path, info: os.stat_result) -> None:
    """Check the type of the object actually opened, not of the name that led to it."""
    if not stat.S_ISREG(info.st_mode):
        raise ClientConfigError(
            f"{path} is not a regular file. Engram will not write a configuration over it."
        )


def _require_writable_target(path: Path) -> None:
    """Refuse a destination that writing through would destroy rather than update.

    A FIFO blocks on open, a socket cannot be opened at all, and a directory is
    not a file. None of them is something a configuration writer should be
    turning into one, and unlike the properties above, none can be preserved by
    any means: the refusal is the contract, not a limitation of the method.
    """
    try:
        info = path.stat()
    except (FileNotFoundError, NotADirectoryError):
        return
    except OSError as exc:
        raise ClientConfigError(f"{path} cannot be inspected: {exc}") from exc
    if not stat.S_ISREG(info.st_mode):
        raise ClientConfigError(
            f"{path} is {_describe_file_type(info.st_mode)}, not a regular file. "
            "Engram will not write a configuration over it. Point the client at a regular "
            "file, or use --print and place the block yourself."
        )


def _describe_file_type(mode: int) -> str:
    for predicate, description in (
        (stat.S_ISDIR, "a directory"),
        (stat.S_ISFIFO, "a FIFO"),
        (stat.S_ISSOCK, "a socket"),
        (stat.S_ISBLK, "a block device"),
        (stat.S_ISCHR, "a character device"),
    ):
        if predicate(mode):
            return description
    return "of an unsupported type"


def default_home() -> Path:
    """Return the home directory vendor configurations are resolved against."""
    override = os.environ.get("ENGRAM_CLIENT_HOME")
    return Path(override) if override else Path.home()
