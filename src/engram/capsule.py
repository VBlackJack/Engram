# Copyright 2026 Julien Bombled
# SPDX-License-Identifier: Apache-2.0

"""Trust-aware recall capsule policy independent from retrieval ranking."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
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
        reasons = ["retrieval rank with recency tie-break"]
        if scope is not None:
            reasons.append(f"scope filter: {scope}")
        if unclassified_ids:
            reasons.append(_unclassified_omission_note(len(unclassified_ids)))
        if conflict_ids and not include_conflicts:
            reasons.append(
                f"{len(conflict_ids)} unresolved conflict entries omitted; "
                "retry with include_conflicts=true"
            )

        candidates: list[tuple[str, CapsuleItem | ConflictItem | PendingItem]] = []
        candidates.extend(("current", item) for item in current)
        candidates.extend(("next_action", item) for item in next_action)
        candidates.extend(("relevant", item) for item in relevant)
        candidates.extend(("conflicts", item) for item in conflicts)
        candidates.extend(("own_pending", item) for item in own_pending)

        capsule = _empty_capsule(scope, reasons)
        omitted = 0
        for section, item in candidates:
            _append_item(capsule, section, item)
            _refresh_sources(capsule)
            if _estimated_tokens(render_capsule_text(capsule)) > token_budget:
                _pop_item(capsule, section)
                omitted += _item_count(item)
                _refresh_sources(capsule)

        if omitted:
            omission_note = f"{omitted} entries omitted, budget"
            capsule.notes.why_returned.append(omission_note)
            while _estimated_tokens(render_capsule_text(capsule)) > token_budget:
                removed = _remove_lowest_priority(capsule)
                if removed == 0:
                    break
                omitted += removed
                capsule.notes.why_returned[-1] = f"{omitted} entries omitted, budget"
                _refresh_sources(capsule)

        text = render_capsule_text(capsule)
        return capsule, text


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


def _empty_capsule(scope: str | None, reasons: list[str]) -> CapsuleResult:
    return CapsuleResult(
        notes=CapsuleNotes(scope_used=scope, why_returned=reasons.copy()),
    )


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


def _pop_item(capsule: CapsuleResult, name: str) -> None:
    if name == "current":
        capsule.current.pop()
    elif name == "next_action":
        capsule.next_action.pop()
    elif name == "relevant":
        capsule.relevant.pop()
    elif name == "conflicts":
        capsule.conflicts.pop()
    elif name == "own_pending":
        capsule.own_pending.pop()
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
        capsule.conflicts,
        capsule.relevant,
        capsule.next_action,
        capsule.current,
    ):
        if section:
            return _item_count(section.pop())
    return 0


def _item_count(item: CapsuleItem | ConflictItem | PendingItem) -> int:
    return len(item.versions) if isinstance(item, ConflictItem) else 1


def _estimated_tokens(text: str) -> int:
    return (len(text) + 3) // 4


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
