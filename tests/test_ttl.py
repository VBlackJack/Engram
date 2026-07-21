# Copyright 2026 Julien Bombled
# SPDX-License-Identifier: Apache-2.0

"""Fixed TTL, logical expiration, and physical purge tests."""

from __future__ import annotations

from datetime import timedelta

from engram.models import AuditAction, EntryStatus
from engram.store import EngramStore
from tests.conftest import MutableClock


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


def test_zero_ttl_never_sets_expiration(store: EngramStore) -> None:
    entry = store.add_candidate(
        kind="preference",
        scope="user",
        statement="Prefer concise status reports.",
        writer_model="test-model",
    )

    assert entry.expires_at is None
    assert store.expire_due() == 0
