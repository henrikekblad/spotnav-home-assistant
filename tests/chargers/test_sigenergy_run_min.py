"""Sigenergy's DC charger (EVDC): the running minimum (`sigenergy.EVDC_RUN_MIN_KW`), a lower floor once it charges.

Behind the recipe's unrecorded fact `EVDC_RUN_MIN_CONFIRMED`, shipped off: with it off every floor is the start
minimum (1.5 kW, 2.17 A at 400 V) exactly as before; with it on a charge that is already running may be lowered to
1.0 kW (1.45 A) by load balancing and the sun before it pauses or stops, while a start still needs the start minimum
and, capped by the site, `EVDC_START_MARGIN_A` more. Never below the power-limit number's own `min`.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from datetime import datetime
from types import SimpleNamespace
from typing import Any

import pytest
from freezegun import freeze_time
from homeassistant.core import HomeAssistant

from custom_components.spotnav.execution.chargers import sigenergy
from custom_components.spotnav.execution.chargers.base import (
    ASSIGN_ASSIGNED,
    ASSIGN_BELOW_MINIMUM,
    WRITE_REGULATOR,
)

from ..charger_helpers import adapter_for
from .test_sigenergy import _found, _mock_writes, _set_limit, LIMIT, POWER_LIMIT_ATTRS, START, STATE, STOP
from .test_sigenergy_safety import _evdc_controller, _sun, _unload

RUN_MIN_A = 1.45  # 1.0 kW at 0.6928 kW per ampere (400 V), rounded up to the hundredth.


@pytest.fixture
def frozen(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """Time stands still; the controller's wait before a retried start write moves it on (as in the safety tests)."""
    from custom_components.spotnav.execution import controller as controller_module

    with freeze_time(datetime(2026, 11, 17, 12, 0, 0)) as frozen_time:

        async def sleep(seconds: float) -> None:
            frozen_time.tick(seconds)

        monkeypatch.setattr(controller_module, "power_start_retry_sleep", sleep)
        yield frozen_time


@pytest.fixture
def confirmed(monkeypatch: pytest.MonkeyPatch) -> None:
    """The recipe's fact recorded, as a tester's confirmation would set it."""
    monkeypatch.setattr(sigenergy, "EVDC_RUN_MIN_CONFIRMED", True)


def _charging(hass: HomeAssistant, charging: bool) -> None:
    hass.states.async_set(STATE, "charging" if charging else "occupied", {"device_class": "enum"})


def test_the_fact_is_shipped_off() -> None:
    assert sigenergy.EVDC_RUN_MIN_CONFIRMED is False
    assert sigenergy.EVDC_RUN_MIN_KW == 1.0 < sigenergy.EVDC_MIN_POWER_KW


# -- the path ---------------------------------------------------------------------------------------


async def test_with_the_fact_off_the_running_floor_is_the_start_floor(hass: HomeAssistant) -> None:
    _ids, found = _found(hass)
    calls = _mock_writes(hass)
    _set_limit(hass, 12)
    _charging(hass, True)
    adapter = adapter_for(hass, found)

    assert adapter.current.run_min_kw() is None
    assert adapter.current.run_min_a() == adapter.current.floor_a() == adapter.min_current_a == 2.17
    assert adapter.run_min_current_a == 2.17
    assert await adapter.async_set_current(RUN_MIN_A, reason=WRITE_REGULATOR) == ASSIGN_BELOW_MINIMUM
    assert await adapter.async_set_current(2.16, reason=WRITE_REGULATOR) == ASSIGN_BELOW_MINIMUM
    assert calls == []
    state = adapter.current.describe_state()
    assert (state["run_min_confirmed"], state["run_min_kw"], state["run_min_a"]) == (False, None, 2.17)
    adapter.cancel_pending_reads()


async def test_with_the_fact_on_a_running_charge_is_written_down_to_one_kw(
    hass: HomeAssistant, confirmed: None
) -> None:
    _ids, found = _found(hass)
    calls = _mock_writes(hass)
    _set_limit(hass, 12)
    _charging(hass, True)
    adapter = adapter_for(hass, found)

    assert adapter.current.run_min_kw() == 1.0
    assert adapter.current.run_min_a() == adapter.run_min_current_a == RUN_MIN_A
    # The start floor stays the start minimum: a start reckons with it.
    assert adapter.min_current_a == adapter.min_start_current_a == 2.17

    assert await adapter.async_set_current(RUN_MIN_A, reason=WRITE_REGULATOR) == ASSIGN_ASSIGNED

    assert calls[-1].data == {"entity_id": LIMIT, "value": pytest.approx(1.005, abs=0.001)}
    assert calls[-1].data["value"] >= sigenergy.EVDC_RUN_MIN_KW
    # Read back as the running floor, not rounded down to 1 A.
    assert adapter.current.read_back_a() == RUN_MIN_A
    assert await adapter.async_set_current(1.44, reason=WRITE_REGULATOR) == ASSIGN_BELOW_MINIMUM
    adapter.cancel_pending_reads()


