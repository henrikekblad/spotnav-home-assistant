"""Marstek through `marstek_local_api` (jaapp `624881c`) or `marstek_modbus` (ViperRNMC 2026.10.1): the reader, found
by the key at the end of the unique id (the control recipe is tested in `test_battery_marstek_control.py`).

The local API's registry is the fixture `marstek` in `tests/fixtures/battery/brands.json` (`<mac>_<key>`), with a
"Marstek System" device (`<macs sorted, joined>_<key>`) built here where an entry holds two batteries; the Modbus
registry (`<config entry id>_<key>`) is built here too.
"""

from __future__ import annotations

from typing import Any

import pytest

from custom_components.spotnav.battery import commands
from custom_components.spotnav.battery.adapters import MARSTEK, adapter_for
from custom_components.spotnav.battery.coexist import coexistence_for
from custom_components.spotnav.battery.facts import EntityFact, battery_facts

from tests.test_battery_facts import _facts

MAC_A, MAC_B = "aabbccddee01", "aabbccddee02"
SYSTEM = f"{MAC_A}_{MAC_B}"
ENTRY = "01JMARSTEKMODBUS0000000000"


def _fact(platform: str, entity_id: str, unique_id: str, device: str, state: str, unit: str | None = None,
          disabled: bool = False) -> EntityFact:
    return EntityFact(
        entity_id=entity_id, platform=platform, unique_id=unique_id, device_id=device, config_entry_id="entry",
        disabled=disabled, state=state, unit=unit,
    )


def test_both_marstek_integrations_are_the_marstek_adapter() -> None:
    assert adapter_for("marstek_local_api", "Marstek") is MARSTEK
    assert adapter_for("marstek_modbus", "Marstek") is MARSTEK
    assert adapter_for("marstek_modbus", None) is MARSTEK


def test_marstek_has_the_modbus_recipe_and_the_adapter_maps_no_control_of_its_own() -> None:
    # The recipe is `commands.MARSTEK_CONTROL` (tests/test_battery_marstek_control.py); the adapter stays the reader.
    assert commands.control_for("marstek") is commands.MARSTEK_CONTROL
    assert MARSTEK.hold is None and MARSTEK.grid_charge is None


# ---- the local API --------------------------------------------------------------------------------------------------


def test_the_local_api_reads_level_size_firmware_and_mode() -> None:
    facts = _facts("marstek")

    assert facts.brand == "marstek" and facts.label == "Marstek"
    assert facts.soc_pct == 55.0
    # The rated capacity, not the energy left now (`battery_capacity`, 2.816 kWh).
    assert facts.capacity_kwh == pytest.approx(5.12)
    assert facts.sources["capacity"] == "sensor.marstek_venuse_3_0_rated_capacity"
    assert facts.firmware == "148"
    assert facts.management.state == "free" and facts.management.settings == {"operating mode": "Auto"}
    # No rating, floor, counter or per-phase power is an entity of the local API.
    assert facts.rated_charge_kw is None and facts.rated_discharge_kw is None
    assert facts.reserve_soc_pct is None and facts.charged_total_kwh is None
    assert facts.plant_phase_entities is None


@pytest.mark.parametrize("mode", ["AI", "Manual", "Passive", "UPS"])
def test_the_local_api_outside_auto_is_not_free(mode: str) -> None:
    facts = _facts("marstek", **{"sensor.marstek_venuse_3_0_operating_mode": {"state": mode}})

    assert facts.management.state == "self_managed"
    assert facts.management.reason == f"operating mode:{mode}"


def test_the_local_api_mode_not_read_yet_is_unknown() -> None:
    facts = _facts("marstek", **{"sensor.marstek_venuse_3_0_operating_mode": {"state": "unknown"}})

    assert facts.management.state == "unknown"
    assert facts.soc_pct == 55.0


