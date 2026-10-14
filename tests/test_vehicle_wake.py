"""Waking a car for a reading: off by default, only through an integration whose wake is known, at most once per
charge end and once per half hour per car (across a restart), and never an error.

A sleeping car's cloud cache can be hours old (an EV6 on kia_uvo read 86 % for a car full at 100 %). With the
setting on, the target's confirmation and a car-ended wait follow the cheap re-read with the integration's own wake.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr, entity_registry as er
from pytest_homeassistant_custom_component.common import async_mock_service, MockConfigEntry

from custom_components.spotnav.api import dashboard as dashboard_api
from custom_components.spotnav.api.webhook import _for_app
from custom_components.spotnav.execution.target_stop import ESTIMATE_CONFIRM_WAIT_S
from custom_components.spotnav.runtime import charger_data, domain_data
from custom_components.spotnav.vehicles import vehicle_properties, vehicle_wake
from custom_components.spotnav.vehicles.discovery_decisions import (
    DECISION_DOMAIN_VEHICLE_WAKE,
    DiscoveryDecisionStore,
)

from .world import add_car, admin, setup_charger_and_site, ws_call

pytestmark = pytest.mark.usefixtures("offline_relay")

START = datetime(2026, 10, 14, 1, 0, tzinfo=timezone.utc)


def _entity(hass: HomeAssistant, platform: str, device_id: str, domain: str, key: str, state: str, **attrs: Any) -> str:
    entry = er.async_get(hass).async_get_or_create(
        domain, platform, f"{device_id}-{key}", device_id=device_id, suggested_object_id=f"{platform}_{key}",
        translation_key=key if domain == "button" else None,
    )
    hass.states.async_set(entry.entity_id, state, attrs)
    return entry.entity_id


def _car(hass: HomeAssistant, platform: str, name: str = "EV6", *, button: bool = False) -> str:
    """A car of `platform`: a battery level and a range, and (for a Tesla integration) its wake button."""
    MockConfigEntry(domain=platform, entry_id=f"{platform}_entry").add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=f"{platform}_entry", identifiers={(platform, name)}, name=name
    )
    _entity(hass, platform, device.id, "sensor", "battery_level", "62", device_class="battery", unit_of_measurement="%")
    _entity(hass, platform, device.id, "sensor", "range", "300", device_class="distance", unit_of_measurement="km")
    if button:
        _entity(hass, platform, device.id, "button", "wake", "unknown")
    return device.id


async def _turn_on(hass: HomeAssistant, car: str) -> None:
    await vehicle_properties.async_update_vehicle_properties(
        hass, domain_data(hass).decision_store, car, {vehicle_properties.KEY_WAKE: True}
    )


async def _setup(hass: HomeAssistant, freezer: Any, platform: str = "kia_uvo", *, button: bool = False):
    freezer.move_to(START)
    car = _car(hass, platform, button=button)
    charger, _ = await setup_charger_and_site(hass)
    async_mock_service(hass, "homeassistant", "update_entity")
    # The car's last report is two hours old: the re-read brings nothing fresh.
    freezer.move_to(START + timedelta(hours=2))
    return charger, car


async def _ask_for_target(hass: HomeAssistant, charger: Any, car: str) -> None:
    controller = charger_data(hass, charger.entry_id).controller
    assert controller._reading_asker is not None
    assert controller._reading_asker(car) is True
    await hass.async_block_till_done()


async def _car_ended(hass: HomeAssistant, charger: Any, car: str, ended_at: datetime) -> None:
    controller = charger_data(hass, charger.entry_id).controller
    controller._car_ended_at = ended_at
    controller._car_ended_vehicle = car
    controller._car_ended_wait = {"level": 86.0, "estimated": True, "target": 91.0}
    assert controller.car_ended_wait is not None
    controller._car_ended_observer(car, False)
    await hass.async_block_till_done()


def _row(hass: HomeAssistant, charger: Any, car: str) -> dict[str, Any]:
    payload = dashboard_api.serialize_dashboard(dashboard_api.capture_dashboard(hass, charger), can_act=True)
    return next(v for v in payload["vehicles"] if v["id"] == car)


# ------------------------------------------------------------------ the setting


async def test_the_setting_is_off_by_default_and_offered_only_for_a_car_that_can_be_woken(
    hass: HomeAssistant, hass_ws_client
) -> None:
    kia = _car(hass, "kia_uvo")
    charger, _ = await setup_charger_and_site(hass)
    other = add_car(hass, "Volvo")
    assert _row(hass, charger, kia)["wake_for_reading"] is False
    # An integration with no known wake: no row (`null`), and turning it on is refused.
    assert _row(hass, charger, other)["wake_for_reading"] is None
    client = await admin(hass, hass_ws_client)
    message = {
        "type": "spotnav/update_vehicle", "api_version": 1, "charger_id": charger.entry_id,
        "vehicle_id": other, "changes": {"wake_for_reading": True},
    }
    refused = (await ws_call(client, message))["result"]
    assert [(e["field"], e["code"]) for e in refused["field_errors"]] == [("wake_for_reading", "wake_unsupported")]

    message = {**message, "vehicle_id": kia, "expected": {"wake_for_reading": False}}
    message.pop("id", None)
    result = (await ws_call(client, message))["result"]
    assert result["ok"] is True and result["vehicle"]["wake_for_reading"] is True
    assert vehicle_properties.stored_properties(hass, kia).wake_for_reading is True
    # Off clears the record: off is the default and is not stored.
    message = {**message, "changes": {"wake_for_reading": False}, "expected": {"wake_for_reading": True}}
    message.pop("id", None)
    result = (await ws_call(client, message))["result"]
    assert result["vehicle"]["wake_for_reading"] is False
    assert vehicle_properties.stored_properties(hass, kia) == vehicle_properties.VehicleProperties()
    assert vehicle_properties.validate_changes({"wake_for_reading": "yes"}) == {
        "wake_for_reading": "invalid_wake_for_reading"
    }


def test_an_app_that_does_not_read_the_wake_gets_rows_without_it() -> None:
    body = {"vehicles": [{"id": "car", "min_percent": None, "wake_for_reading": False}]}
    assert _for_app(body, {"reads": ["min_soc"]})["vehicles"] == [{"id": "car", "min_percent": None}]
    assert _for_app(body, {"reads": ["min_soc", "wake_for_reading"]})["vehicles"] == body["vehicles"]
    assert _for_app(body, {})["vehicles"] == [{"id": "car"}]


# ------------------------------------------------------------------ when it wakes


async def test_off_never_wakes_the_car(hass: HomeAssistant, freezer) -> None:
    charger, car = await _setup(hass, freezer)
    wakes = async_mock_service(hass, "kia_uvo", "force_update")

    await _ask_for_target(hass, charger, car)
    await _car_ended(hass, charger, car, START + timedelta(hours=2))

    assert wakes == []
    assert vehicle_wake.last_wake(hass, car) is None


async def test_on_wakes_a_kia_once_at_the_targets_confirmation_with_its_device(hass: HomeAssistant, freezer) -> None:
    charger, car = await _setup(hass, freezer)
    await _turn_on(hass, car)
    wakes = async_mock_service(hass, "kia_uvo", "force_update")
    rereads = async_mock_service(hass, "homeassistant", "update_entity")

    await _ask_for_target(hass, charger, car)

    assert [call.data for call in wakes] == [{"device_id": car}]
    # The cheap re-read first, and one more after the wake answered.
    assert len(rereads) == 2
    record = vehicle_wake.last_wake(hass, car)
    assert record is not None and record["result"] == "ok" and record["integration"] == "kia_uvo"
    assert record["occasion"].startswith("target:")

    # A re-ask for the same charge end does not wake it again.
    freezer.tick(timedelta(minutes=40))
    await _ask_for_target(hass, charger, car)
    assert len(wakes) == 1


async def test_on_wakes_a_kia_once_when_the_plan_waits_for_the_level_the_car_ended_at(
    hass: HomeAssistant, freezer
) -> None:
    charger, car = await _setup(hass, freezer)
    await _turn_on(hass, car)
    wakes = async_mock_service(hass, "kia_uvo", "force_update")
    ended_at = START + timedelta(hours=2)

    await _car_ended(hass, charger, car, ended_at)

    assert [call.data for call in wakes] == [{"device_id": car}]
    assert vehicle_wake.last_wake(hass, car)["occasion"] == f"car_ended:{ended_at.isoformat()}"


async def test_a_fresh_reading_after_the_re_read_needs_no_wake(hass: HomeAssistant, freezer) -> None:
    charger, car = await _setup(hass, freezer)
    await _turn_on(hass, car)
    wakes = async_mock_service(hass, "kia_uvo", "force_update")

    async def reread(call: ServiceCall) -> None:
        # The car was awake: its cloud answers with a new level.
        hass.states.async_set("sensor.kia_uvo_battery_level", "88", {"device_class": "battery", "unit_of_measurement": "%"})

    hass.services.async_register("homeassistant", "update_entity", reread)

    await _ask_for_target(hass, charger, car)

    assert wakes == []


async def test_a_tesla_is_woken_with_its_own_wake_button(hass: HomeAssistant, freezer) -> None:
    charger, car = await _setup(hass, freezer, "tesla_fleet", button=True)
    await _turn_on(hass, car)
    presses = async_mock_service(hass, "button", "press")

    await _ask_for_target(hass, charger, car)

    assert [call.data for call in presses] == [{"entity_id": "button.tesla_fleet_wake"}]


async def test_an_unsupported_integration_is_never_woken(hass: HomeAssistant, freezer) -> None:
    charger, car = await _setup(hass, freezer, "polestar_api")
    # Stored on by hand (the command refuses it): still nothing to call.
    await _turn_on(hass, car)
    called: list[str] = []
    original = type(hass.services).async_call

    async def spy(registry, domain, service, *args, **kwargs):
        called.append(f"{domain}.{service}")
        return await original(registry, domain, service, *args, **kwargs)

    type(hass.services).async_call = spy
    try:
        await _ask_for_target(hass, charger, car)
        await _car_ended(hass, charger, car, START + timedelta(hours=2))
    finally:
        type(hass.services).async_call = original

    assert set(called) == {"homeassistant.update_entity"}
    assert _row(hass, charger, car)["wake_for_reading"] is None


# ------------------------------------------------------------------ how often, and failures


async def test_the_interval_holds_across_a_restart(hass: HomeAssistant, freezer) -> None:
    charger, car = await _setup(hass, freezer)
    await _turn_on(hass, car)
    wakes = async_mock_service(hass, "kia_uvo", "force_update")
    await _car_ended(hass, charger, car, START + timedelta(hours=2))
    assert len(wakes) == 1

    # Home Assistant restarts: the stored decisions are read again from storage.
    store = DiscoveryDecisionStore(hass)
    await store.async_load()
    domain_data(hass).decision_store = store
    assert store.confirmed_payload(DECISION_DOMAIN_VEHICLE_WAKE, car)["result"] == "ok"

    # A new charge end ten minutes later: too soon.
    freezer.tick(timedelta(minutes=10))
    assert await vehicle_wake.async_wake_vehicle(hass, car, "car_ended:later") == vehicle_wake.SKIP_TOO_SOON
    assert len(wakes) == 1

    # After the interval a new charge end may wake it.
    freezer.tick(timedelta(seconds=vehicle_wake.WAKE_MIN_INTERVAL_S))
    assert await vehicle_wake.async_wake_vehicle(hass, car, "car_ended:later") == vehicle_wake.RESULT_OK
    assert len(wakes) == 2


async def test_a_failed_wake_is_swallowed_and_still_counts(hass: HomeAssistant, freezer) -> None:
    charger, car = await _setup(hass, freezer)
    await _turn_on(hass, car)
    attempts: list[ServiceCall] = []

    async def fail(call: ServiceCall) -> None:
        attempts.append(call)
        raise HomeAssistantError("rate limited")

    hass.services.async_register("kia_uvo", "force_update", fail)

    await _ask_for_target(hass, charger, car)

    assert len(attempts) == 1
    assert vehicle_wake.last_wake(hass, car)["result"] == "failed"
    freezer.tick(timedelta(minutes=5))
    assert await vehicle_wake.async_wake_vehicle(hass, car, "other") == vehicle_wake.SKIP_TOO_SOON


async def test_diagnostics_show_the_last_wake(hass: HomeAssistant, freezer) -> None:
    charger, car = await _setup(hass, freezer)
    await _turn_on(hass, car)
    async_mock_service(hass, "kia_uvo", "force_update")
    await _ask_for_target(hass, charger, car)

    shown = vehicle_wake.wake_diagnostics(hass)[car]
    assert shown["enabled"] is True and shown["supported"] is True
    assert shown["last"]["result"] == "ok" and shown["last"]["at"] == (START + timedelta(hours=2)).isoformat()


def test_a_wake_fits_inside_the_targets_wait_for_an_asked_reading() -> None:
    # The re-read after a wake must land before the estimate alone decides the stop.
    assert vehicle_wake.WAKE_TIMEOUT_S < ESTIMATE_CONFIRM_WAIT_S
