"""FoxESS Modbus (`foxess_modbus`) in the site detection table and the battery adapter: read only.

Each fixture (`tests/fixtures/foxess/`) is one model family's entities as the integration names them, hand-picked from
its source at the commit its `_source` names: the keys SpotNav reads and the ones that end like them (the per-input
battery entities, the reactive and apparent CT entities, the inverter's own phase current). The 1KOMMA5 `1K5-HI`
units are read as an H3-Smart, the register set the integration maps them to.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from custom_components.spotnav.battery.adapters import adapter_for
from custom_components.spotnav.battery.commands import CONTROLS
from custom_components.spotnav.battery.facts import EntityFact, battery_facts
from custom_components.spotnav.battery.plan_shadow import battery_params
from custom_components.spotnav.const import MEASUREMENT_MODE_DERIVED
from custom_components.spotnav.site.measurement_source import GridPowerSource

from .test_site_detection import Registry, one_meter, roles

FIXTURES = Path(__file__).parent / "fixtures" / "foxess"
PLATFORM = "foxess_modbus"
FAMILIES = ("h1_g2", "kh", "h3", "h3_pro", "one_komma_five")
THREE_PHASE = ("h3", "h3_pro", "one_komma_five")


def fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def _ids(data: dict[str, Any], key: str) -> tuple[str, str]:
    """The object id and the unique id of a key: `<prefix>_<key>` and `foxess_modbus_[<prefix>_]<key>`."""
    prefixed = f"{data['prefix']}_{key}" if data["prefix"] else key
    return prefixed.lower(), f"foxess_modbus_{prefixed}"


def registry(name: str) -> Registry:
    data = fixture(name)
    r = Registry()
    r.device("inverter", model=data["model"], manufacturer=data["manufacturer"], name="FoxESS - Modbus", entry=name)
    for entity in data["entities"]:
        if entity["domain"] != "sensor":
            continue
        object_id, unique_id = _ids(data, entity["key"])
        r.add(
            PLATFORM,
            object_id,
            unique_id,
            device_class=entity["device_class"],
            unit=entity["unit"],
            device="inverter",
            entry=name,
            disabled=not entity["enabled"],
            original_name=entity["name"],
        )
    return r


def facts(
    name: str, states: dict[str, str], *, enable: tuple[str, ...] = (), drop: tuple[str, ...] = (),
    power_kw: float | None = 1.5,
):
    """The battery facts of a fixture whose entities read `states` (by `<domain>.<key>`, or by key for a sensor);
    `enable` the keys enabled beside the ones the integration enables, `drop` the keys left out."""
    data = fixture(name)
    entities = []
    for entity in data["entities"]:
        if entity["key"] in drop:
            continue
        object_id, unique_id = _ids(data, entity["key"])
        state = states.get(
            f"{entity['domain']}.{entity['key']}", states.get(entity["key"]) if entity["domain"] == "sensor" else None
        )
        entities.append(
            EntityFact(
                entity_id=f"{entity['domain']}.{object_id}",
                platform=PLATFORM,
                unique_id=unique_id,
                device_id="inverter",
                config_entry_id="entry",
                disabled=not entity["enabled"] and entity["key"] not in enable,
                device_class=entity["device_class"],
                state=state,
                unit=entity["unit"],
            )
        )
    battery = next(e for e in entities if e.domain == "sensor" and e.unique_id == _ids(data, "invbatpower")[1])
    return battery_facts(battery, entities, manufacturer=data["manufacturer"], power_kw=power_kw)


# ---- the battery's power ----------------------------------------------------------------------------


@pytest.mark.parametrize("name", FAMILIES)
def test_every_family_has_one_battery_the_whole_power_discharge_positive_and_negated(name: str) -> None:
    [battery] = registry(name).detect().batteries

    assert battery.entity_id == f"sensor.{_ids(fixture(name), 'invbatpower')[0]}"
    assert battery.inverted is True and battery.discharge_entity_id is None


# ---- the grid meter ---------------------------------------------------------------------------------


@pytest.mark.parametrize("name", THREE_PHASE)
def test_the_h3_family_meter_is_the_grid_ct_per_phase_export_positive_with_an_estimated_current(name: str) -> None:
    data = fixture(name)
    meter = one_meter(registry(name))

    assert meter.mode == MEASUREMENT_MODE_DERIVED and meter.catalogued
    assert meter.power_inverted is True and meter.estimated is True
    assert meter.derived_entities["L2"] == {
        "power": f"sensor.{_ids(data, 'grid_ct_S')[0]}",
        "voltage": f"sensor.{_ids(data, 'grid_voltage_S')[0]}",
    }
    # The inverter's own `inv_current_R/S/T` is never the grid's current.
    assert "current" not in roles(meter)
    # The grid voltages ship disabled: offered to be enabled, never assumed on.
    assert set(meter.disabled_entity_ids) == {f"sensor.{_ids(data, f'grid_voltage_{p}')[0]}" for p in "RST"}


@pytest.mark.parametrize("name", THREE_PHASE)
def test_the_h3_meter_reads_only_the_grid_ct_and_voltage_per_phase(name: str) -> None:
    meter = one_meter(registry(name))

    used = {entity_id for ids in roles(meter).values() for entity_id in ids}
    assert all(
        entity_id.endswith("grid_ct") or "grid_ct_" in entity_id or "grid_voltage_" in entity_id for entity_id in used
    )
    assert not [e for e in used if any(word in e for word in ("reactive", "apparent", "pf", "feed_in", "consumption"))]


def test_the_h3_pro_and_the_1k5_add_their_total_grid_ct_and_the_h3_has_none() -> None:
    for name in ("h3_pro", "one_komma_five"):
        assert one_meter(registry(name)).grid_power == GridPowerSource(power="sensor.grid_ct")
    assert one_meter(registry("h3")).grid_power is None


@pytest.mark.parametrize("name", ["h1_g2", "kh"])
def test_a_single_phase_family_with_only_a_total_grid_ct_makes_no_meter(name: str) -> None:
    assert registry(name).detect().meters == ()


# ---- the battery's facts ----------------------------------------------------------------------------


def test_h1_reads_its_level_floor_mode_and_firmware_and_learns_its_size() -> None:
    found = facts(
        "h1_g2",
        {
            "battery_soc": "64",
            "bms_kwh_remaining": "6.2",
            "invbatvolt": "400",
            "bms_charge_rate": "25",
            "bms_discharge_rate": "25",
            "number.min_soc_on_grid": "15",
            "min_soc_on_grid": "15",
            "number.min_soc": "10",
            "select.work_mode": "Self Use",
            "master_version": "1.44",
        },
    )

    assert found.brand == "foxess" and found.label == "FoxESS"
    assert found.soc_pct == 64.0 and found.sources["soc"] == "sensor.battery_soc"
    # `bms_kwh_remaining` is the energy left now, never the size.
    assert found.capacity_kwh is None
    assert found.reserve_soc_pct == 15.0 and found.sources["reserve"] == "number.min_soc_on_grid"
    assert found.management.state == "free" and found.firmware == "1.44"
    # The BMS rates ship disabled: until they are enabled there is no rating to read, so it is learnt.
    assert found.rated_charge_kw is None and found.rated_discharge_kw is None and found.estimates == ()


@pytest.mark.parametrize("name", ["h1_g2", "kh"])
def test_h1_and_kh_with_the_bms_rates_enabled_read_their_ratings_as_current_times_voltage(name: str) -> None:
    found = facts(
        name,
        {"battery_soc": "64", "invbatvolt": "400", "bms_charge_rate": "25", "bms_discharge_rate": "30"},
        enable=("bms_charge_rate", "bms_discharge_rate"),
    )

    assert found.rated_charge_kw == pytest.approx(10.0) and found.rated_discharge_kw == pytest.approx(12.0)
    assert found.estimates == ("rated_from_current",)
    assert found.sources["rated_charge"] == "sensor.bms_charge_rate x sensor.invbatvolt"


def test_h3_reads_its_level_and_has_no_rating_to_read() -> None:
    found = facts("h3", {"battery_soc": "71", "batvolt": "350", "number.min_soc_on_grid": "20"})

    assert found.soc_pct == 71.0 and found.sources["soc"] == "sensor.garage_battery_soc"
    assert found.reserve_soc_pct == 20.0
    assert found.capacity_kwh is None and found.rated_charge_kw is None and found.rated_discharge_kw is None
    assert "soc_packs" not in found.sources


@pytest.mark.parametrize("name", ["h3_pro", "one_komma_five"])
def test_two_packs_are_weighted_by_the_energy_each_holds(name: str) -> None:
    # Pack 1: 5 kWh left at 50 % (a 10 kWh pack); pack 2: 4.5 kWh left at 90 % (a 5 kWh pack). 9.5 of 15 kWh.
    found = facts(
        name,
        {"battery_soc_1": "50", "bms_kwh_remaining_1": "5.0", "battery_soc_2": "90", "bms_kwh_remaining_2": "4.5"},
    )

    assert found.soc_pct == pytest.approx(63.3)
    assert found.sources["soc"] == "sensor.battery_soc_1"
    assert found.sources["soc_packs"] == "sensor.battery_soc_1, sensor.battery_soc_2"


def test_two_packs_without_their_energy_or_nearly_empty_are_the_plain_mean() -> None:
    no_energy = facts("h3_pro", {"battery_soc_1": "50", "battery_soc_2": "90"})
    nearly_empty = facts(
        "h3_pro",
        {"battery_soc_1": "4", "bms_kwh_remaining_1": "0.4", "battery_soc_2": "8", "bms_kwh_remaining_2": "0.4"},
    )

    assert no_energy.soc_pct == 70.0
    assert nearly_empty.soc_pct == 6.0


@pytest.mark.parametrize("second", ["unknown", "unavailable", None])
def test_a_site_with_one_pack_reads_that_pack(second: str | None) -> None:
    states = {"battery_soc_1": "42", "bms_kwh_remaining_1": "4.2"}
    if second is not None:
        states["battery_soc_2"] = second

    found = facts("one_komma_five", states)

    assert found.soc_pct == 42.0 and found.sources["soc_packs"] == "sensor.battery_soc_1"


def test_no_pack_that_reads_a_level_is_no_level() -> None:
    found = facts("h3_pro", {"battery_soc_1": "unknown", "battery_soc_2": "unknown"})

    assert found.soc_pct is None and "soc" not in found.sources


def test_the_1k5_reads_the_h3_smart_floor_and_mode_and_has_no_size_or_rating() -> None:
    found = facts(
        "one_komma_five",
        {
            "battery_soc_1": "80",
            "battery_soc_2": "80",
            "number.min_soc_on_grid": "10",
            "select.work_mode": "Self Use",
            "master_version": "1.02",
        },
    )

    assert found.soc_pct == 80.0 and found.reserve_soc_pct == 10.0 and found.firmware == "1.02"
    assert found.capacity_kwh is None and found.rated_charge_kw is None and found.rated_discharge_kw is None
    assert found.management.state == "free"


@pytest.mark.parametrize("mode", ["Force Charge", "Force Discharge", "Feed-in First", "Back-up", "Peak Shaving"])
def test_any_other_work_mode_is_run_by_itself(mode: str) -> None:
    found = facts("h3_pro", {"battery_soc_1": "50", "select.work_mode": mode})

    assert found.management.state == "self_managed" and found.management.settings == {"work mode": mode}


def test_a_family_without_a_work_mode_is_not_unknown() -> None:
    # The H1 LAN and the early AIO-H3 have no work mode select.
    found = facts("h1_g2", {"battery_soc": "50"}, drop=("work_mode",))

    assert found.management.state == "free" and found.management.settings == {}


@pytest.mark.parametrize("name", FAMILIES)
def test_foxess_is_read_only(name: str) -> None:
    found = facts(name, {"battery_soc": "50", "battery_soc_1": "50"})

    assert found.hold is None and found.grid_charge is None
    assert all(control.brand != "foxess" for control in CONTROLS)


@pytest.mark.parametrize("name", FAMILIES)
def test_the_plan_waits_for_a_learnt_size_on_every_family(name: str) -> None:
    found = facts(name, {"battery_soc": "50", "battery_soc_1": "50", "battery_soc_2": "50"})

    params, reason, _estimates = battery_params(found)
    assert params is None and reason == "no_capacity"


def test_the_adapter_is_found_by_the_integration_alone() -> None:
    assert adapter_for(PLATFORM, "FoxESS").brand == "foxess"
    assert adapter_for(PLATFORM, None).brand == "foxess"