def _two_batteries() -> list[EntityFact]:
    """One entry with two Venus E and the integration's system device (`sensor.py` lines 589-590, 748)."""
    rows = []
    for mac, soc, rated, mode in ((MAC_A, "40", "5.12", "Auto"), (MAC_B, "80", "5.12", "Auto")):
        name = f"sensor.marstek_venuse_{mac[-4:]}_"
        rows += [
            _fact("marstek_local_api", name + "power", f"{mac}_battery_power", mac, "-400", "W"),
            _fact("marstek_local_api", name + "state_of_charge", f"{mac}_battery_soc", mac, soc, "%"),
            _fact("marstek_local_api", name + "rated_capacity", f"{mac}_battery_rated_capacity", mac, rated, "kWh"),
            _fact("marstek_local_api", name + "firmware_version", f"{mac}_firmware_version", mac, "148"),
            _fact("marstek_local_api", name + "operating_mode", f"{mac}_operating_mode", mac, mode),
        ]
    rows += [
        _fact("marstek_local_api", "sensor.marstek_system_total_battery_power", f"{SYSTEM}_system_total_power",
              "system", "-800", "W"),
        _fact("marstek_local_api", "sensor.marstek_system_average_state_of_charge", f"{SYSTEM}_system_average_soc",
              "system", "60.0", "%"),
        _fact("marstek_local_api", "sensor.marstek_system_total_rated_capacity",
              f"{SYSTEM}_system_total_rated_capacity", "system", "10.24", "kWh"),
    ]
    return rows


def test_two_batteries_in_one_entry_are_read_whole_on_the_system_device() -> None:
    entities = _two_batteries()
    battery = next(entity for entity in entities if entity.entity_id == "sensor.marstek_system_total_battery_power")

    facts = battery_facts(battery, entities, manufacturer="Marstek", power_kw=-0.8)

    assert facts.soc_pct == 60.0 and facts.sources["soc"] == "sensor.marstek_system_average_state_of_charge"
    assert facts.capacity_kwh == pytest.approx(10.24)
    # The system device has no mode and the batteries' two cannot be told apart: who runs them is not known.
    assert facts.management.state == "unknown"
    assert facts.management.reason == "operating mode:not_found"


def test_one_battery_of_an_entry_picked_as_the_site_battery_reads_its_own() -> None:
    entities = _two_batteries()
    battery = next(entity for entity in entities if entity.unique_id == f"{MAC_B}_battery_power")

    facts = battery_facts(battery, entities, manufacturer="Marstek", power_kw=-0.4)

    assert facts.soc_pct == 80.0 and facts.capacity_kwh == pytest.approx(5.12)
    assert facts.management.state == "free"


# ---- Modbus ---------------------------------------------------------------------------------------------------------


def _modbus(**changes: dict[str, Any]) -> tuple[EntityFact, list[EntityFact]]:
    """A Venus E v2 through `marstek_modbus` (`registers/e_v12.yaml`), one entry, its keys after the entry id."""
    rows: list[tuple[str, str, str, str | None, bool]] = [
        ("sensor.venus_battery_power", "battery_power", "1200", "W", False),
        ("sensor.venus_ac_power", "ac_power", "-1250", "W", False),
        ("sensor.venus_battery_soc", "battery_soc", "47", "%", False),
        ("sensor.venus_battery_total_energy", "battery_total_energy", "5.12", "kWh", False),
        ("sensor.venus_stored_energy", "stored_energy", "2.41", "kWh", False),
        ("sensor.venus_firmware_version", "firmware_version", "V153.212", None, False),
        ("sensor.venus_total_charging_energy", "total_charging_energy", "812.35", "kWh", False),
        ("sensor.venus_total_discharging_energy", "total_discharging_energy", "701.20", "kWh", False),
        ("select.venus_user_work_mode", "user_work_mode", "anti_feed", None, False),
        ("switch.venus_rs485_control_mode", "rs485_control_mode", "off", None, True),
        ("number.venus_discharging_cutoff_capacity", "discharging_cutoff_capacity", "12.0", "%", True),
        ("number.venus_max_charge_power", "max_charge_power", "2500", "W", True),
        ("number.venus_max_discharge_power", "max_discharge_power", "800", "W", True),
    ]
    entities = []
    for entity_id, key, state, unit, disabled in rows:
        change = changes.get(entity_id, {})
        entities.append(
            _fact("marstek_modbus", entity_id, f"{ENTRY}_{key}", "venus", change.get("state", state), unit,
                  change.get("disabled", disabled))
        )
    battery = next(entity for entity in entities if entity.entity_id == "sensor.venus_battery_power")
    return battery, entities


