"""The history profile built from held day files, as the relay builds its `profile.json`.

A port of `spotnav-relay/internal/profile/profile.go` (`BuildZones`), rule for rule, so the same day
files give the same [PriceProfile] to the last decimal:

* the window is the 28 **market** days ending with the market's today (`now` in `market_tz`);
* each held file in it adds its positional prices, stepped from its first instant by its resolution
  on the absolute timeline (23-, 24- and 25-hour days alike), keyed by ISO weekday and hour of that
  instant in the **display** zone (`tz`);
* fewer than 21 usable files in the window is no profile at all;
* a weekday-hour with fewer than 8 values is left out (absent, never zero);
* each kept one is the median and the population standard deviation of its values, both rounded to
  5 decimals half away from zero (Go's `math.Round`, not Python's banker's `round`);
* `generated` is `now` in the display zone, whole seconds; `from`/`to` are the window, `weeks` is 4.

The documents are the market-day files as held (a parsed day file's `tz` is its market calendar),
keyed by their market date; a display day cut from two of them is not one. Pure: no `hass`, no clock,
no storage.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import date, datetime, timedelta
from types import MappingProxyType

from .. import time_zones
from .relay_contract import RELAY_PRICE_UNIT, SUPPORTED_VERSION, PriceDocument, PriceProfile, ProfileHour

# The relay's constants (`profile.go`), kept equal on purpose.
WINDOW_DAYS = 28
WEEKS = 4
MIN_DAYS = 21
MIN_SAMPLES = 8
_DECIMALS = 5


def profile_window(now: datetime, market_tz: str) -> tuple[date, date]:
    """The first and last market date of the window that ends with the market's date of [now]."""
    if now.tzinfo is None:
        raise ValueError("profile_window needs an aware instant")
    to_date = now.astimezone(time_zones.require_zone(market_tz)).date()
    return to_date - timedelta(days=WINDOW_DAYS - 1), to_date


def build_profile(
    area_id: str,
    tz: str,
    market_tz: str,
    now: datetime,
    documents: Mapping[date, PriceDocument],
) -> PriceProfile | None:
    """The profile of [area_id] from the held market-day [documents], or `None` when fewer than 21 of
    them fall in the window. Files outside the window are ignored, and so is one with no prices.
    """
    display = time_zones.require_zone(tz)
    from_date, to_date = profile_window(now, market_tz)
    groups: dict[tuple[int, int], list[float]] = {}
    used = 0
    day = from_date
    while day <= to_date:
        document = documents.get(day)
        day += timedelta(days=1)
        if document is None or document.resolution_minutes <= 0 or not document.prices:
            continue
        step = timedelta(minutes=document.resolution_minutes)
        for index, price in enumerate(document.prices):
            at = (document.start_instant + step * index).astimezone(display)
            groups.setdefault((at.isoweekday(), at.hour), []).append(price)
        used += 1
    if used < MIN_DAYS:
        return None

    hours: dict[tuple[int, int], ProfileHour] = {}
    for key in sorted(groups):
        values = groups[key]
        if len(values) < MIN_SAMPLES:
            continue
        median, std = _stats(values)
        hours[key] = ProfileHour(median=_round(median), std=_round(std), n=len(values))
    return PriceProfile(
        version=SUPPORTED_VERSION,
        area_id=area_id,
        tz=tz,
        unit=RELAY_PRICE_UNIT,
        generated=now.astimezone(display).replace(microsecond=0),
        from_date=from_date,
        to_date=to_date,
        weeks=WEEKS,
        hours=MappingProxyType(hours),
    )


def _stats(values: list[float]) -> tuple[float, float]:
    """The median and the population standard deviation, summed in the relay's order (sorted, one
    value at a time) so the floating-point result is the same.
    """
    ordered = sorted(values)
    count = len(ordered)
    middle = count // 2
    median = ordered[middle] if count % 2 == 1 else (ordered[middle - 1] + ordered[middle]) / 2
    total = 0.0
    for value in ordered:
        total += value
    mean = total / count
    squares = 0.0
    for value in ordered:
        squares += (value - mean) * (value - mean)
    return median, math.sqrt(squares / count)


def _round(value: float) -> float:
    """Go's `math.Round(x*1e5) / 1e5`: half away from zero, on the scaled float as it is."""
    scale = math.pow(10, _DECIMALS)
    scaled = value * scale
    whole = math.trunc(scaled)
    if abs(scaled - whole) >= 0.5:
        whole += math.copysign(1.0, scaled)
    return whole / scale
