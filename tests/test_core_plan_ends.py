"""The ownership core beside the plan ends and replans 1.11 brought (the best-effort plan for a departure the need
cannot be met by, the replan a returning register reading asks for, and a need met at that same reading), in both
modes: the core agrees with today's code (`conftest.ownership_shadow_agrees` fails a test otherwise) and the charger
sees the same starts and stops whether it shadows or drives."""

from __future__ import annotations

from datetime import time, timedelta
from typing import Any

import pytest
from freezegun import freeze_time
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.spotnav.core.session import OWNER_NONE, OWNER_PLAN
from custom_components.spotnav.execution import ownership_shadow
from tests.relay import serve as serve_prices

from .test_dashboard_api import NOW
from .test_register_unread_notice import _charged_and_restarted
from .test_replug import _car, _meter, _record_charger_commands, Car

pytestmark = pytest.mark.usefixtures("offline_relay")


@pytest.fixture(params=[False, True], ids=["shadows", "drives"])
def drives(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> bool:
    monkeypatch.setattr(ownership_shadow, "CORE_OWNERSHIP_DEFAULT", request.param)
    return request.param


def _agrees(car: Car, drives: bool) -> None:
    shadow = car.controller.ownership_shadow
    assert shadow.drives is drives
    assert shadow.counts["disagreements"] == 0 and shadow.counts["errors"] == 0


async def _at(car: Car, frozen: Any, delta: timedelta) -> None:
    frozen.tick(delta)
    async_fire_time_changed(car.hass, dt_util.utcnow())
    await car.hass.async_block_till_done()


async def test_a_best_effort_plan_charges_to_the_departure_and_the_next_departures_plan_takes_it_over(
    hass: HomeAssistant, transport: Any, drives: bool
) -> None:
    """08:00 with a departure at 10:00 and far more asked than two hours give: every slot to the departure is the
    plan's own charge, started at once. At 10:00 its last window ends while the next departure's plan (rising prices),
    waiting for that boundary, opens a window then: that plan takes the charge over, with no stop and no start (no
    contactor cycle, no `plan_done`), and the departure appointment's replan a second later changes nothing of it."""
    serve_prices(transport, rising=True)
    calls = _record_charger_commands(hass)
    with freeze_time(NOW) as frozen:
        car = await _car(hass, frozen, kwh=100.0, departure=time(10, 0), departure_enabled=True)
        proposal = car.preview.snapshot().proposal
        assert proposal is not None and proposal.short_of_deadline
        assert car.executor.window_charging_now()
        assert [call[0] for call in calls] == ["turn_on"]
        assert car.controller.ownership_shadow.session.owner == OWNER_PLAN
        assert car.controller.charge_origin == "plan_window"

        await _at(car, frozen, timedelta(hours=1, minutes=59))
        assert [call[0] for call in calls] == ["turn_on"], "nothing stops it before the departure"
        departing = car.controller.plan

        await _at(car, frozen, timedelta(minutes=1))
        assert car.controller.plan is not departing, "the next departure's plan is installed at the departure"
        assert [call[0] for call in calls] == ["turn_on"], "the next departure's plan takes the charge over"
        completion = car.controller.completion_record
        assert completion is None or completion.get("reason") != "plan_done"
        await _at(car, frozen, timedelta(minutes=2))
        await _at(car, frozen, timedelta(minutes=15))
        assert [call[0] for call in calls] == ["turn_on"], "one charge across the departure"
        assert car.controller.charging
        snapshot = car.preview.snapshot()
        assert snapshot.departure_at is not None and snapshot.departure_at > dt_util.utcnow() + timedelta(hours=20)
        assert car.controller.ownership_shadow.session.owner == OWNER_PLAN
        assert car.controller.charge_origin == "plan_window"
        _agrees(car, drives)


async def _window_charging_after_restart(hass: HomeAssistant, transport: Any, frozen: Any) -> tuple[Car, list[Any]]:
    """Restarted with the register not read yet (6.5 kWh kept of 10), then into the plan's window at 06:30: the
    plan's own charge runs, counted on the kept remainder within the unread grace."""
    car = await _charged_and_restarted(hass, transport, frozen)
    calls = _record_charger_commands(hass)
    await _at(car, frozen, timedelta(minutes=9))
    assert car.executor.window_charging_now()
    assert [call[0] for call in calls] == ["turn_on"]
    assert car.preview.snapshot().energy_basis == "kept_recent"
    assert car.controller.ownership_shadow.session.owner == OWNER_PLAN
    assert car.controller.charge_origin == "plan_window"
    calls.clear()
    return car, calls


async def test_a_returning_reading_replans_a_running_plan_charge_without_touching_it(
    hass: HomeAssistant, transport: Any, drives: bool
) -> None:
    with freeze_time(NOW) as frozen:
        car, calls = await _window_charging_after_restart(hass, transport, frozen)
        attempt = car.preview.snapshot().attempt

        frozen.tick(timedelta(minutes=1))
        _meter(hass, 1003.5)  # the first meter value since the restart
        await hass.async_block_till_done()

        assert car.preview.snapshot().attempt > attempt, "the reading planned again"
        assert car.preview.snapshot().energy_basis == "register"
        assert calls == [], "the replan neither stops nor starts the plan's charge"
        assert car.executor.window_charging_now()
        assert car.controller.ownership_shadow.session.owner == OWNER_PLAN
        assert car.controller.charge_origin == "plan_window"
        _agrees(car, drives)


async def test_a_returning_reading_that_meets_the_need_ends_the_plan_once(
    hass: HomeAssistant, transport: Any, drives: bool
) -> None:
    """The same reading asks for a replan and meets the need: one stop, the plan ended, nothing started again."""
    with freeze_time(NOW) as frozen:
        car, calls = await _window_charging_after_restart(hass, transport, frozen)

        frozen.tick(timedelta(minutes=30))
        _meter(hass, 1010.0)  # the first meter value since the restart: all ten delivered
        await hass.async_block_till_done()
        await _at(car, frozen, timedelta(seconds=10))

        assert [call[0] for call in calls] == ["turn_off"]
        assert not car.executor.window_charging_now()
        assert car.controller.ownership_shadow.session.owner == OWNER_NONE
        assert car.controller.charge_origin is None
        _agrees(car, drives)
