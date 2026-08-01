# Copyright 2026 Julien Bombled
# SPDX-License-Identifier: Apache-2.0

"""Cross-platform configured-database ownership tests."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import BinaryIO

import pytest

from engram import process_lock
from engram.config import AppConfig, DatabaseConfig, LimitsConfig, LoggingConfig, TtlConfig
from engram.process_lock import (
    LOCKED_BYTE_COUNT,
    DatabaseLockError,
    DatabaseLockOwner,
    DatabaseLockRole,
    DatabaseProcessLock,
    _clear_owner,
    _locked_message,
    _read_owner,
    _required_command,
    _write_owner,
    database_lock_path,
)
from engram.store import EngramStore


def test_daemon_lock_rejects_offline_writer_with_owner_pid(tmp_path: Path) -> None:
    database_path = tmp_path / "engram.db"

    with (
        DatabaseProcessLock(
            database_path,
            role=DatabaseLockRole.DAEMON,
            command="serve",
        ),
        pytest.raises(DatabaseLockError, match=rf"daemon is active \(pid {os.getpid()}\)"),
        DatabaseProcessLock(
            database_path,
            role=DatabaseLockRole.OFFLINE_WRITER,
            command="attest",
        ),
    ):
        pytest.fail("Offline writer acquired the daemon lock")

    with DatabaseProcessLock(
        database_path,
        role=DatabaseLockRole.OFFLINE_WRITER,
        command="attest",
    ):
        pass


def test_unlocked_stale_pid_metadata_is_reclaimed(tmp_path: Path) -> None:
    database_path = tmp_path / "engram.db"
    lock_path = database_lock_path(database_path)
    lock_path.write_bytes(
        b"\0"
        + json.dumps(
            {
                "command": "serve",
                "pid": 2_147_483_647,
                "role": DatabaseLockRole.DAEMON.value,
            }
        ).encode("ascii")
    )

    with DatabaseProcessLock(
        database_path,
        role=DatabaseLockRole.OFFLINE_WRITER,
        command="attest",
    ):
        with lock_path.open("rb", buffering=0) as lock_file:
            lock_file.seek(1)
            owner = json.loads(lock_file.read().decode("ascii"))
        assert owner == {
            "command": "attest",
            "pid": os.getpid(),
            "role": DatabaseLockRole.OFFLINE_WRITER.value,
        }

    assert lock_path.read_bytes() == b"\0"


def test_offline_writer_lock_rejects_daemon_start(tmp_path: Path) -> None:
    database_path = tmp_path / "engram.db"

    with (
        DatabaseProcessLock(
            database_path,
            role=DatabaseLockRole.OFFLINE_WRITER,
            command="consolidate",
        ),
        pytest.raises(DatabaseLockError, match="offline writer 'consolidate' is active"),
        DatabaseProcessLock(
            database_path,
            role=DatabaseLockRole.DAEMON,
            command="serve",
        ),
    ):
        pytest.fail("Daemon acquired the offline writer lock")


def test_store_writer_lease_blocks_another_process_and_releases(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "engram.db"
    config = AppConfig(
        database=DatabaseConfig(path=database_path),
        ttl_days=TtlConfig(),
        limits=LimitsConfig(),
        logging=LoggingConfig(path=tmp_path / "engram.log"),
    )
    script = """
import sys
from pathlib import Path
from engram.config import AppConfig, DatabaseConfig, LimitsConfig, LoggingConfig, TtlConfig
from engram.process_lock import DatabaseLockError
from engram.store import EngramStore

database_path = Path(sys.argv[1])
config = AppConfig(
    database=DatabaseConfig(path=database_path),
    ttl_days=TtlConfig(),
    limits=LimitsConfig(),
    logging=LoggingConfig(path=database_path.with_suffix(".log")),
)
try:
    with EngramStore(config):
        pass
except DatabaseLockError:
    raise SystemExit(23)
raise SystemExit(0)
""".strip()

    with EngramStore(config):
        locked = subprocess.run(  # noqa: S603
            [sys.executable, "-c", script, str(database_path)],
            check=False,
            timeout=30,
        )
    released = subprocess.run(  # noqa: S603
        [sys.executable, "-c", script, str(database_path)],
        check=False,
        timeout=30,
    )

    assert locked.returncode == 23
    assert released.returncode == 0


def _owner_payload(**overrides: object) -> bytes:
    payload = {"command": "serve", "pid": 4321, "role": DatabaseLockRole.DAEMON.value}
    payload.update(overrides)
    return b"\0" + json.dumps(payload).encode("ascii")


def test_acquiring_an_already_held_lock_is_a_programming_error(tmp_path: Path) -> None:
    lock = DatabaseProcessLock(
        tmp_path / "engram.db",
        role=DatabaseLockRole.DAEMON,
        command="serve",
    )

    with lock, pytest.raises(RuntimeError, match="already acquired"):
        lock.acquire()


def test_releasing_a_lock_that_was_never_acquired_is_silent(tmp_path: Path) -> None:
    lock = DatabaseProcessLock(
        tmp_path / "engram.db",
        role=DatabaseLockRole.DAEMON,
        command="serve",
    )

    lock.release()

    assert not lock.covers(tmp_path / "engram.db")


def test_a_lock_held_by_another_process_reports_its_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cover the contended branch in-process; the real contention runs in a subprocess."""
    database_path = tmp_path / "engram.db"
    database_lock_path(database_path).write_bytes(
        _owner_payload(command="consolidate", role=DatabaseLockRole.OFFLINE_WRITER.value)
    )
    monkeypatch.setattr(process_lock, "_try_lock", lambda _handle: False)

    with pytest.raises(DatabaseLockError, match=r"offline writer 'consolidate' is active") as held:
        DatabaseProcessLock(
            database_path,
            role=DatabaseLockRole.DAEMON,
            command="serve",
        ).acquire()

    assert held.value.owner is not None
    assert held.value.owner.pid == 4321


