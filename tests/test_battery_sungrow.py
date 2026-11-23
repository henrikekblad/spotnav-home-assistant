"""Sungrow SH hybrids through mkaiser's YAML Modbus package (`e2adb62`): read only, found by the package's fixed unique ids.

The entities are `modbus` and `template` ones with no config entry and no device, so the adapter is chosen by the
battery entity's unique id and the inverter by its `_inv_<n>` suffix. The registry is the fixture `sungrow` in
`tests/fixtures/battery/brands.json`.
"""

from __future__ import annotations

import copy

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.spotnav.battery import commands
from custom_components.spotnav.battery.adapters import SUNGROW, adapter_for
from custom_components.spotnav.battery.coexist import coexistence_for
from custom_components.spotnav.battery.facts import EntityFact, battery_facts
from custom_components.spotnav.battery.runtime import read_battery_facts
from custom_components.spotnav.const import CONF_BATTERY_AGGREGATE_POWER_ENTITY

from tests.test_battery_facts import BRANDS, _entities, _facts


def _suffixed(suffix: str, **states: str) -> list[EntityFact]:
    """The fixture's package as a multi-inverter file writes it: every unique id and entity id carries `suffix`."""
    brand = copy.deepcopy(BRANDS["sungrow"])
    for row in brand["entities"]:
        row["unique_id"] += suffix
        row["entity_id"] += suffix
        row["state"] = states.get(row["entity_id"], row["state"])
    return _entities(brand)


def _read(entities: list[EntityFact], battery_id: str):
    battery = next(entity for entity in entities if entity.entity_id == battery_id)
    return battery_facts(battery, entities, manufacturer=None, power_kw=-1.0)


def test_sungrow_is_read_only_and_chosen_by_the_packages_unique_id() -> None:
    assert adapter_for("modbus", None, "sg_battery_power") is SUNGROW
    assert adapter_for("modbus", None, "sg_battery_power_inv_2") is SUNGROW
    assert adapter_for("modbus", None, "uid_ems_mode") is SUNGROW
    assert adapter_for("modbus") is None
    assert adapter_for("modbus", None, "garage_battery_power") is None
    assert adapter_for("sungrow", "Sungrow", "123_battery_power") is None
    assert all(control.brand != "sungrow" for control in commands.CONTROLS)
    assert SUNGROW.hold is None and SUNGROW.grid_charge is None


def test_it_reads_level_size_rating_counters_and_firmware() -> None:
    facts = _facts("sungrow")

    assert facts.brand == "sungrow" and facts.integration == "modbus"
    assert facts.soc_pct == 63.5
    assert facts.capacity_kwh == pytest.approx(9.6)
    # One figure, the battery converter's rating, for both directions.
    assert facts.rated_charge_kw == 5.0 and facts.rated_discharge_kw == 5.0
    assert facts.charged_total_kwh == pytest.approx(812.4)
    assert facts.discharged_total_kwh == pytest.approx(741.0)
    assert facts.firmware == "SAPPHIRE-H_P_V11 V01 B 95.14"
    assert facts.sources["soc"] == "sensor.battery_level"
    assert facts.sources["firmware"] == "sensor.inverter_firmware_version"


def test_no_floor_and_no_plant_limits_are_read() -> None:
    facts = _facts("sungrow")

    assert facts.reserve_soc_pct is None and facts.plant_max_kw is None and facts.on_grid is None


@pytest.mark.parametrize(
    "ems, command, expected",
    [
        ("Self-consumption mode (default)", "Stop (default)", "free"),
        ("Forced mode", "Stop (default)", "self_managed"),
        ("External EMS", "Stop (default)", "self_managed"),
        ("VPP", "Stop (default)", "self_managed"),
        ("Self-consumption mode (default)", "Forced charge", "self_managed"),
        ("Self-consumption mode (default)", "Forced discharge", "self_managed"),
        ("unavailable", "Stop (default)", "unknown"),
    ],
)
def test_the_battery_is_free_only_in_self_consumption_with_no_forced_command(ems: str, command: str, expected: str) -> None:
    facts = _facts(
        "sungrow",
        **{"select.ems_mode": {"state": ems}, "select.battery_forced_charge_discharge": {"state": command}},
    )

    assert facts.management.state == expected


def test_a_package_without_its_selects_leaves_the_mode_unknown() -> None:
    brand = copy.deepcopy(BRANDS["sungrow"])
    brand["entities"] = [row for row in brand["entities"] if not row["entity_id"].startswith("select.")]
    entities = _entities(brand)

    facts = battery_facts(entities[0], entities, manufacturer=None, power_kw=-1.0)

    assert facts.management.state == "unknown"


