# Copyright 2026 Julien Bombled
# SPDX-License-Identifier: Apache-2.0

"""Short French Markdown report for the deterministic evaluation."""

from __future__ import annotations

from .models import EvaluationMetrics, FamilyMetrics, ModeMetrics, ModeStatus


def render_report(metrics: EvaluationMetrics) -> str:
    """Render a two-minute, ASCII-punctuation French evaluation report."""
    lines = [
        "# Rapport d'évaluation Engram",
        "",
        "## Verdict P2",
        "",
        f"**{metrics.p2_verdict.value}**",
        "",
        f"- Gain dégradé hybride: {_points(metrics.p2_measures.degraded_gain_points)}",
        f"- Delta global hybride: {_points(metrics.p2_measures.global_delta_points)}",
        f"- Recall p95 hybride: {_milliseconds(metrics.p2_measures.hybrid_recall_p95_ms)}",
        "",
        "## F1 - Rappel utile",
        "",
        "| Mode | Gold global | Gold degrade | Budget | Position moyenne |",
        "|---|---:|---:|---:|---:|",
    ]
    for name, mode in metrics.modes.items():
        if mode.status is ModeStatus.MEASURED and mode.f1_recall is not None:
            family = mode.f1_recall
            lines.append(
                f"| {name} | {_percent(family.global_gold_in_capsule_rate)} | "
                f"{_percent(family.degraded_gold_in_capsule_rate)} | "
                f"{_percent(family.budget_pass_rate)} | "
                f"{_number(family.mean_gold_position)} |"
            )
        else:
            lines.append(f"| {name} | non mesuré | non mesuré | non mesuré | non mesuré |")

    lines.extend(
        [
            "",
            "## F2 - Contradiction",
            "",
            "| Mode | Tasks | Passées | Taux |",
            "|---|---:|---:|---:|",
        ]
    )
    _append_family_rows(lines, metrics, "f2_contradiction")

    lines.extend(
        [
            "",
            "Note de contrat: D7 prime. Un conflit non demandé est masqué, jamais affirmé "
            "dans CURRENT. Avec `include_conflicts=true`, les versions restent symétriques "
            "et unresolved.",
            "",
            "## F3 - Consolidation read-only",
            "",
            "| Classe | Précision | Rappel | Support |",
            "|---|---:|---:|---:|",
        ]
    )
    for classification, values in metrics.f3_consolidation.per_class.items():
        lines.append(
            f"| {classification.value} | {_percent(values.precision)} | "
            f"{_percent(values.recall)} | {values.support} |"
        )

    lines.extend(
        [
            "",
            "## F4 - Résistance au poisoning",
            "",
            "| Mode | Tasks | Passées | Taux |",
            "|---|---:|---:|---:|",
        ]
    )
    _append_family_rows(lines, metrics, "f4_poisoning")

    lines.extend(
        [
            "",
            "## F5 - Système",
            "",
            "| Mode | Remember p95 | Recall p95 | SQLITE_BUSY | WAL | Reindex FTS | "
            "Reindex vecteurs | Expire/Purge |",
            "|---|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for name, mode in metrics.modes.items():
        if mode.status is ModeStatus.MEASURED and mode.f5_system is not None:
            system = mode.f5_system
            lines.append(
                f"| {name} | {_milliseconds(system.remember_latency.p95_ms)} | "
                f"{_milliseconds(system.recall_latency.p95_ms)} | "
                f"{system.sqlite_busy_count}/{system.concurrent_writes} | "
                f"{system.wal_size_bytes} octets/{system.wal_write_count} écritures | "
                f"{_milliseconds(system.fts_reindex_ms)} | "
                f"{_milliseconds(system.vector_reindex_ms)} | "
                f"{_milliseconds(system.expire_ms)}/{_milliseconds(system.purge_ms)} |"
            )
        else:
            lines.append(f"| {name} | non mesuré | non mesuré | - | - | - | - | - |")

    lines.extend(
        [
            "",
            "## Méthode",
            "",
            "Le harnais appelle directement EngramStore, le retriever configuré et "
            "CapsuleBuilder dans le même processus. Il note la capsule structurée et son "
            "fallback texte réels, sans bruit du transport HTTP. Le schéma strict de "
            "`remember` est celui du serveur MCP. Les données vivent uniquement dans une "
            "base temporaire; aucun client Datacron n'est importé.",
        ]
    )
    unavailable = [
        f"{name}: {mode.unavailable_reason}"
        for name, mode in metrics.modes.items()
        if mode.status is ModeStatus.UNAVAILABLE
    ]
    if unavailable:
        lines.extend(["", "Modes indisponibles: " + "; ".join(unavailable) + "."])
    return "\n".join(lines) + "\n"


def _append_family_rows(lines: list[str], metrics: EvaluationMetrics, field: str) -> None:
    for name, mode in metrics.modes.items():
        family = _family(mode, field)
        if mode.status is ModeStatus.MEASURED and family is not None:
            lines.append(
                f"| {name} | {family.tasks} | {family.passed} | {_percent(family.pass_rate)} |"
            )
        else:
            lines.append(f"| {name} | non mesuré | non mesuré | non mesuré |")


def _family(mode: ModeMetrics, field: str) -> FamilyMetrics | None:
    if field == "f2_contradiction":
        return mode.f2_contradiction
    return mode.f4_poisoning


def _percent(value: float) -> str:
    return f"{value * 100:.1f} %"


def _points(value: float | None) -> str:
    return "non mesuré" if value is None else f"{value:+.1f} points"


def _milliseconds(value: float | None) -> str:
    return "non mesuré" if value is None else f"{value:.3f} ms"


def _number(value: float | None) -> str:
    return "-" if value is None else f"{value:.2f}"
