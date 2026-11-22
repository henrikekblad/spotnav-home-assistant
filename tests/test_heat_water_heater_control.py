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
    heater.meter_seen = {"entity": HEATER, "sensor": "sensor.varmvatten_effekt"}  # the element has been seen drawing
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


async def test_another_switch_chosen_while_held_off_turns_the_old_one_on_until_it_reads_on(hass: HomeAssistant, freezer) -> None:
    site, heater, switch, now = await _site(hass, freezer)
    heater.rules.kept.last_full = now
    await heater.async_look(now)
    moment = _outside_windows(heater, now)
    await heater.async_look(moment)
    assert switch.calls == ["off"] and heater.held_off
    hass.states.async_set("switch.annan", "on")
    heat = dict(site.data[CONF_HEAT])
    heat["water_heater"] = {"entity_id": "switch.annan", "control": False, "tank_sensor": None}
    hass.config_entries.async_update_entry(site, data={**site.data, CONF_HEAT: heat})
    # The first on is lost: the old switch stays SpotNav's to turn on, and the on is written again.
    switch.ignore = True
    await heater.async_look(moment + MIN)
    assert switch.calls == ["off", "on"] and heater.entity == HEATER and heater.held_off
    await heater.async_look(moment + 2 * MIN)
    assert switch.calls == ["off", "on"]
    switch.ignore = False
    await heater.async_look(moment + 3 * MIN)
    assert switch.calls == ["off", "on", "on"] and switch.state == "on"
    # Read on: let go, and the new switch is the one looked at.
    await heater.async_look(moment + 4 * MIN)
    assert heater.entity == "switch.annan" and not heater.held_off
    assert switch.calls == ["off", "on", "on"]


