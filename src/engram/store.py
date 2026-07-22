# Copyright 2026 Julien Bombled
# SPDX-License-Identifier: Apache-2.0

"""Single-writer storage API with transactional append-only auditing."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import secrets
import sqlite3
import struct
import unicodedata
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from threading import RLock
from types import TracebackType
from typing import Any, Literal, Self, cast, overload

from .config import AppConfig
from .db import (
    FTS_TABLE_NAME,
    open_database,
    open_database_read_only,
    rebuild_fts_index,
    transaction,
)
from .models import (
    AuditAction,
    AuditRecord,
    CandidateWriteResult,
    Confidence,
    Entry,
    EntryKind,
    EntryStatus,
    Evidence,
    EvidenceType,
    PromotionState,
    SourceType,
)

LOGGER = logging.getLogger(__name__)
SCOPE_PATTERN = re.compile(
    r"^(?:global|user|project/[a-z0-9][a-z0-9._-]*|session/[a-z0-9][a-z0-9._-]*)$"
)
ULID_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
PROCESS_WRITE_LOCK = RLock()


class StoreClosedError(RuntimeError):
    """Raised when an operation targets a closed store."""


class StoreValidationError(ValueError):
    """Raised when an entry hint violates the storage contract."""


class StoreBusyError(RuntimeError):
    """Raised when the bounded writer wait expires."""


@dataclass(frozen=True, slots=True)
class StoredConsolidationPlan:
    """Trusted immutable plan snapshot anchored outside the review artifact."""

    plan_id: str
    created_at: datetime
    snapshot_json: str
    snapshot_hash: str
    consumed_at: datetime | None


class EngramReader:
    """Read an existing database without migrations or SQLite write transactions."""

    def __init__(self, config: AppConfig) -> None:
        """Open the configured database in SQLite read-only mode."""
        self._connection = open_database_read_only(config.database)
        self._closed = False

    def __enter__(self) -> Self:
        """Return the open reader."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the reader when leaving a context manager."""
        del exc_type, exc_value, traceback
        self.close()

    def close(self) -> None:
        """Close the read-only SQLite connection."""
        if self._closed:
            return
        self._connection.close()
        self._closed = True

    def list_entries(self) -> tuple[Entry, ...]:
        """Return payload rows newest first."""
        if self._closed:
            raise StoreClosedError("Store is closed")
        rows = self._connection.execute(
            "SELECT * FROM entries ORDER BY recorded_at DESC, id DESC"
        ).fetchall()
        return tuple(_entry_from_row(row) for row in rows)


