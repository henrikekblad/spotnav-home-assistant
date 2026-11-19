"""`spotnav/get_heat`'s answer (API v1), built from what the shell (`api/heat.py`) read. Pure.

    {api_version: 1, site_id, now, time_zone, writable, shadow: false only while SpotNav's heat control runs,
     config: the settings (`api/heat.config_block`),
     comfort: {target_c, low_c, high_c, mode, away_until, aim_c},
     indoor: {value_c, coldest, coldest_name, sensors: [{entity_id, name, value_c, rise_c, status, offset_c}],
              relative_c: the coldest room's reading less its offset, what the comfort band is compared with},
     outdoor: {value_c, entity_id, name, source} | null,
     zones: [{entity_id, name, kind: "floor" | "air", offset_c, aim_c, current_c, setpoint_c, hvac_mode, heating,
              power_w, available}],
     pump: {integration, brand, label, priority, compressor_running, supply_c, hot_water_c, degree_minutes,
            immersion_on, price_control, control, capabilities: [kind], disabled: [entity ids], device_id,
            paths: [{path, control_entity, control_entity_b}]} | null,
     meter: {status, entity_id, name, source, setting_offered, candidates, kw_now, state, baseline_kw} | null,
     today: {date, total_kwh, heat_kwh, hot_water_kwh, zones_kwh, runs: [{start, end, kwh}], baseline_kw, cost,
             money} | null,
     status: {pump_state, pump_state_source, kw, would: {action, target_c, until, reason} | null, guard},
     band: {date, hours: [{start, action, target_c, reason, price, past, hot_water, car, relay_min, relay_expected,
                           executed_min, raised_min}], hot_water_pick} | null,
     indoor_day: the day's measured indoor curve up to now (`heat/indoor_day.py`, 15-minute steps, `setpoint` null:
                 the band's `target_c` is the setpoint) | null,
     notes: [{code, params}],
     choices: {indoor, outdoor, zones, relay, offset, setpoint, hot_water, meter} | null (administrators only),
     relay: {entity_id, name, closed, since, source, automation: {entity_id, name} | null,
             effect: {lower_share, rebound, blocks}} | null,
     review: yesterday's review (`heat/review.py`) | null,
     control: {enabled, state: "off" | "verifying" | "running" | "waiting", reason, automation: {entity_id, name} | null,
               check: "passed" | "failed" | null, lowered, since, until, paused_until, raised, raise_until,
               path: the control path steered | null, untested: bool,
               enable_entities: {integration, entities: [{entity_id, name}]} | null,
               offset_home: {entity_id, value, home} | null} | null,
     ahead: [{start, action, target_c, reason, hot_water}],
     hot_water: {boost: {state, source, started, ends, ends_estimated, stoppable} | null,
                 peaks: [{ready_by: "HH:MM", kwh, learned}],
                 planned: [{start, end, kwh, price, reason: "cheap" | "sun", peak: "HH:MM", live, pump_run}],
                 runs_today: [{start, end, kwh, source: "pump" | "plan" | "button"}],
                 review: {date, mode, would_cost, actual_cost, saved, moved_kwh, hits, peaks, misses, cold_risks,
                          blocks_cut, a_mode, per_peak: [{peak, hit, pick, pump_run, miss, cold_risk}]} | null,
                 gate: {level, days, days_needed, weekdays, peaks, peaks_needed, hits, hit_needed, saved, open,
                        backed_off},
                 level: "A" | "B" | null, mode: "shadow" | "live" | "watch",
                 blocking: {mode: "shadow" | "live" | "watch", gate: {as gate}, tank: bool, strict: bool,
                            cuts: [{start, end, reason: "peak" | "day_cap" | "hygiene" | "moved", peak: "HH:MM" | null,
                                   live}]} | null}

`hot_water` is the site's hot water (`heat/dhw_runtime.py`, plan `plans/v3_dhw_plan.md`): the Boost button's state
(`null` where the pump has no boost that ends by itself), the peaks (`learned` false: the defaults), the boosts
planned for today's and tomorrow's peaks (`live`: written; else what SpotNav would do; `pump_run` the pump's own
run the boost would have replaced, once seen), today's measured runs and who started them, the last day's review
and the gate to live. Money in the minor unit, as `today.cost`. `blocking` is level A where the path blocks hot water
too (`heat/dhw_level_a.py`): the heat plan's hours its rules take out (`cuts`: before the peak at `peak`, over the day's
cap, the pump's periodic increase, or moved where the plan chose again around a kept hour; `live` when the plan
steered by has them out, else what it would do), its own mode and gate, and whether a
tank sensor reads (`tank`: the tank rule then stands in for the day's cap), and whether its stricter rules hold
after a back-off (`strict`). The review's `cold_risks` are the peaks a
block held the pump's run into, `blocks_cut` the hours level A took out that day, `a_mode` level A's mode that day. The band's `hot_water_pick` is the first planned
boost ahead (an alias kept one release), and an hour with a planned boost is marked `hot_water` as a measured run's.

`ahead` is the plan from the running hour on, `AHEAD_HOURS` of them at most (the card's strip under the dial): the
band's own hours to midnight, then tomorrow's as the same rule plans them once every hour of tomorrow has its price
(without them the list ends at midnight: nothing is guessed).

`control` is SpotNav's own steering of the pump (`heat/control.py`, `HeatControl.status`): `reason` is the check's
phase while verifying, the rules' reason while running (`heat/relay_control.py`), what it waits for while waiting.
`path` is the settings' control path once it has what it needs to steer (`heat/paths.py`), `untested` that the path
has not run on a real heat pump yet (the card says so), `enable_entities` the path's entities that are disabled in
their integration: the card lists them when "Styr värmen" is turned on and the write's `enable_control_entities`
enables them (`heat/optin.py`). `offset_home` is a stored offset that reads other than the household's own value
while SpotNav holds nothing (`HeatControl._track_home`): the card asks to put it back or keep it (the write's
`offset_home`). `pump.paths` are the control paths the pump's own entities offer
(`paths.detected_paths`): the card fills a chosen path's entities in from them.
Each band hour's `executed_min` is the minutes SpotNav itself held the relay closed (`null` without the control), its
`raised_min` the minutes SpotNav held SG Ready's second contact (B, the raise) closed: `null` where the pump cannot be
raised (the relay, or SG Ready without its second contact), so the card draws the plan's pre-heat hours as raises
only there.

The band is SpotNav's own plan (`heat/shadow_rule.band`, joint with the cars, the battery and the import cap: the
`tight` hours); an hour already begun shows what was planned when it began (`kept`). Beside it each hour says how many
minutes the heat pump's relay was closed (`relay_min`, `null` without a relay in the settings), or is expected to be
(`relay_expected`: the SpotNav blueprint's closings ahead, or a closing now running its course): what really steers
the heat while SpotNav only watches.

`notes` are only what the household can act on, one each (plan section 15): `pump_price_control` (the pump's own
price control is on; `{label, where}`), `readings_disabled` (readings SpotNav needs are turned off in the integration;
`{entities, device_id}`), `other_controller` (another optimiser steers the pump; `{names}`), `automation_steers` (an
enabled automation of the household's references the relay or a zone, `heat/automations.py`; `{entity_id, name,
targets: [names], relay, blocks}`, `relay` when the relay is among them, `blocks` when that keeps SpotNav's heat
control from starting: one note per automation). A thing the household
cannot change (no writable control path) is no note: `pump.capabilities` says it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Final

from .. import time_zones
from .config import BAND_C, MODE_AWAY, HeatConfig
from .facts import MISSING_DISABLED, HeatPumpFacts
from .meter_split import DaySplit, Levels, state_now
from .paths import detected_paths
from .rooms import Indoor
from .automations import SteeringAutomation
from .relay_effect import Effect
from .shadow_rule import ACTION_LOWER, ACTION_NORMAL, ACTION_PREHEAT, BandHour, band, day_hours, would_do

HEAT_API_VERSION: Final = 1
HEAT_KEYS: Final = (
    "api_version", "site_id", "now", "time_zone", "writable", "shadow", "config", "comfort", "indoor", "outdoor",
    "zones", "pump", "meter", "today", "status", "band", "indoor_day", "notes", "choices", "relay", "review", "control",
    "ahead", "hot_water",
)
AHEAD_HOUR_KEYS: Final = ("start", "action", "target_c", "reason", "hot_water")
#: The hours the card's "now" strip shows from the running one on.
AHEAD_HOURS: Final = 12
BAND_HOUR_KEYS: Final = (
    "start", "action", "target_c", "reason", "price", "past", "hot_water", "car", "relay_min", "relay_expected",
    "executed_min", "raised_min",
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
    #: The heat zones' own energy today (`site/heat_zones.py`, kWh per 5-minute period start, together); `None`
    #: without a zone counted.
    zone_periods: Mapping[datetime, float] | None = None


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
    #: The heat control's status (`HeatControl.status`) and the minutes it held the relay closed per hour.
    control: Mapping[str, Any] | None = None
    executed: Mapping[datetime, int] = field(default_factory=dict)
    #: The minutes SpotNav held SG Ready's B (the raise) closed per hour; `None` where the pump cannot be raised.
    raised: Mapping[datetime, int] | None = None
    #: The day's indoor curve (`heat/indoor_day.indoor_day`), `None` without indoor sensors.
    indoor_day: Mapping[str, Any] | None = None
    #: Whether the house is too warm now (the heat shadow's `RoomWarm`): the band lowers the running hours for it.
    room_warm: bool = False
    #: The site's hot water (`heat/dhw_runtime.HotWater.view`), `None` without the heat shadow.
    hot_water: Mapping[str, Any] | None = None


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
        "paths": detected_paths(facts),
    }


def notes(
    pump: HeatPumpFacts | None,
    other_controllers: Sequence[str],
    automations: Sequence[SteeringAutomation] = (),
    relay: str | None = None,
    names: Mapping[str, str] | None = None,
    control_enabled: bool = False,
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
                    "blocks": control_enabled and relay is not None and relay in automation.targets,
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
        room_warm=inputs.room_warm,
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
    # The planned boosts (`hot_water.planned`): the first ahead is the band's `hot_water_pick` (kept one release as
    # the alias it was), each marks its hour.
    hot_water_pick: datetime | None = None
    planned_hours: set[datetime] = set()
    for item in (inputs.hot_water or {}).get("planned") or ():
        start = datetime.fromisoformat(item["start"])
        planned_hours.add(start.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0))
        if hot_water_pick is None and start >= current:
            hot_water_pick = start

    band_block = {
        "date": now.astimezone(zone_info).date().isoformat(),
        "hours": [
            {
                **hour.as_dict(),
                "past": hour.start < current,
                "hot_water": hour.start in run_hours or hour.start in planned_hours,
                "car": hour.start in inputs.car_hours,
                "relay_min": None if relay is None else relay.minutes.get(hour.start, (0, False))[0],
                "relay_expected": relay is not None and relay.minutes.get(hour.start, (0, False))[1],
                "executed_min": None if inputs.control is None else inputs.executed.get(hour.start, 0),
                "raised_min": None if inputs.raised is None else inputs.raised.get(hour.start, 0),
            }
            for hour in band_hours
        ],
        "hot_water_pick": None if hot_water_pick is None else hot_water_pick.isoformat(),
    }

    ahead = [
        {**{key: item for key, item in hour.as_dict().items() if key != "price"}, "hot_water": hour.start in run_hours or hour.start in planned_hours}
        for hour in band_hours
        if hour.start >= current
    ][:AHEAD_HOURS]
    if len(ahead) < AHEAD_HOURS and hours:
        tomorrow = day_hours(hours[-1] + timedelta(hours=1), zone_info)
        if all(hour in hour_prices for hour in tomorrow):
            following, _guard = band(
                config,
                tomorrow,
                hour_prices,
                now,
                zone_info,
                outdoor_c=outdoor_c,
                tight=inputs.tight,
                outdoor_by_hour=inputs.outdoor_by_hour,
            )
            ahead += [
                {**{key: item for key, item in hour.as_dict().items() if key != "price"}, "hot_water": hour.start in planned_hours}
                for hour in following[: AHEAD_HOURS - len(ahead)]
            ]

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
        # Heat is the heat pump's and the zones' (electric floor heating, an air-to-air pump); hot water and its runs
        # stay the heat pump meter's own split.
        zones = meter.zone_periods
        zones_kwh = None if zones is None else sum(zones.values())
        spent: dict[datetime, float] = dict(meter.periods)
        for start, kwh in (zones or {}).items():
            spent[start] = spent.get(start, 0.0) + kwh
        cost = _cost(spent, inputs.prices) if inputs.money is not None else None
        figures = split.as_dict()
        if zones_kwh is not None:
            figures["total_kwh"] = round(split.total_kwh + zones_kwh, 3)
            figures["heat_kwh"] = round(split.heat_kwh + zones_kwh, 3)
        today = {
            "date": band_block["date"],
            **figures,
            "zones_kwh": None if zones_kwh is None else round(zones_kwh, 3),
            "cost": None if cost is None else round(cost, 2),
            "money": None if inputs.money is None else {"currency": inputs.money[0], "major_unit": inputs.money[1], "minor_unit": inputs.money[2]},
        }

    aim = config.target_c if would is None else would.target_c
    return {
        "api_version": HEAT_API_VERSION,
        "site_id": inputs.site_id,
        "now": now.isoformat(),
        "time_zone": inputs.tz,
        "writable": inputs.writable,
        "shadow": inputs.control is None or inputs.control.get("state") != "running",
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
        "indoor_day": None if inputs.indoor_day is None else dict(inputs.indoor_day),
        "notes": notes(
            inputs.pump,
            inputs.other_controllers,
            inputs.automations,
            None if relay is None else relay.entity_id,
            inputs.names,
            control_enabled=inputs.control is not None and inputs.control.get("enabled") is True,
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
        "control": None if inputs.control is None else dict(inputs.control),
        "ahead": ahead,
        "hot_water": None if inputs.hot_water is None else dict(inputs.hot_water),
    }

