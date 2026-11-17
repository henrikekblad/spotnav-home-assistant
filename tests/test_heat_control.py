"""SpotNav's heat control on the pilot's site (`heat/control.py`): the relay switched on SpotNav's plan behind the
opt-in, read back, never against another writer, opened on every exit, and checked before control runs. Every write
goes to a fake switch service in the test; nothing reaches a device."""

from __future__ import annotations

import asyncio
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
from .test_heat_api import read, update

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
    assert control.lowered_24h(now + 3 * H) == 2 * H


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
    await control.async_look(now + 11 * MIN)
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
    await control.async_look(now + 2 * MIN)
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
        control.signal = last[0].kw is not None or last[0].supply_c is not None
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



async def test_the_card_reads_the_control_and_turning_it_off_opens_the_relay_at_once(hass: HomeAssistant, freezer) -> None:
    site, control, relay, now = await _running(hass, freezer, 0, 1)
    await control.async_look(now)
    await control.async_look(now + 30 * MIN)
    freezer.move_to(now + 30 * MIN)
    answer = (await read(hass, api_version=1, site_id=site.entry_id))["result"]
    assert answer["control"]["state"] == "running" and answer["control"]["lowered"] is True
    assert answer["config"]["control_enabled"] is True
    current = next(hour for hour in answer["band"]["hours"] if hour["start"] == now.isoformat())
    assert current["executed_min"] == 30 and answer["shadow"] is False
    # Turned off from the card: the write makes the control look at once, and the relay opens.
    result = (await update(hass, api_version=1, site_id=site.entry_id, changes={"control_enabled": False}, expected={"control_enabled": True}))["result"]
    assert result["ok"] is True and result["config"]["control_enabled"] is False
    await hass.async_block_till_done()
    assert relay.calls == ["on", "off"] and relay.state == "off"
    # Only an administrator may turn it on.
    refused = (await update(hass, admin=False, api_version=1, site_id=site.entry_id, changes={"control_enabled": True}))["result"]
    assert refused["error"] == "spotnav_not_admin"


async def test_a_site_without_a_meter_or_a_pump_reading_asks_for_the_meter(hass: HomeAssistant, freezer) -> None:
    _site_, controller, relay = await _site(hass, freezer)
    control: HeatControl = controller.heat_control
    now = controller.demand.now()
    _plan(control, now)
    await control.async_look(now)
    assert control.signal is False and relay.calls == []
    assert (control.status()["state"], control.status()["reason"]) == ("waiting", "no_signal")


async def test_turning_off_while_a_closing_is_read_back_opens_the_relay(hass: HomeAssistant, freezer) -> None:
    site, control, relay, now = await _running(hass, freezer, 0, 1)
    relay.ignore = True
    await control.async_look(now)
    assert relay.calls == ["on"] and control.pending is not None
    # The closing takes late, and the person turns control off before it is read back.
    hass.states.async_set(RELAY, "on")
    relay.ignore = False
    hass.config_entries.async_update_entry(site, data={**site.data, CONF_HEAT: {**site.data[CONF_HEAT], "control_enabled": False}})
    await control.async_look(now + 30 * timedelta(seconds=1))
    await control.async_look(now + MIN)
    assert relay.calls == ["on", "off"] and relay.state == "off" and not control.held


async def test_turning_off_while_a_closing_is_never_read_back_opens_the_relay(hass: HomeAssistant, freezer) -> None:
    site, control, relay, now = await _running(hass, freezer, 0, 1)
    relay.ignore = True
    await control.async_look(now)
    hass.config_entries.async_update_entry(site, data={**site.data, CONF_HEAT: {**site.data[CONF_HEAT], "control_enabled": False}})
    await control.async_look(now + MIN)
    relay.ignore = False
    await control.async_look(now + READ_BACK + MIN)
    await control.async_look(now + READ_BACK + 2 * MIN)
    assert relay.calls == ["on", "off"] and relay.state == "off" and not control.held and control.pending is None


