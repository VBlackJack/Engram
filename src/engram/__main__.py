# Copyright 2026 Julien Bombled
# SPDX-License-Identifier: Apache-2.0

"""Module entry point, so an interpreter without a console can start Engram.

The console script installed by the distribution is a launcher executable bound
to a console subsystem. A windowed interpreter cannot run it without opening the
window this module exists to avoid, so it needs a target it can import instead.
"""

from __future__ import annotations

from .cli import main

__all__ = ["main"]

if __name__ == "__main__":
    main()
