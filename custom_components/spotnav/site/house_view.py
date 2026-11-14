"""The house card's view (SpotNav 3.0, shadow): what the site's forecasts and its battery plan say, in one answer.

Pure: the shell (`api/house.py`) hands over the parts as they are held; nothing is read, fetched or written here.
It shows what the site diagnostics already carry, cut to what a card draws:

* **`hours`**: one row per local hour from `PAST_HOURS` before the running hour to `AHEAD_HOURS` after it. A past
  hour has its outcome (the house's measured load, the sun's production, the battery's SoC at the hour's end: the
  battery plan's log, else the recorder's statistics of the SoC sensor, `HouseInputs.soc_recorded`) beside
  the *first* forecast made for it (the forecast log's, the one the nightly review scores). The running hour and
  the hours ahead have the latest forecast: the house forecast, the sun as SpotNav reads it and as the shadow
  correction would make it, the SoC the battery plan in force expects at each hour's end and its strongest action
  in the hour. The house figure is the forecast's (`house.basis`): the whole load once it has its hours, else the
  import less the chargers.
* **`house`**, **`sun`**, **`battery`**: the last week's review of each (mean absolute error and how often the
  forecast was below the outcome), and the battery plan's money side by side: the plan (the site's mode), the
  battery's own way (its optimiser, or whatever it did) and plain self-consumption, over the reviewed days. `sun` is
  `None` on a site without a solar forecast, `battery` on a site without a battery.
* **`today`**: today so far, split as the battery's own app splits a day (sold, arbitrage, bought) with the mode in
  force, from the stored energy's running value (`battery/stored_value.py`); **`flow`**: today's energy from sources (sun, battery, grid) to uses
  (battery, load, grid); **`energy_flow`**: today's completed hours as a Sankey diagram's nodes and links, from the
  Energy dashboard's meters and the chargers' registers, the flows allocated hour by hour (`site/energy_flow.py`, its
  docstring has the rule; `None` without the dashboard's grid meter, also on a site without a battery); **`calendar`**: the running month's reviewed days, the plan beside the battery's own way.
  All three are `None` without a battery. Each hour also has its mean buy and sell price (`buy`, `sell`, minor per
  kWh) where the battery plan priced it.
* **`day`**: the day shown (`date`), whether it is today, the first day that may be asked for (`first`: the forecast
  log keeps `KEEP_HOURS_DAYS` days of hours) and whether anything was measured on it (`has_data`). A past day
  (`HouseInputs.day`) shows its own local hours, all of them past (its outcomes beside its first forecasts), its money
  (`today`, from the stored energy's day) and its energy flow; the bars of `flow` are today's only.
* **`days`**: the last reviewed days, one row each, for the card's details.
* **`sources`**: where the forecasts come from (weather entity, outdoor sensor, holiday calendar, solar forecast).

Money is in the area's minor unit (öre), as the battery plan reckons it; `money` names the units.
Everything here is shadow: nothing in it controls a charger or a battery.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Final

from ..battery.plan_model import (
    ACTION_EXPORT_DISCHARGE,
    ACTION_GRID_CHARGE,
    ACTION_HOLD,
    DEFAULT_MODE,
    MODES,
)
from .. import time_zones
from ..battery.plan_shadow import KIND_BATTERY, Bucket, DayReview, PlannedSlot, PriceSlot, soc_by_hour_end
from ..battery.stored_value import StoredValue
from ..planning.demand_charge import hour_start
from .forecast_log import KEEP_HOURS_DAYS, KIND_HOUSE, KIND_SUN, KIND_SUN_CORRECTED, ForecastLog, LoggedHour, Score
from .load_forecast import LoadForecast

HOUSE_API_VERSION: Final = 1
#: The hours before the running one that are shown, and the hours from the running one on.
PAST_HOURS: Final = 12
AHEAD_HOURS: Final = 24
#: The reviewed days shown in the details and summed in the battery's review (a week, as the forecasts' review).
REVIEW_DAYS_SHOWN: Final = 7

#: Every key of the answer, in order.
HOUSE_KEYS: Final = (
    "api_version",
    "site_id",
    "state",
    "time_zone",
    "generated_at",
    "money",
    "hours",
    "house",
    "sun",
    "battery",
    "today",
    "flow",
    "energy_flow",
    "day",
    "calendar",
    "days",
    "sources",
)
HOUR_KEYS: Final = (
    "start",
    "house_kw",
    "house_forecast_kw",
    "sun_kwh",
    "sun_forecast_kwh",
    "sun_corrected_kwh",
    "soc_pct",
    "soc_plan_pct",
    "battery_action",
    "buy",
    "sell",
)
#: The hour's battery action shown: the strongest planned in the hour (grid charging, then selling, then holding).
HOUR_ACTIONS: Final = (ACTION_GRID_CHARGE, ACTION_EXPORT_DISCHARGE, ACTION_HOLD)


@dataclass(frozen=True, slots=True)
class HouseSources:
    """Where the house forecast reads from (`SiteHouseForecast`): the weather entity and its status, the outdoor
    temperature's source and sensor, the holiday calendar (`None`: weekday/weekend only)."""

    weather_entity: str | None = None
    weather: str | None = None
    outdoor_source: str | None = None
    outdoor_sensor: str | None = None
    holiday_calendar: str | None = None


