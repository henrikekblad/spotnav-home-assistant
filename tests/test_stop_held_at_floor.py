"""A stop the charger answers by holding the charge at its 6 A floor is not a stop.

A Peblar allows three pauses in ten minutes; a Stop beyond that lowers the current limit to 6 A and the charge
runs on (`peblar.PauseBudgetSwitchPath`). The adapter answers `STOP_HELD_AT_FLOOR` for it, never a plain `True`:
the controller keeps who owns the charge and the plan as for a stop that was not executed, says so, tries the
stop again, and load balancing counts the 6 A still drawn instead of nothing.
"""

from __future__ import annotations

from datetime import datetime

import pytest
from freezegun import freeze_time
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.spotnav.execution.chargers.base import STOP_HELD_AT_FLOOR
from custom_components.spotnav.execution.controller import (
    ChargingController,
    ChargingExecutionError,
    EXECUTION_STOP_HELD_AT_FLOOR,
    REGULATED_FLOORED,
)

from .test_charger_closed_loop import _world
from .test_stop_not_executed import _charging_plan_controller, _CONTROLLERS  # noqa: F401 - the fixture's list
from .test_stop_not_executed import _shut_down_controllers  # noqa: F401 - shuts the controllers down
from .world import controller_of, set_site_current_a

pytestmark = pytest.mark.usefixtures("offline_relay")


def _hold_at_floor(controller: ChargingController) -> list[int]:
    calls: list[int] = []

    async def floored() -> str:
        calls.append(1)
        return STOP_HELD_AT_FLOOR

    controller.adapter.path.async_stop = floored  # type: ignore[method-assign]
    return calls


async def test_a_stop_held_at_the_floor_keeps_the_owner_and_the_plan_and_says_so(hass: HomeAssistant) -> None:
    controller = await _charging_plan_controller(hass)
    plan = controller.plan
    calls = _hold_at_floor(controller)

    with pytest.raises(ChargingExecutionError) as raised:
        await controller.async_stop(clear_schedule=True)

    assert raised.value.code == EXECUTION_STOP_HELD_AT_FLOOR
    assert calls == [1]
    assert controller.charge_origin == "plan_window"
    assert controller.plan is plan, "the charge runs on, so the plan behind it is not dropped"
    assert controller.adapter.command_log()[-1]["outcome"] == STOP_HELD_AT_FLOOR


async def test_a_window_end_held_at_the_floor_is_tried_again(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    controller = await _charging_plan_controller(hass)
    _hold_at_floor(controller)

    controller._async_end_callback(dt_util.utcnow())  # noqa: SLF001 - the window timer fires
    await hass.async_block_till_done()

    assert controller.charge_origin == "plan_window"
    assert controller._stop_retry_cancel is not None, "the stop is taken again"  # noqa: SLF001
    assert "held the charge at the floor" in caplog.text
    assert "6 A" in caplog.text


async def test_an_overload_stop_held_at_the_floor_counts_the_floor(hass: HomeAssistant) -> None:
    with freeze_time(datetime(2026, 9, 30, 12, 0, 0)) as frozen:
        charger, entry, controller, site, prefix = await _world(hass, "fl")
        await controller.async_start(amps=16)
        set_site_current_a(hass, prefix, 16.0)
        assert await hass.config_entries.async_setup(site.entry_id)
        await hass.async_block_till_done()
        site_controller = controller_of(hass, site.entry_id)
        calls = _hold_at_floor(controller)
        origin = controller.charge_origin

        # The fuse needs a lower current the 90 s policy will not write in time: a safety stop, which the
        # charger answers by holding the charge at 6 A.
        frozen.tick(20)
        set_site_current_a(hass, prefix, 22.0)
        await hass.async_block_till_done()

        assert calls, "the safety stop was sent"
        assert charger.stops == []
        write = await controller.async_apply_regulated_current(6, must_lower=True)
        assert write.outcome == REGULATED_FLOORED and write.code == STOP_HELD_AT_FLOOR
        assert site_controller._dampers[entry.entry_id].last_written_a == 6.0, (  # noqa: SLF001
            "the charger draws the floor, not nothing"
        )
        assert controller.charge_origin == origin, "the charge runs on, and is still whose it was"
        assert not controller._paused_by_balancing, "nothing was paused"  # noqa: SLF001
