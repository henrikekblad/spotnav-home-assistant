"""A restart of Home Assistant in the middle of what the notifier is doing: nobody gets a notification twice, loses
one they should get, or gets a wrong one.

A restart here is the charger entry unloaded and set up again with the same stores, the clock moving on. Each case
of the notifier's in-memory state is one test; what the restart leaves to chance is said there:

* a new plan still settling, or owed after a plug-in, is judged again after the start (kept);
* a `plan_at_risk` held back while the car is away is told after the plug-in (Auto's first snapshot after the start
  says it again; nothing kept);
* a `plan_at_risk` told before the restart is not told again for the same departure (kept);
* the open window's `plan_stopped`: told once, and as `stopped` when it had charged (kept); a restart inside the
  grace period tells it a grace period after the start (later, not lost);
* the baseline: a charge running, a car plugged in, a charge completed before the restart are no events;
* the rate limits (`REPEAT_S`, `HOURLY_LIMIT`, and the app's wake-up's own) are not kept: a real event after the
  restart that the repeat rule would have held back is told, replacing the earlier one on the phone (same tag).

The question which car is plugged in is the identification's: a question open at a restart is asked again as a
plug-in seen late (`test_identification_history.py`).
"""

from __future__ import annotations

from dataclasses import replace
from datetime import time, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from homeassistant.core import HomeAssistant, ServiceCall
from pytest_homeassistant_custom_component.common import async_mock_service

from custom_components.spotnav.execution.chargers.adapter import ChargerAdapter
from custom_components.spotnav.execution.controller import ChargingPlan
from custom_components.spotnav.notifications import push as push_module
from custom_components.spotnav.notifications.notifier import ChargerNotifier, PLUG_IN_WAIT_S, SETTLE_S
from custom_components.spotnav.notifications.push import PushRegistration
from custom_components.spotnav.notifications.settings import NOTIFICATION_EVENTS
from custom_components.spotnav.notifications.unexpected_stop import GRACE_S
from custom_components.spotnav.runtime import charger_data, domain_data

from .test_notifications import _choose
from .test_plan_notify_noise import _go, at, plan_of
from .test_push import FakeRelay, REF
from .world import controller_of, setup_charger

#: The night's windows (UTC), before a departure at 07:30 local.
FIRST = (at(1, day=1), at(2, day=1))
LAST = (at(4, 15, day=1), at(5, 15, day=1))
AT_RISK = SimpleNamespace(reason="deadline_too_short", proposal=None, applied=True)
ON_TIME = SimpleNamespace(reason="ready", proposal=None, applied=True)
#: Every event but the identification question, which is not the notifier's to decide.
EVENTS = tuple(event for event in NOTIFICATION_EVENTS if event != "vehicle_identify")

NEW_PLAN = "New plan"
NOT_READY = "The charge will not be ready by the departure at 07:30."
NOT_STARTED = "Charging did not start as planned."
STOPPED = "Charging stopped during a planned window."
STARTED = "Charging started"
FINISHED = "The planned charge has finished."
PLUGGED_IN = "The car is plugged in."
UNPLUGGED = "The car is unplugged."


def night(periods: list[Any] | None = None, kwh: float = 30.0) -> ChargingPlan:
    """A plan as Auto installs and stores one (with the identity a restart restores it by)."""
    return plan_of(periods or [FIRST, LAST], kwh, auto_settings_revision=1, auto_price_identity="prices")


@pytest.fixture
def relay(monkeypatch: pytest.MonkeyPatch) -> FakeRelay:
    fake = FakeRelay()
    monkeypatch.setattr(push_module, "async_get_clientsession", lambda _hass: fake)
    return fake


