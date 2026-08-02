# Copyright 2026 Julien Bombled
# SPDX-License-Identifier: Apache-2.0

"""Windowless logon autostart tests."""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING
from xml.etree import ElementTree as ET

import pytest

import engram.__main__ as module_entry_point
import engram.autostart as autostart_module
from engram.autostart import (
    TASK_NAMESPACE,
    AutostartError,
    AutostartUnsupportedError,
    DatabaseOwnership,
    build_plan,
    database_ownership,
    install,
    is_installed,
    require_supported_platform,
    task_name,
    uninstall,
    windowed_interpreter,
)
from engram.cli import (
    EXIT_LOCAL_RESOURCE,
    EXIT_USAGE_OR_CONFIG,
    _error_exit_code,
    _resolve_config_path,
    _setup_autostart,
    main,
)
from engram.config import DEFAULT_CONFIG_PATH, ENV_PREFIX
from engram.process_lock import DatabaseLockRole, DatabaseProcessLock

if TYPE_CHECKING:
    from collections.abc import Mapping

    from engram.config import AppConfig

EXAMPLE_CONFIG = Path(__file__).resolve().parent.parent / "engram.example.toml"
LOGGER = logging.getLogger("engram-autostart-test")


class RecordingScheduler:
    """Answer scheduler invocations from a script and record every argv."""

    def __init__(self, *, present: bool, failures: Mapping[str, int] | None = None) -> None:
        """Start from a known registration state and optional per-verb failures."""
        self.present = present
        self.failures = dict(failures or {})
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, arguments: tuple[str, ...]) -> subprocess.CompletedProcess[bytes]:
        """Record one invocation and answer as the scheduler would."""
        self.calls.append(arguments)
        verb = arguments[0]
        code = (0 if self.present else 1) if verb == "/Query" else self.failures.get(verb, 0)
        if code == 0 and verb == "/Create":
            self.present = True
        if code == 0 and verb == "/Delete":
            self.present = False
        return subprocess.CompletedProcess([verb], code, b"", b"scheduler refused the request")

    def verbs(self) -> list[str]:
        """Return the verb of every recorded invocation, in order."""
        return [call[0] for call in self.calls]


