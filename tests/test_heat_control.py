"""SpotNav's heat control on the pilot's site (`heat/control.py`): the relay switched on SpotNav's plan behind the
opt-in, read back, never against another writer, opened on every exit, and checked before control runs. Every write
goes to a fake switch service in the test; nothing reaches a device."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import CoreState, HomeAssistant, ServiceCall
from homeassistant.setup import async_setup_component

from custom_components.spotnav.const import CONF_HEAT
from custom_components.spotnav.heat.control import READ_BACK, HeatControl
from custom_components.spotnav.heat.relay_control import Sample
from custom_components.spotnav.heat.runtime import SOURCE_SPOTNAV
from custom_components.spotnav.heat.shadow_rule import BandHour
from custom_components.spotnav.runtime import site_controller_for

from .test_demand_site import pilot_site

pytestmark = pytest.mark.usefixtures("offline_relay")

RELAY = "switch.vp_rele"
OUTDOOR = "sensor.ute"
ROOM = "sensor.vardagsrum"
MIN = timedelta(minutes=1)
H = timedelta(hours=1)


class Relay:
    """A fake switch: records each call and shows the new state unless told to ignore calls or to raise."""

    def __init__(self, hass: HomeAssistant, entity_id: str = RELAY) -> None:
        self.hass = hass
        self.entity_id = entity_id
        self.calls: list[str] = []
        self.ignore = False
        self.fail = False
        hass.states.async_set(entity_id, "off")
        domain = entity_id.split(".")[0]
        hass.services.async_register(domain, "turn_on", self._on)
        hass.services.async_register(domain, "turn_off", self._off)

    def _do(self, call: ServiceCall, state: str) -> None:
        if call.data["entity_id"] != self.entity_id and self.entity_id not in (call.data["entity_id"] or []):
            return
        self.calls.append(state)
        if self.fail:
            raise RuntimeError("relay offline")
        if not self.ignore:
            self.hass.states.async_set(self.entity_id, state)

    async def _on(self, call: ServiceCall) -> None:
        self._do(call, "on")

    async def _off(self, call: ServiceCall) -> None:
        self._do(call, "off")

    @property
    def state(self) -> str:
        return self.hass.states.get(self.entity_id).state  # type: ignore[union-attr]


async def _site(hass: HomeAssistant, freezer: Any, **heat: Any) -> tuple[Any, Any, Relay]:
    site = await pilot_site(hass, freezer)
    relay = Relay(hass)
    hass.states.async_set(OUTDOOR, "4.0", {"unit_of_measurement": "°C", "device_class": "temperature"})
    hass.states.async_set(ROOM, "21.0", {"unit_of_measurement": "°C", "device_class": "temperature"})
    config = {
        "control_path": "relay",
        "control_entity": RELAY,
        "outdoor_sensor": OUTDOOR,
        "indoor_sensors": [ROOM],
        "control_enabled": True,
        **heat,
    }
    hass.config_entries.async_update_entry(site, data={**site.data, CONF_HEAT: config})
    controller = site_controller_for(hass, site.entry_id)
    assert controller is not None
    await hass.async_block_till_done()
    return site, controller, relay


def _plan(control: HeatControl, now: datetime, *lowered: int) -> None:
    """SpotNav's plan made now: the hours `lowered` (from the running hour) lowered, the rest normal."""
    current = now.replace(minute=0, second=0, microsecond=0)
    shadow = control.shadow
    shadow.plan = [
        BandHour(current + h * H, "lower" if h in lowered else "normal", 20.5, "dear_hour", 100.0) for h in range(-2, 12)
    ]
    shadow.made_at = now


def _passed(control: HeatControl) -> None:
    control.checks[RELAY] = {"result": "passed", "outcome": "passed", "at": "2026-11-01T00:00:00+00:00"}


async def _running(hass: HomeAssistant, freezer: Any, *lowered: int, **heat: Any) -> tuple[Any, HeatControl, Relay, datetime]:
    """A site whose check passed and whose data has been good for ten minutes, at a whole hour."""
    site, controller, relay = await _site(hass, freezer, **heat)
    control: HeatControl = controller.heat_control
    _passed(control)
    now = controller.demand.now().replace(minute=0, second=0, microsecond=0) + H
    _plan(control, now - 11 * MIN, *lowered)
    await control.async_look(now - 11 * MIN)
    _plan(control, now, *lowered)
    return site, control, relay, now


