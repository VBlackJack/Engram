# Copyright 2026 Julien Bombled
# SPDX-License-Identifier: Apache-2.0

"""Entry invariants, migrations, and concurrent writer tests."""

from __future__ import annotations

import hashlib
import inspect
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from threading import Barrier

import pytest

import engram.db as db_module
from engram.cli import _list_entries
from engram.config import AppConfig
from engram.db import (
    MAX_CONSOLIDATION_SNAPSHOT_BYTES,
    DatabaseError,
    SQLiteVersionError,
    open_database,
    verify_sqlite_version,
)
from engram.models import (
    AuditAction,
    Confidence,
    Entry,
    EntryKind,
    EntryStatus,
    PromotionState,
    RememberOutcome,
    SourceType,
)
from engram.normalization import HARD_MAX_STATEMENT_CHARS, canonical_key, generation_key
from engram.retrieval import FtsRetriever, RetrievalRequest
from engram.store import EngramStore, StoreValidationError
from tests.conftest import MutableClock


def _seed_v4_trusted_entry(  # noqa: PLR0913
    config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
    *,
    entry_id: str,
    kind: EntryKind,
    statement: str,
    promotion_state: PromotionState = PromotionState.APPROVED,
    stale: bool = False,
    valid_from: str | None = None,
) -> None:
    migrations = db_module.MIGRATIONS
    content_key = canonical_key(kind, "project/engram", statement)
    promoted = promotion_state is PromotionState.PROMOTED
    connection = sqlite3.connect(config.database.path, isolation_level=None)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        monkeypatch.setattr(db_module, "MIGRATIONS", migrations[:4])
        db_module.apply_migrations(connection)
        connection.execute(
            """
            INSERT INTO entries(
                id, kind, scope, statement, subject_keys, status, promotion_state,
                source_type, writer_model, confidence, observed_at, recorded_at,
                valid_from, valid_until, expires_at, idempotency_key, supersedes,
                evidence, is_stale, datacron_ref, datacron_hash, synced_at
            ) VALUES (
                ?, ?, 'project/engram', ?, '[]', 'active', ?, 'human', NULL,
                'high', NULL, '2026-07-21T12:00:00.000000Z', ?, NULL, NULL, ?,
                '[]', '[]', ?, ?, ?, ?
            )
            """,
            (
                entry_id,
                kind.value,
                statement,
                promotion_state.value,
                valid_from,
                content_key,
                int(stale),
                "datacron://legacy" if promoted else None,
                "a" * 64 if promoted else None,
                "2026-07-21T12:00:00.000000Z" if promoted else None,
            ),
        )
    finally:
        connection.close()
        monkeypatch.setattr(db_module, "MIGRATIONS", migrations)


def _retrieval_request(query: str) -> RetrievalRequest:
    return RetrievalRequest(
        query=query,
        scope="project/engram",
        kinds=None,
        writer_model="test-client",
    )


def test_store_wraps_database_open_failure(
    app_config: AppConfig,
    tmp_path: Path,
) -> None:
    config = replace(
        app_config,
        database=replace(app_config.database, path=tmp_path),
    )

    with pytest.raises(DatabaseError, match="database open failed"):
        EngramStore(config)


def test_add_candidate_owns_provenance_fields(store: EngramStore) -> None:
    parameters = inspect.signature(store.add_candidate).parameters
    assert "status" not in parameters
    assert "promotion_state" not in parameters
    assert "source_type" not in parameters

    entry = store.add_candidate(
        kind=EntryKind.DECISION,
        scope="project/Engram",
        statement="  Keep   provenance server-side. ",
        writer_model="test-model",
        confidence=Confidence.LOW,
        subject_keys=("Security", "security", "Provenance"),
    )

    assert entry.status is EntryStatus.QUARANTINED
    assert entry.promotion_state is PromotionState.CANDIDATE
    assert entry.source_type is SourceType.MODEL_INFERRED
    assert entry.scope == "project/engram"
    assert entry.statement == "Keep provenance server-side."
    assert entry.subject_keys == ("security", "provenance")
    assert len(entry.id) == 26


def test_public_entry_constructor_preserves_version_four_arguments(
    clock: MutableClock,
) -> None:
    legacy = Entry(
        "01JJJJJJJJJJJJJJJJJJJJJJJJ",
        EntryKind.EPISODE,
        "user",
        "Legacy public constructor.",
        (),
        EntryStatus.ACTIVE,
        PromotionState.APPROVED,
        SourceType.HUMAN,
        None,
        Confidence.HIGH,
        None,
        clock.current,
        None,
        None,
        None,
        "legacy-idempotency",
        (),
        (),
        False,  # noqa: FBT003
        None,
        None,
        None,
    )

    assert legacy.canonical_key == ""
    assert legacy.claim_key is None


def test_add_attested_restricts_trusted_provenance(store: EngramStore) -> None:
    entry = store.add_attested(
        kind="fact",
        scope="global",
        statement="The value was verified by a tool.",
        source_type=SourceType.TOOL_VERIFIED,
        claim_key="runtime/sqlite-version",
    )

    assert entry.status is EntryStatus.ACTIVE
    assert entry.promotion_state is PromotionState.APPROVED
    assert entry.source_type is SourceType.TOOL_VERIFIED
    assert entry.confidence is Confidence.HIGH

    with pytest.raises(StoreValidationError, match="only accepts"):
        store.add_attested(
            kind="fact",
            scope="global",
            statement="Untrusted input",
            source_type=SourceType.MODEL_INFERRED,
            claim_key="invalid/provenance",
        )


