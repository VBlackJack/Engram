# Copyright 2026 Julien Bombled
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import sqlite3
from collections import deque
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import replace

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.types import Implementation
from starlette.applications import Starlette
from starlette.types import Message, Receive, Scope, Send

from engram import __version__
from engram.config import MAX_FTS_QUERY_CHARS, AppConfig, LimitsConfig
from engram.db import DatabaseError
from engram.models import Evidence, EvidenceType
from engram.normalization import (
    HARD_MAX_STATEMENT_CHARS,
    HARD_MAX_SUBJECT_KEYS,
    MAX_EVIDENCE_ITEMS,
    MAX_EVIDENCE_REF_CHARS,
    MAX_SCOPE_CHARS,
    MAX_SUBJECT_KEY_CHARS,
    MAX_WRITER_MODEL_CHARS,
    normalize_mcp_client_component,
)
from engram.server import (
    MAX_CLIENT_COMPONENT_CHARS,
    RequestBodyLimitMiddleware,
    create_mcp_server,
    publish_tool_schemas,
)
from engram.store import EngramStore, StoreValidationError


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class _RecordingApp:
    def __init__(self) -> None:
        self.called = False
        self.received_message: Message | None = None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        del scope
        self.called = True
        self.received_message = await receive()
        await send(
            {
                "type": "http.response.start",
                "status": 204,
                "headers": [],
            }
        )
        await send({"type": "http.response.body", "body": b""})


MCP_OPENING_HEADERS: tuple[tuple[bytes, bytes], ...] = (
    (b"accept", b"application/json, text/event-stream"),
    (b"content-type", b"application/json"),
)


async def _invoke_body_middleware(
    *,
    max_body_bytes: int,
    headers: tuple[tuple[bytes, bytes], ...] = (),
    messages: tuple[Message, ...] = (),
) -> tuple[_RecordingApp, list[Message], int]:
    downstream = _RecordingApp()
    queued = deque(messages)
    receive_calls = 0
    sent: list[Message] = []

    async def receive() -> Message:
        nonlocal receive_calls
        receive_calls += 1
        if not queued:
            raise AssertionError("middleware unexpectedly requested another ASGI message")
        return queued.popleft()

    async def send(message: Message) -> None:
        sent.append(message)

    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/mcp",
        "raw_path": b"/mcp",
        "query_string": b"",
        "root_path": "",
        "headers": [*MCP_OPENING_HEADERS, *headers],
        "client": ("127.0.0.1", 50000),
        "server": ("127.0.0.1", 8377),
        "state": {},
    }
    middleware = RequestBodyLimitMiddleware(downstream, max_body_bytes=max_body_bytes)
    await middleware(scope, receive, send)
    return downstream, sent, receive_calls


@asynccontextmanager
async def _mcp_client(
    app: Starlette,
    *,
    name: str = "test-client",
    version: str = "1.0",
) -> AsyncIterator[ClientSession]:
    transport = httpx.ASGITransport(app=app)
    async with (
        httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8377") as client,
        streamable_http_client(
            "http://127.0.0.1:8377/mcp",
            http_client=client,
        ) as (read_stream, write_stream, _),
        ClientSession(
            read_stream,
            write_stream,
            client_info=Implementation(name=name, version=version),
        ) as session,
    ):
        initialization = await session.initialize()
        assert initialization.serverInfo.version == __version__
        yield session


def _hard_limit_config(app_config: AppConfig) -> AppConfig:
    return replace(
        app_config,
        limits=LimitsConfig(
            max_statement_chars=HARD_MAX_STATEMENT_CHARS,
            max_subject_keys=HARD_MAX_SUBJECT_KEYS,
        ),
    )


def test_public_argument_schemas_publish_every_hard_input_limit(app_config: AppConfig) -> None:
    """The published bounds are the ones this server enforces, not the ones it may be given.

    `statement` and `subject_keys` are configurable, so they are published from
    the configuration: advertising the hard ceiling made a client compose calls
    the server then refused, on exactly the long rationales worth keeping. Every
    other bound here is fixed, so its published value is the constant.
    """
    schemas = publish_tool_schemas(app_config)
    remember_properties = schemas["remember"]["properties"]
    recall_properties = schemas["recall"]["properties"]

    assert remember_properties["statement"]["maxLength"] == app_config.limits.max_statement_chars
    assert remember_properties["scope"]["maxLength"] == MAX_SCOPE_CHARS
    assert remember_properties["subject_keys"]["maxItems"] == app_config.limits.max_subject_keys
    assert remember_properties["subject_keys"]["items"]["maxLength"] == MAX_SUBJECT_KEY_CHARS
    assert remember_properties["evidence"]["maxItems"] == MAX_EVIDENCE_ITEMS
    assert remember_properties["evidence"]["items"]["properties"]["ref"]["maxLength"] == (
        MAX_EVIDENCE_REF_CHARS
    )
    assert recall_properties["query"]["maxLength"] == MAX_FTS_QUERY_CHARS
    assert recall_properties["scope"]["maxLength"] == MAX_SCOPE_CHARS
    assert recall_properties["scope"]["type"] == ["string", "null"]
    assert recall_properties["kinds"]["maxItems"] == 5


