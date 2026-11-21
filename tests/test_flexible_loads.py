"""Every load the plan can move describes itself in one shape (plan `plans/v3_flexible_loads_design.md`, step 1):
each describer on representative settings, the JSON round trip, and the site's diagnostics carrying them."""

from __future__ import annotations

import json
from datetime import datetime, time, timedelta, timezone

import pytest
from homeassistant.core import HomeAssistant

from custom_components.spotnav.battery.commands import SETTING_WRITES, control_for
from custom_components.spotnav.battery.executor import HEADROOM_MARGIN_A
from custom_components.spotnav.battery.facts import BatteryFacts
from custom_components.spotnav.battery.flexible import battery_load
from custom_components.spotnav.battery.gate import Options
from custom_components.spotnav.const import CONF_HEAT
from custom_components.spotnav.diagnostics import async_get_config_entry_diagnostics
from custom_components.spotnav.execution.controller import ChargingPlan
from custom_components.spotnav.execution.flexible_car import car_load
from custom_components.spotnav.heat import relay_control, zone_control
from custom_components.spotnav.heat.config import (
    PATH_OFFSET,
    PATH_RELAY,
    PATH_SG_READY,
    HeatConfig,
    Night,
    WaterHeater,
    Zone,
)
from custom_components.spotnav.heat.flexible import heat_pump_load, water_heater_load, zone_loads
from custom_components.spotnav.heat.shadow_rule import MAX_LOWERED_DAY, MAX_LOWERED_RUN
from custom_components.spotnav.heat.water_heater import FULL_EVERY, MAX_OFF
from custom_components.spotnav.planning.auto_settings import (
    DRIVER_TARGET_SOC,
    IF_NOT_READY_HOLD,
    IF_NOT_READY_RELEASE,
    MANUAL_STOP,
    PAUSE_MANUAL,
    MANUAL_SCOPE_PLUG_IN,
    AutoSettings,
    PauseIntent,
    TargetSocIntent,
)
from custom_components.spotnav.planning.flexible_load import (
    CHOICE_ACCEPT_PEAK,
    CHOICE_LATER,
    CHOICE_LESS_COMFORT,
    CHOICE_PAUSE_NOW,
    CHOICE_TAKES_WHAT_FITS,
    GIVE_COMFORT_DEG_H,
    GIVE_HOURS_PAUSED,
    GIVE_KR,
    GIVE_KWH_SHORT,
    GOAL_COMFORT_BAND,
    GOAL_ENERGY_BY,
    GOAL_STORE,
    GOAL_TEMPERATURE_BY,
    HANDLE_CONTINUOUS,
    HANDLE_ON_OFF,
    HANDLE_SETPOINT_OFFSET,
    HANDLE_STEPS,
    FlexibleLoad,
)

from tests.world import setup_charger_and_site

SITE = "site_x"
NOW = datetime(2026, 11, 22, 12, 0, tzinfo=timezone.utc)


def _rule(load: FlexibleLoad, code: str) -> dict:
    return next(dict(item.values) for item in load.hard_rules if item.code == code)


def _kinds(load: FlexibleLoad) -> list[str]:
    return [choice.kind for choice in load.choices]


def _round_trip(load: FlexibleLoad) -> None:
    text = json.dumps(load.as_dict())
    assert FlexibleLoad.from_dict(json.loads(text)) == load


# ---------------------------------------------------------------------------------------------------------- the car


def test_a_car_with_a_plan_wants_the_plans_energy_by_its_departure() -> None:
    plan = ChargingPlan(
        start="2026-11-22T22:00:00+00:00",
        end="2026-11-23T05:00:00+00:00",
        amps=16,
        energy_kwh=18.0,
        departure="2026-11-23T06:00:00+00:00",
    )
    load = car_load(SITE, "charger_a", AutoSettings(amps=16, if_not_ready=IF_NOT_READY_HOLD), plan, current_a=16.0, min_soc_pct=30.0, power_tariff=True)

    assert load.kind == "car" and load.site == SITE and load.id == "charger_a"
    assert load.goal.kind == GOAL_ENERGY_BY and load.goal.by == datetime(2026, 11, 23, 6, tzinfo=timezone.utc)
    assert dict(load.goal.values)["kwh"] == 18.0 and dict(load.goal.values)["source"] == "plan"
    handle = load.handles[0]
    assert (handle.kind, handle.unit, handle.low, handle.high) == (HANDLE_CONTINUOUS, "A", 6.0, 16.0)
    assert _rule(load, "min_charge_level") == {"pct": 30.0}
    assert _rule(load, "person_start_stop") == {"action": None, "scope": None}
    assert load.give.kind == GIVE_KWH_SHORT
    assert _kinds(load) == [CHOICE_TAKES_WHAT_FITS, CHOICE_LATER, CHOICE_ACCEPT_PEAK]
    assert [choice.chosen for choice in load.choices] == [True, False, False]
    _round_trip(load)


