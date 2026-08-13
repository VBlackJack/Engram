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
import stat
import tomllib
from pathlib import Path

import pytest

from engram.clients import ClientKind, connect, endpoint_url, install_protocol, plan_client
from engram.config import AppConfig

posix_only = pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")

WRITERS = ("claude", "codex", "gemini")


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
