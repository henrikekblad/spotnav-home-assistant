"""GivEnergy through GivTCP's MQTT discovery (britkat1980 `788b53f`) or `givenergy_local` (cdpuk `782ed42`): read only.
`mqtt` is every device's platform, so the adapter is chosen for a device made by GivEnergy only and every key is anchored
after an inverter serial's shape.

GivTCP's registry is the fixture `givenergy` in `tests/fixtures/battery/brands.json` (with unrelated MQTT entities in it);
`givenergy_local`'s is built here.
"""

from __future__ import annotations

import pytest

from custom_components.spotnav.battery import commands
from custom_components.spotnav.battery.adapters import GIVENERGY, adapter_for
from custom_components.spotnav.battery.coexist import coexistence_for
from custom_components.spotnav.battery.facts import EntityFact, battery_facts

from tests.test_battery_facts import BRANDS, _entities, _facts
from tests.test_site_detection import Registry

SERIAL = "SD2345G123"


def _local(key: str, state: str, unit: str | None = None, domain: str = "sensor") -> EntityFact:
    return EntityFact(
        entity_id=f"{domain}.inv_{key}", platform="givenergy_local", unique_id=f"{SERIAL}_{key}", device_id="inv",
        config_entry_id="entry", state=state, unit=unit,
    )


def test_givenergy_is_read_only_and_chosen_only_for_a_device_made_by_givenergy() -> None:
    assert adapter_for("mqtt", "GivEnergy") is GIVENERGY and adapter_for("givenergy_local", "GivEnergy") is GIVENERGY
    # `mqtt` is shared: another maker's device, or none, is not GivEnergy's.
    assert adapter_for("mqtt", "Shelly") is None and adapter_for("mqtt", None) is None
    assert all(control.brand != "givenergy" for control in commands.CONTROLS)
    assert GIVENERGY.hold is None and GIVENERGY.grid_charge is None


def test_givtcp_reads_level_rating_and_counters_but_not_the_size_or_the_floor() -> None:
    facts = _facts("givenergy")

    assert facts.brand == "givenergy"
    assert facts.soc_pct == 64.0 and facts.sources["soc"] == "sensor.givtcp_sd2345g123_soc"
    assert facts.rated_charge_kw == facts.rated_discharge_kw == 2.6
    assert (facts.charged_total_kwh, facts.discharged_total_kwh) == (3100.5, 2900.2)
    # `Battery_Capacity_kWh` is misreported on some packs, and the reserve is a floor on some firmware only.
    assert facts.capacity_kwh is None and facts.reserve_soc_pct is None


def test_the_todays_counter_and_a_battery_modules_level_are_not_the_inverters() -> None:
    facts = _facts("givenergy")

    assert facts.sources["charged_total"].endswith("battery_charge_energy_total_kwh")
    assert facts.sources["soc"] != "sensor.givtcp_bg2134g000_battery_soc"


def test_another_mqtt_devices_entities_are_never_picked_even_when_the_inverters_are_missing() -> None:
    brand = BRANDS["givenergy"]
    entities = [
        item for item in _entities(brand)
        if item.device_id != "inv" or item.entity_id == brand["battery"]
    ]
    battery = next(item for item in entities if item.entity_id == brand["battery"])

    facts = battery_facts(battery, entities, manufacturer="GivEnergy")

    # `esp32cam_abc123_soc` (12 %) and the module's `Battery_SOC` are on the platform, and neither is the level.
    assert facts.soc_pct is None
    assert facts.rated_charge_kw is None and facts.charged_total_kwh is None


