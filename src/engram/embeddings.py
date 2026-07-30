# Copyright 2026 Julien Bombled
# SPDX-License-Identifier: Apache-2.0

"""Minimal remote embedding provider for OpenAI-compatible HTTP endpoints."""

from __future__ import annotations

import json
import math
import time
from collections.abc import Sequence
from typing import Protocol

import httpx

from .config import RetrievalConfig
from .vectors import FLOAT32_MAX, MAX_VECTOR_DIMENSIONS

MAX_EMBEDDING_BATCH_ITEMS = 64
MAX_EMBEDDING_INPUT_CHARS = 32_768
MAX_EMBEDDING_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_EMBEDDING_VECTOR_DIMENSIONS = MAX_VECTOR_DIMENSIONS


class EmbeddingError(RuntimeError):
    """Raised when a remote embedding response cannot be used safely."""


class EmbeddingProvider(Protocol):
    """Provider contract independent from the HTTP implementation."""

    def embed(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        """Return one finite vector per input text in the same order."""
        ...


class HttpEmbeddingProvider:
    """Call a local OpenAI-compatible embeddings endpoint with strict timeouts."""

    def __init__(
        self,
        config: RetrievalConfig,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        """Store endpoint settings and an optional test transport."""
        self._endpoint = config.embeddings_endpoint
        self._model = config.embeddings_model
        self._timeout_seconds = config.embeddings_timeout_ms / 1000
        self._transport = transport

    def embed(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        """Embed a batch or raise one normalized provider error."""
        inputs = _validated_inputs(texts)
        if not inputs:
            return ()
        deadline = time.monotonic() + self._timeout_seconds
        payload = self._request_payload(inputs, deadline=deadline)
        vectors = _parse_embeddings(payload, len(inputs))
        if time.monotonic() >= deadline:
            raise EmbeddingError("embedding endpoint deadline expired")
        return vectors

    def _request_payload(
        self,
        inputs: tuple[str, ...],
        *,
        deadline: float,
    ) -> object:
        """Read and decode one bounded uncompressed provider response."""
        try:
            with (
                httpx.Client(
                    transport=self._transport,
                    timeout=self._timeout_seconds,
                ) as client,
                client.stream(
                    "POST",
                    self._endpoint,
                    headers={"accept-encoding": "identity"},
                    json={"model": self._model, "input": list(inputs)},
                ) as response,
            ):
                response.raise_for_status()
                _reject_compressed_response(response)
                _reject_oversized_content_length(response)
                body = bytearray()
                chunks = (response.content,) if response.is_stream_consumed else response.iter_raw()
                for chunk in chunks:
                    if time.monotonic() >= deadline:
                        raise EmbeddingError("embedding endpoint deadline expired")
                    if len(chunk) > MAX_EMBEDDING_RESPONSE_BYTES - len(body):
                        raise EmbeddingError("embedding response body is too large")
                    body.extend(chunk)
            if time.monotonic() >= deadline:
                raise EmbeddingError("embedding endpoint deadline expired")
            payload: object = json.loads(body)
            if time.monotonic() >= deadline:
                raise EmbeddingError("embedding endpoint deadline expired")
        except (
            httpx.HTTPError,
            httpx.InvalidURL,
            RecursionError,
            UnicodeError,
            ValueError,
        ) as exc:
            raise EmbeddingError("embedding endpoint request failed") from exc
        return payload


def _validated_inputs(texts: Sequence[str]) -> tuple[str, ...]:
    inputs = tuple(texts)
    if len(inputs) > MAX_EMBEDDING_BATCH_ITEMS:
        raise EmbeddingError(f"embedding batch exceeds {MAX_EMBEDDING_BATCH_ITEMS} items")
    if any(
        not isinstance(text, str) or not text or len(text) > MAX_EMBEDDING_INPUT_CHARS
        for text in inputs
    ):
        raise EmbeddingError(
            f"embedding input must be non-empty text under {MAX_EMBEDDING_INPUT_CHARS} characters"
        )
    return inputs


def _reject_compressed_response(response: httpx.Response) -> None:
    encoding = response.headers.get("content-encoding", "").strip().casefold()
    if encoding not in {"", "identity"}:
        raise EmbeddingError("compressed embedding responses are not accepted")


def _reject_oversized_content_length(response: httpx.Response) -> None:
    raw_length = response.headers.get("content-length")
    if raw_length is None or not raw_length.isdecimal():
        return
    if (
        len(raw_length) > len(str(MAX_EMBEDDING_RESPONSE_BYTES))
        or int(raw_length) > MAX_EMBEDDING_RESPONSE_BYTES
    ):
        raise EmbeddingError("embedding response body is too large")


def _parse_embeddings(  # noqa: C901
    payload: object,
    expected_count: int,
) -> tuple[tuple[float, ...], ...]:
    if not isinstance(payload, dict):
        raise EmbeddingError("embedding response must be an object")
    data = payload.get("data")
    if not isinstance(data, list) or len(data) != expected_count:
        raise EmbeddingError("embedding response count does not match the request")

    indexed: dict[int, tuple[float, ...]] = {}
    for item in data:
        if not isinstance(item, dict):
            raise EmbeddingError("embedding response item must be an object")
        index = item.get("index")
        values = item.get("embedding")
        if not isinstance(index, int) or isinstance(index, bool):
            raise EmbeddingError("embedding response index must be an integer")
        if not isinstance(values, list) or not values:
            raise EmbeddingError("embedding vector must not be empty")
        if len(values) > MAX_EMBEDDING_VECTOR_DIMENSIONS:
            raise EmbeddingError(
                f"embedding vector exceeds {MAX_EMBEDDING_VECTOR_DIMENSIONS} dimensions"
            )
        vector = _finite_vector(values)
        if index in indexed:
            raise EmbeddingError("embedding response contains a duplicate index")
        indexed[index] = vector

    expected_indexes = set(range(expected_count))
    if set(indexed) != expected_indexes:
        raise EmbeddingError("embedding response indexes are incomplete")
    dimensions = {len(vector) for vector in indexed.values()}
    if len(dimensions) != 1:
        raise EmbeddingError("embedding response dimensions are inconsistent")
    return tuple(indexed[index] for index in range(expected_count))


def _finite_vector(values: list[object]) -> tuple[float, ...]:
    vector: list[float] = []
    for value in values:
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise EmbeddingError("embedding vector values must be numbers")
        try:
            converted = float(value)
        except OverflowError as exc:
            raise EmbeddingError("embedding vector values must fit in float32") from exc
        if not math.isfinite(converted):
            raise EmbeddingError("embedding vector values must be finite")
        if abs(converted) > FLOAT32_MAX:
            raise EmbeddingError("embedding vector values must fit in float32")
        vector.append(converted)
    if not any(vector):
        raise EmbeddingError("embedding vector must have a non-zero norm")
    return tuple(vector)
