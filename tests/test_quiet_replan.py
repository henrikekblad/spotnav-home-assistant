"""No "New plan" in the night for a plan only calculated again: once a plan is told for a plug-in, a later one is told
only when it rests on new prices (it reaches a later day).

The field night of 17-18 October 2026 (Stockholm): the car was plugged in at the evening and its plan told; load
balancing changed the current the car could get, so the plan was installed again at 00:15, 00:29 and 01:43 with its
windows moved, and two of those were told as new plans. Nobody had changed anything.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_mock_service

from custom_components.spotnav.execution.chargers.adapter import ChargerAdapter
from custom_components.spotnav.notifications.notifier import SETTLE_S
from custom_components.spotnav.notifications.push import PushRegistration
from custom_components.spotnav.runtime import charger_data

from .test_notifications import _choose
from .test_plan_notify_noise import _go, _install, _notice, _told, plan_of, relay  # noqa: F401 - a fixture
from .test_push import FakeRelay, REF
from .world import setup_charger

ZONE = ZoneInfo("Europe/Stockholm")


def local(day: int, hour: int, minute: int = 0) -> datetime:
    """A Stockholm wall-clock time in October 2026, as UTC."""
    return datetime(2026, 10, day, hour, minute, tzinfo=ZONE).astimezone(timezone.utc)


class Plug:
    """The car at the charger, for every charger adapter (also one made again by a restart)."""

    def __init__(self, hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch) -> None:
        self.hass = hass
        self.connected: bool | None = True
        self._stamp = 0
        monkeypatch.setattr(ChargerAdapter, "reports_connection", property(lambda _self: True))
        monkeypatch.setattr(ChargerAdapter, "vehicle_connected", lambda _self: self.connected)

    async def set(self, connected: bool | None) -> None:
        self.connected = connected
        self._stamp += 1
        state = self.hass.states.get("switch.charger_a")
        self.hass.states.async_set("switch.charger_a", "off" if state is None else state.state, {"stamp": self._stamp})
        await self.hass.async_block_till_done()


async def _world(hass: HomeAssistant, freezer: Any, monkeypatch: pytest.MonkeyPatch, start: datetime) -> Any:
    hass.config.language = "en"
    await hass.config.async_set_time_zone("Europe/Stockholm")
    plug = Plug(hass, monkeypatch)
    freezer.move_to(start)
    entry = await setup_charger(hass, title="Garage")
    async_mock_service(hass, "switch", "turn_on")
    async_mock_service(hass, "switch", "turn_off")
    calls = async_mock_service(hass, "notify", "mobile_app_pixel")
    await _choose(hass, entry.entry_id, targets=("mobile_app_pixel",), events=("plan_installed", "plan_at_risk"))
    await charger_data(hass, entry.entry_id).push.async_register(
        PushRegistration(push_ref=REF, events=("plan_installed", "plan_at_risk"))
    )
    await plug.set(False)
    await _go(hass, freezer, start + timedelta(minutes=5))  # the set-up's own writes are long past
    return entry, calls, plug


async def _settled(hass: HomeAssistant, freezer: Any, entry_id: str, moment: datetime, plan: Any) -> None:
    """At `moment` a plan is installed, and it settles."""
    await _go(hass, freezer, moment)
    await _install(hass, entry_id, plan)
    await _go(hass, freezer, moment + timedelta(seconds=SETTLE_S + 1))


#: The evening's plan: 23:00-01:00 and 02:00-05:00, so it reaches 18 October.
EVENING = [(local(17, 23), local(18, 1)), (local(18, 2), local(18, 5))]
#: Load balancing gave the car another current: the windows moved, three times in the night.
NIGHT = (
    (local(18, 0, 15), [(local(18, 0, 15), local(18, 1, 30)), (local(18, 2, 30), local(18, 5))], 26.0),
    (local(18, 0, 29), [(local(18, 0, 29), local(18, 1, 15)), (local(18, 3), local(18, 5, 30))], 25.0),
    (local(18, 1, 43), [(local(18, 1, 43), local(18, 2, 30)), (local(18, 3, 30), local(18, 6))], 22.0),
)


async def _the_evening(hass: HomeAssistant, freezer: Any, monkeypatch: pytest.MonkeyPatch, relay: FakeRelay) -> Any:
    entry, calls, plug = await _world(hass, freezer, monkeypatch, local(17, 17))
    await _go(hass, freezer, local(17, 18))
    await plug.set(True)
    await _settled(hass, freezer, entry.entry_id, local(17, 18, 10), plan_of(EVENING, 30.0))
    assert len(_told(calls)) == 1 and len(relay.requests) == 1, "the plan after the plug-in is told"
    assert _notice(hass, entry)["seq"] == 1
    return entry, calls, plug


async def test_the_field_night_tells_the_evenings_plan_once(
    hass: HomeAssistant, freezer: Any, monkeypatch: pytest.MonkeyPatch, relay: FakeRelay
) -> None:
    entry, calls, _plug = await _the_evening(hass, freezer, monkeypatch, relay)
    for moment, periods, kwh in NIGHT:
        await _settled(hass, freezer, entry.entry_id, moment, plan_of(periods, kwh))
    assert len(_told(calls)) == 1 and len(relay.requests) == 1, "nobody is woken for a replan"
    assert _notice(hass, entry)["seq"] == 1, "the app is not told either"


async def test_a_departure_at_risk_in_the_night_is_still_told(
    hass: HomeAssistant, freezer: Any, monkeypatch: pytest.MonkeyPatch, relay: FakeRelay
) -> None:
    entry, calls, _plug = await _the_evening(hass, freezer, monkeypatch, relay)
    for moment, periods, kwh in NIGHT:
        await _settled(hass, freezer, entry.entry_id, moment, plan_of(periods, kwh))
    notifier = charger_data(hass, entry.entry_id).notifier
    notifier._maybe_send("plan_at_risk", {"departure_time": "07:00"}, dt_util.utcnow())
    await hass.async_block_till_done()
    assert calls[-1].data["message"] == "The charge will not be ready by the departure at 07:00."
    assert len(relay.requests) == 2


async def test_a_plan_on_new_prices_is_told(
    hass: HomeAssistant, freezer: Any, monkeypatch: pytest.MonkeyPatch, relay: FakeRelay
) -> None:
    entry, calls, plug = await _world(hass, freezer, monkeypatch, local(18, 7))
    await _go(hass, freezer, local(18, 8))
    await plug.set(True)
    # Before midday only today's prices are known: the plan reaches the evening of 18 October.
    await _settled(hass, freezer, entry.entry_id, local(18, 8, 10), plan_of([(local(18, 21), local(18, 23))], 12.0))
    assert len(_told(calls)) == 1
    # Calculated again on the same prices: not told.
    await _settled(hass, freezer, entry.entry_id, local(18, 11), plan_of([(local(18, 22), local(18, 23, 45))], 12.0))
    assert len(_told(calls)) == 1
    # Tomorrow's prices were published: the plan now reaches into 19 October.
    await _settled(hass, freezer, entry.entry_id, local(18, 13, 5), plan_of([(local(19, 2), local(19, 4))], 12.0))
    assert len(_told(calls)) == 2 and len(relay.requests) == 2
    assert _notice(hass, entry)["seq"] == 2


async def test_the_first_plan_after_a_new_plug_in_is_told(
    hass: HomeAssistant, freezer: Any, monkeypatch: pytest.MonkeyPatch, relay: FakeRelay
) -> None:
    entry, calls, plug = await _the_evening(hass, freezer, monkeypatch, relay)
    moment, periods, kwh = NIGHT[0]
    await _settled(hass, freezer, entry.entry_id, moment, plan_of(periods, kwh))
    assert len(_told(calls)) == 1
    await _go(hass, freezer, local(18, 0, 40))
    await plug.set(False)
    await _go(hass, freezer, local(18, 0, 50))
    await plug.set(True)
    moment, periods, kwh = NIGHT[1]
    await _settled(hass, freezer, entry.entry_id, moment + timedelta(minutes=25), plan_of(periods, kwh))
    assert len(_told(calls)) == 2 and len(relay.requests) == 2, "a new plug-in's first plan is told"
    assert _notice(hass, entry)["seq"] == 2


async def test_a_restart_in_the_plug_in_keeps_a_moved_plan_quiet(
    hass: HomeAssistant, freezer: Any, monkeypatch: pytest.MonkeyPatch, relay: FakeRelay, hass_storage: dict
) -> None:
    entry, calls, _plug = await _the_evening(hass, freezer, monkeypatch, relay)
    await _go(hass, freezer, local(18, 0))
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert hass_storage[f"spotnav.notified_plan.{entry.entry_id}"]["data"]["plug_in_told"] is True
    for moment, periods, kwh in NIGHT:
        await _settled(hass, freezer, entry.entry_id, moment, plan_of(periods, kwh))
    assert len(_told(calls)) == 1 and len(relay.requests) == 1
    assert _notice(hass, entry)["seq"] == 1
