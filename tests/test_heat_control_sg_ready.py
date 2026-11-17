"""SG Ready's raise in SpotNav's heat control (`heat/control.py`, `heat/relay_control.py`): contact B closed in the
plan's pre-heat hours, A in its lowered hours, (1,1) never, not even for an instant, and every exit back at (0,0).
Every write goes to a fake switch service in the test; nothing reaches a device."""

from __future__ import annotations

import asyncio

from datetime import datetime, timedelta
from typing import Any

import pytest
from homeassistant.const import EVENT_HOMEASSISTANT_STOP, EVENT_STATE_CHANGED
from homeassistant.core import CoreState, Event, HomeAssistant, ServiceCall

from custom_components.spotnav.const import CONF_HEAT
from custom_components.spotnav.heat.control import HeatControl
from custom_components.spotnav.heat.relay_control import (
    MAX_RAISE_DAY,
    RaiseCheck,
    RaiseSituation,
    Sample,
    decide_raise,
)
from custom_components.spotnav.heat.shadow_rule import BandHour
from custom_components.spotnav.runtime import site_controller_for

from pytest_homeassistant_custom_component.common import async_fire_time_changed

from .test_demand_site import pilot_site
from .test_heat_api import read

pytestmark = pytest.mark.usefixtures("offline_relay")

A = "switch.sg_a"
B = "switch.sg_b"
OUTDOOR = "sensor.ute"
ROOM = "sensor.vardagsrum"
SEC = timedelta(seconds=1)
MIN = timedelta(minutes=1)
H = timedelta(hours=1)


class Contacts:
    """Two fake switches on one service: each call recorded as `(entity, state)`; a contact can ignore calls or
    raise. Every state change is watched: A and B both on is a failure at once."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.calls: list[tuple[str, str]] = []
        self.ignore: set[str] = set()
        self.fail: set[str] = set()
        self.both_on = False
        for entity_id in (A, B):
            hass.states.async_set(entity_id, "off")
        hass.services.async_register("switch", "turn_on", self._on)
        hass.services.async_register("switch", "turn_off", self._off)
        hass.bus.async_listen(EVENT_STATE_CHANGED, self._changed)

    def _changed(self, _event: Event) -> None:
        if self.state(A) == "on" and self.state(B) == "on":
            self.both_on = True

    def _do(self, call: ServiceCall, state: str) -> None:
        ids = call.data["entity_id"]
        for entity_id in [ids] if isinstance(ids, str) else ids:
            if entity_id not in (A, B):
                continue
            self.calls.append((entity_id, state))
            if entity_id in self.fail:
                raise RuntimeError("contact offline")
            if entity_id not in self.ignore:
                self.hass.states.async_set(entity_id, state)

    async def _on(self, call: ServiceCall) -> None:
        self._do(call, "on")

    async def _off(self, call: ServiceCall) -> None:
        self._do(call, "off")

    def state(self, entity_id: str) -> str | None:
        state = self.hass.states.get(entity_id)
        return None if state is None else state.state

    @property
    def pair(self) -> tuple[int, int]:
        return (int(self.state(A) == "on"), int(self.state(B) == "on"))


def _plan(control: HeatControl, now: datetime, actions: dict[int, str]) -> None:
    """SpotNav's plan made now: `actions` per hour from the running one, the rest normal."""
    current = now.replace(minute=0, second=0, microsecond=0)
    control.shadow.plan = [
        BandHour(current + h * H, actions.get(h, "normal"), 21.0, "cheap_before_dear" if actions.get(h) == "preheat" else "dear_hour", 100.0)
        for h in range(-2, 12)
    ]
    control.shadow.made_at = now


async def _site(hass: HomeAssistant, freezer: Any, **heat: Any) -> tuple[Any, HeatControl, Contacts]:
    site = await pilot_site(hass, freezer)
    contacts = Contacts(hass)
    hass.states.async_set(OUTDOOR, "4.0", {"unit_of_measurement": "°C", "device_class": "temperature"})
    hass.states.async_set(ROOM, "21.0", {"unit_of_measurement": "°C", "device_class": "temperature"})
    config = {
        "control_path": "sg_ready",
        "control_entity": A,
        "control_entity_b": B,
        "outdoor_sensor": OUTDOOR,
        "indoor_sensors": [ROOM],
        "control_enabled": True,
        **heat,
    }
    hass.config_entries.async_update_entry(site, data={**site.data, CONF_HEAT: config})
    controller = site_controller_for(hass, site.entry_id)
    assert controller is not None
    await hass.async_block_till_done()
    control: HeatControl = controller.heat_control
    control.checks[A] = {"result": "passed", "outcome": "passed", "at": "2026-11-01T00:00:00+00:00"}
    return site, control, contacts


