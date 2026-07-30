# Copyright 2026 Julien Bombled
# SPDX-License-Identifier: Apache-2.0

"""Evaluation runner determinism and artifact tests."""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

import engram.cli as cli_module
import engram.eval.gate as gate_module
from engram.capsule import (
    CALL_RESULT_BYTE_MARGIN,
    CAPSULE_BUDGET_UNIT,
    CAPSULE_ESTIMATOR_VERSION,
)
from engram.config import AppConfig, CapsuleConfig, RetrievalConfig
from engram.embeddings import EmbeddingProvider
from engram.eval.gate import FTS_CONTRACT_CAPSULE_BYTE_BUDGET, EvaluationGateError
from engram.eval.models import (
    EvalMode,
    FtsContractThresholds,
    ModeStatus,
    P2Verdict,
)
from engram.eval.runner import METRICS_FILENAME, REPORT_FILENAME, run_evaluation
from engram.logging_setup import FileLogger


@dataclass(slots=True)
class DeterministicTimer:
    """Return equal elapsed measurements across independent test runs."""

    current: float = 0.0

    def __call__(self) -> float:
        self.current += 0.001
        return self.current


class ConstantEmbeddingProvider:
    """Simple provider that exercises the complete hybrid path without a server."""

    def embed(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        return tuple((1.0, 0.0) for _ in texts)


def _constant_provider(config: RetrievalConfig) -> EmbeddingProvider:
    del config
    return ConstantEmbeddingProvider()


def test_two_fts_runs_produce_identical_metrics(
    app_config: AppConfig,
    tmp_path: Path,
) -> None:
    first = run_evaluation(
        app_config,
        mode=EvalMode.FTS,
        output_directory=tmp_path / "first",
        timer=DeterministicTimer(),
    )
    second = run_evaluation(
        app_config,
        mode=EvalMode.FTS,
        output_directory=tmp_path / "second",
        timer=DeterministicTimer(),
    )

    assert first.model_dump() == second.model_dump()
    first_json = json.loads((tmp_path / "first" / METRICS_FILENAME).read_text(encoding="utf-8"))
    second_json = json.loads((tmp_path / "second" / METRICS_FILENAME).read_text(encoding="utf-8"))
    assert first_json == second_json


def test_fts_run_writes_all_families_and_unmeasured_verdict(
    app_config: AppConfig,
    tmp_path: Path,
) -> None:
    output = tmp_path / "artifacts"
    metrics = run_evaluation(
        app_config,
        mode=EvalMode.FTS,
        output_directory=output,
        timer=DeterministicTimer(),
    )

    fts = metrics.modes["fts"]
    assert fts.status is ModeStatus.MEASURED
    assert fts.f1_recall is not None
    assert fts.f1_recall.tasks == 88
    assert fts.f1_recall.global_tasks == 40
    assert fts.f1_recall.degraded_tasks == 24
    assert fts.f1_recall.fts_contract_tasks == 24
    assert fts.f1_recall.fts_contract_gold_in_capsule_rate >= 0.90
    assert (
        fts.f1_recall.degraded_gold_in_capsule_rate
        < fts.f1_recall.fts_contract_gold_in_capsule_rate
    )
    assert fts.f2_contradiction is not None
    assert fts.f2_contradiction.tasks == 5
    assert metrics.f3_consolidation.tasks == 8
    assert fts.f4_poisoning is not None
    assert fts.f4_poisoning.tasks == 4
    assert fts.f5_system is not None
    assert fts.f5_system.recall_latency.samples == 88
    assert metrics.fts_contract.passed is True
    assert metrics.fts_contract.thresholds.minimum_global_gold_in_capsule_rate == 0.98
    assert metrics.fts_contract.thresholds.minimum_lexical_degraded_gold_in_capsule_rate == 0.90
    assert metrics.fts_contract.thresholds.minimum_budget_pass_rate == 1.0
    assert metrics.fts_contract.thresholds.minimum_global_complete_recall_rate == 0.85
    assert metrics.fts_contract.thresholds.minimum_lexical_complete_recall_rate == 0.75
    assert metrics.fts_contract.thresholds.minimum_contradiction_pass_rate == 1.0
    assert metrics.fts_contract.thresholds.minimum_poisoning_pass_rate == 1.0
    assert metrics.fts_contract.thresholds.maximum_recall_p95_ms == 100.0
    assert metrics.fts_contract.capsule_byte_budget == FTS_CONTRACT_CAPSULE_BYTE_BUDGET
    assert metrics.fts_contract.capsule_budget_unit == CAPSULE_BUDGET_UNIT
    assert metrics.fts_contract.capsule_estimator_version == CAPSULE_ESTIMATOR_VERSION
    assert metrics.fts_contract.call_result_byte_margin == CALL_RESULT_BYTE_MARGIN
    assert metrics.fts_contract.retrieval_config == {
        "embeddings_endpoint": "http://127.0.0.1:1234/v1/embeddings",
        "embeddings_model": "",
        "embeddings_timeout_ms": 3000,
        "fts_max_query_chars": 1024,
        "fts_max_query_terms": 24,
        "fts_min_prefix_chars": 4,
        "fts_top_k": 64,
        "hybrid_max_candidates": 4096,
        "mode": "fts",
        "rrf_k": 60,
    }
    assert metrics.p2_verdict is P2Verdict.HYBRID_UNMEASURED
    assert (output / METRICS_FILENAME).is_file()
    payload = json.loads((output / METRICS_FILENAME).read_text(encoding="utf-8"))
    assert payload["schema_version"] == 3
    assert payload["corpus"]["semantic_benchmark_version"] == "om-04-v3"
    assert payload["corpus"]["fts_contract_version"] == "fts-r2-v3"
    assert payload["fts_contract"]["capsule_byte_budget"] == 4800
    report = (output / REPORT_FILENAME).read_text(encoding="utf-8")
    assert "## Verdict P2" in report
    assert "## Gate FTS" in report
    assert "Plafond capsule contractuel: 4800 octets UTF-8 conservateurs (" in report
    assert "Rappel global complet" in report
    assert "Warnings du contrat FTS:" in report
    assert "Semantique historique" in report
    assert all(f"## F{index}" in report for index in range(1, 6))


def test_both_mode_measures_hybrid_with_an_available_provider(
    app_config: AppConfig,
    tmp_path: Path,
) -> None:
    config = replace(
        app_config,
        retrieval=RetrievalConfig(embeddings_model="mock-embedding-model"),
    )

    metrics = run_evaluation(
        config,
        mode=EvalMode.BOTH,
        output_directory=tmp_path / "hybrid",
        timer=DeterministicTimer(),
        provider_factory=_constant_provider,
    )

    hybrid = metrics.modes["hybrid"]
    assert hybrid.status is ModeStatus.MEASURED
    assert hybrid.f5_system is not None
    assert hybrid.f5_system.vectors_indexed == 72
    assert metrics.p2_measures.hybrid_recall_p95_ms is not None


def test_failed_fts_contract_writes_artifacts_then_raises(
    app_config: AppConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    impossible = FtsContractThresholds(
        minimum_global_gold_in_capsule_rate=1.01,
        minimum_lexical_degraded_gold_in_capsule_rate=1.01,
        minimum_budget_pass_rate=1.01,
        minimum_global_complete_recall_rate=1.01,
        minimum_lexical_complete_recall_rate=1.01,
        minimum_contradiction_pass_rate=1.01,
        minimum_poisoning_pass_rate=1.01,
        maximum_recall_p95_ms=0.0,
    )
    monkeypatch.setattr(gate_module, "FTS_CONTRACT_THRESHOLDS", impossible)
    output = tmp_path / "failed-contract"

    with pytest.raises(EvaluationGateError) as error:
        run_evaluation(
            app_config,
            mode=EvalMode.FTS,
            output_directory=output,
            timer=DeterministicTimer(),
        )

    assert isinstance(error.value, RuntimeError)
    payload = json.loads((output / METRICS_FILENAME).read_text(encoding="utf-8"))
    assert payload["fts_contract"]["passed"] is False
    assert payload["fts_contract"]["checks"] == {
        "budget_pass_rate": False,
        "contradiction_pass_rate": False,
        "global_complete_recall_rate": False,
        "global_gold_in_capsule_rate": False,
        "lexical_complete_recall_rate": False,
        "lexical_degraded_gold_in_capsule_rate": False,
        "poisoning_pass_rate": False,
        "recall_p95_ms": False,
    }
    assert (output / REPORT_FILENAME).is_file()


def test_fts_contract_is_independent_from_runtime_retrieval_bounds(
    app_config: AppConfig,
    tmp_path: Path,
) -> None:
    constrained = replace(
        app_config,
        retrieval=replace(
            app_config.retrieval,
            fts_top_k=1,
            fts_max_query_terms=1,
        ),
    )
    output = tmp_path / "configured-bounds"

    metrics = run_evaluation(
        constrained,
        mode=EvalMode.FTS,
        output_directory=output,
        timer=DeterministicTimer(),
    )

    assert metrics.fts_contract.passed is True
    assert metrics.fts_contract.retrieval_config["fts_top_k"] == 64
    assert metrics.fts_contract.retrieval_config["fts_max_query_terms"] == 24
    payload = json.loads((output / METRICS_FILENAME).read_text(encoding="utf-8"))
    assert payload["fts_contract"]["retrieval_config"]["fts_top_k"] == 64


def test_fts_contract_budget_is_independent_from_runtime_default(
    app_config: AppConfig,
    tmp_path: Path,
) -> None:
    runtime_budget = replace(
        app_config,
        capsule=CapsuleConfig(
            default_token_budget=6000,
            min_token_budget=1200,
            max_token_budget=6000,
        ),
    )

    metrics = run_evaluation(
        runtime_budget,
        mode=EvalMode.FTS,
        output_directory=tmp_path / "fixed-budget",
        timer=DeterministicTimer(),
    )

    recall = metrics.modes["fts"].f1_recall
    assert recall is not None
    assert metrics.fts_contract.capsule_byte_budget == FTS_CONTRACT_CAPSULE_BYTE_BUDGET == 4800
    assert {outcome.metrics["capsule_byte_budget"] for outcome in recall.outcomes} == {4800}


def test_eval_command_exits_nonzero_when_fts_contract_fails(
    app_config: AppConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    impossible = FtsContractThresholds(
        minimum_global_gold_in_capsule_rate=1.01,
        minimum_lexical_degraded_gold_in_capsule_rate=1.01,
        minimum_budget_pass_rate=1.01,
        minimum_global_complete_recall_rate=1.01,
        minimum_lexical_complete_recall_rate=1.01,
        minimum_contradiction_pass_rate=1.01,
        minimum_poisoning_pass_rate=1.01,
        maximum_recall_p95_ms=0.0,
    )
    logger = logging.getLogger("engram.test.eval-gate")
    logger.handlers = [logging.NullHandler()]
    logger.propagate = False
    monkeypatch.setattr(gate_module, "FTS_CONTRACT_THRESHOLDS", impossible)
    monkeypatch.setattr(cli_module, "load_config", lambda: app_config)
    monkeypatch.setattr(FileLogger, "configure", lambda _self: logger)
    monkeypatch.setattr(
        sys,
        "argv",
        ["engram", "eval", "--mode", "fts", "--out", str(tmp_path / "cli")],
    )

    with pytest.raises(SystemExit) as exit_info:
        cli_module.main()

    assert exit_info.value.code == cli_module.EXIT_PARTIAL_RESULT
    assert "FTS evaluation contract failed" in capsys.readouterr().err


def test_ci_runs_the_fts_contract_with_the_checked_in_config() -> None:
    workflow = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")

    assert "name: FTS evaluation contract" in workflow
    assert "ENGRAM_CONFIG: ${{ github.workspace }}/engram.example.toml" in workflow
    assert "engram eval --mode fts --out local/ci-eval" in workflow
    assert "name: Upload FTS evaluation artifacts" in workflow
    assert "if: always()" in workflow
    assert "os: [ubuntu-latest, windows-latest]" in workflow
    assert "name: fts-eval-${{ runner.os }}" in workflow
    assert "local/ci-eval/metrics.json" in workflow
    assert "local/ci-eval/rapport-eval.md" in workflow
