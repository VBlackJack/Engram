# Copyright 2026 Julien Bombled
# SPDX-License-Identifier: Apache-2.0

"""Full MCP SDK integration tests over streamable HTTP."""

from __future__ import annotations

import json
import logging
import math
import sqlite3
import time
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import replace
from statistics import fmean
from typing import Any

import anyio
import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.server.fastmcp import FastMCP
from mcp.server.streamable_http import StreamableHTTPServerTransport
from mcp.types import CallToolResult, Implementation, TextContent
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.types import Message

import engram.server as server_module
import engram.store as store_module
from engram import __version__
from engram.capsule import CapsuleResult, estimate_capsule_bytes
from engram.config import (
    AppConfig,
    CapsuleConfig,
    RetrievalConfig,
    RetrievalMode,
    ServerConfig,
)
from engram.db import MINIMUM_SQLITE_VERSION
from engram.embeddings import EmbeddingError
from engram.models import EntryStatus, PromotionState, SourceType
from engram.retrieval import NOTICE_FTS_QUERY_TIMEOUT, HybridRetriever
from engram.server import create_mcp_server
from engram.store import EngramStore
from tests.conftest import MutableClock

LOGGER = logging.getLogger(__name__)


class FailingEmbeddingProvider:
    """Provider used to prove that derived indexing cannot fail a write."""

    def embed(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        del texts
        raise EmbeddingError("mock endpoint unavailable")


class ProgressTimeoutClock:
    """Cross an FTS deadline only when SQLite invokes its progress callback."""

    def __init__(self) -> None:
        """Start before the synthetic deadline."""
        self.calls = 0

    def __call__(self) -> float:
        self.calls += 1
        return 0.0 if self.calls <= 2 else 1.0


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
        initialization = await session.initialize()
        assert initialization.serverInfo.version == __version__
        yield session


def _structured(result: CallToolResult) -> dict[str, Any]:
    assert result.structuredContent is not None
    return result.structuredContent


def _fallback_text(result: CallToolResult) -> str:
    assert len(result.content) == 1
    content = result.content[0]
    assert isinstance(content, TextContent)
    return content.text


@pytest.mark.anyio
async def test_remember_round_trip_is_strict_owned_and_idempotent(  # noqa: PLR0915
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
            evidence_schema = tools["remember"].inputSchema["properties"]["evidence"]["items"]
            assert evidence_schema["additionalProperties"] is False
            output_schema = tools["remember"].outputSchema
            assert output_schema is not None
            assert set(output_schema["required"]) == {
                "entry_id",
                "status",
                "promotion_state",
                "expires_at",
                "idempotent",
                "outcome",
            }
            assert output_schema["$defs"]["RememberOutcome"]["enum"] == [
                "created",
                "retry",
                "corroborated",
                "existing_trusted",
                "renewed",
            ]
            recall_output_schema = tools["recall"].outputSchema
            assert recall_output_schema is not None
            assert "claim_key" in recall_output_schema["$defs"]["ConflictItem"]["required"]
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
            corroborated = await session.call_tool(
                "remember",
                {
                    **arguments,
                    "evidence": [{"type": "tool_result", "ref": "tool://result/2"}],
                },
            )

            assert first.isError is False
            assert second.isError is False
            assert corroborated.isError is False
            assert _structured(first)["status"] == "quarantined"
            assert _structured(first)["promotion_state"] == "candidate"
            assert _structured(first)["idempotent"] is False
            assert _structured(first)["outcome"] == "created"
            assert _structured(second)["idempotent"] is True
            assert _structured(second)["outcome"] == "retry"
            assert _structured(corroborated)["outcome"] == "corroborated"
            assert _structured(corroborated)["idempotent"] is False
            assert _structured(first)["entry_id"] == _structured(second)["entry_id"]
            assert _structured(first)["entry_id"] == _structured(corroborated)["entry_id"]
            assert "unconfirmed candidate" in _fallback_text(first)
            assert "Resolved retry" in _fallback_text(second)
            assert "Recorded corroboration" in _fallback_text(corroborated)
            assert store.count_entries() == 1
            stored = store.list_entries()[0]
            assert stored.writer_model == "sdk-client/2.4"
            assert stored.status is EntryStatus.QUARANTINED
            assert stored.promotion_state is PromotionState.CANDIDATE
            assert stored.source_type is SourceType.MODEL_INFERRED
            assert len(store.list_observations(stored.id)) == 2

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
async def test_remember_reports_trusted_match_and_terminal_renewal_over_http(
    app_config: AppConfig,
    clock: MutableClock,
) -> None:
    with EngramStore(app_config, clock=clock) as store:
        trusted = store.add_attested(
            kind="fact",
            scope="user",
            statement="The verified endpoint is stable.",
            source_type=SourceType.TOOL_VERIFIED,
            claim_key="endpoint/stability",
        )
        terminal = store.add_candidate(
            kind="episode",
            scope="session/retry",
            statement="The terminal observation can be renewed.",
            writer_model="sdk-client/2.4",
        )
        assert terminal.expires_at is not None
        clock.current = terminal.expires_at

        server = create_mcp_server(app_config, store)
        app = server.streamable_http_app()
        async with (
            app.router.lifespan_context(app),
            _client(app, name="sdk-client", version="2.4") as session,
        ):
            trusted_result = await session.call_tool(
                "remember",
                {
                    "statement": "The verified endpoint is stable.",
                    "kind": "fact",
                },
            )
            renewed_result = await session.call_tool(
                "remember",
                {
                    "statement": "The terminal observation can be renewed.",
                    "kind": "episode",
                    "scope": "session/retry",
                },
            )

        assert _structured(trusted_result)["outcome"] == "existing_trusted"
        assert _structured(trusted_result)["entry_id"] == trusted.id
        assert _structured(trusted_result)["status"] == "active"
        assert "Found trusted entry" in _fallback_text(trusted_result)
        assert _structured(renewed_result)["outcome"] == "renewed"
        assert _structured(renewed_result)["entry_id"] != terminal.id
        assert _structured(renewed_result)["status"] == "quarantined"
        assert "Renewed terminal content" in _fallback_text(renewed_result)
        expired = store.get_entry(terminal.id)
        assert expired is not None
        assert expired.status is EntryStatus.EXPIRED


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
async def test_recall_timeout_returns_structured_incomplete_success(
    app_config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    progress_clock = ProgressTimeoutClock()
    monkeypatch.setattr(store_module, "SQLITE_PROGRESS_HANDLER_STEPS", 1)
    with EngramStore(app_config, monotonic_clock=progress_clock) as store:
        store.add_attested(
            kind="episode",
            scope="user",
            statement="Timeout capsule remains explicit.",
            source_type=SourceType.HUMAN,
        )
        server = create_mcp_server(app_config, store)
        app = server.streamable_http_app()
        async with app.router.lifespan_context(app), _client(app) as session:
            result = await session.call_tool(
                "recall",
                {"query": "timeout capsule"},
            )

        assert result.isError is False
        capsule = _structured(result)
        assert capsule["current"] == []
        assert capsule["relevant"] == []
        assert capsule["notes"]["recall_complete"] is False
        assert capsule["notes"]["warnings"] == [NOTICE_FTS_QUERY_TIMEOUT]
        assert NOTICE_FTS_QUERY_TIMEOUT in _fallback_text(result)
        store.add_attested(
            kind="episode",
            scope="user",
            statement="Connection remains writable after timeout.",
            source_type=SourceType.HUMAN,
        )


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
async def test_embedding_failure_does_not_fail_remember(
    app_config: AppConfig,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Commit the primary entry even when optional vectorization fails."""
    config = replace(
        app_config,
        retrieval=RetrievalConfig(
            mode=RetrievalMode.HYBRID,
            embeddings_model="mock-model",
        ),
    )
    with EngramStore(config) as store:
        retriever = HybridRetriever(
            store,
            config.retrieval,
            provider=FailingEmbeddingProvider(),
        )
        server = create_mcp_server(config, store, retriever=retriever)
        app = server.streamable_http_app()
        with caplog.at_level(logging.WARNING, logger="engram.retrieval"):
            async with app.router.lifespan_context(app), _client(app) as session:
                result = await session.call_tool(
                    "remember",
                    {"statement": "Committed without a vector", "kind": "fact"},
                )

        assert result.isError is False
        assert store.count_entries() == 1
        assert store.list_vectors("mock-model") == {}
        assert "stored without a vector" in caplog.text


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
            claim_key="theme/color",
        )
        current = store.add_attested(
            kind="fact",
            scope="project/engram",
            statement="Theme color is green.",
            source_type=SourceType.TOOL_VERIFIED,
            subject_keys=("theme/color",),
            claim_key="theme/color",
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
            claim_key="theme/color",
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
            kind="decision",
            scope="user",
            statement="The editor theme is light.",
            source_type=SourceType.HUMAN,
            subject_keys=("editor/theme",),
            claim_key="editor/theme",
        )
        second = store.add_attested(
            kind="decision",
            scope="user",
            statement="The editor theme is dark.",
            source_type=SourceType.TOOL_VERIFIED,
            subject_keys=("editor/theme",),
            claim_key="editor/theme",
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
        assert conflict["claim_key"] == "editor/theme"
        assert {item["id"] for item in conflict["versions"]} == {first.id, second.id}


@pytest.mark.anyio
async def test_recall_keeps_complementary_kinds_in_current(
    app_config: AppConfig,
) -> None:
    """Do not turn a shared subject across kinds into a conflict."""
    with EngramStore(app_config) as store:
        fact = store.add_attested(
            kind="fact",
            scope="project/engram",
            statement="The storage engine is SQLite.",
            source_type=SourceType.TOOL_VERIFIED,
            subject_keys=("storage/engine",),
            claim_key="storage/engine/fact",
        )
        decision = store.add_attested(
            kind="decision",
            scope="project/engram",
            statement="Keep the storage engine local.",
            source_type=SourceType.HUMAN,
            subject_keys=("storage/engine",),
            claim_key="storage/engine/decision",
        )
        server = create_mcp_server(app_config, store)
        app = server.streamable_http_app()
        async with app.router.lifespan_context(app), _client(app) as session:
            result = await session.call_tool(
                "recall",
                {
                    "query": "storage engine",
                    "scope": "project/engram",
                    "include_conflicts": True,
                },
            )

        capsule = _structured(result)
        assert {item["id"] for item in capsule["current"]} == {fact.id, decision.id}
        assert capsule["conflicts"] == []


@pytest.mark.anyio
async def test_recall_does_not_merge_conflicts_across_scopes(
    app_config: AppConfig,
) -> None:
    """Partition conflict families by scope even without a scope filter."""
    with EngramStore(app_config) as store:
        first = store.add_attested(
            kind="decision",
            scope="project/engram",
            statement="Shared theme stays green in Engram.",
            source_type=SourceType.HUMAN,
            subject_keys=("shared/theme",),
            claim_key="shared/theme",
        )
        second = store.add_attested(
            kind="decision",
            scope="project/other",
            statement="Shared theme stays blue elsewhere.",
            source_type=SourceType.HUMAN,
            subject_keys=("shared/theme",),
            claim_key="shared/theme",
        )
        server = create_mcp_server(app_config, store)
        app = server.streamable_http_app()
        async with app.router.lifespan_context(app), _client(app) as session:
            result = await session.call_tool(
                "recall",
                {"query": "shared theme", "include_conflicts": True},
            )

        capsule = _structured(result)
        assert {item["id"] for item in capsule["current"]} == {first.id, second.id}
        assert capsule["conflicts"] == []


@pytest.mark.anyio
async def test_recall_budget_omits_whole_entries_and_validates_bounds(
    app_config: AppConfig,
) -> None:
    """Enforce the serialized capsule budget without truncating statements."""
    config = replace(
        app_config,
        capsule=CapsuleConfig(
            default_token_budget=1200,
            min_token_budget=1200,
            max_token_budget=2400,
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
                claim_key=f"budget/{index}",
            )
        server = create_mcp_server(config, store)
        app = server.streamable_http_app()
        async with app.router.lifespan_context(app), _client(app) as session:
            result = await session.call_tool(
                "recall",
                {"query": "budget", "token_budget": 1200},
            )
            rejected = await session.call_tool(
                "recall",
                {"query": "budget", "token_budget": 1199},
            )

        assert result.isError is False
        assert rejected.isError is True
        assert result.content
        fallback = result.content[0]
        assert isinstance(fallback, TextContent)
        capsule = CapsuleResult.model_validate(_structured(result))
        assert estimate_capsule_bytes(capsule, fallback.text) <= 1200
        assert "entries omitted, budget" in fallback.text
        for statement in statements:
            prefix = statement[: len(statement) // 2]
            if prefix in fallback.text:
                assert statement in fallback.text


@pytest.mark.anyio
async def test_mcp_average_latency(app_config: AppConfig) -> None:
    """Record FTS-mode in-process HTTP averages and recall p95."""
    remember_latencies: list[float] = []
    recall_latencies: list[float] = []
    assert app_config.retrieval.mode is RetrievalMode.FTS
    with EngramStore(app_config) as store:
        server = create_mcp_server(app_config, store)
        app = server.streamable_http_app()
        async with (
            app.router.lifespan_context(app),
            _client(app, name="latency-client") as session,
        ):
            for index in range(20):
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

            for _ in range(20):
                started = time.perf_counter()
                result = await session.call_tool("recall", {"query": "latency"})
                recall_latencies.append((time.perf_counter() - started) * 1000)
                assert result.isError is False

    recall_p95_index = math.ceil(len(recall_latencies) * 0.95) - 1
    recall_p95 = sorted(recall_latencies)[recall_p95_index]
    LOGGER.info(
        "FTS MCP latency: remember_avg=%.3f ms, recall_avg=%.3f ms, recall_p95=%.3f ms",
        fmean(remember_latencies),
        fmean(recall_latencies),
        recall_p95,
    )


@pytest.mark.anyio
async def test_daemon_lifespan_sweeps_due_entries(
    app_config: AppConfig,
    clock: MutableClock,
) -> None:
    """Expire due rows autonomously while the HTTP application is alive."""
    config = replace(
        app_config,
        server=replace(app_config.server, ttl_sweep_interval_seconds=0.01),
    )
    with EngramStore(config, clock=clock) as store:
        entry = store.add_candidate(
            kind="episode",
            scope="user",
            statement="Autonomous sweep candidate.",
            writer_model="sweep-client/1.0",
        )
        assert entry.expires_at is not None
        server = create_mcp_server(config, store)
        app = server.streamable_http_app()

        async with app.router.lifespan_context(app):
            clock.current = entry.expires_at
            with anyio.fail_after(1):
                while True:
                    refreshed = store.get_entry(entry.id)
                    assert refreshed is not None
                    if refreshed.status is EntryStatus.EXPIRED:
                        break
                    await anyio.sleep(0.01)

        refreshed = store.get_entry(entry.id)
        assert refreshed is not None
        assert refreshed.status is EntryStatus.EXPIRED


@pytest.mark.anyio
async def test_daemon_lifespan_cancels_ttl_task_on_shutdown(
    app_config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = anyio.Event()
    stopped = anyio.Event()

    async def wait_until_cancelled(_config: AppConfig, _store: EngramStore) -> None:
        started.set()
        try:
            await anyio.sleep_forever()
        finally:
            stopped.set()

    monkeypatch.setattr(server_module, "_run_ttl_sweeper", wait_until_cancelled)
    with EngramStore(app_config) as store:
        server = create_mcp_server(app_config, store)
        app = server.streamable_http_app()
        async with app.router.lifespan_context(app):
            await started.wait()

        assert stopped.is_set()


def _sessions(server: FastMCP[object]) -> int:
    """Count the transports the SDK is holding open."""
    return len(server.session_manager._server_instances)  # noqa: SLF001


_ABSENT = object()
SSE_DATA_PREFIX = "data: "


def _refusal_message(response: httpx.Response) -> str:
    """Read the refusal out of whichever of three framings the SDK layer uses.

    The security middleware sends a bare text body with no content type at all.
    The transport sends a JSON-RPC error as plain JSON. The session behind it
    answers through the response stream, so its error arrives as one server-sent
    event. A guard standing in front of all three has to reproduce whichever
    applies, so the test reads all three rather than assuming one.
    """
    media_type = response.headers.get("content-type", "").split(";")[0].strip()
    if not media_type:
        return response.text
    if media_type == "text/event-stream":
        payloads = [
            line[len(SSE_DATA_PREFIX) :]
            for line in response.text.splitlines()
            if line.startswith(SSE_DATA_PREFIX)
        ]
        assert len(payloads) == 1
        message = json.loads(payloads[0])
    else:
        assert media_type == "application/json"
        message = response.json()
    assert isinstance(message, dict)
    error = message["error"]
    assert isinstance(error, dict)
    detail = error["message"]
    assert isinstance(detail, str)
    return detail


MCP_ACCEPT = "application/json, text/event-stream"
MCP_HEADERS = {"content-type": "application/json", "accept": MCP_ACCEPT}


def _initialize(params: object = _ABSENT) -> bytes:
    """Build one initialize request, optionally without its params."""
    body: dict[str, object] = {"jsonrpc": "2.0", "id": 1, "method": "initialize"}
    if params is not _ABSENT:
        body["params"] = params
    return json.dumps(body).encode()


VALID_INITIALIZE_PARAMS = {
    "protocolVersion": "2025-06-18",
    "capabilities": {},
    "clientInfo": {"name": "probe", "version": "1.0"},
}
# Every shape below reaches the transport without a session id, and no shape but
# a valid initialize request can ever be accepted. Two halves are the interesting
# ones. Well-formed JSON that is not an MCP request survives a parse check, so it
# used to reach the SDK and leave a session behind before the SDK answered 400.
# Well-formed JSON-RPC whose MCP params are invalid survives that check too, and
# is refused one layer deeper — by the session, with 200 and -32602, after the
# transport has already been registered. Both are refused here now, each with the
# answer its own layer would have given.
RequestHeaders = Mapping[str, str] | Sequence[tuple[str, str]]
REJECTED_OPENINGS: dict[str, tuple[str, RequestHeaders, bytes | None, int]] = {
    # The security middleware runs first and refuses anything whose Content-Type
    # does not begin with the JSON media type, so this never reaches the 415.
    "content type is not json": (
        "POST",
        {"content-type": "text/plain", "accept": MCP_ACCEPT},
        b"x",
        400,
    ),
    # This one does: it begins with the JSON media type, so the security check
    # passes it, and the transport's exact comparison refuses it.
    "content type is a json dialect": (
        "POST",
        {"content-type": "application/json-patch+json", "accept": MCP_ACCEPT},
        _initialize(VALID_INITIALIZE_PARAMS),
        415,
    ),
    "accept header absent": ("POST", {"content-type": "application/json"}, b"{}", 406),
    "accept omits the event stream": (
        "POST",
        {"content-type": "application/json", "accept": "application/json"},
        b"{}",
        406,
    ),
    "body is not json": ("POST", MCP_HEADERS, b"{ not json", 400),
    "body is an empty object": ("POST", MCP_HEADERS, b"{}", 400),
    "body is an empty array": ("POST", MCP_HEADERS, b"[]", 400),
    "body is bare null": ("POST", MCP_HEADERS, b"null", 400),
    "body omits the jsonrpc version": (
        "POST",
        MCP_HEADERS,
        b'{"id":1,"method":"initialize","params":{}}',
        400,
    ),
    "body omits the method": ("POST", MCP_HEADERS, b'{"jsonrpc":"2.0","id":1}', 400),
    "method is not initialize": (
        "POST",
        MCP_HEADERS,
        b'{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}',
        400,
    ),
    "notification carries no session": (
        "POST",
        MCP_HEADERS,
        b'{"jsonrpc":"2.0","method":"notifications/initialized"}',
        400,
    ),
    "get cannot open a session": ("GET", {"accept": "text/event-stream"}, None, 400),
    "get does not accept the stream": ("GET", {"accept": "application/json"}, None, 406),
    "delete cannot open a session": ("DELETE", {}, None, 400),
    "initialize params are empty": ("POST", MCP_HEADERS, _initialize({}), 200),
    "initialize params are absent": ("POST", MCP_HEADERS, _initialize(), 200),
    "initialize params are null": ("POST", MCP_HEADERS, _initialize(None), 200),
    # A list is not a JSON-RPC params object at all, so this one never reaches the
    # session layer: the transport's own envelope check refuses it with 400.
    "initialize params are a list": ("POST", MCP_HEADERS, _initialize([]), 400),
    "initialize params have wrong types": (
        "POST",
        MCP_HEADERS,
        _initialize({"protocolVersion": 1, "capabilities": "no", "clientInfo": []}),
        200,
    ),
    "initialize omits clientInfo": (
        "POST",
        MCP_HEADERS,
        _initialize({"protocolVersion": "2025-06-18", "capabilities": {}}),
        200,
    ),
    "initialize omits protocolVersion": (
        "POST",
        MCP_HEADERS,
        _initialize({"capabilities": {}, "clientInfo": {"name": "p", "version": "1"}}),
        200,
    ),
    "initialize clientInfo omits its name": (
        "POST",
        MCP_HEADERS,
        _initialize({**VALID_INITIALIZE_PARAMS, "clientInfo": {"version": "1.0"}}),
        200,
    ),
    # Three layers read Content-Type, and the strictest requires the value to
    # begin with the JSON media type. A guard that parses the header its own way
    # is more permissive than one of them on exactly these inputs, and every
    # disagreement in that direction is a session waved through to be refused.
    "content type is a list": (
        "POST",
        [("content-type", "text/plain, application/json"), ("accept", MCP_ACCEPT)],
        _initialize(VALID_INITIALIZE_PARAMS),
        400,
    ),
    "content type is a list with a parameter": (
        "POST",
        [("content-type", "text/plain;charset=utf-8, application/json"), ("accept", MCP_ACCEPT)],
        _initialize(VALID_INITIALIZE_PARAMS),
        400,
    ),
    "content type is duplicated": (
        "POST",
        [
            ("content-type", "text/plain"),
            ("content-type", "application/json"),
            ("accept", MCP_ACCEPT),
        ],
        _initialize(VALID_INITIALIZE_PARAMS),
        400,
    ),
    "accept is duplicated": (
        "POST",
        [
            ("accept", "text/plain"),
            ("accept", MCP_ACCEPT),
            ("content-type", "application/json"),
        ],
        _initialize(VALID_INITIALIZE_PARAMS),
        406,
    ),
    # FastMCP turns DNS rebinding protection on by default for a loopback host,
    # and those refusals are made inside the request handler — after the session
    # has been registered.
    "host carries no port": (
        "POST",
        {**MCP_HEADERS, "host": "127.0.0.1"},
        _initialize(VALID_INITIALIZE_PARAMS),
        421,
    ),
    "host is a name": (
        "POST",
        {**MCP_HEADERS, "host": "engram.local:8377"},
        _initialize(VALID_INITIALIZE_PARAMS),
        421,
    ),
    "host is a foreign address": (
        "POST",
        {**MCP_HEADERS, "host": "10.0.0.5:8377"},
        _initialize(VALID_INITIALIZE_PARAMS),
        421,
    ),
    "origin is hostile": (
        "POST",
        {**MCP_HEADERS, "origin": "http://evil.example"},
        _initialize(VALID_INITIALIZE_PARAMS),
        403,
    ),
    "origin is https loopback": (
        "POST",
        {**MCP_HEADERS, "origin": "https://127.0.0.1:8377"},
        _initialize(VALID_INITIALIZE_PARAMS),
        403,
    ),
}


@pytest.mark.anyio
@pytest.mark.parametrize("shape", sorted(REJECTED_OPENINGS))
async def test_a_rejected_session_opening_creates_no_session(
    shape: str,
    app_config: AppConfig,
    store: EngramStore,
) -> None:
    """The SDK registers a transport before it validates, so a refusal used to leak one.

    Measured before the guard existed: every rejected request that carried no
    session id left a live session and an anyio task for the lifetime of the
    process, so a daemon meant to run for weeks grew without bound under anything
    that could reach the port.
    """
    method, headers, body, expected_status = REJECTED_OPENINGS[shape]
    server = create_mcp_server(app_config, store)
    app = server.streamable_http_app()
    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8377") as client,
    ):
        before = _sessions(server)
        response = await client.request(method, "/mcp", headers=headers, content=body)

        assert response.status_code == expected_status
        assert _refusal_message(response)
        # No session was opened, so there is none to name in the reply.
        assert "mcp-session-id" not in response.headers
        assert _sessions(server) == before


@pytest.mark.anyio
@pytest.mark.parametrize("shape", sorted(REJECTED_OPENINGS))
async def test_a_flood_of_rejected_openings_leaves_the_session_table_bounded(
    shape: str,
    app_config: AppConfig,
    store: EngramStore,
) -> None:
    """One session per rejected request is unbounded growth; zero is the contract.

    Run for every refused shape, not one: a guard that closes the shape it was
    written against and leaves the rest open is what shipped in 2026.813.1.
    """
    method, headers, body, _ = REJECTED_OPENINGS[shape]
    server = create_mcp_server(app_config, store)
    app = server.streamable_http_app()
    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8377") as client,
    ):
        before = _sessions(server)
        for _ in range(500):
            await client.request(method, "/mcp", headers=headers, content=body)

        assert _sessions(server) == before


@pytest.mark.anyio
async def test_the_guard_leaves_a_real_session_working_end_to_end(
    app_config: AppConfig,
    store: EngramStore,
) -> None:
    """The guard sits in front of the transport, so the transport must be untouched."""
    server = create_mcp_server(app_config, store)
    app = server.streamable_http_app()
    async with (
        app.router.lifespan_context(app),
        _client(app) as session,
    ):
        listed = await session.list_tools()
        assert sorted(tool.name for tool in listed.tools) == ["recall", "remember"]

        written = await session.call_tool(
            "remember",
            {
                "statement": "The guard must not break a legitimate session.",
                "kind": "fact",
                "scope": "project/engram",
                "subject_keys": ["engram:guard"],
            },
        )
        assert _structured(written)["outcome"] == "created"

        recalled = await session.call_tool(
            "recall",
            {"query": "guard legitimate session", "scope": "project/engram"},
        )
        capsule = _structured(recalled)
        assert len(capsule["own_pending"]) == 1
        assert _sessions(server) == 1


HEADER_READINGS = [
    "application/json",
    "application/json, text/event-stream",
    "application/json;charset=utf-8",
    "application/json;charset=utf-8, text/event-stream",
    "text/plain, application/json",
    "text/plain;charset=utf-8, application/json",
    "text/event-stream, application/json",
    "application/json-patch+json",
    "application/jsonrequest",
    "*/*",
    "",
    "   ",
    "APPLICATION/JSON",
]


@pytest.mark.parametrize("value", HEADER_READINGS)
def test_the_guard_reads_a_header_exactly_as_the_transport_reads_it(value: str) -> None:
    """The two Accept and Content-Type checks here are copies; copies drift.

    They cannot be delegated the way the security middleware is, because the
    transport holds them as private methods on an object that only exists once a
    session has been created. So they are transcribed, and pinned here against
    the original for every shape where two readings of a media-type header could
    possibly differ. If an SDK upgrade changes one of them, this fails rather
    than quietly reopening the class it closed.
    """
    transport = StreamableHTTPServerTransport(mcp_session_id=None)
    request = Request({"type": "http", "headers": [(b"accept", value.encode("latin-1"))]})
    assert server_module._accepted_media(request.headers) == transport._check_accept_headers(  # noqa: SLF001
        request
    )

    request = Request({"type": "http", "headers": [(b"content-type", value.encode("latin-1"))]})
    assert server_module._declares_json(request.headers) == transport._check_content_type(request)  # noqa: SLF001


@pytest.mark.anyio
async def test_a_refused_initialize_is_answered_exactly_as_the_session_would(
    app_config: AppConfig,
    store: EngramStore,
) -> None:
    """The guard speaks for the session layer here, so it must speak in its words.

    An initialize whose MCP params are invalid is refused by the session, not the
    transport: 200, the error carried as one server-sent event, code -32602,
    addressed to the request that caused it. Anything else is a behaviour change
    the client can see.
    """
    server = create_mcp_server(app_config, store)
    app = server.streamable_http_app()
    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8377") as client,
    ):
        response = await client.post("/mcp", headers=MCP_HEADERS, content=_initialize({}))

        assert response.status_code == 200
        assert response.headers["content-type"] == "text/event-stream"
        assert response.text == (
            'event: message\r\ndata: {"jsonrpc":"2.0","id":1,'
            '"error":{"code":-32602,"message":"Invalid request parameters","data":""}}\r\n\r\n'
        )
        assert _sessions(server) == 0


@pytest.mark.anyio
async def test_a_session_the_client_closes_is_forgotten_not_only_terminated(
    app_config: AppConfig,
    store: EngramStore,
) -> None:
    """The ordinary path grew the table, and no refusal could have stopped it.

    Terminating a session closes its streams and marks the transport terminated;
    neither of the SDK's removal paths then applies, so the entry survives, and
    the idle deadline cannot reclaim it because the task it would cancel has
    already ended. Measured on 2026.813.2: 25 open-then-close cycles, 25 entries
    retained. An editor that restarts does exactly this.
    """
    server = create_mcp_server(app_config, store)
    app = server.streamable_http_app()
    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8377") as client,
    ):
        for _ in range(25):
            opened = await client.post(
                "/mcp",
                headers=MCP_HEADERS,
                content=_initialize(VALID_INITIALIZE_PARAMS),
            )
            assert opened.status_code == 200
            session_id = opened.headers["mcp-session-id"]
            assert _sessions(server) == 1

            closed = await client.delete(
                "/mcp", headers={**MCP_HEADERS, "mcp-session-id": session_id}
            )
            assert closed.status_code == 200
            assert _sessions(server) == 0


def _delete_scope(session_id: str) -> dict[str, Any]:
    """One DELETE addressed to an established session."""
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "DELETE",
        "scheme": "http",
        "path": "/mcp",
        "raw_path": b"/mcp",
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"host", b"127.0.0.1:8377"),
            (b"accept", b"application/json, text/event-stream"),
            (b"content-type", b"application/json"),
            (b"mcp-session-id", session_id.encode("ascii")),
        ],
        "client": ("127.0.0.1", 50000),
        "server": ("127.0.0.1", 8377),
        "state": {},
    }


@pytest.mark.anyio
async def test_a_close_interrupted_before_its_answer_still_frees_the_session(
    app_config: AppConfig,
    store: EngramStore,
) -> None:
    """The SDK terminates the transport and only then answers.

    A client that has already gone makes that answer raise, so anything the
    manager does after awaiting the SDK is exactly what never runs. Measured on
    2026.813.3, which reclaimed only after a normal return: 25 interrupted closes
    left 25 entries, permanently — the idle deadline cannot reach them, because
    the task it would cancel has already ended. The exception belongs to the
    caller and is left to propagate; the table must be empty regardless.
    """
    server = create_mcp_server(app_config, store)
    app = server.streamable_http_app()
    transport = httpx.ASGITransport(app=app)

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def peer_is_gone(message: Message) -> None:
        del message
        raise OSError("client went away")

    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8377") as client,
    ):
        for _ in range(25):
            opened = await client.post(
                "/mcp",
                headers=MCP_HEADERS,
                content=_initialize(VALID_INITIALIZE_PARAMS),
            )
            assert opened.status_code == 200
            assert _sessions(server) == 1

            with pytest.raises(OSError, match="client went away"):
                await app(_delete_scope(opened.headers["mcp-session-id"]), receive, peer_is_gone)

            assert _sessions(server) == 0


@pytest.mark.anyio
async def test_an_abandoned_session_is_reclaimed_once_its_deadline_passes(
    app_config: AppConfig,
    store: EngramStore,
) -> None:
    """Refusing invalid openings does nothing for openings that are valid.

    A client that initialises and then vanishes — a crash, a closed laptop, a
    killed editor — leaves a session the SDK removes only on an explicit delete.
    FastMCP sets no deadline, so before this configuration existed 300 abandoned
    initialisations were still held after the last request, for the life of the
    process.
    """
    config = replace(
        app_config,
        server=replace(app_config.server, session_idle_timeout_seconds=0.2),
    )
    server = create_mcp_server(config, store)
    app = server.streamable_http_app()
    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8377") as client,
    ):
        for _ in range(20):
            opened = await client.post(
                "/mcp",
                headers=MCP_HEADERS,
                content=_initialize(VALID_INITIALIZE_PARAMS),
            )
            assert opened.status_code == 200
        assert _sessions(server) == 20

        with anyio.fail_after(10):
            while True:
                if not _sessions(server):
                    break
                await anyio.sleep(0.05)


@pytest.mark.anyio
async def test_a_flood_of_valid_json_cannot_disturb_a_live_session(
    app_config: AppConfig,
    store: EngramStore,
) -> None:
    """The two halves of the contract, measured against each other.

    Well-formed JSON that is not an initialize request is the shape 2026.813.1
    shipped open: it survived the parse check, reached the SDK, and left a session
    behind. Five hundred of them must add nothing, and the session working
    alongside them must not notice they happened.
    """
    server = create_mcp_server(app_config, store)
    app = server.streamable_http_app()
    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        _client(app) as session,
        httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8377") as flooder,
    ):
        assert _sessions(server) == 1

        for _ in range(500):
            refused = await flooder.post("/mcp", headers=MCP_HEADERS, content=b"{}")
            assert refused.status_code == 400

        assert _sessions(server) == 1
        listed = await session.list_tools()
        assert sorted(tool.name for tool in listed.tools) == ["recall", "remember"]
        written = await session.call_tool(
            "remember",
            {
                "statement": "A flood of refusals must not cost a live session anything.",
                "kind": "fact",
                "scope": "project/engram",
            },
        )
        assert _structured(written)["outcome"] == "created"
        assert _sessions(server) == 1
