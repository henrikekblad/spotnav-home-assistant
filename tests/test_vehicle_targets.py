"""A car's target follows the car, and the cars at a charger limit what that charger plans for.

* The target percent is a property of the vehicle (`vehicle_properties`, beside its battery size), the same at
  every charger: a charger's settings `target.target_percent` is its planned car's, and setting it there sets
  the car's, which every other charger planning for that car follows. A switch of car that leaves the percent
  as it was takes the new car's target. `update_vehicle` writes it too (`target_percent`, 0-100, `null`
  clears), and a vehicle row states it. A charger's target from before is the selected car's at set-up.
* `vehicle_ids` limits the dashboard's vehicles, the target vehicle and the planner's car; `null` is every car.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest
from homeassistant.core import HomeAssistant

from custom_components.spotnav.api import dashboard as dashboard_api
from custom_components.spotnav.planning.auto_settings import DRIVER_TARGET_SOC, TargetSocIntent
from custom_components.spotnav.runtime import domain_data, preview_for
from custom_components.spotnav.vehicles import vehicle_properties

from .messages import update_settings_message
from .world import add_car, admin, settings_of, setup_charger, ws_call

pytestmark = pytest.mark.usefixtures("offline_relay")


async def _world(hass: HomeAssistant) -> tuple[Any, Any, str, str]:
    a = await setup_charger(hass)
    b = await setup_charger(hass, entry_id="entry_b", webhook_id="webhook-b", charge_control="switch.charger_b")
    kia, tesla = add_car(hass, "Kia"), add_car(hass, "Tesla")
    return a, b, kia, tesla


async def _plan_for(hass: HomeAssistant, entry_id: str, car: str, percent: float | None) -> None:
    preview = preview_for(hass, entry_id)
    await preview.async_apply_settings(
        mutate=lambda settings: replace(
            settings, driver=DRIVER_TARGET_SOC, target=TargetSocIntent(vehicle_id=car, target_percent=percent)
        )
    )
    await hass.async_block_till_done()


def target_of(hass: HomeAssistant, car: str) -> float | None:
    return vehicle_properties.stored_properties(hass, car).target_percent


def _body(hass: HomeAssistant, entry_id: str, **changes: Any) -> dict[str, Any]:
    from custom_components.spotnav.api.settings import encode_settings

    encoded = encode_settings(settings_of(hass, entry_id))
    return {k: v for k, v in encoded.items() if k not in ("revision", "fiscal_included")} | changes


async def test_a_target_set_at_one_charger_is_the_cars_at_every_charger(hass: HomeAssistant) -> None:
    a, b, kia, _tesla = await _world(hass)
    await _plan_for(hass, b.entry_id, kia, 70.0)
    await _plan_for(hass, a.entry_id, kia, 85.0)
    assert target_of(hass, kia) == 85.0
    assert settings_of(hass, b.entry_id).target.target_percent == 85.0, "the other charger follows the car"


async def test_switching_the_car_takes_its_own_target_and_a_new_percent_is_kept(
    hass: HomeAssistant, hass_ws_client
) -> None:
    a, _b, kia, tesla = await _world(hass)
    await vehicle_properties.async_update_vehicle_properties(
        hass, domain_data(hass).decision_store, tesla, {vehicle_properties.KEY_TARGET: 60.0}
    )
    await _plan_for(hass, a.entry_id, kia, 80.0)
    socket = await admin(hass, hass_ws_client)
    sent = _body(hass, a.entry_id, target={"vehicle_id": tesla, "target_percent": 80.0})
    answer = await ws_call(socket, update_settings_message(a.entry_id, settings_of(hass, a.entry_id).revision, sent))
    assert answer["result"]["settings"]["target"] == {"vehicle_id": tesla, "target_percent": 60.0}
    assert target_of(hass, kia) == 80.0
    sent = _body(hass, a.entry_id, target={"vehicle_id": kia, "target_percent": 75.0})
    answer = await ws_call(socket, update_settings_message(a.entry_id, settings_of(hass, a.entry_id).revision, sent))
    await hass.async_block_till_done()
    assert answer["result"]["settings"]["target"] == {"vehicle_id": kia, "target_percent": 75.0}
    assert target_of(hass, kia) == 75.0 and target_of(hass, tesla) == 60.0


async def test_update_vehicle_sets_the_cars_target_and_its_chargers_follow(hass: HomeAssistant, hass_ws_client) -> None:
    a, _b, kia, _tesla = await _world(hass)
    await _plan_for(hass, a.entry_id, kia, 80.0)
    socket = await admin(hass, hass_ws_client)
    answer = await ws_call(
        socket,
        {"type": "spotnav/update_vehicle", "api_version": 1, "charger_id": a.entry_id, "vehicle_id": kia,
         "changes": {"target_percent": 90}, "expected": {"target_percent": 80.0}},
    )
    await hass.async_block_till_done()
    assert answer["result"]["ok"] is True and answer["result"]["vehicle"]["target_percent"] == 90.0
    assert settings_of(hass, a.entry_id).target.target_percent == 90.0
    refused = await ws_call(
        socket,
        {"type": "spotnav/update_vehicle", "api_version": 1, "charger_id": a.entry_id, "vehicle_id": kia,
         "changes": {"target_percent": 101}},
    )
    assert refused["result"]["field_errors"] == [{"field": "target_percent", "code": "invalid_target"}]


async def test_a_chargers_target_from_before_becomes_its_cars_at_set_up(hass: HomeAssistant) -> None:
    kia = add_car(hass, "Kia")
    a = await setup_charger(hass)
    await domain_data(hass).auto_store.async_update(
        a.entry_id, mutate=lambda s: replace(s, target=TargetSocIntent(vehicle_id=kia, target_percent=72.0))
    )
    assert await hass.config_entries.async_reload(a.entry_id)
    await hass.async_block_till_done()
    assert target_of(hass, kia) == 72.0


async def test_the_cars_at_a_charger_limit_what_it_plans_for(hass: HomeAssistant) -> None:
    a, _b, kia, tesla = await _world(hass)
    capture = lambda: dashboard_api.serialize_dashboard(dashboard_api.capture_dashboard(hass, a), can_act=True)  # noqa: E731
    assert {row["id"] for row in capture()["vehicles"]} == {kia, tesla}
    await _plan_for(hass, a.entry_id, kia, 80.0)
    preview = preview_for(hass, a.entry_id)
    await preview.async_apply_settings(mutate=lambda s: replace(s, vehicle_ids=(tesla,)))
    await hass.async_block_till_done()
    payload = capture()
    assert [row["id"] for row in payload["vehicles"]] == [tesla]
    assert payload["target_vehicle_id"] == tesla, "the only car at this charger"
    assert vehicle_properties.resolved_vehicle_id(hass, a.entry_id) == tesla
    await preview.async_apply_settings(mutate=lambda s: replace(s, vehicle_ids=None))
    await hass.async_block_till_done()
    assert capture()["target_vehicle_id"] == kia


async def test_a_vehicle_row_states_the_cars_target(hass: HomeAssistant) -> None:
    a, _b, kia, tesla = await _world(hass)
    await _plan_for(hass, a.entry_id, kia, 80.0)
    rows = {
        row["id"]: row
        for row in dashboard_api.serialize_dashboard(dashboard_api.capture_dashboard(hass, a), can_act=True)["vehicles"]
    }
    assert rows[kia]["target_percent"] == 80.0 and rows[tesla]["target_percent"] is None


async def test_every_detected_car_stays_choosable_for_the_cars_at_a_charger(hass: HomeAssistant) -> None:
    a, _b, kia, tesla = await _world(hass)
    preview = preview_for(hass, a.entry_id)
    await preview.async_apply_settings(mutate=lambda s: replace(s, vehicle_ids=(tesla,)))
    await hass.async_block_till_done()
    payload = dashboard_api.serialize_dashboard(dashboard_api.capture_dashboard(hass, a), can_act=True)
    assert [row["id"] for row in payload["vehicles"]] == [tesla], "planning sees the charger's cars"
    assert payload["vehicle_choices"] == [{"id": kia, "name": "Kia"}, {"id": tesla, "name": "Tesla"}], (
        "the settings can tick a car again"
    )
