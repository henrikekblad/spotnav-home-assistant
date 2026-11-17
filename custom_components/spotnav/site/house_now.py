"""`spotnav/get_house_now`'s answer (API v1): the house card's live flows, for its "Now" tab (SpotNav 3.0).

Pure: the shell (`api/house_now.py`) reads each figure and hands it over; nothing is read or written here.

    {api_version: 1, site_id, at, grid_kw, sun_kw, sun_from, battery_kw, soc_pct, car_kw, car_connected, car_soc_pct,
     heat_kw, heat_from, house_kw, has: {sun, battery, car, heat}}

* `grid_kw`: the site's net grid power now (+ import, − export), as load balancing and solar read it; `null` unread.
* `sun_kw`: the sun's production now: the Energy dashboard's solar power sensors summed (`sun_from: "power"`), else
  the mean of the last period the energy flow read from the recorder's 5-minute statistics (`"recent"`, at most
  `RECENT_MAX` old); `null` (and `sun_from` `null`) without either, or without the sun (`has.sun` false).
* `battery_kw`: the home battery's power (+ charging, − discharging), `soc_pct` its level; `null` unread.
* `car_kw`: what the site's chargers draw now; `null` unread or without chargers. `car_connected`: whether a car is
  plugged into any of them (`null` without chargers or unread); `car_soc_pct` the plugged-in car's level where its
  plan's vehicle can be read (the first charger's with one).
* `heat_kw`: the heat pump's meter where it is a power sensor (`heat_from: "power"`), else the last period read
  (`"recent"`); part of the house, never added to it.
* `house_kw`: the whole house (the heat included): the grid, the sun and the battery's discharge less the battery's
  charge and the cars, never below 0; `null` when a figure it needs is unread.
* `has`: which parts the site has, so the card draws only those (a part that is had but unread is drawn without a
  figure).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final

HOUSE_NOW_API_VERSION: Final = 1
NOW_KEYS: Final = (
    "api_version",
    "site_id",
    "at",
    "grid_kw",
    "sun_kw",
    "sun_from",
    "battery_kw",
    "soc_pct",
    "car_kw",
    "car_connected",
    "car_soc_pct",
    "heat_kw",
    "heat_from",
    "house_kw",
    "has",
)
#: The last period read stands in for a figure without a power sensor only while it ended this recently.
RECENT_MAX: Final = timedelta(minutes=20)
FROM_POWER: Final = "power"
FROM_RECENT: Final = "recent"


@dataclass(frozen=True, slots=True)
class HouseNowInputs:
    site_id: str
    at: datetime
    grid_kw: float | None
    sun_kw: float | None
    sun_from: str | None
    battery_kw: float | None
    soc_pct: float | None
    car_kw: float | None
    heat_kw: float | None
    heat_from: str | None
    has_sun: bool
    has_battery: bool
    has_car: bool
    has_heat: bool
    car_connected: bool | None = None
    car_soc_pct: float | None = None


def recent_kw(kwh: float | None, minutes: int, end: datetime, now: datetime) -> float | None:
    """A period's mean power (kW) when it ended within `RECENT_MAX` of `now`, else `None`."""
    if kwh is None or minutes <= 0 or now - end > RECENT_MAX:
        return None
    return kwh * 60.0 / minutes


def _r(value: float | None, digits: int = 2) -> float | None:
    return None if value is None else round(value, digits)


def house_kw(inputs: HouseNowInputs) -> float | None:
    """The whole house now (see the module), `None` when a figure it needs is unread."""
    if inputs.grid_kw is None:
        return None
    parts = [inputs.grid_kw]
    if inputs.has_sun:
        if inputs.sun_kw is None:
            return None
        parts.append(inputs.sun_kw)
    if inputs.has_battery:
        if inputs.battery_kw is None:
            return None
        parts.append(-inputs.battery_kw)
    if inputs.has_car:
        if inputs.car_kw is None:
            return None
        parts.append(-inputs.car_kw)
    return max(0.0, sum(parts))


def house_now(inputs: HouseNowInputs) -> dict[str, Any]:
    """The answer (see the module)."""
    sun = inputs.sun_kw if inputs.has_sun else None
    heat = inputs.heat_kw if inputs.has_heat else None
    return {
        "api_version": HOUSE_NOW_API_VERSION,
        "site_id": inputs.site_id,
        "at": inputs.at.isoformat(),
        "grid_kw": _r(inputs.grid_kw),
        "sun_kw": _r(None if sun is None else max(0.0, sun)),
        "sun_from": None if sun is None else inputs.sun_from,
        "battery_kw": _r(inputs.battery_kw if inputs.has_battery else None),
        "soc_pct": _r(inputs.soc_pct if inputs.has_battery else None, 1),
        "car_kw": _r(None if not inputs.has_car or inputs.car_kw is None else max(0.0, inputs.car_kw)),
        "car_connected": inputs.car_connected if inputs.has_car else None,
        "car_soc_pct": _r(inputs.car_soc_pct if inputs.has_car and inputs.car_connected else None, 1),
        "heat_kw": _r(None if heat is None else max(0.0, heat)),
        "heat_from": None if heat is None else inputs.heat_from,
        "house_kw": _r(house_kw(inputs)),
        "has": {
            "sun": inputs.has_sun,
            "battery": inputs.has_battery,
            "car": inputs.has_car,
            "heat": inputs.has_heat,
        },
    }