async def test_a_look_that_fails_turns_it_on_and_keeps_trying(hass: HomeAssistant, freezer) -> None:
    _site_, heater, switch, now = await _site(hass, freezer)
    heater.rules.kept.last_full = now
    await heater.async_look(now)
    moment = _outside_windows(heater, now)
    await heater.async_look(moment)
    await heater.async_look(moment + MIN // 2)
    assert switch.calls == ["off"] and heater.held_off

    def broken(_now: datetime) -> dict[datetime, float]:
        raise RuntimeError("no prices")

    heater._prices = broken  # type: ignore[method-assign]  # noqa: SLF001
    switch.ignore = True
    await heater.async_look(moment + MIN)
    await heater.async_look(moment + 2 * MIN)
    assert switch.calls == ["off", "on"] and heater.held_off
    # Not seen within the read-back: written again, look after look, until it reads on.
    await heater.async_look(moment + 3 * MIN)
    assert switch.calls == ["off", "on", "on"]
    switch.ignore = False
    await heater.async_look(moment + 5 * MIN)
    await heater.async_look(moment + 6 * MIN)
    assert switch.state == "on" and not heater.held_off


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
    heater.meter_seen = {"entity": HEATER, "sensor": "sensor.varmvatten_effekt"}
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


def _meter(heater: WaterHeaterControl, switch: Relay, on_w: float, off_w: float, sensor: str = "sensor.coil") -> None:
    """The device's meter reads `on_w` while the switch is on and `off_w` while it is off."""
    heater._power = lambda _entity: (sensor, on_w if switch.state == "on" else off_w)  # type: ignore[method-assign]  # noqa: SLF001


async def _switch_off_and_on(heater: WaterHeaterControl, now: datetime) -> datetime:
    """One of SpotNav's own offs (outside the windows) and the on that follows, each read back."""
    await heater.async_look(now)
    moment = _outside_windows(heater, now)
    await heater.async_look(moment)
    await heater.async_look(moment + MIN)
    await heater.async_look(moment + 2 * MIN)
    window = next(window for window in heater.rules.kept.windows if window.start > moment)
    await heater.async_look(window.start)
    await heater.async_look(window.start + MIN)
    await heater.async_look(window.start + 2 * MIN)
    return window.start + 2 * MIN


async def test_a_meter_is_trusted_only_once_it_follows_spotnav_s_switching(hass: HomeAssistant, freezer) -> None:
    _site_, heater, switch, now = await _site(hass, freezer)
    heater.rules.kept.last_full = now
    # A contactor's coil: a few watts, never the element.
    _meter(heater, switch, 4.0, 1.0)
    later = await _switch_off_and_on(heater, now)
    status = heater.status()
    assert status is not None
    assert status["metered"] is False and status["meter_waiting"] is True and status["mode"] == "unmetered"
    assert heater.not_full() is False
    # 2.5 kW seen once proves nothing.
    heater._power = lambda _entity: ("sensor.coil", 2500.0)  # type: ignore[method-assign]  # noqa: SLF001
    await heater.async_look(later + H)
    assert heater.metered is False
    # The element's meter: SpotNav's off takes it from 2.5 kW to nothing. Trusted, and kept through a restart.
    _meter(heater, switch, 2500.0, 1.0)
    heater.rules.kept.last_full = later
    later = await _switch_off_and_on(heater, later + H)
    assert heater.metered is True and heater.mode(heater.shadow.config()) == "metered"
    kept = heater.stored()
    assert kept["meter_seen"] == {"entity": HEATER, "sensor": "sensor.coil"}
    heater.meter_seen = None
    heater.restore(kept)
    assert heater.meter_seen == {"entity": HEATER, "sensor": "sensor.coil"}
    # Another meter has to follow SpotNav's switching itself.
    heater._power = lambda _entity: ("sensor.other", 1.0)  # type: ignore[method-assign]  # noqa: SLF001
    await heater.async_look(later + H)
    assert heater.metered is False and heater.meter_waiting is True and heater.meter_seen is None


async def test_another_channel_s_meter_is_never_trusted_and_the_night_window_stays(hass: HomeAssistant, freezer) -> None:
    """A two-channel device whose other channel draws 2.5 kW whatever this switch does: never trusted, so no full
    heat is ever seen on it and the unmetered night window (the hard one) is kept."""
    _site_, heater, switch, now = await _site(hass, freezer)
    heater.rules.kept.last_full = now
    _meter(heater, switch, 2500.0, 2500.0)
    later = await _switch_off_and_on(heater, now)
    await _switch_off_and_on(heater, later + H)
    assert heater.metered is False and heater.mode(heater.shadow.config()) == "unmetered"
    assert any(window.reason == "full_heat" for window in heater.rules.kept.windows)


async def test_a_trusted_meter_that_no_longer_follows_an_off_loses_the_trust(hass: HomeAssistant, freezer) -> None:
    _site_, heater, switch, now = await _site(hass, freezer)
    heater.rules.kept.last_full = now
    _meter(heater, switch, 2500.0, 1.0)
    later = await _switch_off_and_on(heater, now)
    assert heater.metered is True
    # The meter now reads the element whatever the switch does (re-wired, another channel): the next off shows it.
    _meter(heater, switch, 2500.0, 2500.0)
    heater.rules.kept.last_full = later
    moment = _outside_windows(heater, later + H)
    for minute in range(0, 6):
        await heater.async_look(moment + minute * MIN)
    assert heater.metered is False and heater.meter_seen is None
    assert any(entry["what"] == "meter_not_cut" for entry in heater.log)


async def test_a_frozen_meter_is_never_trusted(hass: HomeAssistant, freezer) -> None:
    """A meter that stopped reporting gives no reading (`read_power_w`): it cannot follow a switching."""
    _site_, heater, switch, now = await _site(hass, freezer)
    heater.rules.kept.last_full = now
    heater._power = lambda _entity: ("sensor.coil", None)  # type: ignore[method-assign]  # noqa: SLF001
    await _switch_off_and_on(heater, now)
    assert heater.metered is False and heater.meter_waiting is True

def _in_cheap_window(heater: WaterHeaterControl) -> datetime:
    """Five minutes into the first cheap window of two hours or more (a pause to the hour's end leaves it time)."""
    window = next(window for window in heater.rules.kept.windows if window.reason == "cheap" and window.end - window.start >= 2 * H)
    return window.start + 5 * MIN


POWER = "sensor.varmvatten_effekt"


def _metered(hass: HomeAssistant, heater: WaterHeaterControl, switch: Relay, draw_w: float = 3000.0) -> None:
    """A trusted meter on the switch's device: the element draws `draw_w` while the switch is on."""
    heater.meter_seen = {"entity": HEATER, "sensor": POWER}
    heater.daily = [1.5] * 5

    def power(_entity: str) -> tuple[str, float]:
        value = draw_w if switch.state == "on" else 2.0
        hass.states.async_set(POWER, str(value), {"unit_of_measurement": "W"})
        return POWER, value

    heater._power = power  # type: ignore[method-assign]  # noqa: SLF001


async def _checked(hass: HomeAssistant, heater: WaterHeaterControl, switch: Relay, now: datetime) -> datetime:
    """The check passed, full heat just seen, a trusted meter showing the element draw: a look inside a cheap window,
    on."""
    _metered(hass, heater, switch)
    heater.check = {"entity": HEATER, "result": "passed", "at": now.isoformat()}
    heater.rules.kept.last_full = now
    heater.kw = 3.0
    await heater.async_look(now)
    moment = _in_cheap_window(heater)
    heater.rules.kept.last_full = moment
    await heater.async_look(moment)
    return moment


async def test_the_shed_order_pauses_it_for_the_ceiling_and_puts_it_back(hass: HomeAssistant, freezer) -> None:
    from custom_components.spotnav.site.shed_order import LOAD_WATER_HEATER, Step

    site, heater, switch, now = await _site(hass, freezer)
    controller = site_controller_for(hass, site.entry_id)
    moment = await _checked(hass, heater, switch, now)
    assert switch.state == "on" and switch.calls == []
    offer = heater.shed_offer()
    assert offer is not None and offer.live and offer.ready and offer.give_w == 3000.0 and offer.drawing
    end = moment.replace(minute=0) + H
    controller.shed_order.steps[LOAD_WATER_HEATER] = Step(LOAD_WATER_HEATER, True, moment, end, 3000.0, end - H)
    await heater.async_look(moment + MIN)
    assert switch.calls == ["off"] and heater.held_off
    status = heater.status()
    assert status is not None and status["reason"] == "ceiling" and status["until"] == end.isoformat() and status["on"] is False
    # Still ready while paused (its rules allow it), no longer drawing.
    await heater.async_look(moment + 2 * MIN)
    offer = heater.shed_offer()
    assert offer is not None and offer.ready and not offer.drawing
    # Put back: on at the next look, inside its window.
    del controller.shed_order.steps[LOAD_WATER_HEATER]
    await heater.async_look(moment + 3 * MIN)
    await heater.async_look(moment + 4 * MIN)
    assert switch.calls == ["off", "on"] and not heater.held_off
    # Both switchings count toward the day's cap.
    assert sum(heater.rules.kept.switchings.values()) == 2


async def test_a_hard_rule_keeps_it_on_whatever_the_shed_order_asks(hass: HomeAssistant, freezer) -> None:
    from custom_components.spotnav.site.shed_order import LOAD_WATER_HEATER, Step

    site, heater, switch, now = await _site(hass, freezer)
    controller = site_controller_for(hass, site.entry_id)
    moment = await _checked(hass, heater, switch, now)
    end = moment.replace(minute=0) + H
    controller.shed_order.steps[LOAD_WATER_HEATER] = Step(LOAD_WATER_HEATER, True, moment, end, 3000.0, end - H)
    # Metered and full heat due (21 hours since the last): held on, and the offer says why.
    heater.meter_seen = {"entity": HEATER, "sensor": "sensor.varmvatten_effekt"}
    heater._power = lambda _entity: ("sensor.varmvatten_effekt", 2900.0)  # type: ignore[method-assign]  # noqa: SLF001
    heater.rules.kept.last_full = moment - 21 * H
    await heater.async_look(moment + MIN)
    assert switch.calls == []
    offer = heater.shed_offer()
    assert offer is not None and not offer.ready and offer.why == "full_heat"


async def test_a_watched_water_heater_is_only_offered_as_would(hass: HomeAssistant, freezer) -> None:
    from custom_components.spotnav.site.shed_order import LOAD_WATER_HEATER, Step

    site, heater, switch, now = await _site(hass, freezer, control=False)
    controller = site_controller_for(hass, site.entry_id)
    _metered(hass, heater, switch)
    heater.kw = 3.0
    await heater.async_look(now)
    moment = _in_cheap_window(heater)
    await heater.async_look(moment)
    offer = heater.shed_offer()
    assert offer is not None and not offer.live and offer.ready and offer.give_w == 3000.0
    end = moment.replace(minute=0) + H
    controller.shed_order.steps[LOAD_WATER_HEATER] = Step(LOAD_WATER_HEATER, False, moment, end, 3000.0, end - H)
    await heater.async_look(moment + MIN)
    assert switch.calls == []


async def test_a_learnt_power_is_never_offered_as_freed(hass: HomeAssistant, freezer) -> None:
    """Without a reading of its trusted meter (lost, or never trusted) the heater gives nothing: nothing is counted for
    the cars and no step is taken for power that may not be drawn."""
    _site_, heater, switch, now = await _site(hass, freezer)
    moment = await _checked(hass, heater, switch, now)
    assert heater.shed_offer().give_w == 3000.0  # type: ignore[union-attr]
    hass.states.async_set(POWER, "unavailable")
    offer = heater.shed_offer()
    assert offer is not None and offer.give_w == 0.0 and not offer.useful
    heater.meter_seen = None
    heater._power = lambda _entity: (None, None)  # type: ignore[method-assign]  # noqa: SLF001
    await heater.async_look(moment + MIN)
    assert heater.shed_offer().give_w == 0.0  # type: ignore[union-attr]


async def test_the_offer_says_no_where_the_day_s_switchings_are_spent(hass: HomeAssistant, freezer) -> None:
    _site_, heater, switch, now = await _site(hass, freezer)
    moment = await _checked(hass, heater, switch, now)
    heater.rules.kept.switchings[heater._day(moment)] = 7  # noqa: SLF001
    await heater.async_look(moment + MIN)
    offer = heater.shed_offer()
    assert offer is not None and not offer.ready and offer.why == "cheap"


async def test_no_pause_where_the_window_left_would_not_cover_the_need(hass: HomeAssistant, freezer) -> None:
    from custom_components.spotnav.site.shed_order import LOAD_WATER_HEATER, Step

    site, heater, switch, now = await _site(hass, freezer)
    controller = site_controller_for(hass, site.entry_id)
    moment = await _checked(hass, heater, switch, now)
    window = next(item for item in heater.rules.kept.windows if item.covers(moment))
    offer = heater.shed_offer()
    assert offer is not None and offer.until_max is not None and offer.until_max <= window.end
    # Asked to pause to after the latest that still covers the need: it stays on.
    late = offer.until_max + 10 * MIN
    controller.shed_order.steps[LOAD_WATER_HEATER] = Step(LOAD_WATER_HEATER, True, moment, late, 3000.0, moment.replace(minute=0))
    await heater.async_look(moment + MIN)
    assert switch.calls == [] and switch.state == "on"
    # The element's power not known: the need is not known either, so no pause at all.
    heater.kw = None
    assert heater._until_max(moment + 2 * MIN) == moment + 2 * MIN  # noqa: SLF001


async def test_the_meter_is_read_from_its_power_sensor_beside_an_energy_one(hass: HomeAssistant, freezer, monkeypatch) -> None:
    """A Shelly 1PM or a plug has both a power (W) and an energy (kWh) sensor: the power one is read, from its text
    state, in its unit; a unit Home Assistant does not know is no reading."""
    from custom_components.spotnav.site import heat_zones
    from custom_components.spotnav.site.heat_zones import DeviceSensor

    _site_, heater, _switch, _now = await _site(hass, freezer)
    sensors = [
        DeviceSensor("sensor.vvb_energy", "energy", "kWh", "total_increasing"),
        DeviceSensor(POWER, "power", "W", "measurement"),
    ]
    monkeypatch.setattr(heat_zones, "device_sensors", lambda _hass, _entity: ("device", sensors))
    hass.states.async_set(POWER, "2950.5", {"unit_of_measurement": "W"})
    assert heater._power(HEATER) == (POWER, 2950.5)  # noqa: SLF001
    hass.states.async_set(POWER, "2.9", {"unit_of_measurement": "kW"})
    assert heater._power(HEATER) == (POWER, 2900.0)  # noqa: SLF001
    hass.states.async_set(POWER, "2900", {"unit_of_measurement": "VA"})
    assert heater._power(HEATER) == (POWER, None)  # noqa: SLF001
    hass.states.async_set(POWER, "unavailable")
    assert heater._power(HEATER) == (POWER, None)  # noqa: SLF001


async def test_a_frozen_reading_is_no_reading(hass: HomeAssistant, freezer, monkeypatch) -> None:
    from custom_components.spotnav.site import heat_zones
    from custom_components.spotnav.site.heat_zones import DeviceSensor, POWER_STALE_S

    _site_, heater, _switch, _now = await _site(hass, freezer)
    monkeypatch.setattr(heat_zones, "device_sensors", lambda _hass, _entity: ("device", [DeviceSensor(POWER, "power", "W", "measurement")]))
    hass.states.async_set(POWER, "3000", {"unit_of_measurement": "W"})
    assert heater._power(HEATER) == (POWER, 3000.0)  # noqa: SLF001
    freezer.tick(timedelta(seconds=POWER_STALE_S + 60))
    assert heater._power(HEATER) == (POWER, None)  # noqa: SLF001


async def test_a_quiet_change_only_meter_at_nothing_stays_a_reading_and_full_heat_is_seen(
    hass: HomeAssistant, freezer, monkeypatch
) -> None:
    """A Zigbee or Z-Wave plug reports 0 W once and is then quiet: still a reading after 20 minutes, so full heat is
    seen and no `sensor_lost` turns the switch on 40 minutes on; a draw frozen for over 15 minutes is none."""
    from custom_components.spotnav.site import heat_zones
    from custom_components.spotnav.site.heat_zones import DeviceSensor, POWER_STALE_S

    _site_, heater, switch, now = await _site(hass, freezer)
    monkeypatch.setattr(heat_zones, "device_sensors", lambda _hass, _entity: ("device", [DeviceSensor(POWER, "power", "W", "measurement")]))
    heater.meter_seen = {"entity": HEATER, "sensor": POWER}
    heater.check = {"entity": HEATER, "result": "passed", "at": now.isoformat()}
    heater.rules.kept.last_full = now - 19 * H
    hass.states.async_set(POWER, "2800", {"unit_of_measurement": "W"})
    await heater.async_look(now)
    # The thermostat is satisfied: one 0 W report, then silence.
    hass.states.async_set(POWER, "0", {"unit_of_measurement": "W"})
    for minute in range(1, 41):
        freezer.tick(MIN)
        await heater.async_look(now + minute * MIN)
    assert heater.rules.kept.last_full is not None and heater.rules.kept.last_full > now
    assert heater.want is not None and heater.want.reason != "sensor_lost"
    assert heater._power(HEATER) == (POWER, 0.0)  # noqa: SLF001
    # A draw that stopped reporting: no reading once older than the limit.
    hass.states.async_set(POWER, "2500", {"unit_of_measurement": "W"})
    freezer.tick(timedelta(seconds=POWER_STALE_S + 60))
    assert heater._power(HEATER) == (POWER, None)  # noqa: SLF001
