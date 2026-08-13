# Copyright 2026 Julien Bombled
# SPDX-License-Identifier: Apache-2.0

"""MCP client connection tests."""

from __future__ import annotations

import json
import tomllib
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlparse

import pytest

from engram.clients import (
    ClientConfigError,
    ClientKind,
    _render_codex_block,
    connect,
    endpoint_url,
    install_protocol,
    plan_client,
)
from engram.config import AppConfig, ServerConfig
from engram.resources import client_protocol_text

DOCUMENTED_PROTOCOL = Path(__file__).resolve().parent.parent / "docs" / "en" / "client-protocol.md"


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Run each client command in a directory of its own, with its own home."""
    monkeypatch.chdir(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    return home


def test_the_endpoint_comes_from_the_configuration_not_from_a_documented_default(
    app_config: AppConfig,
) -> None:
    """A user who changed the port must never have to notice the docs say 8377."""
    assert endpoint_url(app_config) == "http://127.0.0.1:8377/mcp"

    moved = AppConfig(
        database=app_config.database,
        ttl_days=app_config.ttl_days,
        limits=app_config.limits,
        logging=app_config.logging,
        server=type(app_config.server)(host="127.0.0.1", port=9999, path="/memory"),
    )

    assert endpoint_url(moved) == "http://127.0.0.1:9999/memory"


@pytest.mark.parametrize("kind", list(ClientKind))
def test_connecting_a_client_writes_an_endpoint_it_can_reach(
    kind: ClientKind,
    app_config: AppConfig,
    workspace: Path,
) -> None:
    plan = plan_client(kind, app_config, home=workspace)

    assert connect(plan, force=False) is True
    assert plan.config_path.is_file()
    assert endpoint_url(app_config) in plan.config_path.read_text(encoding="utf-8")


@pytest.mark.parametrize("kind", list(ClientKind))
def test_connecting_twice_changes_nothing_the_second_time(
    kind: ClientKind,
    app_config: AppConfig,
    workspace: Path,
) -> None:
    connect(plan_client(kind, app_config, home=workspace), force=False)
    written = plan_client(kind, app_config, home=workspace).config_path.read_text(encoding="utf-8")

    replanned = plan_client(kind, app_config, home=workspace)

    assert replanned.already_correct is True
    assert connect(replanned, force=False) is False
    assert replanned.config_path.read_text(encoding="utf-8") == written


def test_another_mcp_server_in_the_same_json_file_survives(
    app_config: AppConfig,
    workspace: Path,
) -> None:
    plan = plan_client(ClientKind.GEMINI, app_config, home=workspace)
    plan.config_path.parent.mkdir(parents=True, exist_ok=True)
    plan.config_path.write_text(
        json.dumps({"theme": "dark", "mcpServers": {"datacron": {"httpUrl": "http://x/mcp"}}}),
        encoding="utf-8",
    )

    connect(plan, force=False)

    document = json.loads(plan.config_path.read_text(encoding="utf-8"))
    assert document["theme"] == "dark"
    assert document["mcpServers"]["datacron"] == {"httpUrl": "http://x/mcp"}
    assert document["mcpServers"]["engram"]["httpUrl"] == endpoint_url(app_config)


def test_comments_and_other_servers_in_the_codex_file_survive(
    app_config: AppConfig,
    workspace: Path,
) -> None:
    plan = plan_client(ClientKind.CODEX, app_config, home=workspace)
    plan.config_path.parent.mkdir(parents=True, exist_ok=True)
    plan.config_path.write_text(
        '# kept\nmodel = "gpt-5"\n\n[mcp_servers.datacron]\nurl = "http://x/mcp"\n',
        encoding="utf-8",
    )

    connect(plan, force=False)

    text = plan.config_path.read_text(encoding="utf-8")
    document = tomllib.loads(text)
    assert "# kept" in text
    assert document["model"] == "gpt-5"
    assert document["mcp_servers"]["datacron"]["url"] == "http://x/mcp"
    assert document["mcp_servers"]["engram"]["url"] == endpoint_url(app_config)


def test_the_codex_block_never_ties_startup_to_engram_being_up() -> None:
    """`required` fails Codex startup when the server is down, which is not ours to spend."""
    assert "required" not in _render_codex_block("http://127.0.0.1:8377/mcp")


def test_an_entry_naming_another_endpoint_is_not_replaced_silently(
    app_config: AppConfig,
    workspace: Path,
) -> None:
    plan = plan_client(ClientKind.CLAUDE, app_config, home=workspace)
    plan.config_path.write_text(
        json.dumps({"mcpServers": {"engram": {"type": "http", "url": "http://other/mcp"}}}),
        encoding="utf-8",
    )

    with pytest.raises(ClientConfigError, match="already configures"):
        connect(plan_client(ClientKind.CLAUDE, app_config, home=workspace), force=False)


def test_a_replacement_is_written_when_it_is_asked_for(
    app_config: AppConfig,
    workspace: Path,
) -> None:
    plan = plan_client(ClientKind.CLAUDE, app_config, home=workspace)
    plan.config_path.write_text(
        json.dumps({"mcpServers": {"engram": {"type": "http", "url": "http://other/mcp"}}}),
        encoding="utf-8",
    )

    connect(plan_client(ClientKind.CLAUDE, app_config, home=workspace), force=True)

    document = json.loads(plan.config_path.read_text(encoding="utf-8"))
    assert document["mcpServers"]["engram"]["url"] == endpoint_url(app_config)


def test_unreadable_vendor_json_is_refused_rather_than_overwritten(
    app_config: AppConfig,
    workspace: Path,
) -> None:
    plan = plan_client(ClientKind.GEMINI, app_config, home=workspace)
    plan.config_path.parent.mkdir(parents=True, exist_ok=True)
    plan.config_path.write_text("{ not json", encoding="utf-8")

    with pytest.raises(ClientConfigError, match="not readable JSON"):
        connect(plan_client(ClientKind.GEMINI, app_config, home=workspace), force=True)


def test_the_protocol_is_appended_once_and_keeps_what_was_there(
    app_config: AppConfig,
    workspace: Path,
) -> None:
    plan = plan_client(ClientKind.CLAUDE, app_config, home=workspace)
    plan.instructions_path.write_text("# My project\n\nExisting guidance.\n", encoding="utf-8")

    assert install_protocol(plan) is True
    assert install_protocol(plan) is False

    text = plan.instructions_path.read_text(encoding="utf-8")
    assert "Existing guidance." in text
    assert text.count("Engram session protocol") == 1


def test_the_packaged_protocol_is_the_one_the_documentation_publishes() -> None:
    """A protocol that exists only in a page is one nobody can be given by a command."""
    published = DOCUMENTED_PROTOCOL.read_text(encoding="utf-8")

    assert client_protocol_text().strip() in published


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("127.0.0.1", "http://127.0.0.1:8377/mcp"),
        ("127.0.0.5", "http://127.0.0.5:8377/mcp"),
        ("::1", "http://[::1]:8377/mcp"),
    ],
)
def test_an_ipv6_endpoint_is_bracketed_so_a_client_can_parse_it(
    host: str,
    expected: str,
    app_config: AppConfig,
) -> None:
    """server.host accepts every loopback literal, so the URL has to survive all of them."""
    config = replace(app_config, server=ServerConfig(host=host, port=8377, path="/mcp"))

    url = endpoint_url(config)
    parsed = urlparse(url)

    assert url == expected
    assert parsed.hostname == host
    assert parsed.port == 8377


def test_a_codex_table_without_a_url_is_refused_rather_than_duplicated(
    app_config: AppConfig,
    workspace: Path,
) -> None:
    """TOML forbids the same table twice; appending one produced a file Codex cannot read."""
    plan = plan_client(ClientKind.CODEX, app_config, home=workspace)
    plan.config_path.parent.mkdir(parents=True, exist_ok=True)
    plan.config_path.write_text("[mcp_servers.engram]\nenabled = true\n", encoding="utf-8")

    with pytest.raises(ClientConfigError, match="already declares"):
        connect(plan_client(ClientKind.CODEX, app_config, home=workspace), force=False)

    text = plan.config_path.read_text(encoding="utf-8")
    assert text.count("[mcp_servers.engram]") == 1
    assert tomllib.loads(text)


def test_a_codex_file_that_is_not_valid_toml_is_never_appended_to(
    app_config: AppConfig,
    workspace: Path,
) -> None:
    """A document that does not parse is not an absent table."""
    plan = plan_client(ClientKind.CODEX, app_config, home=workspace)
    plan.config_path.parent.mkdir(parents=True, exist_ok=True)
    plan.config_path.write_text("[mcp_servers.engram\nbroken\n", encoding="utf-8")

    with pytest.raises(ClientConfigError, match="not valid TOML"):
        plan_client(ClientKind.CODEX, app_config, home=workspace)

    assert plan.config_path.read_text(encoding="utf-8") == "[mcp_servers.engram\nbroken\n"


def test_a_codex_table_naming_another_endpoint_is_refused_even_with_force(
    app_config: AppConfig,
    workspace: Path,
) -> None:
    """Rewriting a table textually would lose the comments and ordering around it."""
    plan = plan_client(ClientKind.CODEX, app_config, home=workspace)
    plan.config_path.parent.mkdir(parents=True, exist_ok=True)
    plan.config_path.write_text(
        '# kept\n[mcp_servers.engram]\nurl = "http://127.0.0.1:9999/mcp"\n',
        encoding="utf-8",
    )

    with pytest.raises(ClientConfigError, match="already declares"):
        connect(plan_client(ClientKind.CODEX, app_config, home=workspace), force=True)

    assert "# kept" in plan.config_path.read_text(encoding="utf-8")
