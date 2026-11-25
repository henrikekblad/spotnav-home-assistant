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
(`site/heat_meter.py`), or a heat zone has a sensor of its own (`site/heat_zones.py`), the energy flow reads their
statistics too (a `heat` use split out of the house, the meter and the zones together), and the hours and the `heat`
block take those hours (the flow's reads over the house forecast's own) and the heat forecast the house forecast
keeps (`_heat`).

Refusals (WebSocket errors): `spotnav_unsupported_api_version`, `spotnav_invalid_date` (not a date, in the future or
before `day.first`), `spotnav_charger_required` (neither id), the charger
lookup's codes, `spotnav_unknown_site` and `spotnav_no_site` (the charger is in no site, or the site is not loaded).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Final

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import callback, HomeAssistant

from ..battery.plan_model import DEFAULT_MODE
from .. import time_zones
from ..const import CONF_CHARGER_ENTRY_IDS, CONF_SOLAR_FORECAST_ENTRIES, DOMAIN
from ..core.electric import site_fuse_kw
from ..planning.demand_charge import hour_start
from ..site.energy_flow import (
    HeatPartEnergy,
    with_heat_parts,
    SETTLE,
    EnergyHour,
    FlowSeries,
    energy_flow,
    energy_hours,
    hour_figures,
    inverter_use,
    quarter_hours,
)
from ..site.energy_statistics import (
    short_term_days,
    FlowIds,
    async_energy_prefs,
    async_soc_statistics,
    async_statistics,
    energy_flow_ids,
    soc_at_hour_ends,
    summed,
)
from ..site.session_car import car_energy_from_sessions
from ..site.spotnav_saving import saving_block
from ..pricing.price_source import site_price_source
from ..runtime import controller_for, domain_data, site_controller_for
from ..site.heat_meter import SOURCE_SETTING, STATUS_NONE, async_heat_periods, needs_setting
from ..site.heat_zones import NO_ZONES, ZoneSources
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
from ..site.solar_correction import STATUS_LEARNING
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
    days_left = correction.state.days_left if correction.status == STATUS_LEARNING else None
    return SunInputs(
        status=correction.status, raw=raw, corrected=corrected, forecast_names=tuple(names), days_left=days_left
    )


def _learning(controller: Any, now: datetime) -> tuple[dict[str, Any], ...]:
    """The learning periods that hold something back now (`BatteryControl.learning_hold`); none where the site has
    no battery control or it cannot be read."""
    control = getattr(controller, "battery_control", None)
    if control is None:
        return ()
    try:
        hold = control.learning_hold(now)
    except Exception:  # noqa: BLE001 - the house view never fails on a status line
        _LOGGER.debug("SpotNav house view: the learning period could not be read", exc_info=True)
        return ()
    return () if hold is None else (hold,)


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
        control=_control_state(controller),
        action_now=_action_now(controller),
        power_kw=facts.power_kw,
        round_trip=_plan_round_trip(facts, plan.learned),
        round_trip_learned=plan.learned.round_trip.efficiency is not None,
        round_trip_series=tuple(
            (window.end.date().isoformat(), _on_ac_side(facts, plan.learned, window.efficiency))
            for window in plan.learned.round_trip.windows
        ),
    )


def _plan_round_trip(facts: Any, learned: Any) -> float:
    """The round trip the plan uses (`battery/learned.with_learned`): on a battery read at the battery the AC one, not
    the counters' own; the assumed one until it is learnt."""
    from ..battery.learned import with_learned  # noqa: PLC0415
    from ..battery.plan_model import DEFAULT_ROUND_TRIP_EFFICIENCY  # noqa: PLC0415

    if getattr(facts, "battery_entity_id", None) is None:
        return float(learned.round_trip_efficiency)
    value = with_learned(facts, learned).round_trip_efficiency
    return DEFAULT_ROUND_TRIP_EFFICIENCY if value is None else float(value)


def _on_ac_side(facts: Any, learned: Any, efficiency: float) -> float:
    """A window's round trip as the plan counts it: turned to the AC side on a battery read at the battery."""
    from ..battery.conversion_loss import ac_round_trip  # noqa: PLC0415

    if not getattr(facts, "power_dc", False):
        return efficiency
    return ac_round_trip(efficiency, learned.conversion.loss(True))