async def _running(hass: HomeAssistant, freezer: Any, actions: dict[int, str], **heat: Any) -> tuple[Any, HeatControl, Contacts, datetime]:
    """A site on the SG Ready path whose check passed and whose data has been good for ten minutes, at a whole hour."""
    site, control, contacts = await _site(hass, freezer, **heat)
    now = control.site.demand.now().replace(minute=0, second=0, microsecond=0) + H
    _plan(control, now - 11 * MIN, {})
    await control.async_look(now - 11 * MIN)
    _plan(control, now, actions)
    return site, control, contacts, now


async def _look(control: HeatControl, now: datetime, actions: dict[int, str] | None = None) -> None:
    if actions is not None:
        _plan(control, now, actions)
    await control.async_look(now)


# ------------------------------------------------------------------------------------------------ the states


async def test_a_preheat_hour_raises_and_a_lowered_hour_lowers_through_normal(hass: HomeAssistant, freezer) -> None:
    _site_, control, contacts, now = await _running(hass, freezer, {0: "preheat", 1: "lower", 2: "lower"})
    await _look(control, now)
    assert contacts.pair == (0, 1) and contacts.calls == [(B, "on")]
    status = control.status()
    assert status["raised"] is True and status["raise_until"] == (now + H).isoformat() and status["lowered"] is False
    await _look(control, now + 30 * MIN)
    assert contacts.calls == [(B, "on")]
    # The lowered run begins: B opens and is read back before A closes.
    await _look(control, now + H, {0: "lower", 1: "lower"})
    assert contacts.calls == [(B, "on"), (B, "off"), (A, "on")] and contacts.pair == (1, 0)
    assert control.raised_minutes([now], now + 2 * H) == {now: 60}
    # And back to normal at the run's end.
    await _look(control, now + 3 * H, {})
    assert contacts.pair == (0, 0) and not contacts.both_on


async def test_a_lowering_ending_into_a_raise_opens_a_first(hass: HomeAssistant, freezer) -> None:
    _site_, control, contacts, now = await _running(hass, freezer, {0: "lower", 1: "preheat"})
    await _look(control, now)
    assert contacts.pair == (1, 0)
    await _look(control, now + H, {0: "preheat"})
    assert contacts.calls == [(A, "on"), (A, "off"), (B, "on")] and contacts.pair == (0, 1) and not contacts.both_on


async def test_b_that_does_not_open_keeps_a_open(hass: HomeAssistant, freezer) -> None:
    _site_, control, contacts, now = await _running(hass, freezer, {0: "preheat", 1: "lower", 2: "lower"})
    await _look(control, now)
    contacts.ignore.add(B)
    for minute in range(0, 10):
        await _look(control, now + H + minute * MIN, {0: "lower", 1: "lower"})
    assert (A, "on") not in contacts.calls and contacts.state(A) == "off" and not contacts.both_on


async def test_b_whose_opening_raises_keeps_a_open(hass: HomeAssistant, freezer) -> None:
    _site_, control, contacts, now = await _running(hass, freezer, {0: "preheat", 1: "lower", 2: "lower"})
    await _look(control, now)
    contacts.fail.add(B)
    for minute in range(0, 10):
        await _look(control, now + H + minute * MIN, {0: "lower", 1: "lower"})
    assert (A, "on") not in contacts.calls and not contacts.both_on
    # Back: B opens, then A closes.
    contacts.fail.clear()
    for minute in range(10, 20):
        await _look(control, now + H + minute * MIN, {0: "lower", 1: "lower"})
    assert contacts.pair == (1, 0) and not contacts.both_on


@pytest.mark.parametrize("trouble", ["ignore", "fail"])
async def test_a_that_does_not_open_keeps_b_open(hass: HomeAssistant, freezer, trouble: str) -> None:
    _site_, control, contacts, now = await _running(hass, freezer, {0: "lower", 1: "preheat"})
    await _look(control, now)
    getattr(contacts, trouble).add(A)
    for minute in range(0, 30):
        await _look(control, now + H + minute * MIN, {0: "preheat"})
    assert (B, "on") not in contacts.calls and not contacts.both_on


