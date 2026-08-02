# Copyright 2026 Julien Bombled
# SPDX-License-Identifier: Apache-2.0

"""Module entry point, so an interpreter without a console can start Engram.

The console script installed by the distribution is a launcher executable bound
to a console subsystem. A windowed interpreter cannot run it without opening the
window this module exists to avoid, so it needs a target it can import instead.

A windowed interpreter also starts with no standard streams at all: `sys.stdout`
and its siblings are `None` rather than closed files. The transport stack does
not expect that. Uvicorn asks standard output whether it is a terminal while it
configures its own logging, before the server is built, so the daemon reaches the
point of announcing its endpoint and then dies on the next statement. Binding the
missing streams to the null device here keeps that failure from being the price
of removing the window, and costs nothing: the daemon's real record is the log
file, which is written either way.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import IO, Any

from .cli import main

__all__ = ["main"]

NULL_STREAM_MODES = {"stdin": "r", "stdout": "w", "stderr": "w"}


def bind_absent_standard_streams() -> tuple[str, ...]:
    """Give a windowless interpreter the streams the transport stack assumes.

    The streams are held for the lifetime of the process on purpose. They are
    the process-wide standard streams, so there is no scope at which closing
    them would be correct.
    """
    bound: list[str] = []
    for name, mode in NULL_STREAM_MODES.items():
        if getattr(sys, name, None) is not None:
            continue
        stream: IO[Any] = Path(os.devnull).open(mode, encoding="utf-8")  # noqa: SIM115
        setattr(sys, name, stream)
        bound.append(name)
    return tuple(bound)


if __name__ == "__main__":
    bind_absent_standard_streams()
    main()
