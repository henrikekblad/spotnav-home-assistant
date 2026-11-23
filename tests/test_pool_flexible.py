"""The pool's and the spa's loads and house cooling in the common shape (`pool/flexible.py`, `heat/flexible.py`, spec
section 4): each describes itself, the JSON round trip, a closed season, and the site's diagnostics carrying them."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from homeassistant.core import HomeAssistant

from custom_components.spotnav.const import CONF_HEAT
from custom_components.spotnav.diagnostics import async_get_config_entry_diagnostics
from custom_components.spotnav.heat.config import HeatConfig, Zone
from custom_components.spotnav.heat.flexible import zone_loads
from custom_components.spotnav.planning.flexible_load import (
    CHOICE_ACCEPT_PEAK,
    CHOICE_LATER,
    CHOICE_TAKES_WHAT_FITS,
    GIVE_COMFORT_DEG_H,
    GIVE_HOURS_PAUSED,
    GIVE_MINUTES_LATE,
    GOAL_RUNTIME_PER_DAY,
    GOAL_TEMPERATURE_BY,
    HANDLE_ON_OFF,
    HANDLE_SETPOINT_OFFSET,
    KINDS,
    FlexibleLoad,
)
from custom_components.spotnav.pool.config import PoolConfig, PoolHeatPump
from custom_components.spotnav.pool.flexible import circulation_load, heat_pump_load, spa_load
from custom_components.spotnav.spa.config import SpaConfig

from tests.world import setup_charger_and_site

SITE = "site_x"
BATH = datetime(2026, 11, 27, 18, 0, tzinfo=timezone.utc)
POOL = PoolConfig(
    heat_pump=PoolHeatPump("climate.poolpump"),
    circulation_id="switch.pool_cirkulation",
    heat_pump_kw=1.6,
    circulation_kw=0.4,
    control_circulation=True,
)


def _rule(load: FlexibleLoad, code: str) -> dict:
    return next(dict(item.values) for item in load.hard_rules if item.code == code)


def _round_trip(load: FlexibleLoad) -> None:
    assert FlexibleLoad.from_dict(json.loads(json.dumps(load.as_dict()))) == load


def test_the_new_kinds_are_known() -> None:
    assert {"pool_heat", "pool_circulation", "spa"} <= set(KINDS)


def test_the_circulation_runs_its_filtering_need_per_day_moved_never_dropped() -> None:
    load = circulation_load(SITE, POOL, need_h=8.0, extra_h=2.0)
    assert load is not None
    assert (load.id, load.kind, load.controlled) == ("pool_circulation", "pool_circulation", True)
    assert load.goal.kind == GOAL_RUNTIME_PER_DAY
    assert dict(load.goal.values) == {"hours": 8.0, "source": "water", "extra_h": 2.0}
    handle = load.handles[0]
    assert (handle.kind, handle.high, dict(handle.values)) == (HANDLE_ON_OFF, 0.4, {"entity_id": "switch.pool_cirkulation"})
    assert [rule.code for rule in load.hard_rules] == [
        "season_closed", "heat_pump_needs_circulation", "run_on", "min_on", "min_off", "starts_cap"
    ]
    assert _rule(load, "run_on") == {"minutes": 5}
    assert load.give.kind == GIVE_HOURS_PAUSED and load.choices == ()
    _round_trip(load)
    # The person's own hours.
    override = circulation_load(SITE, PoolConfig(circulation_id="switch.c", filter_hours=4.0), need_h=4.0)
    assert dict(override.goal.values)["source"] == "override"  # type: ignore[union-attr]


def test_the_heat_pump_keeps_its_temperature_inside_the_circulations_hours() -> None:
    load = heat_pump_load(SITE, POOL)
    assert load is not None
    assert (load.id, load.kind, load.controlled) == ("pool_heat", "pool_heat", False)
    assert load.goal.kind == GOAL_TEMPERATURE_BY and load.goal.by is None
    assert dict(load.goal.values) == {"target_c": 27.0}
    assert dict(load.handles[0].values) == {"entity_id": "climate.poolpump", "shape": "climate", "setpoint_c": 27.0}
    assert _rule(load, "inside_circulation") == {"circulation": "switch.pool_cirkulation"}
    assert _rule(load, "write_cap") == {"per_day": 6}
    assert load.give.kind == GIVE_COMFORT_DEG_H
    _round_trip(load)


def test_a_closed_season_or_a_missing_pump_is_no_load() -> None:
    closed = PoolConfig(heat_pump=PoolHeatPump("climate.poolpump"), circulation_id="switch.c", season_open=False)
    assert circulation_load(SITE, closed, need_h=3.0) is None and heat_pump_load(SITE, closed) is None
    assert heat_pump_load(SITE, PoolConfig(circulation_id="switch.c")) is None
    assert circulation_load(SITE, PoolConfig(heat_pump=PoolHeatPump("climate.p")), need_h=3.0) is None


def test_the_spa_wants_its_bath_by_the_next_bath_with_its_choices() -> None:
    balboa = SpaConfig("climate.spa", "balboa", heater_kw=3.0, control=True)
    load = spa_load(SITE, balboa, next_bath=BATH, ceiling_choice=True)
    assert (load.id, load.kind, load.controlled) == ("spa", "spa", True)
    assert load.goal.by == BATH and dict(load.goal.values) == {"bath_c": 38.0, "idle_c": 30.0, "floor_c": 10.0}
    assert load.handles[0].kind == HANDLE_ON_OFF and load.handles[0].high == 3.0
    assert _rule(load, "floor") == {"floor_c": 10.0} and _rule(load, "write_cap") == {"per_day": 12}
    assert [(c.kind, c.sets) for c in load.choices] == [
        (CHOICE_TAKES_WHAT_FITS, "spa_take_what_fits"), (CHOICE_LATER, "spa_later_bath"), (CHOICE_ACCEPT_PEAK, "spa_accept_peak")
    ]
    assert load.give.kind == GIVE_MINUTES_LATE
    _round_trip(load)
    # SmartTub has no heater off: a setpoint between the idle and the bath temperature.
    smarttub = spa_load(SITE, SpaConfig("climate.tub", "smarttub", idle_c=32.0))
    assert smarttub.handles[0].kind == HANDLE_SETPOINT_OFFSET and smarttub.handles[0].high == 6.0
    assert smarttub.goal.by is None and [c.kind for c in smarttub.choices] == [CHOICE_TAKES_WHAT_FITS, CHOICE_LATER]
    # A watched-only spa has no handle and is never controlled.
    generic = spa_load(SITE, SpaConfig("climate.bubbel", "generic", control=True))
    assert generic.handles == () and not generic.controlled
    _round_trip(generic)


def test_a_zone_in_cool_mode_is_house_cooling_raised_one_degree_under_its_ceiling() -> None:
    config = HeatConfig(zones=(Zone("climate.air", 0.0, True), Zone("climate.floor", 0.0)))
    loads = zone_loads(SITE, config, {"climate.air": 24.0, "climate.floor": 21.0}, power_tariff=True, modes={"climate.air": "cool"})
    cooling, heating = loads
    assert dict(cooling.goal.values) == {"target_c": 24.0, "high_c": 25.0, "mode": "cool", "load": "cooling"}
    handle = cooling.handles[0]
    assert (handle.kind, handle.low, handle.high) == (HANDLE_SETPOINT_OFFSET, 0.0, 1.0)
    assert _rule(cooling, "comfort_ceiling") == {"above_c": 25.0}
    assert _rule(cooling, "write_cap") == {"writes_per_day": 24} and cooling.controlled
    assert "mode" not in dict(heating.goal.values)
    _round_trip(cooling)
    # Never above 28 °C; a setpoint already over it is not raised.
    warm = zone_loads(SITE, config, {"climate.air": 27.5}, power_tariff=False, modes={"climate.air": "cool"})[0]
    assert dict(warm.goal.values)["high_c"] == 28.0
    hot = zone_loads(SITE, config, {"climate.air": 29.0}, power_tariff=False, modes={"climate.air": "cool"})[0]
    assert dict(hot.goal.values)["high_c"] == 29.0


async def test_the_sites_diagnostics_list_the_pool_and_the_spa(hass: HomeAssistant) -> None:
    _charger, site = await setup_charger_and_site(hass)
    assert site is not None
    heat = {
        "pool": {"heat_pump": {"entity_id": "climate.poolpump"}, "circulation_id": "switch.pool_cirkulation"},
        "spa": {"entity_id": "climate.spa", "family": "balboa"},
    }
    hass.config_entries.async_update_entry(site, data={**site.data, CONF_HEAT: heat})
    await hass.async_block_till_done()

    loads = (await async_get_config_entry_diagnostics(hass, site))["flexible_loads"]

    assert [(load["kind"], load["id"]) for load in loads] == [
        ("car", "entry_a"), ("pool_heat", "pool_heat"), ("pool_circulation", "pool_circulation"), ("spa", "spa")
    ]
    assert [load["state"] for load in loads[1:]] == ["watched", "watched", "watched"]
    assert [FlexibleLoad.from_dict(load).as_dict() for load in loads] == loads


def test_the_sauna_heats_only_for_a_booked_session_and_respects_its_lock() -> None:
    from custom_components.spotnav.pool.flexible import sauna_load
    from custom_components.spotnav.sauna.config import SaunaConfig

    relay = SaunaConfig("switch.bastu", "relay", heater_kw=9.0, control=True)
    load = sauna_load(SITE, relay, next_session=BATH, ceiling_choice=True, max_on_min=60.0)
    assert (load.id, load.kind, load.goal.by) == ("sauna", "sauna", BATH)
    # A relay without its declared safety measure is never controlled.
    assert not load.controlled
    assert [rule.code for rule in load.hard_rules] == ["booked_session_only", "remote_start_lock", "max_on", "write_cap", "hand_back"]
    assert _rule(load, "remote_start_lock") == {"lock": None, "declared": False} and _rule(load, "max_on") == {"minutes": 60.0}
    assert load.handles[0].high == 9.0 and [c.sets for c in load.choices][-1] == "sauna_accept_peak"
    _round_trip(load)
    declared = sauna_load(SITE, SaunaConfig("switch.bastu", "relay", safety_declared=True, control=True))
    assert declared.controlled
