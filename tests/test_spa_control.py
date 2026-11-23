"""SpotNav setting the spa and the sauna (`spa/control.py`, spec stages 12 to 14): Balboa by its presets (its hvac
`off` is Rest), the bath temperature once per change of the dial, the spa's floor before the plan, the shed order and
the budget, Gecko by its water heater only, MSpa watched while its own schedule is set, the on/off families as the
idle thermostat, the sauna only for a booked session, never while locked, never past its longest run, never through a
relay without its declared safety and never turned on by a restore; cloud families never step live. Every write goes
to a fake service; nothing reaches a device."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.spotnav.loads.hold_part import STATE_CONTROLLED, STATE_HANDED_BACK, STATE_OFF, STATE_PAUSED
from custom_components.spotnav.pool.rules import LOAD_SAUNA, LOAD_SPA
from custom_components.spotnav.spa.control import LOAD_SPA_IDLE, SAUNA_MAX_ON

from .pool_spa_fakes import H, MIN, NOW, S, Devices, looks, make_control

SPA = "climate.spa"
SAUNA = "climate.bastu"


def _spa(family: str = "balboa", entity_id: str = SPA, **extra: Any) -> dict[str, Any]:
    return {"spa": {"entity_id": entity_id, "family": family, "bath_c": 38.0, "idle_c": 30.0, "floor_c": 10.0, "heater_kw": 3.0, "control": True, **extra}}


def _balboa(hass: HomeAssistant, *, preset: str = "rest", setpoint: float = 37.0) -> Devices:
    devices = Devices(hass)
    devices.add(SPA, "off" if preset == "rest" else "heat", preset_mode=preset, temperature=setpoint, current_temperature=31.0, min_temp=26.5, max_temp=40.0)
    return devices


async def test_nothing_is_written_without_the_spa_s_opt_in(hass: HomeAssistant) -> None:
    devices = _balboa(hass)
    control, shadow, _order = make_control(hass, _spa(control=False))
    shadow.spa.set("heat", current=31.0)
    await looks(control, NOW, NOW + 5 * MIN)
    assert devices.calls == [] and control.spa.part.state == STATE_OFF


async def test_balboa_heats_by_ready_rests_by_rest_and_never_by_hvac_off(hass: HomeAssistant) -> None:
    devices = _balboa(hass)
    control, shadow, _order = make_control(hass, _spa())
    shadow.spa.set("heat", current=31.0)
    await looks(control, NOW, NOW + MIN)
    assert ("climate.set_preset_mode", SPA, "ready") in devices.writes()
    # The bath temperature once, as the dial changed it.
    assert [call for call in devices.writes() if call[0] == "climate.set_temperature"] == [("climate.set_temperature", SPA, 38.0)]
    shadow.spa.set("idle", current=38.0)
    await looks(control, NOW + 2 * MIN, NOW + 3 * MIN)
    assert devices.writes()[-1] == ("climate.set_preset_mode", SPA, "rest")
    assert not any(call[0] == "climate.set_hvac_mode" for call in devices.writes())
    assert control.spa.part.state == STATE_CONTROLLED
    # The idle hold: Ready again only under the idle temperature less a degree.
    shadow.spa.set("idle", current=29.5)
    await control.async_look(NOW + 4 * MIN)
    assert devices.writes()[-1] == ("climate.set_preset_mode", SPA, "rest")
    shadow.spa.set("idle", current=28.9)
    await control.async_look(NOW + 5 * MIN)
    assert devices.writes()[-1] == ("climate.set_preset_mode", SPA, "ready")


async def test_the_floor_comes_before_the_shed_and_the_budget(hass: HomeAssistant) -> None:
    devices = _balboa(hass)
    control, shadow, order = make_control(hass, _spa())
    order.steps[LOAD_SPA_IDLE] = NOW + 5 * H
    # The day's preset budget spent by ups and downs.
    moment = NOW
    for index in range(9):
        shadow.spa.set("heat" if index % 2 == 0 else "idle", current=35.0)
        moment = await looks(control, moment, moment + MIN)
        moment += MIN
    presets = [call for call in devices.writes() if call[0] == "climate.set_preset_mode"]
    assert len(presets) == 6 and control.spa.part.reason == "write_cap"
    shadow.spa.set("idle", current=9.5)
    await control.async_look(moment + MIN)
    assert devices.writes()[-1] == ("climate.set_preset_mode", SPA, "ready") and control.spa.part.reason == "floor"
    # The idle step is not ready at the floor (the step ends by the rule).
    offers = control.spa.offers(moment + MIN, control.config().spa)
    assert not offers[LOAD_SPA_IDLE].ready and offers[LOAD_SPA_IDLE].why == "floor"


async def test_a_booked_bath_and_the_idle_hold_step_live_and_a_hand_change_hands_back(hass: HomeAssistant) -> None:
    devices = _balboa(hass)
    control, shadow, order = make_control(hass, _spa())
    shadow.spa.set("heat", current=33.0)
    await looks(control, NOW, NOW + MIN)
    offers = control.spa.offers(NOW + MIN, control.config().spa)
    assert offers[LOAD_SPA].live and offers[LOAD_SPA].useful and offers[LOAD_SPA].give_w == 3000.0
    order.steps[LOAD_SPA] = NOW + H
    await looks(control, NOW + 90 * S, NOW + 2 * MIN)
    assert devices.writes()[-1] == ("climate.set_preset_mode", SPA, "rest")
    assert control.spa.offers(NOW + 2 * MIN, control.config().spa)[LOAD_SPA].held
    order.steps.clear()
    await looks(control, NOW + 3 * MIN, NOW + 4 * MIN)
    assert devices.writes()[-1] == ("climate.set_preset_mode", SPA, "ready")
    # The person sets Rest on the panel: theirs to the next day, nothing written back.
    devices.set(SPA, "off", preset_mode="rest")
    count = len(devices.calls)
    await looks(control, NOW + 5 * MIN, NOW + 8 * MIN)
    assert len(devices.calls) == count and control.spa.part.state == STATE_HANDED_BACK


async def test_the_spa_s_values_come_back_at_the_opt_out(hass: HomeAssistant) -> None:
    devices = _balboa(hass, preset="rest", setpoint=36.0)
    control, shadow, _order = make_control(hass, _spa())
    shadow.spa.set("heat", current=33.0)
    await looks(control, NOW, NOW + MIN)
    assert devices.attr(SPA, "preset_mode") == "ready" and devices.attr(SPA, "temperature") == 38.0
    shadow.raw = _spa(control=False)
    await looks(control, NOW + 2 * MIN, NOW + 3 * MIN)
    assert devices.attr(SPA, "preset_mode") == "rest" and devices.attr(SPA, "temperature") == 36.0
    assert not control.holds_any()


async def test_gecko_is_set_by_its_water_heater_only(hass: HomeAssistant) -> None:
    devices = Devices(hass)
    devices.add("water_heater.gecko", "Heating", temperature=36.0, current_temperature=31.0, min_temp=15, max_temp=40)
    devices.add("climate.gecko", "heat", preset_mode="Standard", temperature=36.0, current_temperature=31.0)
    control, shadow, _order = make_control(hass, _spa("gecko", "water_heater.gecko"))
    shadow.spa.set("heat", current=31.0)
    await looks(control, NOW, NOW + MIN)
    shadow.spa.set("idle", current=38.0)
    await looks(control, NOW + 2 * MIN, NOW + 3 * MIN)
    assert devices.writes() == [("water_heater.set_temperature", "water_heater.gecko", 38.0), ("water_heater.set_temperature", "water_heater.gecko", 30.0)]
    # Never below the idle temperature or the floor, and never the climate's water care.
    control2, shadow2, _order2 = make_control(hass, _spa("gecko", "climate.gecko"))
    shadow2.spa.set("heat", current=31.0)
    await looks(control2, NOW, NOW + 2 * MIN)
    assert devices.writes("climate.gecko") == [] and control2.spa.part.reason == "unsupported"


async def test_mspa_is_watched_while_its_own_schedule_is_set(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(domain="mspa")
    entry.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(config_entry_id=entry.entry_id, identifiers={("mspa", "tub")})
    registry = er.async_get(hass)
    registry.async_get_or_create("climate", "mspa", "tub", device_id=device.id, suggested_object_id="mspa")
    registry.async_get_or_create("datetime", "mspa", "mspa_scheduled_ready_at_tub", device_id=device.id, suggested_object_id="mspa_scheduled")
    devices = Devices(hass)
    devices.add("climate.mspa", "off", temperature=38.0, current_temperature=30.0, min_temp=20, max_temp=40)
    hass.states.async_set("datetime.mspa_scheduled", "2026-07-01T18:00:00+00:00")
    control, shadow, _order = make_control(hass, _spa("mspa", "climate.mspa"))
    shadow.spa.set("heat", current=30.0)
    await looks(control, NOW, NOW + 2 * MIN)
    assert devices.calls == [] and control.spa.part.reason == "own_scheduler"
    hass.states.async_set("datetime.mspa_scheduled", "unknown")
    await looks(control, NOW + 3 * MIN, NOW + 4 * MIN)
    assert devices.writes() == [("climate.set_hvac_mode", "climate.mspa", "heat")]
    # A cloud family: slow planned actions only, never a live step.
    assert control.spa.part.reason == "cloud_slow" and control.spa.offers(NOW + 4 * MIN, control.config().spa) == {}


async def test_an_on_off_spa_keeps_its_idle_temperature_as_a_thermostat(hass: HomeAssistant) -> None:
    devices = Devices(hass)
    devices.add("climate.intex", "off", temperature=38.0, current_temperature=30.0, min_temp=20, max_temp=40)
    control, shadow, _order = make_control(hass, _spa("intex_spa", "climate.intex"))
    shadow.spa.set("idle", current=29.5)
    await looks(control, NOW, NOW + MIN)
    assert devices.writes() == []
    shadow.spa.set("idle", current=28.8)
    await looks(control, NOW + 2 * MIN, NOW + 3 * MIN)
    assert devices.writes() == [("climate.set_hvac_mode", "climate.intex", "heat")]
    shadow.spa.set("idle", current=29.6)
    await control.async_look(NOW + 4 * MIN)
    assert len(devices.writes()) == 1
    shadow.spa.set("idle", current=30.0)
    await control.async_look(NOW + 5 * MIN)
    assert devices.writes()[-1] == ("climate.set_hvac_mode", "climate.intex", "off")


async def test_tuya_local_s_intex_water_heater_by_its_operation(hass: HomeAssistant) -> None:
    devices = Devices(hass)
    devices.add("water_heater.purespa", "off", temperature=100, current_temperature=86, min_temp=68, max_temp=104)
    hass.config.units = __import__("homeassistant.util.unit_system", fromlist=["US_CUSTOMARY_SYSTEM"]).US_CUSTOMARY_SYSTEM
    control, shadow, _order = make_control(hass, _spa("tuya_intex", "water_heater.purespa"))
    shadow.spa.set("heat", current=30.0)
    await looks(control, NOW, NOW + MIN)
    assert ("water_heater.set_operation_mode", "water_heater.purespa", "electric") in devices.writes()
    # The dial's 38 °C is the 100 °F already set: nothing written; 40 °C is written as 104 °F.
    assert [call for call in devices.writes() if call[0] == "water_heater.set_temperature"] == []
    shadow.raw = _spa("tuya_intex", "water_heater.purespa", bath_c=40.0)
    await control.async_look(NOW + 2 * MIN)
    assert devices.writes()[-1] == ("water_heater.set_temperature", "water_heater.purespa", 104)


# ---- the sauna


def _sauna(family: str = "saunum", entity_id: str = SAUNA, **extra: Any) -> dict[str, Any]:
    return {"sauna": {"entity_id": entity_id, "family": family, "target_c": 80, "heater_kw": 9.0, "control": True, **extra}}


def _bastu(hass: HomeAssistant, state: str = "off") -> Devices:
    devices = Devices(hass)
    devices.add(SAUNA, state, temperature=80, current_temperature=22, min_temp=40, max_temp=110, hvac_modes=["off", "heat"])
    return devices


async def test_the_sauna_heats_only_for_a_booked_session(hass: HomeAssistant) -> None:
    devices = _bastu(hass)
    control, shadow, _order = make_control(hass, _sauna())
    # The plan heating without a session is never a start.
    shadow.sauna.set("heat", current=22.0, session=None)
    await looks(control, NOW, NOW + 2 * MIN)
    assert devices.calls == []
    shadow.sauna.set("heat", current=22.0, session=NOW + H)
    await looks(control, NOW + 3 * MIN, NOW + 4 * MIN)
    assert devices.writes() == [("climate.set_hvac_mode", SAUNA, "heat")]
    shadow.sauna.set("off", current=80.0, session=None)
    await looks(control, NOW + 5 * MIN, NOW + 6 * MIN)
    assert devices.writes()[-1] == ("climate.set_hvac_mode", SAUNA, "off")


async def test_a_session_the_person_started_is_never_turned_off(hass: HomeAssistant) -> None:
    devices = _bastu(hass, "heat")
    control, shadow, _order = make_control(hass, _sauna())
    shadow.sauna.set("off", current=60.0)
    await looks(control, NOW, NOW + 10 * MIN)
    assert devices.calls == []


async def test_the_sauna_never_starts_while_locked_and_stops_when_the_lock_reads(hass: HomeAssistant) -> None:
    devices = _bastu(hass)
    control, shadow, _order = make_control(hass, _sauna())
    shadow.sauna.set("heat", current=22.0, session=NOW + H, locked=True)
    await looks(control, NOW, NOW + 2 * MIN)
    assert devices.calls == [] and control.sauna.part.reason == "lock"
    shadow.sauna.set("heat", current=22.0, session=NOW + H, locked=False)
    await looks(control, NOW + 3 * MIN, NOW + 4 * MIN)
    shadow.sauna.set("heat", current=40.0, session=NOW + H, locked=True)
    await control.async_look(NOW + 5 * MIN)
    assert devices.writes() == [("climate.set_hvac_mode", SAUNA, "heat"), ("climate.set_hvac_mode", SAUNA, "off")]


async def test_the_sauna_never_runs_past_its_longest_run(hass: HomeAssistant) -> None:
    devices = _bastu(hass)
    control, shadow, _order = make_control(hass, _sauna("tylo_sauna"))
    session = NOW + 5 * H
    shadow.sauna.set("hold", current=80.0, session=session, max_on=90 * MIN)
    moment = await looks(control, NOW, NOW + 95 * MIN, MIN)
    assert devices.writes() == [("climate.set_hvac_mode", SAUNA, "heat"), ("climate.set_hvac_mode", SAUNA, "off")]
    assert control.sauna.part.reason == "max_on"
    # Not again for the same session.
    await looks(control, moment + MIN, moment + 20 * MIN, MIN)
    assert len(devices.writes()) == 2
    # Without a longest run of its own, SpotNav's cap.
    assert SAUNA_MAX_ON <= 3 * H


async def test_a_relay_sauna_needs_its_declared_safety(hass: HomeAssistant) -> None:
    devices = Devices(hass)
    devices.add("switch.bastu", "off")
    # Not declared: the settings never hold the opt-in, nothing is written.
    control, shadow, _order = make_control(hass, _sauna("relay", "switch.bastu"))
    shadow.sauna.set("heat", current=None, session=NOW + H)
    await looks(control, NOW, NOW + 2 * MIN)
    assert devices.calls == [] and control.sauna.part.state == STATE_OFF
    control2, shadow2, _order2 = make_control(hass, _sauna("relay", "switch.bastu", safety_declared=True))
    shadow2.sauna.set("heat", current=None, session=NOW + H)
    await looks(control2, NOW, NOW + MIN)
    assert devices.writes() == [("switch.turn_on", "switch.bastu", "on")]


async def test_a_restore_never_turns_a_sauna_on(hass: HomeAssistant) -> None:
    devices = _bastu(hass, "heat")
    control, shadow, _order = make_control(hass, _sauna())
    # The person's own session at the opt-in; the plan's off was SpotNav's last write... then the opt-out.
    shadow.sauna.set("heat", current=60.0, session=NOW + H)
    await looks(control, NOW, NOW + MIN)
    held = control.sauna.part.held("heat")
    held.person, held.written = "heat", "off"
    devices.set(SAUNA, "off")
    shadow.raw = _sauna(control=False)
    await looks(control, NOW + 2 * MIN, NOW + 5 * MIN)
    assert devices.calls == [] and not control.holds_any()


async def test_the_sauna_s_live_step_and_a_cloud_sauna_never_steps(hass: HomeAssistant) -> None:
    devices = _bastu(hass)
    control, shadow, order = make_control(hass, _sauna())
    shadow.sauna.set("heat", current=30.0, session=NOW + H)
    await looks(control, NOW, NOW + MIN)
    offers = control.sauna.offers(NOW + MIN, control.config().sauna)
    assert offers[LOAD_SAUNA].useful and offers[LOAD_SAUNA].give_w == 9000.0
    order.steps[LOAD_SAUNA] = NOW + H
    await looks(control, NOW + 90 * S, NOW + 2 * MIN)
    assert devices.writes()[-1] == ("climate.set_hvac_mode", SAUNA, "off")
    assert control.sauna.offers(NOW + 2 * MIN, control.config().sauna)[LOAD_SAUNA].held
    huum, huum_shadow, _o = make_control(hass, _sauna("huum"))
    huum_shadow.sauna.set("heat", current=30.0, session=NOW + H)
    await looks(huum, NOW + 3 * MIN, NOW + 4 * MIN)
    assert huum.sauna.part.reason == "cloud_slow" and huum.sauna.offers(NOW + 4 * MIN, huum.config().sauna) == {}


async def test_a_paused_spa_is_paused_and_says_until_when(hass: HomeAssistant) -> None:
    devices = _balboa(hass)
    devices.ignore.add(SPA)
    control, shadow, _order = make_control(hass, _spa())
    shadow.spa.set("heat", current=33.0)
    await looks(control, NOW, NOW + 3 * MIN)
    status = control.spa.status(NOW + 3 * MIN, control.config().spa)
    assert status["state"] == STATE_PAUSED and status["reason"] == "no_read_back" and status["failures"] >= 1
    assert status["paused_until"] is not None
