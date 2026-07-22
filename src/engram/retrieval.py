# Copyright 2026 Julien Bombled
# SPDX-License-Identifier: Apache-2.0

"""Replaceable FTS5 and optional hybrid retrieval implementations."""

from __future__ import annotations

import logging
import math
import re
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from .config import AppConfig, RetrievalConfig, RetrievalMode
from .embeddings import EmbeddingError, EmbeddingProvider, HttpEmbeddingProvider
from .models import Entry, EntryKind, EntryStatus
from .store import EngramStore

LOGGER = logging.getLogger(__name__)
FTS_TERM_PATTERN = re.compile(r"\w+", flags=re.UNICODE)


@dataclass(frozen=True, slots=True)
class RetrievalRequest:
    """Normalized inputs passed to a retrieval implementation."""

    query: str
    scope: str | None
    kinds: frozenset[EntryKind] | None
    writer_model: str


@dataclass(frozen=True, slots=True)
class RetrievalResult:
    """Policy-neutral candidate groups returned by a retriever."""

    matches: tuple[Entry, ...]
    next_actions: tuple[Entry, ...]
    own_pending: tuple[Entry, ...]


class Retriever(Protocol):
    """Interface kept stable across lexical and hybrid implementations."""

    def retrieve(self, request: RetrievalRequest) -> RetrievalResult:
        """Return candidates for capsule construction."""
        ...


@runtime_checkable
class EntryIndexer(Protocol):
    """Optional post-commit hook implemented by derived vector indexes."""

    def index_entry(self, entry: Entry) -> None:
        """Best-effort index one committed entry without raising."""
        ...


class FtsRetriever:
    """Rank lexical matches with SQLite FTS5 BM25 and a substring fallback."""

    def __init__(self, store: EngramStore) -> None:
        """Bind the retriever to one store."""
        self._store = store

    def retrieve(self, request: RetrievalRequest) -> RetrievalResult:
        """Rank direct matches and apply shared lifecycle visibility rules."""
        return self.assemble(request, self.rank(request))

    def rank(self, request: RetrievalRequest) -> tuple[Entry, ...]:
        """Return direct visible matches in lexical rank order."""
        eligible = self.eligible_entries(request)
        eligible_ids = {entry.id for entry in eligible}
        match_expression = escape_fts_query(request.query)
        ranked = (
            ()
            if not match_expression
            else self._store.search_fts(
                match_expression,
                scope=_normalized_scope(request.scope),
                kinds=request.kinds,
            )
        )
        visible_ranked = tuple(entry for entry in ranked if entry.id in eligible_ids)
        if visible_ranked:
            return visible_ranked

        normalized_query = _normalized_text(request.query)
        return tuple(entry for entry in eligible if _matches_substring(entry, normalized_query))

    def eligible_entries(self, request: RetrievalRequest) -> tuple[Entry, ...]:
        """Return visible active rows and only the caller's quarantined rows."""
        normalized_scope = _normalized_scope(request.scope)
        entries: list[Entry] = []
        for entry in self._store.list_entries():
            if entry.stale or self._store.is_ttl_expired(entry):
                continue
            if normalized_scope is not None and entry.scope != normalized_scope:
                continue
            if request.kinds is not None and entry.kind not in request.kinds:
                continue
            if entry.status is EntryStatus.ACTIVE or (
                entry.status is EntryStatus.QUARANTINED
                and entry.writer_model == request.writer_model
            ):
                entries.append(entry)
        return tuple(entries)

    def assemble(
        self,
        request: RetrievalRequest,
        ranked: Sequence[Entry],
    ) -> RetrievalResult:
        """Add direct conflict neighbors and derive next-action/pending groups."""
        eligible = self.eligible_entries(request)
        eligible_by_id = {entry.id: entry for entry in eligible}
        direct = _unique_entries(entry for entry in ranked if entry.id in eligible_by_id)
        direct_active = tuple(entry for entry in direct if entry.status is EntryStatus.ACTIVE)
        own_pending = tuple(entry for entry in direct if entry.status is EntryStatus.QUARANTINED)
        related = tuple(
            entry
            for entry in eligible
            if entry.status is EntryStatus.ACTIVE
            and entry.id not in {item.id for item in direct_active}
            and any(_same_conflict_family(entry, seed) for seed in direct_active)
        )
        matches = (*direct_active, *related)
        direct_ids = {entry.id for entry in direct_active}
        next_actions = tuple(
            entry
            for entry in eligible
            if entry.status is EntryStatus.ACTIVE
            and entry.kind is EntryKind.PROJECT_STATE
            and (request.scope is not None or entry.id in direct_ids)
        )
        return RetrievalResult(
            matches=matches,
            next_actions=next_actions,
            own_pending=own_pending,
        )