def _action_now(controller: Any) -> str | None:
    """The action SpotNav's battery control runs now (its executor's, while active); `None` when it runs none."""
    control = getattr(controller, "battery_control", None)
    executor = None if control is None else getattr(control, "executor", None)
    state = None if executor is None else getattr(executor, "state", None)
    if state is None or getattr(state, "phase", None) != "active":
        return None
    action = getattr(state, "action", None)
    return action if isinstance(action, str) else None


def _control_state(controller: Any) -> str | None:
    """The battery control's state (`BatteryControl.state`), `None` where the site has none to read."""
    control = getattr(controller, "battery_control", None)
    if control is None:
        return None
    try:
        state = control.state()
    except Exception:  # noqa: BLE001 - the house view never fails on the control's status; it says Test instead
        _LOGGER.debug("control_state: ignored failure (the house view never fails on the control's status; it says Test instead)", exc_info=True)
        return None
    return state if isinstance(state, str) else None


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
        actual={**heat.combined_by_hour(), **(read or {})},
        forecast={} if split is None else split.heat.by_hour(),
    )


def fuse_kw(controller: Any) -> float | None:
    """The site's main fuse as power (kW, `site_fuse_kw`); `None` where it is not set, 0 kW where it is unusable."""
    return site_fuse_kw(controller.config)


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


def _session_car(
    hass: HomeAssistant, controller: Any, starts: Sequence[datetime], step: timedelta
) -> dict[datetime, float]:
    """The cars' energy per period from the site's chargers' own charging sessions (`site/session_car.py`): the
    fallback for a charger whose energy register keeps no long-term statistics."""
    store = getattr(domain_data(hass), "session_store", None)
    if store is None:
        return {}
    sessions: list[Any] = []
    for charger_entry_id in controller.config.get(CONF_CHARGER_ENTRY_IDS) or []:
        sessions.extend(store.closed_raw(charger_entry_id))
        if (open_session := store.open_raw(charger_entry_id)) is not None:
            sessions.append(open_session)
    return car_energy_from_sessions(sessions, starts, step)


def _car_hours(
    hass: HomeAssistant, controller: Any, rows: Any, car_ids: tuple[str, ...], starts: Sequence[datetime] = ()
) -> dict[datetime, float]:
    """The cars' energy per hour (kWh, UTC hour starts): the battery plan's measure, the chargers' registers over it,
    and for the hours of `starts` neither has, the charging sessions' own energy."""
    car = _bucket_car(controller) if controller.battery_shadow.facts.battery_entity_id is not None else {}
    if car_ids:
        car = {**car, **summed(rows, car_ids)}
    missing = [start for start in starts if start not in car]
    if missing:
        car = {**_session_car(hass, controller, missing, timedelta(hours=1)), **car}
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
    car = _car_hours(hass, controller, rows, car_ids, left)
    return {**{start: kwh for start, kwh in car.items() if start in wanted}, **known}


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


