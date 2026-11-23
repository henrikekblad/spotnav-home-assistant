"""Anker SOLIX Solarbank through `anker_solix_official` (anker-charging `a9930d7`, 1.5.1): read only, found by the key
at the end of the unique id (`<config entry id>_<key>`).

The registry is the fixture `anker` in `tests/fixtures/battery/brands.json`.
"""

from __future__ import annotations

import pytest

from custom_components.spotnav.battery import commands
from custom_components.spotnav.battery.adapters import ANKER, adapter_for
from custom_components.spotnav.battery.coexist import coexistence_for

from tests.test_battery_facts import _facts
from tests.test_site_detection import Registry


def test_anker_is_read_only_and_only_the_official_integration_has_an_adapter() -> None:
    assert adapter_for("anker_solix_official", "Anker") is ANKER
    assert adapter_for("anker_solix", "Anker") is None
    assert all(control.brand != "anker" for control in commands.CONTROLS)
    assert ANKER.hold is None and ANKER.grid_charge is None


def test_it_reads_level_size_firmware_and_the_discharge_floor() -> None:
    facts = _facts("anker")

    assert facts.brand == "anker"
    assert facts.soc_pct == 64.0
    assert facts.capacity_kwh == pytest.approx(7.0)
    assert facts.firmware == "1.0.2.23"
    # The discharge limit, not the backup reserve (which holds only while an unreadable switch is on).
    assert facts.reserve_soc_pct == 10.0
    assert facts.sources["reserve"] == "number.solarbank_discharge_limit_soc"
    assert facts.rated_charge_kw is None and facts.rated_discharge_kw is None


def test_the_energy_counters_are_not_read_because_the_discharge_one_under_counts() -> None:
    facts = _facts("anker")

    assert facts.charged_total_kwh is None and facts.discharged_total_kwh is None


@pytest.mark.parametrize(
    "state, expected",
    [("self_consumption", "free"), ("tou_mode", "self_managed"), ("smart_mode", "self_managed"),
     ("dynamic_pricing", "self_managed"), ("third_party_control", "self_managed"), ("custom_mode", "self_managed")],
)
def test_only_self_consumption_is_the_units_own_and_free(state: str, expected: str) -> None:
    facts = _facts("anker", **{"select.solarbank_operating_mode": {"state": state}})

    assert facts.management.state == expected


def test_the_two_unsigned_power_sensors_are_the_batterys_pair() -> None:
    r = Registry()
    r.device("sb", model="Anker SOLIX Solarbank Max AC", manufacturer="Anker", entry="an")
    for key in ("battery_charging_power", "battery_discharging_power", "pv_power"):
        r.add("anker_solix_official", f"sb_{key}", f"01JANK_{key}", device_class="power", unit="W", device="sb", entry="an")

    [battery] = r.detect().batteries

    assert battery.entity_id == "sensor.sb_battery_charging_power"
    assert battery.discharge_entity_id == "sensor.sb_battery_discharging_power"
    assert battery.inverted is False


def test_the_coexistence_row_names_the_apps_modes_and_no_aggregator() -> None:
    row = coexistence_for(platform="anker_solix_official")

    assert row is not None and row.brand == "anker" and row.aggregators == ()