@pytest.mark.parametrize(
    "changes, expected",
    [
        ({}, "free"),
        ({"select.givtcp_sd2345g123_mode": {"state": "Timed Export"}}, "self_managed"),
        ({"select.givtcp_sd2345g123_mode": {"state": "Eco (Paused)"}}, "self_managed"),
        ({"switch.givtcp_sd2345g123_enable_charge_schedule": {"state": "on"}}, "self_managed"),
        ({"switch.givtcp_sd2345g123_enable_discharge_schedule": {"state": "on"}}, "self_managed"),
        ({"select.givtcp_sd2345g123_battery_pause_mode": {"state": "PauseBoth"}}, "self_managed"),
        ({"switch.givtcp_sd2345g123_enable_charge_schedule": {"state": "unavailable"}}, "unknown"),
    ],
)
def test_a_schedule_or_a_mode_other_than_eco_is_the_inverters_own(changes: dict, expected: str) -> None:
    assert _facts("givenergy", **changes).management.state == expected


def test_the_system_mode_sensor_is_not_the_mode_that_is_read() -> None:
    # `System_Mode` also ends `_mode`; its states are grid states, never `Eco`.
    facts = _facts("givenergy", **{"sensor.givtcp_sd2345g123_system_mode": {"state": "Offline"}})

    assert facts.management.state == "free"


def test_givenergy_local_reads_the_level_and_the_schedule_switch_and_the_pause_select() -> None:
    entities = [
        _local("p_battery", "800", "W"),
        _local("battery_percent", "71", "%"),
        _local("enable_charge", "off", domain="switch"),
        _local("enable_discharge", "off", domain="switch"),
        _local("battery_pause_mode", "Not Paused", domain="select"),
    ]

    facts = battery_facts(entities[0], entities, manufacturer="GivEnergy", sw_version="D0.449-A0.449")

    assert facts.brand == "givenergy" and facts.soc_pct == 71.0 and facts.management.state == "free"
    assert facts.capacity_kwh is None and facts.charged_total_kwh is None
    assert facts.firmware == "D0.449-A0.449"
    on = [*entities[:2], _local("enable_charge", "on", domain="switch")]
    assert battery_facts(on[0], on, manufacturer="GivEnergy").management.state == "self_managed"


def test_the_battery_power_is_discharge_positive_for_givenergys_devices_only() -> None:
    r = Registry()
    r.device("inv", model="Gen 2 5kW", manufacturer="GivEnergy", entry="mq")
    r.add("mqtt", "givtcp_sd2345g123_battery_power", f"givtcp_{SERIAL}_Battery_Power", device_class="power", unit="W", device="inv", entry="mq")
    r.add("mqtt", "givtcp_sd2345g123_charge_power", f"givtcp_{SERIAL}_Charge_Power", device_class="power", unit="W", device="inv", entry="mq")
    [battery] = r.detect().batteries
    assert battery.entity_id == "sensor.givtcp_sd2345g123_battery_power"
    assert battery.inverted is True and battery.discharge_entity_id is None

    other = Registry()
    other.device("pw", model="Something", manufacturer="Someone else", entry="mq")
    other.add("mqtt", "pw_battery_power", f"givtcp_{SERIAL}_Battery_Power", device_class="power", unit="W", device="pw", entry="mq")
    other.add("mqtt", "pw2_battery_power", "teslamate_1_battery_power", device_class="power", unit="W", device="pw", entry="mq")
    assert other.detect().batteries == ()


def test_givenergy_localss_p_battery_is_discharge_positive() -> None:
    r = Registry()
    r.device("inv", model="Hybrid Gen 2 5.0kW", manufacturer="GivEnergy", entry="gl")
    r.add("givenergy_local", "inverter_battery_power", f"{SERIAL}_p_battery", device_class="power", unit="W", device="inv", entry="gl")

    [battery] = r.detect().batteries

    assert battery.entity_id == "sensor.inverter_battery_power" and battery.inverted is True


def test_the_coexistence_row_is_the_apps_schedules_and_mqtt_names_no_brand() -> None:
    row = coexistence_for(brand="givenergy")

    assert row is not None and row.aggregators == () and "slots" in row.own_optimiser
    assert coexistence_for(platform="givenergy_local") is row
    assert coexistence_for(platform="mqtt") is None