def _car_quarters(
    hass: HomeAssistant, controller: Any, now: datetime, car_ids: tuple[str, ...], hours: Sequence[datetime]
) -> dict[datetime, float] | None:
    """The cars' energy per quarter hour where the chargers' registers give none: the battery plan's measure (where
    the chargers have no register), else the charging sessions' own energy."""
    if not car_ids and controller.battery_shadow.facts.battery_entity_id is not None:
        return _bucket_car_quarters(controller, now)
    quarters = [hour + index * timedelta(minutes=15) for hour in hours for index in range(4)]
    return _session_car(hass, controller, quarters, timedelta(minutes=15)) or None


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
    zones: ZoneSources = NO_ZONES,
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
        try:
            heat = await async_heat_periods(hass, meter, zones, first, end, "5minute", fuse_kw(controller))
        except Exception:  # noqa: BLE001 - without the heat figures, heat stays in the house
            _LOGGER.debug("SpotNav house view: the heat's 5-minute rows could not be read", exc_info=True)
        has_battery = bool(ids.battery_in)
        five = FlowSeries(
            grid_in=summed(rows, ids.grid_in),
            grid_out=summed(rows, ids.grid_out) if ids.grid_out else None,
            solar=summed(rows, ids.solar) if ids.solar else None,
            battery_in=summed(rows, ids.battery_in) if has_battery else None,
            battery_out=summed(rows, ids.battery_out) if has_battery else None,
            car=summed(rows, car_ids) if car_ids else None,
            heat=heat,
            car_quarters=_car_quarters(hass, controller, now, car_ids, to_read),
        )
        read, missing = quarter_hours(to_read, five, now)
        if missing:
            read += await _hourly_periods(
                hass, controller, missing, missing[-1] + timedelta(hours=1), ids, car_ids, meter, zones
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
    zones: ZoneSources = NO_ZONES,
) -> list[EnergyHour]:
    """The hours of `starts` (completed, before `end`) from the recorder's hourly rows."""
    rows = await async_statistics(hass, (*ids.all(), *car_ids), starts[0], end, "hour")
    heat = None
    try:
        heat = await async_heat_periods(hass, meter, zones, starts[0], end, cap_kw=fuse_kw(controller))
    except Exception:  # noqa: BLE001 - without the heat figures, heat stays in the house
        _LOGGER.debug("SpotNav house view: the heat's hours could not be read", exc_info=True)
    has_battery = bool(ids.battery_in)
    return energy_hours(
        starts,
        summed(rows, ids.grid_in),
        summed(rows, ids.grid_out) if ids.grid_out else None,
        summed(rows, ids.solar) if ids.solar else None,
        summed(rows, ids.battery_in) if has_battery else None,
        summed(rows, ids.battery_out) if has_battery else None,
        _car_hours(hass, controller, rows, car_ids, starts),
        heat,
    )


@dataclass(frozen=True, slots=True)
class HeatPart:
    """A pool, spa or sauna part counted in heat (`site/energy_flow.with_heat_parts`): its part name
    (`pool/runtime.ENERGY_PARTS`), the entity it is set up by, its name, its energy per UTC quarter hour and what of
    it is an estimate (on time at its power, `pool/runtime.estimated_quarters`)."""

    part: str
    entity_id: str
    name: str
    quarters: Mapping[datetime, float]
    estimated: Mapping[datetime, float] = field(default_factory=dict)


def heat_parts(hass: HomeAssistant, controller: Any, meter: Any, zones: ZoneSources) -> list[HeatPart]:
    """The pool's heat pump and circulation, the spa and the sauna as parts of heat: those set up with energy counted,
    less a part whose device the heat pump's meter, a zone's climate or a zone's sensor (a meter moved to heat) is on,
    which already counts it (counted once)."""
    from homeassistant.helpers import entity_registry as er  # noqa: PLC0415

    shadow = getattr(controller, "pool_spa", None)
    if shadow is None:
        return []
    config = shadow.config()
    found: list[tuple[str, str]] = []
    pool = config.pool
    if pool is not None:
        if pool.heat_pump is not None:
            found.append(("pool_heat", pool.heat_pump.switch_id or pool.heat_pump.entity_id))
        if pool.circulation_id is not None:
            found.append(("pool_circulation", pool.circulation_id))
    for name in ("spa", "sauna"):
        block = getattr(config, name)
        if block is not None:
            found.append((name, block.entity_id))
    if not found:
        return []
    registry = er.async_get(hass)

    def device(entity_id: str | None) -> str | None:
        entry = None if entity_id is None else registry.async_get(entity_id)
        return None if entry is None else entry.device_id

    covered = {
        device(entity_id)
        for entity_id in (
            meter.entity_id if meter.found else None,
            *(source.sensor_id for source in zones.sources),
            *(source.entity_id for source in zones.sources),
            *controller.house_forecast.heat.zone_climates(),
        )
    } - {None}
    out: list[HeatPart] = []
    for part, entity_id in found:
        if device(entity_id) in covered:
            continue
        quarters = shadow.energy_quarters(part)
        if not quarters:
            continue
        state = hass.states.get(entity_id)
        estimated = getattr(shadow, "estimated_quarters", None)
        # A shadow that cannot tell: every quarter an estimate (it may take only the house's room).
        guessed = quarters if estimated is None else estimated(part)
        out.append(HeatPart(part, entity_id, entity_id if state is None else state.name, quarters, guessed))
    return out


