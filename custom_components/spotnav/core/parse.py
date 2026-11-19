"""Small parsers for stored and reported values, shared by the domain packages. Pure: no Home Assistant import.

Each variant is explicit about its edge cases, so a caller picks the one that matches what it reads:

- `is_number(value)`: a real finite JSON number (`int` or `float`); a boolean, NaN, infinity and text are not.
- `finite_number(value)`: the same number as a float, else `None`.
- `number_from_state(state)`: a state's text as a finite float, else `None` (no state, no number, NaN, infinity).
- `aware_instant(raw)`: an ISO text with a UTC offset, keeping that offset; else `None`.
- `aware_utc(raw)`: the same instant converted to UTC.

A time without an offset is never guessed: it is `None` (a naive value compared with an aware one raises).
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any


def is_number(value: Any) -> bool:
    """Whether `value` is an `int` or `float` that is finite; `True`/`False`, NaN, infinity and text are not."""
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


def finite_number(value: Any) -> float | None:
    """A finite `int` or `float` as a float, else `None` (a boolean, NaN, infinity and text included)."""
    return float(value) if is_number(value) else None


def number_from_state(state: str | None) -> float | None:
    """A state's text as a finite float, else `None`; `None`, text that is no number, "nan" and "inf" give `None`."""
    if state is None:
        return None
    try:
        value = float(state)
    except TypeError, ValueError:
        return None
    return value if math.isfinite(value) else None


def aware_instant(raw: Any) -> datetime | None:
    """An ISO 8601 text with a UTC offset as that datetime (its offset kept); `None` for anything else, a naive
    time included."""
    if not isinstance(raw, str):
        return None
    try:
        moment = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return None if moment.tzinfo is None else moment


def aware_utc(raw: Any) -> datetime | None:
    """`aware_instant(raw)` converted to UTC."""
    moment = aware_instant(raw)
    return None if moment is None else moment.astimezone(timezone.utc)
