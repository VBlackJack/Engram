# Copyright 2026 Julien Bombled
# SPDX-License-Identifier: Apache-2.0

"""Cross-process ownership lock for one configured Engram database."""

from __future__ import annotations

import errno
import importlib
import json
import logging
import os
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from types import TracebackType
from typing import BinaryIO, Protocol, Self, cast


class _WindowsLockApi(Protocol):
    LK_NBLCK: int
    LK_UNLCK: int

    def locking(self, fd: int, mode: int, nbytes: int) -> None: ...


class _PosixLockApi(Protocol):
    LOCK_EX: int
    LOCK_NB: int
    LOCK_UN: int

    def flock(self, fd: int, operation: int) -> None: ...


WINDOWS_LOCK_API = (
    cast("_WindowsLockApi", importlib.import_module("msvcrt")) if os.name == "nt" else None
)
POSIX_LOCK_API = (
    cast("_PosixLockApi", importlib.import_module("fcntl")) if os.name != "nt" else None
)

LOCKED_BYTE_COUNT = 1
LOGGER = logging.getLogger(__name__)


class DatabaseLockRole(StrEnum):
    """Process roles serialized by the database ownership lock."""

    DAEMON = "daemon"
    OFFLINE_WRITER = "offline_writer"


@dataclass(frozen=True, slots=True)
class DatabaseLockOwner:
    """Diagnostic owner metadata stored outside the locked byte."""

    pid: int
    role: DatabaseLockRole
    command: str


class DatabaseLockError(RuntimeError):
    """Raised when another process owns the configured database."""

    def __init__(self, path: Path, owner: DatabaseLockOwner | None) -> None:
        """Retain the coordination path and best-effort owner metadata."""
        self.path = path
        self.owner = owner
        super().__init__(_locked_message(owner))


class DatabaseProcessLock:
    """Hold one non-blocking OS lock for a daemon or offline writer lifetime."""

    def __init__(
        self,
        database_path: Path,
        *,
        role: DatabaseLockRole,
        command: str,
    ) -> None:
        """Describe ownership before opening the coordination file."""
        self.path = database_lock_path(database_path)
        self._owner = DatabaseLockOwner(
            pid=os.getpid(),
            role=role,
            command=_required_command(command),
        )
        self._handle: BinaryIO | None = None

    def __enter__(self) -> Self:
        """Acquire and return this lock."""
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Release ownership when leaving the context."""
        del exc_type, exc_value, traceback
        self.release()

    def acquire(self) -> None:
        """Acquire ownership or fail immediately with current owner diagnostics."""
        if self._handle is not None:
            raise RuntimeError("Database process lock is already acquired")
        handle = _open_lock_file(self.path)
        try:
            acquired = _try_lock(handle)
        except BaseException:
            handle.close()
            raise
        if not acquired:
            owner = _read_owner(handle)
            handle.close()
            raise DatabaseLockError(self.path, owner)
        try:
            _write_owner(handle, self._owner)
        except BaseException:
            handle.close()
            raise
        self._handle = handle

    def release(self) -> None:
        """Clear diagnostics and release OS ownership without unlinking the lock inode."""
        handle = self._handle
        if handle is None:
            return
        self._handle = None
        try:
            try:
                _clear_owner(handle)
            except OSError as exc:
                LOGGER.warning("Could not clear database lock metadata: %s", exc)
            try:
                _unlock(handle)
            except OSError as exc:
                LOGGER.warning("Could not explicitly unlock database lock: %s", exc)
        finally:
            handle.close()


def database_lock_path(database_path: Path) -> Path:
    """Derive the persistent coordination file beside the configured database."""
    resolved = database_path.expanduser().resolve()
    return resolved.with_name(f"{resolved.name}.lock")


def _open_lock_file(path: Path) -> BinaryIO:
    path.parent.mkdir(parents=True, exist_ok=True)
    binary_flag = getattr(os, "O_BINARY", 0)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | binary_flag, 0o600)
    handle = cast("BinaryIO", os.fdopen(descriptor, "r+b", buffering=0))
    if os.fstat(descriptor).st_size == 0:
        handle.write(b"\0")
        handle.flush()
    return handle


def _try_lock(handle: BinaryIO) -> bool:
    handle.seek(0)
    try:
        if os.name == "nt":
            if WINDOWS_LOCK_API is None:  # pragma: no cover - platform invariant
                raise RuntimeError("Windows lock API is unavailable")
            WINDOWS_LOCK_API.locking(
                handle.fileno(),
                WINDOWS_LOCK_API.LK_NBLCK,
                LOCKED_BYTE_COUNT,
            )
        else:
            if POSIX_LOCK_API is None:  # pragma: no cover - platform invariant
                raise RuntimeError("POSIX lock API is unavailable")
            POSIX_LOCK_API.flock(
                handle.fileno(),
                POSIX_LOCK_API.LOCK_EX | POSIX_LOCK_API.LOCK_NB,
            )
    except OSError as exc:
        if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
            return False
        raise
    return True


def _unlock(handle: BinaryIO) -> None:
    handle.seek(0)
    if os.name == "nt":
        if WINDOWS_LOCK_API is None:  # pragma: no cover - platform invariant
            raise RuntimeError("Windows lock API is unavailable")
        WINDOWS_LOCK_API.locking(
            handle.fileno(),
            WINDOWS_LOCK_API.LK_UNLCK,
            LOCKED_BYTE_COUNT,
        )
    else:
        if POSIX_LOCK_API is None:  # pragma: no cover - platform invariant
            raise RuntimeError("POSIX lock API is unavailable")
        POSIX_LOCK_API.flock(handle.fileno(), POSIX_LOCK_API.LOCK_UN)


def _write_owner(handle: BinaryIO, owner: DatabaseLockOwner) -> None:
    payload = json.dumps(
        {
            "command": owner.command,
            "pid": owner.pid,
            "role": owner.role.value,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    handle.seek(LOCKED_BYTE_COUNT)
    handle.truncate()
    handle.write(payload)
    handle.flush()
    os.fsync(handle.fileno())


def _clear_owner(handle: BinaryIO) -> None:
    handle.seek(LOCKED_BYTE_COUNT)
    handle.truncate()
    handle.flush()
    os.fsync(handle.fileno())


def _read_owner(handle: BinaryIO) -> DatabaseLockOwner | None:
    try:
        handle.seek(LOCKED_BYTE_COUNT)
        raw = handle.read()
        payload = json.loads(raw.decode("ascii"))
        if not isinstance(payload, dict):
            return None
        pid = payload.get("pid")
        role = payload.get("role")
        command = payload.get("command")
        if not isinstance(pid, int) or not isinstance(role, str) or not isinstance(command, str):
            return None
        return DatabaseLockOwner(
            pid=pid,
            role=DatabaseLockRole(role),
            command=_required_command(command),
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return None


def _required_command(value: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError("Lock command must be a non-empty string")
    return normalized


def _locked_message(owner: DatabaseLockOwner | None) -> str:
    if owner is None:
        return "Engram database is locked by another process; wait for it to finish"
    if owner.role is DatabaseLockRole.DAEMON:
        return f"Engram daemon is active (pid {owner.pid}); stop it before an offline write"
    return (
        f"Engram offline writer '{owner.command}' is active (pid {owner.pid}); "
        "wait for it to finish"
    )
