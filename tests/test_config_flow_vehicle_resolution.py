"""Tests for the config flow that resolves (or dismisses) an ambiguous vehicle.

The flow deliberately creates **no** config entry: it records one decision in
`vehicles/discovery_decisions.py`'s store and then aborts, so an ambiguity a human
resolves never turns into a lasting item in Settings → Devices & Services the
way a charger or site does. Several of these tests pin exactly that.
"""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.spotnav.const import (
    CONF_ENTRY_TYPE,
    DOMAIN,
    ENTRY_TYPE_RESOLVE_VEHICLE,
)
from custom_components.spotnav.vehicles.discovery_decisions import (
    DECISION_DOMAIN_VEHICLE,
    async_setup_decisions,
)
from custom_components.spotnav.vehicles.vehicle_discovery import (
    discover_ambiguous_vehicles,
    discover_vehicles,
)

from .helpers import add_ambiguous_vehicle_device
from custom_components.spotnav.runtime import domain_data


def _options(result: dict, field: str) -> list[dict]:
    """The `SelectOptionDict`s of one field of a rendered form."""
    key = next(key for key in result["data_schema"].schema if str(key) == field)
    return result["data_schema"].schema[key].config["options"]


async def _start_resolve_flow(hass: HomeAssistant) -> dict:
    """`user` → choosing the "resolve an ambiguous vehicle" entry type."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    assert result["step_id"] == "user"
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ENTRY_TYPE: ENTRY_TYPE_RESOLVE_VEHICLE}
    )


async def test_the_resolve_choice_is_offered_only_when_something_is_ambiguous(
    hass: HomeAssistant,
) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    assert ENTRY_TYPE_RESOLVE_VEHICLE not in _options(result, CONF_ENTRY_TYPE)

    add_ambiguous_vehicle_device(hass, unique_id="car_offer", name="Offer car")
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    assert ENTRY_TYPE_RESOLVE_VEHICLE in _options(result, CONF_ENTRY_TYPE)


async def test_the_step_aborts_when_there_is_nothing_ambiguous(hass: HomeAssistant) -> None:
    """Reached anyway (a stale flow), the step says so and stops, creating nothing."""
    before = len(hass.config_entries.async_entries(DOMAIN))
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})

    flow = hass.config_entries.flow._progress[result["flow_id"]]
    result = await flow.async_step_resolve_vehicle()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_ambiguous_vehicles"
    assert len(hass.config_entries.async_entries(DOMAIN)) == before


async def test_choosing_the_right_battery_sensor_confirms_it_and_creates_no_entry(
    hass: HomeAssistant,
) -> None:
    """The device is offered by name, its sensors by entity id, and the choice
    is stored as a confirmation -- with the flow ending in an abort, not an
    entry.
    """
    device_id, soc_entity_id, health_entity_id = add_ambiguous_vehicle_device(
        hass, unique_id="car_flow_resolve", name="Flow car"
    )
    before = len(hass.config_entries.async_entries(DOMAIN))

    result = await _start_resolve_flow(hass)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "resolve_vehicle"
    device_options = _options(result, "device_id")
    assert [option["value"] for option in device_options] == [device_id]
    # Never the bare device id: the label says which device this is and how
    # many readings the next step will ask about.
    assert device_options[0]["label"] != device_id
    assert "Flow car" in device_options[0]["label"]
    assert "2" in device_options[0]["label"]

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"device_id": device_id}
    )

    assert result["step_id"] == "resolve_vehicle_soc"
    # The candidates in a deterministic order, with the "not a vehicle" option
    # last rather than mixed in among entity ids.
    assert [option["value"] for option in _options(result, "soc_entity_id")] == [
        soc_entity_id,
        health_entity_id,
        "dismiss",
    ]

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"soc_entity_id": soc_entity_id}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "vehicle_resolved"

    store = domain_data(hass).decision_store
    assert store is not None
    assert store.confirmed_payload(DECISION_DOMAIN_VEHICLE, device_id) == {
        "soc_entity_id": soc_entity_id
    }

    # Resolved: the device is now a reported vehicle, not an ambiguity to review.
    vehicles = discover_vehicles(hass)
    assert [vehicle.id for vehicle in vehicles] == [device_id]
    assert vehicles[0].soc_percent == 42.0
    assert discover_ambiguous_vehicles(hass) == []
    assert len(hass.config_entries.async_entries(DOMAIN)) == before


async def test_choosing_not_a_vehicle_dismisses_it_and_creates_no_entry(
    hass: HomeAssistant,
) -> None:
    """The other outcome of the same workflow, through the same store.

    "It is not a vehicle" reuses the dismissal decision rather than inventing
    a second mechanism, so the device is neither reported nor offered for
    review again.
    """
    device_id, _soc_entity_id, _health_entity_id = add_ambiguous_vehicle_device(
        hass, unique_id="car_flow_dismiss", name="Not my car"
    )
    before = len(hass.config_entries.async_entries(DOMAIN))

    result = await _start_resolve_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"device_id": device_id}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"soc_entity_id": "dismiss"}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "vehicle_dismissed"

    store = domain_data(hass).decision_store
    assert store is not None
    assert store.is_dismissed(DECISION_DOMAIN_VEHICLE, device_id) is True
    assert store.confirmed_payload(DECISION_DOMAIN_VEHICLE, device_id) is None

    assert discover_vehicles(hass) == []
    assert discover_ambiguous_vehicles(hass) == []
    assert len(hass.config_entries.async_entries(DOMAIN)) == before


async def test_each_resolution_only_touches_the_device_it_was_run_for(
    hass: HomeAssistant,
) -> None:
    """Two ambiguous devices, one reviewed: the other stays for later."""
    first_id, first_soc, _ = add_ambiguous_vehicle_device(
        hass, unique_id="car_flow_first", name="First car"
    )
    second_id, _second_soc, _ = add_ambiguous_vehicle_device(
        hass, unique_id="car_flow_second", name="Second car"
    )

    result = await _start_resolve_flow(hass)

    assert sorted(option["value"] for option in _options(result, "device_id")) == sorted(
        [first_id, second_id]
    )

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"device_id": first_id}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"soc_entity_id": first_soc}
    )

    assert result["reason"] == "vehicle_resolved"
    assert [candidate.id for candidate in discover_ambiguous_vehicles(hass)] == [second_id]


async def test_a_device_that_stops_being_ambiguous_mid_flow_is_not_decided_again(
    hass: HomeAssistant,
) -> None:
    """Something else resolved (or dismissed) the device while the form was open.

    Then there is nothing left to decide, so the flow says so rather than
    recording a decision on top of the one already there.
    """
    device_id, soc_entity_id, _ = add_ambiguous_vehicle_device(
        hass, unique_id="car_flow_raced", name="Raced car"
    )
    before = len(hass.config_entries.async_entries(DOMAIN))

    result = await _start_resolve_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"device_id": device_id}
    )
    assert result["step_id"] == "resolve_vehicle_soc"

    # Another resolution lands first -- e.g. the same flow open in a second
    # browser, or a `dismiss_vehicle` action from an automation.
    store = await async_setup_decisions(hass)
    await store.async_dismiss(DECISION_DOMAIN_VEHICLE, device_id)

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"soc_entity_id": soc_entity_id}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_ambiguous_vehicles"
    assert store.confirmed_payload(DECISION_DOMAIN_VEHICLE, device_id) is None
    assert store.is_dismissed(DECISION_DOMAIN_VEHICLE, device_id) is True
    assert len(hass.config_entries.async_entries(DOMAIN)) == before
