# Copyright 2026 Julien Bombled
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import hashlib
import logging
import re
from datetime import timedelta
from pathlib import Path

import pytest

import engram.cli as cli_module
from engram.cli import _consolidate
from engram.config import AppConfig, DatacronConfig
from engram.consolidation.gateway import DatacronGatewayError, FakeDatacronGateway
from engram.consolidation.mcp_gateway import McpDatacronGateway, _server_parameters
from engram.consolidation.models import (
    ApplyReport,
    ApplyStatus,
    ConsolidationPlan,
    NeighborSection,
    NoteView,
    ReviewDecision,
)
from engram.consolidation.report import model_json, render_plan_markdown
from engram.consolidation.service import ConsolidationService
from engram.eval.models import ConsolidationClass
from engram.models import (
    AuditAction,
    EntryKind,
    PromotionState,
    SourceType,
)
from engram.retrieval import FtsRetriever, RetrievalRequest
from engram.store import EngramStore
from tests.conftest import MutableClock


def _hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _approve(plan: ConsolidationPlan) -> ConsolidationPlan:
    return plan.model_copy(
        update={
            "propositions": tuple(
                item.model_copy(update={"decision": ReviewDecision.APPROVE})
                for item in plan.propositions
            )
        }
    )


class _WrappedReadGateway(FakeDatacronGateway):
    """Model Datacron's sandbox wrapper around content returned by get_note."""

    def get_note(self, rel_path: str) -> NoteView | None:
        note = super().get_note(rel_path)
        if note is None:
            return None
        wrapped = f'<vault_content path="{rel_path}">\n{note.content.rstrip()}\n</vault_content>'
        return note.model_copy(update={"content": wrapped})


def _neighbor(  # noqa: PLR0913
    *,
    rel_path: str,
    heading: str,
    statement: str,
    subject_key: str,
    content: str,
    search_rank: int = 0,
    heading_level: int | None = None,
) -> NeighborSection:
    selected_level = heading_level
    if selected_level is None:
        match = re.search(
            rf"^(?P<marks>#{{1,6}})\s+{re.escape(heading)}\s*$",
            content,
            flags=re.MULTILINE,
        )
        selected_level = len(match.group("marks")) if match is not None else 2
    return NeighborSection(
        rel_path=rel_path,
        heading=heading,
        statement=statement,
        subject_keys=(subject_key,),
        content_hash=_hash(content),
        heading_level=selected_level,
        search_rank=search_rank,
        excerpt=statement,
    )


def test_plan_selects_only_active_approved_attestations(
    store: EngramStore,
    app_config: AppConfig,
) -> None:
    eligible = store.add_attested(
        kind=EntryKind.FACT,
        scope="global",
        statement="The release channel is stable.",
        source_type=SourceType.HUMAN,
        subject_keys=("release/channel",),
    )
    store.add_candidate(
        kind=EntryKind.FACT,
        scope="global",
        statement="An untrusted release guess.",
        writer_model="test-client/1.0",
        subject_keys=("release/channel",),
    )
    gateway = FakeDatacronGateway()

    plan = ConsolidationService(store, gateway, app_config.datacron).plan()

    assert [item.candidate_id for item in plan.propositions] == [eligible.id]
    assert plan.propositions[0].decision is ReviewDecision.PENDING