async def test_b_closed_late_while_a_closes_is_never_both(hass: HomeAssistant, freezer) -> None:
    # B's closing is not seen at once; the plan turns to lowering meanwhile: B is asked to open, and A waits a while
    # since B's closing could still take.
    _site_, control, contacts, now = await _running(hass, freezer, {0: "preheat", 1: "lower"})
    contacts.ignore.add(B)
    await _look(control, now)
    assert control.pending_b is not None
    await _look(control, now + 2 * MIN, {0: "lower", 1: "lower"})
    assert (A, "on") not in contacts.calls and contacts.calls[-1] == (B, "off")
    # The late closing takes now, after SpotNav already asked B to open: SpotNav's own, opened once.
    contacts.ignore.clear()
    hass.states.async_set(B, "on")
    for minute in range(3, 15):
        await _look(control, now + minute * MIN, {0: "lower", 1: "lower"})
    assert not contacts.both_on and contacts.pair == (1, 0)


# ------------------------------------------------------------------------------------------------ the exits


async def _raised(hass: HomeAssistant, freezer: Any) -> tuple[Any, HeatControl, Contacts, datetime]:
    site, control, contacts, now = await _running(hass, freezer, {0: "preheat", 1: "preheat"})
    await _look(control, now)
    assert contacts.pair == (0, 1)
    return site, control, contacts, now


async def test_turning_off_opens_b(hass: HomeAssistant, freezer) -> None:
    site, control, contacts, now = await _raised(hass, freezer)
    heat = site.data[CONF_HEAT]
    hass.config_entries.async_update_entry(site, data={**site.data, CONF_HEAT: {**heat, "control_enabled": False}})
    await _look(control, now + 5 * MIN)
    assert contacts.pair == (0, 0) and not control.raised


async def test_the_stop_opens_b(hass: HomeAssistant, freezer) -> None:
    _site_, control, contacts, _now = await _raised(hass, freezer)
    await control.async_shutdown()
    assert contacts.pair == (0, 0) and not control.raised


async def test_home_assistants_stop_opens_b(hass: HomeAssistant, freezer) -> None:
    _site_, control, contacts, _now = await _raised(hass, freezer)
    hass.set_state(CoreState.stopping)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
    await hass.async_block_till_done()
    hass.set_state(CoreState.running)
    assert contacts.pair == (0, 0)


async def test_unloading_opens_b(hass: HomeAssistant, freezer) -> None:
    site, _control, contacts, _now = await _raised(hass, freezer)
    assert await hass.config_entries.async_unload(site.entry_id)
    await hass.async_block_till_done()
    assert contacts.pair == (0, 0)


async def test_a_restart_opens_b_held_at_the_stop(hass: HomeAssistant, freezer) -> None:
    _site_, control, contacts, now = await _raised(hass, freezer)
    contacts.fail.add(B)
    await control.async_shutdown()
    assert control.raised and contacts.pair == (0, 1)
    fresh = HeatControl(hass, control.shadow)
    fresh._restore(control._data())  # noqa: SLF001 - what the store keeps
    fresh._loaded = True  # noqa: SLF001
    fresh.checks[A] = {"result": "passed"}
    contacts.fail.clear()
    await _look(fresh, now + 5 * MIN, {0: "preheat"})
    assert contacts.pair == (0, 0) and not fresh.raised


async def test_an_unavailable_a_opens_b(hass: HomeAssistant, freezer) -> None:
    _site_, control, contacts, now = await _raised(hass, freezer)
    hass.states.async_set(A, "unavailable")
    await _look(control, now + 5 * MIN)
    assert contacts.state(B) == "off" and not control.raised


async def test_an_unavailable_b_is_opened_once_it_reads(hass: HomeAssistant, freezer) -> None:
    _site_, control, contacts, now = await _raised(hass, freezer)
    hass.states.async_set(B, "unavailable")
    await _look(control, now + 5 * MIN)
    assert contacts.calls == [(B, "on")]
    hass.states.async_set(B, "on")
    await _look(control, now + 6 * MIN)
    assert contacts.pair == (0, 0) and not control.raised


