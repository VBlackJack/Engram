# Copyright 2026 Julien Bombled
# SPDX-License-Identifier: Apache-2.0

"""Prove the turnkey contract holds on both branches an unmodified runtime can land on."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from ci.coldstart import (
    DOCUMENTED_REMEDY,
    OUTPUT_ENVIRONMENT_KEY,
    REFUSAL_EXIT_CODE,
    ColdStartError,
    publish_outcome,
    satisfies_minimum,
    verify_refusal,
    verify_success,
)
from engram.cli import EXIT_LOCAL_RESOURCE, _error_exit_code
from engram.db import MINIMUM_SQLITE_VERSION, SQLiteVersionError

REFUSAL_TEMPLATE = (
    "engram: error: SQLite 3.51.3 or newer is required; found {found}. "
    "Older runtimes are rejected because they do not contain the WAL-reset bug fix. "
    "See docs/en/installation-windows.md for supported Windows installation steps.\n"
)
OLD_VERSION = "3.50.4"


def _completed(returncode: int, stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["engram", "migrate"], returncode=returncode, stderr=stderr, stdout=""
    )


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    remedy = tmp_path / DOCUMENTED_REMEDY
    remedy.parent.mkdir(parents=True)
    remedy.write_text("remedy", encoding="utf-8")
    return tmp_path


def test_documented_refusal_satisfies_the_contract(repository: Path) -> None:
    result = _completed(REFUSAL_EXIT_CODE, REFUSAL_TEMPLATE.format(found=OLD_VERSION))

    verify_refusal(result, found=OLD_VERSION, repository=repository)


def test_a_silent_success_below_the_minimum_is_refused(repository: Path) -> None:
    with pytest.raises(ColdStartError, match="exit code 0"):
        verify_refusal(_completed(0), found=OLD_VERSION, repository=repository)


@pytest.mark.parametrize(
    ("removed", "expected"),
    [
        ("3.51.3", "3.51.3"),
        (OLD_VERSION, OLD_VERSION),
        ("WAL-reset", "WAL-reset"),
        (DOCUMENTED_REMEDY, DOCUMENTED_REMEDY),
    ],
)
def test_every_element_of_the_message_is_required(
    repository: Path,
    removed: str,
    expected: str,
) -> None:
    stderr = REFUSAL_TEMPLATE.format(found=OLD_VERSION).replace(removed, "")
    result = _completed(REFUSAL_EXIT_CODE, stderr)

    with pytest.raises(ColdStartError, match=f"never names {expected}"):
        verify_refusal(result, found=OLD_VERSION, repository=repository)


def test_a_remedy_missing_from_the_checkout_is_refused(tmp_path: Path) -> None:
    result = _completed(REFUSAL_EXIT_CODE, REFUSAL_TEMPLATE.format(found=OLD_VERSION))

    with pytest.raises(ColdStartError, match="which this checkout lacks"):
        verify_refusal(result, found=OLD_VERSION, repository=tmp_path)


def test_every_violation_is_reported_in_one_pass(tmp_path: Path) -> None:
    with pytest.raises(ColdStartError) as failure:
        verify_refusal(_completed(0), found=OLD_VERSION, repository=tmp_path)

    reported = str(failure.value)
    assert "exit code 0" in reported
    assert "never names WAL-reset" in reported
    assert "which this checkout lacks" in reported


def test_a_runtime_above_the_minimum_must_succeed() -> None:
    verify_success(_completed(0), found="3.53.3")

    with pytest.raises(ColdStartError, match="must succeed; it exited 3"):
        verify_success(_completed(REFUSAL_EXIT_CODE, "broken"), found="3.53.3")


@pytest.mark.parametrize("version", ["3.50.4", "3.51.2", "3.49.1"])
def test_a_version_below_the_shipped_constant_is_refused(version: str) -> None:
    assert not satisfies_minimum(version)


@pytest.mark.parametrize("version", ["3.51.3", "3.53.3", "4.0.0"])
def test_a_version_at_or_above_the_shipped_constant_is_accepted(version: str) -> None:
    assert satisfies_minimum(version)


def test_the_boundary_comes_from_the_shipped_constant_alone() -> None:
    assert satisfies_minimum(".".join(str(part) for part in MINIMUM_SQLITE_VERSION))


def test_the_asserted_exit_code_is_the_one_the_command_returns() -> None:
    """Tie the contract to the shipped dispatcher rather than to a copied literal."""
    assert REFUSAL_EXIT_CODE == EXIT_LOCAL_RESOURCE
    assert _error_exit_code(SQLiteVersionError("below the floor")) == REFUSAL_EXIT_CODE


def test_the_measured_branch_is_published_for_later_steps(tmp_path: Path) -> None:
    destination = tmp_path / "outputs"
    destination.write_text("", encoding="utf-8")

    publish_outcome(satisfied=False, environ={OUTPUT_ENVIRONMENT_KEY: str(destination)})
    publish_outcome(satisfied=True, environ={OUTPUT_ENVIRONMENT_KEY: str(destination)})

    assert (
        destination.read_text(encoding="utf-8") == "floor-satisfied=false\nfloor-satisfied=true\n"
    )


def test_publication_is_skipped_outside_a_workflow() -> None:
    publish_outcome(satisfied=True, environ={})