@pytest.mark.anyio
async def test_body_limit_rejects_declared_oversize_without_reading() -> None:
    downstream, sent, receive_calls = await _invoke_body_middleware(
        max_body_bytes=8,
        headers=((b"content-length", b"9"),),
    )

    assert downstream.called is False
    assert receive_calls == 0
    assert sent[0]["status"] == 413
    assert sent[1]["body"] == b"request body too large"


@pytest.mark.anyio
async def test_body_limit_rejects_chunked_actual_oversize() -> None:
    downstream, sent, receive_calls = await _invoke_body_middleware(
        max_body_bytes=8,
        messages=(
            {"type": "http.request", "body": b"12345678", "more_body": True},
            {"type": "http.request", "body": b"9", "more_body": False},
        ),
    )

    assert downstream.called is False
    assert receive_calls == 2
    assert sent[0]["status"] == 413


@pytest.mark.anyio
async def test_body_limit_rejects_giant_numeric_content_length_without_int_conversion() -> None:
    downstream, sent, receive_calls = await _invoke_body_middleware(
        max_body_bytes=8,
        headers=((b"content-length", b"9" * 5000),),
    )

    assert downstream.called is False
    assert receive_calls == 0
    assert sent[0]["status"] == 413


@pytest.mark.anyio
async def test_body_limit_accepts_and_replays_exact_boundary_once() -> None:
    downstream, sent, receive_calls = await _invoke_body_middleware(
        max_body_bytes=8,
        headers=((b"content-length", b"8"),),
        messages=(
            {"type": "http.request", "body": b'{"a', "more_body": True},
            {"type": "http.request", "body": b'b":1}', "more_body": False},
        ),
    )

    assert downstream.called is True
    assert receive_calls == 2
    assert downstream.received_message == {
        "type": "http.request",
        "body": b'{"ab":1}',
        "more_body": False,
    }
    assert sent[0]["status"] == 204


@pytest.mark.anyio
@pytest.mark.parametrize(
    "invalid_name",
    [
        "",
        " padded",
        "client\u0085forged",
        "client\u2028forged",
        "client\u202eforged",
        "n" * (MAX_CLIENT_COMPONENT_CHARS + 1),
    ],
    ids=[
        "empty",
        "surrounding-whitespace",
        "c1-control",
        "line-separator",
        "bidi-control",
        "overlong",
    ],
)
async def test_mcp_rejects_invalid_client_and_field_without_writing(
    app_config: AppConfig,
    sqlite_runtime_compatibility: None,
    invalid_name: str,
) -> None:
    del sqlite_runtime_compatibility
    with EngramStore(app_config) as store:
        server = create_mcp_server(app_config, store)
        app = server.streamable_http_app()
        async with app.router.lifespan_context(app):
            async with _mcp_client(
                app,
                name=invalid_name,
            ) as session:
                rejected_client = await session.call_tool(
                    "remember",
                    {"statement": "Must not be written.", "kind": "fact"},
                )
            async with _mcp_client(app) as session:
                rejected_field = await session.call_tool(
                    "remember",
                    {
                        "statement": "Must not be written either.",
                        "kind": "fact",
                        "evidence": [
                            {
                                "type": "tool_result",
                                "ref": "r" * (MAX_EVIDENCE_REF_CHARS + 1),
                            }
                        ],
                    },
                )

        assert rejected_client.isError is True
        assert rejected_field.isError is True
        assert store.count_entries() == 0
        assert store.list_audit() == ()


def test_mcp_identity_normalizer_rejects_unicode_surrogates() -> None:
    with pytest.raises(StoreValidationError, match="surrogate"):
        normalize_mcp_client_component("\ud800", "client name")


