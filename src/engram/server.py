# Copyright 2026 Julien Bombled
# SPDX-License-Identifier: Apache-2.0

"""Stateful FastMCP server exposing the Engram remember and recall tools."""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
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
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from . import __version__
from .capsule import CapsuleBuilder, CapsuleResult
from .config import MAX_FTS_QUERY_CHARS, AppConfig
from .models import (
    CandidateWriteResult,
    EntryKind,
    EntryStatus,
    Evidence,
    EvidenceType,
    PromotionState,
    RememberOutcome,
)
from .normalization import (
    HARD_MAX_STATEMENT_CHARS,
    HARD_MAX_SUBJECT_KEYS,
    MAX_EVIDENCE_ITEMS,
    MAX_EVIDENCE_REF_CHARS,
    MAX_MCP_CLIENT_COMPONENT_CHARS,
    MAX_SCOPE_CHARS,
    MAX_SUBJECT_KEY_CHARS,
    MAX_WRITER_MODEL_CHARS,
    StoreValidationError,
    bounded_text,
    normalize_mcp_client_component,
    normalize_scope,
)
from .retrieval import EntryIndexer, RetrievalRequest, Retriever, build_retriever
from .store import EngramStore, StoreBusyError

MAX_HTTP_BODY_CHUNKS = 256
MAX_CLIENT_COMPONENT_CHARS = MAX_MCP_CLIENT_COMPONENT_CHARS
ASCII_ZERO = ord("0")
ASCII_NINE = ord("9")
LOGGER = logging.getLogger(__name__)
ToolContext = Context[ServerSession, object, Request]
McpLogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
SubjectKeyInput = Annotated[
    str,
    Field(min_length=1, max_length=MAX_SUBJECT_KEY_CHARS),
]


class EvidenceInput(BaseModel):
    """Strict opaque evidence input accepted by remember."""

    model_config = ConfigDict(extra="forbid")

    type: EvidenceType
    ref: str = Field(min_length=1, max_length=MAX_EVIDENCE_REF_CHARS)


class RememberArguments(ArgModelBase):
    """Strict top-level remember input schema."""

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    statement: str = Field(min_length=1, max_length=HARD_MAX_STATEMENT_CHARS)
    kind: EntryKind
    scope: str = Field(default="user", min_length=1, max_length=MAX_SCOPE_CHARS)
    subject_keys: list[SubjectKeyInput] = Field(
        default_factory=list,
        max_length=HARD_MAX_SUBJECT_KEYS,
    )
    observed_at: datetime | None = None
    evidence: list[EvidenceInput] = Field(default_factory=list, max_length=MAX_EVIDENCE_ITEMS)


class RecallArguments(ArgModelBase):
    """Strict top-level recall input schema."""

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    query: str = Field(min_length=1, max_length=MAX_FTS_QUERY_CHARS)
    scope: str | None = Field(default=None, min_length=1, max_length=MAX_SCOPE_CHARS)
    kinds: list[EntryKind] = Field(default_factory=list, max_length=len(EntryKind))
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
    outcome: RememberOutcome


