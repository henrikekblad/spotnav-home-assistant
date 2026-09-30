"""The `refresh_vehicle` action: asking Home Assistant to re-read a vehicle.

The distinction this feature lives on: `homeassistant.update_entity` re-reads an
entity from whichever integration owns it -- cheap, generic, no effect on the
car -- while a brand's own force-update service *wakes the car*, drawing on its
12 V battery and the vendor's rate limit. Only the first is ever called, by
name, here and in the integration.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import async_mock_service

from custom_components.spotnav.vehicles.discovery_decisions import (
    DECISION_DOMAIN_VEHICLE,
    async_setup_decisions,
)
from custom_components.spotnav.vehicles.vehicle_refresh import (
    REFRESH_MIN_INTERVAL_S,
    VehicleRefreshLimiter,
)

from .helpers import make_entry, webhook_dashboard
from .world import vehicle_entity, plain_config_entry
from custom_components.spotnav.runtime import domain_data



def _vehicle_device(hass: HomeAssistant, *, unique_id: str, name: str) -> dict[str, str]:
    """A fully equipped vehicle-like device: the shape every rule keys off.

    Returns the device id and the entity ids a reading is taken from, so a test
    can assert on the exact set the refresh asks for.
    """
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=plain_config_entry(hass),
        identifiers={("test", unique_id)},
        name=name,
    )
    return {
        "device_id": device.id,
        "soc": vehicle_entity(
            hass,
            device_id=device.id,
            domain="sensor",
            object_id="battery_level",
            state="55",
            attributes={"device_class": "battery", "unit_of_measurement": "%"},
        ),
        "range": vehicle_entity(
            hass,
            device_id=device.id,
            domain="sensor",
            object_id="range",
            state="310",
            attributes={"device_class": "distance", "unit_of_measurement": "km"},
        ),
        "limit": vehicle_entity(
            hass,
            device_id=device.id,
            domain="number",
            object_id="charge_limit",
            state="90",
            attributes={"unit_of_measurement": "%", "min": 50, "max": 100},
        ),
        "capacity": vehicle_entity(
            hass,
            device_id=device.id,
            domain="number",
            object_id="battery_capacity",
            state="77.4",
            attributes={"unit_of_measurement": "kWh", "min": 5, "max": 250, "device_class": "energy_storage"},
        ),
    }


async def _post(client, webhook_id: str, payload: dict[str, Any]):
    return await client.post(f"/api/webhook/{webhook_id}", json=payload)


async def _refresh(client, webhook_id: str, vehicle_id: object):
    return await _post(client, webhook_id, {"version": 1, "action": "refresh_vehicle", "vehicle_id": vehicle_id})


async def _one_charger(hass: HomeAssistant):
    hass.states.async_set("switch.charger_a", "off")
    entry = make_entry(
        hass,
        entry_id="entry_charger_a",
        charge_control="switch.charger_a",
        current_limit=None,
        webhook_id="webhook-a",
        title="Garage",
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_a_refresh_asks_home_assistant_to_re_read_the_reading_entities(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    vehicle = _vehicle_device(hass, unique_id="ev6", name="EV6")
    await _one_charger(hass)
    calls = async_mock_service(hass, "homeassistant", "update_entity")

    response = await _refresh(await hass_client_no_auth(), "webhook-a", vehicle["device_id"])

    assert response.status == 200
    assert await response.json() == {
        "ok": True,
        "action": "refresh_vehicle",
        "entity_count": 4,
    }
    # One call, not one per entity: the same integration owns the same
    # coordinator, so four calls would be four refreshes of the same thing.
    assert len(calls) == 1
    assert set(calls[0].data["entity_id"]) == {
        vehicle["soc"],
        vehicle["range"],
        vehicle["limit"],
        vehicle["capacity"],
    }


async def test_an_unknown_id_a_non_vehicle_and_a_dismissed_vehicle_are_refused_identically(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    vehicle = _vehicle_device(hass, unique_id="ev6", name="EV6")
    # A battery reading with no range signal: the plain-gadget shape detection
    # already refuses to call a vehicle.
    gadget = dr.async_get(hass).async_get_or_create(
        config_entry_id=plain_config_entry(hass),
        identifiers={("test", "door")},
        name="Back door",
    )
    vehicle_entity(
        hass,
        device_id=gadget.id,
        domain="sensor",
        object_id="battery_level",
        state="88",
        attributes={"device_class": "battery", "unit_of_measurement": "%"},
    )
    await async_setup_decisions(hass)
    await domain_data(hass).decision_store.async_dismiss(DECISION_DOMAIN_VEHICLE, vehicle["device_id"])
    await _one_charger(hass)
    calls = async_mock_service(hass, "homeassistant", "update_entity")
    client = await hass_client_no_auth()

    unknown = await _refresh(client, "webhook-a", "device-that-does-not-exist")
    not_a_vehicle = await _refresh(client, "webhook-a", gadget.id)
    dismissed = await _refresh(client, "webhook-a", vehicle["device_id"])

    # One answer for all three, so the action cannot be used to ask which
    # devices exist or which of them are vehicles.
    assert (unknown.status, not_a_vehicle.status, dismissed.status) == (400, 400, 400)
    bodies = [await response.json() for response in (unknown, not_a_vehicle, dismissed)]
    assert bodies[0] == bodies[1] == bodies[2] == {"ok": False, "error": "Unknown vehicle"}
    assert calls == []


async def test_a_second_refresh_within_the_interval_is_refused_with_when_to_retry(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    vehicle = _vehicle_device(hass, unique_id="ev6", name="EV6")
    await _one_charger(hass)
    calls = async_mock_service(hass, "homeassistant", "update_entity")
    client = await hass_client_no_auth()

    first = await _refresh(client, "webhook-a", vehicle["device_id"])
    second = await _refresh(client, "webhook-a", vehicle["device_id"])

    assert first.status == 200
    # 429, not the 400 a malformed or unknown request gets: the request was
    # fine and the vehicle exists -- it simply cannot be asked again yet.
    assert second.status == 429
    body = await second.json()
    assert body["ok"] is False
    # How long to wait, in the body and in the standard header: "no" without
    # "when" reads as a broken button rather than a rate limit.
    assert body["retry_after_s"] == 60
    assert second.headers["Retry-After"] == "60"
    # The refused press costs the vendor's integration nothing.
    assert len(calls) == 1


async def test_the_vehicle_can_be_refreshed_again_once_the_interval_has_passed(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    vehicle = _vehicle_device(hass, unique_id="ev6", name="EV6")
    await _one_charger(hass)
    calls = async_mock_service(hass, "homeassistant", "update_entity")
    # The clock is injected, so the interval passes without sleeping -- the same
    # choice the pairing register's timeout tests make.
    now = [1000.0]
    domain_data(hass).refresh_limiter = VehicleRefreshLimiter(clock=lambda: now[0])
    client = await hass_client_no_auth()

    assert (await _refresh(client, "webhook-a", vehicle["device_id"])).status == 200
    now[0] += REFRESH_MIN_INTERVAL_S
    assert (await _refresh(client, "webhook-a", vehicle["device_id"])).status == 200

    assert len(calls) == 2


async def test_the_interval_is_per_vehicle_not_for_the_whole_instance(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    first_vehicle = _vehicle_device(hass, unique_id="ev6", name="EV6")
    second_vehicle = _vehicle_device(hass, unique_id="zoe", name="Zoe")
    await _one_charger(hass)
    async_mock_service(hass, "homeassistant", "update_entity")
    client = await hass_client_no_auth()

    assert (await _refresh(client, "webhook-a", first_vehicle["device_id"])).status == 200
    # Two cars are two integrations with two vendor rate limits: refreshing one
    # is no reason to refuse the other.
    assert (await _refresh(client, "webhook-a", second_vehicle["device_id"])).status == 200
    assert (await _refresh(client, "webhook-a", first_vehicle["device_id"])).status == 429


async def test_the_capability_is_advertised(hass: HomeAssistant, hass_client_no_auth) -> None:
    await _one_charger(hass)

    dashboard = await webhook_dashboard(await hass_client_no_auth(), "webhook-a")

    # Advertised explicitly and `true`: its absence means "cannot", which is why the app may
    # offer the button only on this literal value.
    assert dashboard["charger"]["capabilities"]["refresh_vehicle"] is True


async def test_only_home_assistants_own_generic_re_read_is_ever_called(
    hass: HomeAssistant, hass_client_no_auth, monkeypatch
) -> None:
    """The one structural claim the rate limit exists to protect.

    A brand's own force-update service would wake the car: 12 V battery, vendor
    rate limit, and in the worst case an account locked out. So the integration
    names no brand and calls no brand service -- it asks Home Assistant's own
    `update_entity`, which works for Kia, Tesla and Polestar alike. This records
    every service call the request makes.
    """
    vehicle = _vehicle_device(hass, unique_id="ev6", name="EV6")
    await _one_charger(hass)
    async_mock_service(hass, "homeassistant", "update_entity")
    # A brand's force-update service, registered the way the real one would be:
    # it must never be called, whatever a refresh needs.
    brand_calls = async_mock_service(hass, "kia_uvo", "force_update")
    called: list[tuple[str, str]] = []
    original = type(hass.services).async_call

    async def spy(registry, domain, service, *args, **kwargs):
        called.append((domain, service))
        return await original(registry, domain, service, *args, **kwargs)

    monkeypatch.setattr(type(hass.services), "async_call", spy)

    response = await _refresh(await hass_client_no_auth(), "webhook-a", vehicle["device_id"])

    assert response.status == 200
    # Exactly one call, and it is the generic one. Nothing that wakes a car.
    assert called == [("homeassistant", "update_entity")]
    assert brand_calls == []


async def test_nothing_sensitive_reaches_the_log(
    hass: HomeAssistant, hass_client_no_auth, caplog
) -> None:
    vehicle = _vehicle_device(hass, unique_id="ev6", name="EV6")
    await _one_charger(hass)
    async_mock_service(hass, "homeassistant", "update_entity")
    client = await hass_client_no_auth()

    with caplog.at_level(logging.DEBUG):
        await _refresh(client, "webhook-a", vehicle["device_id"])  # succeeds
        await _refresh(client, "webhook-a", vehicle["device_id"])  # rate-limited
        await _refresh(client, "webhook-a", "device-that-does-not-exist")  # refused

    ours = [record.getMessage() for record in caplog.records if record.name.startswith("custom_components.spotnav")]
    assert ours, "the refresh path should have logged something at debug"
    for sensitive in (
        vehicle["device_id"],
        vehicle["soc"],
        vehicle["range"],
        vehicle["limit"],
        vehicle["capacity"],
        "EV6",
    ):
        assert all(sensitive not in message for message in ours)
