"""Growatt through 0xAHA's `growatt_modbus` (`29d6b2b`, v2.0.7): read only, found by the key after the config entry id
(`<entry id>_<key>`). The `solax_modbus` Growatt plugin's adapter (`GROWATT`) is a different one and is unchanged.

The registry is the fixture `growatt_modbus` in `tests/fixtures/battery/brands.json`.
"""

from __future__ import annotations

import pytest

from custom_components.spotnav.battery import commands
from custom_components.spotnav.battery.adapters import GROWATT, GROWATT_MODBUS, adapter_for
from custom_components.spotnav.battery.coexist import coexistence_for
from custom_components.spotnav.battery.facts import EntityFact, battery_facts

from tests.test_battery_facts import BRANDS, _entities, _facts
from tests.test_site_detection import Registry

ENTRY = "01jgrowattmodbus0000000000"


def test_the_two_growatt_adapters_are_told_apart_by_their_integration() -> None:
    assert adapter_for("growatt_modbus", "Growatt") is GROWATT_MODBUS
    assert adapter_for("solax_modbus", "Growatt New Energy") is GROWATT
    assert all(control.brand != "growatt_modbus" for control in commands.CONTROLS)
    assert GROWATT_MODBUS.hold is None and GROWATT_MODBUS.grid_charge is None


def test_it_reads_level_counters_the_floor_and_a_size_worked_out_from_ah_and_volts() -> None:
    facts = _facts("growatt_modbus")

    assert facts.brand == "growatt_modbus"
    assert facts.soc_pct == 64.0
    # 200 Ah x 51.2 V: an estimate, and it says so. The second cluster is not added.
    assert facts.capacity_kwh == pytest.approx(10.24)
    assert facts.estimates == ("capacity_from_ah",)
    assert facts.sources["capacity"].endswith("x sensor.growatt_battery_voltage")
    assert (facts.charged_total_kwh, facts.discharged_total_kwh) == (5200.5, 4800.2)
    assert facts.rated_charge_kw is None and facts.rated_discharge_kw is None


def test_the_grid_charged_energy_and_the_days_energy_are_not_the_counters() -> None:
    facts = _facts("growatt_modbus")

    assert facts.sources["charged_total"] == "sensor.growatt_battery_charge_total"


def test_the_floor_is_the_working_register_and_never_the_one_a_mod_ignores() -> None:
    facts = _facts("growatt_modbus")

    assert facts.reserve_soc_pct == 15.0
    assert "number.growatt_discharge_stopped_soc" in facts.sources["reserve"]
    # `discharge_stopped_soc` (1071) reads 90 here and is not a floor.
    assert "number.growatt_grid_first_discharge_stopped_soc_old" not in facts.sources["reserve"]
    mod = _facts("growatt_modbus", **{"number.growatt_discharge_stopped_soc": {"state": "30"}})
    assert mod.reserve_soc_pct == 30.0


@pytest.mark.parametrize("vpp, priority, expected", [
    ("released", "Load First", "free"),
    ("charge", "Load First", "self_managed"),
    ("authority_only", "Load First", "self_managed"),
    ("released", "Battery First", "self_managed"),
    ("unavailable", "Load First", "unknown"),
])
def test_remote_control_or_another_priority_is_not_the_free_state(vpp: str, priority: str, expected: str) -> None:
    facts = _facts(
        "growatt_modbus",
        **{"sensor.growatt_vpp_control_state": {"state": vpp}, "sensor.growatt_priority_mode": {"state": priority}},
    )

    assert facts.management.state == expected


def test_a_plant_without_a_mode_entity_is_free() -> None:
    brand = BRANDS["growatt_modbus"]
    entities = [item for item in _entities(brand) if not item.entity_id.endswith(("vpp_control_state", "priority_mode"))]
    battery = next(item for item in entities if item.entity_id == brand["battery"])

    assert battery_facts(battery, entities, manufacturer="Growatt").management.state == "free"


def test_the_other_repositorys_entities_of_the_same_domain_are_never_picked() -> None:
    other = [
        EntityFact("sensor.g_battery_power", "growatt_modbus", "growatt_modbus_SN123_battery_power", device_id="d", state="500", unit="W", config_entry_id="e"),
        EntityFact("sensor.g_battery_soc", "growatt_modbus", "growatt_modbus_SN123_battery_soc", device_id="d", state="71", unit="%", config_entry_id="e"),
    ]

    facts = battery_facts(other[0], other, manufacturer="Growatt")

    assert facts.soc_pct is None


def test_the_battery_power_is_charge_positive_as_it_is_and_the_other_repositorys_is_not_detected() -> None:
    r = Registry()
    r.device("bat", model="MOD 10KTL3-XH", manufacturer="Growatt", entry="gm")
    for key in ("battery_power", "battery_charge_power", "battery2_power"):
        r.add("growatt_modbus", f"bat_{key}", f"{ENTRY}_{key}", device_class="power", unit="W", device="bat", entry="gm")
    [battery] = r.detect().batteries
    assert battery.entity_id == "sensor.bat_battery_power"
    assert battery.inverted is False and battery.discharge_entity_id is None

    other = Registry()
    other.device("bat", model="SPH", manufacturer="Growatt", entry="gx")
    other.add("growatt_modbus", "bat_battery_power", "growatt_modbus_SN123_battery_power", device_class="power", unit="W", device="bat", entry="gx")
    assert other.detect().batteries == ()


def test_the_coexistence_row_is_growatts_and_names_no_aggregator() -> None:
    row = coexistence_for(platform="growatt_modbus")

    assert row is not None and row.brand == "growatt" and row.aggregators == ()
