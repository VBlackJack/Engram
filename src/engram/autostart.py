# Copyright 2026 Julien Bombled
# SPDX-License-Identifier: Apache-2.0

"""Windows logon registration that starts the daemon without a console window.

Engram is documented as a foreground process whose terminal must stay open. A
window closed by accident stops the broker for every connected client at once,
without a signal and without a restart, which makes any measurement taken over
several days unreliable rather than merely inconvenient.

This module registers the daemon as a logon task driven by the windowed
interpreter that ships beside the running one. The absence of a window is a
property of that interpreter rather than of a flag asking a console to hide
itself, so it can be proven from the outside: the started process owns no main
window handle.

Nothing here is discovered at import time. The interpreter, the configuration
file, the working directory, and the endpoint all come from the running process
or from the loaded configuration, so a second installation under a different
configuration registers a second, separately named task instead of silently
replacing the first.
"""

from __future__ import annotations

import hashlib
import itertools
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING
from xml.etree import ElementTree as ET

from .config import DEFAULT_CONFIG_PATH, ConfigError, load_config
from .process_lock import (
    DatabaseLockError,
    DatabaseLockRole,
    DatabaseProcessLock,
    database_lock_path,
)

if TYPE_CHECKING:  # pragma: no cover - imported for typing only
    import logging
    from collections.abc import Mapping

    from .config import AppConfig

TASK_NAMESPACE = "http://schemas.microsoft.com/windows/2004/02/mit/task"
TASK_SCHEMA_VERSION = "1.2"
TASK_NAME_PREFIX = "Engram Autostart"
TASK_DESCRIPTION = "Start the local Engram MCP memory broker at logon, without a console window."
TASK_IDENTITY_DIGEST_CHARS = 8
# The interpreter is chosen for the subsystem it is linked against, not for a
# version: this name is the windowed build of whichever interpreter is running.
WINDOWED_INTERPRETER_NAME = "pythonw.exe"
SCHEDULER_COMMAND = "schtasks"
SERVE_COMMAND = "serve"
CONFIG_OPTION = "--config"
MODULE_OPTION = "-m"
PACKAGE_NAME = "engram"
RESTART_COUNT = "3"
RESTART_INTERVAL = "PT1M"
UNLIMITED_EXECUTION_TIME = "PT0S"
SCHEDULER_SUCCESS = 0
WINDOWS_PLATFORM = "win32"
USER_DOMAIN_KEY = "USERDOMAIN"
USER_NAME_KEY = "USERNAME"
# Measured on the scheduler shipped with this host: one <Tasks> root, no XML
# declaration, every task preceded by a comment holding its full path, encoded
# as UTF-8 rather than in the console code page.
SCHEDULER_DUMP_ENCODING = "utf-8"
LOCK_RELEASE_TIMEOUT_SECONDS = 30.0
LOCK_POLL_INTERVAL_SECONDS = 0.25
# The stop request is a sibling of the ownership lock, in the directory that
# holds the database. Asking a daemon to stop then costs exactly the right to
# write beside its database, which is already the right to corrupt it.
STOP_REQUEST_SUFFIX = ".stop"
# A refusal has to stay readable on a terminal. The complete list goes to the
# log, where it can be as long as the machine's task inventory requires.
REPORTED_CONFLICT_LIMIT = 5
GRACEFUL_STOP_TIMEOUT_SECONDS = 10.0
TASK_END_TIMEOUT_SECONDS = 10.0
FORCED_STOP_TIMEOUT_SECONDS = 5.0
TERMINATE_COMMAND = "taskkill"


class AutostartError(RuntimeError):
    """Raised when the logon task cannot be registered, queried, or removed."""


class AutostartUnsupportedError(AutostartError):
    """Raised when the running operating system has no logon task scheduler."""


class AutostartConflictError(AutostartError):
    """Raised when another registered task would open the same database."""


@dataclass(frozen=True, slots=True)
class ScheduledAction:
    """One registered task and the process it starts, as the scheduler stores it."""

    task_name: str
    enabled: bool
    command: str
    arguments: str
    working_directory: str

    def tokens(self) -> tuple[str, ...]:
        """Return the command and its arguments as separate, unquoted values."""
        try:
            split = shlex.split(self.arguments, posix=False)
        except ValueError:
            # An argument line the scheduler accepted but no quoting rule
            # explains. Whatever it starts cannot be resolved from it.
            split = self.arguments.split()
        return tuple(value.strip('"') for value in (self.command, *split) if value.strip('"'))


