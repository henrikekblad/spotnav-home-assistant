"""What a restart must not lose, and what load balancing reckons a charge at when it has nothing on record (2.5.1).

A running charge with no current on record and no usable setpoint (a charge the charger began by itself, or one whose
record was lost, as a restart's first calculation saved over it before 2.5.1) left load balancing nothing to work from
(`requested_current_unknown`), and the charge ran unregulated. It is reckoned now at the charging current set, else the
plan's, else `ASSUMED_CURRENT_A`. And through the real `async_setup_entry` (a reload), the charger's record is read back
before the first calculation, so the plan's charge keeps its owner and its request.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import time, timedelta
from typing import Any

import pytest
from freezegun import freeze_time
from homeassistant.core import HomeAssistant

from custom_components.spotnav.core.session import OWNER_PLAN
from custom_components.spotnav.execution.controller import ASSUMED_CURRENT_A
from tests.relay import serve as serve_prices

from .helpers import install_schedule
from .pause_world import open_window, pause_world
from .relay import FakeScheduler
from .test_dashboard_api import NOW
from .test_replug import _car, _record_charger_commands, Car

pytestmark = pytest.mark.usefixtures("offline_relay")


async def test_a_running_charge_with_no_request_is_reckoned_at_a_safe_current(
    hass: HomeAssistant, timers: FakeScheduler
) -> None:
    """A charge the charger runs with nothing on record (it began by itself, or a record was lost): load balancing
    reckons it at the charging current set, else the plan's, else `ASSUMED_CURRENT_A`, so it is regulated."""
    world = await pause_world(hass, timers, charging=True)
    controller = world.controller
    controller._requested_current_a = None  # noqa: SLF001 - nothing on record
    assert controller.resolve_current().amps is None, "the hand-back still reads nothing"

    assumed = controller.balancing_current()
    assert (assumed.amps, assumed.source) == (ASSUMED_CURRENT_A, "assumed")

    await world.store.async_update("entry_a", mutate=lambda settings: replace(settings, amps=13))
    assert controller.balancing_current().amps == 13, "the charging current the person set"

    await world.switch("off")
    assert controller.balancing_current().amps is None, "a charger that does not charge asks for nothing here"
    await world.shutdown()


async def test_a_plans_current_is_the_request_when_no_current_is_set(
    hass: HomeAssistant, timers: FakeScheduler
) -> None:
    world = await pause_world(hass, timers)
    await install_schedule(world.controller, open_window(amps=11))
    assert world.controller.charging, "the open window started"
    world.controller._requested_current_a = None  # noqa: SLF001 - a record that lost it
    assert world.controller.balancing_current().amps == 11
    await world.shutdown()


# ------------------------------------------------------------------------------- the real async_setup_entry


async def test_the_real_setup_reads_the_record_before_the_first_calculation(hass: HomeAssistant, transport: Any) -> None:
    """Through `async_setup_entry` (a reload): the plan's charge running in its window keeps its owner and its request,
    and the restart is decided before any plan is installed."""
    serve_prices(transport, rising=True)
    with freeze_time(NOW) as frozen:
        calls = _record_charger_commands(hass)
        # More asked than the hours to the departure give: every slot to it is the plan's, the first one now.
        car = await _car(hass, frozen, kwh=100.0, departure=time(10, 0), departure_enabled=True)
        assert car.executor.window_charging_now()
        assert [call[0] for call in calls] == ["turn_on"]
        assert car.controller.session_runner.session.owner == OWNER_PLAN
        requested = car.controller.requested_current_a
        assert requested is not None
        identity = car.controller.plan.auto_identity

        frozen.tick(timedelta(minutes=1))
        assert await hass.config_entries.async_reload(car.charger.entry_id)
        await hass.async_block_till_done()
        restarted = Car(hass, frozen, car.charger)
        restarted.plug.connected = True

        controller = restarted.controller
        kinds = [record["event"]["kind"] for record in controller.session_runner.events]
        assert kinds[0] == "restart" and "plan_installed" not in kinds
        assert controller.plan is not None and controller.plan.auto_identity == identity
        assert controller.session_runner.session.owner == OWNER_PLAN
        assert controller.requested_current_a == requested
        assert [call[0] for call in calls if call[0] == "turn_off"] == []
