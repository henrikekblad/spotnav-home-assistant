"""`spotnav/get_heat`'s answer (API v1), built from what the shell (`api/heat.py`) read. Pure. Shadow only.

    {api_version: 1, site_id, now, time_zone, writable, shadow: true,
     config: the settings (`api/heat.config_block`),
     comfort: {target_c, low_c, high_c, mode, away_until, aim_c},
     indoor: {value_c, coldest, coldest_name, sensors: [{entity_id, name, value_c, rise_c, status, offset_c}],
              relative_c: the coldest room's reading less its offset, what the comfort band is compared with},
     outdoor: {value_c, entity_id, name, source} | null,
     zones: [{entity_id, name, kind: "floor" | "air", offset_c, aim_c, current_c, setpoint_c, hvac_mode, heating,
              power_w, available}],
     pump: {integration, brand, label, priority, compressor_running, supply_c, hot_water_c, degree_minutes,
            immersion_on, price_control, control, capabilities: [kind], disabled: [entity ids], device_id} | null,
     meter: {status, entity_id, name, source, setting_offered, candidates, kw_now, state, baseline_kw} | null,
     today: {date, total_kwh, heat_kwh, hot_water_kwh, runs: [{start, end, kwh}], baseline_kw, cost, money} | null,
     status: {pump_state, pump_state_source, kw, would: {action, target_c, until, reason} | null, guard},
     band: {date, hours: [{start, action, target_c, reason, price, past, hot_water, car, relay_min, relay_expected}],
            hot_water_pick} | null,
     notes: [{code, params}],
     choices: {indoor, outdoor, zones, relay, offset, setpoint, hot_water, meter} | null (administrators only),
     relay: {entity_id, name, closed, since, source, automation: {entity_id, name} | null,
             effect: {lower_share, rebound, blocks}} | null,
     review: yesterday's review (`heat/review.py`) | null}

The band is SpotNav's own plan (`heat/shadow_rule.band`, joint with the cars, the battery and the import cap: the
`tight` hours); an hour already begun shows what was planned when it began (`kept`). Beside it each hour says how many
minutes the heat pump's relay was closed (`relay_min`, `null` without a relay in the settings), or is expected to be
(`relay_expected`: the SpotNav blueprint's closings ahead, or a closing now running its course): what really steers
the heat while SpotNav only watches.

`notes` are only what the household can act on, one each (plan section 15): `pump_price_control` (the pump's own
price control is on; `{label, where}`), `readings_disabled` (readings SpotNav needs are turned off in the integration;
`{entities, device_id}`), `other_controller` (another optimiser steers the pump; `{names}`), `automation_steers` (an
enabled automation of the household's references the relay or a zone, `heat/automations.py`; `{entity_id, name,
targets: [names], relay}`, `relay` when the relay is among them: one note per automation). A thing the household
cannot change (no writable control path) is no note: `pump.capabilities` says it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Final

from .. import time_zones
from .config import BAND_C, MODE_AWAY, HeatConfig
from .facts import MISSING_DISABLED, HeatPumpFacts
from .meter_split import DaySplit, Levels, state_now
from .rooms import Indoor
from .automations import SteeringAutomation
from .relay_effect import Effect
from .shadow_rule import ACTION_LOWER, ACTION_NORMAL, ACTION_PREHEAT, BandHour, band, day_hours, would_do

HEAT_API_VERSION: Final = 1
HEAT_KEYS: Final = (
    "api_version", "site_id", "now", "time_zone", "writable", "shadow", "config", "comfort", "indoor", "outdoor",
    "zones", "pump", "meter", "today", "status", "band", "notes", "choices", "relay", "review",
)
BAND_HOUR_KEYS: Final = (
    "start", "action", "target_c", "reason", "price", "past", "hot_water", "car", "relay_min", "relay_expected",
)

KIND_FLOOR: Final = "floor"
KIND_AIR: Final = "air"

#: Where each brand's own price control is turned off, as a stable word the card turns into its sentence.
PRICE_CONTROL_WHERE: Final = {"nibe": "nibe_smart_price", "ctc": "ctc_smartgrid"}


@dataclass(frozen=True, slots=True)
class ZoneReading:
    entity_id: str
    name: str
    kind: str
    offset_c: float
    current_c: float | None = None
    setpoint_c: float | None = None
    hvac_mode: str | None = None
    heating: bool | None = None
    power_w: float | None = None
    available: bool = True


@dataclass(frozen=True, slots=True)
class Outdoor:
    value_c: float | None
    entity_id: str | None
    name: str | None
    #: `setting` (chosen in the heat settings), `house_forecast` (the house forecast's own), `pump` (the heat pump's
    #: outdoor sensor).
    source: str


@dataclass(frozen=True, slots=True)
class MeterInputs:
    status: str
    entity_id: str | None
    name: str | None
    source: str | None
    setting_offered: bool
    candidates: tuple[str, ...]
    kw_now: float | None
    last_period_kw: float | None
    levels: Levels | None
    split: DaySplit | None
    #: Today's 5-minute energy (kWh per period start), for the cost.
    periods: Mapping[datetime, float] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RelayInputs:
    """The heat pump's relay as the heat shadow sees it (`heat/runtime.py`)."""

    entity_id: str
    name: str
    closed: bool | None
    since: datetime | None
    #: `blueprint` (the SpotNav blueprint drives it) or `unknown`.
    source: str | None
    automation: tuple[str, str] | None
    #: Per hour of the day: the minutes closed (or expected to be) and whether expected.
    minutes: Mapping[datetime, tuple[int, bool]]
    effect: Effect