async def test_a_removed_b_held_closed_is_opened_then_let_go(hass: HomeAssistant, freezer) -> None:
    site, control, contacts, now = await _raised(hass, freezer)
    heat = site.data[CONF_HEAT]
    hass.config_entries.async_update_entry(site, data={**site.data, CONF_HEAT: {**heat, "control_path": "relay", "control_entity_b": None}})
    await _look(control, now + 5 * MIN)
    assert contacts.pair == (0, 0) and not control.raised and control.raise_entity is None


async def test_an_automation_on_b_blocks_and_opens_b(hass: HomeAssistant, freezer) -> None:
    _site_, control, contacts, now = await _raised(hass, freezer)
    control._blocker = lambda _config, _relay: "automation"  # type: ignore[method-assign]  # noqa: SLF001
    await _look(control, now + 5 * MIN)
    assert contacts.pair == (0, 0) and control.status()["state"] == "waiting"


async def test_an_error_opens_b(hass: HomeAssistant, freezer) -> None:
    _site_, control, contacts, now = await _raised(hass, freezer)

    def broken(*_args: Any) -> Any:
        raise RuntimeError("boom")

    control._safety = broken  # type: ignore[method-assign]  # noqa: SLF001
    await _look(control, now + 5 * MIN)
    assert contacts.pair == (0, 0) and not control.raised


async def test_b_opened_by_someone_else_sets_raising_aside_for_the_day(hass: HomeAssistant, freezer) -> None:
    _site_, control, contacts, now = await _raised(hass, freezer)
    hass.states.async_set(B, "off")
    await _look(control, now + 5 * MIN)
    await _look(control, now + 7 * MIN)
    assert not control.raised and control.raise_paused_reason == "foreign_change"
    await _look(control, now + 2 * H, {0: "preheat"})
    assert contacts.calls == [(B, "on")]
    # Lowering goes on.
    await _look(control, now + 2 * H + MIN, {0: "lower"})
    assert contacts.pair == (1, 0)


async def test_a_closed_by_hand_while_b_is_raised_opens_b_at_the_next_look(hass: HomeAssistant, freezer) -> None:
    # The household closes A (by hand, a script, Node-RED) while SpotNav holds B: B opens at once, not after A's
    # change is confirmed a minute later.
    _site_, control, contacts, now = await _raised(hass, freezer)
    await _look(control, now + 10 * MIN)
    hass.states.async_set(A, "on")
    await _look(control, now + 10 * MIN + 30 * SEC)
    assert contacts.pair == (1, 0) and not control.raised and contacts.calls == [(B, "on"), (B, "off")]
    assert control.status()["state"] == "waiting" and control.raise_decision.reason == "raise_interlock"  # type: ignore[union-attr]
    # Confirmed: SpotNav pauses and never touches A, which is the household's.
    for step in range(2, 8):
        await _look(control, now + 10 * MIN + step * 30 * SEC)
    assert control.paused_reason == "foreign_change" and contacts.calls == [(B, "on"), (B, "off")] and contacts.pair == (1, 0)


def _no_ticks(control: HeatControl) -> None:
    """Only a watched contact's change can start a look from here on (the 30-second looks stop)."""
    assert control._timer_cancel is not None  # noqa: SLF001
    control._timer_cancel()  # noqa: SLF001
    control._timer_cancel = None  # noqa: SLF001


async def _seconds_later(hass: HomeAssistant, freezer: Any, control: HeatControl, seconds: int) -> None:
    freezer.tick(timedelta(seconds=seconds))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    while control._task is not None and not control._task.done():  # noqa: SLF001
        await asyncio.sleep(0)


async def test_a_closed_by_hand_while_b_is_raised_opens_b_within_seconds(hass: HomeAssistant, freezer) -> None:
    # The change is acted on when it happens (the contacts are watched), not at the next 30-second look.
    _site_, control, contacts, now = await _raised(hass, freezer)
    _no_ticks(control)
    freezer.move_to(now + 10 * MIN)
    await _look(control, now + 10 * MIN)
    hass.states.async_set(A, "on")
    await hass.async_block_till_done()
    assert contacts.pair == (1, 1)  # the household's own write, for a moment
    await _seconds_later(hass, freezer, control, 2)
    assert contacts.pair == (1, 0) and not control.raised and contacts.calls == [(B, "on"), (B, "off")]


