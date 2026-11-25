"""`spotnav/get_house_loads` (API v1): the house's known meters over a day, month or year of a site (read-only), for
the pie the house card opens from "House" in its energy flow.

`{api_version: 1, site_id | charger_id, period?: "day" | "month" | "year", date?: "YYYY-MM-DD", kind?: "house" |
"heat"}` (absent: today, the house). `kind: "heat"` is the pie behind "Heat": the heat pump's meter, each heat zone
counted and each meter the household moved to heat (`site/heat_zones.py`), against the flow's heat block; an unknown
kind is `spotnav_invalid_kind`.
Any user who can see the card may read. The answer is `site/house_loads.py:house_loads` (its docstring has the shape).
A command of its own, asked only when the pie is opened, so neither the front's answer (read every five minutes) nor
the history's carries the meters.

The house's figure is the energy flow's house block for the period, as the card shows it: the history's answer for
the period (`api/house_history.history_answer`, its cache), whose day is the front's day flow; each meter's hours are
judged against the energy periods kept with that answer (never read again here). The meters are the Energy
dashboard's individual devices (`energy/get_prefs`); their statistics are read in the recorder's executor over the
period's start up to the flow's `through`: by the hour, and for a day read by the quarter hour its 5-minute rows as
well (the running hour). A month's or a year's hour-by-hour passes (the site's use per hour, each meter's plausible
hours) run in an executor job, not in the event loop. The answer is kept per site and period until the flow's `through` or the meters change.
Refusals as `spotnav/get_house_history`, and `spotnav_read_failed` when the statistics cannot be read.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime, timezone
import logging
from typing import Any, Final

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.core import callback, HomeAssistant
from homeassistant.helpers import entity_registry as er

from ..const import CONF_CHARGE_CONTROL, CONF_CHARGER_ENTRY_IDS, CONF_METER_BELONGS, CONF_POWER_ENTITY
from ..energy_register import _charger_device_id
from ..runtime import controller_for, site_controller_for
from ..site.energy_flow import ROLE_CAR, ROLE_HEAT, ROLE_HOUSE, EnergyHour, allocate_hour, parts_kwh
from ..site.energy_statistics import async_energy_prefs, async_statistics
from ..site.history_period import PERIOD_DAY, period_of, utc_bounds
from ..site.heat_meter import async_heat_hours
from ..site.heat_zones import ORIGIN_ZONE, ZoneSources, async_zone_periods
from ..site.house_loads import (
    HOUSE_LOADS_API_VERSION,
    KIND_HEAT,
    KIND_HOUSE,
    SOURCE_HEAT_PUMP,
    SOURCE_METER,
    SOURCE_ZONE,
    DashboardMeter,
    MeterRead,
    changes,
    counted_meters,
    house_loads,
    plausible_kwh,
    split_belongs,
    stored_belongs,
)
from .common import send_unsupported_version
from .house import HeatPart, fuse_kw, heat_parts
from .house_history import history_with_periods
from .power_month import PowerMonthRefusal, _site_of_request

_LOGGER = logging.getLogger(__name__)

ERROR_READ_FAILED: Final = "spotnav_read_failed"
ERROR_INVALID_KIND: Final = "spotnav_invalid_kind"
_CACHE: Final = "spotnav_house_loads"
#: The answers kept (all sites together); the oldest goes first.
CACHE_SIZE: Final = 24


def dashboard_meters(hass: HomeAssistant, prefs: Any) -> list[DashboardMeter]:
    """The Energy dashboard's individual devices, each with the name it is shown by there (its own, else its entity's
    friendly name, else the statistic id) and the device its statistic's entity is on."""
    devices = prefs.get("device_consumption") if isinstance(prefs, Mapping) else None
    entities = er.async_get(hass)
    out: list[DashboardMeter] = []
    for device in devices if isinstance(devices, list) else []:
        if not isinstance(device, Mapping) or not isinstance(device.get("stat_consumption"), str):
            continue
        stat = device["stat_consumption"]
        entry = entities.async_get(stat)
        state = hass.states.get(stat)
        names = (
            device.get("name"),
            None if state is None else state.attributes.get("friendly_name"),
            None if entry is None else entry.name or entry.original_name,
        )
        name = next((value.strip() for value in names if isinstance(value, str) and value.strip()), stat)
        rate = device.get("stat_rate")
        nested = device.get("included_in_stat")
        out.append(
            DashboardMeter(
                statistic_id=stat,
                name=name,
                rate_id=rate if isinstance(rate, str) else None,
                device_id=None if entry is None else entry.device_id,
                included_in=nested if isinstance(nested, str) else None,
            )
        )
    return out


