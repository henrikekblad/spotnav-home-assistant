"""Homevolt through core `homevolt` (home-assistant/core `42f4a32`, pyHomevolt 0.7.0): read only, found by the
lower-cased display key after the ECU id in the unique id.

The registry is the fixture `homevolt` in `tests/fixtures/battery/brands.json`.
"""

from __future__ import annotations

import pytest

from custom_components.spotnav.battery import commands
from custom_components.spotnav.battery.adapters import HOMEVOLT, adapter_for
from custom_components.spotnav.battery.coexist import coexistence_for

from tests.test_battery_facts import _facts
from tests.test_site_detection import Registry


def test_homevolt_is_read_only() -> None:
    assert adapter_for("homevolt", "Tibber") is HOMEVOLT
    assert all(control.brand != "homevolt" for control in commands.CONTROLS)
    assert HOMEVOLT.hold is None and HOMEVOLT.grid_charge is None


def test_it_reads_the_systems_level_and_nothing_it_cannot_state() -> None:
    facts = _facts("homevolt")

    assert facts.brand == "homevolt"
    assert facts.soc_pct == 68.0
    assert facts.sources["soc"] == "sensor.homevolt_state_of_charge"
    # The sum of the two available energies is one example's size, not a rule; the aggregates are no battery counters;
    # the available powers are what is free now.
    assert facts.capacity_kwh is None
    assert facts.rated_charge_kw is None and facts.rated_discharge_kw is None
    assert facts.charged_total_kwh is None and facts.discharged_total_kwh is None
    assert facts.firmware is None


def test_the_battery_is_the_persons_only_while_local_mode_is_on() -> None:
    assert _facts("homevolt").management.state == "free"

    tibber = _facts("homevolt", **{"switch.homevolt_local_mode": {"state": "off"}})

    assert tibber.management.state == "self_managed" and tibber.management.reason == "local mode:off"


def test_a_missing_local_mode_switch_leaves_it_unknown() -> None:
    facts = _facts("homevolt", **{"switch.homevolt_local_mode": {"disabled": True}})

    assert facts.management.state == "unknown"


def test_the_ems_power_is_the_battery_and_is_discharge_positive() -> None:
    r = Registry()
    r.device("ems", model="EMS", manufacturer="Tibber", entry="hv")
    r.add("homevolt", "homevolt_power", "b8747fe8c066_Power", device_class="power", unit="W", device="ems", entry="hv")
    r.add("homevolt", "homevolt_battery_1_power", "b8747fe8c066_Homevolt battery 1 power", device_class="power", unit="W", device="ems", entry="hv")
    r.add("homevolt", "homevolt_power_grid", "b8747fe8c066_Power grid", device_class="power", unit="W", device="ems", entry="hv")

    [battery] = r.detect().batteries

    assert battery.entity_id == "sensor.homevolt_power" and battery.inverted is True


def test_the_coexistence_row_names_tibber() -> None:
    row = coexistence_for(platform="homevolt")

    assert row is not None and row.brand == "homevolt" and "Tibber Grid Rewards [V]" in row.aggregators
