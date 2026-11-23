"""Omnibattery (`omnibattery`, ffunes/Omnibattery `e60eb4f`): read only, the system's aggregate entities, found by the
key at the end of their legacy unique id `marstek_venus_system_<key>`. The registry is the fixture `omnibattery` in
`tests/fixtures/battery/brands.json`.
"""

from __future__ import annotations

import pytest

from custom_components.spotnav.battery import commands
from custom_components.spotnav.battery.adapters import OMNIBATTERY, adapter_for
from custom_components.spotnav.battery.coexist import outside_integrations

from tests.test_battery_facts import _facts


def test_omnibattery_is_read_only_and_has_no_mode_of_its_own() -> None:
    assert adapter_for("omnibattery") is OMNIBATTERY
    assert all(control.brand != "omnibattery" for control in commands.CONTROLS)
    assert OMNIBATTERY.hold is None and OMNIBATTERY.grid_charge is None and OMNIBATTERY.modes == ()


def test_it_reads_the_systems_level_and_size_and_nothing_else() -> None:
    facts = _facts("omnibattery")

    assert facts.brand == "omnibattery" and facts.integration == "omnibattery"
    assert facts.soc_pct == 64.0
    assert facts.capacity_kwh == pytest.approx(10.24)
    assert facts.rated_charge_kw is None and facts.rated_discharge_kw is None
    assert facts.reserve_soc_pct is None
    # The daily counters restart every day: no lifetime total is read.
    assert facts.charged_total_kwh is None and facts.discharged_total_kwh is None
    assert facts.sources["soc"] == "sensor.omnibattery_system_soc"


def test_a_single_batterys_level_is_not_read_as_the_systems() -> None:
    facts = _facts("omnibattery", **{"sensor.omnibattery_system_soc": {"state": "unavailable"}})

    assert facts.soc_pct is None


def test_the_battery_is_watched_by_the_integrations_presence() -> None:
    found = outside_integrations({"omnibattery"})
    assert [(row.domain, row.strength) for row in found] == [("omnibattery", "runs")]
