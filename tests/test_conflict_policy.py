# Copyright 2026 Julien Bombled
# SPDX-License-Identifier: Apache-2.0

"""Claim-aware conflict policy tests for retrieval and capsule assembly."""

from __future__ import annotations

from dataclasses import replace

from engram.capsule import (
    CONFLICTS_HIDDEN_BY_REQUEST_NOTICE,
    UNCLASSIFIED_CLAIM_OMITTED_NOTICE,
    CapsuleBuilder,
)
from engram.config import AppConfig
from engram.models import PROJECT_STATE_CLAIM_KEY, SourceType
from engram.retrieval import FtsRetriever, RetrievalRequest, RetrievalResult
from engram.store import EngramStore


def _request(query: str, *, scope: str | None = "project/engram") -> RetrievalRequest:
    return RetrievalRequest(
        query=query,
        scope=scope,
        kinds=None,
        writer_model="conflict-policy-test/1.0",
    )


def test_shared_subject_does_not_conflict_when_claims_differ(
    store: EngramStore,
    app_config: AppConfig,
) -> None:
    """Keep complementary facts usable when only their retrieval subject overlaps."""
    engine = store.add_attested(
        kind="fact",
        scope="project/engram",
        statement="The storage engine is SQLite.",
        source_type=SourceType.TOOL_VERIFIED,
        subject_keys=("storage",),
        claim_key="storage/engine",
    )
    journal = store.add_attested(
        kind="fact",
        scope="project/engram",
        statement="The storage journal mode is WAL.",
        source_type=SourceType.TOOL_VERIFIED,
        subject_keys=("storage",),
        claim_key="storage/journal-mode",
    )

    retrieval = FtsRetriever(store).retrieve(_request("storage"))
    capsule, _ = CapsuleBuilder(app_config.capsule).build(
        retrieval,
        scope="project/engram",
        include_conflicts=True,
        token_budget=app_config.capsule.max_token_budget,
    )

    assert {item.id for item in capsule.current} == {engine.id, journal.id}
    assert capsule.conflicts == []


def test_exact_claim_match_expands_and_groups_conflict(
    store: EngramStore,
    app_config: AppConfig,
) -> None:
    """Pull the other explicit claim version even when only one statement ranks."""
    light = store.add_attested(
        kind="decision",
        scope="project/engram",
        statement="The editor theme is light.",
        source_type=SourceType.HUMAN,
        subject_keys=("editor",),
        claim_key="editor/theme",
    )
    dark = store.add_attested(
        kind="decision",
        scope="project/engram",
        statement="The editor theme is dark.",
        source_type=SourceType.TOOL_VERIFIED,
        subject_keys=("display",),
        claim_key="editor/theme",
    )

    retrieval = FtsRetriever(store).retrieve(_request("light"))
    capsule, _ = CapsuleBuilder(app_config.capsule).build(
        retrieval,
        scope="project/engram",
        include_conflicts=True,
        token_budget=app_config.capsule.max_token_budget,
    )

    assert {entry.id for entry in retrieval.matches} == {light.id, dark.id}
    assert capsule.current == []
    assert len(capsule.conflicts) == 1
    assert capsule.conflicts[0].claim_key == "editor/theme"
    assert {item.id for item in capsule.conflicts[0].versions} == {light.id, dark.id}


def test_hidden_conflict_reports_exact_omitted_version_count(
    store: EngramStore,
    app_config: AppConfig,
) -> None:
    """Do not silently turn a hidden conflict into an apparently empty answer."""
    for statement, subject_key in (
        ("The editor theme is light.", "editor"),
        ("The editor theme is dark.", "display"),
    ):
        store.add_attested(
            kind="decision",
            scope="project/engram",
            statement=statement,
            source_type=SourceType.HUMAN,
            subject_keys=(subject_key,),
            claim_key="editor/theme",
        )

    retrieval = FtsRetriever(store).retrieve(_request("light"))
    capsule, _ = CapsuleBuilder(app_config.capsule).build(
        retrieval,
        scope="project/engram",
        include_conflicts=False,
        token_budget=app_config.capsule.max_token_budget,
    )

    assert capsule.current == []
    assert capsule.conflicts == []
    assert (
        "2 unresolved conflict entries omitted; retry with include_conflicts=true"
        in capsule.notes.why_returned
    )
    assert capsule.notes.recall_complete is False
    assert capsule.notes.warnings == [CONFLICTS_HIDDEN_BY_REQUEST_NOTICE]


