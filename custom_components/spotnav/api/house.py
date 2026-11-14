"""`spotnav/get_house` (API v1): the house card's view of one site, read-only and in shadow (SpotNav 3.0 preview).

`{api_version: 1, charger_id | site_id, date?}`: `date` (`YYYY-MM-DD`, the site's local day) asks for a past day from
`day.first` on instead of the hours around now (`site/house_view.py`, its `day` block); absent or today: as before. Any user who can see the card may read, as the dashboard. The answer is
`site/house_view.py:house_view` (its docstring has the shape), built from what the site already holds: the house
load forecast and the forecast log with its nightly reviews (`site/house_forecast.py`), the sun's forecast raw and
corrected (`site/solar_correction_shadow.py`), and the battery's facts and plan in shadow (`battery/runtime.py`,
`battery/plan_runtime.py`). Nothing is computed anew or written; the one thing fetched is today's energy flow
(`energy_flow`, `site/energy_flow.py`): the Energy dashboard's hourly statistics and the chargers' energy registers,
read from the recorder at most every `FLOW_REFRESH_S` per site and between hours kept in `hass.data`; and, under the
same rules, the battery's SoC at the end of each shown past hour its plan's log has no level for (the recorder's
statistics of its SoC sensor, `async_recorded_soc`).

Refusals (WebSocket errors): `spotnav_unsupported_api_version`, `spotnav_invalid_date` (not a date, in the future or
before `day.first`), `spotnav_charger_required` (neither id), the charger
lookup's codes, `spotnav_unknown_site` and `spotnav_no_site` (the charger is in no site, or the site is not loaded).
"""

from __future__ import annotations

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
from ..site.energy_flow import energy_flow, energy_hours
from ..site.energy_statistics import (
    async_energy_prefs,
    async_soc_statistics,
    async_statistics,
    energy_flow_ids,
    soc_at_hour_ends,
    summed,
)
from ..pricing.price_source import site_price_source
from ..runtime import controller_for, site_controller_for
from ..site.house_view import (
    HOUSE_API_VERSION,
    BatteryInputs,
    HouseInputs,
    HouseSources,
    SunInputs,
    day_hour_starts,
    first_day,
    hour_starts,
    house_view,
)
from ..site.forecast_log import KIND_SUN
from ..battery.plan_shadow import KIND_BATTERY
from ..site.house_forecast import BASIS_GROSS
from ..site.solar_correction_shadow import STATUS_NO_FORECAST
from ..battery.shadow import parse_aware_datetime
from .common import send_unsupported_version
from .power_month import ERROR_NO_SITE, PowerMonthRefusal, _site_of_request

#: Today's energy flow is read from the recorder again after this many seconds, and at every new hour.
FLOW_REFRESH_S: Final = 600
_FLOW_CACHE: Final = f"{DOMAIN}_house_flow"


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


def _bucket_car(controller: Any) -> dict[datetime, float]:
    """The cars' energy per local hour (kWh, UTC hour starts) as the battery plan measured it: only hours all four of
    whose quarter hours were measured."""
    tz = controller.demand.tracker.tz
    quarters: dict[datetime, list[float]] = {}
    for bucket in controller.battery_shadow.plan.actuals.buckets.values():
        if bucket.covered:
            quarters.setdefault(hour_start(bucket.start, tz), []).append(bucket.scaled(bucket.car_kwh))
    return {start: sum(values) for start, values in quarters.items() if len(values) == 4}


