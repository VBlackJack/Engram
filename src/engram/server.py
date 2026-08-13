# Copyright 2026 Julien Bombled
# SPDX-License-Identifier: Apache-2.0

"""Stateful FastMCP server exposing the Engram remember and recall tools."""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Annotated, Any, Literal, cast

import anyio
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.fastmcp.tools import Tool
from mcp.server.fastmcp.utilities.func_metadata import ArgModelBase
from mcp.server.session import ServerSession
from mcp.types import (
    INVALID_PARAMS,
    INVALID_REQUEST,
    PARSE_ERROR,
    AnyFunction,
    CallToolResult,
    ErrorData,
    JSONRPCError,
    JSONRPCMessage,
    JSONRPCRequest,
    TextContent,
    ToolAnnotations,
)
from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
)
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
# The media types and refusals of the streamable HTTP transport, named here so
# the guard in front of it cannot drift from the transport behind it.
CONTENT_TYPE_JSON = "application/json"
CONTENT_TYPE_SSE = "text/event-stream"
MCP_SESSION_HEADER = b"mcp-session-id"
INITIALIZE_METHOD = "initialize"
MISSING_SESSION_DETAIL = "Bad Request: Missing session ID"
NOT_ACCEPTABLE_POST_DETAIL = (
    "Not Acceptable: Client must accept both application/json and text/event-stream"
)
NOT_ACCEPTABLE_STREAM_DETAIL = "Not Acceptable: Client must accept text/event-stream"
UNSUPPORTED_MEDIA_TYPE_DETAIL = "Unsupported Media Type: Content-Type must be application/json"
MAX_CLIENT_COMPONENT_CHARS = MAX_MCP_CLIENT_COMPONENT_CHARS
ASCII_ZERO = ord("0")
ASCII_NINE = ord("9")
DEFINITIONS_KEY = "$defs"
DEFINITIONS_POINTER = f"#/{DEFINITIONS_KEY}/"
NULLABLE_VARIANTS = 2
NULL_TYPE = "null"
OBSERVED_AT_EXAMPLES = ("2026-07-31T14:05:00+02:00", "2026-07-31T12:05:00Z")
EMPTY_SCHEMA_OVERRIDES: Mapping[str, Mapping[str, object]] = MappingProxyType({})
REMEMBER_TOOL = "remember"
RECALL_TOOL = "recall"
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
    kind: EntryKind = Field(description="Memory entry kind, restricted to the published values.")
    scope: str = Field(default="user", min_length=1, max_length=MAX_SCOPE_CHARS)
    subject_keys: list[SubjectKeyInput] = Field(
        default_factory=list,
        max_length=HARD_MAX_SUBJECT_KEYS,
    )
    observed_at: AwareDatetime | None = Field(
        default=None,
        description=(
            "Instant the statement was observed, as an RFC 3339 date-time carrying "
            "a mandatory UTC offset. A value without an offset is rejected."
        ),
        examples=list(OBSERVED_AT_EXAMPLES),
    )
    evidence: list[EvidenceInput] = Field(
        default_factory=list,
        max_length=MAX_EVIDENCE_ITEMS,
        description=(
            "Opaque evidence references. Every item is an object carrying a type and "
            "a ref; a bare string is rejected."
        ),
    )

    @field_validator("observed_at", mode="before")
    @classmethod
    def _reject_non_textual_instant(cls, value: object) -> object:
        """Keep accepted instants aligned with the published string schema."""
        if value is None or isinstance(value, str | datetime):
            return value
        raise ValueError("observed_at must be an RFC 3339 date-time carrying a UTC offset")


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
    """Refuse what the transport would refuse, before it can open a session.

    The SDK registers a transport in its session table, and starts the anyio task
    behind it, for **any** request that carries no session id — whatever the
    method, and before a single byte of the body is looked at. Only then does the
    transport discover the request was never acceptable and answer 4xx. The
    session stays. Measured on the 2026.813.1 build: 500 rejected requests, 500
    live sessions, for seven distinct request shapes, and 50 more for each of GET
    and DELETE. A daemon installed to run from logon and stay up for weeks grows
    without bound under a misconfigured client, or under anything that can reach
    the port.

    Without a session id there is exactly one request the transport can accept:
    a POST carrying a valid JSON-RPC ``initialize`` request. That is the rule
    applied here, one layer earlier, using the SDK's own message model so the two
    cannot disagree about what "valid" means. Every refusal below reproduces the
    transport's status code, its JSON-RPC error code and its wording, so a client
    cannot tell which layer answered. Requests carrying a session id are untouched:
    they reach an existing transport and create nothing.
    """

    def __init__(self, app: ASGIApp, *, max_body_bytes: int, mcp_path: str) -> None:
        """Bind one downstream ASGI application, its byte ceiling and its path."""
        self._app = app
        self._max_body_bytes = max_body_bytes
        self._mcp_path = mcp_path

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Screen one request, then replay its bounded body exactly once."""
        if scope["type"] != "http" or scope.get("path") != self._mcp_path:
            await self._app(scope, receive, send)
            return

        try:
            body = await self._screen(scope, receive)
        except _RequestBodyRejectedError as exc:
            await self._reject(send, exc)
            return
        except _RequestAbandonedError:
            return

        if body is None:
            await self._app(scope, receive, send)
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

    async def _screen(self, scope: Scope, receive: Receive) -> bytes | None:
        """Return the buffered POST body, or None when there is nothing to replay."""
        headers = {name.lower(): value for name, value in scope.get("headers", ())}
        opening = MCP_SESSION_HEADER not in headers
        if scope.get("method") != "POST":
            if opening:
                self._refuse_streaming_opening(scope, headers)
            return None
        self._validate_content_length(scope)
        body = await self._read_body(receive)
        if opening:
            self._require_initialize(headers, body)
        return body

    @staticmethod
    def _refuse_streaming_opening(scope: Scope, headers: Mapping[bytes, bytes]) -> None:
        """Refuse a GET or DELETE that carries no session, before one is created.

        Neither method can ever open a session: the transport answers 400 once it
        finds the session id missing. A GET is checked for the event stream first,
        exactly as the transport checks it.
        """
        if scope.get("method") == "GET" and CONTENT_TYPE_SSE not in _offered_media(
            headers, b"accept"
        ):
            raise _RequestBodyRejectedError(406, NOT_ACCEPTABLE_STREAM_DETAIL)
        raise _RequestBodyRejectedError(400, MISSING_SESSION_DETAIL)

    @staticmethod
    def _require_initialize(headers: Mapping[bytes, bytes], body: bytes) -> None:
        """Refuse a session-opening POST that is not a valid initialize request."""
        offered = _offered_media(headers, b"accept")
        if not {CONTENT_TYPE_JSON, CONTENT_TYPE_SSE} <= offered:
            raise _RequestBodyRejectedError(406, NOT_ACCEPTABLE_POST_DETAIL)
        if CONTENT_TYPE_JSON not in _offered_media(headers, b"content-type"):
            raise _RequestBodyRejectedError(415, UNSUPPORTED_MEDIA_TYPE_DETAIL)
        try:
            raw = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise _RequestBodyRejectedError(
                400, f"Parse error: {exc}", error_code=PARSE_ERROR
            ) from exc
        try:
            message = JSONRPCMessage.model_validate(raw)
        except ValidationError as exc:
            raise _RequestBodyRejectedError(
                400, f"Validation error: {exc}", error_code=INVALID_PARAMS
            ) from exc
        if not (
            isinstance(message.root, JSONRPCRequest) and message.root.method == INITIALIZE_METHOD
        ):
            raise _RequestBodyRejectedError(400, MISSING_SESSION_DETAIL)

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

    async def _read_body(self, receive: Receive) -> bytes:
        body = bytearray()
        for _ in range(MAX_HTTP_BODY_CHUNKS):
            message = await receive()
            if message["type"] == "http.disconnect":
                raise _RequestAbandonedError
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
    async def _reject(send: Send, refusal: _RequestBodyRejectedError) -> None:
        """Answer with the JSON-RPC error body the transport itself would send."""
        error = JSONRPCError(
            jsonrpc="2.0",
            id="server-error",
            error=ErrorData(code=refusal.error_code, message=refusal.detail),
        )
        body = error.model_dump_json(by_alias=True, exclude_none=True).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": refusal.status_code,
                "headers": [
                    (b"cache-control", b"no-store"),
                    (b"content-length", str(len(body)).encode("ascii")),
                    (b"content-type", CONTENT_TYPE_JSON.encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


def _offered_media(headers: Mapping[bytes, bytes], name: bytes) -> set[str]:
    """Return the media types one header offers, without their parameters."""
    declared = headers.get(name, b"").decode("latin-1")
    return {part.split(";")[0].strip() for part in declared.split(",")}


class _RequestBodyRejectedError(Exception):
    """Carry one deliberate HTTP-layer refusal, with the code the SDK would use."""

    def __init__(self, status_code: int, detail: str, *, error_code: int = INVALID_REQUEST) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail
        self.error_code = error_code


class _RequestAbandonedError(Exception):
    """The client disconnected before its body arrived."""


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
            mcp_path=self._engram_config.server.path,
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

    schemas = publish_tool_schemas(config)
    tools = [
        _strict_tool(
            remember,
            arguments_model=RememberArguments,
            name=REMEMBER_TOOL,
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
            parameters=schemas[REMEMBER_TOOL],
        ),
        _strict_tool(
            recall,
            arguments_model=RecallArguments,
            name=RECALL_TOOL,
            description="Recall a compact trust-aware memory capsule.",
            annotations=ToolAnnotations(
                readOnlyHint=True,
                openWorldHint=False,
            ),
            parameters=schemas[RECALL_TOOL],
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


def _strict_tool(  # noqa: PLR0913
    function: AnyFunction,
    *,
    arguments_model: type[ArgModelBase],
    name: str,
    description: str,
    annotations: ToolAnnotations,
    parameters: dict[str, Any],
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
    tool.parameters = parameters
    return tool


def publish_tool_schemas(config: AppConfig) -> dict[str, dict[str, Any]]:
    """Return the exact argument schemas both tools publish under one configuration."""
    return {
        REMEMBER_TOOL: _publish_schema(RememberArguments, _remember_schema_overrides(config)),
        RECALL_TOOL: _publish_schema(RecallArguments, _recall_schema_overrides(config)),
    }


def _remember_schema_overrides(config: AppConfig) -> Mapping[str, Mapping[str, object]]:
    """Publish the input bounds this server enforces, not the ones it could be given.

    The argument models carry the hard ceilings, which are the maximum any
    configuration may set rather than the limit this installation applies. A
    client reading the schema alone -- which is the whole point of MCP, and what
    Claude, Codex and Gemini all do -- therefore composed calls the server then
    refused: measured against the shipped limits, 1999 characters accepted and
    2001, 4000 and 16384 all refused while the schema advertised 16384. The
    failure lands on exactly the memories worth keeping, the long rationale and
    the richly tagged fact.

    Recall already published its configured capsule bounds this way. Remember now
    does the same, so both tools describe the server a client is talking to.
    """
    limits = config.limits
    return {
        "statement": {
            "maxLength": limits.max_statement_chars,
            "description": (
                "Canonical statement to remember, at most "
                f"{limits.max_statement_chars} characters on this server."
            ),
        },
        "subject_keys": {
            "maxItems": limits.max_subject_keys,
            "description": (
                f"Stable retrieval keys, at most {limits.max_subject_keys} on this server."
            ),
        },
    }


def _recall_schema_overrides(config: AppConfig) -> Mapping[str, Mapping[str, object]]:
    """Publish the configured capsule budget bounds the server actually enforces."""
    capsule = config.capsule
    return {
        "token_budget": {
            "minimum": capsule.min_token_budget,
            "maximum": capsule.max_token_budget,
            "description": (
                f"Serialized capsule byte budget. Omitting it selects "
                f"{capsule.default_token_budget}; a value outside "
                f"{capsule.min_token_budget}..{capsule.max_token_budget} is rejected."
            ),
        }
    }


def _publish_schema(
    arguments_model: type[ArgModelBase],
    overrides: Mapping[str, Mapping[str, object]],
) -> dict[str, Any]:
    """Render one argument model as a schema a client can read without dereferencing."""
    schema: dict[str, Any] = arguments_model.model_json_schema()
    definitions = schema.pop(DEFINITIONS_KEY, {})
    if not isinstance(definitions, dict):
        raise TypeError(f"{arguments_model.__name__} declares a non-object {DEFINITIONS_KEY}")
    published = _inline_schema_refs(schema, definitions, ())
    if not isinstance(published, dict):
        raise TypeError(f"{arguments_model.__name__} does not publish an object schema")
    properties = published.get("properties")
    if not isinstance(properties, dict):
        raise TypeError(f"{arguments_model.__name__} does not publish properties")
    for property_name, override in overrides.items():
        declared = properties.get(property_name)
        if not isinstance(declared, dict):
            raise ValueError(f"{arguments_model.__name__} has no property {property_name}")
        properties[property_name] = {**declared, **override}
    return published


def _inline_schema_refs(
    node: object,
    definitions: Mapping[str, object],
    visiting: tuple[str, ...],
) -> object:
    """Replace every local reference by its definition and reject a cyclic model."""
    if isinstance(node, list):
        return [_inline_schema_refs(item, definitions, visiting) for item in node]
    if not isinstance(node, dict):
        return node
    reference = node.get("$ref")
    if isinstance(reference, str):
        return _inline_reference(reference, node, definitions, visiting)
    resolved = {
        key: _inline_schema_refs(value, definitions, visiting) for key, value in node.items()
    }
    return _flatten_nullable(resolved)


def _inline_reference(
    reference: str,
    node: Mapping[str, object],
    definitions: Mapping[str, object],
    visiting: tuple[str, ...],
) -> dict[str, object]:
    if not reference.startswith(DEFINITIONS_POINTER):
        raise ValueError(f"unsupported schema reference {reference}")
    definition_name = reference.removeprefix(DEFINITIONS_POINTER)
    if definition_name in visiting:
        raise ValueError(f"recursive schema reference {reference}")
    target = definitions.get(definition_name)
    if target is None:
        raise ValueError(f"unresolved schema reference {reference}")
    inlined = _inline_schema_refs(target, definitions, (*visiting, definition_name))
    if not isinstance(inlined, dict):
        raise TypeError(f"schema reference {reference} does not resolve to an object")
    siblings = {key: value for key, value in node.items() if key != "$ref"}
    return {**inlined, **siblings}


def _flatten_nullable(node: dict[str, object]) -> dict[str, object]:
    """Collapse an optional field so its format and bounds stay visible at one level."""
    variants = node.get("anyOf")
    if not isinstance(variants, list) or len(variants) != NULLABLE_VARIANTS:
        return node
    if not all(isinstance(variant, dict) for variant in variants):
        return node
    typed = cast("list[dict[str, object]]", variants)
    present = [variant for variant in typed if variant.get("type") != NULL_TYPE]
    if len(present) != 1 or len(typed) - len(present) != 1:
        return node
    value_type = present[0].get("type")
    if not isinstance(value_type, str):
        return node
    siblings = {key: value for key, value in node.items() if key != "anyOf"}
    return {**present[0], **siblings, "type": [value_type, NULL_TYPE]}


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
