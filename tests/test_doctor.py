# Copyright 2026 Julien Bombled
# SPDX-License-Identifier: Apache-2.0

"""Installation diagnosis tests."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import engram.doctor as doctor_module
from engram.autostart import DatabaseOwnership
from engram.config import AppConfig
from engram.db import MINIMUM_SQLITE_VERSION
from engram.doctor import Check, Outcome, _check_database, _check_endpoint, diagnose, worst_outcome
from engram.process_lock import DatabaseLockRole
from engram.resources import example_config_text


def _named(checks: tuple[Check, ...], name: str) -> Check:
    return next(check for check in checks if check.name == name)


def _written_config(tmp_path: Path) -> Path:
    path = tmp_path / "engram.toml"
    path.write_text(example_config_text(), encoding="utf-8")
    return path


def test_a_missing_configuration_names_the_command_that_writes_one(tmp_path: Path) -> None:
    checks = diagnose(tmp_path / "engram.toml")

    configuration = _named(checks, "configuration")
    assert configuration.outcome is Outcome.FAIL
    assert configuration.remedy is not None
    assert "engram init" in configuration.remedy
    assert worst_outcome(checks) is Outcome.FAIL


def test_an_unreadable_configuration_is_reported_rather_than_raised(tmp_path: Path) -> None:
    """A diagnosis that fails on the file it exists to inspect explains nothing."""
    path = tmp_path / "engram.toml"
    path.write_text("[database\npath = 'x'\n", encoding="utf-8")

    checks = diagnose(path)

    configuration = _named(checks, "configuration")
    assert configuration.outcome is Outcome.FAIL
    assert "does not load" in configuration.detail
    # The checks that do not depend on the configuration still ran.
    assert _named(checks, "python").outcome is Outcome.OK
    assert _named(checks, "sqlite").outcome is Outcome.OK


def test_checks_that_do_not_need_the_configuration_run_before_it(tmp_path: Path) -> None:
    checks = diagnose(tmp_path / "absent.toml")

    assert [check.name for check in checks] == ["python", "sqlite", "configuration"]


def test_a_runtime_below_the_sqlite_floor_fails_with_a_repair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    below = (MINIMUM_SQLITE_VERSION[0], MINIMUM_SQLITE_VERSION[1], MINIMUM_SQLITE_VERSION[2] - 1)
    monkeypatch.setattr(sqlite3, "sqlite_version_info", below)
    monkeypatch.setattr(sqlite3, "sqlite_version", ".".join(str(part) for part in below))

    checks = diagnose(_written_config(tmp_path))

    sqlite_check = _named(checks, "sqlite")
    assert sqlite_check.outcome is Outcome.FAIL
    assert sqlite_check.remedy is not None
    assert "uv python install" in sqlite_check.remedy
    assert "https://" in sqlite_check.remedy


def test_a_database_that_does_not_exist_yet_is_a_warning_with_the_next_command(
    app_config: AppConfig,
) -> None:
    check = _check_database(app_config)

    assert check.outcome is Outcome.WARN
    assert check.remedy == "Run 'engram migrate' to create it"


def test_a_database_newer_than_this_build_fails_instead_of_being_migrated(
    app_config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app_config.database.path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(app_config.database.path)
    connection.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
    connection.execute("INSERT INTO schema_version(version) VALUES (9999)")
    connection.commit()
    connection.close()
    monkeypatch.setattr(doctor_module, "latest_schema_version", lambda: 5)

    check = _check_database(app_config)

    assert check.outcome is Outcome.FAIL
    assert "newer than" in check.detail


def test_a_listening_port_with_no_daemon_names_the_other_process(
    app_config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A port that accepts is not proof that this Engram is behind it."""
    monkeypatch.setattr(doctor_module, "_endpoint_accepts", lambda _config: True)

    check = _check_endpoint(app_config, serving=False)

    assert check.outcome is Outcome.WARN
    assert "another process is listening" in check.detail
    assert check.remedy is not None
    assert "reaches that process, not this database" in check.remedy


def test_a_serving_daemon_behind_a_closed_port_is_a_failure(
    app_config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(doctor_module, "_endpoint_accepts", lambda _config: False)

    check = _check_endpoint(app_config, serving=True)

    assert check.outcome is Outcome.FAIL
    assert "refuses connections" in check.detail


def test_a_serving_daemon_on_its_own_port_passes(
    app_config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(doctor_module, "_endpoint_accepts", lambda _config: True)

    assert _check_endpoint(app_config, serving=True).outcome is Outcome.OK


def test_a_complete_installation_reports_ready(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _written_config(tmp_path)
    database = tmp_path / "engram.db"
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
    connection.execute("INSERT INTO schema_version(version) VALUES (5)")
    connection.commit()
    connection.close()
    monkeypatch.setattr(doctor_module, "latest_schema_version", lambda: 5)
    monkeypatch.setattr(doctor_module, "_endpoint_accepts", lambda _config: True)
    monkeypatch.setattr(
        doctor_module,
        "database_ownership",
        lambda _config: DatabaseOwnership(locked=True, role=DatabaseLockRole.DAEMON, pid=11),
    )

    checks = diagnose(path)

    assert worst_outcome(checks) is Outcome.OK
    assert [check.name for check in checks] == [
        "python",
        "sqlite",
        "configuration",
        "database",
        "daemon",
        "endpoint",
        "logging",
    ]


def test_worst_outcome_reports_the_most_severe_finding() -> None:
    passing = Check(name="a", outcome=Outcome.OK, detail="")
    warning = Check(name="b", outcome=Outcome.WARN, detail="")
    failing = Check(name="c", outcome=Outcome.FAIL, detail="")

    assert worst_outcome((passing,)) is Outcome.OK
    assert worst_outcome((passing, warning)) is Outcome.WARN
    assert worst_outcome((passing, warning, failing)) is Outcome.FAIL
    assert worst_outcome(()) is Outcome.OK
