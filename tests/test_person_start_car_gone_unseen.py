"""A person's Start at a charger whose car left unseen.

A Charge Amps connector says `Unavailable` while it is off, with or without a car: a car that leaves under a person's
Stop is not seen. The person then presses Start: the connector is turned on and says `Available`, no car. That unplug
lands while the Start is on its way, before its result: the Start ends with that plug-in, as any person's Start whose
car is gone. The core and the stored pause agree (the suite fails a test on any difference), nothing charges, and
nothing is retried or told.

The same at a normal charger (one that says when a car is plugged in, also while it is off) whose car is unplugged
while the Start is on its way.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from homeassistant.core import HomeAssistant, ServiceCall

from custom_components.spotnav.runtime import domain_data

from .test_person_stop_blind_charger import (
    _car_reports_again,
    _charging,
    _connection_events,
    _executor,
    _stop,
    _with_car,
)
from .test_plan_notify_noise import _go, plan_of
from .test_push import FakeRelay
from .test_status_sensor_no_car import _night, FIRST, LAST, Night, relay, STATUS, SWITCH

__all__ = ["relay"]


def _pause(night: Night) -> tuple[bool, str | None, str | None]:
    pause = domain_data(night.hass).auto_store.settings(night.entry.entry_id).pause
    return pause.manual, pause.action, pause.scope


async def _start_after_the_car_left(hass: HomeAssistant, freezer: Any, night: Night) -> int:
    """The car is gone; a plan with a window open is in force again; the person presses Start. The starts before it."""
    await night.controller.async_install(plan_of([(FIRST[0] + timedelta(minutes=15), LAST[1])], 30.0))
    await hass.async_block_till_done()
    await _go(hass, freezer, FIRST[0] + timedelta(minutes=20))
    starts = len(night.starts)
    await _executor(night).async_manual_start()
    await hass.async_block_till_done()
    await _go(hass, freezer, FIRST[0] + timedelta(minutes=21))
    return starts


async def _ends_with_the_plug_in(
    hass: HomeAssistant, freezer: Any, night: Night, relay: FakeRelay, starts: int
) -> None:
    assert hass.states.get(STATUS).state == "Available"
    assert _connection_events(night)[-1]["kind"] == "unplug"
    session = night.controller.session_runner.session
    assert session.manual is None and session.owner == "none" and session.pending == ()
    assert _pause(night) == (False, None, None), "the Start's pause ends with the plug-in it was for"
    assert len(night.starts) == starts + 1, "the person's Start went out, once"
    for minutes in (30, 60, 120, 240):
        await _go(hass, freezer, FIRST[0] + timedelta(minutes=minutes))
    assert len(night.starts) == starts + 1, "nothing is retried"
    assert not night.controller.charging, "nothing charges"
    assert night.told() == [] and relay.requests == [], "nothing is told"


async def test_at_a_status_that_says_nothing(hass: HomeAssistant, freezer: Any, relay: FakeRelay) -> None:
    night = await _night(hass, freezer, off_status="Unavailable", car=True)
    await _with_car(hass, night, plug=None)
    await _charging(hass, freezer, night)
    await _stop(night)
    assert hass.states.get(STATUS).state == "Unavailable"
    assert night.controller.charger_says_connected() is None
    night.car = False  # it leaves unseen
    starts = await _start_after_the_car_left(hass, freezer, night)
    await _ends_with_the_plug_in(hass, freezer, night, relay, starts)


async def test_with_the_cars_plug_sensor_standing_in(hass: HomeAssistant, freezer: Any, relay: FakeRelay) -> None:
    """The car's sensor reported fresh after the Stop, so it stands in; its cloud still says plugged in when the car
    has left. The charger's own `Available` wins."""
    night = await _night(hass, freezer, off_status="Unavailable", car=True)
    await _with_car(hass, night)
    await _charging(hass, freezer, night)
    await _stop(night)
    _car_reports_again(hass)
    await hass.async_block_till_done()
    assert night.controller.adapter.vehicle_connected() is True, "the car's sensor stands in"
    night.car = False  # it leaves; the sensor's cloud has not said so yet
    starts = await _start_after_the_car_left(hass, freezer, night)
    await _ends_with_the_plug_in(hass, freezer, night, relay, starts)


async def test_at_a_charger_that_says_the_car_and_is_unplugged_while_the_start_is_on_its_way(
    hass: HomeAssistant, freezer: Any, relay: FakeRelay
) -> None:
    night = await _night(hass, freezer, off_status="SuspendedEVSE", car=True)
    await _charging(hass, freezer, night)
    await _stop(night)
    assert night.controller.charger_says_connected() is True

    async def unplugged_meanwhile(call: ServiceCall) -> None:
        night.car = False
        await night.turn_on(call)

    hass.services.async_register("switch", "turn_on", unplugged_meanwhile)
    starts = await _start_after_the_car_left(hass, freezer, night)
    await _ends_with_the_plug_in(hass, freezer, night, relay, starts)
    assert hass.states.get(SWITCH).state == "on"


async def test_a_start_whose_car_stays_owns_the_charge_until_the_unplug(
    hass: HomeAssistant, freezer: Any, relay: FakeRelay
) -> None:
    """The same Start with the car still there: the person's charge, which their pause holds until the unplug."""
    night = await _night(hass, freezer, off_status="Unavailable", car=True)
    await _with_car(hass, night, plug=None)
    await _charging(hass, freezer, night)
    await _stop(night)
    await _start_after_the_car_left(hass, freezer, night)
    assert hass.states.get(STATUS).state == "Charging"
    assert night.controller.session_runner.session.owner == "person"
    assert _pause(night) == (True, "start", "plug_in")
    night.car = False
    night._report()  # the charger, on, sees the unplug
    await _go(hass, freezer, FIRST[0] + timedelta(minutes=30))
    assert _pause(night) == (False, None, None)


async def test_a_car_plugged_in_while_the_start_is_on_its_way_is_the_persons(
    hass: HomeAssistant, freezer: Any, relay: FakeRelay
) -> None:
    """Unplugged and another car plugged in before the Start's result: the Start holds for the car now plugged in,
    in the core and the stored pause alike, read under the lock once the Start is done."""
    night = await _night(hass, freezer, off_status="SuspendedEVSE", car=True)
    await _charging(hass, freezer, night)
    await _stop(night)

    async def swapped_meanwhile(call: ServiceCall) -> None:
        night.car = False
        await night.turn_on(call)
        night.car = True
        night._report()

    hass.services.async_register("switch", "turn_on", swapped_meanwhile)
    starts = await _start_after_the_car_left(hass, freezer, night)
    assert [event["kind"] for event in _connection_events(night)[-2:]] == ["unplug", "plug_in"]
    assert hass.states.get(STATUS).state == "Charging"
    session = night.controller.session_runner.session
    assert session.manual is not None and (session.manual.action, session.manual.scope) == ("start", "plug_in")
    assert session.owner == "person"
    assert _pause(night) == (True, "start", "plug_in")
    await _go(hass, freezer, FIRST[0] + timedelta(minutes=60))
    assert len(night.starts) == starts + 1 and night.controller.charging
