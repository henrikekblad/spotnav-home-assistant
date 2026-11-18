"""The Sofar, Growatt and Solinteg plugins of `solax_modbus` in the site detection table and the battery adapters.

Each fixture (`tests/fixtures/solax_plugins/`) is the whole entity set one inverter family gets, built from the
plugin's source at the commit its `_source` names: every entity description whose inverter-type mask matches the
family. So a row is checked against everything the plugin registers, not only the keys it is meant to find, and
against the other plugins' devices, which share the integration and several keys.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from custom_components.spotnav.battery.adapters import adapter_for
from custom_components.spotnav.battery.facts import EntityFact, battery_facts
from custom_components.spotnav.const import MEASUREMENT_MODE_DERIVED
from custom_components.spotnav.site.measurement_source import GridPowerSource
from custom_components.spotnav.site.site_detection import WARNING_SIGN_UNVERIFIED, freshness_warnings, UPDATE_BEHAVIOUR

from .test_site_detection import one_meter, Registry, roles
from .test_site_detection_round2 import solax_battery

FIXTURES = Path(__file__).parent / "fixtures" / "solax_plugins"
PLATFORM = "solax_modbus"


def fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def _object_id(hub: str, name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", f"{hub} {name}".lower()).strip("_")


def registry(name: str, *, manufacturer: str | None = None, into: Registry | None = None) -> Registry:
    """The fixture's sensors as the registry holds them: unique id `<hub name>_<key>`, one device per hub."""
    data = fixture(name)
    r = into or Registry()
    device = f"{name}_inverter"
    maker = data["manufacturer"] if manufacturer is None else manufacturer
    r.device(device, model=data["model"], manufacturer=maker or None, name=data["hub"], entry=name)
    for entity in data["entities"]:
        if entity["domain"] != "sensor":
            continue
        r.add(
            PLATFORM,
            _object_id(data["hub"], entity["name"]),
            f"{data['hub']}_{entity['key']}",
            device_class=entity["device_class"],
            unit=entity["unit"],
            device=device,
            entry=name,
            disabled=not entity["enabled"],
            original_name=entity["name"],
        )
    return r


def facts(name: str, battery_key: str, states: dict[str, str], *, enable: tuple[str, ...] = ()):
    """The battery facts of a fixture whose entities read `states` (by `<domain>.<key>` or by key for a sensor)."""
    data = fixture(name)
    entities = []
    for entity in data["entities"]:
        entity_id = f"{entity['domain']}.{_object_id(data['hub'], entity['name'])}"
        state = states.get(f"{entity['domain']}.{entity['key']}", states.get(entity["key"]) if entity["domain"] == "sensor" else None)
        entities.append(
            EntityFact(
                entity_id=entity_id,
                platform=PLATFORM,
                unique_id=f"{data['hub']}_{entity['key']}",
                device_id="inverter",
                config_entry_id="entry",
                disabled=not entity["enabled"] and entity["key"] not in enable,
                device_class=entity["device_class"],
                state=state,
                unit=entity["unit"],
            )
        )
    battery = next(e for e in entities if e.domain == "sensor" and e.unique_id == f"{data['hub']}_{battery_key}")
    return battery_facts(battery, entities, manufacturer=data["manufacturer"], power_kw=1.5)


# ---- Sofar ----------------------------------------------------------------------------------------


def test_the_sofar_meter_is_the_pcc_block_with_its_own_current_and_export_positive_power() -> None:
    meter = one_meter(registry("sofar_hyd_3ph"))

    assert meter.mode == MEASUREMENT_MODE_DERIVED and meter.catalogued
    assert meter.power_inverted is True and meter.signed_current is False and meter.estimated is False
    assert WARNING_SIGN_UNVERIFIED in meter.warnings
    assert meter.derived_entities["L2"] == {
        "power": "sensor.sofar_active_power_pcc_l2",
        "voltage": "sensor.sofar_voltage_l2",
        "current": "sensor.sofar_current_pcc_l2",
    }
    assert meter.grid_power == GridPowerSource(power="sensor.sofar_active_power_pcc_total")
    assert meter.title == "Sofar"


def test_the_sofar_meter_never_reads_the_inverter_output_the_off_grid_output_or_the_split_phase_entities() -> None:
    meter = one_meter(registry("sofar_hyd_3ph"))

    used = {entity_id for ids in roles(meter).values() for entity_id in ids}
    assert not [entity_id for entity_id in used if re.search(r"output|offgrid|off_grid|l[12]n$|line", entity_id)]
    assert len(used) == 10


def test_a_sofar_pv_inverter_with_a_meter_gets_the_same_meter_and_no_battery() -> None:
    detection = registry("sofar_ktlx_pv").detect()

    [meter] = detection.meters
    assert meter.derived_entities["L1"]["power"] == "sensor.sofar_active_power_pcc_l1"
    assert detection.batteries == ()


