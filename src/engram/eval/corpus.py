# Copyright 2026 Julien Bombled
# SPDX-License-Identifier: Apache-2.0

"""Load the checked-in corpus through Engram's public storage methods."""

from __future__ import annotations

from dataclasses import dataclass

from engram.models import Entry, EntryKind
from engram.store import EngramStore
from evalsets.engram_corpus import (
    DEGRADED_RECALL_TASKS,
    GLOBAL_RECALL_TASKS,
    RECALL_TASKS,
    SEED_ENTRIES,
    SUPERSEDE_LINKS,
    validate_corpus,
)

from .models import CorpusMetrics


@dataclass(frozen=True, slots=True)
class SeededCorpus:
    """Materialized entries keyed by stable corpus identifiers."""

    entries_by_key: dict[str, Entry]


def seed_corpus(store: EngramStore) -> SeededCorpus:
    """Load trusted and quarantined entries into one disposable store."""
    validate_corpus()
    entries: dict[str, Entry] = {}
    for seed in SEED_ENTRIES:
        if seed.writer_model is None:
            entry = store.add_attested(
                kind=seed.kind,
                scope=seed.scope,
                statement=seed.statement,
                source_type=seed.source_type,
                subject_keys=seed.subject_keys,
                actor="eval-seed",
                claim_key=(
                    seed.subject_keys[0]
                    if seed.kind
                    in {
                        EntryKind.PREFERENCE,
                        EntryKind.DECISION,
                        EntryKind.FACT,
                    }
                    else None
                ),
            )
        else:
            entry = store.add_candidate(
                kind=seed.kind,
                scope=seed.scope,
                statement=seed.statement,
                writer_model=seed.writer_model,
                subject_keys=seed.subject_keys,
            )
        entries[seed.key] = entry

    for old_key, new_key in SUPERSEDE_LINKS:
        store.supersede(entries[old_key].id, entries[new_key].id, actor="eval-seed")
    return SeededCorpus(entries_by_key=entries)


def corpus_metrics() -> CorpusMetrics:
    """Return stable corpus dimensions for metrics.json."""
    trusted = sum(entry.writer_model is None for entry in SEED_ENTRIES)
    return CorpusMetrics(
        entries=len(SEED_ENTRIES),
        trusted_entries=trusted,
        quarantined_entries=len(SEED_ENTRIES) - trusted,
        recall_tasks=len(RECALL_TASKS),
        global_tasks=len(GLOBAL_RECALL_TASKS),
        degraded_tasks=len(DEGRADED_RECALL_TASKS),
        scopes=len({entry.scope for entry in SEED_ENTRIES}),
        kinds=len({entry.kind for entry in SEED_ENTRIES}),
    )
