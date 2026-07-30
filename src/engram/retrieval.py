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
from .embeddings import (
    MAX_EMBEDDING_BATCH_ITEMS,
    EmbeddingError,
    EmbeddingProvider,
    HttpEmbeddingProvider,
)
from .models import PROJECT_STATE_CLAIM_KEY, Entry, EntryKind, EntryStatus
from .store import (
    EngramStore,
    FtsQueryDeadline,
    StoreQueryTimeoutError,
    StoreVectorBudgetError,
)
from .vectors import is_usable_float32_vector

LOGGER = logging.getLogger(__name__)
FTS_TERM_PATTERN = re.compile(r"[^\W_]+", flags=re.UNICODE)
FTS_OPERATOR_TERMS = frozenset({"and", "or", "not", "near"})
NOTICE_CONFLICT_FAMILY_OVERFLOW = "conflict_family_overflow"
NOTICE_PROJECT_STATE_OVERFLOW = "project_state_overflow"
NOTICE_HYBRID_PROVIDER_UNAVAILABLE = "hybrid_provider_unavailable"
NOTICE_HYBRID_PROVIDER_INVALID_VECTOR = "hybrid_provider_invalid_vector"
NOTICE_HYBRID_CANDIDATE_OVERFLOW = "hybrid_candidate_overflow"
NOTICE_HYBRID_VECTOR_BUDGET_EXCEEDED = "hybrid_vector_budget_exceeded"
NOTICE_HYBRID_VECTOR_COVERAGE_INCOMPLETE = "hybrid_vector_coverage_incomplete"
NOTICE_QUERY_TOO_LONG = "query_too_long"
NOTICE_QUERY_TOO_MANY_TERMS = "query_too_many_terms"
NOTICE_QUERY_HAS_NO_SEARCH_TERMS = "query_has_no_search_terms"
NOTICE_FTS_QUERY_TIMEOUT = "fts_query_timeout"


class VectorRebuildError(RuntimeError):
    """Raised when an explicit vector reindex could not produce a complete index."""


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


