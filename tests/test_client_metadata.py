# Copyright 2026 Julien Bombled
# SPDX-License-Identifier: Apache-2.0

"""File metadata a vendor configuration keeps across an atomic replacement.

Replacing a path is not the same act as writing a file. A rename carries the
permissions of whatever is renamed, and it replaces a symlink rather than the
file that symlink names. Both cost the user something the command claimed not to
touch, so both are pinned here.
"""

from __future__ import annotations

import json
import os
import socket
import stat
import sys
import tomllib
from pathlib import Path

import pytest

from engram.clients import (
    ClientConfigError,
    ClientKind,
    connect,
    endpoint_url,
    install_protocol,
    plan_client,
)
from engram.config import AppConfig

posix_only = pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX file identity and permission bits",
)


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Run each command in a directory of its own, with its own home."""
    monkeypatch.chdir(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    return home


def _seed(kind: ClientKind, path: Path) -> bytes:
    """Return a valid existing configuration for this client, and write it."""
    content = (
        b'{\n  "mcpServers": {\n    "datacron": {"httpUrl": "http://x/mcp"}\n  }\n}\n'
        if kind is not ClientKind.CODEX
        else b'[mcp_servers.datacron]\nurl = "http://x/mcp"\n'
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return content


def _symlinks_available(directory: Path) -> bool:
    probe, link = directory / "probe", directory / "probe.link"
    probe.write_bytes(b"x")
    try:
        link.symlink_to(probe)
    except (OSError, NotImplementedError):
        return False
    link.unlink()
    return True


@posix_only
@pytest.mark.parametrize("kind", [ClientKind.CLAUDE, ClientKind.CODEX, ClientKind.GEMINI])
def test_an_existing_configuration_keeps_its_permission_bits(
    kind: ClientKind,
    app_config: AppConfig,
    workspace: Path,
) -> None:
    """A temporary file is created private; the destination must not inherit that.

    Renaming a 0600 temporary over a configuration readable by the group left it
    at 0600, so a file a dotfiles repository or a shared machine relied on
    stopped being readable, without a word.
    """
    plan = plan_client(kind, app_config, home=workspace)
    _seed(kind, plan.config_path)
    plan.config_path.chmod(0o644)

    connect(plan_client(kind, app_config, home=workspace), force=False)

    assert stat.S_IMODE(plan.config_path.stat().st_mode) == 0o644


@posix_only
def test_an_existing_instructions_file_keeps_its_permission_bits(
    app_config: AppConfig,
    workspace: Path,
) -> None:
    plan = plan_client(ClientKind.CLAUDE, app_config, home=workspace)
    plan.instructions_path.write_bytes(b"# My project\n")
    plan.instructions_path.chmod(0o644)

    assert install_protocol(plan) is True

    assert stat.S_IMODE(plan.instructions_path.stat().st_mode) == 0o644


@posix_only
@pytest.mark.parametrize("kind", [ClientKind.CLAUDE, ClientKind.CODEX, ClientKind.GEMINI])
def test_a_new_configuration_is_created_with_the_usual_permissions(
    kind: ClientKind,
    app_config: AppConfig,
    workspace: Path,
) -> None:
    """A first run must produce what any other tool would, not a private artefact."""
    umask = os.umask(0)
    os.umask(umask)
    plan = plan_client(kind, app_config, home=workspace)

    connect(plan, force=False)

    assert stat.S_IMODE(plan.config_path.stat().st_mode) == 0o666 & ~umask


@pytest.mark.parametrize("kind", [ClientKind.CLAUDE, ClientKind.CODEX, ClientKind.GEMINI])
def test_a_symlinked_configuration_is_followed_rather_than_replaced(
    kind: ClientKind,
    app_config: AppConfig,
    workspace: Path,
    tmp_path: Path,
) -> None:
    """A dotfiles repository owns the file; the link must survive pointing at it.

    Renaming onto the link replaced the link itself with a regular file, so the
    configuration silently stopped being the one the repository tracks while the
    command reported success.
    """
    if not _symlinks_available(tmp_path):
        pytest.skip("this host does not permit creating symlinks")
    plan = plan_client(kind, app_config, home=workspace)
    canonical = tmp_path / "dotfiles" / f"canonical-{kind.value}"
    canonical.parent.mkdir(parents=True, exist_ok=True)
    seeded = _seed(kind, canonical)
    plan.config_path.parent.mkdir(parents=True, exist_ok=True)
    plan.config_path.symlink_to(canonical)

    connect(plan_client(kind, app_config, home=workspace), force=False)

    assert plan.config_path.is_symlink(), "the link was replaced by a regular file"
    assert plan.config_path.resolve() == canonical.resolve()
    written = canonical.read_bytes()
    assert written != seeded, "the canonical file did not receive the change"
    assert endpoint_url(app_config).encode("utf-8") in written
    document = (
        tomllib.loads(written.decode("utf-8"))
        if kind is ClientKind.CODEX
        else json.loads(written.decode("utf-8"))
    )
    assert document is not None


def test_a_symlinked_instructions_file_is_followed_rather_than_replaced(
    app_config: AppConfig,
    workspace: Path,
    tmp_path: Path,
) -> None:
    if not _symlinks_available(tmp_path):
        pytest.skip("this host does not permit creating symlinks")
    plan = plan_client(ClientKind.CLAUDE, app_config, home=workspace)
    canonical = tmp_path / "dotfiles" / "CLAUDE.md"
    canonical.parent.mkdir(parents=True, exist_ok=True)
    canonical.write_bytes(b"# Shared guidance\n")
    plan.instructions_path.symlink_to(canonical)

    assert install_protocol(plan) is True

    assert plan.instructions_path.is_symlink()
    assert canonical.read_bytes().startswith(b"# Shared guidance\n")
    assert b"Engram session protocol" in canonical.read_bytes()


@posix_only
def test_a_symlinked_configuration_keeps_the_permissions_of_its_target(
    app_config: AppConfig,
    workspace: Path,
    tmp_path: Path,
) -> None:
    if not _symlinks_available(tmp_path):
        pytest.skip("this host does not permit creating symlinks")
    plan = plan_client(ClientKind.GEMINI, app_config, home=workspace)
    canonical = tmp_path / "dotfiles" / "settings.json"
    canonical.parent.mkdir(parents=True, exist_ok=True)
    _seed(ClientKind.GEMINI, canonical)
    canonical.chmod(0o644)
    plan.config_path.parent.mkdir(parents=True, exist_ok=True)
    plan.config_path.symlink_to(canonical)

    connect(plan_client(ClientKind.GEMINI, app_config, home=workspace), force=False)

    assert stat.S_IMODE(canonical.stat().st_mode) == 0o644


def _staging_leftovers(directory: Path) -> list[str]:
    """Return any file the write staged in and failed to clean up."""
    return sorted(entry.name for entry in directory.iterdir() if entry.name.endswith(".part"))


@posix_only
@pytest.mark.parametrize("kind", [ClientKind.CLAUDE, ClientKind.CODEX, ClientKind.GEMINI])
def test_a_hard_linked_configuration_is_refused_rather_than_detached(
    kind: ClientKind,
    app_config: AppConfig,
    workspace: Path,
    tmp_path: Path,
) -> None:
    """A rename swaps a directory entry, so the other names keep the old content.

    Replacing a hard-linked configuration silently makes this name a different
    file from the one its other names still point at.
    """
    plan = plan_client(kind, app_config, home=workspace)
    seeded = _seed(kind, plan.config_path)
    peer = tmp_path / f"peer-{kind.value}"
    os.link(plan.config_path, peer)
    assert plan.config_path.stat().st_nlink == 2

    with pytest.raises(ClientConfigError, match="hard links"):
        connect(plan_client(kind, app_config, home=workspace), force=False)

    assert plan.config_path.read_bytes() == seeded
    assert peer.samefile(plan.config_path)
    assert plan.config_path.stat().st_nlink == 2
    assert _staging_leftovers(plan.config_path.parent) == []


@posix_only
def test_a_hard_linked_instructions_file_is_refused_rather_than_detached(
    app_config: AppConfig,
    workspace: Path,
    tmp_path: Path,
) -> None:
    plan = plan_client(ClientKind.CLAUDE, app_config, home=workspace)
    plan.instructions_path.write_bytes(b"# Shared guidance\n")
    peer = tmp_path / "peer-CLAUDE.md"
    os.link(plan.instructions_path, peer)

    with pytest.raises(ClientConfigError, match="hard links"):
        install_protocol(plan)

    assert plan.instructions_path.read_bytes() == b"# Shared guidance\n"
    assert peer.samefile(plan.instructions_path)
    assert _staging_leftovers(plan.instructions_path.parent) == []


@posix_only
@pytest.mark.parametrize("maker", ["fifo", "socket", "directory"])
def test_a_destination_that_is_not_a_regular_file_is_refused(
    maker: str,
    app_config: AppConfig,
    workspace: Path,
) -> None:
    """Replacing a FIFO, a socket or a directory with an ordinary file destroys it."""
    if sys.platform == "win32":  # pragma: no cover - the decorator already skips
        return
    plan = plan_client(ClientKind.GEMINI, app_config, home=workspace)
    plan.config_path.parent.mkdir(parents=True, exist_ok=True)
    if maker == "fifo":
        os.mkfifo(plan.config_path)
        expected = "a FIFO"
    elif maker == "socket":
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as endpoint:
            endpoint.bind(str(plan.config_path))
        expected = "a socket"
    else:
        plan.config_path.mkdir()
        expected = "a directory"

    with pytest.raises(ClientConfigError, match=expected):
        connect(plan_client(ClientKind.GEMINI, app_config, home=workspace), force=False)

    assert not plan.config_path.is_file()
    assert _staging_leftovers(plan.config_path.parent) == []


@posix_only
@pytest.mark.parametrize("kind", [ClientKind.CLAUDE, ClientKind.CODEX, ClientKind.GEMINI])
def test_an_extended_attribute_survives_the_replacement(
    kind: ClientKind,
    app_config: AppConfig,
    workspace: Path,
) -> None:
    """A new file is a new inode and starts with nothing; xattrs have to be carried."""
    if sys.platform == "win32":  # pragma: no cover - the decorator already skips
        return
    plan = plan_client(kind, app_config, home=workspace)
    _seed(kind, plan.config_path)
    try:
        os.setxattr(plan.config_path, "user.engram-review", b"keep-me")
    except OSError:  # pragma: no cover - filesystem without extended attributes
        pytest.skip("this filesystem does not support extended attributes")

    connect(plan_client(kind, app_config, home=workspace), force=False)

    assert os.getxattr(plan.config_path, "user.engram-review") == b"keep-me"
    assert endpoint_url(app_config).encode("utf-8") in plan.config_path.read_bytes()


@posix_only
def test_an_extended_attribute_survives_the_protocol_append(
    app_config: AppConfig,
    workspace: Path,
) -> None:
    if sys.platform == "win32":  # pragma: no cover - the decorator already skips
        return
    plan = plan_client(ClientKind.CLAUDE, app_config, home=workspace)
    plan.instructions_path.write_bytes(b"# My project\n")
    try:
        os.setxattr(plan.instructions_path, "user.engram-review", b"keep-me")
    except OSError:  # pragma: no cover - filesystem without extended attributes
        pytest.skip("this filesystem does not support extended attributes")

    assert install_protocol(plan) is True

    assert os.getxattr(plan.instructions_path, "user.engram-review") == b"keep-me"


@pytest.mark.parametrize("kind", [ClientKind.CLAUDE, ClientKind.CODEX, ClientKind.GEMINI])
def test_a_successful_write_leaves_no_temporary_behind(
    kind: ClientKind,
    app_config: AppConfig,
    workspace: Path,
) -> None:
    """The public surface must never expose the file the write was staged in."""
    plan = plan_client(kind, app_config, home=workspace)
    _seed(kind, plan.config_path)

    connect(plan_client(kind, app_config, home=workspace), force=False)

    assert _staging_leftovers(plan.config_path.parent) == []
