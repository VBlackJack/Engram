# Copyright 2026 Julien Bombled
# SPDX-License-Identifier: Apache-2.0

"""Bind every published input constraint to the enforcement the server performs."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import httpx
import pytest
from jsonschema import Draft202012Validator
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.types import Implementation
from starlette.applications import Starlette

from engram.config import AppConfig
from engram.models import EntryKind, EvidenceType
from engram.server import (
    OBSERVED_AT_EXAMPLES,
    RECALL_TOOL,
    REMEMBER_TOOL,
    create_mcp_server,
    publish_tool_schemas,
)
from engram.store import EngramStore

BASE_ARGUMENTS: Mapping[str, Mapping[str, object]] = {
    REMEMBER_TOOL: {"statement": "Contract probe statement.", "kind": "fact"},
    RECALL_TOOL: {"query": "contract"},
}
AWARE_INSTANT = "2026-07-31T14:05:00+02:00"
NAIVE_INSTANT = "2026-07-31T14:05:00"
UNIX_INSTANT = 1785000000


@dataclass(frozen=True)
class SchemaContract:
    """One published constraint, the values around it, and the keywords carrying it."""

    identifier: str
    tool: str
    property_name: str
    keywords: tuple[str, ...]
    accepted: object
    rejected: object


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@asynccontextmanager
async def _session(app: Starlette) -> AsyncIterator[ClientSession]:
    transport = httpx.ASGITransport(app=app)
    async with (
        httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8377") as client,
        streamable_http_client("http://127.0.0.1:8377/mcp", http_client=client) as (
            read_stream,
            write_stream,
            _,
        ),
        ClientSession(
            read_stream,
            write_stream,
            client_info=Implementation(name="contract-client", version="1.0"),
        ) as session,
    ):
        await session.initialize()
        yield session


def _payload(contract: SchemaContract, value: object) -> dict[str, object]:
    return {**BASE_ARGUMENTS[contract.tool], contract.property_name: value}


def _without_keywords(
    schema: Mapping[str, Any],
    property_name: str,
    keywords: tuple[str, ...],
) -> dict[str, Any]:
    """Return the same schema with one property stripped of the keywords under test."""
    properties = dict(schema["properties"])
    declared = properties[property_name]
    properties[property_name] = {
        key: value for key, value in declared.items() if key not in keywords
    }
    return {**schema, "properties": properties}


def _contracts(config: AppConfig) -> tuple[SchemaContract, ...]:
    capsule = config.capsule
    return (
        SchemaContract(
            identifier="token_budget-minimum",
            tool=RECALL_TOOL,
            property_name="token_budget",
            keywords=("minimum",),
            accepted=capsule.min_token_budget,
            rejected=capsule.min_token_budget - 1,
        ),
        SchemaContract(
            identifier="token_budget-maximum",
            tool=RECALL_TOOL,
            property_name="token_budget",
            keywords=("maximum",),
            accepted=capsule.max_token_budget,
            rejected=capsule.max_token_budget + 1,
        ),
        SchemaContract(
            identifier="kind-enum",
            tool=REMEMBER_TOOL,
            property_name="kind",
            keywords=("enum",),
            accepted=EntryKind.PROJECT_STATE.value,
            rejected="belief",
        ),
        SchemaContract(
            identifier="evidence-items",
            tool=REMEMBER_TOOL,
            property_name="evidence",
            keywords=("items",),
            accepted=[{"type": EvidenceType.REVIEW.value, "ref": "review/contract"}],
            rejected=["review/contract"],
        ),
        SchemaContract(
            identifier="observed_at-type",
            tool=REMEMBER_TOOL,
            property_name="observed_at",
            keywords=("type",),
            accepted=AWARE_INSTANT,
            rejected=UNIX_INSTANT,
        ),
    )


CONTRACT_IDENTIFIERS = (
    "token_budget-minimum",
    "token_budget-maximum",
    "kind-enum",
    "evidence-items",
    "observed_at-type",
)


@pytest.mark.anyio
@pytest.mark.parametrize("contract_id", CONTRACT_IDENTIFIERS)
async def test_published_constraint_matches_server_enforcement(
    app_config: AppConfig,
    sqlite_runtime_compatibility: None,
    contract_id: str,
) -> None:
    """Prove the declaration rejects, the keyword is load-bearing, and the server agrees."""
    del sqlite_runtime_compatibility
    contract = next(item for item in _contracts(app_config) if item.identifier == contract_id)
    published = publish_tool_schemas(app_config)[contract.tool]
    declared = published["properties"][contract.property_name]
    for keyword in contract.keywords:
        assert keyword in declared, f"{contract} does not publish {keyword}"

    validator = Draft202012Validator(published)
    accepted_payload = _payload(contract, contract.accepted)
    rejected_payload = _payload(contract, contract.rejected)
    assert validator.is_valid(accepted_payload)
    assert not validator.is_valid(rejected_payload)

    relaxed = Draft202012Validator(
        _without_keywords(published, contract.property_name, contract.keywords)
    )
    assert relaxed.is_valid(rejected_payload), (
        f"{contract} would still be rejected without {contract.keywords}, "
        "so the published keyword is not what enforces it"
    )

    with EngramStore(app_config) as store:
        server = create_mcp_server(app_config, store)
        app = server.streamable_http_app()
        async with app.router.lifespan_context(app), _session(app) as session:
            accepted_result = await session.call_tool(contract.tool, accepted_payload)
            rejected_result = await session.call_tool(contract.tool, rejected_payload)

    assert accepted_result.isError is False, f"server rejects a published-valid {contract}"
    assert rejected_result.isError is True, f"server accepts a published-invalid {contract}"


@pytest.mark.anyio
async def test_observed_at_offset_requirement_is_documented_and_enforced(
    app_config: AppConfig,
    sqlite_runtime_compatibility: None,
) -> None:
    """Cover the one rule JSON Schema cannot assert: the mandatory UTC offset."""
    del sqlite_runtime_compatibility
    published = publish_tool_schemas(app_config)[REMEMBER_TOOL]
    declared = published["properties"]["observed_at"]

    assert declared["format"] == "date-time"
    assert "offset" in declared["description"]
    assert declared["examples"] == list(OBSERVED_AT_EXAMPLES)
    assert all(example.endswith("Z") or example[-6] in "+-" for example in declared["examples"])

    with EngramStore(app_config) as store:
        server = create_mcp_server(app_config, store)
        app = server.streamable_http_app()
        async with app.router.lifespan_context(app), _session(app) as session:
            accepted = await session.call_tool(
                REMEMBER_TOOL,
                {**BASE_ARGUMENTS[REMEMBER_TOOL], "observed_at": AWARE_INSTANT},
            )
            rejected = await session.call_tool(
                REMEMBER_TOOL,
                {**BASE_ARGUMENTS[REMEMBER_TOOL], "observed_at": NAIVE_INSTANT},
            )

    assert accepted.isError is False
    assert rejected.isError is True


def test_published_schemas_carry_no_unresolved_reference(app_config: AppConfig) -> None:
    """Keep every constraint readable by a client that does not dereference."""
    for name, schema in publish_tool_schemas(app_config).items():
        assert "$defs" not in schema, f"{name} still publishes definitions"
        assert "$ref" not in _flatten_keys(schema), f"{name} still publishes a reference"


def test_published_schemas_declare_every_entry_kind(app_config: AppConfig) -> None:
    """Prove the enum reaches the client on both tools."""
    schemas = publish_tool_schemas(app_config)
    expected = [kind.value for kind in EntryKind]

    assert schemas[REMEMBER_TOOL]["properties"]["kind"]["enum"] == expected
    assert schemas[RECALL_TOOL]["properties"]["kinds"]["items"]["enum"] == expected


def test_published_token_budget_bounds_follow_configuration(app_config: AppConfig) -> None:
    """Prove the published bounds are read from configuration, not frozen in the schema."""
    declared = publish_tool_schemas(app_config)[RECALL_TOOL]["properties"]["token_budget"]

    assert declared["minimum"] == app_config.capsule.min_token_budget
    assert declared["maximum"] == app_config.capsule.max_token_budget
    assert str(app_config.capsule.default_token_budget) in declared["description"]


@pytest.mark.anyio
async def test_live_tool_listing_serves_the_published_schemas(
    app_config: AppConfig,
    sqlite_runtime_compatibility: None,
) -> None:
    """Anchor every other assertion to what a real client actually receives."""
    del sqlite_runtime_compatibility
    expected = publish_tool_schemas(app_config)

    with EngramStore(app_config) as store:
        server = create_mcp_server(app_config, store)
        app = server.streamable_http_app()
        async with app.router.lifespan_context(app), _session(app) as session:
            listing = await session.list_tools()

    served = {tool.name: tool.inputSchema for tool in listing.tools}
    assert served == expected


def _flatten_keys(node: object) -> set[str]:
    collected: set[str] = set()
    if isinstance(node, dict):
        collected |= {str(key) for key in node}
        for value in node.values():
            collected |= _flatten_keys(value)
    elif isinstance(node, list):
        for item in node:
            collected |= _flatten_keys(item)
    return collected
