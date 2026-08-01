# Copyright 2026 Julien Bombled
# SPDX-License-Identifier: Apache-2.0

"""Enforce a per-module coverage floor for the modules that hold memory.

A whole-project average is not a floor. A well-covered periphery raises it while
the core stays thin, and the number moves in the right direction as the wrong
code is tested. This module names the files whose defects lose or corrupt stored
memory and requires each of them to clear a floor on its own.

The list is derived from that definition, not from what already passes. When it
was first applied, all three modules were below the floor and none of the modules
that already cleared it were on the list. A set chosen for comfort would have had
the opposite shape.

The project-wide floor is not restated here. It lives in the coverage
configuration, which is the one place that enforces it.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

CRITICAL_MODULES = (
    # Schema, migrations and the fail-closed guards. A defect here corrupts
    # storage or lets an unusable database open.
    "src/engram/db.py",
    # Every read and write of an entry, including the trust transitions.
    "src/engram/store.py",
    # The single-writer guarantee. A defect here admits a second writer.
    "src/engram/process_lock.py",
)
CRITICAL_FLOOR = 90.0
FAILED_EXIT_CODE = 1
SUCCESS_EXIT_CODE = 0


def normalize(path: str) -> str:
    """Return one separator convention, because the two legs record different ones."""
    return path.replace("\\", "/")


def measured_percentages(report: Mapping[str, Any]) -> dict[str, float]:
    """Extract the covered percentage of every file in a coverage JSON report."""
    files = report.get("files", {})
    return {
        normalize(path): float(entry["summary"]["percent_covered"]) for path, entry in files.items()
    }


def evaluate(
    percentages: Mapping[str, float],
    *,
    modules: Sequence[str] = CRITICAL_MODULES,
    floor: float = CRITICAL_FLOOR,
) -> list[str]:
    """Return one line per violated floor, empty when every module clears it.

    A declared module absent from the report is a violation rather than a pass.
    Renaming or moving a file would otherwise remove its floor without removing
    its risk, and nothing would say so.
    """
    # Normalised on both sides. Normalising only the declared names would make a
    # backslash report look like a report with no critical modules in it at all.
    measurements = {normalize(path): value for path, value in percentages.items()}
    violations: list[str] = []
    for module in modules:
        measured = measurements.get(normalize(module))
        if measured is None:
            violations.append(
                f"{module} is declared critical but absent from the report. "
                "Update the declaration if the file moved; do not let the floor lapse."
            )
        elif measured < floor:
            violations.append(f"{module} covers {measured:.2f} percent, below the {floor} floor")
    return violations


def main(argv: Sequence[str] | None = None) -> int:
    """Check one coverage report against the per-module floors."""
    parser = _build_parser()
    arguments = parser.parse_args(argv)
    try:
        report = json.loads(arguments.report.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        sys.stderr.write(f"engram-coverage: error: unreadable report {arguments.report}: {exc}\n")
        return FAILED_EXIT_CODE

    percentages = measured_percentages(report)
    violations = evaluate(percentages, floor=arguments.floor)
    for module in CRITICAL_MODULES:
        measured = percentages.get(normalize(module))  # already normalised by the reader
        rendered = "absent" if measured is None else f"{measured:.2f}"
        sys.stdout.write(f"{module}={rendered}\n")
    if violations:
        for violation in violations:
            sys.stderr.write(f"engram-coverage: error: {violation}\n")
        return FAILED_EXIT_CODE
    return SUCCESS_EXIT_CODE


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="engram-coverage",
        description="Refuse a run whose memory-holding modules fall below their floor.",
    )
    parser.add_argument(
        "--report",
        type=Path,
        required=True,
        help="Coverage JSON report covering every leg that was run",
    )
    parser.add_argument(
        "--floor",
        type=float,
        default=CRITICAL_FLOOR,
        help="Percentage each critical module must clear",
    )
    return parser


if __name__ == "__main__":
    raise SystemExit(main())
