"""The one writer of a `"v": 1` day document outside the relay.

A provider that reads prices without the relay (Nord Pool through Home Assistant, later ENTSO-E) hands the
repository the same document the relay publishes, so `parse_day` stays the one validator, the store keeps one
format and nothing downstream knows where a day came from. This module writes it the relay's way
(`spotnav-relay/internal/format/format.go`):

* `tz` is the area's display zone, and `market_tz` is written only when the file's calendar is another zone (a
  Finnish file on the CET delivery day);
* `start` is the market day's first instant in the file's own calendar, `res` the step in minutes, `prices` the
  EUR/kWh values in order, rounded to five decimals;
* `fx` holds the area's own rate only, with `fx_date` and `fx_src`; an area trading in EUR, or a day with no rate
  close enough, gets an empty `fx` and no date (the planner then refuses to convert, as with the relay).

Pure: no `hass`, no clock; zones are read from `time_zones`' cache.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime
from typing import Any, Final

from .. import time_zones
from .relay_contract import RELAY_PRICE_UNIT, SUPPORTED_VERSION

#: The relay publishes EUR/kWh to five decimals.
PRICE_DECIMALS: Final = 5


def build_day_document(
    *,
    area_id: str,
    market_day: date,
    tz: str,
    market_tz: str,
    start: datetime,
    res: int,
    prices_eur_kwh: Sequence[float],
    fx: dict[str, float] | None,
    fx_date: date | None,
    fx_src: str | None,
    src: str,
    published: datetime | None,
    retrieved: datetime | None,
) -> dict[str, Any]:
    """A day document as the relay writes it. `start` must be aware; it is written in the file's calendar."""
    zone = time_zones.require_zone(market_tz)
    document: dict[str, Any] = {
        "v": SUPPORTED_VERSION,
        "area": area_id,
        "date": market_day.isoformat(),
        "tz": tz,
    }
    if market_tz != tz:
        document["market_tz"] = market_tz
    document.update(
        {
            "start": start.astimezone(zone).isoformat(),
            "res": res,
            "unit": RELAY_PRICE_UNIT,
            "prices": [round(value, PRICE_DECIMALS) for value in prices_eur_kwh],
            "fx": dict(fx or {}),
            "src": src,
        }
    )
    if fx and fx_date is not None:
        document["fx_date"] = fx_date.isoformat()
        document["fx_src"] = fx_src
    if retrieved is not None:
        document["retrieved"] = retrieved.astimezone(zone).isoformat()
    if published is not None:
        document["published"] = published.astimezone(zone).isoformat()
    return document
