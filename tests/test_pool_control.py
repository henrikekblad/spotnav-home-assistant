"""SpotNav setting the pool's circulation pump and heat pump (`pool/control.py`, spec stages 9 to 11 and 14): behind
each opt-in, the interlock (circulation before the heat pump, the heat pump off first, the run-on), the minimum times,
the starts and the write budgets, the read-back, the hand-back, the snapshot and its restore at the opt-out, the stop
and after a crash, the season closed, a mode never written, °F, core Tuya's switch and number, and the live steps of
the shed order. Every write goes to a fake service; nothing reaches a device."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.spotnav.loads.hold_part import STATE_CONTROLLED, STATE_HANDED_BACK, STATE_OFF, STATE_PAUSED, STATE_SEASON_CLOSED
from custom_components.spotnav.pool import control as control_module
from custom_components.spotnav.pool.control import PoolSpaControl
from custom_components.spotnav.pool.rules import LOAD_POOL_CIRCULATION, LOAD_POOL_HEAT

from .pool_spa_fakes import MIN, NOW, S, Devices, looks, make_control

PUMP = "climate.poolpump"
CIRC = "switch.pool_cirkulation"


def _pool(**extra: Any) -> dict[str, Any]:
    return {
        "pool": {
            "heat_pump": {"entity_id": PUMP},
            "circulation_id": CIRC,
            "heat_pump_kw": 2.0,
            "circulation_kw": 0.5,
            "control_heat_pump": True,
            "control_circulation": True,
            **extra,
        }
    }


def _devices(hass: HomeAssistant, *, mode: str = "off", circ: str = "off", setpoint: float = 27.0) -> Devices:
    devices = Devices(hass)
    devices.add(PUMP, mode, temperature=setpoint, current_temperature=24.0, min_temp=12, max_temp=40, hvac_modes=["off", "heat", "cool"])
    devices.add(CIRC, circ)
    return devices


async def test_nothing_is_written_without_the_opt_ins(hass: HomeAssistant) -> None:
    devices = _devices(hass)
    control, shadow, _order = make_control(hass, _pool(control_heat_pump=False, control_circulation=False))
    shadow.pool_action("heat")
    await looks(control, NOW, NOW + 10 * MIN)
    assert devices.calls == []
    assert control.pool.hp.state == STATE_OFF and control.pool.circ.state == STATE_OFF


async def test_the_circulation_runs_a_full_minute_before_the_heat_pump_starts(hass: HomeAssistant) -> None:
    devices = _devices(hass, setpoint=28.0)
    control, shadow, _order = make_control(hass, _pool(target_c=28.0))
    shadow.pool_action("heat")
    await control.async_look(NOW)
    assert devices.writes() == [("switch.turn_on", CIRC, "on")]
    # Read on from the next look; the heat pump waits the lead.
    await control.async_look(NOW + 30 * S)
    await control.async_look(NOW + 60 * S)
    assert devices.writes(PUMP) == [] and control.pool.hp.reason == "interlock"
    await control.async_look(NOW + 90 * S)
    assert devices.writes(PUMP) == [("climate.set_hvac_mode", PUMP, "heat")]
    assert control.pool.hp.state == STATE_CONTROLLED


async def test_the_heat_pump_goes_off_first_and_the_circulation_runs_on_five_minutes(hass: HomeAssistant) -> None:
    devices = _devices(hass, mode="heat", circ="on", setpoint=27.0)
    control, shadow, _order = make_control(hass, _pool())
    shadow.pool_action("heat")
    await looks(control, NOW, NOW + 40 * MIN)
    assert devices.calls == []
    shadow.pool_action("off")
    start = NOW + 41 * MIN
    await control.async_look(start)
    assert devices.writes() == [("climate.set_hvac_mode", PUMP, "off")]
    await looks(control, start + 30 * S, start + 5 * MIN)
    assert devices.writes(CIRC) == [] and control.pool.circ.reason == "run_on"
    await looks(control, start + 5 * MIN + 30 * S, start + 6 * MIN)
    assert devices.writes(CIRC) == [("switch.turn_off", CIRC, "off")]


async def test_the_circulation_reading_off_turns_the_heat_pump_off_at_once(hass: HomeAssistant) -> None:
    devices = _devices(hass, mode="heat", circ="on")
    # Only the heat pump is SpotNav's: the person's own schedule runs the circulation.
    control, shadow, _order = make_control(hass, _pool(control_circulation=False))
    shadow.pool_action("heat")
    await looks(control, NOW, NOW + 2 * MIN)
    assert devices.calls == []
    devices.set(CIRC, "off")
    await control.async_look(NOW + 2 * MIN + 30 * S)
    assert devices.writes() == [("climate.set_hvac_mode", PUMP, "off")]
    assert control.pool.hp.reason == "interlock"
    # Not on again until the person's circulation has run a minute.
    devices.set(CIRC, "on")
    await looks(control, NOW + 3 * MIN, NOW + 3 * MIN + 30 * S)
    assert devices.writes(PUMP) == [("climate.set_hvac_mode", PUMP, "off")]


def _device_with(hass: HomeAssistant, sensors: dict[str, str]) -> None:
    """The pump in the registry on a device with binary sensors (`name: device class`)."""
    entry = MockConfigEntry(domain="tuya_local")
    entry.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(config_entry_id=entry.entry_id, identifiers={("tuya_local", "pump")})
    registry = er.async_get(hass)
    pump = registry.async_get_or_create("climate", "tuya_local", "pump", device_id=device.id, suggested_object_id="poolpump")
    assert pump.entity_id == PUMP
    for name, device_class in sensors.items():
        registry.async_get_or_create(
            "binary_sensor", "tuya_local", name, device_id=device.id, original_device_class=device_class or None,
            original_name=name, suggested_object_id=name,
        )


async def test_no_flow_keeps_the_heat_pump_off_and_a_fault_holds_it_off_an_hour(hass: HomeAssistant) -> None:
    _device_with(hass, {"water_flow": "", "fault": "problem"})
    devices = _devices(hass, circ="on")
    devices.add("binary_sensor.water_flow", "off")
    devices.add("binary_sensor.fault", "off")
    control, shadow, _order = make_control(hass, _pool())
    shadow.pool_action("heat")
    await looks(control, NOW, NOW + 3 * MIN)
    assert devices.writes(PUMP) == [] and control.pool.hp.reason == "interlock"
    devices.set("binary_sensor.water_flow", "on")
    await control.async_look(NOW + 3 * MIN + 30 * S)
    assert devices.writes(PUMP) == [("climate.set_hvac_mode", PUMP, "heat")]
    devices.set("binary_sensor.fault", "on")
    await control.async_look(NOW + 4 * MIN)
    assert devices.writes(PUMP)[-1] == ("climate.set_hvac_mode", PUMP, "off") and control.pool.hp.reason == "fault"
    devices.set("binary_sensor.fault", "off")
    await looks(control, NOW + 5 * MIN, NOW + 60 * MIN, 5 * MIN)
    assert len(devices.writes(PUMP)) == 2
    await looks(control, NOW + 66 * MIN, NOW + 80 * MIN)
    assert devices.writes(PUMP)[-1] == ("climate.set_hvac_mode", PUMP, "heat")


async def test_the_minimum_times_and_the_day_s_starts(hass: HomeAssistant) -> None:
    devices = _devices(hass, circ="on")
    control, shadow, _order = make_control(hass, _pool())
    moment = NOW
    for _cycle in range(8):
        shadow.pool_action("heat")
        moment = await looks(control, moment, moment + 35 * MIN, MIN)
        shadow.pool_action("filter")
        moment = await looks(control, moment + MIN, moment + 12 * MIN, MIN)
    starts = [call for call in devices.writes(PUMP) if call[2] == "heat"]
    shadow.pool_action("heat")
    await control.async_look(moment + MIN)
    assert len(starts) == 6 and control.pool.hp.reason == "starts_cap"
    # On at least 30 minutes, off at least 10 between.
    times = control.pool.hp.budget.starts["heat_pump"]
    assert all(later - earlier >= 40 * MIN for earlier, later in zip(times, times[1:], strict=False))


async def test_the_circulation_s_day_of_switchings_and_starts(hass: HomeAssistant) -> None:
    devices = _devices(hass)
    control, shadow, _order = make_control(hass, _pool(heat_pump=None, control_heat_pump=False))
    moment = NOW
    for _cycle in range(10):
        shadow.pool_action("filter")
        moment = await looks(control, moment, moment + 35 * MIN, MIN)
        shadow.pool_action("off")
        moment = await looks(control, moment + MIN, moment + 20 * MIN, MIN)
    shadow.pool_action("filter")
    await control.async_look(moment + MIN)
    ons = [call for call in devices.writes(CIRC) if call[2] == "on"]
    # Twelve on/off writes a day (six cycles) come before the eight starts.
    assert len(ons) == 6 and len(devices.writes(CIRC)) == 12 and control.pool.circ.reason == "write_cap"
    # Never more than two in an hour.
    times = control.pool.circ.budget.starts["circulation"]
    assert all(times[index + 2] - times[index] >= 60 * MIN for index in range(len(times) - 2))


async def test_a_write_not_read_back_is_counted_and_pauses_to_the_next_day(hass: HomeAssistant) -> None:
    devices = _devices(hass)
    devices.ignore.add(CIRC)
    control, shadow, _order = make_control(hass, _pool())
    shadow.pool_action("filter")
    await looks(control, NOW, NOW + 2 * MIN)
    assert control.pool.circ.failures == 1 and control.pool.circ.state == STATE_PAUSED
    assert control.pool.circ.reason == "no_read_back" and control.pool.circ.paused_until is not None
    assert control.pool.status(NOW + 2 * MIN, control.config().pool)["circulation"]["failures"] == 1  # type: ignore[arg-type]
    await looks(control, NOW + 3 * MIN, NOW + 60 * MIN, 5 * MIN)
    assert devices.writes(CIRC) == [("switch.turn_on", CIRC, "on")]


async def test_a_refused_call_is_counted_at_once(hass: HomeAssistant) -> None:
    devices = _devices(hass)
    devices.refuse.add(CIRC)
    control, shadow, _order = make_control(hass, _pool())
    shadow.pool_action("filter")
    await control.async_look(NOW)
    assert control.pool.circ.failures == 1 and control.pool.circ.paused_reason == "no_read_back"


async def test_a_change_spotnav_did_not_make_is_handed_back_without_a_fight(hass: HomeAssistant) -> None:
    devices = _devices(hass)
    control, shadow, _order = make_control(hass, _pool())
    shadow.pool_action("filter")
    await looks(control, NOW, NOW + MIN)
    assert devices.state(CIRC) == "on"
    devices.set(CIRC, "off")
    await looks(control, NOW + 90 * S, NOW + 4 * MIN)
    assert control.pool.circ.state == STATE_HANDED_BACK and control.pool.circ.reason == "foreign_change"
    assert devices.writes(CIRC) == [("switch.turn_on", CIRC, "on")]
    held = control.pool.circ.helds["circulation"]
    assert held.person == "off" and not held.holds()
    # Watched to the next local day, then the plan again.
    tomorrow = control.pool.circ.paused_until
    assert tomorrow is not None
    await control.async_look(tomorrow + MIN)
    assert devices.writes(CIRC)[-1] == ("switch.turn_on", CIRC, "on")


async def test_the_snapshot_is_written_back_at_the_opt_out_only_where_spotnav_s_value_reads(hass: HomeAssistant) -> None:
    devices = _devices(hass, setpoint=26.0)
    control, shadow, _order = make_control(hass, _pool(target_c=29.0))
    shadow.pool_action("heat")
    await looks(control, NOW, NOW + 3 * MIN)
    assert devices.state(PUMP) == "heat" and devices.attr(PUMP, "temperature") == 29.0
    # The heat pump's opt-in off: the person's mode and setpoint back; the circulation runs on, then the plan.
    shadow.raw = _pool(target_c=29.0, control_heat_pump=False)
    await control.async_look(NOW + 4 * MIN)
    assert devices.state(PUMP) == "off" and devices.attr(PUMP, "temperature") == 26.0
    # Both off: the circulation back to off only after the run-on.
    shadow.raw = _pool(target_c=29.0, control_heat_pump=False, control_circulation=False)
    await control.async_look(NOW + 5 * MIN)
    assert devices.state(CIRC) == "on"
    await looks(control, NOW + 6 * MIN, NOW + 11 * MIN)
    assert devices.state(CIRC) == "off" and not control.holds_any()


async def test_a_value_someone_else_set_is_left_at_the_opt_out(hass: HomeAssistant) -> None:
    devices = _devices(hass)
    control, shadow, _order = make_control(hass, _pool(heat_pump=None, control_heat_pump=False))
    shadow.pool_action("filter")
    await looks(control, NOW, NOW + MIN)
    devices.set(CIRC, "off")
    shadow.raw = _pool(heat_pump=None, control_heat_pump=False, control_circulation=False)
    await looks(control, NOW + 90 * S, NOW + 10 * MIN)
    assert devices.writes(CIRC) == [("switch.turn_on", CIRC, "on")]


async def test_the_person_s_values_come_back_at_the_stop_and_after_a_crash(hass: HomeAssistant) -> None:
    devices = _devices(hass, setpoint=26.0)
    control, shadow, _order = make_control(hass, _pool(target_c=28.0))
    shadow.pool_action("filter")
    await looks(control, NOW, NOW + 20 * MIN)
    assert devices.state(CIRC) == "on"
    # A crash: what is kept is read by the next start and written back first.
    await control.async_save()
    again = PoolSpaControl(hass, control.site)
    raw = await again._store.async_load()
    again.pool.restore(raw["pool"])  # type: ignore[index]
    again._started = True
    assert again.pool.circ.restart_restore
    # The heat pump's past is not known after a crash: the circulation's run-on is waited out first; then the person's
    # value is written back before the plan is followed again.
    before = len(devices.calls)
    await looks(again, NOW + 21 * MIN, NOW + 25 * MIN)
    assert devices.calls[before:] == [("climate.set_temperature", PUMP, 26.0)]
    await looks(again, NOW + 26 * MIN + 30 * S, NOW + 27 * MIN)
    assert devices.calls[before + 1] == ("switch.turn_off", CIRC, "off")
    # The stop: the heat pump back at once; the circulation kept while the run-on lasts, for the next start.
    shadow.pool_action("heat")
    await looks(again, NOW + 50 * MIN, NOW + 52 * MIN)
    assert devices.state(PUMP) == "heat"
    await again.async_shutdown()
    assert devices.state(PUMP) == "off" and devices.attr(PUMP, "temperature") == 26.0
    assert devices.state(CIRC) == "on" and again.pool.circ.holds()


async def test_closing_the_season_writes_nothing_at_all(hass: HomeAssistant) -> None:
    devices = _devices(hass)
    control, shadow, _order = make_control(hass, _pool())
    shadow.pool_action("heat")
    await looks(control, NOW, NOW + 3 * MIN)
    count = len(devices.calls)
    assert devices.state(PUMP) == "heat"
    shadow.raw = _pool(season_open=False)
    await looks(control, NOW + 4 * MIN, NOW + 30 * MIN)
    devices.set(CIRC, "off")
    await looks(control, NOW + 31 * MIN, NOW + 40 * MIN)
    await control.async_shutdown()
    assert len(devices.calls) == count and not control.holds_any()
    assert control.pool.hp.state == STATE_SEASON_CLOSED


async def test_a_mode_spotnav_never_writes_is_watched(hass: HomeAssistant) -> None:
    devices = _devices(hass, mode="cool", circ="on")
    control, shadow, _order = make_control(hass, _pool())
    shadow.pool_action("heat")
    await looks(control, NOW, NOW + 5 * MIN)
    assert devices.writes(PUMP) == [] and control.pool.hp.reason == "foreign_mode"
    # The circulation is never turned off under a pump that may run.
    shadow.pool_action("off")
    await looks(control, NOW + 6 * MIN, NOW + 50 * MIN, 5 * MIN)
    assert devices.writes(CIRC) == []


async def test_the_dial_is_written_once_per_change_in_fahrenheit_and_within_the_day_s_budget(hass: HomeAssistant) -> None:
    hass.config.units = __import__("homeassistant.util.unit_system", fromlist=["US_CUSTOMARY_SYSTEM"]).US_CUSTOMARY_SYSTEM
    devices = Devices(hass)
    devices.add(PUMP, "off", temperature=80, current_temperature=75, min_temp=54, max_temp=104)
    devices.add(CIRC, "on")
    control, shadow, _order = make_control(hass, _pool(target_c=28.0))
    shadow.pool_action("filter")
    await looks(control, NOW, NOW + 2 * MIN)
    assert [call for call in devices.writes(PUMP) if call[0] == "climate.set_temperature"] == [("climate.set_temperature", PUMP, 82)]
    for index, target in enumerate((27.0, 26.0, 25.0, 24.0, 23.0, 22.0, 21.0)):
        shadow.raw = _pool(target_c=target)
        await control.async_look(NOW + (3 + index) * MIN)
    sets = [call for call in devices.writes(PUMP) if call[0] == "climate.set_temperature"]
    assert len(sets) == 6 and control.pool.hp.reason == "write_cap"


async def test_core_tuya_s_pool_heat_pump_by_its_switch_and_number(hass: HomeAssistant) -> None:
    devices = Devices(hass)
    devices.add("switch.vp_switch", "off")
    devices.add("number.vp_temp_set", "26", min=15, max=40, unit_of_measurement="°C")
    devices.add(CIRC, "on")
    heat = _pool(target_c=28.0)
    heat["pool"]["heat_pump"] = {"entity_id": "switch.vp_switch", "shape": "switch_number", "switch_id": "switch.vp_switch", "number_id": "number.vp_temp_set"}
    control, shadow, _order = make_control(hass, heat)
    shadow.pool_action("heat")
    await looks(control, NOW, NOW + 2 * MIN)
    assert ("switch.turn_on", "switch.vp_switch", "on") in devices.writes()
    assert ("number.set_value", "number.vp_temp_set", 28.0) in devices.writes()
    assert not any(call[0].startswith("climate") for call in devices.writes())


async def test_a_cloud_heat_pump_follows_the_plan_slowly_and_never_steps_live(hass: HomeAssistant) -> None:
    er.async_get(hass).async_get_or_create("switch", "tuya", "vpswitch", suggested_object_id="vp_switch")
    devices = Devices(hass)
    devices.add("switch.vp_switch", "on")
    devices.add("number.vp_temp_set", "27", min=15, max=40)
    devices.add(CIRC, "on")
    heat = _pool()
    heat["pool"]["heat_pump"] = {"entity_id": "switch.vp_switch", "shape": "switch_number", "switch_id": "switch.vp_switch", "number_id": "number.vp_temp_set"}
    control, shadow, _order = make_control(hass, heat)
    shadow.pool_action("heat")
    await looks(control, NOW, NOW + 40 * MIN, MIN)
    assert control.pool.hp.reason == "cloud_slow"
    offers = control.pool.offers(NOW + 40 * MIN, control.config().pool, beyond=True, until_max=None)  # type: ignore[arg-type]
    assert LOAD_POOL_HEAT not in offers and LOAD_POOL_CIRCULATION in offers
    status = control.pool.status(NOW, control.config().pool)  # type: ignore[arg-type]
    assert status["heat_pump"]["live"] is False and status["circulation"]["live"] is True


async def test_the_shed_order_s_live_steps(hass: HomeAssistant) -> None:
    devices = _devices(hass)
    control, shadow, order = make_control(hass, _pool())
    shadow.pool_action("heat")
    moment = await looks(control, NOW, NOW + 40 * MIN, MIN)
    pool = control.config().pool
    assert pool is not None
    offers = control.pool.offers(moment, pool, beyond=True, until_max=None)
    assert offers[LOAD_POOL_HEAT].useful and offers[LOAD_POOL_HEAT].live and offers[LOAD_POOL_HEAT].give_w == 2000.0
    assert offers[LOAD_POOL_CIRCULATION].useful and offers[LOAD_POOL_CIRCULATION].give_w == 2500.0
    # Not beyond the need, and no room before its deadline: the circulation is not ready.
    short = control.pool.offers(moment, pool, beyond=False, until_max=moment - MIN)
    assert not short[LOAD_POOL_CIRCULATION].ready and short[LOAD_POOL_CIRCULATION].why == "need"
    # The heat pump's step: off, and held once it reads off; the circulation goes on filtering.
    order.steps[LOAD_POOL_HEAT] = moment + 30 * MIN
    await control.async_look(moment + 30 * S)
    await control.async_look(moment + MIN)
    assert devices.state(PUMP) == "off" and devices.state(CIRC) == "on"
    assert control.pool.offers(moment + MIN, pool, beyond=True, until_max=None)[LOAD_POOL_HEAT].held
    # The circulation's step takes the heat pump with it; held after the run-on (a slow step, `pending`).
    order.steps.pop(LOAD_POOL_HEAT)
    order.steps[LOAD_POOL_CIRCULATION] = moment + 60 * MIN
    await control.async_look(moment + 2 * MIN)
    offer = control.pool.offers(moment + 2 * MIN, pool, beyond=True, until_max=None)[LOAD_POOL_CIRCULATION]
    assert offer.pending and not offer.held
    await looks(control, moment + 3 * MIN, moment + 9 * MIN)
    assert devices.state(CIRC) == "off"
    assert control.pool.offers(moment + 9 * MIN, pool, beyond=True, until_max=None)[LOAD_POOL_CIRCULATION].held


async def test_an_automation_that_steers_the_pump_keeps_spotnav_off_it(hass: HomeAssistant, monkeypatch) -> None:
    devices = _devices(hass)
    control, shadow, _order = make_control(hass, _pool())
    found = type("Found", (), {"entity_id": "automation.pool", "name": "Pool"})()
    monkeypatch.setattr(control_module, "steering_automations", lambda _hass, ids: [found] if CIRC in ids else [])
    shadow.pool_action("filter")
    await looks(control, NOW, NOW + 5 * MIN)
    assert devices.writes(CIRC) == [] and control.pool.circ.reason == "automation"


async def test_a_climate_without_heat_and_off_is_never_written(hass: HomeAssistant) -> None:
    devices = Devices(hass)
    devices.add(PUMP, "auto", temperature=27, hvac_modes=["auto", "cool"])
    devices.add(CIRC, "on")
    control, shadow, _order = make_control(hass, _pool())
    shadow.pool_action("heat")
    await looks(control, NOW, NOW + 3 * MIN)
    assert devices.writes(PUMP) == [] and control.pool.hp.reason == "unsupported"


async def test_an_exit_never_leaves_the_heat_pump_on_without_circulation(hass: HomeAssistant) -> None:
    devices = _devices(hass, mode="heat", circ="on")
    # The person had the heat pump on; SpotNav holds it on, then the circulation stops and the opt-in goes off.
    control, shadow, _order = make_control(hass, _pool(control_circulation=False))
    shadow.pool_action("heat")
    await looks(control, NOW, NOW + MIN)
    held = control.pool.hp.held("heat_pump")
    held.written = "heat"
    devices.set(CIRC, "off")
    shadow.raw = _pool(control_circulation=False, control_heat_pump=False)
    await looks(control, NOW + 2 * MIN, NOW + 4 * MIN)
    assert devices.state(PUMP) == "off" and devices.writes(PUMP) == [("climate.set_hvac_mode", PUMP, "off")]
    assert not control.pool.hp.holds()
