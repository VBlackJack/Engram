# Copyright 2026 Julien Bombled
# SPDX-License-Identifier: Apache-2.0

"""File and console logging without print-based fallbacks."""

from __future__ import annotations

import importlib
import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path
from types import TracebackType
from typing import BinaryIO, Protocol, Self, cast

from .config import LoggingConfig

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
MAX_LOG_BYTES = 10 * 1024 * 1024
LOG_BACKUP_COUNT = 5
LOCKED_BYTE_COUNT = 1


class _WindowsLockApi(Protocol):
    LK_LOCK: int
    LK_UNLCK: int

    def locking(self, fd: int, mode: int, nbytes: int) -> None: ...


class _PosixLockApi(Protocol):
    LOCK_EX: int
    LOCK_UN: int

    def flock(self, fd: int, operation: int) -> None: ...


WINDOWS_LOCK_API = (
    cast("_WindowsLockApi", importlib.import_module("msvcrt")) if os.name == "nt" else None
)
POSIX_LOCK_API = (
    cast("_PosixLockApi", importlib.import_module("fcntl")) if os.name != "nt" else None
)


class _InterProcessFileLock:
    """Hold one blocking OS lock over a persistent coordination byte."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._handle: BinaryIO | None = None

    def __enter__(self) -> Self:
        binary_flag = getattr(os, "O_BINARY", 0)
        descriptor = os.open(
            self._path,
            os.O_CREAT | os.O_RDWR | binary_flag,
            0o600,
        )
        try:
            if os.fstat(descriptor).st_size == 0:
                os.write(descriptor, b"\0")
            handle = cast("BinaryIO", os.fdopen(descriptor, "r+b", buffering=0))
        except BaseException:
            os.close(descriptor)
            raise
        try:
            _lock_file(handle)
        except BaseException:
            handle.close()
            raise
        self._handle = handle
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc_value, traceback
        handle = self._handle
        if handle is None:
            return
        self._handle = None
        try:
            _unlock_file(handle)
        finally:
            handle.close()


def _lock_file(handle: BinaryIO) -> None:
    handle.seek(0)
    if os.name == "nt":
        if WINDOWS_LOCK_API is None:  # pragma: no cover - platform invariant
            raise RuntimeError("Windows lock API is unavailable")
        WINDOWS_LOCK_API.locking(
            handle.fileno(),
            WINDOWS_LOCK_API.LK_LOCK,
            LOCKED_BYTE_COUNT,
        )
        return
    if POSIX_LOCK_API is None:  # pragma: no cover - platform invariant
        raise RuntimeError("POSIX lock API is unavailable")
    POSIX_LOCK_API.flock(handle.fileno(), POSIX_LOCK_API.LOCK_EX)


def _unlock_file(handle: BinaryIO) -> None:
    handle.seek(0)
    if os.name == "nt":
        if WINDOWS_LOCK_API is None:  # pragma: no cover - platform invariant
            raise RuntimeError("Windows lock API is unavailable")
        WINDOWS_LOCK_API.locking(
            handle.fileno(),
            WINDOWS_LOCK_API.LK_UNLCK,
            LOCKED_BYTE_COUNT,
        )
        return
    if POSIX_LOCK_API is None:  # pragma: no cover - platform invariant
        raise RuntimeError("POSIX lock API is unavailable")
    POSIX_LOCK_API.flock(handle.fileno(), POSIX_LOCK_API.LOCK_UN)


class InterProcessRotatingFileHandler(RotatingFileHandler):
    """Rotate one shared log only while every Engram process has closed it."""

    def __init__(
        self,
        filename: str | Path,
        *,
        max_bytes: int,
        backup_count: int,
        encoding: str,
    ) -> None:
        """Configure delayed opens so only the lock owner holds the log file."""
        super().__init__(
            filename,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding=encoding,
            delay=True,
        )
        self._rotation_lock_path = Path(f"{self.baseFilename}.rotate.lock")

    def emit(self, record: logging.LogRecord) -> None:
        """Serialize open/write/rollover/close across local processes."""
        try:
            with _InterProcessFileLock(self._rotation_lock_path):
                try:
                    super().emit(record)
                finally:
                    if self.stream is not None:
                        self.stream.close()
                        self.stream = None
        except Exception:  # noqa: BLE001 - logging failures must never escape emit
            self.handleError(record)

    def validate_target(self) -> None:
        """Prove the lock and log targets are writable before accepting work."""
        with _InterProcessFileLock(self._rotation_lock_path):
            stream = self._open()
            stream.close()


class FileLogger:
    """Configure one logger with independent file and console levels."""

    def __init__(self, config: LoggingConfig, *, name: str = "engram") -> None:
        """Store the immutable logging configuration."""
        self._config = config
        self._name = name

    def configure(self) -> logging.Logger:
        """Create file and console handlers and return the configured logger."""
        self._config.path.parent.mkdir(parents=True, exist_ok=True)
        logger = logging.getLogger(self._name)
        for handler in logger.handlers[:]:
            logger.removeHandler(handler)
            handler.close()
        logger.propagate = False

        file_level = _log_level(self._config.file_level)
        console_level = _log_level(self._config.console_level)
        logger.setLevel(min(file_level, console_level))

        formatter = logging.Formatter(LOG_FORMAT)
        file_handler = InterProcessRotatingFileHandler(
            self._config.path,
            max_bytes=MAX_LOG_BYTES,
            backup_count=LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
        file_handler.validate_target()
        file_handler.setLevel(file_level)
        file_handler.setFormatter(formatter)

        console_handler = logging.StreamHandler()
        console_handler.setLevel(console_level)
        console_handler.setFormatter(formatter)

        logger.addHandler(file_handler)
        logger.addHandler(console_handler)
        return logger

    @property
    def path(self) -> Path:
        """Return the configured log file path."""
        return self._config.path


def _log_level(name: str) -> int:
    level = logging.getLevelNamesMapping().get(name.upper())
    if level is None:
        raise ValueError(f"Unsupported log level: {name}")
    return level