def test_a_dc_car_without_a_plan_takes_its_target_from_the_settings_and_its_floor_from_the_charger() -> None:
    settings = AutoSettings(
        driver=DRIVER_TARGET_SOC,
        target=TargetSocIntent(vehicle_id="car_1", target_percent=80.0),
        departure=time(7, 0),
        if_not_ready=IF_NOT_READY_RELEASE,
        pause=PauseIntent(choice=PAUSE_MANUAL, admitted_at=NOW, action=MANUAL_STOP, scope=MANUAL_SCOPE_PLUG_IN),
    )
    load = car_load(SITE, "dc", settings, None, min_current_a=2.17, current_a=16.0, phase_switching=True, import_target=True)

    assert load.goal.by is None
    assert dict(load.goal.values) == {
        "source": "settings", "kwh": None, "target_soc_pct": 80.0, "fill_to_limit": False, "departure": "07:00",
        "strategy": "cheapest",
    }
    assert dict(load.handles[0].values)["power_given"] is True and load.handles[0].low == 2.17
    assert load.handles[1].kind == HANDLE_STEPS and load.handles[1].steps == (1.0, 3.0)
    assert _rule(load, "min_charge_level") == {"pct": None}
    assert _rule(load, "person_start_stop") == {"action": "stop", "scope": "plug_in"}
    # The import target's own choice: release is accepting the peak.
    assert [(choice.kind, choice.chosen) for choice in load.choices][-1] == (CHOICE_ACCEPT_PEAK, True)
    _round_trip(load)


def test_a_car_under_no_ceiling_is_offered_no_peak() -> None:
    load = car_load(SITE, "charger_a", AutoSettings(), None)

    assert CHOICE_ACCEPT_PEAK not in _kinds(load)


# ---------------------------------------------------------------------------------------------------------- the battery


def test_a_home_battery_stores_within_its_floor_the_fuse_and_its_write_caps() -> None:
    facts = BatteryFacts(
        battery_entity_id="sensor.battery_soc",
        brand="sigenergy",
        label="Sigenergy",
        capacity_kwh=16.0,
        rated_charge_kw=8.0,
        rated_discharge_kw=8.0,
        reserve_soc_pct=10.0,
        plant_phase_entities=("sensor.a", "sensor.b", "sensor.c"),
    )
    control = control_for("sigenergy")
    load = battery_load(SITE, facts, Options(enabled=True, shadow_only=False), main_fuse_a=20.0, phased=True, control=control)

    assert load is not None and load.kind == "home_battery" and load.controlled
    assert load.goal.kind == GOAL_STORE and dict(load.goal.values)["floor_soc_pct"] == 10.0
    handle = load.handles[0]
    assert (handle.kind, handle.low, handle.high) == (HANDLE_CONTINUOUS, -8.0, 8.0)
    assert dict(handle.values) == {"per_phase": True, "phases_read": True}
    assert _rule(load, "floor_soc") == {"pct": 10.0}
    assert _rule(load, "fuse")["main_fuse_a"] == 20.0 and _rule(load, "fuse")["margin_a"] == HEADROOM_MARGIN_A
    assert _rule(load, "write_cap")["setting_per_day"] == control.write_cap("setting")
    assert load.give.kind == GIVE_KR and load.choices == ()
    _round_trip(load)


def test_no_battery_is_no_load_and_one_spotnav_writes_nothing_to_is_only_watched() -> None:
    assert battery_load(SITE, BatteryFacts(), Options(), main_fuse_a=None, phased=False, control=None) is None
    load = battery_load(
        SITE, BatteryFacts(battery_entity_id="sensor.soc"), Options(enabled=True, shadow_only=False),
        main_fuse_a=None, phased=False, control=None,
    )
    assert load is not None and not load.controlled
    assert _rule(load, "write_cap") == {"setting_per_day": SETTING_WRITES, "setpoint_per_day": None}


# ---------------------------------------------------------------------------------------------------------- heat


def test_the_heat_pumps_relay_keeps_the_comfort_band_and_the_run_rules() -> None:
    config = HeatConfig(
        target_c=21.0, control_path=PATH_RELAY, control_entity="switch.relay", control_enabled=True,
        night=Night(on=True, delta_c=1.0),
    )
    load = heat_pump_load(SITE, config, power_tariff=True)

    assert load is not None and load.kind == "heat" and load.id == "switch.relay" and load.controlled
    assert load.goal.kind == GOAL_COMFORT_BAND
    goal = dict(load.goal.values)
    assert (goal["low_c"], goal["high_c"], goal["night_c"]) == (20.5, 21.5, 20.0)
    assert load.handles[0].kind == HANDLE_STEPS and load.handles[0].steps == (-1.0, 0.0)
    assert _rule(load, "comfort_floor")["below_c"] == 20.5
    assert _rule(load, "max_lowered_run") == {"hours": relay_control.MAX_RUN.total_seconds() / 3600}
    assert _rule(load, "max_lowered_day") == {"hours": relay_control.MAX_DAY.total_seconds() / 3600}
    assert _rule(load, "plan_lowered_run") == {"hours": MAX_LOWERED_RUN}
    assert _rule(load, "plan_lowered_day") == {"hours": MAX_LOWERED_DAY}
    assert _rule(load, "write_cap") == {"writes_per_day": relay_control.MAX_WRITES_DAY}
    assert load.give.kind == GIVE_COMFORT_DEG_H
    assert _kinds(load) == [CHOICE_LESS_COMFORT, CHOICE_ACCEPT_PEAK]
    _round_trip(load)


