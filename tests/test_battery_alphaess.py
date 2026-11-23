"""Alpha ESS SMILE through `alphaess_modbus` (senalse `b256f55`, v1.15.0): read only, found by the key at the end of the
unique id (`<config entry id>_<key>`). The cloud integration `alphaess` names its entities by English display names and is
not read.

The registry is the fixture `alphaess` in `tests/fixtures/battery/brands.json`.
"""

from __future__ import annotations

import pytest

from custom_components.spotnav.battery import commands
from custom_components.spotnav.battery.adapters import ALPHAESS, adapter_for
from custom_components.spotnav.battery.coexist import coexistence_for, outside_integrations
from custom_components.spotnav.battery.facts import battery_facts

from tests.test_battery_facts import BRANDS, _entities, _facts
from tests.test_site_detection import Registry


def test_alphaess_is_read_only_and_only_the_modbus_integration_has_an_adapter() -> None:
    assert adapter_for("alphaess_modbus", "AlphaESS") is ALPHAESS
    assert adapter_for("alphaess", "AlphaESS") is None
    assert all(control.brand != "alphaess" for control in commands.CONTROLS)
    assert ALPHAESS.hold is None and ALPHAESS.grid_charge is None


def test_it_reads_level_counters_firmware_and_the_floor() -> None:
    facts = _facts("alphaess")

    assert facts.brand == "alphaess"
    assert facts.soc_pct == 64.0
    assert (facts.charged_total_kwh, facts.discharged_total_kwh) == (4100.5, 3800.2)
    # The EMS version, not the inverter's DSP version.
    assert facts.firmware == "V0.11.627"
    assert facts.reserve_soc_pct == 10.0
    # The BMS's present current limit is no rating, and no size is read while its sensor is disabled.
    assert facts.rated_charge_kw is None and facts.rated_discharge_kw is None
    assert facts.capacity_kwh is None


def test_the_grid_charged_counter_is_not_the_charge_counter() -> None:
    facts = _facts("alphaess")

    assert facts.sources["charged_total"] == "sensor.alphaess_total_energy_charge_battery"


def test_the_size_is_read_once_its_disabled_sensor_is_enabled() -> None:
    facts = _facts("alphaess", **{"sensor.alphaess_battery_capacity": {"disabled": False}})

    assert facts.capacity_kwh == pytest.approx(10.1)


@pytest.mark.parametrize("dispatch, windows, expected", [
    ("0", "0", "free"), ("1", "0", "self_managed"), ("0", "1", "self_managed"), ("0", "3", "self_managed"),
    ("unavailable", "0", "unknown"),
])
def test_a_running_dispatch_or_the_apps_windows_are_not_the_free_state(dispatch: str, windows: str, expected: str) -> None:
    facts = _facts(
        "alphaess",
        **{
            "sensor.alphaess_dispatch_start": {"state": dispatch},
            "sensor.alphaess_charging_time_period_control": {"state": windows},
        },
    )

    assert facts.management.state == expected


def test_the_time_period_select_is_not_the_mode_that_is_read() -> None:
    # The select shares the sensor's key; its options are words.
    facts = _facts("alphaess", **{"select.alphaess_charging_time_period_control": {"state": "Enable Grid Charging"}})

    assert facts.management.state == "free"


def test_without_the_dispatch_sensor_the_mode_is_unknown() -> None:
    brand = BRANDS["alphaess"]
    entities = [item for item in _entities(brand) if not item.entity_id.endswith("dispatch_start")]
    battery = next(item for item in entities if item.entity_id == brand["battery"])

    assert battery_facts(battery, entities, manufacturer="AlphaESS").management.state == "unknown"


def test_the_battery_power_is_read_discharge_positive_sign_unverified_on_hardware() -> None:
    r = Registry()
    r.device("inv", model="SMILE-G3-S5", manufacturer="AlphaESS", entry="ae")
    for key in ("power_battery", "power_grid", "power_inverter_l1"):
        r.add("alphaess_modbus", f"inv_{key}", f"01jaess_{key}", device_class="power", unit="W", device="inv", entry="ae")

    [battery] = r.detect().batteries

    assert battery.entity_id == "sensor.inv_power_battery"
    assert battery.inverted is True and battery.discharge_entity_id is None


def test_the_coexistence_row_names_the_app_and_the_suppliers_dispatch() -> None:
    row = coexistence_for(platform="alphaess_modbus")

    assert row is not None and row.brand == "alphaess" and "time windows" in row.own_optimiser
    assert coexistence_for(platform="alphaess") is row
    assert any("Frank Energie" in name for name in row.aggregators)


def test_the_modbus_integration_is_a_hint_and_never_marks_the_battery_by_itself() -> None:
    [found] = outside_integrations({"alphaess_modbus"})

    assert found.strength == "hint"
