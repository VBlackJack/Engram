# Copyright 2026 Julien Bombled
# SPDX-License-Identifier: Apache-2.0

"""Data files that must travel inside the distribution.

The starting configuration used to exist only at the root of the repository, so
it reached the source archive and never the wheel. An installation that was not
a checkout therefore refused to start, naming a file the user had no way to
obtain. Anything the first run needs belongs here instead, where packaging
carries it.
"""

from __future__ import annotations

from importlib.resources import files

EXAMPLE_CONFIG_NAME = "engram.example.toml"


def example_config_text() -> str:
    """Return the starting configuration shipped with this distribution."""
    return (files(__name__) / EXAMPLE_CONFIG_NAME).read_text(encoding="utf-8")
