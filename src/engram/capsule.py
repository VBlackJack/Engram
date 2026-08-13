# Copyright 2026 Julien Bombled
# SPDX-License-Identifier: Apache-2.0

"""Trust-aware recall capsule policy independent from retrieval ranking."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from hashlib import sha256
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field

from .config import CapsuleConfig
from .models import Entry, EntryKind, EntryStatus
from .retrieval import RetrievalResult, effective_claim_key

CURRENT_KINDS = frozenset(
    {
        EntryKind.PREFERENCE,
        EntryKind.DECISION,
        EntryKind.FACT,
    }
)
SAFE_SCOPE_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,39}\Z")
RANK_REASON = "retrieval rank with recency tie-break"
SCOPE_REASON = "scope filter applied"
BUDGET_NOTE_SUFFIX = " entries omitted, budget"
CAPSULE_BUDGET_UNIT = "serialized_utf8_bytes"
CAPSULE_ESTIMATOR_VERSION = "utf8-bytes-v1"
CALL_RESULT_BYTE_MARGIN = 8
CAPSULE_BUDGET_OVERFLOW_NOTICE = "capsule_budget_overflow"
UNCLASSIFIED_CLAIM_OMITTED_NOTICE = "unclassified_claim_omitted"
CONFLICTS_HIDDEN_BY_REQUEST_NOTICE = "conflicts_hidden_by_request"


class CapsuleItem(BaseModel):
    """Safe fields exposed for one recalled entry."""

    model_config = ConfigDict(extra="forbid")

    id: str
    kind: EntryKind
    statement: str
    confidence: str
    source_type: str
    observed_at: str | None = None
    recorded_at: str | None = None


class PendingItem(CapsuleItem):
    """A caller-owned quarantined candidate."""

    label: Literal["unconfirmed candidate"] = "unconfirmed candidate"


class ConflictItem(BaseModel):
    """Symmetric unresolved versions of one explicit claim."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["unresolved"] = "unresolved"
    claim_key: str
    subject_keys: list[str]
    versions: list[CapsuleItem]


class CapsuleNotes(BaseModel):
    """Scope and selection explanations for a capsule."""

    model_config = ConfigDict(extra="forbid")

    scope_used: str | None
    recall_complete: bool = True
    warnings: list[str] = Field(default_factory=list)
    why_returned: list[str]


class CapsuleResult(BaseModel):
    """Structured recall output mirrored by the text fallback."""

    model_config = ConfigDict(extra="forbid")

    current: list[CapsuleItem] = Field(default_factory=list)
    next_action: list[CapsuleItem] = Field(default_factory=list)
    relevant: list[CapsuleItem] = Field(default_factory=list)
    conflicts: list[ConflictItem] = Field(default_factory=list)
    own_pending: list[PendingItem] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)
    notes: CapsuleNotes


class CapsuleBuilder:
    """Apply D6/D7 trust, conflict, section, and budget policy."""

    def __init__(self, config: CapsuleConfig) -> None:
        """Store immutable capsule limits."""
        self._config = config

    def resolve_budget(self, requested: int | None) -> int:
        """Resolve the default and reject values outside configured bounds."""
        budget = self._config.default_token_budget if requested is None else requested
        if not self._config.min_token_budget <= budget <= self._config.max_token_budget:
            raise ValueError(
                "token_budget must be between "
                f"{self._config.min_token_budget} and {self._config.max_token_budget}"
            )
        return budget

    def build(
        self,
        retrieval: RetrievalResult,
        *,
        scope: str | None,
        include_conflicts: bool,
        token_budget: int,
    ) -> tuple[CapsuleResult, str]:
        """Build structured and text capsules under one shared item budget."""
        selected_entries = _unique_entries((*retrieval.matches, *retrieval.next_actions))
        conflict_groups, conflict_ids = _find_conflicts(selected_entries)
        unclassified_ids = {
            entry.id
            for entry in selected_entries
            if entry.status is EntryStatus.ACTIVE
            and entry.kind in CURRENT_KINDS
            and effective_claim_key(entry) is None
        }
        active_matches = [
            entry
            for entry in retrieval.matches
            if entry.status is EntryStatus.ACTIVE
            and entry.id not in conflict_ids
            and entry.id not in unclassified_ids
        ]
        current = [_capsule_item(entry) for entry in active_matches if entry.kind in CURRENT_KINDS]
        next_action = [
            _capsule_item(entry)
            for entry in retrieval.next_actions
            if entry.id not in conflict_ids and entry.id not in unclassified_ids
        ]
        relevant = [
            _capsule_item(entry) for entry in active_matches if entry.kind is EntryKind.EPISODE
        ]
        conflicts = conflict_groups if include_conflicts else []
        own_pending = [_pending_item(entry) for entry in retrieval.own_pending]
        reasons = [RANK_REASON]
        if scope is not None:
            reasons.append(SCOPE_REASON)
        if unclassified_ids:
            reasons.append(_unclassified_omission_note(len(unclassified_ids)))
        if conflict_ids and not include_conflicts:
            reasons.append(
                f"{len(conflict_ids)} unresolved conflict entries omitted; "
                "retry with include_conflicts=true"
            )

        scope_used = _safe_scope_representation(scope)
        notices = (
            *retrieval.notices,
            *((UNCLASSIFIED_CLAIM_OMITTED_NOTICE,) if unclassified_ids else ()),
            *(
                (CONFLICTS_HIDDEN_BY_REQUEST_NOTICE,)
                if conflict_ids and not include_conflicts
                else ()
            ),
        )
        conflicts, unfittable = _conflicts_within_budget(
            conflicts,
            _empty_capsule(scope_used, reasons, notices),
            token_budget,
        )

        candidates: list[tuple[str, CapsuleItem | ConflictItem | PendingItem]] = []
        candidates.extend(("current", item) for item in current)
        candidates.extend(("next_action", item) for item in next_action)
        candidates.extend(("relevant", item) for item in relevant)
        candidates.extend(("conflicts", item) for item in conflicts)
        candidates.extend(("own_pending", item) for item in own_pending)

        capsule = _empty_capsule(scope_used, reasons, notices)
        for section, item in candidates:
            _append_item(capsule, section, item)
        _refresh_sources(capsule)

        return _fit_to_budget(capsule, token_budget, unfittable)


