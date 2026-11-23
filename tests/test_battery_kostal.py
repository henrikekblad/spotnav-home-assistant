"""Kostal Plenticore through core `kostal_plenticore` (`42f4a32`) or HACS `kostal_modbus_control` (mbuhansen `3bd84ac`,
4.3.0): read only, found by the key in the unique id.

Core's registry (`<entry id>_<module id>_<data id>`) is the fixture `kostal` in `tests/fixtures/battery/brands.json`;
the Modbus integration's (`<config entry id>_<key>`) is built here.
"""

from __future__ import annotations

import pytest

from custom_components.spotnav.battery import commands
from custom_components.spotnav.battery.adapters import KOSTAL, adapter_for
from custom_components.spotnav.battery.coexist import coexistence_for, outside_integrations
from custom_components.spotnav.battery.facts import EntityFact, battery_facts

from tests.test_battery_facts import _facts
from tests.test_site_detection import Registry

ENTRY = "01JKOSTALMODBUS0000000000"
MODBUS_NONE = "No external battery management"


def _fact(key: str, state: str, unit: str | None = None, domain: str = "sensor", disabled: bool = False) -> EntityFact:
    return EntityFact(
        entity_id=f"{domain}.{key}", platform="kostal_modbus_control", unique_id=f"{ENTRY}_{key}", device_id="inverter",
        config_entry_id="entry", disabled=disabled, state=state, unit=unit,
    )


def _modbus(*extra: EntityFact):
    entities = [_fact("battery_power", "800", "W"), _fact("battery_soc", "66", "%"), *extra]
    return battery_facts(entities[0], entities, power_kw=-0.8)


def test_both_integrations_are_the_kostal_adapter_and_it_is_read_only() -> None:
    assert adapter_for("kostal_plenticore", "Kostal") is KOSTAL
    assert adapter_for("kostal_modbus_control", None) is KOSTAL
    assert all(control.brand != "kostal" for control in commands.CONTROLS)
    assert KOSTAL.hold is None and KOSTAL.grid_charge is None


def test_core_reads_the_level_and_the_inverters_floor_but_no_size_rating_or_counters() -> None:
    facts = _facts("kostal")

    assert facts.brand == "kostal"
    assert facts.soc_pct == 47.0
    assert facts.sources["soc"] == "sensor.scb_battery_soc"
    assert facts.reserve_soc_pct == 15.0
    assert facts.capacity_kwh is None and facts.rated_charge_kw is None and facts.rated_discharge_kw is None
    # Two charge counters beside one discharge counter: no pair, no round trip.
    assert facts.charged_total_kwh is None and facts.discharged_total_kwh is None


def test_core_cannot_tell_who_runs_it_because_the_select_reads_none_when_the_inverters_own_control_is_off() -> None:
    assert _facts("kostal").management.state == "unknown"
    smart = _facts("kostal", **{"select.scb_battery_charging_usage_mode": {"state": "Battery:SmartBatteryControl:Enable"}})
    assert smart.management.state == "self_managed"


def test_core_does_not_read_the_modules_other_p_or_soc_as_the_battery() -> None:
    entities = [
        EntityFact("sensor.p", "kostal_plenticore", f"{ENTRY}_devices:local_HomeBat_P", state="300", unit="W"),
        EntityFact("sensor.pv", "kostal_plenticore", f"{ENTRY}_devices:local:pv1_P", state="500", unit="W"),
    ]

    facts = battery_facts(entities[0], entities, power_kw=0.3)

    assert facts.soc_pct is None


def test_modbus_reads_level_and_size_but_not_the_limits_the_integration_writes() -> None:
    facts = _modbus(
        _fact("battery_work_capacity", "11520", "Wh"),
        _fact("battery_max_charge_power_limit", "0", "W"),
        _fact("battery_max_discharge_power_limit", "0", "W"),
        _fact("software_version", "3.07.03.27599"),
    )

    assert facts.soc_pct == 66.0
    assert facts.capacity_kwh == pytest.approx(11.52)
    # Registers 1038 and 1040 are what the integration writes (0 to block), not the battery's rating.
    assert facts.rated_charge_kw is None and facts.rated_discharge_kw is None
    assert facts.firmware == "3.07.03.27599"


@pytest.mark.parametrize(
    "state, expected",
    [(MODBUS_NONE, "free"), ("External battery management via Modbus protocol", "self_managed"),
     ("External battery management via digital I/O", "self_managed")],
)
def test_the_modbus_integrations_battery_management_mode_says_who_runs_it(state: str, expected: str) -> None:
    facts = _modbus(_fact("battery_mgmt_mode", state))

    assert facts.management.state == expected


def test_a_disabled_battery_management_mode_leaves_the_mode_unknown() -> None:
    facts = _modbus(_fact("battery_mgmt_mode", MODBUS_NONE, disabled=True))

    assert facts.management.state == "unknown"


def test_the_battery_power_is_discharge_positive_on_both_integrations() -> None:
    r = Registry()
    r.device("inv", model="PLENTICORE plus", manufacturer="Kostal", entry="core")
    r.add("kostal_plenticore", "scb_battery_power", f"{ENTRY}_devices:local:battery_P", device_class="power", unit="W", device="inv", entry="core")
    r.add("kostal_plenticore", "scb_home_power_from_battery", f"{ENTRY}_devices:local_HomeBat_P", device_class="power", unit="W", device="inv", entry="core")
    [core] = r.detect().batteries
    assert core.entity_id == "sensor.scb_battery_power" and core.inverted is True

    r = Registry()
    r.device("inv", entry="mbc")
    r.add("kostal_modbus_control", "kostal_battery_power", f"{ENTRY}_battery_power", device_class="power", unit="W", device="inv", entry="mbc")
    r.add("kostal_modbus_control", "kostal_battery_dc_power_setpoint", f"{ENTRY}_battery_dc_power_setpoint", device_class="power", unit="W", device="inv", entry="mbc")
    [modbus] = r.detect().batteries
    assert modbus.entity_id == "sensor.kostal_battery_power" and modbus.inverted is True


def test_the_coexistence_row_names_the_inverters_own_control_and_tibbers_bridge() -> None:
    row = coexistence_for(platform="kostal_plenticore")

    assert row is not None and row.brand == "kostal"
    assert any("Tibber" in name for name in row.aggregators)
    assert coexistence_for(platform="kostal_modbus_control") is row
    assert [item.strength for item in outside_integrations({"kostal_modbus_control"})] == ["hint"]
