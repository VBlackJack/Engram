# Copyright 2026 Julien Bombled
# SPDX-License-Identifier: Apache-2.0

"""Derived-index command tests."""

from __future__ import annotations

import json
import logging
import os
import socket
import sqlite3
import subprocess
import sys
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

import engram.cli as cli_module
import engram.db as db_module
from engram import __version__
from engram.autostart import DatabaseOwnership
from engram.cli import (
    EXIT_EXTERNAL_DEPENDENCY,
    EXIT_INTERRUPTED,
    EXIT_LOCAL_RESOURCE,
    EXIT_PARTIAL_RESULT,
    EXIT_TRANSIENT_BUSY,
    EXIT_USAGE_OR_CONFIG,
    ConsolidationApplyError,
    DaemonStopError,
    ServerBindError,
    UpgradePreflightError,
    _attest,
    _classify,
    _consolidate,
    _ensure_server_bind_available,
    _list_entries,
    _migrate,
    _preflight,
    _reindex,
    _serve,
    _stop,
    _supersede,
    main,
)
from engram.config import AppConfig, ConfigError, load_config
from engram.consolidation.gateway import DatacronGatewayError
from engram.db import MAX_SQLITE_VALUE_BYTES, DatabaseError, SQLiteVersionError
from engram.logging_setup import FileLogger
from engram.models import (
    AuditAction,
    Confidence,
    EntryKind,
    EntryStatus,
    Evidence,
    EvidenceType,
    SourceType,
)
from engram.normalization import canonical_key
from engram.process_lock import DatabaseLockError, DatabaseLockRole, DatabaseProcessLock
from engram.resources import example_config_text
from engram.retrieval import FtsRetriever, RetrievalRequest, VectorRebuildError
from engram.store import (
    EngramStore,
    StoreBusyError,
    StoreClosedError,
    StoreValidationError,
)


def _initialize_version_four(
    config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
    *,
    seed_fact: bool,
) -> str | None:
    migrations = db_module.MIGRATIONS
    entry_id = "01HHHHHHHHHHHHHHHHHHHHHHHH" if seed_fact else None
    connection = sqlite3.connect(config.database.path, isolation_level=None)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        monkeypatch.setattr(db_module, "MIGRATIONS", migrations[:4])
        db_module.apply_migrations(connection)
        if entry_id is not None:
            statement = "The legacy CLI entry needs classification."
            connection.execute(
                """
                INSERT INTO entries(
                    id, kind, scope, statement, subject_keys, status,
                    promotion_state, source_type, writer_model, confidence,
                    observed_at, recorded_at, valid_from, valid_until, expires_at,
                    idempotency_key, supersedes, evidence, is_stale, datacron_ref,
                    datacron_hash, synced_at
                ) VALUES (
                    ?, 'fact', 'user', ?, '[]', 'active', 'approved', 'human',
                    NULL, 'high', NULL, '2026-07-21T12:00:00.000000Z', NULL,
                    NULL, NULL, ?, '[]', '[]', 0, NULL, NULL, NULL
                )
                """,
                (
                    entry_id,
                    statement,
                    canonical_key(EntryKind.FACT, "user", statement),
                ),
            )
    finally:
        connection.close()
        monkeypatch.setattr(db_module, "MIGRATIONS", migrations)
    return entry_id


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
        (sqlite3.OperationalError("database disk image is malformed"), EXIT_LOCAL_RESOURCE),
        (SQLiteVersionError("SQLite runtime is too old"), EXIT_LOCAL_RESOURCE),
        (DatacronGatewayError("Datacron is unavailable"), EXIT_EXTERNAL_DEPENDENCY),
        (VectorRebuildError("Vector rebuild failed"), EXIT_EXTERNAL_DEPENDENCY),
        (StoreBusyError("server busy, retry"), EXIT_TRANSIENT_BUSY),
        (
            ConsolidationApplyError("apply completed with stale propositions"),
            EXIT_PARTIAL_RESULT,
        ),
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
            claim_key="search/reindex",
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
        claim_key="cli/trusted",
        supersedes=(),
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == EntryStatus.ACTIVE.value
    assert payload["promotion_state"] == "approved"
    assert payload["source_type"] == SourceType.HUMAN.value
    assert payload["claim_key"] == "cli/trusted"
    assert payload["subject_keys"] == ["trusted-cli"]
    assert payload["evidence"] == [{"ref": "review-1", "type": "review"}]
    with EngramStore(app_config) as store:
        assert store.list_audit()[-1].actor == app_config.attestation.default_actor


