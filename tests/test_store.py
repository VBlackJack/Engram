# Copyright 2026 Julien Bombled
# SPDX-License-Identifier: Apache-2.0

"""Entry invariants, migrations, and concurrent writer tests."""

from __future__ import annotations

import hashlib
import inspect
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Barrier

import pytest

import engram.db as db_module
from engram.config import AppConfig
from engram.db import SQLiteVersionError, open_database, verify_sqlite_version
from engram.models import (
    AuditAction,
    Confidence,
    EntryKind,
    EntryStatus,
    PromotionState,
    SourceType,
)
from engram.store import EngramStore, StoreValidationError


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


def test_add_attested_restricts_trusted_provenance(store: EngramStore) -> None:
    entry = store.add_attested(
        kind="fact",
        scope="global",
        statement="The value was verified by a tool.",
        source_type=SourceType.TOOL_VERIFIED,
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
        )


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
                "WHERE name IN ('entries_fts', 'entry_vectors', 'consolidation_plans')"
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
    assert schema_version[0] == 4
    assert schema_tables == {"entries_fts", "entry_vectors", "consolidation_plans"}


def test_migration_four_preserves_existing_version_three_entries(
    app_config: AppConfig,
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "version-three.db"
    config = replace(
        app_config,
        database=replace(app_config.database, path=database_path),
    )
    with EngramStore(config) as initial:
        entry = initial.add_attested(
            kind=EntryKind.FACT,
            scope="project/engram",
            statement="Migration four preserves existing entries.",
            source_type=SourceType.HUMAN,
        )
    connection = sqlite3.connect(database_path)
    try:
        connection.execute("DROP INDEX consolidation_plans_consumed_idx")
        connection.execute("DROP TABLE consolidation_plans")
        connection.execute("UPDATE schema_version SET version = 3")
        connection.commit()
    finally:
        connection.close()

    with EngramStore(config) as upgraded:
        preserved = upgraded.get_entry(entry.id)
        assert preserved is not None
        assert preserved.statement == entry.statement
        assert upgraded.get_consolidation_plan("01AAAAAAAAAAAAAAAAAAAAAAAA") is None

    connection = sqlite3.connect(database_path)
    try:
        version = connection.execute("SELECT version FROM schema_version").fetchone()
        plan_count = connection.execute("SELECT count(*) FROM consolidation_plans").fetchone()
    finally:
        connection.close()
    assert version == (4,)
    assert plan_count == (0,)


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
