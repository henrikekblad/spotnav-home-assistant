"""Zendure SolarFlow, Hyper and AIO through `zendure_ha` (Zendure-HA `e67b04e`): read only, found by the property at the
end of a unique id that is built from the device's name in the app, on the battery's own device first.

One device's registry is the fixture `zendure` in `tests/fixtures/battery/brands.json`; a second device is built here.
"""

from __future__ import annotations

import pytest

from custom_components.spotnav.battery import commands
from custom_components.spotnav.battery.adapters import ZENDURE, adapter_for
from custom_components.spotnav.battery.coexist import coexistence_for
from custom_components.spotnav.battery.facts import EntityFact, battery_facts

from tests.test_battery_facts import BRANDS, _entities, _facts


def test_zendure_is_read_only() -> None:
    assert adapter_for("zendure_ha", "Zendure") is ZENDURE
    assert all(control.brand != "zendure" for control in commands.CONTROLS)
    assert ZENDURE.hold is None and ZENDURE.grid_charge is None


def test_it_reads_level_size_output_rating_floor_and_the_counters_the_integration_adds_up() -> None:
    facts = _facts("zendure")

    assert facts.brand == "zendure"
    assert facts.soc_pct == 58.0
    assert facts.capacity_kwh == pytest.approx(5.76)
    assert facts.rated_discharge_kw == 2.4
    assert facts.reserve_soc_pct == 10.0
    assert (facts.charged_total_kwh, facts.discharged_total_kwh) == (120.5, 108.2)
    assert facts.management.state == "free"


def test_the_charge_rating_is_not_read_from_a_sensor_that_has_no_unit() -> None:
    # `charge_max_limit` reads 3200 with no unit: read as kW it would be 3200 kW.
    facts = _facts("zendure")

    assert facts.rated_charge_kw is None
    assert "rated_charge" not in facts.sources


def test_a_size_of_zero_for_an_unknown_pack_reads_as_no_size() -> None:
    facts = _facts("zendure", **{"sensor.solarflow_2400_ac_plus_total_kwh": {"state": "0"}})

    assert facts.capacity_kwh is None


def test_two_devices_are_told_apart_by_their_own_device() -> None:
    brand = BRANDS["zendure"]
    first = _entities(brand)
    own = {"electric_level": "21", "total_kwh": "1.92"}
    second = [
        EntityFact(
            entity_id=entity.entity_id.replace("solarflow_2400_ac_plus", "hyper_2000"),
            platform="zendure_ha",
            unique_id=entity.unique_id.replace("solarflow_2400_ac_plus", "hyper_2000"),
            device_id="hyper", config_entry_id="entry", device_class=entity.device_class, unit=entity.unit,
            state=next((value for key, value in own.items() if entity.unique_id.endswith(key)), entity.state),
        )
        for entity in first
        if entity.device_id == "sf"
    ]
    entities = [*first, *second]
    battery = next(item for item in entities if item.entity_id == "sensor.hyper_2000_output_pack_power")

    facts = battery_facts(battery, entities, manufacturer="Zendure", power_kw=0.0)

    assert facts.sources["soc"] == "sensor.hyper_2000_electric_level"
    assert facts.soc_pct == 21.0
    assert facts.capacity_kwh == pytest.approx(1.92)


@pytest.mark.parametrize(
    "entity, state",
    [("select.zendure_manager_operation", "manual"), ("select.zendure_manager_operation", "smart"),
     ("binary_sensor.solarflow_2400_ac_plus_hems_state", "on"), ("sensor.solarflow_2400_ac_plus_soc_status", "1")],
)
def test_the_manager_the_apps_hems_and_the_calibration_are_not_free(entity: str, state: str) -> None:
    facts = _facts("zendure", **{entity: {"state": state}})

    assert facts.management.state == "self_managed"


def test_without_the_manager_entity_the_mode_is_unknown() -> None:
    facts = _facts("zendure", **{"select.zendure_manager_operation": {"disabled": True}})

    assert facts.management.state == "unknown"


def test_the_coexistence_row_names_the_apps_hems_and_the_manager() -> None:
    row = coexistence_for(platform="zendure_ha")

    assert row is not None and row.brand == "zendure" and "HEMS" in row.own_optimiser and row.aggregators == ()
