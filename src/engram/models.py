# Copyright 2026 Julien Bombled
# SPDX-License-Identifier: Apache-2.0

"""Typed domain models for Engram storage."""

from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum

PROJECT_STATE_CLAIM_KEY = "project_state/current"


class EntryKind(StrEnum):
    """Supported memory entry kinds."""

    PREFERENCE = "preference"
    DECISION = "decision"
    PROJECT_STATE = "project_state"
    FACT = "fact"
    EPISODE = "episode"


class EntryStatus(StrEnum):
    """Lifecycle status controlled by the store."""

    ACTIVE = "active"
    SUPERSEDED = "superseded"
    QUARANTINED = "quarantined"
    EXPIRED = "expired"


class PromotionState(StrEnum):
    """Promotion workflow status controlled by the store."""

    CANDIDATE = "candidate"
    APPROVED = "approved"
    REJECTED = "rejected"
    PROMOTED = "promoted"


class SourceType(StrEnum):
    """Server-assigned provenance for an entry."""

    HUMAN = "human"
    TOOL_VERIFIED = "tool_verified"
    MODEL_INFERRED = "model_inferred"
    SESSION_SUMMARY = "session_summary"


class Confidence(StrEnum):
    """Confidence levels accepted by the store."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class EvidenceType(StrEnum):
    """Supported evidence reference types."""

    TOOL_RESULT = "tool_result"
    DATACRON_NOTE = "datacron_note"
    HUMAN_MESSAGE = "human_message"
    REVIEW = "review"


class AuditAction(StrEnum):
    """Append-only audit actions emitted by the store."""

    INSERT = "insert"
    ATTEST = "attest"
    CONFIDENCE_CAPPED = "confidence_capped"
    IDEMPOTENT_NOOP = "idempotent_noop"
    SUPERSEDE = "supersede"
    EXPIRE = "expire"
    PURGE = "purge"
    PROMOTE = "promote"
    MARK_STALE = "mark_stale"
    MARK_FRESH = "mark_fresh"
    CORROBORATE = "corroborate"
    CLASSIFY = "classify"


class RememberOutcome(StrEnum):
    """Explicit result of one model candidate write."""

    CREATED = "created"
    RETRY = "retry"
    CORROBORATED = "corroborated"
    EXISTING_TRUSTED = "existing_trusted"
    RENEWED = "renewed"


@dataclass(frozen=True, slots=True)
class Evidence:
    """Opaque evidence reference associated with an entry."""

    type: EvidenceType
    ref: str


@dataclass(frozen=True, slots=True)
class Entry:
    """A fully materialized storage entry."""

    id: str
    kind: EntryKind
    scope: str
    statement: str
    subject_keys: tuple[str, ...]
    status: EntryStatus
    promotion_state: PromotionState
    source_type: SourceType
    writer_model: str | None
    confidence: Confidence
    observed_at: datetime | None
    recorded_at: datetime
    valid_from: date | None
    valid_until: date | None
    expires_at: datetime | None
    idempotency_key: str
    supersedes: tuple[str, ...]
    evidence: tuple[Evidence, ...]
    stale: bool
    datacron_ref: str | None
    datacron_hash: str | None
    synced_at: datetime | None
    canonical_key: str = ""
    claim_key: str | None = None


@dataclass(frozen=True, slots=True)
class AuditRecord:
    """A content-free append-only audit event."""

    seq: int
    ts: datetime
    actor: str
    action: AuditAction
    entry_id: str | None
    detail_hash: str | None


@dataclass(frozen=True, slots=True)
class CandidateWriteResult:
    """Candidate write outcome including retry idempotence information."""

    entry: Entry
    idempotent: bool
    outcome: RememberOutcome


@dataclass(frozen=True, slots=True)
class EntryObservation:
    """One normalized model observation retained behind an entry generation."""

    entry_id: str
    writer_model: str
    claim_hash: str
    recorded_at: datetime
    confidence: Confidence
    observed_at: datetime | None
    valid_from: date | None
    valid_until: date | None
    subject_keys: tuple[str, ...]
    evidence: tuple[Evidence, ...]
