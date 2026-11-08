"""SpotNav's picks on 23 car integrations' real entity shapes (`tests/fixtures/cars/`, research round 6).

Each catalog holds an integration's entities as read from its source at the recorded commit; `car_catalogs`
registers them on one car device. These are the golden picks: the state of charge, the charge limit SpotNav writes,
the charge target it reads (the limit, or a read-only ceiling), the plug source and the location source. Round 6
fixed, and these pin:

* Volvo: the plug is `charger_connection_status`, not the car's online state `availability` ("Car connection").
* Mercedes (mbapi2020): `chargeinletcoupler`, no longer tied with the numeric `chargecoupleracstatus`.
* Skoda (core): its only plug evidence, the charging-state enum `charging_state` (`connect_cable` is unplugged).
* BMW CarData: the whole-car `anyPosition.isPlugged` among the per-port ones, and `stateOfCharge.target` as the
  ceiling (the professional-mode target no longer ties with it).
* Polestar: the live `location`, not `parked_location`; Teslemetry: `location`, not the route's `origin`.
* uconnect and mg_saic: a car whose entity names start with a nickname such as "Max" is still a car.

Toyota keeps no plug source: its `charging_status` value `none` is Toyota's idle value, seen with the cable in
(`docs/vehicle-identification.md`). nissan_leaf and volkswagen_goconnect are not cars to SpotNav: no device
classes on their state of charge or range (and nissan_leaf registers no device).
"""

from __future__ import annotations

import copy
from typing import Any, NamedTuple

import pytest
from homeassistant.core import HomeAssistant

from custom_components.spotnav.vehicles.identification_sources import source_plug_reading

from .car_catalogs import domains, literal_names, load, picks, register

pytestmark = pytest.mark.usefixtures("offline_relay")


class Picks(NamedTuple):
    soc: str | None
    limit: str | None
    target: str | None
    plug: str | None
    location: str | None


# Read once, before any test runs on the event loop.
CATALOGS = {domain: load(domain) for domain in domains()}
LITERAL = [domain for domain, data in CATALOGS.items() if literal_names(data)]

EXPECTED: dict[str, Picks] = {
    "bavariandata": Picks(
        "sensor:vehicle.powertrain.electric.battery.stateOfCharge.displayed",
        None,
        "sensor:vehicle.powertrain.electric.battery.stateOfCharge.target",
        "binary_sensor:vehicle.powertrain.tractionBattery.charging.port.anyPosition.isPlugged",
        "device_tracker:device_tracker",
    ),
    "fordpass": Picks(
        "sensor:soc",
        "select:globalTargetSoc",
        "select:globalTargetSoc",
        "sensor:elVehPlug",
        "device_tracker:tracker",
    ),
    "jlr_incontrol": Picks(
        "sensor:ev_battery",
        None,
        None,
        "binary_sensor:ev_plugged_in",
        "device_tracker:location",
    ),
    "kia_uvo": Picks(
        "sensor:ev_battery_percentage",
        "number:ev_charge_limits_ac",
        "number:ev_charge_limits_ac",
        "binary_sensor:ev_battery_is_plugged_in",
        "device_tracker:location",
    ),
    "mbapi2020": Picks(
        "sensor:soc",
        None,
        "sensor:max_soc",
        "sensor:chargeinletcoupler",
        "device_tracker:tracker",
    ),
    "mg_saic": Picks(
        "sensor:extendedData1 / bmsPackSOCDsp",
        "number:target_soc",
        "number:target_soc",
        "binary_sensor:chargingGunState",
        "device_tracker:gpsPosition",
    ),
    "mynissan": Picks(
        "sensor:battery_level",
        None,
        None,
        "binary_sensor:plugged_in",
        "device_tracker:location",
    ),
    "myskoda": Picks(
        "sensor:battery_percentage",
        "number:charge_limit",
        "number:charge_limit",
        "binary_sensor:charger_connected",
        "device_tracker:device_tracker",
    ),
    "nissan_leaf": Picks(
        None,
        None,
        None,
        "binary_sensor:plugged_in (LeafPluggedInSensor)",
        None,
    ),
    "polestar": Picks(
        "sensor:battery_level",
        "number:target_soc",
        "number:target_soc",
        "binary_sensor:is_plugged_in",
        "device_tracker:location",
    ),
    "renault": Picks(
        "sensor:battery_level",
        "number:charge_limit_target",
        "number:charge_limit_target",
        "binary_sensor:plugged_in",
        "device_tracker:location",
    ),
    "skoda": Picks(
        "sensor:battery_percentage",
        "number:charge_limit",
        "number:charge_limit",
        "sensor:charging_state",
        "device_tracker:parking_position",
    ),
    "stellantis_vehicles": Picks(
        "sensor:battery",
        None,
        None,
        "binary_sensor:battery_plugged",
        "device_tracker:vehicle",
    ),
    "subaru": Picks(
        "sensor:EV_STATE_OF_CHARGE_PERCENT",
        None,
        None,
        "binary_sensor:EV_IS_PLUGGED_IN",
        "device_tracker:location",
    ),
    "tesla_custom": Picks(
        "sensor:battery",
        "number:charge limit",
        "number:charge limit",
        "binary_sensor:charger",
        "device_tracker:location tracker",
    ),
    "tesla_fleet": Picks(
        "sensor:charge_state_battery_level",
        "number:charge_state_charge_limit_soc",
        "number:charge_state_charge_limit_soc",
        "binary_sensor:charge_state_conn_charge_cable",
        "device_tracker:location",
    ),
    "teslemetry": Picks(
        "sensor:charge_state_battery_level",
        "number:charge_state_charge_limit_soc",
        "number:charge_state_charge_limit_soc",
        "binary_sensor:charge_state_conn_charge_cable",
        "device_tracker:location",
    ),
    "tessie": Picks(
        "sensor:charge_state_usable_battery_level",
        "number:charge_state_charge_limit_soc",
        "number:charge_state_charge_limit_soc",
        "binary_sensor:charge_state_conn_charge_cable",
        "device_tracker:location",
    ),
    "toyota": Picks(
        "sensor:battery_level",
        None,
        None,
        None,
        "device_tracker:parking_location",
    ),
    "uconnect": Picks(
        "sensor:state_of_charge",
        None,
        None,
        "binary_sensor:plugged_in",
        "device_tracker:location",
    ),
    "volkswagen_goconnect": Picks(
        None,
        None,
        None,
        None,
        "device_tracker:position",
    ),
    "volkswagencarnet": Picks(
        "sensor:battery_level",
        "number:battery_target_charge_level",
        "number:battery_target_charge_level",
        "binary_sensor:charging_cable_connected",
        "device_tracker:position",
    ),
    "volvo": Picks(
        "sensor:battery_charge_level",
        None,
        "sensor:target_battery_charge_level",
        "sensor:charger_connection_status",
        "device_tracker:location",
    ),
}
NOT_CARS = {"nissan_leaf", "volkswagen_goconnect"}


