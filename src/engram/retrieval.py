# Copyright 2026 Julien Bombled
# SPDX-License-Identifier: Apache-2.0

"""Replaceable retrieval interface and naive substring implementation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .models import Entry, EntryKind, EntryStatus
from .store import EngramStore


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
    """Interface that OM-03 can replace without changing capsule policy."""

    def retrieve(self, request: RetrievalRequest) -> RetrievalResult:
        """Return candidates for capsule construction."""
        ...


class NaiveRetriever:
    """Filter entries and apply case-insensitive substring matching."""

    def __init__(self, store: EngramStore) -> None:
        """Bind the retriever to the read side of one store."""
        self._store = store

    def retrieve(self, request: RetrievalRequest) -> RetrievalResult:
        """Return newest matching rows without assigning capsule sections."""
        normalized_query = " ".join(request.query.split()).casefold()
        normalized_scope = None if request.scope is None else request.scope.strip().casefold()
        eligible: list[Entry] = []
        direct_matches: list[Entry] = []
        next_actions: list[Entry] = []
        own_pending: list[Entry] = []

        for entry in self._store.list_entries():
            if normalized_scope is not None and entry.scope != normalized_scope:
                continue
            if request.kinds is not None and entry.kind not in request.kinds:
                continue
            if entry.status is EntryStatus.EXPIRED:
                continue
            eligible.append(entry)

            query_matches = _matches_query(entry, normalized_query)
            if (
                entry.status is EntryStatus.ACTIVE
                and entry.kind is EntryKind.PROJECT_STATE
                and (normalized_scope is not None or query_matches)
            ):
                next_actions.append(entry)

            if entry.status is EntryStatus.QUARANTINED:
                if entry.writer_model == request.writer_model and query_matches:
                    own_pending.append(entry)
                continue
            if query_matches and entry.status in {EntryStatus.ACTIVE, EntryStatus.SUPERSEDED}:
                direct_matches.append(entry)

        related_subjects = {
            subject_key
            for entry in direct_matches
            if entry.status is EntryStatus.ACTIVE
            for subject_key in entry.subject_keys
        }
        direct_ids = {entry.id for entry in direct_matches}
        related_ids = {
            entry.id
            for entry in eligible
            if entry.status is EntryStatus.ACTIVE
            and related_subjects.intersection(entry.subject_keys)
        }
        match_ids = direct_ids | related_ids
        matches = [entry for entry in eligible if entry.id in match_ids]

        return RetrievalResult(
            matches=tuple(matches),
            next_actions=tuple(next_actions),
            own_pending=tuple(own_pending),
        )


def _matches_query(entry: Entry, normalized_query: str) -> bool:
    if not normalized_query:
        return False
    searchable = " ".join((entry.statement, *entry.subject_keys)).casefold()
    return normalized_query in searchable
