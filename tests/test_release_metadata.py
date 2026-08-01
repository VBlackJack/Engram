# Copyright 2026 Julien Bombled
# SPDX-License-Identifier: Apache-2.0

"""Public release metadata invariants."""

from __future__ import annotations

import json
import tomllib
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft7Validator

from engram import __version__

ROOT = Path(__file__).resolve().parents[1]
REGISTRY_SCHEMA = ROOT / "schemas" / "mcp-server-2025-12-11.schema.json"


def _manifest() -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads((ROOT / "server.json").read_text(encoding="utf-8"))
    return loaded


def _schema() -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(REGISTRY_SCHEMA.read_text(encoding="utf-8"))
    return loaded


def test_public_versions_are_aligned() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    manifest = json.loads((ROOT / "server.json").read_text(encoding="utf-8"))

    assert pyproject["project"]["version"] == __version__
    assert manifest["version"] == __version__
    assert manifest["packages"][0]["version"] == __version__


def test_the_registry_manifest_validates_against_the_schema_it_declares() -> None:
    """The registry rejects a bad manifest at submission; this rejects it at commit time."""
    schema = _schema()
    Draft7Validator.check_schema(schema)

    errors = sorted(
        Draft7Validator(schema).iter_errors(_manifest()),
        key=lambda error: list(error.absolute_path),
    )

    assert not errors, "\n".join(
        f"{'/'.join(str(part) for part in error.absolute_path) or '<root>'}: {error.message}"
        for error in errors
    )


def test_the_declared_schema_is_the_vendored_one() -> None:
    """A bumped `$schema` must bring its schema along, not validate against the old copy."""
    assert _manifest()["$schema"] == _schema()["$id"]
    assert REGISTRY_SCHEMA.name == "mcp-server-2025-12-11.schema.json"
    assert "2025-12-11" in _schema()["$id"]


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        ("drop-name", "name"),
        ("drop-version", "version"),
        ("drop-identifier", "identifier"),
        ("bad-transport", "type"),
    ],
)
def test_the_validation_would_reject_a_broken_manifest(mutation: str, expected: str) -> None:
    """A schema check that cannot fail proves the file parses, nothing more."""
    manifest = deepcopy(_manifest())
    package = manifest["packages"][0]
    if mutation == "drop-name":
        del manifest["name"]
    elif mutation == "drop-version":
        del manifest["version"]
    elif mutation == "drop-identifier":
        del package["identifier"]
    else:
        package["transport"] = {"type": "carrier-pigeon"}

    errors = list(Draft7Validator(_schema()).iter_errors(manifest))

    assert errors, f"the {mutation} manifest was accepted"
    assert any(
        expected in str(error.absolute_path) or expected in error.message for error in errors
    )


def test_the_schema_documents_the_registry_type_without_constraining_it() -> None:
    """Record what this gate cannot catch, so nobody reads a pass as more than it is.

    `registryType` carries examples rather than an enum, so a value the registry would
    never resolve still validates. Submission remains the first place that rejects it.
    """
    registry_type = _schema()["definitions"]["Package"]["properties"]["registryType"]
    assert "enum" not in registry_type
    assert "pypi" in registry_type["examples"]

    manifest = deepcopy(_manifest())
    manifest["packages"][0]["registryType"] = "a-registry-that-does-not-exist"

    assert not list(Draft7Validator(_schema()).iter_errors(manifest))


def test_documentation_trees_are_mirrored() -> None:
    french = {path.name for path in (ROOT / "docs" / "fr").glob("*.md")}
    english = {path.name for path in (ROOT / "docs" / "en").glob("*.md")}

    assert french == english
    assert french == {
        "architecture.md",
        "client-protocol.md",
        "datacron-cortex.md",
        "faq.md",
        "index.md",
        "installation-windows.md",
        "operator-guide.md",
        "quick-start.md",
        "security.md",
        "setup.md",
        "spec.md",
        "user-guide.md",
    }