async def test_with_the_fact_on_nothing_below_the_start_floor_is_written_while_not_charging(
    hass: HomeAssistant, confirmed: None
) -> None:
    _ids, found = _found(hass)
    calls = _mock_writes(hass)
    _set_limit(hass, 12)
    _charging(hass, False)
    adapter = adapter_for(hass, found)

    assert adapter.current.floor_a() == 2.17
    assert await adapter.async_set_current(RUN_MIN_A, reason=WRITE_REGULATOR) == ASSIGN_BELOW_MINIMUM
    assert await adapter.async_set_current(2.0, reason=WRITE_REGULATOR) == ASSIGN_BELOW_MINIMUM
    assert calls == []
    adapter.cancel_pending_reads()


@pytest.mark.parametrize(("number_min", "run_min_a"), [(0, RUN_MIN_A), (1.2, 1.74), (1.5, 2.17), (3.0, 2.17)])
async def test_the_running_floor_is_never_below_the_numbers_own_minimum(
    hass: HomeAssistant, confirmed: None, number_min: float, run_min_a: float
) -> None:
    """1.2 kW as the number's `min` is 1.74 A; a `min` at or above the start minimum leaves no running floor."""
    _ids, found = _found(hass)
    hass.states.async_set(LIMIT, "12", {**POWER_LIMIT_ATTRS, "min": number_min})
    adapter = adapter_for(hass, found)

    assert adapter.current.run_min_a() == run_min_a
    assert adapter.current.run_min_kw() == min(max(1.0, number_min), sigenergy.EVDC_MIN_POWER_KW)
    adapter.cancel_pending_reads()


async def test_a_number_in_watts_states_its_minimum_in_watts(hass: HomeAssistant, confirmed: None) -> None:
    _ids, found = _found(hass)
    hass.states.async_set(LIMIT, "12000", {"device_class": "power", "unit_of_measurement": "W", "min": 1200, "max": 100000})
    adapter = adapter_for(hass, found)

    assert adapter.current.run_min_kw() == pytest.approx(1.2)
    adapter.cancel_pending_reads()


# -- the controller and the site --------------------------------------------------------------------


async def _charging_controller(hass: HomeAssistant, frozen_time: Any):
    controller, presses, charger = await _evdc_controller(hass, limit_kw=1.0)
    calls = _mock_writes(hass)
    assert await controller.async_start(amps=16, manual=True) is True
    _charging(hass, True)
    await hass.async_block_till_done()
    frozen_time.tick(sigenergy.EVDC_SETTLE_S + 1)
    return controller, presses, charger, calls


async def test_with_the_fact_off_load_balancing_pauses_below_the_start_floor(
    hass: HomeAssistant, frozen: Any
) -> None:
    controller, presses, charger, calls = await _charging_controller(hass, frozen)
    assert controller.running_min_current_a == controller.min_current_a == 2.17

    paused = await controller.async_apply_regulated_current(RUN_MIN_A, must_lower=True)

    assert not paused.written
    assert [call.data["entity_id"] for call in presses] == [START, STOP]
    assert all(call.data["value"] >= sigenergy.EVDC_MIN_POWER_KW for call in calls)
    controller.adapter.cancel_pending_reads()
    await _unload(hass, charger)


async def test_with_the_fact_on_load_balancing_lowers_a_running_charge_to_one_kw_before_it_pauses(
    hass: HomeAssistant, frozen: Any, confirmed: None
) -> None:
    controller, presses, charger, calls = await _charging_controller(hass, frozen)
    assert controller.running_min_current_a == RUN_MIN_A
    assert controller.min_current_a == 2.17

    held = await controller.async_apply_regulated_current(RUN_MIN_A, must_lower=True)

    assert held.written
    assert calls[-1].data["value"] == pytest.approx(1.005, abs=0.001)
    assert [call.data["entity_id"] for call in presses] == [START]
    frozen.tick(sigenergy.EVDC_SETTLE_S + 1)

    paused = await controller.async_apply_regulated_current(1.4, must_lower=True)

    assert not paused.written
    assert [call.data["entity_id"] for call in presses] == [START, STOP]
    assert all(call.data["value"] >= sigenergy.EVDC_RUN_MIN_KW for call in calls)
    controller.adapter.cancel_pending_reads()
    await _unload(hass, charger)


