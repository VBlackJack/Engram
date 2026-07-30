# Copyright 2026 Julien Bombled
# SPDX-License-Identifier: Apache-2.0

"""Seed corpus dimensions, lifecycle, and isolation invariants."""

from __future__ import annotations

import ast
from dataclasses import replace
from pathlib import Path

import pytest

import engram.eval.gate as gate_module
import evalsets.engram_corpus as corpus_module
from engram.eval.corpus import seed_corpus
from engram.eval.models import CapsuleSection, DegradationClass
from engram.models import PROJECT_STATE_CLAIM_KEY, EntryKind, EntryStatus
from engram.store import EngramStore
from evalsets.engram_corpus import (
    COMPLEMENT_TASKS,
    CONFLICT_TASKS,
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
    validate_corpus,
)

BANNED_PUNCTUATION = frozenset(
    {
        "\u00a0",
        "\u2009",
        "\u2013",
        "\u2014",
        "\u2018",
        "\u2019",
        "\u201c",
        "\u201d",
        "\u2026",
        "\u202f",
    }
)


def test_corpus_static_dimensions_and_ascii_punctuation() -> None:
    validate_corpus()

    assert CORPUS_VERSION == "r3.0"
    assert SEMANTIC_BENCHMARK_VERSION == "om-04-v4"
    assert FTS_CONTRACT_VERSION == "fts-r3-v1"
    assert len(SEMANTIC_BENCHMARK_SHA256) == 64
    assert len(FTS_CONTRACT_SHA256) == 64
    assert len(SEED_ENTRIES) == 72
    assert len(GLOBAL_RECALL_TASKS) == 40
    assert len(DEGRADED_RECALL_TASKS) == 24
    assert len(FTS_CONTRACT_RECALL_TASKS) == 24
    assert (
        sum(task.degradation is DegradationClass.NATURAL for task in FTS_CONTRACT_RECALL_TASKS)
        == 12
    )
    assert (
        sum(task.degradation is DegradationClass.ADVERSARIAL for task in FTS_CONTRACT_RECALL_TASKS)
        == 12
    )
    assert DEGRADED_RECALL_TASKS[0].query == "brief progress summaries"
    assert FTS_CONTRACT_RECALL_TASKS[0].query == "please show concise status updates"
    source = Path("evalsets/engram_corpus.py").read_text(encoding="utf-8")
    assert not BANNED_PUNCTUATION.intersection(source)


def test_manifest_fingerprint_detects_seed_statement_tampering(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tampered = (replace(SEED_ENTRIES[0], statement="Tampered statement."), *SEED_ENTRIES[1:])
    monkeypatch.setattr(corpus_module, "SEED_ENTRIES", tampered)

    with pytest.raises(ValueError, match="Semantic benchmark changed"):
        corpus_module.validate_corpus()


def test_manifest_fingerprint_detects_expected_section_tampering(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tampered = (
        replace(RECALL_TASKS[0], expected_section=CapsuleSection.NEXT_ACTION),
        *RECALL_TASKS[1:],
    )
    monkeypatch.setattr(corpus_module, "RECALL_TASKS", tampered)

    with pytest.raises(ValueError, match="Semantic benchmark changed"):
        corpus_module.validate_corpus()


def test_manifest_fingerprint_detects_retrieval_contract_tampering(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tampered = replace(gate_module.FTS_CONTRACT_RETRIEVAL_CONFIG, fts_top_k=63)
    monkeypatch.setattr(gate_module, "FTS_CONTRACT_RETRIEVAL_CONFIG", tampered)

    with pytest.raises(ValueError, match="Semantic benchmark changed"):
        corpus_module.validate_corpus()


def test_corpus_loads_through_store_and_preserves_scenarios(store: EngramStore) -> None:
    seeded = seed_corpus(store)

    assert store.count_entries() == 72
    assert set(seeded.entries_by_key) == {entry.key for entry in SEED_ENTRIES}
    for conflict_task in CONFLICT_TASKS:
        left, right = (seeded.entries_by_key[key] for key in conflict_task.entry_keys)
        assert left.kind is right.kind
        assert left.scope == right.scope
        assert left.claim_key == right.claim_key
        assert set(left.subject_keys).intersection(right.subject_keys)
        assert left.idempotency_key != right.idempotency_key
    for complement_task in COMPLEMENT_TASKS:
        left, right = (seeded.entries_by_key[key] for key in complement_task.entry_keys)
        assert left.kind is not right.kind
        assert left.scope == right.scope
        assert left.claim_key == right.claim_key
        assert set(left.subject_keys).intersection(right.subject_keys)
    for old_key, new_key in SUPERSEDE_LINKS:
        assert seeded.entries_by_key[old_key].id != seeded.entries_by_key[new_key].id
        stored_old = store.get_entry(seeded.entries_by_key[old_key].id)
        assert stored_old is not None
        assert stored_old.status is EntryStatus.SUPERSEDED

    trusted_project_states = [
        entry
        for entry in seeded.entries_by_key.values()
        if entry.writer_model is None and entry.kind is EntryKind.PROJECT_STATE
    ]
    assert len({entry.scope for entry in trusted_project_states}) == len(trusted_project_states)
    assert all(entry.claim_key == PROJECT_STATE_CLAIM_KEY for entry in trusted_project_states)
    assert all(
        entry.claim_key is not None
        for entry in seeded.entries_by_key.values()
        if entry.writer_model is None and entry.kind is not EntryKind.EPISODE
    )
    preference = seeded.entries_by_key["pref_01"]
    assert preference.claim_key == "eval/pref_01"
    assert preference.claim_key not in preference.subject_keys
    assert all(
        entry.claim_key is None
        for entry in seeded.entries_by_key.values()
        if entry.kind is EntryKind.EPISODE or entry.writer_model is not None
    )


def test_harness_has_no_datacron_client_imports() -> None:
    files = [*Path("src/engram/eval").glob("*.py"), *Path("evalsets").glob("*.py")]
    imported_modules: set[str] = set()
    for file in files:
        tree = ast.parse(file.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_modules.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                imported_modules.add(node.module)

    assert not {name for name in imported_modules if name.startswith("datacron")}
