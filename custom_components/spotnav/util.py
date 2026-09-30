"""Small conversions shared across the integration: one definition each."""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any


def finite_number(value: Any) -> float | None:
    """A finite float, or `None`: NaN, the infinities, booleans and non-numbers are all `None`."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def aware_iso(moment: datetime | None) -> str | None:
    """An offset-bearing ISO-8601 string, or `None`; a naive value would read as local time."""
    if moment is None or moment.tzinfo is None:
        return None
    return moment.isoformat()
