# Copyright 2026 Julien Bombled
# SPDX-License-Identifier: Apache-2.0

"""Deterministic planning, reviewed application, and freshness checks."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from pathlib import PurePosixPath

from engram.config import DatacronConfig
from engram.eval.consolidation import propose_consolidation
from engram.eval.models import ConsolidationClass, ConsolidationStatement
from engram.models import Entry, EntryStatus, PromotionState, SourceType
from engram.store import EngramStore, StoreValidationError

from .gateway import DatacronConflictError, DatacronGateway, DatacronGatewayError
from .models import (
    ApplyOutcome,
    ApplyReport,
    ApplyStatus,
    ConsolidationAction,
    ConsolidationPlan,
    ContradictionSignal,
    FreshnessOutcome,
    FreshnessReport,
    NeighborSection,
    Proposition,
    ReviewDecision,
)

SLUG_PATTERN = re.compile(r"[^a-z0-9]+")


class ConsolidationService:
    """Coordinate Engram state with Datacron through an injected gateway."""

    def __init__(
        self,
        store: EngramStore,
        gateway: DatacronGateway,
        config: DatacronConfig,
    ) -> None:
        """Bind storage, Datacron boundary, and consolidation configuration."""
        self._store = store
        self._gateway = gateway
        self._config = config

    def plan(self) -> ConsolidationPlan:
        """Build a stable review artifact without mutating either data source."""
        entries = sorted(
            (entry for entry in self._store.list_entries() if _is_promotable(entry)),
            key=lambda entry: entry.id,
        )
        signal = self._gateway.contradiction_scan() if entries else None
        propositions = tuple(self._plan_entry(entry, signal) for entry in entries)
        return ConsolidationPlan(propositions=propositions)

    def apply(self, plan: ConsolidationPlan) -> ApplyReport:
        """Apply approved propositions with preread CAS and post-write verification."""
        outcomes: list[ApplyOutcome] = []
        for proposition in plan.propositions:
            if proposition.decision is not ReviewDecision.APPROVE:
                reason = (
                    "rejected by reviewer"
                    if proposition.decision is ReviewDecision.REJECT
                    else "decision still pending"
                )
                outcomes.append(_outcome(proposition, ApplyStatus.SKIPPED, reason))
                continue
            outcomes.append(self._apply_approved(proposition))
        return ApplyReport(outcomes=tuple(outcomes))

    def check_freshness(self) -> FreshnessReport:
        """Compare every promoted content address and update only Engram freshness state."""
        outcomes: list[FreshnessOutcome] = []
        promoted = sorted(
            (
                entry
                for entry in self._store.list_entries()
                if entry.promotion_state is PromotionState.PROMOTED
                and entry.datacron_ref is not None
                and entry.datacron_hash is not None
            ),
            key=lambda entry: entry.id,
        )
        for entry in promoted:
            rel_path = entry.datacron_ref
            expected_hash = entry.datacron_hash
            if rel_path is None or expected_hash is None:  # narrowed by the selection above
                continue
            note = self._gateway.get_note(rel_path)
            current_hash = None if note is None else note.content_hash
            stale = current_hash != expected_hash
            self._store.set_stale(
                entry.id,
                stale=stale,
                actor="consolidate:freshness",
            )
            outcomes.append(
                FreshnessOutcome(
                    candidate_id=entry.id,
                    rel_path=rel_path,
                    stale=stale,
                    expected_hash=expected_hash,
                    current_hash=current_hash,
                )
            )
        return FreshnessReport(outcomes=tuple(outcomes))

    def _plan_entry(
        self,
        entry: Entry,
        signal: ContradictionSignal | None,
    ) -> Proposition:
        neighbors = tuple(
            sorted(
                self._gateway.search_neighbors(
                    entry.subject_keys,
                    entry.scope,
                    self._config.neighbor_limit,
                ),
                key=lambda item: (
                    -item.heading_level,
                    item.rel_path,
                    item.heading,
                    item.content_hash,
                ),
            )
        )
        candidate = ConsolidationStatement(
            statement_id=entry.id,
            statement=entry.statement,
            subject_keys=entry.subject_keys,
        )
        durable = tuple(
            ConsolidationStatement(
                statement_id=f"{neighbor.rel_path}#{neighbor.heading}",
                statement=neighbor.statement,
                subject_keys=neighbor.subject_keys,
            )
            for neighbor in neighbors
        )
        classification = propose_consolidation((candidate,), durable)[0].classification
        action = _action_for(classification)
        heading = _candidate_heading(entry)
        expected_hash: str | None = None
        if classification is ConsolidationClass.NEW:
            rel_path = self._available_new_path(entry)
            content = _render_new_note(heading, entry)
        else:
            target = _target_neighbor(neighbors)
            rel_path = target.rel_path
            heading = target.heading
            expected_hash = target.content_hash
            content = _render_section(entry)
        return Proposition(
            candidate_id=entry.id,
            classification=classification,
            proposed_action=action,
            rel_path=rel_path,
            heading=heading,
            new_content=content,
            expected_hash=expected_hash,
            neighbors=neighbors,
            contradiction_signal=signal,
        )

    def _available_new_path(self, entry: Entry) -> str:
        directory = self._config.new_note_directory.rstrip("/")
        slug = _slug(entry.subject_keys[0] if entry.subject_keys else entry.id)
        primary = f"{directory}/{slug}.md"
        if self._gateway.get_note(primary) is None:
            return primary
        return f"{directory}/{slug}-{entry.id.casefold()}.md"

    def _apply_approved(self, proposition: Proposition) -> ApplyOutcome:  # noqa: PLR0911
        try:
            _validate_reviewed_proposition(proposition)
            entry = self._store.get_entry(proposition.candidate_id)
            if entry is None:
                return _outcome(proposition, ApplyStatus.FAILED, "candidate no longer exists")
            if entry.promotion_state is PromotionState.PROMOTED:
                return self._already_promoted(proposition, entry)
            if not _is_promotable(entry):
                return _outcome(
                    proposition,
                    ApplyStatus.FAILED,
                    "candidate is no longer promotable",
                )
            _validate_candidate_semantics(proposition, entry)
            proposition = proposition.model_copy(
                update={"new_content": _canonical_content(proposition, entry)}
            )
            if proposition.classification is ConsolidationClass.CONTRADICTORY:
                return _outcome(
                    proposition,
                    ApplyStatus.SKIPPED,
                    "contradiction requires a separate human resolution",
                )
            if proposition.classification is ConsolidationClass.REDUNDANT:
                return self._promote_redundant(proposition, entry)
            return self._write_and_promote(proposition, entry)
        except DatacronConflictError as exc:
            return _outcome(proposition, ApplyStatus.STALE, str(exc))
        except (DatacronGatewayError, StoreValidationError, ValueError) as exc:
            return _outcome(proposition, ApplyStatus.FAILED, str(exc))

    def _already_promoted(self, proposition: Proposition, entry: Entry) -> ApplyOutcome:
        if entry.datacron_ref != proposition.rel_path or entry.datacron_hash is None:
            return _outcome(proposition, ApplyStatus.FAILED, "candidate was promoted elsewhere")
        note = self._gateway.get_note(entry.datacron_ref)
        if note is None or note.content_hash != entry.datacron_hash:
            self._store.set_stale(entry.id, stale=True, actor="consolidate:apply")
            return _outcome(proposition, ApplyStatus.STALE, "existing promotion hash diverged")
        return _outcome(
            proposition,
            ApplyStatus.SKIPPED,
            "candidate was already promoted and is still current",
            note.content_hash,
        )

    def _promote_redundant(self, proposition: Proposition, entry: Entry) -> ApplyOutcome:
        if proposition.expected_hash is None:
            return _outcome(proposition, ApplyStatus.FAILED, "redundant target has no CAS hash")
        note = self._gateway.get_note(proposition.rel_path)
        if note is None or note.content_hash != proposition.expected_hash:
            raise DatacronConflictError("redundant target changed; generate a new plan")
        if _normalize(entry.statement) not in _normalize(note.content):
            return _outcome(
                proposition,
                ApplyStatus.FAILED,
                "redundant statement is absent from the reread note",
            )
        promoted = self._store.mark_promoted(
            entry.id,
            datacron_ref=proposition.rel_path,
            datacron_hash=note.content_hash,
            actor="consolidate:apply",
        )
        return _outcome(
            proposition,
            ApplyStatus.APPLIED,
            "redundant candidate linked without a Datacron write",
            promoted.datacron_hash,
        )

    def _write_and_promote(self, proposition: Proposition, entry: Entry) -> ApplyOutcome:
        if proposition.proposed_action is ConsolidationAction.CREATE_NOTE:
            if self._gateway.get_note(proposition.rel_path) is not None:
                raise DatacronConflictError("create target appeared; generate a new plan")
            written_hash = self._gateway.create_note(
                proposition.rel_path,
                proposition.new_content,
            )
        else:
            before = self._gateway.get_note(proposition.rel_path)
            if proposition.expected_hash is None:
                return _outcome(proposition, ApplyStatus.FAILED, "patch target has no CAS hash")
            if before is None or before.content_hash != proposition.expected_hash:
                raise DatacronConflictError("patch target changed; generate a new plan")
            written_hash = self._gateway.patch_section(
                proposition.rel_path,
                proposition.heading,
                proposition.new_content,
                proposition.expected_hash,
            )
        after = self._gateway.get_note(proposition.rel_path)
        if (
            after is None
            or after.content_hash != written_hash
            or proposition.new_content.strip() not in after.content
        ):
            return _outcome(
                proposition,
                ApplyStatus.FAILED,
                "Datacron reread did not verify the exact write",
            )
        promoted = self._store.mark_promoted(
            entry.id,
            datacron_ref=proposition.rel_path,
            datacron_hash=after.content_hash,
            actor="consolidate:apply",
        )
        return _outcome(
            proposition,
            ApplyStatus.APPLIED,
            "Datacron write verified and Engram promotion recorded",
            promoted.datacron_hash,
        )


def _is_promotable(entry: Entry) -> bool:
    return (
        entry.status is EntryStatus.ACTIVE
        and entry.promotion_state is PromotionState.APPROVED
        and entry.source_type in {SourceType.HUMAN, SourceType.TOOL_VERIFIED}
    )


def _action_for(classification: ConsolidationClass) -> ConsolidationAction:
    if classification is ConsolidationClass.NEW:
        return ConsolidationAction.CREATE_NOTE
    if classification is ConsolidationClass.UPDATE:
        return ConsolidationAction.PATCH_SECTION
    return ConsolidationAction.SKIP


def _target_neighbor(neighbors: Sequence[NeighborSection]) -> NeighborSection:
    if not neighbors:
        raise ValueError("A non-new consolidation proposal requires a durable neighbor")
    return neighbors[0]


def _candidate_heading(entry: Entry) -> str:
    source = entry.subject_keys[0] if entry.subject_keys else entry.kind.value
    words = re.findall(r"[\w]+", source.replace("_", " ").replace("/", " "))
    return " ".join(word.capitalize() for word in words) or "Engram Memory"


def _slug(value: str) -> str:
    ascii_value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    slug = SLUG_PATTERN.sub("-", ascii_value.casefold()).strip("-")
    return slug or "memory"


def _render_section(entry: Entry) -> str:
    return f"{entry.statement}\n\n_Engram source: `{entry.id}`_"


def _render_new_note(heading: str, entry: Entry) -> str:
    return f"# {heading}\n\n{_render_section(entry)}\n"


def _canonical_content(proposition: Proposition, entry: Entry) -> str:
    if proposition.proposed_action is ConsolidationAction.CREATE_NOTE:
        return _render_new_note(proposition.heading, entry)
    return _render_section(entry)


def _normalize(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).split()).casefold()


def _validate_reviewed_proposition(proposition: Proposition) -> None:
    expected_action = _action_for(proposition.classification)
    if proposition.proposed_action is not expected_action:
        raise ValueError("proposed_action does not match the classified operation")
    path = PurePosixPath(proposition.rel_path)
    if path.is_absolute() or ".." in path.parts or path.suffix.casefold() != ".md":
        raise ValueError("rel_path must be a confined Markdown path")
    if not path.parts or path.parts[0] != "_memory":
        raise ValueError("rel_path must remain inside Datacron _memory")
    if not proposition.heading.strip():
        raise ValueError("heading must not be empty")


def _validate_candidate_semantics(proposition: Proposition, entry: Entry) -> None:
    candidate = ConsolidationStatement(
        statement_id=entry.id,
        statement=entry.statement,
        subject_keys=entry.subject_keys,
    )
    durable = tuple(
        ConsolidationStatement(
            statement_id=f"{neighbor.rel_path}#{neighbor.heading}",
            statement=neighbor.statement,
            subject_keys=neighbor.subject_keys,
        )
        for neighbor in proposition.neighbors
    )
    current_class = propose_consolidation((candidate,), durable)[0].classification
    if current_class is not proposition.classification:
        raise ValueError("classification does not match the reviewed candidate evidence")
    expected_body = _render_section(entry).strip()
    if proposition.proposed_action is ConsolidationAction.CREATE_NOTE:
        lines = proposition.new_content.splitlines()
        preview_body = "\n".join(lines[1:]).strip() if lines else ""
    else:
        preview_body = proposition.new_content.strip()
    if preview_body != expected_body:
        raise ValueError("new_content is not the generated candidate preview")


def _outcome(
    proposition: Proposition,
    status: ApplyStatus,
    detail: str,
    content_hash: str | None = None,
) -> ApplyOutcome:
    return ApplyOutcome(
        candidate_id=proposition.candidate_id,
        status=status,
        rel_path=proposition.rel_path,
        content_hash=content_hash,
        detail=detail,
    )
