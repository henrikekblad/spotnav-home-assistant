"""Which of a car's own entities say it is plugged in, and where it is: identification sources only.

* A plug source is a `binary_sensor` with device class `plug`, else one with device class `connectivity`
  (or none) whose key names a cable, a plug or a charger connection, else a text sensor whose key names a
  plug or a connection and that can read both plugged and unplugged (its declared options; never a number).
  A lock, a door, a flap, the car's own online state and a charging sensor are never one, except as the
  weakest kind of all: a charging-state enum with exactly one unplugged value, when nothing else is found.
* A location source is the device's `device_tracker`, never a route's origin or destination; a live one before a
  last or parked position.
* Exactly one candidate of the best kind is detected (among equals, the whole car's: "any" or "all"); several are
  a choice a person makes; a person may also choose "none". Neither is ever a state-of-charge, range or charge-control source: vehicle detection is
  unchanged by them.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import device_registry as dr, entity_registry as er

from custom_components.spotnav.runtime import domain_data
from custom_components.spotnav.vehicles.discovery_decisions import async_setup_decisions
from custom_components.spotnav.vehicles.identification_sources import (
    async_choose_source,
    is_charging_state_source,
    source_plug_reading,
    sources_block,
    identification_sources,
    location_reading,
    plug_reading,
    SOURCE_LOCATION,
    SOURCE_PLUG,
)
from custom_components.spotnav.vehicles.entity_keys import entity_key, without_device_name
from custom_components.spotnav.vehicles.identification import Candidate, judge
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
        # The car asking for the cable: nothing is plugged in (Skoda's charging state).
        ("connect_cable", False), ("Connect cable", False),
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


async def test_bmw_cardatas_charging_port_plug_state_counts_and_the_port_door_does_not(hass: HomeAssistant) -> None:
    """BavarianData (BMW CarData): `vehicle.body.chargingPort.status`, "Charging Port plug state", CONNECTED or
    DISCONNECTED, beside the DC inlet's state and the combined connection state; Kia's port door never counts."""
    vehicle = car(hass)
    add(hass, vehicle, "binary_sensor", "ev_charge_port", "on")
    add(hass, vehicle, "sensor", "ix3_charging_port_plug_dc_state", "DISCONNECTED")
    add(hass, vehicle, "sensor", "ix3_charging_port_connection_state", "CONNECTED")
    plug = add(hass, vehicle, "sensor", "ix3_charging_port_plug_state", "CONNECTED")
    source = identification_sources(hass, vehicle)[0]
    assert source.entity_id == plug
    assert "binary_sensor.ev_charge_port" not in source.candidates
    assert plug_reading(hass.states.get(plug)) is True
    assert plug_reading(State("sensor.x", "INVALID")) is None
    assert plug_reading(State("sensor.x", "-NA-")) is None


async def test_a_navigation_route_tracker_is_no_location(hass: HomeAssistant) -> None:
    """Tesla Fleet has a `location` tracker and a `route` one (the navigation's destination): only the first is
    where the car is, so it is detected alone."""
    vehicle = car(hass)
    add(hass, vehicle, "device_tracker", "tesla_route", "not_home", source_type="gps")
    add(hass, vehicle, "device_tracker", "tesla_destination", "not_home", source_type="gps")
    tracker = add(hass, vehicle, "device_tracker", "tesla_location", "home", source_type="gps")
    location = identification_sources(hass, vehicle)[1]
    assert location.entity_id == tracker
    assert location.candidates == (tracker,)


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


# ------------------------------------------------------------------------------- any sensor of the car


async def test_others_hold_what_detection_rejects_and_nothing_else(hass: HomeAssistant) -> None:
    vehicle = car(hass)
    other_car = car(hass, "Other")
    detected = add(hass, vehicle, "binary_sensor", "ev_plugged_in", "off", device_class="plug")
    port = add(hass, vehicle, "binary_sensor", "ev_charge_port", "on")
    text = add(hass, vehicle, "sensor", "charge_state", "Connected")
    add(hass, vehicle, "sensor", "charging_power", "0", unit_of_measurement="kW")
    add(hass, vehicle, "sensor", "charger_kind", "ac", device_class="power")
    add(hass, vehicle, "device_tracker", "location", "home")
    add(hass, other_car, "binary_sensor", "other_port", "on")
    plug = identification_sources(hass, vehicle)[0]
    assert plug.candidates == (detected,)
    assert plug.others == (text, port), "sorted by name"
    assert identification_sources(hass, vehicle)[1].others == ()
    block = sources_block(hass, vehicle)
    assert [row["entity_id"] for row in block["plug"]["others"]] == [text, port]
    assert block["plug"]["others"][1]["state"] == "on"
    assert block["plug"]["candidates"][0]["state"] == "off"
    assert "others" not in block["location"]


async def test_a_person_chooses_from_others_and_the_reading_is_read(hass: HomeAssistant) -> None:
    await async_setup_decisions(hass)
    vehicle = car(hass)
    port = add(hass, vehicle, "binary_sensor", "ev_charge_port", "on")
    await async_choose_source(hass, vehicle, SOURCE_PLUG, port)
    plug = identification_sources(hass, vehicle)[0]
    assert plug.entity_id == port and plug.chosen is True
    assert sources_block(hass, vehicle)["plug"]["reading"] == "plugged"
    hass.states.async_set(port, "off")
    assert sources_block(hass, vehicle)["plug"]["reading"] == "unplugged"
    assert plug_reading(hass.states.get(port)) is False


async def test_a_chosen_sensor_that_says_nothing_is_shown_so(hass: HomeAssistant) -> None:
    await async_setup_decisions(hass)
    vehicle = car(hass)
    status = add(hass, vehicle, "sensor", "charge_state", "charging")
    await async_choose_source(hass, vehicle, SOURCE_PLUG, status)
    assert sources_block(hass, vehicle)["plug"]["reading"] == "nothing"
    none = identification_sources(hass, vehicle)[0]
    assert none.entity_id == status


async def test_an_entity_of_another_device_or_with_a_unit_is_refused(hass: HomeAssistant) -> None:
    await async_setup_decisions(hass)
    vehicle = car(hass)
    other_car = car(hass, "Other")
    elsewhere = add(hass, other_car, "binary_sensor", "other_port", "on")
    unit = add(hass, vehicle, "sensor", "charging_power", "0", unit_of_measurement="kW")
    for entity_id in (elsewhere, unit):
        with pytest.raises(ValueError):
            await async_choose_source(hass, vehicle, SOURCE_PLUG, entity_id)
    tracker = add(hass, vehicle, "device_tracker", "location", "home")
    port = add(hass, vehicle, "binary_sensor", "ev_charge_port", "on")
    with pytest.raises(ValueError):
        await async_choose_source(hass, vehicle, SOURCE_LOCATION, port)
    await async_choose_source(hass, vehicle, SOURCE_LOCATION, tracker)


# ------------------------------------------------------------------------------- round 6: more makes


async def test_a_text_plug_must_be_able_to_read_both_ways(hass: HomeAssistant) -> None:
    """Volvo: "Car connection" (`availability`) is the car's online state and never says plugged or unplugged; the
    charging connection status does, and is the plug although its name says charging."""
    vehicle = car(hass)
    online = add(
        hass, vehicle, "sensor", "car_connection", "available", device_class="enum",
        options=["available", "car_in_use", "no_internet", "ota_installation_in_progress", "power_saving_mode"],
    )
    assert identification_sources(hass, vehicle)[0].candidates == ()
    status = add(
        hass, vehicle, "sensor", "charger_connection_status", "connected", device_class="enum",
        options=["connected", "disconnected", "fault"],
    )
    source = identification_sources(hass, vehicle)[0]
    assert source.entity_id == status and source.candidates == (status,)
    assert online in source.others


async def test_a_number_is_never_a_text_plug(hass: HomeAssistant) -> None:
    """Mercedes: the coupler's numeric status ("0"-"4") never reads, and no longer ties with the text one."""
    vehicle = car(hass)
    numeric = add(hass, vehicle, "sensor", "chargecoupleracstatus", "0")
    assert identification_sources(hass, vehicle)[0].candidates == ()
    coupler = add(hass, vehicle, "sensor", "chargeinletcoupler", "vehicle not plugged")
    source = identification_sources(hass, vehicle)[0]
    assert source.entity_id == coupler and numeric not in source.candidates


SKODA_CHARGING_STATE = [
    "charging", "connect_cable", "ready_for_charging", "conserving", "discharging", "charging_interrupted",
]


async def test_a_charging_state_enum_with_one_unplugged_value_is_the_weakest_plug(hass: HomeAssistant) -> None:
    """Skoda (core) has no plug entity, only its charging state: `connect_cable` is unplugged, every other value
    says the cable is in."""
    vehicle = car(hass)
    state = add(hass, vehicle, "sensor", "charging_state", "connect_cable", device_class="enum", options=SKODA_CHARGING_STATE)
    source = identification_sources(hass, vehicle)[0]
    assert source.entity_id == state
    assert is_charging_state_source(hass, state)
    assert source_plug_reading(hass, hass.states.get(state)) is False
    for value, reading in (
        ("charging", True), ("ready_for_charging", True), ("conserving", True), ("charging_interrupted", True),
        ("unknown", None), ("not_a_declared_value", None),
    ):
        hass.states.async_set(state, value, hass.states.get(state).attributes)
        assert source_plug_reading(hass, hass.states.get(state)) is reading, value
    assert sources_block(hass, vehicle)["plug"]["reading"] == "nothing"
    hass.states.async_set(state, "conserving", hass.states.get(state).attributes)
    assert sources_block(hass, vehicle)["plug"]["reading"] == "plugged"
    # Its own rule only through the source: read as text, "conserving" says nothing.
    assert plug_reading(hass.states.get(state)) is None


async def test_a_charging_state_enum_is_no_plug_when_a_real_plug_sensor_exists(hass: HomeAssistant) -> None:
    vehicle = car(hass)
    state = add(hass, vehicle, "sensor", "charging_state", "charging", device_class="enum", options=SKODA_CHARGING_STATE)
    plug = add(hass, vehicle, "binary_sensor", "charger_connected", "on", device_class="plug")
    source = identification_sources(hass, vehicle)[0]
    assert source.entity_id == plug
    assert state in source.candidates, "a person may still choose it"


@pytest.mark.parametrize(
    "options",
    [
        # Toyota: `none` is its idle value, seen with the cable in; no value says unplugged.
        ["charge_complete", "charging", "none", "plugged"],
        # Two unplugged values: no single clear one.
        ["charging", "disconnected", "unplugged"],
        # Only the unplugged value and unknown ones.
        ["disconnected", "none", "error"],
        # No unplugged value at all (Volvo's charging status).
        ["charging", "discharging", "done", "error", "idle", "scheduled"],
    ],
)
async def test_a_charging_enum_without_one_clear_unplugged_value_is_no_plug(
    hass: HomeAssistant, options: list[str]
) -> None:
    vehicle = car(hass)
    state = add(hass, vehicle, "sensor", "charging_status", options[0], device_class="enum", options=options)
    assert identification_sources(hass, vehicle)[0].candidates == ()
    assert not is_charging_state_source(hass, state)


async def test_a_charging_state_needs_the_enum_shape(hass: HomeAssistant) -> None:
    """A plain charging sensor (no declared options, or a binary one) is still no plug."""
    vehicle = car(hass)
    add(hass, vehicle, "sensor", "charging_state", "disconnected")
    add(hass, vehicle, "binary_sensor", "charging", "on", device_class="battery_charging")
    assert identification_sources(hass, vehicle)[0].candidates == ()


T0 = datetime(2026, 11, 8, 17, 0, tzinfo=timezone.utc)


def test_the_charging_state_reading_reaches_the_evidence() -> None:
    attributes = {"device_class": "enum", "options": SKODA_CHARGING_STATE}
    plugged = State("sensor.skoda_charging_state", "conserving", attributes)
    assert judge(Candidate("a", plug=plugged, plug_charging_state=True), T0, T0).positive is not None
    assert judge(Candidate("a", plug=plugged), T0, T0).positive is None


async def test_a_whole_car_plug_sensor_wins_a_tie_with_the_ones_per_port(hass: HomeAssistant) -> None:
    """BMW CarData: one `isPlugged` per port position the car streams, and one for any position."""
    vehicle = car(hass)
    left = add(hass, vehicle, "binary_sensor", "charging_port_front_left_is_plugged", "off", device_class="plug")
    whole = add(hass, vehicle, "binary_sensor", "charging_port_any_position_is_plugged", "off", device_class="plug")
    right = add(hass, vehicle, "binary_sensor", "charging_port_rear_right_is_plugged", "off", device_class="plug")
    source = identification_sources(hass, vehicle)[0]
    assert source.entity_id == whole and source.candidates == tuple(sorted((left, whole, right)))


async def test_a_routes_origin_is_no_location_and_a_parked_one_comes_after_a_live_one(hass: HomeAssistant) -> None:
    """Teslemetry's `origin` is where the route began; Polestar has a live `location` and a `parked_location`."""
    vehicle = car(hass)
    add(hass, vehicle, "device_tracker", "origin", "not_home", source_type="gps")
    parked = add(hass, vehicle, "device_tracker", "parked_location", "home", source_type="gps")
    location = identification_sources(hass, vehicle)[1]
    assert location.entity_id == parked, "the only one left, so it is used"
    live = add(hass, vehicle, "device_tracker", "location", "not_home", source_type="gps")
    location = identification_sources(hass, vehicle)[1]
    assert location.entity_id == live and location.candidates == tuple(sorted((live, parked)))
    last = add(hass, vehicle, "device_tracker", "last_known_position", "home", source_type="gps")
    assert identification_sources(hass, vehicle)[1].entity_id == live
    assert last in identification_sources(hass, vehicle)[1].candidates


async def test_a_literal_name_is_read_without_its_devices_name(hass: HomeAssistant) -> None:
    """uconnect, mg_saic: "<make> <nickname> Battery State of Charge". A car called Max is not "max" anything."""
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=plain_config_entry(hass), identifiers={("test", "max")}, name="Jeep Max"
    )
    registry = er.async_get(hass)

    def entry(name: str, *, has_entity_name: bool = False) -> er.RegistryEntry:
        return registry.async_get_or_create(
            "sensor", "test", f"entity_{len(registry.entities)}", device_id=device.id, original_name=name,
            has_entity_name=has_entity_name,
        )

    literal = without_device_name(entry("Jeep Max Battery State of Charge"), device.name)
    assert literal.original_name == "Battery State of Charge"
    assert "max" not in entity_key(literal).tokens
    assert registry.async_get(literal.entity_id).original_name == "Jeep Max Battery State of Charge", "unchanged"
    # Not the device's name: a word that only starts with it, an entity named the modern way, no device name.
    assert without_device_name(entry("Jeep Maxi Charge"), device.name).original_name == "Jeep Maxi Charge"
    modern = entry("Jeep Max Range", has_entity_name=True)
    assert without_device_name(modern, device.name).original_name == "Jeep Max Range"
    assert without_device_name(entry("Jeep Max Odometer"), None).original_name == "Jeep Max Odometer"
