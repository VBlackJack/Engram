# Copyright 2026 Julien Bombled
# SPDX-License-Identifier: Apache-2.0

"""Hybrid vector memory-budget regression tests."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Literal

import pytest

import engram.store as store_module
from engram.config import AppConfig, RetrievalConfig, RetrievalMode
from engram.db import transaction as database_transaction
from engram.embeddings import MAX_EMBEDDING_BATCH_ITEMS, EmbeddingError
from engram.models import Entry, EntryKind, SourceType
from engram.retrieval import (
    NOTICE_HYBRID_CANDIDATE_OVERFLOW,
    NOTICE_HYBRID_VECTOR_BUDGET_EXCEEDED,
    HybridRetriever,
    RetrievalRequest,
    VectorRebuildError,
)
from engram.store import EngramStore, StoreBusyError, StoreVectorBudgetError

MODEL = "vector-budget-model"
WRITER_MODEL = "vector-budget-test/1.0"
BudgetReason = Literal["candidate_count", "vector_values", "vector_bytes"]


@dataclass(frozen=True, slots=True)
class BudgetScenario:
    reason: BudgetReason
    entry_count: int
    candidate_limit: int
    value_budget: int
    byte_budget: int


class RecordingProvider:
    """Return fixed vectors while exposing batch/staging order to the test."""

    def __init__(
        self,
        events: list[tuple[str, int]],
        *,
        fail_on_call: int | None = None,
    ) -> None:
        """Configure event recording and an optional injected failure."""
        self.events = events
        self.fail_on_call = fail_on_call
        self.calls = 0

    def embed(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        batch = tuple(texts)
        self.calls += 1
        self.events.append(("embed", len(batch)))
        if self.calls == self.fail_on_call:
            raise EmbeddingError("injected second-batch failure")
        return tuple((1.0, 0.0) for _ in batch)


def _request(query: str) -> RetrievalRequest:
    return RetrievalRequest(
        query=query,
        scope="user",
        kinds=frozenset({EntryKind.FACT}),
        writer_model=WRITER_MODEL,
    )


def _seed_vector_entries(
    store: EngramStore,
    count: int,
    *,
    lexical_first: bool = False,
) -> tuple[Entry, ...]:
    entries: list[Entry] = []
    for index in range(count):
        statement = (
            "Lexical anchor survives semantic overflow."
            if lexical_first and index == 0
            else f"Opaque vector budget candidate {index}."
        )
        entry = store.add_attested(
            kind=EntryKind.FACT,
            scope="user",
            statement=statement,
            source_type=SourceType.TOOL_VERIFIED,
            claim_key=f"vector-budget/candidate-{index}",
        )
        store.upsert_vector(entry.id, MODEL, (1.0, 0.0))
        entries.append(entry)
    return tuple(entries)


@contextmanager
def _deny_entry_payload_reads(store: EngramStore) -> Iterator[list[tuple[str, str]]]:
    """Fail if the semantic scan materializes canonical entry payload columns."""
    connection = store._connection  # noqa: SLF001
    reads: list[tuple[str, str]] = []
    payload_columns = {
        "statement",
        "subject_keys",
        "supersedes",
        "evidence",
        "datacron_ref",
        "datacron_hash",
    }

    def authorizer(
        action: int,
        table_name: str | None,
        column_name: str | None,
        database_name: str | None,
        trigger_name: str | None,
    ) -> int:
        del database_name, trigger_name
        if (
            action == sqlite3.SQLITE_READ
            and table_name == "entries"
            and column_name in payload_columns
        ):
            reads.append((table_name, str(column_name)))
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    connection.set_authorizer(authorizer)
    try:
        yield reads
    finally:
        connection.set_authorizer(None)


def _set_scan_budget(
    monkeypatch: pytest.MonkeyPatch,
    *,
    values: int,
    vector_bytes: int,
) -> None:
    monkeypatch.setattr(store_module, "MAX_HYBRID_VECTOR_SCAN_VALUES", values)
    monkeypatch.setattr(store_module, "MAX_HYBRID_VECTOR_SCAN_BYTES", vector_bytes)


@pytest.mark.parametrize(
    "scenario",
    [
        pytest.param(BudgetScenario("candidate_count", 3, 2, 100, 400), id="candidate-count"),
        pytest.param(BudgetScenario("vector_values", 2, 3, 3, 400), id="vector-values"),
        pytest.param(BudgetScenario("vector_bytes", 2, 3, 100, 12), id="vector-bytes"),
    ],
)
def test_vector_scan_preflight_rejects_before_reading_blobs(
    store: EngramStore,
    monkeypatch: pytest.MonkeyPatch,
    scenario: BudgetScenario,
) -> None:
    _seed_vector_entries(store, scenario.entry_count)
    _set_scan_budget(
        monkeypatch,
        values=scenario.value_budget,
        vector_bytes=scenario.byte_budget,
    )

    with (
        _deny_entry_payload_reads(store) as payload_reads,
        pytest.raises(StoreVectorBudgetError) as raised,
    ):
        store.list_retrieval_vectors(
            MODEL,
            scope="user",
            kinds=frozenset({EntryKind.FACT}),
            writer_model=WRITER_MODEL,
            limit=scenario.candidate_limit,
        )

    assert raised.value.reason == scenario.reason
    assert payload_reads == []


@pytest.mark.parametrize(
    "scenario",
    [
        pytest.param(BudgetScenario("candidate_count", 3, 1, 100, 400), id="candidate-count"),
        pytest.param(BudgetScenario("vector_values", 2, 8, 3, 400), id="vector-values"),
        pytest.param(BudgetScenario("vector_bytes", 2, 8, 100, 12), id="vector-bytes"),
    ],
)
def test_hybrid_vector_budget_overflow_degrades_explicitly_to_fts(
    store: EngramStore,
    monkeypatch: pytest.MonkeyPatch,
    scenario: BudgetScenario,
) -> None:
    target = _seed_vector_entries(store, scenario.entry_count, lexical_first=True)[0]
    _set_scan_budget(
        monkeypatch,
        values=scenario.value_budget,
        vector_bytes=scenario.byte_budget,
    )
    provider = RecordingProvider([])
    retriever = HybridRetriever(
        store,
        RetrievalConfig(
            mode=RetrievalMode.HYBRID,
            embeddings_model=MODEL,
            hybrid_max_candidates=scenario.candidate_limit,
        ),
        provider=provider,
    )

    result = retriever.retrieve(_request("lexical anchor"))

    assert result.matches
    assert result.matches[0].id == target.id
    expected_notice = (
        NOTICE_HYBRID_CANDIDATE_OVERFLOW
        if scenario.reason == "candidate_count"
        else NOTICE_HYBRID_VECTOR_BUDGET_EXCEEDED
    )
    assert result.notices == (expected_notice,)
    assert provider.calls == 1


def test_vector_scan_returns_ids_without_materializing_entry_payloads(
    store: EngramStore,
) -> None:
    entries = _seed_vector_entries(store, 3)

    with _deny_entry_payload_reads(store) as payload_reads:
        eligible = store.list_retrieval_vectors(
            MODEL,
            scope="user",
            kinds=frozenset({EntryKind.FACT}),
            writer_model=WRITER_MODEL,
            limit=8,
        )

    assert {entry_id for entry_id, _vector in eligible} == {entry.id for entry in entries}
    assert payload_reads == []


def test_vector_scan_rejects_mismatched_large_blob_before_payload_fetch(
    store: EngramStore,
) -> None:
    entry = _seed_vector_entries(store, 1)[0]
    connection = store._connection  # noqa: SLF001
    oversized_blob_bytes = store_module.MAX_HYBRID_VECTOR_SCAN_BYTES + 4096
    connection.execute("PRAGMA ignore_check_constraints = ON")
    try:
        connection.execute(
            "UPDATE entry_vectors SET dim = 1, vector = zeroblob(?) WHERE entry_id = ?",
            (oversized_blob_bytes, entry.id),
        )
    finally:
        connection.execute("PRAGMA ignore_check_constraints = OFF")
    previous_limit = connection.setlimit(
        sqlite3.SQLITE_LIMIT_LENGTH,
        store_module.MAX_HYBRID_VECTOR_SCAN_BYTES + 1024,
    )
    try:
        with pytest.raises(StoreVectorBudgetError) as raised:
            store.list_retrieval_vectors(
                MODEL,
                scope="user",
                kinds=frozenset({EntryKind.FACT}),
                writer_model=WRITER_MODEL,
                limit=8,
            )
    finally:
        connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, previous_limit)

    assert raised.value.reason == "vector_bytes"


def test_vector_rebuild_pages_and_stages_each_embedding_batch(
    store: EngramStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry_count = MAX_EMBEDDING_BATCH_ITEMS * 2 + 1
    _seed_vector_entries(store, entry_count)
    events: list[tuple[str, int]] = []
    page_sizes: list[int] = []
    provider = RecordingProvider(events)
    retriever = HybridRetriever(
        store,
        RetrievalConfig(
            mode=RetrievalMode.HYBRID,
            embeddings_model=MODEL,
        ),
        provider=provider,
    )

    def forbidden_monolithic_path(*args: object, **kwargs: object) -> None:
        del args, kwargs
        pytest.fail("vector rebuild used a corpus-sized in-memory path")

    monkeypatch.setattr(store, "list_entries", forbidden_monolithic_path)
    monkeypatch.setattr(store, "replace_vectors", forbidden_monolithic_path)

    original_page = store.list_entries_page

    def observed_page(
        *,
        after_id: str | None,
        limit: int,
    ) -> tuple[Entry, ...]:
        page = original_page(after_id=after_id, limit=limit)
        page_sizes.append(len(page))
        return page

    monkeypatch.setattr(store, "list_entries_page", observed_page)
    original_stage = store.stage_vector_rebuild

    def observed_stage(
        model: str,
        vectors: Mapping[str, Sequence[float]],
    ) -> None:
        events.append(("stage", len(vectors)))
        original_stage(model, vectors)

    monkeypatch.setattr(store, "stage_vector_rebuild", observed_stage)

    rebuilt = retriever.rebuild_vectors()

    assert rebuilt == entry_count
    assert [size for size in page_sizes if size] == [
        MAX_EMBEDDING_BATCH_ITEMS,
        MAX_EMBEDDING_BATCH_ITEMS,
        1,
    ]
    assert max(page_sizes) <= MAX_EMBEDDING_BATCH_ITEMS
    assert events == [
        ("embed", MAX_EMBEDDING_BATCH_ITEMS),
        ("stage", MAX_EMBEDDING_BATCH_ITEMS),
        ("embed", MAX_EMBEDDING_BATCH_ITEMS),
        ("stage", MAX_EMBEDDING_BATCH_ITEMS),
        ("embed", 1),
        ("stage", 1),
    ]
    assert len(store.list_vectors(MODEL)) == entry_count


def test_vector_rebuild_aborts_staging_and_preserves_old_index(
    store: EngramStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry_count = MAX_EMBEDDING_BATCH_ITEMS + 1
    entries = _seed_vector_entries(store, entry_count)
    old_index = {entries[0].id: (0.0, 1.0)}
    store.replace_vectors(MODEL, old_index)
    events: list[tuple[str, int]] = []
    provider = RecordingProvider(events, fail_on_call=2)
    retriever = HybridRetriever(
        store,
        RetrievalConfig(
            mode=RetrievalMode.HYBRID,
            embeddings_model=MODEL,
        ),
        provider=provider,
    )

    def forbidden_monolithic_path(*args: object, **kwargs: object) -> None:
        del args, kwargs
        pytest.fail("vector rebuild used a corpus-sized in-memory path")

    monkeypatch.setattr(store, "list_entries", forbidden_monolithic_path)
    monkeypatch.setattr(store, "replace_vectors", forbidden_monolithic_path)
    original_stage = store.stage_vector_rebuild

    def observed_stage(
        model: str,
        vectors: Mapping[str, Sequence[float]],
    ) -> None:
        events.append(("stage", len(vectors)))
        original_stage(model, vectors)

    monkeypatch.setattr(store, "stage_vector_rebuild", observed_stage)
    original_abort = store.abort_vector_rebuild

    def observed_abort() -> None:
        events.append(("abort", 0))
        original_abort()

    monkeypatch.setattr(store, "abort_vector_rebuild", observed_abort)

    with pytest.raises(VectorRebuildError, match="existing vector index was retained"):
        retriever.rebuild_vectors()

    assert events == [
        ("embed", MAX_EMBEDDING_BATCH_ITEMS),
        ("stage", MAX_EMBEDDING_BATCH_ITEMS),
        ("embed", 1),
        ("abort", 0),
    ]
    assert store.list_vectors(MODEL) == old_index


def test_vector_rebuild_refuses_swap_after_another_connection_commits(
    store: EngramStore,
    app_config: AppConfig,
) -> None:
    entry = _seed_vector_entries(store, 1)[0]
    old_index = {entry.id: (0.0, 1.0)}
    store.replace_vectors(MODEL, old_index)
    store.begin_vector_rebuild(MODEL)
    store.stage_vector_rebuild(MODEL, {entry.id: (1.0, 0.0)})

    with EngramStore(app_config) as other:
        other.add_attested(
            kind=EntryKind.FACT,
            scope="user",
            statement="A concurrent commit invalidates the staged vector snapshot.",
            source_type=SourceType.HUMAN,
            claim_key="vector-budget/concurrent-commit",
        )

    with pytest.raises(StoreBusyError, match="another connection"):
        store.finish_vector_rebuild(MODEL, expected_count=1)
    store.abort_vector_rebuild()

    assert store.list_vectors(MODEL) == old_index


def test_vector_rebuild_rechecks_data_version_inside_swap_transaction(
    store: EngramStore,
    app_config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry = _seed_vector_entries(store, 1)[0]
    old_index = {entry.id: (0.0, 1.0)}
    store.replace_vectors(MODEL, old_index)
    store.begin_vector_rebuild(MODEL)
    store.stage_vector_rebuild(MODEL, {entry.id: (1.0, 0.0)})
    original_transaction = database_transaction
    injected = False

    @contextmanager
    def commit_immediately_before_begin(
        connection: sqlite3.Connection,
    ) -> Iterator[None]:
        nonlocal injected
        if not injected:
            injected = True
            external = sqlite3.connect(app_config.database.path)
            try:
                external.execute(
                    "UPDATE entries SET observed_at = ? WHERE id = ?",
                    ("2026-07-30T12:00:00.000000Z", entry.id),
                )
                external.commit()
            finally:
                external.close()
        with original_transaction(connection):
            yield

    monkeypatch.setattr(store_module, "transaction", commit_immediately_before_begin)

    with pytest.raises(StoreBusyError, match="another connection"):
        store.finish_vector_rebuild(MODEL, expected_count=1)
    store.abort_vector_rebuild()

    assert injected is True
    assert store.list_vectors(MODEL) == old_index
