"""Vehicle detection against the recorded entity shapes of real integrations.

Each case is one car as its integration registers it (keys, domains, device classes, units,
declared ranges) taken from the integration's source, and the result the Track V rules must give:
which state-of-charge entity, which charge limit (and whether it can be written), which capacity,
or "ambiguous" / nothing. No brand is known to the code: only the shapes differ.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.util import slugify
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.spotnav.vehicles.vehicle_discovery import (
    discover_ambiguous_charge_limits,
    discover_ambiguous_vehicles,
    discover_vehicles,
    vehicle_charge_limit_entity_id,
    vehicle_soc_entity_id,
)

BATTERY = {"device_class": "battery", "unit_of_measurement": "%", "state_class": "measurement"}
PERCENT = {"unit_of_measurement": "%"}
RANGE = {"device_class": "distance", "unit_of_measurement": "km"}


def number(minimum: float, maximum: float) -> dict[str, Any]:
    return {"unit_of_measurement": "%", "min": minimum, "max": maximum, "step": 10}


def storage(unit: str = "kWh", state_class: str | None = "measurement") -> dict[str, Any]:
    attrs: dict[str, Any] = {"device_class": "energy_storage", "unit_of_measurement": unit}
    if state_class:
        attrs["state_class"] = state_class
    return attrs


@dataclass(frozen=True)
class E:
    """One registered entity: platform domain, integration key, state, attributes, display name."""

    domain: str
    key: str
    state: str
    attrs: dict[str, Any] = field(default_factory=dict)
    name: str | None = None


def soc(key: str, value: str = "62", name: str | None = None) -> E:
    return E("sensor", key, value, BATTERY, name)


def rng(key: str = "range") -> E:
    return E("sensor", key, "310", RANGE)


@dataclass(frozen=True)
class Case:
    domain: str
    entities: tuple[E, ...]
    # Key of the chosen state-of-charge entity, "ambiguous", or None (not a vehicle).
    soc: str | None
    # Key of the charge limit a write goes to, "ambiguous", or None.
    limit: str | None = None
    # Key of a read-only target sensor used as a ceiling only (limit must then be None).
    ceiling: str | None = None
    capacity_kwh: float | None = None


CASES: tuple[Case, ...] = (
    Case(
        "kia_uvo EU CCS2 BEV: SoH, AC/DC/V2L limits, kJ capacity and remaining",
        (
            soc("ev_battery_percentage"),
            soc("ev_battery_soh_percentage", "96"),
            E("sensor", "car_battery_percentage", "88", PERCENT),  # 12 V, class stripped on BEV
            E("number", "ev_charge_limits_ac", "80", number(50, 100)),
            E("number", "ev_charge_limits_dc", "100", number(50, 100)),
            E("number", "ev_v2l_discharge_limit", "60", number(20, 80)),
            E("sensor", "ev_battery_capacity", "230400", storage("kJ")),
            E("sensor", "ev_battery_remain", "150000", storage("kJ")),
            rng("ev_driving_range"),
        ),
        soc="ev_battery_percentage",
        limit="ev_charge_limits_ac",
        capacity_kwh=64.0,
    ),
    Case(
        "kia_uvo HEV: the 12 V battery keeps the battery class and is not an EV",
        (soc("car_battery_percentage", "88"), rng("total_driving_range")),
        soc=None,
    ),
    Case(
        "kia_uvo before the API answered: only the 12 V sensor and a range",
        (soc("car_battery_percentage", "88"), soc("ev_battery_soh_percentage", "96"), rng()),
        soc=None,
    ),
    Case(
        "ha_kia_hyundai: AC and DC limits",
        (
            soc("ev_battery_level"),
            E("sensor", "car_battery_level", "88", PERCENT),
            E("number", "ev_charge_limits_ac", "80", number(50, 100)),
            E("number", "ev_charge_limits_dc", "100", number(50, 100)),
            rng("ev_remaining_range_value"),
        ),
        soc="ev_battery_level",
        limit="ev_charge_limits_ac",
    ),
    Case(
        "tesla_fleet: usable level, route arrival and a charge_energy_added meter",
        (
            soc("charge_state_battery_level"),
            soc("charge_state_usable_battery_level", "60"),
            soc("drive_state_active_route_energy_at_arrival", "40"),
            E("number", "charge_state_charge_limit_soc", "80", number(50, 100)),
            E("number", "charge_state_charge_current_request", "16", {"unit_of_measurement": "A", "min": 0, "max": 32}),
            E("sensor", "charge_state_charge_energy_added", "12", {"device_class": "energy", "unit_of_measurement": "kWh", "state_class": "total"}),
            rng("charge_state_battery_range"),
        ),
        soc="charge_state_battery_level",
        limit="charge_state_charge_limit_soc",
    ),
    Case(
        "teslemetry with the remaining-energy diagnostic enabled is not a capacity",
        (
            soc("charge_state_battery_level"),
            soc("charge_state_usable_battery_level", "60"),
            E("number", "charge_state_charge_limit_soc", "80", number(50, 100)),
            E("sensor", "energy_remaining", "47.3", storage()),
            rng("charge_state_est_battery_range"),
        ),
        soc="charge_state_battery_level",
        limit="charge_state_charge_limit_soc",
        capacity_kwh=None,
    ),
    Case(
        "tessie: usable_battery_level is the only level; energy_remaining is not a capacity",
        (
            soc("charge_state_usable_battery_level", "60"),
            soc("drive_state_active_route_energy_at_arrival", "40"),
            E("number", "charge_state_charge_limit_soc", "80", number(50, 100)),
            E("sensor", "charge_state_energy_remaining", "47.3", storage()),
            E("sensor", "charge_state_charge_energy_added", "12", {"device_class": "energy", "unit_of_measurement": "kWh", "state_class": "total_increasing"}),
            rng("charge_state_battery_range"),
        ),
        soc="charge_state_usable_battery_level",
        limit="charge_state_charge_limit_soc",
        capacity_kwh=None,
    ),
    Case(
        "tesla_custom",
        (
            soc("battery"),
            E("number", "charge_limit", "80", {"unit_of_measurement": "%", "min": 50, "max": 100}),
            E("number", "charging_amps", "16", {"unit_of_measurement": "A", "min": 0, "max": 32}),
            rng(),
        ),
        soc="battery",
        limit="charge_limit",
    ),
    Case(
        "myskoda",
        (soc("battery_percentage"), E("number", "charge_limit", "80", number(50, 100)), rng()),
        soc="battery_percentage",
        limit="charge_limit",
    ),
    Case(
        "renault: target and the 15-45 minimum level, remaining energy meter",
        (
            soc("battery_level"),
            E("sensor", "hvac_soc_threshold", "20", PERCENT),
            E("number", "charge_limit_target", "80", number(55, 100)),
            E("number", "charge_limit_min", "20", number(15, 45)),
            E("sensor", "battery_available_energy", "40", {"device_class": "energy", "unit_of_measurement": "kWh", "state_class": "total"}),
            rng("battery_autonomy"),
        ),
        soc="battery_level",
        limit="charge_limit_target",
    ),
    Case(
        "volvo: target is a read-only sensor, capacity from the model data",
        (
            soc("battery_charge_level"),
            E("sensor", "target_battery_charge_level", "80", PERCENT),
            E("sensor", "battery_capacity", "82", storage(state_class=None)),
            rng("distance_to_empty_battery"),
        ),
        soc="battery_charge_level",
        ceiling="target_battery_charge_level",
        capacity_kwh=82.0,
    ),
    Case(
        "mbapi2020: max_soc sensor is a ceiling only",
        (
            soc("soc", name="State of Charge"),
            E("sensor", "max_soc", "80", PERCENT, "Max State of Charge"),
            E("sensor", "starterBatteryState", "1", {}),
            rng("rangeElectricKm"),
        ),
        soc="soc",
        ceiling="max_soc",
    ),
    Case(
        "fordpass: percent selects, the 12 V battery has no class",
        (
            soc("soc"),
            E("sensor", "battery", "90", PERCENT),
            E("select", "elVehTargetCharge", "80", {"options": ["50", "60", "70", "80", "85", "90", "95", "100"]}),
            E("select", "elVehTargetCharge_alt1", "90", {"options": ["50", "60", "70", "80", "85", "90", "95", "100"]}),
            E("select", "globalTargetSoc", "100", {"options": ["50", "60", "70", "80", "85", "90", "95", "100"]}),
            E("select", "globalDcPowerLimit", "50", {"options": ["25", "50", "100", "150"]}),
            E("number", "globalAcCurrentLimit", "16", {"unit_of_measurement": "A", "min": 5, "max": 48}),
            rng("elVeh"),
        ),
        soc="soc",
        limit="globalTargetSoc",
    ),
    Case(
        "stellantis_vehicles: the 15-95 limit is the integration's own soft limit",
        (
            soc("battery"),
            soc("service_battery_voltage", "100"),
            E("number", "battery_charging_limit", "80", number(15, 95)),
            E("switch", "battery_charging_limit", "on", {}),
            E("sensor", "battery_capacity", "50", storage()),
            E("sensor", "battery_residual", "31", storage()),
            rng("autonomy"),
        ),
        soc="battery",
        limit=None,
        capacity_kwh=50.0,
    ),
    Case(
        "audiconnect: global, current-location and per-profile targets",
        (
            soc("state_of_charge"),
            E("number", "target_state_of_charge", "80", number(20, 100), "Global charge target"),
            E("number", "active_charging_profile_target_soc", "90", number(20, 100), "Current location charge target"),
            E("number", "charge_profile_12_target_soc", "100", number(20, 100)),
            rng(),
        ),
        soc="state_of_charge",
        limit="target_state_of_charge",
    ),
    Case(
        "cardata: seven battery-class percent sensors, four energy sensors",
        (
            soc("vehicle.drivetrain.batteryManagement.header"),
            soc("vehicle.powertrain.electric.battery.stateOfCharge.displayed", "61"),
            soc("vehicle.drivetrain.electricEngine.charging.level", "61"),
            soc("vehicle.powertrain.electric.battery.stateOfCharge.target", "80"),
            soc("vehicle.trip.segment.end.drivetrain.batteryManagement.hvSoc", "30"),
            soc("vehicle.predicted_soc", "70"),
            soc("vehicle.magic_soc", "59"),
            E("sensor", "vehicle.drivetrain.batteryManagement.batterySizeMax", "80", storage()),
            E("sensor", "vehicle.drivetrain.batteryManagement.maxEnergy", "76", storage()),
            E("sensor", "vehicle.drivetrain.electricEngine.hvsMaxEnergyAbsolute", "78", storage()),
            E("sensor", "vehicle.drivetrain.electricEngine.charging.smeEnergyDeltaFullyCharged", "20", storage()),
            rng("vehicle.drivetrain.electricEngine.kombiRemainingElectricRange"),
        ),
        soc="vehicle.powertrain.electric.battery.stateOfCharge.displayed",
        ceiling="vehicle.powertrain.electric.battery.stateOfCharge.target",
        capacity_kwh=80.0,
    ),
    Case(
        "cardata with only the header and the target",
        (
            soc("vehicle.drivetrain.batteryManagement.header"),
            soc("vehicle.powertrain.electric.battery.stateOfCharge.target", "80"),
            rng("vehicle.drivetrain.electricEngine.kombiRemainingElectricRange"),
        ),
        soc="vehicle.drivetrain.batteryManagement.header",
        ceiling="vehicle.powertrain.electric.battery.stateOfCharge.target",
    ),
    Case(
        "volkswagencarnet: the charge target is tagged as a battery sensor too",
        (
            soc("battery_level"),
            soc("battery_target_charge_level", "80"),
            E("number", "battery_target_charge_level", "80", number(50, 100)),
            E("number", "scan_interval", "30", {"unit_of_measurement": "min", "min": 0, "max": 60}),
            E("sensor", "last_trip_total_electric_consumption", "14000", {"device_class": "energy", "unit_of_measurement": "Wh", "state_class": "total"}),
            rng("electric_range"),
        ),
        soc="battery_level",
        limit="battery_target_charge_level",
    ),
    Case(
        "toyota BEV: the PHEV usable level is a soft demotion",
        (soc("battery_level"), soc("phev_usable_battery_level", "58"), rng("battery_range")),
        soc="battery_level",
    ),
    Case(
        "byd_vehicle",
        (soc("elec_percent", name="Battery level"), soc("power_battery", "95"), rng()),
        soc="elec_percent",
    ),
    Case(
        "polestar_api: target level is a read-only sensor",
        (
            soc("battery_charge_level"),
            E("sensor", "charging_target_level", "80", PERCENT),
            rng("estimated_range"),
        ),
        soc="battery_charge_level",
        ceiling="charging_target_level",
    ),
    Case(
        "mg_saic: target SOC and fuel level are battery class; capacity is declared a meter",
        (
            soc("extendedData1", name="State of Charge"),
            soc("bmsOnBdChrgTrgtSOCDspCmd", "80", name="Target SOC"),
            soc("fuelLevelPrc", "50", name="Fuel Level"),
            E("number", "target_soc", "80", number(40, 100)),
            E("sensor", "totalBatteryCapacity", "64", {"device_class": "energy", "unit_of_measurement": "kWh", "state_class": "total"}, "Total Battery Capacity"),
            E("sensor", "bmsPackEnergy", "40", storage(), "Battery Energy"),
            rng("fuelRangeElec"),
        ),
        soc="extendedData1",
        limit="target_soc",
        capacity_kwh=64.0,
    ),
    Case(
        "rivian: read-only battery_limit sensor next to the number",
        (
            soc("battery_level"),
            E("number", "charge_limit", "80", number(50, 100)),
            E("sensor", "battery_limit", "80", PERCENT),
            E("sensor", "battery_capacity", "135", storage()),
            rng("distance_to_empty"),
        ),
        soc="battery_level",
        limit="charge_limit",
        capacity_kwh=135.0,
    ),
    Case(
        "porscheconnect",
        (soc("state_of_charge"), E("number", "target_soc", "80", number(25, 100)), rng("remaining_range_electric")),
        soc="state_of_charge",
        limit="target_soc",
    ),
    Case(
        "smarthashtag: target SOC sensor is battery class",
        (
            soc("remaining_battery_percent"),
            soc("charging_target_soc", "80"),
            rng("remaining_range"),
        ),
        soc="remaining_battery_percent",
        ceiling="charging_target_soc",
    ),
    Case("subaru", (soc("ev_battery_level"), rng("ev_range")), soc="ev_battery_level"),
    Case("nissan_connect", (soc("battery_level"), rng("range_ac_off")), soc="battery_level"),
    Case(
        "cupra_we_connect: range sensor without a distance class is a miss",
        (soc("currentSOC_pct"), E("sensor", "cruisingRangeElectric_km", "300", {"unit_of_measurement": "km"})),
        soc=None,
    ),
    Case(
        "toyota_na: no device classes at all is a miss",
        (E("sensor", "ev_battery_level", "50", PERCENT), E("sensor", "ev_range", "200", {"unit_of_measurement": "mi"})),
        soc=None,
    ),
    Case(
        "leafspy: the phone's battery is not the car's",
        (soc("battery_level", name="Battery level"), soc("phone_battery", "70"), rng()),
        soc="battery_level",
    ),
    Case(
        "smartcar: the 12 V battery is a battery-class percent sensor too, and the energy added is not a capacity",
        (
            soc("battery_level"),
            soc("low_voltage_battery_level", "88"),
            E("number", "charge_limit", "80", number(50, 100)),
            E("sensor", "battery_capacity", "75", storage()),
            E("sensor", "energy_added", "11", storage()),
            rng("range"),
        ),
        soc="battery_level",
        limit="charge_limit",
        capacity_kwh=75.0,
    ),
    Case(
        "pycupra: target and minimum levels are percent sensors without a class, the limit is a number",
        (
            soc("battery_level"),
            E("sensor", "min_charge_level", "20", PERCENT),
            E("sensor", "target_soc", "80", PERCENT),
            E("number", "target_state_of_charge", "80", number(10, 100)),
            E("sensor", "fuel_level", "0", PERCENT),
            rng("electric_range"),
        ),
        soc="battery_level",
        limit="target_state_of_charge",
    ),
    Case(
        "volkswagen_we_connect_id: the charge target is a battery sensor and a number",
        (
            soc("currentSOC_pct"),
            soc("targetSOC_pct", "80"),
            E("number", "target_state_of_charge", "80", number(10, 100)),
            rng("cruisingRangeElectric_km"),
        ),
        soc="currentSOC_pct",
        limit="target_state_of_charge",
    ),
    Case(
        "lynkco: the read-only charge limit is a percent sensor, a service sets it",
        (
            soc("battery_level"),
            E("sensor", "fuel_level", "70", PERCENT),
            E("sensor", "charge_limit", "80", PERCENT),
            rng("electric_range"),
        ),
        soc="battery_level",
        ceiling="charge_limit",
    ),
    Case(
        "zeekr_ev: a number sets the charging limit",
        (soc("battery"), E("number", "charging_limit", "80", number(50, 100)), rng("remaining_range")),
        soc="battery",
        limit="charging_limit",
    ),
    Case(
        "hello_smart: the 12 V and backup batteries and the target mirror have no class",
        (
            soc("battery_level"),
            E("sensor", "battery_12v_level", "90", PERCENT),
            E("sensor", "backup_battery_level", "100", PERCENT),
            E("sensor", "charging_target_soc", "80", PERCENT),
            E("number", "smart_target_soc", "80", number(50, 100)),
            rng("range_remaining"),
        ),
        soc="battery_level",
        limit="smart_target_soc",
    ),
    Case(
        "abrp: health and calibration are percent sensors, the capacity is a static energy_storage",
        (
            soc("soc"),
            E("sensor", "soh", "96", PERCENT),
            E("sensor", "calibration_confidence", "80", PERCENT),
            E("sensor", "battery_capacity", "77", storage()),
            rng("est_battery_range"),
        ),
        soc="soc",
        capacity_kwh=77.0,
    ),
    Case(
        "teslafi: the charge limit number starts at 0",
        (soc("battery_level"), E("number", "charge_limit_soc", "80", number(0, 100)), rng("battery_range")),
        soc="battery_level",
        limit="charge_limit_soc",
    ),
    Case(
        "lucidmotors: capacity_kwhr is the pack, kwhr the energy left in it",
        (
            soc("charge_percent"),
            E("sensor", "battery_health_level", "98", PERCENT),
            E("number", "charge_limit_percent", "80", number(50, 100)),
            E("sensor", "capacity_kwhr", "112", storage()),
            E("sensor", "kwhr", "70", storage()),
            rng("remaining_range"),
        ),
        soc="charge_percent",
        limit="charge_limit_percent",
        capacity_kwh=112.0,
    ),
    Case(
        "skodaconnect: the minimum charge level is a battery sensor",
        (soc("battery_level"), soc("min_charge_level", "20"), rng("electric_range")),
        soc="battery_level",
    ),
    Case(
        "leapmotor: the one-decimal level is a diagnostic battery sensor, the remaining energy is not a capacity",
        (
            soc("battery_percent"),
            soc("battery_percent_precise", "61.5"),
            E("number", "charge_limit_setting", "80", number(1, 100)),
            E("sensor", "available_energy_kwh", "40", storage()),
            rng("remaining_range"),
        ),
        soc="battery_percent",
        limit="charge_limit_setting",
    ),
    Case(
        "nissan_carwings: electric mileage declares itself energy storage and is not the capacity",
        (
            soc("battery_soc", name="Battery SOC"),
            E("sensor", "electric_mileage", "6.2", storage(state_class=None)),
            rng("range_ac_off"),
        ),
        soc="battery_soc",
    ),
    Case(
        "uconnect: the extrapolated level and the 12 V state are not the charge level",
        (
            soc("state_of_charge"),
            soc("extrapolated_soc", "63"),
            E("sensor", "battery_state_of_charge", "good", {}),
            rng("distance_to_empty"),
        ),
        soc="state_of_charge",
    ),
    Case(
        "vag_connect: the electric and the 'primary engine' level look the same, a repair asks",
        (
            soc("battery_soc"),
            soc("primary_engine_soc_pct", "61"),
            soc("aux_battery_energy_pct", "88"),
            rng("electric_range"),
        ),
        soc="ambiguous",
    ),
    Case(
        "wican: the displayed and the raw level of the same cell stay a repair",
        (soc("SOC"), soc("SOC_D", "60"), soc("SOC_MAX", "100"), soc("SOC_MIN", "20"), rng("RANGE")),
        soc="ambiguous",
    ),
    Case(
        "two unmarked candidates stay ambiguous",
        (soc("pack_a"), soc("pack_b", "70"), E("number", "limit_a", "80", number(50, 100)), E("number", "limit_b", "90", number(50, 100)), rng()),
        soc="ambiguous",
        limit="ambiguous",
    ),
)


def _state(case: Case, key: str) -> float:
    return float(next(e.state for e in case.entities if e.key == key))


def _register(hass: HomeAssistant, case: Case) -> tuple[str, dict[str, str]]:
    config_entry = MockConfigEntry(domain=case.domain.split(" ")[0].replace("-", "_"))
    config_entry.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={("catalogue", "car")},
        name="Car",
    )
    ids: dict[str, str] = {}
    for spec in case.entities:
        translation_key = spec.key if re.fullmatch(r"[a-z0-9_]+", spec.key) else None
        entry = er.async_get(hass).async_get_or_create(
            spec.domain,
            "catalogue",
            f"WVWZZZ123_{spec.key}",
            device_id=device.id,
            suggested_object_id=slugify(spec.key),
            translation_key=translation_key,
            original_name=spec.name,
        )
        hass.states.async_set(entry.entity_id, spec.state, spec.attrs)
        ids[spec.key] = entry.entity_id
    return device.id, ids


@pytest.mark.parametrize("case", CASES, ids=[case.domain for case in CASES])
async def test_recorded_registry_shapes_resolve_as_the_catalogue_says(
    hass: HomeAssistant, case: Case
) -> None:
    device_id, ids = _register(hass, case)
    vehicles = {vehicle.id: vehicle for vehicle in discover_vehicles(hass)}
    ambiguous_soc = {c.id for c in discover_ambiguous_vehicles(hass)}
    ambiguous_limit = {c.id for c in discover_ambiguous_charge_limits(hass)}

    if case.soc is None:
        assert device_id not in vehicles and device_id not in ambiguous_soc
        return
    if case.soc == "ambiguous":
        assert device_id in ambiguous_soc and device_id not in vehicles
        assert (case.limit == "ambiguous") == (device_id in ambiguous_limit)
    else:
        assert device_id not in ambiguous_soc
        assert vehicle_soc_entity_id(hass, device_id) == ids[case.soc]
        vehicle = vehicles[device_id]

        limit_entity = vehicle_charge_limit_entity_id(hass, device_id)
        if case.limit == "ambiguous":
            assert limit_entity is None and device_id in ambiguous_limit
        elif case.limit is None:
            assert limit_entity is None and device_id not in ambiguous_limit
        else:
            assert limit_entity == ids[case.limit]
            assert vehicle.target_soc_percent_max == _state(case, case.limit)
        if case.ceiling is not None:
            assert limit_entity is None
            assert vehicle.target_soc_percent_max == _state(case, case.ceiling)
        if case.limit is None and case.ceiling is None:
            assert vehicle.target_soc_percent_max is None

        assert vehicle.battery_capacity_kwh == (
            pytest.approx(case.capacity_kwh) if case.capacity_kwh is not None else None
        )
        assert vehicle.capacity_source == ("detected" if case.capacity_kwh is not None else "unknown")


async def test_a_percent_select_limit_is_written_with_the_highest_option_not_above_the_request(
    hass: HomeAssistant,
) -> None:
    from homeassistant.core import ServiceCall

    from custom_components.spotnav.vehicles.vehicle_charge_limit import async_set_charge_limit

    case = next(c for c in CASES if c.domain.startswith("fordpass"))
    device_id, ids = _register(hass, case)
    calls: list[ServiceCall] = []
    hass.services.async_register("select", "select_option", calls.append)

    await async_set_charge_limit(hass, device_id, 87)

    assert [(c.data["entity_id"], c.data["option"]) for c in calls] == [
        (ids["globalTargetSoc"], "85")
    ]
    with pytest.raises(ValueError):
        await async_set_charge_limit(hass, device_id, 40)


async def test_a_health_sensor_beside_the_charge_level_does_not_make_the_choice_ambiguous(
    hass: HomeAssistant,
) -> None:
    """What the vehicle dialog reads (`soc_choices`) agrees with detection: the health sensor is a
    candidate a person may pick, but the charge level is selected automatically."""
    from custom_components.spotnav.vehicles.vehicle_discovery import soc_choices

    case = CASES[0]
    assert case.domain.startswith("kia_uvo EU CCS2")
    device_id, ids = _register(hass, case)

    (choice,) = [c for c in soc_choices(hass) if c.id == device_id]
    assert choice.selected_entity_id == ids["ev_battery_percentage"]
    assert choice.source == "automatic"
    assert ids["ev_battery_soh_percentage"] in choice.candidate_entity_ids
