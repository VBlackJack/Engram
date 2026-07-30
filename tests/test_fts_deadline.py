# Copyright 2026 Julien Bombled
# SPDX-License-Identifier: Apache-2.0

"""Absolute FTS deadline, interruption cleanup, and degradation tests."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from threading import Event, Thread
from time import monotonic

import pytest

import engram.store as store_module
from engram.config import RetrievalConfig, RetrievalMode
from engram.models import Entry, SourceType
from engram.retrieval import (
    NOTICE_FTS_QUERY_TIMEOUT,
    FtsRetriever,
    HybridRetriever,
    RetrievalRequest,
)
from engram.store import EngramStore, StoreQueryTimeoutError

WRITER_MODEL = "deadline-tests/1.0"


@dataclass(slots=True)
class _ProgressTimeoutClock:
    """Expire only once SQLite reaches its first progress callback."""

    calls: int = 0

    def __call__(self) -> float:
        """Keep lock acquisition live, then cross the absolute deadline."""
        self.calls += 1
        return 0.0 if self.calls <= 2 else 1.0


class _NoCallEmbeddingProvider:
    """Record an accidental semantic request and fail immediately."""

    def __init__(self) -> None:
        """Start with no provider calls."""
        self.calls: list[tuple[str, ...]] = []

    def embed(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        """Reject every call because FTS timeout must short-circuit hybrid work."""
        self.calls.append(tuple(texts))
        raise AssertionError("embedding provider must not be called after an FTS timeout")


def _request(query: str) -> RetrievalRequest:
    return RetrievalRequest(
        query=query,
        scope=None,
        kinds=None,
        writer_model=WRITER_MODEL,
    )


def _add_episode(store: EngramStore, statement: str) -> Entry:
    return store.add_attested(
        kind="episode",
        scope="user",
        statement=statement,
        source_type=SourceType.HUMAN,
    )


def test_progress_timeout_leaves_connection_writes_and_fts_reusable(
    store: EngramStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed = _add_episode(store, "Progress timeout seed remains readable.")
    progress_clock = _ProgressTimeoutClock()
    monkeypatch.setattr(store_module, "SQLITE_PROGRESS_HANDLER_STEPS", 1)
    monkeypatch.setattr(store, "_monotonic_clock", progress_clock)
    deadline = store.begin_fts_query(10)

    with pytest.raises(StoreQueryTimeoutError, match="deadline expired") as error:
        store.search_fts(
            '"timeout"',
            scope=None,
            kinds=None,
            writer_model=WRITER_MODEL,
            limit=1,
            deadline=deadline,
        )

    assert progress_clock.calls >= 3
    assert isinstance(error.value.__cause__, sqlite3.OperationalError)
    assert error.value.__cause__.sqlite_errorcode == sqlite3.SQLITE_INTERRUPT
    assert seed.id in {entry.id for entry in store.list_entries()}

    reusable = _add_episode(store, "Connection write and FTS are reusable.")
    matches = store.search_fts(
        '"reusable"',
        scope=None,
        kinds=None,
        writer_model=WRITER_MODEL,
        limit=1,
    )

    assert matches
    assert matches[0].id == reusable.id


def test_deadline_includes_inter_thread_rlock_wait(store: EngramStore) -> None:
    _add_episode(store, "Lock wait timeout seed.")
    acquired = Event()
    release = Event()

    def hold_store_lock() -> None:
        with store.write_access():
            acquired.set()
            release.wait(timeout=2)

    worker = Thread(target=hold_store_lock, daemon=True)
    worker.start()
    elapsed = 0.0
    try:
        assert acquired.wait(timeout=1)
        deadline = store.begin_fts_query(10)
        started = monotonic()
        with pytest.raises(StoreQueryTimeoutError, match="deadline expired"):
            store.search_fts(
                '"timeout"',
                scope=None,
                kinds=None,
                writer_model=WRITER_MODEL,
                limit=1,
                deadline=deadline,
            )
        elapsed = monotonic() - started
    finally:
        release.set()
        worker.join(timeout=1)

    assert not worker.is_alive()
    assert elapsed < 1


def test_non_interrupt_fts_error_is_not_remapped_to_timeout(store: EngramStore) -> None:
    target = _add_episode(store, "Malformed FTS recovery remains available.")

    with pytest.raises(sqlite3.OperationalError) as error:
        store.search_fts(
            '"malformed" AND',
            scope=None,
            kinds=None,
            writer_model=WRITER_MODEL,
            limit=1,
        )

    assert error.value.sqlite_errorcode != sqlite3.SQLITE_INTERRUPT
    recovered = store.search_fts(
        '"malformed"',
        scope=None,
        kinds=None,
        writer_model=WRITER_MODEL,
        limit=1,
    )
    assert recovered[0].id == target.id


def test_timeout_discards_completed_stages_before_unbounded_revalidation(
    store: EngramStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    strict = _add_episode(store, "Strict completed stage.")
    fallback = _add_episode(store, "Fallback completed stage.")
    stage = 0

    def scripted_search(*_args: object, **_kwargs: object) -> tuple[Entry, ...]:
        nonlocal stage
        stage += 1
        if stage == 1:
            return (strict,)
        if stage == 2:
            return ()
        if stage == 3:
            return (fallback,)
        raise StoreQueryTimeoutError("FTS query deadline expired")

    monkeypatch.setattr(store, "search_fts", scripted_search)

    result = FtsRetriever(store).retrieve(_request("alpha beta"))

    assert stage == 4
    assert result.matches == ()
    assert result.next_actions == ()
    assert result.own_pending == ()
    assert result.notices == (NOTICE_FTS_QUERY_TIMEOUT,)


def test_timeout_in_first_stage_returns_empty_rank_with_notice(
    store: EngramStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def timeout_first_stage(*_args: object, **_kwargs: object) -> tuple[Entry, ...]:
        nonlocal calls
        calls += 1
        raise StoreQueryTimeoutError("FTS query deadline expired")

    monkeypatch.setattr(store, "search_fts", timeout_first_stage)

    result = FtsRetriever(store).retrieve(_request("alpha beta"))

    assert calls == 1
    assert result.matches == ()
    assert result.next_actions == ()
    assert result.own_pending == ()
    assert result.notices == (NOTICE_FTS_QUERY_TIMEOUT,)


def test_rank_raises_instead_of_hiding_timeout_as_absence(
    store: EngramStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def timeout_first_stage(*_args: object, **_kwargs: object) -> tuple[Entry, ...]:
        raise StoreQueryTimeoutError("FTS query deadline expired")

    monkeypatch.setattr(store, "search_fts", timeout_first_stage)

    with pytest.raises(StoreQueryTimeoutError, match="rank deadline expired"):
        FtsRetriever(store).rank(_request("alpha beta"))


def test_hybrid_propagates_fts_timeout_without_contacting_provider(
    store: EngramStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    completed = _add_episode(store, "Hybrid keeps a completed lexical stage.")
    calls = 0

    def timeout_after_completed_stage(*_args: object, **_kwargs: object) -> tuple[Entry, ...]:
        nonlocal calls
        calls += 1
        if calls == 1:
            return (completed,)
        raise StoreQueryTimeoutError("FTS query deadline expired")

    monkeypatch.setattr(store, "search_fts", timeout_after_completed_stage)
    provider = _NoCallEmbeddingProvider()
    retriever = HybridRetriever(
        store,
        RetrievalConfig(
            mode=RetrievalMode.HYBRID,
            embeddings_model="deadline-test-model",
        ),
        provider=provider,
    )

    result = retriever.retrieve(_request("alpha beta"))

    assert calls == 2
    assert result.matches == ()
    assert result.next_actions == ()
    assert result.own_pending == ()
    assert result.notices == (NOTICE_FTS_QUERY_TIMEOUT,)
    assert provider.calls == []