@pytest.mark.anyio
async def test_mcp_client_identity_domain_separates_slash_and_percent(
    app_config: AppConfig,
    sqlite_runtime_compatibility: None,
) -> None:
    del sqlite_runtime_compatibility
    identities = (
        ("a/b", "c"),
        ("a", "b/c"),
        ("a%2Fb", "c"),
    )
    arguments = {
        "statement": "Equivalent observations retain distinct client ownership.",
        "kind": "fact",
    }

    with EngramStore(app_config) as store:
        server = create_mcp_server(app_config, store)
        app = server.streamable_http_app()
        async with app.router.lifespan_context(app):
            for name, version in identities:
                async with _mcp_client(app, name=name, version=version) as session:
                    result = await session.call_tool("remember", arguments)
                    assert result.isError is False

        entries = store.list_entries()
        assert len(entries) == len(identities)
        writers = {entry.writer_model for entry in entries}
        assert len(writers) == len(identities)
        assert all(writer is not None and writer.startswith("mcp-v2:") for writer in writers)
        assert all("/" not in writer for writer in writers if writer is not None)
        assert "a%2Fb/c" not in writers


@pytest.mark.anyio
async def test_mcp_transports_maximum_unicode_statement(
    app_config: AppConfig,
    sqlite_runtime_compatibility: None,
) -> None:
    del sqlite_runtime_compatibility
    config = _hard_limit_config(app_config)
    statement = "😀" * HARD_MAX_STATEMENT_CHARS

    with EngramStore(config) as store:
        server = create_mcp_server(config, store)
        app = server.streamable_http_app()
        async with (
            app.router.lifespan_context(app),
            _mcp_client(app) as session,
        ):
            result = await session.call_tool(
                "remember",
                {"statement": statement, "kind": "fact"},
            )

        assert result.isError is False
        entries = store.list_entries()
        assert len(entries) == 1
        assert entries[0].statement == statement


def test_store_accepts_every_exact_hard_boundary(
    app_config: AppConfig,
    sqlite_runtime_compatibility: None,
) -> None:
    del sqlite_runtime_compatibility
    config = _hard_limit_config(app_config)
    subject_keys = tuple(
        f"{index:02d}" + "k" * (MAX_SUBJECT_KEY_CHARS - 2) for index in range(HARD_MAX_SUBJECT_KEYS)
    )
    evidence = tuple(
        Evidence(
            type=EvidenceType.TOOL_RESULT,
            ref=f"{index:02d}" + "r" * (MAX_EVIDENCE_REF_CHARS - 2),
        )
        for index in range(MAX_EVIDENCE_ITEMS)
    )

    with EngramStore(config) as store:
        entry = store.add_candidate(
            kind="fact",
            scope="project/" + "a" * (MAX_SCOPE_CHARS - len("project/")),
            statement="s" * HARD_MAX_STATEMENT_CHARS,
            writer_model="w" * MAX_WRITER_MODEL_CHARS,
            subject_keys=subject_keys,
            evidence=evidence,
        )

        assert len(entry.scope) == MAX_SCOPE_CHARS
        assert len(entry.statement) == HARD_MAX_STATEMENT_CHARS
        assert len(entry.writer_model or "") == MAX_WRITER_MODEL_CHARS
        assert len(entry.subject_keys) == HARD_MAX_SUBJECT_KEYS
        assert all(len(item) == MAX_SUBJECT_KEY_CHARS for item in entry.subject_keys)
        assert len(entry.evidence) == MAX_EVIDENCE_ITEMS
        assert all(len(item.ref) == MAX_EVIDENCE_REF_CHARS for item in entry.evidence)

    with EngramStore(config) as reopened:
        persisted = reopened.get_entry(entry.id)
        assert persisted is not None
        assert persisted.writer_model == "w" * MAX_WRITER_MODEL_CHARS