def charger_sensors(hass: HomeAssistant, controller: Any) -> tuple[set[str], set[str]]:
    """The site's chargers' sensors (energy register, power sensor, charge control) and devices."""
    entities: set[str] = set()
    devices: set[str] = set()
    registry = er.async_get(hass)
    for charger_entry_id in controller.config.get(CONF_CHARGER_ENTRY_IDS) or []:
        charger = controller_for(hass, charger_entry_id)
        register = None if charger is None else charger.energy_register_entity_id
        entry = hass.config_entries.async_get_entry(charger_entry_id)
        data = {} if entry is None else dict(entry.data)
        for entity_id in (register, data.get(CONF_POWER_ENTITY), data.get(CONF_CHARGE_CONTROL)):
            if isinstance(entity_id, str) and entity_id:
                entities.add(entity_id)
                registered = registry.async_get(entity_id)
                if registered is not None and registered.device_id is not None and entity_id == register:
                    devices.add(registered.device_id)
        device_id = _charger_device_id(hass, data) if data else None
        if device_id is not None:
            devices.add(device_id)
    return entities, devices


#: The uses a meter's hour is judged against: the site's whole use (a dashboard device may also measure the car or
#: the heat).
SITE_USES: Final = (ROLE_HOUSE, ROLE_HEAT, ROLE_CAR)


def _limits(periods: Any) -> dict[datetime, float]:
    """What the whole site used (`SITE_USES`) per UTC hour in the flow's periods (`site/energy_flow.allocate_hour`):
    what a meter's hour is judged against (`site/house_loads.plausible_kwh`)."""
    out: dict[datetime, float] = {}
    for item in periods or ():
        hour = item.start.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
        used = sum(kwh for (_source, use), kwh in allocate_hour(item).items() if use in SITE_USES)
        out[hour] = out.get(hour, 0.0) + used
    return out


async def _work(hass: HomeAssistant, heavy: bool, job: Callable[[], Any]) -> Any:
    """`job()` in an executor job when `heavy` (a month's or a year's hours), else at once."""
    return await hass.async_add_executor_job(job) if heavy else job()


async def _read(
    hass: HomeAssistant,
    meters: list[DashboardMeter],
    start: datetime,
    through: datetime,
    five: bool,
    limits: Mapping[datetime, float] | None,
    cap_kwh: float | None,
    heavy: bool = False,
) -> list[tuple[DashboardMeter, MeterRead]]:
    ids = [meter.statistic_id for meter in meters]
    hourly = await async_statistics(hass, ids, start, through, "hour")
    short = await async_statistics(hass, ids, start, through, "5minute") if five else {}

    def judge() -> list[tuple[DashboardMeter, MeterRead]]:
        return [
            (
                meter,
                plausible_kwh(
                    changes(hourly.get(meter.statistic_id) or []),
                    changes(short.get(meter.statistic_id) or []),
                    start,
                    through,
                    limits=limits,
                    cap_kwh=cap_kwh,
                ),
            )
            for meter in meters
        ]

    return await _work(hass, heavy, judge)


async def _read_heat(
    hass: HomeAssistant,
    meter: Any,
    zones: ZoneSources,
    start: datetime,
    through: datetime,
    five: bool,
    limits: Mapping[datetime, float] | None,
    cap_kwh: float | None,
    heavy: bool = False,
) -> list[tuple[DashboardMeter, MeterRead]]:
    """The heat's sources (the heat pump's meter, each zone counted) and their plausible energy in the span."""
    figures: list[tuple[DashboardMeter, Mapping[datetime, float], Mapping[datetime, float]]] = []
    if meter.found and meter.entity_id:
        hourly = await async_heat_hours(hass, meter, start, through)
        short = await async_heat_hours(hass, meter, start, through, "5minute") if five else {}
        state = hass.states.get(meter.entity_id)
        name = meter.entity_id if state is None else state.name
        figures.append((DashboardMeter(meter.entity_id, name, source=SOURCE_HEAT_PUMP), hourly, short))
    if zones.sources:
        hourly_zones = await async_zone_periods(hass, zones, start, through)
        short_zones = await async_zone_periods(hass, zones, start, through, "5minute") if five else {}
        for source in zones.sources:
            row = DashboardMeter(
                source.sensor_id, source.name, source=SOURCE_ZONE if source.origin == ORIGIN_ZONE else SOURCE_METER
            )
            figures.append((row, hourly_zones.get(source.sensor_id, {}), short_zones.get(source.sensor_id, {})))

    def judge() -> list[tuple[DashboardMeter, MeterRead]]:
        return [
            (row, plausible_kwh(hourly, short, start, through, limits=limits, cap_kwh=cap_kwh))
            for row, hourly, short in figures
        ]

    return await _work(hass, heavy, judge)


