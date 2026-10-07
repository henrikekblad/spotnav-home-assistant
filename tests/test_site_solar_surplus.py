"""The site's solar surplus (`site/solar_surplus.py`'s `site_surplus`): what a car could take from the sun
at the site now, on the basis solar charges on, whatever the chargers' strategies and with no charger at all.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from homeassistant.const import STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import async_get_platforms
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.spotnav.const import CONF_GRID_POWER_SOURCE, DOMAIN
from custom_components.spotnav.sensor import surplus_write_delay
from custom_components.spotnav.site.solar_surplus import (
    SolarObservation,
    site_surplus,
    surplus_breakdown,
)

from .test_solar_total_power import _set_total, direct_solar_setup, TOTAL
from .world import controller_of, SecondsClock, set_charger_delivered_a, setup_site, tick_site

BATTERY = "sensor.solax_battery_w"


def test_car_first_counts_export_a_charging_battery_and_the_cars_draw() -> None:
    surplus = site_surplus(
        net_grid_w=-1500.0, car_w=2000.0, battery_w=800.0, battery_configured=True, priority="car_first"
    )

    assert surplus is not None
    assert surplus.surplus_w == 4300.0
    assert (surplus.export_w, surplus.battery_w, surplus.car_w) == (1500.0, 800.0, 2000.0)
    assert surplus.priority_effective == "car_first"


def test_battery_first_leaves_a_charging_battery_out() -> None:
    surplus = site_surplus(
        net_grid_w=-1500.0, car_w=2000.0, battery_w=800.0, battery_configured=True, priority="battery_first"
    )

    assert surplus is not None
    assert (surplus.surplus_w, surplus.battery_w) == (3500.0, 0.0)
    assert surplus.priority_effective == "battery_first"


@pytest.mark.parametrize("priority", ["car_first", "battery_first"])
def test_a_discharging_battery_is_never_surplus(priority) -> None:
    # The battery feeds the car 1000 W of the 2000 W it draws: only the other 1000 W are the sun's.
    surplus = site_surplus(
        net_grid_w=0.0, car_w=2000.0, battery_w=-1000.0, battery_configured=True, priority=priority
    )

    assert surplus is not None
    assert (surplus.surplus_w, surplus.battery_w) == (1000.0, -1000.0)


def test_import_beyond_what_the_car_draws_shows_no_surplus_never_a_negative_one() -> None:
    surplus = site_surplus(
        net_grid_w=2500.0, car_w=2000.0, battery_w=None, battery_configured=False, priority="car_first"
    )

    assert surplus is not None
    assert surplus.available_w == -500.0
    assert surplus.surplus_w == 0.0
    assert surplus.priority_effective == "battery_first"


def test_no_grid_reading_is_no_value() -> None:
    assert (
        site_surplus(net_grid_w=None, car_w=0.0, battery_w=500.0, battery_configured=True, priority="car_first")
        is None
    )


def test_a_configured_battery_that_cannot_be_read_is_no_value() -> None:
    assert (
        site_surplus(net_grid_w=-1000.0, car_w=0.0, battery_w=None, battery_configured=True, priority="car_first")
        is None
    )


def test_a_site_without_chargers_shows_its_export_and_its_charging_battery() -> None:
    surplus = site_surplus(
        net_grid_w=-1200.0, car_w=0.0, battery_w=3000.0, battery_configured=True, priority="car_first"
    )

    assert surplus is not None
    assert (surplus.surplus_w, surplus.export_w, surplus.battery_w, surplus.car_w) == (4200.0, 1200.0, 3000.0, 0.0)


@pytest.mark.parametrize("priority", ["car_first", "battery_first"])
def test_one_charger_alone_reckons_what_the_site_does(priority) -> None:
    observation = SolarObservation(
        now=0.0,
        signed_grid_w={"L1": -400.0, "L2": -300.0, "L3": 100.0},
        voltage_v={"L1": 230.0, "L2": 230.0, "L3": 230.0},
        car_delivered_a={"L1": 6.0, "L2": 6.0, "L3": 6.0},
        battery_w=700.0,
        car_phases=("L1", "L2", "L3"),
        battery_configured=True,
    )

    breakdown = surplus_breakdown(observation, priority)
    site = site_surplus(
        net_grid_w=-600.0, car_w=3 * 6.0 * 230.0, battery_w=700.0, battery_configured=True, priority=priority
    )

    assert breakdown is not None and site is not None
    assert breakdown.available_w == site.available_w
    assert breakdown.priority_effective == site.priority_effective


# ---- when a new value is written --------------------------------------------------------------------


def _shown(surplus_w: float, priority: str = "car_first") -> dict:
    return {"surplus_w": surplus_w, "export_w": 0, "battery_w": 0, "car_w": 0, "priority": priority}


def test_the_write_rule() -> None:
    assert surplus_write_delay(_shown(1000), _shown(1000), 0.1) is None
    # Unknown, or another priority: at once.
    assert surplus_write_delay(_shown(1000), None, 0.1) == 0.0
    assert surplus_write_delay(None, _shown(1000), 0.1) == 0.0
    assert surplus_write_delay(_shown(1000), _shown(1000, "battery_first"), 0.1) == 0.0
    # 50 W or more: once five seconds have passed since the last write.
    assert surplus_write_delay(_shown(1000), _shown(1050), 1.0) == 4.0
    assert surplus_write_delay(_shown(1000), _shown(950), 7.0) == 0.0
    # Less: once ten seconds have passed.
    assert surplus_write_delay(_shown(1000), _shown(1049), 1.0) == 9.0
    assert surplus_write_delay(_shown(1000), _shown(1001), 12.0) == 0.0


# ---- the entity ----------------------------------------------------------------------------------------


async def _solax(hass: HomeAssistant, *, total_w: float | str = -1200.0, battery_w: float = 3000.0, **kwargs):
    """A site with no charger, the meter's total grid power and a battery (the "SolaX" site)."""
    _set_total(hass, TOTAL, total_w)
    hass.states.async_set(BATTERY, str(battery_w), {"unit_of_measurement": "W"})
    kwargs.setdefault("extra_data", {CONF_GRID_POWER_SOURCE: {"power": TOTAL}})
    site = await setup_site(
        hass, entry_id="solax", title="SolaX", battery_aggregate_power_entity=BATTERY, **kwargs
    )
    entity_id = er.async_get(hass).async_get_entity_id("sensor", DOMAIN, "solax_solar_surplus")
    assert entity_id is not None
    return site, entity_id


