"""Field night of 2026-10-09/10: the estimate from delivered energy was thrown away by the card.

A HALO and two cars at it (a Kia EV6 and a second car), a target at the EV6's own 100 % limit, the EV6's cloud reading
72 % all night. The charger's register counted 24.26 kWh by 02:26, which carries 72 % to 100 % at the charging loss
SpotNav assumes. But the state-of-charge reader kept one anchor, and every time the card or the app was given the
charger's dashboard it listed both cars and read each one's level through that reader: reading the second car took
the anchor, and the EV6's next read anchored its stale 72 % again at the register's present value. Nothing delivered
counted, so the planner planned the whole 72 % need again every half hour until the departure.

Now a car read only to be shown (another car at the charger, a site's list, a solar context with no car chosen) is
read without touching the anchor of the car the charger plans for.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from typing import Any

import pytest
from homeassistant.core import HomeAssistant

from custom_components.spotnav.api.dashboard import capture_soc, capture_vehicles
from custom_components.spotnav.planning.auto_settings import TargetSocIntent
from custom_components.spotnav.runtime import charger_data

from .world import add_car, charger_and_car, REGISTER, settings_of

pytestmark = pytest.mark.usefixtures("offline_relay")

ATTRIBUTES = {"unit_of_measurement": "kWh", "device_class": "energy", "state_class": "total_increasing"}
EV6_KWH = 77.4


def _register(hass: HomeAssistant, kwh: float) -> None:
    hass.states.async_set(REGISTER, f"{kwh:.3f}", ATTRIBUTES)


async def _night(hass: HomeAssistant, freezer: Any) -> tuple[Any, str]:
    charger, ev6, _ = await charger_and_car(
        hass, capacity=EV6_KWH, soc_percent="72", target=TargetSocIntent(vehicle_id=None, target_percent=100)
    )
    _register(hass, 6821.568)
    add_car(hass, "testbil", percent="55")
    store_target = settings_of(hass, charger.entry_id).target
    assert store_target.target_percent == 100
    data = charger_data(hass, charger.entry_id)
    # The plug-in reading anchors at the register as it is now.
    await _settings(hass, charger.entry_id, ev6)
    reading = data.soc_reader.read(ev6)
    assert reading is not None and reading.soc_percent == 72.0 and not reading.estimated
    freezer.tick(timedelta(minutes=40))
    return charger, ev6


async def _settings(hass: HomeAssistant, entry_id: str, ev6: str) -> None:
    from custom_components.spotnav.runtime import domain_data

    await domain_data(hass).auto_store.async_update(
        entry_id, mutate=lambda s: replace(s, target=TargetSocIntent(vehicle_id=ev6, target_percent=100))
    )
    await hass.async_block_till_done()


async def test_the_card_listing_both_cars_keeps_the_ev6_estimate(hass: HomeAssistant, freezer: Any) -> None:
    charger, ev6 = await _night(hass, freezer)
    data = charger_data(hass, charger.entry_id)
    _register(hass, 6845.825)
    await hass.async_block_till_done()
    before = data.soc_reader.read(ev6)
    assert before is not None and before.estimated and before.soc_percent == 100.0, "bounded by the car's limit"

    settings = settings_of(hass, charger.entry_id)
    rows, _ = capture_vehicles(hass, charger.entry_id, settings)
    assert {row.name for row in rows} == {"EV6", "testbil"}
    soc = capture_soc(hass, charger.entry_id, settings)

    after = data.soc_reader.read(ev6)
    assert after is not None and after.estimated, "listing the other car took the EV6's anchor"
    assert after.soc_percent == pytest.approx(100.0)
    assert soc is not None and soc.estimated and soc.value == pytest.approx(100.0)


async def test_a_read_for_no_car_or_another_car_leaves_the_anchor(hass: HomeAssistant, freezer: Any) -> None:
    charger, ev6 = await _night(hass, freezer)
    data = charger_data(hass, charger.entry_id)
    _register(hass, 6833.0)
    await hass.async_block_till_done()
    expected = data.soc_reader.read(ev6)
    assert expected is not None and expected.estimated
    for vehicle_id in (None, ""):
        data.soc_reader.read(vehicle_id)
    other = data.soc_reader.peek(next(row.id for row in capture_vehicles(hass, charger.entry_id, settings_of(
        hass, charger.entry_id))[0] if row.id != ev6))
    assert other is not None and other.soc_percent == 55.0 and not other.estimated
    again = data.soc_reader.read(ev6)
    assert again == expected


async def test_the_planner_sees_the_car_at_its_limit_and_plans_nothing(hass: HomeAssistant, freezer: Any) -> None:
    from .world import preview_for

    charger, ev6 = await _night(hass, freezer)
    _register(hass, 6845.825)
    await hass.async_block_till_done()
    capture_vehicles(hass, charger.entry_id, settings_of(hass, charger.entry_id))
    preview = preview_for(hass, charger.entry_id)
    await preview.async_recalculate()
    snapshot = preview.snapshot()
    assert (snapshot.state, snapshot.reason) == ("nothing_to_charge", "already_at_target")

