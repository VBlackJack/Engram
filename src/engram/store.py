# Copyright 2026 Julien Bombled
# SPDX-License-Identifier: Apache-2.0

"""Single-writer storage API with transactional append-only auditing."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import secrets
import sqlite3
import struct
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from threading import RLock
from types import TracebackType
from typing import Any, Literal, Self, cast, overload

from .config import (
    MAX_FTS_QUERY_TIMEOUT_MS,
    MAX_FTS_TOP_K,
    MAX_HYBRID_CANDIDATES,
    MIN_FTS_QUERY_TIMEOUT_MS,
    AppConfig,
)
from .db import (
    FTS_TABLE_NAME,
    MAX_CONSOLIDATION_SNAPSHOT_BYTES,
    open_database,
    open_database_read_only,
    rebuild_fts_index,
    transaction,
    verify_database_integrity,
)
from .models import (
    PROJECT_STATE_CLAIM_KEY,
    AuditAction,
    AuditRecord,
    CandidateWriteResult,
    Confidence,
    Entry,
    EntryKind,
    EntryObservation,
    EntryStatus,
    Evidence,
    EvidenceType,
    PromotionState,
    RememberOutcome,
    SourceType,
)
from .normalization import (
    MAX_EVIDENCE_ITEMS,
    StoreValidationError,
    validate_persisted_content,
)
from .normalization import (
    bounded_text as _bounded_text,
)
from .normalization import (
    canonical_key as _canonical_key,
)
from .normalization import (
    generation_key as _generation_key,
)
from .normalization import (
    normalize_actor as _normalize_actor,
)
from .normalization import (
    normalize_datacron_ref as _normalize_datacron_ref,
)
from .normalization import (
    normalize_embedding_model as _normalize_embedding_model,
)
from .normalization import (
    normalize_entry_id as _normalize_entry_id,
)
from .normalization import (
    normalize_evidence_ref as _normalize_evidence_ref,
)
from .normalization import (
    normalize_scope as _normalize_scope,
)
from .normalization import (
    normalize_sha256_hex as _normalize_sha256_hex,
)
from .normalization import (
    normalize_statement as _normalize_statement,
)
from .normalization import (
    normalize_subject_keys as _normalize_subject_keys,
)
from .normalization import (
    normalize_trusted_claim_key as _normalize_trusted_claim_key,
)
from .normalization import (
    normalize_writer_model as _normalize_writer_model,
)
from .normalization import (
    required_text as _required_text,
)
from .process_lock import DatabaseLockRole, DatabaseProcessLock
from .vectors import FLOAT32_MAX, MAX_VECTOR_DIMENSIONS

__all__ = [
    "EngramReader",
    "EngramStore",
    "FtsQueryDeadline",
    "StoreBusyError",
    "StoreClosedError",
    "StoreQueryTimeoutError",
    "StoreValidationError",
    "StoreVectorBudgetError",
]

LOGGER = logging.getLogger(__name__)
ULID_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
PROCESS_WRITE_LOCK = RLock()
SQLITE_PROGRESS_HANDLER_STEPS = 1000
MAX_FTS_EXPRESSION_CHARS = 8192
MAX_HYBRID_VECTOR_SCAN_VALUES = 262_144
MAX_HYBRID_VECTOR_SCAN_BYTES = MAX_HYBRID_VECTOR_SCAN_VALUES * 4
MAX_VECTOR_REBUILD_BATCH_ITEMS = 64
VECTOR_REBUILD_STAGE_TABLE = "engram_vector_rebuild_stage"


class StoreClosedError(RuntimeError):
    """Raised when an operation targets a closed store."""


class StoreBusyError(RuntimeError):
    """Raised when the bounded writer wait expires."""


class StoreQueryTimeoutError(RuntimeError):
    """Raised when one absolute FTS plan deadline expires."""


class StoreVectorBudgetError(RuntimeError):
    """Raised before a hybrid scan would exceed a fixed memory budget."""

    def __init__(
        self,
        reason: Literal["candidate_count", "vector_values", "vector_bytes"],
    ) -> None:
        """Retain one stable machine-readable overflow reason."""
        super().__init__(f"hybrid vector scan exceeded {reason} budget")
        self.reason = reason


@dataclass(frozen=True, slots=True)
class FtsQueryDeadline:
    """Monotone absolute deadline shared by every stage of one FTS plan."""

    expires_at: float


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
        self._connection = open_database_read_only(
            config.database,
            limits=config.limits,
        )
        self._limits = config.limits
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
        return tuple(
            _entry_from_row(
                row,
                max_statement_chars=self._limits.max_statement_chars,
                max_subject_keys=self._limits.max_subject_keys,
            )
            for row in rows
        )


class EngramStore:
    """Own the single cross-process writer lease and serialize local writes."""

    def __init__(
        self,
        config: AppConfig,
        *,
        clock: Callable[[], datetime] | None = None,
        monotonic_clock: Callable[[], float] | None = None,
        process_lock: DatabaseProcessLock | None = None,
    ) -> None:
        """Open the database under an owned or caller-provided writer lease."""
        self._config = config
        self._clock = clock or _utc_now
        self._monotonic_clock = monotonic_clock or time.monotonic
        self._write_lock = PROCESS_WRITE_LOCK
        self._process_lock = process_lock or DatabaseProcessLock(
            config.database.path,
            role=DatabaseLockRole.WRITER,
            command="library",
        )
        self._owns_process_lock = process_lock is None
        if self._owns_process_lock:
            self._process_lock.acquire()
        elif not self._process_lock.covers(config.database.path):
            raise RuntimeError("Provided database process lock is not acquired for this database")
        try:
            with self._write_lock:
                self._connection = open_database(
                    config.database,
                    limits=config.limits,
                )
        except BaseException:
            if self._owns_process_lock:
                self._process_lock.release()
            raise
        self._vector_rebuild_model: str | None = None
        self._vector_rebuild_data_version: int | None = None
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
            try:
                self._connection.close()
            finally:
                self._closed = True
                if self._owns_process_lock:
                    self._process_lock.release()

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
        actor = _normalize_writer_model(writer_model)
        requested_confidence = _enum_value(Confidence, confidence, "confidence")
        effective_confidence, confidence_was_capped = _cap_confidence(
            SourceType.MODEL_INFERRED,
            requested_confidence,
        )

        entry, idempotent, outcome = self._add_entry(
            kind=kind,
            scope=scope,
            statement=statement,
            subject_keys=subject_keys,
            claim_key=None,
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
            return CandidateWriteResult(
                entry=entry,
                idempotent=idempotent,
                outcome=outcome,
            )
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
        claim_key: str | None = None,
        supersedes: Sequence[str] = (),
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
        entry, _, _ = self._add_entry(
            kind=kind,
            scope=scope,
            statement=statement,
            subject_keys=subject_keys,
            claim_key=claim_key,
            status=EntryStatus.ACTIVE,
            promotion_state=PromotionState.APPROVED,
            source_type=normalized_source,
            writer_model=None,
            confidence=effective_confidence,
            observed_at=observed_at,
            valid_from=valid_from,
            valid_until=valid_until,
            evidence=evidence,
            actor=_normalize_actor(
                self._config.attestation.default_actor if actor is None else actor,
            ),
            action=AuditAction.ATTEST,
            confidence_was_capped=confidence_was_capped,
            attest_existing_candidate=True,
            supersedes=supersedes,
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
        """Atomically retire one active trusted entry behind a trusted replacement."""
        normalized_old_id = _normalize_entry_id(old_id, "old_id")
        normalized_new_id = _normalize_entry_id(new_id, "new_id")
        if normalized_old_id == normalized_new_id:
            raise StoreValidationError("An entry cannot supersede itself")
        normalized_actor = _normalize_actor(
            self._config.attestation.default_actor if actor is None else actor,
        )

        with self._write_lock:
            self._ensure_open()
            now = self._now()
            with transaction(self._connection):
                self._supersede_in_transaction(
                    old_id=normalized_old_id,
                    new_id=normalized_new_id,
                    actor=normalized_actor,
                    now=now,
                    allow_candidate_old=False,
                    classify_legacy_old=False,
                )
        LOGGER.info("Superseded entry %s with %s", normalized_old_id, normalized_new_id)

    def classify_claim(
        self,
        entry_id: str,
        claim_key: str,
        *,
        actor: str | None = None,
    ) -> Entry:
        """Classify one active trusted legacy entry without changing its content."""
        normalized_entry_id = _normalize_entry_id(entry_id)
        normalized_actor = _normalize_actor(
            self._config.attestation.default_actor if actor is None else actor,
        )
        with self._write_lock:
            self._ensure_open()
            now = self._now()
            with transaction(self._connection):
                row = self._fetch_entry_row(normalized_entry_id)
                if row is None:
                    raise StoreValidationError(
                        f"Entry to classify does not exist: {normalized_entry_id}"
                    )
                entry = self._materialize_entry(row)
                if not _is_trusted_active(entry):
                    raise StoreValidationError(
                        "Only an active trusted legacy entry can be classified"
                    )
                normalized_claim_key = _normalize_trusted_claim_key(
                    entry.kind,
                    claim_key,
                )
                if normalized_claim_key is None:
                    raise StoreValidationError(
                        "Episode entries do not require claim classification"
                    )
                if entry.claim_key is not None:
                    if entry.claim_key == normalized_claim_key:
                        self._append_audit(
                            ts=now,
                            actor=normalized_actor,
                            action=AuditAction.IDEMPOTENT_NOOP,
                            entry_id=normalized_entry_id,
                            detail={"claim_key": normalized_claim_key},
                        )
                        return entry
                    raise StoreValidationError("Entry already has a different claim_key")
                self._connection.execute(
                    "UPDATE entries SET claim_key = ? WHERE id = ?",
                    (normalized_claim_key, normalized_entry_id),
                )
                self._append_audit(
                    ts=now,
                    actor=normalized_actor,
                    action=AuditAction.CLASSIFY,
                    entry_id=normalized_entry_id,
                    detail={"claim_key": normalized_claim_key},
                )
                updated_row = self._fetch_entry_row(normalized_entry_id)
                if updated_row is None:  # pragma: no cover - transaction invariant
                    raise RuntimeError("Classified entry disappeared")
                return self._materialize_entry(updated_row)

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
                purge_ids = set(entry_ids)
                blocking_link = self._connection.execute(
                    """
                    SELECT old_entry_id, new_entry_id
                    FROM entry_supersessions
                    ORDER BY old_entry_id
                    """
                ).fetchall()
                for link in blocking_link:
                    old_id = str(link["old_entry_id"])
                    new_id = str(link["new_entry_id"])
                    if new_id in purge_ids and old_id not in purge_ids:
                        raise StoreValidationError(
                            "Cannot purge replacement while retained history points to it: "
                            f"{new_id}"
                        )
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
                verify_database_integrity(
                    self._connection,
                    max_statement_chars=self._config.limits.max_statement_chars,
                    max_subject_keys=self._config.limits.max_subject_keys,
                )
        if entry_ids:
            LOGGER.info("Purged %d expired entries", len(entry_ids))
        return len(entry_ids)

    def get_entry(self, entry_id: str) -> Entry | None:
        """Return one entry by identifier."""
        with self._write_lock:
            self._ensure_open()
            row = self._fetch_entry_row(_normalize_entry_id(entry_id))
            return None if row is None else self._materialize_entry(row)

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
            return tuple(self._materialize_entry(row) for row in rows)

    def list_entries_page(
        self,
        *,
        after_id: str | None,
        limit: int,
    ) -> tuple[Entry, ...]:
        """Return one stable ID-ordered page for bounded offline work."""
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise StoreValidationError("limit must be an integer")
        if not 1 <= limit <= MAX_VECTOR_REBUILD_BATCH_ITEMS:
            raise StoreValidationError(
                f"limit must be between 1 and {MAX_VECTOR_REBUILD_BATCH_ITEMS}"
            )
        normalized_after = None if after_id is None else _normalize_entry_id(after_id, "after_id")
        query = "SELECT * FROM entries"
        parameters: list[object] = []
        if normalized_after is not None:
            query += " WHERE id > ?"
            parameters.append(normalized_after)
        query += " ORDER BY id ASC LIMIT ?"
        parameters.append(limit)
        with self._write_lock:
            self._ensure_open()
            rows = self._connection.execute(query, parameters).fetchall()
            return tuple(self._materialize_entry(row) for row in rows)

    def list_observations(self, entry_id: str) -> tuple[EntryObservation, ...]:
        """Return retained model observations for one entry generation."""
        normalized_entry_id = _normalize_entry_id(entry_id)
        with self._write_lock:
            self._ensure_open()
            rows = self._connection.execute(
                """
                SELECT *
                FROM entry_observations
                WHERE entry_id = ?
                ORDER BY recorded_at, writer_model, claim_hash
                """,
                (normalized_entry_id,),
            ).fetchall()
            return tuple(_observation_from_row(row) for row in rows)

    def create_consolidation_plan(self, snapshot_json: str) -> StoredConsolidationPlan:
        """Persist one immutable consolidation snapshot and return its trusted identity."""
        normalized_snapshot = _required_text(snapshot_json, "snapshot_json")
        encoded_snapshot = normalized_snapshot.encode("utf-8")
        if len(encoded_snapshot) > MAX_CONSOLIDATION_SNAPSHOT_BYTES:
            raise StoreValidationError(
                "snapshot_json exceeds the fixed UTF-8 allocation bound of "
                f"{MAX_CONSOLIDATION_SNAPSHOT_BYTES} bytes"
            )
        with self._write_lock:
            self._ensure_open()
            now = self._now()
            plan_id = _new_ulid(now)
            snapshot_hash = hashlib.sha256(encoded_snapshot).hexdigest()
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
        normalized_entry_id = _normalize_entry_id(entry_id)
        normalized_ref = _normalize_datacron_ref(datacron_ref)
        normalized_hash = _normalize_sha256_hex(datacron_hash, "datacron_hash")
        normalized_actor = _normalize_actor(actor)
        with self._write_lock:
            self._ensure_open()
            now = self._now()
            with transaction(self._connection):
                row = self._fetch_entry_row(normalized_entry_id)
                if row is None:
                    raise StoreValidationError(
                        f"Entry to promote does not exist: {normalized_entry_id}"
                    )
                entry = self._materialize_entry(row)
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
                return self._materialize_entry(updated_row)

    def set_stale(self, entry_id: str, *, stale: bool, actor: str = "cli") -> Entry:
        """Mark a promoted entry stale or fresh without rewriting Datacron."""
        normalized_entry_id = _normalize_entry_id(entry_id)
        normalized_actor = _normalize_actor(actor)
        with self._write_lock:
            self._ensure_open()
            now = self._now()
            with transaction(self._connection):
                row = self._fetch_entry_row(normalized_entry_id)
                if row is None:
                    raise StoreValidationError(
                        f"Freshness entry does not exist: {normalized_entry_id}"
                    )
                entry = self._materialize_entry(row)
                if entry.promotion_state is not PromotionState.PROMOTED:
                    raise StoreValidationError("Only promoted entries have freshness state")
                if entry.kind is not EntryKind.EPISODE and entry.claim_key is None:
                    raise StoreValidationError(
                        "Legacy trusted entry requires claim_key classification "
                        "before freshness updates"
                    )
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
                return self._materialize_entry(updated_row)

    def begin_fts_query(self, timeout_ms: int | None = None) -> FtsQueryDeadline:
        """Create one absolute monotone deadline for a progressive FTS plan."""
        selected_timeout = (
            self._config.retrieval.fts_query_timeout_ms if timeout_ms is None else timeout_ms
        )
        if isinstance(selected_timeout, bool) or not isinstance(selected_timeout, int):
            raise StoreValidationError("timeout_ms must be an integer")
        if not MIN_FTS_QUERY_TIMEOUT_MS <= selected_timeout <= MAX_FTS_QUERY_TIMEOUT_MS:
            raise StoreValidationError(
                "timeout_ms must be between "
                f"{MIN_FTS_QUERY_TIMEOUT_MS} and {MAX_FTS_QUERY_TIMEOUT_MS}"
            )
        return FtsQueryDeadline(
            expires_at=self._monotonic_clock() + selected_timeout / 1000,
        )

    def search_fts(  # noqa: PLR0913
        self,
        match_query: str,
        *,
        scope: str | None,
        kinds: frozenset[EntryKind] | None,
        writer_model: str | None = None,
        limit: int = 64,
        deadline: FtsQueryDeadline | None = None,
    ) -> tuple[Entry, ...]:
        """Return a bounded visible FTS rank ordered by BM25 and recency."""
        normalized_query = _bounded_text(
            match_query,
            "match_query",
            MAX_FTS_EXPRESSION_CHARS,
        )
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise StoreValidationError("limit must be an integer")
        if not 1 <= limit <= MAX_FTS_TOP_K:
            raise StoreValidationError(f"limit must be between 1 and {MAX_FTS_TOP_K}")
        normalized_writer = None if writer_model is None else _normalize_writer_model(writer_model)
        normalized_scope = None if scope is None else _normalize_scope(scope)
        selected_deadline = deadline or self.begin_fts_query()
        now = self._now()
        today = now.date().isoformat()
        visibility_clauses, visibility_parameters = _retrieval_visibility_sql(
            now=now,
            today=today,
            writer_model=normalized_writer,
        )
        clauses = [f"{FTS_TABLE_NAME} MATCH ?", *visibility_clauses]
        parameters: list[object] = [normalized_query, *visibility_parameters]
        if normalized_scope is not None:
            clauses.append("entries.scope = ?")
            parameters.append(normalized_scope)
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
            "ORDER BY bm25(entries_fts), entries.recorded_at DESC, entries.id DESC "
            "LIMIT ?"
        )
        parameters.append(limit)
        rows = self._fetchall_with_deadline(
            query,
            parameters,
            deadline=selected_deadline,
        )
        return tuple(self._materialize_entry(row) for row in rows)

    def _fetchall_with_deadline(
        self,
        query: str,
        parameters: Sequence[object],
        *,
        deadline: FtsQueryDeadline,
    ) -> list[sqlite3.Row]:
        """Run one SQLite statement under the remaining absolute deadline."""
        remaining = deadline.expires_at - self._monotonic_clock()
        if remaining <= 0 or not self._write_lock.acquire(timeout=remaining):
            raise StoreQueryTimeoutError("FTS query deadline expired")
        try:
            self._ensure_open()
            expired = False

            def progress_handler() -> int:
                nonlocal expired
                if self._monotonic_clock() >= deadline.expires_at:
                    expired = True
                    return 1
                return 0

            cursor: sqlite3.Cursor | None = None
            self._connection.set_progress_handler(
                progress_handler,
                SQLITE_PROGRESS_HANDLER_STEPS,
            )
            try:
                cursor = self._connection.cursor()
                cursor.execute(query, parameters)
                rows = cursor.fetchall()
            except sqlite3.DatabaseError as exc:
                if expired and getattr(exc, "sqlite_errorcode", None) == sqlite3.SQLITE_INTERRUPT:
                    raise StoreQueryTimeoutError("FTS query deadline expired") from exc
                raise
            finally:
                if cursor is not None:
                    cursor.close()
                self._connection.set_progress_handler(None, 0)
            if self._monotonic_clock() >= deadline.expires_at:
                raise StoreQueryTimeoutError("FTS query deadline expired")
            return cast("list[sqlite3.Row]", rows)
        finally:
            self._write_lock.release()

    def list_retrieval_entries(
        self,
        *,
        scope: str | None,
        kinds: frozenset[EntryKind] | None,
        writer_model: str,
        limit: int,
    ) -> tuple[Entry, ...]:
        """Return a bounded recency window with recall visibility applied in SQL."""
        normalized_writer = _normalize_writer_model(writer_model)
        normalized_scope = None if scope is None else _normalize_scope(scope)
        normalized_limit = _retrieval_limit(limit)
        now = self._now()
        today = now.date().isoformat()
        clauses, parameters = _retrieval_visibility_sql(
            now=now,
            today=today,
            writer_model=normalized_writer,
        )
        if normalized_scope is not None:
            clauses.append("entries.scope = ?")
            parameters.append(normalized_scope)
        if kinds:
            ordered_kinds = sorted(kind.value for kind in kinds)
            placeholders = ", ".join("?" for _ in ordered_kinds)
            clauses.append(f"entries.kind IN ({placeholders})")
            parameters.extend(ordered_kinds)
        parameters.append(normalized_limit)
        query = (
            "SELECT entries.* FROM entries "  # noqa: S608
            f"WHERE {' AND '.join(clauses)} "
            "ORDER BY entries.recorded_at DESC, entries.id DESC "
            "LIMIT ?"
        )
        with self._write_lock:
            self._ensure_open()
            rows = self._connection.execute(query, parameters).fetchall()
            return tuple(self._materialize_entry(row) for row in rows)

    def list_retrieval_vectors(
        self,
        model: str,
        *,
        scope: str | None,
        kinds: frozenset[EntryKind] | None,
        writer_model: str,
        limit: int,
    ) -> tuple[tuple[str, tuple[float, ...] | None], ...]:
        """Return visible IDs and vectors only after a metadata-only memory preflight."""
        normalized_model = _normalize_embedding_model(model)
        normalized_writer = _normalize_writer_model(writer_model)
        normalized_scope = None if scope is None else _normalize_scope(scope)
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise StoreValidationError("limit must be an integer")
        if not 1 <= limit <= MAX_HYBRID_CANDIDATES:
            raise StoreValidationError(f"limit must be between 1 and {MAX_HYBRID_CANDIDATES}")
        now = self._now()
        today = now.date().isoformat()
        clauses, visibility_parameters = _retrieval_visibility_sql(
            now=now,
            today=today,
            writer_model=normalized_writer,
        )
        parameters: list[object] = [normalized_model, *visibility_parameters]
        if normalized_scope is not None:
            clauses.append("entries.scope = ?")
            parameters.append(normalized_scope)
        if kinds:
            ordered_kinds = sorted(kind.value for kind in kinds)
            placeholders = ", ".join("?" for _ in ordered_kinds)
            clauses.append(f"entries.kind IN ({placeholders})")
            parameters.extend(ordered_kinds)
        where_clause = " AND ".join(clauses)
        preflight_parameters = [*parameters, limit + 1]
        preflight_query = (
            "SELECT COUNT(*) AS candidate_count, "  # noqa: S608
            "COALESCE(SUM(CASE "
            "WHEN vector_dim IS NULL THEN 0 "
            f"WHEN vector_dim BETWEEN 1 AND {MAX_HYBRID_VECTOR_SCAN_VALUES} THEN vector_dim "
            f"ELSE {MAX_HYBRID_VECTOR_SCAN_VALUES + 1} END), 0) AS vector_values, "
            "COALESCE(SUM(CASE "
            "WHEN vector_blob_bytes IS NULL THEN 0 "
            f"WHEN vector_blob_bytes <= {MAX_HYBRID_VECTOR_SCAN_BYTES} THEN vector_blob_bytes "
            f"ELSE {MAX_HYBRID_VECTOR_SCAN_BYTES + 1} END), 0) AS vector_bytes "
            "FROM ("
            "SELECT entry_vectors.dim AS vector_dim, "
            "octet_length(entry_vectors.vector) AS vector_blob_bytes "
            "FROM entries "
            "LEFT JOIN entry_vectors "
            "ON entries.id = entry_vectors.entry_id AND entry_vectors.model = ? "
            f"WHERE {where_clause} "
            "ORDER BY entries.id ASC "
            "LIMIT ?"
            ")"
        )
        query = (
            "SELECT entries.id AS entry_id, entry_vectors.dim AS vector_dim, "  # noqa: S608
            "CASE WHEN entry_vectors.vector IS NULL THEN 0 "
            "WHEN typeof(entry_vectors.vector) = 'blob' "
            f"AND entry_vectors.dim BETWEEN 1 AND {MAX_VECTOR_DIMENSIONS} "
            "AND octet_length(entry_vectors.vector) = entry_vectors.dim * 4 "
            "THEN 0 ELSE 1 END AS vector_invalid, "
            "CASE "
            "WHEN typeof(entry_vectors.vector) = 'blob' "
            f"AND entry_vectors.dim BETWEEN 1 AND {MAX_VECTOR_DIMENSIONS} "
            "AND octet_length(entry_vectors.vector) = entry_vectors.dim * 4 "
            "THEN entry_vectors.vector ELSE NULL END AS vector_blob "
            "FROM entries "
            "LEFT JOIN entry_vectors "
            "ON entries.id = entry_vectors.entry_id AND entry_vectors.model = ? "
            f"WHERE {where_clause} "
            "ORDER BY entries.id ASC "
            "LIMIT ?"
        )
        with self._write_lock:
            self._ensure_open()
            preflight = self._connection.execute(
                preflight_query,
                preflight_parameters,
            ).fetchone()
            if preflight is None:  # pragma: no cover - aggregate always returns one row
                raise RuntimeError("Hybrid vector preflight returned no row")
            candidate_count = int(preflight["candidate_count"])
            vector_values = int(preflight["vector_values"])
            vector_bytes = int(preflight["vector_bytes"])
            if candidate_count > limit:
                raise StoreVectorBudgetError("candidate_count")
            if vector_values > MAX_HYBRID_VECTOR_SCAN_VALUES:
                raise StoreVectorBudgetError("vector_values")
            if vector_bytes > MAX_HYBRID_VECTOR_SCAN_BYTES:
                raise StoreVectorBudgetError("vector_bytes")

            cursor = self._connection.execute(query, [*parameters, limit])
            decoded: list[tuple[str, tuple[float, ...] | None]] = []
            try:
                while rows := cursor.fetchmany(MAX_VECTOR_REBUILD_BATCH_ITEMS):
                    decoded.extend(
                        (
                            str(row["entry_id"]),
                            _decode_retrieval_vector(row),
                        )
                        for row in rows
                    )
            finally:
                cursor.close()
            return tuple(decoded)

    def get_retrieval_entries(
        self,
        entry_ids: Sequence[str],
        *,
        writer_model: str,
    ) -> tuple[Entry, ...]:
        """Revalidate a bounded direct rank against current recall visibility."""
        normalized_ids = _normalize_entry_ids(entry_ids, "entry_ids")
        if not normalized_ids:
            return ()
        if len(normalized_ids) > MAX_FTS_TOP_K:
            raise StoreValidationError(f"entry_ids must contain at most {MAX_FTS_TOP_K} values")
        normalized_writer = _normalize_writer_model(writer_model)
        now = self._now()
        today = now.date().isoformat()
        clauses, parameters = _retrieval_visibility_sql(
            now=now,
            today=today,
            writer_model=normalized_writer,
        )
        placeholders = ", ".join("?" for _ in normalized_ids)
        clauses.append(f"entries.id IN ({placeholders})")
        parameters.extend(normalized_ids)
        query = f"SELECT entries.* FROM entries WHERE {' AND '.join(clauses)}"  # noqa: S608
        with self._write_lock:
            self._ensure_open()
            rows = self._connection.execute(query, parameters).fetchall()
            return tuple(self._materialize_entry(row) for row in rows)

    def list_conflict_family(
        self,
        *,
        kind: EntryKind,
        scope: str,
        claim_key: str | None,
        limit: int,
    ) -> tuple[Entry, ...]:
        """Return one bounded trusted conflict family, including an overflow probe."""
        normalized_limit = _retrieval_limit(limit, allow_overflow_probe=True)
        normalized_scope = _normalize_scope(scope)
        if kind is not EntryKind.PROJECT_STATE and claim_key is None:
            return ()
        normalized_claim_key = (
            PROJECT_STATE_CLAIM_KEY
            if kind is EntryKind.PROJECT_STATE
            else _required_text(claim_key, "claim_key")
        )
        now = self._now()
        today = now.date().isoformat()
        clauses, parameters = _active_retrieval_visibility_sql(now=now, today=today)
        clauses.extend(("entries.kind = ?", "entries.scope = ?"))
        parameters.extend((kind.value, normalized_scope))
        if kind is not EntryKind.PROJECT_STATE:
            clauses.append("entries.claim_key = ?")
            parameters.append(normalized_claim_key)
        parameters.append(normalized_limit)
        query = (
            "SELECT entries.* FROM entries "  # noqa: S608
            f"WHERE {' AND '.join(clauses)} "
            "ORDER BY entries.recorded_at DESC, entries.id DESC "
            "LIMIT ?"
        )
        with self._write_lock:
            self._ensure_open()
            rows = self._connection.execute(query, parameters).fetchall()
            return tuple(self._materialize_entry(row) for row in rows)

    def list_project_states(
        self,
        *,
        scope: str,
        limit: int,
    ) -> tuple[Entry, ...]:
        """Return bounded trusted project states for one exact scope."""
        return self.list_conflict_family(
            kind=EntryKind.PROJECT_STATE,
            scope=scope,
            claim_key=PROJECT_STATE_CLAIM_KEY,
            limit=limit,
        )

    def rebuild_fts(self) -> None:
        """Reconstruct the derived FTS table from canonical entry rows."""
        with self._write_lock:
            self._ensure_open()
            rebuild_fts_index(self._connection)
        LOGGER.info("Rebuilt the FTS index")

    def upsert_vector(self, entry_id: str, model: str, vector: Sequence[float]) -> None:
        """Store one derived vector for an existing entry."""
        normalized_entry_id = _normalize_entry_id(entry_id)
        normalized_model = _normalize_embedding_model(model)
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
        normalized_model = _normalize_embedding_model(model)
        encoded = {
            _normalize_entry_id(entry_id): _encode_vector(vector)
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

    def begin_vector_rebuild(self, model: str) -> None:
        """Create an empty connection-local stage without touching the live index."""
        normalized_model = _normalize_embedding_model(model)
        with self._write_lock:
            self._ensure_open()
            if self._vector_rebuild_model is not None:
                raise StoreBusyError("a vector rebuild is already in progress")
            self._connection.execute(
                f"""
                CREATE TEMP TABLE IF NOT EXISTS {VECTOR_REBUILD_STAGE_TABLE} (
                    entry_id TEXT PRIMARY KEY,
                    model TEXT NOT NULL,
                    dim INTEGER NOT NULL,
                    vector BLOB NOT NULL
                )
                """
            )
            with transaction(self._connection):
                self._connection.execute(f"DELETE FROM {VECTOR_REBUILD_STAGE_TABLE}")  # noqa: S608
            self._vector_rebuild_model = normalized_model
            self._vector_rebuild_data_version = _database_data_version(self._connection)

    def stage_vector_rebuild(
        self,
        model: str,
        vectors: Mapping[str, Sequence[float]],
    ) -> None:
        """Append one encoded bounded batch to the connection-local stage."""
        normalized_model = _normalize_embedding_model(model)
        if len(vectors) > MAX_VECTOR_REBUILD_BATCH_ITEMS:
            raise StoreValidationError(
                f"vector rebuild batch exceeds {MAX_VECTOR_REBUILD_BATCH_ITEMS} items"
            )
        encoded = {
            _normalize_entry_id(entry_id): _encode_vector(vector)
            for entry_id, vector in vectors.items()
        }
        with self._write_lock:
            self._ensure_open()
            if self._vector_rebuild_model != normalized_model:
                raise StoreValidationError("vector rebuild stage is not active for this model")
            if self._vector_rebuild_data_version != _database_data_version(self._connection):
                raise StoreBusyError(
                    "database changed from another connection during vector rebuild"
                )
            with transaction(self._connection):
                self._connection.executemany(
                    f"""
                    INSERT INTO {VECTOR_REBUILD_STAGE_TABLE}(entry_id, model, dim, vector)
                    VALUES (?, ?, ?, ?)
                    """,  # noqa: S608
                    (
                        (entry_id, normalized_model, dimension, vector_blob)
                        for entry_id, (vector_blob, dimension) in encoded.items()
                    ),
                )

    def finish_vector_rebuild(self, model: str, *, expected_count: int) -> None:
        """Atomically replace one live model after validating the full stage."""
        normalized_model = _normalize_embedding_model(model)
        if (
            isinstance(expected_count, bool)
            or not isinstance(expected_count, int)
            or expected_count < 0
        ):
            raise StoreValidationError("expected_count must be a non-negative integer")
        with self._write_lock:
            self._ensure_open()
            if self._vector_rebuild_model != normalized_model:
                raise StoreValidationError("vector rebuild stage is not active for this model")
            if self._vector_rebuild_data_version != _database_data_version(self._connection):
                raise StoreBusyError(
                    "database changed from another connection during vector rebuild"
                )
            with transaction(self._connection):
                if self._vector_rebuild_data_version != _database_data_version(self._connection):
                    raise StoreBusyError(
                        "database changed from another connection during vector rebuild"
                    )
                row = self._connection.execute(
                    f"""
                    SELECT COUNT(*) AS staged_count,
                           COUNT(DISTINCT model) AS model_count,
                           MIN(model) AS staged_model
                    FROM {VECTOR_REBUILD_STAGE_TABLE}
                    """  # noqa: S608
                ).fetchone()
                if row is None:  # pragma: no cover - aggregate always returns one row
                    raise RuntimeError("Vector rebuild stage returned no row")
                staged_count = int(row["staged_count"])
                if staged_count != expected_count:
                    raise StoreValidationError(
                        "vector rebuild stage count does not match expected_count"
                    )
                if staged_count and (
                    int(row["model_count"]) != 1 or str(row["staged_model"]) != normalized_model
                ):
                    raise StoreValidationError("vector rebuild stage model does not match")
                self._connection.execute(
                    "DELETE FROM entry_vectors WHERE model = ?",
                    (normalized_model,),
                )
                self._connection.execute(
                    f"""
                    INSERT INTO entry_vectors(entry_id, model, dim, vector)
                    SELECT entry_id, model, dim, vector
                    FROM {VECTOR_REBUILD_STAGE_TABLE}
                    ORDER BY entry_id
                    """  # noqa: S608
                )
                self._connection.execute(f"DELETE FROM {VECTOR_REBUILD_STAGE_TABLE}")  # noqa: S608
            self._vector_rebuild_model = None
            self._vector_rebuild_data_version = None

    def abort_vector_rebuild(self) -> None:
        """Discard a connection-local stage while preserving every live vector."""
        with self._write_lock:
            self._ensure_open()
            row = self._connection.execute(
                "SELECT 1 FROM temp.sqlite_master WHERE type = 'table' AND name = ?",
                (VECTOR_REBUILD_STAGE_TABLE,),
            ).fetchone()
            if row is None:
                self._vector_rebuild_model = None
                self._vector_rebuild_data_version = None
                return
            with transaction(self._connection):
                self._connection.execute(f"DELETE FROM {VECTOR_REBUILD_STAGE_TABLE}")  # noqa: S608
            self._vector_rebuild_model = None
            self._vector_rebuild_data_version = None

    def clear_vectors(self) -> None:
        """Clear every derived vector before a full configured rebuild."""
        with self._write_lock:
            self._ensure_open()
            with transaction(self._connection):
                self._connection.execute("DELETE FROM entry_vectors")

    def list_vectors(
        self,
        model: str,
        *,
        entry_ids: Sequence[str] | None = None,
    ) -> dict[str, tuple[float, ...]]:
        """Return derived vectors for one model, optionally restricted by ID."""
        normalized_model = _normalize_embedding_model(model)
        normalized_ids = None if entry_ids is None else _normalize_entry_ids(entry_ids, "entry_ids")
        if normalized_ids is not None and len(normalized_ids) > MAX_FTS_TOP_K:
            raise StoreValidationError(f"entry_ids must contain at most {MAX_FTS_TOP_K} values")
        if normalized_ids == ():
            return {}
        query = "SELECT entry_id, dim, vector FROM entry_vectors WHERE model = ?"
        parameters: list[object] = [normalized_model]
        if normalized_ids is not None:
            placeholders = ", ".join("?" for _ in normalized_ids)
            query += f" AND entry_id IN ({placeholders})"
            parameters.extend(normalized_ids)
        with self._write_lock:
            self._ensure_open()
            rows = self._connection.execute(query, parameters).fetchall()
            return {
                str(row["entry_id"]): _decode_vector(bytes(row["vector"]), int(row["dim"]))
                for row in rows
            }

    def _add_entry(  # noqa: C901, PLR0912, PLR0913, PLR0915
        self,
        *,
        kind: EntryKind | str,
        scope: str,
        statement: str,
        subject_keys: Sequence[str],
        claim_key: str | None,
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
        supersedes: Sequence[str] = (),
    ) -> tuple[Entry, bool, RememberOutcome]:
        actor = _normalize_actor(actor)
        writer_model = None if writer_model is None else _normalize_writer_model(writer_model)
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
        canonical_key = _canonical_key(
            normalized_kind,
            normalized_scope,
            normalized_statement,
        )
        normalized_claim_key = (
            _normalize_trusted_claim_key(normalized_kind, claim_key)
            if attest_existing_candidate
            else None
        )
        normalized_supersedes = _normalize_entry_ids(supersedes, "supersedes")
        observation_hash = (
            None
            if writer_model is None
            else _observation_hash(
                canonical_key=canonical_key,
                writer_model=writer_model,
                confidence=confidence,
                observed_at=normalized_observed_at,
                valid_from=valid_from,
                valid_until=valid_until,
                subject_keys=normalized_subject_keys,
                evidence=normalized_evidence,
            )
        )

        with self._write_lock:
            self._ensure_open()
            now = self._now()
            with transaction(self._connection):
                self._expire_due_canonical(
                    canonical_key=canonical_key,
                    now=now,
                    actor=actor,
                )
                live_rows = self._canonical_rows(
                    canonical_key,
                    statuses=(EntryStatus.ACTIVE, EntryStatus.QUARANTINED),
                )
                active_rows = [
                    row for row in live_rows if str(row["status"]) == EntryStatus.ACTIVE.value
                ]
                candidate_rows = [
                    row for row in live_rows if str(row["status"]) == EntryStatus.QUARANTINED.value
                ]
                for row in active_rows:
                    active = self._materialize_entry(row)
                    if active.stale or (
                        active.valid_from is not None and active.valid_from > now.date()
                    ):
                        raise StoreValidationError(
                            "Canonical trusted entry exists but is not currently recallable"
                        )
                if attest_existing_candidate:
                    entry = self._attest_canonical_in_transaction(
                        active_rows=active_rows,
                        candidate_rows=candidate_rows,
                        canonical_key=canonical_key,
                        claim_key=normalized_claim_key,
                        subject_keys=normalized_subject_keys,
                        source_type=source_type,
                        confidence=confidence,
                        observed_at=normalized_observed_at,
                        valid_from=valid_from,
                        valid_until=valid_until,
                        evidence=normalized_evidence,
                        actor=actor,
                        now=now,
                    )
                    if entry is not None:
                        self._apply_supersessions_in_transaction(
                            old_ids=normalized_supersedes,
                            new_id=entry.id,
                            actor=actor,
                            now=now,
                        )
                        refreshed = self._fetch_entry_row(entry.id)
                        if refreshed is None:  # pragma: no cover - transaction invariant
                            raise RuntimeError("Attested entry disappeared")
                        return (
                            self._materialize_entry(refreshed),
                            False,
                            RememberOutcome.EXISTING_TRUSTED,
                        )

                if not attest_existing_candidate and writer_model is not None:
                    trusted_row = next(
                        (
                            row
                            for row in active_rows
                            if _is_classified_trusted_active(self._materialize_entry(row))
                        ),
                        None,
                    )
                    if trusted_row is not None:
                        trusted = self._materialize_entry(trusted_row)
                        if observation_hash is None:  # pragma: no cover - normalized above
                            raise RuntimeError("Candidate observation hash is missing")
                        inserted = self._insert_observation(
                            entry_id=trusted.id,
                            writer_model=writer_model,
                            claim_hash=observation_hash,
                            recorded_at=now,
                            confidence=confidence,
                            observed_at=normalized_observed_at,
                            valid_from=valid_from,
                            valid_until=valid_until,
                            subject_keys=normalized_subject_keys,
                            evidence=normalized_evidence,
                        )
                        self._append_audit(
                            ts=now,
                            actor=actor,
                            action=(
                                AuditAction.CORROBORATE if inserted else AuditAction.IDEMPOTENT_NOOP
                            ),
                            entry_id=trusted.id,
                            detail={
                                "canonical_key": canonical_key,
                                "writer_model": writer_model,
                            },
                        )
                        return (
                            trusted,
                            not inserted,
                            RememberOutcome.EXISTING_TRUSTED,
                        )
                    own_row = next(
                        (row for row in candidate_rows if str(row["writer_model"]) == writer_model),
                        None,
                    )
                    if own_row is not None:
                        existing = self._materialize_entry(own_row)
                        if observation_hash is None:  # pragma: no cover - normalized above
                            raise RuntimeError("Candidate observation hash is missing")
                        if self._observation_exists(
                            entry_id=existing.id,
                            writer_model=writer_model,
                            claim_hash=observation_hash,
                        ) or _entry_matches_observation(
                            existing,
                            confidence=confidence,
                            observed_at=normalized_observed_at,
                            valid_from=valid_from,
                            valid_until=valid_until,
                            subject_keys=normalized_subject_keys,
                            evidence=normalized_evidence,
                        ):
                            self._append_audit(
                                ts=now,
                                actor=actor,
                                action=AuditAction.IDEMPOTENT_NOOP,
                                entry_id=existing.id,
                                detail={"canonical_key": canonical_key},
                            )
                            return existing, True, RememberOutcome.RETRY
                        self._insert_observation(
                            entry_id=existing.id,
                            writer_model=writer_model,
                            claim_hash=observation_hash,
                            recorded_at=now,
                            confidence=confidence,
                            observed_at=normalized_observed_at,
                            valid_from=valid_from,
                            valid_until=valid_until,
                            subject_keys=normalized_subject_keys,
                            evidence=normalized_evidence,
                        )
                        existing = self._widen_subject_keys(
                            row=own_row,
                            existing=existing,
                            incoming=normalized_subject_keys,
                        )
                        self._append_audit(
                            ts=now,
                            actor=actor,
                            action=AuditAction.CORROBORATE,
                            entry_id=existing.id,
                            detail={
                                "canonical_key": canonical_key,
                                "writer_model": writer_model,
                                "subject_keys": list(existing.subject_keys),
                            },
                        )
                        return existing, False, RememberOutcome.CORROBORATED

                terminal_exists = self._canonical_exists(
                    canonical_key,
                    statuses=(EntryStatus.SUPERSEDED, EntryStatus.EXPIRED),
                )
                other_live_candidate = bool(candidate_rows)
                ttl_days = self._config.ttl_days.for_kind(normalized_kind)
                expires_at = None if ttl_days == 0 else now + timedelta(days=ttl_days)
                entry_id = _new_ulid(now)
                entry = Entry(
                    id=entry_id,
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
                    idempotency_key=_generation_key(
                        canonical_key=canonical_key,
                        entry_id=entry_id,
                    ),
                    canonical_key=canonical_key,
                    claim_key=normalized_claim_key,
                    supersedes=(),
                    evidence=normalized_evidence,
                    stale=False,
                    datacron_ref=None,
                    datacron_hash=None,
                    synced_at=None,
                )
                self._insert_entry(entry)
                if writer_model is not None:
                    if observation_hash is None:  # pragma: no cover - normalized above
                        raise RuntimeError("Candidate observation hash is missing")
                    self._insert_observation(
                        entry_id=entry.id,
                        writer_model=writer_model,
                        claim_hash=observation_hash,
                        recorded_at=now,
                        confidence=confidence,
                        observed_at=normalized_observed_at,
                        valid_from=valid_from,
                        valid_until=valid_until,
                        subject_keys=normalized_subject_keys,
                        evidence=normalized_evidence,
                    )
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
                if attest_existing_candidate:
                    self._apply_supersessions_in_transaction(
                        old_ids=normalized_supersedes,
                        new_id=entry.id,
                        actor=actor,
                        now=now,
                    )
                    refreshed = self._fetch_entry_row(entry.id)
                    if refreshed is None:  # pragma: no cover - transaction invariant
                        raise RuntimeError("Attested entry disappeared")
                    entry = self._materialize_entry(refreshed)
        LOGGER.info("Stored %s entry %s", entry.kind.value, entry.id)
        if terminal_exists:
            outcome = RememberOutcome.RENEWED
        elif other_live_candidate:
            outcome = RememberOutcome.CORROBORATED
        else:
            outcome = RememberOutcome.CREATED
        return entry, False, outcome

    def _attest_canonical_in_transaction(  # noqa: C901, PLR0913
        self,
        *,
        active_rows: Sequence[sqlite3.Row],
        candidate_rows: Sequence[sqlite3.Row],
        canonical_key: str,
        claim_key: str | None,
        subject_keys: tuple[str, ...],
        source_type: SourceType,
        confidence: Confidence,
        observed_at: datetime | None,
        valid_from: date | None,
        valid_until: date | None,
        evidence: tuple[Evidence, ...],
        actor: str,
        now: datetime,
    ) -> Entry | None:
        if active_rows:
            existing = self._materialize_entry(active_rows[0])
            if not _is_trusted_active(existing):
                raise StoreValidationError(
                    "Canonical content already exists in an invalid active lifecycle state"
                )
            if existing.claim_key is not None and existing.claim_key != claim_key:
                raise StoreValidationError(
                    "Canonical trusted content already has a different claim_key"
                )
            if existing.claim_key is None:
                self._connection.execute(
                    "UPDATE entries SET claim_key = ? WHERE id = ?",
                    (claim_key, existing.id),
                )
                refreshed_row = self._fetch_entry_row(existing.id)
                if refreshed_row is None:  # pragma: no cover - protected by transaction
                    raise RuntimeError("Classified trusted entry disappeared")
                existing = self._materialize_entry(refreshed_row)
                audit_action = AuditAction.ATTEST
            else:
                _require_attestation_adds_nothing(
                    existing,
                    source_type=source_type,
                    confidence=confidence,
                    observed_at=observed_at,
                    valid_from=valid_from,
                    valid_until=valid_until,
                    subject_keys=subject_keys,
                    evidence=evidence,
                )
                audit_action = AuditAction.IDEMPOTENT_NOOP
            self._append_audit(
                ts=now,
                actor=actor,
                action=audit_action,
                entry_id=existing.id,
                detail={"canonical_key": canonical_key, "claim_key": claim_key},
            )
            for row in candidate_rows:
                self._supersede_in_transaction(
                    old_id=str(row["id"]),
                    new_id=existing.id,
                    actor=actor,
                    now=now,
                    allow_candidate_old=True,
                    classify_legacy_old=False,
                )
            refreshed = self._fetch_entry_row(existing.id)
            if refreshed is None:  # pragma: no cover - transaction invariant
                raise RuntimeError("Attested entry disappeared")
            return self._materialize_entry(refreshed)
        if not candidate_rows:
            return None
        selected_row = candidate_rows[0]
        selected = self._materialize_entry(selected_row)
        if not _is_attestable_candidate(selected):
            raise StoreValidationError(
                "Canonical content already exists in a non-attestable lifecycle state"
            )
        updated = self._attest_existing_candidate(
            row=selected_row,
            existing=selected,
            claim_key=claim_key,
            subject_keys=subject_keys,
            source_type=source_type,
            confidence=confidence,
            observed_at=observed_at,
            valid_from=valid_from,
            valid_until=valid_until,
            evidence=evidence,
            now=now,
        )
        self._append_audit(
            ts=now,
            actor=actor,
            action=AuditAction.ATTEST,
            entry_id=updated.id,
            detail=_entry_detail(updated),
        )
        for row in candidate_rows[1:]:
            self._supersede_in_transaction(
                old_id=str(row["id"]),
                new_id=updated.id,
                actor=actor,
                now=now,
                allow_candidate_old=True,
                classify_legacy_old=False,
            )
        refreshed = self._fetch_entry_row(updated.id)
        if refreshed is None:  # pragma: no cover - transaction invariant
            raise RuntimeError("Attested entry disappeared")
        return self._materialize_entry(refreshed)

    def _canonical_rows(
        self,
        canonical_key: str,
        *,
        statuses: Sequence[EntryStatus],
    ) -> tuple[sqlite3.Row, ...]:
        placeholders = ", ".join("?" for _ in statuses)
        rows = self._connection.execute(
            "SELECT rowid AS entry_rowid, * FROM entries "  # noqa: S608
            f"WHERE canonical_key = ? AND status IN ({placeholders}) "
            "ORDER BY recorded_at, id",
            (canonical_key, *(status.value for status in statuses)),
        ).fetchall()
        return tuple(rows)

    def _canonical_exists(
        self,
        canonical_key: str,
        *,
        statuses: Sequence[EntryStatus],
    ) -> bool:
        placeholders = ", ".join("?" for _ in statuses)
        row = self._connection.execute(
            "SELECT 1 FROM entries "  # noqa: S608
            f"WHERE canonical_key = ? AND status IN ({placeholders}) LIMIT 1",
            (canonical_key, *(status.value for status in statuses)),
        ).fetchone()
        return row is not None

    def _expire_due_canonical(
        self,
        *,
        canonical_key: str,
        now: datetime,
        actor: str,
    ) -> None:
        now_text = _format_datetime(now)
        today_text = now.date().isoformat()
        rows = self._connection.execute(
            """
            SELECT id, expires_at, valid_until
            FROM entries
            WHERE canonical_key = ?
              AND status IN (?, ?)
              AND (
                  (expires_at IS NOT NULL AND expires_at <= ?)
                  OR (valid_until IS NOT NULL AND valid_until < ?)
              )
            ORDER BY id
            """,
            (
                canonical_key,
                EntryStatus.ACTIVE.value,
                EntryStatus.QUARANTINED.value,
                now_text,
                today_text,
            ),
        ).fetchall()
        for row in rows:
            entry_id = str(row["id"])
            expired_by: list[str] = []
            if row["expires_at"] is not None and str(row["expires_at"]) <= now_text:
                expired_by.append("ttl")
            if row["valid_until"] is not None and str(row["valid_until"]) < today_text:
                expired_by.append("business_validity")
            self._connection.execute(
                "UPDATE entries SET status = ? WHERE id = ?",
                (EntryStatus.EXPIRED.value, entry_id),
            )
            self._append_audit(
                ts=now,
                actor=actor,
                action=AuditAction.EXPIRE,
                entry_id=entry_id,
                detail={
                    "expired_at": now_text,
                    "expired_by": expired_by,
                    "reason": "write-time renewal",
                },
            )

    def _insert_observation(  # noqa: PLR0913
        self,
        *,
        entry_id: str,
        writer_model: str,
        claim_hash: str,
        recorded_at: datetime,
        confidence: Confidence,
        observed_at: datetime | None,
        valid_from: date | None,
        valid_until: date | None,
        subject_keys: tuple[str, ...],
        evidence: tuple[Evidence, ...],
    ) -> bool:
        inserted = self._connection.execute(
            """
            INSERT OR IGNORE INTO entry_observations(
                entry_id, writer_model, claim_hash, recorded_at, confidence,
                observed_at, valid_from, valid_until, subject_keys, evidence
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                entry_id,
                writer_model,
                claim_hash,
                _format_datetime(recorded_at),
                confidence.value,
                _format_optional_datetime(observed_at),
                None if valid_from is None else valid_from.isoformat(),
                None if valid_until is None else valid_until.isoformat(),
                _encode_json(list(subject_keys)),
                _encode_evidence(evidence),
            ),
        )
        return inserted.rowcount == 1

    def _observation_exists(
        self,
        *,
        entry_id: str,
        writer_model: str,
        claim_hash: str,
    ) -> bool:
        row = self._connection.execute(
            """
            SELECT 1
            FROM entry_observations
            WHERE entry_id = ? AND writer_model = ? AND claim_hash = ?
            """,
            (entry_id, writer_model, claim_hash),
        ).fetchone()
        return row is not None

    def _apply_supersessions_in_transaction(
        self,
        *,
        old_ids: Sequence[str],
        new_id: str,
        actor: str,
        now: datetime,
    ) -> None:
        for old_id in old_ids:
            self._supersede_in_transaction(
                old_id=old_id,
                new_id=new_id,
                actor=actor,
                now=now,
                allow_candidate_old=False,
                classify_legacy_old=True,
            )

    def _supersede_in_transaction(  # noqa: C901, PLR0912, PLR0913
        self,
        *,
        old_id: str,
        new_id: str,
        actor: str,
        now: datetime,
        allow_candidate_old: bool,
        classify_legacy_old: bool,
    ) -> bool:
        if old_id == new_id:
            raise StoreValidationError("An entry cannot supersede itself")
        existing_link = self._connection.execute(
            """
            SELECT new_entry_id
            FROM entry_supersessions
            WHERE old_entry_id = ?
            """,
            (old_id,),
        ).fetchone()
        if existing_link is not None:
            if str(existing_link["new_entry_id"]) == new_id:
                return False
            raise StoreValidationError(f"Entry already has a different replacement: {old_id}")
        old_row = self._fetch_entry_row(old_id)
        new_row = self._fetch_entry_row(new_id)
        if old_row is None:
            raise StoreValidationError(f"Superseded entry does not exist: {old_id}")
        if new_row is None:
            raise StoreValidationError(f"Replacement entry does not exist: {new_id}")
        old = self._materialize_entry(old_row)
        new = self._materialize_entry(new_row)
        if old.kind is not new.kind or old.scope != new.scope:
            raise StoreValidationError("Supersession entries must share kind and scope")
        if not _is_classified_trusted_active(new):
            raise StoreValidationError("Replacement must be an active classified trusted entry")
        if allow_candidate_old:
            if not _is_attestable_candidate(old):
                raise StoreValidationError("Only a live candidate can be merged during attestation")
        elif (
            classify_legacy_old
            and _is_trusted_active(old)
            and not _is_classified_trusted_active(old)
            and old.claim_key is None
        ):
            if new.claim_key is None:  # pragma: no cover - replacement checked above
                raise RuntimeError("Classified replacement has no claim_key")
            self._connection.execute(
                "UPDATE entries SET claim_key = ? WHERE id = ?",
                (new.claim_key, old.id),
            )
            self._append_audit(
                ts=now,
                actor=actor,
                action=AuditAction.CLASSIFY,
                entry_id=old.id,
                detail={
                    "claim_key": new.claim_key,
                    "reason": "legacy supersession classification",
                },
            )
            refreshed_old = self._fetch_entry_row(old.id)
            if refreshed_old is None:  # pragma: no cover - transaction invariant
                raise RuntimeError("Classified legacy entry disappeared")
            old = self._materialize_entry(refreshed_old)
        elif not _is_classified_trusted_active(old):
            raise StoreValidationError("Only an active classified trusted entry can be superseded")
        if new.stale:
            raise StoreValidationError("Replacement must not be stale")
        if not allow_candidate_old and old.claim_key != new.claim_key:
            raise StoreValidationError("Supersession entries must share claim_key")
        if not allow_candidate_old and old.stale:
            raise StoreValidationError("Superseded entry is stale")
        if not allow_candidate_old and old.expires_at is not None and old.expires_at <= now:
            raise StoreValidationError("Superseded entry is TTL-expired")
        if not allow_candidate_old and not _is_business_valid_on(old, now.date()):
            raise StoreValidationError("Superseded entry is outside its business validity window")
        if new.expires_at is not None and new.expires_at <= now:
            raise StoreValidationError("Replacement is TTL-expired")
        if not _is_business_valid_on(new, now.date()):
            raise StoreValidationError("Replacement is outside its business validity window")
        if self._would_create_supersession_cycle(old_id=old_id, new_id=new_id):
            raise StoreValidationError("Supersession would create a cycle")
        self._connection.execute(
            """
            INSERT INTO entry_supersessions(
                old_entry_id, new_entry_id, recorded_at, actor
            ) VALUES (?, ?, ?, ?)
            """,
            (old_id, new_id, _format_datetime(now), actor),
        )
        self._append_audit(
            ts=now,
            actor=actor,
            action=AuditAction.SUPERSEDE,
            entry_id=old_id,
            detail={"new_entry_id": new_id},
        )
        return True

    def _would_create_supersession_cycle(self, *, old_id: str, new_id: str) -> bool:
        row = self._connection.execute(
            """
            WITH RECURSIVE descendants(entry_id) AS (
                VALUES (?)
                UNION
                SELECT link.new_entry_id
                FROM entry_supersessions AS link
                JOIN descendants
                  ON link.old_entry_id = descendants.entry_id
            )
            SELECT 1
            FROM descendants
            WHERE entry_id = ?
            LIMIT 1
            """,
            (new_id, old_id),
        ).fetchone()
        return row is not None

    def _widen_subject_keys(
        self,
        *,
        row: sqlite3.Row,
        existing: Entry,
        incoming: tuple[str, ...],
    ) -> Entry:
        """Attach the corroborating keys to the entry, so they can be searched.

        A second remember from the same writer carrying extra subject keys
        reported `corroborated` and recorded those keys in the observation only.
        Nothing reads them from there: the entry kept its original keys, the
        lexical index kept them too, and the key the caller added to make the
        memory findable never made it findable. Measured: `coldstart` added on a
        corroboration returned zero hits.

        The union is written instead. Keys are discovery hints rather than
        identity, and the writer already owns this candidate, so widening its own
        hints changes nothing about what the entry claims. A union that would
        exceed the configured maximum is refused rather than truncated, because
        dropping the overflow is the same silent loss one level along.
        """
        merged = tuple(dict.fromkeys((*existing.subject_keys, *incoming)))
        if merged == existing.subject_keys:
            return existing
        limit = self._config.limits.max_subject_keys
        if len(merged) > limit:
            raise StoreValidationError(
                f"Corroboration would give the entry {len(merged)} subject keys, above the "
                f"configured limit of {limit}; remember it with fewer keys, or attest a "
                "replacement"
            )
        encoded = _encode_json(list(merged))
        self._delete_fts_row(row)
        self._connection.execute(
            "UPDATE entries SET subject_keys = ? WHERE id = ?",
            (encoded, existing.id),
        )
        self._connection.execute(
            "INSERT INTO entries_fts(rowid, statement, subject_keys) VALUES (?, ?, ?)",
            (int(row["entry_rowid"]), existing.statement, encoded),
        )
        widened_row = self._fetch_entry_row(existing.id)
        if widened_row is None:  # pragma: no cover - protected by the transaction
            raise RuntimeError("Corroborated entry disappeared")
        return self._materialize_entry(widened_row)

    def _attest_existing_candidate(  # noqa: PLR0913
        self,
        *,
        row: sqlite3.Row,
        existing: Entry,
        claim_key: str | None,
        subject_keys: tuple[str, ...],
        source_type: SourceType,
        confidence: Confidence,
        observed_at: datetime | None,
        valid_from: date | None,
        valid_until: date | None,
        evidence: tuple[Evidence, ...],
        now: datetime,
    ) -> Entry:
        """Promote matching canonical candidate content without duplicating its identity.

        The lifetime restarts here. A candidate carries the short expiry its kind
        gives a model's guess, and promoting it in place used to leave that clock
        running: a human attestation made half an hour before the candidate was
        due inherited half an hour of life, then expired and disappeared from
        recall. Measured on both kinds that expire -- episode and project_state --
        against the same attestation made with no candidate present, which lived
        the full term. `recorded_at` deliberately stays as it was, because it
        records when the content was first seen rather than when it was trusted,
        so the recomputed expiry is no longer `recorded_at` plus the lifetime.
        """
        ttl_days = self._config.ttl_days.for_kind(existing.kind)
        expires_at = None if ttl_days == 0 else now + timedelta(days=ttl_days)
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
                valid_until = ?, expires_at = ?, evidence = ?, claim_key = ?
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
                _format_optional_datetime(expires_at),
                _encode_evidence(updated_evidence),
                claim_key,
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
        return self._materialize_entry(updated_row)

    def _insert_entry(self, entry: Entry) -> None:
        encoded_subject_keys = _encode_json(list(entry.subject_keys))
        cursor = self._connection.execute(
            """
            INSERT INTO entries (
                id, kind, scope, statement, subject_keys, status, promotion_state,
                source_type, writer_model, confidence, observed_at, recorded_at,
                valid_from, valid_until, expires_at, idempotency_key, supersedes,
                evidence, is_stale, datacron_ref, datacron_hash, synced_at,
                canonical_key, claim_key
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?
            )
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
                entry.canonical_key,
                entry.claim_key,
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

    def _materialize_entry(self, row: sqlite3.Row) -> Entry:
        return _entry_from_row(
            row,
            max_statement_chars=self._config.limits.max_statement_chars,
            max_subject_keys=self._config.limits.max_subject_keys,
        )

    def _ensure_open(self) -> None:
        if self._closed:
            raise StoreClosedError("EngramStore is closed")

    def _now(self) -> datetime:
        return _aware_datetime(self._clock(), "clock result")


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _database_data_version(connection: sqlite3.Connection) -> int:
    row = connection.execute("PRAGMA data_version").fetchone()
    if row is None or type(row[0]) is not int:  # pragma: no cover - SQLite invariant
        raise RuntimeError("SQLite returned an invalid data_version")
    return int(row[0])


def _encode_vector(vector: Sequence[float]) -> tuple[bytes, int]:
    if len(vector) > MAX_VECTOR_DIMENSIONS:
        raise StoreValidationError(f"vector exceeds limit of {MAX_VECTOR_DIMENSIONS} dimensions")
    try:
        values = tuple(float(value) for value in vector)
    except (OverflowError, TypeError, ValueError) as exc:
        raise StoreValidationError("vector values must be numeric float32 values") from exc
    if not values:
        raise StoreValidationError("vector must not be empty")
    if not all(math.isfinite(value) for value in values):
        raise StoreValidationError("vector values must be finite")
    if any(abs(value) > FLOAT32_MAX for value in values):
        raise StoreValidationError("vector values must fit in float32")
    if not any(values):
        raise StoreValidationError("vector must have a non-zero norm")
    try:
        encoded = struct.pack(f"<{len(values)}f", *values)
    except (OverflowError, struct.error) as exc:
        raise StoreValidationError("vector values must fit in float32") from exc
    return encoded, len(values)


def _decode_vector(vector_blob: bytes, dimension: int) -> tuple[float, ...]:
    if dimension <= 0 or dimension > MAX_VECTOR_DIMENSIONS or len(vector_blob) != dimension * 4:
        raise StoreValidationError("stored vector has an invalid dimension")
    values = tuple(struct.unpack(f"<{dimension}f", vector_blob))
    if not all(math.isfinite(value) for value in values):
        raise StoreValidationError("stored vector contains a non-finite value")
    if not any(values):
        raise StoreValidationError("stored vector has a zero norm")
    return values


def _decode_retrieval_vector(row: sqlite3.Row) -> tuple[float, ...] | None:
    """Treat one invalid derived vector as missing instead of failing recall."""
    if row["vector_blob"] is None:
        if bool(row["vector_invalid"]):
            LOGGER.warning("Stored hybrid vector is invalid; semantic coverage is incomplete")
        return None
    try:
        return _decode_vector(
            bytes(row["vector_blob"]),
            int(row["vector_dim"]),
        )
    except (StoreValidationError, TypeError, ValueError, OverflowError, struct.error):
        LOGGER.warning("Stored hybrid vector is invalid; semantic coverage is incomplete")
        return None


def _enum_value[EnumType: StrEnum](
    enum_class: type[EnumType], value: EnumType | str, field_name: str
) -> EnumType:
    try:
        return enum_class(value)
    except (TypeError, ValueError) as exc:
        raise StoreValidationError(f"Invalid {field_name}: {value}") from exc


def _normalize_evidence(values: Sequence[Evidence]) -> tuple[Evidence, ...]:
    if isinstance(values, str) or not isinstance(values, Sequence):
        raise StoreValidationError("evidence must be a sequence of Evidence instances")
    if len(values) > MAX_EVIDENCE_ITEMS:
        raise StoreValidationError(f"evidence exceeds limit of {MAX_EVIDENCE_ITEMS} items")
    normalized: list[Evidence] = []
    for evidence in values:
        if not isinstance(evidence, Evidence):
            raise StoreValidationError("evidence items must be Evidence instances")
        reference = _normalize_evidence_ref(evidence.ref)
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


def _observation_hash(  # noqa: PLR0913
    *,
    canonical_key: str,
    writer_model: str,
    confidence: Confidence,
    observed_at: datetime | None,
    valid_from: date | None,
    valid_until: date | None,
    subject_keys: tuple[str, ...],
    evidence: tuple[Evidence, ...],
) -> str:
    payload = {
        "canonical_key": canonical_key,
        "confidence": confidence.value,
        "evidence": [
            {"ref": item.ref, "type": item.type.value}
            for item in sorted(evidence, key=lambda item: (item.type.value, item.ref))
        ],
        "observed_at": _format_optional_datetime(observed_at),
        "subject_keys": sorted(subject_keys),
        "valid_from": None if valid_from is None else valid_from.isoformat(),
        "valid_until": None if valid_until is None else valid_until.isoformat(),
        "writer_model": writer_model,
    }
    return hashlib.sha256(_encode_json(payload).encode("utf-8")).hexdigest()


def _normalize_entry_ids(values: Sequence[str], field_name: str) -> tuple[str, ...]:
    if isinstance(values, str):
        raise StoreValidationError(f"{field_name} must be a sequence of entry IDs")
    result: list[str] = []
    for value in values:
        normalized = _normalize_entry_id(value, field_name)
        if normalized not in result:
            result.append(normalized)
    return tuple(result)


def _retrieval_limit(limit: int, *, allow_overflow_probe: bool = False) -> int:
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise StoreValidationError("limit must be an integer")
    maximum = MAX_FTS_TOP_K + int(allow_overflow_probe)
    if not 1 <= limit <= maximum:
        raise StoreValidationError(f"limit must be between 1 and {maximum}")
    return limit


def _active_retrieval_visibility_sql(
    *,
    now: datetime,
    today: str,
) -> tuple[list[str], list[object]]:
    return (
        [
            "entries.is_stale = 0",
            "(entries.expires_at IS NULL OR entries.expires_at > ?)",
            "(entries.valid_from IS NULL OR entries.valid_from <= ?)",
            "(entries.valid_until IS NULL OR entries.valid_until >= ?)",
            "entries.status = ?",
        ],
        [
            _format_datetime(now),
            today,
            today,
            EntryStatus.ACTIVE.value,
        ],
    )


def _retrieval_visibility_sql(
    *,
    now: datetime,
    today: str,
    writer_model: str | None,
) -> tuple[list[str], list[object]]:
    clauses, parameters = _active_retrieval_visibility_sql(now=now, today=today)
    if writer_model is None:
        return clauses, parameters
    clauses[-1] = "(entries.status = ? OR (entries.status = ? AND entries.writer_model = ?))"
    parameters.extend((EntryStatus.QUARANTINED.value, writer_model))
    return clauses, parameters


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


def _observation_from_row(row: sqlite3.Row) -> EntryObservation:
    return EntryObservation(
        entry_id=str(row["entry_id"]),
        writer_model=str(row["writer_model"]),
        claim_hash=str(row["claim_hash"]),
        recorded_at=_parse_datetime(str(row["recorded_at"])),
        confidence=Confidence(str(row["confidence"])),
        observed_at=_parse_optional_datetime(row["observed_at"]),
        valid_from=_parse_optional_date(row["valid_from"]),
        valid_until=_parse_optional_date(row["valid_until"]),
        subject_keys=tuple(_decode_string_array(str(row["subject_keys"]))),
        evidence=_decode_evidence(str(row["evidence"])),
    )


def _entry_from_row(
    row: sqlite3.Row,
    *,
    max_statement_chars: int,
    max_subject_keys: int,
) -> Entry:
    entry = Entry(
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
        canonical_key=str(row["canonical_key"]),
        claim_key=None if row["claim_key"] is None else str(row["claim_key"]),
        supersedes=tuple(_decode_string_array(str(row["supersedes"]))),
        evidence=_decode_evidence(str(row["evidence"])),
        stale=bool(int(row["is_stale"])),
        datacron_ref=None if row["datacron_ref"] is None else str(row["datacron_ref"]),
        datacron_hash=None if row["datacron_hash"] is None else str(row["datacron_hash"]),
        synced_at=_parse_optional_datetime(row["synced_at"]),
    )
    _validate_stored_entry(
        entry,
        max_statement_chars=max_statement_chars,
        max_subject_keys=max_subject_keys,
    )
    return entry


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
        "canonical_key": entry.canonical_key,
        "claim_key": entry.claim_key,
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


def _validate_stored_entry(
    entry: Entry,
    *,
    max_statement_chars: int,
    max_subject_keys: int,
) -> None:
    untrusted = (
        entry.promotion_state in {PromotionState.CANDIDATE, PromotionState.REJECTED}
        and entry.source_type in {SourceType.MODEL_INFERRED, SourceType.SESSION_SUMMARY}
        and entry.writer_model is not None
        and entry.confidence is not Confidence.HIGH
        and entry.claim_key is None
        and (
            (
                entry.status is EntryStatus.QUARANTINED
                and entry.promotion_state is PromotionState.CANDIDATE
            )
            or entry.status in {EntryStatus.SUPERSEDED, EntryStatus.EXPIRED}
        )
    )
    trusted = (
        entry.promotion_state in {PromotionState.APPROVED, PromotionState.PROMOTED}
        and entry.source_type in {SourceType.HUMAN, SourceType.TOOL_VERIFIED}
        and entry.writer_model is None
        and entry.status in {EntryStatus.ACTIVE, EntryStatus.SUPERSEDED, EntryStatus.EXPIRED}
    )
    promoted_fields = (
        entry.datacron_ref is not None
        and entry.datacron_hash is not None
        and entry.synced_at is not None
    )
    unpromoted_fields = (
        entry.datacron_ref is None
        and entry.datacron_hash is None
        and entry.synced_at is None
        and not entry.stale
    )
    datacron_valid = (entry.promotion_state is PromotionState.PROMOTED and promoted_fields) or (
        entry.promotion_state is not PromotionState.PROMOTED and unpromoted_fields
    )
    if not (untrusted or trusted) or not datacron_valid:
        raise RuntimeError(f"Stored entry violates lifecycle invariant: {entry.id}")
    try:
        validate_persisted_content(
            kind=entry.kind,
            entry_id=entry.id,
            scope=entry.scope,
            statement=entry.statement,
            subject_keys=entry.subject_keys,
            canonical=entry.canonical_key,
            idempotency_key=entry.idempotency_key,
            claim_key=entry.claim_key,
            trusted=trusted,
            max_statement_chars=max_statement_chars,
            max_subject_keys=max_subject_keys,
        )
        if entry.writer_model is not None:
            _require_equal(
                _normalize_writer_model(entry.writer_model),
                entry.writer_model,
                "stored writer_model is not normalized",
            )
        _require_equal(
            _normalize_evidence(entry.evidence),
            entry.evidence,
            "stored evidence is not normalized",
        )
        if (
            entry.datacron_ref is not None
            and _normalize_datacron_ref(entry.datacron_ref) != entry.datacron_ref
        ):
            raise StoreValidationError(  # noqa: TRY301
                "stored datacron_ref is not normalized"
            )
        if entry.datacron_hash is not None:
            _normalize_sha256_hex(entry.datacron_hash, "datacron_hash")
    except StoreValidationError as exc:
        raise RuntimeError(
            f"Stored entry violates normalized content invariant: {entry.id}"
        ) from exc


def _require_equal(actual: object, expected: object, message: str) -> None:
    if actual != expected:
        raise StoreValidationError(message)


def _require_attestation_adds_nothing(  # noqa: PLR0913
    existing: Entry,
    *,
    source_type: SourceType,
    confidence: Confidence,
    observed_at: datetime | None,
    valid_from: date | None,
    valid_until: date | None,
    subject_keys: tuple[str, ...],
    evidence: tuple[Evidence, ...],
) -> None:
    """Refuse a re-attestation whose metadata this entry cannot be given.

    Attesting content that already has an active trusted entry returned that
    entry and reported success while every field the caller supplied was dropped:
    different subject keys, a different confidence, new evidence, a different
    validity window. Nothing recorded the loss either, because the audit row is
    content-free, and nothing could undo it -- no path updates a trusted entry's
    metadata in place, deliberately, since attested content is not rewritten
    without history.

    So the honest answer is a refusal that names what differs. The caller who
    genuinely wants different metadata supersedes the entry, which keeps both
    generations.
    """
    differing = sorted(
        name
        for name, matches in (
            ("source_type", existing.source_type is source_type),
            ("confidence", existing.confidence is confidence),
            ("observed_at", existing.observed_at == observed_at),
            ("valid_from", existing.valid_from == valid_from),
            ("valid_until", existing.valid_until == valid_until),
            ("subject_keys", existing.subject_keys == subject_keys),
            ("evidence", existing.evidence == evidence),
        )
        if not matches
    )
    if differing:
        raise StoreValidationError(
            "Canonical trusted content already exists with different "
            f"{', '.join(differing)}; supersede it instead of re-attesting"
        )


def _entry_matches_observation(  # noqa: PLR0913
    entry: Entry,
    *,
    confidence: Confidence,
    observed_at: datetime | None,
    valid_from: date | None,
    valid_until: date | None,
    subject_keys: tuple[str, ...],
    evidence: tuple[Evidence, ...],
) -> bool:
    return (
        entry.confidence is confidence
        and entry.observed_at == observed_at
        and entry.valid_from == valid_from
        and entry.valid_until == valid_until
        and entry.subject_keys == subject_keys
        and entry.evidence == evidence
    )


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


def _is_classified_trusted_active(entry: Entry) -> bool:
    return _is_trusted_active(entry) and (
        entry.kind is EntryKind.EPISODE or entry.claim_key is not None
    )


def _require_promotable(entry: Entry) -> None:
    if entry.status is not EntryStatus.ACTIVE:
        raise StoreValidationError("Only active entries can be promoted")
    if entry.promotion_state is not PromotionState.APPROVED:
        raise StoreValidationError("Only approved entries can be promoted")
    if entry.source_type not in {SourceType.HUMAN, SourceType.TOOL_VERIFIED}:
        raise StoreValidationError("Only human or tool_verified entries can be promoted")
    if entry.kind is not EntryKind.EPISODE and entry.claim_key is None:
        raise StoreValidationError(
            "Legacy trusted entry requires claim_key classification before promotion"
        )


def _is_business_valid_on(entry: Entry, today: date) -> bool:
    return (entry.valid_from is None or entry.valid_from <= today) and (
        entry.valid_until is None or today <= entry.valid_until
    )
