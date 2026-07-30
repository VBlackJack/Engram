# Copyright 2026 Julien Bombled
# SPDX-License-Identifier: Apache-2.0

"""Shared validation for vectors persisted as IEEE-754 float32 values."""

from __future__ import annotations

import math
from collections.abc import Sequence

FLOAT32_MAX = float.fromhex("0x1.fffffep+127")
MAX_VECTOR_DIMENSIONS = 8192


def is_usable_float32_vector(vector: Sequence[float]) -> bool:
    """Accept only finite, representable vectors with a non-zero cosine norm."""
    try:
        if not vector or len(vector) > MAX_VECTOR_DIMENSIONS:
            return False
        has_non_zero_value = False
        for value in vector:
            if isinstance(value, bool) or not isinstance(value, int | float):
                return False
            converted = float(value)
            if not math.isfinite(converted) or abs(converted) > FLOAT32_MAX:
                return False
            has_non_zero_value = has_non_zero_value or converted != 0
    except (OverflowError, TypeError, ValueError):
        return False
    else:
        return has_non_zero_value
