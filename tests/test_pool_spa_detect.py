"""The pool's and the spa's detection (`pool/detect.py`, `spa/detect.py`): ranked from what the registry shows, with
fixtures from the research's tables (`plans/research_integrations/pool_heat_pump_control_2026-11-23.md` section 4,
`spa_control_2026-11-23.md` section 3)."""

from __future__ import annotations

from custom_components.spotnav.pool.detect import (
    Seen,
    circulations,
    heat_pump_score,
    heat_pumps,
    read_seen,
    suggest,
    water_sensors,
)
from custom_components.spotnav.spa.detect import spas

#: The owner's Madimack Eco 9 kW on `tuya_local`, with its compressor-strength sensor.
MADIMACK = Seen(
    "climate.poolpump", "tuya_local", "dev_madimack", "Poolpump", "Pool", "pool_heatpump", "tl_1",
    "Madimack", "Eco 9kW", ("off", "heat"), False, 18.0, 40.0,
)
MADIMACK_COMPRESSOR = Seen("sensor.poolpump_compressor_strength", "tuya_local", "dev_madimack", "Compressor strength", "Pool")
#: The owner's air-to-air pump on ESPHome: dry and fan_only, fan modes.
AIR = Seen(
    "climate.ovanvaning", "esphome", "dev_air", "Värmepump ovanvåning", None, None, "e_1", None, None,
    ("off", "heat_cool", "cool", "heat", "fan_only", "dry", "auto"), True, 16.0, 26.0,
)
#: Core `tuya`'s pool heat pump: no climate; a switch, a `temp_set` number and the compressor sensor.
TUYA = [
    Seen("switch.pool_hp", "tuya", "dev_tuya", "Pool HP", None, "switch", "tuya.abcswitch"),
    Seen("switch.pool_hp_lock", "tuya", "dev_tuya", "Child lock", None, "child_lock", "tuya.abcchild_lock"),
    Seen("number.pool_hp_temperature", "tuya", "dev_tuya", "Temperature", None, "temperature", "tuya.abctemp_set"),
    Seen("sensor.pool_hp_compressor", "tuya", "dev_tuya", "Compressor strength", None, "compressor_strength", "tuya.abccompressor_strength"),
    Seen("sensor.pool_hp_temperature", "tuya", "dev_tuya", "Temperature", None, "temperature", "tuya.abctemp_current", device_class="temperature"),
]
FIBARO = Seen("switch.pool_cirkulation", "zwave_js", "dev_fibaro", "Pool cirkulation", "Pool")
LAMP = Seen("switch.lampa", "zwave_js", "dev_lamp", "Lampa", "Vardagsrum")
POOL_WATER = Seen("sensor.pool_vatten", "zha", "dev_temp", "Pool vatten", "Pool", device_class="temperature", unit="°C")
INDOOR = Seen("sensor.kok", "zha", "dev_kok", "Kök temperatur", "Kök", device_class="temperature", unit="°C")


def test_the_owners_madimack_is_a_pool_heat_pump_and_the_air_to_air_is_not() -> None:
    assert heat_pump_score(MADIMACK, [MADIMACK, MADIMACK_COMPRESSOR]) == 0.95
    assert heat_pump_score(AIR) == 0.0
    # Even named "pool", an air-to-air unit's dry and fan modes keep it out.
    assert heat_pump_score(Seen("climate.pool", "esphome", name="Pool", hvac_modes=("heat", "dry"), min_c=16, max_c=30)) == 0.0


def test_a_pool_domain_wins_even_with_fan_modes_and_a_brand_is_asked() -> None:
    aquatemp = Seen("climate.aquatemp", "aqua_temp", hvac_modes=("off", "heat", "cool", "auto"), fan_modes=True)
    assert heat_pump_score(aquatemp) == 0.95
    poolex = Seen("climate.poolex", "localtuya", "d", manufacturer="Poolex", hvac_modes=("off", "heat"), min_c=8.0, max_c=40.0)
    # Brand 0.8 + range and modes, capped at 0.95.
    assert heat_pump_score(poolex) == 0.95
    bare = Seen("climate.bare", "localtuya", "d", manufacturer="Poolex")
    assert heat_pump_score(bare) == 0.8
    # A panel that heats a pool and a spa is always asked.
    assert heat_pump_score(Seen("climate.pool_heater", "screenlogic")) == 0.8
    # A floor thermostat: one support only, not offered.
    floor = Seen("climate.floor", "zha", hvac_modes=("off", "heat"), min_c=5.0, max_c=35.0)
    assert heat_pumps([floor]) == []


def test_core_tuyas_pool_heat_pump_is_found_as_the_switch_and_number_shape() -> None:
    found = heat_pumps(TUYA)
    assert len(found) == 1
    pump = found[0]
    assert (pump.entity_id, pump.shape, pump.switch_id, pump.number_id) == (
        "switch.pool_hp", "switch_number", "switch.pool_hp", "number.pool_hp_temperature"
    )
    assert pump.score == 0.9 and not pump.ask and pump.cloud and pump.water_sensor == "sensor.pool_hp_temperature"


def test_the_circulation_and_the_water_sensor_are_asked() -> None:
    circ = circulations([FIBARO, LAMP, Seen("switch.poolpump_power", "tuya_local", "dev_madimack", "Poolpump power")], "dev_madimack")
    assert [(c.entity_id, c.score, c.ask) for c in circ] == [("switch.pool_cirkulation", 0.5, True), ("switch.lampa", 0.0, True)]
    sensors = water_sensors([POOL_WATER, INDOOR, LAMP])
    assert [(c.entity_id, c.score) for c in sensors] == [("sensor.pool_vatten", 0.5), ("sensor.kok", 0.0)]


