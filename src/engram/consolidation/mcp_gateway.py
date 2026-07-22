# Copyright 2026 Julien Bombled
# SPDX-License-Identifier: Apache-2.0

"""Synchronous adapter over one initialized Datacron MCP stdio session."""

from __future__ import annotations

import asyncio
import json
import os
import re
from collections.abc import Sequence
from concurrent.futures import Future
from dataclasses import dataclass
from queue import Empty, Queue
from threading import Thread
from types import TracebackType
from typing import Self

from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.types import CallToolResult, TextContent

from engram.config import DatacronConfig

from .gateway import DatacronConflictError, DatacronGatewayError
from .models import ContradictionSignal, NeighborSection, NoteView

QUERY_TERM_PATTERN = re.compile(r"[\w]+", flags=re.UNICODE)
CONFLICT_MARKERS = ("conflict", "expected_hash", "hash mismatch", "already exists")
MISSING_MARKERS = ("not found", "does not exist", "no note")


@dataclass(frozen=True, slots=True)
class _ToolRequest:
    name: str
    arguments: dict[str, object]
    result: Future[CallToolResult]


class McpDatacronGateway:
    """Keep one Datacron subprocess/session alive for an entire CLI operation."""

    def __init__(self, config: DatacronConfig) -> None:
        """Retain the transport configuration until context entry."""
        self._config = config
        self._requests: Queue[_ToolRequest | None] = Queue()
        self._ready: Future[None] = Future()
        self._worker: Thread | None = None
        self._worker_error: BaseException | None = None

    def __enter__(self) -> Self:
        """Start Datacron and initialize one MCP client session."""
        self._worker = Thread(
            target=self._worker_main,
            name="engram-datacron-mcp",
            daemon=True,
        )
        self._worker.start()
        try:
            self._ready.result()
        except BaseException:
            self._worker.join()
            self._worker = None
            raise
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the MCP session, stdio transport, and event loop."""
        del exc_type, exc_value, traceback
        worker = self._worker
        if worker is None:
            return
        self._requests.put(None)
        worker.join()
        self._worker = None
        if self._worker_error is not None:
            raise DatacronGatewayError(
                f"Datacron MCP worker failed: {self._worker_error}"
            ) from self._worker_error

    def get_note(self, rel_path: str) -> NoteView | None:
        """Call Datacron ``get_note`` in full mode."""
        try:
            payload = self._call("get_note", {"id_or_path": rel_path, "format": "full"})
        except DatacronGatewayError as exc:
            if any(marker in str(exc).casefold() for marker in MISSING_MARKERS):
                return None
            raise
        content_hash = _required_payload_string(payload, "content_hash")
        content = _required_payload_string(payload, "content")
        title_value = payload.get("title")
        title = title_value if isinstance(title_value, str) and title_value else rel_path
        returned_path = payload.get("rel_path")
        return NoteView(
            rel_path=returned_path if isinstance(returned_path, str) else rel_path,
            title=title,
            content=content,
            content_hash=content_hash,
        )

    def search_neighbors(
        self,
        subject_keys: Sequence[str],
        scope: str,
        limit: int,
    ) -> tuple[NeighborSection, ...]:
        """Search subject terms and enrich hits with their current note hash."""
        terms = [term for key in subject_keys for term in QUERY_TERM_PATTERN.findall(key)]
        if not terms:
            terms = QUERY_TERM_PATTERN.findall(scope)
        query = " ".join(dict.fromkeys(term.casefold() for term in terms))
        if not query:
            return ()
        payload = self._call("search_text", {"query": query, "limit": limit})
        raw_results = payload.get("results")
        if not isinstance(raw_results, list):
            raise DatacronGatewayError("Datacron search_text returned no results array")
        neighbors: list[NeighborSection] = []
        seen: set[tuple[str, str]] = set()
        for search_rank, raw in enumerate(raw_results):
            if not isinstance(raw, dict):
                continue
            rel_path = raw.get("note_rel_path")
            if not isinstance(rel_path, str):
                continue
            note = self.get_note(rel_path)
            if note is None:
                continue
            heading_value = raw.get("section_title")
            heading = heading_value if isinstance(heading_value, str) else note.title
            key = (rel_path, heading)
            if key in seen:
                continue
            seen.add(key)
            snippet_value = raw.get("snippet")
            snippet = snippet_value if isinstance(snippet_value, str) else ""
            statement = _section_content(note.content, heading) or snippet
            neighbors.append(
                NeighborSection(
                    rel_path=rel_path,
                    heading=heading,
                    statement=statement,
                    subject_keys=tuple(subject_keys),
                    content_hash=note.content_hash,
                    heading_level=_heading_level(note.content, heading),
                    search_rank=search_rank,
                    excerpt=snippet,
                )
            )
        return tuple(neighbors[:limit])

    def contradiction_scan(self) -> ContradictionSignal | None:
        """Call the read-only Datacron contradiction scanner."""
        payload = self._call(
            "contradiction_scan",
            {"mode": "scan", "detail": "summary"},
        )
        count_value = payload.get("candidate_count")
        if not isinstance(count_value, int):
            candidates = payload.get("candidates")
            count_value = len(candidates) if isinstance(candidates, list) else 0
        return ContradictionSignal(
            candidate_count=count_value,
            summary=f"Datacron contradiction_scan found {count_value} candidate(s).",
        )

    def patch_section(
        self,
        rel_path: str,
        heading: str,
        new_content: str,
        expected_hash: str,
    ) -> str:
        """Call ``patch_note_section`` with mandatory CAS."""
        payload = self._call(
            "patch_note_section",
            {
                "rel_path": rel_path,
                "heading": heading,
                "new_content": new_content,
                "expected_hash": expected_hash,
            },
        )
        _require_indexed(payload, "patch_note_section")
        return _required_payload_string(payload, "content_hash")

    def create_note(self, rel_path: str, content: str) -> str:
        """Call ``create_note_ai`` with merged high-confidence provenance."""
        title, body = _split_title(content, rel_path)
        payload = self._call(
            "create_note_ai",
            {
                "rel_path": rel_path,
                "title": title,
                "body": body,
                "origin": "merged",
                "confidence": "high",
                "tags": ["engram"],
            },
        )
        _require_indexed(payload, "create_note_ai")
        return _required_payload_string(payload, "content_hash")

    def _call(self, tool_name: str, arguments: dict[str, object]) -> dict[str, object]:
        if self._worker is None or not self._worker.is_alive():
            raise DatacronGatewayError("Datacron MCP gateway is not open")
        pending: Future[CallToolResult] = Future()
        self._requests.put(_ToolRequest(tool_name, arguments, pending))
        try:
            result = pending.result()
        except Exception as exc:
            raise DatacronGatewayError(f"Datacron {tool_name} transport failed: {exc}") from exc
        return _decode_result(tool_name, result)

    def _worker_main(self) -> None:
        try:
            asyncio.run(self._serve())
        except Exception as exc:  # noqa: BLE001 - cross-thread transport boundary
            self._worker_error = exc
            if not self._ready.done():
                self._ready.set_exception(exc)
            self._fail_pending(exc)

    async def _serve(self) -> None:
        params = _server_parameters(self._config)
        async with (
            stdio_client(params) as (read_stream, write_stream),
            ClientSession(read_stream, write_stream) as session,
        ):
            await session.initialize()
            self._ready.set_result(None)
            while True:
                request = await asyncio.to_thread(self._requests.get)
                if request is None:
                    return
                try:
                    response = await session.call_tool(request.name, request.arguments)
                except Exception as exc:  # noqa: BLE001 - forward exact tool failure
                    request.result.set_exception(exc)
                else:
                    request.result.set_result(response)

    def _fail_pending(self, exc: BaseException) -> None:
        while True:
            try:
                request = self._requests.get_nowait()
            except Empty:
                return
            if request is not None:
                request.result.set_exception(exc)


def _server_parameters(config: DatacronConfig) -> StdioServerParameters:
    env = dict(os.environ)
    if config.vault_root is not None:
        env["DATACRON_VAULT_ROOT"] = str(config.vault_root)
    if config.read_paths:
        env["DATACRON_READ_PATHS"] = os.pathsep.join(str(path) for path in config.read_paths)
    env["DATACRON_WRITE_PATHS"] = os.pathsep.join(str(path) for path in config.write_paths)
    env["PYTHONUNBUFFERED"] = "1"
    return StdioServerParameters(command=config.command, args=list(config.args), env=env)


def _decode_result(tool_name: str, result: CallToolResult) -> dict[str, object]:
    response_text = _response_text(result)
    if result.isError:
        folded = response_text.casefold()
        error_type = (
            DatacronConflictError
            if any(marker in folded for marker in CONFLICT_MARKERS)
            else DatacronGatewayError
        )
        raise error_type(f"Datacron {tool_name} failed: {response_text}")
    try:
        decoded: object = json.loads(response_text)
    except json.JSONDecodeError as exc:
        raise DatacronGatewayError(f"Datacron {tool_name} returned invalid JSON") from exc
    if not isinstance(decoded, dict):
        raise DatacronGatewayError(f"Datacron {tool_name} returned a non-object payload")
    return {str(key): value for key, value in decoded.items()}


def _response_text(result: CallToolResult) -> str:
    for block in result.content:
        if isinstance(block, TextContent):
            return block.text
    raise DatacronGatewayError("Datacron MCP response contains no text block")


def _required_payload_string(payload: dict[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise DatacronGatewayError(f"Datacron payload has no {key}")
    return value


def _require_indexed(payload: dict[str, object], tool_name: str) -> None:
    if payload.get("indexed") is not True:
        raise DatacronGatewayError(f"Datacron {tool_name} did not confirm indexing")


def _split_title(content: str, rel_path: str) -> tuple[str, str]:
    lines = content.splitlines()
    if lines and lines[0].startswith("# "):
        return lines[0][2:].strip(), "\n".join(lines).strip() + "\n"
    return rel_path.rsplit("/", 1)[-1].removesuffix(".md"), content


def _section_content(content: str, heading: str) -> str:
    heading_pattern = re.compile(
        rf"^(?P<marks>#{{1,6}})\s+{re.escape(heading)}\s*$",
        flags=re.MULTILINE,
    )
    match = heading_pattern.search(content)
    if match is None:
        return ""
    level = len(match.group("marks"))
    following = re.compile(rf"^#{{1,{level}}}\s+", flags=re.MULTILINE).search(
        content,
        match.end(),
    )
    end = len(content) if following is None else following.start()
    body = content[match.end() : end].strip()
    lines = [
        line
        for line in body.splitlines()
        if not line.startswith("_Engram source:") and line.strip() != "</vault_content>"
    ]
    return "\n".join(lines).strip()


def _heading_level(content: str, heading: str) -> int:
    match = re.search(
        rf"^(?P<marks>#{{1,6}})\s+{re.escape(heading)}\s*$",
        content,
        flags=re.MULTILINE,
    )
    return len(match.group("marks")) if match is not None else 2