def test_sg_ready_raises_too_an_offset_moves_in_steps_and_no_path_is_no_load() -> None:
    sg = heat_pump_load(
        SITE, HeatConfig(control_path=PATH_SG_READY, control_entity="switch.a", control_entity_b="switch.b"),
        power_tariff=False,
    )
    assert sg is not None and sg.handles[0].steps == (-1.0, 0.0, 1.0) and not sg.controlled
    assert _kinds(sg) == [CHOICE_LESS_COMFORT]
    offset = heat_pump_load(SITE, HeatConfig(control_path=PATH_OFFSET, control_entity="number.offset"), power_tariff=False)
    assert offset is not None and offset.handles[0].kind == HANDLE_SETPOINT_OFFSET
    assert (offset.handles[0].low, offset.handles[0].high) == (-3.0, 3.0)
    assert heat_pump_load(SITE, HeatConfig(), power_tariff=False) is None


def test_each_zone_keeps_its_band_around_the_persons_own_setpoint() -> None:
    config = HeatConfig(zones=(Zone("climate.hall", control=True), Zone("climate.bad")))
    loads = zone_loads(SITE, config, {"climate.hall": 22.0}, power_tariff=False)

    hall, bad = loads
    assert hall.kind == "heat_zone" and hall.controlled and not bad.controlled
    assert dict(hall.goal.values) == {"target_c": 22.0, "low_c": 21.5, "high_c": 22.5}
    assert dict(bad.goal.values)["target_c"] is None
    assert (hall.handles[0].low, hall.handles[0].high) == (-zone_control.LOWER_C, zone_control.RAISE_C)
    assert _rule(hall, "comfort_floor")["below_c"] == 21.5
    assert _rule(hall, "write_cap") == {"writes_per_day": zone_control.MAX_WRITES_DAY}
    for load in loads:
        _round_trip(load)


def test_the_water_heater_is_full_every_day_never_off_long_and_on_without_a_reading() -> None:
    last = NOW - timedelta(hours=5)
    config = HeatConfig(water_heater=WaterHeater("switch.vvb", control=True))
    load = water_heater_load(SITE, config, metered=True, last_full=last, kw=3.0)

    assert load is not None and load.kind == "water_heater" and load.controlled
    assert load.goal.kind == GOAL_TEMPERATURE_BY and load.goal.by == last + FULL_EVERY
    assert dict(load.goal.values)["mode"] == "metered"
    assert load.handles[0].kind == HANDLE_ON_OFF and load.handles[0].high == 3.0
    assert _rule(load, "full_heat_every")["hours"] == FULL_EVERY.total_seconds() / 3600
    assert _rule(load, "max_off")["hours"] == MAX_OFF.total_seconds() / 3600
    assert "fail_on" in [item.code for item in load.hard_rules]
    assert load.give.kind == GIVE_HOURS_PAUSED and _kinds(load) == [CHOICE_PAUSE_NOW]
    _round_trip(load)

    unmetered = water_heater_load(SITE, config, metered=False, last_full=last, kw=None)
    assert unmetered is not None and unmetered.goal.by is None
    assert "night_window" in [item.code for item in unmetered.hard_rules]
    assert water_heater_load(SITE, HeatConfig(), metered=False, last_full=None, kw=None) is None


# ---------------------------------------------------------------------------------------------------------- the site


@pytest.mark.usefixtures("offline_relay")
async def test_the_sites_diagnostics_list_its_loads(hass: HomeAssistant) -> None:
    _charger, site = await setup_charger_and_site(hass)
    assert site is not None
    heat = {
        "control_path": PATH_RELAY,
        "control_entity": "switch.relay",
        "zones": [{"entity_id": "climate.hall", "offset_c": 0.0, "control": False}],
        "water_heater": {"entity_id": "switch.vvb", "control": False, "tank_sensor": None},
    }
    hass.config_entries.async_update_entry(site, data={**site.data, CONF_HEAT: heat})
    await hass.async_block_till_done()

    loads = (await async_get_config_entry_diagnostics(hass, site))["flexible_loads"]

    assert [(load["kind"], load["id"]) for load in loads] == [
        ("car", "entry_a"),
        ("heat", "switch.relay"),
        ("heat_zone", "climate.hall"),
        ("water_heater", "switch.vvb"),
    ]
    assert all(load["site"] == site.entry_id for load in loads)
    assert [FlexibleLoad.from_dict(load).as_dict() for load in loads] == loads
    json.dumps(loads)
