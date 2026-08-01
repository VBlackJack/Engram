# Copyright 2026 Julien Bombled
# SPDX-License-Identifier: Apache-2.0

"""Pure read-only consolidation proposal logic for the evaluation spike."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence

from .models import (
    ConsolidationClass,
    ConsolidationProposal,
    ConsolidationStatement,
)

NEGATION_TERMS = frozenset({"no", "not", "never", "none", "without"})
TOKEN_PATTERN = re.compile(r"\w+", flags=re.UNICODE)


def propose_consolidation(
    candidates: Sequence[ConsolidationStatement],
    durable_neighbors: Sequence[ConsolidationStatement],
) -> tuple[ConsolidationProposal, ...]:
    """Classify candidates without network access, side effects, or durable writes."""
    return tuple(
        ConsolidationProposal(
            candidate_id=candidate.statement_id,
            classification=_classify(candidate, durable_neighbors),
        )
        for candidate in candidates
    )


def _classify(
    candidate: ConsolidationStatement,
    durable_neighbors: Sequence[ConsolidationStatement],
) -> ConsolidationClass:
    normalized = _normalize(candidate.statement)
    if any(normalized == _normalize(neighbor.statement) for neighbor in durable_neighbors):
        return ConsolidationClass.REDUNDANT

    related = [
        neighbor
        for neighbor in durable_neighbors
        if set(candidate.subject_keys).intersection(neighbor.subject_keys)
    ]
    if not related:
        return ConsolidationClass.NEW
    if any(_negated(candidate.statement) != _negated(item.statement) for item in related):
        return ConsolidationClass.CONTRADICTORY
    return ConsolidationClass.UPDATE


def _normalize(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).split()).casefold()


def _negated(value: str) -> bool:
    terms = {term.casefold() for term in TOKEN_PATTERN.findall(value)}
    return bool(terms.intersection(NEGATION_TERMS))
