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


def amps_at_floor(amps: float, floor: float, *, rounding: Any = math.floor) -> int | float:
    """`amps` as a current SpotNav writes: whole amperes (`rounding`: `math.floor` by default, `round` where a
    proposal is rounded), except where the charger's floor is not a whole ampere (a DC charger's lowest power,
    `execution/chargers/sigenergy.py`): a value at or above the floor that would round below it is the floor
    itself. Never above `amps` on that account, and a value below the floor stays below it (a pause stays a pause).
    For a floor that is a whole ampere (6 A) this is plain `int(rounding(amps))`, an `int` as before."""
    whole = int(rounding(amps))
    if whole < floor - 1e-9 <= amps:
        return float(floor)
    return whole
