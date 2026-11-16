"""The heat pump's facts per brand, read from fixture registries (`tests/fixtures/heat/brands.json`).

Each brand's entities are found by the integration and the key in the unique id, never by entity id; `unknown`,
`unavailable`, disabled entities and implausible figures are never a value; the control paths are listed only.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from custom_components.spotnav.heat.adapters import ADAPTERS, PLATFORMS
from custom_components.spotnav.heat.facts import (
    HeatEntity,
    HeatPumpFacts,
    find_heat_pumps,
    priority_of,
    running_of,
)

BRANDS: dict[str, dict[str, Any]] = json.loads(
    (Path(__file__).parent / "fixtures" / "heat" / "brands.json").read_text(encoding="utf-8")
)
_READINGS = (
    "outdoor_temp_c",
    "supply_temp_c",
    "return_temp_c",
    "room_temp_c",
    "hot_water_temp_c",
    "degree_minutes",
    "compressor_speed",
)


def _entities(name: str, **changes: dict[str, Any]) -> list[HeatEntity]:
    brand = BRANDS[name]
    entities = []
    for row in brand["entities"]:
        row = row | changes.get(row["entity_id"], {})
        entities.append(
            HeatEntity(
                entity_id=row["entity_id"],
                platform=brand["platform"],
                unique_id=row["unique_id"],
                device_id=row["device"],
                config_entry_id=f"entry_{name}",
                disabled=row["disabled"],
                state=row["state"],
                unit=row["unit"],
                minimum=row.get("min"),
                maximum=row.get("max"),
                step=row.get("step"),
                options=tuple(row.get("options", ())),
            )
        )
    return entities


def _pump(name: str, *, other_controllers: tuple[str, ...] = (), **changes: dict[str, Any]) -> HeatPumpFacts:
    pumps = find_heat_pumps(_entities(name, **changes), other_controllers=other_controllers)
    assert len(pumps) == 1
    return pumps[0]


def test_every_adapter_has_a_fixture() -> None:
    assert {adapter.brand for adapter in ADAPTERS} == {brand["expected"]["brand"] for brand in BRANDS.values()}
    assert {brand["platform"] for brand in BRANDS.values()} == PLATFORMS


@pytest.mark.parametrize("name", sorted(BRANDS))
def test_each_brand_reads_its_facts(name: str) -> None:
    expected = BRANDS[name]["expected"]

    pump = _pump(name)
    data = pump.as_dict()

    assert pump.brand == expected["brand"]
    assert pump.integration == BRANDS[name]["platform"]
    for key, value in expected["readings"].items():
        if isinstance(value, float):
            assert data["readings"][key] == pytest.approx(value), key
        else:
            assert data["readings"][key] == value, key
    assert pump.missing == expected["missing"]
    # A diagnostics form is plain data.
    json.dumps(data)


@pytest.mark.parametrize("name", sorted(BRANDS))
def test_each_brand_lists_its_control_paths(name: str) -> None:
    expected = BRANDS[name]["expected"]["capabilities"]

    pump = _pump(name)

    found: dict[str, list[str]] = {}
    for capability in pump.capabilities:
        found.setdefault(capability.kind, []).append(capability.entity_id)
    assert found == expected


@pytest.mark.parametrize("name", sorted(BRANDS))
def test_each_brand_marks_its_own_price_control(name: str) -> None:
    expected = BRANDS[name]["expected"]

    pump = _pump(name)

    assert pump.price_control.state == expected["price_control"]
    assert pump.control == expected["control"]
    assert pump.watch_only is (expected["control"] == "watch_only")


@pytest.mark.parametrize("name", sorted(BRANDS))
@pytest.mark.parametrize("state", ["unknown", "unavailable", None])
def test_no_reading_is_ever_a_value(name: str, state: str | None) -> None:
    """Every entity of the pump without a reading: no figure, no derived state, every role missing."""
    changes = {row["entity_id"]: {"state": state} for row in BRANDS[name]["entities"]}

    pump = _pump(name, **changes)

    for key in _READINGS:
        assert getattr(pump, key) is None, key
    assert pump.compressor_running is None
    assert pump.immersion_kw is None and pump.immersion_on is None
    assert pump.priority is None
    assert pump.price_control.state in ("unknown", "none")
    assert pump.control != "watch_only"
    assert set(pump.missing.values()) <= {"unavailable", "disabled", "not_found"}
    for capability in pump.capabilities:
        assert capability.as_dict()["current"] is None


@pytest.mark.parametrize("name", sorted(BRANDS))
def test_a_disabled_entity_is_no_value_and_says_so(name: str) -> None:
    anchor = next(row for row in BRANDS[name]["entities"] if row["unique_id"].endswith(("40004", "30002", "62000", "_outdoor_temp", "TOP14")))
    changes = {anchor["entity_id"]: {"disabled": True}}

    pump = _pump(name, **changes)

    assert pump.outdoor_temp_c is None
    assert pump.missing["outdoor_temp"] == "disabled"


@pytest.mark.parametrize(
    ("state", "unit"),
    [("-105.0", "°C"), ("213.0", "°C"), ("-3276.8", "°C"), ("-32768", "°C"), ("nan", "°C"), ("cold", "°C"), ("5.0", "K")],
)
def test_an_implausible_outdoor_reading_is_none(state: str, unit: str) -> None:
    pump = _pump("nibe_f", **{"sensor.bt1_outdoor_temperature_40004": {"state": state, "unit": unit}})

    assert pump.outdoor_temp_c is None
    assert pump.missing["outdoor_temp"] == "implausible"


def test_a_fahrenheit_reading_is_turned_into_celsius() -> None:
    pump = _pump("nibe_f", **{"sensor.bt1_outdoor_temperature_40004": {"state": "23.0", "unit": "°F"}})

    assert pump.outdoor_temp_c == pytest.approx(-5.0)


def test_the_next_source_is_read_when_the_first_has_none() -> None:
    """An SMO has no BT2 (40008): its supply is the external BT25 (40071)."""
    pump = _pump(
        "nibe_f",
        **{
            "sensor.bt2_supply_temp_s1_40008": {"state": "unavailable"},
            "sensor.bt25_ext_supply_40071": {"disabled": False, "state": "44.0"},
        },
    )

    assert pump.supply_temp_c == 44.0
    assert pump.sources["supply_temp"] == "sensor.bt25_ext_supply_40071"


def test_s_series_40012_is_degree_minutes_never_the_return() -> None:
    pump = _pump("nibe_s", **{"sensor.return_line_bt3_30008": {"state": "unknown"}})

    assert pump.return_temp_c is None
    assert pump.missing["return_temp"] == "unavailable"
    assert pump.degree_minutes == -150.0


def test_an_entity_id_does_not_find_a_reading() -> None:
    """A sensor named like the pump's but of another integration (or with another key) is not read."""
    entities = _entities("nibe_f")
    entities.append(
        HeatEntity(
            entity_id="sensor.bt50_room_temp_s1_40033_copy",
            platform="template",
            unique_id="room-40033",
            config_entry_id="entry_nibe_f",
            state="35.0",
            unit="°C",
        )
    )

    pump = find_heat_pumps(entities)[0]

    assert pump.room_temp_c == 21.4


