"""Deye and Sunsynk hybrids through `solarman` (davidrapan `84982c5`, the Deye profiles) or kellerza's MQTT add-on
(`49efe3f`): found by the key at the end of the unique id and only on a device made by Deye or Sunsynk. The control
(limits only) is in `test_battery_deye_control.py`.

The `solarman` registry is the fixture `deye` in `tests/fixtures/battery/brands.json`; kellerza's
(`<inverter serial>_<slug of the name>`) is built here.
"""

from __future__ import annotations

import pytest

from custom_components.spotnav.battery import commands
from custom_components.spotnav.battery.adapters import DEYE, adapter_for
from custom_components.spotnav.battery.coexist import coexistence_for
from custom_components.spotnav.battery.facts import EntityFact, battery_facts

from tests.test_battery_facts import _facts
from tests.test_site_detection import Registry

SERIAL = "2201010101"


def _mqtt(key: str, state: str, unit: str | None = None, domain: str = "sensor") -> EntityFact:
    return EntityFact(
        entity_id=f"{domain}.ss_{key}", platform="mqtt", unique_id=f"{SERIAL}_{key}", device_id="inverter",
        config_entry_id="entry", state=state, unit=unit,
    )


def test_deye_and_sunsynk_makers_on_both_platforms_are_the_deye_adapter_and_its_control_is_limits_only() -> None:
    assert adapter_for("solarman", "Deye") is DEYE
    assert adapter_for("solarman", "Sunsynk") is DEYE
    assert adapter_for("mqtt", "Sunsynk") is DEYE
    # The platforms are shared: another maker's profile or MQTT device is not read as Deye.
    assert adapter_for("solarman", "Solis") is None
    assert adapter_for("solarman", None) is None
    assert adapter_for("mqtt", "Shelly") is None
    assert commands.control_for("deye") is commands.DEYE_CONTROL
    assert DEYE.hold is None and DEYE.grid_charge is None


def test_solarman_reads_level_learnt_size_and_counters_but_not_the_ah_setting_or_the_current_limits() -> None:
    facts = _facts("deye")

    assert facts.brand == "deye"
    assert facts.soc_pct == 56.0
    assert facts.capacity_kwh == pytest.approx(10.2)
    assert facts.sources["capacity"] == "sensor.deye_battery_capacity"
    assert (facts.charged_total_kwh, facts.discharged_total_kwh) == (3200.5, 3000.1)
    # The Ah the person types and the current settings are no size and no rating.
    assert facts.rated_charge_kw is None and facts.rated_discharge_kw is None
    assert facts.estimates == ()


def test_the_size_is_empty_until_solarman_has_learnt_it() -> None:
    facts = _facts("deye", **{"sensor.deye_battery_capacity": {"state": "unknown"}})

    assert facts.capacity_kwh is None


@pytest.mark.parametrize("state, expected", [("Disabled", "free"), ("Week", "self_managed"), ("Weekdays", "self_managed")])
def test_the_time_of_use_setting_says_whether_the_inverter_runs_its_programmes(state: str, expected: str) -> None:
    facts = _facts("deye", **{"select.deye_time_of_use": {"state": state}})

    assert facts.management.state == expected


def test_kellerza_reads_the_level_and_counters_and_the_timer_switch() -> None:
    entities = [
        _mqtt("battery_power", "-800", "W"),
        _mqtt("battery_soc", "71", "%"),
        _mqtt("total_battery_charge", "1000.5", "kWh"),
        _mqtt("total_battery_discharge", "900", "kWh"),
        _mqtt("use_timer", "off", domain="switch"),
        # Not read: a second battery's level and the capacity setting in Ah.
        _mqtt("battery_2_soc", "10", "%"),
        _mqtt("battery_capacity_current", "200", "A", domain="number"),
    ]

    facts = battery_facts(entities[0], entities, manufacturer="Sunsynk", power_kw=-0.8)

    assert facts.brand == "deye" and facts.soc_pct == 71.0
    assert (facts.charged_total_kwh, facts.discharged_total_kwh) == (1000.5, 900.0)
    assert facts.capacity_kwh is None
    assert facts.management.state == "free"

    on = [*entities[:4], _mqtt("prog_time_of_use_enabled", "on", domain="switch")]
    assert battery_facts(on[0], on, manufacturer="Deye").management.state == "self_managed"


def test_the_battery_power_is_discharge_positive_for_the_makers_devices_only() -> None:
    r = Registry()
    r.device("ss", model="Sunsynk 5.0", manufacturer="Sunsynk", entry="mq")
    r.add("mqtt", "ss_battery_power", f"{SERIAL}_battery_power", device_class="power", unit="W", device="ss", entry="mq")
    [battery] = r.detect().batteries
    assert battery.entity_id == "sensor.ss_battery_power" and battery.inverted is True

    other = Registry()
    other.device("pw", model="Something", manufacturer="Someone else", entry="mq")
    other.add("mqtt", "pw_battery_power", "abcd_battery_power", device_class="power", unit="W", device="pw", entry="mq")
    assert other.detect().batteries == ()


def test_the_coexistence_row_is_the_vendors_time_of_use_and_names_no_aggregator() -> None:
    row = coexistence_for(brand="deye")

    assert row is not None and row.aggregators == () and "time-of-use" in row.own_optimiser
    # `mqtt` is every MQTT device's: it names no brand.
    assert coexistence_for(platform="mqtt") is None