class RequestBodyLimitMiddleware:
    """Reject oversized POST bodies before the MCP SDK buffers or parses them."""

    def __init__(self, app: ASGIApp, *, max_body_bytes: int) -> None:
        """Bind one downstream ASGI application and its byte ceiling."""
        self._app = app
        self._max_body_bytes = max_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Buffer one bounded POST body and replay it exactly once."""
        if scope["type"] != "http" or scope.get("method") != "POST":
            await self._app(scope, receive, send)
            return

        try:
            self._validate_content_length(scope)
            body = await self._read_body(receive)
        except _RequestBodyRejectedError as exc:
            await self._reject(send, exc.status_code, exc.detail)
            return
        if body is None:
            return

        replayed = False

        async def replay_receive() -> Message:
            nonlocal replayed
            if not replayed:
                replayed = True
                return {
                    "type": "http.request",
                    "body": bytes(body),
                    "more_body": False,
                }
            return await receive()

        await self._app(scope, replay_receive, send)

    def _validate_content_length(self, scope: Scope) -> None:
        content_lengths = [
            value for name, value in scope.get("headers", ()) if name.lower() == b"content-length"
        ]
        if len(content_lengths) > 1:
            raise _RequestBodyRejectedError(400, "invalid Content-Length")
        if not content_lengths:
            return
        raw_length = content_lengths[0]
        if not raw_length or any(byte < ASCII_ZERO or byte > ASCII_NINE for byte in raw_length):
            raise _RequestBodyRejectedError(400, "invalid Content-Length")
        if (
            len(raw_length) > len(str(self._max_body_bytes))
            or int(raw_length) > self._max_body_bytes
        ):
            raise _RequestBodyRejectedError(413, "request body too large")

    async def _read_body(self, receive: Receive) -> bytes | None:
        body = bytearray()
        for _ in range(MAX_HTTP_BODY_CHUNKS):
            message = await receive()
            if message["type"] == "http.disconnect":
                return None
            if message["type"] != "http.request":
                raise _RequestBodyRejectedError(400, "invalid request body")
            chunk = message.get("body", b"")
            if not isinstance(chunk, bytes):
                raise _RequestBodyRejectedError(400, "invalid request body")
            if len(chunk) > self._max_body_bytes - len(body):
                raise _RequestBodyRejectedError(413, "request body too large")
            body.extend(chunk)
            if not message.get("more_body", False):
                return bytes(body)
        raise _RequestBodyRejectedError(413, "request body too large")

    @staticmethod
    async def _reject(send: Send, status_code: int, detail: str) -> None:
        body = detail.encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": status_code,
                "headers": [
                    (b"cache-control", b"no-store"),
                    (b"content-length", str(len(body)).encode("ascii")),
                    (b"content-type", b"text/plain; charset=utf-8"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


class _RequestBodyRejectedError(Exception):
    """Carry one deliberate HTTP-layer body rejection."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class EngramFastMCP(FastMCP[object]):
    """FastMCP server with one daemon-scoped TTL maintenance task."""

    def __init__(self, config: AppConfig, store: EngramStore, tools: list[Tool]) -> None:
        """Configure the public MCP surface and retain daemon lifecycle dependencies."""
        self._engram_config = config
        self._engram_store = store
        super().__init__(
            name="Engram",
            instructions="Selective shared memory with quarantined model candidates.",
            tools=tools,
            host=config.server.host,
            port=config.server.port,
            streamable_http_path=config.server.path,
            log_level=cast("McpLogLevel", config.logging.console_level),
        )
        self._mcp_server.version = __version__

    def streamable_http_app(self) -> Starlette:
        """Return the SDK application with TTL maintenance bound to its lifespan."""
        app = super().streamable_http_app()
        session_lifespan = app.router.lifespan_context

        @asynccontextmanager
        async def lifespan(starlette_app: Starlette) -> AsyncIterator[None]:
            async with session_lifespan(starlette_app), anyio.create_task_group() as task_group:
                task_group.start_soon(
                    _run_ttl_sweeper,
                    self._engram_config,
                    self._engram_store,
                )
                try:
                    yield
                finally:
                    task_group.cancel_scope.cancel()

        app.router.lifespan_context = lifespan
        app.add_middleware(
            RequestBodyLimitMiddleware,
            max_body_bytes=self._engram_config.server.max_request_body_bytes,
        )
        return app


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
        """Resolve one model observation through trust-aware idempotent storage."""
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
        except StoreValidationError as exc:
            raise ToolError(str(exc)) from exc

        entry = outcome.entry
        if isinstance(selected_retriever, EntryIndexer):
            await anyio.to_thread.run_sync(selected_retriever.index_entry, entry)
        structured = RememberResult(
            entry_id=entry.id,
            status=entry.status,
            promotion_state=entry.promotion_state,
            expires_at=(None if entry.expires_at is None else _format_datetime(entry.expires_at)),
            idempotent=outcome.idempotent,
            outcome=outcome.outcome,
        )
        text = _remember_text(outcome)
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

        try:
            normalized_scope = None if scope is None else normalize_scope(scope)
        except StoreValidationError as exc:
            raise ToolError(str(exc)) from exc
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
            description=(
                "Resolve a memory observation as a new candidate, retry, "
                "corroboration, trusted match, or renewal."
            ),
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
    return EngramFastMCP(config, store, tools)


async def _run_ttl_sweeper(config: AppConfig, store: EngramStore) -> None:
    while True:
        await anyio.sleep(config.server.ttl_sweep_interval_seconds)
        try:
            expired_count = await anyio.to_thread.run_sync(_expire_due, config, store)
        except StoreBusyError:
            LOGGER.warning("TTL sweep skipped: server busy, retry")
        except Exception:
            LOGGER.exception("TTL sweep failed")
        else:
            LOGGER.info("TTL sweep complete: expired=%d", expired_count)


def _expire_due(config: AppConfig, store: EngramStore) -> int:
    with store.write_access(config.server.write_wait_timeout_ms):
        return store.expire_due()


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
        raise ToolError("initialized client identity is required")
    try:
        bounded_name = _client_component(params.clientInfo.name, "client name")
        bounded_version = _client_component(params.clientInfo.version, "client version")
        return _client_writer_model(bounded_name, bounded_version)
    except StoreValidationError as exc:
        raise ToolError(str(exc)) from exc


def _client_component(value: str, field_name: str) -> str:
    return normalize_mcp_client_component(value, field_name)


def _client_writer_model(name: str, version: str) -> str:
    """Preserve safe legacy owners and domain-separate ambiguous components."""
    if all(character not in component for component in (name, version) for character in "%/"):
        return bounded_text(
            f"{name}/{version}",
            "writer_model",
            MAX_WRITER_MODEL_CHARS,
        )
    payload = json.dumps(
        [name, version],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"mcp-v2:{hashlib.sha256(payload).hexdigest()}"


def _remember_text(outcome: CandidateWriteResult) -> str:
    entry = outcome.entry
    if outcome.outcome is RememberOutcome.EXISTING_TRUSTED:
        return f"Found trusted entry {entry.id}; no unconfirmed replacement was created."
    if outcome.outcome is RememberOutcome.RETRY:
        return f"Resolved retry to existing unconfirmed candidate {entry.id}."
    if outcome.outcome is RememberOutcome.CORROBORATED:
        return f"Recorded corroboration as unconfirmed candidate {entry.id}."
    if outcome.outcome is RememberOutcome.RENEWED:
        return f"Renewed terminal content as unconfirmed candidate {entry.id}."
    return f"Stored {entry.id} as an unconfirmed candidate."


def _format_datetime(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
