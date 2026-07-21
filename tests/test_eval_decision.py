# Copyright 2026 Julien Bombled
# SPDX-License-Identifier: Apache-2.0

"""Fixed-threshold P2 verdict tests."""

from engram.eval.decision import decide_p2
from engram.eval.models import P2Verdict


def test_p2_retains_a_measurably_better_fast_hybrid() -> None:
    verdict, measures = decide_p2(
        fts_global_rate=0.90,
        fts_degraded_rate=0.50,
        hybrid_global_rate=0.90,
        hybrid_degraded_rate=0.60,
        hybrid_recall_p95_ms=99.999,
    )

    assert verdict is P2Verdict.HYBRID_RETAINED
    assert measures.degraded_gain_points == 10.0
    assert measures.global_delta_points == 0.0


def test_p2_rejects_hybrid_when_any_fixed_threshold_fails() -> None:
    verdict, measures = decide_p2(
        fts_global_rate=0.90,
        fts_degraded_rate=0.40,
        hybrid_global_rate=0.89,
        hybrid_degraded_rate=0.70,
        hybrid_recall_p95_ms=80.0,
    )

    assert verdict is P2Verdict.FTS_ADOPTED
    assert measures.global_delta_points == -1.0


def test_p2_defaults_to_fts_when_hybrid_is_unavailable() -> None:
    verdict, measures = decide_p2(
        fts_global_rate=1.0,
        fts_degraded_rate=0.0,
        hybrid_global_rate=None,
        hybrid_degraded_rate=None,
        hybrid_recall_p95_ms=None,
    )

    assert verdict is P2Verdict.HYBRID_UNMEASURED
    assert measures.degraded_gain_points is None
    assert measures.global_delta_points is None
    assert measures.hybrid_recall_p95_ms is None