def test_nibe_smart_price_adaption_read_makes_it_watch_only() -> None:
    pump = _pump(
        "nibe_f",
        **{
            "sensor.smart_price_adaption_price_level_41929": {"disabled": False, "state": "2"},
            "sensor.smart_price_adaption_heating_offset_44896": {"disabled": False, "state": "0.0"},
        },
    )

    assert pump.price_control.state == "active"
    assert pump.control == "watch_only"
    assert pump.control_reason == "price_control:Smart Price Adaption price level"


def test_nibe_smart_price_adaption_off_is_free() -> None:
    pump = _pump(
        "nibe_f",
        **{
            "sensor.smart_price_adaption_price_level_41929": {"disabled": False, "state": "0"},
            "sensor.smart_price_adaption_heating_offset_44896": {"disabled": False, "state": "0.0"},
        },
    )

    assert pump.price_control.state == "off"
    assert pump.control == "free"


def test_s_series_smart_price_adaption_switched_off_is_free() -> None:
    pump = _pump("nibe_s", **{"switch.activated_smart_price_adaption_40844": {"state": "off"}})

    assert pump.price_control.state == "off"
    assert pump.control == "free"


def test_ctc_smartgrid_or_a_spot_price_makes_it_watch_only() -> None:
    assert _pump("ctc_bms", **{"sensor.ctc_smartgrid_mode": {"state": "Low price"}}).control == "watch_only"
    assert _pump("ctc_bms", **{"sensor.ctc_elspot_price_mwh": {"state": "412"}}).control == "watch_only"
    assert _pump("ctc_local", **{"sensor.ctc_smartgrid_lage": {"state": "Normal"}}).control == "free"


def test_another_heat_controller_makes_any_pump_watch_only() -> None:
    pump = _pump("heishamon", other_controllers=("effektguard",))

    assert pump.control == "watch_only"
    assert pump.control_reason == "controller:effektguard"