def part_reads(
    parts: Sequence[HeatPart], start: datetime, through: datetime, periods: Sequence[EnergyHour] | None = None
) -> tuple[list[tuple[DashboardMeter, MeterRead]], dict[str, tuple[float, float]]]:
    """The pool's, the spa's and the sauna's parts in the heat pie: each part's energy in `[start, through)` as the
    energy flow's heat holds it (`periods`' `parts`: an estimate only where the house had room,
    `site/energy_flow.with_heat_parts`), its `source` the part's name; and each part's `(estimated kWh counted, kWh
    clamped)` by its entity (`site/house_loads.house_loads`'s `estimates`). Without the periods, the quarters as
    counted (an estimate in full)."""
    held = None if periods is None else parts_kwh(periods, start, through)
    out: list[tuple[DashboardMeter, MeterRead]] = []
    estimates: dict[str, tuple[float, float]] = {}
    for part in parts:
        row = DashboardMeter(part.entity_id, part.name, source=part.part)
        if held is not None:
            share = held.get(part.part)
            kwh = 0.0 if share is None else share.kwh
            estimates[part.entity_id] = (0.0, 0.0) if share is None else (share.estimated, share.clamped)
        else:
            kwh = sum(value for moment, value in part.quarters.items() if start <= moment < through)
            guess = sum(value for moment, value in part.estimated.items() if start <= moment < through)
            estimates[part.entity_id] = (min(kwh, guess), 0.0)
        out.append((row, MeterRead(kwh)))
    return out, estimates


def _zone_devices(hass: HomeAssistant, climates: Any) -> set[str]:
    registry = er.async_get(hass)
    out: set[str] = set()
    for climate in climates:
        entry = registry.async_get(climate)
        if entry is not None and entry.device_id is not None:
            out.add(entry.device_id)
    return out


async def loads_answer(
    hass: HomeAssistant, site: Any, raw_period: Any, raw_date: Any, kind: str = KIND_HOUSE
) -> dict[str, Any]:
    """The answer for one site, period and pie (`kind`), or a `PowerMonthRefusal`."""
    history, periods = await history_with_periods(
        hass, site, PERIOD_DAY if raw_period is None else raw_period, raw_date
    )
    controller = site_controller_for(hass, site.entry_id)
    if controller is None:  # unloaded between the two reads
        raise PowerMonthRefusal("spotnav_no_site", "The site is not loaded")
    tz = controller.demand.tracker.tz
    period = period_of(history["period"], date.fromisoformat(history["date"]))
    flow = history["energy_flow"]
    role = ROLE_HEAT if kind == KIND_HEAT else ROLE_HOUSE
    figure = None
    through = None
    five = False
    inverter = None
    if flow is not None:
        figure = next((float(node["kwh"]) for node in flow["nodes"] if node["role"] == role), 0.0)
        # The inverter's own slice of the house pie (`site/energy_flow.inverter_use`), where the flow counts it.
        inverter = flow.get("inverter_kwh") if kind == KIND_HOUSE else None
        through = datetime.fromisoformat(flow["through"])
        five = int(flow.get("step_min") or 60) < 60
    belongs = stored_belongs(site.data, CONF_METER_BELONGS)
    heat = controller.house_forecast.heat
    meter = heat.meter
    zones = await heat.async_current_zones()
    moved: list[tuple[DashboardMeter, str]] = []
    meters: list[DashboardMeter] = []
    try:
        parts = heat_parts(hass, controller, meter, zones)
    except Exception:  # noqa: BLE001 - without them they stay in the house
        _LOGGER.debug("SpotNav house loads: the pool's and the spa's energy could not be read", exc_info=True)
        parts = []
    if kind == KIND_HOUSE:
        car_entities, car_devices = charger_sensors(hass, controller)
        # The heat's own sensors are sorted out by themselves; a meter the household moved to heat is listed as moved.
        heat_entities = {meter.entity_id} if meter.found and meter.entity_id else set()
        heat_entities |= {source.sensor_id for source in zones.sources if source.origin == ORIGIN_ZONE}
        counted = counted_meters(
            dashboard_meters(hass, await async_energy_prefs(hass)),
            car_entities=car_entities,
            car_devices=car_devices,
            heat_entities=heat_entities,
            # A dashboard meter on the pool's, the spa's or the sauna's device: that part is in the heat already.
            heat_devices=_zone_devices(hass, [*heat.zone_climates(), *(part.entity_id for part in parts)]),
        )
        meters, moved = split_belongs(counted, belongs)
    cache: dict[tuple[Any, ...], tuple[Any, dict[str, Any]]] = hass.data.setdefault(_CACHE, {})
    key = (site.entry_id, kind, period.kind, period.first)
    part_mark = tuple((part.part, part.entity_id, round(sum(part.quarters.values()), 4)) for part in parts)
    mark = (through, figure, inverter, tuple(meters), tuple(moved), zones.signature(), meter.entity_id, tuple(sorted(belongs.items())), part_mark)
    held = cache.get(key)
    if held is not None and held[0] == mark:
        return held[1]
    start = utc_bounds(period, tz)[0]
    reads: list[tuple[DashboardMeter, MeterRead]] = []
    estimates: dict[str, tuple[float, float]] = {}
    if through is not None and through > start and (meters or kind == KIND_HEAT):
        # Without the flow's hours (they could not be read) only the fuse bounds a meter's hour.
        heavy = period.kind != PERIOD_DAY
        limits = await _work(hass, heavy, lambda: _limits(periods)) if periods else None
        cap = fuse_kw(controller)  # kW over an hour: kWh
        if kind == KIND_HEAT:
            reads = await _read_heat(hass, meter, zones, start, through, five, limits, cap, heavy)
            counted, estimates = part_reads(parts, start, through, periods)
            reads += counted
        else:
            reads = await _read(hass, meters, start, through, five, limits, cap, heavy)
    elif kind == KIND_HEAT:
        reads = []
    if not reads and kind == KIND_HOUSE:
        reads = [(item, MeterRead(0.0)) for item in meters]
    answer = house_loads(
        site_id=site.entry_id,
        period=period.kind,
        first=period.first.isoformat(),
        end=period.end.isoformat(),
        through=through,
        house_kwh=figure,
        loads=[(item, read.kwh) for item, read in reads],
        kind=kind,
        moved=moved,
        belongs=belongs,
        implausible=reads,
        inverter_kwh=inverter,
        estimates=estimates,
    )
    cache.pop(key, None)
    cache[key] = (mark, answer)
    while len(cache) > CACHE_SIZE:
        del cache[next(iter(cache))]
    return answer