@dataclass(frozen=True, slots=True)
class _RankOutcome:
    """Completed lexical stages plus any runtime completeness notice."""

    entries: tuple[Entry, ...]
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
        outcome = self._rank_plan(request, plan)
        if NOTICE_FTS_QUERY_TIMEOUT in outcome.notices:
            return _timed_out_result((*plan.notices, *outcome.notices))
        return self.assemble(
            request,
            outcome.entries,
            notices=(*plan.notices, *outcome.notices),
        )

    def rank(self, request: RetrievalRequest) -> tuple[Entry, ...]:
        """Return direct visible matches in lexical rank order."""
        plan = self.query_plan(request.query)
        outcome = self._rank_plan(request, plan)
        if NOTICE_FTS_QUERY_TIMEOUT in outcome.notices:
            raise StoreQueryTimeoutError("FTS rank deadline expired")
        return outcome.entries

    def rank_with_notices(
        self,
        request: RetrievalRequest,
    ) -> tuple[tuple[Entry, ...], tuple[str, ...]]:
        """Return lexical rank plus every query/runtime completeness notice."""
        plan = self.query_plan(request.query)
        outcome = self._rank_plan(request, plan)
        notices = tuple(dict.fromkeys((*plan.notices, *outcome.notices)))
        return outcome.entries, notices

    def _rank_plan(
        self,
        request: RetrievalRequest,
        plan: _FtsQueryPlan,
    ) -> _RankOutcome:
        """Execute a prevalidated progressive plan."""
        if not plan.strict and not plan.fallback:
            return _RankOutcome(())
        deadline = self._store.begin_fts_query(self._config.fts_query_timeout_ms)
        strict_ranked: list[Entry] = []
        strict_seen: set[str] = set()
        for stage_index, expression in enumerate(plan.strict, start=1):
            try:
                ranked = self._search(request, expression, deadline=deadline)
            except StoreQueryTimeoutError:
                self._log_timeout("strict", stage_index)
                return _RankOutcome(
                    tuple(strict_ranked),
                    (NOTICE_FTS_QUERY_TIMEOUT,),
                )
            for entry in ranked:
                if entry.id not in strict_seen:
                    strict_ranked.append(entry)
                    strict_seen.add(entry.id)
                    if len(strict_ranked) == self._config.fts_top_k:
                        return _RankOutcome(tuple(strict_ranked))

        fallback_rankings: list[tuple[Entry, ...]] = []
        for stage_index, expression in enumerate(plan.fallback, start=1):
            try:
                fallback_rankings.append(self._search(request, expression, deadline=deadline))
            except StoreQueryTimeoutError:
                self._log_timeout("fallback", stage_index)
                return _RankOutcome(
                    _interleave_entries(
                        fallback_rankings,
                        self._config.fts_top_k,
                        initial=strict_ranked,
                    ),
                    (NOTICE_FTS_QUERY_TIMEOUT,),
                )
        return _RankOutcome(
            _interleave_entries(
                fallback_rankings,
                self._config.fts_top_k,
                initial=strict_ranked,
            )
        )

    def query_plan(self, query: str) -> _FtsQueryPlan:
        """Return safe FTS stages, or an empty plan for invalid input."""
        return _build_fts_query_plan(query, self._config)

    def _search(
        self,
        request: RetrievalRequest,
        expression: str,
        *,
        deadline: FtsQueryDeadline,
    ) -> tuple[Entry, ...]:
        """Execute one already-sanitized stage with all SQL visibility filters."""
        return self._store.search_fts(
            expression,
            scope=_normalized_scope(request.scope),
            kinds=request.kinds,
            writer_model=request.writer_model,
            limit=self._config.fts_top_k,
            deadline=deadline,
        )

    def _log_timeout(self, phase: str, stage_index: int) -> None:
        LOGGER.warning(
            "FTS plan deadline expired phase=%s stage=%d timeout_ms=%d",
            phase,
            stage_index,
            self._config.fts_query_timeout_ms,
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
        lexical, lexical_notices = self._fts.rank_with_notices(request)
        if NOTICE_FTS_QUERY_TIMEOUT in lexical_notices:
            return _timed_out_result(lexical_notices)
        try:
            query_vectors = self._provider.embed((request.query,))
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("Embedding endpoint unavailable; degrading to FTS: %s", exc)
            return self._fts.assemble(
                request,
                lexical,
                notices=(*lexical_notices, NOTICE_HYBRID_PROVIDER_UNAVAILABLE),
            )
        try:
            query_vector = query_vectors[0] if len(query_vectors) == 1 else None
            provider_result_is_valid = query_vector is not None and _is_usable_vector(query_vector)
        except Exception:  # noqa: BLE001
            query_vector = None
            provider_result_is_valid = False
        if not provider_result_is_valid or query_vector is None:
            LOGGER.warning("Embedding endpoint returned an unusable query vector; degrading to FTS")
            return self._fts.assemble(
                request,
                lexical,
                notices=(*lexical_notices, NOTICE_HYBRID_PROVIDER_INVALID_VECTOR),
            )
        try:
            eligible = self._store.list_retrieval_vectors(
                self._config.embeddings_model,
                scope=_normalized_scope(request.scope),
                kinds=request.kinds,
                writer_model=request.writer_model,
                limit=self._config.hybrid_max_candidates,
            )
        except StoreVectorBudgetError as exc:
            notice = (
                NOTICE_HYBRID_CANDIDATE_OVERFLOW
                if exc.reason == "candidate_count"
                else NOTICE_HYBRID_VECTOR_BUDGET_EXCEEDED
            )
            description = (
                "candidate cap exceeded"
                if exc.reason == "candidate_count"
                else "fixed vector budget exceeded"
            )
            LOGGER.warning(
                "Hybrid semantic scan omitted: %s reason=%s candidate_limit=%d",
                description,
                exc.reason,
                self._config.hybrid_max_candidates,
            )
            return self._fts.assemble(
                request,
                lexical,
                notices=(*lexical_notices, notice),
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
            ([entry.id for entry in lexical], semantic),
            self._config.rrf_k,
        )[: self._config.fts_top_k]
        revalidated = self._store.get_retrieval_entries(
            fused_ids,
            writer_model=request.writer_model,
        )
        entries_by_id = {entry.id: entry for entry in revalidated}
        fused = tuple(
            entries_by_id[entry_id] for entry_id in fused_ids if entry_id in entries_by_id
        )
        notices = (
            (*lexical_notices, NOTICE_HYBRID_VECTOR_COVERAGE_INCOMPLETE)
            if missing_vectors
            else lexical_notices
        )
        return self._fts.assemble(request, fused, notices=notices)

    def index_entry(self, entry: Entry) -> None:
        """Best-effort vectorize one committed entry without failing its write."""
        try:
            vector = self._provider.embed((_embedding_text(entry),))[0]
            self._store.upsert_vector(entry.id, self._config.embeddings_model, vector)
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("Entry %s stored without a vector: %s", entry.id, exc)

    def rebuild_vectors(self) -> int:
        """Stage bounded pages and swap one complete model atomically."""
        model = self._config.embeddings_model
        with self._store.write_access():
            self._store.begin_vector_rebuild(model)
            after_id: str | None = None
            rebuilt = 0
            expected_dimension: int | None = None
            try:
                while entries := self._store.list_entries_page(
                    after_id=after_id,
                    limit=MAX_EMBEDDING_BATCH_ITEMS,
                ):
                    try:
                        vectors = self._provider.embed(
                            tuple(_embedding_text(entry) for entry in entries)
                        )
                        expected_dimension = _validate_rebuild_batch(
                            entries,
                            vectors,
                            expected_dimension=expected_dimension,
                        )
                    except Exception as exc:
                        raise EmbeddingError(
                            "embedding provider failed during vector rebuild"
                        ) from exc
                    self._store.stage_vector_rebuild(
                        model,
                        {entry.id: vector for entry, vector in zip(entries, vectors, strict=True)},
                    )
                    rebuilt += len(entries)
                    after_id = entries[-1].id
                self._store.finish_vector_rebuild(model, expected_count=rebuilt)
            except EmbeddingError as exc:
                self._store.abort_vector_rebuild()
                raise VectorRebuildError(
                    "Vector rebuild failed; the existing vector index was retained"
                ) from exc
            except Exception:
                self._store.abort_vector_rebuild()
                raise
        LOGGER.info("Rebuilt %d vectors for model %s", rebuilt, model)
        return rebuilt


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
    eligible: Sequence[tuple[str, tuple[float, ...] | None]],
    query_vector: Sequence[float],
    *,
    limit: int,
) -> tuple[str, ...]:
    scored: list[tuple[float, int, str]] = []
    for order_index, (entry_id, vector) in enumerate(eligible):
        if vector is None or len(vector) != len(query_vector):
            continue
        similarity = _cosine_similarity(vector, query_vector)
        if similarity is not None and similarity > 0.0:
            scored.append((similarity, order_index, entry_id))
    scored.sort(key=lambda item: (-item[0], item[1], item[2]))
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


def _timed_out_result(notices: Sequence[str]) -> RetrievalResult:
    """Fail closed without any post-timeout database read or provider call."""
    return RetrievalResult(
        matches=(),
        next_actions=(),
        own_pending=(),
        notices=tuple(dict.fromkeys((*notices, NOTICE_FTS_QUERY_TIMEOUT))),
    )


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


def _validate_rebuild_batch(
    entries: Sequence[Entry],
    vectors: Sequence[Sequence[float]],
    *,
    expected_dimension: int | None,
) -> int:
    """Validate one provider batch and return its stable model dimension."""
    if len(vectors) != len(entries):
        raise EmbeddingError("embedding response count does not match the request")
    dimensions = {len(vector) for vector in vectors}
    if len(dimensions) != 1 or any(not _is_usable_vector(vector) for vector in vectors):
        raise EmbeddingError("embedding endpoint returned unusable vectors")
    batch_dimension = next(iter(dimensions))
    if expected_dimension is not None and batch_dimension != expected_dimension:
        raise EmbeddingError("embedding response dimensions are inconsistent")
    return batch_dimension


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