def test_business_validity_filters_plan_and_is_rechecked_before_apply(
    store: EngramStore,
    app_config: AppConfig,
    clock: MutableClock,
) -> None:
    current = clock.current
    today = current.date()
    store.add_attested(
        kind=EntryKind.FACT,
        scope="global",
        statement="The future business value is enabled.",
        source_type=SourceType.HUMAN,
        subject_keys=("business/future",),
        valid_from=today + timedelta(days=1),
    )
    store.add_attested(
        kind=EntryKind.FACT,
        scope="global",
        statement="The elapsed business value is enabled.",
        source_type=SourceType.HUMAN,
        subject_keys=("business/elapsed",),
        valid_until=today - timedelta(days=1),
    )
    boundary = store.add_attested(
        kind=EntryKind.FACT,
        scope="global",
        statement="The boundary business value is enabled.",
        source_type=SourceType.HUMAN,
        subject_keys=("business/boundary",),
        valid_from=today,
        valid_until=today,
    )
    gateway = FakeDatacronGateway()
    service = ConsolidationService(store, gateway, app_config.datacron)

    plan = service.plan()

    assert [item.candidate_id for item in plan.propositions] == [boundary.id]

    clock.current = current + timedelta(days=1)
    gateway.calls.clear()
    report = service.apply(_approve(plan))

    assert report.outcomes[0].status is ApplyStatus.FAILED
    assert report.outcomes[0].detail == "candidate is outside its business validity window"
    assert gateway.calls == []
    stored = store.get_entry(boundary.id)
    assert stored is not None
    assert stored.promotion_state is PromotionState.APPROVED


def test_plan_maps_all_four_classifications_and_is_deterministic(
    store: EngramStore,
    app_config: AppConfig,
) -> None:
    cases = (
        ("new/topic", "A new durable statement."),
        ("same/topic", "The stable value is blue."),
        ("update/topic", "The stable value is green."),
        ("conflict/topic", "The feature is not enabled."),
    )
    for subject_key, statement in cases:
        store.add_attested(
            kind=EntryKind.FACT,
            scope="global",
            statement=statement,
            source_type=SourceType.TOOL_VERIFIED,
            subject_keys=(subject_key,),
        )
    same_content = "# Stable\n\nThe stable value is blue.\n"
    update_content = "# Stable\n\nThe stable value is red.\n"
    conflict_content = "# Feature\n\nThe feature is enabled.\n"
    gateway = FakeDatacronGateway(
        {
            "_memory/same.md": same_content,
            "_memory/update.md": update_content,
            "_memory/conflict.md": conflict_content,
        },
        neighbors=(
            _neighbor(
                rel_path="_memory/same.md",
                heading="Stable",
                statement="The stable value is blue.",
                subject_key="same/topic",
                content=same_content,
            ),
            _neighbor(
                rel_path="_memory/update.md",
                heading="Stable",
                statement="The stable value is red.",
                subject_key="update/topic",
                content=update_content,
            ),
            _neighbor(
                rel_path="_memory/conflict.md",
                heading="Feature",
                statement="The feature is enabled.",
                subject_key="conflict/topic",
                content=conflict_content,
            ),
        ),
    )
    service = ConsolidationService(store, gateway, app_config.datacron)

    first = service.plan()
    second = service.plan()
    classes = {item.new_content.splitlines()[0]: item.classification for item in first.propositions}

    assert set(classes.values()) == {
        ConsolidationClass.NEW,
        ConsolidationClass.REDUNDANT,
        ConsolidationClass.UPDATE,
        ConsolidationClass.CONTRADICTORY,
    }
    assert model_json(first) == model_json(second)
    assert render_plan_markdown(first).count("- Decision: `pending`") == 4


