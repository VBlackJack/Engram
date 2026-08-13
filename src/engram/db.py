# Copyright 2026 Julien Bombled
# SPDX-License-Identifier: Apache-2.0

"""SQLite connection safeguards and transactional migrations."""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime
from functools import lru_cache
from pathlib import Path
from tempfile import TemporaryDirectory

from .config import DatabaseConfig, LimitsConfig
from .models import (
    PROJECT_STATE_CLAIM_KEY,
    AuditAction,
    Confidence,
    EntryKind,
    EntryStatus,
    EvidenceType,
    PromotionState,
    SourceType,
)
from .normalization import (
    HARD_MAX_STATEMENT_CHARS,
    HARD_MAX_SUBJECT_KEYS,
    MAX_ACTOR_CHARS,
    MAX_CLAIM_KEY_CHARS,
    MAX_DATACRON_REF_CHARS,
    MAX_EVIDENCE_ITEMS,
    MAX_EVIDENCE_REF_CHARS,
    MAX_SCOPE_CHARS,
    MAX_SUBJECT_KEY_CHARS,
    MAX_WRITER_MODEL_CHARS,
    ULID_LENGTH,
    StoreValidationError,
    canonical_key,
    normalize_actor,
    normalize_datacron_ref,
    normalize_entry_id,
    normalize_evidence_ref,
    normalize_scope,
    normalize_sha256_hex,
    normalize_statement,
    normalize_subject_keys,
    normalize_writer_model,
    validate_persisted_content,
)

