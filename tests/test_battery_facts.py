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
from custom_components.spotnav.battery.gate import firmware_key, firmware_known

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
                platform=row.get("platform", brand["platform"]),
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


def test_sigenergy_2_0_1_shows_a_never_set_limit_as_unknown_which_is_no_limit_and_never_zero() -> None:
    facts = _facts("sigenergy", **{"number.sigen_plant_ess_max_discharging_limit": {"state": "unknown"}})

    hold = facts.hold
    assert hold is not None and hold.unset is True and hold.current_kw is None
    assert hold.usable and hold.current == "unknown"
    # Unavailable stays no reading at all: not unset, not usable.
    gone = _facts("sigenergy", **{"number.sigen_plant_ess_max_discharging_limit": {"state": "unavailable"}}).hold
    assert gone is not None and gone.unset is None and gone.current_kw is None and not gone.usable


def test_unknown_is_no_reading_for_a_brand_that_does_not_say_otherwise() -> None:
    facts = _facts("solaredge", **{"number.solaredge_i1_storage_discharge_limit": {"state": "unknown"}})

    assert facts.hold is not None and facts.hold.unset is None and facts.hold.current_kw is None


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
    assert adapter_for("solax_modbus", "Ginlog Solis") is None and adapter_for("solax_modbus", None) is None
    assert adapter_for("solax_modbus", "Sofar Solar").brand == "sofar"


def test_solax_under_another_remote_control_is_not_free() -> None:
    facts = _facts("solax", **{"select.solax_remotecontrol_power_control": {"state": "Enabled Power Control"}})

    assert facts.management.state == "self_managed"


def _solax(*extra: EntityFact, sw_version: str | None = None, without: str = ""):
    brand = BRANDS["solax"]
    entities = [*(entity for entity in _entities(brand) if entity.entity_id != without), *extra]
    battery = next(entity for entity in entities if entity.entity_id == brand["battery"])
    return battery_facts(battery, entities, manufacturer=brand["manufacturer"], sw_version=sw_version)


def _solax_entity(key: str, state: str, unit: str | None = None, *, disabled: bool = False) -> EntityFact:
    return EntityFact(
        f"sensor.solax_{key}", "solax_modbus", f"SolaX_{key}", device_id="battery", config_entry_id="entry",
        disabled=disabled, state=state, unit=unit,
    )


def test_solax_without_a_firmware_entity_takes_the_devices_software_version() -> None:
    """Field report SN-7R5Y: the firmware registers are internal in solax_modbus, the version only on the device."""
    facts = _solax(sw_version="DSP v3.05 ARM v3.12")

    assert facts.firmware == "DSP v3.05 ARM v3.12" and facts.sources["firmware"] == "device sw_version"
    key = firmware_key(facts.firmware, facts.integration, "2026.11.04")
    assert key == "DSP v3.05 ARM v3.12 | solax_modbus 2026.11.04" and firmware_known(key)
    # The same string after a restart is the same key.
    assert firmware_key(_solax(sw_version="DSP v3.05 ARM v3.12").firmware, "solax_modbus", "2026.11.04") == key


@pytest.mark.parametrize("sw_version", [None, "", "   ", "unknown", "unavailable"])
def test_no_software_version_on_the_device_leaves_the_firmware_unknown(sw_version: str | None) -> None:
    facts = _solax(sw_version=sw_version)

    assert facts.firmware is None and "firmware" not in facts.sources
    assert not firmware_known(firmware_key(facts.firmware, facts.integration, "2026.11.04"))


def test_the_firmware_entity_comes_before_the_devices_software_version() -> None:
    facts = _solax(_solax_entity("software_version", "DSP v3.05 ARM v3.12"), sw_version="something else")

    assert facts.firmware == "DSP v3.05 ARM v3.12" and facts.sources["firmware"] == "sensor.solax_software_version"
    # An entity that reads nothing is no firmware; the device's version is.
    blank = _solax(_solax_entity("software_version", "unavailable"), sw_version="DSP v3.05 ARM v3.12")
    assert blank.firmware == "DSP v3.05 ARM v3.12"


