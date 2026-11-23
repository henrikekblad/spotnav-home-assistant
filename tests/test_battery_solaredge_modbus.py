"""Core `solaredge_modbus` (home-assistant/core `d33dfe3a`, solaredged 0.5.0) and binsentsu's HACS `solaredge_modbus`
(`e6734e47`, 2.3.0) share a domain and are read for the facts only, as the brand `solaredge_modbus`.

The core registry is the fixture `solaredge_modbus` in `tests/fixtures/battery/brands.json`; binsentsu's is built here
(`<hub name>_battery<n>_<key>`, `hub.name` the person's own).
"""

from __future__ import annotations

import pytest

from custom_components.spotnav.battery import commands
from custom_components.spotnav.battery.adapters import SOLAREDGE, SOLAREDGE_MODBUS, adapter_for
from custom_components.spotnav.battery.coexist import coexistence_for
from custom_components.spotnav.battery.facts import EntityFact, battery_facts

from tests.test_battery_facts import _facts


def _fact(entity_id: str, unique_id: str, state: str, unit: str | None, device_class: str | None) -> EntityFact:
    return EntityFact(
        entity_id=entity_id, platform="solaredge_modbus", unique_id=unique_id, device_id="battery",
        config_entry_id="entry", disabled=False, device_class=device_class, state=state, unit=unit,
    )


def test_the_shared_domain_is_its_own_read_only_brand() -> None:
    assert adapter_for("solaredge_modbus", "SolarEdge") is SOLAREDGE_MODBUS
    assert adapter_for("solaredge_modbus_multi", "SolarEdge") is SOLAREDGE
    # No control recipe: the Multi's is written against the Multi's entities.
    assert all(control.brand != "solaredge_modbus" for control in commands.CONTROLS)
    assert commands.control_for("solaredge_modbus") is None
    assert SOLAREDGE_MODBUS.hold is None and SOLAREDGE_MODBUS.grid_charge is None


def test_core_reads_level_size_rating_and_counters() -> None:
    facts = _facts("solaredge_modbus")

    assert facts.brand == "solaredge_modbus"
    assert facts.soc_pct == 55.0
    assert facts.capacity_kwh == pytest.approx(9.7)
    assert (facts.rated_charge_kw, facts.rated_discharge_kw) == (5.0, 5.0)
    assert facts.sources["capacity"] == "sensor.battery_1_usable_capacity"


def test_core_counters_are_read_in_kwh_into_and_out_of_the_battery() -> None:
    entities = [
        _fact("sensor.dc", "SERIAL_battery_B1_dc_power", "-800", "W", "power"),
        _fact("sensor.in", "SERIAL_battery_B1_energy_imported", "500000", "Wh", "energy"),
        _fact("sensor.out", "SERIAL_battery_B1_energy_exported", "450000", "Wh", "energy"),
    ]

    facts = battery_facts(entities[0], entities, manufacturer="SolarEdge", power_kw=-1.0)

    assert (facts.charged_total_kwh, facts.discharged_total_kwh) == (500.0, 450.0)


def test_binsentsu_reads_level_size_and_counters_but_states_no_rating() -> None:
    entities = [
        _fact("sensor.se_battery1_power", "SolarEdge_battery1_power", "-800", "W", "power"),
        _fact("sensor.se_battery1_state_of_charge", "SolarEdge_battery1_state_of_charge", "61", "%", "battery"),
        _fact("sensor.se_battery1_size_max", "SolarEdge_battery1_size_max", "9700", "Wh", "energy_storage"),
        _fact("sensor.se_battery1_size_available", "SolarEdge_battery1_size_available", "5000", "Wh", "energy_storage"),
        _fact("sensor.se_battery1_energy_charged", "SolarEdge_battery1_energy_charged", "1200.5", "kWh", "energy"),
        _fact("sensor.se_battery1_energy_discharged", "SolarEdge_battery1_energy_discharged", "1100", "kWh", "energy"),
    ]

    facts = battery_facts(entities[0], entities, manufacturer="SolarEdge", power_kw=-1.0)

    assert facts.brand == "solaredge_modbus"
    assert facts.soc_pct == 61.0
    assert facts.capacity_kwh == pytest.approx(9.7)
    assert facts.sources["capacity"] == "sensor.se_battery1_size_max"
    assert (facts.charged_total_kwh, facts.discharged_total_kwh) == (1200.5, 1100.0)
    assert facts.rated_charge_kw is None and facts.rated_discharge_kw is None


def test_the_health_and_available_energy_are_not_the_level_or_the_size() -> None:
    entities = [
        _fact("sensor.dc", "SERIAL_battery_B1_dc_power", "-800", "W", "power"),
        _fact("sensor.soh", "SERIAL_battery_B1_state_of_health", "98", "%", None),
        _fact("sensor.avail", "SERIAL_battery_B1_energy_available", "4000", "Wh", "energy_storage"),
        _fact("sensor.rated", "SERIAL_battery_B1_rated_energy", "10000", "Wh", "energy_storage"),
    ]

    facts = battery_facts(entities[0], entities, manufacturer="SolarEdge", power_kw=-1.0)

    assert facts.soc_pct is None and facts.capacity_kwh is None


def test_the_coexistence_row_names_the_shared_domain_as_solaredge() -> None:
    row = coexistence_for(brand="solaredge_modbus", platform="solaredge_modbus")
    assert row is not None and row.brand == "solaredge"
