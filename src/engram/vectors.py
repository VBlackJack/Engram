# Copyright 2026 Julien Bombled
# SPDX-License-Identifier: Apache-2.0

"""Shared validation for vectors persisted as IEEE-754 float32 values."""

from __future__ import annotations

import math
from collections.abc import Sequence

FLOAT32_MAX = float.fromhex("0x1.fffffep+127")


def is_usable_float32_vector(vector: Sequence[float]) -> bool:
    """Accept only finite, representable vectors with a non-zero cosine norm."""
    return (
        bool(vector)
        and all(math.isfinite(value) and abs(value) <= FLOAT32_MAX for value in vector)
        and any(vector)
    )