async def test_an_error_while_a_write_is_pending_opens_the_relay_once_it_expires(hass: HomeAssistant, freezer) -> None:
    _site_, control, relay, now = await _running(hass, freezer, 0, 1)
    relay.ignore = True
    await control.async_look(now)
    relay.ignore = False

    async def broken(_now: datetime) -> None:
        raise RuntimeError("broken")

    control._async_look = broken  # type: ignore[method-assign]  # noqa: SLF001
    await control.async_look(now + READ_BACK + MIN)
    assert relay.calls == ["on", "off"] and relay.state == "off"


async def test_one_open_reading_while_held_is_no_foreign_change(hass: HomeAssistant, freezer) -> None:
    _site_, control, relay, now = await _running(hass, freezer, 0, 1)
    await control.async_look(now)
    # A reading that bounces open for one look: SpotNav keeps its lowering and is not paused.
    hass.states.async_set(RELAY, "off")
    await control.async_look(now + 10 * MIN)
    hass.states.async_set(RELAY, "on")
    await control.async_look(now + 10 * MIN + 30 * timedelta(seconds=1))
    assert relay.calls == ["on"] and control.held and control.paused_until is None


async def test_a_late_closed_reading_after_spotnavs_own_opening_is_opened_once(hass: HomeAssistant, freezer) -> None:
    _site_, control, relay, now = await _running(hass, freezer, 0)
    await control.async_look(now)
    _plan(control, now + 50 * MIN)
    await control.async_look(now + 50 * MIN)
    assert relay.calls == ["on", "off"] and not control.held
    # SpotNav's own closing reads again (a late or bounced reading): taken as its own and opened, no pause.
    hass.states.async_set(RELAY, "on")
    await control.async_look(now + 51 * MIN)
    await control.async_look(now + 52 * MIN)
    assert relay.calls == ["on", "off", "off"] and relay.state == "off" and not control.held
    assert control.paused_until is None
    # Closed again after that one opening: someone else's, never fought.
    hass.states.async_set(RELAY, "on")
    await control.async_look(now + 53 * MIN)
    await control.async_look(now + 54 * MIN)
    assert relay.calls == ["on", "off", "off"] and control.status()["reason"] == "foreign_change"


async def test_a_closing_that_takes_after_it_timed_out_is_opened(hass: HomeAssistant, freezer) -> None:
    _site_, control, relay, now = await _running(hass, freezer, 0, 1)
    relay.ignore = True
    await control.async_look(now)
    await control.async_look(now + READ_BACK + MIN)
    # Not read back: paused, the opening written (not seen either, the relay reads open all along).
    assert relay.calls == ["on", "off"] and not control.held
    relay.ignore = False
    # The closing takes late: SpotNav's own, opened, never left closed.
    hass.states.async_set(RELAY, "on")
    await control.async_look(now + READ_BACK + 2 * MIN)
    await control.async_look(now + READ_BACK + 3 * MIN)
    assert relay.calls == ["on", "off", "off"] and relay.state == "off" and not control.held


async def test_a_relay_found_closed_is_noted_once_and_left_alone(hass: HomeAssistant, freezer) -> None:
    _site_, control, relay, now = await _running(hass, freezer, 0, 1)
    hass.states.async_set(RELAY, "on")
    control._expect = None  # noqa: SLF001 - found so, not seen to change
    await control.async_look(now)
    await control.async_look(now + MIN)
    assert relay.calls == [] and control.status()["reason"] == "relay_closed"
    assert [item["what"] for item in control.log].count("found_closed") == 1


async def _change_relay(hass: HomeAssistant, site: Any) -> Relay:
    other = Relay(hass, "input_boolean.vp_rele_2")
    hass.config_entries.async_update_entry(
        site, data={**site.data, CONF_HEAT: {**site.data[CONF_HEAT], "control_entity": "input_boolean.vp_rele_2"}}
    )
    return other


