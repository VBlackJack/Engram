# Copyright 2026 Julien Bombled
# SPDX-License-Identifier: Apache-2.0

"""Derived-index command tests."""

from __future__ import annotations

import json
import logging
import sqlite3
import sys
from datetime import UTC, date, datetime

import pytest

from engram import __version__
from engram.cli import _attest, _consolidate, _list_entries, _reindex, _supersede, main
from engram.config import AppConfig
from engram.models import (
    Confidence,
    EntryKind,
    EntryStatus,
    Evidence,
    EvidenceType,
    SourceType,
)
from engram.process_lock import DatabaseLockError, DatabaseLockRole, DatabaseProcessLock
from engram.retrieval import FtsRetriever, RetrievalRequest
from engram.store import EngramStore


def test_version_command_reports_package_version(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(sys, "argv", ["engram", "--version"])
    with pytest.raises(SystemExit) as exit_info:
        main()

    assert exit_info.value.code == 0
    assert capsys.readouterr().out.strip() == f"engram {__version__}"


def test_reindex_command_rebuilds_a_dropped_fts_table(app_config: AppConfig) -> None:
    with EngramStore(app_config) as store:
        entry = store.add_attested(
            kind="fact",
            scope="user",
            statement="The command rebuilds derived search.",
            source_type=SourceType.TOOL_VERIFIED,
        )

    connection = sqlite3.connect(app_config.database.path)
    try:
        connection.execute("DROP TABLE entries_fts")
        connection.commit()
    finally:
        connection.close()

    _reindex(config=app_config, logger=logging.getLogger("engram.test.cli"))

    with EngramStore(app_config) as store:
        result = FtsRetriever(store).retrieve(
            RetrievalRequest(
                query="derived search",
                scope=None,
                kinds=None,
                writer_model="test-client/1.0",
            )
        )
        assert result.matches[0].id == entry.id


def test_attest_command_uses_configured_actor_and_prints_json(
    app_config: AppConfig,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _attest(
        config=app_config,
        logger=logging.getLogger("engram.test.cli"),
        statement="The trusted CLI is available.",
        kind=EntryKind.FACT,
        scope="user",
        subject_keys=("trusted-cli",),
        source_type=SourceType.HUMAN,
        confidence=Confidence.HIGH,
        evidence=(Evidence(type=EvidenceType.REVIEW, ref="review-1"),),
        valid_from=date(2026, 7, 22),
        valid_until=None,
        observed_at=datetime(2026, 7, 22, 8, 0, tzinfo=UTC),
        actor=None,
        supersedes=(),
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == EntryStatus.ACTIVE.value
    assert payload["promotion_state"] == "approved"
    assert payload["source_type"] == SourceType.HUMAN.value
    assert payload["subject_keys"] == ["trusted-cli"]
    assert payload["evidence"] == [{"ref": "review-1", "type": "review"}]
    with EngramStore(app_config) as store:
        assert store.list_audit()[-1].actor == app_config.attestation.default_actor


def test_supersede_and_list_commands_expose_lifecycle_state(
    app_config: AppConfig,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with EngramStore(app_config) as store:
        old_entry = store.add_attested(
            kind="project_state",
            scope="project/engram",
            statement="The trusted CLI is pending.",
            source_type=SourceType.HUMAN,
        )
        new_entry = store.add_attested(
            kind="project_state",
            scope="project/engram",
            statement="The trusted CLI is implemented.",
            source_type=SourceType.HUMAN,
        )

    _supersede(
        config=app_config,
        logger=logging.getLogger("engram.test.cli"),
        old_id=old_entry.id,
        new_id=new_entry.id,
        actor="reviewer",
    )
    supersession = json.loads(capsys.readouterr().out)
    assert supersession["old"]["status"] == EntryStatus.SUPERSEDED.value
    assert supersession["new"]["supersedes"] == [old_entry.id]

    _list_entries(config=app_config, status=EntryStatus.ACTIVE)
    active_entries = json.loads(capsys.readouterr().out)
    assert [entry["id"] for entry in active_entries] == [new_entry.id]


def test_daemon_lock_blocks_offline_writers_but_allows_list(
    app_config: AppConfig,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with EngramStore(app_config):
        pass

    with DatabaseProcessLock(
        app_config.database.path,
        role=DatabaseLockRole.DAEMON,
        command="serve",
    ):
        with pytest.raises(DatabaseLockError, match="stop it before an offline write"):
            _attest(
                config=app_config,
                logger=logging.getLogger("engram.test.cli"),
                statement="This write must be rejected.",
                kind=EntryKind.FACT,
                scope="user",
                subject_keys=(),
                source_type=SourceType.HUMAN,
                confidence=Confidence.HIGH,
                evidence=(),
                valid_from=None,
                valid_until=None,
                observed_at=None,
                actor=None,
                supersedes=(),
            )
        with pytest.raises(DatabaseLockError):
            _supersede(
                config=app_config,
                logger=logging.getLogger("engram.test.cli"),
                old_id="01AAAAAAAAAAAAAAAAAAAAAAAA",
                new_id="01BBBBBBBBBBBBBBBBBBBBBBBB",
                actor=None,
            )
        with pytest.raises(DatabaseLockError):
            _reindex(config=app_config, logger=logging.getLogger("engram.test.cli"))
        with pytest.raises(DatabaseLockError):
            _consolidate(
                config=app_config,
                logger=logging.getLogger("engram.test.cli"),
                generate_plan=True,
                apply_path=None,
                check_freshness=False,
                output_path=None,
            )

        _list_entries(config=app_config, status=EntryStatus.ACTIVE)
        assert json.loads(capsys.readouterr().out) == []

    with EngramStore(app_config) as store:
        assert store.count_entries() == 0
