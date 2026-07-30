# Copyright 2026 Julien Bombled
# SPDX-License-Identifier: Apache-2.0

"""Load the checked-in corpus through Engram's public storage methods."""

from __future__ import annotations

from dataclasses import dataclass

from engram.models import Entry
from engram.store import EngramStore
from evalsets.engram_corpus import (
    CORPUS_VERSION,
    DEGRADED_RECALL_TASKS,
    FTS_CONTRACT_RECALL_TASKS,
    FTS_CONTRACT_SHA256,
    FTS_CONTRACT_VERSION,
    GLOBAL_RECALL_TASKS,
    RECALL_TASKS,
    SEED_ENTRIES,
    SEMANTIC_BENCHMARK_SHA256,
    SEMANTIC_BENCHMARK_VERSION,
    SUPERSEDE_LINKS,
    trusted_claim_key,
    validate_corpus,
)

from .models import CorpusMetrics, DegradationClass


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
                claim_key=trusted_claim_key(seed),
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
        version=CORPUS_VERSION,
        semantic_benchmark_version=SEMANTIC_BENCHMARK_VERSION,
        semantic_benchmark_sha256=SEMANTIC_BENCHMARK_SHA256,
        fts_contract_version=FTS_CONTRACT_VERSION,
        fts_contract_sha256=FTS_CONTRACT_SHA256,
        entries=len(SEED_ENTRIES),
        trusted_entries=trusted,
        quarantined_entries=len(SEED_ENTRIES) - trusted,
        recall_tasks=len(RECALL_TASKS),
        global_tasks=len(GLOBAL_RECALL_TASKS),
        degraded_tasks=len(DEGRADED_RECALL_TASKS),
        fts_contract_tasks=len(FTS_CONTRACT_RECALL_TASKS),
        natural_degraded_tasks=sum(
            task.degradation is DegradationClass.NATURAL for task in FTS_CONTRACT_RECALL_TASKS
        ),
        adversarial_degraded_tasks=sum(
            task.degradation is DegradationClass.ADVERSARIAL for task in FTS_CONTRACT_RECALL_TASKS
        ),
        scopes=len({entry.scope for entry in SEED_ENTRIES}),
        kinds=len({entry.kind for entry in SEED_ENTRIES}),
    )
