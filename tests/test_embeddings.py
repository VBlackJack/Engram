# Copyright 2026 Julien Bombled
# SPDX-License-Identifier: Apache-2.0

"""OpenAI-compatible HTTP embedding provider tests."""

from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace

import httpx
import pytest

import engram.embeddings as embeddings_module
from engram.config import RetrievalConfig, RetrievalMode
from engram.embeddings import (
    MAX_EMBEDDING_BATCH_ITEMS,
    MAX_EMBEDDING_INPUT_CHARS,
    MAX_EMBEDDING_RESPONSE_BYTES,
    MAX_EMBEDDING_VECTOR_DIMENSIONS,
    EmbeddingError,
    HttpEmbeddingProvider,
)


class _UnreadSentinelStream(httpx.SyncByteStream):
    """Expose whether a compressed response body was touched."""

    def __init__(self) -> None:
        self.iterated = False

    def __iter__(self) -> Iterator[bytes]:
        self.iterated = True
        raise AssertionError("compressed response body must not be read")


def test_http_embedding_provider_uses_openai_compatible_batch_contract() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "http://127.0.0.1:1234/v1/embeddings"
        assert request.read() == b'{"model":"mock-model","input":["first","second"]}'
        return httpx.Response(
            200,
            json={
                "data": [
                    {"index": 1, "embedding": [0.0, 1.0]},
                    {"index": 0, "embedding": [1.0, 0.0]},
                ]
            },
        )

    config = RetrievalConfig(
        mode=RetrievalMode.HYBRID,
        embeddings_model="mock-model",
    )
    provider = HttpEmbeddingProvider(config, transport=httpx.MockTransport(handler))

    vectors = provider.embed(("first", "second"))

    assert vectors == ((1.0, 0.0), (0.0, 1.0))


def test_http_embedding_provider_normalizes_invalid_url_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = HttpEmbeddingProvider(
        RetrievalConfig(
            mode=RetrievalMode.HYBRID,
            embeddings_model="mock-model",
        )
    )
    monkeypatch.setattr(
        provider,
        "_endpoint",
        "http://127.0.0.1:notaport/v1/embeddings",
    )

    with pytest.raises(EmbeddingError, match="request failed"):
        provider.embed(("input",))


def test_http_embedding_provider_rejects_zero_norm_vectors() -> None:
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            json={"data": [{"index": 0, "embedding": [0.0, 0.0]}]},
        )
    )
    provider = HttpEmbeddingProvider(
        RetrievalConfig(
            mode=RetrievalMode.HYBRID,
            embeddings_model="mock-model",
        ),
        transport=transport,
    )

    with pytest.raises(EmbeddingError, match="non-zero norm"):
        provider.embed(("zero",))


def test_http_embedding_provider_rejects_values_outside_float32() -> None:
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            json={"data": [{"index": 0, "embedding": [1e300, 1.0]}]},
        )
    )
    provider = HttpEmbeddingProvider(
        RetrievalConfig(
            mode=RetrievalMode.HYBRID,
            embeddings_model="mock-model",
        ),
        transport=transport,
    )

    with pytest.raises(EmbeddingError, match="fit in float32"):
        provider.embed(("oversized",))


def test_http_embedding_provider_rejects_oversized_batch_before_http() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500)

    provider = HttpEmbeddingProvider(
        RetrievalConfig(mode=RetrievalMode.HYBRID, embeddings_model="mock-model"),
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(EmbeddingError, match="batch exceeds"):
        provider.embed(tuple("text" for _ in range(MAX_EMBEDDING_BATCH_ITEMS + 1)))

    assert calls == 0


def test_http_embedding_provider_rejects_oversized_input_before_http() -> None:
    provider = HttpEmbeddingProvider(
        RetrievalConfig(mode=RetrievalMode.HYBRID, embeddings_model="mock-model"),
        transport=httpx.MockTransport(lambda _request: httpx.Response(500)),
    )

    with pytest.raises(EmbeddingError, match="embedding input"):
        provider.embed(("x" * (MAX_EMBEDDING_INPUT_CHARS + 1),))


def test_http_embedding_provider_rejects_declared_oversized_response() -> None:
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            content=b"{}",
            headers={"content-length": str(MAX_EMBEDDING_RESPONSE_BYTES + 1)},
        )
    )
    provider = HttpEmbeddingProvider(
        RetrievalConfig(mode=RetrievalMode.HYBRID, embeddings_model="mock-model"),
        transport=transport,
    )

    with pytest.raises(EmbeddingError, match="response body is too large"):
        provider.embed(("bounded",))


def test_http_embedding_provider_rejects_compressed_response_before_decode() -> None:
    stream = _UnreadSentinelStream()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["accept-encoding"] == "identity"
        return httpx.Response(
            200,
            stream=stream,
            headers={"content-encoding": "gzip"},
        )

    provider = HttpEmbeddingProvider(
        RetrievalConfig(mode=RetrievalMode.HYBRID, embeddings_model="mock-model"),
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(EmbeddingError, match="compressed"):
        provider.embed(("bounded",))
    assert stream.iterated is False


def test_http_embedding_provider_rejects_oversized_vector_dimension() -> None:
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            json={
                "data": [
                    {
                        "index": 0,
                        "embedding": [1.0] * (MAX_EMBEDDING_VECTOR_DIMENSIONS + 1),
                    }
                ]
            },
        )
    )
    provider = HttpEmbeddingProvider(
        RetrievalConfig(mode=RetrievalMode.HYBRID, embeddings_model="mock-model"),
        transport=transport,
    )

    with pytest.raises(EmbeddingError, match="dimensions"):
        provider.embed(("bounded",))


def test_http_embedding_provider_normalizes_huge_json_integer_overflow() -> None:
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            json={
                "data": [
                    {
                        "index": 0,
                        "embedding": [10**400],
                    }
                ]
            },
        )
    )
    provider = HttpEmbeddingProvider(
        RetrievalConfig(mode=RetrievalMode.HYBRID, embeddings_model="mock-model"),
        transport=transport,
    )

    with pytest.raises(EmbeddingError, match="fit in float32"):
        provider.embed(("bounded",))


def test_http_embedding_provider_checks_deadline_after_vector_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock_values = iter((0.0, 0.0, 0.0, 0.0, 1.0))
    monkeypatch.setattr(
        embeddings_module,
        "time",
        SimpleNamespace(monotonic=lambda: next(clock_values)),
    )
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            json={
                "data": [
                    {
                        "index": 0,
                        "embedding": [1.0, 0.0],
                    }
                ]
            },
        )
    )
    provider = HttpEmbeddingProvider(
        RetrievalConfig(
            mode=RetrievalMode.HYBRID,
            embeddings_model="mock-model",
            embeddings_timeout_ms=100,
        ),
        transport=transport,
    )

    with pytest.raises(EmbeddingError, match="deadline expired"):
        provider.embed(("bounded",))
