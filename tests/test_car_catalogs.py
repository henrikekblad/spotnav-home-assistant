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

from typing import NamedTuple

import pytest
from homeassistant.core import HomeAssistant

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