class HybridRetriever:
    """Fuse FTS5 and remote embedding ranks with Reciprocal Rank Fusion."""

    def __init__(
        self,
        store: EngramStore,
        config: RetrievalConfig,
        *,
        provider: EmbeddingProvider | None = None,
    ) -> None:
        """Bind lexical retrieval, vector storage, provider, and RRF settings."""
        self._store = store
        self._config = config
        self._fts = FtsRetriever(store)
        self._provider = provider or HttpEmbeddingProvider(config)

    def retrieve(self, request: RetrievalRequest) -> RetrievalResult:
        """Fuse lexical and semantic ranks, degrading explicitly to FTS."""
        lexical = self._fts.rank(request)
        try:
            query_vector = self._provider.embed((request.query,))[0]
        except EmbeddingError as exc:
            LOGGER.warning("Embedding endpoint unavailable; degrading to FTS: %s", exc)
            return self._fts.assemble(request, lexical)

        eligible = self._fts.eligible_entries(request)
        semantic = _semantic_rank(
            eligible,
            self._store.list_vectors(self._config.embeddings_model),
            query_vector,
        )
        fused_ids = reciprocal_rank_fusion(
            ([entry.id for entry in lexical], [entry.id for entry in semantic]),
            self._config.rrf_k,
        )
        entries_by_id = {entry.id: entry for entry in eligible}
        fused = tuple(entries_by_id[entry_id] for entry_id in fused_ids)
        return self._fts.assemble(request, fused)

    def index_entry(self, entry: Entry) -> None:
        """Best-effort vectorize one committed entry without failing its write."""
        try:
            vector = self._provider.embed((_embedding_text(entry),))[0]
            self._store.upsert_vector(entry.id, self._config.embeddings_model, vector)
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("Entry %s stored without a vector: %s", entry.id, exc)

    def rebuild_vectors(self) -> int:
        """Rebuild all vectors for the configured model, or leave an empty index."""
        entries = self._store.list_entries()
        self._store.clear_vectors()
        if not entries:
            return 0
        try:
            vectors = self._provider.embed(tuple(_embedding_text(entry) for entry in entries))
        except EmbeddingError as exc:
            LOGGER.warning("Embedding endpoint unavailable; vector index left empty: %s", exc)
            return 0
        self._store.replace_vectors(
            self._config.embeddings_model,
            {entry.id: vector for entry, vector in zip(entries, vectors, strict=True)},
        )
        LOGGER.info("Rebuilt %d vectors for model %s", len(entries), self._config.embeddings_model)
        return len(entries)


def build_retriever(
    config: AppConfig,
    store: EngramStore,
    *,
    provider: EmbeddingProvider | None = None,
) -> Retriever:
    """Build the configured retriever while keeping FTS as the default."""
    if config.retrieval.mode is RetrievalMode.HYBRID:
        return HybridRetriever(store, config.retrieval, provider=provider)
    return FtsRetriever(store)


def escape_fts_query(query: str) -> str:
    """Turn arbitrary user text into quoted terms, never FTS operators."""
    normalized = unicodedata.normalize("NFKC", query)
    terms = FTS_TERM_PATTERN.findall(normalized)
    return " AND ".join(f'"{term}"' for term in terms)


def reciprocal_rank_fusion(
    rankings: Sequence[Sequence[str]],
    rrf_k: int,
) -> tuple[str, ...]:
    """Fuse rankings deterministically, preserving first-seen order on ties."""
    if rrf_k <= 0:
        raise ValueError("rrf_k must be greater than zero")
    scores: dict[str, float] = {}
    first_seen: dict[str, int] = {}
    seen_counter = 0
    for ranking in rankings:
        for rank, entry_id in enumerate(_unique_ids(ranking), start=1):
            if entry_id not in first_seen:
                first_seen[entry_id] = seen_counter
                seen_counter += 1
            scores[entry_id] = scores.get(entry_id, 0.0) + 1.0 / (rrf_k + rank)
    return tuple(sorted(scores, key=lambda entry_id: (-scores[entry_id], first_seen[entry_id])))


def _semantic_rank(
    eligible: Sequence[Entry],
    vectors: dict[str, tuple[float, ...]],
    query_vector: Sequence[float],
) -> tuple[Entry, ...]:
    scored: list[tuple[float, int, Entry]] = []
    for recency_index, entry in enumerate(eligible):
        vector = vectors.get(entry.id)
        if vector is None or len(vector) != len(query_vector):
            continue
        similarity = _cosine_similarity(vector, query_vector)
        if similarity is not None:
            scored.append((similarity, recency_index, entry))
    scored.sort(key=lambda item: (-item[0], item[1], item[2].id))
    return tuple(item[2] for item in scored)


def _cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float | None:
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm == 0 or right_norm == 0:
        return None
    return sum(a * b for a, b in zip(left, right, strict=True)) / (left_norm * right_norm)


def _same_conflict_family(candidate: Entry, seed: Entry) -> bool:
    return (
        candidate.kind is seed.kind
        and candidate.scope == seed.scope
        and bool(set(candidate.subject_keys).intersection(seed.subject_keys))
    )


def _embedding_text(entry: Entry) -> str:
    return " ".join((entry.statement, *entry.subject_keys))


def _normalized_scope(scope: str | None) -> str | None:
    return None if scope is None else scope.strip().casefold()


def _normalized_text(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).split()).casefold()


def _matches_substring(entry: Entry, normalized_query: str) -> bool:
    if not normalized_query:
        return False
    searchable = _normalized_text(" ".join((entry.statement, *entry.subject_keys)))
    return normalized_query in searchable


def _unique_entries(entries: Iterable[Entry]) -> tuple[Entry, ...]:
    unique: list[Entry] = []
    seen: set[str] = set()
    for entry in entries:
        if entry.id not in seen:
            seen.add(entry.id)
            unique.append(entry)
    return tuple(unique)


def _unique_ids(values: Sequence[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))
