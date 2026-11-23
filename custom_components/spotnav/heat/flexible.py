"""The site's heat loads in the common shape (`planning/flexible_load.py`, plan `plans/v3_flexible_loads_design.md`
step 1): the heat pump's control path, each floor-heating zone (a zone in cool mode as house cooling, spec
`plans/v3_pool_spa_spec.md` section 4) and the plain water heater. Descriptions only: the
numbers are the rules' own constants, the state what the heat shadow and its controls already hold. Nothing is read
from Home Assistant here beyond the site's settings and the zones' modes, and nothing is written.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any

from ..planning.flexible_load import (
    CHOICE_ACCEPT_PEAK,
    CHOICE_LESS_COMFORT,
    CHOICE_PAUSE_NOW,
    GIVE_COMFORT_DEG_H,
    GIVE_HOURS_PAUSED,
    GOAL_COMFORT_BAND,
    GOAL_TEMPERATURE_BY,
    HANDLE_ON_OFF,
    HANDLE_SETPOINT_OFFSET,
    HANDLE_STEPS,
    KIND_HEAT,
    KIND_HEAT_ZONE,
    KIND_WATER_HEATER,
    Choice,
    FlexibleLoad,
    Give,
    Goal,
    Handle,
    HardRule,
    rule,
    values,
)
from ..pool.rules import COOL_CEILING_MAX_C, COOL_RAISE_C, LOAD_COOLING
from . import paths
from .config import BAND_C, PATH_OFFSET, HeatConfig, away_target
from .relay_control import MAX_DAY, MAX_RUN, MAX_WRITES_DAY, MIN_LOWER, MIN_NORMAL, NO_LOWER_AT_C
from .shadow_rule import MAX_LOWERED_DAY, MAX_LOWERED_RUN, ROOM_COLD_C, ROOM_COLD_HOURS
from .water_heater import (
    FULL_EVERY,
    MAX_OFF,
    MAX_PLANNED_OFF,
    MAX_SWITCHINGS_DAY,
    MODE_TANK,
    MODE_UNMETERED,
    NIGHT_HOURS,
    SENSOR_LOST_AFTER,
    TANK_FULL_C,
    TANK_LOW_C,
    mode_of,
)
from .zone_control import COMFORT_C, DEFAULT_STEP_C, LOWER_C, RAISE_C
from .zone_control import MAX_WRITES_DAY as ZONE_WRITES_DAY

#: What a step of the heat pump's contacts means: lowered, the pump's own curve, raised.
STEP_LOWER = -1.0
STEP_NORMAL = 0.0
STEP_RAISE = 1.0


def _hours(span: timedelta) -> float:
    return span.total_seconds() / 3600


def _comfort_choices(power_tariff: bool) -> tuple[Choice, ...]:
    """Heat's choices: a degree less for those hours, and the peak where a power tariff makes one cost."""
    found = [Choice(CHOICE_LESS_COMFORT, "heat_less_comfort")]
    if power_tariff:
        found.append(Choice(CHOICE_ACCEPT_PEAK, "heat_accept_peak"))
    return tuple(found)


def _lowering_rules(*, writes_day: int) -> tuple[HardRule, ...]:
    """The run rules every lowering keeps (`relay_control`), the heat pump's and each zone's alike."""
    return (
        rule("min_lowered", minutes=_hours(MIN_LOWER) * 60),
        rule("max_lowered_run", hours=_hours(MAX_RUN)),
        rule("min_normal_between", hours=_hours(MIN_NORMAL)),
        rule("max_lowered_day", hours=_hours(MAX_DAY)),
        rule("no_lowering_when_cold", outdoor_at_or_below_c=NO_LOWER_AT_C),
        rule("write_cap", writes_per_day=writes_day),
    )


