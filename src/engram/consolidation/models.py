# Copyright 2026 Julien Bombled
# SPDX-License-Identifier: Apache-2.0

"""Validated machine-readable contracts for consolidation plans and reports."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from engram.eval.models import ConsolidationClass


class StrictModel(BaseModel):
    """Reject unknown plan fields so manual edits fail closed."""

    model_config = ConfigDict(extra="forbid")


class ConsolidationAction(StrEnum):
    """Datacron operation proposed for one candidate."""

    CREATE_NOTE = "create_note"
    PATCH_SECTION = "patch_section"
    SKIP = "skip"


class ReviewDecision(StrEnum):
    """Explicit decision edited by a human in the JSON plan."""

    PENDING = "pending"
    APPROVE = "approve"
    REJECT = "reject"


class ApplyStatus(StrEnum):
    """Per-proposition apply outcome."""

    APPLIED = "applied"
    SKIPPED = "skipped"
    STALE = "stale"
    FAILED = "failed"


class NoteView(StrictModel):
    """Current Datacron note bytes and content address."""

    rel_path: str
    title: str
    content: str
    content_hash: str


class NeighborSection(StrictModel):
    """One ranked Datacron section used by the deterministic classifier."""

    rel_path: str
    heading: str
    statement: str
    subject_keys: tuple[str, ...]
    content_hash: str
    heading_level: int = Field(default=2, ge=1, le=6)
    search_rank: int = Field(default=0, ge=0)
    excerpt: str = ""


class ContradictionSignal(StrictModel):
    """Complementary read-only signal returned by Datacron."""

    candidate_count: int = Field(ge=0)
    summary: str


class Proposition(StrictModel):
    """One stable proposal awaiting an explicit human decision."""

    candidate_id: str
    classification: ConsolidationClass
    proposed_action: ConsolidationAction
    rel_path: str
    heading: str
    new_content: str
    expected_hash: str | None
    neighbors: tuple[NeighborSection, ...]
    contradiction_signal: ContradictionSignal | None = None
    decision: ReviewDecision = ReviewDecision.PENDING


class ConsolidationPlan(StrictModel):
    """Deterministic JSON artifact produced without durable mutation."""

    schema_version: Literal[1] = 1
    propositions: tuple[Proposition, ...]


class ApplyOutcome(StrictModel):
    """Outcome for one reviewed proposition."""

    candidate_id: str
    status: ApplyStatus
    rel_path: str
    content_hash: str | None = None
    detail: str


class ApplyReport(StrictModel):
    """Complete non-fail-fast application report."""

    outcomes: tuple[ApplyOutcome, ...]


class FreshnessOutcome(StrictModel):
    """Hash comparison result for one promoted entry."""

    candidate_id: str
    rel_path: str
    stale: bool
    expected_hash: str
    current_hash: str | None


class FreshnessReport(StrictModel):
    """Complete promoted-entry freshness report."""

    outcomes: tuple[FreshnessOutcome, ...]