def estimate_payload_bytes(payload: str) -> int:
    """Upper-bound byte-level subword tokens by serialized UTF-8 bytes."""
    return len(payload.encode("utf-8"))


def estimate_capsule_bytes(
    capsule: CapsuleResult,
    rendered_text: str | None = None,
) -> int:
    """Measure the serialized tool result, including both representations."""
    text = render_capsule_text(capsule) if rendered_text is None else rendered_text
    serialized = json.dumps(
        {
            "meta": None,
            "content": [
                {
                    "type": "text",
                    "text": text,
                    "annotations": None,
                    "meta": None,
                }
            ],
            "structuredContent": capsule.model_dump(mode="json"),
            "isError": False,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return estimate_payload_bytes(serialized) + CALL_RESULT_BYTE_MARGIN


def render_capsule_text(capsule: CapsuleResult) -> str:
    """Render the compact fallback with the same section order as structured output."""
    lines: list[str] = []
    _render_items(lines, "CURRENT", capsule.current)
    _render_items(lines, "NEXT_ACTION", capsule.next_action)
    _render_items(lines, "RELEVANT", capsule.relevant)
    lines.append("CONFLICTS")
    if capsule.conflicts:
        for conflict in capsule.conflicts:
            lines.append(f"- unresolved: {conflict.claim_key}")
            lines.extend(f"  - {_item_line(version)}" for version in conflict.versions)
    else:
        lines.append("- none")
    _render_items(lines, "OWN_PENDING", capsule.own_pending)
    lines.append("SOURCES")
    lines.extend(f"- {entry_id}" for entry_id in capsule.sources)
    if not capsule.sources:
        lines.append("- none")
    lines.append("NOTES")
    lines.append(f"- scope_used: {capsule.notes.scope_used or 'all'}")
    lines.append(f"- recall_complete: {str(capsule.notes.recall_complete).lower()}")
    lines.extend(f"- warning: {warning}" for warning in capsule.notes.warnings)
    lines.extend(f"- {reason}" for reason in capsule.notes.why_returned)
    return "\n".join(lines)


def _find_conflicts(entries: Sequence[Entry]) -> tuple[list[ConflictItem], set[str]]:
    active = [entry for entry in entries if entry.status is EntryStatus.ACTIVE]
    partitions: dict[tuple[EntryKind, str, str], list[Entry]] = {}
    for entry in active:
        claim_key = effective_claim_key(entry)
        if claim_key is not None:
            partitions.setdefault((entry.kind, entry.scope, claim_key), []).append(entry)

    groups = [
        (claim_key, group)
        for (_, _, claim_key), group in partitions.items()
        if len(group) > 1 and len({entry.canonical_key for entry in group}) > 1
    ]

    conflict_items = [
        ConflictItem(
            claim_key=claim_key,
            subject_keys=sorted({key for entry in group for key in entry.subject_keys}),
            versions=[_capsule_item(entry) for entry in group],
        )
        for claim_key, group in groups
    ]
    conflict_ids = {entry.id for _, group in groups for entry in group}
    return conflict_items, conflict_ids


def _fit_to_budget(
    capsule: CapsuleResult,
    token_budget: int,
    omitted: int,
) -> tuple[CapsuleResult, str]:
    """Evict whole items by section priority until the serialized result fits.

    The count carried in is what the caller already refused to include, so the
    reported total covers everything the request lost rather than only what this
    loop removed.
    """
    if omitted:
        _set_budget_note(capsule, omitted)
        _mark_incomplete(capsule, CAPSULE_BUDGET_OVERFLOW_NOTICE)
    if estimate_capsule_bytes(capsule) > token_budget:
        _compact_notes(capsule)
    while estimate_capsule_bytes(capsule) > token_budget:
        removed = _remove_lowest_priority(capsule)
        if removed == 0:
            break
        omitted += removed
        _refresh_sources(capsule)
        _set_budget_note(capsule, omitted)
        _mark_incomplete(capsule, CAPSULE_BUDGET_OVERFLOW_NOTICE)

    text = render_capsule_text(capsule)
    serialized_bytes = estimate_capsule_bytes(capsule, text)
    if serialized_bytes > token_budget:
        _minimize_notes(capsule)
        text = render_capsule_text(capsule)
        serialized_bytes = estimate_capsule_bytes(capsule, text)
    if serialized_bytes > token_budget:
        raise ValueError(
            "token_budget is too small for mandatory capsule metadata: "
            f"requires {serialized_bytes} serialized bytes, received {token_budget}"
        )
    return capsule, text


def _conflicts_within_budget(
    groups: Sequence[ConflictItem],
    probe: CapsuleResult,
    token_budget: int,
) -> tuple[list[ConflictItem], int]:
    """Keep the conflict groups the capsule can actually deliver, and count the rest.

    A group is only ever removed whole, and eviction reaches it last, so a group
    too large to fit on its own made the builder empty every other section to
    make room and then remove the group as well. The caller paid for information
    it never received: asking for conflicts returned strictly less than not
    asking for them, which is the opposite of what the flag promises.

    Deciding here, before anything is evicted, keeps the priority the sections
    were ordered for -- a conflict that fits still outranks lower-priority
    context -- while making sure nothing is sacrificed for a group that will not
    survive. Groups are offered in retrieval rank order and measured
    cumulatively, so a smaller later group can still be kept after a larger one
    is refused.
    """
    kept: list[ConflictItem] = []
    omitted = 0
    for group in groups:
        probe.conflicts.append(group)
        _refresh_sources(probe)
        if estimate_capsule_bytes(probe) > token_budget:
            probe.conflicts.pop()
            omitted += len(group.versions)
        else:
            kept.append(group)
    return kept, omitted


def _unique_entries(entries: Sequence[Entry]) -> tuple[Entry, ...]:
    unique: list[Entry] = []
    seen: set[str] = set()
    for entry in entries:
        if entry.id not in seen:
            seen.add(entry.id)
            unique.append(entry)
    return tuple(unique)


def _unclassified_omission_note(count: int) -> str:
    noun = "entry" if count == 1 else "entries"
    return f"{count} trusted {noun} omitted: claim_key classification required"


def _capsule_item(entry: Entry) -> CapsuleItem:
    observed_at = None if entry.observed_at is None else _format_datetime(entry.observed_at)
    recorded_at = _format_datetime(entry.recorded_at) if observed_at is None else None
    return CapsuleItem(
        id=entry.id,
        kind=entry.kind,
        statement=entry.statement,
        confidence=entry.confidence.value,
        source_type=entry.source_type.value,
        observed_at=observed_at,
        recorded_at=recorded_at,
    )


def _pending_item(entry: Entry) -> PendingItem:
    item = _capsule_item(entry)
    return PendingItem(**item.model_dump())


def _format_datetime(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _empty_capsule(
    scope: str | None,
    reasons: list[str],
    notices: Sequence[str],
) -> CapsuleResult:
    unique_notices = list(dict.fromkeys(notices))
    return CapsuleResult(
        notes=CapsuleNotes(
            scope_used=scope,
            recall_complete=not unique_notices,
            warnings=unique_notices,
            why_returned=reasons.copy(),
        ),
    )


def _safe_scope_representation(scope: str | None) -> str | None:
    if scope is None:
        return None
    if SAFE_SCOPE_PATTERN.fullmatch(scope):
        return scope
    encoded = scope.encode("utf-8", errors="surrogatepass")
    digest = sha256(encoded).hexdigest()[:16]
    return f"<scope#{digest}>"


def _compact_notes(capsule: CapsuleResult) -> None:
    compacted: list[str] = []
    for reason in capsule.notes.why_returned:
        compact = _compact_reason(reason)
        if compact not in compacted:
            compacted.append(compact)
    capsule.notes.why_returned = compacted


def _minimize_notes(capsule: CapsuleResult) -> None:
    reasons = capsule.notes.why_returned
    parts = ["ranked"]
    unclassified = _reason_count(reasons, "unclassified")
    conflicts = _reason_count(reasons, "conflict")
    budget = _reason_count(reasons, "budget")
    if unclassified is not None:
        parts.append(f"unclassified={unclassified}")
    if conflicts is not None:
        parts.append(f"conflicts={conflicts}")
    if budget is not None:
        parts.append(f"budget={budget}")
    capsule.notes.why_returned = ["; ".join(parts)]


def _reason_count(reasons: Sequence[str], marker: str) -> str | None:
    for reason in reasons:
        if marker in reason:
            match = re.search(r"\d+", reason)
            return None if match is None else match.group()
    return None


def _compact_reason(reason: str) -> str:
    if reason == RANK_REASON:
        return "ranked retrieval"
    if reason == SCOPE_REASON or reason.startswith("scope filter:"):
        return "scope filtered"
    if "claim_key classification required" in reason:
        return f"unclassified omitted: {_leading_count(reason)}"
    if "unresolved conflict entries omitted" in reason:
        return f"conflicts omitted: {_leading_count(reason)}"
    return reason


def _leading_count(reason: str) -> str:
    count, separator, _ = reason.partition(" ")
    return count if separator and count.isdecimal() else "unknown"


def _set_budget_note(capsule: CapsuleResult, omitted: int) -> None:
    note = f"{omitted}{BUDGET_NOTE_SUFFIX}"
    for index, reason in enumerate(capsule.notes.why_returned):
        if reason.endswith(BUDGET_NOTE_SUFFIX):
            capsule.notes.why_returned[index] = note
            return
    capsule.notes.why_returned.append(note)


def _mark_incomplete(capsule: CapsuleResult, notice: str) -> None:
    """Persist one bounded completeness warning through later compaction."""
    capsule.notes.recall_complete = False
    if notice not in capsule.notes.warnings:
        capsule.notes.warnings.append(notice)


def _append_item(
    capsule: CapsuleResult,
    name: str,
    item: CapsuleItem | ConflictItem | PendingItem,
) -> None:
    if name == "current":
        capsule.current.append(cast("CapsuleItem", item))
    elif name == "next_action":
        capsule.next_action.append(cast("CapsuleItem", item))
    elif name == "relevant":
        capsule.relevant.append(cast("CapsuleItem", item))
    elif name == "conflicts":
        capsule.conflicts.append(cast("ConflictItem", item))
    elif name == "own_pending":
        capsule.own_pending.append(cast("PendingItem", item))
    else:
        raise ValueError(f"Unknown capsule section: {name}")


def _refresh_sources(capsule: CapsuleResult) -> None:
    identifiers: list[str] = []
    direct_items: Iterable[CapsuleItem] = (
        *capsule.current,
        *capsule.next_action,
        *capsule.relevant,
        *capsule.own_pending,
    )
    for item in direct_items:
        if item.id not in identifiers:
            identifiers.append(item.id)
    for conflict in capsule.conflicts:
        for version in conflict.versions:
            if version.id not in identifiers:
                identifiers.append(version.id)
    capsule.sources = identifiers


def _remove_lowest_priority(capsule: CapsuleResult) -> int:
    for section in (
        capsule.own_pending,
        capsule.relevant,
        capsule.next_action,
        capsule.current,
        capsule.conflicts,
    ):
        if section:
            return _item_count(section.pop())
    return 0


def _item_count(item: CapsuleItem | ConflictItem | PendingItem) -> int:
    return len(item.versions) if isinstance(item, ConflictItem) else 1


def _render_items(
    lines: list[str],
    heading: str,
    items: Sequence[CapsuleItem] | Sequence[PendingItem],
) -> None:
    lines.append(heading)
    if items:
        lines.extend(f"- {_item_line(item)}" for item in items)
    else:
        lines.append("- none")


def _item_line(item: CapsuleItem) -> str:
    timestamp = item.observed_at or item.recorded_at or "unknown-time"
    label = f", {item.label}" if isinstance(item, PendingItem) else ""
    return (
        f"[{item.id}] ({item.kind.value}, {item.confidence}, {item.source_type}{label}, "
        f"{timestamp}) {item.statement}"
    )
