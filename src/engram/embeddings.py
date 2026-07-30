# Copyright 2026 Julien Bombled
# SPDX-License-Identifier: Apache-2.0

"""Minimal remote embedding provider for OpenAI-compatible HTTP endpoints."""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Protocol

import httpx

from .config import RetrievalConfig
from .vectors import FLOAT32_MAX


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
        inputs = tuple(texts)
        if not inputs:
            return ()
        try:
            with httpx.Client(
                transport=self._transport,
                timeout=self._timeout_seconds,
            ) as client:
                response = client.post(
                    self._endpoint,
                    json={"model": self._model, "input": list(inputs)},
                )
                response.raise_for_status()
                payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise EmbeddingError("embedding endpoint request failed") from exc
        return _parse_embeddings(payload, len(inputs))


def _parse_embeddings(payload: object, expected_count: int) -> tuple[tuple[float, ...], ...]:
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
        converted = float(value)
        if not math.isfinite(converted):
            raise EmbeddingError("embedding vector values must be finite")
        if abs(converted) > FLOAT32_MAX:
            raise EmbeddingError("embedding vector values must fit in float32")
        vector.append(converted)
    if not any(vector):
        raise EmbeddingError("embedding vector must have a non-zero norm")
    return tuple(vector)