def heat_pump_load(site_id: str, config: HeatConfig, *, power_tariff: bool) -> FlexibleLoad | None:
    """The heat pump's control path, `None` where the settings name nothing SpotNav could steer."""
    lower = paths.lower_contact(config)
    if lower is None or config.control_entity is None:
        return None
    raises = paths.raise_contact(config) is not None
    if config.control_path == PATH_OFFSET:
        handle = Handle(
            HANDLE_SETPOINT_OFFSET,
            unit="step",
            low=-paths.MAX_SHIFT,
            high=paths.MAX_SHIFT if raises else 0.0,
            values=values(path=config.control_path),
        )
    else:
        handle = Handle(
            HANDLE_STEPS,
            unit="level",
            steps=(STEP_LOWER, STEP_NORMAL, STEP_RAISE) if raises else (STEP_LOWER, STEP_NORMAL),
            values=values(path=config.control_path),
        )
    away = config.away_end()
    return FlexibleLoad(
        id=config.control_entity,
        kind=KIND_HEAT,
        site=site_id,
        controlled=config.control_enabled,
        goal=_band(config.target_c, config, away),
        handles=(handle,),
        hard_rules=(
            # The comfort floor is the cold-room release: no lowering while the coldest room is under it.
            rule("comfort_floor", below_c=round(config.target_c - ROOM_COLD_C, 2), release_hours=ROOM_COLD_HOURS),
            rule("plan_lowered_run", hours=MAX_LOWERED_RUN),
            rule("plan_lowered_day", hours=MAX_LOWERED_DAY),
            *_lowering_rules(writes_day=MAX_WRITES_DAY),
        ),
        give=Give(GIVE_COMFORT_DEG_H),
        choices=_comfort_choices(power_tariff),
    )


def _band(target_c: float, config: HeatConfig, away: datetime | None) -> Goal:
    """The comfort band around a target, with the household's own night and away."""
    return Goal(
        GOAL_COMFORT_BAND,
        by=away,
        values=values(
            target_c=target_c,
            low_c=round(target_c - BAND_C, 2),
            high_c=round(target_c + BAND_C, 2),
            mode=config.mode,
            away_c=away_target(target_c, config.away_delta_c) if away is not None else None,
            night_c=round(target_c - config.night.delta_c, 2) if config.night.on else None,
            night_start=config.night.start if config.night.on else None,
            night_end=config.night.end if config.night.on else None,
        ),
    )


def cooling_zone_load(site_id: str, entity_id: str, home: float | None, *, controlled: bool, power_tariff: bool) -> FlexibleLoad:
    """A zone in cool mode (an air-to-air unit, a heat pump cooling): house cooling (spec section 4). Its shed step is
    a raise of `COOL_RAISE_C` over the person's setpoint, within the comfort ceiling (never above
    `COOL_CEILING_MAX_C`), through the zone's own run rules and write cap; its on/off and its mode are never written."""
    ceiling = None if home is None else round(min(home + COOL_RAISE_C, max(home, COOL_CEILING_MAX_C)), 2)
    return FlexibleLoad(
        id=entity_id,
        kind=KIND_HEAT_ZONE,
        site=site_id,
        controlled=controlled,
        goal=Goal(GOAL_COMFORT_BAND, values=values(target_c=home, high_c=ceiling, mode="cool", load=LOAD_COOLING)),
        handles=(Handle(HANDLE_SETPOINT_OFFSET, unit="°C", low=0.0, high=COOL_RAISE_C, values=values(step_c=DEFAULT_STEP_C)),),
        hard_rules=(rule("comfort_ceiling", above_c=ceiling), *_lowering_rules(writes_day=ZONE_WRITES_DAY)),
        give=Give(GIVE_COMFORT_DEG_H),
        choices=_comfort_choices(power_tariff),
    )


