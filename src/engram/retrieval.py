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
from .models import PROJECT_STATE_CLAIM_KEY, Entry, EntryKind, EntryStatus
from .store import EngramStore
from .vectors import is_usable_float32_vector

LOGGER = logging.getLogger(__name__)
FTS_TERM_PATTERN = re.compile(r"[^\W_]+", flags=re.UNICODE)
FTS_OPERATOR_TERMS = frozenset({"and", "or", "not", "near"})
NOTICE_CONFLICT_FAMILY_OVERFLOW = "conflict_family_overflow"
NOTICE_PROJECT_STATE_OVERFLOW = "project_state_overflow"
NOTICE_HYBRID_PROVIDER_UNAVAILABLE = "hybrid_provider_unavailable"
NOTICE_HYBRID_PROVIDER_INVALID_VECTOR = "hybrid_provider_invalid_vector"
NOTICE_HYBRID_CANDIDATE_OVERFLOW = "hybrid_candidate_overflow"
NOTICE_HYBRID_VECTOR_COVERAGE_INCOMPLETE = "hybrid_vector_coverage_incomplete"
NOTICE_QUERY_TOO_LONG = "query_too_long"
NOTICE_QUERY_TOO_MANY_TERMS = "query_too_many_terms"
NOTICE_QUERY_HAS_NO_SEARCH_TERMS = "query_has_no_search_terms"


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
    notices: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class _FtsQueryPlan:
    """Safe progressive FTS expressions derived from one bounded query."""

    strict: tuple[str, ...]
    fallback: tuple[str, ...]
    notices: tuple[str, ...] = ()


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
    """Rank lexical matches through a bounded progressive FTS5 pipeline."""

    def __init__(
        self,
        store: EngramStore,
        config: RetrievalConfig | None = None,
    ) -> None:
        """Bind the retriever to one store."""
        self._store = store
        self._config = config or RetrievalConfig()

    def retrieve(self, request: RetrievalRequest) -> RetrievalResult:
        """Rank direct matches and apply shared lifecycle visibility rules."""
        plan = self.query_plan(request.query)
        return self.assemble(
            request,
            self._rank_plan(request, plan),
            notices=plan.notices,
        )

    def rank(self, request: RetrievalRequest) -> tuple[Entry, ...]:
        """Return direct visible matches in lexical rank order."""
        plan = self.query_plan(request.query)
        return self._rank_plan(request, plan)

    def _rank_plan(
        self,
        request: RetrievalRequest,
        plan: _FtsQueryPlan,
    ) -> tuple[Entry, ...]:
        """Execute a prevalidated progressive plan."""
        if not plan.strict and not plan.fallback:
            return ()
        strict_ranked: list[Entry] = []
        strict_seen: set[str] = set()
        for expression in plan.strict:
            ranked = self._search(request, expression)
            for entry in ranked:
                if entry.id not in strict_seen:
                    strict_ranked.append(entry)
                    strict_seen.add(entry.id)
                    if len(strict_ranked) == self._config.fts_top_k:
                        return tuple(strict_ranked)

        fallback_rankings = tuple(self._search(request, expression) for expression in plan.fallback)
        return _interleave_entries(
            fallback_rankings,
            self._config.fts_top_k,
            initial=strict_ranked,
        )

    def query_plan(self, query: str) -> _FtsQueryPlan:
        """Return safe FTS stages, or an empty plan for invalid input."""
        return _build_fts_query_plan(query, self._config)

    def _search(
        self,
        request: RetrievalRequest,
        expression: str,
    ) -> tuple[Entry, ...]:
        """Execute one already-sanitized stage with all SQL visibility filters."""
        return self._store.search_fts(
            expression,
            scope=_normalized_scope(request.scope),
            kinds=request.kinds,
            writer_model=request.writer_model,
            limit=self._config.fts_top_k,
        )

    def eligible_entries(self, request: RetrievalRequest) -> tuple[Entry, ...]:
        """Return a bounded SQL-filtered semantic candidate window."""
        return self._store.list_retrieval_entries(
            scope=_normalized_scope(request.scope),
            kinds=request.kinds,
            writer_model=request.writer_model,
            limit=self._config.fts_top_k,
        )

    def assemble(
        self,
        request: RetrievalRequest,
        ranked: Sequence[Entry],
        *,
        notices: Sequence[str] = (),
    ) -> RetrievalResult:
        """Add complete bounded conflict families and derive output groups."""
        bounded_ranked = _unique_entries(ranked)[: self._config.fts_top_k]
        revalidated = self._store.get_retrieval_entries(
            tuple(entry.id for entry in bounded_ranked),
            writer_model=request.writer_model,
        )
        visible_by_id = {entry.id: entry for entry in revalidated}
        direct = tuple(
            visible_by_id[entry.id] for entry in bounded_ranked if entry.id in visible_by_id
        )
        direct_active = tuple(entry for entry in direct if entry.status is EntryStatus.ACTIVE)
        own_pending = tuple(entry for entry in direct if entry.status is EntryStatus.QUARANTINED)
        matches, conflict_notices = self._expand_conflict_families(direct_active)
        next_actions, project_state_notices = self._next_actions(
            request,
            matches,
            direct_active,
        )
        return RetrievalResult(
            matches=matches,
            next_actions=next_actions,
            own_pending=own_pending,
            notices=tuple(dict.fromkeys((*notices, *conflict_notices, *project_state_notices))),
        )

    def _expand_conflict_families(
        self,
        direct_active: Sequence[Entry],
    ) -> tuple[tuple[Entry, ...], tuple[str, ...]]:
        """Expand exact families without ever returning a truncated family."""
        accepted_keys: set[tuple[EntryKind, str, str]] = set()
        accepted_singletons: set[str] = set()
        related: list[Entry] = []
        used = 0
        overflowed = False
        for seed in direct_active:
            family_key = _conflict_family_key(seed)
            if family_key is None:
                if used < self._config.fts_top_k:
                    accepted_singletons.add(seed.id)
                    used += 1
                continue
            if family_key in accepted_keys:
                continue
            family = self._store.list_conflict_family(
                kind=seed.kind,
                scope=seed.scope,
                claim_key=family_key[2],
                limit=self._config.fts_top_k + 1,
            )
            if seed.id not in {entry.id for entry in family}:
                continue
            if len(family) > self._config.fts_top_k or used + len(family) > self._config.fts_top_k:
                overflowed = True
                LOGGER.warning(
                    "Recall omitted oversized conflict family kind=%s size=%d limit=%d",
                    seed.kind.value,
                    len(family),
                    self._config.fts_top_k,
                )
                continue
            accepted_keys.add(family_key)
            used += len(family)
            related.extend(family)

        direct_ids = {entry.id for entry in direct_active}
        accepted_direct = tuple(
            entry
            for entry in direct_active
            if entry.id in accepted_singletons or _conflict_family_key(entry) in accepted_keys
        )
        accepted_related = tuple(
            entry for entry in _unique_entries(related) if entry.id not in direct_ids
        )
        notices = (NOTICE_CONFLICT_FAMILY_OVERFLOW,) if overflowed else ()
        return (*accepted_direct, *accepted_related), notices

    def _next_actions(
        self,
        request: RetrievalRequest,
        matches: Sequence[Entry],
        direct_active: Sequence[Entry],
    ) -> tuple[tuple[Entry, ...], tuple[str, ...]]:
        """Return bounded project states, omitting an overflowing scope."""
        if request.kinds is not None and EntryKind.PROJECT_STATE not in request.kinds:
            return (), ()
        if request.scope is not None:
            normalized_scope = _normalized_scope(request.scope)
            if not normalized_scope:
                return (), ()
            states = self._store.list_project_states(
                scope=normalized_scope,
                limit=self._config.fts_top_k + 1,
            )
            if len(states) > self._config.fts_top_k:
                LOGGER.warning(
                    "Recall omitted oversized project-state family size=%d limit=%d",
                    len(states),
                    self._config.fts_top_k,
                )
                return (), (NOTICE_PROJECT_STATE_OVERFLOW,)
            return states, ()
        direct_ids = {entry.id for entry in direct_active}
        return (
            tuple(
                entry
                for entry in matches
                if entry.kind is EntryKind.PROJECT_STATE and entry.id in direct_ids
            ),
            (),
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
        self._fts = FtsRetriever(store, config)
        self._provider = provider or HttpEmbeddingProvider(config)

    def retrieve(self, request: RetrievalRequest) -> RetrievalResult:
        """Fuse lexical and semantic ranks, degrading explicitly to FTS."""
        plan = self._fts.query_plan(request.query)
        if not plan.strict:
            return self._fts.assemble(request, (), notices=plan.notices)
        lexical = self._fts.rank(request)
        try:
            query_vector = self._provider.embed((request.query,))[0]
        except EmbeddingError as exc:
            LOGGER.warning("Embedding endpoint unavailable; degrading to FTS: %s", exc)
            return self._fts.assemble(
                request,
                lexical,
                notices=(NOTICE_HYBRID_PROVIDER_UNAVAILABLE,),
            )
        if not _is_usable_vector(query_vector):
            LOGGER.warning("Embedding endpoint returned an unusable query vector; degrading to FTS")
            return self._fts.assemble(
                request,
                lexical,
                notices=(NOTICE_HYBRID_PROVIDER_INVALID_VECTOR,),
            )

        eligible = self._store.list_retrieval_vectors(
            self._config.embeddings_model,
            scope=_normalized_scope(request.scope),
            kinds=request.kinds,
            writer_model=request.writer_model,
            limit=self._config.hybrid_max_candidates + 1,
        )
        if len(eligible) > self._config.hybrid_max_candidates:
            LOGGER.warning(
                "Hybrid semantic scan omitted: candidate cap exceeded limit=%d",
                self._config.hybrid_max_candidates,
            )
            return self._fts.assemble(
                request,
                lexical,
                notices=(NOTICE_HYBRID_CANDIDATE_OVERFLOW,),
            )
        missing_vectors = any(
            vector is None or len(vector) != len(query_vector) for _, vector in eligible
        )
        semantic = _semantic_rank(
            eligible,
            query_vector,
            limit=self._config.fts_top_k,
        )
        fused_ids = reciprocal_rank_fusion(
            ([entry.id for entry in lexical], [entry.id for entry in semantic]),
            self._config.rrf_k,
        )
        entries_by_id = {entry.id: entry for entry in lexical}
        entries_by_id.update((entry.id, entry) for entry, _ in eligible)
        fused = tuple(
            entries_by_id[entry_id] for entry_id in fused_ids if entry_id in entries_by_id
        )
        notices = (NOTICE_HYBRID_VECTOR_COVERAGE_INCOMPLETE,) if missing_vectors else ()
        return self._fts.assemble(request, fused, notices=notices)

    def index_entry(self, entry: Entry) -> None:
        """Best-effort vectorize one committed entry without failing its write."""
        try:
            vector = self._provider.embed((_embedding_text(entry),))[0]
            self._store.upsert_vector(entry.id, self._config.embeddings_model, vector)
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("Entry %s stored without a vector: %s", entry.id, exc)

    def rebuild_vectors(self) -> int:
        """Replace one model atomically only after every new vector is usable."""
        entries = self._store.list_entries()
        if not entries:
            self._store.replace_vectors(self._config.embeddings_model, {})
            return 0
        try:
            vectors = self._provider.embed(tuple(_embedding_text(entry) for entry in entries))
        except EmbeddingError as exc:
            LOGGER.warning(
                "Embedding endpoint unavailable; existing vector index retained: %s", exc
            )
            return 0
        if (
            len(vectors) != len(entries)
            or len({len(vector) for vector in vectors}) != 1
            or any(not _is_usable_vector(vector) for vector in vectors)
        ):
            LOGGER.warning("Embedding endpoint returned unusable vectors; existing index retained")
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
    return FtsRetriever(store, config.retrieval)


def escape_fts_query(query: str) -> str:
    """Turn arbitrary user text into quoted terms, never FTS operators."""
    terms = _bounded_fts_terms(query, RetrievalConfig())
    return _join_fts_terms(terms, " AND ")


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
    eligible: Sequence[tuple[Entry, tuple[float, ...] | None]],
    query_vector: Sequence[float],
    *,
    limit: int,
) -> tuple[Entry, ...]:
    scored: list[tuple[float, int, Entry]] = []
    for order_index, (entry, vector) in enumerate(eligible):
        if vector is None or len(vector) != len(query_vector):
            continue
        similarity = _cosine_similarity(vector, query_vector)
        if similarity is not None and similarity > 0.0:
            scored.append((similarity, order_index, entry))
    scored.sort(key=lambda item: (-item[0], item[1], item[2].id))
    return tuple(item[2] for item in scored[:limit])


