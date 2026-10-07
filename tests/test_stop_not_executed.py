"""A stop or a start the charger's control did not execute is not recorded as done.

`ChargerAdapter.async_stop` answers `False` when the command never went out (the control unavailable).
Such a stop used to clear who owned the charge (and with `clear_schedule` the plan) as if it had
worked: a charge still running then read as one the charger began by itself after `STOP_ACK_S`, and
solar could take it over. The owner, the plan and the error stay instead, and the ordinary retries
(a pause's retry, the stray-charge stop) still apply.

Likewise a solar start the executor or the charger refused leaves solar `off`, not believing it runs.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from freezegun import freeze_time
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.spotnav.const import CONF_CHARGE_CONTROL
from custom_components.spotnav.execution.controller import (
    STOP_ACK_S,
    ChargingController,
    ChargingExecutionError,
    EXECUTION_STOP_NOT_EXECUTED,
)

from .helpers import install_schedule
from .test_replug import _obedient_switch, _open_window
from .world import controller_of, set_charger_delivered_a, set_site_power_w, solar_setup, tick_site

pytestmark = pytest.mark.usefixtures("offline_relay")


_CONTROLLERS: list[ChargingController] = []


@pytest.fixture(autouse=True)
async def _shut_down_controllers():
    yield
    while _CONTROLLERS:
        await _CONTROLLERS.pop().async_shutdown()


async def _charging_plan_controller(hass: HomeAssistant) -> ChargingController:
    hass.states.async_set("switch.a", "off")
    _obedient_switch(hass)
    controller = ChargingController(hass, "entry_a", {CONF_CHARGE_CONTROL: "switch.a"})
    _CONTROLLERS.append(controller)
    await controller.async_initialize()
    await install_schedule(controller, _open_window())
    await hass.async_block_till_done()
    assert controller.charging and controller.charge_origin == "plan_window"
    return controller


def _refuse_stops(controller: ChargingController) -> list[int]:
    calls: list[int] = []

    async def refused() -> bool:
        calls.append(1)
        return False

    controller.adapter.async_stop = refused  # type: ignore[method-assign]
    return calls


async def test_a_stop_that_was_not_executed_keeps_the_owner_and_the_plan(hass: HomeAssistant) -> None:
    controller = await _charging_plan_controller(hass)
    plan = controller.plan
    calls = _refuse_stops(controller)

    with pytest.raises(ChargingExecutionError) as raised:
        await controller.async_stop(clear_schedule=True)

    assert raised.value.code == EXECUTION_STOP_NOT_EXECUTED
    assert calls == [1]
    assert controller.charge_origin == "plan_window"
    assert controller.plan is plan, "a plan is not dropped behind a stop that never went out"


async def test_a_charge_behind_an_unexecuted_stop_never_reads_as_self_started(hass: HomeAssistant) -> None:
    """The report's sequence: the stop does not go out, the charger keeps charging, and two minutes later
    the charge must still be the plan's, not one solar may take over and stop."""
    controller = await _charging_plan_controller(hass)
    _refuse_stops(controller)
    with pytest.raises(ChargingExecutionError):
        await controller.async_stop()

    with freeze_time(dt_util.utcnow() + timedelta(seconds=STOP_ACK_S + 5)):
        assert controller.charging
        assert not controller.self_started_charge()
        assert controller.charge_origin == "plan_window"


async def test_a_window_end_whose_stop_is_not_executed_is_logged_not_recorded(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    controller = await _charging_plan_controller(hass)
    _refuse_stops(controller)

    controller._async_end_callback(dt_util.utcnow())  # noqa: SLF001 - the window timer fires
    await hass.async_block_till_done()

    assert controller.charge_origin == "plan_window"
    assert "not executed" in caplog.text


async def test_a_refused_solar_start_leaves_solar_off(hass: HomeAssistant) -> None:
    """Solar arms and gives a start verdict; the charge control is unavailable, so the start is not
    executed. Solar must not believe it runs a charge (it would hold `on` for `min_on_s` with nothing
    charging and adopt a later self-start as its own)."""
    charger, site_entry, controller, coordinator, clock, turn_on_calls, _ = await solar_setup(hass)
    site = controller_of(hass, site_entry.entry_id)

    async def not_executed(_amps: Any = None) -> bool:
        return False

    controller.adapter.async_start = not_executed  # type: ignore[method-assign]
    set_site_power_w(hass, "solar_site", -1400.0)
    set_charger_delivered_a(hass, charger.entry_id, 0.0)
    await tick_site(hass, site)
    clock.value = 125.0
    await tick_site(hass, site)

    assert coordinator.state is not None
    assert coordinator.state.state == "off", coordinator.state
    assert coordinator._solar is not None and not coordinator._solar.running  # noqa: SLF001


# ------------------------------------------------------------------- automatic paths retry, never raise


async def test_a_last_window_end_whose_stop_is_not_executed_is_retried(hass: HomeAssistant, freezer) -> None:
    """The plan's last window ends while the charge control is unavailable: nothing is raised, the charge
    stays the plan's, and the stop is decided again `STOP_RETRY_S` later, when the control is back."""
    from pytest_homeassistant_custom_component.common import async_fire_time_changed

    from custom_components.spotnav.execution.controller import STOP_RETRY_S

    controller = await _charging_plan_controller(hass)
    real_stop = controller.adapter.async_stop
    calls = _refuse_stops(controller)

    freezer.tick(timedelta(minutes=51))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert calls, "the last window's end tried to stop"
    assert controller.charging and controller.charge_origin == "plan_window"

    controller.adapter.async_stop = real_stop  # type: ignore[method-assign]  # the control is back
    freezer.tick(timedelta(seconds=STOP_RETRY_S + 1))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert not controller.charging, "the retry stopped the plan's charge"


async def test_a_stop_command_that_fails_outright_is_one_that_was_not_executed(hass: HomeAssistant) -> None:
    controller = await _charging_plan_controller(hass)

    async def fails() -> bool:
        raise TimeoutError("the charger's cloud did not answer")

    controller.adapter.async_stop = fails  # type: ignore[method-assign]
    with pytest.raises(ChargingExecutionError) as raised:
        await controller.async_stop()

    assert raised.value.code == EXECUTION_STOP_NOT_EXECUTED
    assert controller.charge_origin == "plan_window", "the owner is kept as for a refused stop"


async def test_a_stop_not_executed_reaches_a_person_as_a_translated_error(hass: HomeAssistant) -> None:
    import json
    from pathlib import Path

    from homeassistant.exceptions import HomeAssistantError

    controller = await _charging_plan_controller(hass)
    _refuse_stops(controller)
    with pytest.raises(HomeAssistantError) as raised:
        await controller.async_stop()

    assert raised.value.translation_key == EXECUTION_STOP_NOT_EXECUTED
    root = Path(__file__).parents[1] / "custom_components" / "spotnav" / "translations"
    for language in ("en", "sv", "da", "nb", "fi"):
        exceptions = json.loads((root / f"{language}.json").read_text())["exceptions"]
        for code in ("stop_not_executed", "storage_failed", "reschedule_failed", "rollback_failed"):
            assert exceptions[code]["message"]
