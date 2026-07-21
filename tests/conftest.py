# Copyright 2026 Julien Bombled
# SPDX-License-Identifier: Apache-2.0

"""Shared deterministic fixtures for storage tests."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest

import engram.db as db_module
from engram.config import (
    AppConfig,
    DatabaseConfig,
    LimitsConfig,
    LoggingConfig,
    TtlConfig,
)
from engram.store import EngramStore


@dataclass(slots=True)
class MutableClock:
    """Clock whose current instant can be advanced by a test."""

    current: datetime

    def __call__(self) -> datetime:
        return self.current


@pytest.fixture
def clock() -> MutableClock:
    """Return a fixed UTC clock."""
    return MutableClock(datetime(2026, 7, 21, 12, 0, tzinfo=UTC))


@pytest.fixture
def app_config(tmp_path: Path) -> AppConfig:
    """Return a complete configuration rooted in a temporary directory."""
    return AppConfig(
        database=DatabaseConfig(path=tmp_path / "engram.db", busy_timeout_ms=5000),
        ttl_days=TtlConfig(
            preference=0,
            decision=0,
            fact=0,
            project_state=30,
            episode=7,
        ),
        limits=LimitsConfig(max_statement_chars=2000, max_subject_keys=8),
        logging=LoggingConfig(
            path=tmp_path / "logs" / "engram.log",
            file_level="INFO",
            console_level="WARNING",
        ),
    )


@pytest.fixture
def sqlite_runtime_compatibility(monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate storage semantics from the older SQLite bundled by the test host."""

    def accept_runtime(
        connection: sqlite3.Connection,
        minimum: tuple[int, int, int] = db_module.MINIMUM_SQLITE_VERSION,
    ) -> None:
        del connection, minimum

    monkeypatch.setattr(db_module, "verify_sqlite_version", accept_runtime)


@pytest.fixture
def store(
    app_config: AppConfig,
    clock: MutableClock,
    sqlite_runtime_compatibility: None,
) -> Iterator[EngramStore]:
    """Open and reliably close a deterministic store."""
    del sqlite_runtime_compatibility
    instance = EngramStore(app_config, clock=clock)
    try:
        yield instance
    finally:
        instance.close()
