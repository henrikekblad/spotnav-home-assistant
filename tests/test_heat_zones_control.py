"""SpotNav setting the zones' thermostats on the pilot's site (`heat/zones_control.py`): behind each zone's opt-in,
relative to the person's own setpoint, read back, never against someone else's change, blocked by an automation that
steers the thermostat, and the person's value written back on every exit, at the stop and at the next start. Every
write goes to a fake climate service in the test; nothing reaches a device."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any

import pytest
from homeassistant.core import HomeAssistant, ServiceCall

from custom_components.spotnav.const import CONF_HEAT
from custom_components.spotnav.heat.shadow_rule import BandHour
from custom_components.spotnav.heat import zones_control as zones_module
from custom_components.spotnav.heat.zones_control import ZonesControl
from custom_components.spotnav.runtime import site_controller_for

from .test_demand_site import pilot_site

pytestmark = pytest.mark.usefixtures("offline_relay")

FLOOR = "climate.badrum"
OUTDOOR = "sensor.ute"
MIN = timedelta(minutes=1)
H = timedelta(hours=1)


class Thermostat:
    """A fake climate entity: records each setpoint written and shows it unless told to ignore calls."""

    def __init__(self, hass: HomeAssistant, entity_id: str = FLOOR, setpoint: float = 22.0, current: float = 22.0) -> None:
        self.hass = hass
        self.entity_id = entity_id
        self.calls: list[float] = []
        self.ignore = False
        self.mode = "heat"
        self.attributes: dict[str, Any] = {
            "temperature": setpoint,
            "current_temperature": current,
            "supported_features": 1,
            "target_temp_step": 0.5,
            "min_temp": 5,
            "max_temp": 35,
            "hvac_modes": ["heat", "off"],
        }
        self.show()
        if not hass.services.has_service("climate", "set_temperature"):
            hass.services.async_register("climate", "set_temperature", self._set)

    def show(self) -> None:
        self.hass.states.async_set(self.entity_id, self.mode, self.attributes)

    async def _set(self, call: ServiceCall) -> None:
        self.calls.append(call.data["temperature"])
        if not self.ignore:
            self.attributes = {**self.attributes, "temperature": call.data["temperature"]}
            self.show()

    def set_by_hand(self, value: float) -> None:
        self.attributes = {**self.attributes, "temperature": value}
        self.show()

    @property
    def setpoint(self) -> float:
        return self.hass.states.get(self.entity_id).attributes["temperature"]  # type: ignore[union-attr]


async def _site(hass: HomeAssistant, freezer: Any, *, control: bool = True, **heat: Any) -> tuple[Any, Any, Thermostat]:
    site = await pilot_site(hass, freezer)
    thermostat = Thermostat(hass)
    hass.states.async_set(OUTDOOR, "4.0", {"unit_of_measurement": "°C", "device_class": "temperature"})
    config = {"outdoor_sensor": OUTDOOR, "zones": [{"entity_id": FLOOR, "offset_c": 0.0, "control": control}], **heat}
    hass.config_entries.async_update_entry(site, data={**site.data, CONF_HEAT: config})
    controller = site_controller_for(hass, site.entry_id)
    assert controller is not None
    await hass.async_block_till_done()
    return site, controller, thermostat


def _plan(zones: ZonesControl, now: datetime, actions: dict[int, str]) -> None:
    """The heat plan made now: the hours from the running one with their actions, the rest normal."""
    current = now.replace(minute=0, second=0, microsecond=0)
    shadow = zones.shadow
    shadow.first_plan = shadow.plan = [
        BandHour(current + h * H, actions.get(h, "normal"), 21.0, "dear_hour", 100.0) for h in range(-2, 12)
    ]
    shadow.made_at = now


async def _running(hass: HomeAssistant, freezer: Any, actions: dict[int, str], **site: Any) -> tuple[Any, ZonesControl, Thermostat, datetime]:
    """A site whose zone's data has been good for ten minutes, at a whole hour."""
    entry, controller, thermostat = await _site(hass, freezer, **site)
    zones: ZonesControl = controller.zones_control
    now = controller.demand.now().replace(minute=0, second=0, microsecond=0) + H
    _plan(zones, now - 11 * MIN, actions)
    await zones.async_look(now - 11 * MIN)
    _plan(zones, now, actions)
    return entry, zones, thermostat, now


async def test_nothing_is_written_without_the_zone_s_opt_in(hass: HomeAssistant, freezer) -> None:
    _entry, zones, thermostat, now = await _running(hass, freezer, {0: "lower", 1: "lower"}, control=False)
    for minute in range(0, 30, 5):
        await zones.async_look(now + minute * MIN)
    assert thermostat.calls == [] and zones.status(FLOOR) is None