def battery_loss(controller: Any) -> tuple[float, float] | None:
    """The inverter's `(charge, discharge)` loss for a battery read at the battery (`battery/conversion_loss.py`: the
    plan's learnt one, else the assumed one), `None` for one read on the AC side or without a battery."""
    shadow = getattr(controller, "battery_shadow", None)
    facts = getattr(shadow, "facts", None)
    if facts is None or getattr(facts, "battery_entity_id", None) is None or not getattr(facts, "power_dc", False):
        return None
    learned = getattr(getattr(shadow, "plan", None), "learned", None)
    if learned is None:
        from ..battery.conversion_loss import ConversionLoss  # noqa: PLC0415 - kept beside its only use

        return ConversionLoss().loss(True)
    loss: tuple[float, float] = learned.conversion.loss(True)
    return loss


def inverter_idle_kw(controller: Any) -> float | None:
    """The inverter's standing use (kW) the battery plan learnt for a battery read at the battery
    (`battery/conversion_loss.ConversionLoss.idle`), `None` where it is not learnt or the battery is read on the AC
    side."""
    shadow = getattr(controller, "battery_shadow", None)
    facts = getattr(shadow, "facts", None)
    learned = getattr(getattr(shadow, "plan", None), "learned", None)
    if facts is None or learned is None or not getattr(facts, "power_dc", False):
        return None
    idle: float | None = learned.conversion.idle(True)
    return idle


def flow_battery_loss(hass: HomeAssistant, controller: Any, ids: FlowIds) -> tuple[float, float] | None:
    """`battery_loss` where the Energy dashboard's battery meters are the battery's own (every one an entity of the
    battery's integration, so counted on the same side as its power): the energy flow counts what the inverter used
    from them (`site/energy_flow.inverter_use`); `None` otherwise."""
    loss = battery_loss(controller)
    meters = (*ids.battery_in, *ids.battery_out)
    if loss is None or not meters:
        return None
    from homeassistant.helpers import entity_registry as er  # noqa: PLC0415

    registry = er.async_get(hass)
    platform = controller.battery_shadow.facts.integration
    for meter in meters:
        entry = registry.async_get(meter)
        if entry is None or entry.platform != platform:
            return None
    return loss


def with_inverter(hass: HomeAssistant, controller: Any, ids: FlowIds, periods: Sequence[EnergyHour]) -> list[EnergyHour]:
    """The periods with what a hybrid's inverter used of the house (`site/energy_flow.inverter_use`: its loss on a
    battery whose meters are its own counters, `flow_battery_loss`, and its standing use where learnt), else as they
    are. The battery's figures stay as read either way."""
    try:
        loss = flow_battery_loss(hass, controller, ids)
        idle = None if loss is None else inverter_idle_kw(controller)
    except Exception:  # noqa: BLE001 - without the loss the inverter is not said apart
        _LOGGER.debug("SpotNav house view: the battery's side could not be told", exc_info=True)
        loss = None
        idle = None
    return list(periods) if loss is None else inverter_use(periods, loss, idle)


