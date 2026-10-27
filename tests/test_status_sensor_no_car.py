"""No car is read from a charger's status sensor the way the dashboard reads it.

The field night of 2026-10-13 at a Charge Amps charger through the `chargeamps` integration: a switch starts and
stops it and a status sensor says `Available` all night with no car plugged in (`Unavailable` while the switch has
the connector off). The dashboard showed the connection as disconnected from that sensor, but the execution side
read "cannot tell" (the profile named no value meaning no car), so the plan's windows started the charger and the
phone was told the charge would not be ready, had not started and had finished.

Now the same table decides both: a value the platform's connection table words as disconnected is no car, one that
says a vehicle is there is a car, and anything else (`Unavailable`, `Unknown`, a fault) says nothing.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import time, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from homeassistant.core import HomeAssistant, ServiceCall
from pytest_homeassistant_custom_component.common import async_mock_service

from custom_components.spotnav.const import (
    CONF_CHARGE_CONTROL,
    CONF_CHARGER_PLATFORM,
    CONF_CHARGING_STATE,
)
from custom_components.spotnav.execution.charger_connection import (
    DISCONNECTED,
    NO_VEHICLE_UNSURE,
    PLATFORM_STATUS,
)
from custom_components.spotnav.execution.chargers.adapter import build_adapter
from custom_components.spotnav.notifications import push as push_module
from custom_components.spotnav.notifications.push import PushRegistration
from custom_components.spotnav.notifications.settings import DEFAULT_EVENTS
from custom_components.spotnav.notifications.unexpected_stop import GRACE_S
from custom_components.spotnav.runtime import charger_data, domain_data

from .helpers import make_entry
from .test_notifications import _choose
from .test_plan_notify_noise import _go, at, plan_of
from .test_push import FakeRelay, REF
from .world import controller_of

SWITCH = "switch.roback_connector_1"
STATUS = "sensor.roback_connector_1_status"
FIRST = (at(1, day=1), at(2, day=1))
LAST = (at(4, 15, day=1), at(5, 15, day=1))
AT_RISK = SimpleNamespace(reason="deadline_too_short", proposal=None, applied=True)


def _adapter(hass: HomeAssistant, platform: str | None, status: str | None = STATUS) -> Any:
    state = {} if status is None else {"entity_id": status, "charging_values": ["charging"]}
    config = {CONF_CHARGER_PLATFORM: platform, CONF_CHARGE_CONTROL: SWITCH, CONF_CHARGING_STATE: state}
    return build_adapter(hass, config, ocpp_target=lambda: None, energy_entity_id=None)


# ------------------------------------------------------------------ the adapter


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Available", False),
        ("Preparing", True),
        ("Charging", True),
        ("Connected", True),
        ("SuspendedEV", True),
        ("SuspendedEVSE", True),
        ("Finishing", True),
        ("Unavailable", None),
        ("Reserved", None),
        ("Unknown", None),
        ("Faulted", None),
        ("Error", None),
        ("unavailable", None),
        ("unknown", None),
    ],
)
async def test_a_charge_amps_status_says_whether_a_car_is_plugged_in(
    hass: HomeAssistant, raw: str, expected: bool | None
) -> None:
    adapter = _adapter(hass, "chargeamps")
    hass.states.async_set(STATUS, raw)
    assert adapter.reports_connection
    assert adapter.vehicle_connected() is expected


@pytest.mark.parametrize(
    ("platform", "raw"),
    [
        (platform, raw)
        for platform, table in PLATFORM_STATUS.items()
        for raw, state in table.items()
        if state == DISCONNECTED and (platform, raw) not in NO_VEHICLE_UNSURE
    ],
)
async def test_every_status_the_dashboard_words_as_disconnected_is_no_car(
    hass: HomeAssistant, platform: str, raw: str
) -> None:
    adapter = _adapter(hass, platform)
    hass.states.async_set(STATUS, raw)
    assert adapter.connection()[0] == DISCONNECTED
    assert adapter.vehicle_connected() is False


async def test_smartevse_ready_to_charge_says_nothing_about_the_car(hass: HomeAssistant) -> None:
    """SmartEVSE stays in state A with a car plugged in while its access is off (its Off and Pause modes)."""
    adapter = _adapter(hass, "smartevse")
    hass.states.async_set(STATUS, "Ready to Charge")
    assert adapter.vehicle_connected() is None
    assert not adapter.reports_connection
    hass.states.async_set(STATUS, "Connected to EV")
    assert adapter.vehicle_connected() is None, "a charger that cannot say no car says nothing either way"


async def test_a_plain_switch_and_an_unmapped_platform_still_cannot_say(hass: HomeAssistant) -> None:
    plain = _adapter(hass, None, status=None)
    assert not plain.reports_connection and plain.vehicle_connected() is None
    unmapped = _adapter(hass, "v2c")
    hass.states.async_set(STATUS, "Available")
    assert not unmapped.reports_connection and unmapped.vehicle_connected() is None


# ------------------------------------------------------------------ the night


@pytest.fixture
def relay(monkeypatch: pytest.MonkeyPatch) -> FakeRelay:
    fake = FakeRelay()
    monkeypatch.setattr(push_module, "async_get_clientsession", lambda _hass: fake)
    return fake


class Night:
    """A Charge Amps connector: `Unavailable` while its switch has it off (`off_status`), otherwise what the car
    makes it (`Available` with none, `Preparing` with one waiting, `Charging` while charging)."""

    def __init__(self, hass: HomeAssistant, entry: Any, calls: list[Any], *, off_status: str, takes_start: bool) -> None:
        self.hass = hass
        self.entry = entry
        self.calls = calls
        self.off_status = off_status
        self.takes_start = takes_start
        self.car = False
        self.starts: list[Any] = []

    @property
    def controller(self) -> Any:
        return controller_of(self.hass, self.entry.entry_id)

    @property
    def notifier(self) -> Any:
        return charger_data(self.hass, self.entry.entry_id).notifier

    def told(self) -> list[str]:
        return [call.data["message"] for call in self.calls]

    def _report(self) -> None:
        on = self.hass.states.get(SWITCH).state == "on"
        if not on:
            status = self.off_status
        elif not self.car:
            status = "Available"
        else:
            status = "Charging"
        self.hass.states.async_set(STATUS, status)

    async def turn_on(self, call: ServiceCall) -> None:
        self.starts.append(call)
        if self.takes_start:
            self.hass.states.async_set(SWITCH, "on")
            self._report()

    async def turn_off(self, call: ServiceCall) -> None:
        self.hass.states.async_set(SWITCH, "off")
        self._report()

    async def plug_in(self) -> None:
        self.car = True
        on = self.hass.states.get(SWITCH).state == "on"
        self.hass.states.async_set(STATUS, "Charging" if on else "Preparing")
        await self.hass.async_block_till_done()


async def _night(
    hass: HomeAssistant, freezer: Any, *, off_status: str = "Available", takes_start: bool = True, car: bool = False
) -> Night:
    hass.config.language = "en"
    freezer.move_to(at(14))
    hass.states.async_set(SWITCH, "off")
    hass.states.async_set(STATUS, "Preparing" if car else off_status)
    entry = make_entry(
        hass,
        entry_id="entry_a",
        charge_control=SWITCH,
        current_limit=None,
        webhook_id="webhook-a",
        title="Garage",
        extra={
            CONF_CHARGER_PLATFORM: "chargeamps",
            CONF_CHARGING_STATE: {"entity_id": STATUS, "charging_values": ["charging"]},
        },
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    calls = async_mock_service(hass, "notify", "mobile_app_pixel")
    night = Night(hass, entry, calls, off_status=off_status, takes_start=takes_start)
    night.car = car
    hass.services.async_register("switch", "turn_on", night.turn_on)
    hass.services.async_register("switch", "turn_off", night.turn_off)
    await _choose(hass, entry.entry_id, targets=("mobile_app_pixel",), events=DEFAULT_EVENTS)
    await domain_data(hass).auto_store.async_update(
        entry.entry_id, mutate=lambda settings: replace(settings, departure=time(7, 30))
    )
    await charger_data(hass, entry.entry_id).push.async_register(PushRegistration(push_ref=REF, events=DEFAULT_EVENTS))
    # The night's plans are given by hand: Auto's own snapshots (settings incomplete here) are not heard.
    notifier = night.notifier
    notifier._unsubscribe.pop()()
    await _go(hass, freezer, at(14, 5))
    return night


async def _through_the_night(hass: HomeAssistant, freezer: Any, night: Night) -> None:
    await _go(hass, freezer, at(20))
    await night.controller.async_install(plan_of([FIRST, LAST], 30.0))
    await hass.async_block_till_done()
    await _go(hass, freezer, at(1, 57, day=1))
    night.notifier._on_snapshot(AT_RISK)
    await hass.async_block_till_done()
    for moment in (FIRST[0], FIRST[0] + timedelta(seconds=GRACE_S + 5), FIRST[1], LAST[0]):
        await _go(hass, freezer, moment)
    for minutes in range(1, 61):
        await _go(hass, freezer, LAST[0] + timedelta(minutes=minutes))
    await _go(hass, freezer, LAST[1] + timedelta(seconds=1))
    await _go(hass, freezer, LAST[1] + timedelta(hours=2))


async def test_an_available_connector_starts_nothing_and_tells_nothing(
    hass: HomeAssistant, freezer: Any, relay: FakeRelay
) -> None:
    night = await _night(hass, freezer)
    assert night.controller.adapter.vehicle_connected() is False
    await _through_the_night(hass, freezer, night)
    assert night.starts == [], "no start at an empty charger"
    assert night.controller.completion_record is None
    assert night.told() == [] and relay.requests == []


async def test_the_field_night_a_connector_off_reads_unavailable_and_tells_nothing(
    hass: HomeAssistant, freezer: Any, relay: FakeRelay
) -> None:
    """The switch off makes the connector `Unavailable`, which cannot tell a car (one plugged in then is not
    seen), so the window's start is sent; the connector then says `Available`, and nothing is told."""
    night = await _night(hass, freezer, off_status="Unavailable")
    assert night.controller.adapter.vehicle_connected() is None
    await _through_the_night(hass, freezer, night)
    assert len(night.starts) >= 1, "a connector that cannot say is started, as before"
    assert night.told() == [] and relay.requests == []