@dataclass(frozen=True, slots=True)
class TaskConflict:
    """One registered task that reaches, or may reach, the same database."""

    task_name: str
    enabled: bool
    database: Path | None
    reason: str

    @property
    def resolved(self) -> bool:
        """Return whether the database this task opens could be determined."""
        return self.database is not None


class StopMethod(StrEnum):
    """How far the escalation had to go before the database was released."""

    SENTINEL = "sentinel"
    TASK_END = "task_end"
    FORCED = "forced"


@dataclass(frozen=True, slots=True)
class DisabledTask:
    """One task this command took out of the way, reversibly."""

    task_name: str
    ended: bool
    stopped_pid: int | None
    stop_method: StopMethod | None


def stop_request_path(database_path: Path) -> Path:
    """Derive the stop request beside the ownership lock of one database."""
    return database_lock_path(database_path).with_suffix(STOP_REQUEST_SUFFIX)


def request_stop(database_path: Path) -> Path:
    """Ask whichever daemon owns this database to close it and exit."""
    path = stop_request_path(database_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()
    return path


def stop_requested(database_path: Path) -> bool:
    """Return whether a stop has been requested for this database."""
    return stop_request_path(database_path).is_file()


def clear_stop_request(database_path: Path) -> bool:
    """Remove one stop request and report whether there was one to remove.

    Callers must already own the database. Clearing a request without the lock
    would let a process that is about to fail on the lock cancel a stop meant
    for the daemon that holds it.
    """
    path = stop_request_path(database_path)
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    return True


@dataclass(frozen=True, slots=True)
class AutostartPlan:
    """The exact process the logon task will start, resolved at run time."""

    task_name: str
    command: Path
    arguments: tuple[str, ...]
    working_directory: Path
    config_path: Path

    @property
    def argument_line(self) -> str:
        """Return the arguments as the single command line the scheduler stores.

        The scheduler keeps one string, not a list, so the quoting rule that
        turns the list back into these arguments has to be the Windows one.
        """
        return subprocess.list2cmdline(self.arguments)


@dataclass(frozen=True, slots=True)
class DatabaseOwnership:
    """What currently holds the configured database, from the ownership lock."""

    locked: bool
    role: DatabaseLockRole | None
    pid: int | None

    @property
    def daemon_running(self) -> bool:
        """Return whether the current owner is a serving daemon."""
        return self.role is DatabaseLockRole.DAEMON


@dataclass(frozen=True, slots=True)
class InstallOutcome:
    """What one idempotent installation changed."""

    created: bool
    started: bool
    start_skipped_reason: str | None


def build_plan(config_path: Path) -> AutostartPlan:
    """Resolve the command the logon task must run for this configuration."""
    require_supported_platform()
    resolved_config = config_path.expanduser().resolve()
    return AutostartPlan(
        task_name=task_name(resolved_config),
        command=windowed_interpreter(Path(sys.executable)),
        arguments=(
            MODULE_OPTION,
            PACKAGE_NAME,
            CONFIG_OPTION,
            str(resolved_config),
            SERVE_COMMAND,
        ),
        working_directory=resolved_config.parent,
        config_path=resolved_config,
    )


def require_supported_platform() -> None:
    """Fail loudly where no logon task scheduler exists, instead of doing nothing."""
    if sys.platform != WINDOWS_PLATFORM:
        raise AutostartUnsupportedError(
            f"engram setup autostart requires Windows; this host reports {sys.platform}. "
            "On another operating system, supervise 'engram serve' with the local service "
            "manager instead."
        )


def task_name(config_path: Path) -> str:
    """Derive one stable task name per configuration file.

    Two Engram installations on one account own different databases. A single
    shared name would let the second installation silently take over the first
    one's task, so the configuration path that decides the database also decides
    the identity of the task that serves it.
    """
    digest = hashlib.sha256(str(config_path).casefold().encode("utf-8")).hexdigest()
    return f"{TASK_NAME_PREFIX} {digest[:TASK_IDENTITY_DIGEST_CHARS]}"


def windowed_interpreter(executable: Path) -> Path:
    """Return the windowed build beside the running interpreter, or fail.

    Falling back to the console interpreter would register a task that works and
    opens a window, which is the outcome this command exists to remove. A missing
    windowed build is reported so the operator can choose a different runtime.
    """
    candidate = executable.expanduser().resolve().with_name(WINDOWED_INTERPRETER_NAME)
    if not candidate.is_file():
        raise AutostartError(
            f"No windowed interpreter beside {executable}: expected {candidate}. "
            "Install Engram on a runtime that ships this interpreter, because the console "
            "interpreter would open the window this command removes."
        )
    return candidate


def database_ownership(config: AppConfig) -> DatabaseOwnership:
    """Report what holds the configured database, through the ownership lock.

    The lock is the same one every writer takes, so this answer cannot disagree
    with the daemon's own view of whether it is running. It is acquired and
    released immediately, which keeps this a probe rather than a second writer.
    """
    probe = DatabaseProcessLock(
        config.database.path,
        role=DatabaseLockRole.OFFLINE_WRITER,
        command="setup",
    )
    try:
        probe.acquire()
    except DatabaseLockError as exc:
        owner = exc.owner
        return DatabaseOwnership(
            locked=True,
            role=None if owner is None else owner.role,
            pid=None if owner is None else owner.pid,
        )
    probe.release()
    return DatabaseOwnership(locked=False, role=None, pid=None)


def canonical_path(value: str, *, base: Path | None = None) -> Path | None:
    """Return one comparable form of a path the scheduler stored as free text.

    Windows reaches the same file through different spellings: a different case,
    a home shortcut, a junction, an 8.3 short name, an environment variable left
    unexpanded. Resolution collapses all of them for a path that exists, which is
    the only case that matters here: a task pointing at a file that is not there
    cannot be holding its lock.

    A token the scheduler stored relative belongs to the working directory of its
    own task, never to wherever Engram happens to be invoked from. Resolving it
    against the current directory places unrelated tasks inside the configuration
    directory and reports them as conflicts, which is how every maintenance task
    on the machine came to look like a competing Engram. Such a token is refused
    unless the caller supplies the base it is relative to.
    """
    cleaned = os.path.expandvars(value.strip().strip('"').strip())
    if not cleaned:
        return None
    try:
        candidate = Path(cleaned).expanduser()
        if not candidate.is_absolute():
            if base is None or not base.is_absolute():
                return None
            candidate = base / candidate
        return candidate.resolve()
    except (OSError, ValueError, RuntimeError):
        return None


def same_path(left: Path, right: Path) -> bool:
    """Compare two canonical paths the way the file system does."""
    return str(left).casefold() == str(right).casefold()


def registered_actions() -> tuple[ScheduledAction, ...]:
    """Return every registered task and the process it starts."""
    require_supported_platform()
    completed = _run_scheduler(("/Query", "/XML", "ONE"))
    _require_scheduler_success(completed, action="list the registered tasks")
    return _parse_task_dump(completed.stdout.decode(SCHEDULER_DUMP_ENCODING, errors="replace"))


def _parse_task_dump(document: str) -> tuple[ScheduledAction, ...]:
    """Read the scheduler's own dump of every task it holds.

    The dump names each task in a comment rather than in an element, and not
    every task carries a registration URI, so the comments are read as data
    instead of being discarded by the parser.
    """
    # The document is this host's own scheduler describing its own tasks, over a
    # pipe, not input from anywhere else. Reaching a hardened parser would mean
    # a new dependency, which this lot is not allowed to add.
    parser = ET.XMLParser(target=ET.TreeBuilder(insert_comments=True))  # noqa: S314
    parser.feed(document)
    root = parser.close()
    actions: list[ScheduledAction] = []
    pending_name: str | None = None
    for child in root:
        if not isinstance(child.tag, str):
            pending_name = "" if child.text is None else child.text.strip().lstrip("\\")
            continue
        execution = child.find("./{*}Actions/{*}Exec")
        command = None if execution is None else execution.findtext("./{*}Command")
        if execution is None or command is None or not pending_name:
            pending_name = None
            continue
        enabled = child.findtext("./{*}Settings/{*}Enabled")
        actions.append(
            ScheduledAction(
                task_name=pending_name,
                enabled=enabled is None or enabled.strip().casefold() != "false",
                command=command,
                arguments=execution.findtext("./{*}Arguments") or "",
                working_directory=execution.findtext("./{*}WorkingDirectory") or "",
            )
        )
        pending_name = None
    return tuple(actions)


def registered_command(plan: AutostartPlan) -> Path | None:
    """Return the executable the scheduler holds for this task, if it holds one.

    The plan is recomputed from the running interpreter, so it can never be
    missing. What can go missing is the interpreter written into the task at
    install time, which is a different path and the one that runs at logon.
    """
    require_supported_platform()
    for action in registered_actions():
        if action.task_name.casefold() == plan.task_name.casefold():
            return canonical_path(action.command)
    return None


def conflicting_tasks(plan: AutostartPlan, *, database: Path) -> tuple[TaskConflict, ...]:
    """Return every enabled task that reaches, or may reach, this database.

    The test is the database, never the name of a task. A literal name would be
    the hardcoding this project refuses everywhere else, and it would miss any
    installation that chose a different one. What creates the collision is the
    exclusive lock over one SQLite file, so that file is what is compared.
    """
    target = database.expanduser().resolve()
    scope = plan.config_path.parent
    conflicts: list[TaskConflict] = []
    for action in registered_actions():
        if action.task_name.casefold() == plan.task_name.casefold() or not action.enabled:
            continue
        opened = _action_database(action)
        if opened is not None:
            if same_path(opened, target):
                conflicts.append(
                    TaskConflict(
                        task_name=action.task_name,
                        enabled=action.enabled,
                        database=opened,
                        reason="starts Engram on the same database",
                    )
                )
            continue
        if _action_reaches_into(action, scope):
            conflicts.append(
                TaskConflict(
                    task_name=action.task_name,
                    enabled=action.enabled,
                    database=None,
                    reason=(
                        f"runs {action.command} against a file inside {scope}, and does not "
                        "name the configuration it loads, so the database it opens is "
                        "undetermined"
                    ),
                )
            )
    return tuple(conflicts)


def _action_database(action: ScheduledAction) -> Path | None:
    """Return the database this task opens, or None when it cannot be read from it."""
    tokens = action.tokens()
    if not _starts_this_package(tokens):
        return None
    configured = _configured_path(tokens, _action_directory(action)) or _default_config_path(action)
    if configured is None or not configured.is_file():
        return None
    try:
        return load_config(configured).database.path.expanduser().resolve()
    except (ConfigError, OSError):
        return None


def _starts_this_package(tokens: tuple[str, ...]) -> bool:
    """Return whether these arguments start this distribution, not something else."""
    if not tokens:
        return False
    if Path(tokens[0]).stem.casefold() == PACKAGE_NAME:
        return True
    return any(
        left == MODULE_OPTION and right.casefold() == PACKAGE_NAME
        for left, right in itertools.pairwise(tokens)
    )


def _action_directory(action: ScheduledAction) -> Path | None:
    """Return the directory this task starts in, when the scheduler records one."""
    return canonical_path(action.working_directory)


def _configured_path(tokens: tuple[str, ...], base: Path | None) -> Path | None:
    """Return the configuration named on the command line, when one is."""
    for index, token in enumerate(tokens):
        if token == CONFIG_OPTION and index + 1 < len(tokens):
            return canonical_path(tokens[index + 1], base=base)
        if token.startswith(f"{CONFIG_OPTION}="):
            return canonical_path(token.split("=", 1)[1], base=base)
    return None


def _default_config_path(action: ScheduledAction) -> Path | None:
    """Return the configuration a command with no explicit path would discover."""
    directory = _action_directory(action)
    if directory is None:
        return None
    return directory / DEFAULT_CONFIG_PATH.name


def _action_reaches_into(action: ScheduledAction, directory: Path) -> bool:
    """Return whether this task names any file inside the configuration's directory."""
    base = _action_directory(action)
    for token in action.tokens():
        candidate = canonical_path(token, base=base)
        if candidate is None:
            continue
        if same_path(candidate, directory) or any(
            same_path(parent, directory) for parent in candidate.parents
        ):
            return True
    return False


def resolve_conflicts(
    conflicts: tuple[TaskConflict, ...],
    *,
    config: AppConfig,
    replace: bool,
    force: bool,
    logger: logging.Logger,
) -> tuple[DisabledTask, ...]:
    """Refuse, bypass, or clear the tasks that would fight for this database.

    An undetermined answer is not an absence of conflict. Installing over one
    would produce a task that looks registered and never serves, which is the
    exact failure this command exists to remove.
    """
    if not conflicts:
        return ()
    if replace:
        return _disable_conflicts(conflicts, config=config, logger=logger)
    if force:
        logger.warning(
            "Installing over %d unresolved or conflicting task(s) because --force was given: %s",
            len(conflicts),
            ", ".join(conflict.task_name for conflict in conflicts),
        )
        return ()
    logger.warning(
        "Refusing to install over %d conflicting task(s): %s",
        len(conflicts),
        "; ".join(f"'{conflict.task_name}' {conflict.reason}" for conflict in conflicts),
    )
    named = conflicts[:REPORTED_CONFLICT_LIMIT]
    remainder = len(conflicts) - len(named)
    raise AutostartConflictError(
        "Another registered task would open this database: "
        + "; ".join(f"'{conflict.task_name}' {conflict.reason}" for conflict in named)
        + (f"; and {remainder} more, named in the log" if remainder else "")
        + ". Rerun with --replace to disable it, or with --force to install anyway."
    )


def _disable_conflicts(
    conflicts: tuple[TaskConflict, ...],
    *,
    config: AppConfig,
    logger: logging.Logger,
) -> tuple[DisabledTask, ...]:
    """Take the competing tasks out of the way without destroying them."""
    disabled: list[DisabledTask] = []
    for conflict in conflicts:
        _require_scheduler_success(
            _run_scheduler(("/Change", "/TN", conflict.task_name, "/DISABLE")),
            action=f"disable the competing task {conflict.task_name}",
        )
        ownership = database_ownership(config)
        method = _stop_owner(conflict.task_name, config=config, ownership=ownership, logger=logger)
        logger.info(
            "Disabled competing task %s (stop=%s, database owner pid=%s)",
            conflict.task_name,
            "none" if method is None else method.value,
            ownership.pid,
        )
        disabled.append(
            DisabledTask(
                task_name=conflict.task_name,
                ended=method is not None,
                stopped_pid=ownership.pid if ownership.daemon_running else None,
                stop_method=method,
            )
        )
    _wait_for_release(config, logger=logger)
    return tuple(disabled)


def _stop_owner(
    task_name: str,
    *,
    config: AppConfig,
    ownership: DatabaseOwnership,
    logger: logging.Logger,
) -> StopMethod | None:
    """Escalate from asking to ending to terminating, verifying each rung.

    Ending a task only terminates the process the scheduler started. When that
    process is a wrapper script, the daemon it launched survives, keeps the lock
    and keeps serving, while the scheduler reports the task stopped. Every rung
    is therefore checked against the ownership lock rather than believed.
    """
    if not ownership.locked:
        return None
    try:
        request_stop(config.database.path)
        logger.info("Stop requested for %s through the sentinel", task_name)
        if _released_within(config, GRACEFUL_STOP_TIMEOUT_SECONDS):
            return StopMethod.SENTINEL
        ended = _run_scheduler(("/End", "/TN", task_name)).returncode == SCHEDULER_SUCCESS
        if ended and _released_within(config, TASK_END_TIMEOUT_SECONDS):
            return StopMethod.TASK_END
        surviving = database_ownership(config)
        if not surviving.locked:
            return StopMethod.TASK_END
        if surviving.pid is not None:
            logger.warning(
                "Task %s reported stopped while pid %d still owns the database; "
                "terminating the surviving tree",
                task_name,
                surviving.pid,
            )
            _terminate_tree(surviving.pid)
            if _released_within(config, FORCED_STOP_TIMEOUT_SECONDS):
                return StopMethod.FORCED
    finally:
        # Never leave a request behind. The next daemon clears one it finds, but
        # a stale sentinel is exactly the failure this command exists to remove.
        clear_stop_request(config.database.path)
    return None


def _terminate_tree(pid: int) -> None:
    """Terminate one process and its descendants, as a last resort."""
    executable = shutil.which(TERMINATE_COMMAND)
    if executable is None:
        raise AutostartError(
            f"The {TERMINATE_COMMAND} command is not available on PATH, so the surviving "
            f"process {pid} cannot be stopped. Stop it manually, then rerun."
        )
    subprocess.run(  # noqa: S603 - resolved executable, list argv, no shell
        [executable, "/PID", str(pid), "/T", "/F"],
        capture_output=True,
        check=False,
    )


def _released_within(config: AppConfig, budget_seconds: float) -> bool:
    """Poll the ownership lock for one budget and report whether it came free."""
    deadline = time.monotonic() + budget_seconds
    while True:
        if not database_ownership(config).locked:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(LOCK_POLL_INTERVAL_SECONDS)


def _wait_for_release(config: AppConfig, *, logger: logging.Logger) -> None:
    """Wait for the ownership lock itself, never for a fixed delay."""
    if _released_within(config, LOCK_RELEASE_TIMEOUT_SECONDS):
        logger.info("Database released by the competing task")
        return
    raise AutostartError(
        f"The database is still held by {_describe_owner(database_ownership(config))} after "
        f"{LOCK_RELEASE_TIMEOUT_SECONDS:.0f} seconds. Stop that process, then rerun."
    )


def is_installed(plan: AutostartPlan) -> bool:
    """Return whether the scheduler already knows this task."""
    require_supported_platform()
    completed = _run_scheduler(("/Query", "/TN", plan.task_name))
    return completed.returncode == SCHEDULER_SUCCESS


def install(
    plan: AutostartPlan,
    *,
    ownership: DatabaseOwnership,
    logger: logging.Logger,
) -> InstallOutcome:
    """Register or update the logon task, then start it when the database is free.

    Registration replaces a task of the same name rather than adding a second
    one, so repeating this command converges instead of accumulating. The daemon
    is only started when nothing else owns the database, because a second serving
    process would fail on the lock and leave a task that looks installed and
    never runs.
    """
    require_supported_platform()
    already_present = is_installed(plan)
    definition = _task_definition(plan)
    definition_path = _write_definition(definition)
    try:
        _require_scheduler_success(
            _run_scheduler(("/Create", "/TN", plan.task_name, "/XML", str(definition_path), "/F")),
            action=f"register the logon task {plan.task_name}",
        )
    finally:
        definition_path.unlink(missing_ok=True)
    logger.info(
        "Autostart task %s: command=%s arguments=%s working_directory=%s",
        "updated" if already_present else "registered",
        plan.command,
        plan.argument_line,
        plan.working_directory,
    )
    if ownership.locked:
        reason = (
            f"the database is already held by {_describe_owner(ownership)}; "
            "the task will start at the next logon"
        )
        logger.info("Autostart task %s not started: %s", plan.task_name, reason)
        return InstallOutcome(
            created=not already_present,
            started=False,
            start_skipped_reason=reason,
        )
    _require_scheduler_success(
        _run_scheduler(("/Run", "/TN", plan.task_name)),
        action=f"start the logon task {plan.task_name}",
    )
    logger.info("Autostart task %s started", plan.task_name)
    return InstallOutcome(created=not already_present, started=True, start_skipped_reason=None)


def uninstall(plan: AutostartPlan, *, logger: logging.Logger) -> bool:
    """Remove the logon task and report whether it was there to remove."""
    require_supported_platform()
    if not is_installed(plan):
        logger.info("Autostart task %s was already absent", plan.task_name)
        return False
    _require_scheduler_success(
        _run_scheduler(("/Delete", "/TN", plan.task_name, "/F")),
        action=f"remove the logon task {plan.task_name}",
    )
    logger.info("Autostart task %s removed", plan.task_name)
    return True


def _describe_owner(ownership: DatabaseOwnership) -> str:
    """Describe the current database owner for one operator-facing message."""
    role = "an unidentified process" if ownership.role is None else f"an Engram {ownership.role}"
    return role if ownership.pid is None else f"{role} (pid {ownership.pid})"


def _task_definition(plan: AutostartPlan) -> str:
    """Build the scheduler document describing this logon task."""
    root = ET.Element(
        "Task",
        {"version": TASK_SCHEMA_VERSION, "xmlns": TASK_NAMESPACE},
    )
    registration = ET.SubElement(root, "RegistrationInfo")
    ET.SubElement(registration, "Description").text = TASK_DESCRIPTION
    ET.SubElement(registration, "URI").text = f"\\{plan.task_name}"

    account = _current_account()
    triggers = ET.SubElement(root, "Triggers")
    logon = ET.SubElement(triggers, "LogonTrigger")
    ET.SubElement(logon, "Enabled").text = "true"
    ET.SubElement(logon, "UserId").text = account

    principals = ET.SubElement(root, "Principals")
    principal = ET.SubElement(principals, "Principal", {"id": "Author"})
    ET.SubElement(principal, "UserId").text = account
    # The token of the logged-on account, so no password is stored and no
    # interactive console is inherited.
    ET.SubElement(principal, "LogonType").text = "InteractiveToken"
    ET.SubElement(principal, "RunLevel").text = "LeastPrivilege"

    _append_settings(root)

    actions = ET.SubElement(root, "Actions", {"Context": "Author"})
    execute = ET.SubElement(actions, "Exec")
    ET.SubElement(execute, "Command").text = str(plan.command)
    ET.SubElement(execute, "Arguments").text = plan.argument_line
    ET.SubElement(execute, "WorkingDirectory").text = str(plan.working_directory)

    body = ET.tostring(root, encoding="unicode")
    return f'<?xml version="1.0" encoding="UTF-16"?>\n{body}'


def _append_settings(root: ET.Element) -> None:
    """Describe a broker that runs for as long as the session lasts."""
    settings = ET.SubElement(root, "Settings")
    ET.SubElement(settings, "Enabled").text = "true"
    ET.SubElement(settings, "AllowStartOnDemand").text = "true"
    ET.SubElement(settings, "DisallowStartIfOnBatteries").text = "false"
    ET.SubElement(settings, "StopIfGoingOnBatteries").text = "false"
    ET.SubElement(settings, "RunOnlyIfIdle").text = "false"
    ET.SubElement(settings, "StartWhenAvailable").text = "false"
    ET.SubElement(settings, "RunOnlyIfNetworkAvailable").text = "false"
    # A single-writer broker: a second instance would only fail on the lock.
    ET.SubElement(settings, "MultipleInstancesPolicy").text = "IgnoreNew"
    # Zero means no limit. The default would stop the broker after three days.
    ET.SubElement(settings, "ExecutionTimeLimit").text = UNLIMITED_EXECUTION_TIME
    restart = ET.SubElement(settings, "RestartOnFailure")
    ET.SubElement(restart, "Interval").text = RESTART_INTERVAL
    ET.SubElement(restart, "Count").text = RESTART_COUNT


def _current_account(environ: Mapping[str, str] | None = None) -> str:
    """Return the account the task runs as, so no password is ever stored."""
    environment = os.environ if environ is None else environ
    domain = environment.get(USER_DOMAIN_KEY, "").strip()
    user = environment.get(USER_NAME_KEY, "").strip()
    if not user:
        raise AutostartError(
            f"Cannot identify the logon account: {USER_NAME_KEY} is not set. "
            "A logon task must name the account it starts for."
        )
    return f"{domain}\\{user}" if domain else user


def _write_definition(definition: str) -> Path:
    """Write the scheduler document where the scheduler expects to read it."""
    descriptor, name = tempfile.mkstemp(prefix="engram-autostart-", suffix=".xml")
    path = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(definition.encode("utf-16"))
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return path


def _run_scheduler(arguments: tuple[str, ...]) -> subprocess.CompletedProcess[bytes]:
    """Run one scheduler command with a fully resolved executable and argv."""
    executable = shutil.which(SCHEDULER_COMMAND)
    if executable is None:
        raise AutostartError(
            f"The {SCHEDULER_COMMAND} command is not available on PATH, so no logon task "
            "can be registered on this host."
        )
    return subprocess.run(  # noqa: S603 - resolved executable, list argv, no shell
        [executable, *arguments],
        capture_output=True,
        check=False,
    )


def _require_scheduler_success(
    completed: subprocess.CompletedProcess[bytes],
    *,
    action: str,
) -> None:
    """Turn a scheduler failure into a diagnostic instead of a silent no-op."""
    if completed.returncode == SCHEDULER_SUCCESS:
        return
    raise AutostartError(
        f"The task scheduler refused to {action} (exit {completed.returncode}): "
        f"{_scheduler_text(completed.stderr) or _scheduler_text(completed.stdout)}"
    )


def _scheduler_text(raw: bytes) -> str:
    """Decode scheduler output without depending on the console code page."""
    return raw.decode("utf-8", errors="replace").strip()