def test_plan_targets_exact_redundancy_and_preserves_gateway_search_rank(
    store: EngramStore,
    app_config: AppConfig,
) -> None:
    redundant = store.add_attested(
        kind=EntryKind.FACT,
        scope="global",
        statement="The redundant value is blue.",
        source_type=SourceType.HUMAN,
        subject_keys=("redundant/value",),
    )
    update = store.add_attested(
        kind=EntryKind.FACT,
        scope="global",
        statement="The ranked value is green.",
        source_type=SourceType.HUMAN,
        subject_keys=("ranked/value",),
    )
    notes = {
        "_memory/a-distractor.md": "# Distractor\n\nA different value.\n",
        "_memory/z-exact.md": "# Exact\n\nThe redundant value is blue.\n",
        "_memory/a-lower-rank.md": "# Lower\n\nThe ranked value is red.\n",
        "_memory/z-top-rank.md": "# Top\n\nThe ranked value is blue.\n",
    }
    gateway = FakeDatacronGateway(
        notes,
        neighbors=(
            _neighbor(
                rel_path="_memory/a-distractor.md",
                heading="Distractor",
                statement="A different value.",
                subject_key="redundant/value",
                content=notes["_memory/a-distractor.md"],
                search_rank=0,
            ),
            _neighbor(
                rel_path="_memory/z-exact.md",
                heading="Exact",
                statement="The redundant value is blue.",
                subject_key="redundant/value",
                content=notes["_memory/z-exact.md"],
                search_rank=1,
            ),
            _neighbor(
                rel_path="_memory/a-lower-rank.md",
                heading="Lower",
                statement="The ranked value is red.",
                subject_key="ranked/value",
                content=notes["_memory/a-lower-rank.md"],
                search_rank=1,
            ),
            _neighbor(
                rel_path="_memory/z-top-rank.md",
                heading="Top",
                statement="The ranked value is blue.",
                subject_key="ranked/value",
                content=notes["_memory/z-top-rank.md"],
                search_rank=0,
            ),
        ),
    )

    plan = ConsolidationService(store, gateway, app_config.datacron).plan()
    propositions = {item.candidate_id: item for item in plan.propositions}

    assert propositions[redundant.id].classification is ConsolidationClass.REDUNDANT
    assert propositions[redundant.id].rel_path == "_memory/z-exact.md"
    assert propositions[update.id].classification is ConsolidationClass.UPDATE
    assert propositions[update.id].rel_path == "_memory/z-top-rank.md"


def test_datacron_gateway_clears_inherited_write_allowlist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATACRON_WRITE_PATHS", str(Path("host-vault") / "_memory"))

    parameters = _server_parameters(DatacronConfig(write_paths=()))

    assert parameters.command == "datacron"
    assert parameters.args == ["mcp", "serve"]
    assert parameters.env is not None
    assert parameters.env["DATACRON_WRITE_PATHS"] == ""


def test_datacron_gateway_wraps_missing_command_without_transport_traceback() -> None:
    config = DatacronConfig(
        command="engram-om-l5-command-that-does-not-exist",
        args=("mcp", "serve"),
    )

    with (
        pytest.raises(
            DatacronGatewayError,
            match=r"Could not start Datacron MCP command.*install Datacron",
        ),
        McpDatacronGateway(config),
    ):
        pytest.fail("Missing Datacron command opened a gateway")