LOGGER = logging.getLogger(__name__)
MINIMUM_SQLITE_VERSION = (3, 51, 3)
# A refusal has to be actionable for someone who installed a wheel and has no
# checkout, so it names a document that can be opened rather than a repository
# path that only exists for contributors.
WINDOWS_SQLITE_GUIDE_URL = (
    "https://github.com/VBlackJack/Engram/blob/main/docs/en/installation-windows.md"
)
SQLITE_VERSION_COMPONENTS = 3
LIFECYCLE_SCHEMA_VERSION = 5
MINIMUM_PREFLIGHT_SCHEMA_VERSION = 3
CONSOLIDATION_SCHEMA_VERSION = 4
SHA256_HEX_LENGTH = 64
MAX_TEMPORAL_TEXT_CHARS = 64
MAX_JSON_ESCAPE_CHARS = 6
MAX_UTF8_BYTES_PER_CHAR = 4
MAX_SCHEMA_SQL_BYTES = 256 * 1024
MAX_SQLITE_VALUE_BYTES = 8 * 1024 * 1024
MAX_CONSOLIDATION_SNAPSHOT_BYTES = 4 * 1024 * 1024
LEGACY_CLAIM_HASH_CHARS = len("legacy:") + SHA256_HEX_LENGTH
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
    dim INTEGER NOT NULL CHECK (dim BETWEEN 1 AND 8192),
    vector BLOB NOT NULL CHECK (
        typeof(vector) = 'blob' AND octet_length(vector) = dim * 4
    ),
    PRIMARY KEY (entry_id, model)
)
"""
CANONICAL_V5_TABLES = (
    "entries",
    "audit_log",
    "consolidation_plans",
    "entry_observations",
    "entry_supersessions",
)
REQUIRED_V5_TRIGGERS = {
    "entries_lifecycle_insert": "entries",
    "entries_lifecycle_update": "entries",
    "entries_immutable_identity": "entries",
    "entries_claim_insert": "entries",
    "entries_claim_update": "entries",
    "entry_supersessions_validate": "entry_supersessions",
    "entry_supersessions_no_update": "entry_supersessions",
    "entry_supersessions_sync_insert": "entry_supersessions",
    "entry_supersessions_sync_delete": "entry_supersessions",
}
REQUIRED_V5_PARTIAL_INDEX_FRAGMENTS = {
    "entries_live_candidate_owner_idx": (
        "CREATE UNIQUE INDEX",
        "ON ENTRIES(CANONICAL_KEY, WRITER_MODEL)",
        "WHERE STATUS = 'QUARANTINED' AND PROMOTION_STATE = 'CANDIDATE'",
    ),
    "entries_live_trusted_canonical_idx": (
        "CREATE UNIQUE INDEX",
        "ON ENTRIES(CANONICAL_KEY)",
        "WHERE STATUS = 'ACTIVE'",
    ),
}


class SQLiteVersionError(RuntimeError):
    """Raised when the SQLite runtime is affected by the WAL-reset bug."""


class DatabaseError(RuntimeError):
    """Raised when database setup or migration cannot complete safely."""


@dataclass(frozen=True, slots=True)
class UpgradePreflightReport:
    """Read-only compatibility result for one supported persisted schema."""

    schema_version: int
    target_schema_version: int
    fts_rebuild_required: bool | None
    vector_rebuild_required: bool


@dataclass(frozen=True, slots=True)
class _SupersessionNode:
    """Compact canonical projection retained while validating an edge graph."""

    kind: str
    scope: str
    status: str
    promotion_state: str
    source_type: str
    canonical_key: str
    claim_key: str | None
    claim_key_present: bool


@dataclass(frozen=True, slots=True)
class _SchemaObjectDefinition:
    """Bounded sqlite_schema projection for one exact package-owned name."""

    object_type: str
    table_matches: bool
    sql: str | None
    definition_within_bound: bool


@dataclass(frozen=True, slots=True)
class Migration:
    """A numbered group of SQL statements applied atomically."""

    version: int
    statements: tuple[str, ...]
    preflight: Callable[[sqlite3.Connection, LimitsConfig | None], None] | None = None


def _run_v5_preflight(
    connection: sqlite3.Connection,
    limits: LimitsConfig | None,
) -> None:
    _preflight_lifecycle_v5(connection, limits=limits)


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
    Migration(
        version=5,
        preflight=_run_v5_preflight,
        statements=(
            "ALTER TABLE entries ADD COLUMN canonical_key TEXT",
            "ALTER TABLE entries ADD COLUMN claim_key TEXT",
            "UPDATE entries SET canonical_key = idempotency_key",
            """
            UPDATE entries
            SET claim_key = 'project_state/current'
            WHERE kind = 'project_state'
              AND promotion_state IN ('approved', 'promoted')
            """,
            """
            CREATE TABLE entry_observations (
                entry_id TEXT NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
                writer_model TEXT NOT NULL,
                claim_hash TEXT NOT NULL,
                recorded_at TEXT NOT NULL,
                confidence TEXT NOT NULL CHECK (confidence IN ('high', 'medium', 'low')),
                observed_at TEXT,
                valid_from TEXT,
                valid_until TEXT,
                subject_keys TEXT NOT NULL DEFAULT '[]',
                evidence TEXT NOT NULL DEFAULT '[]',
                PRIMARY KEY (entry_id, writer_model, claim_hash)
            )
            """,
            """
            INSERT INTO entry_observations(
                entry_id, writer_model, claim_hash, recorded_at, confidence,
                observed_at, valid_from, valid_until, subject_keys, evidence
            )
            SELECT
                id, writer_model, 'legacy:' || idempotency_key, recorded_at, confidence,
                observed_at, valid_from, valid_until, subject_keys, evidence
            FROM entries
            WHERE writer_model IS NOT NULL
            """,
            """
            CREATE INDEX entry_observations_writer_idx
            ON entry_observations(writer_model, entry_id)
            """,
            """
            CREATE TABLE entry_supersessions (
                old_entry_id TEXT PRIMARY KEY
                    REFERENCES entries(id) ON DELETE CASCADE,
                new_entry_id TEXT NOT NULL
                    REFERENCES entries(id) ON DELETE CASCADE,
                recorded_at TEXT NOT NULL,
                actor TEXT NOT NULL,
                CHECK (old_entry_id != new_entry_id),
                CHECK (length(actor) > 0)
            )
            """,
            """
            INSERT INTO entry_supersessions(
                old_entry_id, new_entry_id, recorded_at, actor
            )
            SELECT
                CAST(link.value AS TEXT), replacement.id, replacement.recorded_at,
                'migration-v5'
            FROM entries AS replacement, json_each(replacement.supersedes) AS link
            """,
            """
            CREATE INDEX entry_supersessions_new_idx
            ON entry_supersessions(new_entry_id, old_entry_id)
            """,
            """
            CREATE INDEX entries_canonical_idx
            ON entries(canonical_key, recorded_at, id)
            """,
            """
            CREATE UNIQUE INDEX entries_live_candidate_owner_idx
            ON entries(canonical_key, writer_model)
            WHERE status = 'quarantined' AND promotion_state = 'candidate'
            """,
            """
            CREATE UNIQUE INDEX entries_live_trusted_canonical_idx
            ON entries(canonical_key)
            WHERE status = 'active'
            """,
            """
            CREATE INDEX entries_claim_family_idx
            ON entries(kind, scope, claim_key, status)
            WHERE claim_key IS NOT NULL
            """,
            "ALTER TABLE audit_log RENAME TO audit_log_v4",
            """
            CREATE TABLE audit_log (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                actor TEXT NOT NULL,
                action TEXT NOT NULL CHECK (
                    action IN (
                        'insert', 'attest', 'confidence_capped', 'idempotent_noop',
                        'supersede', 'expire', 'purge', 'promote', 'mark_stale',
                        'mark_fresh', 'corroborate', 'classify'
                    )
                ),
                entry_id TEXT,
                detail_hash TEXT
            )
            """,
            """
            INSERT INTO audit_log(seq, ts, actor, action, entry_id, detail_hash)
            SELECT seq, ts, actor, action, entry_id, detail_hash
            FROM audit_log_v4
            ORDER BY seq
            """,
            "DROP TABLE audit_log_v4",
            """
            CREATE TRIGGER entries_lifecycle_insert
            BEFORE INSERT ON entries
            WHEN
                NEW.status NOT IN ('active', 'quarantined')
                OR NEW.canonical_key IS NULL
                OR length(NEW.canonical_key) != 64
                OR NOT (
                    (
                        NEW.status = 'quarantined'
                        AND NEW.promotion_state = 'candidate'
                        AND NEW.source_type IN ('model_inferred', 'session_summary')
                        AND NEW.writer_model IS NOT NULL
                        AND NEW.confidence != 'high'
                        AND NEW.claim_key IS NULL
                    )
                    OR (
                        NEW.status = 'active'
                        AND NEW.promotion_state IN ('approved', 'promoted')
                        AND NEW.source_type IN ('human', 'tool_verified')
                        AND NEW.writer_model IS NULL
                    )
                )
                OR NOT (
                    (
                        NEW.promotion_state = 'promoted'
                        AND NEW.datacron_ref IS NOT NULL
                        AND NEW.datacron_hash IS NOT NULL
                        AND NEW.synced_at IS NOT NULL
                    )
                    OR (
                        NEW.promotion_state != 'promoted'
                        AND NEW.datacron_ref IS NULL
                        AND NEW.datacron_hash IS NULL
                        AND NEW.synced_at IS NULL
                        AND NEW.is_stale = 0
                    )
                )
            BEGIN
                SELECT RAISE(ABORT, 'invalid entry lifecycle');
            END
            """,
            """
            CREATE TRIGGER entries_lifecycle_update
            BEFORE UPDATE ON entries
            WHEN
                NEW.canonical_key IS NULL
                OR length(NEW.canonical_key) != 64
                OR NOT (
                    (
                        NEW.promotion_state IN ('candidate', 'rejected')
                        AND NEW.source_type IN ('model_inferred', 'session_summary')
                        AND NEW.writer_model IS NOT NULL
                        AND NEW.confidence != 'high'
                        AND NEW.claim_key IS NULL
                        AND (
                            (
                                NEW.status = 'quarantined'
                                AND NEW.promotion_state = 'candidate'
                            )
                            OR NEW.status IN ('superseded', 'expired')
                        )
                    )
                    OR (
                        NEW.promotion_state IN ('approved', 'promoted')
                        AND NEW.source_type IN ('human', 'tool_verified')
                        AND NEW.writer_model IS NULL
                        AND (
                            NEW.status = 'active'
                            OR NEW.status IN ('superseded', 'expired')
                        )
                    )
                )
                OR NOT (
                    (
                        NEW.promotion_state = 'promoted'
                        AND NEW.datacron_ref IS NOT NULL
                        AND NEW.datacron_hash IS NOT NULL
                        AND NEW.synced_at IS NOT NULL
                    )
                    OR (
                        NEW.promotion_state != 'promoted'
                        AND NEW.datacron_ref IS NULL
                        AND NEW.datacron_hash IS NULL
                        AND NEW.synced_at IS NULL
                        AND NEW.is_stale = 0
                    )
                )
                OR NOT (
                    (
                        NEW.status = OLD.status
                        AND NEW.promotion_state = OLD.promotion_state
                        AND NEW.source_type = OLD.source_type
                        AND NEW.writer_model IS OLD.writer_model
                    )
                    OR (
                        OLD.status = 'active'
                        AND OLD.promotion_state = 'approved'
                        AND NEW.status = 'active'
                        AND NEW.promotion_state = 'promoted'
                        AND NEW.source_type = OLD.source_type
                        AND NEW.writer_model IS OLD.writer_model
                    )
                    OR (
                        OLD.status = 'quarantined'
                        AND OLD.promotion_state = 'candidate'
                        AND NEW.status = 'active'
                        AND NEW.promotion_state = 'approved'
                        AND NEW.source_type IN ('human', 'tool_verified')
                        AND NEW.writer_model IS NULL
                    )
                    OR (
                        OLD.status IN ('active', 'quarantined')
                        AND NEW.status IN ('superseded', 'expired')
                        AND (
                            NEW.status != 'superseded'
                            OR EXISTS (
                                SELECT 1
                                FROM entry_supersessions
                                WHERE old_entry_id = NEW.id
                            )
                        )
                        AND NEW.promotion_state = OLD.promotion_state
                        AND NEW.source_type = OLD.source_type
                        AND NEW.writer_model IS OLD.writer_model
                    )
                    OR (
                        OLD.status = 'superseded'
                        AND NEW.status = 'expired'
                        AND NEW.promotion_state = OLD.promotion_state
                        AND NEW.source_type = OLD.source_type
                        AND NEW.writer_model IS OLD.writer_model
                    )
                )
            BEGIN
                SELECT RAISE(ABORT, 'invalid entry lifecycle transition');
            END
            """,
            """
            CREATE TRIGGER entries_immutable_identity
            BEFORE UPDATE OF
                id, kind, scope, statement, recorded_at, expires_at,
                idempotency_key, canonical_key
            ON entries
            WHEN
                NEW.id != OLD.id
                OR NEW.kind != OLD.kind
                OR NEW.scope != OLD.scope
                OR NEW.statement != OLD.statement
                OR NEW.recorded_at != OLD.recorded_at
                OR NEW.expires_at IS NOT OLD.expires_at
                OR NEW.idempotency_key != OLD.idempotency_key
                OR NEW.canonical_key != OLD.canonical_key
            BEGIN
                SELECT RAISE(ABORT, 'entry identity is immutable');
            END
            """,
            f"""
            CREATE TRIGGER entries_claim_insert
            BEFORE INSERT ON entries
            WHEN
                NEW.status = 'active'
                AND (
                    (
                        NEW.kind = 'project_state'
                        AND NEW.claim_key IS NOT '{PROJECT_STATE_CLAIM_KEY}'
                    )
                    OR (
                        NEW.kind IN ('preference', 'decision', 'fact')
                        AND (
                            NEW.claim_key IS NULL
                            OR length(NEW.claim_key) = 0
                            OR length(NEW.claim_key) > 200
                        )
                    )
                    OR (
                        NEW.kind = 'episode'
                        AND NEW.claim_key IS NOT NULL
                    )
                )
            BEGIN
                SELECT RAISE(ABORT, 'trusted entry requires a valid claim_key');
            END
            """,
            f"""
            CREATE TRIGGER entries_claim_update
            BEFORE UPDATE ON entries
            WHEN
                (
                    OLD.claim_key IS NOT NULL
                    AND NEW.claim_key IS NOT OLD.claim_key
                )
                OR (
                    NEW.claim_key IS NOT NULL
                    AND (
                        (
                            NEW.kind = 'project_state'
                            AND NEW.claim_key IS NOT '{PROJECT_STATE_CLAIM_KEY}'
                        )
                        OR (
                            NEW.kind IN ('preference', 'decision', 'fact')
                            AND (
                                length(NEW.claim_key) = 0
                                OR length(NEW.claim_key) > 200
                            )
                        )
                        OR NEW.kind = 'episode'
                    )
                )
                OR (
                    NEW.status = 'active'
                    AND NEW.kind IN ('preference', 'decision', 'fact', 'project_state')
                    AND NEW.claim_key IS NULL
                )
            BEGIN
                SELECT RAISE(ABORT, 'trusted entry requires a stable claim_key');
            END
            """,
            """
            CREATE TRIGGER entry_supersessions_validate
            BEFORE INSERT ON entry_supersessions
            BEGIN
                SELECT RAISE(ABORT, 'supersession entries must share kind and scope')
                WHERE EXISTS (
                    SELECT 1
                    FROM entries AS old_entry
                    JOIN entries AS new_entry
                      ON new_entry.id = NEW.new_entry_id
                    WHERE old_entry.id = NEW.old_entry_id
                      AND (
                          old_entry.kind != new_entry.kind
                          OR old_entry.scope != new_entry.scope
                      )
                );
                SELECT RAISE(ABORT, 'supersession old entry must be open')
                WHERE EXISTS (
                    SELECT 1 FROM entries
                    WHERE id = NEW.old_entry_id
                      AND status NOT IN ('active', 'quarantined')
                );
                SELECT RAISE(
                    ABORT,
                    'candidate supersession must merge identical canonical content'
                )
                WHERE EXISTS (
                    SELECT 1
                    FROM entries AS old_entry
                    JOIN entries AS new_entry
                      ON new_entry.id = NEW.new_entry_id
                    WHERE old_entry.id = NEW.old_entry_id
                      AND old_entry.status = 'quarantined'
                      AND (
                          old_entry.promotion_state != 'candidate'
                          OR old_entry.source_type NOT IN (
                              'model_inferred', 'session_summary'
                          )
                          OR old_entry.canonical_key != new_entry.canonical_key
                      )
                );
                SELECT RAISE(ABORT, 'supersession replacement must be trusted')
                WHERE EXISTS (
                    SELECT 1 FROM entries
                    WHERE id = NEW.new_entry_id
                      AND NOT (
                          status = 'active'
                          AND promotion_state IN ('approved', 'promoted')
                          AND source_type IN ('human', 'tool_verified')
                          AND writer_model IS NULL
                      )
                );
                SELECT RAISE(ABORT, 'supersession entries must share claim_key')
                WHERE EXISTS (
                    SELECT 1
                    FROM entries AS old_entry
                    JOIN entries AS new_entry
                      ON new_entry.id = NEW.new_entry_id
                    WHERE old_entry.id = NEW.old_entry_id
                      AND old_entry.status = 'active'
                      AND old_entry.claim_key IS NOT new_entry.claim_key
                );
                SELECT RAISE(ABORT, 'supersession cycle detected')
                WHERE EXISTS (
                    WITH RECURSIVE descendants(entry_id) AS (
                        VALUES (NEW.new_entry_id)
                        UNION
                        SELECT link.new_entry_id
                        FROM entry_supersessions AS link
                        JOIN descendants
                          ON link.old_entry_id = descendants.entry_id
                    )
                    SELECT 1
                    FROM descendants
                    WHERE entry_id = NEW.old_entry_id
                );
            END
            """,
            """
            CREATE TRIGGER entry_supersessions_no_update
            BEFORE UPDATE ON entry_supersessions
            BEGIN
                SELECT RAISE(ABORT, 'supersession links are immutable');
            END
            """,
            """
            CREATE TRIGGER entry_supersessions_sync_insert
            AFTER INSERT ON entry_supersessions
            BEGIN
                UPDATE entries
                SET status = 'superseded'
                WHERE id = NEW.old_entry_id;
                UPDATE entries
                SET supersedes = (
                    SELECT json_group_array(old_entry_id)
                    FROM (
                        SELECT old_entry_id
                        FROM entry_supersessions
                        WHERE new_entry_id = NEW.new_entry_id
                        ORDER BY old_entry_id
                    )
                )
                WHERE id = NEW.new_entry_id;
            END
            """,
            """
            CREATE TRIGGER entry_supersessions_sync_delete
            AFTER DELETE ON entry_supersessions
            BEGIN
                UPDATE entries
                SET supersedes = (
                    SELECT json_group_array(old_entry_id)
                    FROM (
                        SELECT old_entry_id
                        FROM entry_supersessions
                        WHERE new_entry_id = OLD.new_entry_id
                        ORDER BY old_entry_id
                    )
                )
                WHERE id = OLD.new_entry_id;
            END
            """,
        ),
    ),
)


def latest_schema_version() -> int:
    """Return the latest schema version supported by this package."""
    return MIGRATIONS[-1].version if MIGRATIONS else 0


def open_database(
    config: DatabaseConfig,
    *,
    limits: LimitsConfig | None = None,
) -> sqlite3.Connection:
    """Open, validate, configure, and migrate an Engram database."""
    try:
        config.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(
            config.path,
            timeout=config.busy_timeout_ms / 1000,
            isolation_level=None,
            check_same_thread=False,
        )
    except (OSError, sqlite3.Error) as exc:
        raise DatabaseError(f"SQLite database open failed: {exc}") from exc
    connection.row_factory = sqlite3.Row
    try:
        _apply_sqlite_resource_limits(connection)
        verify_sqlite_version(connection)
        _configure_connection(connection, config.busy_timeout_ms)
        apply_migrations(connection, limits=limits)
        ensure_derived_indexes(connection)
        verify_database_integrity(
            connection,
            max_statement_chars=None if limits is None else limits.max_statement_chars,
            max_subject_keys=None if limits is None else limits.max_subject_keys,
        )
    except (DatabaseError, SQLiteVersionError):
        connection.close()
        raise
    except sqlite3.Error as exc:
        connection.close()
        raise DatabaseError(f"SQLite database initialization failed: {exc}") from exc
    except BaseException:
        connection.close()
        raise
    return connection


def open_database_read_only(
    config: DatabaseConfig,
    *,
    limits: LimitsConfig | None = None,
) -> sqlite3.Connection:
    """Open an existing migrated database without taking a SQLite write transaction."""
    database_path = config.path.expanduser().resolve()
    if not database_path.is_file():
        raise DatabaseError(f"Engram database does not exist: {database_path}")
    try:
        connection = sqlite3.connect(
            f"{database_path.as_uri()}?mode=ro",
            uri=True,
            timeout=config.busy_timeout_ms / 1000,
            isolation_level=None,
            check_same_thread=False,
        )
    except (OSError, sqlite3.Error) as exc:
        raise DatabaseError(f"SQLite read-only open failed: {exc}") from exc
    connection.row_factory = sqlite3.Row
    try:
        _apply_sqlite_resource_limits(connection)
        verify_sqlite_version(connection)
        _configure_read_only_connection(connection, config.busy_timeout_ms)
        _validate_read_only_schema(connection)
        verify_database_integrity(
            connection,
            max_statement_chars=None if limits is None else limits.max_statement_chars,
            max_subject_keys=None if limits is None else limits.max_subject_keys,
        )
    except (DatabaseError, SQLiteVersionError):
        connection.close()
        raise
    except sqlite3.Error as exc:
        connection.close()
        raise DatabaseError(f"SQLite read-only validation failed: {exc}") from exc
    except BaseException:
        connection.close()
        raise
    return connection


def preflight_database(
    config: DatabaseConfig,
    *,
    limits: LimitsConfig | None = None,
) -> UpgradePreflightReport:
    """Validate an upgrade read-only, including supported pre-v5 databases."""
    database_path = config.path.expanduser().resolve()
    if not database_path.is_file():
        raise DatabaseError(f"Engram database does not exist: {database_path}")
    try:
        connection = sqlite3.connect(
            f"{database_path.as_uri()}?mode=ro",
            uri=True,
            timeout=config.busy_timeout_ms / 1000,
            isolation_level=None,
            check_same_thread=False,
        )
    except (OSError, sqlite3.Error) as exc:
        raise DatabaseError(f"SQLite upgrade preflight failed: {exc}") from exc
    connection.row_factory = sqlite3.Row
    try:
        _apply_sqlite_resource_limits(connection)
        verify_sqlite_version(connection)
        _configure_read_only_connection(connection, config.busy_timeout_ms)
        connection.execute("BEGIN")
        if not _table_exists(connection, "schema_version"):
            raise DatabaseError("Engram database has no schema version")
        current_version = _schema_version(connection)
        latest_version = latest_schema_version()
        if current_version > latest_version:
            raise DatabaseError(
                f"Engram database schema version {current_version} is newer than "
                f"supported version {latest_version}"
            )
        if current_version < MINIMUM_PREFLIGHT_SCHEMA_VERSION:
            raise DatabaseError(
                f"Engram database schema version {current_version} requires a staged upgrade "
                "with Engram 2026.0721.04 before this preflight"
            )
        if current_version == latest_version:
            _validate_preflight_schema(connection, current_version)
        _simulate_upgrade(
            connection,
            busy_timeout_ms=config.busy_timeout_ms,
            limits=limits,
        )
        return UpgradePreflightReport(
            schema_version=current_version,
            target_schema_version=latest_version,
            fts_rebuild_required=(None if _fts_schema_matches(connection) else True),
            vector_rebuild_required=_vector_rebuild_required(connection),
        )
    except (OSError, sqlite3.Error) as exc:
        raise DatabaseError(f"SQLite upgrade preflight failed: {exc}") from exc
    finally:
        if connection.in_transaction:
            connection.rollback()
        connection.close()


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
            "Run 'engram doctor' for the repair that applies to this machine, or see "
            f"{WINDOWS_SQLITE_GUIDE_URL} for the Windows installation steps."
        )


def apply_migrations(
    connection: sqlite3.Connection,
    *,
    limits: LimitsConfig | None = None,
) -> None:
    """Apply every pending migration and final canonical validation atomically."""
    _ensure_version_table(connection)
    current_version = _schema_version(connection)
    latest_version = MIGRATIONS[-1].version if MIGRATIONS else 0
    if current_version > latest_version:
        raise DatabaseError(
            f"Database schema version {current_version} is newer than supported "
            f"version {latest_version}"
        )

    pending = tuple(migration for migration in MIGRATIONS if migration.version > current_version)
    if not pending:
        return
    with transaction(connection):
        for migration in pending:
            if migration.preflight is not None:
                migration.preflight(connection, limits)
            for statement in migration.statements:
                connection.execute(statement)
            connection.execute("UPDATE schema_version SET version = ?", (migration.version,))
        if latest_version >= LIFECYCLE_SCHEMA_VERSION:
            verify_database_integrity(
                connection,
                max_statement_chars=None if limits is None else limits.max_statement_chars,
                max_subject_keys=None if limits is None else limits.max_subject_keys,
            )


def ensure_derived_indexes(connection: sqlite3.Connection) -> None:
    """Recreate missing or inconsistent derived indexes from canonical entries."""
    if not _fts_schema_matches(connection):
        if _table_exists(connection, FTS_TABLE_NAME):
            LOGGER.warning("FTS table schema is unexpected; rebuilding the derived index")
        rebuild_fts_index(connection)
    else:
        try:
            _verify_fts_index(connection)
        except sqlite3.DatabaseError:
            LOGGER.warning("FTS index is inconsistent with canonical entries; rebuilding it")
            rebuild_fts_index(connection)
            try:
                _verify_fts_index(connection)
            except sqlite3.DatabaseError as exc:
                raise DatabaseError("FTS index remains inconsistent after rebuild") from exc
    if not _vector_schema_matches(connection):
        if _schema_object_type(connection, "entry_vectors") is not None:
            LOGGER.warning("Vector table schema is unexpected; rebuilding the empty derived index")
        _rebuild_vector_table(connection)


def _verify_fts_index(connection: sqlite3.Connection) -> None:
    """Compare the FTS5 index with its external canonical content table."""
    connection.execute("INSERT INTO entries_fts(entries_fts, rank) VALUES ('integrity-check', 1)")


def _fts_schema_matches(connection: sqlite3.Connection) -> bool:
    """Require the exact checked-in FTS5 external-content table definition."""
    return _table_schema_matches(connection, FTS_TABLE_NAME, CREATE_FTS_TABLE_SQL)


def _vector_schema_matches(connection: sqlite3.Connection) -> bool:
    """Require the exact checked-in derived vector table definition."""
    return _table_schema_matches(connection, "entry_vectors", CREATE_VECTOR_TABLE_SQL)


def _table_schema_matches(
    connection: sqlite3.Connection,
    name: str,
    expected_sql: str,
) -> bool:
    """Compare one table with a normalized checked-in schema definition."""
    definition = _read_bounded_schema_object(
        connection,
        name=name,
        expected_table=name,
    )
    if definition is None:
        return False
    if (
        definition.object_type != "table"
        or not definition.table_matches
        or not definition.definition_within_bound
        or definition.sql is None
    ):
        return False
    actual = " ".join(definition.sql.split()).casefold()
    expected = " ".join(expected_sql.split()).casefold()
    return actual == expected


def _read_bounded_schema_object(
    connection: sqlite3.Connection,
    *,
    name: str,
    expected_table: str,
) -> _SchemaObjectDefinition | None:
    """Fetch one schema definition only after SQLite proves its byte bound."""
    row = connection.execute(
        """
        SELECT
            CASE
                WHEN typeof(type) != 'text' THEN NULL
                WHEN octet_length(type) > 16 THEN NULL
                ELSE type
            END AS bounded_type,
            tbl_name = ? AS table_matches,
            CASE
                WHEN sql IS NULL THEN NULL
                WHEN typeof(sql) != 'text' THEN NULL
                WHEN octet_length(sql) > ? THEN NULL
                ELSE sql
            END AS bounded_sql,
            CASE
                WHEN sql IS NULL THEN 1
                WHEN typeof(sql) != 'text' THEN 0
                WHEN octet_length(sql) > ? THEN 0
                ELSE 1
            END AS definition_within_bound
        FROM sqlite_schema
        WHERE name = ?
        """,
        (
            expected_table,
            MAX_SCHEMA_SQL_BYTES,
            MAX_SCHEMA_SQL_BYTES,
            name,
        ),
    ).fetchone()
    if row is None:
        return None
    if isinstance(row, sqlite3.Row):
        object_type = row["bounded_type"]
        table_matches = row["table_matches"]
        bounded_sql = row["bounded_sql"]
        definition_within_bound = row["definition_within_bound"]
    else:
        object_type, table_matches, bounded_sql, definition_within_bound = row
    return _SchemaObjectDefinition(
        object_type="" if object_type is None else str(object_type),
        table_matches=bool(table_matches),
        sql=None if bounded_sql is None else str(bounded_sql),
        definition_within_bound=bool(definition_within_bound),
    )


def rebuild_fts_index(connection: sqlite3.Connection) -> None:
    """Drop and rebuild the external-content FTS table transactionally."""
    object_type = _schema_object_type(connection, FTS_TABLE_NAME)
    with transaction(connection):
        if object_type == "table":
            connection.execute("DROP TABLE entries_fts")
        elif object_type == "view":
            connection.execute("DROP VIEW entries_fts")
        elif object_type is not None:
            raise DatabaseError(
                f"Cannot rebuild FTS index over unexpected SQLite object type: {object_type}"
            )
        connection.execute(CREATE_FTS_TABLE_SQL)
        connection.execute("INSERT INTO entries_fts(entries_fts) VALUES ('rebuild')")


def _rebuild_vector_table(connection: sqlite3.Connection) -> None:
    """Recreate malformed derived vector storage as an empty valid table."""
    object_type = _schema_object_type(connection, "entry_vectors")
    with transaction(connection):
        if object_type == "table":
            connection.execute("DROP TABLE entry_vectors")
        elif object_type == "view":
            connection.execute("DROP VIEW entry_vectors")
        elif object_type is not None:
            raise DatabaseError(
                f"Cannot rebuild vector index over unexpected SQLite object type: {object_type}"
            )
        connection.execute(CREATE_VECTOR_TABLE_SQL)


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    return _schema_object_type(connection, name) == "table"


def _schema_object_type(connection: sqlite3.Connection, name: str) -> str | None:
    """Return one SQLite schema object type without interpolating its name."""
    row = connection.execute(
        "SELECT type FROM sqlite_schema WHERE name = ?",
        (name,),
    ).fetchone()
    if row is None:
        return None
    value = row["type"] if isinstance(row, sqlite3.Row) else row[0]
    return str(value)


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


def _apply_sqlite_resource_limits(connection: sqlite3.Connection) -> None:
    """Bound SQL text, stored values, rows, and lazy sqlite_schema parsing."""
    connection.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH, MAX_SCHEMA_SQL_BYTES)
    connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, MAX_SCHEMA_SQL_BYTES)
    connection.execute("SELECT COUNT(*) FROM sqlite_schema").fetchone()
    connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, MAX_SQLITE_VALUE_BYTES)
    if (
        connection.getlimit(sqlite3.SQLITE_LIMIT_LENGTH) != MAX_SQLITE_VALUE_BYTES
        or connection.getlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH) != MAX_SCHEMA_SQL_BYTES
    ):
        raise DatabaseError("SQLite refused a configured resource length limit")


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
            "entry_observations",
            "entry_supersessions",
        )
        if not _table_exists(connection, name)
    ]
    if missing_tables:
        missing = ", ".join(missing_tables)
        raise DatabaseError(f"Engram database is missing required tables: {missing}")


def _validate_preflight_schema(
    connection: sqlite3.Connection,
    schema_version: int,
) -> None:
    """Require canonical tables while allowing derived indexes to be rebuilt."""
    required_tables = ["entries", "audit_log"]
    if schema_version >= CONSOLIDATION_SCHEMA_VERSION:
        required_tables.append("consolidation_plans")
    if schema_version >= LIFECYCLE_SCHEMA_VERSION:
        required_tables.extend(("entry_observations", "entry_supersessions"))
    missing_tables = [name for name in required_tables if not _table_exists(connection, name)]
    if missing_tables:
        raise DatabaseError(
            "Engram database is missing required tables for schema "
            f"{schema_version}: {', '.join(missing_tables)}"
        )


def _simulate_upgrade(
    source: sqlite3.Connection,
    *,
    busy_timeout_ms: int,
    limits: LimitsConfig | None,
) -> None:
    """Prove the complete migration against a disposable on-disk snapshot."""
    with TemporaryDirectory(prefix="engram-preflight-") as temporary_directory:
        snapshot_path = Path(temporary_directory) / "engram-upgrade.db"
        snapshot = sqlite3.connect(
            snapshot_path,
            timeout=busy_timeout_ms / 1000,
            isolation_level=None,
            check_same_thread=False,
        )
        snapshot.row_factory = sqlite3.Row
        try:
            source.backup(snapshot, pages=256, sleep=0.01)
            _apply_sqlite_resource_limits(snapshot)
            _configure_connection(snapshot, busy_timeout_ms)
            apply_migrations(snapshot, limits=limits)
            ensure_derived_indexes(snapshot)
            verify_database_integrity(
                snapshot,
                max_statement_chars=None if limits is None else limits.max_statement_chars,
                max_subject_keys=None if limits is None else limits.max_subject_keys,
            )
        finally:
            snapshot.close()


def _vector_rebuild_required(connection: sqlite3.Connection) -> bool:
    if not _vector_schema_matches(connection):
        return True
    invalid = connection.execute(
        """
        SELECT 1
        FROM entry_vectors
        WHERE dim NOT BETWEEN 1 AND 8192
           OR typeof(vector) != 'blob'
           OR octet_length(vector) != dim * 4
        LIMIT 1
        """
    ).fetchone()
    return invalid is not None


def _precheck_entry_payload_lengths(
    connection: sqlite3.Connection,
    *,
    max_statement_chars: int | None,
    max_subject_keys: int | None,
) -> None:
    """Reject one oversized persisted value before SQLite returns its payload."""
    statement_limit = (
        HARD_MAX_STATEMENT_CHARS
        if max_statement_chars is None
        else min(max_statement_chars, HARD_MAX_STATEMENT_CHARS)
    )
    subject_key_limit = (
        HARD_MAX_SUBJECT_KEYS
        if max_subject_keys is None
        else min(max_subject_keys, HARD_MAX_SUBJECT_KEYS)
    )
    subject_json_limit = (
        2
        + subject_key_limit * (MAX_JSON_ESCAPE_CHARS * MAX_SUBJECT_KEY_CHARS + 2)
        + max(subject_key_limit - 1, 0)
    )
    evidence_json_limit = (
        2
        + MAX_EVIDENCE_ITEMS * (MAX_JSON_ESCAPE_CHARS * MAX_EVIDENCE_REF_CHARS + 128)
        + max(MAX_EVIDENCE_ITEMS - 1, 0)
    )
    count_row = connection.execute("SELECT COUNT(*) FROM entries").fetchone()
    entry_count = 0 if count_row is None else int(count_row[0])
    supersedes_json_limit = 2 + entry_count * (ULID_LENGTH + 8)
    invalid = connection.execute(
        """
        WITH checked AS (
            SELECT
                rowid AS entry_rowid,
                CASE
                    WHEN typeof(id) != 'text' THEN 'id'
                    WHEN octet_length(id) > :id_bytes THEN 'id'
                    WHEN instr(id, char(0)) > 0 OR length(id) != :id_chars THEN 'id'
                    WHEN typeof(kind) != 'text' THEN 'kind'
                    WHEN octet_length(kind) > :enum_bytes THEN 'kind'
                    WHEN instr(kind, char(0)) > 0 OR length(kind) > 64 THEN 'kind'
                    WHEN typeof(scope) != 'text' THEN 'scope'
                    WHEN octet_length(scope) > :scope_bytes THEN 'scope'
                    WHEN instr(scope, char(0)) > 0
                      OR length(scope) > :scope_chars THEN 'scope'
                    WHEN typeof(statement) != 'text' THEN 'statement'
                    WHEN octet_length(statement) > :statement_bytes THEN 'statement'
                    WHEN instr(statement, char(0)) > 0
                      OR length(statement) > :statement_chars THEN 'statement'
                    WHEN typeof(subject_keys) != 'text' THEN 'subject_keys'
                    WHEN octet_length(subject_keys) > :subject_json_bytes THEN 'subject_keys'
                    WHEN instr(subject_keys, char(0)) > 0
                      OR length(subject_keys) > :subject_json_chars THEN 'subject_keys'
                    WHEN typeof(status) != 'text' THEN 'status'
                    WHEN octet_length(status) > :enum_bytes THEN 'status'
                    WHEN instr(status, char(0)) > 0 OR length(status) > 64 THEN 'status'
                    WHEN typeof(promotion_state) != 'text' THEN 'promotion_state'
                    WHEN octet_length(promotion_state) > :enum_bytes THEN 'promotion_state'
                    WHEN instr(promotion_state, char(0)) > 0
                      OR length(promotion_state) > 64 THEN 'promotion_state'
                    WHEN typeof(source_type) != 'text' THEN 'source_type'
                    WHEN octet_length(source_type) > :enum_bytes THEN 'source_type'
                    WHEN instr(source_type, char(0)) > 0
                      OR length(source_type) > 64 THEN 'source_type'
                    WHEN typeof(writer_model) NOT IN ('null', 'text') THEN 'writer_model'
                    WHEN octet_length(writer_model) > :writer_model_bytes THEN 'writer_model'
                    WHEN instr(writer_model, char(0)) > 0
                      OR length(writer_model) > :writer_model_chars THEN 'writer_model'
                    WHEN typeof(confidence) != 'text' THEN 'confidence'
                    WHEN octet_length(confidence) > :enum_bytes THEN 'confidence'
                    WHEN instr(confidence, char(0)) > 0
                      OR length(confidence) > 64 THEN 'confidence'
                    WHEN typeof(idempotency_key) != 'text' THEN 'idempotency_key'
                    WHEN octet_length(idempotency_key) > :hash_bytes THEN 'idempotency_key'
                    WHEN instr(idempotency_key, char(0)) > 0
                      OR length(idempotency_key) != :hash_chars THEN 'idempotency_key'
                    WHEN typeof(supersedes) != 'text' THEN 'supersedes'
                    WHEN octet_length(supersedes) > :supersedes_json_bytes THEN 'supersedes'
                    WHEN instr(supersedes, char(0)) > 0
                      OR length(supersedes) > :supersedes_json_chars THEN 'supersedes'
                    WHEN typeof(evidence) != 'text' THEN 'evidence'
                    WHEN octet_length(evidence) > :evidence_json_bytes THEN 'evidence'
                    WHEN instr(evidence, char(0)) > 0
                      OR length(evidence) > :evidence_json_chars THEN 'evidence'
                    WHEN typeof(datacron_ref) NOT IN ('null', 'text') THEN 'datacron_ref'
                    WHEN octet_length(datacron_ref) > :datacron_ref_bytes THEN 'datacron_ref'
                    WHEN instr(datacron_ref, char(0)) > 0
                      OR length(datacron_ref) > :datacron_ref_chars THEN 'datacron_ref'
                    WHEN typeof(datacron_hash) NOT IN ('null', 'text') THEN 'datacron_hash'
                    WHEN octet_length(datacron_hash) > :hash_bytes THEN 'datacron_hash'
                    WHEN instr(datacron_hash, char(0)) > 0
                      OR length(datacron_hash) > :hash_chars THEN 'datacron_hash'
                    WHEN typeof(recorded_at) != 'text' THEN 'recorded_at'
                    WHEN octet_length(recorded_at) > :temporal_bytes THEN 'recorded_at'
                    WHEN instr(recorded_at, char(0)) > 0
                      OR length(recorded_at) > :temporal_chars THEN 'recorded_at'
                    WHEN typeof(observed_at) NOT IN ('null', 'text') THEN 'observed_at'
                    WHEN octet_length(observed_at) > :temporal_bytes THEN 'observed_at'
                    WHEN instr(observed_at, char(0)) > 0
                      OR length(observed_at) > :temporal_chars THEN 'observed_at'
                    WHEN typeof(valid_from) NOT IN ('null', 'text') THEN 'valid_from'
                    WHEN octet_length(valid_from) > :date_bytes THEN 'valid_from'
                    WHEN instr(valid_from, char(0)) > 0
                      OR length(valid_from) > 10 THEN 'valid_from'
                    WHEN typeof(valid_until) NOT IN ('null', 'text') THEN 'valid_until'
                    WHEN octet_length(valid_until) > :date_bytes THEN 'valid_until'
                    WHEN instr(valid_until, char(0)) > 0
                      OR length(valid_until) > 10 THEN 'valid_until'
                    WHEN typeof(expires_at) NOT IN ('null', 'text') THEN 'expires_at'
                    WHEN octet_length(expires_at) > :temporal_bytes THEN 'expires_at'
                    WHEN instr(expires_at, char(0)) > 0
                      OR length(expires_at) > :temporal_chars THEN 'expires_at'
                    WHEN typeof(synced_at) NOT IN ('null', 'text') THEN 'synced_at'
                    WHEN octet_length(synced_at) > :temporal_bytes THEN 'synced_at'
                    WHEN instr(synced_at, char(0)) > 0
                      OR length(synced_at) > :temporal_chars THEN 'synced_at'
                    WHEN typeof(is_stale) != 'integer' THEN 'is_stale'
                END AS invalid_field
            FROM entries
        )
        SELECT entry_rowid, invalid_field
        FROM checked
        WHERE invalid_field IS NOT NULL
        ORDER BY entry_rowid
        LIMIT 1
        """,
        {
            "id_bytes": ULID_LENGTH * MAX_UTF8_BYTES_PER_CHAR,
            "id_chars": ULID_LENGTH,
            "enum_bytes": 64 * MAX_UTF8_BYTES_PER_CHAR,
            "scope_bytes": MAX_SCOPE_CHARS * MAX_UTF8_BYTES_PER_CHAR,
            "scope_chars": MAX_SCOPE_CHARS,
            "statement_bytes": statement_limit * MAX_UTF8_BYTES_PER_CHAR,
            "statement_chars": statement_limit,
            "subject_json_bytes": subject_json_limit * MAX_UTF8_BYTES_PER_CHAR,
            "subject_json_chars": subject_json_limit,
            "writer_model_bytes": MAX_WRITER_MODEL_CHARS * MAX_UTF8_BYTES_PER_CHAR,
            "writer_model_chars": MAX_WRITER_MODEL_CHARS,
            "hash_bytes": SHA256_HEX_LENGTH * MAX_UTF8_BYTES_PER_CHAR,
            "hash_chars": SHA256_HEX_LENGTH,
            "supersedes_json_bytes": supersedes_json_limit * MAX_UTF8_BYTES_PER_CHAR,
            "supersedes_json_chars": supersedes_json_limit,
            "evidence_json_bytes": evidence_json_limit * MAX_UTF8_BYTES_PER_CHAR,
            "evidence_json_chars": evidence_json_limit,
            "datacron_ref_bytes": MAX_DATACRON_REF_CHARS * MAX_UTF8_BYTES_PER_CHAR,
            "datacron_ref_chars": MAX_DATACRON_REF_CHARS,
            "temporal_bytes": MAX_TEMPORAL_TEXT_CHARS * MAX_UTF8_BYTES_PER_CHAR,
            "temporal_chars": MAX_TEMPORAL_TEXT_CHARS,
            "date_bytes": 10 * MAX_UTF8_BYTES_PER_CHAR,
        },
    ).fetchone()
    if invalid is not None:
        safe_identity = connection.execute(
            """
            SELECT id
            FROM entries
            WHERE rowid = ?
              AND CASE
                    WHEN typeof(id) != 'text' THEN 0
                    WHEN octet_length(id) > ? THEN 0
                    WHEN instr(id, char(0)) = 0 AND length(id) = ? THEN 1
                    ELSE 0
                  END = 1
            """,
            (
                invalid["entry_rowid"],
                ULID_LENGTH * MAX_UTF8_BYTES_PER_CHAR,
                ULID_LENGTH,
            ),
        ).fetchone()
        location = (
            f"entry {safe_identity['id']!s}"
            if safe_identity is not None
            else f"row {invalid['entry_rowid']!s}"
        )
        raise DatabaseError(
            "Persisted entry payload exceeds a fixed preflight allocation bound or has "
            f"an invalid SQLite type at {location}, "
            f"field {invalid['invalid_field']!s}"
        )
    if _schema_version(connection) >= LIFECYCLE_SCHEMA_VERSION:
        invalid_v5 = connection.execute(
            """
            SELECT rowid
            FROM entries
            WHERE CASE
                    WHEN typeof(canonical_key) != 'text' THEN 1
                    WHEN octet_length(canonical_key) > :canonical_bytes THEN 1
                    WHEN instr(canonical_key, char(0)) > 0
                      OR length(canonical_key) != :canonical_chars THEN 1
                    WHEN typeof(claim_key) NOT IN ('null', 'text') THEN 1
                    WHEN octet_length(claim_key) > :claim_bytes THEN 1
                    WHEN instr(claim_key, char(0)) > 0
                      OR length(claim_key) > :claim_chars THEN 1
                    ELSE 0
                  END = 1
            ORDER BY rowid
            LIMIT 1
            """,
            {
                "canonical_bytes": SHA256_HEX_LENGTH * MAX_UTF8_BYTES_PER_CHAR,
                "canonical_chars": SHA256_HEX_LENGTH,
                "claim_bytes": MAX_CLAIM_KEY_CHARS * MAX_UTF8_BYTES_PER_CHAR,
                "claim_chars": MAX_CLAIM_KEY_CHARS,
            },
        ).fetchone()
        if invalid_v5 is not None:
            raise DatabaseError(
                "Persisted entry identity payload exceeds a fixed preflight allocation "
                f"bound or has an invalid SQLite type at row {invalid_v5['rowid']!s}"
            )


def _precheck_observation_payload_lengths(
    connection: sqlite3.Connection,
    *,
    max_subject_keys: int | None,
) -> None:
    subject_key_limit = (
        HARD_MAX_SUBJECT_KEYS
        if max_subject_keys is None
        else min(max_subject_keys, HARD_MAX_SUBJECT_KEYS)
    )
    subject_json_limit = (
        2
        + subject_key_limit * (MAX_JSON_ESCAPE_CHARS * MAX_SUBJECT_KEY_CHARS + 2)
        + max(subject_key_limit - 1, 0)
    )
    evidence_json_limit = (
        2
        + MAX_EVIDENCE_ITEMS * (MAX_JSON_ESCAPE_CHARS * MAX_EVIDENCE_REF_CHARS + 128)
        + max(MAX_EVIDENCE_ITEMS - 1, 0)
    )
    invalid = connection.execute(
        """
        SELECT 1
        FROM entry_observations
        WHERE CASE
                WHEN typeof(entry_id) != 'text' THEN 1
                WHEN octet_length(entry_id) > :entry_id_bytes THEN 1
                WHEN instr(entry_id, char(0)) > 0
                  OR length(entry_id) != :entry_id_chars THEN 1
                WHEN typeof(writer_model) != 'text' THEN 1
                WHEN octet_length(writer_model) > :writer_model_bytes THEN 1
                WHEN instr(writer_model, char(0)) > 0
                  OR length(writer_model) > :writer_model_chars THEN 1
                WHEN typeof(claim_hash) != 'text' THEN 1
                WHEN octet_length(claim_hash) > :claim_hash_bytes THEN 1
                WHEN instr(claim_hash, char(0)) > 0
                  OR length(claim_hash) > :claim_hash_chars THEN 1
                WHEN typeof(confidence) != 'text' THEN 1
                WHEN octet_length(confidence) > :enum_bytes THEN 1
                WHEN instr(confidence, char(0)) > 0
                  OR length(confidence) > 64 THEN 1
                WHEN typeof(subject_keys) != 'text' THEN 1
                WHEN octet_length(subject_keys) > :subject_json_bytes THEN 1
                WHEN instr(subject_keys, char(0)) > 0
                  OR length(subject_keys) > :subject_json_chars THEN 1
                WHEN typeof(evidence) != 'text' THEN 1
                WHEN octet_length(evidence) > :evidence_json_bytes THEN 1
                WHEN instr(evidence, char(0)) > 0
                  OR length(evidence) > :evidence_json_chars THEN 1
                WHEN typeof(recorded_at) != 'text' THEN 1
                WHEN octet_length(recorded_at) > :temporal_bytes THEN 1
                WHEN instr(recorded_at, char(0)) > 0
                  OR length(recorded_at) > :temporal_chars THEN 1
                WHEN typeof(observed_at) NOT IN ('null', 'text') THEN 1
                WHEN octet_length(observed_at) > :temporal_bytes THEN 1
                WHEN instr(observed_at, char(0)) > 0
                  OR length(observed_at) > :temporal_chars THEN 1
                WHEN typeof(valid_from) NOT IN ('null', 'text') THEN 1
                WHEN octet_length(valid_from) > :date_bytes THEN 1
                WHEN instr(valid_from, char(0)) > 0
                  OR length(valid_from) > 10 THEN 1
                WHEN typeof(valid_until) NOT IN ('null', 'text') THEN 1
                WHEN octet_length(valid_until) > :date_bytes THEN 1
                WHEN instr(valid_until, char(0)) > 0
                  OR length(valid_until) > 10 THEN 1
                ELSE 0
              END = 1
        LIMIT 1
        """,
        {
            "entry_id_bytes": ULID_LENGTH * MAX_UTF8_BYTES_PER_CHAR,
            "entry_id_chars": ULID_LENGTH,
            "writer_model_bytes": MAX_WRITER_MODEL_CHARS * MAX_UTF8_BYTES_PER_CHAR,
            "writer_model_chars": MAX_WRITER_MODEL_CHARS,
            "claim_hash_bytes": LEGACY_CLAIM_HASH_CHARS * MAX_UTF8_BYTES_PER_CHAR,
            "claim_hash_chars": LEGACY_CLAIM_HASH_CHARS,
            "enum_bytes": 64 * MAX_UTF8_BYTES_PER_CHAR,
            "subject_json_bytes": subject_json_limit * MAX_UTF8_BYTES_PER_CHAR,
            "subject_json_chars": subject_json_limit,
            "evidence_json_bytes": evidence_json_limit * MAX_UTF8_BYTES_PER_CHAR,
            "evidence_json_chars": evidence_json_limit,
            "temporal_bytes": MAX_TEMPORAL_TEXT_CHARS * MAX_UTF8_BYTES_PER_CHAR,
            "temporal_chars": MAX_TEMPORAL_TEXT_CHARS,
            "date_bytes": 10 * MAX_UTF8_BYTES_PER_CHAR,
        },
    ).fetchone()
    if invalid is not None:
        raise DatabaseError(
            "Persisted observation payload exceeds a fixed preflight allocation bound "
            "or has an invalid SQLite type"
        )


def _precheck_audit_payload_lengths(connection: sqlite3.Connection) -> None:
    invalid = connection.execute(
        """
        SELECT 1
        FROM audit_log
        WHERE CASE
                WHEN typeof(actor) != 'text' THEN 1
                WHEN octet_length(actor) > :actor_bytes THEN 1
                WHEN instr(actor, char(0)) > 0
                  OR length(actor) > :actor_chars THEN 1
                WHEN typeof(ts) != 'text' THEN 1
                WHEN octet_length(ts) > :temporal_bytes THEN 1
                WHEN instr(ts, char(0)) > 0
                  OR length(ts) > :temporal_chars THEN 1
                WHEN typeof(action) != 'text' THEN 1
                WHEN octet_length(action) > :temporal_bytes THEN 1
                WHEN instr(action, char(0)) > 0
                  OR length(action) > :temporal_chars THEN 1
                WHEN typeof(entry_id) NOT IN ('null', 'text') THEN 1
                WHEN octet_length(entry_id) > :audit_reference_bytes THEN 1
                WHEN instr(entry_id, char(0)) > 0
                  OR length(entry_id) > :audit_reference_chars THEN 1
                WHEN typeof(detail_hash) NOT IN ('null', 'text') THEN 1
                WHEN octet_length(detail_hash) > :audit_reference_bytes THEN 1
                WHEN instr(detail_hash, char(0)) > 0
                  OR length(detail_hash) > :audit_reference_chars THEN 1
                ELSE 0
              END = 1
        LIMIT 1
        """,
        {
            "actor_bytes": MAX_ACTOR_CHARS * MAX_UTF8_BYTES_PER_CHAR,
            "actor_chars": MAX_ACTOR_CHARS,
            "temporal_bytes": MAX_TEMPORAL_TEXT_CHARS * MAX_UTF8_BYTES_PER_CHAR,
            "temporal_chars": MAX_TEMPORAL_TEXT_CHARS,
            "audit_reference_bytes": 1024 * MAX_UTF8_BYTES_PER_CHAR,
            "audit_reference_chars": 1024,
        },
    ).fetchone()
    if invalid is not None:
        raise DatabaseError(
            "Persisted audit payload exceeds a fixed preflight allocation bound "
            "or has an invalid SQLite type"
        )


def _precheck_supersession_payload_lengths(connection: sqlite3.Connection) -> None:
    invalid = connection.execute(
        """
        SELECT 1
        FROM entry_supersessions
        WHERE CASE
                WHEN typeof(old_entry_id) != 'text' THEN 1
                WHEN octet_length(old_entry_id) > :entry_id_bytes THEN 1
                WHEN instr(old_entry_id, char(0)) > 0
                  OR length(old_entry_id) != :entry_id_chars THEN 1
                WHEN typeof(new_entry_id) != 'text' THEN 1
                WHEN octet_length(new_entry_id) > :entry_id_bytes THEN 1
                WHEN instr(new_entry_id, char(0)) > 0
                  OR length(new_entry_id) != :entry_id_chars THEN 1
                WHEN typeof(recorded_at) != 'text' THEN 1
                WHEN octet_length(recorded_at) > :temporal_bytes THEN 1
                WHEN instr(recorded_at, char(0)) > 0
                  OR length(recorded_at) > :temporal_chars THEN 1
                WHEN typeof(actor) != 'text' THEN 1
                WHEN octet_length(actor) > :actor_bytes THEN 1
                WHEN instr(actor, char(0)) > 0
                  OR length(actor) > :actor_chars THEN 1
                ELSE 0
              END = 1
        LIMIT 1
        """,
        {
            "entry_id_bytes": ULID_LENGTH * MAX_UTF8_BYTES_PER_CHAR,
            "entry_id_chars": ULID_LENGTH,
            "temporal_bytes": MAX_TEMPORAL_TEXT_CHARS * MAX_UTF8_BYTES_PER_CHAR,
            "temporal_chars": MAX_TEMPORAL_TEXT_CHARS,
            "actor_bytes": MAX_ACTOR_CHARS * MAX_UTF8_BYTES_PER_CHAR,
            "actor_chars": MAX_ACTOR_CHARS,
        },
    ).fetchone()
    if invalid is not None:
        raise DatabaseError(
            "Persisted supersession payload exceeds a fixed preflight allocation bound "
            "or has an invalid SQLite type"
        )


def _precheck_consolidation_plan_payload_lengths(connection: sqlite3.Connection) -> None:
    invalid = connection.execute(
        """
        SELECT 1
        FROM consolidation_plans
        WHERE CASE
                WHEN typeof(plan_id) != 'text' THEN 1
                WHEN octet_length(plan_id) > :plan_id_bytes THEN 1
                WHEN instr(plan_id, char(0)) > 0
                  OR length(plan_id) != :plan_id_chars THEN 1
                WHEN typeof(created_at) != 'text' THEN 1
                WHEN octet_length(created_at) > :temporal_bytes THEN 1
                WHEN instr(created_at, char(0)) > 0
                  OR length(created_at) > :temporal_chars THEN 1
                WHEN typeof(snapshot_json) != 'text' THEN 1
                WHEN octet_length(snapshot_json) > :snapshot_bytes THEN 1
                WHEN instr(snapshot_json, char(0)) > 0 THEN 1
                WHEN typeof(snapshot_hash) != 'text' THEN 1
                WHEN octet_length(snapshot_hash) > :hash_bytes THEN 1
                WHEN instr(snapshot_hash, char(0)) > 0
                  OR length(snapshot_hash) != :hash_chars THEN 1
                WHEN typeof(consumed_at) NOT IN ('null', 'text') THEN 1
                WHEN octet_length(consumed_at) > :temporal_bytes THEN 1
                WHEN instr(consumed_at, char(0)) > 0
                  OR length(consumed_at) > :temporal_chars THEN 1
                ELSE 0
              END = 1
        LIMIT 1
        """,
        {
            "plan_id_bytes": ULID_LENGTH * MAX_UTF8_BYTES_PER_CHAR,
            "plan_id_chars": ULID_LENGTH,
            "temporal_bytes": MAX_TEMPORAL_TEXT_CHARS * MAX_UTF8_BYTES_PER_CHAR,
            "temporal_chars": MAX_TEMPORAL_TEXT_CHARS,
            "snapshot_bytes": MAX_CONSOLIDATION_SNAPSHOT_BYTES,
            "hash_bytes": SHA256_HEX_LENGTH * MAX_UTF8_BYTES_PER_CHAR,
            "hash_chars": SHA256_HEX_LENGTH,
        },
    ).fetchone()
    if invalid is not None:
        raise DatabaseError(
            "Persisted consolidation plan payload exceeds a fixed preflight "
            "allocation bound or has an invalid SQLite type"
        )


def _preflight_lifecycle_v5(  # noqa: C901
    connection: sqlite3.Connection,
    *,
    limits: LimitsConfig | None,
) -> None:
    """Validate every v4 row and JSON edge before installing stricter structures."""
    _verify_rebuildable_derived_objects(connection)
    _precheck_consolidation_plan_payload_lengths(connection)
    _precheck_entry_payload_lengths(
        connection,
        max_statement_chars=None if limits is None else limits.max_statement_chars,
        max_subject_keys=None if limits is None else limits.max_subject_keys,
    )
    rows = connection.execute(
        """
        SELECT
            id, kind, scope, statement, subject_keys, status, promotion_state,
            source_type, writer_model, confidence, observed_at, recorded_at,
            valid_from, valid_until, expires_at, idempotency_key, supersedes,
            evidence, is_stale, datacron_ref, datacron_hash, synced_at
        FROM entries
        ORDER BY id
        """
    )
    rows_by_id: dict[str, _SupersessionNode] = {}
    edges: dict[str, str] = {}
    for row in rows:
        entry_id = str(row["id"])
        try:
            _require_equal(
                normalize_entry_id(entry_id),
                entry_id,
                "id is not normalized",
            )
        except (StoreValidationError, ValueError) as exc:
            raise DatabaseError(f"Migration v5 rejected entry {entry_id} field id: {exc}") from exc
        if type(row["is_stale"]) is not int or int(row["is_stale"]) not in {0, 1}:
            raise DatabaseError(
                f"Migration v5 rejected entry {entry_id} field is_stale: expected integer 0 or 1"
            )
        if not _legacy_lifecycle_is_valid(row):
            raise DatabaseError(f"Migration v5 rejected invalid lifecycle entry: {entry_id}")
        _validate_v4_entry_content(row, limits=limits)
        rows_by_id[entry_id] = _SupersessionNode(
            kind=str(row["kind"]),
            scope=str(row["scope"]),
            status=str(row["status"]),
            promotion_state=str(row["promotion_state"]),
            source_type=str(row["source_type"]),
            canonical_key=str(row["idempotency_key"]),
            claim_key=None,
            claim_key_present=False,
        )
        try:
            decoded = json.loads(str(row["supersedes"]))
        except json.JSONDecodeError as exc:
            raise DatabaseError(
                f"Migration v5 rejected invalid supersedes JSON: {entry_id}"
            ) from exc
        if not isinstance(decoded, list) or not all(
            isinstance(old_id, str) and old_id for old_id in decoded
        ):
            raise DatabaseError(f"Migration v5 rejected invalid supersedes list: {entry_id}")
        if len(decoded) != len(set(decoded)):
            raise DatabaseError(f"Migration v5 rejected duplicate supersedes IDs: {entry_id}")
        for old_id in decoded:
            try:
                _require_equal(
                    normalize_entry_id(old_id, "supersedes entry ID"),
                    old_id,
                    "supersedes entry ID is not normalized",
                )
            except (StoreValidationError, ValueError) as exc:
                raise DatabaseError(
                    f"Migration v5 rejected entry {entry_id} field supersedes: {exc}"
                ) from exc
            if old_id == entry_id:
                raise DatabaseError(f"Migration v5 rejected self supersession: {entry_id}")
            previous = edges.get(old_id)
            if previous is not None and previous != entry_id:
                raise DatabaseError(
                    f"Migration v5 rejected multiple replacements for entry: {old_id}"
                )
            edges[old_id] = entry_id
    _verify_edge_graph(rows_by_id, edges, context="Migration v5")
    _verify_audit_integrity(connection)


def _verify_rebuildable_derived_objects(connection: sqlite3.Connection) -> None:
    for object_name in (FTS_TABLE_NAME, "entry_vectors"):
        object_type = _schema_object_type(connection, object_name)
        if object_type not in {None, "table"}:
            raise DatabaseError(
                f"Cannot rebuild derived object {object_name!r} over unexpected "
                f"SQLite object type {object_type}"
            )


def _validate_v4_entry_content(
    row: sqlite3.Row,
    *,
    limits: LimitsConfig | None,
) -> None:
    entry_id = str(row["id"])
    max_statement_chars = None if limits is None else limits.max_statement_chars
    max_subject_keys = None if limits is None else limits.max_subject_keys
    try:
        kind = EntryKind(str(row["kind"]))
        EntryStatus(str(row["status"]))
        PromotionState(str(row["promotion_state"]))
        SourceType(str(row["source_type"]))
        Confidence(str(row["confidence"]))
        scope = str(row["scope"])
        statement = str(row["statement"])
        _require_equal(normalize_scope(scope), scope, "scope is not normalized")
        _require_equal(
            normalize_statement(statement, max_statement_chars),
            statement,
            "statement is not normalized",
        )
        subject_keys = _decode_string_list(row["subject_keys"], "subject_keys")
        _require_equal(
            normalize_subject_keys(subject_keys, max_subject_keys),
            tuple(subject_keys),
            "subject_keys are not normalized",
        )
        expected_canonical = canonical_key(kind, scope, statement)
        _require_equal(
            str(row["idempotency_key"]),
            expected_canonical,
            "idempotency_key does not match normalized content",
        )
        writer_model = row["writer_model"]
        if writer_model is not None:
            _require_equal(
                normalize_writer_model(writer_model),
                writer_model,
                "writer_model is not normalized",
            )
        _validate_temporal_fields(row)
        _validate_evidence_json(row["evidence"])
        _validate_datacron_fields(row)
    except (StoreValidationError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise DatabaseError(f"Migration v5 rejected entry {entry_id} content field: {exc}") from exc


def _require_equal(actual: object, expected: object, message: str) -> None:
    if actual != expected:
        raise ValueError(message)


def _decode_string_list(value: object, field_name: str) -> list[str]:
    try:
        decoded = json.loads(str(value))
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{field_name} is not valid JSON") from exc
    if not isinstance(decoded, list) or not all(isinstance(item, str) for item in decoded):
        raise ValueError(f"{field_name} is not an array of strings")
    return decoded


def _validate_evidence_json(value: object) -> list[dict[str, str]]:
    try:
        decoded = json.loads(str(value))
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("evidence is not valid JSON") from exc
    if not isinstance(decoded, list):
        raise ValueError("evidence is not an array")
    if len(decoded) > MAX_EVIDENCE_ITEMS:
        raise ValueError(f"evidence exceeds limit of {MAX_EVIDENCE_ITEMS} items")
    normalized: list[dict[str, str]] = []
    for index, item in enumerate(decoded):
        if not isinstance(item, dict) or set(item) != {"type", "ref"}:
            raise ValueError(f"evidence[{index}] is not a canonical evidence object")
        evidence_type = item["type"]
        reference = item["ref"]
        if not isinstance(evidence_type, str):
            raise ValueError(f"evidence[{index}].type is not a string")
        try:
            EvidenceType(evidence_type)
        except ValueError as exc:
            raise ValueError(f"evidence[{index}].type is invalid") from exc
        if not isinstance(reference, str) or normalize_evidence_ref(reference) != reference:
            raise ValueError(f"evidence[{index}].ref is not normalized")
        normalized.append({"type": evidence_type, "ref": reference})
    return normalized


def _validate_temporal_fields(row: sqlite3.Row) -> None:
    _validate_datetime_field(row["recorded_at"], "recorded_at", optional=False)
    _validate_datetime_field(row["observed_at"], "observed_at", optional=True)
    _validate_datetime_field(row["expires_at"], "expires_at", optional=True)
    _validate_datetime_field(row["synced_at"], "synced_at", optional=True)
    valid_from = _validate_date_field(row["valid_from"], "valid_from")
    valid_until = _validate_date_field(row["valid_until"], "valid_until")
    _require_valid_date_range(valid_from, valid_until)


def _validate_datacron_fields(row: sqlite3.Row) -> None:
    reference = row["datacron_ref"]
    if reference is not None:
        _require_equal(
            normalize_datacron_ref(reference),
            reference,
            "datacron_ref is not normalized",
        )
    content_hash = row["datacron_hash"]
    if content_hash is not None:
        normalized_hash = normalize_sha256_hex(str(content_hash), "datacron_hash")
        _require_equal(
            normalized_hash,
            content_hash,
            "datacron_hash is not normalized",
        )


def _validate_datetime_field(
    value: object,
    field_name: str,
    *,
    optional: bool,
) -> datetime | None:
    if value is None:
        if optional:
            return None
        raise ValueError(f"{field_name} is missing")
    text = str(value)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{field_name} is not an ISO datetime") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field_name} is not timezone-aware")
    canonical = parsed.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
    if text != canonical:
        raise ValueError(f"{field_name} is not canonical UTC")
    return parsed


def _validate_date_field(value: object, field_name: str) -> date | None:
    if value is None:
        return None
    text = str(value)
    try:
        parsed = date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{field_name} is not an ISO date") from exc
    if parsed.isoformat() != text:
        raise ValueError(f"{field_name} is not canonical")
    return parsed


def _require_valid_date_range(
    valid_from: date | None,
    valid_until: date | None,
) -> None:
    if valid_from is not None and valid_until is not None and valid_until < valid_from:
        raise ValueError("valid_until is earlier than valid_from")


def _validate_stale_flag(value: object) -> None:
    if type(value) is not int or int(value) not in {0, 1}:
        raise ValueError("is_stale must be integer 0 or 1")


def _legacy_lifecycle_is_valid(row: sqlite3.Row) -> bool:
    status = str(row["status"])
    promotion = str(row["promotion_state"])
    source = str(row["source_type"])
    writer_is_null = row["writer_model"] is None
    untrusted = (
        promotion in {"candidate", "rejected"}
        and source in {"model_inferred", "session_summary"}
        and not writer_is_null
        and str(row["confidence"]) != "high"
        and (
            (status == "quarantined" and promotion == "candidate")
            or status in {"superseded", "expired"}
        )
    )
    trusted = (
        promotion in {"approved", "promoted"}
        and source in {"human", "tool_verified"}
        and writer_is_null
        and status in {"active", "superseded", "expired"}
    )
    promoted_fields = (
        row["datacron_ref"] is not None
        and row["datacron_hash"] is not None
        and row["synced_at"] is not None
    )
    unpromoted_fields = (
        row["datacron_ref"] is None
        and row["datacron_hash"] is None
        and row["synced_at"] is None
        and int(row["is_stale"]) == 0
    )
    datacron_valid = (promotion == "promoted" and promoted_fields) or (
        promotion != "promoted" and unpromoted_fields
    )
    return (untrusted or trusted) and datacron_valid


def verify_database_integrity(
    connection: sqlite3.Connection,
    *,
    max_statement_chars: int | None = None,
    max_subject_keys: int | None = None,
) -> None:
    """Fail closed when canonical lifecycle or relationship invariants diverge."""
    _verify_required_v5_schema(connection)
    _precheck_consolidation_plan_payload_lengths(connection)
    _precheck_entry_payload_lengths(
        connection,
        max_statement_chars=max_statement_chars,
        max_subject_keys=max_subject_keys,
    )
    foreign_key_violation = connection.execute("PRAGMA foreign_key_check").fetchone()
    if foreign_key_violation is not None:
        raise DatabaseError(
            "Database foreign key check failed: "
            f"{foreign_key_violation['table']!s}/{foreign_key_violation['rowid']!s}"
        )
    lifecycle_row = connection.execute(
        """
        SELECT id
        FROM entries
        WHERE
            canonical_key IS NULL
            OR length(canonical_key) != 64
            OR NOT (
                (
                    promotion_state IN ('candidate', 'rejected')
                    AND source_type IN ('model_inferred', 'session_summary')
                    AND writer_model IS NOT NULL
                    AND confidence != 'high'
                    AND (
                        (
                            status = 'quarantined'
                            AND promotion_state = 'candidate'
                        )
                        OR status IN ('superseded', 'expired')
                    )
                )
                OR (
                    promotion_state IN ('approved', 'promoted')
                    AND source_type IN ('human', 'tool_verified')
                    AND writer_model IS NULL
                    AND status IN ('active', 'superseded', 'expired')
                )
            )
            OR NOT (
                (
                    promotion_state = 'promoted'
                    AND datacron_ref IS NOT NULL
                    AND datacron_hash IS NOT NULL
                    AND synced_at IS NOT NULL
                )
                OR (
                    promotion_state != 'promoted'
                    AND datacron_ref IS NULL
                    AND datacron_hash IS NULL
                    AND synced_at IS NULL
                    AND is_stale = 0
                )
            )
        ORDER BY id
        LIMIT 1
        """
    ).fetchone()
    if lifecycle_row is not None:
        raise DatabaseError(f"Entry violates the lifecycle invariant: {lifecycle_row['id']!s}")
    _verify_normalized_entry_content(
        connection,
        max_statement_chars=max_statement_chars,
        max_subject_keys=max_subject_keys,
    )
    _verify_observation_integrity(
        connection,
        max_subject_keys=max_subject_keys,
    )
    _verify_audit_integrity(connection)
    _verify_live_canonical_uniqueness(connection)
    _verify_supersession_integrity(connection)
    _warn_unclassified_legacy_entries(connection)


def _verify_required_v5_schema(connection: sqlite3.Connection) -> None:
    _verify_canonical_table_schemas(connection)
    names = tuple(REQUIRED_V5_TRIGGERS) + tuple(REQUIRED_V5_PARTIAL_INDEX_FRAGMENTS)
    expected_definitions = _expected_v5_schema_definitions(names)
    for name, table_name in REQUIRED_V5_TRIGGERS.items():
        definition = _read_bounded_schema_object(
            connection,
            name=name,
            expected_table=table_name,
        )
        if definition is not None and not definition.definition_within_bound:
            raise DatabaseError(
                f"Database schema definition exceeds fixed allocation bound: {name}"
            )
        if (
            definition is None
            or definition.object_type != "trigger"
            or not definition.table_matches
            or definition.sql is None
        ):
            raise DatabaseError(f"Database is missing required trigger: {name}")
        if _normalize_schema_sql(definition.sql) != expected_definitions[name]:
            raise DatabaseError(f"Database trigger definition is invalid: {name}")
    for name, fragments in REQUIRED_V5_PARTIAL_INDEX_FRAGMENTS.items():
        definition = _read_bounded_schema_object(
            connection,
            name=name,
            expected_table="entries",
        )
        if definition is not None and not definition.definition_within_bound:
            raise DatabaseError(
                f"Database schema definition exceeds fixed allocation bound: {name}"
            )
        if (
            definition is None
            or definition.object_type != "index"
            or not definition.table_matches
            or definition.sql is None
        ):
            raise DatabaseError(f"Database is missing required partial index: {name}")
        normalized_sql = _normalize_schema_sql(definition.sql)
        if (
            any(fragment not in normalized_sql for fragment in fragments)
            or normalized_sql != expected_definitions[name]
        ):
            raise DatabaseError(f"Database partial index definition is invalid: {name}")


def _verify_canonical_table_schemas(connection: sqlite3.Connection) -> None:
    expected = _expected_canonical_table_schemas()
    for table_name in CANONICAL_V5_TABLES:
        definition = _read_bounded_schema_object(
            connection,
            name=table_name,
            expected_table=table_name,
        )
        if definition is not None and not definition.definition_within_bound:
            raise DatabaseError(
                f"Database canonical table definition exceeds fixed allocation bound: {table_name}"
            )
        if (
            definition is None
            or definition.object_type != "table"
            or not definition.table_matches
            or definition.sql is None
            or _normalize_schema_sql(definition.sql) != expected[table_name]
        ):
            raise DatabaseError(f"Database canonical table definition is invalid: {table_name}")


@lru_cache(maxsize=1)
def _expected_canonical_table_schemas() -> dict[str, str]:
    reference = sqlite3.connect(":memory:", isolation_level=None)
    reference.row_factory = sqlite3.Row
    try:
        _apply_sqlite_resource_limits(reference)
        reference.execute("PRAGMA foreign_keys = ON")
        with transaction(reference):
            for migration in MIGRATIONS:
                for statement in migration.statements:
                    reference.execute(statement)
        expected: dict[str, str] = {}
        for table_name in CANONICAL_V5_TABLES:
            row = reference.execute(
                "SELECT sql FROM sqlite_schema WHERE type = 'table' AND name = ?",
                (table_name,),
            ).fetchone()
            if row is None or row["sql"] is None:  # pragma: no cover - package invariant
                raise RuntimeError(f"Migration set lacks canonical table: {table_name}")
            expected[table_name] = _normalize_schema_sql(str(row["sql"]))
    except sqlite3.Error as exc:  # pragma: no cover - package invariant
        raise RuntimeError("Could not construct the canonical migration schema") from exc
    else:
        return expected
    finally:
        reference.close()


def _expected_v5_schema_definitions(names: tuple[str, ...]) -> dict[str, str]:
    migration = next(
        (candidate for candidate in MIGRATIONS if candidate.version == LIFECYCLE_SCHEMA_VERSION),
        None,
    )
    if migration is None:  # pragma: no cover - package invariant
        raise RuntimeError("Migration v5 is missing")
    expected: dict[str, str] = {}
    for statement in migration.statements:
        normalized = _normalize_schema_sql(statement)
        for name in names:
            upper_name = name.upper()
            prefixes = (
                f"CREATE TRIGGER {upper_name} ",
                f"CREATE INDEX {upper_name} ",
                f"CREATE UNIQUE INDEX {upper_name} ",
            )
            if normalized.startswith(prefixes):
                expected[name] = normalized
    missing = set(names) - set(expected)
    if missing:  # pragma: no cover - package invariant
        raise RuntimeError(
            "Migration v5 lacks required schema definitions: " + ", ".join(sorted(missing))
        )
    return expected


def _normalize_schema_sql(statement: str) -> str:
    return " ".join(statement.upper().split())


def _verify_normalized_entry_content(
    connection: sqlite3.Connection,
    *,
    max_statement_chars: int | None,
    max_subject_keys: int | None,
) -> None:
    rows = connection.execute(
        """
        SELECT
            id, kind, scope, statement, subject_keys, status, promotion_state,
            source_type, writer_model, confidence, observed_at, recorded_at,
            valid_from, valid_until, expires_at, idempotency_key, supersedes,
            evidence, is_stale, datacron_ref, datacron_hash, synced_at,
            canonical_key, claim_key
        FROM entries
        ORDER BY id
        """
    )
    for row in rows:
        entry_id = str(row["id"])
        try:
            _validate_stale_flag(row["is_stale"])
            kind = EntryKind(str(row["kind"]))
            subject_keys = _decode_string_list(row["subject_keys"], "subject_keys")
            trusted = str(row["promotion_state"]) in {"approved", "promoted"}
            validate_persisted_content(
                kind=kind,
                entry_id=entry_id,
                scope=str(row["scope"]),
                statement=str(row["statement"]),
                subject_keys=subject_keys,
                canonical=str(row["canonical_key"]),
                idempotency_key=str(row["idempotency_key"]),
                claim_key=(None if row["claim_key"] is None else str(row["claim_key"])),
                trusted=trusted,
                max_statement_chars=max_statement_chars,
                max_subject_keys=max_subject_keys,
            )
            writer_model = row["writer_model"]
            if writer_model is not None:
                _require_equal(
                    normalize_writer_model(writer_model),
                    writer_model,
                    "writer_model is not normalized",
                )
            _validate_temporal_fields(row)
            _validate_evidence_json(row["evidence"])
            _validate_datacron_fields(row)
        except (
            StoreValidationError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
        ) as exc:
            raise DatabaseError(
                f"Entry violates normalized content invariant: {entry_id}: {exc}"
            ) from exc


def _verify_observation_integrity(
    connection: sqlite3.Connection,
    *,
    max_subject_keys: int | None,
) -> None:
    _precheck_observation_payload_lengths(
        connection,
        max_subject_keys=max_subject_keys,
    )
    rows = connection.execute(
        """
        SELECT
            observation.*, entries.canonical_key AS entry_canonical_key
        FROM entry_observations AS observation
        JOIN entries ON entries.id = observation.entry_id
        ORDER BY observation.entry_id, observation.writer_model,
                 observation.claim_hash
        """
    )
    for row in rows:
        entry_id = str(row["entry_id"])
        claim_hash = str(row["claim_hash"])
        try:
            writer_model = str(row["writer_model"])
            _require_equal(
                normalize_writer_model(writer_model),
                writer_model,
                "writer_model is not normalized",
            )
            Confidence(str(row["confidence"]))
            recorded_at = _validate_datetime_field(
                row["recorded_at"],
                "recorded_at",
                optional=False,
            )
            observed_at = _validate_datetime_field(
                row["observed_at"],
                "observed_at",
                optional=True,
            )
            valid_from = _validate_date_field(row["valid_from"], "valid_from")
            valid_until = _validate_date_field(row["valid_until"], "valid_until")
            _require_valid_date_range(valid_from, valid_until)
            subject_keys = _decode_string_list(row["subject_keys"], "subject_keys")
            _require_equal(
                normalize_subject_keys(subject_keys, max_subject_keys),
                tuple(subject_keys),
                "subject_keys are not normalized",
            )
            evidence = _validate_evidence_json(row["evidence"])
            if claim_hash.startswith("legacy:"):
                _require_equal(
                    claim_hash,
                    f"legacy:{row['entry_canonical_key']!s}",
                    "legacy claim_hash does not match migrated canonical content",
                )
            else:
                _validate_sha256_hex(claim_hash, "claim_hash")
                expected_hash = _observation_content_hash(
                    canonical=str(row["entry_canonical_key"]),
                    writer_model=writer_model,
                    confidence=str(row["confidence"]),
                    observed_at=observed_at,
                    valid_from=valid_from,
                    valid_until=valid_until,
                    subject_keys=subject_keys,
                    evidence=evidence,
                )
                _require_equal(
                    claim_hash,
                    expected_hash,
                    "claim_hash does not match observation content",
                )
            if recorded_at is None:  # pragma: no cover - required above
                raise RuntimeError("Required observation timestamp disappeared")
        except (StoreValidationError, TypeError, ValueError) as exc:
            raise DatabaseError(
                f"Observation violates content invariant for entry {entry_id}: {exc}"
            ) from exc

    missing_observation = connection.execute(
        """
        SELECT entries.id
        FROM entries
        LEFT JOIN entry_observations AS observation
          ON observation.entry_id = entries.id
         AND observation.writer_model = entries.writer_model
        WHERE entries.status = 'quarantined'
          AND entries.promotion_state = 'candidate'
        GROUP BY entries.id
        HAVING COUNT(observation.claim_hash) = 0
        LIMIT 1
        """
    ).fetchone()
    if missing_observation is not None:
        raise DatabaseError(
            f"Live candidate has no retained owner observation: {missing_observation['id']!s}"
        )


def _validate_sha256_hex(value: str, field_name: str) -> None:
    if (
        len(value) != SHA256_HEX_LENGTH
        or value.casefold() != value
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{field_name} is not lowercase SHA-256 hexadecimal")


def _observation_content_hash(  # noqa: PLR0913
    *,
    canonical: str,
    writer_model: str,
    confidence: str,
    observed_at: datetime | None,
    valid_from: date | None,
    valid_until: date | None,
    subject_keys: list[str],
    evidence: list[dict[str, str]],
) -> str:
    payload = {
        "canonical_key": canonical,
        "confidence": confidence,
        "evidence": sorted(
            evidence,
            key=lambda item: (item["type"], item["ref"]),
        ),
        "observed_at": (
            None
            if observed_at is None
            else observed_at.astimezone(UTC)
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z")
        ),
        "subject_keys": sorted(subject_keys),
        "valid_from": None if valid_from is None else valid_from.isoformat(),
        "valid_until": None if valid_until is None else valid_until.isoformat(),
        "writer_model": writer_model,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _verify_live_canonical_uniqueness(connection: sqlite3.Connection) -> None:
    trusted_duplicate = connection.execute(
        """
        SELECT canonical_key
        FROM entries
        WHERE status = 'active'
        GROUP BY canonical_key
        HAVING COUNT(*) > 1
        LIMIT 1
        """
    ).fetchone()
    if trusted_duplicate is not None:
        raise DatabaseError(
            "Database contains duplicate live trusted canonical content: "
            f"{trusted_duplicate['canonical_key']!s}"
        )
    candidate_duplicate = connection.execute(
        """
        SELECT canonical_key, writer_model
        FROM entries
        WHERE status = 'quarantined' AND promotion_state = 'candidate'
        GROUP BY canonical_key, writer_model
        HAVING COUNT(*) > 1
        LIMIT 1
        """
    ).fetchone()
    if candidate_duplicate is not None:
        raise DatabaseError(
            "Database contains duplicate live candidate owner content: "
            f"{candidate_duplicate['canonical_key']!s}"
        )


def _warn_unclassified_legacy_entries(connection: sqlite3.Connection) -> None:
    row = connection.execute(
        """
        SELECT COUNT(*)
        FROM entries
        WHERE status = 'active'
          AND promotion_state IN ('approved', 'promoted')
          AND kind IN ('preference', 'decision', 'fact')
          AND claim_key IS NULL
        """
    ).fetchone()
    count = 0 if row is None else int(row[0])
    if count:
        LOGGER.warning(
            "%d active trusted legacy entries are unclassified and omitted from "
            "current recall; run `engram list --unclassified`, then "
            "`engram classify ENTRY_ID --claim-key KEY`",
            count,
        )


def _verify_audit_integrity(connection: sqlite3.Connection) -> None:
    _precheck_audit_payload_lengths(connection)
    rows = connection.execute(
        """
        SELECT seq, ts, actor, action, entry_id, detail_hash
        FROM audit_log
        ORDER BY seq
        """
    )
    for row in rows:
        sequence = int(row["seq"])
        try:
            actor = str(row["actor"])
            _require_equal(
                normalize_actor(actor),
                actor,
                "actor is not normalized",
            )
            _validate_datetime_field(row["ts"], "ts", optional=False)
            AuditAction(str(row["action"]))
            if row["entry_id"] is not None:
                entry_id = str(row["entry_id"])
                _require_equal(
                    normalize_entry_id(entry_id),
                    entry_id,
                    "entry_id is not normalized",
                )
            if row["detail_hash"] is not None:
                detail_hash = str(row["detail_hash"])
                _require_equal(
                    normalize_sha256_hex(detail_hash, "detail_hash"),
                    detail_hash,
                    "detail_hash is not normalized",
                )
        except (StoreValidationError, TypeError, ValueError) as exc:
            raise DatabaseError(
                f"Audit record violates content invariant at sequence {sequence}: {exc}"
            ) from exc


def _verify_supersession_integrity(connection: sqlite3.Connection) -> None:
    _precheck_supersession_payload_lengths(connection)
    rows = connection.execute(
        "SELECT id, kind, scope, status, promotion_state, source_type, supersedes, "
        "canonical_key, claim_key "
        "FROM entries ORDER BY id"
    )
    rows_by_id: dict[str, _SupersessionNode] = {}
    json_edges: dict[str, str] = {}
    for row in rows:
        entry_id = str(row["id"])
        rows_by_id[entry_id] = _SupersessionNode(
            kind=str(row["kind"]),
            scope=str(row["scope"]),
            status=str(row["status"]),
            promotion_state=str(row["promotion_state"]),
            source_type=str(row["source_type"]),
            canonical_key=str(row["canonical_key"]),
            claim_key=None if row["claim_key"] is None else str(row["claim_key"]),
            claim_key_present=True,
        )
        try:
            decoded = json.loads(str(row["supersedes"]))
        except json.JSONDecodeError as exc:
            raise DatabaseError(f"Invalid supersedes JSON: {entry_id}") from exc
        if not isinstance(decoded, list) or not all(
            isinstance(old_id, str) and old_id for old_id in decoded
        ):
            raise DatabaseError(f"Invalid supersedes list: {entry_id}")
        if len(decoded) != len(set(decoded)):
            raise DatabaseError(f"Duplicate supersedes IDs: {entry_id}")
        for old_id in decoded:
            previous = json_edges.get(old_id)
            if previous is not None and previous != entry_id:
                raise DatabaseError(f"Entry has multiple replacements: {old_id}")
            json_edges[old_id] = entry_id
    relation_rows = connection.execute(
        """
        SELECT old_entry_id, new_entry_id, recorded_at, actor
        FROM entry_supersessions
        ORDER BY old_entry_id, new_entry_id
        """
    )
    relation_edges: dict[str, str] = {}
    for row in relation_rows:
        old_id = str(row["old_entry_id"])
        try:
            actor = str(row["actor"])
            _require_equal(
                normalize_actor(actor),
                actor,
                "actor is not normalized",
            )
            _validate_datetime_field(row["recorded_at"], "recorded_at", optional=False)
        except (StoreValidationError, TypeError, ValueError) as exc:
            raise DatabaseError(
                f"Supersession metadata violates content invariant for entry {old_id}: {exc}"
            ) from exc
        relation_edges[old_id] = str(row["new_entry_id"])
    if json_edges != relation_edges:
        raise DatabaseError("Supersession relation and legacy JSON are inconsistent")
    _verify_edge_graph(rows_by_id, relation_edges, context="Database")


def _verify_edge_graph(  # noqa: C901, PLR0912
    rows_by_id: dict[str, _SupersessionNode],
    edges: dict[str, str],
    *,
    context: str,
) -> None:
    for entry_id, row in rows_by_id.items():
        if row.status == "superseded" and entry_id not in edges:
            raise DatabaseError(
                f"{context} contains a superseded entry without replacement: {entry_id}"
            )
    for old_id, new_id in edges.items():
        old = rows_by_id.get(old_id)
        new = rows_by_id.get(new_id)
        if old is None or new is None:
            raise DatabaseError(f"{context} contains a dangling supersession: {old_id}")
        if old.kind != new.kind or old.scope != new.scope:
            raise DatabaseError(
                f"{context} contains a cross-kind or cross-scope supersession: {old_id}"
            )
        if old.status not in {"superseded", "expired"}:
            raise DatabaseError(f"{context} contains a non-terminal superseded entry: {old_id}")
        if not (
            new.promotion_state in {"approved", "promoted"}
            and new.source_type in {"human", "tool_verified"}
        ):
            raise DatabaseError(f"{context} contains an untrusted replacement: {new_id}")
        if old.promotion_state == "candidate":
            if old.canonical_key != new.canonical_key:
                raise DatabaseError(
                    f"{context} contains a cross-canonical candidate merge: {old_id}"
                )
        elif old.claim_key_present and old.claim_key != new.claim_key:
            raise DatabaseError(f"{context} contains a cross-claim supersession: {old_id}")
    checked: set[str] = set()
    for start in edges:
        if start in checked:
            continue
        path: set[str] = set()
        current = start
        while current in edges and current not in checked:
            if current in path:
                raise DatabaseError(f"{context} contains a supersession cycle at entry: {current}")
            path.add(current)
            current = edges[current]
        checked.update(path)


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
    row = connection.execute(
        """
        SELECT
            COUNT(*) AS row_count,
            SUM(CASE WHEN typeof(version) = 'integer' THEN 1 ELSE 0 END)
                AS integer_count,
            MIN(CASE WHEN typeof(version) = 'integer' THEN version END)
                AS valid_version
        FROM schema_version
        """
    ).fetchone()
    if row is None or int(row[0]) != 1:
        raise DatabaseError("schema_version must contain exactly one row")
    if int(row[1]) != 1:
        raise DatabaseError("schema_version must contain one non-negative integer")
    value = row[2]
    if type(value) is not int or value < 0:
        raise DatabaseError("schema_version must contain one non-negative integer")
    return value