def _cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float | None:
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm == 0 or right_norm == 0:
        return None
    return sum(a * b for a, b in zip(left, right, strict=True)) / (left_norm * right_norm)


def _is_usable_vector(vector: Sequence[float]) -> bool:
    """Require a finite, float32-safe vector with a non-zero cosine norm."""
    return is_usable_float32_vector(vector)


def _conflict_family_key(entry: Entry) -> tuple[EntryKind, str, str] | None:
    claim_key = effective_claim_key(entry)
    if claim_key is None:
        return None
    return entry.kind, entry.scope, claim_key


def effective_claim_key(entry: Entry) -> str | None:
    """Return the explicit claim identity or the reserved project-state identity."""
    if entry.kind is EntryKind.PROJECT_STATE:
        return PROJECT_STATE_CLAIM_KEY
    return entry.claim_key


def _embedding_text(entry: Entry) -> str:
    return " ".join((entry.statement, *entry.subject_keys))


def _normalized_scope(scope: str | None) -> str | None:
    return None if scope is None else scope.strip().casefold()


def _normalized_text(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).split()).casefold()


def _build_fts_query_plan(query: str, config: RetrievalConfig) -> _FtsQueryPlan:
    terms, notices = _bounded_fts_terms_with_notices(query, config)
    if not terms:
        return _FtsQueryPlan(strict=(), fallback=(), notices=notices)

    phrase = f'"{" ".join(terms)}"'
    all_terms = _join_fts_terms(terms, " AND ")
    any_term = _join_fts_terms(terms, " OR ")
    prefix_terms = tuple(
        term for term in terms if len(term) >= config.fts_min_prefix_chars and not term.isdecimal()
    )
    prefix = " OR ".join(f'"{term}"*' for term in prefix_terms)
    strict = tuple(dict.fromkeys((phrase, all_terms)))
    fallback = tuple(
        expression for expression in dict.fromkeys((any_term, prefix)) if expression not in strict
    )
    return _FtsQueryPlan(strict=strict, fallback=fallback)


