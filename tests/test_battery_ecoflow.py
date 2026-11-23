"""EcoFlow PowerOcean through `ef_powerocean_tcpmodbus` (MaxGrmm `56331de`) or `ecoflow_energy` (shuette42 `771b646`):
read only, found by the key in the unique id.

MaxGrmm's registry (`<config entry id>_<key>`) is the fixture `ecoflow` in `tests/fixtures/battery/brands.json`;
shuette42's (`<device sn>_<key>`) is built here.
"""

from __future__ import annotations

import pytest

from custom_components.spotnav.battery import commands
from custom_components.spotnav.battery.adapters import ECOFLOW, adapter_for
from custom_components.spotnav.battery.coexist import coexistence_for
from custom_components.spotnav.battery.facts import EntityFact, battery_facts

from tests.test_battery_facts import _facts
from tests.test_site_detection import Registry

SN = "HJ31ZDH4ABC123"


def _fact(unique_id: str, state: str, unit: str | None = None, domain: str = "sensor", disabled: bool = False,
          platform: str = "ecoflow_energy") -> EntityFact:
    return EntityFact(
        entity_id=f"{domain}.{unique_id.lower()}", platform=platform, unique_id=unique_id, device_id="inverter",
        config_entry_id="entry", disabled=disabled, state=state, unit=unit,
    )


def _energy(extra: list[EntityFact] | None = None):
    entities = [
        _fact(f"{SN}_batt_w", "-800", "W"),
        _fact(f"{SN}_soc_pct", "58", "%"),
        _fact(f"{SN}_batt_charge_energy_kwh", "300.5", "kWh"),
        _fact(f"{SN}_batt_discharge_energy_kwh", "280", "kWh"),
        # Not the system's: a STREAM unit's own level and pack power, and a second inverter's.
        _fact(f"{SN}_unit_soc_pct", "11", "%"),
        _fact(f"{SN}_unit_batt_w", "5", "W"),
        _fact(f"{SN}_inverter_2_soc_pct", "22", "%"),
        _fact(f"{SN}_inverter_2_batt_w", "6", "W"),
        _fact(f"{SN}_max_charge_soc_pct", "90", "%"),
        _fact(f"{SN}_unit_batt_charge_energy_kwh", "1", "kWh"),
        *(extra or []),
    ]
    return entities, battery_facts(entities[0], entities, manufacturer="EcoFlow", power_kw=-0.8)


def test_both_integrations_are_the_ecoflow_adapter_and_it_is_read_only() -> None:
    assert adapter_for("ef_powerocean_tcpmodbus", "EcoFlow") is ECOFLOW
    assert adapter_for("ecoflow_energy", "EcoFlow") is ECOFLOW
    # The cloud integration is a different adapter-less one (its power rows only).
    assert adapter_for("ecoflow_cloud", "EcoFlow") is None
    assert all(control.brand != "ecoflow" for control in commands.CONTROLS)
    assert ECOFLOW.hold is None and ECOFLOW.grid_charge is None


def test_modbus_reads_level_size_counters_and_the_apps_reserve() -> None:
    facts = _facts("ecoflow")

    assert facts.brand == "ecoflow"
    assert facts.soc_pct == 62.0
    assert facts.capacity_kwh == pytest.approx(10.24)
    assert (facts.charged_total_kwh, facts.discharged_total_kwh) == (812.4, 760.1)
    assert facts.reserve_soc_pct == 10.0
    assert facts.sources["capacity"] == "sensor.ecoflow_powerocean_battery_capacity"
    # The BMS's momentary charge limit is no rating.
    assert facts.rated_charge_kw is None and facts.rated_discharge_kw is None
    assert facts.management.state == "free"


@pytest.mark.parametrize("state, managed", [("self_consumption", False), ("ai", True), ("standby", True)])
def test_modbus_is_free_only_in_self_consumption(state: str, managed: bool) -> None:
    facts = _facts("ecoflow", **{"sensor.ecoflow_powerocean_operating_mode": {"state": state}})

    assert (facts.management.state == "self_managed") is managed


