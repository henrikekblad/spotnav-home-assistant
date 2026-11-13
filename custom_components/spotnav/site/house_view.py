"""The house card's view (SpotNav 3.0, shadow): what the site's forecasts and its battery plan say, in one answer.

Pure: the shell (`api/house.py`) hands over the parts as they are held; nothing is read, fetched or written here.
It shows what the site diagnostics already carry, cut to what a card draws:

* **`hours`**: one row per local hour from `PAST_HOURS` before the running hour to `AHEAD_HOURS` after it. A past
  hour has its outcome (the house's measured load, the sun's production, the battery's SoC at the hour's end) beside
  the *first* forecast made for it (the forecast log's, the one the nightly review scores). The running hour and
  the hours ahead have the latest forecast: the house forecast, the sun as SpotNav reads it and as the shadow
  correction would make it, the SoC the battery plan in force expects at each hour's end and its strongest action
  in the hour.
* **`house`**, **`sun`**, **`battery`**: the last week's review of each (mean absolute error and how often the
  forecast was below the outcome), and the battery plan's money side by side: the plan (the site's mode), the
  battery's own way (its optimiser, or whatever it did) and plain self-consumption, over the reviewed days. `sun` is
  `None` on a site without a solar forecast, `battery` on a site without a battery.
* **`days`**: the last reviewed days, one row each, for the card's details.
* **`sources`**: where the forecasts come from (weather entity, outdoor sensor, holiday calendar, solar forecast).

Money is in the area's minor unit (öre), as the battery plan reckons it; `money` names the units.
Everything here is shadow: nothing in it controls a charger or a battery.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Final

from ..battery.plan_model import (
    ACTION_EXPORT_DISCHARGE,
    ACTION_GRID_CHARGE,
    ACTION_HOLD,
    DEFAULT_MODE,
    MODES,
)
from ..battery.plan_shadow import KIND_BATTERY, DayReview, PlannedSlot, soc_by_hour_end
from ..planning.demand_charge import hour_start
from .forecast_log import KIND_HOUSE, KIND_SUN, KIND_SUN_CORRECTED, ForecastLog, LoggedHour, Score
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


@dataclass(frozen=True, slots=True)
class HouseInputs:
    site_id: str
    now: datetime
    tz: str
    log: ForecastLog
    forecast: LoadForecast | None
    #: The house's measured load per completed hour (kW, by UTC hour start), from the site's hours.
    house_actual: Mapping[datetime, float]
    sources: HouseSources
    sun: SunInputs | None = None
    battery: BatteryInputs | None = None
    #: The price area's currency, major and minor unit; `None` without an area.
    money: tuple[str | None, str | None, str | None] | None = None


def _round(value: float | None, digits: int = 3) -> float | None:
    return None if value is None else round(value, digits)


def _score(score: Score | None) -> dict[str, Any] | None:
    return None if score is None else score.as_dict()


def hour_starts(now: datetime, tz: str) -> list[datetime]:
    """The shown hours' starts (UTC): `PAST_HOURS` before the running hour to `AHEAD_HOURS` from it on."""
    current = hour_start(now, tz)
    return [current + timedelta(hours=offset) for offset in range(-PAST_HOURS, AHEAD_HOURS)]


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
    rows = []
    for start in hour_starts(inputs.now, tz):
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
        }
        if sun is not None:
            row["sun_kwh"] = _round(_actual(start, current, sun_logged))
            row["sun_forecast_kwh"] = _round(_forecast_or_logged(start, current, sun.raw, sun_logged))
            row["sun_corrected_kwh"] = _round(_forecast_or_logged(start, current, sun.corrected, corrected_logged))
        if battery is not None:
            row["soc_pct"] = _round(_actual(start, current, battery_logged), 1)
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
        # The house's load is measured as the grid less the chargers: on a site with a battery, the battery's power
        # is in it (`house_counts_battery`), so the house looks smaller while the battery covers it.
        "counts_battery": battery is not None,
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
            day, {"date": day, **{key: None for key in kinds}, "plan_saving": None, "own_saving": None}
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


def house_view(inputs: HouseInputs) -> dict[str, Any]:
    """The whole answer (`HOUSE_KEYS`)."""
    sources = inputs.sources
    money = inputs.money
    return {
        "api_version": HOUSE_API_VERSION,
        "site_id": inputs.site_id,
        "state": "shadow",
        "time_zone": inputs.tz,
        "generated_at": inputs.now.isoformat(),
        "money": None if money is None else {"currency": money[0], "major_unit": money[1], "minor_unit": money[2]},
        "hours": hours_view(inputs),
        "house": _house_block(inputs),
        "sun": None if inputs.sun is None else _sun_block(inputs.sun, inputs.log),
        "battery": None if inputs.battery is None else _battery_block(inputs.battery, inputs.log),
        "days": days_view(inputs),
        "sources": {
            "weather_entity": sources.weather_entity,
            "weather": sources.weather,
            "outdoor": {"source": sources.outdoor_source, "sensor": sources.outdoor_sensor},
            "holiday_calendar": sources.holiday_calendar,
            "solar_forecasts": [] if inputs.sun is None else list(inputs.sun.forecast_names),
        },
    }