async def test_with_the_fact_on_a_start_still_needs_the_start_floor_and_its_margin(
    hass: HomeAssistant, frozen: Any, confirmed: None
) -> None:
    """Even with the status still reading charging: a capped start at 2.6 A is held back, at 2.7 A it goes out at the
    start floor (1.50 kW), never at the running one."""
    controller, presses, charger = await _evdc_controller(hass, limit_kw=1.0)
    calls = _mock_writes(hass)
    controller.set_start_cap(lambda: 2.6)

    assert await controller.async_start(amps=16, manual=True) is False
    assert controller.start_held_by_balancing
    assert calls == []

    controller.set_start_cap(lambda: 2.7)
    assert await controller.async_start(amps=16, manual=True) is True
    assert calls[-1].data["value"] == pytest.approx(1.503, abs=0.001)
    assert [call.data["entity_id"] for call in presses] == [START]
    controller.adapter.cancel_pending_reads()
    await _unload(hass, charger)


@pytest.mark.parametrize("fact", [False, True])
async def test_the_sites_floor_is_the_running_one_only_while_the_charger_charges(
    hass: HomeAssistant, frozen: Any, monkeypatch: pytest.MonkeyPatch, fact: bool
) -> None:
    from custom_components.spotnav.runtime import site_controller_for

    monkeypatch.setattr(sigenergy, "EVDC_RUN_MIN_CONFIRMED", fact)
    controller, _presses, charger = await _evdc_controller(hass, limit_kw=1.0)
    assert await hass.config_entries.async_setup("home")
    await hass.async_block_till_done()
    site = site_controller_for(hass, "home")
    assert site is not None

    _charging(hass, False)
    assert site.charger_min_current_a(charger.entry_id) == 2.17
    _charging(hass, True)
    assert site.charger_min_current_a(charger.entry_id) == (RUN_MIN_A if fact else 2.17)
    assert site._minimum_setpoint(charger.entry_id) == (RUN_MIN_A if fact else 2.17)  # noqa: SLF001
    controller.adapter.cancel_pending_reads()
    await hass.config_entries.async_unload("home")
    await _unload(hass, charger)


@pytest.mark.parametrize("fact", [False, True])
async def test_the_sun_starts_at_the_start_floor_and_lowers_a_running_charge_to_the_running_one(
    hass: HomeAssistant, frozen: Any, monkeypatch: pytest.MonkeyPatch, fact: bool
) -> None:
    """The sun starts at 2.17 A (1.50 kW) either way; with the fact on a running charge is held down to 1.45 A
    (1.0 kW) while the surplus falls, where with it off it stays at 2.17 A; the stop threshold is the same."""
    from custom_components.spotnav.execution.solar_execution import SolarExecutionCoordinator
    from custom_components.spotnav.runtime import site_controller_for
    from custom_components.spotnav.site.solar_surplus import SolarController

    monkeypatch.setattr(sigenergy, "EVDC_RUN_MIN_CONFIRMED", fact)
    controller, _presses, charger = await _evdc_controller(hass, limit_kw=1.0)
    assert await hass.config_entries.async_setup("home")
    await hass.async_block_till_done()
    site = site_controller_for(hass, "home")
    assert site is not None
    coordinator = SimpleNamespace(
        _charger_entry_id=charger.entry_id, _controller=controller, _solar_ceiling_a=lambda: 16.0
    )
    idle = SolarExecutionCoordinator._solar_config(coordinator, site)  # noqa: SLF001
    assert (idle.start_a, idle.min_current_a) == (2.17, 2.17)
    _charging(hass, True)
    running = SolarExecutionCoordinator._solar_config(coordinator, site)  # noqa: SLF001
    floor = RUN_MIN_A if fact else 2.17
    assert (running.start_a, running.min_current_a) == (2.17, floor)
    assert running.stop_a == pytest.approx(idle.stop_a) == pytest.approx(1.17)
    # The sun's writes: a start at the start floor, a running charge's floor kept as it is.
    assert SolarExecutionCoordinator._amps(coordinator, 2.17, running=False) == 2.17  # noqa: SLF001
    assert SolarExecutionCoordinator._amps(coordinator, 1.8) == (RUN_MIN_A if fact else 1)  # noqa: SLF001

    solar = SolarController(replace(running, start_delay_s=0.0, min_off_s=0.0, verify_s=0.0, step_confirm_s=0.0))
    started = solar.observe(_sun(0.0, export_w=1600.0))
    assert (started.action, started.requested_a) == ("start", 2.17)
    # The surplus falls to 1.1 kW with the car drawing its 1.50 kW: lowered to the running floor, not stopped.
    for tick in range(1, 30):
        verdict = solar.observe(_sun(float(tick * 10), export_w=1100.0 - 1503.0, car_a=2.17))
        assert verdict.action != "stop", verdict
    assert solar.last_requested_a == pytest.approx(floor)
    controller.adapter.cancel_pending_reads()
    await hass.config_entries.async_unload("home")
    await _unload(hass, charger)
