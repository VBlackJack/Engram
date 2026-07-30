# Copyright 2026 Julien Bombled
# SPDX-License-Identifier: Apache-2.0

"""Deterministic hybrid retrieval contract without an embedding service."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from typing import cast

import httpx

from engram.config import RetrievalConfig, RetrievalMode
from engram.embeddings import HttpEmbeddingProvider
from engram.models import EntryKind, SourceType
from engram.retrieval import FtsRetriever, HybridRetriever, RetrievalRequest
from engram.store import EngramStore

HYBRID_CONTRACT_VERSION = "hybrid-mechanics-v1"
PROVIDER_ID = "alias-hash-v1"
PROVIDER_ENDPOINT = "http://hybrid-contract.invalid/v1/embeddings"
VECTOR_DIMENSIONS = 64
TOKEN_PATTERN = re.compile(r"[^\W_]+", flags=re.UNICODE)

ALIASES = {
    "brief": "concise",
    "database": "sqlite",
    "delivery": "deployment",
    "descriptors": "metadata",
    "engine": "storage",
    "guides": "documentation",
    "language": "french",
    "local": "embedded",
    "manuals": "documentation",
    "moliere": "french",
    "recap": "summary",
    "recovery": "rollback",
    "shipping": "release",
}

SEMANTIC_CASES = (
    (
        "release-summary",
        "Release summary stays concise.",
        "brief shipping recap",
    ),
    (
        "embedded-database",
        "SQLite remains embedded storage.",
        "local database engine",
    ),
    (
        "french-documentation",
        "French documentation guides.",
        "Moliere manuals language",
    ),
    (
        "rollback-metadata",
        "Rollback metadata accompanies deployment.",
        "recovery descriptors delivery",
    ),
)

LEXICAL_CASES = (
    (
        "api-port",
        "The API listens on port 8377.",
        "API port 8377",
    ),
    (
        "dashboard-theme",
        "Use Dracula theme for dashboards.",
        "Dracula dashboards",
    ),
)

DISTRACTORS = (
    "Operator approval controls trusted promotion.",
    "Immutable audit records preserve provenance.",
    "Expiration policy removes obsolete episodes.",
    "Capsule budgeting protects response structure.",
)


def _alias_hash_vector(text: str) -> tuple[float, ...]:
    """Map normalized aliases into a stable signed feature-hash vector."""
    vector = [0.0] * VECTOR_DIMENSIONS
    normalized = unicodedata.normalize("NFKC", text).casefold()
    for token in TOKEN_PATTERN.findall(normalized):
        canonical = ALIASES.get(token, token)
        digest = hashlib.sha256(canonical.encode("utf-8")).digest()
        bucket = int.from_bytes(digest[:2], byteorder="big") % VECTOR_DIMENSIONS
        sign = 1.0 if digest[2] & 1 else -1.0
        vector[bucket] += sign
    assert any(vector)
    return tuple(vector)


def _request(query: str) -> RetrievalRequest:
    return RetrievalRequest(
        query=query,
        scope="user",
        kinds=frozenset({EntryKind.FACT}),
        writer_model=f"{HYBRID_CONTRACT_VERSION}/test-client",
    )


def test_deterministic_hybrid_contract_uses_real_http_provider(
    store: EngramStore,
) -> None:
    calls: list[tuple[str, ...]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url == PROVIDER_ENDPOINT
        payload = cast("dict[str, object]", json.loads(request.content))
        assert set(payload) == {"input", "model"}
        assert payload["model"] == PROVIDER_ID
        raw_inputs = payload["input"]
        assert isinstance(raw_inputs, list)
        inputs: list[str] = []
        for item in raw_inputs:
            assert isinstance(item, str)
            inputs.append(item)
        batch = tuple(inputs)
        calls.append(batch)
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "index": index,
                        "embedding": list(_alias_hash_vector(batch[index])),
                    }
                    for index in range(len(batch) - 1, -1, -1)
                ]
            },
        )

    targets: dict[str, str] = {}
    for case_id, statement, _query in (*SEMANTIC_CASES, *LEXICAL_CASES):
        entry = store.add_attested(
            kind=EntryKind.FACT,
            scope="user",
            statement=statement,
            source_type=SourceType.TOOL_VERIFIED,
            claim_key=f"{HYBRID_CONTRACT_VERSION}/{case_id}",
        )
        targets[case_id] = entry.id
    for index, statement in enumerate(DISTRACTORS):
        store.add_attested(
            kind=EntryKind.FACT,
            scope="user",
            statement=statement,
            source_type=SourceType.TOOL_VERIFIED,
            claim_key=f"{HYBRID_CONTRACT_VERSION}/distractor-{index}",
        )

    config = RetrievalConfig(
        mode=RetrievalMode.HYBRID,
        embeddings_endpoint=PROVIDER_ENDPOINT,
        embeddings_model=PROVIDER_ID,
        fts_top_k=10,
        hybrid_max_candidates=32,
    )
    provider = HttpEmbeddingProvider(
        config,
        transport=httpx.MockTransport(handler),
    )
    lexical = FtsRetriever(store, config)
    hybrid = HybridRetriever(store, config, provider=provider)

    assert store.count_entries() == 10
    assert hybrid.rebuild_vectors() == 10
    assert len(store.list_vectors(PROVIDER_ID)) == 10

    for case_id, _statement, query in SEMANTIC_CASES:
        lexical_result = lexical.retrieve(_request(query))
        assert lexical_result.matches == ()
        assert lexical_result.notices == ()

        first = hybrid.retrieve(_request(query))
        second = hybrid.retrieve(_request(query))

        assert first.matches[0].id == targets[case_id]
        assert tuple(entry.id for entry in second.matches) == tuple(
            entry.id for entry in first.matches
        )
        assert first.notices == second.notices == ()

    for case_id, _statement, query in LEXICAL_CASES:
        lexical_result = lexical.retrieve(_request(query))
        assert lexical_result.matches[0].id == targets[case_id]
        assert lexical_result.notices == ()

        first = hybrid.retrieve(_request(query))
        second = hybrid.retrieve(_request(query))

        assert first.matches[0].id == targets[case_id]
        assert tuple(entry.id for entry in second.matches) == tuple(
            entry.id for entry in first.matches
        )
        assert first.notices == second.notices == ()

    expected_queries = tuple(
        query
        for _case_id, _statement, query in (*SEMANTIC_CASES, *LEXICAL_CASES)
        for _repeat in range(2)
    )
    assert len(calls[0]) == 10
    assert tuple(batch[0] for batch in calls[1:]) == expected_queries
    assert all(len(batch) == 1 for batch in calls[1:])
