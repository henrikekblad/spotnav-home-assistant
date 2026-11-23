"""E3/DC through `e3dc_rscp` (torbennehmer `3fd5ab0`, 4.0.0): read only, found by the hyphenated key after the `_` that
follows the serial, so the day, month and year statistics (`db-day-battery-charge`) are never the battery's power.

The registry is the fixture `e3dc` in `tests/fixtures/battery/brands.json`.
"""

from __future__ import annotations

import pytest

from custom_components.spotnav.battery import commands
from custom_components.spotnav.battery.adapters import E3DC, adapter_for
from custom_components.spotnav.battery.coexist import coexistence_for, outside_integrations
from custom_components.spotnav.battery.facts import battery_facts

from tests.test_battery_facts import BRANDS, _entities, _facts
from tests.test_site_detection import Registry


def test_e3dc_is_read_only() -> None:
    assert adapter_for("e3dc_rscp", "E3DC") is E3DC
    assert adapter_for("e3dc_rscp_connect", "E3DC") is None
    assert all(control.brand != "e3dc" for control in commands.CONTROLS)
    assert E3DC.hold is None and E3DC.grid_charge is None


def test_it_reads_level_size_and_the_devices_own_ratings() -> None:
    facts = _facts("e3dc")

    assert facts.brand == "e3dc"
    assert facts.soc_pct == 64.0
    assert facts.capacity_kwh == pytest.approx(13.8)
    assert facts.rated_charge_kw == facts.rated_discharge_kw == 4.0
    assert facts.reserve_soc_pct is None


def test_the_statistics_are_no_counters_and_are_not_read() -> None:
    facts = _facts("e3dc")

    assert facts.charged_total_kwh is None and facts.discharged_total_kwh is None


def test_a_statistics_key_is_never_matched_in_place_of_the_live_one() -> None:
    entities = [item for item in _entities(BRANDS["e3dc"]) if "db-" in item.unique_id]
    assert entities, "the fixture holds the statistics"
    # Only the day, month and year statistics: nothing is the battery's power, level or rating.
    battery = entities[0]
    facts = battery_facts(battery, entities, manufacturer="E3DC")

    assert facts.soc_pct is None and facts.rated_charge_kw is None and facts.capacity_kwh is None


def test_a_site_with_limits_or_a_manual_charge_running_is_not_free() -> None:
    limits = _facts("e3dc", **{"binary_sensor.s10_power_limits_enabled": {"state": "on"}})
    manual = _facts("e3dc", **{"binary_sensor.s10_manual_charge_active": {"state": "on"}})

    assert limits.management.state == manual.management.state == "self_managed"
    assert _facts("e3dc").management.state == "free"


def test_an_enabled_idle_period_is_the_units_own_schedule() -> None:
    facts = _facts("e3dc", **{"binary_sensor.s10_discharge_idle_period_active": {"disabled": False, "state": "on"}})

    assert facts.management.state == "self_managed"
    assert "discharge idle period:on" in facts.management.reason


def test_the_ems_power_mode_is_not_who_runs_the_battery() -> None:
    # `power-mode` is the present state (idle, discharge, charge) of any mode, the unit's own included.
    for state in ("0", "1", "2"):
        assert _facts("e3dc", **{"sensor.s10_power_mode": {"state": state}}).management.state == "free"


def test_the_battery_power_is_the_charge_and_discharge_pair_not_the_statistics() -> None:
    r = Registry()
    r.device("s10", model="S10 E", manufacturer="E3DC", entry="e3")
    for key in ("db-day-battery-charge", "db-day-battery-discharge", "battery-charge", "battery-discharge"):
        r.add("e3dc_rscp", f"s10_{key.replace('-', '_')}", f"S10-1_{key}", device_class="power", unit="W", device="s10", entry="e3")

    [battery] = r.detect().batteries

    assert battery.entity_id == "sensor.s10_battery_charge"
    assert battery.discharge_entity_id == "sensor.s10_battery_discharge"
    assert battery.inverted is False


def test_the_coexistence_row_names_ai360_and_no_aggregator() -> None:
    row = coexistence_for(platform="e3dc_rscp")

    assert row is not None and row.brand == "e3dc" and "AI360" in row.own_optimiser and row.aggregators == ()


def test_e3dc_maestro_is_an_outside_controller_that_runs_the_battery() -> None:
    [found] = outside_integrations({"e3dc_maestro", "e3dc_rscp"})

    assert found.domain == "e3dc_maestro" and found.strength == "runs"
