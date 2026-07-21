# Copyright 2026 Julien Bombled
# SPDX-License-Identifier: Apache-2.0

"""Public release metadata invariants."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

from engram import __version__

ROOT = Path(__file__).resolve().parents[1]


def test_public_versions_are_aligned() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    manifest = json.loads((ROOT / "server.json").read_text(encoding="utf-8"))

    assert pyproject["project"]["version"] == __version__
    assert manifest["version"] == __version__
    assert manifest["packages"][0]["version"] == __version__


def test_documentation_trees_are_mirrored() -> None:
    french = {path.name for path in (ROOT / "docs" / "fr").glob("*.md")}
    english = {path.name for path in (ROOT / "docs" / "en").glob("*.md")}

    assert french == english
    assert french == {
        "architecture.md",
        "client-protocol.md",
        "faq.md",
        "index.md",
        "installation-windows.md",
        "security.md",
        "setup.md",
        "spec.md",
        "user-guide.md",
    }