def test_every_adapter_falls_back_on_the_devices_software_version() -> None:
    brand = BRANDS["sofar"]
    entities = _entities(brand)
    battery = next(entity for entity in entities if entity.entity_id == brand["battery"])

    facts = battery_facts(battery, entities, manufacturer=brand["manufacturer"], sw_version=" V1.2 ")

    assert facts.firmware == "V1.2"
    # Sigenergy's own firmware entity is still read first.
    sigen = BRANDS["sigenergy"]
    entities = _entities(sigen)
    battery = next(entity for entity in entities if entity.entity_id == sigen["battery"])
    assert battery_facts(battery, entities, manufacturer="Sigenergy", sw_version="other").firmware == "V100R001C21SPC117"


def test_solax_gen4_reads_its_installed_capacity_in_wh() -> None:
    # A GEN4 inverter has no GEN3 install capacity.
    gen3 = "sensor.solax_battery_install_capacity"
    facts = _solax(_solax_entity("battery_system_installed_capacity", "11520", "Wh"), without=gen3)

    assert facts.capacity_kwh == pytest.approx(11.52)
    assert facts.sources["capacity"] == "sensor.solax_battery_system_installed_capacity"
    # The BMS' total energy (battery 1 only on GEN5/6) is never the size.
    assert _solax(without=gen3).capacity_kwh is None


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

    assert facts.management.state == "externally_controlled" and facts.management.reason == "planner:predbat"


def test_an_unknown_brand_reads_its_level_by_device_class_and_has_no_controls() -> None:
    battery = EntityFact("sensor.box_power", "acme", "acme_1_power", device_id="box", state="-300", unit="W")
    soc = EntityFact("sensor.box_level", "acme", "acme_1_level", device_id="box", device_class="battery", state="77", unit="%")
    other = EntityFact("sensor.phone_level", "acme", "acme_2_level", device_id="phone", device_class="battery", state="12", unit="%")

    facts = battery_facts(battery, [battery, other, soc], power_kw=-0.3)

    assert facts.brand is None and facts.soc_pct == 77.0 and facts.power_kw == -0.3
    assert facts.hold is None and facts.grid_charge is None
    assert facts.management.state == "unknown" and facts.management.reason == "no_adapter"


def _car_or_phone(platform: str, uid: str, **extra: EntityFact) -> list[EntityFact]:
    power = EntityFact(f"sensor.{uid}_power", platform, f"{uid}_battery_power", device_id="dev", state="1200", unit="W")
    level = EntityFact(
        f"sensor.{uid}_level", platform, f"{uid}_battery_level", device_id="dev", device_class="battery", state="64",
        unit="%",
    )
    return [power, level, *extra.values()]


@pytest.mark.parametrize(
    ("platform", "uid", "extra"),
    [
        # The Android companion app: "Battery power" (W) beside the phone's level.
        ("mobile_app", "pixel", {}),
        # Car integrations: a power and the car's level on the car's device.
        ("mg_saic", "mg4", {}),
        ("volkswagen_we_connect_id", "id4", {}),
        ("octopus_energy", "intelligent", {}),
        # TeslaMate's MQTT sensors, by their names.
        ("mqtt", "teslamate_1", {}),
        # Tibber's Data API puts a car's level (`storage.stateOfCharge`) on its device, as it does a home battery's;
        # a car's device also says where it is.
        ("tibber", "car", {"tracker": EntityFact("device_tracker.car", "tibber", "car_location", device_id="dev")}),
        # A Tesla integration holds Powerwalls too: the car's range on its device tells it apart.
        ("tessie", "model_y", {"range": EntityFact(
            "sensor.model_y_range", "tessie", "vin_range", device_id="dev", device_class="distance", state="300",
        )}),
    ],
)
def test_a_cars_or_a_phones_battery_level_is_never_the_house_batterys(
    platform: str, uid: str, extra: dict[str, EntityFact]
) -> None:
    entities = _car_or_phone(platform, uid, **extra)
    facts = battery_facts(entities[0], entities, power_kw=1.2)
    assert facts.soc_pct is None and "soc" not in facts.sources


def test_a_home_battery_of_a_shared_integration_still_reads_its_level() -> None:
    """Tibber's Data API home battery (a Homevolt) and Tesla Custom's energy site: no place, no range, a level."""
    for platform in ("tibber", "tesla_custom"):
        entities = _car_or_phone(platform, "home")
        assert battery_facts(entities[0], entities, power_kw=1.2).soc_pct == 64.0


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
    # The effective limits the plan uses are in the diagnostics too, not only the raw rating and the plant maximum.
    assert facts.as_dict()["max_charge_kw"] == 13.2 and facts.as_dict()["max_discharge_kw"] == 13.2


