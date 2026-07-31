# Copyright 2026 Julien Bombled
# SPDX-License-Identifier: Apache-2.0

"""Prove every continuous-integration precondition refuses before it can do harm."""

from __future__ import annotations

from pathlib import Path

import pytest

from ci.isolate import (
    CONFIG_ENVIRONMENT_KEY,
    ISOLATED_SERVER_PORT,
    IsolationError,
    main,
    verify_isolated_config,
    verify_runtime_sqlite,
    verify_workspace_location,
    write_isolated_config,
)
from engram.config import load_config


@pytest.mark.parametrize(
    "relation",
    ["identical", "workspace-inside-protected", "protected-inside-workspace"],
)
def test_overlapping_workspace_is_refused(tmp_path: Path, relation: str) -> None:
    installation = tmp_path / "installation"
    installation.mkdir()
    if relation == "identical":
        workspace = installation
    elif relation == "workspace-inside-protected":
        workspace = installation / "runner" / "_work"
    else:
        workspace = tmp_path
    with pytest.raises(IsolationError, match="git clean -ffdx"):
        verify_workspace_location(workspace, [installation])


def test_disjoint_workspace_is_accepted(tmp_path: Path) -> None:
    installation = tmp_path / "installation"
    workspace = tmp_path / "runner" / "_work"
    installation.mkdir()
    workspace.mkdir(parents=True)

    assert verify_workspace_location(workspace, [installation]) == workspace.resolve()


def test_runtime_below_minimum_is_refused() -> None:
    unreachable = (9, 0, 0)
    with pytest.raises(IsolationError, match="Pin the interpreter"):
        verify_runtime_sqlite(unreachable)


def test_runtime_at_minimum_is_accepted() -> None:
    assert verify_runtime_sqlite((0, 0, 0))


def test_written_config_resolves_every_path_outside_the_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    isolated = tmp_path / "isolated"
    workspace.mkdir()

    config_path = write_isolated_config(isolated)
    config = load_config(config_path, environ={})

    assert config_path.is_absolute()
    assert config.database.path.is_absolute()
    assert config.logging.path.is_absolute()
    assert not config.database.path.is_relative_to(workspace.resolve())
    assert not config.logging.path.is_relative_to(workspace.resolve())
    assert config.server.port == ISOLATED_SERVER_PORT


def test_missing_environment_variable_is_refused(tmp_path: Path) -> None:
    with pytest.raises(IsolationError, match="is not set"):
        verify_isolated_config(tmp_path, {})


def test_relative_environment_variable_is_refused(tmp_path: Path) -> None:
    with pytest.raises(IsolationError, match="must be absolute"):
        verify_isolated_config(tmp_path, {CONFIG_ENVIRONMENT_KEY: "engram-ci.toml"})


def test_storage_inside_the_workspace_is_refused(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config_path = write_isolated_config(workspace / "isolated")

    with pytest.raises(IsolationError, match="inside the workspace"):
        verify_isolated_config(workspace, {CONFIG_ENVIRONMENT_KEY: str(config_path)})


def test_relative_override_is_refused_because_it_lands_beside_the_configuration(
    tmp_path: Path,
) -> None:
    """Cover the resolution rule: an override resolves against the configuration directory."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config_path = write_isolated_config(workspace / "isolated")
    environment = {
        CONFIG_ENVIRONMENT_KEY: str(config_path),
        "ENGRAM_DATABASE_PATH": "relative.db",
    }

    with pytest.raises(IsolationError, match=r"database\.path"):
        verify_isolated_config(workspace, environment)


def test_accepted_configuration_is_returned(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config_path = write_isolated_config(tmp_path / "isolated")

    assert (
        verify_isolated_config(workspace, {CONFIG_ENVIRONMENT_KEY: str(config_path)}) == config_path
    )


def test_main_refuses_an_overlapping_workspace_without_writing(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    installation = tmp_path / "installation"
    isolated = tmp_path / "isolated"
    installation.mkdir()

    code = main(
        [
            "--workspace",
            str(installation / "_work"),
            "--protected",
            str(installation),
            "prepare",
            "--directory",
            str(isolated),
        ]
    )

    assert code == 2
    assert "git clean -ffdx" in capsys.readouterr().err
    assert not isolated.exists()


def test_omitting_the_protection_declaration_is_refused_by_the_parser(tmp_path: Path) -> None:
    """Keep the silent-empty-list failure unreachable: the declaration is mandatory."""
    with pytest.raises(SystemExit) as refusal:
        main(["--workspace", str(tmp_path), "prepare", "--directory", str(tmp_path / "isolated")])

    assert refusal.value.code == 2
    assert not (tmp_path / "isolated").exists()


def test_declaring_both_protection_forms_is_refused_by_the_parser(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        main(
            [
                "--workspace",
                str(tmp_path / "workspace"),
                "--protected",
                str(tmp_path / "installation"),
                "--no-protected-paths",
                "prepare",
                "--directory",
                str(tmp_path / "isolated"),
            ]
        )


def test_explicitly_declaring_no_protected_path_is_recorded(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Allow a hosted runner to declare the absence, and keep that choice auditable."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    code = main(
        [
            "--workspace",
            str(workspace),
            "--no-protected-paths",
            "prepare",
            "--directory",
            str(tmp_path / "isolated"),
        ]
    )

    assert code == 0
    assert "protected=none-declared" in capsys.readouterr().out


def test_main_prepare_reports_the_configuration_it_wrote(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    workspace = tmp_path / "workspace"
    isolated = tmp_path / "isolated"
    workspace.mkdir()

    code = main(
        [
            "--workspace",
            str(workspace),
            "--protected",
            str(tmp_path / "installation"),
            "prepare",
            "--directory",
            str(isolated),
        ]
    )

    assert code == 0
    reported = capsys.readouterr().out
    assert f"{CONFIG_ENVIRONMENT_KEY}={isolated.resolve()}" in reported
    assert (isolated / "engram-ci.toml").is_file()
