"""The field night of 2026-11-27: a Charge Amps HALO over OCPP whose link drops every two to three minutes, no car
plugged in since the afternoon before (the charger said `Available`), and a departure plan for the last session's
energy. At 05:45 local a re-arm inside the open window came while the charger's entities were unavailable: the
reading said nothing, so the window's start was sent to an empty, offline charger (its current write failed, its
switch was unavailable, and the start was owed "until the integration has loaded").

Now a charger that says nothing at that moment counts as what it last said: after an unplug, an automatic start
(a window's, the car's minimum level's) and the re-arm's stop of an empty charger are not sent. The plan stays (it is
made ahead for the next plug-in, shown in the card), nothing is told on the phones while the car is away, and a car
that plugs in later is started, and told about if its departure cannot be met. A charger that never said (after a
restart) is started as before.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from homeassistant.core import HomeAssistant

from custom_components.spotnav.core import events as ev
from custom_components.spotnav.core.ownership import car_away, decide
from custom_components.spotnav.core.session import ChargeSession

from custom_components.spotnav.notifications import push as push_module

from .test_plan_notify_noise import _go, plan_of
from .test_push import FakeRelay
from .test_unplugged_plan_notices import _world, AT_RISK, FIRST, LAST

T0 = datetime(2026, 11, 27, 4, 45, tzinfo=timezone.utc)
UNPLUGGED = ChargeSession(plugged=False)


@pytest.fixture
def relay(monkeypatch: pytest.MonkeyPatch) -> FakeRelay:
    fake = FakeRelay()
    monkeypatch.setattr(push_module, "async_get_clientsession", lambda _hass: fake)
    return fake


def _kinds(session: ChargeSession, event: ev.Event) -> list[str]:
    _, commands = decide(session, event, T0)
    return [command.kind for command in commands if command.kind in ("start", "stop")]


@pytest.mark.parametrize(
    ("plugged", "connected", "away"),
    [
        (False, None, True),
        (False, False, True),
        (None, None, False),
        (True, None, False),
        (False, True, False),
    ],
)
def test_an_offline_charger_counts_as_what_it_last_said(plugged: bool | None, connected: bool | None, away: bool) -> None:
    assert car_away(ChargeSession(plugged=plugged), connected) is away


@pytest.mark.parametrize("trigger", [ev.TRIGGER_TIMER, ev.TRIGGER_REARM])
def test_a_window_opening_at_an_offline_charger_after_an_unplug_starts_nothing(trigger: str) -> None:
    assert _kinds(UNPLUGGED, ev.WindowStart(trigger=trigger, open_window_start=T0, connected=None)) == []
    # A charger that never said anything since a restart is started as before.
    assert _kinds(ChargeSession(), ev.WindowStart(trigger=trigger, open_window_start=T0, connected=None)) == ["start"]


def test_the_minimum_level_starts_nothing_at_an_offline_charger_after_an_unplug() -> None:
    assert _kinds(UNPLUGGED, ev.MinSocStart(connected=None)) == []
    assert _kinds(ChargeSession(plugged=True), ev.MinSocStart(connected=None)) == ["start"]


def test_the_re_arm_sends_no_stop_to_an_offline_empty_charger() -> None:
    assert _kinds(UNPLUGGED, ev.Rearm(connected=None, charging=False)) == []


def _decided(world: Any) -> list[str]:
    """The starts and stops the core decided since the unplug."""
    events = world.controller.session_runner.diagnostics()["events"]
    unplug = max(index for index, entry in enumerate(events) if entry["event"]["kind"] == "unplug")
    return [command["kind"] for entry in events[unplug:] for command in entry["core"] if command["kind"] in ("start", "stop")]


async def _offline(world: Any) -> None:
    """The OCPP link drops: the charger's entities are unavailable and its reading says nothing."""
    assert world.car is not None
    await world.car.set(None)
    world.hass.states.async_set("switch.charger_a", "unavailable")
    await world.hass.async_block_till_done()


async def _online(world: Any, connected: bool) -> None:
    world.hass.states.async_set("switch.charger_a", "off")
    assert world.car is not None
    await world.car.set(connected)


async def test_the_field_night_offline_and_unplugged_sends_and_tells_nothing(
    hass: HomeAssistant, freezer: Any, relay: FakeRelay, caplog: pytest.LogCaptureFixture
) -> None:
    world = await _world(hass, freezer, connected=False)
    assert world.controller.session_runner.session.plugged is False
    await world.install(plan_of([FIRST, LAST], 25.5))
    # The link flaps through the night; the last window opens (and the plan is installed again) while it is down.
    await _go(hass, freezer, FIRST[0] - timedelta(minutes=1))
    await _offline(world)
    await _go(hass, freezer, FIRST[0])
    await _online(world, False)
    await _offline(world)
    await world.install(plan_of([LAST], 25.5))
    await _go(hass, freezer, LAST[0])
    assert not world.controller.commands_owed
    world.snapshot(AT_RISK)
    await hass.async_block_till_done()
    await _go(hass, freezer, LAST[0] + timedelta(minutes=30))

    assert _decided(world) == [], "no start nor stop for an empty charger"
    assert world.starts == []
    assert not world.controller.commands_owed, "nothing is owed to it either"
    assert "was not sent" not in caplog.text
    assert world.controller.plan is not None, "the plan is kept for the next plug-in"
    assert world.told() == [] and relay.requests == [], "nothing on the phones nor in the app's wake-up"


async def test_a_car_plugged_in_after_the_outage_is_started_and_told_it_will_not_be_ready(
    hass: HomeAssistant, freezer: Any, relay: FakeRelay
) -> None:
    world = await _world(hass, freezer, connected=False)
    await world.install(plan_of([FIRST, LAST], 25.5))
    await _go(hass, freezer, FIRST[0] - timedelta(minutes=1))
    await _offline(world)
    await _go(hass, freezer, FIRST[0])
    world.snapshot(AT_RISK)
    await hass.async_block_till_done()
    assert world.starts == [] and world.told() == []
    # The link comes back with the car plugged in inside the window.
    await _online(world, True)
    await _go(hass, freezer, FIRST[0] + timedelta(minutes=1))
    assert len(world.starts) == 1, "the plug-in inside the window starts it"
    await _go(hass, freezer, FIRST[0] + timedelta(minutes=40))
    assert world.told() == ["The charge will not be ready by the departure at 07:30."]