async def test_spotnavs_own_writes_ask_for_no_extra_look(hass: HomeAssistant, freezer) -> None:
    _site_, control, contacts, now = await _running(hass, freezer, {0: "preheat", 1: "preheat"})
    looks: list[datetime] = []
    real = control.async_look

    async def counted(at: datetime) -> None:
        looks.append(at)
        await real(at)

    control.async_look = counted  # type: ignore[method-assign]
    _no_ticks(control)
    freezer.move_to(now)
    await _look(control, now)
    assert contacts.pair == (0, 1)
    await _seconds_later(hass, freezer, control, 5)
    assert looks == [now]


async def test_a_closed_by_hand_while_a_closing_of_b_is_on_its_way_opens_b(hass: HomeAssistant, freezer) -> None:
    _site_, control, contacts, now = await _running(hass, freezer, {0: "preheat", 1: "preheat"})
    contacts.ignore.add(B)
    await _look(control, now)
    assert control.pending_b is not None and control.pending_b.closed
    contacts.ignore.clear()
    hass.states.async_set(A, "on")
    await _look(control, now + 30 * SEC)
    assert contacts.calls[-1] == (B, "off") and contacts.state(B) == "off"


async def test_b_closed_by_someone_else_is_never_taken_over_and_keeps_a_open(hass: HomeAssistant, freezer) -> None:
    _site_, control, contacts, now = await _running(hass, freezer, {0: "lower", 1: "lower"})
    hass.states.async_set(B, "on")
    await _look(control, now)
    assert contacts.calls == [] and control.status()["reason"] == "sg_ready_b"


# ------------------------------------------------------------------------------------------------ the guards


def _s(**changes: Any) -> RaiseSituation:
    now = datetime(2026, 11, 17, 10, 0)
    base: dict[str, Any] = {
        "now": now,
        "plan_until": now + H,
        "held_since": None,
        "last_end": None,
        "raised_24h": timedelta(0),
        "writes_today": 0,
    }
    return RaiseSituation(**{**base, **changes})


def test_the_raise_rules() -> None:
    now = datetime(2026, 11, 17, 10, 0)
    assert decide_raise(_s()).closed
    # At least 30 minutes: held after the plan stops, then let go.
    assert decide_raise(_s(plan_until=None, held_since=now - 10 * MIN)).reason == "raise_min"
    assert not decide_raise(_s(plan_until=None, held_since=now - 31 * MIN)).closed
    assert decide_raise(_s(plan_until=now + 20 * MIN)).reason == "raise_too_short"
    # At most three hours in a row and six in a rolling day.
    assert decide_raise(_s(plan_until=now + 4 * H, held_since=now - 3 * H)).reason == "raise_max_run"
    assert decide_raise(_s(raised_24h=MAX_RAISE_DAY)).reason in ("raise_too_short", "raise_day_cap")
    assert decide_raise(_s(raised_24h=MAX_RAISE_DAY, held_since=now - H)).reason == "raise_day_cap"
    # An hour of rest after the last raise; room left for the lowering's two writes.
    assert decide_raise(_s(last_end=now - 30 * MIN)).reason == "raise_rest"
    assert decide_raise(_s(writes_today=3)).reason == "raise_write_cap"
    assert decide_raise(_s(writes_today=2)).closed
    # A block ends a raise at once, also before 30 minutes.
    assert decide_raise(_s(held_since=now - 5 * MIN, block="raise_warm")).reason == "raise_warm"
    # Settling starts none but ends none.
    assert not decide_raise(_s(block="raise_settling")).closed
    assert decide_raise(_s(held_since=now - 5 * MIN, block="raise_settling")).closed


async def test_a_raise_lasts_at_least_30_minutes(hass: HomeAssistant, freezer) -> None:
    _site_, control, contacts, now = await _running(hass, freezer, {0: "preheat"})
    await _look(control, now + 5 * MIN)
    await _look(control, now + 20 * MIN, {})
    assert contacts.pair == (0, 1) and control.raise_decision.reason == "raise_min"  # type: ignore[union-attr]
    await _look(control, now + 36 * MIN, {})
    assert contacts.pair == (0, 0)


async def test_a_raise_ends_after_three_hours_and_rests(hass: HomeAssistant, freezer) -> None:
    run = {h: "preheat" for h in range(0, 6)}
    _site_, control, contacts, now = await _running(hass, freezer, run)
    await _look(control, now)
    await _look(control, now + 3 * H, run)
    assert contacts.pair == (0, 0) and control.raise_decision.reason == "raise_max_run"  # type: ignore[union-attr]
    await _look(control, now + 3 * H + 30 * MIN, run)
    assert contacts.pair == (0, 0) and control.raise_decision.reason == "raise_rest"  # type: ignore[union-attr]


