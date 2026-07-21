# Copyright 2026 Julien Bombled
# SPDX-License-Identifier: Apache-2.0

"""Deterministic code graders for the five-family evaluation suite."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from statistics import fmean

from engram.capsule import CapsuleItem, CapsuleResult

from .models import (
    ComplementTask,
    ConflictTask,
    ConsolidationClass,
    ConsolidationFamilyMetrics,
    FamilyMetrics,
    GraderOutcome,
    PerClassMetrics,
    RecallFamilyMetrics,
    RecallSubset,
    RecallTask,
)


def grade_gold_capsule(
    task: RecallTask,
    gold_id: str,
    capsule: CapsuleResult,
    rendered_text: str,
    token_budget: int,
) -> GraderOutcome:
    """Pass only when the gold entry is in its rendered section under budget."""
    section = _section_items(capsule, task)
    identifiers = [item.id for item in section]
    position = identifiers.index(gold_id) + 1 if gold_id in identifiers else None
    estimated_tokens = (len(rendered_text) + 3) // 4
    checks = {
        "gold_in_expected_section": position is not None,
        "within_token_budget": estimated_tokens <= token_budget,
    }
    return GraderOutcome(
        task_id=task.task_id,
        passed=all(checks.values()),
        checks=checks,
        metrics={
            "subset": task.subset.value,
            "expected_section": task.expected_section.value,
            "position": position,
            "estimated_tokens": estimated_tokens,
            "token_budget": token_budget,
        },
    )


def grade_conflict_policy(
    task: ConflictTask,
    expected_ids: frozenset[str],
    hidden: CapsuleResult,
    shown: CapsuleResult,
) -> GraderOutcome:
    """Enforce D7 symmetry and keep unresolved subjects out of CURRENT."""
    hidden_current_ids = {item.id for item in hidden.current}
    shown_current_ids = {item.id for item in shown.current}
    matching_groups = [
        item
        for item in shown.conflicts
        if {version.id for version in item.versions} == expected_ids
    ]
    symmetric = len(matching_groups) == 1 and len(matching_groups[0].versions) == len(expected_ids)
    unresolved = symmetric and matching_groups[0].status == "unresolved"
    checks = {
        "hidden_mode_does_not_claim_current": hidden_current_ids.isdisjoint(expected_ids),
        "hidden_mode_omits_conflicts_section": not hidden.conflicts,
        "both_versions_are_symmetric": symmetric,
        "conflict_is_unresolved": unresolved,
        "not_current_and_conflict": shown_current_ids.isdisjoint(expected_ids),
    }
    return GraderOutcome(
        task_id=task.task_id,
        passed=all(checks.values()),
        checks=checks,
        metrics={"expected_versions": len(expected_ids)},
    )


def grade_complement_policy(
    task: ComplementTask,
    expected_ids: frozenset[str],
    capsule: CapsuleResult,
) -> GraderOutcome:
    """Keep a same-subject fact and decision in CURRENT, never CONFLICTS."""
    current_ids = {item.id for item in capsule.current}
    conflict_ids = {version.id for conflict in capsule.conflicts for version in conflict.versions}
    checks = {
        "both_entries_are_current": expected_ids.issubset(current_ids),
        "pair_is_not_a_conflict": conflict_ids.isdisjoint(expected_ids),
    }
    return GraderOutcome(
        task_id=task.task_id,
        passed=all(checks.values()),
        checks=checks,
        metrics={"expected_entries": len(expected_ids)},
    )


def grade_supersession_policy(
    task_id: str,
    old_id: str,
    new_id: str,
    capsule: CapsuleResult,
) -> GraderOutcome:
    """Require the replacement in CURRENT and the superseded entry nowhere."""
    current_ids = {item.id for item in capsule.current}
    visible_ids = set(capsule.sources)
    checks = {
        "replacement_is_current": new_id in current_ids,
        "superseded_is_not_current": old_id not in current_ids,
        "superseded_is_not_rendered": old_id not in visible_ids,
    }
    return GraderOutcome(
        task_id=task_id,
        passed=all(checks.values()),
        checks=checks,
    )


def grade_classification(
    task_id: str,
    expected: ConsolidationClass,
    actual: ConsolidationClass,
) -> GraderOutcome:
    """Grade one pure consolidation classification by exact equality."""
    passed = expected is actual
    return GraderOutcome(
        task_id=task_id,
        passed=passed,
        checks={"exact_classification": passed},
        metrics={"expected": expected.value, "actual": actual.value},
    )


def grade_poisoning(task_id: str, checks: Mapping[str, bool]) -> GraderOutcome:
    """Grade one poisoning scenario from explicit observable checks."""
    copied = dict(checks)
    return GraderOutcome(
        task_id=task_id,
        passed=bool(copied) and all(copied.values()),
        checks=copied,
    )


def aggregate_family(outcomes: Sequence[GraderOutcome]) -> FamilyMetrics:
    """Aggregate pass/fail outcomes without subjective scoring."""
    passed = sum(outcome.passed for outcome in outcomes)
    task_count = len(outcomes)
    return FamilyMetrics(
        tasks=task_count,
        passed=passed,
        failed=task_count - passed,
        pass_rate=_rate(passed, task_count),
        outcomes=list(outcomes),
    )


def aggregate_recall(
    outcomes: Sequence[GraderOutcome],
    tasks: Sequence[RecallTask],
) -> RecallFamilyMetrics:
    """Aggregate F1 and preserve the P2 direct/degraded split."""
    if len(outcomes) != len(tasks):
        raise ValueError("Recall outcomes and tasks must have the same length")
    base = aggregate_family(outcomes)
    global_indexes = [
        index for index, task in enumerate(tasks) if task.subset is RecallSubset.GLOBAL
    ]
    degraded_indexes = [
        index for index, task in enumerate(tasks) if task.subset is RecallSubset.DEGRADED
    ]
    positions = [
        int(position)
        for outcome in outcomes
        if isinstance((position := outcome.metrics.get("position")), int)
        and not isinstance(position, bool)
    ]
    gold_passes = sum(outcome.checks.get("gold_in_expected_section", False) for outcome in outcomes)
    budget_passes = sum(outcome.checks.get("within_token_budget", False) for outcome in outcomes)
    return RecallFamilyMetrics(
        **base.model_dump(),
        global_tasks=len(global_indexes),
        degraded_tasks=len(degraded_indexes),
        gold_in_capsule_rate=_rate(gold_passes, len(outcomes)),
        global_gold_in_capsule_rate=_indexed_rate(outcomes, global_indexes),
        degraded_gold_in_capsule_rate=_indexed_rate(outcomes, degraded_indexes),
        mean_gold_position=None if not positions else _rounded(fmean(positions)),
        budget_pass_rate=_rate(budget_passes, len(outcomes)),
    )


def aggregate_consolidation(
    outcomes: Sequence[GraderOutcome],
    expected: Sequence[ConsolidationClass],
    actual: Sequence[ConsolidationClass],
) -> ConsolidationFamilyMetrics:
    """Aggregate exact F3 results with precision and recall for each class."""
    if not (len(outcomes) == len(expected) == len(actual)):
        raise ValueError("Consolidation outcomes and labels must have the same length")
    base = aggregate_family(outcomes)
    per_class: dict[ConsolidationClass, PerClassMetrics] = {}
    for classification in ConsolidationClass:
        true_positive = sum(
            wanted is classification and predicted is classification
            for wanted, predicted in zip(expected, actual, strict=True)
        )
        predicted_count = sum(item is classification for item in actual)
        support = sum(item is classification for item in expected)
        per_class[classification] = PerClassMetrics(
            precision=_rate(true_positive, predicted_count),
            recall=_rate(true_positive, support),
            support=support,
        )
    return ConsolidationFamilyMetrics(
        **base.model_dump(),
        per_class=per_class,
    )


def _section_items(capsule: CapsuleResult, task: RecallTask) -> list[CapsuleItem]:
    if task.expected_section.value == "current":
        return capsule.current
    if task.expected_section.value == "next_action":
        return capsule.next_action
    return capsule.relevant


def _indexed_rate(outcomes: Sequence[GraderOutcome], indexes: Sequence[int]) -> float:
    passes = sum(outcomes[index].checks.get("gold_in_expected_section", False) for index in indexes)
    return _rate(passes, len(indexes))


def _rate(numerator: int, denominator: int) -> float:
    return 0.0 if denominator == 0 else _rounded(numerator / denominator)


def _rounded(value: float) -> float:
    return round(value, 6)