@callback
def forget_cached_loads(hass: HomeAssistant, site_entry_id: str) -> None:
    """Drop a site's kept answers (its unload)."""
    cache: dict[tuple[Any, ...], Any] | None = hass.data.get(_CACHE)
    if cache:
        for key in [key for key in cache if key[0] == site_entry_id]:
            del cache[key]


async def async_loads_answer(hass: HomeAssistant, site: Any, request: Mapping[str, Any]) -> dict[str, Any]:
    """The answer to one request for one site (the WebSocket command and the webhook's `get_house_loads` alike): its
    `period`, `date` and `kind` judged, then `loads_answer`. Every refusal is a `PowerMonthRefusal`."""
    kind = request.get("kind", KIND_HOUSE)
    if kind not in (KIND_HOUSE, KIND_HEAT):
        raise PowerMonthRefusal(ERROR_INVALID_KIND, "The pie is house or heat")
    try:
        return await loads_answer(hass, site, request.get("period"), request.get("date"), kind)
    except PowerMonthRefusal:
        raise
    except Exception:  # noqa: BLE001 - no recorder, statistics that fail: the card says it could not be read
        _LOGGER.debug("SpotNav house loads: the meters could not be read", exc_info=True)
        raise PowerMonthRefusal(ERROR_READ_FAILED, "The meters could not be read") from None


@websocket_api.websocket_command(
    {
        vol.Required("type"): "spotnav/get_house_loads",
        vol.Optional("api_version"): object,
        vol.Optional("charger_id"): object,
        vol.Optional("site_id"): object,
        vol.Optional("period"): object,
        vol.Optional("date"): object,
        vol.Optional("kind"): object,
    }
)
@websocket_api.async_response
async def websocket_get_house_loads(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """The house's known meters over one period of a site."""
    version = msg.get("api_version")
    if type(version) is not int or version != HOUSE_LOADS_API_VERSION:
        send_unsupported_version(connection, msg, HOUSE_LOADS_API_VERSION)
        return
    try:
        answer = await async_loads_answer(hass, _site_of_request(hass, msg), msg)
    except PowerMonthRefusal as refusal:
        connection.send_error(msg["id"], refusal.code, refusal.message)
        return
    connection.send_result(msg["id"], answer)


@callback
def async_setup_house_loads_api(hass: HomeAssistant) -> None:
    """Register the command once for the domain."""
    websocket_api.async_register_command(hass, websocket_get_house_loads)