def test_the_one_time_grouping_proposes_the_circulation_and_the_sensor_in_the_pools_area() -> None:
    group = suggest([MADIMACK, MADIMACK_COMPRESSOR, AIR, FIBARO, LAMP, POOL_WATER, INDOOR])
    assert group == {
        "heat_pump": {"entity_id": "climate.poolpump", "shape": "climate", "switch_id": None, "number_id": None},
        "circulation_id": "switch.pool_cirkulation",
        "water_sensor": "sensor.pool_vatten",
        "ask": True,
    }
    assert suggest([AIR, LAMP]) is None
    # Core tuya: the device's own water reading.
    assert suggest(TUYA)["water_sensor"] == "sensor.pool_hp_temperature"  # type: ignore[index]


def test_the_spa_families() -> None:
    found = spas(
        [
            Seen("climate.spa", "balboa", "dev_balboa", "Spa"),
            Seen("climate.smarttub", "smarttub", "dev_st", "SmartTub"),
            # Gecko: a climate and a water heater on one device; the water heater is the spa.
            Seen("climate.gecko", "gecko", "dev_gecko", "Gecko"),
            Seen("water_heater.gecko", "gecko", "dev_gecko", "Gecko heater"),
            Seen("climate.bestway", "bestway", "dev_bw", "Lay-Z-Spa"),
            Seen("climate.mspa", "mspa", "dev_mspa", "MSpa"),
            Seen("climate.intex", "intex_spa", "dev_intex", "Intex"),
            Seen("water_heater.purespa", "tuya_local", "dev_ty", "PureSpa", manufacturer="Intex"),
            Seen("climate.cms", "control_my_spa", "dev_cms", "Balboa cloud"),
            Seen("climate.bubbel", "esphome", "dev_gen", "Bubbelpool", min_c=10.0, max_c=40.0),
            # A water heater is no spa (its range), nor the air-to-air pump.
            Seen("water_heater.boiler", "econet", "dev_boil", "Varmvatten", min_c=30.0, max_c=75.0),
            AIR,
        ]
    )
    by_id = {c.entity_id: c for c in found}
    assert set(by_id) == {
        "climate.spa", "climate.smarttub", "water_heater.gecko", "climate.bestway", "climate.mspa", "climate.intex",
        "water_heater.purespa", "climate.cms", "climate.bubbel",
    }
    assert by_id["climate.spa"].family == "balboa" and by_id["climate.spa"].score == 1.0
    assert by_id["water_heater.purespa"].family == "tuya_intex" and by_id["water_heater.purespa"].score == 0.9
    generic = by_id["climate.bubbel"]
    assert (generic.family, generic.score, generic.ask, generic.controllable) == ("generic", 0.5, True, False)
    assert not by_id["climate.cms"].controllable and by_id["climate.cms"].family == "control_my_spa"
    assert by_id["climate.smarttub"].as_dict()["cloud"] is True


async def test_the_registry_reading_leaves_out_spotnavs_own_and_disabled_entities(hass) -> None:
    from homeassistant.helpers import device_registry as dr, entity_registry as er
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(domain="tuya_local")
    entry.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={("tuya_local", "pool")}, manufacturer="Madimack", model="Eco 9kW"
    )
    registry = er.async_get(hass)
    pump = registry.async_get_or_create(
        "climate", "tuya_local", "pool_1", device_id=device.id, translation_key="pool_heatpump", config_entry=entry
    )
    hass.states.async_set(pump.entity_id, "heat", {"hvac_modes": ["off", "heat"], "min_temp": 18, "max_temp": 40})
    off = registry.async_get_or_create("switch", "tuya_local", "pool_2", disabled_by=er.RegistryEntryDisabler.USER)
    own = registry.async_get_or_create("sensor", "spotnav", "own")
    hass.states.async_set("input_boolean.pool_cirkulation", "off", {"friendly_name": "Pool cirkulation"})
    seen = {item.entity_id: item for item in read_seen(hass)}
    assert pump.entity_id in seen and off.entity_id not in seen and own.entity_id not in seen
    assert seen[pump.entity_id].manufacturer == "Madimack" and seen[pump.entity_id].max_c == 40.0
    assert "input_boolean.pool_cirkulation" in seen
    assert heat_pumps(list(seen.values()))[0].entity_id == pump.entity_id


async def test_the_heat_cards_choices_carry_the_pool_the_spa_and_the_phones(hass) -> None:
    from custom_components.spotnav.api.heat import pool_spa_choices
    from custom_components.spotnav.heat.config import HeatConfig
    from custom_components.spotnav.pool.config import PoolConfig

    hass.states.async_set("climate.spa_heater", "heat", {"friendly_name": "Spa", "min_temp": 10, "max_temp": 40})
    hass.states.async_set("switch.pool_cirkulation", "off", {"friendly_name": "Pool cirkulation"})
    none = pool_spa_choices(hass, HeatConfig())
    assert none["pool"] is None
    assert [row["entity_id"] for row in none["spa"]] == ["climate.spa_heater"]
    assert none["spa"][0]["family"] == "generic" and none["spa"][0]["ask"] is True
    assert none["phones"] == []
    # A chosen pool is always listed, its chosen circulation first; no grouping is proposed once one is saved.
    chosen = pool_spa_choices(hass, HeatConfig(pool=PoolConfig(circulation_id="switch.pool_cirkulation")))
    assert chosen["pool"]["heat_pump"] == [] and chosen["pool"]["suggested"] is None
    assert chosen["pool"]["circulation"][0] == {
        "entity_id": "switch.pool_cirkulation", "name": "Pool cirkulation", "value": "off", "score": 0.5, "ask": True
    }
