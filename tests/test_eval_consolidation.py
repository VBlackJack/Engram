# Copyright 2026 Julien Bombled
# SPDX-License-Identifier: Apache-2.0

"""Pure read-only consolidation proposal tests."""

from engram.eval.consolidation import propose_consolidation
from evalsets.engram_corpus import CONSOLIDATION_NEIGHBORS, CONSOLIDATION_TASKS


def test_propose_consolidation_classifies_every_seeded_case_exactly() -> None:
    candidates = tuple(task.candidate for task in CONSOLIDATION_TASKS)
    original_candidates = candidates
    original_neighbors = CONSOLIDATION_NEIGHBORS

    proposals = propose_consolidation(candidates, CONSOLIDATION_NEIGHBORS)

    assert tuple(item.classification for item in proposals) == tuple(
        task.expected for task in CONSOLIDATION_TASKS
    )
    assert candidates == original_candidates
    assert original_neighbors == CONSOLIDATION_NEIGHBORS
