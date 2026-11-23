"""SMA Sunny Boy Storage and Sunny Tripower Smart Energy through core `sma` or `pysmaplus` (webconnect): read only. The unique
id is `<serial>-<pysma code>_<key index>` (`6100_00295A00`), a register code and not a name.

The registry is the fixture `sma` in `tests/fixtures/battery/brands.json`.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from custom_components.spotnav.battery import commands
from custom_components.spotnav.battery.adapters import SMA, adapter_for
from custom_components.spotnav.battery.coexist import coexistence_for
from custom_components.spotnav.battery.facts import battery_facts

from tests.test_battery_facts import BRANDS, _entities, _facts
from tests.test_site_detection import Registry


def test_sma_is_read_only_through_both_integrations() -> None:
    assert adapter_for("sma", "SMA") is SMA and adapter_for("pysmaplus", "SMA") is SMA
    assert all(control.brand != "sma" for control in commands.CONTROLS)
    assert SMA.hold is None and SMA.grid_charge is None and SMA.modes == ()


def test_it_reads_level_and_counters_and_no_size_rating_floor_or_mode() -> None:
    facts = _facts("sma")

    assert facts.brand == "sma"
    assert facts.soc_pct == 64.0 and facts.sources["soc"] == "sensor.sma_battery_soc_total"
    assert (facts.charged_total_kwh, facts.discharged_total_kwh) == (2100.0, 1900.0)
    assert facts.rated_charge_kw is None and facts.rated_discharge_kw is None
    assert facts.reserve_soc_pct is None and facts.management.settings == {}


def test_the_battery_capacity_total_is_a_percent_and_is_never_the_size() -> None:
    # 100 % read as 100 kWh would size the plan wrong by an order of magnitude.
    facts = _facts("sma")

    assert facts.capacity_kwh is None
    assert "capacity" not in facts.sources


def test_a_per_battery_level_is_not_the_total() -> None:
    facts = _facts("sma", **{"sensor.sma_battery_soc_total": {"state": "unavailable"}, "sensor.sma_battery_soc_a": {"disabled": False}})

    assert facts.soc_pct is None


def test_the_same_registry_under_pysmaplus_reads_the_same() -> None:
    brand = BRANDS["sma"]
    entities = [replace(item, platform="pysmaplus") for item in _entities(brand)]
    battery = next(item for item in entities if item.entity_id == brand["battery"])

    facts = battery_facts(battery, entities, manufacturer="SMA")

    assert facts.brand == "sma" and facts.integration == "pysmaplus" and facts.soc_pct == 64.0


def test_the_firmware_is_the_devices_software_version() -> None:
    brand = BRANDS["sma"]
    entities = _entities(brand)
    battery = next(item for item in entities if item.entity_id == brand["battery"])

    facts = battery_facts(battery, entities, manufacturer="SMA", sw_version="3.10.20.R")

    assert facts.firmware == "3.10.20.R"


@pytest.mark.parametrize("platform", ["sma", "pysmaplus"])
def test_the_two_unsigned_sensors_are_the_pair_on_both_integrations(platform: str) -> None:
    r = Registry()
    r.device("sbs", model="Sunny Boy Storage 3.7", manufacturer="SMA", entry="sm")
    for key in ("battery_power_charge_total", "battery_power_discharge_total", "battery_power_charge_a"):
        r.add(platform, f"sbs_{key}", f"1234567890-{key}", device_class="power", unit="W", device="sbs", entry="sm")

    [battery] = r.detect().batteries

    assert battery.entity_id == "sensor.sbs_battery_power_charge_total"
    assert battery.discharge_entity_id == "sensor.sbs_battery_power_discharge_total"
    assert battery.inverted is False


def test_the_coexistence_row_names_the_portals_forecast_charging_and_no_aggregator() -> None:
    row = coexistence_for(platform="pysmaplus")

    assert row is not None and row.brand == "sma" and "Forecast-based Charging" in row.own_optimiser and row.aggregators == ()
