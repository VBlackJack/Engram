# Copyright 2026 Julien Bombled
# SPDX-License-Identifier: Apache-2.0

"""Seed corpus dimensions, lifecycle, and isolation invariants."""

from __future__ import annotations

import ast
from pathlib import Path

from engram.eval.corpus import seed_corpus
from engram.models import EntryStatus
from engram.store import EngramStore
from evalsets.engram_corpus import (
    COMPLEMENT_TASKS,
    CONFLICT_TASKS,
    DEGRADED_RECALL_TASKS,
    GLOBAL_RECALL_TASKS,
    SEED_ENTRIES,
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

    assert len(SEED_ENTRIES) == 72
    assert len(GLOBAL_RECALL_TASKS) == 40
    assert len(DEGRADED_RECALL_TASKS) == 24
    source = Path("evalsets/engram_corpus.py").read_text(encoding="utf-8")
    assert not BANNED_PUNCTUATION.intersection(source)


def test_corpus_loads_through_store_and_preserves_scenarios(store: EngramStore) -> None:
    seeded = seed_corpus(store)

    assert store.count_entries() == 72
    assert set(seeded.entries_by_key) == {entry.key for entry in SEED_ENTRIES}
    for conflict_task in CONFLICT_TASKS:
        left, right = (seeded.entries_by_key[key] for key in conflict_task.entry_keys)
        assert left.kind is right.kind
        assert left.scope == right.scope
        assert set(left.subject_keys).intersection(right.subject_keys)
        assert left.idempotency_key != right.idempotency_key
    for complement_task in COMPLEMENT_TASKS:
        left, right = (seeded.entries_by_key[key] for key in complement_task.entry_keys)
        assert left.kind is not right.kind
        assert left.scope == right.scope
        assert set(left.subject_keys).intersection(right.subject_keys)
    for old_key, new_key in SUPERSEDE_LINKS:
        assert seeded.entries_by_key[old_key].id != seeded.entries_by_key[new_key].id
        stored_old = store.get_entry(seeded.entries_by_key[old_key].id)
        assert stored_old is not None
        assert stored_old.status is EntryStatus.SUPERSEDED


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
