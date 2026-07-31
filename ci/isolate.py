# Copyright 2026 Julien Bombled
# SPDX-License-Identifier: Apache-2.0

"""Refuse a continuous-integration run whose preconditions are silently wrong.

Three hazards on a self-hosted runner produce no error of their own.

A workspace overlapping a working installation is destructive: `actions/checkout`
cleans with `git clean -ffdx`, which removes ignored files, and a repository whose
local state is entirely ignored and untracked loses that state on the first run.

An interpreter resolved from the launcher rather than pinned may link a SQLite older
than the fail-closed minimum, which surfaces much later as a storage error.

An unset configuration variable resolves the default file name against the working
directory instead of failing, so a run can write its database and log into the
checkout and break the working-tree gates.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

from engram.config import load_config
from engram.db import MINIMUM_SQLITE_VERSION

CONFIG_ENVIRONMENT_KEY = "ENGRAM_CONFIG"
ISOLATED_CONFIG_NAME = "engram-ci.toml"
ISOLATED_DATABASE_NAME = "engram-ci.db"
ISOLATED_LOG_NAME = "engram-ci.log"
ISOLATED_SERVER_PORT = 18377
REFUSED_EXIT_CODE = 2


class IsolationError(Exception):
    """One refused continuous-integration precondition."""


def verify_workspace_location(workspace: Path, protected: Sequence[Path]) -> Path:
    """Refuse a workspace sharing a path with an installation that must survive.

    Checkout cleaning removes ignored files, so an overlapping workspace destroys
    local state that no commit protects.
    """
    resolved_workspace = _resolved(workspace)
    for candidate in protected:
        resolved_candidate = _resolved(candidate)
        overlaps = resolved_workspace.is_relative_to(
            resolved_candidate
        ) or resolved_candidate.is_relative_to(resolved_workspace)
        if overlaps:
            raise IsolationError(
                f"workspace {resolved_workspace} shares a path with {resolved_candidate}. "
                "Checkout cleaning runs 'git clean -ffdx' and would remove every ignored "
                "file it finds. Configure the runner with a dedicated work directory."
            )
    return resolved_workspace


def verify_runtime_sqlite(minimum: tuple[int, ...] = MINIMUM_SQLITE_VERSION) -> str:
    """Refuse an interpreter whose linked SQLite predates the fail-closed minimum."""
    actual_text = sqlite3.sqlite_version
    if _parse_version(actual_text) < minimum:
        raise IsolationError(
            f"{sys.executable} links SQLite {actual_text}; {_version_text(minimum)} or newer "
            "is required. Pin the interpreter explicitly instead of resolving it from PATH."
        )
    return actual_text


def write_isolated_config(directory: Path, *, port: int = ISOLATED_SERVER_PORT) -> Path:
    """Write a configuration whose every path is absolute and rooted outside a checkout.

    Relative paths resolve against the configuration file's own directory, never against
    the working directory, so only absolute values keep the run predictable.
    """
    resolved = _resolved(directory)
    resolved.mkdir(parents=True, exist_ok=True)
    database = resolved / ISOLATED_DATABASE_NAME
    log = resolved / ISOLATED_LOG_NAME
    config_path = resolved / ISOLATED_CONFIG_NAME
    config_path.write_text(
        "\n".join(
            (
                "[database]",
                f'path = "{database.as_posix()}"',
                "",
                "[logging]",
                f'path = "{log.as_posix()}"',
                "",
                "[server]",
                f"port = {port}",
                "",
            )
        ),
        encoding="utf-8",
    )
    return config_path


def verify_isolated_config(
    workspace: Path,
    environ: Mapping[str, str] | None = None,
) -> Path:
    """Refuse an invocation whose resolved storage paths could land inside the workspace."""
    environment = os.environ if environ is None else environ
    declared = environment.get(CONFIG_ENVIRONMENT_KEY, "")
    if not declared:
        raise IsolationError(
            f"{CONFIG_ENVIRONMENT_KEY} is not set. Engram would resolve the default "
            "configuration name against the working directory instead of the isolated file."
        )
    config_path = Path(declared)
    if not config_path.is_absolute():
        raise IsolationError(f"{CONFIG_ENVIRONMENT_KEY} must be absolute; found {declared}")
    if not config_path.is_file():
        raise IsolationError(f"{CONFIG_ENVIRONMENT_KEY} does not name a file: {config_path}")

    config = load_config(config_path, environ=environment)
    resolved_workspace = _resolved(workspace)
    declared_paths = (
        ("database.path", config.database.path),
        ("logging.path", config.logging.path),
    )
    for label, candidate in declared_paths:
        if _resolved(candidate).is_relative_to(resolved_workspace):
            raise IsolationError(
                f"{label} resolves to {candidate}, inside the workspace. An isolated run "
                "must not write into the checkout or the working-tree gates will fail."
            )
    return config_path


def main(argv: Sequence[str] | None = None) -> int:
    """Run one isolation step and report a refusal on standard error."""
    parser = _build_parser()
    arguments = parser.parse_args(argv)
    try:
        verify_workspace_location(arguments.workspace, arguments.protected)
        version = verify_runtime_sqlite()
        if arguments.command == "prepare":
            config_path = write_isolated_config(arguments.directory, port=arguments.port)
        else:
            config_path = verify_isolated_config(arguments.workspace)
    except IsolationError as exc:
        sys.stderr.write(f"engram-ci: error: {exc}\n")
        return REFUSED_EXIT_CODE
    sys.stdout.write(
        f"protected={_protection_text(arguments.protected)}\n"
        f"sqlite={version}\n"
        f"{CONFIG_ENVIRONMENT_KEY}={config_path}\n"
    )
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="engram-ci",
        description="Prepare or verify an isolated Engram continuous-integration run.",
    )
    parser.add_argument(
        "--workspace",
        type=Path,
        required=True,
        help="Checkout directory the run must never write into",
    )
    protection = parser.add_mutually_exclusive_group(required=True)
    protection.add_argument(
        "--protected",
        type=Path,
        action="append",
        default=[],
        help="Installation directory that must not overlap the workspace; repeatable",
    )
    protection.add_argument(
        "--no-protected-paths",
        action="store_true",
        help="Declare that no installation shares this machine, as on a hosted runner",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare = subparsers.add_parser("prepare", help="Write an isolated configuration")
    prepare.add_argument(
        "--directory",
        type=Path,
        required=True,
        help="Absolute directory receiving the configuration, database, and log",
    )
    prepare.add_argument(
        "--port",
        type=int,
        default=ISOLATED_SERVER_PORT,
        help="Server port reserved for the isolated run",
    )
    subparsers.add_parser("verify", help="Refuse the run unless every precondition holds")
    return parser


def _protection_text(protected: Sequence[Path]) -> str:
    if not protected:
        return "none-declared"
    return ";".join(str(_resolved(candidate)) for candidate in protected)


def _resolved(path: Path) -> Path:
    return path.expanduser().resolve()


def _parse_version(text: str) -> tuple[int, ...]:
    try:
        return tuple(int(part) for part in text.split("."))
    except ValueError as exc:
        raise IsolationError(f"unreadable SQLite version: {text}") from exc


def _version_text(version: Sequence[int]) -> str:
    return ".".join(str(part) for part in version)


if __name__ == "__main__":
    raise SystemExit(main())