async def test_a_removed_relay_held_closed_is_let_go_and_the_new_one_taken_up(hass: HomeAssistant, freezer) -> None:
    site, control, relay, now = await _running(hass, freezer, 0, 1)
    await control.async_look(now)
    await _change_relay(hass, site)
    hass.states.async_remove(RELAY)
    for minute in range(5, 20):
        await control.async_look(now + minute * MIN)
    assert control.relay == "input_boolean.vp_rele_2" and not control.held
    assert any(item["what"] == "relay_let_go" for item in control.log)


async def test_an_old_relay_that_never_opens_is_let_go_after_a_few_tries(hass: HomeAssistant, freezer) -> None:
    site, control, relay, now = await _running(hass, freezer, 0, 1)
    await control.async_look(now)
    relay.ignore = True
    await _change_relay(hass, site)
    for minute in range(5, 60, 2):
        await control.async_look(now + minute * MIN)
    assert 2 <= relay.calls.count("off") <= 4
    assert control.relay == "input_boolean.vp_rele_2" and not control.held


async def test_a_compressor_start_spike_is_not_read_as_hot_water(hass: HomeAssistant, freezer) -> None:
    from custom_components.spotnav.heat.meter_split import Levels
    from custom_components.spotnav.site.heat_meter import KIND_POWER, STATUS_FOUND, HeatMeter

    _site_, controller, _relay = await _site(hass, freezer)
    control: HeatControl = controller.heat_control
    control.site.house_forecast.heat.meter = HeatMeter(STATUS_FOUND, "sensor.vp_effekt", KIND_POWER)
    now = controller.demand.now()
    control._levels, control._levels_hour = Levels(0.4, 1.1), now.replace(minute=0, second=0, microsecond=0)  # noqa: SLF001
    for second in range(0, 300, 30):
        hass.states.async_set("sensor.vp_effekt", "1.1", {"unit_of_measurement": "kW"})
        await control.async_sample(now + timedelta(seconds=second))
    # A 2.2 kW start spike after five minutes at 1.1 kW: the 5-minute mean is far below a hot-water run.
    hass.states.async_set("sensor.vp_effekt", "2.2", {"unit_of_measurement": "kW"})
    sample = await control.async_sample(now + timedelta(seconds=300))
    assert sample.state == "heating" and sample.kw == 2.2


async def test_closings_already_measured_let_one_power_try_pass(hass: HomeAssistant, freezer) -> None:
    from custom_components.spotnav.heat.relay_effect import BlockResult

    samples = [Sample(1.1, "heating")] * 6 + [Sample(0.5, "heating")] * 2 + [Sample(1.1, "heating")] * 2
    control, relay, now = await _checking(hass, freezer, samples)
    control.shadow.blocks = [
        BlockResult(now - (d + 1) * 24 * H, now - (d + 1) * 24 * H + 2 * H, 1.2, 0.5, 1.4) for d in range(2)
    ]
    await _looks(control, now, 11)
    assert relay.calls == ["on", "off"] and control.checks[RELAY]["result"] == "passed"


@pytest.mark.parametrize("room", ["unavailable", "55.0"])
async def test_chosen_rooms_none_readable_release(hass: HomeAssistant, freezer, room: str) -> None:
    _site_, control, relay, now = await _running(hass, freezer, 0, 1, 2)
    await control.async_look(now)
    hass.states.async_set(ROOM, room, {"unit_of_measurement": "°C"})
    await control.async_look(now + 5 * MIN)
    assert relay.calls == ["on", "off"] and control.status()["reason"] == "stale"


async def test_no_chosen_room_still_lowers(hass: HomeAssistant, freezer) -> None:
    _site_, control, relay, now = await _running(hass, freezer, 0, 1, indoor_sensors=[])
    await control.async_look(now)
    assert relay.calls == ["on"] and control.status()["reason"] == "planned"