@pytest.fixture
def windows_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Pretend to run on Windows from an interpreter that ships a windowed build."""
    scripts = tmp_path / "Scripts"
    scripts.mkdir()
    (scripts / "python.exe").write_bytes(b"")
    (scripts / "pythonw.exe").write_bytes(b"")
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(sys, "executable", str(scripts / "python.exe"))
    return scripts


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    """Return a configuration path outside the working directory."""
    path = tmp_path / "runtime" / DEFAULT_CONFIG_PATH.name
    path.parent.mkdir(parents=True)
    path.write_text(EXAMPLE_CONFIG.read_text(encoding="utf-8"), encoding="utf-8")
    return path


def test_module_entry_point_delegates_to_the_console_entry_point() -> None:
    """The windowed launch path and the console script must run the same command."""
    assert module_entry_point.main is main


def test_module_entry_point_starts_without_the_console_script() -> None:
    """The package must be runnable by an interpreter that cannot run a launcher."""
    completed = subprocess.run(
        [sys.executable, "-m", "engram", "--help"],
        capture_output=True,
        check=False,
        text=True,
    )

    assert completed.returncode == 0
    assert "setup" in completed.stdout


def test_task_name_is_stable_and_specific_to_one_configuration(tmp_path: Path) -> None:
    first = tmp_path / "one" / DEFAULT_CONFIG_PATH.name
    second = tmp_path / "two" / DEFAULT_CONFIG_PATH.name

    assert task_name(first) == task_name(first)
    assert task_name(first) != task_name(second)


def test_task_name_ignores_the_case_windows_ignores(tmp_path: Path) -> None:
    lowered = tmp_path / "runtime" / DEFAULT_CONFIG_PATH.name

    assert task_name(lowered) == task_name(Path(str(lowered).upper()))


def test_windowed_interpreter_is_the_build_beside_the_running_one(
    windows_runtime: Path,
) -> None:
    resolved = windowed_interpreter(windows_runtime / "python.exe")

    assert resolved == (windows_runtime / "pythonw.exe").resolve()


def test_windowed_interpreter_refuses_a_runtime_without_one(tmp_path: Path) -> None:
    console_only = tmp_path / "python.exe"
    console_only.write_bytes(b"")

    with pytest.raises(AutostartError) as failure:
        windowed_interpreter(console_only)

    assert "pythonw.exe" in str(failure.value)


def test_require_supported_platform_refuses_other_operating_systems(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "linux")

    with pytest.raises(AutostartUnsupportedError) as failure:
        require_supported_platform()

    assert "linux" in str(failure.value)


def test_unsupported_platform_is_a_configuration_exit_not_a_silent_success() -> None:
    assert _error_exit_code(AutostartUnsupportedError("unsupported")) == EXIT_USAGE_OR_CONFIG
    assert _error_exit_code(AutostartError("refused")) == EXIT_LOCAL_RESOURCE


def test_setup_command_refuses_a_host_without_a_logon_scheduler(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    config_file: Path,
) -> None:
    del tmp_path
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(
        sys,
        "argv",
        ["engram", "--config", str(config_file), "setup", "autostart", "--status"],
    )

    with pytest.raises(SystemExit) as exit_info:
        main()

    assert exit_info.value.code == EXIT_USAGE_OR_CONFIG


def test_build_plan_resolves_the_running_interpreter_and_configuration(
    windows_runtime: Path,
    config_file: Path,
) -> None:
    plan = build_plan(config_file)

    assert plan.command == (windows_runtime / "pythonw.exe").resolve()
    assert plan.arguments == ("-m", "engram", "--config", str(config_file.resolve()), "serve")
    assert plan.working_directory == config_file.resolve().parent
    assert plan.task_name == task_name(config_file.resolve())


def test_argument_line_quotes_a_configuration_path_containing_spaces(
    windows_runtime: Path,
    tmp_path: Path,
) -> None:
    del windows_runtime
    spaced = tmp_path / "an engram runtime" / DEFAULT_CONFIG_PATH.name
    spaced.parent.mkdir(parents=True)
    spaced.write_text("", encoding="utf-8")

    plan = build_plan(spaced)

    assert f'"{spaced.resolve()}"' in plan.argument_line


def test_resolve_config_path_prefers_the_explicit_option(tmp_path: Path) -> None:
    explicit = tmp_path / "explicit.toml"

    resolved = _resolve_config_path(explicit, {f"{ENV_PREFIX}CONFIG": str(tmp_path / "env.toml")})

    assert resolved == explicit.resolve()


def test_resolve_config_path_falls_back_to_the_environment(tmp_path: Path) -> None:
    from_environment = tmp_path / "env.toml"

    resolved = _resolve_config_path(None, {f"{ENV_PREFIX}CONFIG": str(from_environment)})

    assert resolved == from_environment.resolve()


def test_resolve_config_path_falls_back_to_the_documented_default() -> None:
    assert _resolve_config_path(None, {}) == DEFAULT_CONFIG_PATH.resolve()


def test_database_ownership_reports_a_free_database(app_config: AppConfig) -> None:
    ownership = database_ownership(app_config)

    assert ownership == DatabaseOwnership(locked=False, role=None, pid=None)
    assert not ownership.daemon_running


def test_database_ownership_names_the_daemon_that_holds_the_lock(
    app_config: AppConfig,
) -> None:
    with DatabaseProcessLock(
        app_config.database.path,
        role=DatabaseLockRole.DAEMON,
        command="serve",
    ):
        ownership = database_ownership(app_config)

    assert ownership.locked
    assert ownership.daemon_running
    assert ownership.role is DatabaseLockRole.DAEMON
    assert ownership.pid is not None


def test_install_registers_then_converges_without_a_second_task(
    windows_runtime: Path,
    config_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del windows_runtime
    scheduler = RecordingScheduler(present=False)
    monkeypatch.setattr(autostart_module, "_run_scheduler", scheduler)
    plan = build_plan(config_file)
    free = DatabaseOwnership(locked=False, role=None, pid=None)

    first = install(plan, ownership=free, logger=LOGGER)
    second = install(plan, ownership=free, logger=LOGGER)

    assert first.created
    assert not second.created
    assert scheduler.verbs().count("/Create") == 2
    assert all(call[2] == plan.task_name for call in scheduler.calls)
    assert is_installed(plan)


def test_install_starts_the_daemon_only_when_the_database_is_free(
    windows_runtime: Path,
    config_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del windows_runtime
    scheduler = RecordingScheduler(present=False)
    monkeypatch.setattr(autostart_module, "_run_scheduler", scheduler)

    outcome = install(
        build_plan(config_file),
        ownership=DatabaseOwnership(locked=False, role=None, pid=None),
        logger=LOGGER,
    )

    assert outcome.started
    assert outcome.start_skipped_reason is None
    assert "/Run" in scheduler.verbs()


def test_install_refuses_to_start_a_second_writer(
    windows_runtime: Path,
    config_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del windows_runtime
    scheduler = RecordingScheduler(present=False)
    monkeypatch.setattr(autostart_module, "_run_scheduler", scheduler)

    outcome = install(
        build_plan(config_file),
        ownership=DatabaseOwnership(locked=True, role=DatabaseLockRole.DAEMON, pid=4321),
        logger=LOGGER,
    )

    assert not outcome.started
    assert outcome.start_skipped_reason is not None
    assert "4321" in outcome.start_skipped_reason
    assert "/Run" not in scheduler.verbs()


def test_install_reports_an_unidentified_owner_without_claiming_success(
    windows_runtime: Path,
    config_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del windows_runtime
    monkeypatch.setattr(autostart_module, "_run_scheduler", RecordingScheduler(present=False))

    outcome = install(
        build_plan(config_file),
        ownership=DatabaseOwnership(locked=True, role=None, pid=None),
        logger=LOGGER,
    )

    assert not outcome.started
    assert outcome.start_skipped_reason is not None
    assert "unidentified" in outcome.start_skipped_reason


def test_install_surfaces_a_scheduler_refusal(
    windows_runtime: Path,
    config_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del windows_runtime
    monkeypatch.setattr(
        autostart_module,
        "_run_scheduler",
        RecordingScheduler(present=False, failures={"/Create": 1}),
    )

    with pytest.raises(AutostartError) as failure:
        install(
            build_plan(config_file),
            ownership=DatabaseOwnership(locked=False, role=None, pid=None),
            logger=LOGGER,
        )

    assert "exit 1" in str(failure.value)
    assert "scheduler refused the request" in str(failure.value)


def test_uninstall_removes_the_task_then_reports_it_absent(
    windows_runtime: Path,
    config_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del windows_runtime
    scheduler = RecordingScheduler(present=True)
    monkeypatch.setattr(autostart_module, "_run_scheduler", scheduler)
    plan = build_plan(config_file)

    assert uninstall(plan, logger=LOGGER)
    assert not uninstall(plan, logger=LOGGER)
    assert scheduler.verbs().count("/Delete") == 1


def test_uninstall_surfaces_a_scheduler_refusal(
    windows_runtime: Path,
    config_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del windows_runtime
    monkeypatch.setattr(
        autostart_module,
        "_run_scheduler",
        RecordingScheduler(present=True, failures={"/Delete": 1}),
    )

    with pytest.raises(AutostartError):
        uninstall(build_plan(config_file), logger=LOGGER)


def test_task_definition_describes_a_windowless_logon_task(
    windows_runtime: Path,
    config_file: Path,
) -> None:
    plan = build_plan(config_file)

    root = ET.fromstring(autostart_module._task_definition(plan))  # noqa: S314, SLF001

    def element(path: str) -> ET.Element:
        found = root.find(path, {"": TASK_NAMESPACE})
        assert found is not None, path
        return found

    assert element("./{*}Principals/{*}Principal/{*}LogonType").text == "InteractiveToken"
    assert element("./{*}Triggers/{*}LogonTrigger/{*}UserId").text
    assert element("./{*}Settings/{*}ExecutionTimeLimit").text == "PT0S"
    assert element("./{*}Settings/{*}MultipleInstancesPolicy").text == "IgnoreNew"
    command = element("./{*}Actions/{*}Exec/{*}Command").text
    assert command == str((windows_runtime / "pythonw.exe").resolve())
    assert element("./{*}Actions/{*}Exec/{*}Arguments").text == plan.argument_line
    assert element("./{*}Actions/{*}Exec/{*}WorkingDirectory").text == str(plan.working_directory)


def test_task_definition_is_handed_to_the_scheduler_as_utf16(
    windows_runtime: Path,
    config_file: Path,
) -> None:
    del windows_runtime
    definition = autostart_module._task_definition(build_plan(config_file))  # noqa: SLF001

    written = autostart_module._write_definition(definition)  # noqa: SLF001
    try:
        raw = written.read_bytes()
    finally:
        written.unlink()

    assert raw.decode("utf-16") == definition
    assert not written.exists()


def test_missing_scheduler_is_reported_rather_than_assumed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(shutil, "which", lambda _command: None)

    with pytest.raises(AutostartError) as failure:
        autostart_module._run_scheduler(("/Query",))  # noqa: SLF001

    assert "schtasks" in str(failure.value)


def test_scheduler_argv_is_resolved_and_never_shelled_out(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    resolved = tmp_path / "schtasks.exe"
    recorded: dict[str, object] = {}

    def capture(argv: list[str], **keywords: object) -> subprocess.CompletedProcess[bytes]:
        recorded["argv"] = argv
        recorded["keywords"] = keywords
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr(shutil, "which", lambda _command: str(resolved))
    monkeypatch.setattr(subprocess, "run", capture)

    autostart_module._run_scheduler(("/Query", "/TN", "Engram Autostart 00000000"))  # noqa: SLF001

    assert recorded["argv"] == [str(resolved), "/Query", "/TN", "Engram Autostart 00000000"]
    assert "shell" not in recorded["keywords"]  # type: ignore[operator]


def test_current_account_requires_a_named_user() -> None:
    with pytest.raises(AutostartError) as failure:
        autostart_module._current_account({})  # noqa: SLF001

    assert "USERNAME" in str(failure.value)


def test_current_account_qualifies_the_user_with_its_domain() -> None:
    account = autostart_module._current_account(  # noqa: SLF001
        {"USERDOMAIN": "NYX", "USERNAME": "User"}
    )

    assert account == "NYX\\User"


def test_status_reports_an_absent_task_and_the_current_owner(
    app_config: AppConfig,
    windows_runtime: Path,
    config_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    del windows_runtime
    monkeypatch.setattr(autostart_module, "_run_scheduler", RecordingScheduler(present=False))

    _setup_autostart(
        config=app_config,
        config_path=config_file,
        logger=LOGGER,
        install_requested=False,
        uninstall_requested=False,
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["action"] == "status"
    assert payload["installed"] is False
    assert payload["daemon_running"] is False
    assert payload["database_locked"] is False
    assert payload["config"] == str(config_file.resolve())
    assert payload["command"].endswith("pythonw.exe")


def test_status_reports_the_registered_task_and_a_running_daemon(
    app_config: AppConfig,
    windows_runtime: Path,
    config_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    del windows_runtime
    monkeypatch.setattr(autostart_module, "_run_scheduler", RecordingScheduler(present=True))

    with DatabaseProcessLock(
        app_config.database.path,
        role=DatabaseLockRole.DAEMON,
        command="serve",
    ):
        _setup_autostart(
            config=app_config,
            config_path=config_file,
            logger=LOGGER,
            install_requested=False,
            uninstall_requested=False,
        )

    payload = json.loads(capsys.readouterr().out)
    assert payload["installed"] is True
    assert payload["daemon_running"] is True
    assert payload["database_owner_role"] == DatabaseLockRole.DAEMON.value
    assert payload["database_owner_pid"] is not None


def test_install_action_reports_what_it_registered(
    app_config: AppConfig,
    windows_runtime: Path,
    config_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    del windows_runtime
    monkeypatch.setattr(autostart_module, "_run_scheduler", RecordingScheduler(present=False))

    _setup_autostart(
        config=app_config,
        config_path=config_file,
        logger=LOGGER,
        install_requested=True,
        uninstall_requested=False,
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["action"] == "install"
    assert payload["created"] is True
    assert payload["started"] is True
    assert payload["start_skipped_reason"] is None
    assert payload["task_name"] == task_name(config_file.resolve())


def test_uninstall_action_reports_an_already_absent_task(
    app_config: AppConfig,
    windows_runtime: Path,
    config_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    del windows_runtime
    monkeypatch.setattr(autostart_module, "_run_scheduler", RecordingScheduler(present=False))

    _setup_autostart(
        config=app_config,
        config_path=config_file,
        logger=LOGGER,
        install_requested=False,
        uninstall_requested=True,
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["action"] == "uninstall"
    assert payload["removed"] is False


def test_setup_requires_one_explicit_action(
    monkeypatch: pytest.MonkeyPatch,
    config_file: Path,
) -> None:
    monkeypatch.setattr(sys, "argv", ["engram", "--config", str(config_file), "setup", "autostart"])

    with pytest.raises(SystemExit) as exit_info:
        main()

    assert exit_info.value.code == EXIT_USAGE_OR_CONFIG
