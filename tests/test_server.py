# Copyright 2026 Julien Bombled
# SPDX-License-Identifier: Apache-2.0

"""Full MCP SDK integration tests over streamable HTTP."""

from __future__ import annotations

import logging
import math
import sqlite3
import time
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import replace
from statistics import fmean
from typing import Any

import anyio
import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.types import CallToolResult, Implementation, TextContent
from starlette.applications import Starlette

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