def zone_loads(
    site_id: str,
    config: HeatConfig,
    home_c: Mapping[str, float | None],
    *,
    power_tariff: bool,
    modes: Mapping[str, str | None] | None = None,
) -> list[FlexibleLoad]:
    """Each chosen floor-heating zone; `home_c` the person's own setpoint per zone where the zones' control has read
    one (the band is then around it, else it has no temperatures yet). A zone whose `modes` reads `cool` is house
    cooling (`cooling_zone_load`)."""
    found = []
    for zone in config.zones:
        home = home_c.get(zone.entity_id)
        if (modes or {}).get(zone.entity_id) == "cool":
            found.append(cooling_zone_load(site_id, zone.entity_id, home, controlled=zone.control, power_tariff=power_tariff))
            continue
        found.append(
            FlexibleLoad(
                id=zone.entity_id,
                kind=KIND_HEAT_ZONE,
                site=site_id,
                controlled=zone.control,
                goal=Goal(
                    GOAL_COMFORT_BAND,
                    values=values(
                        target_c=home,
                        low_c=None if home is None else round(home - COMFORT_C, 2),
                        high_c=None if home is None else round(home + COMFORT_C, 2),
                    ),
                ),
                handles=(
                    Handle(HANDLE_SETPOINT_OFFSET, unit="°C", low=-LOWER_C, high=RAISE_C, values=values(step_c=DEFAULT_STEP_C)),
                ),
                hard_rules=(
                    # A lowering is released at once under it, and none starts there.
                    rule("comfort_floor", below_c=None if home is None else round(home - COMFORT_C, 2), offset_c=-COMFORT_C),
                    *_lowering_rules(writes_day=ZONE_WRITES_DAY),
                ),
                give=Give(GIVE_COMFORT_DEG_H),
                choices=_comfort_choices(power_tariff),
            )
        )
    return found


def water_heater_load(
    site_id: str,
    config: HeatConfig,
    *,
    metered: bool,
    last_full: datetime | None,
    kw: float | None,
) -> FlexibleLoad | None:
    """The plain water heater, `None` without one. `metered`: its meter has been seen drawing the element's power;
    `last_full` the last full heat seen; `kw` the element's power where learnt."""
    heater = config.water_heater
    if heater is None:
        return None
    mode = mode_of(metered, heater.tank_sensor is not None)
    unmetered = mode == MODE_UNMETERED
    hard_rules = [
        rule("full_heat_every", hours=_hours(FULL_EVERY), mode=mode),
        rule("max_off", hours=_hours(MAX_OFF), planned_hours=_hours(MAX_PLANNED_OFF)),
        rule("fail_on", sensor_lost_minutes=_hours(SENSOR_LOST_AFTER) * 60),
        rule("write_cap", switchings_per_day=MAX_SWITCHINGS_DAY),
    ]
    if unmetered:
        hard_rules.insert(1, rule("night_window", hours=NIGHT_HOURS))
    if mode == MODE_TANK:
        hard_rules.insert(1, rule("tank_low", below_c=TANK_LOW_C))
    return FlexibleLoad(
        id=heater.entity_id,
        kind=KIND_WATER_HEATER,
        site=site_id,
        controlled=heater.control,
        goal=Goal(
            GOAL_TEMPERATURE_BY,
            # Unmetered there is no full heat to see: the night window is the rule, so nothing is due by a time.
            by=None if unmetered or last_full is None else last_full + FULL_EVERY,
            values=values(full=True, mode=mode, tank_full_c=TANK_FULL_C if mode == MODE_TANK else None),
        ),
        handles=(Handle(HANDLE_ON_OFF, unit="kW", high=kw),),
        hard_rules=tuple(hard_rules),
        give=Give(GIVE_HOURS_PAUSED),
        choices=(Choice(CHOICE_PAUSE_NOW, "water_heater_pause_now"),),
    )


def heat_loads(site: Any, *, power_tariff: bool) -> list[FlexibleLoad]:
    """The site's heat loads from what its heat shadow and controls hold now (see the module)."""
    config: HeatConfig = site.heat_shadow.config()
    found: list[FlexibleLoad] = []
    pump = heat_pump_load(site.entry_id, config, power_tariff=power_tariff)
    if pump is not None:
        found.append(pump)
    zones = getattr(site, "zones_control", None)
    home = {} if zones is None else {entity_id: state.home_c for entity_id, state in zones.zones.items()}
    states = getattr(getattr(site, "hass", None), "states", None)
    modes = {
        zone.entity_id: None if states is None or (state := states.get(zone.entity_id)) is None else state.state
        for zone in config.zones
    }
    found.extend(zone_loads(site.entry_id, config, home, power_tariff=power_tariff, modes=modes))
    heater = getattr(site, "water_heater_control", None)
    load = water_heater_load(
        site.entry_id,
        config,
        metered=bool(heater is not None and heater.metered),
        last_full=None if heater is None else heater.rules.kept.last_full,
        kw=None if heater is None else heater.kw,
    )
    if load is not None:
        found.append(load)
    return found