def test_sigenergy_finds_the_plants_per_phase_power_not_the_grid_sensors() -> None:
    facts = _facts("sigenergy")

    assert facts.plant_phase_entities == (
        "sensor.sigen_plant_phase_a_active_power",
        "sensor.sigen_plant_phase_b_active_power",
        "sensor.sigen_plant_phase_c_active_power",
    )
    # Positive is the plant's output (its integration's reading as it is).
    assert facts.plant_phase_sign == 1.0
    assert facts.sources["plant_phase_power"].startswith("sensor.sigen_plant_phase_a_active_power, ")


def test_the_plants_per_phase_power_needs_all_three_phases_enabled() -> None:
    disabled = _facts("sigenergy", **{"sensor.sigen_plant_phase_b_active_power": {"disabled": True, "state": None}})
    assert disabled.plant_phase_entities is None and "plant_phase_power" not in disabled.sources
    # An entity that is there but has no reading now is still the one to read: its value is read live, and an
    # unknown reading is no value there.
    unknown = _facts("sigenergy", **{"sensor.sigen_plant_phase_c_active_power": {"state": "unknown"}})
    assert unknown.plant_phase_entities is not None
    # A brand whose adapter knows no per-phase power has none.
    assert _facts("huawei").plant_phase_entities is None


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


# ---- what the shadow plan needs from an adapter (W8-D) ----------------------------------------------------------------

#: The plan needs the level, the size and both rated powers (`plan_shadow.battery_params`); each fixture's first
#: reason. SolaX's fixture has no install capacity (the entity exists on a real one, but it has no rated power either).
PLAN_REASON = {
    "sigenergy": None, "huawei": None, "solaredge": None, "victron": None, "victron_modbus": None,
    "solaredge_modbus": None, "solax": "no_capacity", "goodwe": "no_capacity", "enphase": "no_rated_power", "tesla": "no_rated_power",
    # Ferroamp's hub states the system's size, not its power (the ESOs' ratings are per device).
    "ferroamp": "no_rated_power",
    # The other `solax_modbus` plugins: Sofar and Growatt state no size (learnt), Solinteg states all the plan needs.
    "sofar": "no_capacity", "growatt": "no_capacity", "solinteg": None,
    # FoxESS states no size on any family (learnt).
    "foxess": "no_capacity",
    # Solis states no size the BMS stands behind (its Ah number is a setting): learnt.
    "solis": "no_capacity",
    # Marstek states its size (rated capacity) but no rating on either integration: learnt.
    "marstek": "no_rated_power",
    # EcoFlow states its size (Modbus) but no rating: the BMS's limits move with the moment, so they are learnt.
    "ecoflow": "no_rated_power",
    # Kostal's core integration states no size and no rating (the Modbus one states a size, no rating).
    "kostal": "no_capacity",
    # Fronius states all the plan needs where the inverter accepts control over Modbus (the rating is WChaMax).
    "fronius": None,
    # Homevolt reads the level only: no size, no rating, no counters (see the adapter).
    "homevolt": "no_capacity",
    # Deye states a size only where `solarman` has learnt one, and no rating (the current limits are settings): learnt.
    "deye": "no_rated_power",
    # Anker's local integration states the size and no rating (the limits are internal to it): learnt.
    "anker": "no_rated_power",
    # Zendure states the size and the output's rating but not the charge rating (a raw sensor with no unit): learnt.
    "zendure": "no_rated_power",
    # Sungrow's package states the size and the converter's rating: the plan has all it needs.
    "sungrow": None,
    # Omnibattery states the system's level and size and no rating: learnt.
    "omnibattery": "no_rated_power",
    # sonnen states the rating (`max_power`) but its size sensor is disabled by default: learnt until it is enabled.
    "sonnen": "no_capacity",
}


@pytest.mark.parametrize("name", sorted(BRANDS))
def test_the_shadow_plan_runs_for_a_brand_exactly_when_the_adapter_reads_what_it_needs(name: str) -> None:
    from custom_components.spotnav.battery.plan_shadow import battery_params

    params, reason, _estimates = battery_params(_facts(name))

    assert reason == PLAN_REASON[name]
    assert (params is not None) is (PLAN_REASON[name] is None)


