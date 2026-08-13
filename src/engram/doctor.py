# Copyright 2026 Julien Bombled
# SPDX-License-Identifier: Apache-2.0

"""One command that answers why Engram is not working.

Every failure this project has shipped shares a shape: a state that looks
applied and is not. A configuration key spelled wrong loads on defaults, a
database is opened somewhere other than where it was meant to be, a daemon holds
a lock nobody can see, a client points at a port nothing listens on. None of
these announce themselves, and the person meeting them first is rarely the
person who can read the source.

So this module never raises. It measures each fact separately, reports what it
found beside what it expected, and names the command that repairs it. A check
that cannot be run says so instead of stopping the ones after it.
"""

from __future__ import annotations

import socket
import sqlite3
import sys
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

from .autostart import DatabaseOwnership, database_ownership
from .config import ConfigError, load_config
from .db import MINIMUM_SQLITE_VERSION, WINDOWS_SQLITE_GUIDE_URL, latest_schema_version
from .process_lock import DatabaseLockRole, database_lock_path

if TYPE_CHECKING:  # pragma: no cover
    from .config import AppConfig

ENDPOINT_PROBE_TIMEOUT_SECONDS = 2.0


class Outcome(StrEnum):
    """What one check concluded."""

    OK = "ok"
    WARN = "warn"
    FAIL = "fail"


@dataclass(frozen=True, slots=True)
class Check:
    """One measured fact, with the repair for it when it is wrong."""

    name: str
    outcome: Outcome
    detail: str
    remedy: str | None = None


def diagnose(config_path: Path) -> tuple[Check, ...]:
    """Run every check that can be run, in the order a first run meets them."""
    checks = [_check_interpreter(), _check_sqlite()]
    config, configuration = _check_configuration(config_path)
    checks.append(configuration)
    if config is None:
        return tuple(checks)
    checks.append(_check_database(config))
    owner = database_ownership(config)
    checks.append(_check_ownership(config, owner))
    checks.append(_check_endpoint(config, serving=owner.role is DatabaseLockRole.DAEMON))
    checks.append(_check_logging(config))
    return tuple(checks)


def worst_outcome(checks: tuple[Check, ...]) -> Outcome:
    """Return the most severe outcome across every check."""
    if any(check.outcome is Outcome.FAIL for check in checks):
        return Outcome.FAIL
    if any(check.outcome is Outcome.WARN for check in checks):
        return Outcome.WARN
    return Outcome.OK


def _check_interpreter() -> Check:
    version = ".".join(str(part) for part in sys.version_info[:3])
    return Check(
        name="python",
        outcome=Outcome.OK,
        detail=f"{version} at {sys.executable}",
    )


def _check_sqlite() -> Check:
    minimum = ".".join(str(part) for part in MINIMUM_SQLITE_VERSION)
    if sqlite3.sqlite_version_info >= MINIMUM_SQLITE_VERSION:
        return Check(
            name="sqlite",
            outcome=Outcome.OK,
            detail=f"{sqlite3.sqlite_version}, at or above the {minimum} floor",
        )
    return Check(
        name="sqlite",
        outcome=Outcome.FAIL,
        detail=(
            f"{sqlite3.sqlite_version}, below the {minimum} floor this interpreter must clear. "
            "Older runtimes do not contain the WAL-reset bug fix, so Engram refuses to open a "
            "database with them"
        ),
        remedy=(
            "Run Engram on an interpreter whose sqlite3 links a newer library, for example the "
            f"one uv installs with 'uv python install 3.14.6'. See {WINDOWS_SQLITE_GUIDE_URL}"
        ),
    )


def _check_configuration(config_path: Path) -> tuple[AppConfig | None, Check]:
    if not config_path.is_file():
        return None, Check(
            name="configuration",
            outcome=Outcome.FAIL,
            detail=f"no file at {config_path}",
            remedy="Run 'engram init' to write a starting configuration there",
        )
    try:
        config = load_config(config_path)
    except ConfigError as exc:
        return None, Check(
            name="configuration",
            outcome=Outcome.FAIL,
            detail=f"{config_path} does not load: {exc}",
            remedy="Correct the reported key, or run 'engram init --force' to start again",
        )
    return config, Check(
        name="configuration",
        outcome=Outcome.OK,
        detail=f"loaded {config_path}",
    )


