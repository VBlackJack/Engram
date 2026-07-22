# Copyright 2026 Julien Bombled
# SPDX-License-Identifier: Apache-2.0

"""SQLite connection safeguards and transactional migrations."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from .config import DatabaseConfig

MINIMUM_SQLITE_VERSION = (3, 51, 3)
SQLITE_VERSION_COMPONENTS = 3
FTS_TABLE_NAME = "entries_fts"
CREATE_FTS_TABLE_SQL = """
CREATE VIRTUAL TABLE entries_fts USING fts5(
    statement,
    subject_keys,
    content='entries',
    content_rowid='rowid',
    tokenize='unicode61 remove_diacritics 2'
)
"""
CREATE_VECTOR_TABLE_SQL = """
CREATE TABLE entry_vectors (
    entry_id TEXT NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
    model TEXT NOT NULL,
    dim INTEGER NOT NULL CHECK (dim > 0),
    vector BLOB NOT NULL,
    PRIMARY KEY (entry_id, model)
)
"""


class SQLiteVersionError(RuntimeError):
    """Raised when the SQLite runtime is affected by the WAL-reset bug."""


class DatabaseError(RuntimeError):
    """Raised when database setup or migration cannot complete safely."""


@dataclass(frozen=True, slots=True)
class Migration:
    """A numbered group of SQL statements applied atomically."""

    version: int
    statements: tuple[str, ...]


MIGRATIONS = (
    Migration(
        version=1,
        statements=(
            """
            CREATE TABLE entries (
                id TEXT PRIMARY KEY,
                kind TEXT NOT NULL CHECK (
                    kind IN ('preference', 'decision', 'project_state', 'fact', 'episode')
                ),
                scope TEXT NOT NULL,
                statement TEXT NOT NULL,
                subject_keys TEXT NOT NULL DEFAULT '[]',
                status TEXT NOT NULL CHECK (
                    status IN ('active', 'superseded', 'quarantined', 'expired')
                ),
                promotion_state TEXT NOT NULL CHECK (
                    promotion_state IN ('candidate', 'approved', 'rejected', 'promoted')
                ),
                source_type TEXT NOT NULL CHECK (
                    source_type IN (
                        'human', 'tool_verified', 'model_inferred', 'session_summary'
                    )
                ),
                writer_model TEXT,
                confidence TEXT NOT NULL CHECK (confidence IN ('high', 'medium', 'low')),
                observed_at TEXT,
                recorded_at TEXT NOT NULL,
                valid_from TEXT,
                valid_until TEXT,
                expires_at TEXT,
                idempotency_key TEXT NOT NULL UNIQUE,
                supersedes TEXT NOT NULL DEFAULT '[]',
                evidence TEXT NOT NULL DEFAULT '[]',
                datacron_ref TEXT,
                datacron_hash TEXT,
                synced_at TEXT
            )
            """,
            """
            CREATE TABLE audit_log (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                actor TEXT NOT NULL,
                action TEXT NOT NULL CHECK (
                    action IN (
                        'insert', 'attest', 'confidence_capped', 'idempotent_noop',
                        'supersede', 'expire', 'purge'
                    )
                ),
                entry_id TEXT,
                detail_hash TEXT
            )
            """,
            """
            CREATE INDEX entries_expiration_idx
            ON entries(status, expires_at)
            """,
        ),
    ),
    Migration(
        version=2,
        statements=(
            CREATE_FTS_TABLE_SQL,
            "INSERT INTO entries_fts(entries_fts) VALUES ('rebuild')",
            CREATE_VECTOR_TABLE_SQL,
        ),
    ),
    Migration(
        version=3,
        statements=(
            """
            ALTER TABLE entries
            ADD COLUMN is_stale INTEGER NOT NULL DEFAULT 0
            CHECK (is_stale IN (0, 1))
            """,
            "ALTER TABLE audit_log RENAME TO audit_log_v2",
            """
            CREATE TABLE audit_log (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                actor TEXT NOT NULL,
                action TEXT NOT NULL CHECK (
                    action IN (
                        'insert', 'attest', 'confidence_capped', 'idempotent_noop',
                        'supersede', 'expire', 'purge', 'promote', 'mark_stale',
                        'mark_fresh'
                    )
                ),
                entry_id TEXT,
                detail_hash TEXT
            )
            """,
            """
            INSERT INTO audit_log(seq, ts, actor, action, entry_id, detail_hash)
            SELECT seq, ts, actor, action, entry_id, detail_hash
            FROM audit_log_v2
            ORDER BY seq
            """,
            "DROP TABLE audit_log_v2",
        ),
    ),
    Migration(
        version=4,
        statements=(
            """
            CREATE TABLE consolidation_plans (
                plan_id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                snapshot_json TEXT NOT NULL,
                snapshot_hash TEXT NOT NULL,
                consumed_at TEXT
            )
            """,
            """
            CREATE INDEX consolidation_plans_consumed_idx
            ON consolidation_plans(consumed_at, created_at)
            """,
        ),
    ),
)


def open_database(config: DatabaseConfig) -> sqlite3.Connection:
    """Open, validate, configure, and migrate an Engram database."""
    config.path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(
        config.path,
        timeout=config.busy_timeout_ms / 1000,
        isolation_level=None,
        check_same_thread=False,
    )
    connection.row_factory = sqlite3.Row
    try:
        verify_sqlite_version(connection)
        _configure_connection(connection, config.busy_timeout_ms)
        apply_migrations(connection)
        ensure_derived_indexes(connection)
    except BaseException:
        connection.close()
        raise
    return connection


def open_database_read_only(config: DatabaseConfig) -> sqlite3.Connection:
    """Open an existing migrated database without taking a SQLite write transaction."""
    database_path = config.path.expanduser().resolve()
    if not database_path.is_file():
        raise DatabaseError(f"Engram database does not exist: {database_path}")
    connection = sqlite3.connect(
        f"{database_path.as_uri()}?mode=ro",
        uri=True,
        timeout=config.busy_timeout_ms / 1000,
        isolation_level=None,
        check_same_thread=False,
    )
    connection.row_factory = sqlite3.Row
    try:
        verify_sqlite_version(connection)
        _configure_read_only_connection(connection, config.busy_timeout_ms)
        _validate_read_only_schema(connection)
    except BaseException:
        connection.close()
        raise
    return connection


def verify_sqlite_version(
    connection: sqlite3.Connection,
    minimum: tuple[int, int, int] = MINIMUM_SQLITE_VERSION,
) -> None:
    """Fail closed when SQLite predates the WAL-reset bug fix."""
    actual_text = _read_sqlite_version(connection)
    actual = _parse_sqlite_version(actual_text)
    if actual < minimum:
        minimum_text = ".".join(str(part) for part in minimum)
        raise SQLiteVersionError(
            "SQLite "
            f"{minimum_text} or newer is required; found {actual_text}. "
            "Older runtimes are rejected because they do not contain the WAL-reset bug fix. "
            "See docs/en/installation-windows.md for supported Windows installation steps."
        )


def apply_migrations(connection: sqlite3.Connection) -> None:
    """Apply each pending numbered migration in its own transaction."""
    _ensure_version_table(connection)
    current_version = _schema_version(connection)
    latest_version = MIGRATIONS[-1].version if MIGRATIONS else 0
    if current_version > latest_version:
        raise DatabaseError(
            f"Database schema version {current_version} is newer than supported "
            f"version {latest_version}"
        )

    for migration in MIGRATIONS:
        if migration.version <= current_version:
            continue
        with transaction(connection):
            for statement in migration.statements:
                connection.execute(statement)
            connection.execute("UPDATE schema_version SET version = ?", (migration.version,))
        current_version = migration.version


def ensure_derived_indexes(connection: sqlite3.Connection) -> None:
    """Recreate missing derived index tables from canonical entries."""
    if not _table_exists(connection, FTS_TABLE_NAME):
        rebuild_fts_index(connection)
    if not _table_exists(connection, "entry_vectors"):
        with transaction(connection):
            connection.execute(CREATE_VECTOR_TABLE_SQL)


def rebuild_fts_index(connection: sqlite3.Connection) -> None:
    """Drop and rebuild the external-content FTS table transactionally."""
    with transaction(connection):
        connection.execute("DROP TABLE IF EXISTS entries_fts")
        connection.execute(CREATE_FTS_TABLE_SQL)
        connection.execute("INSERT INTO entries_fts(entries_fts) VALUES ('rebuild')")


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_schema WHERE type = 'table' AND name = ?",
        (name,),
    ).fetchone()
    return row is not None


@contextmanager
def transaction(connection: sqlite3.Connection) -> Iterator[None]:
    """Run a fail-closed immediate transaction."""
    if connection.in_transaction:
        raise DatabaseError("Nested database transactions are not supported")
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()


def _read_sqlite_version(connection: sqlite3.Connection) -> str:
    row = connection.execute("SELECT sqlite_version()").fetchone()
    if row is None:
        raise SQLiteVersionError("SQLite did not report a runtime version")
    return str(row[0])


def _parse_sqlite_version(version: str) -> tuple[int, int, int]:
    parts = version.split(".")
    if len(parts) < SQLITE_VERSION_COMPONENTS:
        raise SQLiteVersionError(f"SQLite reported an invalid runtime version: {version}")
    try:
        return int(parts[0]), int(parts[1]), int(parts[2])
    except ValueError as exc:
        raise SQLiteVersionError(f"SQLite reported an invalid runtime version: {version}") from exc


def _configure_connection(connection: sqlite3.Connection, busy_timeout_ms: int) -> None:
    journal_mode_row = connection.execute("PRAGMA journal_mode = WAL").fetchone()
    journal_mode = "" if journal_mode_row is None else str(journal_mode_row[0]).lower()
    if journal_mode != "wal":
        raise DatabaseError(f"SQLite refused WAL journal mode: {journal_mode or 'unknown'}")
    connection.execute(f"PRAGMA busy_timeout = {busy_timeout_ms:d}")
    connection.execute("PRAGMA foreign_keys = ON")
    foreign_keys_row = connection.execute("PRAGMA foreign_keys").fetchone()
    if foreign_keys_row is None or int(foreign_keys_row[0]) != 1:
        raise DatabaseError("SQLite foreign key enforcement could not be enabled")


def _configure_read_only_connection(
    connection: sqlite3.Connection,
    busy_timeout_ms: int,
) -> None:
    connection.execute(f"PRAGMA busy_timeout = {busy_timeout_ms:d}")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA query_only = ON")


def _validate_read_only_schema(connection: sqlite3.Connection) -> None:
    if not _table_exists(connection, "schema_version"):
        raise DatabaseError("Engram database has no schema version")
    current_version = _schema_version(connection)
    latest_version = MIGRATIONS[-1].version if MIGRATIONS else 0
    if current_version != latest_version:
        raise DatabaseError(
            f"Engram database schema version {current_version} requires offline migration "
            f"to version {latest_version}"
        )
    missing_tables = [
        name
        for name in (
            "entries",
            "audit_log",
            FTS_TABLE_NAME,
            "entry_vectors",
            "consolidation_plans",
        )
        if not _table_exists(connection, name)
    ]
    if missing_tables:
        missing = ", ".join(missing_tables)
        raise DatabaseError(f"Engram database is missing required tables: {missing}")


def _ensure_version_table(connection: sqlite3.Connection) -> None:
    with transaction(connection):
        connection.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
        row = connection.execute("SELECT COUNT(*) FROM schema_version").fetchone()
        row_count = 0 if row is None else int(row[0])
        if row_count == 0:
            connection.execute("INSERT INTO schema_version(version) VALUES (0)")
        elif row_count != 1:
            raise DatabaseError("schema_version must contain exactly one row")


def _schema_version(connection: sqlite3.Connection) -> int:
    row = connection.execute("SELECT version FROM schema_version").fetchone()
    if row is None:
        raise DatabaseError("schema_version is empty")
    return int(row[0])
