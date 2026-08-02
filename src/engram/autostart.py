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
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from xml.etree import ElementTree as ET

from .process_lock import (
    DatabaseLockError,
    DatabaseLockRole,
    DatabaseProcessLock,
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


class AutostartError(RuntimeError):
    """Raised when the logon task cannot be registered, queried, or removed."""


class AutostartUnsupportedError(AutostartError):
    """Raised when the running operating system has no logon task scheduler."""


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
