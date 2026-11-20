"""SpotNav switching a plain water heater on the pilot's site (`heat/water_heater_control.py`): watched until the opt-in,
then switched inside the legionella rules, read back and checked, never against the person, on at every exit, at the
stop and at the next start. Every write goes to a fake switch service in the test; nothing reaches a device."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest
from homeassistant.core import HomeAssistant

from custom_components.spotnav.const import CONF_HEAT
from custom_components.spotnav.heat.water_heater import GIVE_UP_AFTER, Kept
from custom_components.spotnav.heat.water_heater_control import WaterHeaterControl
from custom_components.spotnav.runtime import site_controller_for

from .test_demand_site import pilot_site
from .test_heat_control import Relay

pytestmark = pytest.mark.usefixtures("offline_relay")

HEATER = "switch.varmvatten"
MIN = timedelta(minutes=1)
H = timedelta(hours=1)


async def _site(hass: HomeAssistant, freezer: Any, *, control: bool = True) -> tuple[Any, WaterHeaterControl, Relay, datetime]:
    site = await pilot_site(hass, freezer)
    switch = Relay(hass, HEATER)
    hass.states.async_set(HEATER, "on")
    config = {"water_heater": {"entity_id": HEATER, "control": control, "tank_sensor": None}}
    hass.config_entries.async_update_entry(site, data={**site.data, CONF_HEAT: config})
    controller = site_controller_for(hass, site.entry_id)
    assert controller is not None
    await hass.async_block_till_done()
    heater: WaterHeaterControl = controller.water_heater_control
    now = controller.demand.now().replace(minute=0, second=0, microsecond=0) + H
    # Prices for two days: every hour dear, so outside the windows it is off.
    heater._prices = lambda _now: {now - 12 * H + i * H: 100.0 for i in range(60)}  # type: ignore[method-assign]  # noqa: SLF001
    switch.calls.clear()
    return site, heater, switch, now


def _outside_windows(heater: WaterHeaterControl, now: datetime) -> datetime:
    """A moment at or after `now` outside every window, as the control plans them."""
    moment = now
    while any(window.start <= moment < window.end for window in heater.rules.kept.windows):
        moment += 15 * MIN
    return moment


async def test_watched_only_without_the_opt_in(hass: HomeAssistant, freezer) -> None:
    _site_, heater, switch, now = await _site(hass, freezer, control=False)
    for minute in range(0, 60, 10):
        await heater.async_look(now + minute * MIN)
    assert switch.calls == []
    status = heater.status()
    assert status is not None and status["state"] == "off" and status["live"] is False and status["windows"]


async def test_off_outside_the_windows_read_back_and_checked_by_the_next_on(hass: HomeAssistant, freezer) -> None:
    _site_, heater, switch, now = await _site(hass, freezer)
    heater.rules.kept.last_full = now  # full heat just seen
    await heater.async_look(now)
    moment = _outside_windows(heater, now)
    await heater.async_look(moment)
    assert switch.calls == ["off"] and switch.state == "off" and heater.held_off
    await heater.async_look(moment + MIN)
    assert heater.status()["state"] == "verifying"  # type: ignore[index]
    # The next window turns it on: the check has passed.
    window = next(window for window in heater.rules.kept.windows if window.start > moment)
    await heater.async_look(window.start)
    assert switch.calls == ["off", "on"]
    await heater.async_look(window.start + MIN)
    status = heater.status()
    assert status is not None and status["check"] == "passed" and status["state"] == "running" and status["live"]
    # Never off over eight hours in a row, whatever the windows say.
    heater.rules.kept.windows = []
    assert heater.rules.kept.off_since is None


async def test_h1_holds_it_on_until_full_heat_is_seen(hass: HomeAssistant, freezer) -> None:
    _site_, heater, switch, now = await _site(hass, freezer)
    # Metered: the element idle (its thermostat not calling yet), the last full heat 21 hours ago.
    heater._power = lambda _entity: ("sensor.varmvatten_effekt", 3.0)  # type: ignore[method-assign]  # noqa: SLF001
    heater.rules.kept.last_full = now - 21 * H
    for minute in range(0, 120, 10):
        await heater.async_look(_outside_windows(heater, now) + minute * MIN)
    assert switch.calls == [] and heater.status()["reason"] == "full_heat"  # type: ignore[index]


async def test_the_person_s_on_is_never_cut(hass: HomeAssistant, freezer) -> None:
    _site_, heater, switch, now = await _site(hass, freezer)
    heater.rules.kept.last_full = now
    await heater.async_look(now)
    moment = _outside_windows(heater, now)
    await heater.async_look(moment)
    await heater.async_look(moment + MIN // 2)
    assert switch.state == "off" and heater.held_off
    hass.states.async_set(HEATER, "on")
    await heater.async_look(moment + MIN)
    await heater.async_look(moment + 3 * MIN)
    status = heater.status()
    assert status is not None and status["reason"] == "foreign_on" and status["paused_until"] is not None
    for minute in range(5, 60, 10):
        await heater.async_look(moment + minute * MIN)
    assert switch.calls == ["off"] and switch.state == "on" and not heater.held_off


async def test_the_person_s_off_is_left_until_the_next_day(hass: HomeAssistant, freezer) -> None:
    _site_, heater, switch, now = await _site(hass, freezer)
    heater._power = lambda _entity: ("sensor.varmvatten_effekt", 3.0)  # type: ignore[method-assign]  # noqa: SLF001
    heater.rules.kept.last_full = now - 21 * H  # held on by H1
    await heater.async_look(now)
    await heater.async_look(now + MIN)
    hass.states.async_set(HEATER, "off")
    await heater.async_look(now + 2 * MIN)
    await heater.async_look(now + 4 * MIN)
    assert heater.status()["reason"] == "foreign_off"  # type: ignore[index]
    await heater.async_look(now + 30 * MIN)
    assert switch.calls == [] and switch.state == "off"


async def test_an_unreadable_switch_is_not_written_and_turned_on_when_it_reads(hass: HomeAssistant, freezer) -> None:
    _site_, heater, switch, now = await _site(hass, freezer)
    heater.rules.kept.last_full = now
    await heater.async_look(now)
    moment = _outside_windows(heater, now)
    await heater.async_look(moment)
    hass.states.async_set(HEATER, "unavailable")
    heater._prices = lambda _now: {}  # type: ignore[method-assign]  # noqa: SLF001 - no prices: on
    await heater.async_look(moment + MIN)
    assert switch.calls == ["off"]
    hass.states.async_set(HEATER, "off")
    await heater.async_look(moment + 2 * MIN)
    assert switch.calls == ["off", "on"] and switch.state == "on"


async def test_turning_the_opt_in_off_turns_it_on(hass: HomeAssistant, freezer) -> None:
    site, heater, switch, now = await _site(hass, freezer)
    heater.rules.kept.last_full = now
    await heater.async_look(now)
    moment = _outside_windows(heater, now)
    await heater.async_look(moment)
    heat = dict(site.data[CONF_HEAT])
    heat["water_heater"] = {"entity_id": HEATER, "control": False, "tank_sensor": None}
    hass.config_entries.async_update_entry(site, data={**site.data, CONF_HEAT: heat})
    await heater.async_look(moment + MIN)
    assert switch.calls == ["off", "on"]


async def test_on_at_the_stop_and_at_the_next_start(hass: HomeAssistant, freezer) -> None:
    _site_, heater, switch, now = await _site(hass, freezer)
    heater.rules.kept.last_full = now
    await heater.async_look(now)
    moment = _outside_windows(heater, now)
    await heater.async_look(moment)
    await heater.async_shutdown()
    assert switch.calls == ["off", "on"] and not heater.held_off
    # A stop that could not: the next start turns it on before anything else.
    hass.states.async_set(HEATER, "off")
    fresh = WaterHeaterControl(hass, heater.shadow, heater.control)
    fresh.restore({"entity": HEATER, "held_off": True, "rules": Kept(last_full=now).as_dict()})
    fresh._prices = heater._prices  # type: ignore[method-assign]  # noqa: SLF001
    fresh._started = True  # noqa: SLF001
    await fresh.async_look(moment + 5 * MIN)
    assert switch.calls[-1] == "on"


async def test_an_off_that_does_not_cut_the_element_fails_the_check(hass: HomeAssistant, freezer) -> None:
    _site_, heater, switch, now = await _site(hass, freezer)
    heater.rules.kept.last_full = now
    power = {"w": 2500.0}
    heater._power = lambda _entity: ("sensor.varmvatten_effekt", power["w"])  # type: ignore[method-assign]  # noqa: SLF001
    heater.rules.kept.last_full = now
    await heater.async_look(now)
    moment = _outside_windows(heater, now)
    await heater.async_look(moment)
    assert switch.calls == ["off"]
    await heater.async_look(moment + MIN)
    await heater.async_look(moment + 4 * MIN)
    status = heater.status()
    assert status is not None and status["check"] == "failed" and status["reason"] == "check_failed"
    assert switch.calls == ["off", "on"]
    await heater.async_look(moment + 30 * MIN)
    assert switch.calls == ["off", "on"]


async def test_two_days_without_full_heat_stops_control_with_one_note(hass: HomeAssistant, freezer) -> None:
    _site_, heater, switch, now = await _site(hass, freezer)
    heater._power = lambda _entity: ("sensor.varmvatten_effekt", 3.0)  # type: ignore[method-assign]  # noqa: SLF001
    heater.rules.kept.last_full = now - 3 * GIVE_UP_AFTER
    await heater.async_look(now)
    await heater.async_look(now + GIVE_UP_AFTER + MIN)
    assert heater.status()["reason"] == "not_full" and heater.not_full()  # type: ignore[index]
    assert switch.state == "on"


async def test_its_state_is_kept_in_the_heat_control_s_store(hass: HomeAssistant, freezer) -> None:
    _site_, heater, _switch, now = await _site(hass, freezer)
    heater.rules.kept.last_full = now
    await heater.async_look(now)
    await heater.async_look(_outside_windows(heater, now))
    data = heater.control._data()  # noqa: SLF001
    assert data["water_heater"]["entity"] == HEATER and data["water_heater"]["held_off"] is True


async def test_the_heat_card_and_the_diagnostics_show_the_water_heater_and_a_controlled_zone(hass: HomeAssistant, freezer) -> None:
    from custom_components.spotnav.api.heat import heat_settings_diagnostics

    from .test_heat_api import read

    site, heater, _switch, now = await _site(hass, freezer)
    hass.states.async_set("climate.badrum", "heat", {"temperature": 22.0, "current_temperature": 22.0, "supported_features": 1})
    heat = dict(site.data[CONF_HEAT])
    heat["zones"] = [{"entity_id": "climate.badrum", "offset_c": 0.0, "control": True}]
    hass.config_entries.async_update_entry(site, data={**site.data, CONF_HEAT: heat})
    heater.rules.kept.last_full = now
    await heater.async_look(now)
    result = (await read(hass, api_version=1, site_id=site.entry_id))["result"]
    assert result["water_heater"]["entity_id"] == HEATER and result["water_heater"]["windows"]
    assert result["zones"][0]["control"]["enabled"] is True
    assert {item["entity_id"] for item in result["choices"]["water_heater"]} >= {HEATER}
    assert "tank_sensor" in result["choices"]
    diagnostics = heat_settings_diagnostics(hass, site)
    assert diagnostics["control"]["water_heater"]["mode"] == "unmetered"
    assert "zones" in diagnostics["control"]