async def test_a_lowering_is_relative_to_the_person_s_value_read_back_and_handed_back(hass: HomeAssistant, freezer) -> None:
    _entry, zones, thermostat, now = await _running(hass, freezer, {0: "lower", 1: "lower"})
    await zones.async_look(now)
    assert thermostat.calls == [21.0] and thermostat.setpoint == 21.0
    await zones.async_look(now + MIN)
    status = zones.status(FLOOR)
    assert status is not None
    assert (status["state"], status["home_c"], status["held_c"], status["check"]) == ("running", 22.0, 21.0, "passed")
    assert status["until"] == (now + 2 * H).isoformat()
    # The plan's two hours, then the person's value back: one write each way.
    await zones.async_look(now + 90 * MIN)
    assert thermostat.calls == [21.0]
    await zones.async_look(now + 2 * H + MIN)
    assert thermostat.calls == [21.0, 22.0] and thermostat.setpoint == 22.0
    await zones.async_look(now + 2 * H + 2 * MIN)
    assert zones.status(FLOOR)["held_c"] is None  # type: ignore[index]
    # The lowering is counted in the forecast once its power is known.
    zones.kw[FLOOR] = 1.0
    assert zones.forecast_delta(now + 2 * H + 3 * MIN)


async def test_the_night_and_away_and_a_pre_heat_follow_the_person_s_value(hass: HomeAssistant, freezer) -> None:
    _entry, zones, thermostat, now = await _running(hass, freezer, {0: "night", 1: "away", 2: "preheat"}, night={"on": True, "delta_c": 1.5})
    await zones.async_look(now)
    await zones.async_look(now + H)
    await zones.async_look(now + 2 * H)
    assert thermostat.calls == [20.5, 19.0, 22.5]


async def test_a_hand_change_wins_and_is_the_person_s_new_value(hass: HomeAssistant, freezer) -> None:
    _entry, zones, thermostat, now = await _running(hass, freezer, {0: "lower", 1: "lower"})
    await zones.async_look(now)
    await zones.async_look(now + MIN)
    thermostat.set_by_hand(23.0)
    await zones.async_look(now + 2 * MIN)
    assert zones.status(FLOOR)["state"] == "waiting"  # type: ignore[index]
    await zones.async_look(now + 4 * MIN)
    status = zones.status(FLOOR)
    assert status is not None and (status["reason"], status["home_c"], status["held_c"]) == ("foreign_change", 23.0, None)
    assert status["paused_until"] is not None
    # Nothing written back, nothing more the rest of the day.
    await zones.async_look(now + 30 * MIN)
    assert thermostat.calls == [21.0] and thermostat.setpoint == 23.0
    # Holding nothing, a hand change is simply the person's value.
    zones.zones[FLOOR].paused_until = None
    thermostat.set_by_hand(21.5)
    _plan(zones, now + 3 * H, {})
    await zones.async_look(now + 3 * H)
    assert zones.status(FLOOR)["home_c"] == 21.5  # type: ignore[index]


async def test_the_mode_leaving_heat_writes_nothing_and_the_person_s_value_comes_back_when_it_heats_again(hass: HomeAssistant, freezer) -> None:
    _entry, zones, thermostat, now = await _running(hass, freezer, {0: "lower", 1: "lower"})
    await zones.async_look(now)
    await zones.async_look(now + MIN)
    thermostat.mode = "off"
    thermostat.show()
    for minute in range(2, 40, 5):
        await zones.async_look(now + minute * MIN)
    status = zones.status(FLOOR)
    # Paused for the day, nothing written while off, and SpotNav's 21 is not taken for the person's value.
    assert status is not None and (status["reason"], status["home_c"], status["held_c"]) == ("foreign_change", 22.0, 21.0)
    assert status["paused_until"] is not None and thermostat.calls == [21.0]
    # The stop writes nothing to a thermostat that is off either: it stays kept for the next start.
    await zones.async_shutdown()
    assert thermostat.calls == [21.0] and zones.holds_any()
    zones._closed = False  # noqa: SLF001 - looked at again by hand
    # Heating again and still showing SpotNav's value: the person's value is written back.
    thermostat.mode = "heat"
    thermostat.show()
    await zones.async_look(now + 45 * MIN)
    await zones.async_look(now + 46 * MIN)
    status = zones.status(FLOOR)
    assert thermostat.calls == [21.0, 22.0] and status is not None and (status["home_c"], status["held_c"]) == (22.0, None)


