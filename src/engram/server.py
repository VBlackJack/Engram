# Copyright 2026 Julien Bombled
# SPDX-License-Identifier: Apache-2.0

"""Stateful FastMCP server exposing the Engram remember and recall tools."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal, cast

import anyio
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.fastmcp.tools import Tool
from mcp.server.fastmcp.utilities.func_metadata import ArgModelBase
from mcp.server.session import ServerSession
from mcp.types import AnyFunction, CallToolResult, TextContent, ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field
from starlette.requests import Request

from .capsule import CapsuleBuilder, CapsuleResult
from .config import AppConfig
from .models import (
    CandidateWriteResult,
    EntryKind,
    EntryStatus,
    Evidence,
    EvidenceType,
    PromotionState,
)
from .retrieval import EntryIndexer, RetrievalRequest, Retriever, build_retriever
from .store import EngramStore, StoreBusyError

UNKNOWN_CLIENT = "unknown-client"
ToolContext = Context[ServerSession, object, Request]
McpLogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


class EvidenceInput(BaseModel):
    """Strict opaque evidence input accepted by remember."""

    model_config = ConfigDict(extra="forbid")

    type: EvidenceType
    ref: str = Field(min_length=1)


class RememberArguments(ArgModelBase):
    """Strict top-level remember input schema."""

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    statement: str = Field(min_length=1)
    kind: EntryKind
    scope: str = "user"
    subject_keys: list[str] = Field(default_factory=list)
    observed_at: datetime | None = None
    evidence: list[EvidenceInput] = Field(default_factory=list)


class RecallArguments(ArgModelBase):
    """Strict top-level recall input schema."""

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    query: str = Field(min_length=1)
    scope: str | None = None
    kinds: list[EntryKind] = Field(default_factory=list)
    include_conflicts: bool = False
    token_budget: int | None = None


class RememberResult(BaseModel):
    """Structured result returned by remember."""

    model_config = ConfigDict(extra="forbid")

    entry_id: str
    status: EntryStatus
    promotion_state: PromotionState
    expires_at: str | None
    idempotent: bool


def create_mcp_server(
    config: AppConfig,
    store: EngramStore,
    *,
    retriever: Retriever | None = None,
) -> FastMCP[object]:
    """Build the stateful streamable HTTP server and its two strict tools."""
    selected_retriever = retriever or build_retriever(config, store)
    capsule_builder = CapsuleBuilder(config.capsule)

    async def remember(  # noqa: PLR0913
        statement: str,
        kind: EntryKind,
        scope: str,
        subject_keys: list[str],
        observed_at: datetime | None,
        evidence: list[EvidenceInput],
        ctx: ToolContext,
    ) -> Annotated[CallToolResult, RememberResult]:
        """Store an unconfirmed model candidate with server-owned provenance."""
        writer_model = _client_identity(ctx)

        def write_candidate() -> CandidateWriteResult:
            with store.write_access(config.server.write_wait_timeout_ms):
                return store.add_candidate(
                    kind=kind,
                    scope=scope,
                    statement=statement,
                    writer_model=writer_model,
                    subject_keys=subject_keys,
                    observed_at=observed_at,
                    evidence=tuple(Evidence(type=item.type, ref=item.ref) for item in evidence),
                    include_outcome=True,
                )

        try:
            outcome = await anyio.to_thread.run_sync(write_candidate)
        except StoreBusyError as exc:
            raise ToolError("server busy, retry") from exc

        entry = outcome.entry
        if isinstance(selected_retriever, EntryIndexer):
            await anyio.to_thread.run_sync(selected_retriever.index_entry, entry)
        structured = RememberResult(
            entry_id=entry.id,
            status=entry.status,
            promotion_state=entry.promotion_state,
            expires_at=(None if entry.expires_at is None else _format_datetime(entry.expires_at)),
            idempotent=outcome.idempotent,
        )
        text = (
            f"Stored {entry.id} as {entry.status.value}/{entry.promotion_state.value}; "
            "unconfirmed candidate."
        )
        return CallToolResult(
            content=[TextContent(type="text", text=text)],
            structuredContent=structured.model_dump(mode="json"),
        )

    async def recall(  # noqa: PLR0913
        query: str,
        scope: str | None,
        kinds: list[EntryKind],
        include_conflicts: bool,  # noqa: FBT001
        token_budget: int | None,
        ctx: ToolContext,
    ) -> Annotated[CallToolResult, CapsuleResult]:
        """Return a compact trust-aware capsule using configured retrieval."""
        try:
            resolved_budget = capsule_builder.resolve_budget(token_budget)
        except ValueError as exc:
            raise ToolError(str(exc)) from exc

        normalized_scope = None if scope is None else scope.strip().casefold()
        writer_model = _client_identity(ctx)
        request = RetrievalRequest(
            query=query,
            scope=normalized_scope,
            kinds=None if not kinds else frozenset(kinds),
            writer_model=writer_model,
        )

        def retrieve_and_build() -> tuple[CapsuleResult, str]:
            retrieval = selected_retriever.retrieve(request)
            return capsule_builder.build(
                retrieval,
                scope=normalized_scope,
                include_conflicts=include_conflicts,
                token_budget=resolved_budget,
            )

        capsule, text = await anyio.to_thread.run_sync(retrieve_and_build)
        return CallToolResult(
            content=[TextContent(type="text", text=text)],
            structuredContent=capsule.model_dump(mode="json"),
        )

    tools = [
        _strict_tool(
            remember,
            arguments_model=RememberArguments,
            name="remember",
            description="Store an unconfirmed memory candidate.",
            annotations=ToolAnnotations(
                readOnlyHint=False,
                destructiveHint=False,
                idempotentHint=True,
                openWorldHint=False,
            ),
        ),
        _strict_tool(
            recall,
            arguments_model=RecallArguments,
            name="recall",
            description="Recall a compact trust-aware memory capsule.",
            annotations=ToolAnnotations(
                readOnlyHint=True,
                openWorldHint=False,
            ),
        ),
    ]
    return FastMCP(
        name="Engram",
        instructions="Selective shared memory with quarantined model candidates.",
        tools=tools,
        host=config.server.host,
        port=config.server.port,
        streamable_http_path=config.server.path,
        log_level=cast("McpLogLevel", config.logging.console_level),
    )


def _strict_tool(
    function: AnyFunction,
    *,
    arguments_model: type[ArgModelBase],
    name: str,
    description: str,
    annotations: ToolAnnotations,
) -> Tool:
    tool = Tool.from_function(
        function,
        name=name,
        description=description,
        context_kwarg="ctx",
        annotations=annotations,
        structured_output=True,
    )
    tool.fn_metadata.arg_model = arguments_model
    tool.parameters = arguments_model.model_json_schema()
    return tool


def _client_identity(context: ToolContext) -> str:
    params = context.request_context.session.client_params
    if params is None:
        return UNKNOWN_CLIENT
    name = params.clientInfo.name.strip()
    version = params.clientInfo.version.strip()
    if not name or not version:
        return UNKNOWN_CLIENT
    return f"{name}/{version}"


def _format_datetime(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