@dataclass(frozen=True, slots=True)
class SunInputs:
    """The solar correction in shadow (`SiteSolarCorrection`): its status, the next hours raw and corrected (kWh, by
    UTC hour start) and the solar forecasts' names."""

    status: str
    raw: Mapping[datetime, float] = field(default_factory=dict)
    corrected: Mapping[datetime, float] = field(default_factory=dict)
    forecast_names: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class BatteryInputs:
    """The battery's facts and its plan in shadow (`BatteryShadow`, `BatteryPlanShadow`)."""

    brand: str | None
    label: str | None
    soc_pct: float | None
    usable_kwh: float | None
    management: str
    status: str
    reason: str | None = None
    mode: str = DEFAULT_MODE
    made_at: datetime | None = None
    horizon_end: datetime | None = None
    plan: Sequence[PlannedSlot] = ()
    #: Each mode's view as last planned (`savings` in minor units), `None` for one the CPU budget skipped.
    mode_views: Mapping[str, Mapping[str, Any] | None] = field(default_factory=dict)
    reviews: Sequence[DayReview] = ()
    estimates: tuple[str, ...] = ()
    #: The quarter hours measured (`PlanActuals.buckets`): energies, the battery's signed (+ charging), and prices.
    quarters: Sequence[Bucket] = ()
    #: The price slots the plan read (today and the next day), buy with every fee and sell at spot (minor per kWh).
    prices: Sequence[PriceSlot] = ()
    #: The sun's production per hour (kWh, UTC hour starts) from the Energy dashboard's statistics; empty without.
    production: Mapping[datetime, float] = field(default_factory=dict)
    #: The stored energy's running value and each day's money split (`battery/stored_value.py`).
    stored: StoredValue | None = None


@dataclass(frozen=True, slots=True)
class HouseInputs:
    site_id: str
    now: datetime
    tz: str
    log: ForecastLog
    forecast: LoadForecast | None
    #: The house's measured load per completed hour (kW, by UTC hour start), on the forecast's basis.
    house_actual: Mapping[datetime, float]
    sources: HouseSources
    sun: SunInputs | None = None
    battery: BatteryInputs | None = None
    #: The price area's currency, major and minor unit; `None` without an area.
    money: tuple[str | None, str | None, str | None] | None = None
    #: What the house figure is: the import less the chargers (`net`) or the house's whole load (`gross`).
    basis: str = "net"
    #: The day's energy flow as `site/energy_flow.energy_flow` built it (the shell reads the statistics); `None` without.
    energy_flow: Mapping[str, Any] | None = None
    #: A past local day to show instead of the hours around now (`None`: today).
    day: date | None = None
    #: The battery's SoC (%) at each past hour's end from the recorder (the shell reads it), by UTC hour start: shown
    #: where the battery plan's log has no level for the hour (it was not measuring yet); a logged level wins.
    soc_recorded: Mapping[datetime, float] = field(default_factory=dict)


def _round(value: float | None, digits: int = 3) -> float | None:
    return None if value is None else round(value, digits)


