# Copyright 2026 Julien Bombled
# SPDX-License-Identifier: Apache-2.0

"""Prove that an open which fails leaves no connection behind.

Every failure branch of the two open paths closes the connection before it
propagates. A leaked connection is not a visible failure: it holds a file
handle and, in write mode, keeps SQLite state alive against a database the
caller believes it never touched.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, cast

import pytest

from engram import db
from engram.config import DatabaseConfig
from engram.db import DatabaseError, SQLiteVersionError, open_database, open_database_read_only


def _record_connections(monkeypatch: pytest.MonkeyPatch) -> list[sqlite3.Connection]:
    """Capture every connection SQLite hands out during one open attempt."""
    opened: list[sqlite3.Connection] = []
    original = sqlite3.connect

    def _connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:  # noqa: ANN401
        connection = cast("sqlite3.Connection", original(*args, **kwargs))
        opened.append(connection)
        return connection

    monkeypatch.setattr(sqlite3, "connect", _connect)
    return opened


def _assert_all_closed(opened: list[sqlite3.Connection]) -> None:
    assert opened, "no connection was opened, so the test proved nothing"
    for connection in opened:
        with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
            connection.execute("SELECT 1")


@pytest.mark.parametrize(
    ("raised", "surfaces_as"),
    [
        (DatabaseError("refused by validation"), DatabaseError),
        (SQLiteVersionError("below the floor"), SQLiteVersionError),
        (sqlite3.OperationalError("disk I/O error"), DatabaseError),
        (KeyboardInterrupt(), KeyboardInterrupt),
    ],
)
def test_a_failed_write_open_closes_its_connection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    raised: BaseException,
    surfaces_as: type[BaseException],
) -> None:
    config = DatabaseConfig(path=tmp_path / "engram.db")
    opened = _record_connections(monkeypatch)

    def _explode(*_args: object, **_kwargs: object) -> None:
        raise raised

    monkeypatch.setattr(db, "_configure_connection", _explode)

    with pytest.raises(surfaces_as):
        open_database(config)

    _assert_all_closed(opened)


@pytest.mark.parametrize(
    ("raised", "surfaces_as"),
    [
        (DatabaseError("refused by validation"), DatabaseError),
        (SQLiteVersionError("below the floor"), SQLiteVersionError),
        (sqlite3.OperationalError("disk I/O error"), DatabaseError),
        (KeyboardInterrupt(), KeyboardInterrupt),
    ],
)
def test_a_failed_read_only_open_closes_its_connection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    raised: BaseException,
    surfaces_as: type[BaseException],
) -> None:
    config = DatabaseConfig(path=tmp_path / "engram.db")
    open_database(config).close()
    opened = _record_connections(monkeypatch)

    def _explode(*_args: object, **_kwargs: object) -> None:
        raise raised

    monkeypatch.setattr(db, "_configure_read_only_connection", _explode)

    with pytest.raises(surfaces_as):
        open_database_read_only(config)

    _assert_all_closed(opened)


def test_a_read_only_open_of_a_missing_database_names_the_path(tmp_path: Path) -> None:
    """Read-only mode never creates the file, so a missing one must be said out loud."""
    missing = tmp_path / "absent" / "engram.db"

    with pytest.raises(DatabaseError, match="does not exist") as failure:
        open_database_read_only(DatabaseConfig(path=missing))

    assert str(missing.resolve()) in str(failure.value)


@pytest.mark.parametrize(
    "opener",
    [open_database, open_database_read_only],
    ids=["write", "read-only"],
)
def test_a_refused_connect_is_reported_as_a_database_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    opener: object,
) -> None:
    """A driver-level refusal must not reach the caller as a bare sqlite3 error."""
    config = DatabaseConfig(path=tmp_path / "engram.db")
    open_database(config).close()

    def _refuse(*_args: object, **_kwargs: object) -> None:
        raise sqlite3.OperationalError("unable to open database file")

    monkeypatch.setattr(sqlite3, "connect", _refuse)

    with pytest.raises(DatabaseError, match="open failed"):
        opener(config)  # type: ignore[operator]