@pytest.mark.parametrize(
    ("datacron_ref", "datacron_hash", "expected"),
    [
        (" ", "a" * 64, "datacron_ref"),
        ("datacron://verified", "A" * 64, "datacron_hash"),
        ("datacron://verified", "not-a-digest", "datacron_hash"),
    ],
)
def test_mark_promoted_rejects_noncanonical_datacron_identity_before_writing(
    store: EngramStore,
    datacron_ref: str,
    datacron_hash: str,
    expected: str,
) -> None:
    entry = store.add_attested(
        kind="fact",
        scope="user",
        statement="Promotion metadata is canonical.",
        source_type=SourceType.HUMAN,
        claim_key="promotion/metadata",
    )
    audit_before = store.list_audit()

    with pytest.raises(StoreValidationError, match=expected):
        store.mark_promoted(
            entry.id,
            datacron_ref=datacron_ref,
            datacron_hash=datacron_hash,
        )

    assert store.get_entry(entry.id) == entry
    assert store.list_audit() == audit_before


def test_add_attested_promotes_matching_candidate_in_place(store: EngramStore) -> None:
    candidate = store.add_candidate(
        kind="fact",
        scope="user",
        statement="The local endpoint is verified.",
        writer_model="client-a/1.0",
        confidence=Confidence.LOW,
        subject_keys=("endpoint",),
    )

    attested = store.add_attested(
        kind="fact",
        scope="USER",
        statement=" the local endpoint is VERIFIED. ",
        source_type=SourceType.HUMAN,
        confidence=Confidence.HIGH,
        claim_key="endpoint/local",
    )

    assert attested.id == candidate.id
    assert store.count_entries() == 1
    assert attested.status is EntryStatus.ACTIVE
    assert attested.promotion_state is PromotionState.APPROVED
    assert attested.source_type is SourceType.HUMAN
    assert attested.writer_model is None
    assert attested.confidence is Confidence.HIGH
    assert attested.subject_keys == ("endpoint",)
    assert [record.action for record in store.list_audit()] == [
        AuditAction.INSERT,
        AuditAction.ATTEST,
    ]


def test_sqlite_version_guard_rejects_older_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    connection = sqlite3.connect(":memory:")
    monkeypatch.setattr(db_module, "_read_sqlite_version", lambda connection: "3.51.2")
    try:
        with pytest.raises(SQLiteVersionError, match=r"installation-windows\.md"):
            verify_sqlite_version(connection)
    finally:
        connection.close()


def test_database_uses_wal_and_numbered_migration(
    store: EngramStore,
    app_config: AppConfig,
) -> None:
    del store
    connection = open_database(app_config.database)
    try:
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()
        busy_timeout = connection.execute("PRAGMA busy_timeout").fetchone()
        foreign_keys = connection.execute("PRAGMA foreign_keys").fetchone()
        schema_version = connection.execute("SELECT version FROM schema_version").fetchone()
        schema_tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_schema "
                "WHERE name IN ("
                "'entries_fts', 'entry_vectors', 'consolidation_plans', "
                "'entry_observations', 'entry_supersessions'"
                ")"
            ).fetchall()
        }
    finally:
        connection.close()

    assert journal_mode is not None
    assert journal_mode[0] == "wal"
    assert busy_timeout is not None
    assert busy_timeout[0] == app_config.database.busy_timeout_ms
    assert foreign_keys is not None
    assert foreign_keys[0] == 1
    assert schema_version is not None
    assert schema_version[0] == 5
    assert schema_tables == {
        "entries_fts",
        "entry_vectors",
        "consolidation_plans",
        "entry_observations",
        "entry_supersessions",
    }