def _modbus_facts(**changes: dict[str, Any]):
    battery, entities = _modbus(**changes)
    return battery_facts(battery, entities, manufacturer="Marstek", power_kw=1.2)


def test_modbus_reads_level_size_firmware_counters_and_mode() -> None:
    facts = _modbus_facts()

    assert facts.brand == "marstek"
    assert facts.soc_pct == 47.0
    # The integration's own capacity (its stored energy is the SoC times it), not the stored energy itself.
    assert facts.capacity_kwh == pytest.approx(5.12)
    assert facts.sources["capacity"] == "sensor.venus_battery_total_energy"
    assert facts.firmware == "V153.212"
    assert facts.charged_total_kwh == pytest.approx(812.35)
    assert facts.discharged_total_kwh == pytest.approx(701.20)
    # The RS485 switch is disabled by default: not read, and not needed for "free".
    assert facts.management.state == "free"
    assert facts.management.settings == {"operating mode": "anti_feed"}


def test_modbus_charge_and_discharge_limits_are_settings_not_ratings() -> None:
    facts = _modbus_facts(**{
        "number.venus_max_charge_power": {"disabled": False},
        "number.venus_max_discharge_power": {"disabled": False},
    })

    assert facts.rated_charge_kw is None and facts.rated_discharge_kw is None


def test_modbus_floor_is_the_discharging_cutoff_once_enabled() -> None:
    assert _modbus_facts().reserve_soc_pct is None

    facts = _modbus_facts(**{"number.venus_discharging_cutoff_capacity": {"disabled": False}})

    assert facts.reserve_soc_pct == 12.0
    assert facts.sources["reserve"] == "number.venus_discharging_cutoff_capacity"


@pytest.mark.parametrize("mode", ["manual", "trade_mode"])
def test_modbus_outside_self_consumption_is_not_free(mode: str) -> None:
    facts = _modbus_facts(**{"select.venus_user_work_mode": {"state": mode}})

    assert facts.management.state == "self_managed"


def test_modbus_rs485_control_on_is_an_outside_controller_even_in_self_consumption() -> None:
    facts = _modbus_facts(**{"switch.venus_rs485_control_mode": {"state": "on", "disabled": False}})

    assert facts.management.state == "self_managed"
    assert facts.management.reason == "RS485 control:on"


def test_modbus_without_its_optional_entities_still_reads_level_and_power() -> None:
    battery, entities = _modbus()
    keep = {"sensor.venus_battery_power", "sensor.venus_battery_soc"}
    facts = battery_facts(
        battery, [entity for entity in entities if entity.entity_id in keep], manufacturer="Marstek", power_kw=1.2
    )

    assert facts.brand == "marstek" and facts.soc_pct == 47.0 and facts.power_kw == 1.2
    assert facts.capacity_kwh is None and facts.reserve_soc_pct is None
    assert facts.management.state == "unknown"


def test_the_official_marstek_integration_is_named_but_not_read_because_its_power_has_no_direction() -> None:
    """`marstek` (MarstekEnergy/ha_marstek 0.3.0): `battery_power` is `abs(bat_power)`, so there is no sign to read. The
    coexistence row still names Marstek's own modes for it."""
    from custom_components.spotnav.site.site_detection import BATTERY_ROWS  # noqa: PLC0415

    assert adapter_for("marstek", "Marstek") is None
    assert not any("marstek" in row.platforms for row in BATTERY_ROWS)
    row = coexistence_for(platform="marstek")
    assert row is not None and row.brand == "marstek"
