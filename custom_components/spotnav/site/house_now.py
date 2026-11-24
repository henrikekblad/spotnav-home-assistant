"""`spotnav/get_house_now`'s answer (API v1): the house card's live flows, for its "Now" tab (SpotNav 3.0).

Pure: the shell (`api/house_now.py`) reads each figure and hands it over; nothing is read or written here.

    {api_version: 1, site_id, at, grid_kw, sun_kw, sun_from, battery_kw, soc_pct, car_kw, car_connected, car_soc_pct,
     heat_kw, heat_from, house_kw, has: {sun, battery, car, heat}, battery_ac_kw, house_from}

* `grid_kw`: the site's net grid power now (+ import, − export), as load balancing and solar read it; `null` unread.
* `sun_kw`: the sun's production now: the Energy dashboard's solar power sensors summed (`sun_from: "power"`), else
  the mean of the last period the energy flow read from the recorder's 5-minute statistics (`"recent"`, at most
  `RECENT_MAX` old); `null` (and `sun_from` `null`) without either, or without the sun (`has.sun` false).
* `battery_kw`: the home battery's power as read (+ charging, − discharging), what the battery's own app shows;
  `soc_pct` its level; `null` unread.
* `battery_ac_kw` (added 2026-11-24): the battery on the AC side, where the grid meter sees it, as the review and
  the plan count it. For a battery read on the AC side it is `battery_kw`; for one read at the battery (the DC side
  of a hybrid, `battery/conversion_loss.py`) `battery_kw` turned with the learnt (else assumed) loss,
  `conversion_loss.ac_kw` with the grid's share of a charge. `null` unread or without a battery. Since 2026-11-25
  the card draws `battery_kw` (see `house_kw`).
* `car_kw`: what the site's chargers draw now; `null` unread or without chargers. `car_connected`: whether a car is
  plugged into any of them (`null` without chargers or unread); `car_soc_pct` the plugged-in car's level where its
  plan's vehicle can be read (the first charger's with one).
* `heat_kw`: the heat pump's meter where it is a power sensor (`heat_from: "power"`), else the last period read
  (`"recent"`); part of the house, never added to it.
* `house_kw`: the whole house (the heat included), never below 0; `null` when a figure it needs is unread: the grid
  and the sun less the battery as read (`battery_kw`) and the cars, so the lines the card draws add up,
  `grid + sun = house + cars + battery` (before the house's floor of 0). On a hybrid that reads its battery at the
  battery the house so holds the inverter's loss and standing use (2026-11-25, as the energy flow's house does,
  `site/energy_flow.py`): the inverter uses that energy behind the grid's meter like an appliance (on the owner's
  Sigenergy plant about 0.6 kW while it discharges 10 kW, 0.2 kW standing). So it is a little more than a plant's
  own house load (Sigenergy's `plant_total_load_power`, which leaves the inverter out), and the battery's line is
  what its own app shows. `house_from` is `"balance"` (`null` unread): a plant's own load is no longer taken (it was,
  as `"plant"`, from 2026-11-24 in a 3.0 beta). The sun is as read.
* `has`: which parts the site has, so the card draws only those (a part that is had but unread is drawn without a
  figure).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final

from ..battery.conversion_loss import ac_kw

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
    "battery_ac_kw",
    "house_from",
)
#: The last period read stands in for a figure without a power sensor only while it ended this recently.
RECENT_MAX: Final = timedelta(minutes=20)
FROM_POWER: Final = "power"
FROM_RECENT: Final = "recent"
HOUSE_FROM_BALANCE: Final = "balance"


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
    #: The inverter's `(charge, discharge)` loss for a battery read at the battery (`conversion_loss.ConversionLoss
    #: .loss`), `None` for one read on the AC side.
    battery_loss: tuple[float, float] | None = None


def recent_kw(kwh: float | None, minutes: int, end: datetime, now: datetime) -> float | None:
    """A period's mean power (kW) when it ended within `RECENT_MAX` of `now`, else `None`."""
    if kwh is None or minutes <= 0 or now - end > RECENT_MAX:
        return None
    return kwh * 60.0 / minutes


def _r(value: float | None, digits: int = 2) -> float | None:
    return None if value is None else round(value, digits)


def battery_ac_kw(inputs: HouseNowInputs) -> float | None:
    """The battery's power on the AC side turned from the reading (see the module), `None` unread."""
    if not inputs.has_battery or inputs.battery_kw is None:
        return None
    if inputs.battery_loss is None:
        return inputs.battery_kw
    return ac_kw(inputs.battery_kw, inputs.battery_loss, inputs.grid_kw)


def figures(inputs: HouseNowInputs) -> tuple[float | None, float | None, str | None]:
    """The house, the battery on the AC side and where the house is had from (see the module)."""
    battery = battery_ac_kw(inputs)
    if inputs.grid_kw is None:
        return None, battery, None
    parts = [inputs.grid_kw]
    if inputs.has_sun:
        if inputs.sun_kw is None:
            return None, battery, None
        parts.append(inputs.sun_kw)
    if inputs.has_car:
        if inputs.car_kw is None:
            return None, battery, None
        parts.append(-inputs.car_kw)
    if inputs.has_battery:
        if inputs.battery_kw is None:
            return None, battery, None
        # As read: the inverter's loss and standing use stay in the house (see the module).
        parts.append(-inputs.battery_kw)
    return max(0.0, sum(parts)), battery, HOUSE_FROM_BALANCE


def house_kw(inputs: HouseNowInputs) -> float | None:
    """The whole house now (see the module), `None` when a figure it needs is unread."""
    return figures(inputs)[0]


def house_now(inputs: HouseNowInputs) -> dict[str, Any]:
    """The answer (see the module)."""
    sun = inputs.sun_kw if inputs.has_sun else None
    heat = inputs.heat_kw if inputs.has_heat else None
    house, battery_ac, house_from = figures(inputs)
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
        "house_kw": _r(house),
        "has": {
            "sun": inputs.has_sun,
            "battery": inputs.has_battery,
            "car": inputs.has_car,
            "heat": inputs.has_heat,
        },
        "battery_ac_kw": _r(battery_ac),
        "house_from": house_from,
    }
