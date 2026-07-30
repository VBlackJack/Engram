# Copyright 2026 Julien Bombled
# SPDX-License-Identifier: Apache-2.0

"""Contractual release gate for the deterministic FTS baseline."""

from __future__ import annotations

from dataclasses import fields
from enum import StrEnum

from engram.capsule import (
    CALL_RESULT_BYTE_MARGIN,
    CAPSULE_BUDGET_UNIT,
    CAPSULE_ESTIMATOR_VERSION,
)
from engram.config import RetrievalConfig, RetrievalMode
from engram.retrieval import NOTICE_FTS_QUERY_TIMEOUT

from .models import (
    FtsContractMetrics,
    FtsContractThresholds,
    ModeMetrics,
    ModeStatus,
)

FTS_CONTRACT_CAPSULE_BYTE_BUDGET = 4800
FTS_CONTRACT_RETRIEVAL_CONFIG = RetrievalConfig(
    mode=RetrievalMode.FTS,
    embeddings_endpoint="http://127.0.0.1:1234/v1/embeddings",
    embeddings_model="",
    embeddings_timeout_ms=3000,
    rrf_k=60,
    fts_top_k=64,
    fts_max_query_chars=1024,
    fts_max_query_terms=24,
    fts_min_prefix_chars=4,
    fts_query_timeout_ms=250,
    hybrid_max_candidates=4096,
)
FTS_CONTRACT_THRESHOLDS = FtsContractThresholds(
    minimum_global_gold_in_capsule_rate=0.98,
    minimum_lexical_degraded_gold_in_capsule_rate=0.90,
    minimum_budget_pass_rate=1.0,
    minimum_global_complete_recall_rate=0.85,
    minimum_lexical_complete_recall_rate=0.75,
    minimum_contradiction_pass_rate=1.0,
    minimum_poisoning_pass_rate=1.0,
    maximum_recall_p95_ms=100.0,
)


def fts_contract_retrieval_snapshot() -> dict[str, str | int]:
    """Return every versioned RetrievalConfig field in JSON-canonical scalar form."""
    snapshot: dict[str, str | int] = {}
    for field in fields(FTS_CONTRACT_RETRIEVAL_CONFIG):
        value = getattr(FTS_CONTRACT_RETRIEVAL_CONFIG, field.name)
        if isinstance(value, StrEnum):
            snapshot[field.name] = value.value
        elif isinstance(value, (str, int)):
            snapshot[field.name] = value
        else:
            raise TypeError(f"Unsupported retrieval contract field: {field.name}")
    return snapshot


class EvaluationGateError(RuntimeError):
    """Raised after artifacts record a failed FTS release contract."""

    def __init__(self, contract: FtsContractMetrics) -> None:
        """Retain the failed measurements and expose a concise CLI message."""
        self.contract = contract
        failed_checks = ", ".join(name for name, passed in contract.checks.items() if not passed)
        super().__init__(f"FTS evaluation contract failed: {failed_checks}")


def evaluate_fts_contract(
    mode: ModeMetrics,
    thresholds: FtsContractThresholds | None = None,
) -> FtsContractMetrics:
    """Compare measured FTS recall against the checked-in release thresholds."""
    active_thresholds = FTS_CONTRACT_THRESHOLDS if thresholds is None else thresholds
    if (
        mode.status is not ModeStatus.MEASURED
        or mode.f1_recall is None
        or mode.f2_contradiction is None
        or mode.f4_poisoning is None
        or mode.f5_system is None
    ):
        raise ValueError("FTS contract requires measured recall, safety, and system metrics")

    recall = mode.f1_recall
    contradiction = mode.f2_contradiction
    poisoning = mode.f4_poisoning
    recall_p95_ms = mode.f5_system.recall_latency.p95_ms
    checks = {
        "global_gold_in_capsule_rate": (
            recall.global_gold_in_capsule_rate
            >= active_thresholds.minimum_global_gold_in_capsule_rate
        ),
        "lexical_degraded_gold_in_capsule_rate": (
            recall.fts_contract_gold_in_capsule_rate
            >= active_thresholds.minimum_lexical_degraded_gold_in_capsule_rate
        ),
        "budget_pass_rate": (recall.budget_pass_rate >= active_thresholds.minimum_budget_pass_rate),
        "global_complete_recall_rate": (
            recall.global_complete_recall_rate
            >= active_thresholds.minimum_global_complete_recall_rate
        ),
        "lexical_complete_recall_rate": (
            recall.fts_contract_complete_recall_rate
            >= active_thresholds.minimum_lexical_complete_recall_rate
        ),
        "contradiction_pass_rate": (
            contradiction.pass_rate >= active_thresholds.minimum_contradiction_pass_rate
        ),
        "poisoning_pass_rate": (
            poisoning.pass_rate >= active_thresholds.minimum_poisoning_pass_rate
        ),
        "recall_p95_ms": recall_p95_ms <= active_thresholds.maximum_recall_p95_ms,
        "fts_query_timeout_absent": (recall.warning_counts.get(NOTICE_FTS_QUERY_TIMEOUT, 0) == 0),
    }
    return FtsContractMetrics(
        passed=all(checks.values()),
        checks=checks,
        thresholds=active_thresholds,
        capsule_byte_budget=FTS_CONTRACT_CAPSULE_BYTE_BUDGET,
        capsule_budget_unit=CAPSULE_BUDGET_UNIT,
        capsule_estimator_version=CAPSULE_ESTIMATOR_VERSION,
        call_result_byte_margin=CALL_RESULT_BYTE_MARGIN,
        retrieval_config=fts_contract_retrieval_snapshot(),
        global_gold_in_capsule_rate=recall.global_gold_in_capsule_rate,
        lexical_degraded_gold_in_capsule_rate=(recall.fts_contract_gold_in_capsule_rate),
        budget_pass_rate=recall.budget_pass_rate,
        global_complete_recall_rate=recall.global_complete_recall_rate,
        lexical_complete_recall_rate=recall.fts_contract_complete_recall_rate,
        contradiction_pass_rate=contradiction.pass_rate,
        poisoning_pass_rate=poisoning.pass_rate,
        warning_counts=recall.fts_contract_warning_counts,
        global_warning_counts=recall.warning_counts,
        recall_p95_ms=recall_p95_ms,
    )
