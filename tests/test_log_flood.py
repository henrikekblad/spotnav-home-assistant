"""Rapid sensor updates neither flood the log nor repeat a decision that changes nothing.

The field case (2026-10-06, hybrid, a target of 100 % that the car ends itself): from 08:11:47 the line "the plan
charges to the car's own limit, leaving its end to the car" was logged in pairs about every 80 ms, several hundred
times, after one reading of 100 %; and from 08:01 hybrid's solar decision log recorded a `set_current` several times
a second while the plan held the charge, none of which reached the charger.
"""

from __future__ import annotations

from datetime import timedelta
import logging
from typing import Any

import pytest
from freezegun import freeze_time
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_mock_service

from custom_components.spotnav.execution.controller import ChargingPlan
from custom_components.spotnav.planning.auto_settings import STRATEGY_HYBRID
from custom_components.spotnav.runtime import preview_for
from tests.relay import serve as serve_prices
from tests.test_dashboard_api import NOW

from .test_solar_charger_priority import _two_charger_site
from .world import charger_and_car, controller_of, set_charger_delivered_a, set_site_power_w, solar_setup, tick_site

pytestmark = pytest.mark.usefixtures("offline_relay")

LIMIT_LINE = "leaving its end to the car"


def _soc(hass: HomeAssistant, entity_id: str, percent: float) -> None:
    hass.states.async_set(entity_id, str(percent), {"device_class": "battery", "unit_of_measurement": "%"})


async def test_a_plan_left_to_the_car_says_so_once_however_often_it_is_calculated(
    hass: HomeAssistant, transport: Any, caplog: pytest.LogCaptureFixture
) -> None:
    serve_prices(transport, rising=True)
    async_mock_service(hass, "switch", "turn_on")
    async_mock_service(hass, "switch", "turn_off")
    with freeze_time(NOW):
        charger, device_id, soc_entity = await charger_and_car(
            hass, capacity=15.6, soc_percent="71", amps=16, phases=1
        )
        preview = preview_for(hass, charger.entry_id)
        # The car's own limit is the target (80 %): the car, not SpotNav, ends the charge.
        controller_of(hass, charger.entry_id)._vehicle_limit_reader = lambda _vehicle_id: 80.0  # noqa: SLF001
        controller = controller_of(hass, charger.entry_id)
        assert controller.plan is not None and controller.charges_to_vehicle_limit()

        calls = 0
        real = controller.async_end_plan_need_met

        async def counted() -> bool:
            nonlocal calls
            calls += 1
            return await real()

        controller.async_end_plan_need_met = counted  # type: ignore[method-assign]
        caplog.set_level(logging.INFO)
        # One reading at the limit, and then every sensor update of the site asks for a calculation.
        _soc(hass, soc_entity, 80.0)
        await hass.async_block_till_done()
        for _ in range(60):
            await preview.async_recalculate()
        await hass.async_block_till_done()

        assert preview.snapshot().state == "nothing_to_charge"
        assert controller.plan is not None, "the car ends the charge"
        assert sum(LIMIT_LINE in record.getMessage() for record in caplog.records) == 1
        assert calls <= 1, f"the same plan's need-met decided {calls} times"


async def test_a_solar_reevaluation_does_not_make_the_other_charger_reevaluate(hass: HomeAssistant) -> None:
    """A coordinator re-renders the site's sensors after it acted (`notify_solar_surplus_changed`): that carries no
    new reading, so no other charger on the site evaluates again for it. One site update, one evaluation each."""
    site, coordinators, _clocks, _on = await _two_charger_site(hass, priority_a=None, priority_b=None)
    counts = dict.fromkeys(coordinators, 0)
    for name, coordinator in coordinators.items():
        real = coordinator._async_evaluate

        async def counted(real: Any = real, name: str = name) -> None:
            counts[name] += 1
            await real()

        coordinator._async_evaluate = counted  # type: ignore[method-assign]
    set_site_power_w(hass, "pair_site", -100.0)

    await tick_site(hass, site)

    assert counts == {"a": 1, "b": 1}


async def test_while_the_plan_holds_the_charge_the_suns_decision_log_records_only_changes(
    hass: HomeAssistant,
) -> None:
    charger, site_entry, controller, coordinator, clock, turn_on_calls, turn_off_calls = await solar_setup(
        hass, strategy=STRATEGY_HYBRID
    )
    site = controller_of(hass, site_entry.entry_id)
    now = dt_util.utcnow()
    await controller.async_install(
        ChargingPlan(
            start=(now - timedelta(minutes=1)).isoformat(),
            end=(now + timedelta(minutes=30)).isoformat(),
            amps=16,
            phases=3,
            auto_identity="held",
        )
    )
    hass.states.async_set(f"switch.{charger.entry_id}", "on")
    set_charger_delivered_a(hass, charger.entry_id, 6.0)
    await hass.async_block_till_done()
    assert controller.plan_window_active_now
    requested = controller.requested_current_a
    turn_on_calls.clear()

    # The surplus moves every reading: the sun, on beside the plan, would ask for 7 to 13 A.
    for tick in range(80):
        clock.value = 5.0 * tick
        set_site_power_w(hass, "solar_site", -(333.0 + (tick % 5) * 333.0))
        await tick_site(hass, site)
    assert coordinator.state is not None and coordinator.state.state == "on" and coordinator.state.held_by_plan

    held = [entry for entry in coordinator.decision_log if entry["held_by_plan"]]
    assert [entry["to"] for entry in held] == ["off", "arming", "on"], held
    assert controller.requested_current_a == requested, "nothing the sun decided reached the charger"
    assert turn_on_calls == [] and turn_off_calls == []