async def test_nothing_is_written_without_the_opt_in(hass: HomeAssistant, freezer) -> None:
    _site_, controller, relay = await _site(hass, freezer, control_enabled=False)
    control = controller.heat_control
    _passed(control)
    now = controller.demand.now()
    _plan(control, now, 0, 1)
    for minute in range(0, 30, 5):
        await control.async_look(now + minute * MIN)
    assert relay.calls == [] and control.status()["state"] == "off" and control.status()["enabled"] is False


async def test_a_planned_lowering_is_switched_read_back_and_counted_ahead(hass: HomeAssistant, freezer) -> None:
    _site_, control, relay, now = await _running(hass, freezer, 0, 1)
    await control.async_look(now)
    assert relay.calls == ["on"] and relay.state == "on" and control.held
    status = control.status()
    assert (status["state"], status["reason"], status["lowered"]) == ("running", "planned", True)
    assert status["until"] == (now + 2 * H).isoformat()
    # The heat shadow counts SpotNav's own closing ahead, not someone unknown's.
    control.shadow.relay_id = RELAY
    assert control.shadow.relay_source()[0] == SOURCE_SPOTNAV
    _past, ahead = control.shadow.relay_intervals(now + MIN)
    assert ahead == [(now + MIN, now + 2 * H)]
    # Held through the plan's two hours, then opened: one write each way.
    await control.async_look(now + 90 * MIN)
    assert relay.calls == ["on"]
    await control.async_look(now + 2 * H)
    assert relay.calls == ["on", "off"] and not control.held
    assert control.executed_minutes([now, now + H, now + 2 * H], now + 3 * H) == {now: 60, now + H: 60, now + 2 * H: 0}
    assert control.lowered_today(now + 3 * H) == 2 * H


async def test_a_lowering_lasts_at_least_45_minutes(hass: HomeAssistant, freezer) -> None:
    _site_, control, relay, now = await _running(hass, freezer, 0)
    await control.async_look(now + 5 * MIN)
    # The prices changed: the plan no longer lowers. The lowering is held to 45 minutes, not blipped.
    _plan(control, now + 20 * MIN)
    await control.async_look(now + 20 * MIN)
    assert relay.calls == ["on"] and control.status()["reason"] == "min_lower"
    await control.async_look(now + 50 * MIN)
    assert relay.calls == ["on", "off"]
    # Twenty minutes left of a planned hour: no start.
    _plan(control, now + 3 * H + 40 * MIN, 0)
    await control.async_look(now + 3 * H + 40 * MIN)
    assert relay.calls == ["on", "off"] and control.status()["reason"] == "too_short"


async def test_a_cold_room_releases_at_once(hass: HomeAssistant, freezer) -> None:
    _site_, control, relay, now = await _running(hass, freezer, 0, 1, 2)
    await control.async_look(now)
    hass.states.async_set(ROOM, "20.3", {"unit_of_measurement": "°C"})
    await control.async_look(now + 10 * MIN)
    assert relay.calls == ["on", "off"] and control.status()["reason"] == "room_cold"


async def test_the_rooms_offset_counts_in_the_coldest_room_rule(hass: HomeAssistant, freezer) -> None:
    # The room is kept a degree cooler: its 20.2 counts as 21.2, no release.
    _site_, control, relay, now = await _running(
        hass, freezer, 0, 1, indoor_offsets=[{"entity_id": ROOM, "offset_c": -1.0}]
    )
    hass.states.async_set(ROOM, "20.2", {"unit_of_measurement": "°C"})
    await control.async_look(now)
    assert relay.calls == ["on"] and control.status()["reason"] == "planned"