def _check_database(config: AppConfig) -> Check:
    path = config.database.path
    if not path.is_file():
        return Check(
            name="database",
            outcome=Outcome.WARN,
            detail=f"no database yet at {path}",
            remedy="Run 'engram migrate' to create it",
        )
    latest = latest_schema_version()
    try:
        version = _read_schema_version(path)
    except sqlite3.Error as exc:
        return Check(
            name="database",
            outcome=Outcome.FAIL,
            detail=f"{path} cannot be read: {exc}",
            remedy="Restore the database from a backup, or run 'engram preflight' to inspect it",
        )
    if version is None:
        return Check(
            name="database",
            outcome=Outcome.FAIL,
            detail=f"{path} exists but records no schema version",
            remedy="Run 'engram preflight' before anything else; it never writes",
        )
    if version < latest:
        return Check(
            name="database",
            outcome=Outcome.WARN,
            detail=f"{path} is at schema {version}, this build expects {latest}",
            remedy="Back up the database, then run 'engram preflight' and 'engram migrate'",
        )
    if version > latest:
        return Check(
            name="database",
            outcome=Outcome.FAIL,
            detail=f"{path} is at schema {version}, newer than the {latest} this build knows",
            remedy="Use the Engram version that wrote this database, or restore an older copy",
        )
    return Check(
        name="database",
        outcome=Outcome.OK,
        detail=f"{path}, schema {version}",
    )


def _check_ownership(config: AppConfig, owner: DatabaseOwnership) -> Check:
    lock = database_lock_path(config.database.path)
    if not owner.locked:
        return Check(
            name="daemon",
            outcome=Outcome.WARN,
            detail=f"no process owns {lock}",
            remedy="Run 'engram serve', or install the logon task with 'engram setup autostart'",
        )
    if owner.role is DatabaseLockRole.DAEMON:
        return Check(
            name="daemon",
            outcome=Outcome.OK,
            detail=f"serving, pid {owner.pid}",
        )
    return Check(
        name="daemon",
        outcome=Outcome.WARN,
        detail=f"an offline writer holds the database, pid {owner.pid}",
        remedy="Wait for it to finish; recall is unavailable while it runs",
    )


def _check_endpoint(config: AppConfig, *, serving: bool) -> Check:
    """Report the endpoint against the lock, because either answer alone misleads.

    A port that accepts is not proof that this Engram is behind it: two
    installations, or any unrelated process, can hold the same loopback port,
    and a client pointed there reaches whatever answers rather than the database
    the operator is looking at.
    """
    endpoint = f"http://{config.server.host}:{config.server.port}{config.server.path}"
    accepting = _endpoint_accepts(config)
    if serving and accepting:
        return Check(name="endpoint", outcome=Outcome.OK, detail=f"{endpoint} accepts")
    if serving:
        return Check(
            name="endpoint",
            outcome=Outcome.FAIL,
            detail=f"a daemon owns this database but {endpoint} refuses connections",
            remedy=(
                "Check [server].host and [server].port against the log; the daemon may have "
                "bound another address, and no client can reach it at this one"
            ),
        )
    if accepting:
        return Check(
            name="endpoint",
            outcome=Outcome.WARN,
            detail=(
                f"{endpoint} accepts, but no daemon owns this database, so another process "
                "is listening there"
            ),
            remedy=(
                "A client pointed at this URL reaches that process, not this database. "
                "Give this installation its own [server].port, or stop the other one"
            ),
        )
    return Check(
        name="endpoint",
        outcome=Outcome.WARN,
        detail=f"{endpoint} refuses connections, and no daemon owns this database",
        remedy="Run 'engram serve', then point the MCP client at exactly this URL",
    )


def _endpoint_accepts(config: AppConfig) -> bool:
    try:
        with socket.create_connection(
            (config.server.host, config.server.port),
            timeout=ENDPOINT_PROBE_TIMEOUT_SECONDS,
        ):
            return True
    except OSError:
        return False


def _check_logging(config: AppConfig) -> Check:
    path = config.logging.path
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8"):
            pass
    except OSError as exc:
        return Check(
            name="logging",
            outcome=Outcome.FAIL,
            detail=f"{path} cannot be written: {exc.strerror or exc}",
            remedy="Point [logging].path at a writable directory",
        )
    return Check(name="logging", outcome=Outcome.OK, detail=str(path))


def _read_schema_version(path: Path) -> int | None:
    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    try:
        row = connection.execute("SELECT version FROM schema_version").fetchone()
    finally:
        connection.close()
    if row is None or not isinstance(row[0], int):
        return None
    return int(row[0])
