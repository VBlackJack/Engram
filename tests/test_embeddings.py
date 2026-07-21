# Copyright 2026 Julien Bombled
# SPDX-License-Identifier: Apache-2.0

"""OpenAI-compatible HTTP embedding provider tests."""

from __future__ import annotations

import httpx

from engram.config import RetrievalConfig, RetrievalMode
from engram.embeddings import HttpEmbeddingProvider


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