@pytest.mark.parametrize(("outdoor", "reason"), [("-15.0", "too_cold"), ("unavailable", "stale")])
async def test_cold_or_unreadable_outdoors_releases(hass: HomeAssistant, freezer, outdoor: str, reason: str) -> None:
    _site_, control, relay, now = await _running(hass, freezer, 0, 1)
    await control.async_look(now)
    hass.states.async_set(OUTDOOR, outdoor, {"unit_of_measurement": "°C"})
    await control.async_look(now + 5 * MIN)
    assert relay.calls == ["on", "off"] and control.status()["reason"] == reason
    # Good again: no start for ten minutes.
    hass.states.async_set(OUTDOOR, "3.0", {"unit_of_measurement": "°C"})
    _plan(control, now + 6 * MIN, 0, 1)
    await control.async_look(now + 6 * MIN)
    assert control.status()["reason"] in ("settling", "rest", "too_cold") and relay.calls == ["on", "off"]


async def test_a_stale_plan_releases(hass: HomeAssistant, freezer) -> None:
    _site_, control, relay, now = await _running(hass, freezer, 0, 1, 2)
    await control.async_look(now)
    await control.async_look(now + 2 * H + 30 * MIN)
    assert relay.calls == ["on", "off"] and control.status()["reason"] == "stale"


async def test_turning_off_opens_the_relay(hass: HomeAssistant, freezer) -> None:
    site, control, relay, now = await _running(hass, freezer, 0, 1)
    await control.async_look(now)
    hass.config_entries.async_update_entry(site, data={**site.data, CONF_HEAT: {**site.data[CONF_HEAT], "control_enabled": False}})
    await control.async_look(now + 5 * MIN)
    assert relay.calls == ["on", "off"] and control.status()["state"] == "off"


async def test_another_writer_is_never_fought(hass: HomeAssistant, freezer) -> None:
    _site_, control, relay, now = await _running(hass, freezer, 0, 1)
    await control.async_look(now)
    # The household opens the relay: SpotNav gives it up, writes nothing and waits for the next day.
    hass.states.async_set(RELAY, "off")
    await control.async_look(now + 10 * MIN)
    assert relay.calls == ["on"] and not control.held
    status = control.status()
    assert (status["state"], status["reason"]) == ("waiting", "foreign_change") and status["paused_until"] is not None
    # Closed again by the household: still nothing written.
    hass.states.async_set(RELAY, "on")
    await control.async_look(now + 20 * MIN)
    assert relay.calls == ["on"]
    # The next day it runs again (once the relay is its own again).
    hass.states.async_set(RELAY, "off")
    tomorrow = datetime.fromisoformat(status["paused_until"]) + H
    _plan(control, tomorrow, 0, 1)
    await control.async_look(tomorrow)
    assert relay.calls == ["on", "on"]


async def test_a_close_by_someone_else_while_open_pauses(hass: HomeAssistant, freezer) -> None:
    _site_, control, relay, now = await _running(hass, freezer)
    await control.async_look(now)
    hass.states.async_set(RELAY, "on")
    await control.async_look(now + MIN)
    assert relay.calls == [] and control.status()["reason"] == "foreign_change"


async def test_a_write_not_read_back_pauses_and_opens(hass: HomeAssistant, freezer) -> None:
    _site_, control, relay, now = await _running(hass, freezer, 0, 1)
    relay.ignore = True
    await control.async_look(now)
    await control.async_look(now + MIN)
    assert relay.calls == ["on"]
    await control.async_look(now + READ_BACK + 2 * MIN)
    # Not seen: paused for the day and an opening written (the relay may have closed after all).
    assert relay.calls == ["on", "off"] and control.status()["reason"] == "no_read_back"


async def test_a_write_that_raises_pauses_and_the_opening_is_tried_again(hass: HomeAssistant, freezer) -> None:
    _site_, control, relay, now = await _running(hass, freezer, 0)
    relay.fail = True
    await control.async_look(now)
    # The closing raised: paused for the day, opened all the same (that raised too), the relay reads open.
    assert relay.calls == ["on", "off"] and relay.state == "off" and not control.held
    assert control.status()["reason"] == "no_read_back"
    # An opening that raises while SpotNav holds the relay: tried again, at most every five minutes.
    control.paused_until = control.paused_reason = None
    relay.fail = False
    _plan(control, now + 2 * H, 0)
    await control.async_look(now + 2 * H)
    assert relay.state == "on" and control.held
    relay.fail = True
    _plan(control, now + 3 * H)
    await control.async_look(now + 3 * H)
    assert relay.calls[-1] == "off" and relay.state == "on" and control.held
    assert control.status()["reason"] == "no_read_back"
    calls = len(relay.calls)
    await control.async_look(now + 3 * H + 2 * MIN)
    assert len(relay.calls) == calls
    relay.fail = False
    await control.async_look(now + 3 * H + 6 * MIN)
    assert relay.calls[-1] == "off" and relay.state == "off" and not control.held