@pytest.mark.parametrize("frozen", [OUTDOOR, ROOM])
async def test_a_frozen_sensor_is_stale(hass: HomeAssistant, freezer, frozen: str) -> None:
    _site_, control, relay, now = await _running(hass, freezer, 0, 1)
    later = now + 4 * H
    freezer.move_to(later)
    # The other sensor reports on; the frozen one last reported four hours ago.
    for entity_id, value in ((OUTDOOR, "4.0"), (ROOM, "21.0")):
        if entity_id != frozen:
            hass.states.async_set(entity_id, value, {"unit_of_measurement": "°C", "device_class": "temperature"})
    _plan(control, later, 0, 1)
    await control.async_look(later)
    assert relay.calls == [] and control.status()["reason"] == "stale"


async def test_the_forecast_colder_than_the_sensor_rules_the_cold_limit(hass: HomeAssistant, freezer) -> None:
    _site_, control, relay, now = await _running(hass, freezer, 0, 1)
    control.site.house_forecast._temperatures = {now: -16.0}  # noqa: SLF001 - the weather's hourly forecast
    await control.async_look(now)
    assert relay.calls == [] and control.status()["reason"] == "too_cold"


async def test_a_frozen_meter_gives_no_power(hass: HomeAssistant, freezer) -> None:
    from custom_components.spotnav.heat.meter_split import Levels
    from custom_components.spotnav.site.heat_meter import KIND_POWER, STATUS_FOUND, HeatMeter

    _site_, controller, _relay = await _site(hass, freezer)
    control: HeatControl = controller.heat_control
    control.site.house_forecast.heat.meter = HeatMeter(STATUS_FOUND, "sensor.vp_effekt", KIND_POWER)
    now = controller.demand.now()
    hass.states.async_set("sensor.vp_effekt", "1.1", {"unit_of_measurement": "kW"})
    freezer.move_to(now + 4 * H)
    control._levels, control._levels_hour = Levels(0.4, 1.1), (now + 4 * H).replace(minute=0, second=0, microsecond=0)  # noqa: SLF001
    sample = await control.async_sample(now + 4 * H)
    assert sample.kw is None and sample.state is None


async def test_an_unavailable_relay_is_not_written_and_opened_once_it_reads(hass: HomeAssistant, freezer) -> None:
    _site_, control, relay, now = await _running(hass, freezer, 0, 1)
    await control.async_look(now)
    # The integration restarts: the relay reads unavailable for a few minutes. Nothing is written, no pause.
    relay.ignore = True
    hass.states.async_set(RELAY, "unavailable")
    await control.async_look(now + MIN)
    await control.async_look(now + 4 * MIN)
    assert relay.calls == ["on"] and control.paused_until is None
    assert control.status()["reason"] == "relay_unavailable"
    # Back, closed: opened at the first readable look.
    relay.ignore = False
    hass.states.async_set(RELAY, "on")
    await control.async_look(now + 5 * MIN)
    await control.async_look(now + 6 * MIN)
    assert relay.calls == ["on", "off"] and relay.state == "off" and not control.held and control.paused_until is None


async def test_a_closing_pending_while_the_relay_is_unavailable_starts_no_pause(hass: HomeAssistant, freezer) -> None:
    _site_, control, relay, now = await _running(hass, freezer, 0, 1)
    relay.ignore = True
    await control.async_look(now)
    hass.states.async_set(RELAY, "unavailable")
    await control.async_look(now + READ_BACK + MIN)
    assert relay.calls == ["on"] and control.paused_until is None and control.pending is None
    # Back open: nothing more to do.
    relay.ignore = False
    hass.states.async_set(RELAY, "off")
    await control.async_look(now + READ_BACK + 2 * MIN)
    assert relay.calls == ["on"] and not control.held and control.paused_until is None


async def test_the_store_is_saved_only_when_its_data_changed(hass: HomeAssistant, freezer) -> None:
    _site_, control, relay, now = await _running(hass, freezer)
    await control.async_look(now)
    saves: list[int] = []
    control._store.async_delay_save = lambda *_args, **_kw: saves.append(1)  # type: ignore[method-assign]  # noqa: SLF001
    for minute in range(1, 6):
        await control.async_look(now + minute * MIN)
    assert saves == []
    _plan(control, now + 6 * MIN, 0, 1)
    await control.async_look(now + 6 * MIN)
    assert relay.calls == ["on"] and saves


