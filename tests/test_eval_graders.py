# Copyright 2026 Julien Bombled
# SPDX-License-Identifier: Apache-2.0

"""Explicit pass and fail cases for every code grader."""

from engram.capsule import (
    CapsuleItem,
    CapsuleNotes,
    CapsuleResult,
    ConflictItem,
    estimate_capsule_bytes,
)
from engram.eval.graders import (
    grade_classification,
    grade_complement_policy,
    grade_conflict_policy,
    grade_gold_capsule,
    grade_poisoning,
    grade_supersession_policy,
)
from engram.eval.models import (
    CapsuleSection,
    ComplementTask,
    ConflictTask,
    ConsolidationClass,
    RecallSubset,
    RecallTask,
)
from engram.models import EntryKind


def _item(identifier: str, kind: EntryKind = EntryKind.FACT) -> CapsuleItem:
    return CapsuleItem(
        id=identifier,
        kind=kind,
        statement=f"Statement {identifier}.",
        confidence="high",
        source_type="human",
    )


def _capsule(
    *,
    current: list[CapsuleItem] | None = None,
    conflicts: list[ConflictItem] | None = None,
) -> CapsuleResult:
    return CapsuleResult(
        current=[] if current is None else current,
        conflicts=[] if conflicts is None else conflicts,
        notes=CapsuleNotes(scope_used="user", why_returned=[]),
    )


def test_gold_grader_has_manual_pass_and_fail_cases() -> None:
    task = RecallTask(
        "gold",
        "alpha",
        "seed",
        CapsuleSection.CURRENT,
        "user",
        RecallSubset.GLOBAL,
    )
    passing_capsule = _capsule(current=[_item("gold-id")])
    required_bytes = estimate_capsule_bytes(passing_capsule, "ok")
    passed = grade_gold_capsule(
        task,
        "gold-id",
        passing_capsule,
        "ok",
        required_bytes,
    )
    failed = grade_gold_capsule(task, "gold-id", _capsule(), "text above budget", 1)

    assert passed.passed is True
    assert passed.metrics["serialized_bytes"] == required_bytes
    assert passed.metrics["capsule_byte_budget"] == required_bytes
    assert failed.passed is False


def test_conflict_grader_has_manual_pass_and_fail_cases() -> None:
    task = ConflictTask("conflict", "theme", ("left", "right"), "user")
    expected = frozenset({"left-id", "right-id"})
    shown = _capsule(
        conflicts=[
            ConflictItem(
                claim_key="theme/current",
                subject_keys=["theme"],
                versions=[_item("left-id"), _item("right-id")],
            )
        ]
    )

    assert grade_conflict_policy(task, expected, _capsule(), shown).passed is True
    assert grade_conflict_policy(task, expected, _capsule(), _capsule()).passed is False


def test_complement_grader_has_manual_pass_and_fail_cases() -> None:
    task = ComplementTask("complement", "storage", ("fact", "decision"), "user")
    expected = frozenset({"fact-id", "decision-id"})
    current = [_item("fact-id"), _item("decision-id", EntryKind.DECISION)]

    assert grade_complement_policy(task, expected, _capsule(current=current)).passed is True
    assert grade_complement_policy(task, expected, _capsule(current=current[:1])).passed is False


def test_supersession_grader_has_manual_pass_and_fail_cases() -> None:
    passed = grade_supersession_policy("supersede", "old", "new", _capsule(current=[_item("new")]))
    failed = grade_supersession_policy(
        "supersede",
        "old",
        "new",
        _capsule(current=[_item("old")]),
    )

    assert passed.passed is True
    assert failed.passed is False


def test_classification_grader_has_manual_pass_and_fail_cases() -> None:
    passed = grade_classification("class", ConsolidationClass.NEW, ConsolidationClass.NEW)
    failed = grade_classification("class", ConsolidationClass.NEW, ConsolidationClass.UPDATE)

    assert passed.passed is True
    assert failed.passed is False


def test_poisoning_grader_has_manual_pass_and_fail_cases() -> None:
    assert grade_poisoning("poison", {"isolated": True}).passed is True
    assert grade_poisoning("poison", {"isolated": False}).passed is False
