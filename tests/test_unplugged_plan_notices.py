"""No charge outcome told about a charge that could not happen: the charger says no car is plugged in.

The field night of 2026-10-10 at a Charge Amps charger over OCPP, two cars at home, neither plugged in, a departure
at 07:30. Since 1.14.1 a plan is made while the car is away, so the plan's windows opened at an empty charger. The
window's start was sent all the same (an OCPP charger takes a remote start without a car and waits for one), and the
phones were told at 03:57 that the charge would not be ready by 07:30 (`plan_at_risk`), at about 07:27 that it did not
start as planned (`plan_stopped`, `not_started`) and at about 09:27 that the planned charge had finished
(`charge_complete`, `plan_done`: the charge control was on when the last window ended, with nothing delivered).

Now a window that opens with no car starts nothing (a plug-in inside it starts it, as designed), and while the
charger says no car is plugged in none of these three is told, on the phones and in the app's wake-up alike. An
unknown reading keeps the last known one; a charger that cannot say tells as before. A plugged-in car that does not
start, cannot be ready in time or finishes is told exactly as before.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import time, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from homeassistant.core import HomeAssistant, ServiceCall
from pytest_homeassistant_custom_component.common import async_mock_service

from custom_components.spotnav.api import dashboard as dashboard_api
from custom_components.spotnav.execution.controller import ChargingPlan
from custom_components.spotnav.notifications import push as push_module
from custom_components.spotnav.notifications.push import PushRegistration
from custom_components.spotnav.notifications.settings import DEFAULT_EVENTS
from custom_components.spotnav.notifications.unexpected_stop import GRACE_S
from custom_components.spotnav.runtime import charger_data, domain_data

from .test_notifications import _choose
from .test_plan_notify_noise import _go, at, Car, plan_of
from .test_push import FakeRelay, REF
from .world import controller_of, setup_charger

#: The night's windows (UTC): 01:00-02:00 and 04:15-05:15, before a departure at 07:30 local (05:30 UTC).
FIRST = (at(1, day=1), at(2, day=1))
LAST = (at(4, 15, day=1), at(5, 15, day=1))
AT_RISK = SimpleNamespace(reason="deadline_too_short", proposal=None, applied=True)
ON_TIME = SimpleNamespace(reason="ready", proposal=None, applied=True)


@pytest.fixture
def relay(monkeypatch: pytest.MonkeyPatch) -> FakeRelay:
    fake = FakeRelay()
    monkeypatch.setattr(push_module, "async_get_clientsession", lambda _hass: fake)
    return fake


class World:
    def __init__(self, hass: HomeAssistant, entry: Any, calls: list[Any], starts: list[Any], car: Car | None) -> None:
        self.hass = hass
        self.entry = entry
        self.calls = calls
        self.starts = starts
        self.car = car

    @property
    def controller(self) -> Any:
        return controller_of(self.hass, self.entry.entry_id)

    @property
    def notifier(self) -> Any:
        return charger_data(self.hass, self.entry.entry_id).notifier

    def told(self) -> list[str]:
        return [call.data["message"] for call in self.calls]

    def snapshot(self, snapshot: Any) -> None:
        self.notifier._on_snapshot(snapshot)

    def status(self) -> list[str]:
        payload = dashboard_api.serialize_dashboard(
            dashboard_api.capture_dashboard(self.hass, self.entry), can_act=True
        )
        return [line["code"] for line in payload["status"]["lines"]]

    async def install(self, plan: ChargingPlan) -> None:
        await self.controller.async_install(plan)
        await self.hass.async_block_till_done()


async def _world(
    hass: HomeAssistant, freezer: Any, *, connected: bool | None, takes_start: bool = True
) -> World:
    """A charger with the default events chosen for a phone and the paired app. `connected`: the car at a charger
    that can say (`None`: a charger that cannot). `takes_start`: the charge control turns on when started."""
    hass.config.language = "en"
    freezer.move_to(at(14))
    entry = await setup_charger(hass, title="Garage")
    starts: list[Any] = []

    async def _turn_on(call: ServiceCall) -> None:
        starts.append(call)
        if takes_start:
            hass.states.async_set("switch.charger_a", "on")

    async def _turn_off(call: ServiceCall) -> None:
        hass.states.async_set("switch.charger_a", "off")

    hass.services.async_register("switch", "turn_on", _turn_on)
    hass.services.async_register("switch", "turn_off", _turn_off)
    calls = async_mock_service(hass, "notify", "mobile_app_pixel")
    await _choose(hass, entry.entry_id, targets=("mobile_app_pixel",), events=DEFAULT_EVENTS)
    await domain_data(hass).auto_store.async_update(
        entry.entry_id, mutate=lambda settings: replace(settings, departure=time(7, 30))
    )
    await charger_data(hass, entry.entry_id).push.async_register(PushRegistration(push_ref=REF, events=DEFAULT_EVENTS))
    car = None
    if connected is not None:
        car = Car(hass, entry.entry_id)
        await car.set(connected)
    await _go(hass, freezer, at(14, 5))
    return World(hass, entry, calls, starts, car)


# ------------------------------------------------------------------ the field night, unplugged


async def test_the_field_night_unplugged_tells_nothing_and_starts_nothing(
    hass: HomeAssistant, freezer: Any, relay: FakeRelay
) -> None:
    world = await _world(hass, freezer, connected=False)
    # The evening: tomorrow's prices give the night's plan with both cars away.
    await _go(hass, freezer, at(20))
    await world.install(plan_of([FIRST, LAST], 30.0))

    # 03:57 local: the plan cannot meet the departure.
    await _go(hass, freezer, at(1, 57, day=1))
    world.snapshot(AT_RISK)
    await hass.async_block_till_done()

    # The windows pass at the empty charger, looked at minute by minute and past every grace period.
    for moment in (FIRST[0], FIRST[0] + timedelta(seconds=GRACE_S + 5), FIRST[1], LAST[0]):
        await _go(hass, freezer, moment)
    assert world.controller.plan is not None, "the plan is kept for when the car comes"
    assert "charging_now" not in world.status(), "the status says no charge"
    for minutes in range(1, 61, 1):
        await _go(hass, freezer, LAST[0] + timedelta(minutes=minutes))
    await _go(hass, freezer, LAST[1] + timedelta(seconds=1))
    await _go(hass, freezer, LAST[1] + timedelta(hours=2))

    assert world.starts == [], "no start at an empty charger"
    assert world.controller.completion_record is None, "nothing is called complete"
    assert world.told() == [] and relay.requests == [], "nothing on the phones nor in the app's wake-up"


async def test_a_plug_in_inside_an_open_window_starts_it(hass: HomeAssistant, freezer: Any, relay: FakeRelay) -> None:
    world = await _world(hass, freezer, connected=False)
    await world.install(plan_of([FIRST, LAST], 30.0))
    await _go(hass, freezer, FIRST[0] + timedelta(minutes=10))
    assert world.starts == []
    assert world.car is not None
    await world.car.set(True)
    await _go(hass, freezer, FIRST[0] + timedelta(minutes=11))
    assert len(world.starts) == 1, "the window's charge starts at the plug-in"
    assert world.controller.charging


async def test_the_at_risk_notice_withheld_while_away_is_told_after_the_plug_in(
    hass: HomeAssistant, freezer: Any, relay: FakeRelay
) -> None:
    world = await _world(hass, freezer, connected=False)
    await world.install(plan_of([FIRST, LAST], 30.0))
    world.snapshot(AT_RISK)
    await hass.async_block_till_done()
    assert world.told() == [] and relay.requests == []
    assert world.car is not None
    await world.car.set(True)
    await _go(hass, freezer, at(14, 6))
    assert world.told() == [], "not before the plan made for the car is in"
    await _go(hass, freezer, at(15))
    assert world.told() == ["The charge will not be ready by the departure at 07:30."]
    assert len(relay.requests) == 1


async def test_an_at_risk_notice_withheld_while_away_is_dropped_once_the_plan_fits(
    hass: HomeAssistant, freezer: Any, relay: FakeRelay
) -> None:
    world = await _world(hass, freezer, connected=False)
    await world.install(plan_of([FIRST, LAST], 30.0))
    world.snapshot(AT_RISK)
    await hass.async_block_till_done()
    world.snapshot(ON_TIME)
    await hass.async_block_till_done()
    assert world.car is not None
    await world.car.set(True)
    await _go(hass, freezer, at(15))
    assert world.told() == [] and relay.requests == []


async def test_an_unknown_reading_while_away_still_counts_as_away(
    hass: HomeAssistant, freezer: Any, relay: FakeRelay
) -> None:
    world = await _world(hass, freezer, connected=False)
    assert world.car is not None
    await world.car.set(None)  # the charger went offline while the car is away
    await world.install(plan_of([FIRST, LAST], 30.0))
    world.snapshot(AT_RISK)
    await hass.async_block_till_done()
    world.controller._record_completion("plan_done", target_soc_percent=None)
    world.controller._notify()
    await hass.async_block_till_done()
    assert world.told() == [] and relay.requests == []


# ------------------------------------------------------------------ a plugged-in car is told as before


async def test_a_plugged_in_car_whose_window_does_not_start_is_told(
    hass: HomeAssistant, freezer: Any, relay: FakeRelay
) -> None:
    world = await _world(hass, freezer, connected=True, takes_start=False)
    await world.install(plan_of([FIRST, LAST], 30.0))
    await _go(hass, freezer, FIRST[0])
    assert len(world.starts) == 1, "the window's start is sent"
    await _go(hass, freezer, FIRST[0] + timedelta(seconds=GRACE_S + 5))
    assert world.told() == ["Charging did not start as planned."]
    assert len(relay.requests) == 1


async def test_a_plugged_in_car_that_cannot_be_ready_is_told_at_once(
    hass: HomeAssistant, freezer: Any, relay: FakeRelay
) -> None:
    world = await _world(hass, freezer, connected=True)
    await world.install(plan_of([FIRST, LAST], 30.0))
    world.snapshot(AT_RISK)
    await hass.async_block_till_done()
    assert world.told() == ["The charge will not be ready by the departure at 07:30."]
    assert len(relay.requests) == 1


async def test_a_plugged_in_car_charging_to_the_plans_end_is_told_complete(
    hass: HomeAssistant, freezer: Any, relay: FakeRelay
) -> None:
    world = await _world(hass, freezer, connected=True)
    await world.install(plan_of([FIRST], 10.0))
    await _go(hass, freezer, FIRST[0])
    assert world.controller.charging
    await _go(hass, freezer, FIRST[1] + timedelta(seconds=1))
    assert world.controller.completion_record["reason"] == "plan_done"
    assert world.told() == ["The planned charge has finished."]
    assert len(relay.requests) == 1


async def test_a_charger_that_cannot_say_starts_and_tells_as_before(
    hass: HomeAssistant, freezer: Any, relay: FakeRelay
) -> None:
    world = await _world(hass, freezer, connected=None, takes_start=False)
    await world.install(plan_of([FIRST, LAST], 30.0))
    world.snapshot(AT_RISK)
    await hass.async_block_till_done()
    await _go(hass, freezer, FIRST[0])
    assert len(world.starts) == 1
    await _go(hass, freezer, FIRST[0] + timedelta(seconds=GRACE_S + 5))
    assert world.told() == [
        "The charge will not be ready by the departure at 07:30.",
        "Charging did not start as planned.",
    ]