def test_victron_size_and_ratings_are_amp_hours_and_amperes_times_the_system_voltage_and_say_so() -> None:
    facts = _facts("victron")

    assert facts.capacity_kwh == pytest.approx(10.24)
    assert facts.rated_charge_kw == pytest.approx(5.12)
    assert facts.rated_discharge_kw == pytest.approx(7.68)
    assert facts.reserve_soc_pct == 20.0
    assert facts.estimates == ("capacity_from_ah", "rated_from_current")
    assert facts.sources["capacity"].endswith("x sensor.victron_system_battery_voltage")
    assert facts.as_dict()["estimates"] == ["capacity_from_ah", "rated_from_current"]


def test_victron_without_a_can_bms_has_a_size_but_no_ratings() -> None:
    gone = {"sensor.victron_battery_max_charge_current": {"state": "unavailable"}}

    facts = _facts("victron", **gone)

    assert facts.capacity_kwh == pytest.approx(10.24) and facts.rated_charge_kw is None
    assert facts.rated_discharge_kw == pytest.approx(7.68)


def test_victron_without_a_voltage_reads_no_energy_and_no_power() -> None:
    facts = _facts("victron", **{"sensor.victron_system_battery_voltage": {"state": "unavailable"}})

    assert facts.capacity_kwh is None and facts.rated_charge_kw is None and facts.estimates == ()


def test_a_voltage_that_is_no_battery_voltage_is_not_used() -> None:
    facts = _facts("victron", **{"sensor.victron_system_battery_voltage": {"state": "0.4"}})

    assert facts.capacity_kwh is None


def test_victron_modbus_prefers_the_dynamic_ess_size_in_kwh_and_reads_the_a_labelled_ah_register_otherwise() -> None:
    facts = _facts("victron_modbus")

    assert facts.capacity_kwh == 15.0 and facts.estimates == ("rated_from_current",)
    assert facts.rated_charge_kw == pytest.approx(4.0) and facts.rated_discharge_kw == pytest.approx(5.0)
    unset = _facts("victron_modbus", **{"sensor.victron_settings_dynamicess_batterycapacity": {"state": "0"}})
    assert unset.capacity_kwh == pytest.approx(15.0) and "capacity_from_ah" in unset.estimates
    assert unset.sources["capacity"].startswith("sensor.victron_battery_capacity x ")


def test_victron_modbus_without_the_advanced_selects_is_unknown_and_left_alone() -> None:
    facts = _facts("victron_modbus")

    assert facts.management.state == "unknown" and facts.management.reason == "ESS mode:not_found"


def test_victron_modbus_in_self_consumption_is_free() -> None:
    brand = BRANDS["victron_modbus"]
    entities = _entities(brand) + [
        EntityFact("select.victron_ess_mode", "victron", "100_settings_ess_mode", device_id="system", state="SELF_CONSUMPTION"),
        EntityFact("select.victron_dess", "victron", "100_settings_dynamicess_mode", device_id="system", state="AUTO"),
    ]
    battery = next(entity for entity in entities if entity.entity_id == brand["battery"])

    assert battery_facts(battery, entities).management.state == "self_managed"
    entities[-1] = EntityFact("select.victron_dess", "victron", "100_settings_dynamicess_mode", device_id="system", state="OFF")
    assert battery_facts(battery, entities).management.state == "free"


def test_goodwe_reads_its_ratings_from_the_bms_limits_but_no_size_and_so_no_plan() -> None:
    facts = _facts("goodwe")

    assert facts.capacity_kwh is None
    assert facts.rated_charge_kw == pytest.approx(2.56) and facts.rated_discharge_kw == pytest.approx(3.072)
    assert facts.estimates == ("rated_from_current",)


def test_the_plan_carries_the_adapters_estimates_on() -> None:
    from custom_components.spotnav.battery.plan_runtime import limited_params
    from custom_components.spotnav.battery.plan_shadow import battery_params

    facts = _facts("victron")

    _params, _reason, estimates = limited_params(*battery_params(facts), facts)

    assert {"capacity_from_ah", "rated_from_current", "round_trip_assumed"} <= set(estimates)


def test_sigenergy_finds_the_plants_cell_temperature_else_the_inverters() -> None:
    facts = _facts("sigenergy")
    assert facts.cell_temperature_entity == "sensor.sigen_plant_ess_average_cell_temperature"
    assert facts.sources["cell_temperature"] == "sensor.sigen_plant_ess_average_cell_temperature"
    # Register 30286 needs firmware V2.8: without the plant's, the inverter's own.
    older = _facts("sigenergy", **{"sensor.sigen_plant_ess_average_cell_temperature": {"disabled": True}})
    assert older.cell_temperature_entity == "sensor.sigen_inverter_battery_average_cell_temperature"
    assert _facts("huawei").cell_temperature_entity is None