def _score(score: Score | None) -> dict[str, Any] | None:
    return None if score is None else score.as_dict()


def hour_starts(now: datetime, tz: str) -> list[datetime]:
    """The shown hours' starts (UTC): `PAST_HOURS` before the running hour to `AHEAD_HOURS` from it on."""
    current = hour_start(now, tz)
    return [current + timedelta(hours=offset) for offset in range(-PAST_HOURS, AHEAD_HOURS)]


def day_hour_starts(day: date, tz: str) -> list[datetime]:
    """A local day's hour starts (UTC): 23, 24 or 25 of them."""
    zone = time_zones.require_zone(tz)
    start = datetime(day.year, day.month, day.day, tzinfo=zone).astimezone(timezone.utc)
    following = day + timedelta(days=1)
    end = datetime(following.year, following.month, following.day, tzinfo=zone).astimezone(timezone.utc)
    return [start + timedelta(hours=offset) for offset in range(int((end - start).total_seconds() // 3600))]


def first_day(now: datetime, tz: str) -> date:
    """The first day that may be asked for: the forecast log's hours reach this far back."""
    return now.astimezone(time_zones.require_zone(tz)).date() - timedelta(days=KEEP_HOURS_DAYS - 1)


def hour_actions(plan: Iterable[PlannedSlot], tz: str) -> dict[datetime, str]:
    """The strongest action (`HOUR_ACTIONS`) planned in each local hour, by the hour's start; hours without one are
    left out."""
    out: dict[datetime, str] = {}
    for slot in plan:
        if slot.action not in HOUR_ACTIONS:
            continue
        start = hour_start(slot.start, tz)
        held = out.get(start)
        if held is None or HOUR_ACTIONS.index(slot.action) < HOUR_ACTIONS.index(held):
            out[start] = slot.action
    return out


def hour_prices(
    prices: Iterable[PriceSlot], quarters: Iterable[Bucket], tz: str
) -> dict[datetime, tuple[float, float]]:
    """The mean buy and sell price of each local hour (minor per kWh): the slots the plan read, and for an hour before
    them the prices its quarter hours were measured at."""
    slots: dict[datetime, list[tuple[float, float]]] = {}
    for slot in prices:
        slots.setdefault(hour_start(slot.start, tz), []).append((slot.buy, slot.sell))
    measured: dict[datetime, list[tuple[float, float]]] = {}
    for bucket in quarters:
        if bucket.buy is not None and bucket.sell is not None:
            measured.setdefault(hour_start(bucket.start, tz), []).append((bucket.buy, bucket.sell))
    out: dict[datetime, tuple[float, float]] = {}
    for source in (measured, slots):
        for start, values in source.items():
            out[start] = (sum(v[0] for v in values) / len(values), sum(v[1] for v in values) / len(values))
    return out


def _today(quarters: Iterable[Bucket], now: datetime, tz: str) -> tuple[date, list[Bucket]]:
    zone = time_zones.require_zone(tz)
    today = now.astimezone(zone).date()
    return today, [
        bucket
        for bucket in quarters
        if bucket.covered and bucket.start.astimezone(zone).date() == today and bucket.end <= now
    ]


def today_money(battery: BatteryInputs, now: datetime, tz: str, day: date | None = None) -> dict[str, Any] | None:
    """Today so far (minor units), the way the battery's own app splits a day, from the stored energy's running value
    (`battery/stored_value.py`): `sold` is the sun's surplus exported, `bought` the import, `arbitrage` what each kWh
    the battery gave back earned over its average cost (its exports included). `average_cost` is a stored kWh's cost
    now and `estimated` says it still rests on the starting estimate. `None` before a counted quarter hour today."""
    stored = battery.stored
    if stored is None:
        return None
    day = day or now.astimezone(time_zones.require_zone(tz)).date()
    money = stored.days.get(day)
    if money is None:
        return None
    average = stored.average
    return {
        "date": day.isoformat(),
        "quarters": money.quarters,
        "sold": _money(money.sold),
        "arbitrage": _money(money.arbitrage),
        "bought": _money(money.bought),
        "net": _money(money.sold + money.arbitrage - money.bought),
        "mode": battery.mode,
        "battery_export_kwh": round(money.battery_export_kwh, 2),
        "stored_kwh": None if stored.kwh is None else round(stored.kwh, 2),
        "average_cost": None if average is None else round(average, 2),
        "estimated": stored.estimated,
    }


def day_flow(battery: BatteryInputs, now: datetime, tz: str, has_sun: bool) -> dict[str, Any] | None:
    """Today's energy so far (kWh), sources to uses: the sun, the battery and the grid into the battery, the load and
    the grid. The sun counts only the hours its production is known for (`sun_hours`); the load is the balance (the
    cars in it). `None` before a measured quarter hour of today."""
    day, quarters = _today(battery.quarters, now, tz)
    if not quarters:
        return None
    grid_in = sum(bucket.scaled(bucket.import_kwh) for bucket in quarters)
    grid_out = sum(bucket.scaled(bucket.export_kwh) for bucket in quarters)
    battery_in = sum(max(0.0, bucket.scaled(bucket.battery_kwh)) for bucket in quarters)
    battery_out = sum(max(0.0, -bucket.scaled(bucket.battery_kwh)) for bucket in quarters)
    hours = {hour_start(bucket.start, tz) for bucket in quarters}
    known = [start for start in hours if start in battery.production]
    solar = sum(battery.production[start] for start in known)
    load = max(0.0, solar + grid_in + battery_out - grid_out - battery_in)
    r = lambda value: round(value, 2)  # noqa: E731
    return {
        "date": day.isoformat(),
        "sources": {"solar": r(solar) if has_sun else None, "battery": r(battery_out), "grid": r(grid_in)},
        "uses": {"battery": r(battery_in), "load": r(load), "grid": r(grid_out)},
        "sun_hours": len(known),
        "hours": len(hours),
    }


def month_calendar(battery: BatteryInputs, now: datetime, tz: str) -> dict[str, Any]:
    """The running month's reviewed days: the plan's saving and the battery's own against self-consumption."""
    today = now.astimezone(time_zones.require_zone(tz)).date()
    month = today.strftime("%Y-%m")
    tallies = {} if battery.stored is None else battery.stored.days
    days = [
        {
            "date": review.day.isoformat(),
            "plan_saving": _money(review.saving),
            "own_saving": _money(review.actual_saving),
            "mode": review.mode,
            "arbitrage": _money(tallies[review.day].arbitrage) if review.day in tallies else None,
        }
        for review in battery.reviews
        if review.day.strftime("%Y-%m") == month
    ]
    return {"month": month, "days": days}


def _forecast_or_logged(
    start: datetime, current: datetime, latest: Mapping[datetime, float], logged: Mapping[datetime, LoggedHour]
) -> float | None:
    """A past hour's first forecast (the logged one, which the review scores); from the running hour on the latest
    forecast, else the logged one."""
    first = logged.get(start)
    if start < current:
        return None if first is None else first.forecast
    if start in latest:
        return latest[start]
    return None if first is None else first.forecast


def _actual(start: datetime, current: datetime, logged: Mapping[datetime, LoggedHour]) -> float | None:
    if start >= current:
        return None
    hour = logged.get(start)
    return None if hour is None else hour.actual


def hours_view(inputs: HouseInputs) -> list[dict[str, Any]]:
    """The `hours` rows (see the module)."""
    tz = inputs.tz
    current = hour_start(inputs.now, tz)
    log = inputs.log
    house_logged = log.hours(KIND_HOUSE)
    sun_logged = log.hours(KIND_SUN)
    corrected_logged = log.hours(KIND_SUN_CORRECTED)
    battery_logged = log.hours(KIND_BATTERY)
    house_latest = {} if inputs.forecast is None else inputs.forecast.by_hour()
    sun = inputs.sun
    battery = inputs.battery
    plan_soc = {} if battery is None else soc_by_hour_end(battery.plan, tz)
    actions = {} if battery is None else hour_actions(battery.plan, tz)
    prices = {} if battery is None else hour_prices(battery.prices, battery.quarters, tz)
    rows = []
    for start in hour_starts(inputs.now, tz) if inputs.day is None else day_hour_starts(inputs.day, tz):
        past = start < current
        house_actual = inputs.house_actual.get(start) if past else None
        row: dict[str, Any] = {
            "start": start.isoformat(),
            "house_kw": _round(house_actual),
            "house_forecast_kw": _round(_forecast_or_logged(start, current, house_latest, house_logged)),
            "sun_kwh": None,
            "sun_forecast_kwh": None,
            "sun_corrected_kwh": None,
            "soc_pct": None,
            "soc_plan_pct": None,
            "battery_action": None,
            "buy": None,
            "sell": None,
        }
        if start in prices:
            row["buy"], row["sell"] = (round(value, 2) for value in prices[start])
        if sun is not None:
            row["sun_kwh"] = _round(_actual(start, current, sun_logged))
            row["sun_forecast_kwh"] = _round(_forecast_or_logged(start, current, sun.raw, sun_logged))
            row["sun_corrected_kwh"] = _round(_forecast_or_logged(start, current, sun.corrected, corrected_logged))
        if battery is not None:
            logged_soc = _actual(start, current, battery_logged)
            if logged_soc is None and past:
                logged_soc = inputs.soc_recorded.get(start)
            row["soc_pct"] = _round(logged_soc, 1)
            if past:
                first = battery_logged.get(start)
                row["soc_plan_pct"] = None if first is None else round(first.forecast, 1)
            else:
                row["soc_plan_pct"] = _round(plan_soc.get(start), 1)
                row["battery_action"] = actions.get(start)
        rows.append(row)
    return rows


def _house_block(inputs: HouseInputs) -> dict[str, Any]:
    forecast = inputs.forecast
    battery = inputs.battery
    return {
        "made_at": None if forecast is None else forecast.made_at.isoformat(),
        "samples": 0 if forecast is None else forecast.samples,
        "temperature": None
        if forecast is None
        else {"active": forecast.temperature.active, "reason": forecast.temperature.reason},
        "basis": inputs.basis,
        # The house figure is the grid less the chargers while the whole load is still being collected: on a site
        # with a battery, the battery's power is then in it, so the house looks smaller while the battery covers it.
        "counts_battery": battery is not None and inputs.basis != "gross",
        "review": _score(inputs.log.last_week(KIND_HOUSE)),
    }


def _sun_block(sun: SunInputs, log: ForecastLog) -> dict[str, Any]:
    return {
        "status": sun.status,
        "review": _score(log.last_week(KIND_SUN)),
        "review_corrected": _score(log.last_week(KIND_SUN_CORRECTED)),
    }


def _money(value: float) -> float:
    return round(value, 2)


def _battery_block(battery: BatteryInputs, log: ForecastLog) -> dict[str, Any]:
    reviews = list(battery.reviews)[-REVIEW_DAYS_SHOWN:]
    modes: dict[str, Any] = {}
    for mode in MODES:
        view = battery.mode_views.get(mode)
        ran = [review for review in reviews if mode in review.modes]
        planned = None if view is None else view.get("savings")
        modes[mode] = {
            "planned_saving": None if not isinstance(planned, (int, float)) else _money(float(planned)),
            "reviewed_saving": _money(sum(review.mode_savings[mode] for review in ran)) if ran else None,
            "reviewed_days": len(ran),
        }
    review = None
    if reviews:
        review = {
            "days": len(reviews),
            "first": reviews[0].day.isoformat(),
            "last": reviews[-1].day.isoformat(),
            # Against plain self-consumption on the same measured load and sun: the plan (the site's mode) and what
            # the battery really did (its own optimiser, or its own way).
            "plan_saving": _money(sum(item.saving for item in reviews)),
            "own_saving": _money(sum(item.actual_saving for item in reviews)),
            "self_consumption_net": _money(sum(item.net(item.self_consumption) for item in reviews)),
        }
    return {
        "brand": battery.brand,
        "label": battery.label,
        "soc_pct": _round(battery.soc_pct, 1),
        "usable_kwh": _round(battery.usable_kwh, 2),
        "management": battery.management,
        "status": battery.status,
        "reason": battery.reason,
        "mode": battery.mode,
        "made_at": None if battery.made_at is None else battery.made_at.isoformat(),
        "horizon_end": None if battery.horizon_end is None else battery.horizon_end.isoformat(),
        "modes": modes,
        "review": review,
        "soc_review": _score(log.last_week(KIND_BATTERY)),
        "estimates": sorted(set(battery.estimates)),
    }


def days_view(inputs: HouseInputs) -> list[dict[str, Any]]:
    """The last `REVIEW_DAYS_SHOWN` days any review covers, newest first: each forecast's mean absolute error that
    day and the battery's money (`None` where that part was not reviewed)."""
    log = inputs.log
    kinds = {
        "house_mae": KIND_HOUSE,
        "sun_mae": KIND_SUN,
        "sun_corrected_mae": KIND_SUN_CORRECTED,
        "soc_mae": KIND_BATTERY,
    }
    by_day: dict[str, dict[str, Any]] = {}

    def day_row(day: str) -> dict[str, Any]:
        return by_day.setdefault(
            day, {"date": day, **{key: None for key in kinds}, "plan_saving": None, "own_saving": None, "arbitrage": None}
        )

    for key, kind in kinds.items():
        body = log.kinds.get(kind)
        for review in [] if body is None else body.reviews:
            day_row(review.day.isoformat())[key] = round(review.all.mae, 3)
    battery = inputs.battery
    for item in [] if battery is None else battery.reviews:
        row = day_row(item.day.isoformat())
        row["plan_saving"] = _money(item.saving)
        row["own_saving"] = _money(item.actual_saving)
    if battery is not None and battery.stored is not None:
        today = inputs.now.astimezone(time_zones.require_zone(inputs.tz)).date()
        for day, money in battery.stored.days.items():
            if day < today:
                day_row(day.isoformat())["arbitrage"] = _money(money.arbitrage)
    if inputs.sun is None:
        for row in by_day.values():
            row["sun_mae"] = row["sun_corrected_mae"] = None
    if battery is None:
        for row in by_day.values():
            row["soc_mae"] = None
    shown = [
        row for _day, row in sorted(by_day.items(), reverse=True) if any(row[k] is not None for k in row if k != "date")
    ]
    return shown[:REVIEW_DAYS_SHOWN]


def day_block(inputs: HouseInputs, hours: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """The `day` block (see the module)."""
    today = inputs.now.astimezone(time_zones.require_zone(inputs.tz)).date()
    shown = inputs.day or today
    measured = ("house_kw", "sun_kwh", "soc_pct")
    return {
        "date": shown.isoformat(),
        "today": shown == today,
        "first": first_day(inputs.now, inputs.tz).isoformat(),
        "has_data": inputs.energy_flow is not None or any(row[key] is not None for row in hours for key in measured),
    }


def house_view(inputs: HouseInputs) -> dict[str, Any]:
    """The whole answer (`HOUSE_KEYS`)."""
    sources = inputs.sources
    money = inputs.money
    hours = hours_view(inputs)
    return {
        "api_version": HOUSE_API_VERSION,
        "site_id": inputs.site_id,
        "state": "shadow",
        "time_zone": inputs.tz,
        "generated_at": inputs.now.isoformat(),
        "money": None if money is None else {"currency": money[0], "major_unit": money[1], "minor_unit": money[2]},
        "hours": hours,
        "house": _house_block(inputs),
        "sun": None if inputs.sun is None else _sun_block(inputs.sun, inputs.log),
        "battery": None if inputs.battery is None else _battery_block(inputs.battery, inputs.log),
        "today": None if inputs.battery is None else today_money(inputs.battery, inputs.now, inputs.tz, inputs.day),
        "flow": None
        if inputs.battery is None or inputs.day is not None
        else day_flow(inputs.battery, inputs.now, inputs.tz, inputs.sun is not None),
        "energy_flow": None if inputs.energy_flow is None else dict(inputs.energy_flow),
        "day": day_block(inputs, hours),
        "calendar": None if inputs.battery is None else month_calendar(inputs.battery, inputs.now, inputs.tz),
        "days": days_view(inputs),
        "sources": {
            "weather_entity": sources.weather_entity,
            "weather": sources.weather,
            "outdoor": {"source": sources.outdoor_source, "sensor": sources.outdoor_sensor},
            "holiday_calendar": sources.holiday_calendar,
            "solar_forecasts": [] if inputs.sun is None else list(inputs.sun.forecast_names),
        },
    }
