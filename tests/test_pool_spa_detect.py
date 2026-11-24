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
    # Core tuya: the device's own water reading is the heat pump's (read first at run time), never the standalone one.
    assert suggest(TUYA)["water_sensor"] is None  # type: ignore[index]


#: The owner's own setup: the pump's ambient sensor (not diagnostic, so detection sees it) and a standalone sensor.
MADIMACK_AMBIENT = Seen(
    "sensor.poolpump_ambient_temperature", "tuya_local", "dev_madimack", "Poolpump Ambient temperature", "Pool",
    "ambient_temperature", device_class="temperature", unit="°C",
)
POOL_TEMPERATUR = Seen("sensor.pool_temperatur", "zha", "dev_pooltemp", "Pool temperatur", "Pool", device_class="temperature", unit="°C")


def test_the_standalone_sensor_is_the_second_choice_and_the_pumps_own_sensors_are_not_offered_as_it() -> None:
    sensors = water_sensors([MADIMACK_AMBIENT, POOL_TEMPERATUR, INDOOR], "dev_madimack")
    assert [(c.entity_id, c.score) for c in sensors] == [
        ("sensor.pool_temperatur", 0.5), ("sensor.kok", 0.0), ("sensor.poolpump_ambient_temperature", 0.0)
    ]
    # Named for the air, even off the pump's device: never likely.
    outdoor = Seen("sensor.pool_ute", "zha", "d", "Pool utetemperatur", "Pool", device_class="temperature")
    assert water_sensors([outdoor])[0].score == 0.0
    group = suggest([MADIMACK, MADIMACK_COMPRESSOR, MADIMACK_AMBIENT, POOL_TEMPERATUR, FIBARO])
    # The heat pump's own reading comes first at run time; the standalone sensor is proposed beside it.
    assert group is not None and group["water_sensor"] == "sensor.pool_temperatur"
    alone = suggest([MADIMACK, MADIMACK_COMPRESSOR, MADIMACK_AMBIENT, FIBARO])
    assert alone is not None and alone["water_sensor"] is None


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


def test_the_sauna_routes_and_their_door() -> None:
    from custom_components.spotnav.sauna.detect import saunas

    found = saunas(
        [
            Seen("climate.huum", "huum", "dev_huum", "Huum"),
            Seen("binary_sensor.huum_door", "huum", "dev_huum", "Door", device_class="door"),
            Seen("climate.saunum", "saunum", "dev_saunum", "Saunum", hvac_modes=("off", "heat"), fan_modes=True),
            Seen("binary_sensor.saunum_door", "saunum", "dev_saunum", "Door", translation_key=None, device_class="door"),
            Seen("climate.harvia", "harvia_sauna", "dev_harvia", "Harvia", translation_key="thermostat"),
            Seen("binary_sensor.harvia_door", "harvia_sauna", "dev_harvia", "Door", translation_key="door"),
            Seen("climate.tylo", "tylo_sauna", "dev_tylo", "Tylö"),
            Seen("climate.sentio", "sentio", "dev_sentio", "Sentio"),
            Seen("switch.bastuaggregat", "zwave_js", "dev_relay", "Bastuaggregat"),
            Seen("climate.ugn", "esphome", "dev_ugn", "Bastu ugn", max_c=110.0, min_c=40.0),
            Seen("switch.lampa", "zwave_js", "dev_lamp", "Lampa"),
        ]
    )
    by_id = {c.entity_id: c for c in found}
    assert set(by_id) == {
        "climate.huum", "climate.saunum", "climate.harvia", "climate.tylo", "climate.sentio", "switch.bastuaggregat", "climate.ugn"
    }
    assert (by_id["climate.huum"].family, by_id["climate.huum"].score, by_id["climate.huum"].lock_id) == ("huum", 1.0, "binary_sensor.huum_door")
    assert by_id["climate.harvia"].lock_id == "binary_sensor.harvia_door" and by_id["climate.harvia"].as_dict()["cloud"] is True
    assert by_id["climate.tylo"].lock_id is None
    assert (by_id["switch.bastuaggregat"].family, by_id["switch.bastuaggregat"].ask) == ("relay", True)
    assert by_id["climate.ugn"].family == "generic"
    # A sauna's climate (40-110 °C) is never a pool heat pump, nor a spa.
    assert heat_pump_score(Seen("climate.saunum", "saunum", hvac_modes=("off", "heat"), fan_modes=True, min_c=40, max_c=100)) == 0.0
    assert spas([Seen("climate.huum", "huum", min_c=40.0, max_c=110.0, name="Sauna")]) == []


async def test_the_zone_and_setpoint_choices_leave_out_the_pool_pump_the_spa_and_the_sauna(hass) -> None:
    from homeassistant.helpers import device_registry as dr, entity_registry as er
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.spotnav.api.heat import heat_choices
    from custom_components.spotnav.heat.config import HeatConfig, Zone

    entry = MockConfigEntry(domain="tuya_local")
    entry.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={("tuya_local", "pool")}, manufacturer="Madimack", model="Eco 9kW"
    )
    registry = er.async_get(hass)
    registry.async_get_or_create("climate", "tuya_local", "p1", suggested_object_id="poolpump", device_id=device.id, translation_key="pool_heatpump", config_entry=entry)
    registry.async_get_or_create("climate", "balboa", "b1", suggested_object_id="balboa_spa_fake")
    registry.async_get_or_create("climate", "saunum", "s1", suggested_object_id="saunum_leil_fake")
    registry.async_get_or_create("climate", "generic_thermostat", "t1", suggested_object_id="golvvarme")
    modes = {"hvac_modes": ["off", "heat"], "min_temp": 18, "max_temp": 40}
    for entity_id in ("poolpump", "balboa_spa_fake", "saunum_leil_fake", "golvvarme"):
        hass.states.async_set(f"climate.{entity_id}", "heat", modes)

    def ids(config: HeatConfig, group: str) -> list[str]:
        return [row["entity_id"] for row in heat_choices(hass, config, None, None)[group]]

    for group in ("zones", "setpoint"):
        assert ids(HeatConfig(), group) == ["climate.golvvarme"]
    # What the person already saved stays, and is listed first.
    saved = HeatConfig(zones=(Zone("climate.balboa_spa_fake"),))
    assert ids(saved, "zones") == ["climate.balboa_spa_fake", "climate.golvvarme"]
