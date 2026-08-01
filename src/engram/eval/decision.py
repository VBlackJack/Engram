# Copyright 2026 Julien Bombled
# SPDX-License-Identifier: Apache-2.0

"""Mechanical P2 decision using the fixed release thresholds."""

from __future__ import annotations

from .models import P2Measures, P2Thresholds, P2Verdict

MINIMUM_DEGRADED_GAIN_POINTS = 10.0
MINIMUM_GLOBAL_DELTA_POINTS = 0.0
MAXIMUM_HYBRID_RECALL_P95_MS = 100.0


def decide_p2(
    *,
    fts_global_rate: float,
    fts_degraded_rate: float,
    hybrid_global_rate: float | None,
    hybrid_degraded_rate: float | None,
    hybrid_recall_p95_ms: float | None,
) -> tuple[P2Verdict, P2Measures]:
    """Return the fixed-threshold verdict and its three motivating measures."""
    if hybrid_global_rate is None or hybrid_degraded_rate is None or hybrid_recall_p95_ms is None:
        return (
            P2Verdict.HYBRID_UNMEASURED,
            P2Measures(
                degraded_gain_points=None,
                global_delta_points=None,
                hybrid_recall_p95_ms=None,
            ),
        )

    degraded_gain = _points(hybrid_degraded_rate - fts_degraded_rate)
    global_delta = _points(hybrid_global_rate - fts_global_rate)
    retained = (
        degraded_gain >= MINIMUM_DEGRADED_GAIN_POINTS
        and global_delta >= MINIMUM_GLOBAL_DELTA_POINTS
        and hybrid_recall_p95_ms < MAXIMUM_HYBRID_RECALL_P95_MS
    )
    return (
        P2Verdict.HYBRID_RETAINED if retained else P2Verdict.FTS_ADOPTED,
        P2Measures(
            degraded_gain_points=degraded_gain,
            global_delta_points=global_delta,
            hybrid_recall_p95_ms=round(hybrid_recall_p95_ms, 3),
        ),
    )


def p2_thresholds() -> P2Thresholds:
    """Serialize the fixed thresholds beside every verdict."""
    return P2Thresholds(
        minimum_degraded_gain_points=MINIMUM_DEGRADED_GAIN_POINTS,
        minimum_global_delta_points=MINIMUM_GLOBAL_DELTA_POINTS,
        maximum_hybrid_recall_p95_ms=MAXIMUM_HYBRID_RECALL_P95_MS,
    )


def _points(value: float) -> float:
    return round(value * 100, 3)