def _entity(hass: HomeAssistant, entity_id: str):
    for platform in async_get_platforms(hass, DOMAIN):
        if entity_id in platform.entities:
            return platform.entities[entity_id]
    raise AssertionError(entity_id)


async def test_a_site_without_chargers_has_the_sensor(hass: HomeAssistant) -> None:
    _site, entity_id = await _solax(hass)

    state = hass.states.get(entity_id)
    assert state.state == "4200"
    assert state.attributes["unit_of_measurement"] == "W"
    assert state.attributes["device_class"] == "power"
    assert state.attributes["state_class"] == "measurement"
    assert state.attributes["friendly_name"] == "SolaX Solar surplus"
    assert {key: state.attributes[key] for key in ("export_w", "battery_w", "car_w", "priority")} == {
        "export_w": 1200,
        "battery_w": 3000,
        "car_w": 0,
        "priority": "car_first",
    }
    registry_entry = er.async_get(hass).async_get(entity_id)
    assert registry_entry.entity_category is None
    assert registry_entry.disabled_by is None


async def test_battery_first_leaves_the_charging_battery_out(hass: HomeAssistant) -> None:
    _site, entity_id = await _solax(hass, solar_priority="battery_first")

    state = hass.states.get(entity_id)
    assert (state.state, state.attributes["battery_w"], state.attributes["priority"]) == ("1200", 0, "battery_first")


async def test_no_usable_grid_reading_is_unknown(hass: HomeAssistant) -> None:
    site, entity_id = await _solax(hass)

    hass.states.async_set(TOTAL, "unavailable")
    await tick_site(hass, site.runtime_data.controller)

    assert hass.states.get(entity_id).state == STATE_UNKNOWN


async def test_a_site_without_a_total_grid_power_is_unknown(hass: HomeAssistant) -> None:
    _site, entity_id = await _solax(hass, extra_data={})

    assert hass.states.get(entity_id).state == STATE_UNKNOWN


async def test_the_cars_draw_counts_whatever_the_strategy(hass: HomeAssistant) -> None:
    charger, site_entry, site, *_ = await direct_solar_setup(hass, source={"power": TOTAL}, strategy="cheapest")
    hass.states.async_set(f"switch.{charger.entry_id}", "on")
    set_charger_delivered_a(hass, charger.entry_id, 10.0)
    _set_total(hass, TOTAL, -1000.0)
    await tick_site(hass, site)
    entity = _entity(
        hass, er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"{site_entry.entry_id}_solar_surplus")
    )
    entity._written_at = None  # the next reading is written at once
    await tick_site(hass, site)

    state = hass.states.get(entity.entity_id)
    assert state.state == "7900"
    assert state.attributes["car_w"] == 6900

    # A charger that is not charging draws nothing, whatever its sensor still reads.
    hass.states.async_set(f"switch.{charger.entry_id}", "off")
    entity._written_at = None
    await tick_site(hass, site)
    assert hass.states.get(entity.entity_id).state == "1000"


async def test_writes_are_throttled(hass: HomeAssistant) -> None:
    site_entry, entity_id = await _solax(hass)
    site = controller_of(hass, site_entry.entry_id)
    entity = _entity(hass, entity_id)
    clock = SecondsClock(0.0)
    entity._now = clock.now
    entity._written_at = 0.0
    writes: list[str] = []
    hass.bus.async_listen(
        "state_changed",
        lambda event: writes.append(event.data["new_state"].state) if event.data["entity_id"] == entity_id else None,
    )

    async def reading(at: float, total_w: float | str) -> str:
        clock.value = at
        _set_total(hass, TOTAL, total_w)
        await tick_site(hass, site)
        return hass.states.get(entity_id).state

    # A small change waits ten seconds from the last write.
    assert await reading(1.0, -1220.0) == "4200"
    assert await reading(5.0, -1230.0) == "4200"
    assert await reading(10.5, -1230.0) == "4230"
    # A change of 50 W or more waits only five seconds.
    assert await reading(11.0, -1400.0) == "4230"
    assert await reading(16.0, -1400.0) == "4400"
    # A held change is written when its time comes, with no new reading.
    assert await reading(17.0, -1500.0) == "4400"
    clock.value = 21.0
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=5))
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == "4500"
    # Unknown at once.
    assert await reading(21.5, "unavailable") == STATE_UNKNOWN

    assert writes == ["4230", "4400", "4500", STATE_UNKNOWN]
