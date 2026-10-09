"""Fewer "New plan" notifications: none while the car is away, one per burst, none for a plan's remainder.

The field evening of 2026-10-09 (UTC), at an OCPP charger with a Kia EV6: the car was unplugged at 15:49 and
driven away; its state of charge, reported on the road, gave new plans at 16:28, 17:31 and 18:30, each one
told. It was plugged in again at 20:47 and the night's plan came at 21:20 (wanted). Two settings writes at
21:26:10 and 21:26:17 gave two plans, and at 23:30 the first window ended and the rest of the same plan was
installed, which woke the app at night. The rules here hold for the Companion phones and the paired app's
wake-up alike.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed, async_mock_service

from custom_components.spotnav.execution.controller import ChargingPlan
from custom_components.spotnav.notifications.notifier import (
    plan_fingerprint,
    plan_is_remainder,
    PLUG_IN_WAIT_S,
    SETTLE_S,
)
from custom_components.spotnav.notifications import push as push_module
from custom_components.spotnav.notifications.push import PushRegistration
from custom_components.spotnav.runtime import charger_data, domain_data

from .test_notifications import _choose
from .test_push import FakeRelay, REF
from .world import controller_of, setup_charger

@pytest.fixture
def relay(monkeypatch: pytest.MonkeyPatch) -> FakeRelay:
    """The SpotNav relay the paired app's wake-ups go to, recording each one."""
    fake = FakeRelay()
    monkeypatch.setattr(push_module, "async_get_clientsession", lambda _hass: fake)
    return fake


DAY = datetime(2026, 10, 9, tzinfo=timezone.utc)


def at(hour: int, minute: int = 0, second: int = 0, *, day: int = 0) -> datetime:
    return DAY + timedelta(days=day, hours=hour, minutes=minute, seconds=second)


def plan_of(periods: list[tuple[datetime, datetime]], kwh: float, **changes: Any) -> ChargingPlan:
    """A plan as Auto installs one: each calculation has its own identity (settings revision, prices)."""
    changes.setdefault("auto_identity", uuid4().hex)
    return ChargingPlan(
        start=periods[0][0].isoformat(),
        end=periods[-1][1].isoformat(),
        amps=16,
        energy_kwh=kwh,
        periods=[{"start": start.isoformat(), "end": end.isoformat()} for start, end in periods],
        **changes,
    )


#: The night's two windows: 22:00-23:30 and 01:00-03:00.
FIRST = (at(22), at(23, 30))
SECOND = (at(1, day=1), at(3, day=1))


# ------------------------------------------------------------------ the remainder of a told plan


def test_the_rest_of_a_told_plan_after_its_first_window_is_its_remainder() -> None:
    told = plan_fingerprint(plan_of([FIRST, SECOND], 30.0))
    assert plan_is_remainder(plan_of([SECOND], 18.0), told, at(23, 30))
    assert plan_is_remainder(plan_of([SECOND], 30.0), told, at(23, 30)), "no more energy than told"
    # A continuation inside the first window: the part of it still ahead, and the second window.
    assert plan_is_remainder(plan_of([(at(22, 45), at(23, 30)), SECOND], 24.0), told, at(22, 45, 10))


def test_a_real_change_in_what_is_left_is_not_a_remainder() -> None:
    told = plan_fingerprint(plan_of([FIRST, SECOND], 30.0))
    assert not plan_is_remainder(plan_of([SECOND], 31.0), told, at(23, 30)), "more energy"
    assert not plan_is_remainder(plan_of([(at(1, day=1), at(4, day=1))], 18.0), told, at(23, 30)), "longer"
    assert not plan_is_remainder(plan_of([(at(2, day=1), at(3, day=1))], 9.0), told, at(23, 30)), "a part left out"
    assert not plan_is_remainder(plan_of([SECOND], 18.0, vehicle_id="other"), told, at(23, 30)), "another car"
    # Before the first window ended, dropping it is a change, not a remainder.
    assert not plan_is_remainder(plan_of([SECOND], 18.0), told, at(21, 30))
    assert not plan_is_remainder(plan_of([SECOND], 18.0), None, at(23, 30))


