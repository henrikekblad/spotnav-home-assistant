"""`spotnav/get_house_now`'s answer (API v1): the house card's live flows, for its "Now" tab (SpotNav 3.0).

Pure: the shell (`api/house_now.py`) reads each figure and hands it over; nothing is read or written here.

    {api_version: 1, site_id, at, grid_kw, sun_kw, sun_from, battery_kw, soc_pct, car_kw, car_connected, car_soc_pct,
     heat_kw, heat_from, house_kw, has: {sun, battery, car, heat, pool}, battery_ac_kw, house_from, rest_kw, estimated,
     pool_kw, other_kw}

Every figure is in kW to the hundredth and balances as drawn (2026-11-27): `grid + sun = house + car + battery`
exactly, and the heat and the pool are parts of the house (`heat + rest = house`, `heat + pool + other = house`).

* `grid_kw`: the site's net grid power now (+ import, − export), as load balancing and solar read it; `null` unread.
* `sun_kw`: the sun's production now: the Energy dashboard's solar power sensors summed (`sun_from: "power"`), else
  the battery plant's own PV power where every solar source is the plant's (`"power"` too, `api/house_now.py`), else
  each solar source's PV power sensor on its energy sensor's device (`"power"`, `api/house_now.solar_live_ids`),
  else
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
* `heat_kw`: the heat now, the heat pump's meter and the heat zones together, each read from its power sensor
  (`heat_from: "power"`, `site/heat_meter.live_power_id`), else the last period read (`"recent"`); part of the
  house, never added to it, and never more than it.
* `house_kw`: the whole house (the heat included), never below 0; `null` when a figure it needs is unread: the grid
  and the sun less the battery as read (`battery_kw`) and the cars, so the lines the card draws add up,
  `grid + sun = house + cars + battery` (the floor of 0 kept by `estimated`'s rule). On a hybrid that reads its battery at the
  battery the house so holds the inverter's loss and standing use (2026-11-25, as the energy flow's house does,
  `site/energy_flow.py`): the inverter uses that energy behind the grid's meter like an appliance (on the owner's
  Sigenergy plant about 0.6 kW while it discharges 10 kW, 0.2 kW standing). So it is a little more than a plant's
  own house load (Sigenergy's `plant_total_load_power`, which leaves the inverter out), and the battery's line is
  what its own app shows. `house_from` is `"balance"` (`null` unread): a plant's own load is no longer taken (it was,
  as `"plant"`, from 2026-11-24 in a 3.0 beta). The sun is as read.
* `rest_kw` (added 2026-11-27): the house less its heat, what the card writes in the house's disc: the heat is drawn
  as its own line out of the house, as the energy flow has the heat beside the house (`site/energy_flow.py`), so the
  disc's figure and the lines out of it add up to the lines into it. With a site without heat it is `house_kw`.
* `estimated` (added 2026-11-27): the roles (`grid`, `sun`, `battery`, `car`, `heat`, `pool`) whose figure is not a reading
  of this moment, for the card to mark: the sun or the heat from the last period (`"recent"`, up to 20 minutes old),
  and any figure moved to make the lines meet. Meters are read at different moments, so the balance may come out
  below 0 (more going out than in); the deficit is then taken from the car's draw first (a charger's meter lags its
  ramp), then given to the sun, then to the battery, last to the grid, and the house is 0. A heat
  larger than the house is cut to the house. The grid meter is the anchor and is moved only when nothing else can.
* `pool_kw` (added 2026-11-27): what the pool's heat pump and circulation, the spa and the sauna draw now, those
  the heat's meter and zones do not already read (`api/house.heat_part_ids`), each by its own trusted power sensor,
  else while on at its power as it counts (`pool/runtime.live_draws`: an estimate, `pool` in `estimated`); `null`
  without them (`has.pool` false). A part of the house beside the heat, never larger than what the heat leaves of
  it (cut, and estimated, where it would be). Where the heat is the last period's it holds the parts, so the shell
  takes the pool's draw off it.
* `other_kw` (added 2026-11-27): the house less its heat and its pool, the rest of the house's ring on the card
  (`heat + pool + other = house`); `rest_kw` without a pool.
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
    "rest_kw",
    "estimated",
    "pool_kw",
    "other_kw",
)
#: The last period read stands in for a figure without a power sensor only while it ended this recently.
RECENT_MAX: Final = timedelta(minutes=20)
FROM_POWER: Final = "power"
FROM_RECENT: Final = "recent"
HOUSE_FROM_BALANCE: Final = "balance"
ROLE_GRID: Final = "grid"
ROLE_SUN: Final = "sun"
ROLE_BATTERY: Final = "battery"
ROLE_CAR: Final = "car"
ROLE_HEAT: Final = "heat"
ROLE_POOL: Final = "pool"
#: The roles `estimated` names, in this order.
ROLES: Final = (ROLE_GRID, ROLE_SUN, ROLE_BATTERY, ROLE_CAR, ROLE_HEAT, ROLE_POOL)


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
    #: The pool's, the spa's and the sauna's draw now (`pool_kw`), whether the site has them, and whether the draw is
    #: an estimate (a part without its own trusted power sensor).
    pool_kw: float | None = None
    has_pool: bool = False
    pool_estimated: bool = False


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


@dataclass(frozen=True, slots=True)
class Balanced:
    """The figures as drawn (see the module): the lines and the house balance to the hundredth."""

    grid: float | None
    sun: float | None
    battery: float | None
    car: float | None
    heat: float | None
    house: float | None
    rest: float | None
    estimated: tuple[str, ...]
    pool: float | None = None
    other: float | None = None


def _c(value: float | None) -> float | None:
    """A figure in hundredths, so the balance holds exactly as the answer rounds."""
    return None if value is None else round(value, 2)


def balanced(inputs: HouseNowInputs) -> Balanced:
    """The figures balanced (see the module's `estimated`): the house the residual of the grid, the sun, the battery
    and the cars, never below 0; a deficit taken from the figure least likely right; the heat fitted inside it."""
    grid = _c(inputs.grid_kw)
    sun = _c(None if not inputs.has_sun or inputs.sun_kw is None else max(0.0, inputs.sun_kw))
    battery = _c(inputs.battery_kw if inputs.has_battery else None)
    car = _c(None if not inputs.has_car or inputs.car_kw is None else max(0.0, inputs.car_kw))
    if inputs.has_car and car is None and inputs.car_connected is False:
        # No car plugged in draws nothing, its charger read or not (one may not be read yet after a restart).
        car = 0.0
    heat = _c(None if not inputs.has_heat or inputs.heat_kw is None else max(0.0, inputs.heat_kw))
    pool = _c(None if not inputs.has_pool or inputs.pool_kw is None else max(0.0, inputs.pool_kw))
    estimated: set[str] = set()
    if pool is not None and inputs.pool_estimated and pool > 0:
        estimated.add(ROLE_POOL)
    if sun is not None and inputs.sun_from == FROM_RECENT:
        estimated.add(ROLE_SUN)
    if heat is not None and inputs.heat_from == FROM_RECENT:
        estimated.add(ROLE_HEAT)
    needed = (
        grid is None
        or (inputs.has_sun and sun is None)
        or (inputs.has_car and car is None)
        or (inputs.has_battery and battery is None)
    )
    if needed:
        return Balanced(grid, sun, battery, car, heat, None, None, tuple(r for r in ROLES if r in estimated), pool, None)
    house = round(grid + (sun or 0.0) - (car or 0.0) - (battery or 0.0), 2)  # type: ignore[operator]
    if house < 0:
        # More goes out than comes in: readings taken at different moments. The deficit is taken from the car's
        # draw first (a charger's meter lags its ramp), then given to the sun (the slowest of the plant's figures,
        # or the last period's), then to the battery, and last to the grid, so the lines always meet.
        deficit = -house

        def take(role: str, room: float) -> float:
            nonlocal deficit
            moved = round(min(deficit, max(0.0, room)), 2)
            if moved > 0:
                estimated.add(role)
                deficit = round(deficit - moved, 2)
            return moved

        if car:
            car = round(car - take(ROLE_CAR, car), 2)
        if sun is not None and deficit > 0:
            sun = round(sun + take(ROLE_SUN, deficit), 2)
        if battery is not None and deficit > 0:
            battery = round(battery - take(ROLE_BATTERY, deficit), 2)
        if deficit > 0:
            grid = round(grid + take(ROLE_GRID, deficit), 2)  # type: ignore[operator]
        house = 0.0
    rest = house
    if heat is not None:
        if heat > house:
            # A part never larger than the whole: the heat's meter ran ahead of (or lags) the others.
            heat = house
            estimated.add(ROLE_HEAT)
        rest = round(house - heat, 2)
    other = rest
    if pool is not None:
        if pool > rest:
            # The pool's draw (often an estimate) never more than what the heat leaves of the house.
            pool = rest
            estimated.add(ROLE_POOL)
        other = round(rest - pool, 2)
    return Balanced(grid, sun, battery, car, heat, house, rest, tuple(r for r in ROLES if r in estimated), pool, other)


def figures(inputs: HouseNowInputs) -> tuple[float | None, float | None, str | None]:
    """The house, the battery on the AC side and where the house is had from (see the module)."""
    house = balanced(inputs).house
    return house, battery_ac_kw(inputs), None if house is None else HOUSE_FROM_BALANCE


def house_kw(inputs: HouseNowInputs) -> float | None:
    """The whole house now (see the module), `None` when a figure it needs is unread."""
    return figures(inputs)[0]


def house_now(inputs: HouseNowInputs) -> dict[str, Any]:
    """The answer (see the module)."""
    shown = balanced(inputs)
    house_from = None if shown.house is None else HOUSE_FROM_BALANCE
    return {
        "api_version": HOUSE_NOW_API_VERSION,
        "site_id": inputs.site_id,
        "at": inputs.at.isoformat(),
        "grid_kw": shown.grid,
        "sun_kw": shown.sun,
        "sun_from": None if shown.sun is None else inputs.sun_from,
        "battery_kw": shown.battery,
        "soc_pct": _r(inputs.soc_pct if inputs.has_battery else None, 1),
        "car_kw": shown.car,
        "car_connected": inputs.car_connected if inputs.has_car else None,
        "car_soc_pct": _r(inputs.car_soc_pct if inputs.has_car and inputs.car_connected else None, 1),
        "heat_kw": shown.heat,
        "heat_from": None if shown.heat is None else inputs.heat_from,
        "house_kw": shown.house,
        "has": {
            "sun": inputs.has_sun,
            "battery": inputs.has_battery,
            "car": inputs.has_car,
            "heat": inputs.has_heat,
            "pool": inputs.has_pool,
        },
        "battery_ac_kw": _r(battery_ac_kw(inputs)),
        "house_from": house_from,
        "rest_kw": shown.rest,
        "estimated": list(shown.estimated),
        "pool_kw": shown.pool,
        "other_kw": shown.other,
    }