@dataclass(frozen=True, slots=True)
class HeatViewInputs:
    site_id: str
    now: datetime
    tz: str
    writable: bool
    config: HeatConfig
    config_block: Mapping[str, Any]
    indoor: Indoor
    outdoor: Outdoor | None = None
    zones: tuple[ZoneReading, ...] = ()
    pump: HeatPumpFacts | None = None
    other_controllers: tuple[str, ...] = ()
    meter: MeterInputs | None = None
    #: Buy price with every fee per period start (minor unit per kWh) and the area's money words.
    prices: Sequence[tuple[datetime, datetime, float]] = ()
    money: tuple[str, str, str] | None = None
    #: The hours a car charged (measured) or is planned to charge (UTC hour starts).
    car_hours: frozenset[datetime] = frozenset()
    choices: Mapping[str, Any] | None = None
    #: The joint plan's hours at the import cap and the outdoor forecast (°C), both by UTC hour start.
    tight: frozenset[datetime] = frozenset()
    outdoor_by_hour: Mapping[datetime, float] = field(default_factory=dict)
    #: Today's hours as planned when each began: `{hour start: (action, reason)}`.
    kept: Mapping[datetime, tuple[str, str]] = field(default_factory=dict)
    relay: RelayInputs | None = None
    automations: Sequence[SteeringAutomation] = ()
    #: Names of the entities the automations reference.
    names: Mapping[str, str] = field(default_factory=dict)
    review: Mapping[str, Any] | None = None


def _hour_prices(prices: Sequence[tuple[datetime, datetime, float]]) -> dict[datetime, float]:
    """The mean buy price per clock hour (UTC hour starts) of the slots that cover it."""
    sums: dict[datetime, list[float]] = {}
    for start, end, price in prices:
        moment = start
        while moment < end:
            hour = moment.replace(minute=0, second=0, microsecond=0)
            sums.setdefault(hour, []).append(price)
            moment += timedelta(minutes=15)
    return {hour: sum(values) / len(values) for hour, values in sums.items()}


hour_prices_of = _hour_prices


def _price_at(prices: Sequence[tuple[datetime, datetime, float]], moment: datetime) -> float | None:
    return next((price for start, end, price in prices if start <= moment < end), None)


def _cost(periods: Mapping[datetime, float], prices: Sequence[tuple[datetime, datetime, float]]) -> float | None:
    """The periods' energy at the buy price of each (minor unit), `None` when a period has no price."""
    if not periods:
        return 0.0
    total = 0.0
    for start, kwh in periods.items():
        price = _price_at(prices, start)
        if price is None:
            return None
        total += kwh * price
    return total


