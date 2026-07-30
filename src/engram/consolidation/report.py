# Copyright 2026 Julien Bombled
# SPDX-License-Identifier: Apache-2.0

"""Deterministic JSON and concise French Markdown consolidation artifacts."""

from __future__ import annotations

import difflib
import json

from pydantic import BaseModel

from .models import (
    ApplyReport,
    ConsolidationPlan,
    FreshnessReport,
    NeighborSection,
    Proposition,
)


def model_json(model: BaseModel) -> str:
    """Serialize a validated artifact with stable keys and a final newline."""
    payload = model.model_dump(mode="json")
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def render_plan_markdown(plan: ConsolidationPlan) -> str:
    """Render one compact review block per proposition."""
    lines = ["# Plan de consolidation Engram", "", f"Plan ID : `{plan.plan_id}`", ""]
    if not plan.propositions:
        lines.extend(["Aucune memoire eligible a promouvoir.", ""])
    for proposition in plan.propositions:
        target_selector = (
            f"H{proposition.heading_level} {proposition.heading}"
            if proposition.heading_level is not None
            else f"nouvelle note {proposition.heading}"
        )
        signal = (
            proposition.contradiction_signal.summary
            if proposition.contradiction_signal is not None
            else "aucun"
        )
        lines.extend(
            [
                f"## {proposition.candidate_id}",
                "",
                f"- Classification: `{proposition.classification.value}`",
                f"- Action: `{proposition.proposed_action.value}`",
                f"- Cible: `{proposition.rel_path}` / `{target_selector}`",
                f"- Hash CAS: `{proposition.expected_hash or 'nouvelle note'}`",
                f"- Decision: `{proposition.decision.value}`",
                f"- Signal Datacron: {signal}",
                "",
                "### Candidat",
                "",
                proposition.new_content,
                "",
            ]
        )
        if proposition.classification.value == "update":
            target = _review_target(proposition)
            lines.extend(["### Diff de la cible (lecture seule)", ""])
            if target is None:
                lines.extend(
                    [
                        "Diff indisponible: la cible ne correspond pas a un voisin unique.",
                        "",
                    ]
                )
            else:
                diff = difflib.unified_diff(
                    target.statement.strip().splitlines(),
                    proposition.new_content.strip().splitlines(),
                    fromfile=f"{target.rel_path}#{target.heading}",
                    tofile=f"candidate:{proposition.candidate_id}",
                    lineterm="",
                )
                lines.extend(["```diff", *diff, "```", ""])
        if proposition.classification.value == "contradictory":
            lines.extend(["### Versions voisines", ""])
            lines.extend(
                f"- `{neighbor.rel_path}#{neighbor.heading}`: {neighbor.statement}"
                for neighbor in proposition.neighbors
            )
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _review_target(proposition: Proposition) -> NeighborSection | None:
    """Return the unique snapshotted neighbor selected by the proposition."""
    matches = tuple(
        neighbor
        for neighbor in proposition.neighbors
        if (
            neighbor.rel_path,
            neighbor.heading,
            neighbor.heading_level,
            neighbor.content_hash,
        )
        == (
            proposition.rel_path,
            proposition.heading,
            proposition.heading_level,
            proposition.expected_hash,
        )
    )
    return matches[0] if len(matches) == 1 else None


def render_apply_markdown(report: ApplyReport) -> str:
    """Render apply outcomes without hiding per-candidate failures."""
    lines = ["# Application du plan de consolidation", ""]
    lines.extend(
        (f"- `{outcome.candidate_id}`: **{outcome.status.value}** - {outcome.detail}")
        for outcome in report.outcomes
    )
    if not report.outcomes:
        lines.append("Aucune proposition dans le plan.")
    return "\n".join(lines) + "\n"


def render_freshness_markdown(report: FreshnessReport) -> str:
    """Render promoted-note hash comparisons."""
    lines = ["# Fraicheur des promotions Engram", ""]
    for outcome in report.outcomes:
        status = "stale" if outcome.stale else "current"
        lines.append(f"- `{outcome.candidate_id}` -> `{outcome.rel_path}`: **{status}**")
    if not report.outcomes:
        lines.append("Aucune promotion a verifier.")
    return "\n".join(lines) + "\n"