@pytest.mark.parametrize("failing", ["_try_lock", "_write_owner"])
def test_a_failure_while_taking_ownership_closes_the_handle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failing: str,
) -> None:
    """A half-taken lock must not leave a descriptor behind that nothing will close."""
    opened: list[BinaryIO] = []
    # Reaching inside is the point: these branches exist for failures the
    # operating system produces, and nothing public can provoke them.
    original_open = process_lock._open_lock_file  # noqa: SLF001

    def _record(path: Path) -> BinaryIO:
        handle = original_open(path)
        opened.append(handle)
        return handle

    def _explode(*_args: object, **_kwargs: object) -> None:
        raise OSError("ownership could not be taken")

    monkeypatch.setattr(process_lock, "_open_lock_file", _record)
    monkeypatch.setattr(process_lock, failing, _explode)

    with pytest.raises(OSError, match="ownership could not be taken"):
        DatabaseProcessLock(
            tmp_path / "engram.db",
            role=DatabaseLockRole.DAEMON,
            command="serve",
        ).acquire()

    assert opened, "the coordination file was never opened"
    assert all(handle.closed for handle in opened)


@pytest.mark.parametrize("failing", ["_clear_owner", "_unlock"])
def test_release_survives_an_operating_system_refusal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    failing: str,
) -> None:
    """Releasing must not raise: the caller is already on its way out."""
    lock = DatabaseProcessLock(
        tmp_path / "engram.db",
        role=DatabaseLockRole.DAEMON,
        command="serve",
    )
    lock.acquire()

    def _explode(*_args: object, **_kwargs: object) -> None:
        raise OSError("refused by the operating system")

    monkeypatch.setattr(process_lock, failing, _explode)
    with caplog.at_level("WARNING", logger="engram.process_lock"):
        lock.release()

    assert "refused by the operating system" in caplog.text
    assert not lock.covers(tmp_path / "engram.db")


def test_owner_metadata_survives_a_write_and_read_round_trip(tmp_path: Path) -> None:
    lock_path = tmp_path / "engram.db.lock"
    owner = DatabaseLockOwner(pid=99, role=DatabaseLockRole.WRITER, command="remember")

    with lock_path.open("wb+", buffering=0) as handle:
        handle.write(b"\0")
        _write_owner(handle, owner)
        assert _read_owner(handle) == owner
        _clear_owner(handle)
        assert _read_owner(handle) is None

    assert lock_path.read_bytes() == b"\0"


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        (b"\0[]", "payload is not an object"),
        (b"\0not-json", "payload is not JSON"),
        (b"\0\xff\xfe", "payload is not ASCII"),
        (_owner_payload(pid="4321"), "pid is not an integer"),
        (_owner_payload(role=7), "role is not a string"),
        (_owner_payload(command=None), "command is not a string"),
        (_owner_payload(command="   "), "command is blank"),
        (_owner_payload(role="archivist"), "role is not a known role"),
    ],
)
def test_unreadable_owner_metadata_yields_no_owner(tmp_path: Path, raw: bytes, reason: str) -> None:
    """Diagnostics are best effort: a corrupt record must not mask the lock itself."""
    lock_path = tmp_path / "engram.db.lock"
    lock_path.write_bytes(raw)

    with lock_path.open("rb", buffering=0) as handle:
        assert _read_owner(handle) is None, reason


def test_a_blank_command_is_refused_at_construction() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        _required_command("   ")

    assert _required_command("  serve  ") == "serve"


@pytest.mark.parametrize(
    ("owner", "expected"),
    [
        (None, "locked by another process"),
        (
            DatabaseLockOwner(pid=1, role=DatabaseLockRole.DAEMON, command="serve"),
            "daemon is active (pid 1)",
        ),
        (
            DatabaseLockOwner(pid=2, role=DatabaseLockRole.WRITER, command="remember"),
            "writer 'remember' is active (pid 2)",
        ),
        (
            DatabaseLockOwner(pid=3, role=DatabaseLockRole.OFFLINE_WRITER, command="attest"),
            "offline writer 'attest' is active (pid 3)",
        ),
    ],
)
def test_every_role_states_who_holds_the_lock(
    owner: DatabaseLockOwner | None,
    expected: str,
) -> None:
    """The message is the only thing an operator sees; each role must name itself."""
    assert expected in _locked_message(owner)


def test_the_locked_byte_is_never_part_of_the_metadata(tmp_path: Path) -> None:
    lock_path = tmp_path / "engram.db.lock"
    owner = DatabaseLockOwner(pid=5, role=DatabaseLockRole.DAEMON, command="serve")

    with lock_path.open("wb+", buffering=0) as handle:
        handle.write(b"\0")
        _write_owner(handle, owner)

    assert lock_path.read_bytes()[:LOCKED_BYTE_COUNT] == b"\0"