async def test_a_setpoint_changed_while_the_mode_is_off_is_left_and_is_the_person_s(hass: HomeAssistant, freezer) -> None:
    _entry, zones, thermostat, now = await _running(hass, freezer, {0: "lower", 1: "lower"})
    await zones.async_look(now)
    await zones.async_look(now + MIN)
    thermostat.mode = "off"
    thermostat.show()
    await zones.async_look(now + 2 * MIN)
    thermostat.set_by_hand(19.0)
    await zones.async_look(now + 3 * MIN)
    await zones.async_look(now + 5 * MIN)
    thermostat.mode = "heat"
    thermostat.show()
    await zones.async_look(now + 6 * MIN)
    status = zones.status(FLOOR)
    assert thermostat.calls == [21.0] and thermostat.setpoint == 19.0
    assert status is not None and (status["home_c"], status["held_c"]) == (19.0, None)


async def test_the_person_s_value_lost_on_its_way_back_is_never_taken_for_written_nor_let_go(hass: HomeAssistant, freezer) -> None:
    """A pre-heat is half a degree above the person's value, one step of this thermostat: a write back that is lost
    leaves the thermostat a step from the person's value. That is no read-back, SpotNav's value never becomes the
    person's, and the write is tried again until it takes."""
    _entry, zones, thermostat, now = await _running(hass, freezer, {0: "preheat"})
    await zones.async_look(now)
    await zones.async_look(now + MIN)
    assert thermostat.calls == [22.5] and zones.status(FLOOR)["held_c"] == 22.5  # type: ignore[index]
    thermostat.ignore = True
    moment = now + H + MIN
    await zones.async_look(moment)
    assert thermostat.calls == [22.5, 22.0]
    # Looked at for an hour and a half: tried again, three times at once and then every half hour; still held.
    for step in range(1, 180):
        await zones.async_look(moment + step * MIN / 2)
        status = zones.status(FLOOR)
        assert status is not None and (status["home_c"], status["held_c"]) == (22.0, 22.5), step
    tries = thermostat.calls.count(22.0)
    assert 4 <= tries <= 7 and set(thermostat.calls) == {22.5, 22.0}
    # The thermostat takes writes again: the next try puts the person's value back.
    thermostat.ignore = False
    for step in range(180, 260):
        await zones.async_look(moment + step * MIN / 2)
    status = zones.status(FLOOR)
    assert thermostat.setpoint == 22.0 and status is not None and (status["home_c"], status["held_c"]) == (22.0, None)


async def test_turning_the_opt_in_off_writes_the_person_s_value_back(hass: HomeAssistant, freezer) -> None:
    entry, zones, thermostat, now = await _running(hass, freezer, {0: "lower", 1: "lower"})
    await zones.async_look(now)
    heat = dict(entry.data[CONF_HEAT])
    heat["zones"] = [{"entity_id": FLOOR, "offset_c": 0.0, "control": False}]
    hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_HEAT: heat})
    await zones.async_look(now + MIN)
    assert thermostat.calls == [21.0, 22.0]


async def test_a_write_not_read_back_fails_the_check_and_writes_the_person_s_value(hass: HomeAssistant, freezer) -> None:
    _entry, zones, thermostat, now = await _running(hass, freezer, {0: "lower", 1: "lower"})
    thermostat.ignore = True
    await zones.async_look(now)
    await zones.async_look(now + 2 * MIN)
    status = zones.status(FLOOR)
    assert status is not None and (status["state"], status["reason"], status["check"]) == ("waiting", "check_failed", "failed")
    # It still reads the person's value: nothing to put back. Nothing more until the switch is turned off and on.
    await zones.async_look(now + 10 * MIN)
    assert thermostat.calls == [21.0]


async def test_an_automation_that_steers_the_thermostat_blocks_its_control(hass: HomeAssistant, freezer) -> None:
    from custom_components.spotnav.heat import zones_control as module
    from custom_components.spotnav.heat.automations import SteeringAutomation

    _entry, zones, thermostat, now = await _running(hass, freezer, {0: "lower", 1: "lower"})
    await zones.async_look(now)
    found = [SteeringAutomation("automation.pris", "Pris", (FLOOR,))]
    original = module.steering_automations
    module.steering_automations = lambda _hass, ids: found if FLOOR in ids else []  # type: ignore[assignment]
    try:
        await zones.async_look(now + MIN)
    finally:
        module.steering_automations = original  # type: ignore[assignment]
    status = zones.status(FLOOR)
    assert status is not None and status["reason"] == "automation" and status["automation"]["name"] == "Pris"
    assert thermostat.calls == [21.0, 22.0]