async def test_the_raised_hours_cap_is_a_rolling_day(hass: HomeAssistant, freezer) -> None:
    _site_, control, contacts, now = await _running(hass, freezer, {0: "preheat"})
    control.raisings = [[now - 20 * H, now - 14 * H]]
    await _look(control, now)
    assert contacts.calls == [] and control.raise_decision.reason in ("raise_too_short", "raise_day_cap")  # type: ignore[union-attr]


@pytest.mark.parametrize("room", ["21.5", "22.0"])
async def test_no_raise_into_a_warm_house_and_a_raise_ends_at_the_high_edge(hass: HomeAssistant, freezer, room: str) -> None:
    _site_, control, contacts, now = await _running(hass, freezer, {0: "preheat"})
    hass.states.async_set(ROOM, room, {"unit_of_measurement": "°C"})
    await _look(control, now)
    assert contacts.calls == [] and control.raise_decision.reason == "raise_warm"  # type: ignore[union-attr]
    hass.states.async_set(ROOM, "21.0", {"unit_of_measurement": "°C"})
    await _look(control, now + MIN)
    assert contacts.pair == (0, 1)
    # The room reaches the high edge: the raise ends at once, before its 30 minutes.
    hass.states.async_set(ROOM, "21.5", {"unit_of_measurement": "°C"})
    await _look(control, now + 5 * MIN)
    assert contacts.pair == (0, 0)


async def test_no_raise_while_the_room_warm_rule_is_on(hass: HomeAssistant, freezer) -> None:
    _site_, control, contacts, now = await _running(hass, freezer, {0: "preheat"})
    control.shadow.room_warm.active = True
    control.shadow._step_warm = lambda *_args: None  # type: ignore[method-assign]  # noqa: SLF001
    await _look(control, now)
    assert contacts.calls == [] and control.raise_decision.reason == "raise_warm"  # type: ignore[union-attr]


async def test_no_raise_in_the_cooler_night(hass: HomeAssistant, freezer) -> None:
    _site_, control, contacts, now = await _running(hass, freezer, {0: "preheat"}, night={"on": True, "delta_c": 1.0, "start": "00:00", "end": "23:59"})
    await _look(control, now)
    assert contacts.calls == [] and control.raise_decision.reason == "raise_night"  # type: ignore[union-attr]


async def test_no_raise_while_away_but_before_the_return(hass: HomeAssistant, freezer) -> None:
    site, control, contacts = await _site(hass, freezer)
    now = control.site.demand.now().replace(minute=0, second=0, microsecond=0) + H
    heat = site.data[CONF_HEAT]
    hass.config_entries.async_update_entry(site, data={**site.data, CONF_HEAT: {**heat, "mode": "away", "away_until": (now + 10 * H).isoformat()}})
    await hass.async_block_till_done()
    _plan(control, now - 11 * MIN, {})
    await control.async_look(now - 11 * MIN)
    await _look(control, now, {0: "preheat"})
    assert contacts.calls == [] and control.raise_decision.reason == "raise_away"  # type: ignore[union-attr]
    # The warm-up before the return may raise.
    await _look(control, now + 8 * H + MIN, {0: "return", 1: "return"})
    assert contacts.pair == (0, 1)


# ------------------------------------------------------------------------------------------------ the raise check


def _watch(samples: list[Sample]) -> tuple[RaiseCheck, list[str | None]]:
    check = RaiseCheck()
    start = datetime(2026, 11, 17, 10, 0)
    check.begin(start, samples[:1] * 5)
    outcomes = [check.step(start + (i + 1) * MIN, sample) for i, sample in enumerate(samples[1:])]
    return check, outcomes


def test_a_raise_seen_on_the_supply_passes() -> None:
    check, _ = _watch([Sample(supply_c=35.0)] + [Sample(supply_c=35.2)] * 3 + [Sample(supply_c=36.4)] * 2)
    assert check.result == "passed"


def test_no_rise_on_the_supply_is_raise_not_observed() -> None:
    check, _ = _watch([Sample(supply_c=35.0)] * 30)
    assert check.result == "failed" and check.last == "failed:raise_not_observed"