async def test_a_plug_in_inside_an_open_window_starts_it(hass: HomeAssistant, freezer: Any, relay: FakeRelay) -> None:
    night = await _night(hass, freezer)
    await night.controller.async_install(plan_of([FIRST, LAST], 30.0))
    await hass.async_block_till_done()
    await _go(hass, freezer, FIRST[0] + timedelta(minutes=10))
    assert night.starts == []
    await night.plug_in()
    await _go(hass, freezer, FIRST[0] + timedelta(minutes=11))
    assert len(night.starts) == 1, "the window's charge starts at the plug-in"
    assert night.controller.plugged_in_at is not None, "the plug-in was seen"
    assert night.controller.charging


async def test_a_plugged_in_car_whose_window_does_not_start_is_told(
    hass: HomeAssistant, freezer: Any, relay: FakeRelay
) -> None:
    night = await _night(hass, freezer, takes_start=False, car=True)
    assert night.controller.adapter.vehicle_connected() is True
    await night.controller.async_install(plan_of([FIRST, LAST], 30.0))
    await hass.async_block_till_done()
    await _go(hass, freezer, FIRST[0])
    assert len(night.starts) == 1
    # The start is sent again at 90 s (`START_RETRY_AFTER_S`), at its own moment: it does not move when this is told.
    await _go(hass, freezer, FIRST[0] + timedelta(seconds=95))
    await _go(hass, freezer, FIRST[0] + timedelta(seconds=GRACE_S + 5))
    assert night.told() == ["Charging did not start as planned."]