# ------------------------------------------------------------------ a charger that says whether a car is there


class Car:
    """The car at the charger: what the charger says about it, reported on its charge control."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self.hass = hass
        self.connected: bool | None = True
        adapter = controller_of(hass, entry_id).adapter
        adapter._disconnected_values = ("available",)  # a status sensor that can say "no car"
        adapter.vehicle_connected = lambda: self.connected  # type: ignore[method-assign]
        self._stamp = 0

    async def set(self, connected: bool | None) -> None:
        self.connected = connected
        self._stamp += 1
        state = self.hass.states.get("switch.charger_a")
        self.hass.states.async_set(
            "switch.charger_a", "off" if state is None else state.state, {"stamp": self._stamp}
        )
        await self.hass.async_block_till_done()


async def _go(hass: HomeAssistant, freezer: Any, moment: datetime) -> None:
    freezer.move_to(moment)
    async_fire_time_changed(hass, moment)
    await hass.async_block_till_done()


async def _world(hass: HomeAssistant, freezer: Any, relay: FakeRelay, *, connection: bool = True) -> Any:
    hass.config.language = "en"
    freezer.move_to(at(14))
    entry = await setup_charger(hass, title="Garage")
    async_mock_service(hass, "switch", "turn_on")
    async_mock_service(hass, "switch", "turn_off")
    calls = async_mock_service(hass, "notify", "mobile_app_pixel")
    await _choose(hass, entry.entry_id, targets=("mobile_app_pixel",), events=("plan_installed", "plan_at_risk"))
    await charger_data(hass, entry.entry_id).push.async_register(
        PushRegistration(push_ref=REF, events=("plan_installed", "plan_at_risk"))
    )
    car = Car(hass, entry.entry_id) if connection else None
    if car is not None:
        await car.set(True)
    await _go(hass, freezer, at(14, 5))  # the set-up's own writes are long past
    return entry, calls, car


def _told(calls: list[Any]) -> list[str]:
    return [call.data["message"] for call in calls if call.data["message"].startswith("New plan")]


async def _install(hass: HomeAssistant, entry_id: str, plan: ChargingPlan) -> None:
    await controller_of(hass, entry_id).async_install(plan)
    await hass.async_block_till_done()


async def test_the_field_evening_tells_the_nights_plan_once(hass: HomeAssistant, freezer: Any, relay: FakeRelay) -> None:
    entry, calls, car = await _world(hass, freezer, relay)
    store = domain_data(hass).auto_store

    # The afternoon: plugged in, the plan told as before (once its settle time passed).
    await _install(hass, entry.entry_id, plan_of([FIRST, SECOND], 30.0))
    assert _told(calls) == [] and relay.requests == [], "not before the settle time"
    await _go(hass, freezer, at(14, 5) + timedelta(seconds=SETTLE_S + 1))
    assert len(_told(calls)) == 1 and len(relay.requests) == 1
    calls.clear()
    relay.requests.clear()

    # 15:49 unplugged and driven away; the car's readings on the road give new plans.
    await _go(hass, freezer, at(15, 49))
    await car.set(False)
    for moment, kwh in ((at(16, 28), 32.0), (at(17, 31), 34.0), (at(18, 30), 36.0)):
        await _go(hass, freezer, moment)
        await _install(hass, entry.entry_id, plan_of([(at(22), at(23, 45)), SECOND], kwh))
        await _go(hass, freezer, moment + timedelta(seconds=SETTLE_S + 1))
    assert controller_of(hass, entry.entry_id).plan.energy_kwh == 36.0, "the plan stays current"
    assert _told(calls) == [] and relay.requests == [], "nothing while the car is away"

    # 20:47 plugged in again; the night's plan comes at 21:20.
    await _go(hass, freezer, at(20, 47))
    await car.set(True)
    await _go(hass, freezer, at(21, 20))
    assert _told(calls) == [] and relay.requests == [], "the plan after the plug-in is awaited"
    await _install(hass, entry.entry_id, plan_of([FIRST, SECOND], 38.0))
    await _go(hass, freezer, at(21, 20) + timedelta(seconds=SETTLE_S + 1))
    assert len(_told(calls)) == 1 and len(relay.requests) == 1, "the night's plan, once"

    # 21:26:10 and 21:26:17: the person switches to a target state of charge in two writes.
    for moment, kwh in ((at(21, 26, 10), 40.0), (at(21, 26, 17), 41.0)):
        await _go(hass, freezer, moment)
        await store.async_update(entry.entry_id, mutate=lambda settings: replace(settings, amps=settings.amps))
        await _install(hass, entry.entry_id, plan_of([FIRST, SECOND], kwh))
    await _go(hass, freezer, at(21, 28))
    assert len(_told(calls)) == 1 and len(relay.requests) == 1, "the person sees the plan they made"

    # 23:30 the first window ended: the rest of the same plan is installed.
    await _go(hass, freezer, at(23, 30))
    await _install(hass, entry.entry_id, plan_of([SECOND], 27.0))
    await _go(hass, freezer, at(23, 31))
    assert len(_told(calls)) == 1 and len(relay.requests) == 1, "no night-time notification for the remainder"


async def test_a_burst_of_plans_tells_only_the_last_one(hass: HomeAssistant, freezer: Any, relay: FakeRelay) -> None:
    entry, calls, _car = await _world(hass, freezer, relay)
    for second, kwh in ((0, 30.0), (7, 31.0), (20, 32.0)):
        await _go(hass, freezer, at(14, 10, second))
        await _install(hass, entry.entry_id, plan_of([FIRST, SECOND], kwh))
    await _go(hass, freezer, at(14, 10, 20) + timedelta(seconds=SETTLE_S - 2))
    assert _told(calls) == [] and relay.requests == []
    await _go(hass, freezer, at(14, 10, 20) + timedelta(seconds=SETTLE_S + 1))
    told = _told(calls)
    assert len(told) == 1 and "32.0 kWh planned" in told[0]
    assert len(relay.requests) == 1


async def test_a_burst_back_to_the_told_plan_tells_nothing(hass: HomeAssistant, freezer: Any, relay: FakeRelay) -> None:
    entry, calls, _car = await _world(hass, freezer, relay)
    await _install(hass, entry.entry_id, plan_of([FIRST, SECOND], 30.0))
    await _go(hass, freezer, at(14, 20))
    assert len(_told(calls)) == 1
    await _install(hass, entry.entry_id, plan_of([FIRST, SECOND], 31.0))
    await _go(hass, freezer, at(14, 20, 5))
    await _install(hass, entry.entry_id, plan_of([FIRST, SECOND], 30.0, auto_identity="b" * 32))
    await _go(hass, freezer, at(14, 25))
    assert len(_told(calls)) == 1 and len(relay.requests) == 1


async def test_a_stream_of_plans_is_told_after_the_longest_settle(
    hass: HomeAssistant, freezer: Any, relay: FakeRelay
) -> None:
    from custom_components.spotnav.notifications.notifier import SETTLE_MAX_S

    entry, calls, _car = await _world(hass, freezer, relay)
    second = 0
    kwh = 30.0
    while second <= SETTLE_MAX_S + 20:
        await _go(hass, freezer, at(14, 10) + timedelta(seconds=second))
        await _install(hass, entry.entry_id, plan_of([FIRST, SECOND], kwh))
        second += 20
        kwh += 0.5
    assert len(_told(calls)) == 1, "a plan that keeps changing is still told within the longest settle"


async def test_other_events_are_not_held_back(hass: HomeAssistant, freezer: Any, relay: FakeRelay) -> None:
    entry, calls, _car = await _world(hass, freezer, relay)
    notifier = charger_data(hass, entry.entry_id).notifier
    await _install(hass, entry.entry_id, plan_of([FIRST, SECOND], 30.0))
    notifier._maybe_send("plan_at_risk", {"departure_time": "07:00"}, dt_util.utcnow())
    await hass.async_block_till_done()
    assert [call.data["message"] for call in calls] == ["The charge will not be ready by the departure at 07:00."]
    assert len(relay.requests) == 1


async def test_the_standing_plan_is_told_when_none_follows_the_plug_in(
    hass: HomeAssistant, freezer: Any, relay: FakeRelay
) -> None:
    entry, calls, car = await _world(hass, freezer, relay)
    await car.set(False)
    await _install(hass, entry.entry_id, plan_of([FIRST, SECOND], 30.0))
    await _go(hass, freezer, at(15))
    assert _told(calls) == []
    await car.set(True)
    await _go(hass, freezer, at(15) + timedelta(seconds=PLUG_IN_WAIT_S - 60))
    assert _told(calls) == []
    await _go(hass, freezer, at(15) + timedelta(seconds=PLUG_IN_WAIT_S + 1))
    assert len(_told(calls)) == 1 and len(relay.requests) == 1


async def test_the_standing_plan_is_told_before_its_window_opens(
    hass: HomeAssistant, freezer: Any, relay: FakeRelay
) -> None:
    entry, calls, car = await _world(hass, freezer, relay)
    await car.set(False)
    soon = (at(15, 10), at(16))
    await _install(hass, entry.entry_id, plan_of([soon], 10.0))
    await _go(hass, freezer, at(15))
    await car.set(True)
    await _go(hass, freezer, at(15, 10, 1))
    assert len(_told(calls)) == 1


async def test_a_replug_to_the_told_plan_tells_nothing(hass: HomeAssistant, freezer: Any, relay: FakeRelay) -> None:
    entry, calls, car = await _world(hass, freezer, relay)
    await _install(hass, entry.entry_id, plan_of([FIRST, SECOND], 30.0))
    await _go(hass, freezer, at(14, 20))
    assert len(_told(calls)) == 1
    await car.set(False)
    await _go(hass, freezer, at(14, 30))
    await car.set(True)
    await _go(hass, freezer, at(16))
    assert len(_told(calls)) == 1 and len(relay.requests) == 1


async def test_an_unknown_connection_keeps_the_last_known_one(
    hass: HomeAssistant, freezer: Any, relay: FakeRelay
) -> None:
    entry, calls, car = await _world(hass, freezer, relay)
    await car.set(False)
    await car.set(None)  # the charger went offline while the car is away
    await _install(hass, entry.entry_id, plan_of([FIRST, SECOND], 30.0))
    await _go(hass, freezer, at(15))
    assert _told(calls) == []
    await car.set(True)
    await car.set(None)  # a fault after the plug-in: the car is still taken as there
    await _install(hass, entry.entry_id, plan_of([FIRST, SECOND], 31.0))
    await _go(hass, freezer, at(15, 5))
    assert len(_told(calls)) == 1


async def test_a_charger_that_cannot_say_tells_as_before(hass: HomeAssistant, freezer: Any, relay: FakeRelay) -> None:
    entry, calls, _car = await _world(hass, freezer, relay, connection=False)
    await _install(hass, entry.entry_id, plan_of([FIRST, SECOND], 30.0))
    await _go(hass, freezer, at(14, 10))
    assert len(_told(calls)) == 1 and len(relay.requests) == 1
    # The remainder rule holds for it too.
    await _go(hass, freezer, at(23, 30))
    await _install(hass, entry.entry_id, plan_of([SECOND], 18.0))
    await _go(hass, freezer, at(23, 31))
    assert len(_told(calls)) == 1


async def test_a_remainder_after_a_restart_is_not_told(hass: HomeAssistant, freezer: Any, relay: FakeRelay) -> None:
    entry, calls, _car = await _world(hass, freezer, relay, connection=False)
    await _install(hass, entry.entry_id, plan_of([FIRST, SECOND], 30.0))
    await _go(hass, freezer, at(14, 10))
    assert len(_told(calls)) == 1
    await _go(hass, freezer, at(23, 30))
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    await _go(hass, freezer, at(23, 32))
    await _install(hass, entry.entry_id, plan_of([SECOND], 18.0))
    await _go(hass, freezer, at(23, 34))
    assert len(_told(calls)) == 1