def test_migration_five_preserves_existing_version_four_entries(
    app_config: AppConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "version-four.db"
    config = replace(
        app_config,
        database=replace(app_config.database, path=database_path),
    )
    legacy_canonical = canonical_key(
        EntryKind.FACT,
        "project/engram",
        "Migration five preserves existing entries.",
    )
    migrations = db_module.MIGRATIONS
    connection = sqlite3.connect(database_path, isolation_level=None)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        monkeypatch.setattr(db_module, "MIGRATIONS", migrations[:4])
        db_module.apply_migrations(connection)
        connection.execute(
            """
            INSERT INTO entries(
                id, kind, scope, statement, subject_keys, status, promotion_state,
                source_type, writer_model, confidence, observed_at, recorded_at,
                valid_from, valid_until, expires_at, idempotency_key, supersedes,
                evidence, is_stale, datacron_ref, datacron_hash, synced_at
            ) VALUES (
                '01AAAAAAAAAAAAAAAAAAAAAAAA', 'fact', 'project/engram',
                'Migration five preserves existing entries.', '[]', 'active',
                'approved', 'human', NULL, 'high', NULL,
                '2026-07-21T12:00:00.000000Z', NULL, NULL, NULL, ?, '[]', '[]',
                0, NULL, NULL, NULL
            )
            """,
            (legacy_canonical,),
        )
    finally:
        connection.close()
        monkeypatch.setattr(db_module, "MIGRATIONS", migrations)

    with EngramStore(config) as upgraded:
        preserved = upgraded.get_entry("01AAAAAAAAAAAAAAAAAAAAAAAA")
        assert preserved is not None
        assert preserved.statement == "Migration five preserves existing entries."
        assert preserved.canonical_key == legacy_canonical
        assert preserved.claim_key is None
        classified = upgraded.add_attested(
            kind="fact",
            scope="project/engram",
            statement="Migration five preserves existing entries.",
            source_type=SourceType.HUMAN,
            claim_key="migration/preservation",
        )
        assert classified.id == preserved.id
        assert classified.claim_key == "migration/preservation"

    connection = sqlite3.connect(database_path)
    try:
        version = connection.execute("SELECT version FROM schema_version").fetchone()
        observations = connection.execute("SELECT count(*) FROM entry_observations").fetchone()
    finally:
        connection.close()
    assert version == (5,)
    assert observations == (0,)


@pytest.mark.parametrize("mode", ["stale", "future"])
def test_legacy_unclassified_inventory_and_explicit_classification(  # noqa: PLR0913
    app_config: AppConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    clock: MutableClock,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
    mode: str,
) -> None:
    database_path = tmp_path / f"legacy-{mode}.db"
    config = replace(
        app_config,
        database=replace(app_config.database, path=database_path),
    )
    entry_id = "01EEEEEEEEEEEEEEEEEEEEEEEE"
    statement = f"The legacy {mode} setting is retained."
    _seed_v4_trusted_entry(
        config,
        monkeypatch,
        entry_id=entry_id,
        kind=EntryKind.FACT,
        statement=statement,
        promotion_state=(PromotionState.PROMOTED if mode == "stale" else PromotionState.APPROVED),
        stale=mode == "stale",
        valid_from="2026-07-22" if mode == "future" else None,
    )

    with caplog.at_level("WARNING"), EngramStore(config, clock=clock) as upgraded:
        assert "list --unclassified" in caplog.text
        _list_entries(config=config, unclassified=True)
        inventory = json.loads(capsys.readouterr().out)
        assert [item["id"] for item in inventory] == [entry_id]

        classified = upgraded.classify_claim(
            entry_id,
            "legacy/setting",
            actor="migration-reviewer",
        )
        assert classified.claim_key == "legacy/setting"
        assert classified.stale is (mode == "stale")
        assert classified.valid_from is not None if mode == "future" else True
        assert upgraded.list_audit()[-1].action is AuditAction.CLASSIFY

        retriever = FtsRetriever(upgraded)
        assert retriever.retrieve(_retrieval_request(statement)).matches == ()
        if mode == "stale":
            upgraded.set_stale(entry_id, stale=False)
        else:
            clock.current += timedelta(days=1)
        assert [item.id for item in retriever.retrieve(_retrieval_request(statement)).matches] == [
            entry_id
        ]


def test_migration_backfills_project_state_claim_for_recall_and_supersession(
    app_config: AppConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    clock: MutableClock,
) -> None:
    database_path = tmp_path / "legacy-project-state.db"
    config = replace(
        app_config,
        database=replace(app_config.database, path=database_path),
    )
    entry_id = "01FFFFFFFFFFFFFFFFFFFFFFFF"
    statement = "The legacy project state is active."
    _seed_v4_trusted_entry(
        config,
        monkeypatch,
        entry_id=entry_id,
        kind=EntryKind.PROJECT_STATE,
        statement=statement,
    )

    with EngramStore(config, clock=clock) as upgraded:
        legacy = upgraded.get_entry(entry_id)
        assert legacy is not None
        assert legacy.claim_key == "project_state/current"
        recalled = FtsRetriever(upgraded).retrieve(_retrieval_request(statement))
        assert [entry.id for entry in recalled.next_actions] == [entry_id]

        promoted = upgraded.mark_promoted(
            entry_id,
            datacron_ref="datacron://project-state",
            datacron_hash="b" * 64,
        )
        assert promoted.promotion_state is PromotionState.PROMOTED
        replacement = upgraded.add_attested(
            kind="project_state",
            scope="project/engram",
            statement="The migrated project state is complete.",
            source_type=SourceType.HUMAN,
            supersedes=(entry_id,),
        )
        retired = upgraded.get_entry(entry_id)
        assert retired is not None
        assert retired.status is EntryStatus.SUPERSEDED
        assert replacement.supersedes == (entry_id,)


def test_legacy_supersession_classifies_old_entry_atomically(
    app_config: AppConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    clock: MutableClock,
) -> None:
    database_path = tmp_path / "legacy-supersession.db"
    config = replace(
        app_config,
        database=replace(app_config.database, path=database_path),
    )
    old_id = "01GGGGGGGGGGGGGGGGGGGGGGGG"
    _seed_v4_trusted_entry(
        config,
        monkeypatch,
        entry_id=old_id,
        kind=EntryKind.FACT,
        statement="The legacy release is pending.",
    )

    with EngramStore(config, clock=clock) as upgraded:
        replacement = upgraded.add_attested(
            kind="fact",
            scope="project/engram",
            statement="The legacy release is complete.",
            source_type=SourceType.HUMAN,
            claim_key="release/state",
            supersedes=(old_id,),
        )
        retired = upgraded.get_entry(old_id)
        assert retired is not None
        assert retired.claim_key == "release/state"
        assert retired.status is EntryStatus.SUPERSEDED
        assert replacement.supersedes == (old_id,)
        assert [record.action for record in upgraded.list_audit()] == [
            AuditAction.ATTEST,
            AuditAction.CLASSIFY,
            AuditAction.SUPERSEDE,
        ]


def test_migration_transaction_rolls_back_schema_and_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
    connection.execute("INSERT INTO schema_version(version) VALUES (3)")
    migration = db_module.Migration(
        version=4,
        statements=(
            "CREATE TABLE migration_probe (value TEXT NOT NULL)",
            "INVALID SQL",
        ),
    )
    monkeypatch.setattr(db_module, "MIGRATIONS", (migration,))
    try:
        with pytest.raises(sqlite3.OperationalError):
            db_module.apply_migrations(connection)
        version = connection.execute("SELECT version FROM schema_version").fetchone()
        probe = connection.execute(
            "SELECT count(*) FROM sqlite_schema WHERE name = 'migration_probe'"
        ).fetchone()
    finally:
        connection.close()

    assert version == (3,)
    assert probe == (0,)


def test_migration_five_rejects_invalid_v4_lifecycle_without_advancing(
    app_config: AppConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "invalid-version-four.db"
    config = replace(
        app_config,
        database=replace(app_config.database, path=database_path),
    )
    migrations = db_module.MIGRATIONS
    connection = sqlite3.connect(database_path, isolation_level=None)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        monkeypatch.setattr(db_module, "MIGRATIONS", migrations[:4])
        db_module.apply_migrations(connection)
        connection.execute(
            """
            INSERT INTO entries(
                id, kind, scope, statement, subject_keys, status, promotion_state,
                source_type, writer_model, confidence, observed_at, recorded_at,
                valid_from, valid_until, expires_at, idempotency_key, supersedes,
                evidence, is_stale, datacron_ref, datacron_hash, synced_at
            ) VALUES (
                '01AAAAAAAAAAAAAAAAAAAAAAAA', 'fact', 'user', 'Invalid trusted state.',
                '[]', 'active', 'candidate', 'model_inferred', 'model-a', 'medium',
                NULL, '2026-07-21T12:00:00.000000Z', NULL, NULL, NULL, ?, '[]',
                '[]', 0, NULL, NULL, NULL
            )
            """,
            ("b" * 64,),
        )
    finally:
        connection.close()
        monkeypatch.setattr(db_module, "MIGRATIONS", migrations)

    with pytest.raises(DatabaseError, match="invalid lifecycle"):
        EngramStore(config)

    connection = sqlite3.connect(database_path)
    try:
        version = connection.execute("SELECT version FROM schema_version").fetchone()
        canonical_column = connection.execute(
            "SELECT 1 FROM pragma_table_info('entries') WHERE name = 'canonical_key'"
        ).fetchone()
    finally:
        connection.close()
    assert version == (4,)
    assert canonical_column is None


def test_database_triggers_reject_illegal_lifecycle_and_claim_links(
    store: EngramStore,
    app_config: AppConfig,
) -> None:
    candidate = store.add_candidate(
        kind="fact",
        scope="user",
        statement="Candidate must stay quarantined.",
        writer_model="model-a",
    )
    old = store.add_attested(
        kind="fact",
        scope="user",
        statement="Storage uses one engine.",
        source_type=SourceType.HUMAN,
        claim_key="storage/engine",
    )
    wrong_claim = store.add_attested(
        kind="fact",
        scope="user",
        statement="Storage uses another engine.",
        source_type=SourceType.HUMAN,
        claim_key="storage/other",
    )
    connection = sqlite3.connect(app_config.database.path)
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE entries SET status = 'active' WHERE id = ?",
                (candidate.id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="transition"):
            connection.execute(
                "UPDATE entries SET status = 'superseded' WHERE id = ?",
                (old.id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="claim_key"):
            connection.execute(
                """
                INSERT INTO entry_supersessions(
                    old_entry_id, new_entry_id, recorded_at, actor
                ) VALUES (?, ?, '2026-07-21T12:00:00.000000Z', 'sql-test')
                """,
                (old.id, wrong_claim.id),
            )
        with pytest.raises(sqlite3.IntegrityError, match="identical canonical"):
            connection.execute(
                """
                INSERT INTO entry_supersessions(
                    old_entry_id, new_entry_id, recorded_at, actor
                ) VALUES (?, ?, '2026-07-21T12:00:00.000000Z', 'sql-test')
                """,
                (candidate.id, wrong_claim.id),
            )
    finally:
        connection.close()

    stored_candidate = store.get_entry(candidate.id)
    stored_old = store.get_entry(old.id)
    assert stored_candidate is not None
    assert stored_candidate.status is EntryStatus.QUARANTINED
    assert stored_old is not None
    assert stored_old.status is EntryStatus.ACTIVE


@pytest.mark.parametrize(
    ("object_type", "name", "expected"),
    [
        ("TRIGGER", "entries_lifecycle_insert", "required trigger"),
        ("INDEX", "entries_live_trusted_canonical_idx", "required partial index"),
    ],
)
def test_reopen_rejects_missing_critical_schema_objects(
    store: EngramStore,
    app_config: AppConfig,
    object_type: str,
    name: str,
    expected: str,
) -> None:
    del store
    connection = sqlite3.connect(app_config.database.path)
    try:
        connection.execute(f'DROP {object_type} "{name}"')
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(DatabaseError, match=expected):
        EngramStore(app_config)


@pytest.mark.parametrize(
    ("drop_sql", "create_sql", "expected"),
    [
        (
            "DROP TRIGGER entries_lifecycle_insert",
            """
            CREATE TRIGGER entries_lifecycle_insert
            BEFORE INSERT ON entries
            BEGIN
                SELECT 1;
            END
            """,
            "trigger definition is invalid",
        ),
        (
            "DROP INDEX entries_live_trusted_canonical_idx",
            """
            CREATE UNIQUE INDEX entries_live_trusted_canonical_idx
            ON entries(canonical_key)
            WHERE status = 'active' AND kind = 'fact'
            """,
            "partial index definition is invalid",
        ),
    ],
)
def test_reopen_rejects_same_name_schema_definition_drift(
    store: EngramStore,
    app_config: AppConfig,
    drop_sql: str,
    create_sql: str,
    expected: str,
) -> None:
    del store
    connection = sqlite3.connect(app_config.database.path)
    try:
        connection.execute(drop_sql)
        connection.execute(create_sql)
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(DatabaseError, match=expected):
        EngramStore(app_config)


@pytest.mark.parametrize(
    ("corruption", "expected"),
    [
        ("canonical", "canonical_key"),
        ("scope", "scope"),
        ("statement", "statement"),
        ("subject_keys", "subject_keys"),
        ("claim_key", "claim_key"),
        ("idempotency", "idempotency_key"),
    ],
)
def test_reopen_rejects_direct_sql_content_corruption(
    store: EngramStore,
    app_config: AppConfig,
    corruption: str,
    expected: str,
) -> None:
    del store
    scope = "USER" if corruption == "scope" else "user"
    statement = (
        "Direct   SQL statement is malformed."
        if corruption == "statement"
        else "Direct SQL content must remain normalized."
    )
    subject_keys = '["Runtime","runtime"]' if corruption == "subject_keys" else "[]"
    claim_key = " Editor/Theme " if corruption == "claim_key" else "editor/theme"
    computed_canonical = canonical_key(EntryKind.FACT, scope, statement)
    stored_canonical = "f" * 64 if corruption == "canonical" else computed_canonical
    stored_idempotency = (
        "d" * 64
        if corruption == "idempotency"
        else generation_key(
            canonical_key=stored_canonical,
            entry_id="01CCCCCCCCCCCCCCCCCCCCCCCC",
        )
    )
    connection = sqlite3.connect(app_config.database.path)
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        connection.execute(
            """
            INSERT INTO entries(
                id, kind, scope, statement, subject_keys, status, promotion_state,
                source_type, writer_model, confidence, observed_at, recorded_at,
                valid_from, valid_until, expires_at, idempotency_key, supersedes,
                evidence, is_stale, datacron_ref, datacron_hash, synced_at,
                canonical_key, claim_key
            ) VALUES (
                '01CCCCCCCCCCCCCCCCCCCCCCCC', 'fact', ?, ?, ?, 'active',
                'approved', 'human', NULL, 'high', NULL,
                '2026-07-21T12:00:00.000000Z', NULL, NULL, NULL, ?, '[]', '[]',
                0, NULL, NULL, NULL, ?, ?
            )
            """,
            (
                scope,
                statement,
                subject_keys,
                stored_idempotency,
                stored_canonical,
                claim_key,
            ),
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(DatabaseError, match=expected):
        EngramStore(app_config)


def test_reopen_rejects_live_candidate_without_owner_observation(
    store: EngramStore,
    app_config: AppConfig,
) -> None:
    candidate = store.add_candidate(
        kind="fact",
        scope="user",
        statement="A live candidate retains its owner observation.",
        writer_model="model-a",
    )
    connection = sqlite3.connect(app_config.database.path)
    try:
        connection.execute(
            "DELETE FROM entry_observations WHERE entry_id = ?",
            (candidate.id,),
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(DatabaseError, match="no retained owner observation"):
        EngramStore(app_config)


@pytest.mark.parametrize(
    ("column", "value", "expected"),
    [
        ("claim_hash", "z" * 64, "claim_hash"),
        ("subject_keys", '[" Runtime "]', "subject_keys"),
        ("recorded_at", "2026-07-21T12:00:00", "recorded_at"),
    ],
)
def test_reopen_rejects_corrupt_observation_content(
    store: EngramStore,
    app_config: AppConfig,
    column: str,
    value: str,
    expected: str,
) -> None:
    candidate = store.add_candidate(
        kind="fact",
        scope="user",
        statement="Observation metadata remains canonical.",
        writer_model="model-a",
    )
    connection = sqlite3.connect(app_config.database.path)
    try:
        connection.execute(
            f'UPDATE entry_observations SET "{column}" = ? WHERE entry_id = ?',  # noqa: S608
            (value, candidate.id),
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(DatabaseError, match=expected):
        EngramStore(app_config)


@pytest.mark.parametrize(
    ("column", "value", "expected"),
    [
        ("subject_keys", "not-json", "subject_keys"),
        ("evidence", '[{"type":"unknown","ref":"tool://bad"}]', "evidence"),
        ("recorded_at", "2026-07-21T12:00:00", "recorded_at"),
        ("valid_from", "2026-99-99", "valid_from"),
        ("scope", "USER", "scope"),
        ("statement", "Legacy   spacing is invalid.", "statement"),
        ("is_stale", "broken", "is_stale"),
    ],
)
def test_migration_five_rejects_malformed_v4_content_atomically(  # noqa: PLR0913
    app_config: AppConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    column: str,
    value: str,
    expected: str,
) -> None:
    database_path = tmp_path / f"invalid-v4-{column}.db"
    config = replace(
        app_config,
        database=replace(app_config.database, path=database_path),
    )
    migrations = db_module.MIGRATIONS
    statement = "Legacy normalized content."
    legacy_canonical = canonical_key(EntryKind.FACT, "user", statement)
    connection = sqlite3.connect(database_path, isolation_level=None)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        monkeypatch.setattr(db_module, "MIGRATIONS", migrations[:4])
        db_module.apply_migrations(connection)
        connection.execute(
            """
            INSERT INTO entries(
                id, kind, scope, statement, subject_keys, status, promotion_state,
                source_type, writer_model, confidence, observed_at, recorded_at,
                valid_from, valid_until, expires_at, idempotency_key, supersedes,
                evidence, is_stale, datacron_ref, datacron_hash, synced_at
            ) VALUES (
                '01DDDDDDDDDDDDDDDDDDDDDDDD', 'fact', 'user', ?, '[]',
                'active', 'approved', 'human', NULL, 'high', NULL,
                '2026-07-21T12:00:00.000000Z', NULL, NULL, NULL, ?, '[]', '[]',
                0, NULL, NULL, NULL
            )
            """,
            (statement, legacy_canonical),
        )
        if column == "is_stale":
            connection.execute("PRAGMA ignore_check_constraints = ON")
        connection.execute(
            f'UPDATE entries SET "{column}" = ? WHERE id = ?',  # noqa: S608
            (value, "01DDDDDDDDDDDDDDDDDDDDDDDD"),
        )
    finally:
        connection.close()
        monkeypatch.setattr(db_module, "MIGRATIONS", migrations)

    with pytest.raises(DatabaseError, match=rf"{expected}.*01D|01D.*{expected}"):
        EngramStore(config)

    connection = sqlite3.connect(database_path)
    try:
        version = connection.execute("SELECT version FROM schema_version").fetchone()
        canonical_column = connection.execute(
            "SELECT 1 FROM pragma_table_info('entries') WHERE name = 'canonical_key'"
        ).fetchone()
    finally:
        connection.close()
    assert version == (4,)
    assert canonical_column is None


def test_the_recency_window_narrows_by_scope_and_by_kind(store: EngramStore) -> None:
    """Both optional filters build SQL of their own; an unfiltered call exercises neither."""
    for kind, scope, statement in (
        (EntryKind.FACT, "project/engram", "The recency window keeps facts."),
        (EntryKind.DECISION, "project/engram", "The recency window keeps decisions."),
        (EntryKind.FACT, "user", "The recency window separates scopes."),
    ):
        store.add_candidate(
            kind=kind,
            scope=scope,
            statement=statement,
            writer_model="test-model",
            confidence=Confidence.HIGH,
            subject_keys=("recency",),
        )

    def _statements(**kwargs: object) -> set[str]:
        entries = store.list_retrieval_entries(
            writer_model="test-model",
            limit=10,
            **kwargs,  # type: ignore[arg-type]
        )
        return {entry.statement for entry in entries}

    unfiltered = _statements(scope=None, kinds=None)
    by_scope = _statements(scope="project/Engram", kinds=None)
    by_kind = _statements(scope=None, kinds=frozenset({EntryKind.DECISION}))
    by_both = _statements(scope="project/engram", kinds=frozenset({EntryKind.FACT}))

    assert len(unfiltered) == 3
    assert by_scope == {
        "The recency window keeps facts.",
        "The recency window keeps decisions.",
    }
    assert by_kind == {"The recency window keeps decisions."}
    assert by_both == {"The recency window keeps facts."}


@pytest.mark.parametrize(
    ("supersedes", "expected"),
    [
        ("not-json", "invalid supersedes JSON"),
        ('{"old": "01AAAAAAAAAAAAAAAAAAAAAAAA"}', "invalid supersedes list"),
        ("[42]", "invalid supersedes list"),
        ('[""]', "invalid supersedes list"),
        (
            '["01AAAAAAAAAAAAAAAAAAAAAAAA", "01AAAAAAAAAAAAAAAAAAAAAAAA"]',
            "duplicate supersedes IDs",
        ),
        ('["not a normalized id"]', "field supersedes"),
        ('["01SSSSSSSSSSSSSSSSSSSSSSSS"]', "self supersession"),
    ],
)
def test_migration_five_refuses_an_unusable_supersession_edge(
    app_config: AppConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    supersedes: str,
    expected: str,
) -> None:
    """A supersession edge decides which memory is current, so a bad one blocks the upgrade."""
    database_path = tmp_path / "invalid-supersedes-v4.db"
    config = replace(
        app_config,
        database=replace(app_config.database, path=database_path),
    )
    entry_id = "01SSSSSSSSSSSSSSSSSSSSSSSS"
    migrations = db_module.MIGRATIONS
    connection = sqlite3.connect(database_path, isolation_level=None)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        monkeypatch.setattr(db_module, "MIGRATIONS", migrations[:4])
        db_module.apply_migrations(connection)
        # The preflight bound on the supersedes column scales with the number of
        # entries, so a lone row would be refused for its length before the
        # migration ever inspects the edge. Seed a second row to clear it.
        for identifier, statement, value in (
            ("01FFFFFFFFFFFFFFFFFFFFFFFF", "Legacy unrelated content.", "[]"),
            (entry_id, "Legacy normalized content.", supersedes),
        ):
            connection.execute(
                """
                INSERT INTO entries(
                    id, kind, scope, statement, subject_keys, status, promotion_state,
                    source_type, writer_model, confidence, observed_at, recorded_at,
                    valid_from, valid_until, expires_at, idempotency_key, supersedes,
                    evidence, is_stale, datacron_ref, datacron_hash, synced_at
                ) VALUES (
                    ?, 'fact', 'user', ?, '[]',
                    'active', 'approved', 'human', NULL, 'high', NULL,
                    '2026-07-21T12:00:00.000000Z', NULL, NULL, NULL, ?, ?, '[]',
                    0, NULL, NULL, NULL
                )
                """,
                (
                    identifier,
                    statement,
                    canonical_key(EntryKind.FACT, "user", statement),
                    value,
                ),
            )
    finally:
        connection.close()
        monkeypatch.setattr(db_module, "MIGRATIONS", migrations)

    with pytest.raises(DatabaseError, match=expected):
        EngramStore(config)

    connection = sqlite3.connect(database_path)
    try:
        version = connection.execute("SELECT version FROM schema_version").fetchone()
    finally:
        connection.close()
    assert version == (4,), "a refused migration must leave the schema where it was"


def test_migration_five_carries_a_valid_supersession_edge_forward(
    app_config: AppConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The refusals above only mean something if the accepted shape is exercised too."""
    database_path = tmp_path / "valid-supersedes-v4.db"
    config = replace(
        app_config,
        database=replace(app_config.database, path=database_path),
    )
    old_id = "01BBBBBBBBBBBBBBBBBBBBBBBB"
    new_id = "01NEWNEWNEWNEWNEWNEWNEWNEW"
    migrations = db_module.MIGRATIONS
    connection = sqlite3.connect(database_path, isolation_level=None)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        monkeypatch.setattr(db_module, "MIGRATIONS", migrations[:4])
        db_module.apply_migrations(connection)
        # The replaced entry must already be terminal: migration v5 refuses an
        # edge that would leave two entries current for the same claim.
        for identifier, statement, status, supersedes in (
            (old_id, "Legacy superseded content.", "superseded", "[]"),
            (new_id, "Legacy replacement content.", "active", f'["{old_id}"]'),
        ):
            connection.execute(
                """
                INSERT INTO entries(
                    id, kind, scope, statement, subject_keys, status, promotion_state,
                    source_type, writer_model, confidence, observed_at, recorded_at,
                    valid_from, valid_until, expires_at, idempotency_key, supersedes,
                    evidence, is_stale, datacron_ref, datacron_hash, synced_at
                ) VALUES (
                    ?, 'fact', 'user', ?, '[]',
                    ?, 'approved', 'human', NULL, 'high', NULL,
                    '2026-07-21T12:00:00.000000Z', NULL, NULL, NULL, ?, ?, '[]',
                    0, NULL, NULL, NULL
                )
                """,
                (
                    identifier,
                    statement,
                    status,
                    canonical_key(EntryKind.FACT, "user", statement),
                    supersedes,
                ),
            )
    finally:
        connection.close()
        monkeypatch.setattr(db_module, "MIGRATIONS", migrations)

    with EngramStore(config):
        pass

    connection = sqlite3.connect(database_path)
    try:
        version = connection.execute("SELECT version FROM schema_version").fetchone()
    finally:
        connection.close()
    assert version == (len(migrations),)


def test_migration_precheck_bounds_nul_terminated_text_before_materializing(
    app_config: AppConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "nul-terminated-oversize-v4.db"
    config = replace(
        app_config,
        database=replace(app_config.database, path=database_path),
    )
    entry_id = "01NNNNNNNNNNNNNNNNNNNNNNNN"
    _seed_v4_trusted_entry(
        config,
        monkeypatch,
        entry_id=entry_id,
        kind=EntryKind.FACT,
        statement="Legacy bounded content.",
    )
    connection = sqlite3.connect(database_path)
    try:
        connection.execute(
            "UPDATE entries SET statement = ? WHERE id = ?",
            ("A\0" + "x" * (HARD_MAX_STATEMENT_CHARS * 8), entry_id),
        )
        connection.commit()
    finally:
        connection.close()

    def forbidden_materialization(*args: object, **kwargs: object) -> None:
        del args, kwargs
        pytest.fail("migration materialized an entry after the allocation precheck")

    monkeypatch.setattr(db_module, "_validate_v4_entry_content", forbidden_materialization)

    with pytest.raises(DatabaseError, match=rf"{entry_id}.*statement|statement.*{entry_id}"):
        EngramStore(config)


def test_concurrent_writes_are_serialized_without_loss(store: EngramStore) -> None:
    entry_count = 48

    def add(index: int) -> str:
        return store.add_candidate(
            kind=EntryKind.EPISODE,
            scope="session/concurrency",
            statement=f"Concurrent entry {index}",
            writer_model="thread-test",
        ).id

    with ThreadPoolExecutor(max_workers=12) as executor:
        identifiers = tuple(executor.map(add, range(entry_count)))

    assert len(set(identifiers)) == entry_count
    assert store.count_entries() == entry_count


def test_independent_stores_share_one_retry_identity(
    app_config: AppConfig,
    clock: MutableClock,
) -> None:
    with EngramStore(app_config, clock=clock):
        pass
    stores = [EngramStore(app_config, clock=clock) for _ in range(12)]
    barrier = Barrier(len(stores))

    def remember(instance: EngramStore) -> tuple[str, RememberOutcome]:
        barrier.wait()
        result = instance.add_candidate(
            kind="fact",
            scope="project/engram",
            statement="All clients observe one canonical retry generation.",
            writer_model="shared-client",
            include_outcome=True,
        )
        return result.entry.id, result.outcome

    try:
        with ThreadPoolExecutor(max_workers=len(stores)) as executor:
            outcomes = tuple(executor.map(remember, stores))
    finally:
        for instance in stores:
            instance.close()

    assert len({entry_id for entry_id, _ in outcomes}) == 1
    assert sum(outcome is RememberOutcome.CREATED for _, outcome in outcomes) == 1
    assert sum(outcome is RememberOutcome.RETRY for _, outcome in outcomes) == 11


def test_concurrent_replacements_leave_one_commit_and_no_orphan(
    store: EngramStore,
    app_config: AppConfig,
    clock: MutableClock,
) -> None:
    old = store.add_attested(
        kind="fact",
        scope="project/engram",
        statement="The release is pending.",
        source_type=SourceType.HUMAN,
        claim_key="release/state",
    )
    first = EngramStore(app_config, clock=clock)
    second = EngramStore(app_config, clock=clock)
    barrier = Barrier(2)

    def replace(item: tuple[EngramStore, str]) -> str:
        instance, statement = item
        barrier.wait()
        try:
            entry = instance.add_attested(
                kind="fact",
                scope="project/engram",
                statement=statement,
                source_type=SourceType.HUMAN,
                claim_key="release/state",
                supersedes=(old.id,),
            )
        except StoreValidationError as exc:
            return f"error:{exc}"
        return f"committed:{entry.id}"

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = tuple(
                executor.map(
                    replace,
                    (
                        (first, "The release is complete."),
                        (second, "The release was cancelled."),
                    ),
                )
            )
    finally:
        first.close()
        second.close()

    assert sum(outcome.startswith("committed:") for outcome in outcomes) == 1
    assert sum("different replacement" in outcome for outcome in outcomes) == 1
    assert store.count_entries() == 2
    stored_old = store.get_entry(old.id)
    assert stored_old is not None
    assert stored_old.status is EntryStatus.SUPERSEDED
    assert [record.action for record in store.list_audit()] == [
        AuditAction.ATTEST,
        AuditAction.ATTEST,
        AuditAction.SUPERSEDE,
    ]


def test_supersession_chain_expires_and_purges_without_dangling_links(
    store: EngramStore,
    app_config: AppConfig,
    clock: MutableClock,
) -> None:
    first = store.add_attested(
        kind="project_state",
        scope="project/engram",
        statement="The reliability work is planned.",
        source_type=SourceType.HUMAN,
    )
    second = store.add_attested(
        kind="project_state",
        scope="project/engram",
        statement="The reliability work is active.",
        source_type=SourceType.HUMAN,
        supersedes=(first.id,),
    )
    third = store.add_attested(
        kind="project_state",
        scope="project/engram",
        statement="The reliability work is complete.",
        source_type=SourceType.HUMAN,
        supersedes=(second.id,),
    )
    assert third.expires_at is not None
    clock.current = third.expires_at + timedelta(microseconds=1)

    assert store.expire_due() == 3
    assert store.purge_expired(clock.current) == 3
    assert store.count_entries() == 0

    connection = sqlite3.connect(app_config.database.path)
    try:
        relation_count = connection.execute("SELECT COUNT(*) FROM entry_supersessions").fetchone()
    finally:
        connection.close()
    assert relation_count == (0,)


def test_consolidation_plan_snapshot_is_persisted_and_consumed_once(
    store: EngramStore,
) -> None:
    snapshot_json = '{"schema_version":1,"propositions":[]}'

    created = store.create_consolidation_plan(snapshot_json)
    loaded = store.get_consolidation_plan(created.plan_id)

    assert loaded is not None
    assert loaded.snapshot_json == snapshot_json
    assert loaded.snapshot_hash == hashlib.sha256(snapshot_json.encode("utf-8")).hexdigest()
    assert loaded.consumed_at is None

    consumed = store.consume_consolidation_plan(
        created.plan_id,
        expected_hash=created.snapshot_hash,
    )
    assert consumed.consumed_at is not None
    with pytest.raises(StoreValidationError, match="already consumed"):
        store.consume_consolidation_plan(
            created.plan_id,
            expected_hash=created.snapshot_hash,
        )


def test_consolidation_plan_rejects_oversized_snapshot_before_mutation(
    store: EngramStore,
) -> None:
    with pytest.raises(StoreValidationError, match=r"snapshot_json.*allocation bound"):
        store.create_consolidation_plan("x" * (MAX_CONSOLIDATION_SNAPSHOT_BYTES + 1))


def test_reopen_rejects_legacy_oversized_consolidation_snapshot(
    app_config: AppConfig,
) -> None:
    with EngramStore(app_config) as store:
        created = store.create_consolidation_plan('{"schema_version":1,"propositions":[]}')
    connection = sqlite3.connect(app_config.database.path)
    try:
        connection.execute(
            "UPDATE consolidation_plans SET snapshot_json = ? WHERE plan_id = ?",
            (
                "x" * (MAX_CONSOLIDATION_SNAPSHOT_BYTES + 1),
                created.plan_id,
            ),
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(DatabaseError, match="consolidation plan payload"):
        EngramStore(app_config)


def test_consolidation_plan_is_consumed_by_exactly_one_independent_store(
    store: EngramStore,
    app_config: AppConfig,
) -> None:
    created = store.create_consolidation_plan('{"schema_version":1,"propositions":[]}')
    first = EngramStore(app_config)
    second = EngramStore(app_config)
    barrier = Barrier(2)

    def consume(candidate: EngramStore) -> str:
        barrier.wait()
        try:
            candidate.consume_consolidation_plan(
                created.plan_id,
                expected_hash=created.snapshot_hash,
            )
        except StoreValidationError as exc:
            return str(exc)
        return "consumed"

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = tuple(executor.map(consume, (first, second)))
    finally:
        first.close()
        second.close()

    assert outcomes.count("consumed") == 1
    assert sum("already consumed" in outcome for outcome in outcomes) == 1


def test_consolidation_plan_consumption_detects_snapshot_corruption(
    store: EngramStore,
    app_config: AppConfig,
) -> None:
    created = store.create_consolidation_plan('{"schema_version":1,"propositions":[]}')
    connection = sqlite3.connect(app_config.database.path)
    try:
        connection.execute(
            "UPDATE consolidation_plans SET snapshot_json = ? WHERE plan_id = ?",
            ('{"schema_version":1,"propositions":["changed"]}', created.plan_id),
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(StoreValidationError, match="snapshot changed"):
        store.consume_consolidation_plan(
            created.plan_id,
            expected_hash=created.snapshot_hash,
        )
