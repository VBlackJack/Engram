# Copyright 2026 Julien Bombled
# SPDX-License-Identifier: Apache-2.0

"""Entry invariants, migrations, and concurrent writer tests."""

from __future__ import annotations

import inspect
import sqlite3
from concurrent.futures import ThreadPoolExecutor

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
        derived_tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE name IN ('entries_fts', 'entry_vectors')"
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
    assert schema_version[0] == 3
    assert derived_tables == {"entries_fts", "entry_vectors"}


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
