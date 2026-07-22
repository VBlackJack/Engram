# Copyright 2026 Julien Bombled
# SPDX-License-Identifier: Apache-2.0

"""Derived-index command tests."""

from __future__ import annotations

import json
import logging
import socket
import sqlite3
import sys
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

import engram.cli as cli_module
from engram import __version__
from engram.cli import (
    EXIT_EXTERNAL_DEPENDENCY,
    EXIT_INTERRUPTED,
    EXIT_LOCAL_RESOURCE,
    EXIT_TRANSIENT_BUSY,
    EXIT_USAGE_OR_CONFIG,
    ServerBindError,
    _attest,
    _consolidate,
    _ensure_server_bind_available,
    _list_entries,
    _reindex,
    _serve,
    _supersede,
    main,
)
from engram.config import AppConfig, ConfigError
from engram.consolidation.gateway import DatacronGatewayError
from engram.db import SQLiteVersionError
from engram.logging_setup import FileLogger
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
from engram.store import EngramStore, StoreBusyError, StoreClosedError


def test_version_command_reports_package_version(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(sys, "argv", ["engram", "--version"])
    with pytest.raises(SystemExit) as exit_info:
        main()

    assert exit_info.value.code == 0
    assert capsys.readouterr().out.strip() == f"engram {__version__}"


def test_main_formats_configuration_error_without_traceback(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def reject_config() -> AppConfig:
        raise ConfigError("Configuration file does not exist: missing.toml")

    monkeypatch.setattr(sys, "argv", ["engram", "serve"])
    monkeypatch.setattr(cli_module, "load_config", reject_config)

    with pytest.raises(SystemExit) as exit_info:
        main()

    captured = capsys.readouterr()
    assert exit_info.value.code == EXIT_USAGE_OR_CONFIG
    assert captured.out == ""
    assert captured.err == ("engram: error: Configuration file does not exist: missing.toml\n")
    assert "Traceback" not in captured.err


@pytest.mark.parametrize(
    ("error", "exit_code"),
    [
        (ServerBindError("Port 8377 is already in use"), EXIT_LOCAL_RESOURCE),
        (
            DatabaseLockError(Path("engram.db.lock"), None),
            EXIT_LOCAL_RESOURCE,
        ),
        (SQLiteVersionError("SQLite runtime is too old"), EXIT_LOCAL_RESOURCE),
        (DatacronGatewayError("Datacron is unavailable"), EXIT_EXTERNAL_DEPENDENCY),
        (StoreBusyError("server busy, retry"), EXIT_TRANSIENT_BUSY),
    ],
)
def test_main_maps_known_failures_without_tracebacks(
    app_config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    error: Exception,
    exit_code: int,
) -> None:
    logger = logging.getLogger(f"engram.test.main-errors.{exit_code}")
    logger.handlers = [logging.NullHandler()]
    logger.propagate = False

    def fail_dispatch(*_args: object, **_kwargs: object) -> None:
        raise error

    monkeypatch.setattr(sys, "argv", ["engram", "serve"])
    monkeypatch.setattr(cli_module, "load_config", lambda: app_config)
    monkeypatch.setattr(FileLogger, "configure", lambda _self: logger)
    monkeypatch.setattr(cli_module, "_dispatch", fail_dispatch)

    with pytest.raises(SystemExit) as exit_info:
        main()

    captured = capsys.readouterr()
    assert exit_info.value.code == exit_code
    assert captured.out == ""
    assert captured.err == f"engram: error: {error}\n"
    assert "Traceback" not in captured.err


def test_debug_flag_reraises_known_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def reject_config() -> AppConfig:
        raise ConfigError("broken debug config")

    monkeypatch.setattr(sys, "argv", ["engram", "--debug", "serve"])
    monkeypatch.setattr(cli_module, "load_config", reject_config)

    with pytest.raises(ConfigError, match="broken debug config"):
        main()


def test_debug_environment_reraises_known_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def reject_config() -> AppConfig:
        raise ConfigError("broken environment debug config")

    monkeypatch.setattr(sys, "argv", ["engram", "serve"])
    monkeypatch.setenv("ENGRAM_DEBUG", "yes")
    monkeypatch.setattr(cli_module, "load_config", reject_config)

    with pytest.raises(ConfigError, match="environment debug"):
        main()


def test_main_maps_keyboard_interrupt_to_shell_convention(
    app_config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    logger = logging.getLogger("engram.test.keyboard-interrupt")
    logger.handlers = [logging.NullHandler()]
    logger.propagate = False

    def interrupt(*_args: object, **_kwargs: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(sys, "argv", ["engram", "serve"])
    monkeypatch.setattr(cli_module, "load_config", lambda: app_config)
    monkeypatch.setattr(FileLogger, "configure", lambda _self: logger)
    monkeypatch.setattr(cli_module, "_dispatch", interrupt)

    with pytest.raises(SystemExit) as exit_info:
        main()

    captured = capsys.readouterr()
    assert exit_info.value.code == EXIT_INTERRUPTED
    assert captured.out == ""
    assert captured.err == "engram: interrupted\n"


def test_port_guard_reports_live_listener() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = int(listener.getsockname()[1])

        with pytest.raises(ServerBindError, match=rf"Port {port} is already in use"):
            _ensure_server_bind_available("127.0.0.1", port)


def test_serve_closes_store_and_lock_when_server_creation_fails(
    app_config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, EngramStore] = {}

    def fail_creation(_config: AppConfig, store: EngramStore) -> object:
        captured["store"] = store
        raise RuntimeError("server construction failed")

    monkeypatch.setattr(cli_module, "_ensure_server_bind_available", lambda *_args: None)
    monkeypatch.setattr(cli_module, "create_mcp_server", fail_creation)

    with pytest.raises(RuntimeError, match="server construction failed"):
        _serve(config=app_config, logger=logging.getLogger("engram.test.serve-cleanup"))

    with pytest.raises(StoreClosedError):
        captured["store"].count_entries()
    with DatabaseProcessLock(
        app_config.database.path,
        role=DatabaseLockRole.OFFLINE_WRITER,
        command="cleanup-proof",
    ):
        pass


def test_serve_closes_store_and_lock_on_keyboard_interrupt(
    app_config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, EngramStore] = {}

    class InterruptingServer:
        def run(self, *, transport: str) -> None:
            assert transport == "streamable-http"
            raise KeyboardInterrupt

    def create_interrupting_server(
        _config: AppConfig,
        store: EngramStore,
    ) -> InterruptingServer:
        captured["store"] = store
        return InterruptingServer()

    monkeypatch.setattr(cli_module, "_ensure_server_bind_available", lambda *_args: None)
    monkeypatch.setattr(cli_module, "create_mcp_server", create_interrupting_server)

    with pytest.raises(KeyboardInterrupt):
        _serve(config=app_config, logger=logging.getLogger("engram.test.serve-interrupt"))

    with pytest.raises(StoreClosedError):
        captured["store"].count_entries()
    with DatabaseProcessLock(
        app_config.database.path,
        role=DatabaseLockRole.OFFLINE_WRITER,
        command="interrupt-cleanup-proof",
    ):
        pass


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
