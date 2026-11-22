"""`spotnav/get_plan`'s answer (API v1): every load SpotNav plans for a site on one time axis, against the ceiling
that applies (plan `plans/v3_flexible_loads_design.md`, step 2). Pure: the shell (`api/plan.py`) hands over what the
plans already hold (the cars' installed plans, the battery plan, the heat plan, the water heater's windows, the
zones' lowerings, the house forecast, the sun's forecast, the prices); nothing is planned, read or written here.

    {api_version: 1, site_id, time_zone, generated_at, money: {currency, major_unit, minor_unit} | null,
     window: {start, end, hours, prices_end},
     house: {basis: "net" | "gross", sun_in_house: bool},
     power_tariff: bool, would: bool,
     month_peak: {kw, start} | null,
     summary: {cost, peak_kw, peak_start, peak_ceiling_kw, peak_ceiling, over} ,
     hours: [{start, price, sell, house_kw, sun_kw, net_kw, ceiling_kw, ceiling}],
     loads: [{id, kind, name, state: "live" | "would", in_house, kw: [...], level: [...] | null,
              runs: [{start, end, what, reason, kw, kwh}], summary: {...}, does_not_fit: {...} | null}]}

* **The window** rolls: from the running local hour to the end of the prices held (today's, and tomorrow's once
  published), at most `MAX_HOURS`; `MIN_HOURS` from the running hour without prices. Never a calendar day.
* **`hours`**: `price` and `sell` the hour's mean buy (every fee) and sell price (minor per kWh, `null` unpriced);
  `house_kw` the house forecast (the whole house without the cars and the battery: heat and the water heater are in
  it), `sun_kw` the sun's forecast as the plans count on it (`null` without a solar forecast); `net_kw` from the grid
  (export negative): the house, the cars and the battery's power (+ charging), less the sun unless the house figure
  already has it taken off (`house.sun_in_house`). Every load counts in it as planned, also one only watched
  (`would`: the answer then says so). `ceiling_kw` and `ceiling` the lowest limit that applies to the hour:
  `import_target` or `power_level` (the hold the import target keeps), `power_tariff` (the soft ceiling under the
  demand charge), else `fuse` (the main fuse as total power); `null` where none is known.
* **`loads`**, in the site's order (`site/flexible_loads.py`): each car (`car`, kW per hour from its installed
  plan), the home battery (`home_battery`, + charging from the grid or the sun, - covering the house or selling),
  the heat pump (`heat`: `level` -1 lowered, 0 its own curve, +1 raised or pre-heated per hour, `kw` its expected
  heat where the meter's split knows it), each floor-heating zone (`heat_zone`: `level` -1 in its lowered hours, `kw`
  null), the plain water heater (`water_heater`: on in its windows at its learnt power). `in_house` loads are part of
  `house_kw` (drawn inside it, never added). `state` is `live` where SpotNav steers it now, `would` where it only
  watches (what it would do, never claimed as done). `runs` are the hours in words: contiguous hours with one
  `what` (`charge`, `grid_charge`, `sun_charge`, `cover`, `sell`, `on`, `lower`, `preheat`, `night`, `away`)
  and the plan's `reason` where it has one; the shed order under the ceiling (`site/shed_order.py`) shows as the
  water heater `paused` and the heat (and its watched zones) `lower`, each with the reason `ceiling`, over the step's
  hours (a watched load's lane says `would`). `does_not_fit`: a car's charge that does not fit under the ceiling that
  acts now by its departure, with the person's choices (`site/does_not_fit.py`, the same block as the charger's
  dashboard); `null` for every other load and for a car whose charge fits.
* **`summary`**: the window's import cost less export earnings (`cost`, minor units, over the priced hours), the
  highest hour from the grid and the ceiling at it (`over` when above); `month_peak` the month's highest measured
  hour so far, where the price has a demand charge or a power level (`power_tariff`).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Final

from ..battery.plan_model import (
    ACTION_DISCHARGE_HOUSE,
    ACTION_EXPORT_DISCHARGE,
    ACTION_GRID_CHARGE,
    ACTION_HOLD,
    ACTION_SHAVE,
)
from ..planning.demand_charge import hour_start
from ..planning.flexible_load import KIND_CAR, KIND_HEAT, KIND_HEAT_ZONE, KIND_HOME_BATTERY, KIND_WATER_HEATER

PLAN_API_VERSION: Final = 1
#: The window's hours at most (today's evening and all of tomorrow), and without prices.
MAX_HOURS: Final = 36
MIN_HOURS: Final = 24
HOUR: Final = timedelta(hours=1)

PLAN_KEYS: Final = (
    "api_version",
    "site_id",
    "time_zone",
    "generated_at",
    "money",
    "window",
    "house",
    "power_tariff",
    "would",
    "month_peak",
    "summary",
    "hours",
    "loads",
)
HOUR_KEYS: Final = ("start", "price", "sell", "house_kw", "sun_kw", "net_kw", "ceiling_kw", "ceiling")
LOAD_KEYS: Final = ("id", "kind", "name", "state", "in_house", "kw", "level", "runs", "summary", "does_not_fit")
RUN_KEYS: Final = ("start", "end", "what", "reason", "kw", "kwh")

STATE_LIVE: Final = "live"
STATE_WOULD: Final = "would"

CEILING_IMPORT_TARGET: Final = "import_target"
CEILING_POWER_LEVEL: Final = "power_level"
CEILING_POWER_TARIFF: Final = "power_tariff"
CEILING_FUSE: Final = "fuse"

WHAT_CHARGE: Final = "charge"
WHAT_GRID_CHARGE: Final = "grid_charge"
WHAT_SUN_CHARGE: Final = "sun_charge"
WHAT_COVER: Final = "cover"
WHAT_SELL: Final = "sell"
WHAT_ON: Final = "on"
WHAT_LOWER: Final = "lower"
WHAT_PAUSED: Final = "paused"
#: The reason of a step the shed order under the ceiling takes (`site/shed_order.REASON_CEILING`).
REASON_CEILING: Final = "ceiling"
#: The heat plan's actions shown as levels; `normal` and `return` are the pump's own curve (no run).
HEAT_LEVELS: Final = {"lower": -1, "night": -1, "away": -1, "preheat": 1, "normal": 0, "return": 0}
#: The battery's power under this (kW) is no run (a hold or rounding).
QUIET_KW: Final = 0.05


@dataclass(frozen=True, slots=True)
class PriceHour:
    start: datetime
    end: datetime
    buy: float
    sell: float


@dataclass(frozen=True, slots=True)
class CarPlan:
    id: str
    name: str | None
    #: The installed plan's windows: `(start, end, kW)`.
    windows: tuple[tuple[datetime, datetime, float], ...]
    live: bool
    departure: datetime | None = None
    #: The charge does not fit under the ceiling by its departure (`site/does_not_fit.py`), else `None`.
    does_not_fit: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class BatteryPlan:
    id: str
    name: str | None
    #: The plan in force's slots: `(start, end, action, battery kW (+ charging))`.
    slots: tuple[tuple[datetime, datetime, str, float], ...]
    live: bool


@dataclass(frozen=True, slots=True)
class HeatPlan:
    id: str
    name: str | None
    #: The heat plan's hours: `(start, action, reason)`.
    hours: tuple[tuple[datetime, str, str], ...]
    live: bool
    #: The heat pump's own heat per hour (kW), `None` where unknown.
    heat_kw: Callable[[datetime], float | None] = lambda _hour: None
    #: The share of the heat a lowered hour takes away (`relay_effect.Effect.lower_share`).
    lower_share: float = 0.0
    #: Lowered one step for the ceiling by the shed order (`site/shed_order.py`): `(since, until)`.
    shed: tuple[datetime, datetime] | None = None


@dataclass(frozen=True, slots=True)
class ZonePlan:
    id: str
    name: str | None
    lowered: tuple[tuple[datetime, datetime], ...]
    live: bool


@dataclass(frozen=True, slots=True)
class WaterHeaterPlan:
    id: str
    name: str | None
    #: The windows it may heat in: `(start, end, reason)`.
    windows: tuple[tuple[datetime, datetime, str], ...]
    kw: float | None
    live: bool
    #: Paused for the ceiling by the shed order (`site/shed_order.py`): `(since, until)`.
    shed: tuple[datetime, datetime] | None = None


@dataclass(frozen=True, slots=True)
class PlanInputs:
    site_id: str
    now: datetime
    tz: str
    prices: Sequence[PriceHour] = ()
    money: tuple[str | None, str | None, str | None] | None = None
    #: The house forecast per hour (kW, UTC hour starts): the whole house without the cars and the battery.
    house: Mapping[datetime, float] = field(default_factory=dict)
    house_basis: str = "net"
    #: The house figure has the sun's production taken off already (its hours are net of it).
    sun_in_house: bool = False
    #: The sun's forecast per hour (kWh = mean kW); `None` without a solar forecast.
    sun: Mapping[datetime, float] | None = None
    cars: tuple[CarPlan, ...] = ()
    battery: BatteryPlan | None = None
    heat: HeatPlan | None = None
    zones: tuple[ZonePlan, ...] = ()
    water_heater: WaterHeaterPlan | None = None
    #: The import target's hold per hour (kW) and which it is (`import_target` or `power_level`).
    hold_kw: Mapping[datetime, float] = field(default_factory=dict)
    hold_kind: str = CEILING_IMPORT_TARGET
    #: The soft ceiling under the demand charge per hour (kW).
    soft_kw: Mapping[datetime, float] = field(default_factory=dict)
    fuse_kw: float | None = None
    power_tariff: bool = False
    #: The month's highest measured hour so far: `(kW, start)`.
    month_peak: tuple[float, datetime] | None = None


def _r(value: float | None, digits: int = 3) -> float | None:
    return None if value is None else round(value, digits)


def window(now: datetime, tz: str, prices: Sequence[PriceHour]) -> list[datetime]:
    """The window's hour starts (see the module)."""
    first = hour_start(now, tz)
    end = max((price.end for price in prices), default=None)
    if end is None or end <= first + HOUR:
        count = MIN_HOURS
    else:
        count = min(MAX_HOURS, int((end - first).total_seconds() // 3600))
    return [first + HOUR * index for index in range(count)]


def _mean_in(start: datetime, end: datetime, parts: Sequence[tuple[datetime, datetime, float]]) -> float:
    """The mean of `(start, end, value)` parts over `[start, end)`, each for its share of it."""
    span = (end - start).total_seconds()
    total = 0.0
    for first, last, value in parts:
        overlap = (min(end, last) - max(start, first)).total_seconds()
        if overlap > 0:
            total += value * overlap / span
    return total


def _covers(start: datetime, spans: Sequence[tuple[datetime, datetime]]) -> float:
    """The share of the hour from `start` the spans cover (0 to 1)."""
    return min(1.0, _mean_in(start, start + HOUR, [(a, b, 1.0) for a, b in spans]))


def hour_prices(prices: Sequence[PriceHour], hours: Sequence[datetime]) -> dict[datetime, tuple[float, float]]:
    """The mean buy and sell price per hour of the slots that cover it whole."""
    out: dict[datetime, tuple[float, float]] = {}
    for start in hours:
        inside = [p for p in prices if p.start < start + HOUR and p.end > start]
        if not inside or _covers(start, [(p.start, p.end) for p in inside]) < 0.999:
            continue
        out[start] = (
            _mean_in(start, start + HOUR, [(p.start, p.end, p.buy) for p in inside]),
            _mean_in(start, start + HOUR, [(p.start, p.end, p.sell) for p in inside]),
        )
    return out


def _runs(
    hours: Sequence[datetime],
    what: Sequence[str | None],
    kw: Sequence[float | None],
    reason: Sequence[str | None] | None = None,
) -> list[dict[str, Any]]:
    """Contiguous hours with the same `what` (and reason) as runs, with their mean kW and energy."""
    runs: list[dict[str, Any]] = []
    index = 0
    while index < len(hours):
        current = what[index]
        why = None if reason is None else reason[index]
        if current is None:
            index += 1
            continue
        end = index
        while end + 1 < len(hours) and what[end + 1] == current and (reason is None or reason[end + 1] == why):
            end += 1
        values = [value for value in kw[index : end + 1] if value is not None]
        kwh = sum(abs(value) for value in values) if values else None
        runs.append(
            {
                "start": hours[index].isoformat(),
                "end": (hours[end] + HOUR).isoformat(),
                "what": current,
                "reason": why,
                "kw": _r(sum(values) / len(values), 2) if values else None,
                "kwh": _r(kwh, 2),
            }
        )
        index = end + 1
    return runs


def _load(
    plan_id: str,
    kind: str,
    name: str | None,
    live: bool,
    in_house: bool,
    kw: Sequence[float | None],
    level: Sequence[int] | None,
    runs: list[dict[str, Any]],
    summary: dict[str, Any],
    does_not_fit: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": plan_id,
        "kind": kind,
        "name": name,
        "state": STATE_LIVE if live else STATE_WOULD,
        "in_house": in_house,
        "kw": [_r(value, 2) for value in kw],
        "level": None if level is None else list(level),
        "runs": runs,
        "summary": summary,
        "does_not_fit": does_not_fit,
    }


def _car(car: CarPlan, hours: Sequence[datetime]) -> dict[str, Any]:
    kw = [_mean_in(start, start + HOUR, car.windows) for start in hours]
    what = [WHAT_CHARGE if value > QUIET_KW else None for value in kw]
    planned = [value for value in kw if value > QUIET_KW]
    return _load(
        car.id,
        KIND_CAR,
        car.name,
        car.live,
        False,
        kw,
        None,
        _runs(hours, what, kw),
        {"kwh": _r(sum(planned), 2), "departure": None if car.departure is None else car.departure.isoformat()},
        car.does_not_fit,
    )


def _battery_what(action: str, kw: float, sun_kw: float | None) -> str | None:
    if kw > QUIET_KW:
        if action == ACTION_GRID_CHARGE:
            return WHAT_GRID_CHARGE
        return WHAT_SUN_CHARGE if sun_kw is not None and sun_kw > QUIET_KW else WHAT_CHARGE
    if kw < -QUIET_KW:
        return WHAT_SELL if action == ACTION_EXPORT_DISCHARGE else WHAT_COVER
    # A hold, or the battery idle: nothing to say.
    return None


def _battery(battery: BatteryPlan, hours: Sequence[datetime], sun: Mapping[datetime, float] | None) -> dict[str, Any]:
    kw: list[float | None] = []
    what: list[str | None] = []
    strongest = (ACTION_GRID_CHARGE, ACTION_EXPORT_DISCHARGE, ACTION_SHAVE, ACTION_DISCHARGE_HOUSE, ACTION_HOLD)
    for start in hours:
        inside = [slot for slot in battery.slots if slot[0] < start + HOUR and slot[1] > start]
        if not inside:
            kw.append(None)
            what.append(None)
            continue
        value = _mean_in(start, start + HOUR, [(a, b, power) for a, b, _action, power in inside])
        actions = [action for _a, _b, action, _p in inside]
        action = next((item for item in strongest if item in actions), actions[0])
        kw.append(value)
        what.append(_battery_what(action, value, None if sun is None else sun.get(start)))
    charged = sum(value for value in kw if value is not None and value > 0)
    grid = sum(value for value, item in zip(kw, what, strict=True) if item == WHAT_GRID_CHARGE and value is not None)
    covered = -sum(value for value in kw if value is not None and value < 0)
    return _load(
        battery.id,
        KIND_HOME_BATTERY,
        battery.name,
        battery.live,
        False,
        kw,
        None,
        _runs(hours, what, kw),
        {"charge_kwh": _r(charged, 2), "grid_charge_kwh": _r(grid, 2), "cover_kwh": _r(covered, 2)},
    )


def _heat(heat: HeatPlan, hours: Sequence[datetime]) -> dict[str, Any]:
    by_start = {start: (action, reason) for start, action, reason in heat.hours}
    level: list[int] = []
    what: list[str | None] = []
    reason: list[str | None] = []
    kw: list[float | None] = []
    for start in hours:
        action, why = by_start.get(start, ("normal", None))
        if heat.shed is not None and _covers(start, [heat.shed]) > 0 and HEAT_LEVELS.get(action, 0) == 0:
            # Lowered one step for the ceiling where the plan leaves the hour normal.
            action, why = WHAT_LOWER, REASON_CEILING
        step = HEAT_LEVELS.get(action, 0)
        level.append(step)
        what.append(None if step == 0 else action)
        reason.append(None if step == 0 else why)
        own = heat.heat_kw(start)
        kw.append(None if own is None else (own * (1.0 - heat.lower_share) if action == WHAT_LOWER else own))
    return _load(
        heat.id,
        KIND_HEAT,
        heat.name,
        heat.live,
        True,
        kw,
        level,
        _runs(hours, what, kw, reason),
        {
            "lowered_hours": sum(1 for step in level if step < 0),
            "raised_hours": sum(1 for step in level if step > 0),
        },
    )


def _zone(zone: ZonePlan, hours: Sequence[datetime]) -> dict[str, Any]:
    level = [-1 if _covers(start, zone.lowered) >= 0.5 else 0 for start in hours]
    what = [WHAT_LOWER if step < 0 else None for step in level]
    none: list[float | None] = [None] * len(hours)
    return _load(
        zone.id,
        KIND_HEAT_ZONE,
        zone.name,
        zone.live,
        True,
        none,
        level,
        _runs(hours, what, none),
        {"lowered_hours": sum(1 for step in level if step < 0)},
    )


def _water_heater(heater: WaterHeaterPlan, hours: Sequence[datetime]) -> dict[str, Any]:
    shares = [_covers(start, [(a, b) for a, b, _reason in heater.windows]) for start in hours]
    reasons: list[str | None] = []
    what: list[str | None] = []
    for index, (start, share) in enumerate(zip(hours, shares, strict=True)):
        found = next((why for a, b, why in heater.windows if a < start + HOUR and b > start), None)
        paused = 0.0 if heater.shed is None or share <= 0 else _covers(start, [heater.shed])
        if paused > 0:
            # Paused for the ceiling: its window's hours less the pause.
            shares[index] = max(0.0, share - paused)
            what.append(WHAT_PAUSED)
            reasons.append(REASON_CEILING)
            continue
        what.append(WHAT_ON if share > 0 else None)
        reasons.append(found if share > 0 else None)
    kw = [None if heater.kw is None else heater.kw * share for share in shares]
    return _load(
        heater.id,
        KIND_WATER_HEATER,
        heater.name,
        heater.live,
        True,
        kw,
        None,
        _runs(hours, what, kw, reasons),
        {"on_hours": _r(sum(shares), 2), "kw": _r(heater.kw, 2)},
    )


def ceiling_at(inputs: PlanInputs, start: datetime) -> tuple[float | None, str | None]:
    """The lowest limit that applies to the hour, and which it is (see the module)."""
    found: list[tuple[float, str]] = []
    if start in inputs.hold_kw:
        found.append((inputs.hold_kw[start], inputs.hold_kind))
    if start in inputs.soft_kw:
        found.append((inputs.soft_kw[start], CEILING_POWER_TARIFF))
    if found:
        kw, kind = min(found, key=lambda item: item[0])
        return kw, kind
    if inputs.fuse_kw is not None and inputs.fuse_kw > 0:
        return inputs.fuse_kw, CEILING_FUSE
    return None, None


def plan_view(inputs: PlanInputs) -> dict[str, Any]:
    """The answer (see the module)."""
    hours = window(inputs.now, inputs.tz, inputs.prices)
    priced = hour_prices(inputs.prices, hours)
    sun = inputs.sun
    loads: list[dict[str, Any]] = [_car(car, hours) for car in inputs.cars]
    if inputs.battery is not None:
        loads.append(_battery(inputs.battery, hours, sun))
    if inputs.heat is not None:
        loads.append(_heat(inputs.heat, hours))
    loads.extend(_zone(zone, hours) for zone in inputs.zones)
    if inputs.water_heater is not None:
        loads.append(_water_heater(inputs.water_heater, hours))

    rows: list[dict[str, Any]] = []
    cost = 0.0
    priced_any = False
    peak: tuple[float, datetime, float | None, str | None] | None = None
    for index, start in enumerate(hours):
        house = inputs.house.get(start)
        sun_kw = None if sun is None else sun.get(start, 0.0)
        # The cars and the battery come on top of the house; the in-house loads are inside its figure.
        moved = sum(load["kw"][index] or 0.0 for load in loads if not load["in_house"])
        taken = 0.0 if inputs.sun_in_house or sun_kw is None else sun_kw
        net = None if house is None else house + moved - taken
        ceiling_kw, ceiling = ceiling_at(inputs, start)
        price = priced.get(start)
        if net is not None and price is not None:
            priced_any = True
            cost += net * price[0] if net >= 0 else net * price[1]
        if net is not None and (peak is None or net > peak[0]):
            peak = (net, start, ceiling_kw, ceiling)
        rows.append(
            {
                "start": start.isoformat(),
                "price": None if price is None else round(price[0], 2),
                "sell": None if price is None else round(price[1], 2),
                "house_kw": _r(house, 2),
                "sun_kw": _r(sun_kw, 2),
                "net_kw": _r(net, 2),
                "ceiling_kw": _r(ceiling_kw, 2),
                "ceiling": ceiling,
            }
        )
    money = inputs.money
    prices_end = max((price.end for price in inputs.prices), default=None)
    return {
        "api_version": PLAN_API_VERSION,
        "site_id": inputs.site_id,
        "time_zone": inputs.tz,
        "generated_at": inputs.now.isoformat(),
        "money": None if money is None else {"currency": money[0], "major_unit": money[1], "minor_unit": money[2]},
        "window": {
            "start": hours[0].isoformat(),
            "end": (hours[-1] + HOUR).isoformat(),
            "hours": len(hours),
            "prices_end": None if prices_end is None else prices_end.isoformat(),
        },
        "house": {"basis": inputs.house_basis, "sun_in_house": inputs.sun_in_house},
        "power_tariff": inputs.power_tariff,
        "would": any(load["state"] == STATE_WOULD and load["runs"] for load in loads),
        "month_peak": None
        if inputs.month_peak is None or not inputs.power_tariff
        else {"kw": round(inputs.month_peak[0], 2), "start": inputs.month_peak[1].isoformat()},
        "summary": {
            "cost": round(cost, 2) if priced_any else None,
            "peak_kw": None if peak is None else round(peak[0], 2),
            "peak_start": None if peak is None else peak[1].isoformat(),
            "peak_ceiling_kw": None if peak is None else _r(peak[2], 2),
            "peak_ceiling": None if peak is None else peak[3],
            "over": peak is not None and peak[2] is not None and peak[0] > peak[2] + 0.01,
        },
        "hours": rows,
        "loads": loads,
    }
