# Copyright 2026 Julien Bombled
# SPDX-License-Identifier: Apache-2.0

"""Append-only transactional audit tests."""

from __future__ import annotations

import re
from datetime import timedelta

import pytest

from engram.models import (
    AuditAction,
    Confidence,
    EntryKind,
    EntryStatus,
    Evidence,
    EvidenceType,
    RememberOutcome,
    SourceType,
)
from engram.store import EngramStore, StoreValidationError
from tests.conftest import MutableClock


@pytest.mark.parametrize(
    "actor",
    [
        "operator\nforged",
        "operator\x1b[31m",
        "operator\u2028forged",
        "operator\u2029forged",
        "operator\u202eforged",
    ],
)
def test_trusted_mutation_rejects_control_bearing_actor(
    store: EngramStore,
    actor: str,
) -> None:
    with pytest.raises(StoreValidationError, match="control characters"):
        store.add_attested(
            kind="fact",
            scope="user",
            statement="Audit lines cannot be forged.",
            source_type=SourceType.HUMAN,
            claim_key="audit/control-actor",
            actor=actor,
        )

    assert store.count_entries() == 0
    assert store.list_audit() == ()


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


def test_same_content_from_other_writer_is_an_isolated_corroboration(
    store: EngramStore,
) -> None:
    first = store.add_candidate(
        kind="decision",
        scope="project/engram",
        statement="Use one writer.",
        writer_model="model-a",
    )
    second_outcome = store.add_candidate(
        kind="decision",
        scope="PROJECT/ENGRAM",
        statement=" use   ONE writer. ",
        writer_model="model-b",
        include_outcome=True,
    )

    second = second_outcome.entry
    assert second.id != first.id
    assert second.canonical_key == first.canonical_key
    assert second.idempotency_key != first.idempotency_key
    assert second.writer_model == "model-b"
    assert second_outcome.outcome is RememberOutcome.CORROBORATED
    assert second_outcome.idempotent is False
    assert store.count_entries() == 2
    assert [record.action for record in store.list_audit()] == [
        AuditAction.INSERT,
        AuditAction.INSERT,
    ]


def test_same_writer_retry_and_new_evidence_are_distinguished(
    store: EngramStore,
) -> None:
    first = store.add_candidate(
        kind="fact",
        scope="user",
        statement="The endpoint is reachable.",
        writer_model="model-a",
        include_outcome=True,
    )
    retry = store.add_candidate(
        kind="fact",
        scope="user",
        statement="The endpoint is reachable.",
        writer_model="model-a",
        include_outcome=True,
    )
    corroborated = store.add_candidate(
        kind="fact",
        scope="user",
        statement="The endpoint is reachable.",
        writer_model="model-a",
        include_outcome=True,
        evidence=(Evidence(type=EvidenceType.TOOL_RESULT, ref="tool://probe/2"),),
    )

    assert first.entry.id == retry.entry.id == corroborated.entry.id
    assert first.outcome is RememberOutcome.CREATED
    assert retry.outcome is RememberOutcome.RETRY
    assert retry.idempotent is True
    assert corroborated.outcome is RememberOutcome.CORROBORATED
    assert corroborated.idempotent is False
    assert len(store.list_observations(first.entry.id)) == 2


def test_observation_metadata_permutations_are_one_retry(
    store: EngramStore,
) -> None:
    first = store.add_candidate(
        kind="fact",
        scope="user",
        statement="The endpoint metadata is stable.",
        writer_model="model-a",
        subject_keys=("endpoint", "runtime"),
        evidence=(
            Evidence(type=EvidenceType.TOOL_RESULT, ref="tool://probe/1"),
            Evidence(type=EvidenceType.REVIEW, ref="review://operator/1"),
        ),
        include_outcome=True,
    )
    retry = store.add_candidate(
        kind="fact",
        scope="user",
        statement="The endpoint metadata is stable.",
        writer_model="model-a",
        subject_keys=("runtime", "endpoint"),
        evidence=(
            Evidence(type=EvidenceType.REVIEW, ref="review://operator/1"),
            Evidence(type=EvidenceType.TOOL_RESULT, ref="tool://probe/1"),
        ),
        include_outcome=True,
    )

    assert retry.entry.id == first.entry.id
    assert retry.outcome is RememberOutcome.RETRY
    assert retry.idempotent is True
    assert len(store.list_observations(first.entry.id)) == 1


def test_terminal_content_creates_a_new_generation(
    store: EngramStore,
    clock: MutableClock,
) -> None:
    first = store.add_candidate(
        kind="episode",
        scope="session/restart",
        statement="One short-lived observation.",
        writer_model="model-a",
        include_outcome=True,
    )
    assert first.entry.expires_at is not None
    clock.current = first.entry.expires_at

    renewed = store.add_candidate(
        kind="episode",
        scope="session/restart",
        statement="One short-lived observation.",
        writer_model="model-a",
        include_outcome=True,
    )

    assert renewed.outcome is RememberOutcome.RENEWED
    assert renewed.entry.id != first.entry.id
    expired = store.get_entry(first.entry.id)
    assert expired is not None
    assert expired.status is EntryStatus.EXPIRED