def _with_parts(hass: HomeAssistant, controller: Any, periods: Sequence[EnergyHour], meter: Any, zones: ZoneSources) -> tuple[list[EnergyHour], int]:
    """The periods with the pool's, the spa's and the sauna's energy in their heat (an estimate only where the house
    has room, `site/energy_flow.with_heat_parts`), and how many parts that is."""
    try:
        parts = heat_parts(hass, controller, meter, zones)
    except Exception:  # noqa: BLE001 - without them they stay in the house
        _LOGGER.debug("SpotNav house view: the pool's and the spa's energy could not be read", exc_info=True)
        return list(periods), 0
    energies = [HeatPartEnergy(part.part, part.quarters, part.estimated) for part in parts]
    return with_heat_parts(periods, energies), len(parts)


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
    zones = await controller.house_forecast.heat.async_current_zones()
    hours = []
    moment = midnight
    while moment < end:
        hours.append(moment)
        moment += timedelta(hours=1)
    if quarters:
        signature = (
            ids,
            car_ids,
            meter.entity_id if meter.found else None,
            meter.kind if meter.found else None,
            zones.signature(),
        )
        held = None if site_entry_id is None else _held_day(hass, (site_entry_id, day), signature)
        periods = await _quarter_periods(hass, controller, now, hours, ids, car_ids, meter, held, zones)
    else:
        periods = await _hourly_periods(hass, controller, hours, end, ids, car_ids, meter, zones)
    periods, parts = _with_parts(hass, controller, periods, meter, zones)
    periods = with_inverter(hass, controller, ids, periods)
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
        has_heat=meter.found or bool(zones.sources) or parts > 0,
        heat_sources=(1 if meter.found else 0) + len(zones.sources) + parts,
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


async def async_saving(
    hass: HomeAssistant, site_entry_id: str, controller: Any, day: date | None
) -> dict[str, Any] | None:
    """What SpotNav saved on the day shown (`api/saving.py`, `site/spotnav_saving.saving_block`)."""
    from .saving import async_day_savings  # noqa: PLC0415 - it imports this module

    shown = day or controller.demand.now().astimezone(time_zones.require_zone(controller.demand.tracker.tz)).date()
    days, since = await async_day_savings(hass, site_entry_id, controller, [shown])
    saving = days.get(shown)
    return None if saving is None else saving_block(shown, saving, since)


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
    saving: Mapping[str, Any] | None = None,
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
            learning=_learning(controller, demand.now()),
            saving=saving,
            demand_ceiling=_demand_ceiling(controller),
        )
    )


def _demand_ceiling(controller: Any) -> dict[str, Any] | None:
    """The site's soft ceiling in shadow (`site/demand_ceiling.py`), `None` where it has none to show."""
    ceiling = getattr(controller, "demand_ceiling", None)
    if ceiling is None:
        return None
    try:
        return ceiling.as_dict()
    except Exception:  # noqa: BLE001 - a shadow block never fails the answer
        _LOGGER.debug("house_answer: the demand ceiling could not be read", exc_info=True)
        return None


async def async_house_answer(hass: HomeAssistant, site: Any, date_raw: Any = None) -> dict[str, Any]:
    """The whole read for one site and day (the WebSocket command and the webhook's `get_house` alike): the cached
    reads, then `house_answer`. A `PowerMonthRefusal` for a request it cannot honour."""
    controller = site_controller_for(hass, site.entry_id)
    flow = day = soc = car = saving = None
    heat: dict[datetime, float] = {}
    if site.state is ConfigEntryState.LOADED and controller is not None:
        day = requested_day(date_raw, controller.demand.now(), controller.demand.tracker.tz)
        read = await _or_none(async_cached_day_flow(hass, site.entry_id, controller, day), "energy flow")
        periods = () if read is None else read.periods
        flow = None if read is None else read.flow
        soc = await _or_none(async_cached_recorded_soc(hass, site.entry_id, controller, day), "recorded SoC")
        car = await _or_none(async_cached_car_hours(hass, site.entry_id, controller, day, periods), "cars' hours")
        tz = controller.demand.tracker.tz
        heat = hour_figures(periods, "heat", lambda m: hour_start(m, tz))
        saving = await _or_none(async_saving(hass, site.entry_id, controller, day), "saving")
    return house_answer(hass, site, flow, day, soc, car, heat, saving)


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
        answer = await async_house_answer(hass, _site_of_request(hass, msg), msg.get("date"))
    except PowerMonthRefusal as refusal:
        connection.send_error(msg["id"], refusal.code, refusal.message)
        return
    connection.send_result(msg["id"], answer)


@callback
def async_setup_house_api(hass: HomeAssistant) -> None:
    """Register the command once for the domain."""
    websocket_api.async_register_command(hass, websocket_get_house)
