"""sonnenBatterie through `sonnenbatterie` (weltmeyer `9441b4a`, 2026.11.6): read only, found by the key at the end of the
unique id (`sensor.sonnenbatterie_<serial>_<key>` for sensors, `sonnenbatterie_<serial>_<key>` for the rest).

The registry is the fixture `sonnen` in `tests/fixtures/battery/brands.json`.
"""

from __future__ import annotations

import pytest

from custom_components.spotnav.battery import commands
from custom_components.spotnav.battery.adapters import SONNEN, adapter_for
from custom_components.spotnav.battery.coexist import coexistence_for
from custom_components.spotnav.battery.facts import battery_facts

from tests.test_battery_facts import BRANDS, _entities, _facts
from tests.test_site_detection import Registry


def test_sonnen_is_read_only() -> None:
    assert adapter_for("sonnenbatterie", "sonnen") is SONNEN
    assert all(control.brand != "sonnen" for control in commands.CONTROLS)
    assert SONNEN.hold is None and SONNEN.grid_charge is None


def test_it_reads_the_user_level_the_rating_and_the_floor() -> None:
    facts = _facts("sonnen")

    assert facts.brand == "sonnen"
    # USOC, the user-visible level, not the real one (RSOC, disabled by default).
    assert facts.soc_pct == 64.0
    assert facts.sources["soc"] == "sensor.sonnen_state_of_charge"
    assert facts.rated_charge_kw == facts.rated_discharge_kw == 4.0
    assert facts.reserve_soc_pct == 15.0
    assert facts.on_grid is None


def test_the_rating_is_not_the_time_of_use_power_setting() -> None:
    # `tou_max_power` also ends in `max_power`.
    facts = _facts("sonnen")

    assert facts.sources["rated_charge"] == "sensor.sonnen_max_power"


def test_the_size_is_empty_while_its_sensor_is_disabled_and_read_in_wh_once_enabled() -> None:
    assert _facts("sonnen").capacity_kwh is None
    enabled = _facts("sonnen", **{"sensor.sonnen_installed_capacity": {"disabled": False}})

    assert enabled.capacity_kwh == pytest.approx(10.0)
    # The usable capacity is not the size.
    assert enabled.sources["capacity"] == "sensor.sonnen_installed_capacity"


def test_the_counters_are_not_read_because_the_integration_adds_them_up_itself() -> None:
    facts = _facts("sonnen")

    assert facts.charged_total_kwh is None and facts.discharged_total_kwh is None


@pytest.mark.parametrize(
    "state, expected",
    [("2", "free"), ("1", "self_managed"), ("10", "self_managed"), ("11", "self_managed"), ("unknown", "unknown")],
)
def test_only_automatic_is_the_units_own_and_free(state: str, expected: str) -> None:
    facts = _facts("sonnen", **{"sensor.sonnen_operating_mode": {"state": state}})

    assert facts.management.state == expected


def test_the_modes_select_is_not_the_mode_that_is_read() -> None:
    # The select's unique id also ends `_operating_mode`; its options are words, never `2`.
    facts = _facts("sonnen", **{"select.sonnen_operating_mode": {"state": "timeofuse"}})

    assert facts.management.state == "free"


def test_the_firmware_is_the_devices_software_version() -> None:
    brand = BRANDS["sonnen"]
    entities = _entities(brand)
    battery = next(item for item in entities if item.entity_id == brand["battery"])

    facts = battery_facts(battery, entities, manufacturer="sonnen", sw_version="1.40.5 (2.5.1)")

    assert facts.firmware == "1.40.5 (2.5.1)" and facts.sources["firmware"] == "device sw_version"


def test_the_battery_power_is_discharge_positive_and_the_positive_only_sensor_is_not_it() -> None:
    r = Registry()
    r.device("sb", model="sonnenBatterie 10", manufacturer="sonnen", entry="so")
    for key in ("state_battery_input", "state_battery_inout", "state_grid_inout"):
        r.add("sonnenbatterie", f"sb_{key}", f"sensor.sonnenbatterie_123456_{key}", device_class="power", unit="W", device="sb", entry="so")

    [battery] = r.detect().batteries

    assert battery.entity_id == "sensor.sb_state_battery_inout"
    assert battery.inverted is True and battery.discharge_entity_id is None


def test_the_coexistence_row_names_the_apps_modes_and_the_tibber_vpp() -> None:
    row = coexistence_for(platform="sonnenbatterie")

    assert row is not None and row.brand == "sonnen" and "optimization" in row.own_optimiser
    assert any("Tibber VPP" in name for name in row.aggregators)