def test_the_sofar_battery_is_all_inputs_together_and_charge_positive() -> None:
    [battery] = registry("sofar_hyd_3ph").detect().batteries

    assert battery.entity_id == "sensor.sofar_battery_power_total"
    assert battery.inverted is False and battery.discharge_entity_id is None


def test_sofar_reads_the_total_level_the_floor_from_the_depth_of_discharge_and_its_mode() -> None:
    found = facts(
        "sofar_hyd_3ph",
        "battery_power_total",
        {
            "battery_capacity_total": "57",
            "battery_capacity_1": "55",
            "bat_config_depth_of_discharge": "80",
            "bat_config_capacity": "50",
            "select.charger_use_mode": "Self Use",
        },
        enable=("bat_config_depth_of_discharge",),
    )

    assert found.brand == "sofar" and found.label == "Sofar (SolaX Modbus)"
    assert found.soc_pct == 57.0 and found.sources["soc"] == "sensor.sofar_battery_capacity_total"
    assert found.reserve_soc_pct == 20.0
    # The size and the ratings are learnt: the battery settings are one input's, not the plant's.
    assert found.capacity_kwh is None and found.rated_charge_kw is None and found.rated_discharge_kw is None
    assert found.management.state == "free"
    assert found.hold is None and found.grid_charge is None and found.plant_phase_entities is None


def test_sofar_in_another_mode_is_run_by_itself_and_a_disabled_depth_of_discharge_is_no_floor() -> None:
    found = facts(
        "sofar_hyd_3ph",
        "battery_power_total",
        {"battery_capacity_total": "57", "bat_config_depth_of_discharge": "80", "select.charger_use_mode": "Time of Use"},
    )

    assert found.management.state == "self_managed" and found.reserve_soc_pct is None


# ---- Growatt --------------------------------------------------------------------------------------


def test_the_growatt_sph_three_phase_meter_is_an_import_and_an_export_entity_per_phase() -> None:
    meter = one_meter(registry("growatt_sph_tl3"))

    assert meter.mode == MEASUREMENT_MODE_DERIVED and meter.power_inverted is False
    assert meter.derived_entities["L3"] == {
        "power": "sensor.growatt_ac_power_to_user_l3",
        "power_export": "sensor.growatt_ac_power_to_grid_l3",
        "voltage": "sensor.growatt_grid_voltage_l3",
    }
    # No current of the meter's: the inverter's own `grid_current_l1..3` is never the grid's.
    assert meter.estimated is True and "current" not in roles(meter)
    assert meter.grid_power == GridPowerSource(
        power="sensor.growatt_ac_power_to_user_total", power_export="sensor.growatt_ac_power_to_grid_total"
    )
    assert WARNING_SIGN_UNVERIFIED in meter.warnings


@pytest.mark.parametrize("name", ["growatt_mod_tl3_xh", "growatt_sph_single", "growatt_spa_ac", "growatt_spf"])
def test_a_growatt_family_without_the_meter_per_phase_makes_no_meter(name: str) -> None:
    # Totals only (TL3-XH), one phase (SPH, SPA) or off-grid (SPF): the site keeps its own grid meter, and the
    # inverter's phase voltages and currents alone are not one.
    assert registry(name).detect().meters == ()


@pytest.mark.parametrize("name", ["growatt_sph_tl3", "growatt_sph_single", "growatt_mod_tl3_xh"])
def test_a_growatt_hybrid_battery_is_the_combined_power_charge_positive(name: str) -> None:
    [battery] = registry(name).detect().batteries

    hub = fixture(name)["hub"].lower()
    assert battery.entity_id == f"sensor.{hub}_battery_combined_power"
    assert battery.inverted is False and battery.discharge_entity_id is None


def test_a_growatt_ac_coupled_battery_is_the_charge_and_discharge_pair() -> None:
    [battery] = registry("growatt_spa_ac").detect().batteries

    assert battery.entity_id == "sensor.growatt_battery_charge_power"
    assert battery.discharge_entity_id == "sensor.growatt_battery_discharge_power" and battery.inverted is False


def test_the_growatt_off_grid_spf_battery_is_not_read() -> None:
    # Its `battery_power_charge` is SolaX's key with another register, and it has no charge power of the pair's name.
    assert registry("growatt_spf").detect().batteries == ()