async def test_an_automation_on_the_relay_blocks_control_with_one_note(hass: HomeAssistant, freezer) -> None:
    _site_, control, relay, now = await _running(hass, freezer, 0, 1)
    assert await async_setup_component(
        hass,
        "automation",
        {
            "automation": [
                {
                    "id": "own_relay",
                    "alias": "Eget relä efter pris",
                    "triggers": [{"trigger": "state", "entity_id": "binary_sensor.dyrt"}],
                    "actions": [{"action": "switch.turn_on", "target": {"entity_id": RELAY}}],
                }
            ]
        },
    )
    await hass.async_block_till_done()
    await control.async_look(now)
    status = control.status()
    assert relay.calls == [] and (status["state"], status["reason"]) == ("waiting", "automation")
    assert status["automation"] == {"entity_id": "automation.eget_rela_efter_pris", "name": "Eget relä efter pris"}


async def test_the_stop_opens_the_relay(hass: HomeAssistant, freezer) -> None:
    _site_, control, relay, now = await _running(hass, freezer, 0, 1)
    await control.async_look(now)
    await control.async_shutdown()
    assert relay.calls == ["on", "off"] and not control.held


async def test_a_restart_opens_a_relay_still_held_before_anything_else(hass: HomeAssistant, freezer) -> None:
    _site_, control, relay, now = await _running(hass, freezer, 0, 1)
    await control.async_look(now)
    relay.fail = True
    await control.async_shutdown()
    assert control.held and relay.state == "on"
    data = control._data()  # noqa: SLF001 - what the store keeps
    fresh = HeatControl(hass, control.shadow)
    fresh._restore(data)  # noqa: SLF001
    fresh._loaded = True  # noqa: SLF001
    relay.fail = False
    relay.calls.clear()
    # The plan still lowers, but the relay held at the stop is opened first.
    _plan(fresh, now + 5 * MIN, 0, 1)
    await fresh.async_look(now + 5 * MIN)
    assert relay.calls == ["off"] and relay.state == "off" and not fresh.held
    # And a new lowering waits the hour of rest after it.
    await fresh.async_look(now + 6 * MIN)
    assert relay.calls == ["off"] and fresh.status()["reason"] == "settling"
    _plan(fresh, now + 20 * MIN, 0, 1)
    await fresh.async_look(now + 20 * MIN)
    assert relay.calls == ["off"] and fresh.status()["reason"] == "rest"


async def test_a_changed_relay_entity_gives_the_old_one_back_and_is_checked_again(hass: HomeAssistant, freezer) -> None:
    site, control, relay, now = await _running(hass, freezer, 0, 1)
    await control.async_look(now)
    other = Relay(hass, "input_boolean.vp_rele_2")
    hass.config_entries.async_update_entry(
        site, data={**site.data, CONF_HEAT: {**site.data[CONF_HEAT], "control_entity": "input_boolean.vp_rele_2"}}
    )
    hass.states.async_set("input_boolean.vp_rele_2", "off")
    await control.async_look(now + 5 * MIN)
    await control.async_look(now + 6 * MIN)
    assert relay.calls == ["on", "off"] and other.calls == []
    assert control.relay == "input_boolean.vp_rele_2" and control.status()["state"] in ("verifying", "waiting")


# ------------------------------------------------------------------------------------------------ the check


async def _checking(hass: HomeAssistant, freezer: Any, samples: list[Sample]) -> tuple[HeatControl, Relay, datetime]:
    _site_, controller, relay = await _site(hass, freezer)
    control: HeatControl = controller.heat_control
    feed = iter(samples)
    last: list[Sample] = [samples[0]]

    async def sample(_now: datetime) -> Sample:
        last[0] = next(feed, last[0])
        return last[0]

    control.async_sample = sample  # type: ignore[method-assign]
    now = controller.demand.now()
    _plan(control, now)
    return control, relay, now


