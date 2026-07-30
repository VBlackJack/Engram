# Copyright 2026 Julien Bombled
# SPDX-License-Identifier: Apache-2.0

"""FTS5, hybrid ranking, and derived-index reconstruction tests."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Sequence
from datetime import timedelta

import pytest

import engram.db as db_module
from engram.config import AppConfig, RetrievalConfig, RetrievalMode
from engram.embeddings import MAX_EMBEDDING_BATCH_ITEMS, EmbeddingError
from engram.models import EntryKind, EntryStatus, SourceType
from engram.retrieval import (
    NOTICE_CONFLICT_FAMILY_OVERFLOW,
    NOTICE_HYBRID_CANDIDATE_OVERFLOW,
    NOTICE_HYBRID_PROVIDER_INVALID_VECTOR,
    NOTICE_HYBRID_VECTOR_COVERAGE_INCOMPLETE,
    NOTICE_PROJECT_STATE_OVERFLOW,
    NOTICE_QUERY_HAS_NO_SEARCH_TERMS,
    NOTICE_QUERY_TOO_LONG,
    NOTICE_QUERY_TOO_MANY_TERMS,
    FtsRetriever,
    HybridRetriever,
    RetrievalRequest,
    VectorRebuildError,
    reciprocal_rank_fusion,
)
from engram.store import EngramStore, StoreValidationError
from tests.conftest import MutableClock

LEXICAL_VARIANTS = (
    pytest.param(
        "Release validation remains pending.",
        (),
        "release validation",
        id="exact-phrase",
    ),
    pytest.param(
        "Release validation remains pending.",
        (),
        "validation release",
        id="reordered-and",
    ),
    pytest.param(
        "Release validation remains pending.",
        (),
        "please release validation",
        id="leading-noise-or",
    ),
    pytest.param(
        "Release validation remains pending.",
        (),
        "release validation please",
        id="trailing-noise-or",
    ),
    pytest.param(
        "Release validation remains pending.",
        (),
        "release please validation",
        id="middle-noise-or",
    ),
    pytest.param(
        "SQLite provides local storage.",
        (),
        "\uff33\uff31\uff2c\uff49\uff54\uff45",
        id="nfkc-width",
    ),
    pytest.param(
        "Le déploiement est validé.",
        (),
        "deploiement valide",
        id="diacritics",
    ),
    pytest.param(
        "The Recall Pipeline Is Deterministic.",
        (),
        "RECALL pipeline",
        id="case-folding",
    ),
    pytest.param(
        "The release build is reproducible.",
        (),
        "release-build",
        id="hyphen-separator",
    ),
    pytest.param(
        "The release build is reproducible.",
        (),
        "release/build",
        id="slash-separator",
    ),
    pytest.param(
        "The release build is reproducible.",
        (),
        "release_build",
        id="underscore-separator",
    ),
    pytest.param(
        "The operator's review is complete.",
        (),
        "operator's review",
        id="apostrophe-separator",
    ),
    pytest.param(
        "Alpha OR beta is recorded literally.",
        (),
        "alpha OR beta",
        id="literal-or",
    ),
    pytest.param(
        "Alpha NOT beta is recorded literally.",
        (),
        "alpha NOT beta",
        id="literal-not",
    ),
    pytest.param(
        "Alpha NEAR beta is recorded literally.",
        (),
        "alpha NEAR beta",
        id="literal-near",
    ),
    pytest.param(
        "Alpha beta remains searchable.",
        (),
        "alpha* OR impossible",
        id="wildcard-neutralized",
    ),
    pytest.param(
        "Alpha beta remains searchable.",
        (),
        'alpha") OR (impossible',
        id="quotes-parentheses-neutralized",
    ),
    pytest.param(
        "Deployment completed successfully.",
        (),
        "deploy",
        id="prefix-deployment",
    ),
    pytest.param(
        "Validated artifacts are immutable.",
        (),
        "validate",
        id="prefix-validated",
    ),
    pytest.param(
        "Reviewed changes passed every gate.",
        (),
        "review",
        id="prefix-reviewed",
    ),
    pytest.param(
        "Localization is pending.",
        (),
        "local",
        id="prefix-localization",
    ),
    pytest.param(
        "Le déploiement est prêt.",
        (),
        "deploi",
        id="prefix-diacritics",
    ),
    pytest.param(
        "The service is ready.",
        ("runtime/configuration",),
        "runtime config",
        id="subject-key-prefix",
    ),
    pytest.param(
        "SQLite 3 53 is the validated runtime.",
        (),
        "SQLite 3 53",
        id="numeric-token",
    ),
)


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


class DateAdvancingEmbeddingProvider(KeywordEmbeddingProvider):
    """Advance the injected store clock while a hybrid query is in flight."""

    def __init__(self, clock: MutableClock) -> None:
        """Bind the clock that advances during the embedding request."""
        super().__init__()
        self._clock = clock

    def embed(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        self._clock.current += timedelta(days=1)
        return super().embed(texts)


class FixedEmbeddingProvider:
    """Return one deliberately malformed provider result."""

    def __init__(self, vectors: tuple[tuple[float, ...], ...]) -> None:
        """Store the fixed malformed response."""
        self._vectors = vectors

    def embed(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        del texts
        return self._vectors


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


@pytest.mark.parametrize(("statement", "subject_keys", "query"), LEXICAL_VARIANTS)
def test_progressive_fts_covers_24_lexical_variants(
    store: EngramStore,
    statement: str,
    subject_keys: tuple[str, ...],
    query: str,
) -> None:
    target = store.add_attested(
        kind="fact",
        scope="user",
        statement=statement,
        source_type=SourceType.TOOL_VERIFIED,
        subject_keys=subject_keys,
        claim_key="lexical/variant",
    )

    result = FtsRetriever(store).retrieve(_request(query))

    assert result.matches
    assert result.matches[0].id == target.id


def test_fts_matches_lexical_subjects_and_diacritics(store: EngramStore) -> None:
    lexical = store.add_attested(
        kind="fact",
        scope="user",
        statement="The lexical engine uses BM25 ranking.",
        source_type=SourceType.TOOL_VERIFIED,
        subject_keys=("search/lexical",),
        claim_key="search/lexical-engine",
    )
    accented = store.add_attested(
        kind="preference",
        scope="user",
        statement="Le cafe est prefere le matin.",
        source_type=SourceType.HUMAN,
        subject_keys=("routine/matinee",),
        claim_key="routine/morning-beverage",
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
        claim_key="search/alphabetical-fallback",
    )
    retriever = FtsRetriever(store)

    injected = retriever.retrieve(_request('alpha OR "broken NEAR('))
    short_query = retriever.retrieve(_request("alphab"))

    assert injected.matches[0].id == operators.id
    assert short_query.matches[0].id == fallback.id


def test_fts_strict_stages_deduplicate_and_preserve_stage_priority(
    store: EngramStore,
    clock: MutableClock,
) -> None:
    exact = store.add_attested(
        kind="fact",
        scope="user",
        statement="Alpha beta exact phrase.",
        source_type=SourceType.HUMAN,
        claim_key="ranking/exact",
    )
    clock.current += timedelta(seconds=1)
    reordered = store.add_attested(
        kind="fact",
        scope="user",
        statement="Beta appears before alpha.",
        source_type=SourceType.HUMAN,
        claim_key="ranking/reordered",
    )

    result = FtsRetriever(store).retrieve(_request("alpha beta"))

    assert [entry.id for entry in result.matches] == [exact.id, reordered.id]


def test_fts_or_fallback_prefers_more_matching_terms(store: EngramStore) -> None:
    partial = store.add_attested(
        kind="fact",
        scope="user",
        statement="Alpha alone.",
        source_type=SourceType.HUMAN,
        claim_key="ranking/partial",
    )
    complete = store.add_attested(
        kind="fact",
        scope="user",
        statement="Alpha and beta together.",
        source_type=SourceType.HUMAN,
        claim_key="ranking/complete",
    )

    result = FtsRetriever(store).retrieve(_request("alpha beta impossible"))

    assert result.matches[0].id == complete.id
    assert {entry.id for entry in result.matches} == {partial.id, complete.id}


def test_fts_prefix_fallback_is_not_starved_by_an_exact_or_distractor(
    store: EngramStore,
) -> None:
    target = store.add_attested(
        kind="fact",
        scope="user",
        statement="Deployment completed successfully.",
        source_type=SourceType.HUMAN,
        claim_key="ranking/prefix-target",
    )
    distractor = store.add_attested(
        kind="fact",
        scope="user",
        statement="Please read the unrelated handbook.",
        source_type=SourceType.HUMAN,
        claim_key="ranking/or-distractor",
    )

    result = FtsRetriever(store).retrieve(_request("please deploy"))

    assert {entry.id for entry in result.matches} == {target.id, distractor.id}


def test_fts_strict_hit_does_not_suppress_prefix_fill(store: EngramStore) -> None:
    exact = store.add_attested(
        kind="fact",
        scope="user",
        statement="Please deploy the unrelated handbook.",
        source_type=SourceType.HUMAN,
        claim_key="ranking/strict-fill",
    )
    target = store.add_attested(
        kind="fact",
        scope="user",
        statement="Deployment completed successfully.",
        source_type=SourceType.HUMAN,
        claim_key="ranking/strict-fill-prefix",
    )

    result = FtsRetriever(
        store,
        RetrievalConfig(fts_top_k=2),
    ).retrieve(_request("please deploy"))

    assert [entry.id for entry in result.matches] == [exact.id, target.id]


@pytest.mark.parametrize("query", ["", "   ", '"*() OR NOT NEAR'])
def test_fts_empty_or_operator_only_query_is_fail_closed(
    store: EngramStore,
    query: str,
) -> None:
    store.add_attested(
        kind="fact",
        scope="user",
        statement="An unrelated entry exists.",
        source_type=SourceType.HUMAN,
        claim_key="query/empty",
    )

    result = FtsRetriever(store).retrieve(_request(query))

    assert result.matches == ()
    assert result.notices == (NOTICE_QUERY_HAS_NO_SEARCH_TERMS,)


def test_fts_oversized_query_is_rejected_before_sql(
    store: EngramStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = RetrievalConfig(fts_max_query_chars=8)

    def unexpected_search(*args: object, **kwargs: object) -> tuple[object, ...]:
        del args, kwargs
        pytest.fail("oversized query reached SQLite")

    monkeypatch.setattr(store, "search_fts", unexpected_search)

    result = FtsRetriever(store, config).retrieve(_request("x" * 9))

    assert result.matches == ()
    assert result.notices == (NOTICE_QUERY_TOO_LONG,)


def test_fts_excessive_term_count_is_fail_closed(store: EngramStore) -> None:
    entry = store.add_attested(
        kind="fact",
        scope="user",
        statement="Alpha beta gamma.",
        source_type=SourceType.HUMAN,
        claim_key="query/terms",
    )
    retriever = FtsRetriever(
        store,
        RetrievalConfig(fts_max_query_terms=2),
    )

    result = retriever.retrieve(_request("alpha beta gamma"))

    assert result.matches == ()
    assert result.notices == (NOTICE_QUERY_TOO_MANY_TERMS,)
    assert store.get_entry(entry.id) is not None


def test_hybrid_rejects_oversized_query_before_embedding(store: EngramStore) -> None:
    provider = KeywordEmbeddingProvider()
    retriever = HybridRetriever(
        store,
        RetrievalConfig(
            mode=RetrievalMode.HYBRID,
            fts_max_query_chars=8,
            embeddings_model="mock-model",
        ),
        provider=provider,
    )

    result = retriever.retrieve(_request("x" * 9))

    assert result.matches == ()
    assert provider.calls == []


def test_fts_top_k_is_applied_in_sql(
    store: EngramStore,
    clock: MutableClock,
) -> None:
    entries = []
    for index in range(5):
        entries.append(
            store.add_attested(
                kind="fact",
                scope="user",
                statement=f"Bounded token result {index}.",
                source_type=SourceType.HUMAN,
                claim_key=f"ranking/bounded-{index}",
            )
        )
        clock.current += timedelta(seconds=1)
    retriever = FtsRetriever(store, RetrievalConfig(fts_top_k=2))

    result = retriever.retrieve(_request("bounded token"))

    assert [entry.id for entry in result.matches] == [
        entries[-1].id,
        entries[-2].id,
    ]


def test_recall_path_never_scans_all_entries(
    store: EngramStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = store.add_attested(
        kind="fact",
        scope="user",
        statement="Bounded SQL recall path.",
        source_type=SourceType.HUMAN,
        claim_key="ranking/no-global-scan",
    )

    def unexpected_scan() -> tuple[object, ...]:
        pytest.fail("recall reached the unbounded list_entries path")

    monkeypatch.setattr(store, "list_entries", unexpected_scan)

    result = FtsRetriever(store).retrieve(_request("bounded SQL"))

    assert [entry.id for entry in result.matches] == [target.id]


def test_oversized_conflict_family_is_omitted_fail_closed(
    store: EngramStore,
    caplog: pytest.LogCaptureFixture,
) -> None:
    for index in range(3):
        store.add_attested(
            kind="fact",
            scope="user",
            statement=f"Retention policy variant {index}.",
            source_type=SourceType.HUMAN,
            claim_key="retention/policy",
        )
    retriever = FtsRetriever(store, RetrievalConfig(fts_top_k=2))

    with caplog.at_level(logging.WARNING, logger="engram.retrieval"):
        result = retriever.retrieve(_request("retention policy"))

    assert result.matches == ()
    assert result.notices == (NOTICE_CONFLICT_FAMILY_OVERFLOW,)
    assert "omitted oversized conflict family" in caplog.text
    assert "retention/policy" not in caplog.text
    assert "scope=user" not in caplog.text


def test_oversized_project_state_scope_is_omitted_fail_closed(
    store: EngramStore,
    caplog: pytest.LogCaptureFixture,
) -> None:
    for index in range(3):
        store.add_attested(
            kind="project_state",
            scope="project/engram",
            statement=f"Project state variant {index}.",
            source_type=SourceType.HUMAN,
        )
    retriever = FtsRetriever(store, RetrievalConfig(fts_top_k=2))
    request = RetrievalRequest(
        query="unrelated query",
        scope="project/engram",
        kinds=None,
        writer_model="test-client/1.0",
    )

    with caplog.at_level(logging.WARNING, logger="engram.retrieval"):
        result = retriever.retrieve(request)

    assert result.next_actions == ()
    assert result.notices == (NOTICE_PROJECT_STATE_OVERFLOW,)
    assert "omitted oversized project-state family" in caplog.text
    assert "project/engram" not in caplog.text


def test_other_writers_cannot_crowd_out_top_k_pending_candidate(
    store: EngramStore,
) -> None:
    own = store.add_candidate(
        kind="episode",
        scope="user",
        statement="Private bounded pending candidate.",
        writer_model="test-client/1.0",
    )
    for index in range(3):
        store.add_candidate(
            kind="episode",
            scope="user",
            statement=f"Private bounded pending candidate {index}.",
            writer_model=f"other-client/{index}",
        )
    retriever = FtsRetriever(store, RetrievalConfig(fts_top_k=1))

    result = retriever.retrieve(_request("private bounded pending"))

    assert [entry.id for entry in result.own_pending] == [own.id]


def test_store_fts_without_writer_returns_active_rows_only(
    store: EngramStore,
) -> None:
    active = store.add_attested(
        kind="fact",
        scope="user",
        statement="Principal free lexical row.",
        source_type=SourceType.HUMAN,
        claim_key="visibility/active",
    )
    store.add_candidate(
        kind="episode",
        scope="user",
        statement="Principal free lexical candidate.",
        writer_model="other-client/1.0",
    )

    result = store.search_fts(
        '"principal" AND "free" AND "lexical"',
        scope=None,
        kinds=None,
        limit=10,
    )

    assert [entry.id for entry in result] == [active.id]


@pytest.mark.parametrize("limit", [0, 257, True])
def test_store_fts_limit_is_fail_closed(
    store: EngramStore,
    limit: int,
) -> None:
    with pytest.raises(StoreValidationError, match="limit"):
        store.search_fts(
            '"bounded"',
            scope=None,
            kinds=None,
            writer_model="test-client/1.0",
            limit=limit,
        )


def test_fts_reindex_preserves_results(store: EngramStore) -> None:
    first = store.add_attested(
        kind="decision",
        scope="project/engram",
        statement="Rebuild the derived lexical index.",
        source_type=SourceType.HUMAN,
        claim_key="search/reindex-action",
    )
    second = store.add_attested(
        kind="decision",
        scope="project/engram",
        statement="The derived lexical index remains reconstructible.",
        source_type=SourceType.TOOL_VERIFIED,
        claim_key="search/reconstructibility",
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
        claim_key="ranking/older",
    )
    clock.current += timedelta(seconds=1)
    newer = store.add_attested(
        kind="fact",
        scope="user",
        statement="Rank token newer.",
        source_type=SourceType.HUMAN,
        claim_key="ranking/newer",
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
            claim_key="search/startup-rebuild",
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


def test_v4_upgrade_repairs_a_missing_fts_index_before_recall(
    app_config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migrations = db_module.MIGRATIONS
    connection = sqlite3.connect(app_config.database.path, isolation_level=None)
    connection.row_factory = sqlite3.Row
    try:
        monkeypatch.setattr(db_module, "MIGRATIONS", migrations[:4])
        db_module.apply_migrations(connection)
        connection.execute("DROP TABLE entries_fts")
    finally:
        connection.close()
        monkeypatch.setattr(db_module, "MIGRATIONS", migrations)

    with EngramStore(app_config) as upgraded:
        entry = upgraded.add_attested(
            kind="fact",
            scope="user",
            statement="Upgrade repair restores lexical recall.",
            source_type=SourceType.TOOL_VERIFIED,
            claim_key="search/upgrade-repair",
        )
        result = FtsRetriever(upgraded).retrieve(_request("upgrade repair"))

    assert result.matches[0].id == entry.id


def test_inconsistent_fts_index_is_rebuilt_on_startup(app_config: AppConfig) -> None:
    with EngramStore(app_config) as store:
        entry = store.add_attested(
            kind="fact",
            scope="user",
            statement="Integrity repair restores indexed search.",
            source_type=SourceType.TOOL_VERIFIED,
            claim_key="search/integrity-repair",
        )

    connection = sqlite3.connect(app_config.database.path)
    try:
        connection.execute("INSERT INTO entries_fts(entries_fts) VALUES ('delete-all')")
        connection.commit()
    finally:
        connection.close()

    with EngramStore(app_config) as reopened:
        result = FtsRetriever(reopened).retrieve(_request("integrity repair"))
        assert result.matches[0].id == entry.id


def test_wrong_table_named_like_fts_index_is_rebuilt_on_startup(
    app_config: AppConfig,
) -> None:
    with EngramStore(app_config) as store:
        entry = store.add_attested(
            kind="fact",
            scope="user",
            statement="Schema repair restores indexed search.",
            source_type=SourceType.TOOL_VERIFIED,
            claim_key="search/schema-repair",
        )

    connection = sqlite3.connect(app_config.database.path)
    try:
        connection.execute("DROP TABLE entries_fts")
        connection.execute("CREATE TABLE entries_fts(entries_fts TEXT, rank INTEGER)")
        connection.commit()
    finally:
        connection.close()

    with EngramStore(app_config) as reopened:
        result = FtsRetriever(reopened).retrieve(_request("schema repair"))
        assert result.matches[0].id == entry.id


def test_wrong_view_named_like_fts_index_is_rebuilt_on_startup(
    app_config: AppConfig,
) -> None:
    with EngramStore(app_config) as store:
        entry = store.add_attested(
            kind="fact",
            scope="user",
            statement="View repair restores indexed search.",
            source_type=SourceType.TOOL_VERIFIED,
            claim_key="search/view-repair",
        )

    connection = sqlite3.connect(app_config.database.path)
    try:
        connection.execute("DROP TABLE entries_fts")
        connection.execute(
            "CREATE VIEW entries_fts AS SELECT statement AS entries_fts, rowid AS rank FROM entries"
        )
        connection.commit()
    finally:
        connection.close()

    with EngramStore(app_config) as reopened:
        result = FtsRetriever(reopened).retrieve(_request("view repair"))
        assert result.matches[0].id == entry.id


def test_wrong_vector_table_schema_is_rebuilt_empty_on_startup(
    app_config: AppConfig,
) -> None:
    with EngramStore(app_config):
        pass

    connection = sqlite3.connect(app_config.database.path)
    try:
        connection.execute("DROP TABLE entry_vectors")
        connection.execute("CREATE TABLE entry_vectors(foo TEXT)")
        connection.commit()
    finally:
        connection.close()

    with EngramStore(app_config):
        pass

    connection = sqlite3.connect(app_config.database.path)
    try:
        columns = connection.execute(
            "SELECT name FROM pragma_table_info('entry_vectors') ORDER BY cid"
        ).fetchall()
    finally:
        connection.close()
    assert [str(row[0]) for row in columns] == [
        "entry_id",
        "model",
        "dim",
        "vector",
    ]


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


def test_business_validity_window_is_inclusive_across_read_paths(
    store: EngramStore,
    clock: MutableClock,
) -> None:
    today = clock.current.date()
    store.add_attested(
        kind="fact",
        scope="user",
        statement="Business window future.",
        source_type=SourceType.HUMAN,
        claim_key="business-window/future",
        valid_from=today + timedelta(days=2),
    )
    store.add_attested(
        kind="fact",
        scope="user",
        statement="Business window elapsed.",
        source_type=SourceType.HUMAN,
        claim_key="business-window/elapsed",
        valid_until=today - timedelta(days=1),
    )
    boundary = store.add_attested(
        kind="fact",
        scope="user",
        statement="Business window boundary.",
        source_type=SourceType.HUMAN,
        claim_key="business-window/boundary",
        valid_from=today,
        valid_until=today,
    )
    retriever = FtsRetriever(store)
    request = _request("business window")

    assert [
        entry.id
        for entry in store.search_fts(
            '"business" AND "window"',
            scope=None,
            kinds=None,
        )
    ] == [boundary.id]
    assert [entry.id for entry in retriever.eligible_entries(request)] == [boundary.id]
    assert [entry.id for entry in retriever.retrieve(request).matches] == [boundary.id]

    clock.current += timedelta(days=1)

    assert store.search_fts('"business" AND "window"', scope=None, kinds=None) == ()
    assert retriever.eligible_entries(request) == ()
    assert retriever.retrieve(request).matches == ()


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
        claim_key="hybrid/lexical",
    )
    semantic = store.add_attested(
        kind="fact",
        scope="user",
        statement="Semantic neighbour uses different wording.",
        source_type=SourceType.HUMAN,
        claim_key="hybrid/semantic",
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
    assert store.list_vectors("stale-model") == {lexical.id: (1.0, 1.0)}
    assert {entry.id for entry in result.matches} == {lexical.id, semantic.id}


def test_hybrid_vector_rebuild_uses_bounded_batches(store: EngramStore) -> None:
    for index in range(MAX_EMBEDDING_BATCH_ITEMS + 1):
        store.add_attested(
            kind="episode",
            scope="user",
            statement=f"Bounded lexical embedding rebuild entry {index}.",
            source_type=SourceType.HUMAN,
        )
    provider = KeywordEmbeddingProvider()
    retriever = HybridRetriever(
        store,
        RetrievalConfig(
            mode=RetrievalMode.HYBRID,
            embeddings_model="mock-model",
        ),
        provider=provider,
    )

    rebuilt = retriever.rebuild_vectors()

    assert rebuilt == MAX_EMBEDDING_BATCH_ITEMS + 1
    assert [len(batch) for batch in provider.calls] == [MAX_EMBEDDING_BATCH_ITEMS, 1]


def test_hybrid_preserves_old_lexical_hit_outside_semantic_window(
    store: EngramStore,
    clock: MutableClock,
) -> None:
    lexical = store.add_attested(
        kind="fact",
        scope="user",
        statement="Ancient lexical anchor remains authoritative.",
        source_type=SourceType.HUMAN,
        claim_key="hybrid/old-lexical",
    )
    store.upsert_vector(lexical.id, "mock-model", (0.0, 1.0))
    for index in range(3):
        clock.current += timedelta(seconds=1)
        recent = store.add_attested(
            kind="fact",
            scope="user",
            statement=f"Recent semantic candidate {index}.",
            source_type=SourceType.HUMAN,
            claim_key=f"hybrid/recent-{index}",
        )
        store.upsert_vector(recent.id, "mock-model", (1.0, 0.0))
    retriever = HybridRetriever(
        store,
        RetrievalConfig(
            mode=RetrievalMode.HYBRID,
            embeddings_model="mock-model",
            fts_top_k=2,
        ),
        provider=KeywordEmbeddingProvider(),
    )

    result = retriever.retrieve(_request("lexical anchor"))

    assert result.matches
    assert result.matches[0].id == lexical.id


def test_hybrid_exact_bounded_scan_finds_an_old_semantic_only_target(
    store: EngramStore,
    clock: MutableClock,
) -> None:
    target = store.add_attested(
        kind="fact",
        scope="user",
        statement="Aged paraphrase destination.",
        source_type=SourceType.HUMAN,
        claim_key="hybrid/old-semantic",
    )
    store.upsert_vector(target.id, "mock-model", (1.0, 0.0))
    for index in range(3):
        clock.current += timedelta(seconds=1)
        distractor = store.add_attested(
            kind="fact",
            scope="user",
            statement=f"Recent orthogonal candidate {index}.",
            source_type=SourceType.HUMAN,
            claim_key=f"hybrid/orthogonal-{index}",
        )
        store.upsert_vector(distractor.id, "mock-model", (0.0, 1.0))
    retriever = HybridRetriever(
        store,
        RetrievalConfig(
            mode=RetrievalMode.HYBRID,
            embeddings_model="mock-model",
            fts_top_k=2,
            hybrid_max_candidates=8,
        ),
        provider=KeywordEmbeddingProvider(),
    )

    result = retriever.retrieve(_request("semantic intent"))

    assert [entry.id for entry in result.matches] == [target.id]
    assert result.notices == ()


def test_hybrid_candidate_overflow_fails_closed_to_lexical_with_notice(
    store: EngramStore,
    caplog: pytest.LogCaptureFixture,
) -> None:
    for index, vector in enumerate(((1.0, 0.0), (0.0, 1.0), (0.0, 1.0))):
        entry = store.add_attested(
            kind="fact",
            scope="user",
            statement=f"Opaque vector candidate {index}.",
            source_type=SourceType.HUMAN,
            claim_key=f"hybrid/overflow-{index}",
        )
        store.upsert_vector(entry.id, "mock-model", vector)
    retriever = HybridRetriever(
        store,
        RetrievalConfig(
            mode=RetrievalMode.HYBRID,
            embeddings_model="mock-model",
            fts_top_k=2,
            hybrid_max_candidates=2,
        ),
        provider=KeywordEmbeddingProvider(),
    )

    with caplog.at_level(logging.WARNING, logger="engram.retrieval"):
        result = retriever.retrieve(_request("semantic intent"))

    assert result.matches == ()
    assert result.notices == (NOTICE_HYBRID_CANDIDATE_OVERFLOW,)
    assert "candidate cap exceeded" in caplog.text


def test_hybrid_missing_vector_marks_semantic_coverage_incomplete(
    store: EngramStore,
) -> None:
    indexed = store.add_attested(
        kind="fact",
        scope="user",
        statement="Indexed opaque candidate.",
        source_type=SourceType.HUMAN,
        claim_key="hybrid/indexed",
    )
    store.upsert_vector(indexed.id, "mock-model", (1.0, 0.0))
    store.add_attested(
        kind="fact",
        scope="user",
        statement="Unindexed opaque candidate.",
        source_type=SourceType.HUMAN,
        claim_key="hybrid/unindexed",
    )
    retriever = HybridRetriever(
        store,
        RetrievalConfig(
            mode=RetrievalMode.HYBRID,
            embeddings_model="mock-model",
        ),
        provider=KeywordEmbeddingProvider(),
    )

    result = retriever.retrieve(_request("semantic intent"))

    assert [entry.id for entry in result.matches] == [indexed.id]
    assert result.notices == (NOTICE_HYBRID_VECTOR_COVERAGE_INCOMPLETE,)


def test_hybrid_incompatible_vector_dimension_marks_coverage_incomplete(
    store: EngramStore,
) -> None:
    entry = store.add_attested(
        kind="fact",
        scope="user",
        statement="Incompatible opaque candidate.",
        source_type=SourceType.HUMAN,
        claim_key="hybrid/incompatible-vector",
    )
    store.upsert_vector(entry.id, "mock-model", (1.0, 0.0, 0.0))
    retriever = HybridRetriever(
        store,
        RetrievalConfig(
            mode=RetrievalMode.HYBRID,
            embeddings_model="mock-model",
        ),
        provider=KeywordEmbeddingProvider(),
    )

    result = retriever.retrieve(_request("semantic intent"))

    assert result.matches == ()
    assert result.notices == (NOTICE_HYBRID_VECTOR_COVERAGE_INCOMPLETE,)


def test_hybrid_corrupt_vector_degrades_to_incomplete_coverage(
    app_config: AppConfig,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with EngramStore(app_config) as store:
        entry = store.add_attested(
            kind="fact",
            scope="user",
            statement="Corrupt opaque candidate.",
            source_type=SourceType.HUMAN,
            claim_key="hybrid/corrupt-vector",
        )
        store.upsert_vector(entry.id, "mock-model", (1.0, 0.0))

    connection = sqlite3.connect(app_config.database.path)
    try:
        connection.execute("PRAGMA ignore_check_constraints = ON")
        connection.execute(
            "UPDATE entry_vectors SET vector = X'00' WHERE entry_id = ?",
            (entry.id,),
        )
        connection.commit()
        connection.execute("PRAGMA ignore_check_constraints = OFF")
    finally:
        connection.close()

    with EngramStore(app_config) as reopened:
        retriever = HybridRetriever(
            reopened,
            RetrievalConfig(
                mode=RetrievalMode.HYBRID,
                embeddings_model="mock-model",
            ),
            provider=KeywordEmbeddingProvider(),
        )
        with caplog.at_level(logging.WARNING, logger="engram.store"):
            result = retriever.retrieve(_request("semantic intent"))

    assert result.matches == ()
    assert result.notices == (NOTICE_HYBRID_VECTOR_COVERAGE_INCOMPLETE,)
    assert "semantic coverage is incomplete" in caplog.text


def test_hybrid_endpoint_failure_degrades_to_fts_with_warning(
    store: EngramStore,
    caplog: pytest.LogCaptureFixture,
) -> None:
    entry = store.add_attested(
        kind=EntryKind.FACT,
        scope="user",
        statement="Lexical fallback survives endpoint failure.",
        source_type=SourceType.HUMAN,
        claim_key="hybrid/fallback",
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


def test_hybrid_zero_norm_query_vector_degrades_to_fts_with_notice(
    store: EngramStore,
) -> None:
    retriever = HybridRetriever(
        store,
        RetrievalConfig(
            mode=RetrievalMode.HYBRID,
            embeddings_model="mock-model",
        ),
        provider=KeywordEmbeddingProvider(),
    )

    result = retriever.retrieve(_request("opaque intent"))

    assert result.matches == ()
    assert result.notices == (NOTICE_HYBRID_PROVIDER_INVALID_VECTOR,)


@pytest.mark.parametrize(
    "vectors",
    [
        (),
        ((10**10_000,),),
    ],
    ids=["missing-vector", "unrepresentable-integer"],
)
def test_hybrid_malformed_provider_result_degrades_to_fts(
    store: EngramStore,
    vectors: tuple[tuple[float, ...], ...],
) -> None:
    entry = store.add_attested(
        kind=EntryKind.FACT,
        scope="user",
        statement="Lexical fallback survives malformed provider output.",
        source_type=SourceType.HUMAN,
        claim_key="hybrid/malformed-provider",
    )
    retriever = HybridRetriever(
        store,
        RetrievalConfig(
            mode=RetrievalMode.HYBRID,
            embeddings_model="mock-model",
        ),
        provider=FixedEmbeddingProvider(vectors),
    )

    result = retriever.retrieve(_request("lexical fallback"))

    assert result.matches[0].id == entry.id
    assert NOTICE_HYBRID_PROVIDER_INVALID_VECTOR in result.notices


def test_failed_vector_rebuild_retains_the_existing_model_index(
    store: EngramStore,
) -> None:
    entry = store.add_attested(
        kind="fact",
        scope="user",
        statement="Existing vector index remains intact.",
        source_type=SourceType.HUMAN,
        claim_key="hybrid/atomic-rebuild",
    )
    store.upsert_vector(entry.id, "mock-model", (1.0, 0.0))
    retriever = HybridRetriever(
        store,
        RetrievalConfig(
            mode=RetrievalMode.HYBRID,
            embeddings_model="mock-model",
        ),
        provider=KeywordEmbeddingProvider(fail=True),
    )

    with pytest.raises(VectorRebuildError, match="existing vector index was retained"):
        retriever.rebuild_vectors()

    assert store.list_vectors("mock-model") == {entry.id: (1.0, 0.0)}


def test_oversized_vector_rebuild_retains_the_existing_model_index(
    store: EngramStore,
) -> None:
    class OversizedProvider:
        def embed(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
            return tuple((1e300, 1.0) for _ in texts)

    entry = store.add_attested(
        kind="fact",
        scope="user",
        statement="Float32 overflow cannot erase the vector index.",
        source_type=SourceType.HUMAN,
        claim_key="hybrid/float32-rebuild",
    )
    store.upsert_vector(entry.id, "mock-model", (1.0, 0.0))
    retriever = HybridRetriever(
        store,
        RetrievalConfig(
            mode=RetrievalMode.HYBRID,
            embeddings_model="mock-model",
        ),
        provider=OversizedProvider(),
    )

    with pytest.raises(VectorRebuildError, match="existing vector index was retained"):
        retriever.rebuild_vectors()

    assert store.list_vectors("mock-model") == {entry.id: (1.0, 0.0)}


def test_store_rejects_zero_norm_vectors(store: EngramStore) -> None:
    entry = store.add_attested(
        kind="fact",
        scope="user",
        statement="Zero norm vectors are invalid.",
        source_type=SourceType.HUMAN,
        claim_key="hybrid/zero-norm",
    )

    with pytest.raises(StoreValidationError, match="non-zero norm"):
        store.upsert_vector(entry.id, "mock-model", (0.0, 0.0))


def test_store_rejects_vectors_outside_float32(store: EngramStore) -> None:
    entry = store.add_attested(
        kind="fact",
        scope="user",
        statement="Oversized float vectors are invalid.",
        source_type=SourceType.HUMAN,
        claim_key="hybrid/float32-range",
    )

    with pytest.raises(StoreValidationError, match="fit in float32"):
        store.upsert_vector(entry.id, "mock-model", (1e300, 1.0))


def test_hybrid_recall_excludes_entry_that_becomes_invalid_during_embedding(
    store: EngramStore,
    clock: MutableClock,
) -> None:
    store.add_attested(
        kind=EntryKind.FACT,
        scope="user",
        statement="Hybrid midnight boundary remains safe.",
        source_type=SourceType.HUMAN,
        claim_key="hybrid/midnight-boundary",
        valid_until=clock.current.date(),
    )
    retriever = HybridRetriever(
        store,
        RetrievalConfig(
            mode=RetrievalMode.HYBRID,
            embeddings_model="mock-model",
        ),
        provider=DateAdvancingEmbeddingProvider(clock),
    )

    result = retriever.retrieve(_request("hybrid midnight boundary"))

    assert result.matches == ()