def test_growatt_sph_reads_the_level_and_the_load_first_floor_and_learns_the_rest() -> None:
    found = facts(
        "growatt_sph_tl3",
        "battery_combined_power",
        {
            "battery_soc": "41",
            "battery_soh": "98",
            "number.load_first_battery_minimum_soc": "12",
            "load_first_battery_minimum_soc": "12",
            "grid_first_battery_minimum_soc": "30",
            "select.priority": "Load First",
        },
    )

    assert found.brand == "growatt" and found.soc_pct == 41.0
    assert found.reserve_soc_pct == 12.0 and found.sources["reserve"] == "number.growatt_load_first_battery_minimum_soc"
    assert found.capacity_kwh is None and found.rated_charge_kw is None
    assert found.management.state == "free" and found.hold is None and found.grid_charge is None


def test_growatt_sph_on_battery_first_is_run_by_itself() -> None:
    found = facts("growatt_sph_tl3", "battery_combined_power", {"battery_soc": "41", "select.priority": "Battery First"})

    assert found.management.state == "self_managed" and found.management.settings == {"priority": "Battery First"}


def test_growatt_tl3_xh_reads_the_on_grid_stop_soc_and_not_the_off_grid_one() -> None:
    states = {
        "battery_soc": "73",
        "bms_1_soc": "70",
        "number.ems_discharging_stop_soc_on_grid": "10",
        "number.ems_discharging_stop_soc": "35",
        "number.reserved_soc_peak_shaving": "50",
    }

    found = facts(
        "growatt_mod_tl3_xh", "battery_combined_power", states,
        enable=("ems_discharging_stop_soc_on_grid", "ems_discharging_stop_soc"),
    )

    assert found.soc_pct == 73.0 and found.sources["soc"] == "sensor.solax_battery_soc"
    assert found.reserve_soc_pct == 10.0
    # Both ship disabled: until the person enables the on-grid one there is no floor to read.
    assert facts("growatt_mod_tl3_xh", "battery_combined_power", states).reserve_soc_pct is None


# ---- Solinteg -------------------------------------------------------------------------------------


def test_the_solinteg_meter_is_the_meter_power_per_phase_export_positive_with_an_estimated_current() -> None:
    meter = one_meter(registry("solinteg_mht"))

    assert meter.mode == MEASUREMENT_MODE_DERIVED and meter.power_inverted is True
    assert meter.derived_entities["L1"] == {
        "power": "sensor.solinteg_meter_active_power_l1",
        "voltage": "sensor.solinteg_inverter_voltage_l1",
    }
    # `grid_current_l1..3` is the inverter's own current ("Inverter Current L1"): never the grid's.
    assert meter.estimated is True and "current" not in roles(meter)
    assert not [e for ids in roles(meter).values() for e in ids if "current" in e]
    assert meter.grid_power == GridPowerSource(power="sensor.solinteg_meter_active_power")
    assert WARNING_SIGN_UNVERIFIED in meter.warnings
    # The meter's power per phase ships disabled: offered to be enabled, never assumed on.
    assert set(meter.disabled_entity_ids) == {f"sensor.solinteg_meter_active_power_l{n}" for n in "123"}


def test_solinteg_is_found_by_the_plugin_authors_name_and_by_its_own() -> None:
    assert len(registry("solinteg_mht", manufacturer="Solinteg").detect().meters) == 1
    assert adapter_for(PLATFORM, "Gabriel C.") is adapter_for(PLATFORM, "Solinteg Power")


@pytest.mark.parametrize("name", ["solinteg_mhs", "solinteg_m2ht_10k_unrecognised"])
def test_a_solinteg_unit_typed_as_single_phase_makes_no_meter(name: str) -> None:
    # A single-phase MHS, and an M2HT-10K-30 on a plugin from before its three-phase fix (October 2026).
    assert registry(name).detect().meters == ()


def test_the_solinteg_battery_is_discharge_positive_and_negated() -> None:
    [battery] = registry("solinteg_mht").detect().batteries

    assert battery.entity_id == "sensor.solinteg_battery_power" and battery.inverted is True
    assert registry("solinteg_m2ht_10k_unrecognised").detect().batteries == ()


def test_solinteg_reads_its_size_in_watt_hours_its_limits_as_currents_and_its_on_grid_floor() -> None:
    found = facts(
        "solinteg_mht",
        "battery_power",
        {
            "battery_soc": "64.5",
            "battery_rated_capacity": "10240",
            "battery_voltage": "204.8",
            "battery_charge_limit": "25",
            "battery_discharge_limit": "30",
            "number.battery_soc_min_ongrid": "15",
            "number.battery_soc_min_offgrid": "40",
            "number.battery_charge_current_limit": "10",
            "select.working_mode": "General",
            "software_version": "V27.52.3.0",
        },
    )

    assert found.brand == "solinteg" and found.soc_pct == 64.5
    assert found.capacity_kwh == pytest.approx(10.24) and found.sources["capacity"] == "sensor.solinteg_battery_rated_capacity"
    assert found.rated_charge_kw == pytest.approx(5.12) and found.rated_discharge_kw == pytest.approx(6.144)
    assert found.estimates == ("rated_from_current",)
    assert found.reserve_soc_pct == 15.0 and found.firmware == "V27.52.3.0"
    assert found.management.state == "free" and found.hold is None and found.grid_charge is None