def test_store_rejects_every_overlong_direct_input_before_mutation(
    app_config: AppConfig,
    sqlite_runtime_compatibility: None,
) -> None:
    del sqlite_runtime_compatibility
    config = _hard_limit_config(app_config)

    with EngramStore(config) as store:
        invalid_calls: tuple[tuple[str, Callable[[], object]], ...] = (
            (
                "statement",
                lambda: store.add_candidate(
                    kind="fact",
                    scope="user",
                    statement="s" * (HARD_MAX_STATEMENT_CHARS + 1),
                    writer_model="writer",
                ),
            ),
            (
                "surrogate",
                lambda: store.add_candidate(
                    kind="fact",
                    scope="user",
                    statement="\ud800",
                    writer_model="writer",
                ),
            ),
            (
                "U\\+0000",
                lambda: store.add_candidate(
                    kind="fact",
                    scope="user",
                    statement="invalid\0statement",
                    writer_model="writer",
                ),
            ),
            (
                "scope",
                lambda: store.add_candidate(
                    kind="fact",
                    scope="project/" + "a" * (MAX_SCOPE_CHARS - len("project/") + 1),
                    statement="valid",
                    writer_model="writer",
                ),
            ),
            (
                "subject key",
                lambda: store.add_candidate(
                    kind="fact",
                    scope="user",
                    statement="valid",
                    writer_model="writer",
                    subject_keys=("k" * (MAX_SUBJECT_KEY_CHARS + 1),),
                ),
            ),
            (
                "subject key",
                lambda: store.add_candidate(
                    kind="fact",
                    scope="user",
                    statement="valid",
                    writer_model="writer",
                    subject_keys=("ẞ" * MAX_SUBJECT_KEY_CHARS,),
                ),
            ),
            (
                "subject_keys",
                lambda: store.add_candidate(
                    kind="fact",
                    scope="user",
                    statement="valid",
                    writer_model="writer",
                    subject_keys=tuple(
                        f"key-{index}" for index in range(HARD_MAX_SUBJECT_KEYS + 1)
                    ),
                ),
            ),
            (
                "evidence ref",
                lambda: store.add_candidate(
                    kind="fact",
                    scope="user",
                    statement="valid",
                    writer_model="writer",
                    evidence=(
                        Evidence(
                            type=EvidenceType.TOOL_RESULT,
                            ref="r" * (MAX_EVIDENCE_REF_CHARS + 1),
                        ),
                    ),
                ),
            ),
            (
                "evidence",
                lambda: store.add_candidate(
                    kind="fact",
                    scope="user",
                    statement="valid",
                    writer_model="writer",
                    evidence=tuple(
                        Evidence(type=EvidenceType.TOOL_RESULT, ref=f"ref-{index}")
                        for index in range(MAX_EVIDENCE_ITEMS + 1)
                    ),
                ),
            ),
            (
                "writer_model",
                lambda: store.add_candidate(
                    kind="fact",
                    scope="user",
                    statement="valid",
                    writer_model="w" * (MAX_WRITER_MODEL_CHARS + 1),
                ),
            ),
        )

        for expected, invalid_call in invalid_calls:
            with pytest.raises(StoreValidationError, match=expected):
                invalid_call()
            assert store.count_entries() == 0
            assert store.list_audit() == ()


def test_reopen_rejects_forged_oversized_evidence_reference(
    app_config: AppConfig,
    sqlite_runtime_compatibility: None,
) -> None:
    del sqlite_runtime_compatibility
    with EngramStore(app_config) as store:
        entry = store.add_candidate(
            kind="fact",
            scope="user",
            statement="Persisted ownership remains bounded.",
            writer_model="writer",
        )

    forged_evidence = json.dumps(
        [
            {
                "type": EvidenceType.TOOL_RESULT.value,
                "ref": "r" * (MAX_EVIDENCE_REF_CHARS + 1),
            }
        ],
        separators=(",", ":"),
        sort_keys=True,
    )
    connection = sqlite3.connect(app_config.database.path)
    try:
        connection.execute(
            "UPDATE entries SET evidence = ? WHERE id = ?",
            (forged_evidence, entry.id),
        )
        connection.execute(
            "UPDATE entry_observations SET evidence = ? WHERE entry_id = ?",
            (forged_evidence, entry.id),
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(DatabaseError, match="evidence"):
        EngramStore(app_config)


def test_reopen_rejects_padded_audit_detail_hash(
    app_config: AppConfig,
    sqlite_runtime_compatibility: None,
) -> None:
    del sqlite_runtime_compatibility
    with EngramStore(app_config) as store:
        store.add_candidate(
            kind="fact",
            scope="user",
            statement="Audit hashes stay canonical.",
            writer_model="writer",
        )

    connection = sqlite3.connect(app_config.database.path)
    try:
        connection.execute(
            """
            UPDATE audit_log
            SET detail_hash = ' ' || detail_hash || ' '
            WHERE seq = (SELECT MIN(seq) FROM audit_log)
            """
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(DatabaseError, match="detail_hash is not normalized"):
        EngramStore(app_config)
