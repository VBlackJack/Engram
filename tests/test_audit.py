# Copyright 2026 Julien Bombled
# SPDX-License-Identifier: Apache-2.0

"""Append-only transactional audit tests."""

from __future__ import annotations

import re

import pytest

from engram.models import AuditAction, Confidence, EntryKind, EntryStatus, SourceType
from engram.store import EngramStore


def test_model_confidence_is_capped_and_audited(store: EngramStore) -> None:
    entry = store.add_candidate(
        kind=EntryKind.FACT,
        scope="global",
        statement="A model requested high confidence.",
        writer_model="test-model",
        confidence=Confidence.HIGH,
    )

    assert entry.confidence is Confidence.MEDIUM
    assert [record.action for record in store.list_audit()] == [
        AuditAction.INSERT,
        AuditAction.CONFIDENCE_CAPPED,
    ]


def test_duplicate_insert_is_audited_noop(store: EngramStore) -> None:
    first = store.add_candidate(
        kind="decision",
        scope="project/engram",
        statement="Use one writer.",
        writer_model="model-a",
    )
    second = store.add_candidate(
        kind="decision",
        scope="PROJECT/ENGRAM",
        statement=" use   ONE writer. ",
        writer_model="model-b",
    )

    assert second == first
    assert store.count_entries() == 1
    assert [record.action for record in store.list_audit()] == [
        AuditAction.INSERT,
        AuditAction.IDEMPOTENT_NOOP,
    ]


def test_supersede_updates_both_entries_with_one_audit_event(store: EngramStore) -> None:
    old_entry = store.add_attested(
        kind="project_state",
        scope="project/engram",
        statement="Storage is pending.",
        source_type=SourceType.HUMAN,
    )
    new_entry = store.add_attested(
        kind="project_state",
        scope="project/engram",
        statement="Storage is implemented.",
        source_type=SourceType.HUMAN,
    )
    audit_before = len(store.list_audit())

    store.supersede(old_entry.id, new_entry.id)

    stored_old = store.get_entry(old_entry.id)
    stored_new = store.get_entry(new_entry.id)
    assert stored_old is not None
    assert stored_old.status is EntryStatus.SUPERSEDED
    assert stored_new is not None
    assert stored_new.supersedes == (old_entry.id,)
    assert len(store.list_audit()) == audit_before + 1
    assert store.list_audit()[-1].action is AuditAction.SUPERSEDE


def test_failed_mutation_leaves_no_orphan_audit(store: EngramStore) -> None:
    old_entry = store.add_candidate(
        kind="fact",
        scope="global",
        statement="Rollback must include audit.",
        writer_model="test-model",
    )
    audit_before = store.list_audit()

    with pytest.raises(KeyError, match="does not exist"):
        store.supersede(old_entry.id, "01AAAAAAAAAAAAAAAAAAAAAAAA")

    stored_old = store.get_entry(old_entry.id)
    assert stored_old is not None
    assert stored_old.status is EntryStatus.QUARANTINED
    assert store.list_audit() == audit_before


def test_audit_contains_hashes_but_not_payload_content(store: EngramStore) -> None:
    payload_statement = "Payload content must never enter the audit table."
    store.add_candidate(
        kind="fact",
        scope="global",
        statement=payload_statement,
        writer_model="test-model",
    )

    record = store.list_audit()[0]
    assert record.detail_hash is not None
    assert re.fullmatch(r"[0-9a-f]{64}", record.detail_hash)
    assert payload_statement not in repr(record)