async def async_energy_flow(
    hass: HomeAssistant, controller: Any, now: datetime, day: date | None = None
) -> dict[str, Any] | None:
    """A day's energy flow (`site/energy_flow.py`, today when `day` is `None`) from the recorder: its completed
    hours; `None` without the Energy dashboard's grid import meter or before a completed hour of the day."""
    tz = controller.demand.tracker.tz
    zone = time_zones.require_zone(tz)
    today = now.astimezone(zone).date()
    day = day or today
    midnight = datetime(day.year, day.month, day.day, tzinfo=zone).astimezone(timezone.utc)
    following = day + timedelta(days=1)
    end = min(
        hour_start(now, tz), datetime(following.year, following.month, following.day, tzinfo=zone).astimezone(timezone.utc)
    )
    if end <= midnight:
        return None
    ids = energy_flow_ids(await async_energy_prefs(hass))
    if not ids.grid_in:
        return None
    registers: list[str | None] = []
    for charger_entry_id in controller.config.get(CONF_CHARGER_ENTRY_IDS) or []:
        charger = controller_for(hass, charger_entry_id)
        registers.append(None if charger is None else charger.energy_register_entity_id)
    car_ids = tuple(dict.fromkeys(r for r in registers if r)) if registers and all(registers) else ()
    rows = await async_statistics(hass, (*ids.all(), *car_ids), midnight, end, "hour")
    starts = []
    moment = midnight
    while moment < end:
        starts.append(moment)
        moment += timedelta(hours=1)
    car = _bucket_car(controller) if controller.battery_shadow.facts.battery_entity_id is not None else {}
    if car_ids:
        car = {**car, **summed(rows, car_ids)}
    has_battery = bool(ids.battery_in)
    hours = energy_hours(
        starts,
        summed(rows, ids.grid_in),
        summed(rows, ids.grid_out) if ids.grid_out else None,
        summed(rows, ids.solar) if ids.solar else None,
        summed(rows, ids.battery_in) if has_battery else None,
        summed(rows, ids.battery_out) if has_battery else None,
        car,
    )
    soc = None
    if has_battery and day == today:
        soc = controller.battery_shadow.facts.soc_pct
    elif has_battery and hours:
        # A past day: the SoC logged at its last hour's end.
        logged = controller.house_forecast.log.hours(KIND_BATTERY).get(max(hour.start for hour in hours))
        soc = None if logged is None else logged.actual
    return energy_flow(hours, day=day, has_solar=bool(ids.solar), has_battery=has_battery, soc_pct=soc)


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


async def _cached(hass: HomeAssistant, key: tuple[str, str, date | None], controller: Any, read: Any) -> Any:
    """`read(now)`, kept per site, kind and day until a new hour or `FLOW_REFRESH_S`."""
    now = controller.demand.now()
    hour = hour_start(now, controller.demand.tracker.tz)
    cache: dict[tuple[str, str, date | None], _FlowCached] = hass.data.setdefault(_FLOW_CACHE, {})
    held = cache.get(key)
    if held is not None and held.hour == hour and (now - held.read_at).total_seconds() < FLOW_REFRESH_S:
        return held.flow
    value = await read(now)
    for stale in [stale for stale, cached in cache.items() if cached.hour != hour]:
        del cache[stale]
    cache[key] = _FlowCached(hour=hour, read_at=now, flow=value)
    return value


async def async_cached_energy_flow(
    hass: HomeAssistant, site_entry_id: str, controller: Any, day: date | None = None
) -> dict[str, Any] | None:
    """`async_energy_flow`, read again at a new hour or after `FLOW_REFRESH_S`; per site and day (a few days)."""
    return await _cached(
        hass, (site_entry_id, "flow", day), controller, lambda now: async_energy_flow(hass, controller, now, day)
    )


async def async_cached_recorded_soc(
    hass: HomeAssistant, site_entry_id: str, controller: Any, day: date | None = None
) -> dict[datetime, float]:
    """`async_recorded_soc` under the energy flow's cache rules."""
    return await _cached(
        hass, (site_entry_id, "soc", day), controller, lambda now: async_recorded_soc(hass, controller, now, day)
    )


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
) -> dict[str, Any]:
    """The answer for one site, or a `PowerMonthRefusal`."""
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
        flow = day = soc = None
        if site.state is ConfigEntryState.LOADED and controller is not None:
            day = requested_day(msg.get("date"), controller.demand.now(), controller.demand.tracker.tz)
            flow = await async_cached_energy_flow(hass, site.entry_id, controller, day)
            soc = await async_cached_recorded_soc(hass, site.entry_id, controller, day)
        answer = house_answer(hass, site, flow, day, soc)
    except PowerMonthRefusal as refusal:
        connection.send_error(msg["id"], refusal.code, refusal.message)
        return
    connection.send_result(msg["id"], answer)


@callback
def async_setup_house_api(hass: HomeAssistant) -> None:
    """Register the command once for the domain."""
    websocket_api.async_register_command(hass, websocket_get_house)