class World:
    """One charger that says whether a car is there, a phone and the paired app, set up again by `restart`."""

    def __init__(self, hass: HomeAssistant, freezer: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        self.hass = hass
        self.freezer = freezer
        self.connected: bool | None = True
        self.takes_start = True
        self.starts: list[Any] = []
        self.calls: list[Any] = []
        self._stamp = 0
        # Every adapter, also the one a restart makes, reads the car from here.
        monkeypatch.setattr(ChargerAdapter, "reports_connection", property(lambda _adapter: True))
        monkeypatch.setattr(ChargerAdapter, "vehicle_connected", lambda _adapter: self.connected)
        # The night's snapshots are given by hand: Auto's own (settings incomplete here) are not heard.
        start = ChargerNotifier.async_start
        monkeypatch.setattr(ChargerNotifier, "async_start", lambda notifier, preview=None: start(notifier, None))

    async def setup(self, *, events: tuple[str, ...] = EVENTS) -> None:
        hass = self.hass
        hass.config.language = "en"
        self.freezer.move_to(at(14))
        self.entry = await setup_charger(hass, title="Garage")

        async def _turn_on(call: ServiceCall) -> None:
            self.starts.append(call)
            if self.takes_start:
                hass.states.async_set("switch.charger_a", "on")

        async def _turn_off(call: ServiceCall) -> None:
            hass.states.async_set("switch.charger_a", "off")

        hass.services.async_register("switch", "turn_on", _turn_on)
        hass.services.async_register("switch", "turn_off", _turn_off)
        self.calls = async_mock_service(hass, "notify", "mobile_app_pixel")
        await _choose(hass, self.entry.entry_id, targets=("mobile_app_pixel",), events=events)
        await domain_data(hass).auto_store.async_update(
            self.entry.entry_id, mutate=lambda settings: replace(settings, departure=time(7, 30))
        )
        await charger_data(hass, self.entry.entry_id).push.async_register(
            PushRegistration(push_ref=REF, events=events)
        )
        await self.car(True)
        await _go(hass, self.freezer, at(14, 5))  # the set-up's own writes are long past

    @property
    def controller(self) -> Any:
        return controller_of(self.hass, self.entry.entry_id)

    @property
    def notifier(self) -> Any:
        return charger_data(self.hass, self.entry.entry_id).notifier

    def told(self) -> list[str]:
        """What was told about the charge: neither a new plan (`plans_told`) nor the car coming or going."""
        return [
            message
            for message in (call.data["message"] for call in self.calls)
            if not message.startswith((NEW_PLAN, STARTED)) and message not in (PLUGGED_IN, UNPLUGGED)
        ]

    def plugs_told(self) -> list[str]:
        return [call.data["message"] for call in self.calls if call.data["message"] in (PLUGGED_IN, UNPLUGGED)]

    def starts_told(self) -> int:
        return sum(call.data["message"].startswith(STARTED) for call in self.calls)

    def plans_told(self) -> int:
        return sum(call.data["message"].startswith(NEW_PLAN) for call in self.calls)

    async def car(self, connected: bool | None) -> None:
        self.connected = connected
        self._stamp += 1
        state = self.hass.states.get("switch.charger_a")
        self.hass.states.async_set("switch.charger_a", "off" if state is None else state.state, {"stamp": self._stamp})
        await self.hass.async_block_till_done()

    async def snapshot(self, snapshot: Any) -> None:
        self.notifier._on_snapshot(snapshot)
        await self.hass.async_block_till_done()

    async def install(self, plan: ChargingPlan) -> None:
        await self.controller.async_install(plan)
        await self.hass.async_block_till_done()

    async def restart(self) -> None:
        assert await self.hass.config_entries.async_reload(self.entry.entry_id)
        await self.hass.async_block_till_done()


@pytest.fixture
async def world(hass: HomeAssistant, freezer: Any, monkeypatch: pytest.MonkeyPatch, relay: FakeRelay) -> World:
    made = World(hass, freezer, monkeypatch)
    await made.setup()
    return made


async def _go_to(world: World, moment: Any) -> None:
    await _go(world.hass, world.freezer, moment)


# ------------------------------------------------------------------ a new plan, settling or owed


async def test_a_plan_settling_at_a_restart_is_told_after_it(world: World, relay: FakeRelay) -> None:
    await world.install(night())
    await _go_to(world, at(14, 5, 10))
    await world.restart()
    assert world.plans_told() == 0
    await _go_to(world, at(14, 5, 10) + timedelta(seconds=SETTLE_S - 5))
    assert world.plans_told() == 0, "the plans calculated at the start settle first"
    await _go_to(world, at(14, 5, 10) + timedelta(seconds=SETTLE_S + 1))
    assert world.plans_told() == 1 and len(relay.requests) == 1
    await _go_to(world, at(15))
    assert world.plans_told() == 1


async def test_a_plan_quiet_after_a_persons_write_stays_quiet_across_a_restart(world: World) -> None:
    await domain_data(world.hass).auto_store.async_update(
        world.entry.entry_id, mutate=lambda settings: replace(settings, amps=12)
    )
    await world.install(night())
    await _go_to(world, at(14, 5, 10))
    await world.restart()
    await _go_to(world, at(15))
    assert world.plans_told() == 0


async def test_the_standing_plan_owed_after_a_plug_in_is_told_after_a_restart(world: World) -> None:
    await world.car(False)
    await world.install(night())
    await _go_to(world, at(15))
    await world.car(True)
    await _go_to(world, at(15, 10))
    await world.restart()
    await _go_to(world, at(15) + timedelta(seconds=PLUG_IN_WAIT_S - 60))
    assert world.plans_told() == 0, "still waiting for the plan made from the car's own reading"
    await _go_to(world, at(15) + timedelta(seconds=PLUG_IN_WAIT_S + 1))
    assert world.plans_told() == 1


async def test_a_told_plan_is_not_told_again_after_a_restart(world: World, relay: FakeRelay) -> None:
    await world.install(night())
    await _go_to(world, at(14, 10))
    assert world.plans_told() == 1
    await world.restart()
    await world.install(night())
    await _go_to(world, at(15))
    assert world.plans_told() == 1 and len(relay.requests) == 1


# ------------------------------------------------------------------ plan_at_risk


async def test_an_at_risk_held_back_while_away_is_told_after_a_restart_and_the_plug_in(world: World) -> None:
    await world.car(False)
    await world.install(night())
    await world.snapshot(AT_RISK)
    await world.restart()
    await world.snapshot(AT_RISK)  # Auto's first snapshot after the start
    assert world.told() == []
    await world.car(True)
    await _go_to(world, at(15))
    assert world.told() == [NOT_READY]


async def test_an_at_risk_told_before_a_restart_is_not_told_again_for_the_same_departure(
    world: World, relay: FakeRelay
) -> None:
    await world.install(night())
    await world.snapshot(AT_RISK)
    assert world.told() == [NOT_READY]
    woken = len(relay.requests)
    await world.restart()
    await world.snapshot(AT_RISK)
    assert world.told() == [NOT_READY] and len(relay.requests) == woken
    # It comes back after it was gone: told again, as without a restart (once the repeat rule allows).
    await _go_to(world, at(15))
    await world.snapshot(ON_TIME)
    await world.snapshot(AT_RISK)
    assert world.told() == [NOT_READY, NOT_READY]


async def test_an_at_risk_for_a_later_departure_after_a_restart_is_told(world: World) -> None:
    await world.install(night())
    await world.snapshot(AT_RISK)
    await world.restart()
    await _go_to(world, at(14, day=1))  # the departure the notice was about has passed
    await world.snapshot(AT_RISK)
    assert world.told().count(NOT_READY) == 2


# ------------------------------------------------------------------ plan_stopped


async def test_a_stop_told_before_a_restart_is_not_told_again(world: World, relay: FakeRelay) -> None:
    world.takes_start = False
    await world.install(night())
    await _go_to(world, FIRST[0])
    await _go_to(world, FIRST[0] + timedelta(seconds=95))
    await _go_to(world, FIRST[0] + timedelta(seconds=GRACE_S + 5))
    assert world.told() == [NOT_STARTED]
    woken = len(relay.requests)
    await _go_to(world, FIRST[0] + timedelta(minutes=5))
    await world.restart()
    for minutes in (6, 8, 10, 20, 30, 50):
        await _go_to(world, FIRST[0] + timedelta(minutes=minutes))
    assert world.told() == [NOT_STARTED] and len(relay.requests) == woken


async def test_a_charge_that_stopped_before_a_restart_is_told_as_stopped(world: World) -> None:
    await world.install(night())
    await _go_to(world, FIRST[0])
    assert world.controller.charging
    world.takes_start = False
    world.hass.states.async_set("switch.charger_a", "off")  # stopped by something other than SpotNav
    await _go_to(world, FIRST[0] + timedelta(minutes=10))
    await world.restart()
    for minutes in (11, 13, 15, 20):
        await _go_to(world, FIRST[0] + timedelta(minutes=minutes))
    assert world.told() == [STOPPED], "it had charged in this window"


async def test_a_restart_inside_the_grace_period_tells_the_trouble_a_grace_period_later(world: World) -> None:
    world.takes_start = False
    await world.install(night())
    await _go_to(world, FIRST[0])
    await _go_to(world, FIRST[0] + timedelta(seconds=100))
    await world.restart()
    await _go_to(world, FIRST[0] + timedelta(seconds=GRACE_S + 5))
    assert world.told() == [], "the trouble is looked at afresh from the start"
    await _go_to(world, FIRST[0] + timedelta(seconds=100 + 95))
    await _go_to(world, FIRST[0] + timedelta(seconds=100 + GRACE_S + 5))
    assert world.told() == [NOT_STARTED]


# ------------------------------------------------------------------ the baseline


async def test_a_running_charge_a_plugged_in_car_and_a_completed_charge_are_no_events_at_a_restart(
    world: World,
) -> None:
    await world.install(night([FIRST], 10.0))
    await _go_to(world, FIRST[0])
    assert world.controller.charging
    assert world.starts_told() == 1
    await world.restart()
    await _go_to(world, FIRST[0] + timedelta(minutes=5))
    assert world.starts_told() == 1, "a charge running at the restart is not a new one"
    await _go_to(world, FIRST[1] + timedelta(seconds=1))
    assert world.told() == [FINISHED]
    await world.restart()
    await _go_to(world, FIRST[1] + timedelta(minutes=5))
    assert world.told() == [FINISHED], "the completion is not told again"
    assert world.plugs_told() == [], "nor the car that stayed plugged in"


# ------------------------------------------------------------------ the rate limits are not kept


async def test_the_repeat_rule_starts_afresh_at_a_restart(world: World) -> None:
    hass = world.hass
    hass.states.async_set("switch.charger_a", "on")
    await hass.async_block_till_done()
    assert world.starts_told() == 1
    hass.states.async_set("switch.charger_a", "off")
    await hass.async_block_till_done()
    await world.restart()
    hass.states.async_set("switch.charger_a", "on")
    await hass.async_block_till_done()
    # A real second start within REPEAT_S: told, replacing the first on the phone (one tag per event).
    assert world.starts_told() == 2
    tags = {call.data["data"]["tag"] for call in world.calls if call.data["message"].startswith(STARTED)}
    assert len(tags) == 1


# ------------------------------------------------------------------ the stored record


async def test_a_record_an_earlier_release_stored_reads_as_it_is(
    hass: HomeAssistant, freezer: Any, monkeypatch: pytest.MonkeyPatch, relay: FakeRelay, hass_storage: dict
) -> None:
    key = "spotnav.notified_plan.entry_a"
    hass_storage[key] = {
        "version": 1,
        "minor_version": 1,
        "key": key,
        "data": {"fingerprint": "[]", "notice_seq": 3, "notice_at": at(12).isoformat(), "plug_in_told": True},
    }
    made = World(hass, freezer, monkeypatch)
    await made.setup()
    assert made.entry.entry_id == "entry_a"
    assert made.notifier._notified_plan == "[]"
    assert made.notifier.plan_notice == {"seq": 3, "at": at(12).isoformat()}
    await made.install(night())
    await _go_to(made, at(14, 10))
    assert hass_storage[key]["minor_version"] == 2