def test_an_unrelated_modbus_entity_is_never_picked() -> None:
    brand = copy.deepcopy(BRANDS["sungrow"])
    # A garage battery read over plain Modbus by someone else, beside the package; its ids have no `sg_` prefix.
    brand["entities"] = [
        row for row in brand["entities"] if row["entity_id"] != "sensor.battery_level"
    ] + [
        {"entity_id": "sensor.garage_battery_level", "unique_id": "garage_battery_level", "platform": "modbus", "device": None,
         "state": "12", "unit": "%", "device_class": "battery", "disabled": False},
        {"entity_id": "sensor.garage_battery_capacity", "unique_id": "uid_garage_battery_capacity", "platform": "template",
         "device": None, "state": "3", "unit": "kWh", "device_class": "energy_storage", "disabled": False},
    ]
    entities = _entities(brand)

    facts = battery_facts(entities[0], entities, manufacturer=None, power_kw=-1.0)

    # The package's own level is gone, so there is none: the garage battery's 12 % is not taken for it.
    assert facts.soc_pct is None
    assert facts.capacity_kwh == pytest.approx(9.6)


def test_a_modbus_battery_that_is_not_the_package_gets_no_sungrow_facts() -> None:
    brand = copy.deepcopy(BRANDS["sungrow"])
    for row in brand["entities"]:
        row["unique_id"] = "other_" + row["unique_id"]
    entities = _entities(brand)

    facts = battery_facts(entities[0], entities, manufacturer=None, power_kw=-1.0)

    assert facts.brand is None and facts.management.reason == "no_adapter"


def test_two_inverters_are_read_each_from_its_own_suffix() -> None:
    first = _suffixed("_inv_1", **{"sensor.battery_level_inv_1": "40", "sensor.bdc_rated_power_inv_1": "6000"})
    second = _suffixed("_inv_2", **{"sensor.battery_level_inv_2": "80", "select.ems_mode_inv_2": "Forced mode"})
    both = first + second

    one = _read(both, "sensor.battery_power_inv_1")
    two = _read(both, "sensor.battery_power_inv_2")

    assert one.soc_pct == 40.0 and one.rated_charge_kw == 6.0 and one.management.state == "free"
    assert two.soc_pct == 80.0 and two.rated_charge_kw == 5.0 and two.management.state == "self_managed"
    assert one.sources["soc"] == "sensor.battery_level_inv_1" and two.sources["soc"] == "sensor.battery_level_inv_2"


def test_the_single_inverter_set_is_not_mixed_with_a_second_inverters() -> None:
    plain = _entities(BRANDS["sungrow"])
    second = _suffixed("_inv_2", **{"sensor.battery_level_inv_2": "80"})

    facts = _read(plain + second, "sensor.battery_power")

    assert facts.soc_pct == 63.5 and facts.sources["soc"] == "sensor.battery_level"


def test_the_sites_battery_row_takes_the_multi_inverter_suffix() -> None:
    from tests.test_site_detection import Registry

    r = Registry()
    for suffix in ("_inv_1", "_inv_2"):
        r.add("modbus", f"battery_power{suffix}", f"sg_battery_power{suffix}", device_class="power", unit="W", device=None, entry="")

    batteries = r.detect().batteries

    assert len(batteries) == 2 and all(battery.inverted for battery in batteries)
    assert len({battery.candidate_id for battery in batteries}) == 2


def test_the_coexistence_row_is_found_by_brand_and_no_modbus_device_is_named_after_it() -> None:
    row = coexistence_for(brand="sungrow")

    assert row is not None and row.own_optimiser == "iSolarCloud"
    assert coexistence_for(platform="modbus") is None


async def test_the_registry_read_finds_the_package_without_a_config_entry(hass: HomeAssistant) -> None:
    registry = er.async_get(hass)
    for brand_rows, suffix, level in ((BRANDS["sungrow"]["entities"], "", "63.5"), (BRANDS["sungrow"]["entities"], "_inv_2", "80")):
        for row in brand_rows:
            domain, object_id = row["entity_id"].split(".", 1)
            entry = registry.async_get_or_create(
                domain, row.get("platform", "modbus"), row["unique_id"] + suffix, suggested_object_id=object_id + suffix
            )
            state = level if object_id == "battery_level" else row["state"]
            attributes = {"unit_of_measurement": row["unit"]} if row["unit"] else {}
            hass.states.async_set(entry.entity_id, state, attributes)
    # Another Modbus device's entity beside them.
    other = registry.async_get_or_create("sensor", "modbus", "pool_pump_power", suggested_object_id="pool_pump_power")
    hass.states.async_set(other.entity_id, "300", {"unit_of_measurement": "W"})

    facts = read_battery_facts(hass, {CONF_BATTERY_AGGREGATE_POWER_ENTITY: "sensor.battery_power_inv_2"}, -1.0)

    assert facts.brand == "sungrow" and facts.soc_pct == 80.0
    assert facts.capacity_kwh == pytest.approx(9.6)
    assert facts.management.state == "free"
    assert facts.sources["soc"] == "sensor.battery_level_inv_2"