def test_modbus_control_held_by_another_client_is_not_free() -> None:
    facts = _facts("ecoflow", **{"binary_sensor.ecoflow_powerocean_modbus_control": {"state": "on"}})

    assert facts.management.state == "self_managed"
    assert facts.management.reason == "Modbus control:on"


def test_a_zero_level_that_the_integration_marks_unavailable_is_no_reading() -> None:
    facts = _facts("ecoflow", **{"sensor.ecoflow_powerocean_battery_soc": {"state": "unavailable"}})

    assert facts.soc_pct is None


def test_ecoflow_energy_reads_the_systems_level_and_counters_not_a_units() -> None:
    _entities, facts = _energy()

    assert facts.brand == "ecoflow"
    assert facts.soc_pct == 58.0
    assert facts.sources["soc"] == f"sensor.{SN.lower()}_soc_pct"
    assert (facts.charged_total_kwh, facts.discharged_total_kwh) == (300.5, 280.0)
    # No size is an entity there (`batt_remaining_wh` is the energy left).
    entities, _ = _energy([_fact(f"{SN}_batt_remaining_wh", "5000", "Wh")])
    assert battery_facts(entities[0], entities, manufacturer="EcoFlow").capacity_kwh is None


def test_ecoflow_energy_without_a_work_mode_cannot_tell_who_runs_it() -> None:
    _entities, facts = _energy()

    assert facts.management.state == "unknown"


@pytest.mark.parametrize(
    "state, expected", [("self_use", "free"), ("self_powered", "free"), ("ai_schedule", "self_managed"),
                        ("intelligent_plus", "self_managed")],
)
def test_ecoflow_energy_work_mode(state: str, expected: str) -> None:
    entities, _ = _energy([_fact(f"{SN}_work_mode", state, domain="select")])

    facts = battery_facts(entities[0], entities, manufacturer="EcoFlow", power_kw=-0.8)

    assert facts.management.state == expected


def test_ecoflow_energy_modbus_control_switch_on_is_not_free() -> None:
    entities, _ = _energy([
        _fact(f"{SN}_work_mode", "self_use", domain="select"),
        _fact(f"{SN}_modbus_control", "on", domain="switch"),
    ])

    facts = battery_facts(entities[0], entities, manufacturer="EcoFlow", power_kw=-0.8)

    assert facts.management.state == "self_managed"


# ---- the power, charge-positive on both ------------------------------------------------------------------------


def test_the_battery_power_is_read_as_it_is_on_both_integrations() -> None:
    r = Registry()
    r.device("pm", model="PowerOcean", manufacturer="EcoFlow", entry="mg")
    r.add("ef_powerocean_tcpmodbus", "powerocean_battery_power", "01JMG_battery_power", device_class="power", unit="W", device="pm", entry="mg")
    r.add("ef_powerocean_tcpmodbus", "powerocean_battery_power_setpoint", "01JMG_battery_power_setpoint", device_class="power", unit="W", device="pm", entry="mg")
    [mg] = r.detect().batteries
    assert mg.entity_id == "sensor.powerocean_battery_power" and mg.inverted is False

    r = Registry()
    r.device("se", model="PowerOcean", manufacturer="EcoFlow", entry="se")
    for key in ("batt_w", "unit_batt_w", "inverter_2_batt_w", "home_from_batt_w"):
        r.add("ecoflow_energy", f"ocean_{key}", f"{SN}_{key}", device_class="power", unit="W", device="se", entry="se")
    [se] = r.detect().batteries
    assert se.entity_id == "sensor.ocean_batt_w" and se.inverted is False


def test_the_coexistence_row_names_the_apps_own_modes_and_no_aggregator() -> None:
    row = coexistence_for(platform="ecoflow_energy")

    assert row is not None and row.brand == "ecoflow" and row.aggregators == ()
    assert coexistence_for(platform="ef_powerocean_tcpmodbus") is row
