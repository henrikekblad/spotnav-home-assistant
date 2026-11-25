"""Solis through `solis_modbus` (Pho3niX90 v4.5.0): read only, found by the key at the end of the unique id.

The registry is shaped like the integration's (`solis_modbus_<serial>_<unique>`, selects `<register>_select`,
switches `<register>_<on value>`); the fixture is `solis` in `tests/fixtures/battery/brands.json`.
"""

from __future__ import annotations

from typing import Any

import pytest

from custom_components.spotnav.battery import commands
from custom_components.spotnav.battery.adapters import SOLIS, adapter_for
from custom_components.spotnav.battery.facts import battery_facts

from tests.test_battery_facts import BRANDS, _entities, _facts

SERIAL = "solis_modbus_2403123456_"


def _without(*entity_ids: str):
    brand = BRANDS["solis"]
    entities = [entity for entity in _entities(brand) if entity.entity_id not in entity_ids]
    battery = next(entity for entity in entities if entity.entity_id == brand["battery"])
    return battery_facts(battery, entities, manufacturer=brand["manufacturer"], power_kw=-1.0)


def test_solis_modbus_is_the_solis_adapter_whatever_the_maker_says() -> None:
    assert adapter_for("solis_modbus", "Solis") is SOLIS
    assert adapter_for("solis_modbus", None) is SOLIS


def test_solis_reads_its_level_ratings_floor_firmware_and_mode() -> None:
    facts = _facts("solis")

    assert facts.brand == "solis" and facts.label == "Solis"
    assert facts.soc_pct == 64.0
    # The BMS' charge and discharge limits times the BMS' voltage: 25 A and 30 A at 400 V.
    assert facts.rated_charge_kw == pytest.approx(10.0) and facts.rated_discharge_kw == pytest.approx(12.0)
    assert facts.estimates == ("rated_from_current",)
    assert facts.sources["rated_charge"] == (
        "sensor.solis_s6_eh3p_battery_charge_current_limitation_bms x sensor.solis_s6_eh3p_battery_voltage_bms"
    )
    # The on-grid overdischarge SoC, not the off-grid one (10) nor the force-charge SoC (10).
    assert facts.reserve_soc_pct == 20.0
    assert facts.firmware == "59" and facts.sources["firmware"] == "sensor.solis_s6_eh3p_hmi_version"
    assert facts.management.state == "free"


def test_solis_size_is_learnt_its_amp_hour_setting_is_not_read() -> None:
    facts = _facts("solis")

    assert facts.capacity_kwh is None and "capacity" not in facts.sources
    assert "capacity" not in SOLIS.facts and "capacity_ah" not in SOLIS.facts


@pytest.mark.parametrize(
    ("entity_id", "state"),
    [
        ("select.solis_s6_eh3p_storage_mode", "Self-Use + TOU"),
        ("select.solis_s6_eh3p_storage_mode", "Self-Use + Reserve/Backup"),
        ("select.solis_s6_eh3p_storage_mode", "Feed-in Priority"),
        ("select.solis_s6_eh3p_storage_mode", "Peak Shaving"),
        ("switch.solis_s6_eh3p_rc_force_battery_charge", "on"),
        ("switch.solis_s6_eh3p_rc_force_battery_discharge", "on"),
    ],
)
def test_solis_outside_plain_self_use_is_not_free(entity_id: str, state: str) -> None:
    facts = _facts("solis", **{entity_id: {"state": state}})

    assert facts.management.state != "free"


def test_solis_in_the_integrations_read_only_mode_has_no_selects_and_its_mode_is_unknown() -> None:
    facts = _without(
        "select.solis_s6_eh3p_storage_mode",
        "switch.solis_s6_eh3p_rc_force_battery_charge",
        "switch.solis_s6_eh3p_rc_force_battery_discharge",
    )

    assert facts.management.state == "unknown"
    assert facts.soc_pct == 64.0


def test_solis_without_its_optional_entities_still_reads_level_and_power() -> None:
    brand = BRANDS["solis"]
    keep = {brand["battery"], "sensor.solis_s6_eh3p_battery_soc"}
    facts = _without(*(row["entity_id"] for row in brand["entities"] if row["entity_id"] not in keep))

    assert facts.brand == "solis" and facts.soc_pct == 64.0 and facts.power_kw == -1.0
    assert facts.rated_charge_kw is None and facts.rated_discharge_kw is None
    assert facts.reserve_soc_pct is None and facts.capacity_kwh is None


