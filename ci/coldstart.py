# Copyright 2026 Julien Bombled
# SPDX-License-Identifier: Apache-2.0

"""Assert what an unmodified interpreter does when it runs Engram.

A quality gate that installs a recent SQLite before running the suite answers a
question no user can answer the same way: it manufactures the version floor
instead of measuring it. This module measures it on a stock runtime.

The assertion is a disjunction because the SQLite linked by a hosted runner's
interpreter is not known in advance. Either it satisfies the fail-closed minimum
and the command succeeds, or it does not and the command must refuse with the
documented exit code and a message naming the requirement, the version found,
the reason, and a remedy that exists in this repository. That message is the
only thing between a Windows user and a silently broken installation, so every
element is checked instead of trusted.

The version is read from the linked library rather than from an opened
connection, so measuring costs no file, no lock, and no log.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import subprocess
import sys
import sysconfig
from collections.abc import Sequence
from pathlib import Path

from ci.isolate import write_isolated_config

# Both halves of the contract are imported from what ships, never restated. A
# contract asserted against a copy of its own constants proves only that the
# copy was faithful on the day it was written.
from engram.cli import EXIT_LOCAL_RESOURCE
from engram.db import MINIMUM_SQLITE_VERSION

REFUSAL_EXIT_CODE = EXIT_LOCAL_RESOURCE
CONSOLE_SCRIPT = "engram"
PROBE_COMMAND = "migrate"
CONFIG_ENVIRONMENT_KEY = "ENGRAM_CONFIG"
OUTPUT_ENVIRONMENT_KEY = "GITHUB_OUTPUT"
SUCCESS_EXIT_CODE = 0
FAILED_EXIT_CODE = 1
REFUSAL_REASON = "WAL-reset"
DOCUMENTED_REMEDY = "docs/en/installation-windows.md"


class ColdStartError(Exception):
    """One violated turnkey contract."""


def linked_sqlite_version() -> str:
    """Return the SQLite version linked by this interpreter, without opening a database."""
    return sqlite3.sqlite_version


def satisfies_minimum(version: str, minimum: tuple[int, ...] = MINIMUM_SQLITE_VERSION) -> bool:
    """Report whether a measured SQLite version clears the fail-closed minimum."""
    return _parse_version(version) >= minimum


def locate_console_script() -> Path:
    """Return the console script owned by the running interpreter.

    Resolving the name from ``PATH`` alone would let a second installation answer for
    the interpreter being measured, which is the failure this job exists to expose.
    """
    located = shutil.which(CONSOLE_SCRIPT)
    if located is None:
        raise ColdStartError(
            f"{CONSOLE_SCRIPT} is not on PATH. Installing the distribution must publish "
            "its console script; a turnkey installation is not complete without it."
        )
    script = Path(located).resolve()
    expected_directory = Path(sysconfig.get_path("scripts")).resolve()
    if script.parent != expected_directory:
        raise ColdStartError(
            f"{script} does not belong to {sys.executable}; its scripts live in "
            f"{expected_directory}. The measured interpreter and the invoked command "
            "must be the same installation."
        )
    return script


def run_probe(script: Path, config_path: Path) -> subprocess.CompletedProcess[str]:
    """Run one storage command against an isolated configuration and capture its result."""
    environment = dict(os.environ)
    environment[CONFIG_ENVIRONMENT_KEY] = str(config_path)
    return subprocess.run(  # noqa: S603
        [str(script), PROBE_COMMAND],
        capture_output=True,
        text=True,
        check=False,
        env=environment,
    )


def verify_refusal(
    result: subprocess.CompletedProcess[str],
    *,
    found: str,
    repository: Path,
) -> None:
    """Refuse a runtime below the minimum that does not fail closed as documented.

    Every missing element is reported at once: a contract checked one clause per run
    takes as many runs as it has clauses to become readable.
    """
    problems: list[str] = []
    if result.returncode != REFUSAL_EXIT_CODE:
        problems.append(
            f"exit code {result.returncode} instead of {REFUSAL_EXIT_CODE} for SQLite {found}"
        )
    required = (
        _version_text(MINIMUM_SQLITE_VERSION),
        found,
        REFUSAL_REASON,
        DOCUMENTED_REMEDY,
    )
    problems.extend(
        f"the refusal never names {element}" for element in required if element not in result.stderr
    )
    if not (repository / DOCUMENTED_REMEDY).is_file():
        problems.append(f"the refusal points at {DOCUMENTED_REMEDY}, which this checkout lacks")
    if problems:
        raise ColdStartError("; ".join(problems))


def verify_success(result: subprocess.CompletedProcess[str], *, found: str) -> None:
    """Refuse a runtime at or above the minimum whose storage command does not succeed."""
    if result.returncode != SUCCESS_EXIT_CODE:
        raise ColdStartError(
            f"SQLite {found} clears the minimum, so {CONSOLE_SCRIPT} {PROBE_COMMAND} must "
            f"succeed; it exited {result.returncode}. Standard error: "
            f"{result.stderr.strip() or '(empty)'}"
        )


def publish_outcome(*, satisfied: bool, environ: dict[str, str] | None = None) -> None:
    """Record the measured branch for later steps of the same job."""
    environment = os.environ if environ is None else environ
    destination = environment.get(OUTPUT_ENVIRONMENT_KEY, "")
    if not destination:
        return
    with Path(destination).open("a", encoding="utf-8") as handle:
        handle.write(f"floor-satisfied={'true' if satisfied else 'false'}\n")


def main(argv: Sequence[str] | None = None) -> int:
    """Measure one runtime and assert the branch of the contract it lands on."""
    parser = _build_parser()
    arguments = parser.parse_args(argv)
    found = linked_sqlite_version()
    satisfied = satisfies_minimum(found)
    sys.stdout.write(
        f"interpreter={sys.executable}\n"
        f"sqlite={found}\n"
        f"minimum={_version_text(MINIMUM_SQLITE_VERSION)}\n"
        f"floor-satisfied={'true' if satisfied else 'false'}\n"
    )
    try:
        script = locate_console_script()
        config_path = write_isolated_config(arguments.directory)
        result = run_probe(script, config_path)
        if satisfied:
            verify_success(result, found=found)
            outcome = f"{CONSOLE_SCRIPT} {PROBE_COMMAND} succeeded"
        else:
            verify_refusal(result, found=found, repository=arguments.repository)
            outcome = f"{CONSOLE_SCRIPT} {PROBE_COMMAND} refused as documented"
    except ColdStartError as exc:
        sys.stderr.write(f"engram-coldstart: error: {exc}\n")
        return FAILED_EXIT_CODE
    publish_outcome(satisfied=satisfied)
    sys.stdout.write(f"outcome={outcome}\n")
    return SUCCESS_EXIT_CODE


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="engram-coldstart",
        description="Assert the turnkey contract of an unmodified interpreter.",
    )
    parser.add_argument(
        "--repository",
        type=Path,
        required=True,
        help="Checkout root holding the remedy the refusal message points at",
    )
    parser.add_argument(
        "--directory",
        type=Path,
        required=True,
        help="Absolute directory receiving the isolated configuration, database, and log",
    )
    return parser


def _parse_version(text: str) -> tuple[int, ...]:
    try:
        return tuple(int(part) for part in text.split("."))
    except ValueError as exc:
        raise ColdStartError(f"unreadable SQLite version: {text}") from exc


def _version_text(version: Sequence[int]) -> str:
    return ".".join(str(part) for part in version)


if __name__ == "__main__":
    raise SystemExit(main())