def test_two_pumps_in_one_myuplink_account_are_read_apart() -> None:
    first = _entities("myuplink")
    second = [
        HeatEntity(
            entity_id=entity.entity_id.replace("f730", "f730b"),
            platform=entity.platform,
            unique_id=entity.unique_id.replace("batman", "alfred"),
            device_id="pump_b",
            config_entry_id=entity.config_entry_id,
            state="-1.0" if entity.unique_id.endswith("-40004") else entity.state,
            unit=entity.unit,
        )
        for entity in first
    ]

    pumps = find_heat_pumps(first + second)

    assert [pump.outdoor_temp_c for pump in pumps] == [-9.3, -1.0]
    assert pumps[1].sources["outdoor_temp"] == "sensor.f730b_cu_3x400v_current_outd_temp_bt1"


def test_ctc_readings_on_other_devices_of_the_same_entry_are_the_pumps() -> None:
    """The BMS integration splits one controller into devices (system, zone, heat pump, hot water)."""
    pump = _pump("ctc_bms")

    assert pump.sources["room_temp"] == "sensor.ctc_heating_system_1_current_room_temp"
    assert pump.sources["compressor_speed"] == "sensor.ctc_heat_pump_1_current_rps"


def test_ctc_immersion_heaters_are_summed() -> None:
    pump = _pump(
        "ctc_bms",
        **{
            "sensor.ctc_power_kw_immersion_heater": {"disabled": False, "state": "3.0"},
            "sensor.ctc_power_kw_immersion_heater_lower": {"disabled": False, "state": "1.5"},
        },
    )

    assert pump.immersion_kw == pytest.approx(4.5)
    assert pump.immersion_on is True


def test_ctc_local_reads_the_controllers_outdoor_sensor_not_the_outdoor_units() -> None:
    pump = _pump("ctc_local")

    assert pump.sources["outdoor_temp"] == "sensor.ctc_utetemperatur"


def test_capabilities_carry_their_range_and_are_listed_only() -> None:
    pump = _pump("nibe_f")

    offset = next(capability for capability in pump.capabilities if capability.kind == "offset")
    assert (offset.minimum, offset.maximum, offset.step) == (-10.0, 10.0, 1.0)
    assert offset.as_dict()["current"] == "0.0"
    assert offset.method == "number"


def test_ctc_volatile_controls_are_marked() -> None:
    pump = _pump("ctc_bms")

    assert all(capability.volatile for capability in pump.capabilities)


def test_no_pump_without_its_anchor() -> None:
    entities = [entity for entity in _entities("nibe_f") if not entity.unique_id.endswith("-40004")]

    assert find_heat_pumps(entities) == []


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("HOT WATER", "hot_water"),
        ("HEAT", "heating"),
        ("Heating", "heating"),
        ("Hot water", "hot_water"),
        ("OFF", "off"),
        ("COOLING", "cooling"),
        ("POOL", "pool"),
        ("TRANSFER", None),
        ("Compressor on, heating", "heating"),
        ("Compressor off, ready to start", "off"),
        ("Charge DHW", "hot_water"),
        ("Till värme", "heating"),
        ("Till varmvatten", "hot_water"),
        ("Kompressor av, startfördröjning", "off"),
        ("Room", "heating"),
        ("Tank", "hot_water"),
        ("Defrost active", None),
        ("", None),
    ],
)
def test_priority_words(text: str, expected: str | None) -> None:
    assert priority_of(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("RUNNING", True),
        ("STARTING", True),
        ("STOPPED", False),
        ("STOPPING", False),
        ("Compressor on, heating", True),
        ("Compressor off, ready to start", False),
        ("Compressor off, blocked", False),
        ("Till värme", True),
        ("Av, blockerad", False),
        ("Redo för start", False),
        ("Defrost active", None),
        (None, None),
    ],
)
def test_running_words(text: str | None, expected: bool | None) -> None:
    assert running_of(text) is expected


def test_the_calculated_supply_is_read_where_the_pump_has_it_and_never_asked_for() -> None:
    entities = _entities("nibe_f")
    anchor = next(entity for entity in entities if entity.entity_id == "sensor.bt1_outdoor_temperature_40004")
    assert _pump("nibe_f").calc_supply_temp_c is None and "calc_supply_temp" not in _pump("nibe_f").missing
    calc = HeatEntity(
        entity_id="sensor.calc_supply_s1_43009",
        platform="nibe_heatpump",
        unique_id=anchor.unique_id.replace("40004", "43009"),
        device_id=anchor.device_id,
        config_entry_id=anchor.config_entry_id,
        disabled=False,
        state="40.3",
        unit="°C",
    )
    pump = find_heat_pumps([*entities, calc])[0]
    assert pump.calc_supply_temp_c == 40.3 and pump.sources["calc_supply_temp"] == "sensor.calc_supply_s1_43009"
    # Disabled, as the integration leaves every coil: still no note.
    disabled = find_heat_pumps([*entities, replace(calc, disabled=True)])[0]
    assert disabled.calc_supply_temp_c is None and "calc_supply_temp" not in disabled.missing
