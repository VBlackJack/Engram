# Copyright 2026 Julien Bombled
# SPDX-License-Identifier: Apache-2.0

"""Cross-platform configured-database ownership tests."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from engram.process_lock import (
    DatabaseLockError,
    DatabaseLockRole,
    DatabaseProcessLock,
    database_lock_path,
)


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