def test_invalid_attest_claim_does_not_migrate_version_four(
    app_config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _initialize_version_four(app_config, monkeypatch, seed_fact=False)

    with pytest.raises(StoreValidationError, match="attestation requires claim_key"):
        _attest(
            config=app_config,
            logger=logging.getLogger("engram.test.cli"),
            statement="This invalid attestation must not migrate.",
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
            claim_key=None,
            supersedes=(),
        )

    connection = sqlite3.connect(app_config.database.path)
    try:
        version = connection.execute("SELECT version FROM schema_version").fetchone()
        canonical_column = connection.execute(
            "SELECT 1 FROM pragma_table_info('entries') WHERE name = 'canonical_key'"
        ).fetchone()
    finally:
        connection.close()
    assert version == (4,)
    assert canonical_column is None


def test_migrate_inventory_and_classify_are_retry_safe(
    app_config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    entry_id = _initialize_version_four(app_config, monkeypatch, seed_fact=True)
    assert entry_id is not None
    logger = logging.getLogger("engram.test.cli")

    _migrate(config=app_config, logger=logger)
    first_migration = json.loads(capsys.readouterr().out)
    _migrate(config=app_config, logger=logger)
    second_migration = json.loads(capsys.readouterr().out)
    assert first_migration == second_migration == {"schema_version": 5}

    _list_entries(config=app_config, unclassified=True)
    inventory = json.loads(capsys.readouterr().out)
    assert [entry["id"] for entry in inventory] == [entry_id]

    _classify(
        config=app_config,
        logger=logger,
        entry_id=entry_id,
        claim_key="cli/legacy",
        actor="reviewer",
    )
    first_classification = json.loads(capsys.readouterr().out)
    _classify(
        config=app_config,
        logger=logger,
        entry_id=entry_id,
        claim_key=" CLI/LEGACY ",
        actor="reviewer",
    )
    second_classification = json.loads(capsys.readouterr().out)
    assert first_classification == second_classification
    assert first_classification["claim_key"] == "cli/legacy"

    with EngramStore(app_config) as store:
        assert [record.action for record in store.list_audit()] == [
            AuditAction.CLASSIFY,
            AuditAction.IDEMPOTENT_NOOP,
        ]


def test_daemon_lock_blocks_migrate_without_advancing_schema(
    app_config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _initialize_version_four(app_config, monkeypatch, seed_fact=False)

    with (
        DatabaseProcessLock(
            app_config.database.path,
            role=DatabaseLockRole.DAEMON,
            command="serve",
        ),
        pytest.raises(DatabaseLockError),
    ):
        _migrate(
            config=app_config,
            logger=logging.getLogger("engram.test.cli"),
        )

    connection = sqlite3.connect(app_config.database.path)
    try:
        version = connection.execute("SELECT version FROM schema_version").fetchone()
    finally:
        connection.close()
    assert version == (4,)


def test_preflight_validates_compatible_database_without_writing(
    app_config: AppConfig,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with EngramStore(app_config) as store:
        store.add_attested(
            kind="fact",
            scope="user",
            statement="The upgrade preflight is read-only.",
            source_type=SourceType.HUMAN,
            claim_key="upgrade/preflight",
        )
    before = app_config.database.path.read_bytes()

    _preflight(
        config=app_config,
        logger=logging.getLogger("engram.test.preflight"),
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["compatible"] is True
    assert payload["schema_version"] == 5
    assert app_config.database.path.read_bytes() == before


def test_preflight_wraps_database_open_failure(
    app_config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with EngramStore(app_config):
        pass

    def refused_open(*args: object, **kwargs: object) -> sqlite3.Connection:
        del args, kwargs
        raise sqlite3.OperationalError("unable to open database file")

    monkeypatch.setattr(sqlite3, "connect", refused_open)

    with pytest.raises(DatabaseError, match="upgrade preflight failed"):
        db_module.preflight_database(
            app_config.database,
            limits=app_config.limits,
        )


def test_preflight_rejects_oversized_non_integer_schema_version(
    app_config: AppConfig,
) -> None:
    connection = sqlite3.connect(app_config.database.path)
    try:
        connection.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
        connection.execute(
            "INSERT INTO schema_version(version) VALUES (zeroblob(?))",
            (8 * 1024 * 1024,),
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(DatabaseError, match="one non-negative integer"):
        db_module.preflight_database(
            app_config.database,
            limits=app_config.limits,
        )


def test_preflight_applies_resource_limits_before_loading_oversized_schema(
    app_config: AppConfig,
) -> None:
    oversized_comment = "x" * (MAX_SQLITE_VALUE_BYTES + 1)
    connection = sqlite3.connect(app_config.database.path)
    try:
        connection.execute(
            f"CREATE TABLE schema_version (version /* {oversized_comment} */ INTEGER NOT NULL)"
        )
        connection.execute("INSERT INTO schema_version(version) VALUES (5)")
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(DatabaseError, match="upgrade preflight failed"):
        db_module.preflight_database(
            app_config.database,
            limits=app_config.limits,
        )


def test_preflight_accepts_v4_with_missing_derived_indexes(
    app_config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _initialize_version_four(app_config, monkeypatch, seed_fact=False)
    connection = sqlite3.connect(app_config.database.path)
    try:
        connection.execute("DROP TABLE entries_fts")
        connection.execute("DROP TABLE entry_vectors")
        connection.commit()
    finally:
        connection.close()
    before = app_config.database.path.read_bytes()

    _preflight(
        config=app_config,
        logger=logging.getLogger("engram.test.preflight"),
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "compatible": True,
        "database": str(app_config.database.path),
        "schema_version": 4,
        "target_schema_version": 5,
        "fts_rebuild_required": True,
        "vector_rebuild_required": True,
    }
    assert app_config.database.path.read_bytes() == before


def test_preflight_rejects_legacy_schema_collision_without_writing(
    app_config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _initialize_version_four(app_config, monkeypatch, seed_fact=False)
    connection = sqlite3.connect(app_config.database.path)
    try:
        connection.execute("ALTER TABLE entries ADD COLUMN canonical_key TEXT")
        connection.commit()
    finally:
        connection.close()
    before = app_config.database.path.read_bytes()

    with pytest.raises(UpgradePreflightError, match="duplicate column name: canonical_key"):
        _preflight(
            config=app_config,
            logger=logging.getLogger("engram.test.preflight"),
        )

    assert app_config.database.path.read_bytes() == before


def test_preflight_rejects_unrebuildable_derived_object_without_writing(
    app_config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _initialize_version_four(app_config, monkeypatch, seed_fact=False)
    connection = sqlite3.connect(app_config.database.path)
    try:
        connection.execute("DROP TABLE entries_fts")
        connection.execute("CREATE INDEX entries_fts ON entries(id)")
        connection.commit()
    finally:
        connection.close()
    before = app_config.database.path.read_bytes()

    with pytest.raises(UpgradePreflightError, match="unexpected SQLite object"):
        _preflight(
            config=app_config,
            logger=logging.getLogger("engram.test.preflight"),
        )

    assert app_config.database.path.read_bytes() == before


def test_migrate_rejects_unrebuildable_derived_object_before_schema_commit(
    app_config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _initialize_version_four(app_config, monkeypatch, seed_fact=False)
    connection = sqlite3.connect(app_config.database.path)
    try:
        connection.execute("DROP TABLE entries_fts")
        connection.execute("CREATE INDEX entries_fts ON entries(id)")
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(DatabaseError, match="unexpected SQLite object"):
        _migrate(
            config=app_config,
            logger=logging.getLogger("engram.test.migrate"),
        )

    connection = sqlite3.connect(app_config.database.path)
    try:
        version = connection.execute("SELECT version FROM schema_version").fetchone()
    finally:
        connection.close()
    assert version == (4,)


def test_preflight_rejects_unrebuildable_v5_derived_object_without_writing(
    app_config: AppConfig,
) -> None:
    with EngramStore(app_config):
        pass
    connection = sqlite3.connect(app_config.database.path)
    try:
        connection.execute("DROP TABLE entries_fts")
        connection.execute("CREATE INDEX entries_fts ON entries(id)")
        connection.commit()
    finally:
        connection.close()
    before = app_config.database.path.read_bytes()

    with pytest.raises(UpgradePreflightError, match="unexpected SQLite object"):
        _preflight(
            config=app_config,
            logger=logging.getLogger("engram.test.preflight"),
        )

    assert app_config.database.path.read_bytes() == before


def test_preflight_rejects_v5_canonical_schema_drift_without_writing(
    app_config: AppConfig,
) -> None:
    with EngramStore(app_config):
        pass
    connection = sqlite3.connect(app_config.database.path)
    try:
        connection.execute("ALTER TABLE entries ADD COLUMN required_extra TEXT NOT NULL")
        connection.commit()
    finally:
        connection.close()
    before = app_config.database.path.read_bytes()

    with pytest.raises(UpgradePreflightError, match="canonical table definition is invalid"):
        _preflight(
            config=app_config,
            logger=logging.getLogger("engram.test.preflight"),
        )

    assert app_config.database.path.read_bytes() == before


def test_daemon_lock_blocks_preflight_without_writing(
    app_config: AppConfig,
) -> None:
    with EngramStore(app_config):
        pass
    before = app_config.database.path.read_bytes()

    with (
        DatabaseProcessLock(
            app_config.database.path,
            role=DatabaseLockRole.DAEMON,
            command="serve",
        ),
        pytest.raises(DatabaseLockError),
    ):
        _preflight(
            config=app_config,
            logger=logging.getLogger("engram.test.preflight"),
        )

    assert app_config.database.path.read_bytes() == before


def test_preflight_reports_legacy_oversize_without_mutating(
    app_config: AppConfig,
) -> None:
    with EngramStore(app_config) as store:
        entry = store.add_attested(
            kind="fact",
            scope="user",
            statement="Legacy oversized metadata needs human review.",
            source_type=SourceType.HUMAN,
            claim_key="upgrade/legacy-bound",
        )
    connection = sqlite3.connect(app_config.database.path)
    try:
        connection.execute(
            "UPDATE entries SET subject_keys = ? WHERE id = ?",
            (json.dumps(["k" * 201]), entry.id),
        )
        connection.commit()
    finally:
        connection.close()
    before = app_config.database.path.read_bytes()

    with pytest.raises(
        UpgradePreflightError,
        match=r"No data was changed.*2026\.0730\.01",
    ) as error:
        _preflight(
            config=app_config,
            logger=logging.getLogger("engram.test.preflight"),
        )

    assert entry.id in str(error.value)
    assert "subject key exceeds" in str(error.value)
    assert app_config.database.path.read_bytes() == before


def test_missing_supersession_ids_exit_without_traceback(
    app_config: AppConfig,
    tmp_path: Path,
) -> None:
    with EngramStore(app_config):
        pass
    config_path = tmp_path / "engram-cli.toml"
    config_path.write_text(
        f'[database]\npath = "{app_config.database.path.as_posix()}"\n',
        encoding="utf-8",
    )
    environment = os.environ.copy()
    environment["ENGRAM_CONFIG"] = str(config_path)

    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "from engram.cli import main; main()",
            "supersede",
            "--old",
            "01AAAAAAAAAAAAAAAAAAAAAAAA",
            "--new",
            "01BBBBBBBBBBBBBBBBBBBBBBBB",
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert completed.returncode == EXIT_USAGE_OR_CONFIG
    assert "does not exist" in completed.stderr
    assert "Traceback" not in completed.stderr
    assert completed.stdout == ""


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
                claim_key="cli/rejected",
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


def test_packaged_configuration_matches_the_repository_example() -> None:
    """The template the wheel carries and the one the checkout shows must not drift."""
    checkout = Path(__file__).resolve().parent.parent / "engram.example.toml"

    assert example_config_text() == checkout.read_text(encoding="utf-8")


def test_init_writes_a_configuration_the_loader_can_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    target = tmp_path / "engram.toml"
    monkeypatch.setattr(sys, "argv", ["engram", "--config", str(target), "init"])

    main()

    captured = capsys.readouterr()
    assert target.is_file()
    assert target.read_text(encoding="utf-8") == example_config_text()
    assert str(target) in captured.out
    assert load_config(target).database.path == (tmp_path / "engram.db").resolve()


def test_init_refuses_to_replace_an_existing_configuration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    target = tmp_path / "engram.toml"
    target.write_text("[database]\npath = 'kept.db'\n", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["engram", "--config", str(target), "init"])

    with pytest.raises(SystemExit) as exit_info:
        main()

    captured = capsys.readouterr()
    assert exit_info.value.code == EXIT_USAGE_OR_CONFIG
    assert "--force" in captured.err
    assert target.read_text(encoding="utf-8") == "[database]\npath = 'kept.db'\n"


def test_init_replaces_an_existing_configuration_when_forced(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    target = tmp_path / "engram.toml"
    target.write_text("[database]\npath = 'replaced.db'\n", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["engram", "--config", str(target), "init", "--force"])

    main()

    capsys.readouterr()
    assert target.read_text(encoding="utf-8") == example_config_text()


def test_stop_reports_that_nothing_owns_a_free_database(
    app_config: AppConfig,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _stop(config=app_config, logger=logging.getLogger("engram.test.stop"))

    assert json.loads(capsys.readouterr().out) == {
        "pid": None,
        "requested": False,
        "stopped": True,
    }
    assert caplog.records == []


def test_stop_confirms_the_daemon_released_the_database(
    app_config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The answer is the ownership lock, never the fact that a request was written."""
    observed = iter(
        (
            DatabaseOwnership(locked=True, role=DatabaseLockRole.DAEMON, pid=4242),
            DatabaseOwnership(locked=False, role=None, pid=None),
        )
    )
    requested: list[Path] = []

    def record(path: Path) -> Path:
        requested.append(path)
        return path

    monkeypatch.setattr(cli_module, "database_ownership", lambda _config: next(observed))
    monkeypatch.setattr(cli_module, "request_stop", record)

    _stop(config=app_config, logger=logging.getLogger("engram.test.stop"))

    assert requested == [app_config.database.path]
    assert json.loads(capsys.readouterr().out) == {
        "pid": 4242,
        "requested": True,
        "stopped": True,
    }


def test_stop_refuses_when_an_offline_writer_holds_the_database(
    app_config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        cli_module,
        "database_ownership",
        lambda _config: DatabaseOwnership(
            locked=True,
            role=DatabaseLockRole.OFFLINE_WRITER,
            pid=99,
        ),
    )

    with pytest.raises(DaemonStopError, match="offline writer"):
        _stop(config=app_config, logger=logging.getLogger("engram.test.stop"))


def test_stop_reports_a_daemon_that_did_not_release_the_database(
    app_config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A request that was written is not a daemon that stopped."""
    monkeypatch.setattr(
        cli_module,
        "database_ownership",
        lambda _config: DatabaseOwnership(locked=True, role=DatabaseLockRole.DAEMON, pid=7),
    )
    monkeypatch.setattr(cli_module, "request_stop", lambda path: path)
    monkeypatch.setattr(cli_module, "STOP_COMMAND_TIMEOUT_SECONDS", 0.0)

    with pytest.raises(DaemonStopError, match="still holds the database"):
        _stop(config=app_config, logger=logging.getLogger("engram.test.stop"))