def test_solinteg_under_an_ems_mode_is_run_by_itself_and_an_old_firmware_has_no_size() -> None:
    found = facts(
        "solinteg_mht",
        "battery_power",
        {"battery_soc": "64.5", "battery_rated_capacity": "unavailable", "select.working_mode": "EMS BattCtrl"},
    )

    assert found.management.state == "self_managed" and found.capacity_kwh is None


# ---- one integration, four makers -----------------------------------------------------------------

#: Each family's meter and battery candidates as (power entity of L1 or None, battery entity or None, negated).
EXPECTED = {
    "sofar_hyd_3ph": ("sensor.sofar_active_power_pcc_l1", "sensor.sofar_battery_power_total", False),
    "growatt_sph_tl3": ("sensor.growatt_ac_power_to_user_l1", "sensor.growatt_battery_combined_power", False),
    "growatt_mod_tl3_xh": (None, "sensor.solax_battery_combined_power", False),
    "solinteg_mht": ("sensor.solinteg_meter_active_power_l1", "sensor.solinteg_battery_power", True),
}
MAKERS = ("SolaX Power", "Sofar Solar", "Growatt New Energy", "Gabriel C.", "Ginlog Solis", None)


@pytest.mark.parametrize("name", sorted(EXPECTED))
@pytest.mark.parametrize("maker", MAKERS)
def test_a_plugins_entities_are_read_only_on_its_own_makers_device(name: str, maker: str | None) -> None:
    detection = registry(name, manufacturer=maker or "").detect()

    power, battery, inverted = EXPECTED[name]
    meters = [m.derived_entities["L1"]["power"] for m in detection.meters]
    batteries = [(b.entity_id, b.inverted) for b in detection.batteries]
    if maker == fixture(name)["manufacturer"]:
        assert meters == ([power] if power else [])
        assert batteries == [(battery, inverted)]
    elif (name, maker) == ("solinteg_mht", "SolaX Power"):
        # The one shared shape: SolaX's own meter has Solinteg's keys (and the same sign). Its battery has not.
        assert meters == [power] and batteries == []
    else:
        assert meters == [] and batteries == []


def test_the_solax_battery_key_under_the_other_makers_is_not_read_as_solax() -> None:
    for maker in ("Sofar Solar", "Growatt New Energy", "Gabriel C."):
        assert solax_battery(manufacturer=maker).detect().batteries == ()


def test_a_growatt_device_under_the_default_hub_name_is_growatt_not_solax() -> None:
    # The integration's default hub name is "SolaX" whatever the plugin, so the unique ids start `SolaX_`.
    data = fixture("growatt_mod_tl3_xh")
    assert data["hub"] == "SolaX"
    assert adapter_for(PLATFORM, data["manufacturer"]).brand == "growatt"
    [battery] = registry("growatt_mod_tl3_xh").detect().batteries
    assert battery.entity_id == "sensor.solax_battery_combined_power"


def test_two_plugins_side_by_side_each_get_their_own_meter_and_battery() -> None:
    both = registry("sofar_hyd_3ph")
    registry("solinteg_mht", into=both)

    detection = both.detect()

    assert {m.title: m.derived_entities["L1"]["power"] for m in detection.meters} == {
        "Sofar": "sensor.sofar_active_power_pcc_l1",
        "Solinteg": "sensor.solinteg_meter_active_power_l1",
    }
    assert {b.entity_id: b.inverted for b in detection.batteries} == {
        "sensor.sofar_battery_power_total": False,
        "sensor.solinteg_battery_power": True,
    }


@pytest.mark.parametrize(
    ("maker", "brand"),
    [("SolaX Power", "solax"), ("Sofar Solar", "sofar"), ("Growatt New Energy", "growatt"), ("Gabriel C.", "solinteg")],
)
def test_each_maker_gets_its_own_adapter_and_an_unknown_plugin_none(maker: str, brand: str) -> None:
    assert adapter_for(PLATFORM, maker).brand == brand
    assert adapter_for(PLATFORM, "Ginlog Solis") is None and adapter_for(PLATFORM, None) is None


def test_every_plugin_updates_at_the_integrations_default_interval() -> None:
    assert UPDATE_BEHAVIOUR[PLATFORM].interval_s == 15.0
    [warning] = freshness_warnings({"sensor.sofar_active_power_pcc_l1": PLATFORM}, 10.0)
    assert warning.interval_s == 15.0 and warning.option == "Default polling interval"
