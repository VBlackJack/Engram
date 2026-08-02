# Copyright 2026 Julien Bombled
# SPDX-License-Identifier: Apache-2.0

"""Windowless logon autostart tests."""

from __future__ import annotations

import _thread
import argparse
import json
import logging
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING
from xml.etree import ElementTree as ET

import pytest

import engram.__main__ as module_entry_point
import engram.autostart as autostart_module
import engram.cli as cli_module
from engram.autostart import (
    TASK_NAMESPACE,
    AutostartConflictError,
    AutostartError,
    AutostartUnsupportedError,
    DatabaseOwnership,
    StopMethod,
    TaskConflict,
    build_plan,
    canonical_path,
    clear_stop_request,
    conflicting_tasks,
    database_ownership,
    install,
    is_installed,
    registered_actions,
    request_stop,
    require_supported_platform,
    resolve_conflicts,
    same_path,
    stop_request_path,
    stop_requested,
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
from engram.config import DEFAULT_CONFIG_PATH, ENV_PREFIX, load_config
from engram.process_lock import (
    DatabaseLockError,
    DatabaseLockRole,
    DatabaseProcessLock,
    database_lock_path,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from engram.config import AppConfig

EXAMPLE_CONFIG = Path(__file__).resolve().parent.parent / "engram.example.toml"
LOGGER = logging.getLogger("engram-autostart-test")


EMPTY_TASK_DUMP = "<Tasks></Tasks>"


def task_dump(*tasks: str) -> str:
    """Build a dump shaped like the one the scheduler produces for every task."""
    return f"<Tasks>{''.join(tasks)}</Tasks>"


def scheduled_task(
    name: str,
    *,
    command: str,
    arguments: str = "",
    working_directory: str | None = None,
    enabled: bool = True,
) -> str:
    """Render one task the way the scheduler dumps it, named by a comment."""
    directory = (
        ""
        if working_directory is None
        else f"<WorkingDirectory>{working_directory}</WorkingDirectory>"
    )
    return (
        f"<!-- \\{name} -->"
        f'<Task version="1.3" xmlns="{TASK_NAMESPACE}">'
        f"<Settings><Enabled>{'true' if enabled else 'false'}</Enabled></Settings>"
        f'<Actions Context="Author"><Exec>'
        f"<Command>{command}</Command><Arguments>{arguments}</Arguments>{directory}"
        f"</Exec></Actions></Task>"
    )


class RecordingScheduler:
    """Answer scheduler invocations from a script and record every argv."""

    def __init__(
        self,
        *,
        present: bool,
        failures: Mapping[str, int] | None = None,
        dump: str = EMPTY_TASK_DUMP,
    ) -> None:
        """Start from a known registration state and optional per-verb failures."""
        self.present = present
        self.failures = dict(failures or {})
        self.dump = dump
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, arguments: tuple[str, ...]) -> subprocess.CompletedProcess[bytes]:
        """Record one invocation and answer as the scheduler would."""
        self.calls.append(arguments)
        verb = arguments[0]
        if verb == "/Query" and "/TN" not in arguments:
            return subprocess.CompletedProcess([verb], 0, self.dump.encode("utf-8"), b"")
        code = (0 if self.present else 1) if verb == "/Query" else self.failures.get(verb, 0)
        if code == 0 and verb == "/Create":
            self.present = True
        if code == 0 and verb == "/Delete":
            self.present = False
        return subprocess.CompletedProcess([verb], code, b"", b"scheduler refused the request")

    def verbs(self) -> list[str]:
        """Return the verb of every recorded invocation, in order."""
        return [call[0] for call in self.calls]

    def named(self, verb: str) -> list[str]:
        """Return the task names this verb was invoked on, in order."""
        return [call[2] for call in self.calls if call[0] == verb and len(call) > 2]


def autostart_arguments(
    *,
    install: bool = False,
    uninstall: bool = False,
    replace: bool = False,
    force: bool = False,
) -> argparse.Namespace:
    """Build the parsed arguments the setup dispatch reads."""
    return argparse.Namespace(
        install=install,
        uninstall=uninstall,
        status=not (install or uninstall),
        replace=replace,
        force=force,
    )


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


@pytest.fixture
def runtime_config(config_file: Path) -> AppConfig:
    """Return the very configuration the competing tasks in these tests also load."""
    return load_config(config_file)


def test_module_entry_point_delegates_to_the_console_entry_point() -> None:
    """The windowed launch path and the console script must run the same command."""
    assert module_entry_point.main is main


def test_absent_standard_streams_are_bound_before_the_transport_reads_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A windowed interpreter starts with no streams, and uvicorn asks stdout for a tty."""
    for name in module_entry_point.NULL_STREAM_MODES:
        monkeypatch.setattr(sys, name, None)

    bound = module_entry_point.bind_absent_standard_streams()

    try:
        assert set(bound) == set(module_entry_point.NULL_STREAM_MODES)
        # The question uvicorn asks while configuring its logging, which raised
        # AttributeError on the None a windowed interpreter leaves behind.
        assert isinstance(sys.stdout.isatty(), bool)
        assert sys.stdout.fileno() >= 0
        sys.stdout.write("discarded")
    finally:
        for name in bound:
            getattr(sys, name).close()


def test_present_standard_streams_are_left_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A console interpreter must keep the streams it was given."""
    monkeypatch.setattr(sys, "stdin", None)
    original = sys.stdout

    bound = module_entry_point.bind_absent_standard_streams()

    try:
        assert bound == ("stdin",)
        assert sys.stdout is original
    finally:
        sys.stdin.close()


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
        arguments=autostart_arguments(),
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
            arguments=autostart_arguments(),
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
        arguments=autostart_arguments(install=True),
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
        arguments=autostart_arguments(uninstall=True),
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


def test_replace_and_force_apply_to_install_only(
    monkeypatch: pytest.MonkeyPatch,
    config_file: Path,
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        ["engram", "--config", str(config_file), "setup", "autostart", "--status", "--replace"],
    )

    with pytest.raises(SystemExit) as exit_info:
        main()

    assert exit_info.value.code == EXIT_USAGE_OR_CONFIG


# --- Competing scheduled tasks -------------------------------------------------


def competing_engram_task(
    name: str,
    interpreter: Path,
    config: Path,
    *,
    enabled: bool = True,
) -> str:
    """Render a task that starts this package with an explicit configuration."""
    return scheduled_task(
        name,
        command=str(interpreter),
        arguments=f'-m engram --config "{config}" serve',
        enabled=enabled,
    )


def test_task_dump_names_every_task_from_the_comment_that_precedes_it(
    windows_runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        autostart_module,
        "_run_scheduler",
        RecordingScheduler(
            present=False,
            dump=task_dump(
                scheduled_task("Some Vendor\\Updater", command="C:\\vendor\\update.exe"),
                scheduled_task("Disabled Thing", command="C:\\vendor\\other.exe", enabled=False),
                # A task whose action is not an executable, which the scheduler
                # also dumps and which names no process to compare.
                f'<!-- \\Handler -->\n<Task version="1.3" xmlns="{TASK_NAMESPACE}">'
                "<Actions><ComHandler /></Actions></Task>",
            ),
        ),
    )
    del windows_runtime

    actions = registered_actions()

    assert [action.task_name for action in actions] == ["Some Vendor\\Updater", "Disabled Thing"]
    assert [action.enabled for action in actions] == [True, False]


def test_canonical_path_collapses_the_spellings_windows_treats_as_one(tmp_path: Path) -> None:
    present = tmp_path / "engram.db"
    present.write_bytes(b"")

    assert canonical_path(f'  "{present}" ') == present.resolve()
    assert same_path(canonical_path(str(present).upper()) or present, present.resolve())
    assert canonical_path("   ") is None


def test_conflicting_tasks_finds_a_task_that_opens_the_same_database(
    windows_runtime: Path,
    config_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    interpreter = windows_runtime / "pythonw.exe"
    monkeypatch.setattr(
        autostart_module,
        "_run_scheduler",
        RecordingScheduler(
            present=False,
            dump=task_dump(competing_engram_task("Engram Local Daemon", interpreter, config_file)),
        ),
    )
    database = (config_file.parent / "engram.db").resolve()

    conflicts = conflicting_tasks(build_plan(config_file), database=database)

    assert [conflict.task_name for conflict in conflicts] == ["Engram Local Daemon"]
    assert conflicts[0].resolved
    assert conflicts[0].database == database


def test_conflicting_tasks_ignores_a_task_serving_another_database(
    windows_runtime: Path,
    config_file: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    elsewhere = tmp_path / "elsewhere" / DEFAULT_CONFIG_PATH.name
    elsewhere.parent.mkdir(parents=True)
    elsewhere.write_text(EXAMPLE_CONFIG.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setattr(
        autostart_module,
        "_run_scheduler",
        RecordingScheduler(
            present=False,
            dump=task_dump(
                competing_engram_task("Other Engram", windows_runtime / "pythonw.exe", elsewhere)
            ),
        ),
    )

    conflicts = conflicting_tasks(
        build_plan(config_file),
        database=(config_file.parent / "engram.db").resolve(),
    )

    assert conflicts == ()


def test_conflicting_tasks_refuses_to_clear_a_task_it_cannot_resolve(
    windows_runtime: Path,
    config_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A wrapper script hides the configuration, and a hidden answer is not 'no conflict'."""
    del windows_runtime
    script = config_file.parent / "start-engram.ps1"
    script.write_text("", encoding="utf-8")
    monkeypatch.setattr(
        autostart_module,
        "_run_scheduler",
        RecordingScheduler(
            present=False,
            dump=task_dump(
                scheduled_task(
                    "Engram Local Daemon",
                    command="C:\\Program Files\\PowerShell\\7\\pwsh.exe",
                    arguments=f'-NoProfile -WindowStyle Hidden -File "{script}"',
                )
            ),
        ),
    )

    conflicts = conflicting_tasks(
        build_plan(config_file),
        database=(config_file.parent / "engram.db").resolve(),
    )

    assert [conflict.task_name for conflict in conflicts] == ["Engram Local Daemon"]
    assert not conflicts[0].resolved
    assert conflicts[0].database is None
    assert "undetermined" in conflicts[0].reason


def test_conflicting_tasks_ignores_a_disabled_task_and_its_own(
    windows_runtime: Path,
    config_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    interpreter = windows_runtime / "pythonw.exe"
    plan = build_plan(config_file)
    monkeypatch.setattr(
        autostart_module,
        "_run_scheduler",
        RecordingScheduler(
            present=True,
            dump=task_dump(
                competing_engram_task("Retired Daemon", interpreter, config_file, enabled=False),
                competing_engram_task(plan.task_name, interpreter, config_file),
            ),
        ),
    )

    conflicts = conflicting_tasks(plan, database=(config_file.parent / "engram.db").resolve())

    assert conflicts == ()


def _conflict(name: str = "Engram Local Daemon", *, database: Path | None = None) -> TaskConflict:
    return TaskConflict(
        task_name=name,
        enabled=True,
        database=database,
        reason="starts Engram on the same database" if database else "undetermined",
    )


def test_resolve_conflicts_refuses_and_names_the_competing_task(
    app_config: AppConfig,
) -> None:
    with pytest.raises(AutostartConflictError) as failure:
        resolve_conflicts(
            (_conflict(database=app_config.database.path),),
            config=app_config,
            replace=False,
            force=False,
            logger=LOGGER,
        )

    assert "Engram Local Daemon" in str(failure.value)
    assert "--replace" in str(failure.value)


def test_resolve_conflicts_refuses_an_undetermined_task_rather_than_assuming_it_is_free(
    app_config: AppConfig,
) -> None:
    with pytest.raises(AutostartConflictError) as failure:
        resolve_conflicts(
            (_conflict(),),
            config=app_config,
            replace=False,
            force=False,
            logger=LOGGER,
        )

    assert "undetermined" in str(failure.value)


def test_force_installs_over_a_conflict_without_disabling_anything(
    app_config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scheduler = RecordingScheduler(present=False)
    monkeypatch.setattr(autostart_module, "_run_scheduler", scheduler)

    disabled = resolve_conflicts(
        (_conflict(),),
        config=app_config,
        replace=False,
        force=True,
        logger=LOGGER,
    )

    assert disabled == ()
    assert "/Change" not in scheduler.verbs()


def test_replace_disables_the_competing_task_without_deleting_it(
    app_config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scheduler = RecordingScheduler(present=True)
    monkeypatch.setattr(autostart_module, "_run_scheduler", scheduler)

    disabled = resolve_conflicts(
        (_conflict(database=app_config.database.path),),
        config=app_config,
        replace=True,
        force=False,
        logger=LOGGER,
    )

    assert [task.task_name for task in disabled] == ["Engram Local Daemon"]
    assert scheduler.named("/Change") == ["Engram Local Daemon"]
    assert "/DISABLE" in scheduler.calls[0]
    assert "/Delete" not in scheduler.verbs()


def _ladder(
    monkeypatch: pytest.MonkeyPatch,
    answers: list[DatabaseOwnership],
) -> None:
    """Answer every ownership probe from a script, with no waiting."""
    monkeypatch.setattr(
        autostart_module,
        "database_ownership",
        lambda _config: (
            answers.pop(0) if answers else DatabaseOwnership(locked=False, role=None, pid=None)
        ),
    )
    monkeypatch.setattr(autostart_module, "LOCK_POLL_INTERVAL_SECONDS", 0.0)
    for name in (
        "GRACEFUL_STOP_TIMEOUT_SECONDS",
        "TASK_END_TIMEOUT_SECONDS",
        "FORCED_STOP_TIMEOUT_SECONDS",
        "LOCK_RELEASE_TIMEOUT_SECONDS",
    ):
        monkeypatch.setattr(autostart_module, name, 0.0)


def test_replace_asks_before_it_ends_or_terminates_anything(
    app_config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A daemon that honours the sentinel must never be ended or terminated."""
    scheduler = RecordingScheduler(present=True)
    monkeypatch.setattr(autostart_module, "_run_scheduler", scheduler)
    terminated: list[int] = []
    monkeypatch.setattr(autostart_module, "_terminate_tree", terminated.append)
    _ladder(
        monkeypatch,
        [
            DatabaseOwnership(locked=True, role=DatabaseLockRole.DAEMON, pid=1234),
            DatabaseOwnership(locked=False, role=None, pid=None),
        ],
    )

    disabled = resolve_conflicts(
        (_conflict(database=app_config.database.path),),
        config=app_config,
        replace=True,
        force=False,
        logger=LOGGER,
    )

    assert disabled[0].stop_method is StopMethod.SENTINEL
    assert disabled[0].stopped_pid == 1234
    assert disabled[0].ended
    assert "/End" not in scheduler.verbs()
    assert terminated == []
    assert not stop_requested(app_config.database.path)


def test_replace_ends_the_task_when_the_daemon_ignores_the_sentinel(
    app_config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scheduler = RecordingScheduler(present=True)
    monkeypatch.setattr(autostart_module, "_run_scheduler", scheduler)
    terminated: list[int] = []
    monkeypatch.setattr(autostart_module, "_terminate_tree", terminated.append)
    held = DatabaseOwnership(locked=True, role=DatabaseLockRole.DAEMON, pid=1234)
    _ladder(monkeypatch, [held, held, DatabaseOwnership(locked=False, role=None, pid=None)])

    disabled = resolve_conflicts(
        (_conflict(database=app_config.database.path),),
        config=app_config,
        replace=True,
        force=False,
        logger=LOGGER,
    )

    assert disabled[0].stop_method is StopMethod.TASK_END
    assert scheduler.named("/End") == ["Engram Local Daemon"]
    assert terminated == []


def test_replace_terminates_the_tree_the_scheduler_left_behind(
    app_config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The production failure: the task ends, its descendants keep the lock."""
    monkeypatch.setattr(autostart_module, "_run_scheduler", RecordingScheduler(present=True))
    terminated: list[int] = []
    monkeypatch.setattr(autostart_module, "_terminate_tree", terminated.append)
    held = DatabaseOwnership(locked=True, role=DatabaseLockRole.DAEMON, pid=82820)
    _ladder(
        monkeypatch,
        [held, held, held, held, DatabaseOwnership(locked=False, role=None, pid=None)],
    )

    disabled = resolve_conflicts(
        (_conflict(database=app_config.database.path),),
        config=app_config,
        replace=True,
        force=False,
        logger=LOGGER,
    )

    assert disabled[0].stop_method is StopMethod.FORCED
    assert terminated == [82820]


def test_replace_leaves_no_stop_request_behind_when_it_gives_up(
    app_config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A sentinel surviving a failed takeover would kill the next daemon at logon."""
    monkeypatch.setattr(autostart_module, "_run_scheduler", RecordingScheduler(present=True))
    monkeypatch.setattr(autostart_module, "_terminate_tree", lambda _pid: None)
    _ladder(monkeypatch, [])
    monkeypatch.setattr(
        autostart_module,
        "database_ownership",
        lambda _config: DatabaseOwnership(locked=True, role=DatabaseLockRole.DAEMON, pid=99),
    )

    with pytest.raises(AutostartError) as failure:
        resolve_conflicts(
            (_conflict(database=app_config.database.path),),
            config=app_config,
            replace=True,
            force=False,
            logger=LOGGER,
        )

    assert "99" in str(failure.value)
    assert not stop_requested(app_config.database.path)


def test_install_refuses_while_a_competing_task_is_enabled(
    runtime_config: AppConfig,
    windows_runtime: Path,
    config_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        autostart_module,
        "_run_scheduler",
        RecordingScheduler(
            present=False,
            dump=task_dump(
                competing_engram_task(
                    "Engram Local Daemon",
                    windows_runtime / "pythonw.exe",
                    config_file,
                )
            ),
        ),
    )

    with pytest.raises(AutostartConflictError):
        _setup_autostart(
            config=runtime_config,
            config_path=config_file,
            logger=LOGGER,
            arguments=autostart_arguments(install=True),
        )


def test_install_replace_reports_the_task_it_disabled(
    runtime_config: AppConfig,
    windows_runtime: Path,
    config_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    scheduler = RecordingScheduler(
        present=False,
        dump=task_dump(
            competing_engram_task(
                "Engram Local Daemon",
                windows_runtime / "pythonw.exe",
                config_file,
            )
        ),
    )
    monkeypatch.setattr(autostart_module, "_run_scheduler", scheduler)

    _setup_autostart(
        config=runtime_config,
        config_path=config_file,
        logger=LOGGER,
        arguments=autostart_arguments(install=True, replace=True),
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["disabled"] == [
        {
            "ended": False,
            "stop_method": None,
            "stopped_pid": None,
            "task_name": "Engram Local Daemon",
        }
    ]
    assert payload["created"] is True
    assert scheduler.named("/Change") == ["Engram Local Daemon"]
    assert "/Delete" not in scheduler.verbs()


def test_status_lists_the_competing_tasks(
    runtime_config: AppConfig,
    windows_runtime: Path,
    config_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        autostart_module,
        "_run_scheduler",
        RecordingScheduler(
            present=True,
            dump=task_dump(
                competing_engram_task(
                    "Engram Local Daemon",
                    windows_runtime / "pythonw.exe",
                    config_file,
                )
            ),
        ),
    )

    _setup_autostart(
        config=runtime_config,
        config_path=config_file,
        logger=LOGGER,
        arguments=autostart_arguments(),
    )

    payload = json.loads(capsys.readouterr().out)
    assert [conflict["task_name"] for conflict in payload["conflicts"]] == ["Engram Local Daemon"]
    assert payload["conflicts"][0]["resolved"] is True
    # The contract L5 established must not move underneath its consumers.
    assert {"installed", "daemon_running", "database_owner_pid"} <= set(payload)


# --- Stop request and graceful shutdown ---------------------------------------


def test_stop_request_is_a_sibling_of_the_ownership_lock(tmp_path: Path) -> None:
    database = tmp_path / "engram.db"

    assert stop_request_path(database) == database_lock_path(database).with_suffix(".stop")
    assert stop_request_path(database).parent == database_lock_path(database).parent


def test_stop_request_round_trip(tmp_path: Path) -> None:
    database = tmp_path / "engram.db"

    assert not stop_requested(database)
    request_stop(database)
    assert stop_requested(database)
    assert clear_stop_request(database)
    assert not stop_requested(database)
    assert not clear_stop_request(database)


def test_serve_clears_a_stop_request_once_it_owns_the_database(
    app_config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rule A: a sentinel surviving an earlier run must not stop the next daemon."""
    monkeypatch.setattr(cli_module, "_ensure_server_bind_available", lambda _host, _port: None)
    monkeypatch.setattr(cli_module, "_run_server", lambda **_keywords: None)
    request_stop(app_config.database.path)

    cli_module._serve(config=app_config, logger=LOGGER)  # noqa: SLF001

    assert not stop_requested(app_config.database.path)


def test_serve_leaves_a_stop_request_it_does_not_own(
    app_config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rule B: clearing before the lock would cancel a stop meant for the owner."""
    monkeypatch.setattr(cli_module, "_ensure_server_bind_available", lambda _host, _port: None)
    monkeypatch.setattr(cli_module, "_run_server", lambda **_keywords: None)
    request_stop(app_config.database.path)

    with (
        DatabaseProcessLock(
            app_config.database.path,
            role=DatabaseLockRole.OFFLINE_WRITER,
            command="migrate",
        ),
        pytest.raises(DatabaseLockError),
    ):
        cli_module._serve(config=app_config, logger=LOGGER)  # noqa: SLF001

    assert stop_requested(app_config.database.path)
    clear_stop_request(app_config.database.path)


def test_stop_watcher_interrupts_the_main_thread_when_asked(
    app_config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    interrupts: list[bool] = []
    monkeypatch.setattr(_thread, "interrupt_main", lambda: interrupts.append(True))
    monkeypatch.setattr(cli_module, "STOP_WATCH_INTERVAL_SECONDS", 0.01)
    watcher = cli_module._StopWatcher(app_config.database.path, logger=LOGGER)  # noqa: SLF001
    watcher.start()
    try:
        request_stop(app_config.database.path)
        deadline = time.monotonic() + 5.0
        while not watcher.requested and time.monotonic() < deadline:
            time.sleep(0.01)
    finally:
        watcher.stop()
        clear_stop_request(app_config.database.path)

    assert watcher.requested
    assert interrupts == [True]


def test_stop_watcher_leaves_an_unasked_server_alone(
    app_config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    interrupts: list[bool] = []
    monkeypatch.setattr(_thread, "interrupt_main", lambda: interrupts.append(True))
    monkeypatch.setattr(cli_module, "STOP_WATCH_INTERVAL_SECONDS", 0.01)
    watcher = cli_module._StopWatcher(app_config.database.path, logger=LOGGER)  # noqa: SLF001
    watcher.start()
    time.sleep(0.1)
    watcher.stop()

    assert not watcher.requested
    assert interrupts == []


# --- interpreter_present -------------------------------------------------------


def test_status_reports_a_registered_interpreter_that_no_longer_exists(
    runtime_config: AppConfig,
    windows_runtime: Path,
    config_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A task whose interpreter was deleted still exists, and can no longer start."""
    del windows_runtime
    plan = build_plan(config_file)
    vanished = config_file.parent / "removed" / "pythonw.exe"
    monkeypatch.setattr(
        autostart_module,
        "_run_scheduler",
        RecordingScheduler(
            present=True,
            dump=task_dump(scheduled_task(plan.task_name, command=str(vanished))),
        ),
    )

    _setup_autostart(
        config=runtime_config,
        config_path=config_file,
        logger=LOGGER,
        arguments=autostart_arguments(),
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["installed"] is True
    assert payload["interpreter_present"] is False
    assert payload["registered_command"] == str(vanished)


def test_status_reports_a_registered_interpreter_that_is_there(
    runtime_config: AppConfig,
    windows_runtime: Path,
    config_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    plan = build_plan(config_file)
    monkeypatch.setattr(
        autostart_module,
        "_run_scheduler",
        RecordingScheduler(
            present=True,
            dump=task_dump(
                scheduled_task(plan.task_name, command=str(windows_runtime / "pythonw.exe"))
            ),
        ),
    )

    _setup_autostart(
        config=runtime_config,
        config_path=config_file,
        logger=LOGGER,
        arguments=autostart_arguments(),
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["installed"] is True
    assert payload["interpreter_present"] is True
