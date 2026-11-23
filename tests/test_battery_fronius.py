"""Fronius through core `fronius` (home-assistant/core `42f4a32`): read only, found by the key after the hyphen in the
unique id (`<serial>-<key>`, `<inverter id>-modbus-<key>`).

The registry is the fixture `fronius` in `tests/fixtures/battery/brands.json`.
"""

from __future__ import annotations

import pytest

from custom_components.spotnav.battery import commands
from custom_components.spotnav.battery.adapters import FRONIUS, adapter_for
from custom_components.spotnav.battery.coexist import coexistence_for

from tests.test_battery_facts import _facts
from tests.test_site_detection import Registry


def test_fronius_is_read_only() -> None:
    assert adapter_for("fronius", "Fronius") is FRONIUS
    assert all(control.brand != "fronius" for control in commands.CONTROLS)
    assert FRONIUS.hold is None and FRONIUS.grid_charge is None


def test_it_reads_level_designed_size_rating_floor_and_counters() -> None:
    facts = _facts("fronius")

    assert facts.brand == "fronius"
    assert facts.soc_pct == 41.0
    # The designed capacity, not the aged `capacity_maximum`.
    assert facts.capacity_kwh == pytest.approx(10.24)
    assert facts.sources["capacity"] == "sensor.byd_designed_capacity"
    assert (facts.rated_charge_kw, facts.rated_discharge_kw) == (6.0, 6.0)
    assert facts.reserve_soc_pct == 10.0
    assert (facts.charged_total_kwh, facts.discharged_total_kwh) == (5200.0, 4700.0)
    assert facts.management.state == "free"


@pytest.mark.parametrize(
    "entity", ["switch.inverter_battery_charge_power_limit_enabled", "switch.inverter_battery_discharge_power_limit_enabled"]
)
def test_a_limit_in_force_is_someone_elses_and_the_grid_charging_switch_is_no_mode(entity: str) -> None:
    facts = _facts("fronius", **{entity: {"state": "on"}})
    assert facts.management.state == "self_managed"

    grid = _facts("fronius", **{"switch.inverter_battery_grid_charging": {"state": "on"}})
    assert grid.management.state == "free"


def test_without_the_modbus_settings_it_reads_the_level_and_size_only() -> None:
    gone = {
        entity: {"disabled": True}
        for entity in (
            "sensor.inverter_battery_power_limit_reference", "number.inverter_battery_minimum_reserve",
            "sensor.inverter_storage_energy_charged_total", "sensor.inverter_storage_energy_discharged_total",
        )
    }

    facts = _facts("fronius", **gone)

    assert facts.soc_pct == 41.0 and facts.capacity_kwh == pytest.approx(10.24)
    assert facts.rated_charge_kw is None and facts.reserve_soc_pct is None and facts.charged_total_kwh is None


def test_the_battery_power_is_discharge_positive_and_the_split_sensors_are_not_the_signed_one() -> None:
    r = Registry()
    r.device("sn", model="Fronius Solar Net", manufacturer="Fronius", entry="fr")
    for key in ("power_battery", "power_battery_charge", "power_battery_discharge"):
        r.add("fronius", f"solarnet_{key}", f"solar_net_1-power_flow-{key}", device_class="power", unit="W", device="sn", entry="fr")

    [battery] = r.detect().batteries

    assert battery.entity_id == "sensor.solarnet_power_battery" and battery.inverted is True


def test_the_coexistence_row_names_the_web_uis_own_control_and_no_aggregator() -> None:
    row = coexistence_for(platform="fronius")

    assert row is not None and row.brand == "fronius" and row.aggregators == ()
