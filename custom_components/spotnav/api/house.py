"""`spotnav/get_house` (API v1): the house card's view of one site, read-only and in shadow (SpotNav 3.0 preview).

`{api_version: 1, charger_id | site_id, date?}`: `date` (`YYYY-MM-DD`, the site's local day) asks for a past day from
`day.first` on instead of the hours around now (`site/house_view.py`, its `day` block); absent or today: as before. Any user who can see the card may read, as the dashboard. The answer is
`site/house_view.py:house_view` (its docstring has the shape), built from what the site already holds: the house
load forecast and the forecast log with its nightly reviews (`site/house_forecast.py`), the sun's forecast raw and
corrected (`site/solar_correction_shadow.py`), and the battery's facts and plan in shadow (`battery/runtime.py`,
`battery/plan_runtime.py`). Nothing is computed anew or written; the one thing fetched is the day's energy flow
(`energy_flow`, `site/energy_flow.py`): the Energy dashboard's statistics and the chargers' energy registers, by the
quarter hour from the recorder's 5-minute statistics within their window (`short_term_days`), else by the hour, read
again once per new 5-minute row per site with the finished hours kept in `hass.data` (only the open hour is read
again; `async_day_flow`); the cars' hours of the chart come from the same reads. And, at most every
`FLOW_REFRESH_S` and between hours kept, the battery's SoC at the end of each shown past hour its plan's log has no level for (the recorder's
statistics of its SoC sensor, `async_recorded_soc`). Where the heat pump's meter is found or set
(`site/heat_meter.py`), the energy flow reads its statistics too (a `heat` use split out of the house), and the
hours and the `heat` block take its hours (the flow's reads over the house forecast's own) and the heat forecast the house forecast keeps (`_heat`).

Refusals (WebSocket errors): `spotnav_unsupported_api_version`, `spotnav_invalid_date` (not a date, in the future or
before `day.first`), `spotnav_charger_required` (neither id), the charger
lookup's codes, `spotnav_unknown_site` and `spotnav_no_site` (the charger is in no site, or the site is not loaded).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Final

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import callback, HomeAssistant

from ..battery.plan_model import DEFAULT_MODE
from .. import time_zones
from ..const import CONF_CHARGER_ENTRY_IDS, CONF_SOLAR_FORECAST_ENTRIES, DOMAIN
from ..planning.demand_charge import hour_start
from ..site.energy_flow import (
    SETTLE,
    EnergyHour,
    FlowSeries,
    energy_flow,
    energy_hours,
    hour_figures,
    quarter_hours,
)
from ..site.energy_statistics import (
    FlowIds,
    async_energy_prefs,
    async_soc_statistics,
    async_statistics,
    energy_flow_ids,
    soc_at_hour_ends,
    summed,
)
from ..pricing.price_source import site_price_source
from ..runtime import controller_for, site_controller_for
from ..site.heat_meter import SOURCE_SETTING, STATUS_NONE, async_heat_hours, needs_setting
from ..site.house_view import (
    HOUSE_API_VERSION,
    BatteryInputs,
    HeatInputs,
    HouseInputs,
    HouseSources,
    SunInputs,
    day_hour_starts,
    first_day,
    hour_starts,
    house_view,
)
from ..site.forecast_log import KIND_SUN
from ..battery.plan_shadow import KIND_BATTERY, MIN_COVERED_SHARE
from ..site.house_forecast import BASIS_GROSS
from ..site.solar_correction_shadow import STATUS_NO_FORECAST
from ..battery.shadow import parse_aware_datetime
from .common import send_unsupported_version
from .power_month import ERROR_NO_SITE, PowerMonthRefusal, _site_of_request

#: The recorded SoC is read from the recorder again after this many seconds, and at every new hour; the energy flow
#: and the cars' hours once the recorder has a new 5-minute row (`five_slot`).
FLOW_REFRESH_S: Final = 600
#: The recorder compiles a 5-minute row this long after its end at most (it starts some 10 s after).
COMPILE_LAG: Final = timedelta(seconds=30)
_FLOW_CACHE: Final = f"{DOMAIN}_house_flow"
#: The finished hours of the days read by the quarter hour, per site and day (`_HeldDay`), for this many days a site.
_PERIOD_CACHE: Final = f"{DOMAIN}_house_flow_periods"
PERIOD_CACHE_DAYS: Final = 4

_LOGGER = logging.getLogger(__name__)


ERROR_INVALID_DATE: Final = "spotnav_invalid_date"


@dataclass(frozen=True, slots=True)
class _FlowCached:
    hour: datetime
    read_at: datetime
    flow: Any


def _sun(hass: HomeAssistant, controller: Any) -> SunInputs | None:
    correction = controller.solar_correction
    entries = list(controller.config.get(CONF_SOLAR_FORECAST_ENTRIES) or [])
    if not entries or correction.status == STATUS_NO_FORECAST:
        return None
    raw: dict[Any, float] = {}
    corrected: dict[Any, float] = {}
    for hour in correction.next_hours:
        start = parse_aware_datetime(hour.get("start"))
        if start is not None:
            raw[start] = float(hour["raw_kwh"])
            corrected[start] = float(hour["corrected_kwh"])
    names = []
    for entry_id in entries:
        entry = hass.config_entries.async_get_entry(entry_id)
        if entry is not None:
            names.append(entry.title)
    return SunInputs(status=correction.status, raw=raw, corrected=corrected, forecast_names=tuple(names))


def _production(controller: Any) -> dict[Any, float]:
    """The sun's production per hour (kWh, UTC hour starts): the outcomes the sun forecast's log holds (filled from
    the Energy dashboard's statistics); empty without a solar forecast."""
    return {
        start: hour.actual
        for start, hour in controller.house_forecast.log.hours(KIND_SUN).items()
        if hour.actual is not None
    }


def _battery(controller: Any) -> BatteryInputs | None:
    shadow = controller.battery_shadow
    facts = shadow.facts
    if facts.battery_entity_id is None:
        return None
    plan = shadow.plan
    inputs = plan.inputs or {}
    usable = (inputs.get("battery") or {}).get("usable_kwh")
    horizon = parse_aware_datetime(inputs.get("horizon_end"))
    return BatteryInputs(
        brand=facts.brand,
        label=facts.label,
        soc_pct=facts.soc_pct,
        usable_kwh=usable if isinstance(usable, (int, float)) else facts.capacity_kwh,
        management=facts.management.state,
        status=plan.status,
        reason=plan.reason,
        mode=plan.mode or DEFAULT_MODE,
        made_at=plan.made_at,
        horizon_end=horizon,
        plan=plan.plan,
        mode_views=dict(plan.mode_views),
        reviews=tuple(plan.reviews),
        estimates=tuple(inputs.get("estimates") or ()),
        quarters=tuple(plan.actuals.buckets.values()),
        prices=tuple(plan._prices),  # noqa: SLF001 - the slots the plan read, held between its runs
        production=_production(controller),
        stored=plan.stored,
    )


def _heat(hass: HomeAssistant, controller: Any, read: Mapping[datetime, float] | None = None) -> HeatInputs | None:
    """The heat pump's meter as the house forecast last found it, its hours (those the energy flow read over the
    forecast's own) and the heat forecast; `None` where none is found, set or ambiguous."""
    forecast = controller.house_forecast
    heat = forecast.heat
    meter = heat.meter
    if meter.status == STATUS_NONE:
        return None
    state = None if meter.entity_id is None else hass.states.get(meter.entity_id)
    split = forecast.heat_split
    return HeatInputs(
        status=meter.status,
        entity_id=meter.entity_id,
        name=None if state is None else state.name,
        kind=meter.kind,
        source=meter.source,
        candidates=meter.candidates,
        setting_offered=needs_setting(heat.detected) or meter.source == SOURCE_SETTING,
        actual={**heat.by_hour, **(read or {})},
        forecast={} if split is None else split.heat.by_hour(),
    )


def _bucket_car(controller: Any) -> dict[datetime, float]:
    """The cars' energy per local hour (kWh, UTC hour starts) as the battery plan measured it: only hours all four of
    whose quarter hours were measured."""
    tz = controller.demand.tracker.tz
    quarters: dict[datetime, list[float]] = {}
    for bucket in controller.battery_shadow.plan.actuals.buckets.values():
        if bucket.covered:
            quarters.setdefault(hour_start(bucket.start, tz), []).append(bucket.scaled(bucket.car_kwh))
    return {start: sum(values) for start, values in quarters.items() if len(values) == 4}


def _car_register_ids(hass: HomeAssistant, controller: Any) -> tuple[str, ...]:
    """The chargers' energy registers, only when every charger has one (else the battery plan's measure is used)."""
    registers: list[str | None] = []
    for charger_entry_id in controller.config.get(CONF_CHARGER_ENTRY_IDS) or []:
        charger = controller_for(hass, charger_entry_id)
        registers.append(None if charger is None else charger.energy_register_entity_id)
    return tuple(dict.fromkeys(r for r in registers if r)) if registers and all(registers) else ()


def _car_hours(controller: Any, rows: Any, car_ids: tuple[str, ...]) -> dict[datetime, float]:
    """The cars' energy per hour (kWh, UTC hour starts): the battery plan's measure, the chargers' registers over it."""
    car = _bucket_car(controller) if controller.battery_shadow.facts.battery_entity_id is not None else {}
    if car_ids:
        car = {**car, **summed(rows, car_ids)}
    return car


async def async_car_hours(
    hass: HomeAssistant,
    controller: Any,
    now: datetime,
    day: date | None = None,
    periods: Sequence[EnergyHour] = (),
) -> dict[datetime, float]:
    """The cars' energy in each completed hour the card's chart shows (the energy flow's own figures, kWh = mean kW):
    the hours the flow's `periods` cover whole with a car figure from them (the same reads), the rest by the hour."""
    tz = controller.demand.tracker.tz
    current = hour_start(now, tz)
    starts = [start for start in (hour_starts(now, tz) if day is None else day_hour_starts(day, tz)) if start < current]
    if not starts:
        return {}
    wanted = set(starts)
    known = {
        start: kwh
        for start, kwh in hour_figures(periods, "car", lambda m: hour_start(m, tz)).items()
        if start in wanted
    }
    left = [start for start in starts if start not in known]
    if not left:
        return known
    car_ids = _car_register_ids(hass, controller)
    rows = await async_statistics(hass, car_ids, left[0], left[-1] + timedelta(hours=1), "hour") if car_ids else {}
    wanted = set(left)
    return {**{start: kwh for start, kwh in _car_hours(controller, rows, car_ids).items() if start in wanted}, **known}


def short_term_days(hass: HomeAssistant) -> int | None:
    """How many days the recorder keeps its short-term (5-minute) statistics: its `purge_keep_days` (they are purged
    with the states); `None` without a recorder."""
    try:
        from homeassistant.components.recorder import get_instance  # noqa: PLC0415 - only when loaded

        days = get_instance(hass).keep_days
    except Exception:  # noqa: BLE001 - no recorder: no short-term statistics
        return None
    return days if isinstance(days, int) and days > 0 else None


def reads_quarters(midnight: datetime, now: datetime, keep_days: int | None) -> bool:
    """Whether a day starting at `midnight` is read by the quarter hour: all of it within the short-term window."""
    return keep_days is not None and midnight >= now - timedelta(days=keep_days)


def _bucket_car_quarters(controller: Any, now: datetime) -> dict[datetime, float]:
    """The cars' energy per quarter hour (kWh over the whole quarter, UTC starts) as the battery plan measured it: the
    covered quarters, and the running one when measured most of its time so far."""
    out: dict[datetime, float] = {}
    for bucket in controller.battery_shadow.plan.actuals.buckets.values():
        if bucket.end <= now:
            if bucket.covered:
                out[bucket.start] = bucket.scaled(bucket.car_kwh)
        elif bucket.covered_s >= MIN_COVERED_SHARE * max(1.0, (now - bucket.start).total_seconds()):
            out[bucket.start] = bucket.scaled(bucket.car_kwh)
    return out


@dataclass(slots=True)
class _HeldDay:
    """A day's finished hours as read by the quarter hour, kept per site and day (`_PERIOD_CACHE`) so only the open
    hour is read again; `signature` is the meters read (another meter: read anew)."""

    signature: tuple[Any, ...]
    hours: dict[datetime, tuple[EnergyHour, ...]]


@dataclass(frozen=True, slots=True)
class DayFlow:
    """A day's energy flow and the periods it was allocated from."""

    flow: dict[str, Any] | None
    periods: tuple[EnergyHour, ...]


def _held_day(hass: HomeAssistant, key: tuple[str, date], signature: tuple[Any, ...]) -> _HeldDay:
    cache: dict[tuple[str, date], _HeldDay] = hass.data.setdefault(_PERIOD_CACHE, {})
    held = cache.get(key)
    if held is None or held.signature != signature:
        held = cache[key] = _HeldDay(signature, {})
        for stale in [k for k in cache if k[0] == key[0]][:-PERIOD_CACHE_DAYS]:
            del cache[stale]
    return held


async def _quarter_periods(
    hass: HomeAssistant,
    controller: Any,
    now: datetime,
    hours: list[datetime],
    ids: FlowIds,
    car_ids: tuple[str, ...],
    meter: Any,
    held: _HeldDay | None,
) -> list[EnergyHour]:
    """The periods of `hours` read by the quarter hour (`site/energy_flow.quarter_hours`): the finished hours `held`
    has from it, the rest from the recorder's 5-minute rows (one read), and a finished hour that lacks a quarter by
    its hourly rows. Finished hours are kept in `held` once settled."""
    have = {} if held is None else dict(held.hours)
    to_read = [hour for hour in hours if hour not in have]
    read: list[EnergyHour] = []
    if to_read:
        first, end = to_read[0], min(now, to_read[-1] + timedelta(hours=1))
        rows = await async_statistics(hass, (*ids.all(), *car_ids), first, end, "5minute")
        heat = None
        if meter.found:
            try:
                heat = await async_heat_hours(hass, meter, first, end, "5minute")
            except Exception:  # noqa: BLE001 - without the heat figures, heat stays in the house
                _LOGGER.debug("SpotNav house view: the heat meter's 5-minute rows could not be read", exc_info=True)
        has_battery = bool(ids.battery_in)
        five = FlowSeries(
            grid_in=summed(rows, ids.grid_in),
            grid_out=summed(rows, ids.grid_out) if ids.grid_out else None,
            solar=summed(rows, ids.solar) if ids.solar else None,
            battery_in=summed(rows, ids.battery_in) if has_battery else None,
            battery_out=summed(rows, ids.battery_out) if has_battery else None,
            car=summed(rows, car_ids) if car_ids else None,
            heat=heat,
            car_quarters=_bucket_car_quarters(controller, now)
            if not car_ids and controller.battery_shadow.facts.battery_entity_id is not None
            else None,
        )
        read, missing = quarter_hours(to_read, five, now)
        if missing:
            read += await _hourly_periods(
                hass, controller, missing, missing[-1] + timedelta(hours=1), ids, car_ids, meter
            )
    for hour in to_read:
        mine = tuple(sorted((p for p in read if hour <= p.start < hour + timedelta(hours=1)), key=lambda p: p.start))
        ended = hour + timedelta(hours=1)
        whole = sum(p.minutes for p in mine) == 60
        if held is not None and (whole and ended + SETTLE <= now or ended + timedelta(minutes=30) <= now):
            held.hours[hour] = mine
    return sorted([p for hour in hours if hour in have for p in have[hour]] + read, key=lambda p: p.start)


async def _hourly_periods(
    hass: HomeAssistant,
    controller: Any,
    starts: list[datetime],
    end: datetime,
    ids: FlowIds,
    car_ids: tuple[str, ...],
    meter: Any,
) -> list[EnergyHour]:
    """The hours of `starts` (completed, before `end`) from the recorder's hourly rows."""
    rows = await async_statistics(hass, (*ids.all(), *car_ids), starts[0], end, "hour")
    heat = None
    if meter.found:
        try:
            heat = await async_heat_hours(hass, meter, starts[0], end)
        except Exception:  # noqa: BLE001 - without the heat figures, heat stays in the house
            _LOGGER.debug("SpotNav house view: the heat meter's hours could not be read", exc_info=True)
    has_battery = bool(ids.battery_in)
    return energy_hours(
        starts,
        summed(rows, ids.grid_in),
        summed(rows, ids.grid_out) if ids.grid_out else None,
        summed(rows, ids.solar) if ids.solar else None,
        summed(rows, ids.battery_in) if has_battery else None,
        summed(rows, ids.battery_out) if has_battery else None,
        _car_hours(controller, rows, car_ids),
        heat,
    )


async def async_day_flow(
    hass: HomeAssistant, controller: Any, now: datetime, day: date | None = None, site_entry_id: str | None = None
) -> DayFlow | None:
    """A day's energy flow (`site/energy_flow.py`, today when `day` is `None`) from the recorder; `None` without the
    Energy dashboard's grid import meter or before anything of the day can be read.

    A day within the recorder's short-term window (`short_term_days`) is read by the quarter hour up to the last
    finished 5-minute row, its finished hours kept per site and day (`site_entry_id`; none without), so a read
    every five minutes reads only the open hour; an older day by its completed hours. Today's first minutes, before
    the first 5-minute row, are the empty flow (the card says it is on its way)."""
    tz = controller.demand.tracker.tz
    zone = time_zones.require_zone(tz)
    today = now.astimezone(zone).date()
    day = day or today
    midnight = datetime(day.year, day.month, day.day, tzinfo=zone).astimezone(timezone.utc)
    following = day + timedelta(days=1)
    next_midnight = datetime(following.year, following.month, following.day, tzinfo=zone).astimezone(timezone.utc)
    quarters = reads_quarters(midnight, now, short_term_days(hass))
    end = min(now if quarters else hour_start(now, tz), next_midnight)
    if end <= midnight:
        return None
    ids = energy_flow_ids(await async_energy_prefs(hass))
    if not ids.grid_in:
        return None
    car_ids = _car_register_ids(hass, controller)
    meter = controller.house_forecast.heat.meter
    hours = []
    moment = midnight
    while moment < end:
        hours.append(moment)
        moment += timedelta(hours=1)
    if quarters:
        signature = (ids, car_ids, meter.entity_id if meter.found else None, meter.kind if meter.found else None)
        held = None if site_entry_id is None else _held_day(hass, (site_entry_id, day), signature)
        periods = await _quarter_periods(hass, controller, now, hours, ids, car_ids, meter, held)
    else:
        periods = await _hourly_periods(hass, controller, hours, end, ids, car_ids, meter)
    has_battery = bool(ids.battery_in)
    soc = None
    if has_battery and day == today:
        soc = controller.battery_shadow.facts.soc_pct
    elif has_battery and periods:
        # A past day: the SoC logged at its last hour's end.
        logged = controller.house_forecast.log.hours(KIND_BATTERY).get(hour_start(periods[-1].start, tz))
        soc = None if logged is None else logged.actual
    # Today's first hour with nothing read yet: the empty flow (the card's short line), not none.
    since = midnight if day == today and now - midnight < timedelta(hours=1) else None
    flow = energy_flow(
        periods,
        day=day,
        has_solar=bool(ids.solar),
        has_battery=has_battery,
        soc_pct=soc,
        has_heat=meter.found,
        step_min=15 if quarters else 60,
        since=since,
    )
    return DayFlow(flow, tuple(periods))


async def async_energy_flow(
    hass: HomeAssistant, controller: Any, now: datetime, day: date | None = None
) -> dict[str, Any] | None:
    """`async_day_flow`'s flow, without keeping the day's hours."""
    read = await async_day_flow(hass, controller, now, day)
    return None if read is None else read.flow


async def async_recorded_soc(
    hass: HomeAssistant, controller: Any, now: datetime, day: date | None = None
) -> dict[datetime, float]:
    """The battery's SoC at the end of each past hour the card shows (today's window, or the past day's hours) from
    the recorder's statistics of the battery facts' SoC sensor (`energy_statistics.soc_at_hour_ends`); empty
    without a battery or its SoC sensor."""
    facts = controller.battery_shadow.facts
    entity_id = facts.sources.get("soc") if facts.battery_entity_id is not None else None
    if not entity_id:
        return {}
    tz = controller.demand.tracker.tz
    current = hour_start(now, tz)
    starts = [start for start in (hour_starts(now, tz) if day is None else day_hour_starts(day, tz)) if start < current]
    if not starts:
        return {}
    short, hourly = await async_soc_statistics(hass, entity_id, starts[0], starts[-1] + timedelta(hours=1))
    return soc_at_hour_ends(starts, short, hourly)


async def async_cached_car_hours(
    hass: HomeAssistant,
    site_entry_id: str,
    controller: Any,
    day: date | None = None,
    periods: Sequence[EnergyHour] = (),
) -> dict[datetime, float]:
    """`async_car_hours` (from the energy flow's `periods` where they cover an hour) under the energy flow's cache
    rules."""
    return await _cached(
        hass,
        (site_entry_id, "car", day),
        controller,
        lambda now: async_car_hours(hass, controller, now, day, periods),
        five=True,
    )


def five_slot(now: datetime) -> datetime:
    """The 5-minute row the recorder has compiled last by `now` (it compiles each a few seconds after its end)."""
    moment = (now - COMPILE_LAG).astimezone(timezone.utc)
    return moment.replace(minute=moment.minute - moment.minute % 5, second=0, microsecond=0)


async def _cached(
    hass: HomeAssistant, key: tuple[str, str, date | None], controller: Any, read: Any, five: bool = False
) -> Any:
    """`read(now)`, kept per site, kind and day: with `five` until the recorder's next 5-minute row, else until a new
    hour or `FLOW_REFRESH_S`."""
    now = controller.demand.now()
    mark = five_slot(now) if five else hour_start(now, controller.demand.tracker.tz)
    cache: dict[tuple[str, str, date | None], _FlowCached] = hass.data.setdefault(_FLOW_CACHE, {})
    held = cache.get(key)
    if held is not None and held.hour == mark and (five or (now - held.read_at).total_seconds() < FLOW_REFRESH_S):
        return held.flow
    value = await read(now)
    for stale in [stale for stale, cached in cache.items() if (now - cached.read_at) >= timedelta(hours=1)]:
        del cache[stale]
    cache[key] = _FlowCached(hour=mark, read_at=now, flow=value)
    return value


async def async_cached_day_flow(
    hass: HomeAssistant, site_entry_id: str, controller: Any, day: date | None = None
) -> DayFlow | None:
    """`async_day_flow`, read again once the recorder has a new 5-minute row; per site and day (a few days)."""
    return await _cached(
        hass,
        (site_entry_id, "flow", day),
        controller,
        lambda now: async_day_flow(hass, controller, now, day, site_entry_id),
        five=True,
    )


async def async_cached_energy_flow(
    hass: HomeAssistant, site_entry_id: str, controller: Any, day: date | None = None
) -> dict[str, Any] | None:
    """`async_cached_day_flow`'s flow."""
    read = await async_cached_day_flow(hass, site_entry_id, controller, day)
    return None if read is None else read.flow


async def async_cached_recorded_soc(
    hass: HomeAssistant, site_entry_id: str, controller: Any, day: date | None = None
) -> dict[datetime, float]:
    """`async_recorded_soc` under the energy flow's cache rules."""
    return await _cached(
        hass, (site_entry_id, "soc", day), controller, lambda now: async_recorded_soc(hass, controller, now, day)
    )


async def _or_none(read: Awaitable[Any], what: str) -> Any:
    """`read`'s answer, or `None` when it cannot be had (no recorder, statistics that fail): the view shows the rest."""
    try:
        return await read
    except Exception:  # noqa: BLE001 - a figure that cannot be read is left out, never the whole view
        _LOGGER.debug("SpotNav house view: the %s could not be read", what, exc_info=True)
        return None


@callback
def forget_cached_flow(hass: HomeAssistant, site_entry_id: str) -> None:
    """Drop the site's cached energy flow, its held hours and recorded SoC (its unload)."""
    for name in (_FLOW_CACHE, _PERIOD_CACHE):
        cache: dict[tuple[Any, ...], Any] | None = hass.data.get(name)
        if cache:
            for key in [key for key in cache if key[0] == site_entry_id]:
                del cache[key]


def requested_day(raw: Any, now: datetime, tz: str) -> date | None:
    """The past day asked for (`None`: today), or a `PowerMonthRefusal`."""
    if raw is None:
        return None
    try:
        day = date.fromisoformat(raw) if isinstance(raw, str) and len(raw) == 10 else None
    except ValueError:
        day = None
    today = now.astimezone(time_zones.require_zone(tz)).date()
    if day is None or day > today or day < first_day(now, tz):
        raise PowerMonthRefusal(ERROR_INVALID_DATE, "The date is not a day the house card can show")
    return None if day == today else day


def house_answer(
    hass: HomeAssistant,
    site: Any,
    flow: dict[str, Any] | None = None,
    day: date | None = None,
    soc: dict[datetime, float] | None = None,
    car: dict[datetime, float] | None = None,
    heat: dict[datetime, float] | None = None,
) -> dict[str, Any]:
    """The answer for one site, or a `PowerMonthRefusal`. `heat`: the heat meter's hours the energy flow read (over
    the house forecast's own hours of it)."""
    controller = site_controller_for(hass, site.entry_id)
    if site.state is not ConfigEntryState.LOADED or controller is None:
        raise PowerMonthRefusal(ERROR_NO_SITE, "The site is not loaded")
    demand = controller.demand
    tracker = demand.tracker
    forecast = controller.house_forecast
    # The house figure the forecast is on: the whole load once it has its hours (a battery or a solar site), else
    # the import less the chargers.
    basis = forecast.basis
    if basis == BASIS_GROSS:
        actual = {hour.start: hour.house_kw for hour in forecast.history.gross_hours() if hour.house_kw is not None}
    else:
        actual = {
            hour.start: hour.house_kw
            for hour in (*tracker.previous, *tracker.hours)
            if hour.house_kw is not None and not hour.imported
        }
    source = site_price_source(hass, site.entry_id)
    area = None if source is None else source.area
    return house_view(
        HouseInputs(
            site_id=site.entry_id,
            now=demand.now(),
            tz=tracker.tz,
            log=forecast.log,
            forecast=forecast.forecast,
            house_actual=actual,
            basis=basis,
            sources=HouseSources(
                weather_entity=forecast.weather_entity_id,
                weather=forecast.weather_status,
                outdoor_source=forecast.outdoor_source,
                outdoor_sensor=forecast.outdoor_entity_id,
                holiday_calendar=forecast.holiday_entity_id,
            ),
            sun=_sun(hass, controller),
            battery=_battery(controller),
            money=None if area is None else (area.currency, area.major_unit, area.minor_unit),
            energy_flow=flow,
            day=day,
            soc_recorded=soc or {},
            car_actual=car or {},
            heat=_heat(hass, controller, heat or {}),
        )
    )


@websocket_api.websocket_command(
    {
        vol.Required("type"): "spotnav/get_house",
        vol.Optional("api_version"): object,
        vol.Optional("charger_id"): object,
        vol.Optional("site_id"): object,
        vol.Optional("date"): object,
    }
)
@websocket_api.async_response
async def websocket_get_house(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """The house card's view of one site."""
    version = msg.get("api_version")
    if type(version) is not int or version != HOUSE_API_VERSION:
        send_unsupported_version(connection, msg, HOUSE_API_VERSION)
        return
    try:
        site = _site_of_request(hass, msg)
        controller = site_controller_for(hass, site.entry_id)
        flow = day = soc = car = None
        heat: dict[datetime, float] = {}
        if site.state is ConfigEntryState.LOADED and controller is not None:
            day = requested_day(msg.get("date"), controller.demand.now(), controller.demand.tracker.tz)
            read = await _or_none(async_cached_day_flow(hass, site.entry_id, controller, day), "energy flow")
            periods = () if read is None else read.periods
            flow = None if read is None else read.flow
            soc = await _or_none(async_cached_recorded_soc(hass, site.entry_id, controller, day), "recorded SoC")
            car = await _or_none(async_cached_car_hours(hass, site.entry_id, controller, day, periods), "cars' hours")
            tz = controller.demand.tracker.tz
            heat = hour_figures(periods, "heat", lambda m: hour_start(m, tz))
        answer = house_answer(hass, site, flow, day, soc, car, heat)
    except PowerMonthRefusal as refusal:
        connection.send_error(msg["id"], refusal.code, refusal.message)
        return
    connection.send_result(msg["id"], answer)


@callback
def async_setup_house_api(hass: HomeAssistant) -> None:
    """Register the command once for the domain."""
    websocket_api.async_register_command(hass, websocket_get_house)
