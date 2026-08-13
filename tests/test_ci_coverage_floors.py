# Copyright 2026 Julien Bombled
# SPDX-License-Identifier: Apache-2.0

"""Prove the per-module floor refuses what a project average would let through."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ci.coverage_floors import (
    CRITICAL_FLOOR,
    CRITICAL_MODULES,
    evaluate,
    main,
    measured_percentages,
    normalize,
)

CLEARING = dict.fromkeys(CRITICAL_MODULES, 95.0)


def _report(percentages: dict[str, float]) -> dict[str, object]:
    return {
        "files": {
            path: {"summary": {"percent_covered": value}} for path, value in percentages.items()
        }
    }


def test_every_declared_module_above_the_floor_passes() -> None:
    assert evaluate(CLEARING) == []


def test_a_module_below_the_floor_is_named_with_its_number() -> None:
    percentages = {**CLEARING, CRITICAL_MODULES[0]: 89.63}

    violations = evaluate(percentages)

    assert len(violations) == 1
    assert CRITICAL_MODULES[0] in violations[0]
    assert "89.63" in violations[0]


def test_a_module_exactly_on_the_floor_passes() -> None:
    assert evaluate({**CLEARING, CRITICAL_MODULES[0]: CRITICAL_FLOOR}) == []


def test_a_declared_module_missing_from_the_report_is_a_violation() -> None:
    """A renamed file must remove its floor deliberately, not by going unnoticed."""
    percentages = dict.fromkeys(CRITICAL_MODULES[1:], 95.0)

    violations = evaluate(percentages)

    assert len(violations) == 1
    assert "absent from the report" in violations[0]


def test_every_violation_is_reported_in_one_pass() -> None:
    violations = evaluate({CRITICAL_MODULES[0]: 10.0})

    assert len(violations) == len(CRITICAL_MODULES)


def test_a_high_project_average_does_not_lift_a_thin_module() -> None:
    """The whole point: peripheral coverage must not answer for the core."""
    percentages = {
        **CLEARING,
        CRITICAL_MODULES[2]: 80.0,
        "src/engram/eval/models.py": 100.0,
        "src/engram/eval/report.py": 100.0,
        "src/engram/eval/graders.py": 100.0,
    }

    violations = evaluate(percentages)

    assert len(violations) == 1
    assert CRITICAL_MODULES[2] in violations[0]


def test_the_windows_separator_is_the_same_module() -> None:
    """The two legs record different separators; one of them must not read as absent."""
    windows = {module.replace("/", "\\"): 95.0 for module in CRITICAL_MODULES}

    assert evaluate(windows) == []
    assert evaluate(measured_percentages(_report(windows))) == []
    assert normalize("src\\engram\\db.py") == "src/engram/db.py"


def test_percentages_are_read_from_the_report_summary() -> None:
    report = _report({"src/engram/db.py": 90.5})

    assert measured_percentages(report) == {"src/engram/db.py": 90.5}


def test_the_declared_set_is_not_the_set_that_already_passed() -> None:
    """Guard the definition itself: comfort would have selected different files.

    The set may only grow by argument. db, store and process_lock hold the memory;
    capsule and retrieval decide whether it can be reached, and an entry nobody
    can reach costs the reader what a lost one costs. Anything outside that
    definition still has no floor, however well covered it happens to be.
    """
    assert set(CRITICAL_MODULES) == {
        "src/engram/db.py",
        "src/engram/store.py",
        "src/engram/process_lock.py",
        "src/engram/capsule.py",
        "src/engram/retrieval.py",
    }
    for module in CRITICAL_MODULES:
        assert Path(module).is_file(), f"{module} is declared critical but does not exist"


def test_a_run_below_the_floor_exits_non_zero(tmp_path: Path) -> None:
    report = tmp_path / "coverage.json"
    report.write_text(
        json.dumps(_report({**CLEARING, CRITICAL_MODULES[0]: 42.0})),
        encoding="utf-8",
    )

    assert main(["--report", str(report)]) == 1


def test_a_clearing_run_exits_zero(tmp_path: Path) -> None:
    report = tmp_path / "coverage.json"
    report.write_text(json.dumps(_report(CLEARING)), encoding="utf-8")

    assert main(["--report", str(report)]) == 0


@pytest.mark.parametrize("content", ["not json", ""])
def test_an_unreadable_report_is_refused_rather_than_ignored(tmp_path: Path, content: str) -> None:
    report = tmp_path / "coverage.json"
    report.write_text(content, encoding="utf-8")

    assert main(["--report", str(report)]) == 1


def test_a_missing_report_is_refused(tmp_path: Path) -> None:
    assert main(["--report", str(tmp_path / "absent.json")]) == 1
