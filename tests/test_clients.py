# Copyright 2026 Julien Bombled
# SPDX-License-Identifier: Apache-2.0

"""MCP client connection tests."""

from __future__ import annotations

import json
import os
import tomllib
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlparse

import pytest

import engram.clients as clients_module
from engram.clients import (
    ClientConfigError,
    ClientKind,
    _read_optional_bytes,
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
    plan.config_path.write_bytes(b"[mcp_servers.engram]\nenabled = true\n")

    with pytest.raises(ClientConfigError, match="already declares"):
        connect(plan_client(ClientKind.CODEX, app_config, home=workspace), force=False)

    raw = plan.config_path.read_bytes()
    assert raw == b"[mcp_servers.engram]\nenabled = true\n"
    assert tomllib.loads(raw.decode("utf-8"))


def test_a_codex_file_that_is_not_valid_toml_is_never_appended_to(
    app_config: AppConfig,
    workspace: Path,
) -> None:
    """A document that does not parse is not an absent table."""
    plan = plan_client(ClientKind.CODEX, app_config, home=workspace)
    plan.config_path.parent.mkdir(parents=True, exist_ok=True)
    plan.config_path.write_bytes(b"[mcp_servers.engram\nbroken\n")

    with pytest.raises(ClientConfigError, match="not valid TOML"):
        plan_client(ClientKind.CODEX, app_config, home=workspace)

    assert plan.config_path.read_bytes() == b"[mcp_servers.engram\nbroken\n"


def test_a_codex_table_naming_another_endpoint_is_refused_even_with_force(
    app_config: AppConfig,
    workspace: Path,
) -> None:
    """Rewriting a table textually would lose the comments and ordering around it."""
    plan = plan_client(ClientKind.CODEX, app_config, home=workspace)
    plan.config_path.parent.mkdir(parents=True, exist_ok=True)
    plan.config_path.write_bytes(
        b'# kept\n[mcp_servers.engram]\nurl = "http://127.0.0.1:9999/mcp"\n'
    )

    with pytest.raises(ClientConfigError, match="already declares"):
        connect(plan_client(ClientKind.CODEX, app_config, home=workspace), force=True)

    assert plan.config_path.read_bytes() == (
        b'# kept\n[mcp_servers.engram]\nurl = "http://127.0.0.1:9999/mcp"\n'
    )


@pytest.mark.parametrize(
    ("label", "content"),
    [
        ("mcp_servers is a string", 'mcp_servers = "legacy"\n'),
        ("mcp_servers.engram is a string", '[mcp_servers]\nengram = "legacy"\n'),
        (
            "mcp_servers is an inline table",
            'mcp_servers = { datacron = { url = "http://x/mcp" } }\n',
        ),
    ],
)
def test_a_structurally_invalid_codex_entry_is_refused_not_read_as_absent(
    label: str,
    content: str,
    app_config: AppConfig,
    workspace: Path,
) -> None:
    """Each of these is valid TOML that no [mcp_servers.engram] header can extend.

    They parse into the same shapes a well-formed configuration parses into, so
    treating a failed lookup as "nothing is configured" appended a second table
    and reported success over a file Codex can no longer read.
    """
    del label
    plan = plan_client(ClientKind.CODEX, app_config, home=workspace)
    plan.config_path.parent.mkdir(parents=True, exist_ok=True)
    plan.config_path.write_bytes(content.encode("utf-8"))

    with pytest.raises(ClientConfigError):
        connect(plan_client(ClientKind.CODEX, app_config, home=workspace), force=False)

    assert plan.config_path.read_bytes() == content.encode("utf-8")
    assert tomllib.loads(content) is not None


def test_a_codex_write_that_would_not_parse_puts_the_file_back(
    app_config: AppConfig,
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reasoning about when appending is safe is what produced the defect twice.

    The result is parsed instead of predicted, so a shape nobody enumerated
    still leaves the file exactly as it was rather than half-written.
    """
    original = '# irreplaceable\nmodel = "gpt-5"\n'
    plan = plan_client(ClientKind.CODEX, app_config, home=workspace)
    plan.config_path.parent.mkdir(parents=True, exist_ok=True)
    plan.config_path.write_bytes(original.encode("utf-8"))
    monkeypatch.setattr(clients_module, "_render_codex_block", lambda _url: "[unterminated\n")

    with pytest.raises(ClientConfigError, match="cannot carry"):
        connect(plan_client(ClientKind.CODEX, app_config, home=workspace), force=False)

    assert plan.config_path.read_bytes() == original.encode("utf-8")


def test_a_codex_write_that_would_not_parse_creates_no_file(
    app_config: AppConfig,
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rollback must not leave behind a file the user never had."""
    plan = plan_client(ClientKind.CODEX, app_config, home=workspace)
    monkeypatch.setattr(clients_module, "_render_codex_block", lambda _url: "[unterminated\n")

    with pytest.raises(ClientConfigError, match="cannot carry"):
        connect(plan_client(ClientKind.CODEX, app_config, home=workspace), force=False)

    assert not plan.config_path.exists()


REFUSED_CODEX_SHAPES = {
    "mcp_servers is a scalar": b'mcp_servers = "legacy"',
    "mcp_servers.engram is a scalar": b'[mcp_servers]\nengram = "legacy"',
    "mcp_servers is an inline table": b'mcp_servers = { datacron = { url = "http://x/mcp" } }',
    "the engram table carries no url": b"[mcp_servers.engram]\nenabled = true",
    "the engram table names another endpoint": b'[mcp_servers.engram]\nurl = "http://other/mcp"',
    "the document is not valid TOML": b"[mcp_servers.engram\nbroken",
}


def _with_endings(body: bytes, family: str) -> bytes:
    if family == "CRLF":
        return body.replace(b"\n", b"\r\n") + b"\r\n"
    if family == "mixed":
        return body.replace(b"\n", b"\r\n", 1) + b"\n"
    return body + b"\n"


@pytest.mark.parametrize("shape", sorted(REFUSED_CODEX_SHAPES))
@pytest.mark.parametrize("family", ["LF", "CRLF", "mixed"])
def test_a_refused_codex_write_leaves_the_file_identical_byte_for_byte(
    shape: str,
    family: str,
    app_config: AppConfig,
    workspace: Path,
) -> None:
    """Refusing is only fail-closed if the destination was never touched.

    The first attempt wrote the destination and undid it afterwards, through a
    text write that rewrites every line ending on Windows: a refusal announcing
    the file was unchanged had turned it from LF to CRLF. Comparing with
    read_text could not see that, because reading normalises it back.
    """
    raw = _with_endings(REFUSED_CODEX_SHAPES[shape], family)
    plan = plan_client(ClientKind.CODEX, app_config, home=workspace)
    plan.config_path.parent.mkdir(parents=True, exist_ok=True)
    plan.config_path.write_bytes(raw)

    with pytest.raises(ClientConfigError):
        connect(plan_client(ClientKind.CODEX, app_config, home=workspace), force=False)

    assert plan.config_path.read_bytes() == raw
    assert list(plan.config_path.parent.iterdir()) == [plan.config_path]


@pytest.mark.parametrize("family", ["LF", "CRLF"])
def test_an_accepted_codex_write_keeps_the_existing_bytes_and_their_newlines(
    family: str,
    app_config: AppConfig,
    workspace: Path,
) -> None:
    """A user's line endings are theirs; appending must not rewrite the file."""
    raw = _with_endings(b'# kept\n[mcp_servers.datacron]\nurl = "http://x/mcp"', family)
    plan = plan_client(ClientKind.CODEX, app_config, home=workspace)
    plan.config_path.parent.mkdir(parents=True, exist_ok=True)
    plan.config_path.write_bytes(raw)

    connect(plan_client(ClientKind.CODEX, app_config, home=workspace), force=False)

    written = plan.config_path.read_bytes()
    newline = b"\r\n" if family == "CRLF" else b"\n"
    assert written.startswith(raw)
    assert tomllib.loads(written.decode("utf-8"))["mcp_servers"]["engram"]["url"] == endpoint_url(
        app_config
    )
    assert written.removeprefix(raw).count(newline) == written.removeprefix(raw).count(b"\n")
    assert list(plan.config_path.parent.iterdir()) == [plan.config_path]


@pytest.mark.parametrize("family", ["LF", "CRLF"])
def test_the_protocol_append_keeps_the_instructions_file_bytes(
    family: str,
    app_config: AppConfig,
    workspace: Path,
) -> None:
    """Rewriting the whole instructions file normalised its line endings too."""
    raw = _with_endings(b"# My project\n\nExisting guidance.", family)
    plan = plan_client(ClientKind.CLAUDE, app_config, home=workspace)
    plan.instructions_path.write_bytes(raw)

    assert install_protocol(plan) is True
    assert install_protocol(plan) is False

    written = plan.instructions_path.read_bytes()
    assert written.startswith(raw)
    assert written.count(b"Engram session protocol") == 1


def test_a_target_that_changed_after_it_was_read_is_refused(
    app_config: AppConfig,
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The content is a function of what was read, so writing it over something else loses an edit.

    The bytes are compared through the descriptor that will do the writing, not
    by looking the path up again: a second existence test reclassified the
    situation instead of checking it.
    """
    plan = plan_client(ClientKind.CODEX, app_config, home=workspace)
    plan.instructions_path.write_bytes(b"# original\n")
    concurrent = b"# original\n# an edit from somewhere else\n"
    genuine = _read_optional_bytes

    def racing_read(path: Path) -> object:
        seen = genuine(path)
        if path == plan.instructions_path and seen is not None and seen.data == b"# original\n":
            path.write_bytes(concurrent)
        return seen

    monkeypatch.setattr(clients_module, "_read_optional_bytes", racing_read)

    with pytest.raises(ClientConfigError, match="changed after Engram read it"):
        install_protocol(plan)

    assert plan.instructions_path.read_bytes() == concurrent


def test_a_file_appearing_where_none_was_found_is_refused(
    app_config: AppConfig,
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Absent is a state too: a newcomer must not be adopted and overwritten."""
    plan = plan_client(ClientKind.CODEX, app_config, home=workspace)
    intruder = b"# created between the read and the write\n"
    genuine = _read_optional_bytes

    def racing_read(path: Path) -> object:
        seen = genuine(path)
        if path == plan.instructions_path and seen is None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(intruder)
        return seen

    monkeypatch.setattr(clients_module, "_read_optional_bytes", racing_read)

    with pytest.raises(ClientConfigError, match="appeared after Engram"):
        install_protocol(plan)

    assert plan.instructions_path.read_bytes() == intruder


def test_a_hard_link_appearing_where_none_was_found_never_reaches_its_target(
    app_config: AppConfig,
    workspace: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same race, aimed: the newcomer is another name for somebody else's file."""
    plan = plan_client(ClientKind.CODEX, app_config, home=workspace)
    victim = tmp_path / "victim.txt"
    victim.write_bytes(b"THIRD PARTY CONTENT\n")
    genuine = _read_optional_bytes

    def racing_read(path: Path) -> object:
        seen = genuine(path)
        if path == plan.instructions_path and seen is None:
            path.parent.mkdir(parents=True, exist_ok=True)
            os.link(victim, path)
        return seen

    monkeypatch.setattr(clients_module, "_read_optional_bytes", racing_read)

    with pytest.raises(ClientConfigError, match="appeared after Engram"):
        install_protocol(plan)

    assert victim.read_bytes() == b"THIRD PARTY CONTENT\n"


def test_a_configuration_written_between_planning_and_connecting_is_merged_not_replaced(
    app_config: AppConfig,
    workspace: Path,
) -> None:
    """A plan is not a promise about the file; the write reads it again and merges."""
    plan = plan_client(ClientKind.GEMINI, app_config, home=workspace)
    plan.config_path.parent.mkdir(parents=True, exist_ok=True)
    plan.config_path.write_bytes(
        b'{"theme":"dark","mcpServers":{"datacron":{"httpUrl":"http://x/mcp"}}}'
    )

    connect(plan, force=False)

    document = json.loads(plan.config_path.read_bytes())
    assert document["theme"] == "dark"
    assert document["mcpServers"]["datacron"] == {"httpUrl": "http://x/mcp"}
    assert document["mcpServers"]["engram"]["httpUrl"] == endpoint_url(app_config)


def test_a_target_repointed_at_another_file_with_the_same_bytes_is_refused(
    app_config: AppConfig,
    workspace: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Identical content is not identity: two files can hold the same bytes.

    Comparing only the bytes accepted a destination repointed at somebody else's
    file in between, so the comparison passed and the write landed on the wrong
    inode. The identity is taken from the descriptor that was read and required
    again on the descriptor that writes.
    """
    shared = b"# shared content\n"
    plan = plan_client(ClientKind.CODEX, app_config, home=workspace)
    plan.instructions_path.write_bytes(shared)
    victim = tmp_path / "victim.md"
    victim.write_bytes(shared)
    genuine = _read_optional_bytes

    def racing_read(path: Path) -> object:
        seen = genuine(path)
        if path == plan.instructions_path and seen is not None:
            # Same bytes, different file: only the identity separates them.
            path.unlink()
            os.link(victim, path)
        return seen

    monkeypatch.setattr(clients_module, "_read_optional_bytes", racing_read)

    with pytest.raises(ClientConfigError, match="no longer the file Engram read"):
        install_protocol(plan)

    assert victim.read_bytes() == shared, "the victim received the write"
    assert plan.instructions_path.read_bytes() == shared
    assert plan.instructions_path.samefile(victim)


@pytest.mark.parametrize("kind", list(ClientKind))
def test_a_stale_plan_never_reports_already_correct_over_a_changed_endpoint(
    kind: ClientKind,
    app_config: AppConfig,
    workspace: Path,
) -> None:
    """A plan describes the file when it was made; acting on that is a false no-op.

    Connecting, then having somebody point the entry elsewhere, then replaying
    the old plan reported "Already correct" and wrote nothing, leaving the client
    aimed at an endpoint that is not this Engram.
    """
    connect(plan_client(kind, app_config, home=workspace), force=False)
    stale = plan_client(kind, app_config, home=workspace)
    assert stale.already_correct is True

    text = stale.config_path.read_text(encoding="utf-8")
    stale.config_path.write_bytes(
        text.replace(endpoint_url(app_config), "http://127.0.0.1:9999/elsewhere").encode("utf-8")
    )

    if kind is ClientKind.CODEX:
        # Codex tables are never rewritten in place, so the honest answer is a
        # refusal that names the conflict rather than a silent no-op.
        with pytest.raises(ClientConfigError, match="already declares"):
            connect(stale, force=False)
    else:
        assert connect(stale, force=True) is True
        assert endpoint_url(app_config) in stale.config_path.read_text(encoding="utf-8")


@pytest.mark.parametrize("kind", list(ClientKind))
def test_a_stale_plan_never_reports_already_correct_over_an_unreadable_file(
    kind: ClientKind,
    app_config: AppConfig,
    workspace: Path,
) -> None:
    """A file that stopped parsing must produce a refusal, not a reported success."""
    connect(plan_client(kind, app_config, home=workspace), force=False)
    stale = plan_client(kind, app_config, home=workspace)
    assert stale.already_correct is True

    stale.config_path.write_bytes(b"{ this is neither valid JSON nor valid TOML")

    with pytest.raises(ClientConfigError):
        connect(stale, force=True)