async def test_the_check_counts_toward_the_days_writes(hass: HomeAssistant, freezer) -> None:
    control, relay, now = await _checking(hass, freezer, [Sample(1.1, "heating")])
    control.writes[control._day(now)] = 5  # noqa: SLF001
    await _looks(control, now, 10)
    assert relay.calls == [] and control.check.phase == "wait"


async def test_the_lowered_hours_cap_is_a_rolling_day(hass: HomeAssistant, freezer) -> None:
    _site_, control, relay, now = await _running(hass, freezer, 0, 1)
    zone = control._midnight(now.date()).tzinfo  # noqa: SLF001
    midnight = control._midnight((now.astimezone(zone) + timedelta(days=1)).date())  # noqa: SLF001
    # Six hours lowered up to just before midnight: two hours after it, no new lowering yet.
    control.lowerings = [[midnight - 6 * H + MIN, midnight]]
    control.last_end = midnight
    later = midnight + 2 * H
    _plan(control, later, 0, 1)
    await control.async_look(later)
    assert relay.calls == [] and control.status()["reason"] in ("day_cap", "too_short")


async def test_the_closings_ahead_keep_the_rest_and_the_rolling_cap(hass: HomeAssistant, freezer) -> None:
    _site_, control, relay, now = await _running(hass, freezer, *range(12))
    await control.async_look(now)
    assert relay.calls == ["on"]
    # Three hours (the longest run), an hour's rest, three more: then six hours in 24 and nothing more.
    assert control.closings_ahead(now + MIN) == [(now + MIN, now + 3 * H), (now + 4 * H, now + 7 * H)]


async def test_the_closings_ahead_keep_the_days_writes(hass: HomeAssistant, freezer) -> None:
    _site_, control, relay, now = await _running(hass, freezer, 0, 2)
    control.writes[control._day(now)] = 3  # noqa: SLF001
    await control.async_look(now)
    assert relay.calls == ["on"]
    # Four writes today with this closing; its opening makes five: no room for another closing and opening today.
    assert control._day(now + 2 * H) == control._day(now)  # noqa: SLF001 - the pilot's morning
    assert control.closings_ahead(now + MIN) == [(now + MIN, now + H)]


async def test_no_closings_ahead_while_a_safety_reason_holds(hass: HomeAssistant, freezer) -> None:
    _site_, control, relay, now = await _running(hass, freezer, 0, 3, 4)
    hass.states.async_set(OUTDOOR, "-16.0", {"unit_of_measurement": "°C"})
    await control.async_look(now)
    assert relay.calls == [] and control.closings_ahead(now + MIN) == []


@pytest.mark.parametrize(
    ("entity_id", "state", "attributes"),
    [
        ("weather.hemma", "cloudy", {"temperature": -4.0, "temperature_unit": "°F"}),
        ("sensor.ute_f", "-4.0", {"unit_of_measurement": "°F", "device_class": "temperature"}),
    ],
)
async def test_the_plan_reads_the_outdoor_temperature_like_the_control(
    hass: HomeAssistant, freezer, monkeypatch: pytest.MonkeyPatch, entity_id: str, state: str, attributes: dict[str, Any]
) -> None:
    from custom_components.spotnav.heat import runtime

    hass.states.async_set(entity_id, state, attributes)
    _site_, controller, _relay = await _site(hass, freezer, outdoor_sensor=entity_id)
    seen: list[float | None] = []

    def plan_days(*_args: Any, outdoor_c: float | None = None, **_kw: Any) -> tuple[list[Any], None]:
        seen.append(outdoor_c)
        return [], None

    monkeypatch.setattr(runtime, "plan_days", plan_days)
    await controller.heat_control.shadow._async_plan(controller.demand.now())  # noqa: SLF001
    assert seen and seen[-1] == pytest.approx(-20.0)


