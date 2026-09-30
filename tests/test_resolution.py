"""What still needs a human's decision, as one composed list.

Detection refuses to guess which of a device's battery sensors is its state of charge and which
of its charge-limit `number`s a write should use. This module covers the read side of both: one
list in one shape over the WebSocket command and the webhook field, plus the state-of-charge
confirmation service.
"""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from custom_components.spotnav.const import DOMAIN
from custom_components.spotnav.vehicles.discovery_decisions import (
    DECISION_DOMAIN_VEHICLE,
    DECISION_DOMAIN_VEHICLE_CHARGE_LIMIT,
    async_setup_decisions,
)
from custom_components.spotnav.vehicles.resolution import resolve_required
from custom_components.spotnav.vehicles.vehicle_discovery import (
    discover_ambiguous_charge_limits,
    valid_soc_entity_id,
)

from .helpers import add_ambiguous_vehicle_device
from .world import vehicle_entity, plain_config_entry
from .world import one_charger, vehicle_device
from custom_components.spotnav.runtime import domain_data


def _numbers_only_device(hass: HomeAssistant, *, unique_id: str, name: str) -> dict[str, Any]:
    """Two charge-limit numbers on a device with no range signal: percent numbers on something that never said it was a vehicle, like a lone battery reading."""
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=plain_config_entry(hass),
        identifiers={("test", unique_id)},
        name=name,
    )
    return {
        "device_id": device.id,
        "limits": [
            vehicle_entity(
                hass,
                device_id=device.id,
                domain="number",
                object_id=object_id,
                state=state,
                attributes={"unit_of_measurement": "%", "min": 50, "max": 100},
            )
            for object_id, state in (("limit_a", "80"), ("limit_b", "100"))
        ],
    }


async def test_two_live_limits_are_reported_as_needing_a_decision(
    hass: HomeAssistant,
) -> None:
    """Two live charge-limit numbers, no confirmation: one row with both ids.

    Four neighbours are not ambiguities: one live candidate, a confirmation, a dismissal, and no range signal.
    """
    ambiguous = vehicle_device(
        hass,
        unique_id="ambiguous",
        name="Two-limit car",
        limits=(("limit_a", "80"), ("limit_b", "100")),
    )
    one_candidate = vehicle_device(hass, unique_id="single", name="One-limit car")
    # One live limit beside an asleep one is *one* candidate, not two.
    half_asleep = vehicle_device(
        hass,
        unique_id="half_asleep",
        name="Half-asleep car",
        limits=(("limit_a", "80"), ("limit_b", "0")),
    )
    confirmed = vehicle_device(
        hass,
        unique_id="confirmed",
        name="Chosen car",
        limits=(("limit_a", "80"), ("limit_b", "100")),
    )
    dismissed = vehicle_device(
        hass,
        unique_id="dismissed",
        name="Not a car",
        limits=(("limit_a", "80"), ("limit_b", "100")),
    )
    numbers_only = _numbers_only_device(hass, unique_id="numbers", name="Two numbers")
    store = await async_setup_decisions(hass)
    await store.async_confirm(
        DECISION_DOMAIN_VEHICLE_CHARGE_LIMIT,
        confirmed["device_id"],
        {"charge_limit_entity_id": confirmed["limits"][0]},
    )
    await store.async_dismiss(DECISION_DOMAIN_VEHICLE, dismissed["device_id"])

    rows = {row.id: row for row in discover_ambiguous_charge_limits(hass)}

    assert list(rows) == [ambiguous["device_id"]]
    assert rows[ambiguous["device_id"]].name == "Two-limit car"
    assert rows[ambiguous["device_id"]].candidate_entity_ids == sorted(ambiguous["limits"])
    # Nothing else was reported, so nothing else is in the list.
    for absent in (one_candidate, half_asleep, confirmed, dismissed, numbers_only):
        assert absent["device_id"] not in rows


async def test_valid_soc_entity_id_accepts_only_a_current_candidate(
    hass: HomeAssistant,
) -> None:
    """Another device's sensor, an unknown entity, a non-candidate: all false, and a device with no ambiguity has no candidates, so confirming records nothing."""
    device_id, soc_entity, _health = add_ambiguous_vehicle_device(
        hass, unique_id="car_soc", name="Ambiguous car"
    )
    other_device, _other_soc, _other_health = add_ambiguous_vehicle_device(
        hass, unique_id="other_car", name="Other car"
    )
    plain = vehicle_device(hass, unique_id="plain", name="Plain car")

    assert valid_soc_entity_id(hass, device_id, soc_entity) is True
    assert valid_soc_entity_id(hass, other_device, soc_entity) is False
    assert valid_soc_entity_id(hass, device_id, "sensor.does_not_exist") is False
    assert valid_soc_entity_id(hass, plain["device_id"], plain["soc"]) is False
    assert valid_soc_entity_id(hass, device_id, None) is False