def _bounded_fts_terms(query: str, config: RetrievalConfig) -> tuple[str, ...]:
    terms, _ = _bounded_fts_terms_with_notices(query, config)
    return terms


def _bounded_fts_terms_with_notices(
    query: str,
    config: RetrievalConfig,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return safe terms plus a client-visible reason for rejected input."""
    if not query or len(query) > config.fts_max_query_chars:
        notice = NOTICE_QUERY_HAS_NO_SEARCH_TERMS if not query else NOTICE_QUERY_TOO_LONG
        return (), (notice,)
    normalized = unicodedata.normalize("NFKC", query)
    if len(normalized) > config.fts_max_query_chars:
        return (), (NOTICE_QUERY_TOO_LONG,)
    terms = FTS_TERM_PATTERN.findall(normalized)
    unique: list[str] = []
    seen: set[str] = set()
    for term in terms:
        identity = term.casefold()
        if identity not in seen:
            seen.add(identity)
            unique.append(term)
    if not unique or all(term.casefold() in FTS_OPERATOR_TERMS for term in unique):
        return (), (NOTICE_QUERY_HAS_NO_SEARCH_TERMS,)
    if len(unique) > config.fts_max_query_terms:
        return (), (NOTICE_QUERY_TOO_MANY_TERMS,)
    return tuple(unique), ()


def _join_fts_terms(terms: Sequence[str], operator: str) -> str:
    return operator.join(f'"{term}"' for term in terms)


def _unique_entries(entries: Iterable[Entry]) -> tuple[Entry, ...]:
    unique: list[Entry] = []
    seen: set[str] = set()
    for entry in entries:
        if entry.id not in seen:
            seen.add(entry.id)
            unique.append(entry)
    return tuple(unique)


def _interleave_entries(
    rankings: Sequence[Sequence[Entry]],
    limit: int,
    *,
    initial: Sequence[Entry] = (),
) -> tuple[Entry, ...]:
    """Fill after strict hits while fairly merging every fallback stage."""
    merged = list(_unique_entries(initial))[:limit]
    seen = {entry.id for entry in merged}
    depth = 0
    while len(merged) < limit:
        added_at_depth = False
        for ranking in rankings:
            if depth >= len(ranking):
                continue
            added_at_depth = True
            entry = ranking[depth]
            if entry.id not in seen:
                seen.add(entry.id)
                merged.append(entry)
                if len(merged) == limit:
                    break
        if not added_at_depth:
            break
        depth += 1
    return tuple(merged)


def _unique_ids(values: Sequence[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))