def test_trusted_content_is_returned_without_unconfirmed_duplicate(
    store: EngramStore,
) -> None:
    trusted = store.add_attested(
        kind="fact",
        scope="user",
        statement="The endpoint is verified.",
        source_type=SourceType.TOOL_VERIFIED,
        claim_key="endpoint/verified",
    )

    outcome = store.add_candidate(
        kind="fact",
        scope="user",
        statement="The endpoint is verified.",
        writer_model="model-a",
        include_outcome=True,
    )

    assert outcome.entry.id == trusted.id
    assert outcome.outcome is RememberOutcome.EXISTING_TRUSTED
    assert store.count_entries() == 1
    assert len(store.list_observations(trusted.id)) == 1


def test_elapsed_business_validity_expires_then_renews_both_write_paths(
    store: EngramStore,
    clock: MutableClock,
) -> None:
    candidate_target = store.add_attested(
        kind="fact",
        scope="user",
        statement="The candidate endpoint generation is current.",
        source_type=SourceType.HUMAN,
        claim_key="endpoint/candidate-generation",
        valid_until=clock.current.date(),
    )
    attested_target = store.add_attested(
        kind="fact",
        scope="user",
        statement="The attested endpoint generation is current.",
        source_type=SourceType.HUMAN,
        claim_key="endpoint/attested-generation",
        valid_until=clock.current.date(),
    )
    clock.current += timedelta(days=1)

    candidate_renewal = store.add_candidate(
        kind="fact",
        scope="user",
        statement="The candidate endpoint generation is current.",
        writer_model="model-a",
        include_outcome=True,
    )
    attested_renewal = store.add_attested(
        kind="fact",
        scope="user",
        statement="The attested endpoint generation is current.",
        source_type=SourceType.HUMAN,
        claim_key="endpoint/attested-generation",
    )

    stored_candidate_target = store.get_entry(candidate_target.id)
    stored_attested_target = store.get_entry(attested_target.id)
    assert stored_candidate_target is not None
    assert stored_candidate_target.status is EntryStatus.EXPIRED
    assert stored_attested_target is not None
    assert stored_attested_target.status is EntryStatus.EXPIRED
    assert candidate_renewal.entry.id != candidate_target.id
    assert candidate_renewal.outcome is RememberOutcome.RENEWED
    assert attested_renewal.id != attested_target.id
    assert sum(record.action is AuditAction.EXPIRE for record in store.list_audit()) == 2


def test_stale_trusted_content_rejects_both_write_paths(
    store: EngramStore,
) -> None:
    candidate_target = store.add_attested(
        kind="fact",
        scope="user",
        statement="The candidate-facing endpoint is fresh.",
        source_type=SourceType.HUMAN,
        claim_key="endpoint/candidate-freshness",
    )
    attested_target = store.add_attested(
        kind="fact",
        scope="user",
        statement="The attested endpoint is fresh.",
        source_type=SourceType.HUMAN,
        claim_key="endpoint/attested-freshness",
    )
    for entry, suffix in (
        (candidate_target, "candidate"),
        (attested_target, "attested"),
    ):
        store.mark_promoted(
            entry.id,
            datacron_ref=f"datacron://{suffix}",
            datacron_hash=("a" * 64 if suffix == "candidate" else "b" * 64),
        )
        store.set_stale(entry.id, stale=True)
    audit_before = store.list_audit()

    with pytest.raises(StoreValidationError, match="not currently recallable"):
        store.add_candidate(
            kind="fact",
            scope="user",
            statement="The candidate-facing endpoint is fresh.",
            writer_model="model-a",
        )
    with pytest.raises(StoreValidationError, match="not currently recallable"):
        store.add_attested(
            kind="fact",
            scope="user",
            statement="The attested endpoint is fresh.",
            source_type=SourceType.HUMAN,
            claim_key="endpoint/attested-freshness",
        )

    assert store.list_audit() == audit_before
    assert store.count_entries() == 2


