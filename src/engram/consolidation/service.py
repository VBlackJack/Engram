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
    ConsolidationPlanSnapshot,
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
        """Build a review artifact and anchor its immutable snapshot in SQLite."""
        entries = sorted(
            (
                entry
                for entry in self._store.list_entries()
                if _is_promotable(entry) and self._store.is_business_valid(entry)
            ),
            key=lambda entry: entry.id,
        )
        signal = self._gateway.contradiction_scan() if entries else None
        propositions = tuple(self._plan_entry(entry, signal) for entry in entries)
        snapshot = ConsolidationPlanSnapshot(propositions=propositions)
        stored = self._store.create_consolidation_plan(snapshot.model_dump_json())
        return ConsolidationPlan(plan_id=stored.plan_id, propositions=propositions)

    def apply(self, plan: ConsolidationPlan) -> ApplyReport:
        """Apply approved propositions with preread CAS and post-write verification."""
        anchored = self._load_and_consume_plan(plan)
        outcomes: list[ApplyOutcome] = []
        potential_write_paths: set[str] = set()
        for proposition in anchored.propositions:
            if proposition.decision is not ReviewDecision.APPROVE:
                reason = (
                    "rejected by reviewer"
                    if proposition.decision is ReviewDecision.REJECT
                    else "decision still pending"
                )
                outcomes.append(_outcome(proposition, ApplyStatus.SKIPPED, reason))
                continue
            outcomes.append(self._apply_approved(proposition, potential_write_paths))
        reconciled = self._reconcile_batch_freshness(outcomes, potential_write_paths)
        return ApplyReport(outcomes=reconciled)

    def _load_and_consume_plan(self, reviewed: ConsolidationPlan) -> ConsolidationPlan:
        stored = self._store.get_consolidation_plan(reviewed.plan_id)
        if stored is None:
            raise StoreValidationError(
                f"consolidation plan is unknown: {reviewed.plan_id}; generate a new plan"
            )
        if stored.consumed_at is not None:
            raise StoreValidationError(
                f"consolidation plan was already consumed: {reviewed.plan_id}; generate a new plan"
            )
        try:
            snapshot = ConsolidationPlanSnapshot.model_validate_json(stored.snapshot_json)
        except ValueError as exc:
            raise StoreValidationError(
                "consolidation plan snapshot is invalid; generate a new plan"
            ) from exc
        normalized_reviewed = tuple(
            proposition.model_copy(update={"decision": ReviewDecision.PENDING})
            for proposition in reviewed.propositions
        )
        if normalized_reviewed != snapshot.propositions:
            raise StoreValidationError("plan was modified; generate a new plan")
        if any(
            proposition.decision is ReviewDecision.PENDING for proposition in reviewed.propositions
        ):
            raise StoreValidationError(
                "consolidation plan has pending decisions; choose approve or reject "
                "for every proposition"
            )
        self._store.consume_consolidation_plan(
            reviewed.plan_id,
            expected_hash=stored.snapshot_hash,
        )
        anchored = tuple(
            proposition.model_copy(update={"decision": reviewed_proposition.decision})
            for proposition, reviewed_proposition in zip(
                snapshot.propositions,
                reviewed.propositions,
                strict=True,
            )
        )
        return reviewed.model_copy(update={"propositions": anchored})

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
                    item.search_rank,
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
        heading_level: int | None = None
        expected_hash: str | None = None
        if classification is ConsolidationClass.NEW:
            rel_path = self._available_new_path(entry)
            content = _render_new_note(heading, entry)
        else:
            target = _target_neighbor(
                neighbors,
                classification=classification,
                candidate_statement=entry.statement,
            )
            rel_path = target.rel_path
            heading = target.heading
            heading_level = target.heading_level
            expected_hash = target.content_hash
            content = _render_section(entry)
        return Proposition(
            candidate_id=entry.id,
            classification=classification,
            proposed_action=action,
            rel_path=rel_path,
            heading=heading,
            heading_level=heading_level,
            new_content=content,
            expected_hash=expected_hash,
            neighbors=neighbors,
            contradiction_signal=signal,
        )

    def _available_new_path(self, entry: Entry) -> str:
        primary, fallback = self._new_paths(entry)
        if self._gateway.get_note(primary) is None:
            return primary
        return fallback

    def _new_paths(self, entry: Entry) -> tuple[str, str]:
        directory = self._config.new_note_directory.rstrip("/")
        slug = _slug(entry.subject_keys[0] if entry.subject_keys else entry.id)
        primary = f"{directory}/{slug}.md"
        return primary, f"{directory}/{slug}-{entry.id.casefold()}.md"

    def _apply_approved(  # noqa: PLR0911
        self,
        proposition: Proposition,
        potential_write_paths: set[str],
    ) -> ApplyOutcome:
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
            if not self._store.is_business_valid(entry):
                return _outcome(
                    proposition,
                    ApplyStatus.FAILED,
                    "candidate is outside its business validity window",
                )
            current = self._plan_entry(entry, self._gateway.contradiction_scan())
            _validate_current_plan(
                proposition,
                current,
                allowed_new_paths=self._new_paths(entry),
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
            return self._write_and_promote(proposition, entry, potential_write_paths)
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

    def _write_and_promote(  # noqa: C901
        self,
        proposition: Proposition,
        entry: Entry,
        potential_write_paths: set[str],
    ) -> ApplyOutcome:
        if proposition.proposed_action is ConsolidationAction.CREATE_NOTE:
            if self._gateway.get_note(proposition.rel_path) is not None:
                raise DatacronConflictError("create target appeared; generate a new plan")
            if not self._store.is_business_valid(entry):
                return _outcome(
                    proposition,
                    ApplyStatus.FAILED,
                    "candidate is outside its business validity window",
                )
            potential_write_paths.add(proposition.rel_path)
            written_hash = self._gateway.create_note(
                proposition.rel_path,
                proposition.new_content,
            )
        else:
            before = self._gateway.get_note(proposition.rel_path)
            if proposition.expected_hash is None:
                return _outcome(proposition, ApplyStatus.FAILED, "patch target has no CAS hash")
            if proposition.heading_level is None:
                return _outcome(
                    proposition,
                    ApplyStatus.FAILED,
                    "patch target has no heading level",
                )
            if before is None or before.content_hash != proposition.expected_hash:
                raise DatacronConflictError("patch target changed; generate a new plan")
            if not self._store.is_business_valid(entry):
                return _outcome(
                    proposition,
                    ApplyStatus.FAILED,
                    "candidate is outside its business validity window",
                )
            potential_write_paths.add(proposition.rel_path)
            written_hash = self._gateway.patch_section(
                proposition.rel_path,
                proposition.heading,
                proposition.heading_level,
                proposition.new_content,
                proposition.expected_hash,
            )
        after = self._gateway.get_note(proposition.rel_path)
        exact_write = (
            proposition.new_content.strip() in after.content if after is not None else False
        )
        if after is not None and proposition.proposed_action is ConsolidationAction.PATCH_SECTION:
            if proposition.heading_level is None:  # narrowed before the write
                exact_write = False
            else:
                exact_write = (
                    _section_content(
                        after.content,
                        proposition.heading,
                        proposition.heading_level,
                    )
                    == proposition.new_content.strip()
                )
        if after is None or after.content_hash != written_hash or not exact_write:
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

    def _reconcile_batch_freshness(
        self,
        outcomes: Sequence[ApplyOutcome],
        potential_write_paths: set[str],
    ) -> tuple[ApplyOutcome, ...]:
        if not potential_write_paths:
            return tuple(outcomes)
        current_hashes: dict[str, str | None] = {}
        for rel_path in sorted(potential_write_paths):
            try:
                note = self._gateway.get_note(rel_path)
            except DatacronGatewayError:
                note = None
            current_hashes[rel_path] = None if note is None else note.content_hash
        stale_ids: set[str] = set()
        for entry in self._store.list_entries():
            if (
                entry.promotion_state is not PromotionState.PROMOTED
                or entry.datacron_ref not in current_hashes
                or entry.datacron_hash is None
            ):
                continue
            stale = current_hashes[entry.datacron_ref] != entry.datacron_hash
            self._store.set_stale(entry.id, stale=stale, actor="consolidate:batch-freshness")
            if stale:
                stale_ids.add(entry.id)
        return tuple(
            outcome.model_copy(
                update={
                    "status": ApplyStatus.STALE,
                    "detail": "promotion hash diverged after a later write in the same batch",
                }
            )
            if outcome.candidate_id in stale_ids
            and outcome.status in {ApplyStatus.APPLIED, ApplyStatus.SKIPPED}
            else outcome
            for outcome in outcomes
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


def _target_neighbor(
    neighbors: Sequence[NeighborSection],
    *,
    classification: ConsolidationClass,
    candidate_statement: str,
) -> NeighborSection:
    if not neighbors:
        raise ValueError("A non-new consolidation proposal requires a durable neighbor")
    if classification is ConsolidationClass.REDUNDANT:
        normalized_candidate = _normalize(candidate_statement)
        exact = next(
            (
                neighbor
                for neighbor in neighbors
                if _normalize(neighbor.statement) == normalized_candidate
            ),
            None,
        )
        if exact is None:
            raise ValueError("A redundant consolidation proposal requires an exact neighbor")
        return exact
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
    if "\\" in proposition.rel_path:
        raise ValueError("rel_path must use normalized forward slashes")
    path = PurePosixPath(proposition.rel_path)
    if path.is_absolute() or ".." in path.parts or path.suffix.casefold() != ".md":
        raise ValueError("rel_path must be a confined Markdown path")
    if path.as_posix() != proposition.rel_path:
        raise ValueError("rel_path must use normalized forward slashes")
    if not path.parts or path.parts[0] != "_memory":
        raise ValueError("rel_path must remain inside Datacron _memory")
    if "\r" in proposition.heading or "\n" in proposition.heading:
        raise ValueError("heading must be a single line")
    if not proposition.heading.strip():
        raise ValueError("heading must not be empty")
    if (
        proposition.proposed_action is ConsolidationAction.PATCH_SECTION
        and proposition.heading_level is None
    ):
        raise ValueError("patch target must include heading_level")


def _validate_current_plan(
    reviewed: Proposition,
    current: Proposition,
    *,
    allowed_new_paths: tuple[str, str],
) -> None:
    immutable_fields = (
        "candidate_id",
        "classification",
        "proposed_action",
    )
    if any(getattr(reviewed, field) != getattr(current, field) for field in immutable_fields):
        raise ValueError("reviewed proposition no longer matches the current deterministic plan")
    if reviewed.classification is ConsolidationClass.NEW:
        if reviewed.expected_hash is not None or reviewed.heading_level is not None:
            raise ValueError("new-note target must not include an existing section selector")
        if reviewed.heading != current.heading or reviewed.rel_path not in allowed_new_paths:
            raise ValueError("reviewed new-note target does not match the current plan")
        if reviewed.rel_path != current.rel_path:
            raise DatacronConflictError("create target changed; generate a new plan")
        return
    if reviewed.classification is ConsolidationClass.REDUNDANT:
        allowed = tuple(
            item
            for item in current.neighbors
            if (
                item.rel_path,
                item.heading,
                item.heading_level,
                item.content_hash,
            )
            == (
                current.rel_path,
                current.heading,
                current.heading_level,
                current.expected_hash,
            )
        )
    else:
        allowed = current.neighbors
    target = (
        reviewed.rel_path,
        reviewed.heading,
        reviewed.heading_level,
        reviewed.expected_hash,
    )
    reviewed_targets = {
        (item.rel_path, item.heading, item.heading_level, item.content_hash)
        for item in reviewed.neighbors
    }
    if target not in reviewed_targets:
        raise ValueError("reviewed target was not present in the reviewed neighbors")
    if target not in {
        (item.rel_path, item.heading, item.heading_level, item.content_hash) for item in allowed
    }:
        raise ValueError("reviewed target is not a current deterministic neighbor")


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


def _section_content(content: str, heading: str, heading_level: int) -> str:
    heading_pattern = re.compile(
        rf"^#{{{heading_level}}}\s+{re.escape(heading)}\s*$",
        flags=re.MULTILINE,
    )
    matches = list(heading_pattern.finditer(content))
    if len(matches) != 1:
        return ""
    match = matches[0]
    following = re.compile(rf"^#{{1,{heading_level}}}\s+", flags=re.MULTILINE).search(
        content,
        match.end(),
    )
    end = len(content) if following is None else following.start()
    body = content[match.end() : end].strip()
    lines = [line for line in body.splitlines() if line.strip() != "</vault_content>"]
    return "\n".join(lines).strip()


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