async def _looks(control: HeatControl, now: datetime, minutes: int) -> datetime:
    for _ in range(minutes):
        _plan(control, now)
        await control.async_look(now)
        now += MIN
    return now


async def test_the_check_passes_then_control_runs(hass: HomeAssistant, freezer) -> None:
    samples = [Sample(supply_c=40.3)] * 6 + [Sample(supply_c=35.7)] * 2 + [Sample(supply_c=40.2)] * 2
    control, relay, now = await _checking(hass, freezer, samples)
    now = await _looks(control, now, 6)
    assert relay.calls == ["on"] and control.status()["state"] == "verifying"
    now = await _looks(control, now, 4)
    assert relay.calls == ["on", "off"]
    assert control.checks[RELAY]["result"] == "passed" and control.status()["check"] == "passed"
    await _looks(control, now, 1)
    assert control.status()["state"] == "running"


async def test_a_failed_check_stops_control_until_turned_on_again(hass: HomeAssistant, freezer) -> None:
    control, relay, now = await _checking(hass, freezer, [Sample(supply_c=40.3)])
    now = await _looks(control, now, 30)
    assert relay.calls == ["on", "off"] and control.checks[RELAY]["result"] == "failed"
    status = control.status()
    assert (status["state"], status["reason"], status["check"]) == ("waiting", "check_failed", "failed")
    await _looks(control, now, 5)
    assert relay.calls == ["on", "off"]
    # Off and on again: tried afresh.
    entry = hass.config_entries.async_get_entry(control.site.entry_id)
    heat = entry.data[CONF_HEAT]  # type: ignore[union-attr]
    hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_HEAT: {**heat, "control_enabled": False}})  # type: ignore[arg-type]
    await _looks(control, now + H, 1)
    hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_HEAT: {**heat, "control_enabled": True}})  # type: ignore[arg-type]
    await _looks(control, now + 2 * H, 1)
    assert RELAY not in control.checks and control.status()["state"] == "verifying"


async def test_an_unjudgeable_moment_is_tried_again_later(hass: HomeAssistant, freezer) -> None:
    samples = [Sample(1.1, "heating")] * 7 + [Sample(3.0, "hot_water")] + [Sample(1.1, "heating")] * 200
    control, relay, now = await _checking(hass, freezer, samples)
    now = await _looks(control, now, 8)
    assert relay.calls == ["on", "off"] and control.check.last == "not_judgeable:hot_water"
    assert RELAY not in control.checks and control.status()["state"] == "verifying"
    # Not again within the hour.
    now = await _looks(control, now, 30)
    assert relay.calls == ["on", "off"]


async def test_without_a_meter_or_a_supply_reading_nothing_is_judged(hass: HomeAssistant, freezer) -> None:
    control, relay, now = await _checking(hass, freezer, [Sample()])
    await _looks(control, now, 10)
    assert relay.calls == [] and control.status()["reason"] == "no_signal"


async def test_a_check_ends_open_when_the_room_turns_cold(hass: HomeAssistant, freezer) -> None:
    control, relay, now = await _checking(hass, freezer, [Sample(1.1, "heating")])
    now = await _looks(control, now, 7)
    assert relay.calls == ["on"]
    hass.states.async_set(ROOM, "20.0", {"unit_of_measurement": "°C"})
    await _looks(control, now, 1)
    assert relay.calls == ["on", "off"] and control.check.last == "not_judgeable:room_cold"


async def test_unloading_the_site_opens_the_relay(hass: HomeAssistant, freezer) -> None:
    site, control, relay, now = await _running(hass, freezer, 0, 1)
    await control.async_look(now)
    assert relay.state == "on"
    assert await hass.config_entries.async_unload(site.entry_id)
    await hass.async_block_till_done()
    assert relay.calls == ["on", "off"] and relay.state == "off"


async def test_home_assistants_stop_opens_the_relay(hass: HomeAssistant, freezer) -> None:
    _site_, control, relay, now = await _running(hass, freezer, 0, 1)
    await control.async_look(now)
    hass.set_state(CoreState.stopping)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
    await hass.async_block_till_done()
    hass.set_state(CoreState.running)
    assert relay.calls == ["on", "off"] and relay.state == "off" and not control.held
