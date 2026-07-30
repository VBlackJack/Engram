# Copyright 2026 Julien Bombled
# SPDX-License-Identifier: Apache-2.0

"""Short French Markdown report for the deterministic evaluation."""

from __future__ import annotations

from .models import (
    ConsolidationClass,
    EvaluationMetrics,
    FamilyMetrics,
    ModeMetrics,
    ModeStatus,
)


def render_report(metrics: EvaluationMetrics) -> str:
    """Render a two-minute, ASCII-punctuation French evaluation report."""
    lexical_threshold = _percent(
        metrics.fts_contract.thresholds.minimum_lexical_degraded_gold_in_capsule_rate
    )
    lexical_check = _check(
        passed=metrics.fts_contract.checks["lexical_degraded_gold_in_capsule_rate"]
    )
    lines = [
        "# Rapport d'evaluation Engram",
        "",
        (
            f"Corpus: `{metrics.corpus.version}`; semantique: "
            f"`{metrics.corpus.semantic_benchmark_version}`; contrat FTS: "
            f"`{metrics.corpus.fts_contract_version}`"
        ),
        "",
        "## Gate FTS",
        "",
        f"**{'PASS' if metrics.fts_contract.passed else 'FAIL'}**",
        "",
        (
            "Plafond capsule contractuel: "
            f"{metrics.fts_contract.capsule_byte_budget} octets UTF-8 conservateurs "
            f"({metrics.fts_contract.capsule_budget_unit}, "
            f"estimateur {metrics.fts_contract.capsule_estimator_version}, "
            f"marge {metrics.fts_contract.call_result_byte_margin} octets)."
        ),
        (
            "Retrieval contractuel: "
            f"top-K {metrics.fts_contract.retrieval_config['fts_top_k']}, "
            f"{metrics.fts_contract.retrieval_config['fts_max_query_terms']} termes maximum."
        ),
        "",
        "| Mesure | Observe | Seuil | Passe |",
        "|---|---:|---:|:---:|",
        (
            "| Gold global | "
            f"{_percent(metrics.fts_contract.global_gold_in_capsule_rate)} | "
            f">= {_percent(metrics.fts_contract.thresholds.minimum_global_gold_in_capsule_rate)} "
            "| "
            f"{_check(passed=metrics.fts_contract.checks['global_gold_in_capsule_rate'])} |"
        ),
        (
            "| Degrade lexical/adversarial | "
            f"{_percent(metrics.fts_contract.lexical_degraded_gold_in_capsule_rate)} | "
            f">= {lexical_threshold} "
            f"| {lexical_check} |"
        ),
        (
            "| Budget | "
            f"{_percent(metrics.fts_contract.budget_pass_rate)} | "
            f">= {_percent(metrics.fts_contract.thresholds.minimum_budget_pass_rate)} "
            f"| {_check(passed=metrics.fts_contract.checks['budget_pass_rate'])} |"
        ),
        (
            "| Rappel global complet | "
            f"{_percent(metrics.fts_contract.global_complete_recall_rate)} | "
            f">= "
            f"{_percent(metrics.fts_contract.thresholds.minimum_global_complete_recall_rate)} "
            f"| {_check(passed=metrics.fts_contract.checks['global_complete_recall_rate'])} |"
        ),
        (
            "| Rappel lexical complet | "
            f"{_percent(metrics.fts_contract.lexical_complete_recall_rate)} | "
            f">= "
            f"{_percent(metrics.fts_contract.thresholds.minimum_lexical_complete_recall_rate)} "
            f"| {_check(passed=metrics.fts_contract.checks['lexical_complete_recall_rate'])} |"
        ),
        (
            "| Politique de contradiction | "
            f"{_percent(metrics.fts_contract.contradiction_pass_rate)} | "
            f">= "
            f"{_percent(metrics.fts_contract.thresholds.minimum_contradiction_pass_rate)} "
            f"| {_check(passed=metrics.fts_contract.checks['contradiction_pass_rate'])} |"
        ),
        (
            "| Resistance au poisoning | "
            f"{_percent(metrics.fts_contract.poisoning_pass_rate)} | "
            f">= {_percent(metrics.fts_contract.thresholds.minimum_poisoning_pass_rate)} "
            f"| {_check(passed=metrics.fts_contract.checks['poisoning_pass_rate'])} |"
        ),
        (
            "| Recall p95 | "
            f"{_milliseconds(metrics.fts_contract.recall_p95_ms)} | "
            f"<= {_milliseconds(metrics.fts_contract.thresholds.maximum_recall_p95_ms)} "
            f"| {_check(passed=metrics.fts_contract.checks['recall_p95_ms'])} |"
        ),
        "",
        f"Warnings du contrat FTS: {_warning_summary(metrics.fts_contract.warning_counts)}.",
        f"Warnings FTS globaux: {_warning_summary(metrics.fts_contract.global_warning_counts)}.",
        "",
        (
            "Le gate FTS couvre le rappel global et les degradations lexicales/adversariales "
            "versionnees. Le benchmark semantique historique reste diagnostique et n'est pas "
            "compte comme robustesse FTS."
        ),
        "",
        "## Verdict P2",
        "",
        f"**{metrics.p2_verdict.value}**",
        "",
        f"- Gain semantique hybride: {_points(metrics.p2_measures.degraded_gain_points)}",
        f"- Delta global hybride: {_points(metrics.p2_measures.global_delta_points)}",
        f"- Recall p95 hybride: {_milliseconds(metrics.p2_measures.hybrid_recall_p95_ms)}",
        "",
        "## F1 - Rappel utile",
        "",
        "| Mode | Gold global | Lexical naturel | Lexical adversarial | "
        "Lexical total | Semantique historique | Budget | Complet | "
        "Warnings | Position moyenne |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---|---:|",
    ]
    for name, mode in metrics.modes.items():
        if mode.status is ModeStatus.MEASURED and mode.f1_recall is not None:
            family = mode.f1_recall
            lines.append(
                f"| {name} | {_percent(family.global_gold_in_capsule_rate)} | "
                f"{_percent(family.natural_degraded_gold_in_capsule_rate)} | "
                f"{_percent(family.adversarial_degraded_gold_in_capsule_rate)} | "
                f"{_percent(family.fts_contract_gold_in_capsule_rate)} | "
                f"{_percent(family.degraded_gold_in_capsule_rate)} | "
                f"{_percent(family.budget_pass_rate)} | "
                f"{_percent(family.complete_recall_rate)} | "
                f"{_warning_summary(family.warning_counts)} | "
                f"{_number(family.mean_gold_position)} |"
            )
        else:
            lines.append(
                f"| {name} | non mesure | non mesure | non mesure | non mesure | "
                "non mesure | non mesure | non mesure | non mesure | non mesure |"
            )

    lines.extend(
        [
            "",
            "## F2 - Contradiction",
            "",
            "| Mode | Tasks | Passees | Taux |",
            "|---|---:|---:|---:|",
        ]
    )
    _append_family_rows(lines, metrics, "f2_contradiction")

    lines.extend(
        [
            "",
            "Note de contrat: D7 prime. Un conflit non demande est masque, jamais affirme "
            "dans CURRENT. Avec `include_conflicts=true`, les versions restent symetriques "
            "et unresolved.",
            "",
            "## F3 - Consolidation read-only",
            "",
            "| Classe | Precision | Rappel | Support |",
            "|---|---:|---:|---:|",
        ]
    )
    for classification in ConsolidationClass:
        values = metrics.f3_consolidation.per_class[classification]
        lines.append(
            f"| {classification.value} | {_percent(values.precision)} | "
            f"{_percent(values.recall)} | {values.support} |"
        )

    lines.extend(
        [
            "",
            "## F4 - Resistance au poisoning",
            "",
            "| Mode | Tasks | Passees | Taux |",
            "|---|---:|---:|---:|",
        ]
    )
    _append_family_rows(lines, metrics, "f4_poisoning")

    lines.extend(
        [
            "",
            "## F5 - Systeme",
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
                f"{system.wal_size_bytes} octets/{system.wal_write_count} ecritures | "
                f"{_milliseconds(system.fts_reindex_ms)} | "
                f"{_milliseconds(system.vector_reindex_ms)} | "
                f"{_milliseconds(system.expire_ms)}/{_milliseconds(system.purge_ms)} |"
            )
        else:
            lines.append(f"| {name} | non mesure | non mesure | - | - | - | - | - |")

    lines.extend(
        [
            "",
            "## Methode",
            "",
            "Le harnais appelle directement EngramStore, le retriever configure et "
            "CapsuleBuilder dans le meme processus. Il note la capsule structuree et son "
            "fallback texte reels, sans bruit du transport HTTP. Le schema strict de "
            "`remember` est celui du serveur MCP. Les donnees vivent uniquement dans une "
            "base temporaire; aucun client Datacron n'est importe.",
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
            lines.append(f"| {name} | non mesure | non mesure | non mesure |")


def _family(mode: ModeMetrics, field: str) -> FamilyMetrics | None:
    if field == "f2_contradiction":
        return mode.f2_contradiction
    return mode.f4_poisoning


def _percent(value: float) -> str:
    return f"{value * 100:.1f} %"


def _points(value: float | None) -> str:
    return "non mesure" if value is None else f"{value:+.1f} points"


def _milliseconds(value: float | None) -> str:
    return "non mesure" if value is None else f"{value:.3f} ms"


def _number(value: float | None) -> str:
    return "-" if value is None else f"{value:.2f}"


def _check(*, passed: bool) -> str:
    return "oui" if passed else "non"


def _warning_summary(counts: dict[str, int]) -> str:
    if not counts:
        return "aucun"
    return ", ".join(f"{code}={count}" for code, count in sorted(counts.items()))
