# Copyright 2026 Julien Bombled
# SPDX-License-Identifier: Apache-2.0

"""FTS5, hybrid ranking, and derived-index reconstruction tests."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Sequence
from datetime import timedelta

import pytest

from engram.config import AppConfig, RetrievalConfig, RetrievalMode
from engram.embeddings import EmbeddingError
from engram.models import EntryKind, EntryStatus, SourceType
from engram.retrieval import (
    FtsRetriever,
    HybridRetriever,
    RetrievalRequest,
    reciprocal_rank_fusion,
)
from engram.store import EngramStore
from tests.conftest import MutableClock


class KeywordEmbeddingProvider:
    """Deterministic provider for retrieval tests without network access."""

    def __init__(self, *, fail: bool = False) -> None:
        """Select successful deterministic output or one provider failure."""
        self.fail = fail
        self.calls: list[tuple[str, ...]] = []

    def embed(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        self.calls.append(tuple(texts))
        if self.fail:
            raise EmbeddingError("mock endpoint unavailable")
        return tuple(_keyword_vector(text) for text in texts)


def _keyword_vector(text: str) -> tuple[float, ...]:
    normalized = text.casefold()
    return (
        1.0 if "semantic" in normalized else 0.0,
        1.0 if "lexical" in normalized else 0.0,
    )


def _request(query: str, *, writer_model: str = "test-client/1.0") -> RetrievalRequest:
    return RetrievalRequest(
        query=query,
        scope=None,
        kinds=None,
        writer_model=writer_model,
    )


def test_fts_matches_lexical_subjects_and_diacritics(store: EngramStore) -> None:
    lexical = store.add_attested(
        kind="fact",
        scope="user",
        statement="The lexical engine uses BM25 ranking.",
        source_type=SourceType.TOOL_VERIFIED,
        subject_keys=("search/lexical",),
    )
    accented = store.add_attested(
        kind="preference",
        scope="user",
        statement="Le cafe est prefere le matin.",
        source_type=SourceType.HUMAN,
        subject_keys=("routine/matinee",),
    )
    retriever = FtsRetriever(store)

    assert retriever.retrieve(_request("BM25 ranking")).matches[0].id == lexical.id
    assert retriever.retrieve(_request("search lexical")).matches[0].id == lexical.id
    assert retriever.retrieve(_request("cafe matinee")).matches[0].id == accented.id


def test_fts_treats_operators_as_terms_and_falls_back_to_substring(
    store: EngramStore,
) -> None:
    operators = store.add_attested(
        kind="episode",
        scope="user",
        statement="Alpha OR broken NEAR syntax was captured.",
        source_type=SourceType.HUMAN,
    )
    fallback = store.add_attested(
        kind="fact",
        scope="user",
        statement="Alphabetical fallback remains available.",
        source_type=SourceType.HUMAN,
    )
    retriever = FtsRetriever(store)

    injected = retriever.retrieve(_request('alpha OR "broken NEAR('))
    short_query = retriever.retrieve(_request("alphab"))

    assert injected.matches[0].id == operators.id
    assert short_query.matches[0].id == fallback.id


def test_fts_reindex_preserves_results(store: EngramStore) -> None:
    first = store.add_attested(
        kind="decision",
        scope="project/engram",
        statement="Rebuild the derived lexical index.",
        source_type=SourceType.HUMAN,
    )
    second = store.add_attested(
        kind="decision",
        scope="project/engram",
        statement="The derived lexical index remains reconstructible.",
        source_type=SourceType.TOOL_VERIFIED,
    )
    retriever = FtsRetriever(store)
    before = tuple(item.id for item in retriever.retrieve(_request("derived lexical")).matches)

    store.rebuild_fts()

    after = tuple(item.id for item in retriever.retrieve(_request("derived lexical")).matches)
    assert before == after
    assert set(after) == {first.id, second.id}


def test_fts_breaks_equal_bm25_scores_by_recency(
    store: EngramStore,
    clock: MutableClock,
) -> None:
    older = store.add_attested(
        kind="fact",
        scope="user",
        statement="Rank token older.",
        source_type=SourceType.HUMAN,
    )
    clock.current += timedelta(seconds=1)
    newer = store.add_attested(
        kind="fact",
        scope="user",
        statement="Rank token newer.",
        source_type=SourceType.HUMAN,
    )

    result = FtsRetriever(store).retrieve(_request("rank token"))

    assert [entry.id for entry in result.matches[:2]] == [newer.id, older.id]


def test_missing_fts_index_is_rebuilt_on_startup(app_config: AppConfig) -> None:
    with EngramStore(app_config) as store:
        entry = store.add_attested(
            kind="fact",
            scope="user",
            statement="Startup rebuild restores search.",
            source_type=SourceType.TOOL_VERIFIED,
        )

    connection = sqlite3.connect(app_config.database.path)
    try:
        connection.execute("DROP TABLE entries_fts")
        connection.commit()
    finally:
        connection.close()

    with EngramStore(app_config) as reopened:
        result = FtsRetriever(reopened).retrieve(_request("startup rebuild"))
        assert result.matches[0].id == entry.id


def test_expire_and_purge_remove_results_and_derived_rows(
    app_config: AppConfig,
    clock: MutableClock,
) -> None:
    with EngramStore(app_config, clock=clock) as store:
        entry = store.add_attested(
            kind="episode",
            scope="user",
            statement="Ephemeral retrieval payload.",
            source_type=SourceType.HUMAN,
        )
        store.upsert_vector(entry.id, "mock-model", (1.0, 0.0))
        clock.current += timedelta(days=8)
        assert store.expire_due() == 1
        assert FtsRetriever(store).retrieve(_request("ephemeral retrieval")).matches == ()
        assert store.purge_expired(clock.current) == 1
        assert store.list_vectors("mock-model") == {}

    connection = sqlite3.connect(app_config.database.path)
    try:
        row = connection.execute(
            "SELECT COUNT(*) FROM entries_fts WHERE entries_fts MATCH ?",
            ('"ephemeral"',),
        ).fetchone()
    finally:
        connection.close()
    assert row is not None
    assert row[0] == 0


def test_read_time_guards_hide_due_entries_before_sweep(
    store: EngramStore,
    clock: MutableClock,
) -> None:
    entry = store.add_candidate(
        kind="episode",
        scope="user",
        statement="Zero window TTL candidate.",
        writer_model="test-client/1.0",
    )
    assert entry.expires_at is not None
    clock.current = entry.expires_at
    request = _request("zero window")
    retriever = FtsRetriever(store)

    assert store.search_fts('"zero" AND "window"', scope=None, kinds=None) == ()
    assert retriever.eligible_entries(request) == ()
    result = retriever.retrieve(request)
    assert result.matches == ()
    assert result.own_pending == ()
    stored = store.get_entry(entry.id)
    assert stored is not None
    assert stored.status is EntryStatus.QUARANTINED


def test_reciprocal_rank_fusion_is_deterministic() -> None:
    rankings = (("a", "b", "c"), ("b", "a", "d"))

    first = reciprocal_rank_fusion(rankings, 60)
    second = reciprocal_rank_fusion(rankings, 60)

    assert first == second == ("a", "b", "c", "d")


def test_hybrid_retrieval_fuses_stored_vectors(store: EngramStore) -> None:
    lexical = store.add_attested(
        kind="fact",
        scope="user",
        statement="Lexical phrase matches directly.",
        source_type=SourceType.HUMAN,
    )
    semantic = store.add_attested(
        kind="fact",
        scope="user",
        statement="Semantic neighbour uses different wording.",
        source_type=SourceType.HUMAN,
    )
    config = RetrievalConfig(
        mode=RetrievalMode.HYBRID,
        embeddings_model="mock-model",
    )
    store.upsert_vector(lexical.id, "stale-model", (1.0, 1.0))
    provider = KeywordEmbeddingProvider()
    retriever = HybridRetriever(store, config, provider=provider)
    rebuilt = retriever.rebuild_vectors()

    result = retriever.retrieve(_request("lexical semantic"))

    assert rebuilt == 2
    assert set(store.list_vectors("mock-model")) == {lexical.id, semantic.id}
    assert store.list_vectors("stale-model") == {}
    assert {entry.id for entry in result.matches} == {lexical.id, semantic.id}


def test_hybrid_endpoint_failure_degrades_to_fts_with_warning(
    store: EngramStore,
    caplog: pytest.LogCaptureFixture,
) -> None:
    entry = store.add_attested(
        kind=EntryKind.FACT,
        scope="user",
        statement="Lexical fallback survives endpoint failure.",
        source_type=SourceType.HUMAN,
    )
    config = RetrievalConfig(
        mode=RetrievalMode.HYBRID,
        embeddings_model="mock-model",
    )
    retriever = HybridRetriever(
        store,
        config,
        provider=KeywordEmbeddingProvider(fail=True),
    )

    with caplog.at_level(logging.WARNING, logger="engram.retrieval"):
        result = retriever.retrieve(_request("lexical fallback"))

    assert result.matches[0].id == entry.id
    assert "degrading to FTS" in caplog.text