async def test_confirming_a_state_of_charge_records_it_and_ends_the_ambiguity(
    hass: HomeAssistant,
) -> None:
    """The recorded choice decides the device.

    `health_entity` is the non-obvious candidate (the state of charge is the other one); what a sensor measures is the human's knowledge, not a name's.
    """
    device_id, soc_entity, health_entity = add_ambiguous_vehicle_device(
        hass, unique_id="car_soc", name="Ambiguous car"
    )
    await one_charger(hass)
    assert valid_soc_entity_id(hass, device_id, soc_entity) is True

    await hass.services.async_call(
        DOMAIN,
        "confirm_vehicle_soc",
        {"device_id": device_id, "entity_id": health_entity},
        blocking=True,
    )

    store = domain_data(hass).decision_store
    assert store.confirmed_payload(DECISION_DOMAIN_VEHICLE, device_id) == {
        "soc_entity_id": health_entity
    }
    # Decided: not ambiguous any more, so not something to choose between.
    assert valid_soc_entity_id(hass, device_id, soc_entity) is False
    assert resolve_required(hass) == []


async def test_a_state_of_charge_confirmation_that_is_not_a_candidate_records_nothing(
    hass: HomeAssistant,
) -> None:
    """A typo, another device's sensor, and a device with nothing to decide are refused with one warning and record nothing."""
    device_id, soc_entity, _health = add_ambiguous_vehicle_device(
        hass, unique_id="car_soc", name="Ambiguous car"
    )
    other_device, other_soc, _other_health = add_ambiguous_vehicle_device(
        hass, unique_id="other_car", name="Other car"
    )
    plain = vehicle_device(hass, unique_id="plain", name="Plain car")
    await one_charger(hass)

    for owner, entity_id in (
        (device_id, other_soc),
        (device_id, "sensor.does_not_exist"),
        (other_device, soc_entity),
        (plain["device_id"], plain["soc"]),
    ):
        await hass.services.async_call(
            DOMAIN,
            "confirm_vehicle_soc",
            {"device_id": owner, "entity_id": entity_id},
            blocking=True,
        )

    store = domain_data(hass).decision_store
    assert store.confirmed_payload(DECISION_DOMAIN_VEHICLE, device_id) is None
    assert store.confirmed_payload(DECISION_DOMAIN_VEHICLE, other_device) is None
    assert store.confirmed_payload(DECISION_DOMAIN_VEHICLE, plain["device_id"]) is None


async def test_resolve_required_is_one_sorted_list_of_both_kinds(
    hass: HomeAssistant,
) -> None:
    """Both kinds in one list, sorted by `(kind, device_id)` so a changed list is a real change.

    Each row carries the decision, the device, a display label and what to choose between.
    """
    soc_device, soc_entity, health_entity = add_ambiguous_vehicle_device(
        hass, unique_id="car_soc", name="Ambiguous car"
    )
    limit_device = vehicle_device(
        hass,
        unique_id="car_limit",
        name="Two-limit car",
        limits=(("limit_a", "80"), ("limit_b", "100")),
    )

    decisions = resolve_required(hass)

    assert [decision.kind for decision in decisions] == ["charge_limit", "soc"]
    assert [decision.device_id for decision in decisions] == [
        limit_device["device_id"],
        soc_device,
    ]
    assert decisions[0].as_dict() == {
        "kind": "charge_limit",
        "device_id": limit_device["device_id"],
        "name": "Two-limit car",
        "candidate_entity_ids": sorted(limit_device["limits"]),
    }
    assert decisions[1].as_dict() == {
        "kind": "soc",
        "device_id": soc_device,
        "name": "Ambiguous car",
        "candidate_entity_ids": sorted([soc_entity, health_entity]),
    }


async def test_the_websocket_command_returns_the_list(
    hass: HomeAssistant, hass_ws_client
) -> None:
    """The Home Assistant-native read: one command, the standard envelope."""
    await one_charger(hass)
    limit_device = vehicle_device(
        hass,
        unique_id="car_limit",
        name="Two-limit car",
        limits=(("limit_a", "80"), ("limit_b", "100")),
    )
    client = await hass_ws_client(hass)

    await client.send_json({"id": 7, "type": "spotnav/resolve_required"})
    response = await client.receive_json()

    assert response["id"] == 7
    assert response["success"] is True
    assert response["result"] == {
        "decisions": [
            {
                "kind": "charge_limit",
                "device_id": limit_device["device_id"],
                "name": "Two-limit car",
                "candidate_entity_ids": sorted(limit_device["limits"]),
            }
        ]
    }