async def test_an_automation_that_only_reads_the_relay_does_not_block(hass: HomeAssistant, freezer) -> None:
    _site_, control, relay, now = await _running(hass, freezer, 0, 1)
    assert await async_setup_component(
        hass,
        "automation",
        {
            "automation": [
                {
                    "id": "tell",
                    "alias": "Säg till när reläet slår",
                    "triggers": [{"trigger": "state", "entity_id": RELAY}],
                    "actions": [{"action": "input_boolean.turn_on", "target": {"entity_id": "input_boolean.flagga"}}],
                }
            ]
        },
    )
    await hass.async_block_till_done()
    await control.async_look(now)
    assert relay.calls == ["on"] and control.status()["reason"] == "planned"


@pytest.mark.parametrize(
    ("domain", "config"),
    [
        ("script", {"vp_dyrt": {"alias": "VP dyrt", "sequence": [{"action": "switch.turn_on", "target": {"entity_id": RELAY}}]}}),
        ("scene", [{"name": "Natt", "entities": {RELAY: "on"}}]),
    ],
)
async def test_a_script_or_scene_on_the_relay_blocks_control(hass: HomeAssistant, freezer, domain: str, config: Any) -> None:
    _site_, control, relay, now = await _running(hass, freezer, 0, 1)
    assert await async_setup_component(hass, domain, {domain: config})
    await hass.async_block_till_done()
    await control.async_look(now)
    assert relay.calls == [] and control.status()["reason"] == "automation"
    assert control.status()["automation"]["entity_id"].startswith(f"{domain}.")


async def test_the_stop_waits_for_a_running_look_before_it_opens(hass: HomeAssistant, freezer) -> None:
    import asyncio

    _site_, control, relay, now = await _running(hass, freezer, 0, 1)
    release = asyncio.Event()

    async def slow_on(call: ServiceCall) -> None:
        relay.calls.append("on")
        try:
            await release.wait()
        except asyncio.CancelledError:
            # The device takes the closing as the call is cancelled.
            hass.states.async_set(RELAY, "on")
            raise
        hass.states.async_set(RELAY, "on")

    look_done_at_opening: list[bool] = []

    async def off(call: ServiceCall) -> None:
        look_done_at_opening.append(task.done())
        relay.calls.append("off")
        hass.states.async_set(RELAY, "off")

    hass.services.async_register("switch", "turn_on", slow_on)
    hass.services.async_register("switch", "turn_off", off)
    task = control._task = hass.async_create_task(control.async_look(now))  # noqa: SLF001
    for _ in range(20):
        await asyncio.sleep(0)
    assert relay.calls == ["on"] and control.held
    await control.async_shutdown()
    # The look ended (its closing taken) before the opening was written: the relay ends open.
    assert look_done_at_opening == [True] and relay.calls == ["on", "off"] and relay.state == "off"