def test_future_trusted_content_rejects_both_write_paths(
    store: EngramStore,
    clock: MutableClock,
) -> None:
    future_date = (clock.current + timedelta(days=1)).date()
    store.add_attested(
        kind="fact",
        scope="user",
        statement="The candidate-facing endpoint becomes valid tomorrow.",
        source_type=SourceType.HUMAN,
        claim_key="endpoint/candidate-schedule",
        valid_from=future_date,
    )
    store.add_attested(
        kind="fact",
        scope="user",
        statement="The attested endpoint becomes valid tomorrow.",
        source_type=SourceType.HUMAN,
        claim_key="endpoint/attested-schedule",
        valid_from=future_date,
    )
    audit_before = store.list_audit()

    with pytest.raises(StoreValidationError, match="not currently recallable"):
        store.add_candidate(
            kind="fact",
            scope="user",
            statement="The candidate-facing endpoint becomes valid tomorrow.",
            writer_model="model-a",
        )
    with pytest.raises(StoreValidationError, match="not currently recallable"):
        store.add_attested(
            kind="fact",
            scope="user",
            statement="The attested endpoint becomes valid tomorrow.",
            source_type=SourceType.HUMAN,
            claim_key="endpoint/attested-schedule",
        )

    assert store.list_audit() == audit_before
    assert store.count_entries() == 2


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

    with pytest.raises(StoreValidationError, match="does not exist"):
        store.supersede(old_entry.id, "01AAAAAAAAAAAAAAAAAAAAAAAA")

    stored_old = store.get_entry(old_entry.id)
    assert stored_old is not None
    assert stored_old.status is EntryStatus.QUARANTINED
    assert store.list_audit() == audit_before


def test_supersede_rejects_untrusted_or_incompatible_replacements(
    store: EngramStore,
) -> None:
    old = store.add_attested(
        kind="project_state",
        scope="project/engram",
        statement="Lifecycle hardening is pending.",
        source_type=SourceType.HUMAN,
    )
    candidate = store.add_candidate(
        kind="project_state",
        scope="project/engram",
        statement="Lifecycle hardening might be complete.",
        writer_model="model-a",
    )
    other_scope = store.add_attested(
        kind="project_state",
        scope="project/other",
        statement="Other project is complete.",
        source_type=SourceType.HUMAN,
    )
    audit_before = store.list_audit()

    with pytest.raises(StoreValidationError, match="classified trusted"):
        store.supersede(old.id, candidate.id)
    with pytest.raises(StoreValidationError, match="kind and scope"):
        store.supersede(old.id, other_scope.id)

    stored_old = store.get_entry(old.id)
    assert stored_old is not None
    assert stored_old.status is EntryStatus.ACTIVE
    assert store.list_audit() == audit_before


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("stale", "Superseded entry is stale"),
        ("ttl", "Superseded entry is TTL-expired"),
        ("business", "Superseded entry is outside its business validity window"),
    ],
)
def test_supersede_rejects_nonrecallable_old_generation_with_precise_error(
    store: EngramStore,
    clock: MutableClock,
    mode: str,
    expected: str,
) -> None:
    kind = "project_state" if mode == "ttl" else "fact"
    claim_key = None if mode == "ttl" else f"lifecycle/{mode}"
    old = store.add_attested(
        kind=kind,
        scope="project/engram",
        statement=f"The {mode} generation is old.",
        source_type=SourceType.HUMAN,
        claim_key=claim_key,
        valid_until=clock.current.date() if mode == "business" else None,
    )
    if mode == "ttl":
        clock.current += timedelta(days=31)
    replacement = store.add_attested(
        kind=kind,
        scope="project/engram",
        statement=f"The {mode} generation is new.",
        source_type=SourceType.HUMAN,
        claim_key=claim_key,
    )
    if mode == "stale":
        store.mark_promoted(
            old.id,
            datacron_ref="datacron://lifecycle/stale",
            datacron_hash="c" * 64,
        )
        store.set_stale(old.id, stale=True)
    elif mode == "business":
        clock.current += timedelta(days=1)
    audit_before = store.list_audit()

    with pytest.raises(StoreValidationError, match=expected):
        store.supersede(old.id, replacement.id)

    stored_old = store.get_entry(old.id)
    assert stored_old is not None
    assert stored_old.status is EntryStatus.ACTIVE
    assert store.list_audit() == audit_before


def test_attest_with_multiple_supersedes_rolls_back_as_one_transaction(
    store: EngramStore,
) -> None:
    old = store.add_attested(
        kind="fact",
        scope="user",
        statement="The rollout is pending.",
        source_type=SourceType.HUMAN,
        claim_key="rollout/state",
    )
    count_before = store.count_entries()
    audit_before = store.list_audit()

    with pytest.raises(StoreValidationError, match="does not exist"):
        store.add_attested(
            kind="fact",
            scope="user",
            statement="The rollout is complete.",
            source_type=SourceType.HUMAN,
            claim_key="rollout/state",
            supersedes=(old.id, "01AAAAAAAAAAAAAAAAAAAAAAAA"),
        )

    stored_old = store.get_entry(old.id)
    assert stored_old is not None
    assert stored_old.status is EntryStatus.ACTIVE
    assert store.count_entries() == count_before
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