def test_scoped_project_states_conflict_without_a_lexical_match(
    store: EngramStore,
    app_config: AppConfig,
) -> None:
    """Apply conflict policy to next_action as well as direct retrieval matches."""
    blocked = store.add_attested(
        kind="project_state",
        scope="project/engram",
        statement="The release candidate is blocked.",
        source_type=SourceType.HUMAN,
        subject_keys=("release",),
    )
    ready = store.add_attested(
        kind="project_state",
        scope="project/engram",
        statement="The release candidate is ready.",
        source_type=SourceType.TOOL_VERIFIED,
        subject_keys=("release",),
    )

    retrieval = FtsRetriever(store).retrieve(_request("unrelated lexical tokens"))
    capsule, _ = CapsuleBuilder(app_config.capsule).build(
        retrieval,
        scope="project/engram",
        include_conflicts=True,
        token_budget=app_config.capsule.max_token_budget,
    )

    assert retrieval.matches == ()
    assert {entry.id for entry in retrieval.next_actions} == {blocked.id, ready.id}
    assert capsule.next_action == []
    assert len(capsule.conflicts) == 1
    assert capsule.conflicts[0].claim_key == PROJECT_STATE_CLAIM_KEY
    assert {item.id for item in capsule.conflicts[0].versions} == {blocked.id, ready.id}


def test_unclassified_legacy_fact_is_omitted_fail_closed(
    store: EngramStore,
    app_config: AppConfig,
) -> None:
    """Keep trusted v4 rows out of current until their claim is classified."""
    classified = store.add_attested(
        kind="fact",
        scope="project/engram",
        statement="The SQLite runtime is supported.",
        source_type=SourceType.TOOL_VERIFIED,
        claim_key="runtime/sqlite-support",
    )
    legacy = replace(classified, claim_key=None)
    retrieval = RetrievalResult(
        matches=(legacy,),
        next_actions=(),
        own_pending=(),
    )

    capsule, _ = CapsuleBuilder(app_config.capsule).build(
        retrieval,
        scope="project/engram",
        include_conflicts=True,
        token_budget=app_config.capsule.max_token_budget,
    )

    assert capsule.current == []
    assert capsule.sources == []
    assert (
        "1 trusted entry omitted: claim_key classification required" in capsule.notes.why_returned
    )
    assert capsule.notes.recall_complete is False
    assert capsule.notes.warnings == [UNCLASSIFIED_CLAIM_OMITTED_NOTICE]


def test_claim_conflicts_are_partitioned_by_kind_and_scope(
    store: EngramStore,
    app_config: AppConfig,
) -> None:
    """Do not merge identical claim labels across semantic or ownership boundaries."""
    fact = store.add_attested(
        kind="fact",
        scope="project/engram",
        statement="The shared setting is documented.",
        source_type=SourceType.TOOL_VERIFIED,
        subject_keys=("shared-setting",),
        claim_key="setting/value",
    )
    decision = store.add_attested(
        kind="decision",
        scope="project/engram",
        statement="The shared setting remains enabled.",
        source_type=SourceType.HUMAN,
        subject_keys=("shared-setting",),
        claim_key="setting/value",
    )
    elsewhere = store.add_attested(
        kind="fact",
        scope="project/other",
        statement="The shared setting is disabled elsewhere.",
        source_type=SourceType.HUMAN,
        subject_keys=("shared-setting",),
        claim_key="setting/value",
    )

    retrieval = FtsRetriever(store).retrieve(_request("shared-setting", scope=None))
    capsule, _ = CapsuleBuilder(app_config.capsule).build(
        retrieval,
        scope=None,
        include_conflicts=True,
        token_budget=app_config.capsule.max_token_budget,
    )

    assert {item.id for item in capsule.current} == {fact.id, decision.id, elsewhere.id}
    assert capsule.conflicts == []
