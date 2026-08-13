# Copyright 2026 Julien Bombled
# SPDX-License-Identifier: Apache-2.0

"""What a vendor configuration keeps when Engram writes to it.

Replacing a path is not the same act as writing a file: a rename discards the
destination inode and everything only it carried -- the permission bits, the
owner, the extended attributes, the hard links, and on Windows the security
descriptor, the file attributes and the alternate data streams. Writing through
the existing file keeps all of them because nothing is discarded, and these tests
are what says so on each platform rather than in a comment.
"""

from __future__ import annotations

import json
import os
import socket
import stat
import subprocess
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
    return sorted(entry.name for entry in directory.iterdir() if entry.name.endswith(".engram-new"))


@pytest.mark.parametrize("kind", [ClientKind.CLAUDE, ClientKind.CODEX, ClientKind.GEMINI])
def test_a_hard_linked_configuration_keeps_every_name_on_the_same_file(
    kind: ClientKind,
    app_config: AppConfig,
    workspace: Path,
    tmp_path: Path,
) -> None:
    """A rename would swap the directory entry and leave the other names behind.

    Writing through the file keeps every name on one inode, which is what a hard
    link means: all of them see the change.
    """
    plan = plan_client(kind, app_config, home=workspace)
    _seed(kind, plan.config_path)
    peer = tmp_path / f"peer-{kind.value}"
    os.link(plan.config_path, peer)
    assert plan.config_path.stat().st_nlink == 2

    connect(plan_client(kind, app_config, home=workspace), force=False)

    assert peer.samefile(plan.config_path), "the link was broken"
    assert plan.config_path.stat().st_nlink == 2
    assert peer.read_bytes() == plan.config_path.read_bytes()
    assert endpoint_url(app_config).encode("utf-8") in peer.read_bytes()
    assert _staging_leftovers(plan.config_path.parent) == []


def test_a_hard_linked_instructions_file_keeps_every_name_on_the_same_file(
    app_config: AppConfig,
    workspace: Path,
    tmp_path: Path,
) -> None:
    plan = plan_client(ClientKind.CLAUDE, app_config, home=workspace)
    plan.instructions_path.write_bytes(b"# Shared guidance\n")
    peer = tmp_path / "peer-CLAUDE.md"
    os.link(plan.instructions_path, peer)

    assert install_protocol(plan) is True

    assert peer.samefile(plan.instructions_path), "the link was broken"
    assert peer.read_bytes().startswith(b"# Shared guidance\n")
    assert b"Engram session protocol" in peer.read_bytes()
    assert _staging_leftovers(plan.instructions_path.parent) == []


@posix_only
@pytest.mark.parametrize("maker", ["fifo", "socket"])
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


def test_a_directory_in_the_way_is_refused_on_every_platform(
    app_config: AppConfig,
    workspace: Path,
) -> None:
    """Every platform has directories, so this is not a POSIX-only contract."""
    plan = plan_client(ClientKind.GEMINI, app_config, home=workspace)
    plan.config_path.parent.mkdir(parents=True, exist_ok=True)
    plan.config_path.mkdir()

    with pytest.raises(ClientConfigError, match="a directory"):
        connect(plan_client(ClientKind.GEMINI, app_config, home=workspace), force=False)

    assert plan.config_path.is_dir()
    assert _staging_leftovers(plan.config_path.parent) == []


windows_only = pytest.mark.skipif(
    sys.platform != "win32",
    reason="NTFS security descriptors, attributes and alternate data streams",
)


@windows_only
@pytest.mark.parametrize("kind", [ClientKind.CLAUDE, ClientKind.CODEX, ClientKind.GEMINI])
def test_an_alternate_data_stream_survives_the_replacement(
    kind: ClientKind,
    app_config: AppConfig,
    workspace: Path,
) -> None:
    """NTFS keeps side data in named streams, which a new file does not carry."""
    plan = plan_client(kind, app_config, home=workspace)
    _seed(kind, plan.config_path)
    stream = Path(f"{plan.config_path}:engram-review")
    stream.write_bytes(b"keep-me")

    connect(plan_client(kind, app_config, home=workspace), force=False)

    assert stream.read_bytes() == b"keep-me"
    assert endpoint_url(app_config).encode("utf-8") in plan.config_path.read_bytes()


@windows_only
def test_an_alternate_data_stream_survives_the_protocol_append(
    app_config: AppConfig,
    workspace: Path,
) -> None:
    plan = plan_client(ClientKind.CLAUDE, app_config, home=workspace)
    plan.instructions_path.write_bytes(b"# My project\n")
    stream = Path(f"{plan.instructions_path}:engram-review")
    stream.write_bytes(b"keep-me")

    assert install_protocol(plan) is True

    assert stream.read_bytes() == b"keep-me"