def test_datacron_gateway_sends_and_verifies_heading_level(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway = McpDatacronGateway(DatacronConfig())
    captured: dict[str, object] = {}

    def fake_call(tool_name: str, arguments: dict[str, object]) -> dict[str, object]:
        captured["tool_name"] = tool_name
        captured["arguments"] = arguments
        return {
            "indexed": True,
            "content_hash": "a" * 64,
            "patched": {"heading": "Runtime", "level": 2},
        }

    monkeypatch.setattr(gateway, "_call", fake_call)

    result = gateway.patch_section(
        "_memory/runtime.md",
        "Runtime",
        2,
        "The nested runtime is Python 3.13.",
        "b" * 64,
    )

    assert result == "a" * 64
    assert captured == {
        "tool_name": "patch_note_section",
        "arguments": {
            "rel_path": "_memory/runtime.md",
            "heading": "Runtime",
            "heading_level": 2,
            "new_content": "The nested runtime is Python 3.13.",
            "expected_hash": "b" * 64,
        },
    }


def test_apply_continues_after_cas_conflict_and_records_verified_promotion(
    store: EngramStore,
    app_config: AppConfig,
) -> None:
    first = store.add_attested(
        kind=EntryKind.DECISION,
        scope="global",
        statement="Use the first durable option.",
        source_type=SourceType.HUMAN,
        subject_keys=("first/option",),
    )
    second = store.add_attested(
        kind=EntryKind.DECISION,
        scope="global",
        statement="Use the second durable option.",
        source_type=SourceType.HUMAN,
        subject_keys=("second/option",),
    )
    gateway = FakeDatacronGateway()
    service = ConsolidationService(store, gateway, app_config.datacron)
    plan = _approve(service.plan())
    by_id = {item.candidate_id: item for item in plan.propositions}
    gateway.replace_note(by_id[first.id].rel_path, "# Concurrent\n\nCreated elsewhere.\n")

    report = service.apply(plan)
    outcomes = {item.candidate_id: item for item in report.outcomes}

    assert outcomes[first.id].status is ApplyStatus.STALE
    assert outcomes[second.id].status is ApplyStatus.APPLIED
    promoted = store.get_entry(second.id)
    assert promoted is not None
    assert promoted.promotion_state is PromotionState.PROMOTED
    assert promoted.datacron_ref == by_id[second.id].rel_path
    assert promoted.datacron_hash == outcomes[second.id].content_hash
    assert promoted.synced_at is not None
    assert AuditAction.PROMOTE in {record.action for record in store.list_audit()}


def test_reread_failure_never_marks_candidate_promoted(
    store: EngramStore,
    app_config: AppConfig,
) -> None:
    entry = store.add_attested(
        kind=EntryKind.PREFERENCE,
        scope="global",
        statement="Prefer concise durable notes.",
        source_type=SourceType.HUMAN,
        subject_keys=("notes/style",),
    )
    path = "_memory/engram/notes-style.md"
    gateway = FakeDatacronGateway(drift_after_write=(path,))
    service = ConsolidationService(store, gateway, app_config.datacron)

    report = service.apply(_approve(service.plan()))

    assert report.outcomes[0].status is ApplyStatus.FAILED
    current = store.get_entry(entry.id)
    assert current is not None
    assert current.promotion_state is PromotionState.APPROVED
    assert AuditAction.PROMOTE not in {record.action for record in store.list_audit()}


def test_update_uses_cas_patch_and_redundant_links_without_write(
    store: EngramStore,
    app_config: AppConfig,
) -> None:
    update = store.add_attested(
        kind=EntryKind.FACT,
        scope="global",
        statement="The supported runtime is Python 3.13.",
        source_type=SourceType.TOOL_VERIFIED,
        subject_keys=("runtime/python",),
    )
    redundant = store.add_attested(
        kind=EntryKind.FACT,
        scope="global",
        statement="The license is Apache-2.0.",
        source_type=SourceType.HUMAN,
        subject_keys=("license/project",),
    )
    runtime_content = "# Runtime\n\nThe supported runtime is Python 3.12.\n"
    license_content = "# License\n\nThe license is Apache-2.0.\n"
    gateway = FakeDatacronGateway(
        {
            "_memory/runtime.md": runtime_content,
            "_memory/license.md": license_content,
        },
        neighbors=(
            _neighbor(
                rel_path="_memory/runtime.md",
                heading="Runtime",
                statement="The supported runtime is Python 3.12.",
                subject_key="runtime/python",
                content=runtime_content,
            ),
            _neighbor(
                rel_path="_memory/license.md",
                heading="License",
                statement="The license is Apache-2.0.",
                subject_key="license/project",
                content=license_content,
            ),
        ),
    )
    service = ConsolidationService(store, gateway, app_config.datacron)
    plan = _approve(service.plan())
    gateway.calls.clear()

    report = service.apply(plan)
    outcomes = {item.candidate_id: item for item in report.outcomes}

    assert outcomes[update.id].status is ApplyStatus.APPLIED
    assert outcomes[redundant.id].status is ApplyStatus.APPLIED
    assert ("patch_section", "_memory/runtime.md") in gateway.calls
    assert ("patch_section", "_memory/license.md") not in gateway.calls
    assert ("create_note", "_memory/license.md") not in gateway.calls
    updated_note = gateway.get_note("_memory/runtime.md")
    assert updated_note is not None
    assert "Python 3.13" in updated_note.content


def test_apply_rejects_reviewed_plan_retargeted_to_another_section(
    store: EngramStore,
    app_config: AppConfig,
) -> None:
    entry = store.add_attested(
        kind=EntryKind.FACT,
        scope="global",
        statement="The supported runtime is Python 3.13.",
        source_type=SourceType.HUMAN,
        subject_keys=("runtime/python",),
    )
    content = (
        "# Runtime\n\nThe supported runtime is Python 3.12.\n\n"
        "# Unrelated\n\nThis section must remain unchanged.\n"
    )
    gateway = FakeDatacronGateway(
        {"_memory/runtime.md": content},
        neighbors=(
            _neighbor(
                rel_path="_memory/runtime.md",
                heading="Runtime",
                statement="The supported runtime is Python 3.12.",
                subject_key="runtime/python",
                content=content,
            ),
        ),
    )
    service = ConsolidationService(store, gateway, app_config.datacron)
    proposition = (
        service.plan()
        .propositions[0]
        .model_copy(
            update={
                "decision": ReviewDecision.APPROVE,
                "heading": "Unrelated",
            }
        )
    )
    gateway.calls.clear()

    report = service.apply(ConsolidationPlan(propositions=(proposition,)))

    assert report.outcomes[0].status is ApplyStatus.FAILED
    assert report.outcomes[0].detail == "reviewed target was not present in the reviewed neighbors"
    assert ("patch_section", "_memory/runtime.md") not in gateway.calls
    note = gateway.get_note("_memory/runtime.md")
    assert note is not None
    assert note.content == content
    stored = store.get_entry(entry.id)
    assert stored is not None
    assert stored.promotion_state is PromotionState.APPROVED


def test_apply_accepts_reviewed_target_matching_a_current_neighbor(
    store: EngramStore,
    app_config: AppConfig,
) -> None:
    store.add_attested(
        kind=EntryKind.FACT,
        scope="global",
        statement="The supported runtime is Python 3.13.",
        source_type=SourceType.HUMAN,
        subject_keys=("runtime/python",),
    )
    first_content = "# Runtime A\n\nThe supported runtime is Python 3.11.\n"
    second_content = "# Runtime B\n\nThe supported runtime is Python 3.12.\n"
    second = _neighbor(
        rel_path="_memory/runtime-b.md",
        heading="Runtime B",
        statement="The supported runtime is Python 3.12.",
        subject_key="runtime/python",
        content=second_content,
        search_rank=1,
    )
    gateway = FakeDatacronGateway(
        {
            "_memory/runtime-a.md": first_content,
            "_memory/runtime-b.md": second_content,
        },
        neighbors=(
            _neighbor(
                rel_path="_memory/runtime-a.md",
                heading="Runtime A",
                statement="The supported runtime is Python 3.11.",
                subject_key="runtime/python",
                content=first_content,
            ),
            second,
        ),
    )
    service = ConsolidationService(store, gateway, app_config.datacron)
    original = service.plan().propositions[0]
    corrected = original.model_copy(
        update={
            "decision": ReviewDecision.APPROVE,
            "rel_path": second.rel_path,
            "heading": second.heading,
            "heading_level": second.heading_level,
            "expected_hash": second.content_hash,
        }
    )
    gateway.calls.clear()

    report = service.apply(ConsolidationPlan(propositions=(corrected,)))

    assert report.outcomes[0].status is ApplyStatus.APPLIED
    assert ("patch_section", "_memory/runtime-b.md") in gateway.calls
    assert ("patch_section", "_memory/runtime-a.md") not in gateway.calls


def test_apply_rejects_target_that_was_not_in_reviewed_neighbors(
    store: EngramStore,
    app_config: AppConfig,
) -> None:
    store.add_attested(
        kind=EntryKind.FACT,
        scope="global",
        statement="The supported runtime is Python 3.13.",
        source_type=SourceType.HUMAN,
        subject_keys=("runtime/python",),
    )
    first_content = "# Runtime A\n\nThe supported runtime is Python 3.11.\n"
    second_content = "# Runtime B\n\nThe supported runtime is Python 3.12.\n"
    first = _neighbor(
        rel_path="_memory/runtime-a.md",
        heading="Runtime A",
        statement="The supported runtime is Python 3.11.",
        subject_key="runtime/python",
        content=first_content,
    )
    second = _neighbor(
        rel_path="_memory/runtime-b.md",
        heading="Runtime B",
        statement="The supported runtime is Python 3.12.",
        subject_key="runtime/python",
        content=second_content,
        search_rank=1,
    )
    reviewed_gateway = FakeDatacronGateway(
        {"_memory/runtime-a.md": first_content},
        neighbors=(first,),
    )
    reviewed = ConsolidationService(store, reviewed_gateway, app_config.datacron).plan()
    proposition = reviewed.propositions[0].model_copy(
        update={
            "decision": ReviewDecision.APPROVE,
            "rel_path": second.rel_path,
            "heading": second.heading,
            "heading_level": second.heading_level,
            "expected_hash": second.content_hash,
        }
    )
    current_gateway = FakeDatacronGateway(
        {
            "_memory/runtime-a.md": first_content,
            "_memory/runtime-b.md": second_content,
        },
        neighbors=(first, second),
    )

    report = ConsolidationService(store, current_gateway, app_config.datacron).apply(
        ConsolidationPlan(propositions=(proposition,))
    )

    assert report.outcomes[0].status is ApplyStatus.FAILED
    assert report.outcomes[0].detail == "reviewed target was not present in the reviewed neighbors"
    assert ("patch_section", "_memory/runtime-b.md") not in current_gateway.calls


def test_apply_binds_new_target_and_rejects_windows_path_escape(
    store: EngramStore,
    app_config: AppConfig,
) -> None:
    store.add_attested(
        kind=EntryKind.FACT,
        scope="global",
        statement="A new durable fact is reviewed.",
        source_type=SourceType.HUMAN,
        subject_keys=("durable/new",),
    )
    gateway = FakeDatacronGateway()
    service = ConsolidationService(store, gateway, app_config.datacron)
    original = service.plan().propositions[0]
    tampered = (
        original.model_copy(
            update={
                "decision": ReviewDecision.APPROVE,
                "rel_path": "_memory/engram/tampered.md",
                "heading": "Tampered Heading",
            }
        ),
        original.model_copy(
            update={
                "decision": ReviewDecision.APPROVE,
                "heading": f"{original.heading}\n\nInjected plan content",
            }
        ),
        original.model_copy(
            update={
                "decision": ReviewDecision.APPROVE,
                "rel_path": "_memory/..\\outside.md",
            }
        ),
    )

    reports = tuple(
        service.apply(ConsolidationPlan(propositions=(proposition,))) for proposition in tampered
    )

    assert all(report.outcomes[0].status is ApplyStatus.FAILED for report in reports)
    assert all(call[0] != "create_note" for call in gateway.calls)
    assert "reviewed new-note target" in reports[0].outcomes[0].detail
    assert "single line" in reports[1].outcomes[0].detail
    assert "forward slashes" in reports[2].outcomes[0].detail


def test_apply_reconciles_earlier_promotion_changed_by_later_batch_write(
    store: EngramStore,
    app_config: AppConfig,
    clock: MutableClock,
) -> None:
    redundant = store.add_attested(
        kind=EntryKind.FACT,
        scope="global",
        statement="Stable mode remains blue.",
        source_type=SourceType.HUMAN,
        subject_keys=("stable/mode",),
    )
    clock.current += timedelta(seconds=1)
    update = store.add_attested(
        kind=EntryKind.FACT,
        scope="global",
        statement="Runtime mode uses green.",
        source_type=SourceType.HUMAN,
        subject_keys=("runtime/mode",),
    )
    content = (
        "# Stable Mode\n\nStable mode remains blue.\n\n# Runtime Mode\n\nRuntime mode uses blue.\n"
    )
    rel_path = "_memory/modes.md"
    gateway = FakeDatacronGateway(
        {rel_path: content},
        neighbors=(
            _neighbor(
                rel_path=rel_path,
                heading="Stable Mode",
                statement="Stable mode remains blue.",
                subject_key="stable/mode",
                content=content,
            ),
            _neighbor(
                rel_path=rel_path,
                heading="Runtime Mode",
                statement="Runtime mode uses blue.",
                subject_key="runtime/mode",
                content=content,
                search_rank=1,
            ),
        ),
    )
    service = ConsolidationService(store, gateway, app_config.datacron)

    report = service.apply(_approve(service.plan()))

    outcomes = {item.candidate_id: item for item in report.outcomes}
    assert outcomes[redundant.id].status is ApplyStatus.STALE
    assert outcomes[update.id].status is ApplyStatus.APPLIED
    stored_redundant = store.get_entry(redundant.id)
    assert stored_redundant is not None
    assert stored_redundant.stale is True
    retrieval = FtsRetriever(store).retrieve(
        RetrievalRequest(
            query="stable mode",
            scope="global",
            kinds=None,
            writer_model="test-client/1.0",
        )
    )
    assert redundant.id not in {item.id for item in retrieval.matches}


def test_apply_rechecks_business_validity_atomically_after_datacron_write(
    store: EngramStore,
    app_config: AppConfig,
    clock: MutableClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry = store.add_attested(
        kind=EntryKind.FACT,
        scope="global",
        statement="The runtime boundary is current.",
        source_type=SourceType.HUMAN,
        subject_keys=("runtime/boundary",),
        valid_until=clock.current.date(),
    )
    content = "# Runtime Boundary\n\nThe runtime boundary is old.\n"
    rel_path = "_memory/runtime-boundary.md"
    gateway = FakeDatacronGateway(
        {rel_path: content},
        neighbors=(
            _neighbor(
                rel_path=rel_path,
                heading="Runtime Boundary",
                statement="The runtime boundary is old.",
                subject_key="runtime/boundary",
                content=content,
            ),
        ),
    )
    service = ConsolidationService(store, gateway, app_config.datacron)
    plan = _approve(service.plan())
    original_get_note = gateway.get_note
    read_count = 0

    def advancing_get_note(path: str) -> NoteView | None:
        nonlocal read_count
        read_count += 1
        note = original_get_note(path)
        if read_count == 2:
            clock.current += timedelta(days=1)
        return note

    monkeypatch.setattr(gateway, "get_note", advancing_get_note)

    report = service.apply(plan)

    assert report.outcomes[0].status is ApplyStatus.FAILED
    assert report.outcomes[0].detail == "candidate is outside its business validity window"
    stored = store.get_entry(entry.id)
    assert stored is not None
    assert stored.promotion_state is PromotionState.APPROVED
    note = gateway.get_note(rel_path)
    assert note is not None
    assert "The runtime boundary is current." in note.content


def test_patch_uses_heading_level_and_verifies_the_exact_wrapped_section(
    store: EngramStore,
    app_config: AppConfig,
) -> None:
    store.add_attested(
        kind=EntryKind.FACT,
        scope="global",
        statement="The nested runtime is Python 3.13.",
        source_type=SourceType.HUMAN,
        subject_keys=("runtime/nested",),
    )
    content = (
        "# Runtime\n\nThe top-level runtime remains unchanged.\n\n"
        "## Runtime\n\nThe nested runtime is Python 3.12.\n\n"
        "# Other\n\nOther content.\n"
    )
    gateway = _WrappedReadGateway(
        {"_memory/runtime.md": content},
        neighbors=(
            _neighbor(
                rel_path="_memory/runtime.md",
                heading="Runtime",
                statement="The nested runtime is Python 3.12.",
                subject_key="runtime/nested",
                content=content,
                heading_level=2,
            ),
        ),
    )
    service = ConsolidationService(store, gateway, app_config.datacron)

    plan = service.plan()
    assert plan.propositions[0].heading_level == 2
    report = service.apply(_approve(plan))

    assert report.outcomes[0].status is ApplyStatus.APPLIED
    note = gateway.get_note("_memory/runtime.md")
    assert note is not None
    assert "The top-level runtime remains unchanged." in note.content
    assert "## Runtime\n\nThe nested runtime is Python 3.13." in note.content


def test_pending_and_rejected_decisions_never_write(
    store: EngramStore,
    app_config: AppConfig,
) -> None:
    store.add_attested(
        kind=EntryKind.FACT,
        scope="global",
        statement="Keep this candidate under review.",
        source_type=SourceType.HUMAN,
        subject_keys=("review/pending",),
    )
    gateway = FakeDatacronGateway()
    service = ConsolidationService(store, gateway, app_config.datacron)
    pending = service.plan()
    rejected = pending.model_copy(
        update={
            "propositions": (
                pending.propositions[0].model_copy(update={"decision": ReviewDecision.REJECT}),
            )
        }
    )
    gateway.calls.clear()

    pending_report = service.apply(pending)
    rejected_report = service.apply(rejected)

    assert pending_report.outcomes[0].status is ApplyStatus.SKIPPED
    assert rejected_report.outcomes[0].status is ApplyStatus.SKIPPED
    assert gateway.calls == []


def test_freshness_marks_drift_stale_and_recall_excludes_it(
    store: EngramStore,
    app_config: AppConfig,
) -> None:
    entry = store.add_attested(
        kind=EntryKind.PROJECT_STATE,
        scope="project/engram",
        statement="The consolidation stage is current.",
        source_type=SourceType.TOOL_VERIFIED,
        subject_keys=("engram/consolidation",),
    )
    gateway = FakeDatacronGateway()
    service = ConsolidationService(store, gateway, app_config.datacron)
    applied = service.apply(_approve(service.plan()))
    path = applied.outcomes[0].rel_path
    gateway.replace_note(path, "# Changed\n\nA human changed the durable source.\n")

    freshness = service.check_freshness()
    retrieval = FtsRetriever(store).retrieve(
        RetrievalRequest(
            query="consolidation stage",
            scope="project/engram",
            kinds=None,
            writer_model="test-client/1.0",
        )
    )

    assert freshness.outcomes[0].stale is True
    current = store.get_entry(entry.id)
    assert current is not None
    assert current.stale is True
    assert entry.id not in {item.id for item in retrieval.matches}
    assert AuditAction.MARK_STALE in {record.action for record in store.list_audit()}


def test_consolidation_service_contains_no_vault_filesystem_writes() -> None:
    source = (Path(__file__).parents[1] / "src/engram/consolidation/service.py").read_text(
        encoding="utf-8"
    )

    assert "write_text(" not in source
    assert "open(" not in source


def test_cli_plan_then_apply_uses_gateway_and_writes_review_artifacts(
    store: EngramStore,
    app_config: AppConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry = store.add_attested(
        kind=EntryKind.DECISION,
        scope="project/engram",
        statement="The reviewed CLI workflow is enabled.",
        source_type=SourceType.HUMAN,
        subject_keys=("engram/review-cli",),
    )
    gateway = FakeDatacronGateway()
    monkeypatch.setattr(cli_module, "McpDatacronGateway", lambda _config: gateway)
    plan_path = tmp_path / "consolidation" / "plan.json"
    logger = logging.getLogger("engram.test.consolidation-cli")

    _consolidate(
        config=app_config,
        logger=logger,
        generate_plan=True,
        apply_path=None,
        check_freshness=False,
        output_path=plan_path,
    )
    plan = ConsolidationPlan.model_validate_json(plan_path.read_text(encoding="utf-8"))
    plan_path.write_text(model_json(_approve(plan)), encoding="utf-8")
    _consolidate(
        config=app_config,
        logger=logger,
        generate_plan=False,
        apply_path=plan_path,
        check_freshness=False,
        output_path=None,
    )

    report = ApplyReport.model_validate_json(
        plan_path.with_name("apply-report.json").read_text(encoding="utf-8")
    )
    assert plan_path.with_suffix(".md").is_file()
    assert plan.propositions[0].decision is ReviewDecision.PENDING
    assert report.outcomes[0].status is ApplyStatus.APPLIED
    current = store.get_entry(entry.id)
    assert current is not None
    assert current.promotion_state is PromotionState.PROMOTED