class EngramStore:
    """Serialize every application write through one in-process lock."""

    def __init__(
        self,
        config: AppConfig,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """Open the configured database and create the writer lock."""
        self._config = config
        self._clock = clock or _utc_now
        self._write_lock = PROCESS_WRITE_LOCK
        with self._write_lock:
            self._connection = open_database(config.database)
        self._closed = False

    def __enter__(self) -> Self:
        """Return the open store for context-manager use."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the store when leaving a context manager."""
        del exc_type, exc_value, traceback
        self.close()

    def close(self) -> None:
        """Close the underlying connection after pending writes complete."""
        with self._write_lock:
            if self._closed:
                return
            self._connection.close()
            self._closed = True

    @overload
    def add_candidate(
        self,
        *,
        kind: EntryKind | str,
        scope: str,
        statement: str,
        writer_model: str,
        confidence: Confidence | str = Confidence.MEDIUM,
        subject_keys: Sequence[str] = (),
        observed_at: datetime | None = None,
        valid_from: date | None = None,
        valid_until: date | None = None,
        evidence: Sequence[Evidence] = (),
        include_outcome: Literal[False] = False,
    ) -> Entry: ...

    @overload
    def add_candidate(
        self,
        *,
        kind: EntryKind | str,
        scope: str,
        statement: str,
        writer_model: str,
        confidence: Confidence | str = Confidence.MEDIUM,
        subject_keys: Sequence[str] = (),
        observed_at: datetime | None = None,
        valid_from: date | None = None,
        valid_until: date | None = None,
        evidence: Sequence[Evidence] = (),
        include_outcome: Literal[True],
    ) -> CandidateWriteResult: ...

    def add_candidate(  # noqa: PLR0913
        self,
        *,
        kind: EntryKind | str,
        scope: str,
        statement: str,
        writer_model: str,
        confidence: Confidence | str = Confidence.MEDIUM,
        subject_keys: Sequence[str] = (),
        observed_at: datetime | None = None,
        valid_from: date | None = None,
        valid_until: date | None = None,
        evidence: Sequence[Evidence] = (),
        include_outcome: bool = False,
    ) -> Entry | CandidateWriteResult:
        """Store an untrusted model inference with server-owned provenance fields."""
        actor = _required_text(writer_model, "writer_model")
        requested_confidence = _enum_value(Confidence, confidence, "confidence")
        effective_confidence, confidence_was_capped = _cap_confidence(
            SourceType.MODEL_INFERRED,
            requested_confidence,
        )

        entry, idempotent = self._add_entry(
            kind=kind,
            scope=scope,
            statement=statement,
            subject_keys=subject_keys,
            status=EntryStatus.QUARANTINED,
            promotion_state=PromotionState.CANDIDATE,
            source_type=SourceType.MODEL_INFERRED,
            writer_model=actor,
            confidence=effective_confidence,
            observed_at=observed_at,
            valid_from=valid_from,
            valid_until=valid_until,
            evidence=evidence,
            actor=actor,
            action=AuditAction.INSERT,
            confidence_was_capped=confidence_was_capped,
        )
        if include_outcome:
            return CandidateWriteResult(entry=entry, idempotent=idempotent)
        return entry

    def add_attested(  # noqa: PLR0913
        self,
        *,
        kind: EntryKind | str,
        scope: str,
        statement: str,
        source_type: SourceType | str,
        actor: str | None = None,
        confidence: Confidence | str = Confidence.HIGH,
        subject_keys: Sequence[str] = (),
        observed_at: datetime | None = None,
        valid_from: date | None = None,
        valid_until: date | None = None,
        evidence: Sequence[Evidence] = (),
    ) -> Entry:
        """Store a human or tool attestation through the trusted local path."""
        normalized_source = _enum_value(SourceType, source_type, "source_type")
        if normalized_source not in {SourceType.HUMAN, SourceType.TOOL_VERIFIED}:
            raise StoreValidationError(
                "add_attested only accepts human or tool_verified provenance"
            )
        effective_confidence, confidence_was_capped = _cap_confidence(
            normalized_source,
            _enum_value(Confidence, confidence, "confidence"),
        )
        entry, _ = self._add_entry(
            kind=kind,
            scope=scope,
            statement=statement,
            subject_keys=subject_keys,
            status=EntryStatus.ACTIVE,
            promotion_state=PromotionState.APPROVED,
            source_type=normalized_source,
            writer_model=None,
            confidence=effective_confidence,
            observed_at=observed_at,
            valid_from=valid_from,
            valid_until=valid_until,
            evidence=evidence,
            actor=_required_text(
                self._config.attestation.default_actor if actor is None else actor,
                "actor",
            ),
            action=AuditAction.ATTEST,
            confidence_was_capped=confidence_was_capped,
            attest_existing_candidate=True,
        )
        return entry

    @contextmanager
    def write_access(self, timeout_ms: int | None = None) -> Iterator[None]:
        """Acquire the process writer lock, optionally with a bounded wait."""
        if timeout_ms is not None and timeout_ms <= 0:
            raise StoreValidationError("timeout_ms must be greater than zero")
        acquired = (
            self._write_lock.acquire()
            if timeout_ms is None
            else self._write_lock.acquire(timeout=timeout_ms / 1000)
        )
        if not acquired:
            raise StoreBusyError("server busy, retry")
        try:
            self._ensure_open()
            yield
        finally:
            self._write_lock.release()

    def supersede(self, old_id: str, new_id: str, *, actor: str | None = None) -> None:
        """Mark the old entry superseded and link it from the replacement."""
        normalized_old_id = _required_text(old_id, "old_id")
        normalized_new_id = _required_text(new_id, "new_id")
        if normalized_old_id == normalized_new_id:
            raise StoreValidationError("An entry cannot supersede itself")
        normalized_actor = _required_text(
            self._config.attestation.default_actor if actor is None else actor,
            "actor",
        )

        with self._write_lock:
            self._ensure_open()
            now = self._now()
            with transaction(self._connection):
                old_row = self._fetch_entry_row(normalized_old_id)
                new_row = self._fetch_entry_row(normalized_new_id)
                if old_row is None:
                    raise KeyError(f"Entry does not exist: {normalized_old_id}")
                if new_row is None:
                    raise KeyError(f"Entry does not exist: {normalized_new_id}")

                supersedes = _decode_string_array(str(new_row["supersedes"]))
                if normalized_old_id not in supersedes:
                    supersedes.append(normalized_old_id)
                self._connection.execute(
                    "UPDATE entries SET status = ? WHERE id = ?",
                    (EntryStatus.SUPERSEDED.value, normalized_old_id),
                )
                # FTS indexes only immutable text; joined status filtering observes this update.
                self._connection.execute(
                    "UPDATE entries SET supersedes = ? WHERE id = ?",
                    (_encode_json(supersedes), normalized_new_id),
                )
                self._append_audit(
                    ts=now,
                    actor=normalized_actor,
                    action=AuditAction.SUPERSEDE,
                    entry_id=normalized_old_id,
                    detail={"new_entry_id": normalized_new_id},
                )
        LOGGER.info("Superseded entry %s with %s", normalized_old_id, normalized_new_id)

    def expire_due(self) -> int:
        """Logically expire every entry whose fixed TTL has elapsed."""
        with self._write_lock:
            self._ensure_open()
            now = self._now()
            now_text = _format_datetime(now)
            with transaction(self._connection):
                rows = self._connection.execute(
                    """
                    SELECT id FROM entries
                    WHERE status != ? AND expires_at IS NOT NULL AND expires_at <= ?
                    ORDER BY id
                    """,
                    (EntryStatus.EXPIRED.value, now_text),
                ).fetchall()
                entry_ids = [str(row["id"]) for row in rows]
                for entry_id in entry_ids:
                    self._connection.execute(
                        "UPDATE entries SET status = ? WHERE id = ?",
                        (EntryStatus.EXPIRED.value, entry_id),
                    )
                    # FTS indexes only immutable text; joined status filtering observes this update.
                    self._append_audit(
                        ts=now,
                        actor="cli",
                        action=AuditAction.EXPIRE,
                        entry_id=entry_id,
                        detail={"expired_at": now_text},
                    )
        if entry_ids:
            LOGGER.info("Expired %d entries", len(entry_ids))
        return len(entry_ids)

    def is_ttl_expired(self, entry: Entry) -> bool:
        """Return whether an entry is due according to this store's injected clock."""
        with self._write_lock:
            self._ensure_open()
            return entry.expires_at is not None and entry.expires_at <= self._now()

    def is_business_valid(self, entry: Entry) -> bool:
        """Return whether an entry is inside its inclusive business-validity window."""
        with self._write_lock:
            self._ensure_open()
            return _is_business_valid_on(entry, self._now().date())

    def purge_expired(self, older_than: datetime) -> int:
        """Physically remove expired payloads older than a required cutoff."""
        cutoff = _aware_datetime(older_than, "older_than")
        cutoff_text = _format_datetime(cutoff)
        with self._write_lock:
            self._ensure_open()
            now = self._now()
            with transaction(self._connection):
                rows = self._connection.execute(
                    """
                    SELECT rowid AS entry_rowid, id, statement, subject_keys FROM entries
                    WHERE status = ? AND expires_at IS NOT NULL AND expires_at <= ?
                    ORDER BY id
                    """,
                    (EntryStatus.EXPIRED.value, cutoff_text),
                ).fetchall()
                entry_ids = [str(row["id"]) for row in rows]
                for row in rows:
                    entry_id = str(row["id"])
                    self._delete_fts_row(row)
                    self._connection.execute("DELETE FROM entries WHERE id = ?", (entry_id,))
                    self._append_audit(
                        ts=now,
                        actor="cli",
                        action=AuditAction.PURGE,
                        entry_id=entry_id,
                        detail={"cutoff": cutoff_text},
                    )
        if entry_ids:
            LOGGER.info("Purged %d expired entries", len(entry_ids))
        return len(entry_ids)

    def get_entry(self, entry_id: str) -> Entry | None:
        """Return one entry by identifier."""
        with self._write_lock:
            self._ensure_open()
            row = self._fetch_entry_row(_required_text(entry_id, "entry_id"))
            return None if row is None else _entry_from_row(row)

    def count_entries(self) -> int:
        """Return the number of stored payload rows."""
        with self._write_lock:
            self._ensure_open()
            row = self._connection.execute("SELECT COUNT(*) FROM entries").fetchone()
            return 0 if row is None else int(row[0])

    def list_audit(self) -> tuple[AuditRecord, ...]:
        """Return the immutable audit stream in sequence order."""
        with self._write_lock:
            self._ensure_open()
            rows = self._connection.execute(
                "SELECT seq, ts, actor, action, entry_id, detail_hash FROM audit_log ORDER BY seq"
            ).fetchall()
            return tuple(_audit_from_row(row) for row in rows)

    def list_entries(self) -> tuple[Entry, ...]:
        """Return payload rows newest first."""
        with self._write_lock:
            self._ensure_open()
            rows = self._connection.execute(
                "SELECT * FROM entries ORDER BY recorded_at DESC, id DESC"
            ).fetchall()
            return tuple(_entry_from_row(row) for row in rows)

    def create_consolidation_plan(self, snapshot_json: str) -> StoredConsolidationPlan:
        """Persist one immutable consolidation snapshot and return its trusted identity."""
        normalized_snapshot = _required_text(snapshot_json, "snapshot_json")
        with self._write_lock:
            self._ensure_open()
            now = self._now()
            plan_id = _new_ulid(now)
            snapshot_hash = hashlib.sha256(normalized_snapshot.encode("utf-8")).hexdigest()
            with transaction(self._connection):
                self._connection.execute(
                    """
                    INSERT INTO consolidation_plans(
                        plan_id, created_at, snapshot_json, snapshot_hash, consumed_at
                    ) VALUES (?, ?, ?, ?, NULL)
                    """,
                    (
                        plan_id,
                        _format_datetime(now),
                        normalized_snapshot,
                        snapshot_hash,
                    ),
                )
            return StoredConsolidationPlan(
                plan_id=plan_id,
                created_at=now,
                snapshot_json=normalized_snapshot,
                snapshot_hash=snapshot_hash,
                consumed_at=None,
            )

    def get_consolidation_plan(self, plan_id: str) -> StoredConsolidationPlan | None:
        """Return one trusted plan snapshot without consuming it."""
        normalized_plan_id = _required_text(plan_id, "plan_id")
        with self._write_lock:
            self._ensure_open()
            row = self._connection.execute(
                "SELECT * FROM consolidation_plans WHERE plan_id = ?",
                (normalized_plan_id,),
            ).fetchone()
            return None if row is None else _consolidation_plan_from_row(row)

    def consume_consolidation_plan(
        self,
        plan_id: str,
        *,
        expected_hash: str,
    ) -> StoredConsolidationPlan:
        """Atomically consume one matching plan before any external write attempt."""
        normalized_plan_id = _required_text(plan_id, "plan_id")
        normalized_hash = _required_text(expected_hash, "expected_hash")
        with self._write_lock:
            self._ensure_open()
            now = self._now()
            with transaction(self._connection):
                row = self._connection.execute(
                    "SELECT * FROM consolidation_plans WHERE plan_id = ?",
                    (normalized_plan_id,),
                ).fetchone()
                if row is None:
                    raise StoreValidationError(
                        f"consolidation plan is unknown: {normalized_plan_id}; generate a new plan"
                    )
                stored = _consolidation_plan_from_row(row)
                actual_hash = hashlib.sha256(stored.snapshot_json.encode("utf-8")).hexdigest()
                if stored.snapshot_hash != normalized_hash or actual_hash != stored.snapshot_hash:
                    raise StoreValidationError(
                        "consolidation plan snapshot changed; generate a new plan"
                    )
                if stored.consumed_at is not None:
                    raise StoreValidationError(
                        f"consolidation plan was already consumed: {normalized_plan_id}; "
                        "generate a new plan"
                    )
                updated = self._connection.execute(
                    """
                    UPDATE consolidation_plans
                    SET consumed_at = ?
                    WHERE plan_id = ? AND consumed_at IS NULL
                    """,
                    (_format_datetime(now), normalized_plan_id),
                )
                if updated.rowcount != 1:
                    raise StoreValidationError(
                        f"consolidation plan could not be consumed: {normalized_plan_id}"
                    )
            return StoredConsolidationPlan(
                plan_id=stored.plan_id,
                created_at=stored.created_at,
                snapshot_json=stored.snapshot_json,
                snapshot_hash=stored.snapshot_hash,
                consumed_at=now,
            )

    def mark_promoted(
        self,
        entry_id: str,
        *,
        datacron_ref: str,
        datacron_hash: str,
        actor: str = "cli",
    ) -> Entry:
        """Record a verified Datacron promotion and its exact content hash."""
        normalized_entry_id = _required_text(entry_id, "entry_id")
        normalized_ref = _required_text(datacron_ref, "datacron_ref")
        normalized_hash = _required_text(datacron_hash, "datacron_hash")
        normalized_actor = _required_text(actor, "actor")
        with self._write_lock:
            self._ensure_open()
            now = self._now()
            with transaction(self._connection):
                row = self._fetch_entry_row(normalized_entry_id)
                if row is None:
                    raise KeyError(f"Entry does not exist: {normalized_entry_id}")
                entry = _entry_from_row(row)
                _require_promotable(entry)
                if not _is_business_valid_on(entry, now.date()):
                    raise StoreValidationError("candidate is outside its business validity window")
                self._connection.execute(
                    """
                    UPDATE entries
                    SET promotion_state = ?, datacron_ref = ?, datacron_hash = ?,
                        synced_at = ?, is_stale = 0
                    WHERE id = ?
                    """,
                    (
                        PromotionState.PROMOTED.value,
                        normalized_ref,
                        normalized_hash,
                        _format_datetime(now),
                        normalized_entry_id,
                    ),
                )
                self._append_audit(
                    ts=now,
                    actor=normalized_actor,
                    action=AuditAction.PROMOTE,
                    entry_id=normalized_entry_id,
                    detail={"datacron_hash": normalized_hash, "datacron_ref": normalized_ref},
                )
                updated_row = self._fetch_entry_row(normalized_entry_id)
                if updated_row is None:  # pragma: no cover - protected by the transaction
                    raise RuntimeError("Promoted entry disappeared")
                return _entry_from_row(updated_row)

    def set_stale(self, entry_id: str, *, stale: bool, actor: str = "cli") -> Entry:
        """Mark a promoted entry stale or fresh without rewriting Datacron."""
        normalized_entry_id = _required_text(entry_id, "entry_id")
        normalized_actor = _required_text(actor, "actor")
        with self._write_lock:
            self._ensure_open()
            now = self._now()
            with transaction(self._connection):
                row = self._fetch_entry_row(normalized_entry_id)
                if row is None:
                    raise KeyError(f"Entry does not exist: {normalized_entry_id}")
                entry = _entry_from_row(row)
                if entry.promotion_state is not PromotionState.PROMOTED:
                    raise StoreValidationError("Only promoted entries have freshness state")
                if entry.stale == stale:
                    return entry
                self._connection.execute(
                    "UPDATE entries SET is_stale = ? WHERE id = ?",
                    (int(stale), normalized_entry_id),
                )
                self._append_audit(
                    ts=now,
                    actor=normalized_actor,
                    action=AuditAction.MARK_STALE if stale else AuditAction.MARK_FRESH,
                    entry_id=normalized_entry_id,
                    detail={"stale": stale},
                )
                updated_row = self._fetch_entry_row(normalized_entry_id)
                if updated_row is None:  # pragma: no cover - protected by the transaction
                    raise RuntimeError("Freshness entry disappeared")
                return _entry_from_row(updated_row)

    def search_fts(
        self,
        match_query: str,
        *,
        scope: str | None,
        kinds: frozenset[EntryKind] | None,
    ) -> tuple[Entry, ...]:
        """Return active or quarantined FTS matches ordered by BM25 and recency."""
        normalized_query = _required_text(match_query, "match_query")
        now = self._now()
        today = now.date().isoformat()
        clauses = [
            f"{FTS_TABLE_NAME} MATCH ?",
            "entries.status IN (?, ?)",
            "(entries.expires_at IS NULL OR entries.expires_at > ?)",
            "(entries.valid_from IS NULL OR entries.valid_from <= ?)",
            "(entries.valid_until IS NULL OR entries.valid_until >= ?)",
        ]
        parameters: list[object] = [
            normalized_query,
            EntryStatus.ACTIVE.value,
            EntryStatus.QUARANTINED.value,
            _format_datetime(now),
            today,
            today,
        ]
        if scope is not None:
            clauses.append("entries.scope = ?")
            parameters.append(scope)
        if kinds:
            ordered_kinds = sorted(kind.value for kind in kinds)
            placeholders = ", ".join("?" for _ in ordered_kinds)
            clauses.append(f"entries.kind IN ({placeholders})")
            parameters.extend(ordered_kinds)
        where_clause = " AND ".join(clauses)
        query = (
            "SELECT entries.* FROM entries_fts "  # noqa: S608
            "JOIN entries ON entries.rowid = entries_fts.rowid "
            f"WHERE {where_clause} "
            "ORDER BY bm25(entries_fts), entries.recorded_at DESC, entries.id DESC"
        )
        with self._write_lock:
            self._ensure_open()
            rows = self._connection.execute(query, parameters).fetchall()
            return tuple(_entry_from_row(row) for row in rows)

    def rebuild_fts(self) -> None:
        """Reconstruct the derived FTS table from canonical entry rows."""
        with self._write_lock:
            self._ensure_open()
            rebuild_fts_index(self._connection)
        LOGGER.info("Rebuilt the FTS index")

    def upsert_vector(self, entry_id: str, model: str, vector: Sequence[float]) -> None:
        """Store one derived vector for an existing entry."""
        normalized_entry_id = _required_text(entry_id, "entry_id")
        normalized_model = _required_text(model, "model")
        encoded, dimension = _encode_vector(vector)
        with self._write_lock:
            self._ensure_open()
            with transaction(self._connection):
                self._connection.execute(
                    """
                    INSERT INTO entry_vectors(entry_id, model, dim, vector)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(entry_id, model) DO UPDATE SET
                        dim = excluded.dim,
                        vector = excluded.vector
                    """,
                    (normalized_entry_id, normalized_model, dimension, encoded),
                )

    def replace_vectors(
        self,
        model: str,
        vectors: Mapping[str, Sequence[float]],
    ) -> None:
        """Replace all derived vectors atomically for one embedding model."""
        normalized_model = _required_text(model, "model")
        encoded = {
            _required_text(entry_id, "entry_id"): _encode_vector(vector)
            for entry_id, vector in vectors.items()
        }
        with self._write_lock:
            self._ensure_open()
            with transaction(self._connection):
                self._connection.execute(
                    "DELETE FROM entry_vectors WHERE model = ?",
                    (normalized_model,),
                )
                self._connection.executemany(
                    """
                    INSERT INTO entry_vectors(entry_id, model, dim, vector)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        (entry_id, normalized_model, dimension, vector_blob)
                        for entry_id, (vector_blob, dimension) in encoded.items()
                    ),
                )

    def clear_vectors(self) -> None:
        """Clear every derived vector before a full configured rebuild."""
        with self._write_lock:
            self._ensure_open()
            with transaction(self._connection):
                self._connection.execute("DELETE FROM entry_vectors")

    def list_vectors(self, model: str) -> dict[str, tuple[float, ...]]:
        """Return derived vectors for one model keyed by entry identifier."""
        normalized_model = _required_text(model, "model")
        with self._write_lock:
            self._ensure_open()
            rows = self._connection.execute(
                "SELECT entry_id, dim, vector FROM entry_vectors WHERE model = ?",
                (normalized_model,),
            ).fetchall()
            return {
                str(row["entry_id"]): _decode_vector(bytes(row["vector"]), int(row["dim"]))
                for row in rows
            }

    def _add_entry(  # noqa: PLR0913
        self,
        *,
        kind: EntryKind | str,
        scope: str,
        statement: str,
        subject_keys: Sequence[str],
        status: EntryStatus,
        promotion_state: PromotionState,
        source_type: SourceType,
        writer_model: str | None,
        confidence: Confidence,
        observed_at: datetime | None,
        valid_from: date | None,
        valid_until: date | None,
        evidence: Sequence[Evidence],
        actor: str,
        action: AuditAction,
        confidence_was_capped: bool,
        attest_existing_candidate: bool = False,
    ) -> tuple[Entry, bool]:
        normalized_kind = _enum_value(EntryKind, kind, "kind")
        normalized_scope = _normalize_scope(scope)
        normalized_statement = _normalize_statement(
            statement, self._config.limits.max_statement_chars
        )
        normalized_subject_keys = _normalize_subject_keys(
            subject_keys, self._config.limits.max_subject_keys
        )
        normalized_evidence = _normalize_evidence(evidence)
        normalized_observed_at = (
            None if observed_at is None else _aware_datetime(observed_at, "observed_at")
        )
        _validate_date_range(valid_from, valid_until)
        idempotency_key = _idempotency_key(normalized_kind, normalized_scope, normalized_statement)

        with self._write_lock:
            self._ensure_open()
            now = self._now()
            with transaction(self._connection):
                existing_row = self._connection.execute(
                    "SELECT rowid AS entry_rowid, * FROM entries WHERE idempotency_key = ?",
                    (idempotency_key,),
                ).fetchone()
                if existing_row is not None:
                    existing = _entry_from_row(existing_row)
                    if attest_existing_candidate and _is_attestable_candidate(existing):
                        if existing.expires_at is not None and existing.expires_at <= now:
                            raise StoreValidationError("An expired candidate cannot be attested")
                        updated = self._attest_existing_candidate(
                            row=existing_row,
                            existing=existing,
                            subject_keys=normalized_subject_keys,
                            source_type=source_type,
                            confidence=confidence,
                            observed_at=normalized_observed_at,
                            valid_from=valid_from,
                            valid_until=valid_until,
                            evidence=normalized_evidence,
                        )
                        self._append_audit(
                            ts=now,
                            actor=actor,
                            action=AuditAction.ATTEST,
                            entry_id=updated.id,
                            detail=_entry_detail(updated),
                        )
                        return updated, False
                    if attest_existing_candidate and not _is_trusted_active(existing):
                        raise StoreValidationError(
                            "Canonical content already exists in a non-attestable lifecycle state"
                        )
                    self._append_audit(
                        ts=now,
                        actor=actor,
                        action=AuditAction.IDEMPOTENT_NOOP,
                        entry_id=existing.id,
                        detail={"idempotency_key": idempotency_key},
                    )
                    return existing, True

                ttl_days = self._config.ttl_days.for_kind(normalized_kind)
                expires_at = None if ttl_days == 0 else now + timedelta(days=ttl_days)
                entry = Entry(
                    id=_new_ulid(now),
                    kind=normalized_kind,
                    scope=normalized_scope,
                    statement=normalized_statement,
                    subject_keys=normalized_subject_keys,
                    status=status,
                    promotion_state=promotion_state,
                    source_type=source_type,
                    writer_model=writer_model,
                    confidence=confidence,
                    observed_at=normalized_observed_at,
                    recorded_at=now,
                    valid_from=valid_from,
                    valid_until=valid_until,
                    expires_at=expires_at,
                    idempotency_key=idempotency_key,
                    supersedes=(),
                    evidence=normalized_evidence,
                    stale=False,
                    datacron_ref=None,
                    datacron_hash=None,
                    synced_at=None,
                )
                self._insert_entry(entry)
                self._append_audit(
                    ts=now,
                    actor=actor,
                    action=action,
                    entry_id=entry.id,
                    detail=_entry_detail(entry),
                )
                if confidence_was_capped:
                    self._append_audit(
                        ts=now,
                        actor=actor,
                        action=AuditAction.CONFIDENCE_CAPPED,
                        entry_id=entry.id,
                        detail={
                            "requested": Confidence.HIGH.value,
                            "stored": Confidence.MEDIUM.value,
                        },
                    )
        LOGGER.info("Stored %s entry %s", entry.kind.value, entry.id)
        return entry, False

    def _attest_existing_candidate(  # noqa: PLR0913
        self,
        *,
        row: sqlite3.Row,
        existing: Entry,
        subject_keys: tuple[str, ...],
        source_type: SourceType,
        confidence: Confidence,
        observed_at: datetime | None,
        valid_from: date | None,
        valid_until: date | None,
        evidence: tuple[Evidence, ...],
    ) -> Entry:
        """Promote matching canonical candidate content without duplicating its identity."""
        updated_subject_keys = subject_keys or existing.subject_keys
        updated_evidence = evidence or existing.evidence
        updated_observed_at = observed_at or existing.observed_at
        updated_valid_from = valid_from or existing.valid_from
        updated_valid_until = valid_until or existing.valid_until
        _validate_date_range(updated_valid_from, updated_valid_until)
        encoded_subject_keys = _encode_json(list(updated_subject_keys))
        self._delete_fts_row(row)
        self._connection.execute(
            """
            UPDATE entries
            SET subject_keys = ?, status = ?, promotion_state = ?, source_type = ?,
                writer_model = NULL, confidence = ?, observed_at = ?, valid_from = ?,
                valid_until = ?, evidence = ?
            WHERE id = ?
            """,
            (
                encoded_subject_keys,
                EntryStatus.ACTIVE.value,
                PromotionState.APPROVED.value,
                source_type.value,
                confidence.value,
                _format_optional_datetime(updated_observed_at),
                None if updated_valid_from is None else updated_valid_from.isoformat(),
                None if updated_valid_until is None else updated_valid_until.isoformat(),
                _encode_evidence(updated_evidence),
                existing.id,
            ),
        )
        self._connection.execute(
            "INSERT INTO entries_fts(rowid, statement, subject_keys) VALUES (?, ?, ?)",
            (int(row["entry_rowid"]), existing.statement, encoded_subject_keys),
        )
        updated_row = self._fetch_entry_row(existing.id)
        if updated_row is None:  # pragma: no cover - protected by the transaction
            raise RuntimeError("Attested candidate disappeared")
        return _entry_from_row(updated_row)

    def _insert_entry(self, entry: Entry) -> None:
        encoded_subject_keys = _encode_json(list(entry.subject_keys))
        cursor = self._connection.execute(
            """
            INSERT INTO entries (
                id, kind, scope, statement, subject_keys, status, promotion_state,
                source_type, writer_model, confidence, observed_at, recorded_at,
                valid_from, valid_until, expires_at, idempotency_key, supersedes,
                evidence, is_stale, datacron_ref, datacron_hash, synced_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                entry.id,
                entry.kind.value,
                entry.scope,
                entry.statement,
                encoded_subject_keys,
                entry.status.value,
                entry.promotion_state.value,
                entry.source_type.value,
                entry.writer_model,
                entry.confidence.value,
                _format_optional_datetime(entry.observed_at),
                _format_datetime(entry.recorded_at),
                None if entry.valid_from is None else entry.valid_from.isoformat(),
                None if entry.valid_until is None else entry.valid_until.isoformat(),
                _format_optional_datetime(entry.expires_at),
                entry.idempotency_key,
                _encode_json(list(entry.supersedes)),
                _encode_evidence(entry.evidence),
                int(entry.stale),
                entry.datacron_ref,
                entry.datacron_hash,
                _format_optional_datetime(entry.synced_at),
            ),
        )
        rowid = cursor.lastrowid
        if rowid is None:
            raise RuntimeError("SQLite did not return an entry rowid")
        self._connection.execute(
            "INSERT INTO entries_fts(rowid, statement, subject_keys) VALUES (?, ?, ?)",
            (rowid, entry.statement, encoded_subject_keys),
        )

    def _delete_fts_row(self, row: sqlite3.Row) -> None:
        self._connection.execute(
            """
            INSERT INTO entries_fts(
                entries_fts, rowid, statement, subject_keys
            ) VALUES ('delete', ?, ?, ?)
            """,
            (int(row["entry_rowid"]), str(row["statement"]), str(row["subject_keys"])),
        )

    def _append_audit(
        self,
        *,
        ts: datetime,
        actor: str,
        action: AuditAction,
        entry_id: str | None,
        detail: dict[str, object],
    ) -> None:
        detail_hash = hashlib.sha256(_encode_json(detail).encode("utf-8")).hexdigest()
        self._connection.execute(
            "INSERT INTO audit_log(ts, actor, action, entry_id, detail_hash) "
            "VALUES (?, ?, ?, ?, ?)",
            (_format_datetime(ts), actor, action.value, entry_id, detail_hash),
        )

    def _fetch_entry_row(self, entry_id: str) -> sqlite3.Row | None:
        row = self._connection.execute("SELECT * FROM entries WHERE id = ?", (entry_id,)).fetchone()
        return cast("sqlite3.Row | None", row)

    def _ensure_open(self) -> None:
        if self._closed:
            raise StoreClosedError("EngramStore is closed")

    def _now(self) -> datetime:
        return _aware_datetime(self._clock(), "clock result")


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _encode_vector(vector: Sequence[float]) -> tuple[bytes, int]:
    values = tuple(float(value) for value in vector)
    if not values:
        raise StoreValidationError("vector must not be empty")
    if not all(math.isfinite(value) for value in values):
        raise StoreValidationError("vector values must be finite")
    return struct.pack(f"<{len(values)}f", *values), len(values)


def _decode_vector(vector_blob: bytes, dimension: int) -> tuple[float, ...]:
    if dimension <= 0 or len(vector_blob) != dimension * 4:
        raise StoreValidationError("stored vector has an invalid dimension")
    return tuple(struct.unpack(f"<{dimension}f", vector_blob))


def _enum_value[EnumType: StrEnum](
    enum_class: type[EnumType], value: EnumType | str, field_name: str
) -> EnumType:
    try:
        return enum_class(value)
    except (TypeError, ValueError) as exc:
        raise StoreValidationError(f"Invalid {field_name}: {value}") from exc


def _required_text(value: object, field_name: str) -> str:
    if not isinstance(value, str):
        raise StoreValidationError(f"{field_name} must be a string")
    normalized = value.strip()
    if not normalized:
        raise StoreValidationError(f"{field_name} must not be empty")
    return normalized


def _normalize_scope(scope: str) -> str:
    normalized = _required_text(scope, "scope").casefold()
    if SCOPE_PATTERN.fullmatch(normalized) is None:
        raise StoreValidationError(f"Invalid scope: {scope}")
    return normalized


def _normalize_statement(statement: str, maximum_chars: int) -> str:
    normalized = " ".join(unicodedata.normalize("NFKC", statement).split())
    if not normalized:
        raise StoreValidationError("statement must not be empty")
    if len(normalized) > maximum_chars:
        raise StoreValidationError(
            f"statement exceeds configured limit of {maximum_chars} characters"
        )
    return normalized


def _normalize_subject_keys(values: Sequence[str], maximum_keys: int) -> tuple[str, ...]:
    if isinstance(values, str):
        raise StoreValidationError("subject_keys must be a sequence of strings")
    normalized: list[str] = []
    for value in values:
        item = _required_text(value, "subject key").casefold()
        if item not in normalized:
            normalized.append(item)
    if len(normalized) > maximum_keys:
        raise StoreValidationError(f"subject_keys exceeds configured limit of {maximum_keys} items")
    return tuple(normalized)


def _normalize_evidence(values: Sequence[Evidence]) -> tuple[Evidence, ...]:
    normalized: list[Evidence] = []
    for evidence in values:
        if not isinstance(evidence, Evidence):
            raise StoreValidationError("evidence items must be Evidence instances")
        reference = _required_text(evidence.ref, "evidence ref")
        evidence_type = _enum_value(EvidenceType, evidence.type, "evidence type")
        normalized.append(Evidence(type=evidence_type, ref=reference))
    return tuple(normalized)


def _validate_date_range(valid_from: date | None, valid_until: date | None) -> None:
    if isinstance(valid_from, datetime) or isinstance(valid_until, datetime):
        raise StoreValidationError("valid_from and valid_until must be dates, not datetimes")
    if valid_from is not None and valid_until is not None and valid_until < valid_from:
        raise StoreValidationError("valid_until must not be earlier than valid_from")


def _aware_datetime(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise StoreValidationError(f"{field_name} must include a timezone")
    return value.astimezone(UTC)


def _format_datetime(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _format_optional_datetime(value: datetime | None) -> str | None:
    return None if value is None else _format_datetime(value)


def _parse_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)


def _parse_optional_datetime(value: object) -> datetime | None:
    return None if value is None else _parse_datetime(str(value))


def _parse_optional_date(value: object) -> date | None:
    return None if value is None else date.fromisoformat(str(value))


def _idempotency_key(kind: EntryKind, scope: str, statement: str) -> str:
    payload = [kind.value, scope, statement.casefold()]
    return hashlib.sha256(_encode_json(payload).encode("utf-8")).hexdigest()


def _new_ulid(recorded_at: datetime) -> str:
    timestamp_ms = int(recorded_at.timestamp() * 1000)
    if not 0 <= timestamp_ms < 2**48:
        raise StoreValidationError("clock result is outside the ULID timestamp range")
    value = int.from_bytes(timestamp_ms.to_bytes(6, "big") + secrets.token_bytes(10), "big")
    characters: list[str] = []
    for _ in range(26):
        characters.append(ULID_ALPHABET[value & 31])
        value >>= 5
    return "".join(reversed(characters))


def _encode_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _encode_evidence(evidence: tuple[Evidence, ...]) -> str:
    return _encode_json([{"ref": item.ref, "type": item.type.value} for item in evidence])


def _decode_string_array(value: str) -> list[str]:
    decoded: Any = json.loads(value)
    if not isinstance(decoded, list) or not all(isinstance(item, str) for item in decoded):
        raise RuntimeError("Stored JSON value is not a string array")
    return cast("list[str]", decoded)


def _decode_evidence(value: str) -> tuple[Evidence, ...]:
    decoded: Any = json.loads(value)
    if not isinstance(decoded, list):
        raise RuntimeError("Stored evidence is not an array")
    result: list[Evidence] = []
    for item in decoded:
        if not isinstance(item, dict):
            raise RuntimeError("Stored evidence item is not an object")
        evidence_type = item.get("type")
        reference = item.get("ref")
        if not isinstance(evidence_type, str) or not isinstance(reference, str):
            raise RuntimeError("Stored evidence item is invalid")
        result.append(Evidence(type=EvidenceType(evidence_type), ref=reference))
    return tuple(result)


def _entry_from_row(row: sqlite3.Row) -> Entry:
    return Entry(
        id=str(row["id"]),
        kind=EntryKind(str(row["kind"])),
        scope=str(row["scope"]),
        statement=str(row["statement"]),
        subject_keys=tuple(_decode_string_array(str(row["subject_keys"]))),
        status=EntryStatus(str(row["status"])),
        promotion_state=PromotionState(str(row["promotion_state"])),
        source_type=SourceType(str(row["source_type"])),
        writer_model=None if row["writer_model"] is None else str(row["writer_model"]),
        confidence=Confidence(str(row["confidence"])),
        observed_at=_parse_optional_datetime(row["observed_at"]),
        recorded_at=_parse_datetime(str(row["recorded_at"])),
        valid_from=_parse_optional_date(row["valid_from"]),
        valid_until=_parse_optional_date(row["valid_until"]),
        expires_at=_parse_optional_datetime(row["expires_at"]),
        idempotency_key=str(row["idempotency_key"]),
        supersedes=tuple(_decode_string_array(str(row["supersedes"]))),
        evidence=_decode_evidence(str(row["evidence"])),
        stale=bool(int(row["is_stale"])),
        datacron_ref=None if row["datacron_ref"] is None else str(row["datacron_ref"]),
        datacron_hash=None if row["datacron_hash"] is None else str(row["datacron_hash"]),
        synced_at=_parse_optional_datetime(row["synced_at"]),
    )


def _audit_from_row(row: sqlite3.Row) -> AuditRecord:
    return AuditRecord(
        seq=int(row["seq"]),
        ts=_parse_datetime(str(row["ts"])),
        actor=str(row["actor"]),
        action=AuditAction(str(row["action"])),
        entry_id=None if row["entry_id"] is None else str(row["entry_id"]),
        detail_hash=None if row["detail_hash"] is None else str(row["detail_hash"]),
    )


def _consolidation_plan_from_row(row: sqlite3.Row) -> StoredConsolidationPlan:
    return StoredConsolidationPlan(
        plan_id=str(row["plan_id"]),
        created_at=_parse_datetime(str(row["created_at"])),
        snapshot_json=str(row["snapshot_json"]),
        snapshot_hash=str(row["snapshot_hash"]),
        consumed_at=_parse_optional_datetime(row["consumed_at"]),
    )


def _entry_detail(entry: Entry) -> dict[str, object]:
    return {
        "confidence": entry.confidence.value,
        "expires_at": _format_optional_datetime(entry.expires_at),
        "idempotency_key": entry.idempotency_key,
        "kind": entry.kind.value,
        "promotion_state": entry.promotion_state.value,
        "scope": entry.scope,
        "stale": entry.stale,
        "source_type": entry.source_type.value,
        "statement_hash": hashlib.sha256(entry.statement.encode("utf-8")).hexdigest(),
        "status": entry.status.value,
    }


def _cap_confidence(
    source_type: SourceType,
    requested: Confidence,
) -> tuple[Confidence, bool]:
    if (
        source_type in {SourceType.MODEL_INFERRED, SourceType.SESSION_SUMMARY}
        and requested is Confidence.HIGH
    ):
        return Confidence.MEDIUM, True
    return requested, False


def _is_attestable_candidate(entry: Entry) -> bool:
    return (
        entry.status is EntryStatus.QUARANTINED
        and entry.promotion_state is PromotionState.CANDIDATE
        and entry.source_type is SourceType.MODEL_INFERRED
    )


def _is_trusted_active(entry: Entry) -> bool:
    return (
        entry.status is EntryStatus.ACTIVE
        and entry.promotion_state in {PromotionState.APPROVED, PromotionState.PROMOTED}
        and entry.source_type in {SourceType.HUMAN, SourceType.TOOL_VERIFIED}
    )


def _require_promotable(entry: Entry) -> None:
    if entry.status is not EntryStatus.ACTIVE:
        raise StoreValidationError("Only active entries can be promoted")
    if entry.promotion_state is not PromotionState.APPROVED:
        raise StoreValidationError("Only approved entries can be promoted")
    if entry.source_type not in {SourceType.HUMAN, SourceType.TOOL_VERIFIED}:
        raise StoreValidationError("Only human or tool_verified entries can be promoted")


def _is_business_valid_on(entry: Entry, today: date) -> bool:
    return (entry.valid_from is None or entry.valid_from <= today) and (
        entry.valid_until is None or today <= entry.valid_until
    )
