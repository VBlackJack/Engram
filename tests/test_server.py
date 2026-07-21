# Copyright 2026 Julien Bombled
# SPDX-License-Identifier: Apache-2.0

"""Full MCP SDK integration tests over streamable HTTP."""

from __future__ import annotations

import logging
import sqlite3
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from statistics import fmean
from typing import Any

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.types import CallToolResult, Implementation, TextContent
from starlette.applications import Starlette

from engram.config import AppConfig, CapsuleConfig, ServerConfig
from engram.db import MINIMUM_SQLITE_VERSION
from engram.models import EntryStatus, PromotionState, SourceType
from engram.server import create_mcp_server
from engram.store import EngramStore

LOGGER = logging.getLogger(__name__)


@pytest.fixture
def anyio_backend() -> str:
    """Run SDK integration tests on the asyncio backend used by the server."""
    return "asyncio"


@asynccontextmanager
async def _client(
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
        await session.initialize()
        yield session


def _structured(result: CallToolResult) -> dict[str, Any]:
    assert result.structuredContent is not None
    return result.structuredContent


@pytest.mark.anyio
async def test_remember_round_trip_is_strict_owned_and_idempotent(
    app_config: AppConfig,
) -> None:
    """Exercise discovery and duplicate writes through the public HTTP transport."""
    assert sqlite3.sqlite_version_info >= MINIMUM_SQLITE_VERSION
    with EngramStore(app_config) as store:
        server = create_mcp_server(app_config, store)
        app = server.streamable_http_app()
        async with (
            app.router.lifespan_context(app),
            _client(app, name="sdk-client", version="2.4") as session,
        ):
            tools = {tool.name: tool for tool in (await session.list_tools()).tools}
            assert set(tools) == {"remember", "recall"}
            assert tools["remember"].inputSchema["additionalProperties"] is False
            assert tools["recall"].inputSchema["additionalProperties"] is False
            assert "writer_model" not in tools["remember"].inputSchema["properties"]
            assert "status" not in tools["remember"].inputSchema["properties"]
            assert "source_type" not in tools["remember"].inputSchema["properties"]
            evidence_schema = tools["remember"].inputSchema["$defs"]["EvidenceInput"]
            assert evidence_schema["additionalProperties"] is False
            assert tools["remember"].annotations is not None
            assert tools["remember"].annotations.readOnlyHint is False
            assert tools["remember"].annotations.destructiveHint is False
            assert tools["remember"].annotations.idempotentHint is True
            assert tools["remember"].annotations.openWorldHint is False
            assert tools["recall"].annotations is not None
            assert tools["recall"].annotations.readOnlyHint is True
            assert tools["recall"].annotations.openWorldHint is False

            arguments = {
                "statement": "Keep provenance server-owned.",
                "kind": "decision",
                "scope": "project/Engram",
                "subject_keys": ["provenance"],
                "evidence": [{"type": "tool_result", "ref": "tool://result/1"}],
            }
            first = await session.call_tool("remember", arguments)
            second = await session.call_tool("remember", arguments)

            assert first.isError is False
            assert second.isError is False
            assert _structured(first)["status"] == "quarantined"
            assert _structured(first)["promotion_state"] == "candidate"
            assert _structured(first)["idempotent"] is False
            assert _structured(second)["idempotent"] is True
            assert _structured(first)["entry_id"] == _structured(second)["entry_id"]
            assert store.count_entries() == 1
            stored = store.list_entries()[0]
            assert stored.writer_model == "sdk-client/2.4"
            assert stored.status is EntryStatus.QUARANTINED
            assert stored.promotion_state is PromotionState.CANDIDATE
            assert stored.source_type is SourceType.MODEL_INFERRED

            rejected = await session.call_tool(
                "remember",
                {
                    "statement": "Attempt to control status.",
                    "kind": "fact",
                    "status": "active",
                    "source_type": "human",
                },
            )
            assert rejected.isError is True
            assert store.count_entries() == 1


@pytest.mark.anyio
async def test_writer_quarantine_is_visible_only_to_the_same_client(
    app_config: AppConfig,
) -> None:
    """Keep model candidates private while exposing a labelled own-pending section."""
    with EngramStore(app_config) as store:
        server = create_mcp_server(app_config, store)
        app = server.streamable_http_app()
        async with app.router.lifespan_context(app):
            async with _client(app, name="client-a") as client_a:
                remembered = await client_a.call_tool(
                    "remember",
                    {"statement": "Private candidate alpha", "kind": "fact"},
                )
                entry_id = str(_structured(remembered)["entry_id"])
                own = await client_a.call_tool("recall", {"query": "alpha"})
                own_capsule = _structured(own)
                assert [item["id"] for item in own_capsule["own_pending"]] == [entry_id]
                assert own_capsule["own_pending"][0]["label"] == "unconfirmed candidate"

            async with _client(app, name="client-b") as client_b:
                other = await client_b.call_tool("recall", {"query": "alpha"})
                other_capsule = _structured(other)
                assert other_capsule["current"] == []
                assert other_capsule["relevant"] == []
                assert other_capsule["own_pending"] == []


@pytest.mark.anyio
async def test_remember_backpressure_returns_retry_before_twice_the_timeout(
    app_config: AppConfig,
) -> None:
    """Bound writer contention without adding a write queue."""
    timeout_ms = 300
    config = replace(
        app_config,
        server=ServerConfig(write_wait_timeout_ms=timeout_ms),
    )
    with EngramStore(config) as store:
        server = create_mcp_server(config, store)
        app = server.streamable_http_app()
        async with app.router.lifespan_context(app), _client(app) as session:
            with store.write_access():
                started = time.perf_counter()
                result = await session.call_tool(
                    "remember",
                    {"statement": "Contended write", "kind": "episode"},
                )
                elapsed = time.perf_counter() - started

            assert result.isError is True
            assert elapsed < (2 * timeout_ms / 1000)
            assert result.content
            content = result.content[0]
            assert isinstance(content, TextContent)
            assert "server busy, retry" in content.text
            assert store.count_entries() == 0


@pytest.mark.anyio
async def test_recall_places_trusted_entries_and_excludes_superseded_versions(
    app_config: AppConfig,
) -> None:
    """Apply scope, recency, section, provenance, and supersession policy."""
    with EngramStore(app_config) as store:
        old = store.add_attested(
            kind="fact",
            scope="project/engram",
            statement="Theme color was blue.",
            source_type=SourceType.HUMAN,
            subject_keys=("theme/color",),
        )
        current = store.add_attested(
            kind="fact",
            scope="project/engram",
            statement="Theme color is green.",
            source_type=SourceType.TOOL_VERIFIED,
            subject_keys=("theme/color",),
        )
        store.supersede(old.id, current.id)
        next_action = store.add_attested(
            kind="project_state",
            scope="project/engram",
            statement="Deploy the updated palette next.",
            source_type=SourceType.HUMAN,
            subject_keys=("deployment",),
        )
        relevant = store.add_attested(
            kind="episode",
            scope="project/engram",
            statement="The team discussed the color migration.",
            source_type=SourceType.HUMAN,
            subject_keys=("meeting/color",),
        )
        store.add_attested(
            kind="fact",
            scope="project/other",
            statement="Theme color is orange.",
            source_type=SourceType.HUMAN,
        )

        server = create_mcp_server(app_config, store)
        app = server.streamable_http_app()
        async with app.router.lifespan_context(app), _client(app) as session:
            result = await session.call_tool(
                "recall",
                {"query": "color", "scope": "project/engram"},
            )

        assert result.isError is False
        capsule = _structured(result)
        assert [item["id"] for item in capsule["current"]] == [current.id]
        assert [item["id"] for item in capsule["next_action"]] == [next_action.id]
        assert [item["id"] for item in capsule["relevant"]] == [relevant.id]
        assert old.id not in capsule["sources"]
        assert set(capsule["sources"]) == {current.id, next_action.id, relevant.id}
        assert capsule["notes"]["scope_used"] == "project/engram"
        assert result.content
        fallback = result.content[0]
        assert isinstance(fallback, TextContent)
        headings = [
            "CURRENT",
            "NEXT_ACTION",
            "RELEVANT",
            "CONFLICTS",
            "OWN_PENDING",
            "SOURCES",
            "NOTES",
        ]
        assert [fallback.text.index(heading) for heading in headings] == sorted(
            fallback.text.index(heading) for heading in headings
        )


@pytest.mark.anyio
async def test_recall_renders_conflicts_only_when_requested(
    app_config: AppConfig,
) -> None:
    """Keep unresolved versions symmetric and out of the current section."""
    with EngramStore(app_config) as store:
        first = store.add_attested(
            kind="fact",
            scope="user",
            statement="The editor theme is light.",
            source_type=SourceType.HUMAN,
            subject_keys=("editor/theme",),
        )
        second = store.add_attested(
            kind="fact",
            scope="user",
            statement="The editor theme is dark.",
            source_type=SourceType.TOOL_VERIFIED,
            subject_keys=("editor/theme",),
        )
        server = create_mcp_server(app_config, store)
        app = server.streamable_http_app()
        async with app.router.lifespan_context(app), _client(app) as session:
            hidden = await session.call_tool(
                "recall",
                {"query": "light", "include_conflicts": False},
            )
            shown = await session.call_tool(
                "recall",
                {"query": "light", "include_conflicts": True},
            )

        hidden_capsule = _structured(hidden)
        shown_capsule = _structured(shown)
        assert hidden_capsule["current"] == []
        assert hidden_capsule["conflicts"] == []
        assert shown_capsule["current"] == []
        assert len(shown_capsule["conflicts"]) == 1
        conflict = shown_capsule["conflicts"][0]
        assert conflict["status"] == "unresolved"
        assert {item["id"] for item in conflict["versions"]} == {first.id, second.id}


@pytest.mark.anyio
async def test_recall_budget_omits_whole_entries_and_validates_bounds(
    app_config: AppConfig,
) -> None:
    """Enforce the conservative character budget without truncating statements."""
    config = replace(
        app_config,
        capsule=CapsuleConfig(
            default_token_budget=150,
            min_token_budget=100,
            max_token_budget=300,
        ),
    )
    statements = [
        f"Budget memory {index}: " + (f"complete-{index} " * 20).strip() for index in range(4)
    ]
    with EngramStore(config) as store:
        for index, statement in enumerate(statements):
            store.add_attested(
                kind="fact",
                scope="user",
                statement=statement,
                source_type=SourceType.HUMAN,
                subject_keys=(f"budget/{index}",),
            )
        server = create_mcp_server(config, store)
        app = server.streamable_http_app()
        async with app.router.lifespan_context(app), _client(app) as session:
            result = await session.call_tool(
                "recall",
                {"query": "budget", "token_budget": 100},
            )
            rejected = await session.call_tool(
                "recall",
                {"query": "budget", "token_budget": 99},
            )

        assert result.isError is False
        assert rejected.isError is True
        assert result.content
        fallback = result.content[0]
        assert isinstance(fallback, TextContent)
        assert (len(fallback.text) + 3) // 4 <= 100
        assert "entries omitted, budget" in fallback.text
        for statement in statements:
            prefix = statement[: len(statement) // 2]
            if prefix in fallback.text:
                assert statement in fallback.text


@pytest.mark.anyio
async def test_mcp_average_latency(app_config: AppConfig) -> None:
    """Record comparable in-process HTTP averages for the delivery report."""
    remember_latencies: list[float] = []
    recall_latencies: list[float] = []
    with EngramStore(app_config) as store:
        server = create_mcp_server(app_config, store)
        app = server.streamable_http_app()
        async with (
            app.router.lifespan_context(app),
            _client(app, name="latency-client") as session,
        ):
            for index in range(10):
                started = time.perf_counter()
                result = await session.call_tool(
                    "remember",
                    {
                        "statement": f"Latency memory {index}",
                        "kind": "episode",
                        "subject_keys": [f"latency/{index}"],
                    },
                )
                remember_latencies.append((time.perf_counter() - started) * 1000)
                assert result.isError is False

            for _ in range(10):
                started = time.perf_counter()
                result = await session.call_tool("recall", {"query": "latency"})
                recall_latencies.append((time.perf_counter() - started) * 1000)
                assert result.isError is False

    LOGGER.info(
        "MCP average latency: remember=%.3f ms, recall=%.3f ms",
        fmean(remember_latencies),
        fmean(recall_latencies),
    )
