# Copyright 2026 Julien Bombled
# SPDX-License-Identifier: Apache-2.0

"""Evaluation runner determinism and artifact tests."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from engram.config import AppConfig, RetrievalConfig
from engram.embeddings import EmbeddingProvider
from engram.eval.models import EvalMode, ModeStatus, P2Verdict
from engram.eval.runner import METRICS_FILENAME, REPORT_FILENAME, run_evaluation


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
    assert fts.f1_recall.tasks == 64
    assert fts.f2_contradiction is not None
    assert fts.f2_contradiction.tasks == 5
    assert metrics.f3_consolidation.tasks == 8
    assert fts.f4_poisoning is not None
    assert fts.f4_poisoning.tasks == 4
    assert fts.f5_system is not None
    assert fts.f5_system.recall_latency.samples == 64
    assert metrics.p2_verdict is P2Verdict.HYBRID_UNMEASURED
    assert (output / METRICS_FILENAME).is_file()
    report = (output / REPORT_FILENAME).read_text(encoding="utf-8")
    assert "## Verdict P2" in report
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