def test_every_catalog_has_expected_picks() -> None:
    assert sorted(EXPECTED) == sorted(CATALOGS)


@pytest.mark.parametrize("all_enabled", [False, True], ids=["default", "all_enabled"])
@pytest.mark.parametrize("domain", sorted(CATALOGS))
async def test_spotnav_picks_each_cars_entities(hass: HomeAssistant, domain: str, all_enabled: bool) -> None:
    device_id, keys = register(hass, CATALOGS[domain], all_enabled=all_enabled)
    found = picks(hass, device_id, keys)
    assert found["vehicle"] is (domain not in NOT_CARS)
    assert Picks(found["soc"], found["limit"], found["target"], found["plug"], found["location"]) == EXPECTED[domain]


@pytest.mark.parametrize("nickname", ["Max", "Trip", "Key"])
@pytest.mark.parametrize("domain", LITERAL)
async def test_a_nickname_in_literal_entity_names_changes_nothing(
    hass: HomeAssistant, domain: str, nickname: str
) -> None:
    device_id, keys = register(hass, CATALOGS[domain], all_enabled=False, nickname=nickname)
    found = picks(hass, device_id, keys)
    assert found["vehicle"] is (domain not in NOT_CARS)
    assert Picks(found["soc"], found["limit"], found["target"], found["plug"], found["location"]) == EXPECTED[domain]


def _bmw_as_streamed_in_german() -> dict[str, Any]:
    """BavarianData's entities as eight real BMW and MINI cars stream them (issue #2): of the plug signals only
    `chargingPort.status` (no `isPlugged` binary sensor, no `connectorStatus`, no other charging port state), on an
    install whose entity names are not English."""
    data = copy.deepcopy(CATALOGS["bavariandata"])
    data["entities"] = [
        entity
        for entity in data["entities"]
        if not entity["key"].endswith((".isPlugged", ".connectorStatus"))
        and entity["key"]
        not in (
            "vehicle.body.chargingPort.combinedStatus",
            "vehicle.body.chargingPort.dcStatus",
            "vehicle.body.chargingPort.statusClearText",
        )
    ]
    for number, entity in enumerate(data["entities"]):
        if entity.get("name_en"):
            entity["name_en"] = (
                "Status der Ladesteckverbindung"
                if entity["key"] == "vehicle.body.chargingPort.status"
                else f"Wert {number}"
            )
    return data


async def test_bmw_plug_state_is_found_in_any_language(hass: HomeAssistant) -> None:
    """Only the English name of `chargingPort.status` says plug: it is found as a charging state, whose one
    unplugged value is `disconnected`."""
    device_id, keys = register(hass, _bmw_as_streamed_in_german(), all_enabled=False)
    assert picks(hass, device_id, keys)["plug"] == "sensor:vehicle.body.chargingPort.status"
    entity_id = next(entity_id for entity_id, key in keys.items() if key == "sensor:vehicle.body.chargingPort.status")
    options = hass.states.get(entity_id).attributes["options"]
    for value, reading in (("connected", True), ("disconnected", False), ("invalid", None)):
        hass.states.async_set(entity_id, value, {"device_class": "enum", "options": options})
        assert source_plug_reading(hass, hass.states.get(entity_id)) is reading