async def test_a_house_above_its_band_for_an_hour_is_lowered_whatever_the_price(hass: HomeAssistant, freezer) -> None:
    site, controller, relay = await _site(hass, freezer, target_c=21.0)
    control: HeatControl = controller.heat_control
    shadow = controller.heat_shadow
    _passed(control)
    start = controller.demand.now().replace(second=0, microsecond=0)

    async def at(minute: int, room: str) -> datetime:
        now = start + minute * MIN
        freezer.move_to(now)
        hass.states.async_set(ROOM, room, {"unit_of_measurement": "°C", "device_class": "temperature"})
        hass.states.async_set(OUTDOOR, "4.0", {"unit_of_measurement": "°C", "device_class": "temperature"})
        shadow.tick(now)
        await hass.async_block_till_done()
        # The plan is made in a background task, which the block above does not wait for.
        while shadow._task is not None and not shadow._task.done():  # noqa: SLF001
            await asyncio.sleep(0)
        await control.async_look(now)
        return now

    for minute in range(0, 65, 5):
        await at(minute, "22.3")
    # Above the band (21.5) for less than an hour (the first look's reading has no known rise yet): nothing.
    assert relay.calls == [] and not shadow.room_warm.active
    now = await at(66, "22.3")
    assert shadow.room_warm.active and relay.calls == ["on"]
    running = next(hour for hour in shadow.plan if hour.start <= now < hour.start + H)
    assert (running.action, running.reason) == ("lower", "room_warm")
    # The card shows the hour lowered for it, and the hour is kept so.
    answer = (await read(hass, api_version=1, site_id=site.entry_id))["result"]
    current = now.replace(minute=0).isoformat()
    shown = next(hour for hour in answer["band"]["hours"] if hour["start"] == current)
    assert (shown["action"], shown["reason"]) == ("lower", "room_warm")
    assert answer["status"]["would"]["reason"] == "room_warm"
    # Back 0.2 inside the band: the lowering ends once its 45 minutes have passed.
    await at(75, "21.2")
    assert not shadow.room_warm.active and relay.calls == ["on"]
    await at(66 + 45, "21.2")
    assert relay.calls == ["on", "off"]


async def test_a_fast_rising_room_is_no_warm_reading(hass: HomeAssistant, freezer) -> None:
    _site_, controller, relay = await _site(hass, freezer, target_c=21.0)
    control: HeatControl = controller.heat_control
    shadow = controller.heat_shadow
    _passed(control)
    start = controller.demand.now().replace(second=0, microsecond=0)
    # A sensor warmed by sun climbs 0.15 °C every five minutes from 21.4 for well over an hour.
    for step in range(0, 18):
        now = start + 5 * step * MIN
        freezer.move_to(now)
        hass.states.async_set(ROOM, f"{21.4 + 0.15 * step:.2f}", {"unit_of_measurement": "°C", "device_class": "temperature"})
        hass.states.async_set(OUTDOOR, "4.0", {"unit_of_measurement": "°C", "device_class": "temperature"})
        shadow.tick(now)
        await hass.async_block_till_done()
        await control.async_look(now)
    assert not shadow.room_warm.active and relay.calls == []


async def test_a_cold_room_warming_fast_still_releases_and_blocks_the_plan(hass: HomeAssistant, freezer) -> None:
    """An aired bedroom coming back (16.0 -> 16.7 in half an hour) beside a quiet living room at 21.8: left out of the
    coldest counted room as a local rise, but the cold safety and the plan's guard still see it."""
    from collections import deque  # noqa: PLC0415

    bed = "sensor.sovrum"
    _site_, control, relay, now = await _running(hass, freezer, 0, 1, indoor_sensors=[ROOM, bed])
    hass.states.async_set(ROOM, "21.8", {"unit_of_measurement": "°C", "device_class": "temperature"})
    hass.states.async_set(bed, "21.8", {"unit_of_measurement": "°C", "device_class": "temperature"})
    await control.async_look(now)
    assert relay.calls == ["on"]
    freezer.move_to(now + 30 * MIN)
    hass.states.async_set(bed, "16.7", {"unit_of_measurement": "°C", "device_class": "temperature"})
    shadow = control.shadow
    shadow._rooms = {  # noqa: SLF001 - the rooms' readings of the last window, as the looks note them
        ROOM: deque([(now - 10 * MIN, 21.8), (now + 30 * MIN, 21.8)]),
        bed: deque([(now - 10 * MIN, 16.0), (now + 30 * MIN, 16.7)]),
    }
    rooms = shadow.indoor_now(shadow.config(), now + 30 * MIN)
    assert rooms is not None and rooms.coldest == ROOM
    assert shadow.coldest_now(shadow.config()) == pytest.approx(16.7)
    _plan(control, now + 30 * MIN, 0, 1)
    await control.async_look(now + 30 * MIN)
    assert relay.calls == ["on", "off"] and control.status()["reason"] == "room_cold"
