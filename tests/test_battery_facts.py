"""The home battery's facts per brand, read from fixture registries (`tests/fixtures/battery/brands.json`).

Each brand's entities are found by the integration and the key in the unique id, never by entity id; a brand
without an adapter gets its level from a battery sensor on its device and no controls.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from custom_components.spotnav.battery.adapters import ADAPTERS, adapter_for
from custom_components.spotnav.battery.facts import EntityFact, battery_facts, to_kw, to_kwh

BRANDS: dict[str, dict[str, Any]] = json.loads(
    (Path(__file__).parent / "fixtures" / "battery" / "brands.json").read_text(encoding="utf-8")
)


def _entities(brand: dict[str, Any], **changes: dict[str, Any]) -> list[EntityFact]:
    entities = []
    for row in brand["entities"]:
        row = row | changes.get(row["entity_id"], {})
        entities.append(
            EntityFact(
                entity_id=row["entity_id"],
                platform=brand["platform"],
                unique_id=row["unique_id"],
                device_id=row["device"],
                config_entry_id="entry",
                disabled=row["disabled"],
                device_class=row["device_class"],
                state=row["state"],
                unit=row["unit"],
                step=row.get("step"),
            )
        )
    return entities


def _facts(name: str, **changes: dict[str, Any]):
    brand = BRANDS[name]
    entities = _entities(brand, **changes)
    battery = next(entity for entity in entities if entity.entity_id == brand["battery"])
    return battery_facts(battery, entities, manufacturer=brand["manufacturer"], power_kw=-1.0)


def test_every_adapter_has_a_fixture() -> None:
    assert {adapter.brand for adapter in ADAPTERS} == set(BRANDS)


@pytest.mark.parametrize("name", sorted(BRANDS))
def test_each_brand_reads_its_facts(name: str) -> None:
    expected = BRANDS[name]["expected"]

    facts = _facts(name)

    assert facts.brand == expected["brand"]
    assert facts.soc_pct == expected["soc_pct"]
    if expected["capacity_kwh"] is None:
        assert facts.capacity_kwh is None
    else:
        assert facts.capacity_kwh == pytest.approx(expected["capacity_kwh"])
    assert facts.rated_charge_kw == expected["rated_charge_kw"]
    assert facts.rated_discharge_kw == expected["rated_discharge_kw"]
    assert facts.on_grid is expected["on_grid"]
    assert facts.management.state == expected["management"]
    assert facts.power_kw == -1.0
    hold = facts.hold
    if expected["hold_entity"] is None:
        assert hold is None
    else:
        assert hold is not None and hold.entity_id == expected["hold_entity"]
        assert hold.usable is expected["hold_usable"]
        assert hold.unset is expected["hold_unset"]
    if expected["grid_charge_usable"] is None:
        assert facts.grid_charge is None
    else:
        assert facts.grid_charge is not None and facts.grid_charge.usable is expected["grid_charge_usable"]
    # A fixture's diagnostics form is plain data.
    json.dumps(facts.as_dict())


def test_sigenergy_in_ai_mode_is_its_own_and_says_why() -> None:
    facts = _facts("sigenergy")

    assert facts.management.state == "self_managed"
    assert facts.management.reason == "EMS work mode:ai_mode"
    assert facts.firmware == "V100R001C21SPC117"
    assert facts.sources["soc"] == "sensor.sigen_plant_battery_state_of_charge"


def test_the_hold_control_carries_its_unit_and_step_for_reading_a_write_back() -> None:
    facts = _facts("sigenergy", **{"number.sigen_plant_ess_max_discharging_limit": {"step": 0.01}})

    assert facts.hold is not None and facts.hold.unit == "kW" and facts.hold.step == 0.01
    assert facts.as_dict()["controls"]["hold"]["step"] == 0.01


def test_sigenergy_in_max_self_consumption_is_free() -> None:
    facts = _facts("sigenergy", **{"sensor.sigen_plant_ems_work_mode": {"state": "max_self_consumption"}})

    assert facts.management.state == "free"


@pytest.mark.parametrize("state", ["tou", "custom", "remote_ems", "vpp_scheduling", "full_feed_in_to_grid"])
def test_sigenergy_runs_itself_in_every_other_mode(state: str) -> None:
    facts = _facts("sigenergy", **{"sensor.sigen_plant_ems_work_mode": {"state": state}})

    assert facts.management.state == "self_managed"


def test_a_mode_that_cannot_be_read_is_unknown() -> None:
    facts = _facts("sigenergy", **{"sensor.sigen_plant_ems_work_mode": {"state": "unavailable"}})

    assert facts.management.state == "unknown"


def test_sigenergy_unset_sentinel_and_a_real_limit() -> None:
    sentinel = _facts("sigenergy", **{"number.sigen_plant_ess_max_discharging_limit": {"state": "4294967.295"}})
    real = _facts("sigenergy", **{"number.sigen_plant_ess_max_discharging_limit": {"state": "4.8"}})

    assert sentinel.hold is not None and sentinel.hold.unset is True
    assert real.hold is not None and real.hold.unset is False and real.hold.current_kw == 4.8


def test_a_disabled_control_is_found_but_not_usable() -> None:
    facts = _facts("sigenergy", **{"number.sigen_plant_ess_max_discharging_limit": {"disabled": True}})

    assert facts.hold is not None and facts.hold.found and not facts.hold.usable
    assert facts.hold.why_not == "control_disabled"


def test_off_grid_is_read() -> None:
    facts = _facts("sigenergy", **{"sensor.sigen_plant_grid_connection_status": {"state": "off_grid_auto"}})

    assert facts.on_grid is False


def test_a_renamed_entity_is_still_found() -> None:
    facts = _facts("sigenergy", **{"sensor.sigen_plant_battery_state_of_charge": {"entity_id": "sensor.my_battery"}})

    assert facts.soc_pct == 44.6 and facts.sources["soc"] == "sensor.my_battery"


def test_a_second_plant_in_the_entry_does_not_confuse_the_first() -> None:
    brand = BRANDS["sigenergy"]
    entities = _entities(brand) + [
        EntityFact(
            "sensor.sigen_plant_2_battery_state_of_charge", "sigen", "ENTRY_sigen_plant_2_plant_ess_soc",
            device_id="plant2", state="12", unit="%",
        )
    ]
    battery = next(entity for entity in entities if entity.entity_id == brand["battery"])

    assert battery_facts(battery, entities, manufacturer="Sigenergy").soc_pct == 44.6


def test_solax_reads_the_soc_not_the_bms_capacity_and_needs_its_own_maker() -> None:
    facts = _facts("solax")

    assert facts.sources["soc"] == "sensor.solax_battery_capacity"
    assert adapter_for("solax_modbus", "Sofar") is None


def test_solax_under_another_remote_control_is_not_free() -> None:
    facts = _facts("solax", **{"select.solax_remotecontrol_power_control": {"state": "Enabled Power Control"}})

    assert facts.management.state == "self_managed"


def test_victron_without_dynamic_ess_follows_the_ess_mode() -> None:
    brand = BRANDS["victron"]
    entities = [e for e in _entities(brand) if e.entity_id != "select.victron_settings_dess_mode"]
    battery = next(entity for entity in entities if entity.entity_id == brand["battery"])

    assert battery_facts(battery, entities).management.state == "free"


def test_solaredge_in_remote_control_offers_its_limit() -> None:
    facts = _facts(
        "solaredge",
        **{
            "select.solaredge_i1_storage_control_mode": {"state": "Remote Control"},
            "number.solaredge_i1_storage_discharge_limit": {"state": "5000"},
        },
    )

    assert facts.management.state == "free"
    assert facts.hold is not None and facts.hold.usable and facts.hold.unset is True


def test_an_external_planner_runs_the_battery_whatever_its_mode() -> None:
    brand = BRANDS["huawei"]
    entities = _entities(brand)
    battery = next(entity for entity in entities if entity.entity_id == brand["battery"])

    facts = battery_facts(battery, entities, external_planner="predbat")

    assert facts.management.state == "self_managed" and facts.management.reason == "planner:predbat"


def test_an_unknown_brand_reads_its_level_by_device_class_and_has_no_controls() -> None:
    battery = EntityFact("sensor.box_power", "acme", "acme_1_power", device_id="box", state="-300", unit="W")
    soc = EntityFact("sensor.box_level", "acme", "acme_1_level", device_id="box", device_class="battery", state="77", unit="%")
    other = EntityFact("sensor.phone_level", "acme", "acme_2_level", device_id="phone", device_class="battery", state="12", unit="%")

    facts = battery_facts(battery, [battery, other, soc], power_kw=-0.3)

    assert facts.brand is None and facts.soc_pct == 77.0 and facts.power_kw == -0.3
    assert facts.hold is None and facts.grid_charge is None
    assert facts.management.state == "unknown" and facts.management.reason == "no_adapter"


def test_no_battery_has_no_facts() -> None:
    facts = battery_facts(None, [])

    assert facts.battery_entity_id is None and facts.management.reason == "no_battery"


@pytest.mark.parametrize(
    ("state", "unit", "kw"),
    [("1500", "W", 1.5), ("1.5", "kW", 1.5), ("1.5", None, 1.5), ("nan", "W", None), ("x", "W", None), ("1", "A", None)],
)
def test_power_units(state: str, unit: str | None, kw: float | None) -> None:
    assert to_kw(state, unit) == kw


def test_an_amp_hour_capacity_is_no_energy() -> None:
    assert to_kwh("200", "Ah") is None and to_kwh("9700", "Wh") == pytest.approx(9.7)


def test_sigenergy_reads_the_plants_maximum_below_the_battery_rating() -> None:
    facts = _facts("sigenergy")

    assert facts.plant_max_kw == 13.2 and facts.rated_discharge_kw == 23.04
    assert facts.max_discharge_kw == 13.2 and facts.max_charge_kw == 13.2
    assert facts.sources["plant_max"] == "sensor.sigen_plant_max_active_power"
    # Its reserves are disabled by default, as on the real plant: none is read, the plan keeps its estimate.
    assert facts.reserve_soc_pct is None and "reserve" not in facts.sources
    assert facts.as_dict()["plant_max_kw"] == 13.2


def test_the_highest_reserve_that_can_be_read_is_the_floor() -> None:
    facts = _facts(
        "sigenergy",
        **{
            "sensor.sigen_plant_discharge_cut_off_soc": {"disabled": False, "state": "5.0", "unit": "%"},
            "number.sigen_plant_ess_backup_state_of_charge": {"disabled": False, "state": "15.0", "unit": "%"},
            "number.sigen_plant_ess_discharge_cut_off_state_of_charge": {"disabled": False, "state": "unavailable"},
        },
    )

    assert facts.reserve_soc_pct == 15.0
    assert facts.sources["reserve"] == "sensor.sigen_plant_discharge_cut_off_soc, number.sigen_plant_ess_backup_state_of_charge"


def test_a_plant_maximum_that_cannot_be_read_leaves_the_rating() -> None:
    facts = _facts("sigenergy", **{"sensor.sigen_plant_max_active_power": {"state": "unavailable"}})

    assert facts.plant_max_kw is None and facts.max_discharge_kw == 23.04 and facts.max_charge_kw == 20.6


def test_goodwe_states_the_depth_of_discharge() -> None:
    from custom_components.spotnav.battery.facts import _reserve  # noqa: PLC0415
    from custom_components.spotnav.battery.adapters import GOODWE  # noqa: PLC0415

    battery = EntityFact("sensor.battery", "goodwe", "goodwe-battery_soc-SN1", device_id="d")
    depth = EntityFact(
        "number.depth", "goodwe", "goodwe-battery_discharge_depth-SN1", device_id="d", state="85", unit="%"
    )
    assert _reserve(GOODWE.reserves, [battery, depth], battery) == (15.0, ["number.depth"])