async def test_the_zone_is_set_back_at_the_stop_and_at_the_next_start(hass: HomeAssistant, freezer) -> None:
    _entry, zones, thermostat, now = await _running(hass, freezer, {0: "lower", 1: "lower"})
    await zones.async_look(now)
    await zones.async_look(now + MIN)
    await zones.async_shutdown()
    assert thermostat.calls == [21.0, 22.0] and not zones.holds_any()
    # A stop that could not set it back: the next start does, where it still reads SpotNav's value.
    kept = {"zones": {FLOOR: {"home_c": 22.0, "written_c": 21.0, "action": "lower"}}}
    thermostat.set_by_hand(21.0)
    fresh = ZonesControl(hass, zones.shadow, zones.control)
    fresh.restore(kept)
    fresh._started = True  # noqa: SLF001 - started without its timer
    await fresh.async_look(now + 5 * MIN)
    assert thermostat.calls[-1] == 22.0
    # One someone else set meanwhile is left, and is the person's.
    thermostat.set_by_hand(20.0)
    other = ZonesControl(hass, zones.shadow, zones.control)
    other.restore(kept)
    other._started = True  # noqa: SLF001
    calls = len(thermostat.calls)
    await other.async_look(now + 6 * MIN)
    assert len(thermostat.calls) == calls and other.zones[FLOOR].home_c == 20.0


async def test_no_lowering_in_a_cold_zone_nor_on_a_stale_plan(hass: HomeAssistant, freezer) -> None:
    _entry, zones, thermostat, now = await _running(hass, freezer, {0: "lower", 1: "lower"})
    thermostat.attributes["current_temperature"] = 21.0
    thermostat.show()
    await zones.async_look(now)
    assert thermostat.calls == [] and zones.status(FLOOR)["reason"] == "zone_cold"  # type: ignore[index]
    thermostat.attributes["current_temperature"] = 22.0
    thermostat.show()
    zones.shadow.made_at = now - 3 * H
    await zones.async_look(now + MIN)
    assert thermostat.calls == [] and zones.status(FLOOR)["reason"] == "stale"  # type: ignore[index]


async def test_the_zones_state_is_kept_in_the_heat_control_s_store(hass: HomeAssistant, freezer) -> None:
    _entry, zones, _thermostat, now = await _running(hass, freezer, {0: "lower", 1: "lower"})
    await zones.async_look(now)
    data = zones.control._data()  # noqa: SLF001 - what the store keeps
    assert data["zones"]["zones"][FLOOR]["written_c"] == 21.0 and data["zones"]["zones"][FLOOR]["home_c"] == 22.0


async def test_the_zones_are_set_back_at_the_stop_at_the_same_time(hass: HomeAssistant, freezer, monkeypatch) -> None:
    """A slow thermostat does not hold up the others: each zone's set-back starts at once and keeps its own bound."""
    living = "climate.vardagsrum"
    zones_config = [{"entity_id": FLOOR, "offset_c": 0.0, "control": True}, {"entity_id": living, "offset_c": 0.0, "control": True}]
    _entry, zones, floor, now = await _running(hass, freezer, {0: "lower", 1: "lower"}, zones=zones_config)
    other = Thermostat(hass, living)
    by_id = {FLOOR: floor, living: other}

    async def route(call: ServiceCall) -> None:
        await by_id[call.data["entity_id"]]._set(call)  # noqa: SLF001

    hass.services.async_remove("climate", "set_temperature")
    hass.services.async_register("climate", "set_temperature", route)
    await zones.async_look(now)
    await zones.async_look(now + MIN)
    assert floor.setpoint == 21.0 and other.setpoint == 21.0

    # Each call waits until both have started: one after the other, the first would time out and stay held.
    started: list[str] = []
    both = asyncio.Event()

    async def gated(call: ServiceCall) -> None:
        started.append(call.data["entity_id"])
        if len(started) == 2:
            both.set()
        await both.wait()
        await route(call)

    monkeypatch.setattr(zones_module, "CALL_TIMEOUT_S", 0.5)
    hass.services.async_remove("climate", "set_temperature")
    hass.services.async_register("climate", "set_temperature", gated)
    await zones.async_shutdown()
    assert sorted(started) == sorted(by_id)
    assert floor.setpoint == 22.0 and other.setpoint == 22.0 and not zones.holds_any()


async def test_a_zone_s_power_is_read_from_its_power_sensor_beside_an_energy_one(hass: HomeAssistant, freezer, monkeypatch) -> None:
    from custom_components.spotnav.site import heat_zones
    from custom_components.spotnav.site.heat_zones import DeviceSensor

    _entry, zones, _thermostat, _now = await _running(hass, freezer, {})
    sensors = [
        DeviceSensor("sensor.golv_energi", "energy", "kWh", "total_increasing"),
        DeviceSensor("sensor.golv_effekt", "power", "W", "measurement"),
    ]
    monkeypatch.setattr(heat_zones, "device_sensors", lambda _hass, _entity: ("device", sensors))
    hass.states.async_set("sensor.golv_effekt", "450", {"unit_of_measurement": "W"})
    assert zones._zone_power_w(FLOOR) == 450.0  # noqa: SLF001
    hass.states.async_set("sensor.golv_effekt", "0.5", {"unit_of_measurement": "kW"})
    assert zones._zone_power_w(FLOOR) == 500.0  # noqa: SLF001