def _with_counters(name: str, charged: dict[str, Any], discharged: dict[str, Any]):
    brand = BRANDS[name]
    rows = [{"device": "battery", "device_class": "energy", "disabled": False} | row for row in (charged, discharged)]
    entities = _entities(brand) + _entities({"platform": brand["platform"], "entities": rows})
    battery = next(entity for entity in entities if entity.entity_id == brand["battery"])
    return battery_facts(battery, entities, manufacturer=brand["manufacturer"], power_kw=-1.0)


def test_the_lifetime_counters_are_read_in_kwh_where_the_brand_has_them() -> None:
    sigen = _with_counters(
        "sigenergy",
        {"entity_id": "sensor.sigen_charged", "unique_id": "ENTRY_sigen_plant_plant_accumulated_battery_charge_energy",
         "state": "1234.5", "unit": "kWh"},
        {"entity_id": "sensor.sigen_discharged",
         "unique_id": "ENTRY_sigen_plant_plant_accumulated_battery_discharge_energy", "state": "1.1", "unit": "MWh"},
    )
    assert sigen.charged_total_kwh == pytest.approx(1234.5)
    assert sigen.discharged_total_kwh == pytest.approx(1100.0)
    assert sigen.sources["charged_total"] == "sensor.sigen_charged"

    solaredge = _with_counters(
        "solaredge",
        {"entity_id": "sensor.b1_in", "unique_id": "SE10K_7E1234_B1_energy_import", "state": "500000", "unit": "Wh"},
        {"entity_id": "sensor.b1_out", "unique_id": "SE10K_7E1234_B1_energy_export", "state": "450000", "unit": "Wh"},
    )
    assert (solaredge.charged_total_kwh, solaredge.discharged_total_kwh) == (500.0, 450.0)

    goodwe = _with_counters(
        "goodwe",
        {"entity_id": "sensor.gw_in", "unique_id": "goodwe-e_bat_charge_total-9010KETU000W0000", "state": "80.2",
         "unit": "kWh"},
        {"entity_id": "sensor.gw_out", "unique_id": "goodwe-e_bat_discharge_total-9010KETU000W0000", "state": "unavailable",
         "unit": "kWh"},
    )
    assert goodwe.charged_total_kwh == pytest.approx(80.2) and goodwe.discharged_total_kwh is None
    json.dumps(goodwe.as_dict())


def test_growatt_has_no_counters_its_charge_counter_is_only_from_the_grid() -> None:
    adapter = adapter_for("solax_modbus", "Growatt New Energy")
    assert adapter is not None and "charged_total" not in adapter.facts
    assert _facts("growatt").charged_total_kwh is None


def test_solax_rated_power_is_the_bms_currents_times_the_battery_voltage() -> None:
    facts = _solax(
        _solax_entity("bms_charge_max_current", "25", "A"),
        _solax_entity("bms_discharge_max_current", "30", "A"),
        _solax_entity("battery_voltage_charge", "200", "V"),
    )

    assert facts.rated_charge_kw == pytest.approx(5.0) and facts.rated_discharge_kw == pytest.approx(6.0)
    assert facts.sources["rated_charge"] == "sensor.solax_bms_charge_max_current x sensor.solax_battery_voltage_charge"
    assert facts.estimates == ("rated_from_current",)
    # GEN5/6 name the voltage for battery 1; the person's own setting number is not the BMS' limit and is not read.
    gen5 = _solax(
        _solax_entity("bms_charge_max_current", "25", "A"),
        _solax_entity("battery_1_voltage_charge", "200", "V"),
        EntityFact("number.solax_battery_charge_max_current", "solax_modbus", "SolaX_battery_charge_max_current",
                   device_id="battery", config_entry_id="entry", state="20", unit="A"),
    )
    assert gen5.rated_charge_kw == pytest.approx(5.0) and gen5.rated_discharge_kw is None


def test_solax_without_the_bms_currents_or_with_them_disabled_reads_no_rated_power() -> None:
    assert _solax().rated_charge_kw is None
    disabled = _solax(
        _solax_entity("bms_charge_max_current", "25", "A", disabled=True),
        _solax_entity("bms_discharge_max_current", "30", "A", disabled=True),
        _solax_entity("battery_voltage_charge", "200", "V"),
    )
    assert disabled.rated_charge_kw is None and disabled.rated_discharge_kw is None
