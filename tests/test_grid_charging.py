"""The "charging from the grid" binary sensors: one per charger, one per site.

On while the charger charges with energy meant to come from the grid (a plan window, hybrid's grid part,
a manual start, or a charge SpotNav cannot attribute while the site imports); off when idle or paused
and for solar-surplus charging.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.spotnav.execution.controller import ChargingPlan
from custom_components.spotnav.execution.grid_charge import grid_charge
from custom_components.spotnav.planning.auto_settings import STRATEGY_HYBRID, STRATEGY_SOLAR
from custom_components.spotnav.runtime import domain_data

from .world import controller_of, entity_id, setup_site_with_charger

pytestmark = pytest.mark.usefixtures("offline_relay")

CHARGER = "entry_a"
SITE = "site_a"
SWITCH = "switch.entry_a"


def _plan(*, active: bool) -> ChargingPlan:
    now = dt_util.utcnow()
    start = now - timedelta(minutes=10) if active else now + timedelta(hours=2)
    end = start + timedelta(hours=1)
    return ChargingPlan(start=start.isoformat(), end=end.isoformat(), amps=10)


def _strategy(hass: HomeAssistant, strategy: str) -> None:
    store = domain_data(hass).auto_store
    original = store.settings
    store.settings = lambda entry_id: replace(original(entry_id), strategy=strategy)  # type: ignore[method-assign]


async def _world(hass: HomeAssistant) -> tuple[Any, str, str]:
    await setup_site_with_charger(hass)
    controller = controller_of(hass, CHARGER)
    return (
        controller,
        entity_id(hass, CHARGER, "grid_charging", "binary_sensor"),
        entity_id(hass, SITE, "grid_charging", "binary_sensor"),
    )


async def _charge(hass: HomeAssistant, controller: Any, *, on: bool = True) -> None:
    hass.states.async_set(SWITCH, "on" if on else "off")
    controller._notify()  # noqa: SLF001
    await hass.async_block_till_done()


async def test_idle_charger_is_off(hass: HomeAssistant) -> None:
    controller, charger, site = await _world(hass)
    assert hass.states.get(charger).state == "off"
    assert hass.states.get(site).state == "off"
    assert hass.states.get(site).attributes["charger_ids"] == []


async def test_plan_window_is_on(hass: HomeAssistant) -> None:
    controller, charger, site = await _world(hass)
    controller.plan = _plan(active=True)
    await _charge(hass, controller)
    state = hass.states.get(charger)
    assert state.state == "on"
    assert state.attributes["source"] == "plan_window"
    assert state.attributes["window_end"] == controller.plan.windows[0][1].isoformat()
    site_state = hass.states.get(site)
    assert site_state.state == "on"
    assert site_state.attributes["charger_ids"] == [CHARGER]


async def test_hybrid_grid_part_is_on(hass: HomeAssistant) -> None:
    controller, charger, _site = await _world(hass)
    _strategy(hass, STRATEGY_HYBRID)
    controller.plan = _plan(active=True)
    await _charge(hass, controller)
    assert hass.states.get(charger).state == "on"
    assert hass.states.get(charger).attributes["source"] == "hybrid_grid"


async def test_manual_start_is_on_even_under_solar(hass: HomeAssistant) -> None:
    controller, charger, _site = await _world(hass)
    _strategy(hass, STRATEGY_SOLAR)
    controller._charge_origin = "manual"  # noqa: SLF001
    await _charge(hass, controller)
    assert hass.states.get(charger).state == "on"
    assert hass.states.get(charger).attributes["source"] == "manual"


@pytest.mark.parametrize("strategy", [STRATEGY_SOLAR, STRATEGY_HYBRID])
async def test_solar_surplus_charge_is_off(hass: HomeAssistant, strategy: str) -> None:
    controller, charger, site = await _world(hass)
    _strategy(hass, strategy)
    controller._charge_origin = "solar"  # noqa: SLF001
    controller.plan = _plan(active=False)
    await _charge(hass, controller)
    assert controller.charging
    assert hass.states.get(charger).state == "off"
    assert hass.states.get(site).state == "off"


async def test_paused_charger_is_off(hass: HomeAssistant) -> None:
    controller, charger, site = await _world(hass)
    controller.plan = _plan(active=True)
    await _charge(hass, controller)
    assert hass.states.get(site).state == "on"
    await _charge(hass, controller, on=False)
    assert hass.states.get(charger).state == "off"
    assert hass.states.get(site).state == "off"


async def test_unattributed_charge_follows_site_import(hass: HomeAssistant) -> None:
    controller, charger, _site = await _world(hass)
    await _charge(hass, controller)
    assert grid_charge(hass, CHARGER, controller).on is False
    site = hass.config_entries.async_get_entry(SITE).runtime_data.controller
    site.grid_total_reading = lambda: (1500.0, "fresh")  # type: ignore[method-assign]
    result = grid_charge(hass, CHARGER, controller)
    assert result.on and result.source == "grid_import"
    site.grid_total_reading = lambda: (-800.0, "fresh")  # type: ignore[method-assign]
    assert grid_charge(hass, CHARGER, controller).on is False


async def test_power_is_an_attribute_when_known(hass: HomeAssistant) -> None:
    controller, charger, _site = await _world(hass)
    controller.power_entity_id = "sensor.car_power"
    hass.states.async_set("sensor.car_power", "7360", {"unit_of_measurement": "W", "device_class": "power"})
    controller._charge_origin = "manual"  # noqa: SLF001
    await _charge(hass, controller)
    assert hass.states.get(charger).attributes["power_w"] == 7360.0


async def test_origin_survives_a_restart(hass: HomeAssistant) -> None:
    controller, _charger, _site = await _world(hass)
    controller._charge_origin = "solar"  # noqa: SLF001
    # Set directly, so the ownership core is told as a solar start would tell it: when it drives, its own stored
    # record is what a restart reads back (`charge_session`), and it must say the same.
    shadow = controller.ownership_shadow
    shadow.session = replace(shadow.session, owner="solar")
    await controller._async_save()  # noqa: SLF001
    saved = await controller._store.async_load()  # noqa: SLF001
    assert saved["charge_origin"] == "solar"
    controller._charge_origin = None  # noqa: SLF001
    await controller._initialize_locked()  # noqa: SLF001
    assert controller.charge_origin == "solar"
