"""Which of a car's own entities say it is plugged in, and where it is: identification sources only.

* A plug source is a `binary_sensor` with device class `plug`, else one with device class `connectivity`
  (or none) whose key names a cable, a plug or a charger connection, else a text sensor whose key names a
  plug or a connection and whose value reads as plugged or unplugged. A lock, a door, a flap, the car's own
  online state and a charging sensor are never one.
* A location source is the device's `device_tracker`.
* Exactly one candidate of the best kind is detected; several are a choice a person makes; a person may also
  choose "none". Neither is ever a state-of-charge, range or charge-control source: vehicle detection is
  unchanged by them.
"""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import device_registry as dr

from custom_components.spotnav.runtime import domain_data
from custom_components.spotnav.vehicles.discovery_decisions import async_setup_decisions
from custom_components.spotnav.vehicles.identification_sources import (
    async_choose_source,
    identification_sources,
    location_reading,
    plug_reading,
    SOURCE_LOCATION,
    SOURCE_PLUG,
)
from custom_components.spotnav.vehicles.vehicle_discovery import discover_vehicles

from .world import plain_config_entry, vehicle_entity

pytestmark = pytest.mark.usefixtures("offline_relay")


def car(hass: HomeAssistant, name: str = "EV6") -> str:
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=plain_config_entry(hass), identifiers={("test", name)}, name=name
    )
    vehicle_entity(
        hass, device_id=device.id, domain="sensor", object_id=f"{name}_battery", state="55",
        attributes={"device_class": "battery", "unit_of_measurement": "%"},
    )
    vehicle_entity(
        hass, device_id=device.id, domain="sensor", object_id=f"{name}_range", state="300",
        attributes={"device_class": "distance", "unit_of_measurement": "km"},
    )
    return device.id


def add(hass: HomeAssistant, device_id: str, domain: str, object_id: str, state: str, **attributes: Any) -> str:
    return vehicle_entity(
        hass, device_id=device_id, domain=domain, object_id=object_id, state=state, attributes=attributes
    )


# ------------------------------------------------------------------------------- readings


@pytest.mark.parametrize(
    ("state", "reading"),
    [
        ("on", True), ("off", False), ("unavailable", None), ("unknown", None),
        ("plugged", True), ("Plugged", True), ("connected", True), ("plugged_waiting_for_charge", True),
        ("unplugged", False), ("disconnected", False), ("Not plugged", False), ("not_connected", False),
        ("plug_error", None), ("fault", None), ("plug_unknown", None), ("charging", None), ("", None),
    ],
)
def test_a_plug_value_reads_as_plugged_unplugged_or_nothing(state: str, reading: bool | None) -> None:
    domain = "binary_sensor" if state in ("on", "off", "unavailable", "unknown") else "sensor"
    assert plug_reading(State(f"{domain}.x", state)) is reading


@pytest.mark.parametrize(
    ("state", "reading"),
    [("home", True), ("not_home", False), ("Work", False), ("unknown", None), ("unavailable", None)],
)
def test_a_tracker_reads_as_home_away_or_nothing(state: str, reading: bool | None) -> None:
    assert location_reading(State("device_tracker.x", state)) is reading


# ------------------------------------------------------------------------------- detection


async def test_a_plug_sensor_and_a_tracker_are_detected_and_change_nothing_else(hass: HomeAssistant) -> None:
    vehicle = car(hass)
    before = discover_vehicles(hass)
    plug = add(hass, vehicle, "binary_sensor", "ev_battery_is_plugged_in", "off", device_class="plug")
    add(hass, vehicle, "binary_sensor", "ev_battery_is_charging", "off", device_class="battery_charging")
    add(hass, vehicle, "binary_sensor", "charging_cable_locked", "off", device_class="lock")
    tracker = add(hass, vehicle, "device_tracker", "location", "home", source_type="gps")
    plug_source, location_source = identification_sources(hass, vehicle)
    assert plug_source.entity_id == plug and plug_source.chosen is False
    assert plug_source.candidates == (plug,)
    assert location_source.entity_id == tracker and location_source.candidates == (tracker,)
    assert discover_vehicles(hass) == before, "never a reading, range or charge-control source"


async def test_a_connectivity_cable_sensor_counts_but_the_cars_online_state_does_not(hass: HomeAssistant) -> None:
    vehicle = car(hass)
    add(hass, vehicle, "binary_sensor", "online", "on", device_class="connectivity")
    cable = add(hass, vehicle, "binary_sensor", "charge_state_conn_charge_cable", "on", device_class="connectivity")
    assert identification_sources(hass, vehicle)[0].entity_id == cable


async def test_a_text_plug_state_counts_when_nothing_better_is_there(hass: HomeAssistant) -> None:
    vehicle = car(hass)
    add(hass, vehicle, "sensor", "charging_power", "0", unit_of_measurement="kW")
    status = add(hass, vehicle, "sensor", "charger_connection_status", "disconnected")
    assert identification_sources(hass, vehicle)[0].entity_id == status
    better = add(hass, vehicle, "binary_sensor", "plugged_in", "off", device_class="plug")
    assert identification_sources(hass, vehicle)[0].entity_id == better


async def test_two_alike_are_a_choice_and_nothing_is_none(hass: HomeAssistant) -> None:
    vehicle = car(hass)
    assert identification_sources(hass, vehicle)[0].entity_id is None
    assert identification_sources(hass, vehicle)[1].entity_id is None
    one = add(hass, vehicle, "binary_sensor", "plug_one", "off", device_class="plug")
    two = add(hass, vehicle, "binary_sensor", "plug_two", "off", device_class="plug")
    plug = identification_sources(hass, vehicle)[0]
    assert plug.entity_id is None and plug.candidates == tuple(sorted((one, two)))


async def test_a_person_chooses_a_candidate_or_none_and_can_go_back(hass: HomeAssistant) -> None:
    await async_setup_decisions(hass)
    vehicle = car(hass)
    one = add(hass, vehicle, "binary_sensor", "plug_one", "off", device_class="plug")
    two = add(hass, vehicle, "binary_sensor", "plug_two", "off", device_class="plug")
    tracker = add(hass, vehicle, "device_tracker", "location", "home")
    await async_choose_source(hass, vehicle, SOURCE_PLUG, two)
    plug, location = identification_sources(hass, vehicle)
    assert plug.entity_id == two and plug.chosen is True
    assert location.entity_id == tracker and location.chosen is False
    await async_choose_source(hass, vehicle, SOURCE_LOCATION, "none")
    location = identification_sources(hass, vehicle)[1]
    assert location.entity_id is None and location.chosen is True
    await async_choose_source(hass, vehicle, SOURCE_PLUG, None)
    plug = identification_sources(hass, vehicle)[0]
    assert plug.entity_id is None and plug.chosen is False, "back to automatic, which is ambiguous here"
    assert identification_sources(hass, vehicle)[1].chosen is True, "the other choice stands"
    with pytest.raises(ValueError):
        await async_choose_source(hass, vehicle, SOURCE_PLUG, "binary_sensor.elsewhere")
    with pytest.raises(ValueError):
        await async_choose_source(hass, vehicle, "camera", one)
    assert domain_data(hass).decision_store is not None