def test_solis_ratings_need_the_bms_voltage() -> None:
    facts = _without("sensor.solis_s6_eh3p_battery_voltage_bms")

    assert facts.rated_charge_kw is None and facts.rated_discharge_kw is None


def test_solis_floor_is_read_from_the_sensor_when_numbers_are_left_out() -> None:
    facts = _without("number.solis_s6_eh3p_overdischarge_soc")

    assert facts.reserve_soc_pct == 20.0


def _counter(key: str, state: str) -> dict[str, Any]:
    return {
        "entity_id": f"sensor.solis_s6_eh3p_{key}", "unique_id": SERIAL + f"solis_modbus_inverter_{key}",
        "device": "inverter", "state": state, "unit": "kWh", "device_class": "energy", "disabled": False,
    }


def test_solis_reads_the_battery_lifetime_counters() -> None:
    brand = BRANDS["solis"]
    rows = [_counter("total_battery_charge_energy", "1520"), _counter("total_battery_discharge_energy", "1390"),
            _counter("today_battery_charge_energy", "12.5")]
    entities = _entities(brand) + _entities({"platform": brand["platform"], "entities": rows})
    battery = next(entity for entity in entities if entity.entity_id == brand["battery"])

    facts = battery_facts(battery, entities, manufacturer=brand["manufacturer"], power_kw=-1.0)

    assert facts.charged_total_kwh == pytest.approx(1520.0) and facts.discharged_total_kwh == pytest.approx(1390.0)
    assert facts.sources["charged_total"] == "sensor.solis_s6_eh3p_total_battery_charge_energy"


def test_a_renamed_solis_entity_is_still_found_by_its_unique_id() -> None:
    facts = _facts("solis", **{"sensor.solis_s6_eh3p_battery_soc": {"entity_id": "sensor.hus_batteri_procent"}})

    assert facts.soc_pct == 64.0 and facts.sources["soc"] == "sensor.hus_batteri_procent"


def test_solis_is_controlled_only_by_its_rc_force_recipe() -> None:
    # The adapter itself writes nothing (no hold or grid charge spec); the control is the RC force recipe.
    assert SOLIS.hold is None and SOLIS.grid_charge is None
    assert commands.control_for("solis") is commands.SOLIS_CONTROL
    # Its export limit is a control of its own, with none of the battery recipe's roles.
    export = commands.export_control_for("solis")
    assert export is commands.SOLIS_EXPORT_CONTROL
    assert {role.name for role in export.roles}.isdisjoint(role.name for role in commands.SOLIS_CONTROL.roles)
    facts = _facts("solis")
    assert facts.hold is None and facts.grid_charge is None


def test_solis_ratings_are_capped_by_the_inverter_rating_the_integration_states() -> None:
    """A cold Dyness lets 100 A at 367 V (37 kW) beside a 15 kW inverter: the inverter's rated output, the advisory
    `device_limit` (W) of the battery discharge limit power number, is the plant's maximum."""
    from custom_components.spotnav.battery.facts import EntityFact

    brand = BRANDS["solis"]
    entities = _entities(brand, **{
        "sensor.solis_s6_eh3p_battery_discharge_current_limitation_bms": {"state": "100"},
        "sensor.solis_s6_eh3p_battery_voltage_bms": {"state": "366.8"},
    })
    battery = next(entity for entity in entities if entity.entity_id == brand["battery"])
    number = EntityFact(
        entity_id="number.solis_s6_eh3p_battery_discharge_limit_power", platform=brand["platform"],
        unique_id=f"{SERIAL}solis_modbus_inverter_battery_discharge_limit_power", device_id="inverter",
        config_entry_id="entry", state="1500", unit="W",
        attributes={"device_limit": 15000, "device_limit_source": "inverter_rating"},
    )
    uncapped = battery_facts(battery, entities, manufacturer=brand["manufacturer"], power_kw=-1.0)
    capped = battery_facts(battery, [*entities, number], manufacturer=brand["manufacturer"], power_kw=-1.0)

    assert uncapped.rated_discharge_kw == pytest.approx(36.68) and uncapped.plant_max_kw is None
    assert capped.plant_max_kw == pytest.approx(15.0)
    assert capped.sources["plant_max"] == "number.solis_s6_eh3p_battery_discharge_limit_power"
    assert capped.max_discharge_kw == pytest.approx(15.0)
    # Without the attribute (an older integration, "essential sensors only") nothing caps it.
    from dataclasses import replace

    bare = replace(number, attributes={})
    assert battery_facts(battery, [*entities, bare], manufacturer=brand["manufacturer"], power_kw=-1.0).plant_max_kw is None