def test_the_power_alone_never_fails_and_passes_on_two_raises() -> None:
    check, _ = _watch([Sample(1.0, "heating")] * 30)
    assert check.result is None and check.last == "not_judgeable:no_effect"
    start = datetime(2026, 11, 17, 12, 0)
    for day in range(2):
        check.begin(start, [Sample(1.0, "heating")])
        check.step(start + MIN, Sample(1.5, "heating"))
        check.step(start + 2 * MIN, Sample(1.5, "heating"))
        start += 3 * H
        assert check.result == (None if day == 0 else "passed")


async def test_a_pump_that_does_not_answer_b_stops_raising_but_keeps_lowering(hass: HomeAssistant, freezer) -> None:
    _site_, control, contacts, now = await _running(hass, freezer, {0: "preheat", 1: "lower"})

    async def sample(_now: datetime) -> Sample:
        control.signal = True
        return Sample(supply_c=35.0)

    control.async_sample = sample  # type: ignore[method-assign]
    for minute in range(-5, 0):
        await _look(control, now + minute * MIN, {0: "normal", 1: "preheat", 2: "lower"} if minute < 0 else None)
    for minute in range(0, 30):
        await _look(control, now + minute * MIN, {0: "preheat", 1: "lower"})
    assert control.raise_check.result == "failed"
    assert contacts.pair == (0, 0) and control.raise_decision.reason == "raise_not_observed"  # type: ignore[union-attr]
    diagnostics = control.diagnostics()["raise"]
    assert diagnostics["observed"] == "raise_not_observed"
    # The card shows no alert for it, and lowering goes on.
    assert "raise" not in str(control.status().get("reason"))
    await _look(control, now + H, {0: "lower"})
    assert contacts.pair == (1, 0)


# ------------------------------------------------------------------------------------------------ shown, and the relay


async def test_the_card_shows_raised_minutes_and_would_raise_hours(hass: HomeAssistant, freezer) -> None:
    site, control, _contacts, now = await _raised(hass, freezer)
    await _look(control, now + 30 * MIN)
    freezer.move_to(now + 30 * MIN)
    answer = (await read(hass, api_version=1, site_id=site.entry_id))["result"]
    assert answer["control"]["raised"] is True
    current = next(hour for hour in answer["band"]["hours"] if hour["start"] == now.isoformat())
    assert current["raised_min"] == 30


async def test_the_relay_path_never_raises(hass: HomeAssistant, freezer) -> None:
    site, control, contacts = await _site(hass, freezer, control_path="relay", control_entity_b=None)
    now = control.site.demand.now().replace(minute=0, second=0, microsecond=0) + H
    _plan(control, now - 11 * MIN, {})
    await control.async_look(now - 11 * MIN)
    await _look(control, now, {0: "preheat", 1: "lower"})
    assert contacts.calls == [] and control.raise_decision is None
    await _look(control, now + H, {0: "lower"})
    assert contacts.calls == [(A, "on")]
    freezer.move_to(now + H)
    answer = (await read(hass, api_version=1, site_id=site.entry_id))["result"]
    assert all(hour["raised_min"] is None for hour in answer["band"]["hours"])
    assert answer["control"]["raised"] is False


async def test_a_warm_house_is_lowered_through_a_and_never_raised(hass: HomeAssistant, freezer) -> None:
    """The room-warm rule lowers on SG Ready as on the relay path: (1,0) through A, and no raise while it holds."""
    _site_, control, contacts = await _site(hass, freezer, target_c=21.0)
    shadow = control.shadow
    start = control.site.demand.now().replace(second=0, microsecond=0)

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
    assert contacts.calls == [] and not shadow.room_warm.active
    now = await at(66, "22.3")
    assert shadow.room_warm.active and contacts.calls == [(A, "on")] and contacts.pair == (1, 0)
    running = next(hour for hour in shadow.plan if hour.start <= now < hour.start + H)
    assert (running.action, running.reason) == ("lower", "room_warm")
    # Even an hour the plan would pre-heat raises nothing while the rule holds.
    _plan(control, now + 50 * MIN, {0: "preheat"})
    control.shadow.made_at = now + 50 * MIN
    shadow.tick = lambda _now: None  # type: ignore[method-assign]
    await control.async_look(now + 50 * MIN)
    assert (B, "on") not in contacts.calls and not contacts.both_on
    assert control.raise_decision is not None and control.raise_decision.reason in ("raise_warm", "raise_interlock")
