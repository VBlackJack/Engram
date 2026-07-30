# Copyright 2026 Julien Bombled
# SPDX-License-Identifier: Apache-2.0

"""Hard resource-bound tests for structured and fallback recall capsules."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from mcp.types import CallToolResult, TextContent

from engram.capsule import (
    CALL_RESULT_BYTE_MARGIN,
    CAPSULE_BUDGET_OVERFLOW_NOTICE,
    CapsuleBuilder,
    CapsuleResult,
    estimate_capsule_bytes,
    estimate_payload_bytes,
)
from engram.config import CapsuleConfig
from engram.models import (
    Confidence,
    Entry,
    EntryKind,
    EntryStatus,
    PromotionState,
    SourceType,
)
from engram.retrieval import RetrievalResult

NOW = datetime(2026, 7, 30, 10, 0, tzinfo=UTC)


def test_oversized_scope_is_safely_represented_under_combined_budget() -> None:
    scope = "project/" + ('😀"\\\n' * 1200)
    capsule, text = CapsuleBuilder(CapsuleConfig()).build(
        RetrievalResult(matches=(), next_actions=(), own_pending=()),
        scope=scope,
        include_conflicts=True,
        token_budget=1200,
    )
    structured = capsule.model_dump_json()

    assert len(scope) > 4000
    assert capsule.notes.scope_used is not None
    assert capsule.notes.scope_used.startswith("<scope#")
    assert "\n" not in capsule.notes.scope_used
    assert scope not in text
    assert scope not in structured
    component_bytes = estimate_payload_bytes(text) + estimate_payload_bytes(structured)
    assert estimate_capsule_bytes(capsule, text) > component_bytes
    tool_result = CallToolResult(
        content=[TextContent(type="text", text=text)],
        structuredContent=capsule.model_dump(mode="json"),
    )
    assert estimate_capsule_bytes(capsule, text) == (
        estimate_payload_bytes(tool_result.model_dump_json()) + CALL_RESULT_BYTE_MARGIN
    )
    assert estimate_capsule_bytes(capsule, text) <= 1200
    _assert_source_invariant(capsule)


def test_low_priority_items_are_dropped_whole_and_deterministically() -> None:
    kept_statement = "Keep this high-priority fact exactly."
    oversized_statement = "oversized-statement " * 250
    high = _entry(
        "high",
        EntryKind.FACT,
        kept_statement,
        claim_key="fact/high",
    )
    low = _entry(
        "low",
        EntryKind.FACT,
        oversized_statement,
        claim_key="fact/low",
    )
    next_action = _entry(
        "next",
        EntryKind.PROJECT_STATE,
        "Ship the verified release next.",
    )
    relevant = _entry(
        "episode",
        EntryKind.EPISODE,
        "A relevant prior deployment succeeded.",
    )
    pending = _entry(
        "pending",
        EntryKind.FACT,
        "An unconfirmed candidate is pending.",
        status=EntryStatus.QUARANTINED,
    )
    retrieval = RetrievalResult(
        matches=(high, low, relevant),
        next_actions=(next_action,),
        own_pending=(pending,),
    )
    builder = CapsuleBuilder(CapsuleConfig())

    first, first_text = builder.build(
        retrieval,
        scope=None,
        include_conflicts=True,
        token_budget=1200,
    )
    second, second_text = builder.build(
        retrieval,
        scope=None,
        include_conflicts=True,
        token_budget=1200,
    )

    assert first.model_dump() == second.model_dump()
    assert first_text == second_text
    assert [item.id for item in first.current] == ["high"]
    assert first.current[0].statement == kept_statement
    assert first.next_action == []
    assert first.relevant == []
    assert first.own_pending == []
    assert first.sources == ["high"]
    assert "4 entries omitted, budget" in first.notes.why_returned
    assert first.notes.recall_complete is False
    assert first.notes.warnings == [CAPSULE_BUDGET_OVERFLOW_NOTICE]
    assert oversized_statement[:100] not in first_text
    assert oversized_statement[:100] not in first.model_dump_json()
    assert estimate_capsule_bytes(first, first_text) <= 1200
    _assert_source_invariant(first)


def test_oversized_conflict_metadata_cannot_leave_dangling_sources() -> None:
    claim_key = "claim/" + ("x" * 5000)
    subject_key = "subject/" + ("z" * 5000)
    alpha = _entry(
        "alpha",
        EntryKind.FACT,
        "Statement alpha remains complete.",
        claim_key=claim_key,
        subject_keys=(subject_key,),
        canonical_key="alpha-generation",
    )
    beta = _entry(
        "beta",
        EntryKind.FACT,
        "Statement beta remains complete.",
        claim_key=claim_key,
        subject_keys=(subject_key,),
        canonical_key="beta-generation",
    )

    capsule, text = CapsuleBuilder(CapsuleConfig()).build(
        RetrievalResult(matches=(alpha, beta), next_actions=(), own_pending=()),
        scope=None,
        include_conflicts=True,
        token_budget=1200,
    )

    assert capsule.conflicts == []
    assert capsule.sources == []
    assert "2 entries omitted, budget" in capsule.notes.why_returned
    assert capsule.notes.recall_complete is False
    assert capsule.notes.warnings == [CAPSULE_BUDGET_OVERFLOW_NOTICE]
    assert alpha.statement[:20] not in text
    assert beta.statement[:20] not in text
    assert claim_key not in capsule.model_dump_json()
    assert subject_key not in capsule.model_dump_json()
    assert estimate_capsule_bytes(capsule, text) <= 1200
    _assert_source_invariant(capsule)


def test_requested_conflict_survives_lower_priority_context_under_budget() -> None:
    alpha = _entry(
        "conflict-alpha",
        EntryKind.FACT,
        "Retention is thirty days.",
        claim_key="retention/days",
        canonical_key="retention-thirty",
    )
    beta = _entry(
        "conflict-beta",
        EntryKind.FACT,
        "Retention is ninety days.",
        claim_key="retention/days",
        canonical_key="retention-ninety",
    )
    conflict_only, conflict_text = CapsuleBuilder(CapsuleConfig()).build(
        RetrievalResult(matches=(alpha, beta), next_actions=(), own_pending=()),
        scope=None,
        include_conflicts=True,
        token_budget=4800,
    )
    budget = estimate_capsule_bytes(conflict_only, conflict_text) + 600
    fillers = tuple(
        _entry(
            f"filler-{index}",
            EntryKind.FACT,
            f"Lower-priority context {index}. " + ("x" * 1200),
            claim_key=f"context/{index}",
        )
        for index in range(3)
    )

    capsule, text = CapsuleBuilder(CapsuleConfig()).build(
        RetrievalResult(
            matches=(*fillers, alpha, beta),
            next_actions=(),
            own_pending=(),
        ),
        scope=None,
        include_conflicts=True,
        token_budget=budget,
    )

    assert capsule.current == []
    assert len(capsule.conflicts) == 1
    assert {item.id for item in capsule.conflicts[0].versions} == {alpha.id, beta.id}
    assert capsule.notes.warnings == [CAPSULE_BUDGET_OVERFLOW_NOTICE]
    assert estimate_capsule_bytes(capsule, text) <= budget
    _assert_source_invariant(capsule)


def test_budget_below_mandatory_envelope_fails_closed() -> None:
    builder = CapsuleBuilder(CapsuleConfig())

    with pytest.raises(ValueError, match="too small for mandatory capsule metadata"):
        builder.build(
            RetrievalResult(matches=(), next_actions=(), own_pending=()),
            scope=None,
            include_conflicts=False,
            token_budget=70,
        )


def test_utf8_byte_estimate_is_a_one_byte_per_token_upper_bound() -> None:
    payload = "".join(chr(33 + ((index * 47) % 90)) for index in range(800))

    assert estimate_payload_bytes(payload) == len(payload.encode("utf-8"))


def test_minimum_budget_preserves_all_internal_incompleteness_notices() -> None:
    notices = (
        "conflict_family_overflow",
        "project_state_overflow",
        "hybrid_vector_coverage_incomplete",
        "unclassified_claim_omitted",
        "conflicts_hidden_by_request",
    )
    oversized = _entry(
        "oversized",
        EntryKind.FACT,
        "complete payload " * 300,
        claim_key="budget/overflow",
    )

    capsule, text = CapsuleBuilder(CapsuleConfig()).build(
        RetrievalResult(
            matches=(oversized,),
            next_actions=(),
            own_pending=(),
            notices=notices,
        ),
        scope="project/" + ("x" * 32),
        include_conflicts=True,
        token_budget=1200,
    )

    assert capsule.notes.recall_complete is False
    assert capsule.notes.warnings == [*notices, CAPSULE_BUDGET_OVERFLOW_NOTICE]
    assert all(notice in text for notice in capsule.notes.warnings)
    assert estimate_capsule_bytes(capsule, text) <= 1200


def _entry(  # noqa: PLR0913
    identifier: str,
    kind: EntryKind,
    statement: str,
    *,
    status: EntryStatus = EntryStatus.ACTIVE,
    claim_key: str | None = None,
    subject_keys: tuple[str, ...] = (),
    canonical_key: str | None = None,
) -> Entry:
    active = status is EntryStatus.ACTIVE
    return Entry(
        id=identifier,
        kind=kind,
        scope="user",
        statement=statement,
        subject_keys=subject_keys,
        status=status,
        promotion_state=PromotionState.APPROVED if active else PromotionState.CANDIDATE,
        source_type=SourceType.HUMAN if active else SourceType.MODEL_INFERRED,
        writer_model=None if active else "capsule-budget-test/1.0",
        confidence=Confidence.HIGH if active else Confidence.LOW,
        observed_at=NOW,
        recorded_at=NOW,
        valid_from=None,
        valid_until=None,
        expires_at=None,
        idempotency_key=f"key-{identifier}",
        supersedes=(),
        evidence=(),
        stale=False,
        datacron_ref=None,
        datacron_hash=None,
        synced_at=None,
        canonical_key=canonical_key or f"canonical-{identifier}",
        claim_key=claim_key,
    )


def _assert_source_invariant(capsule: CapsuleResult) -> None:
    visible = {
        item.id
        for section in (
            capsule.current,
            capsule.next_action,
            capsule.relevant,
            capsule.own_pending,
        )
        for item in section
    }
    visible.update(version.id for conflict in capsule.conflicts for version in conflict.versions)
    assert set(capsule.sources) == visible
