# Copyright 2026 Julien Bombled
# SPDX-License-Identifier: Apache-2.0

"""Fixed TTL, logical expiration, and physical purge tests."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

from engram.config import AppConfig
from engram.models import AuditAction, EntryStatus, SourceType
from engram.store import EngramStore
from tests.conftest import MutableClock

CANDIDATE_MAX_DAYS = 90


def test_episode_ttl_expire_and_purge_are_distinct(
    store: EngramStore,
    clock: MutableClock,
) -> None:
    entry = store.add_candidate(
        kind="episode",
        scope="session/ttl",
        statement="Temporary session context.",
        writer_model="test-model",
    )
    expected_expiration = clock.current + timedelta(days=7)
    assert entry.expires_at == expected_expiration
    initial_audit = store.list_audit()

    assert store.purge_expired(clock.current + timedelta(days=30)) == 0
    assert store.get_entry(entry.id) is not None

    clock.current = expected_expiration
    assert store.expire_due() == 1
    expired_entry = store.get_entry(entry.id)
    assert expired_entry is not None
    assert expired_entry.status is EntryStatus.EXPIRED
    after_expiration = store.list_audit()
    assert len(after_expiration) == len(initial_audit) + 1
    assert after_expiration[-1].action is AuditAction.EXPIRE

    clock.current += timedelta(days=1)
    assert store.purge_expired(clock.current) == 1
    assert store.get_entry(entry.id) is None
    after_purge = store.list_audit()
    assert len(after_purge) == len(after_expiration) + 1
    assert after_purge[: len(after_expiration)] == after_expiration
    assert after_purge[-1].action is AuditAction.PURGE


def test_zero_ttl_never_sets_expiration_on_a_trusted_entry(store: EngramStore) -> None:
    """A kind configured to zero is unbounded, which is a claim about trusted content.

    This test used to make that claim with a candidate, and so pinned as intended
    the behaviour where a model's unreviewed guess inherited the lifetime of a
    human-verified fact. The intent is preserved here on the lifecycle it belongs
    to; the candidate half moved to the three tests below.
    """
    entry = store.add_attested(
        kind="preference",
        scope="user",
        statement="Prefer concise status reports.",
        source_type=SourceType.HUMAN,
        claim_key="user/report-length",
    )

    assert entry.expires_at is None
    assert store.expire_due() == 0


def test_a_candidate_of_an_unbounded_kind_is_still_bounded(
    store: EngramStore,
    clock: MutableClock,
) -> None:
    """Nobody trusted this, so it does not get the lifetime of something trusted."""
    entry = store.add_candidate(
        kind="preference",
        scope="user",
        statement="Prefer concise status reports.",
        writer_model="test-model",
    )

    assert entry.expires_at == clock.current + timedelta(days=CANDIDATE_MAX_DAYS)


def test_the_candidate_ceiling_only_ever_shortens(
    store: EngramStore,
    clock: MutableClock,
) -> None:
    """An episode candidate keeps seven days; the ceiling must not extend it to ninety."""
    entry = store.add_candidate(
        kind="episode",
        scope="session/ttl",
        statement="Temporary session context.",
        writer_model="test-model",
    )

    assert entry.expires_at == clock.current + timedelta(days=7)


def test_attesting_a_bounded_candidate_lifts_its_bound(
    store: EngramStore,
    clock: MutableClock,
) -> None:
    """Taking responsibility for a statement is what buys it an unbounded life."""
    candidate = store.add_candidate(
        kind="fact",
        scope="project/engram",
        statement="The ceiling is lifted by a person, not by time.",
        writer_model="test-model",
    )
    assert candidate.expires_at == clock.current + timedelta(days=CANDIDATE_MAX_DAYS)

    attested = store.add_attested(
        kind="fact",
        scope="project/engram",
        statement="The ceiling is lifted by a person, not by time.",
        source_type=SourceType.HUMAN,
        claim_key="engram/candidate-ceiling",
    )

    assert attested.id == candidate.id
    assert attested.expires_at is None


def test_a_zero_ceiling_restores_the_unbounded_candidate(app_config: AppConfig) -> None:
    """The escape hatch is covered, not merely documented."""
    config = replace(
        app_config,
        ttl_days=replace(app_config.ttl_days, candidate_max_days=0),
    )
    with EngramStore(config) as store:
        entry = store.add_candidate(
            kind="preference",
            scope="user",
            statement="Prefer concise status reports.",
            writer_model="test-model",
        )

        assert entry.expires_at is None