@windows_only
def test_a_file_attribute_survives_the_replacement(
    app_config: AppConfig,
    workspace: Path,
) -> None:
    """Hidden is a property of the file; a newly created one comes back Archive."""
    if sys.platform != "win32":  # pragma: no cover - the decorator already skips
        return
    plan = plan_client(ClientKind.GEMINI, app_config, home=workspace)
    _seed(ClientKind.GEMINI, plan.config_path)
    subprocess.run(  # noqa: S603
        ["attrib", "+H", str(plan.config_path)],  # noqa: S607
        capture_output=True,
        check=True,
    )
    before = plan.config_path.stat().st_file_attributes
    assert before & stat.FILE_ATTRIBUTE_HIDDEN

    connect(plan_client(ClientKind.GEMINI, app_config, home=workspace), force=False)

    after = plan.config_path.stat().st_file_attributes
    assert after & stat.FILE_ATTRIBUTE_HIDDEN, f"Hidden was lost: {before:#x} -> {after:#x}"


@windows_only
def test_an_explicit_security_descriptor_survives_the_replacement(
    app_config: AppConfig,
    workspace: Path,
) -> None:
    """A protected DACL must not come back inherited, which is a weaker grant."""
    plan = plan_client(ClientKind.GEMINI, app_config, home=workspace)
    _seed(ClientKind.GEMINI, plan.config_path)
    subprocess.run(  # noqa: S603
        ["icacls", str(plan.config_path), "/inheritance:r", "/grant:r", f"{os.getlogin()}:F"],  # noqa: S607
        capture_output=True,
        check=True,
    )
    before = _security_descriptor(plan.config_path)

    connect(plan_client(ClientKind.GEMINI, app_config, home=workspace), force=False)

    assert _security_descriptor(plan.config_path) == before


def _security_descriptor(path: Path) -> str:
    """Return the access control list as Windows itself reports it.

    Read through icacls rather than a typed binding so the assertion does not
    depend on a package that is only a transitive dependency here.
    """
    completed = subprocess.run(  # noqa: S603
        ["icacls", str(path)],  # noqa: S607
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout.replace(str(path), "<path>")


@pytest.mark.parametrize("kind", [ClientKind.CLAUDE, ClientKind.CODEX, ClientKind.GEMINI])
def test_a_write_creates_no_file_other_than_the_destination(
    kind: ClientKind,
    app_config: AppConfig,
    workspace: Path,
) -> None:
    """No auxiliary file means no predictable path to aim at and no second copy.

    A sidecar beside the destination was neither: a pre-created hard link at its
    predictable name turned the staging write into a write through to an
    unrelated file, and on POSIX the copy carried the content out of a 0400
    destination at the umask default.
    """
    plan = plan_client(kind, app_config, home=workspace)
    _seed(kind, plan.config_path)
    before = {entry.name for entry in plan.config_path.parent.iterdir()}

    connect(plan_client(kind, app_config, home=workspace), force=False)

    assert {entry.name for entry in plan.config_path.parent.iterdir()} == before


def test_the_protocol_append_creates_no_file_other_than_the_instructions(
    app_config: AppConfig,
    workspace: Path,
) -> None:
    plan = plan_client(ClientKind.CLAUDE, app_config, home=workspace)
    plan.instructions_path.write_bytes(b"# My project\n")
    before = {entry.name for entry in plan.instructions_path.parent.iterdir()}

    assert install_protocol(plan) is True

    assert {entry.name for entry in plan.instructions_path.parent.iterdir()} == before


def test_a_file_at_the_old_staging_name_is_never_written_through(
    app_config: AppConfig,
    workspace: Path,
) -> None:
    """The name the sidecar used is no longer special, and must never be opened.

    Reproduced against the previous commit: pre-creating it as a hard link to an
    unrelated file made install_protocol write the protocol into that file and
    then unlink the name, reporting success.
    """
    plan = plan_client(ClientKind.CODEX, app_config, home=workspace)
    unrelated = workspace.parent / "unrelated.txt"
    unrelated.write_bytes(b"PRIVATE CONTENT\n")
    decoy = plan.instructions_path.with_name(f".{plan.instructions_path.name}.engram-new")
    os.link(unrelated, decoy)

    assert install_protocol(plan) is True

    assert unrelated.read_bytes() == b"PRIVATE CONTENT\n"
    assert decoy.read_bytes() == b"PRIVATE CONTENT\n"
    assert b"Engram session protocol" in plan.instructions_path.read_bytes()


@posix_only
def test_a_refused_write_leaves_no_readable_copy_of_a_private_file(
    app_config: AppConfig,
    workspace: Path,
) -> None:
    """A staged copy at the umask default carried a 0400 secret into the open."""
    plan = plan_client(ClientKind.GEMINI, app_config, home=workspace)
    plan.config_path.parent.mkdir(parents=True, exist_ok=True)
    plan.config_path.write_bytes(b'{"secret": "do-not-leak"}')
    plan.config_path.chmod(0o400)
    before = {entry.name for entry in plan.config_path.parent.iterdir()}

    with pytest.raises(ClientConfigError):
        connect(plan_client(ClientKind.GEMINI, app_config, home=workspace), force=True)

    assert {entry.name for entry in plan.config_path.parent.iterdir()} == before
    assert stat.S_IMODE(plan.config_path.stat().st_mode) == 0o400