def _pump(facts: HeatPumpFacts) -> dict[str, Any]:
    return {
        "integration": facts.integration,
        "brand": facts.brand,
        "label": facts.label,
        "priority": facts.priority,
        "compressor_running": facts.compressor_running,
        "supply_c": facts.supply_temp_c,
        "hot_water_c": facts.hot_water_temp_c,
        "degree_minutes": facts.degree_minutes,
        "immersion_on": facts.immersion_on,
        "price_control": facts.price_control.state,
        "control": facts.control,
        "capabilities": sorted({c.kind for c in facts.capabilities if c.enabled and c.available}),
        "disabled": sorted(role for role, why in facts.missing.items() if why == MISSING_DISABLED),
        "device_id": facts.device_id,
    }


def notes(
    pump: HeatPumpFacts | None,
    other_controllers: Sequence[str],
    automations: Sequence[SteeringAutomation] = (),
    relay: str | None = None,
    names: Mapping[str, str] | None = None,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if pump is not None and pump.price_control.state == "active":
        out.append({"code": "pump_price_control", "params": {"label": pump.label, "where": PRICE_CONTROL_WHERE.get(pump.brand, "")}})
    if pump is not None:
        disabled = sorted(role for role, why in pump.missing.items() if why == MISSING_DISABLED)
        if disabled:
            out.append({"code": "readings_disabled", "params": {"entities": disabled, "device_id": pump.device_id}})
    if other_controllers:
        out.append({"code": "other_controller", "params": {"names": list(other_controllers)}})
    for automation in automations:
        targets = [target for target in automation.targets if target != relay]
        out.append(
            {
                "code": "automation_steers",
                "params": {
                    "entity_id": automation.entity_id,
                    "name": automation.name,
                    "targets": [(names or {}).get(target, target) for target in targets],
                    "relay": relay is not None and relay in automation.targets,
                },
            }
        )
    return out


def _kept_hour(hour: BandHour, kept: tuple[str, str], target: float) -> BandHour:
    """An hour already begun as it was planned then (its aim from its action, as the rule sets it)."""
    action, reason = kept
    aim = {ACTION_LOWER: target - BAND_C, ACTION_PREHEAT: target + BAND_C}.get(action, hour.target_c)
    return BandHour(hour.start, action, aim, reason, hour.price)


def _round(value: float | None, digits: int = 2) -> float | None:
    return None if value is None else round(value, digits)


def heat_view(inputs: HeatViewInputs) -> dict[str, Any]:
    """The answer (see the module)."""
    zone_info = time_zones.require_zone(inputs.tz)
    config = inputs.config
    now = inputs.now
    hours = day_hours(now, zone_info)
    hour_prices = _hour_prices(inputs.prices)
    outdoor_c = None if inputs.outdoor is None else inputs.outdoor.value_c
    band_hours, guard = band(
        config,
        hours,
        hour_prices,
        now,
        zone_info,
        outdoor_c=outdoor_c,
        coldest_c=inputs.indoor.relative_c,
        tight=inputs.tight,
        outdoor_by_hour=inputs.outdoor_by_hour,
    )
    current = now.replace(minute=0, second=0, microsecond=0)
    band_hours = [
        _kept_hour(hour, inputs.kept[hour.start], config.target_c) if hour.start < current and hour.start in inputs.kept else hour
        for hour in band_hours
    ]
    would = would_do(band_hours, now, config, zone_info)
    relay = inputs.relay

    meter = inputs.meter
    split = None if meter is None else meter.split
    runs = () if split is None else split.runs
    run_hours = {run.start.replace(minute=0) for run in runs}
    hot_water_pick: datetime | None = None
    if config.hot_water_entity is not None:
        ahead = [hour for hour in band_hours if hour.start >= current and hour.price is not None]
        if ahead:
            hot_water_pick = min(ahead, key=lambda hour: (hour.price, hour.start)).start

    band_block = {
        "date": now.astimezone(zone_info).date().isoformat(),
        "hours": [
            {
                **hour.as_dict(),
                "past": hour.start < current,
                "hot_water": hour.start in run_hours or hour.start == hot_water_pick,
                "car": hour.start in inputs.car_hours,
                "relay_min": None if relay is None else relay.minutes.get(hour.start, (0, False))[0],
                "relay_expected": relay is not None and relay.minutes.get(hour.start, (0, False))[1],
            }
            for hour in band_hours
        ],
        "hot_water_pick": None if hot_water_pick is None else hot_water_pick.isoformat(),
    }

    pump_state = pump_source = None
    if inputs.pump is not None and inputs.pump.priority is not None:
        priority = inputs.pump.priority
        running = inputs.pump.compressor_running
        pump_state = "idle" if running is False or priority == "off" else ("hot_water" if priority == "hot_water" else "heating")
        pump_source = "pump"
    elif meter is not None:
        pump_state = state_now(meter.kw_now, meter.levels, meter.last_period_kw)
        pump_source = None if pump_state is None else "meter"

    today = None
    if meter is not None and split is not None:
        cost = _cost(meter.periods, inputs.prices) if inputs.money is not None else None
        today = {
            "date": band_block["date"],
            **split.as_dict(),
            "cost": None if cost is None else round(cost, 2),
            "money": None if inputs.money is None else {"currency": inputs.money[0], "major": inputs.money[1], "minor": inputs.money[2]},
        }

    aim = config.target_c if would is None else would.target_c
    return {
        "api_version": HEAT_API_VERSION,
        "site_id": inputs.site_id,
        "now": now.isoformat(),
        "time_zone": inputs.tz,
        "writable": inputs.writable,
        "shadow": True,
        "config": dict(inputs.config_block),
        "comfort": {
            "target_c": config.target_c,
            "low_c": config.target_c - BAND_C,
            "high_c": config.target_c + BAND_C,
            "mode": config.mode,
            "away_until": config.away_until if config.mode == MODE_AWAY else None,
            "aim_c": aim,
        },
        "indoor": inputs.indoor.as_dict(),
        "outdoor": None
        if inputs.outdoor is None
        else {
            "value_c": _round(inputs.outdoor.value_c),
            "entity_id": inputs.outdoor.entity_id,
            "name": inputs.outdoor.name,
            "source": inputs.outdoor.source,
        },
        "zones": [
            {
                "entity_id": zone.entity_id,
                "name": zone.name,
                "kind": zone.kind,
                "offset_c": zone.offset_c,
                "aim_c": aim + zone.offset_c,
                "current_c": _round(zone.current_c),
                "setpoint_c": _round(zone.setpoint_c),
                "hvac_mode": zone.hvac_mode,
                "heating": zone.heating,
                "power_w": _round(zone.power_w, 0),
                "available": zone.available,
            }
            for zone in inputs.zones
        ],
        "pump": None if inputs.pump is None else _pump(inputs.pump),
        "meter": None
        if meter is None
        else {
            "status": meter.status,
            "entity_id": meter.entity_id,
            "name": meter.name,
            "source": meter.source,
            "setting_offered": meter.setting_offered,
            "candidates": list(meter.candidates),
            "kw_now": _round(meter.kw_now, 3),
            "state": None if meter.levels is None else state_now(meter.kw_now, meter.levels, meter.last_period_kw),
            "baseline_kw": None if meter.levels is None else round(meter.levels.baseline_kw, 3),
        },
        "today": today,
        "status": {
            "pump_state": pump_state,
            "pump_state_source": pump_source,
            "kw": None if meter is None else _round(meter.kw_now, 3),
            "would": None if would is None or (would.action == ACTION_NORMAL and would.until is None and guard is None) else would.as_dict(),
            "guard": guard,
        },
        "band": band_block,
        "notes": notes(
            inputs.pump, inputs.other_controllers, inputs.automations, None if relay is None else relay.entity_id, inputs.names
        ),
        "choices": None if not inputs.writable or inputs.choices is None else dict(inputs.choices),
        "relay": None
        if relay is None
        else {
            "entity_id": relay.entity_id,
            "name": relay.name,
            "closed": relay.closed,
            "since": None if relay.since is None else relay.since.isoformat(),
            "source": relay.source,
            "automation": None if relay.automation is None else {"entity_id": relay.automation[0], "name": relay.automation[1]},
            "effect": relay.effect.as_dict(),
        },
        "review": None if inputs.review is None else dict(inputs.review),
    }

