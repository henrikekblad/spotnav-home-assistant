"""The house card's live flows (`site/house_now.py`, `api/house_now.py`): the house as the balance of the readings,
the fallbacks for the sun and the heat, the command end to end on the pilot's site with its refusals, and the
contract fixtures the card reads (`tests/fixtures/house_now/`, `SPOTNAV_WRITE_FIXTURES=1` rewrites them)."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from homeassistant.core import HomeAssistant

import random

from custom_components.spotnav.api.house_now import (
    plant_is_all_sun,
    solar_live_ids,
    solar_power_ids,
    state_kw,
    sun_kw_of,
    websocket_get_house_now,
)
from custom_components.spotnav.site.house_now import (
    NOW_KEYS,
    RECENT_MAX,
    HouseNowInputs,
    house_now,
    recent_kw,
)
from .helpers import answers_of

from .test_demand_site import pilot_site, run

FIXTURES = Path(__file__).parent / "fixtures" / "house_now"
AT = datetime(2026, 11, 16, 16, 20, tzinfo=timezone.utc)


def _fixture(name: str, payload: dict[str, Any]) -> None:
    path = FIXTURES / name
    payload = json.loads(json.dumps(payload))
    if os.environ.get("SPOTNAV_WRITE_FIXTURES") == "1":
        FIXTURES.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    assert json.loads(path.read_text(encoding="utf-8")) == payload, f"{name} drifted from the backend"


def villa(**extra: Any) -> HouseNowInputs:
    base: dict[str, Any] = dict(
        site_id="site",
        at=AT,
        grid_kw=0.4,
        sun_kw=1.2,
        sun_from="power",
        battery_kw=-1.5,
        soc_pct=54.0,
        car_kw=0.0,
        heat_kw=1.1,
        heat_from="recent",
        has_sun=True,
        has_battery=True,
        has_car=True,
        has_heat=True,
        car_connected=True,
        car_soc_pct=73.0,
    )
    base.update(extra)
    return HouseNowInputs(**base)


def test_the_house_is_the_balance_of_the_readings() -> None:
    answer = house_now(villa())
    assert tuple(answer) == NOW_KEYS
    # 0.4 from the grid, 1.2 from the sun, 1.5 from the battery, nothing to the car.
    assert answer["house_kw"] == pytest.approx(3.1)
    assert answer["has"] == {"sun": True, "battery": True, "car": True, "heat": True}
    # Charging the car and the battery from an import: what is left is the house.
    assert house_now(villa(grid_kw=9.0, battery_kw=3.0, car_kw=4.0, sun_kw=0.0))["house_kw"] == pytest.approx(2.0)
    # Meters read at different moments may cross below zero: never a negative house, and the lines still meet.
    crossed = house_now(villa(grid_kw=-3.0, battery_kw=0.0, sun_kw=1.0))
    assert crossed["house_kw"] == 0.0 and crossed["sun_kw"] == 3.0 and crossed["estimated"] == ["sun", "heat"]


def test_a_battery_read_at_the_battery_is_drawn_as_read_and_the_inverter_stays_in_the_house() -> None:
    # The owner's plant at night, discharging 10 kW (DC) with 4.3 % lost on the way out: the house holds the loss.
    night = villa(grid_kw=0.2, sun_kw=0.0, battery_kw=-10.0, car_kw=0.0, battery_loss=(0.0346, 0.0431))
    answer = house_now(night)
    assert answer["battery_kw"] == -10.0 and answer["battery_ac_kw"] == pytest.approx(-9.57)
    assert answer["house_kw"] == pytest.approx(10.2) and answer["house_from"] == "balance"
    # Charging from the grid: the battery's AC side has the way-in loss on top; the house is the balance as read.
    charging = house_now(villa(grid_kw=13.7, sun_kw=0.0, battery_kw=10.57, car_kw=0.0, battery_loss=(0.0346, 0.0431)))
    assert charging["battery_ac_kw"] == pytest.approx(10.94, abs=0.01)
    assert charging["house_kw"] == pytest.approx(3.13, abs=0.01)
    # The lines add up with the battery as read.
    assert charging["grid_kw"] + charging["sun_kw"] == pytest.approx(charging["house_kw"] + charging["battery_kw"])
    # Read on the AC side: as read.
    assert house_now(villa())["battery_ac_kw"] == -1.5
    assert house_now(villa(has_battery=False, battery_kw=None))["battery_ac_kw"] is None
    assert house_now(villa(grid_kw=None))["house_from"] is None


def test_a_part_had_but_unread_leaves_the_house_unread_and_a_part_not_had_is_left_out() -> None:
    assert house_now(villa(sun_kw=None, sun_from=None))["house_kw"] is None
    assert house_now(villa(battery_kw=None))["house_kw"] is None
    assert house_now(villa(car_kw=None))["house_kw"] is None
    assert house_now(villa(grid_kw=None))["house_kw"] is None
    alone = house_now(
        villa(sun_kw=None, sun_from=None, battery_kw=None, soc_pct=None, car_kw=None, heat_kw=None, heat_from=None,
              has_sun=False, has_battery=False, has_car=False, has_heat=False, grid_kw=1.8)
    )
    assert alone["house_kw"] == 1.8
    assert (alone["sun_kw"], alone["battery_kw"], alone["soc_pct"], alone["car_kw"], alone["heat_kw"]) == (None,) * 5
    # A figure of a part the site does not have is never shown.
    assert house_now(villa(has_sun=False))["sun_kw"] is None and house_now(villa(has_sun=False))["sun_from"] is None


def test_the_car_is_plugged_in_with_its_level_and_none_without_chargers() -> None:
    answer = house_now(villa())
    assert (answer["car_connected"], answer["car_soc_pct"]) == (True, 73.0)
    assert house_now(villa(car_connected=False))["car_soc_pct"] is None
    alone = house_now(villa(has_car=False))
    assert (alone["car_connected"], alone["car_soc_pct"]) == (None, None)


def test_the_last_period_stands_in_only_while_recent() -> None:
    end = AT - timedelta(minutes=5)
    assert recent_kw(0.25, 15, end, AT) == pytest.approx(1.0)
    assert recent_kw(0.25, 15, AT - RECENT_MAX - timedelta(seconds=1), AT) is None
    assert recent_kw(None, 15, end, AT) is None and recent_kw(0.2, 0, end, AT) is None


def test_the_solar_power_sensors_count_only_when_every_source_has_one() -> None:
    prefs = {"energy_sources": [{"type": "solar", "stat_energy_from": "sensor.pv_e", "stat_rate": "sensor.pv_w"}, {"type": "grid"}]}
    assert solar_power_ids(prefs) == ("sensor.pv_w",)
    prefs["energy_sources"].append({"type": "solar", "stat_energy_from": "sensor.pv2_e"})
    assert solar_power_ids(prefs) == ()
    assert solar_power_ids(None) == ()


async def test_a_power_state_is_read_in_kw(hass: HomeAssistant) -> None:
    hass.states.async_set("sensor.pv_w", "1500", {"unit_of_measurement": "W"})
    hass.states.async_set("sensor.pv_kw", "1.5", {"unit_of_measurement": "kW"})
    hass.states.async_set("sensor.off", "unavailable", {"unit_of_measurement": "W"})
    hass.states.async_set("sensor.energy", "12", {"unit_of_measurement": "kWh"})
    assert state_kw(hass, "sensor.pv_w") == pytest.approx(1.5)
    assert state_kw(hass, "sensor.pv_kw") == pytest.approx(1.5)
    assert state_kw(hass, "sensor.off") is None and state_kw(hass, "sensor.energy") is None
    assert state_kw(hass, "sensor.none") is None


class _Connection:
    def __init__(self) -> None:
        self.answers: list[dict[str, Any]] = []

    def send_result(self, message_id: int, result: Any) -> None:
        self.answers.append({"id": message_id, "success": True, "result": json.loads(json.dumps(result))})

    def send_error(self, message_id: int, code: str, message: str) -> None:
        self.answers.append({"id": message_id, "success": False, "error": {"code": code, "message": message}})


async def ask(hass: HomeAssistant, message_id: int, **request: Any) -> dict[str, Any]:
    connection = _Connection()
    websocket_get_house_now(hass, connection, {"id": message_id, "type": "spotnav/get_house_now", **request})  # type: ignore[arg-type]
    (answer,) = await answers_of(hass, connection)
    return answer


@pytest.mark.usefixtures("offline_relay")
async def test_the_pilot_site_is_answered_and_refused(hass: HomeAssistant, freezer) -> None:
    site = await pilot_site(hass, freezer, import_target_kw=6.0)
    await run(hass, freezer, 60, 4000.0)
    answer = await ask(hass, 1, api_version=1, charger_id="entry_a")
    assert answer["success"], answer
    result = answer["result"]
    assert tuple(result) == NOW_KEYS and result["site_id"] == site.entry_id
    assert result["has"] == {"sun": False, "battery": False, "car": True, "heat": False}
    assert result["sun_kw"] is None and result["battery_kw"] is None
    assert result["car_connected"] in (True, False, None)
    assert (await ask(hass, 2, api_version=1, site_id=site.entry_id))["result"]["site_id"] == site.entry_id
    for index, (request, code) in enumerate(
        [
            ({"charger_id": "entry_a"}, "spotnav_unsupported_api_version"),
            ({"api_version": 1}, "spotnav_charger_required"),
            ({"api_version": 1, "site_id": "entry_a"}, "spotnav_unknown_site"),
        ],
        start=10,
    ):
        refused = await ask(hass, index, **request)
        assert not refused["success"] and refused["error"]["code"] == code, request
    assert await hass.config_entries.async_unload(site.entry_id)
    refused = await ask(hass, 99, api_version=1, site_id=site.entry_id)
    assert not refused["success"] and refused["error"]["code"] == "spotnav_no_site"


def _meets(answer: dict[str, Any]) -> None:
    """The node balance as drawn: into the house what goes out of it, and the heat a part of the house."""
    grid, sun, battery, car = (answer[key] or 0.0 for key in ("grid_kw", "sun_kw", "battery_kw", "car_kw"))
    assert grid + sun == pytest.approx(answer["house_kw"] + car + battery, abs=1e-9)
    assert answer["house_kw"] >= 0 and answer["rest_kw"] >= 0 and sun >= 0 and car >= 0
    heat = answer["heat_kw"] or 0.0
    assert 0 <= heat <= answer["house_kw"] and heat + answer["rest_kw"] == pytest.approx(answer["house_kw"], abs=1e-9)


# The mismatches the owner's site showed on 2026-11-27 (synthetic figures of their shape).
STALE_PARTS = dict(grid_kw=0.0, sun_kw=0.6, sun_from="recent", battery_kw=-1.44, car_kw=0.0, heat_kw=1.77,
                   heat_from="recent", car_connected=False)
HEAT_OVER = dict(grid_kw=0.1, sun_kw=0.0, battery_kw=-1.2, car_kw=0.0, heat_kw=1.6, heat_from="power",
                 car_connected=False)
CAR_LAGS = dict(grid_kw=0.5, sun_kw=0.0, battery_kw=0.0, car_kw=2.0, heat_kw=0.0, heat_from="power")
SUN_LAGS = dict(grid_kw=-3.0, sun_kw=1.0, sun_from="recent", battery_kw=0.5, car_kw=0.0, heat_kw=0.4,
                heat_from="power", car_connected=False)


def test_the_last_period_parts_are_marked_and_the_heat_is_drawn_beside_the_rest() -> None:
    # The sun and the heat from a 5-minute period up to 20 minutes old: marked; the house's disc holds the rest.
    answer = house_now(villa(**STALE_PARTS))
    assert answer["house_kw"] == pytest.approx(2.04) and answer["heat_kw"] == 1.77
    assert answer["rest_kw"] == pytest.approx(0.27) and answer["estimated"] == ["sun", "heat"]
    _meets(answer)
    # Read now: nothing marked.
    assert house_now(villa(**{**STALE_PARTS, "sun_from": "power", "heat_from": "power"}))["estimated"] == []


def test_a_heat_larger_than_the_house_is_cut_to_it_and_marked() -> None:
    answer = house_now(villa(**HEAT_OVER))
    assert (answer["house_kw"], answer["heat_kw"], answer["rest_kw"]) == (1.3, 1.3, 0.0)
    assert answer["estimated"] == ["heat"]
    _meets(answer)


def test_a_deficit_is_taken_from_the_car_then_given_to_the_sun_the_battery_and_last_the_grid() -> None:
    lagging = house_now(villa(**CAR_LAGS))
    assert (lagging["car_kw"], lagging["house_kw"], lagging["estimated"]) == (0.5, 0.0, ["car"])
    _meets(lagging)
    exporting = house_now(villa(**SUN_LAGS))
    assert (exporting["sun_kw"], exporting["battery_kw"], exporting["grid_kw"]) == (3.5, 0.5, -3.0)
    assert exporting["house_kw"] == 0.0 and exporting["heat_kw"] == 0.0 and exporting["estimated"] == ["sun", "heat"]
    _meets(exporting)
    # Without the sun the battery takes it; with neither, the grid.
    no_sun = house_now(villa(**{**CAR_LAGS, "car_kw": 0.0, "grid_kw": -1.0, "has_sun": False, "sun_kw": None}))
    assert no_sun["battery_kw"] == -1.0 and no_sun["estimated"] == ["battery"]
    _meets(no_sun)
    bare = house_now(villa(grid_kw=-0.4, has_sun=False, sun_kw=None, has_battery=False, battery_kw=None, has_car=False,
                           car_kw=None, has_heat=False, heat_kw=None))
    assert bare["grid_kw"] == 0.0 and bare["estimated"] == ["grid"]
    _meets(bare)


def test_the_lines_meet_for_any_readings() -> None:
    rng = random.Random(20261127)
    for _ in range(2000):
        answer = house_now(
            villa(
                grid_kw=rng.uniform(-12, 17), sun_kw=rng.choice([rng.uniform(-0.2, 11), 0.0]),
                sun_from=rng.choice(["power", "recent"]), battery_kw=rng.uniform(-10, 10),
                car_kw=rng.choice([0.0, rng.uniform(0, 11)]), heat_kw=rng.uniform(0, 6),
                heat_from=rng.choice(["power", "recent"]), battery_loss=rng.choice([None, (0.035, 0.043)]),
            )
        )
        _meets(answer)
        for key in ("grid_kw", "sun_kw", "battery_kw", "car_kw", "heat_kw", "house_kw", "rest_kw"):
            assert answer[key] == round(answer[key], 2)


def test_the_plant_is_all_of_the_sun_only_when_every_solar_source_is_on_its_device(hass: HomeAssistant) -> None:
    from homeassistant.helpers import device_registry as dr, entity_registry as er
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(domain="sigen")
    entry.add_to_hass(hass)
    plant = dr.async_get(hass).async_get_or_create(config_entry_id=entry.entry_id, identifiers={("sigen", "plant")})
    other = dr.async_get(hass).async_get_or_create(config_entry_id=entry.entry_id, identifiers={("sigen", "other")})
    registry = er.async_get(hass)
    for unique, device in (("battery", plant), ("pv", plant), ("pv2", other)):
        registry.async_get_or_create("sensor", "sigen", unique, suggested_object_id=f"plant_{unique}", device_id=device.id)
    prefs = {"energy_sources": [{"type": "solar", "stat_energy_from": "sensor.plant_pv"}]}
    assert plant_is_all_sun(hass, prefs, "sensor.plant_battery")
    prefs["energy_sources"].append({"type": "solar", "stat_energy_from": "sensor.plant_pv2"})
    assert not plant_is_all_sun(hass, prefs, "sensor.plant_battery")
    assert not plant_is_all_sun(hass, {"energy_sources": []}, "sensor.plant_battery")
    assert not plant_is_all_sun(hass, prefs, None)


def test_a_heat_source_is_read_now_from_its_power_sensor(hass: HomeAssistant) -> None:
    from homeassistant.helpers import device_registry as dr, entity_registry as er
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.spotnav.site.heat_meter import live_power_id

    entry = MockConfigEntry(domain="mqtt")
    entry.add_to_hass(hass)
    devices = dr.async_get(hass)
    floor = devices.async_get_or_create(config_entry_id=entry.entry_id, identifiers={("mqtt", "floor")})
    plant = devices.async_get_or_create(config_entry_id=entry.entry_id, identifiers={("mqtt", "plant")})
    registry = er.async_get(hass)
    for unique, device, kind in (("floor_energy", floor, "energy"), ("floor_power", floor, "power"),
                                 ("plant_energy", plant, "energy"), ("plant_pv", plant, "power"),
                                 ("plant_load", plant, "power")):
        registry.async_get_or_create("sensor", "mqtt", unique, suggested_object_id=unique, device_id=device.id,
                                     original_device_class=kind)
    assert live_power_id(hass, "sensor.meter_w", "power", {}) == "sensor.meter_w"
    assert live_power_id(hass, "sensor.zone_e", "energy", {"sensor.zone_w": "sensor.zone_e"}) == "sensor.zone_w"
    # The one power sensor on the energy sensor's device; none told apart among two, and none without a device.
    assert live_power_id(hass, "sensor.floor_energy", "energy", {}) == "sensor.floor_power"
    assert live_power_id(hass, "sensor.plant_energy", "energy", {}) is None
    assert live_power_id(hass, "sensor.nowhere", "energy", {}) is None


def _solar_devices(hass: HomeAssistant) -> None:
    """Inverters as the registry has them: a plain one (energy + its one power sensor), a hybrid with a pv power among
    its battery and output powers, a hybrid where none is named pv, and one with two strings."""
    from homeassistant.helpers import device_registry as dr, entity_registry as er
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(domain="solarman")
    entry.add_to_hass(hass)
    devices = dr.async_get(hass)
    registry = er.async_get(hass)
    layout = {
        "plain": (("plain_yield", "energy"), ("plain_active_power", "power")),
        "hybrid": (("hybrid_pv_energy", "energy"), ("hybrid_pv_power", "power"), ("hybrid_battery_power", "power"),
                   ("hybrid_output_power", "power")),
        "vague": (("vague_energy", "energy"), ("vague_active_power", "power"), ("vague_battery_power", "power")),
        "strings": (("strings_pv1_energy", "energy"), ("strings_pv1_power", "power"), ("strings_pv2_power", "power")),
        "lonely": (("lonely_energy", "energy"), ("lonely_grid_power", "power")),
    }
    for name, sensors in layout.items():
        device = devices.async_get_or_create(config_entry_id=entry.entry_id, identifiers={("solarman", name)})
        for unique, kind in sensors:
            registry.async_get_or_create("sensor", "solarman", unique, suggested_object_id=unique,
                                         device_id=device.id, original_device_class=kind)


def _solar(*stats: str, rate: str | None = None) -> dict[str, Any]:
    sources: list[dict[str, Any]] = [{"type": "solar", "stat_energy_from": stat} for stat in stats]
    if rate:
        sources[0]["stat_rate"] = rate
    return {"energy_sources": [*sources, {"type": "grid"}]}


async def test_a_solar_source_is_read_now_from_the_power_sensor_on_its_device(hass: HomeAssistant) -> None:
    _solar_devices(hass)
    # The device's only power sensor; the one named pv among a hybrid's; the pv string paired by its name.
    assert solar_live_ids(hass, _solar("sensor.plain_yield")) == ("sensor.plain_active_power",)
    assert solar_live_ids(hass, _solar("sensor.hybrid_pv_energy")) == ("sensor.hybrid_pv_power",)
    assert solar_live_ids(hass, _solar("sensor.strings_pv1_energy")) == ("sensor.strings_pv1_power",)
    assert solar_live_ids(hass, _solar("sensor.plain_yield", "sensor.hybrid_pv_energy")) == (
        "sensor.plain_active_power", "sensor.hybrid_pv_power")
    # The Energy dashboard's own power sensor first, the device's for the other source.
    assert solar_live_ids(hass, _solar("sensor.vague_energy", "sensor.plain_yield", rate="sensor.vague_w")) == (
        "sensor.vague_w", "sensor.plain_active_power")
    # Not guessed: several and none named pv (a total may hold the battery), a grid power alone, no device.
    assert solar_live_ids(hass, _solar("sensor.vague_energy")) == ()
    assert solar_live_ids(hass, _solar("sensor.lonely_energy")) == ()
    assert solar_live_ids(hass, _solar("sensor.nowhere")) == ()
    # One of two sources without one: none of the sun is read live.
    assert solar_live_ids(hass, _solar("sensor.plain_yield", "sensor.vague_energy")) == ()


async def test_the_sun_from_found_sensors_is_only_a_plausible_production(hass: HomeAssistant) -> None:
    hass.states.async_set("sensor.a", "1500", {"unit_of_measurement": "W", "state_class": "measurement"})
    hass.states.async_set("sensor.b", "0.5", {"unit_of_measurement": "kW"})
    hass.states.async_set("sensor.tiny", "-3", {"unit_of_measurement": "W"})
    hass.states.async_set("sensor.neg", "-1200", {"unit_of_measurement": "W"})
    hass.states.async_set("sensor.kwh", "12", {"unit_of_measurement": "kWh", "state_class": "total_increasing"})
    hass.states.async_set("sensor.total", "1200", {"unit_of_measurement": "W", "state_class": "total"})
    assert sun_kw_of(hass, ["sensor.a", "sensor.b", "sensor.tiny"]) == pytest.approx(2.0)
    for wrong in ("sensor.neg", "sensor.kwh", "sensor.total", "sensor.unread"):
        assert sun_kw_of(hass, ["sensor.a", wrong]) is None, wrong
    assert sun_kw_of(hass, []) is None


@pytest.mark.usefixtures("offline_relay")
async def test_the_pilot_site_reads_its_inverters_sun_now_or_marks_the_period_estimated(
    hass: HomeAssistant, freezer, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    import custom_components.spotnav.api.house_now as module

    site = await pilot_site(hass, freezer, import_target_kw=6.0)
    await run(hass, freezer, 60, 4000.0)
    _solar_devices(hass)
    hass.states.async_set("sensor.plain_active_power", "2400", {"unit_of_measurement": "W", "state_class": "measurement"})
    prefs: dict[str, Any] = {}

    async def energy_prefs(_hass: HomeAssistant) -> dict[str, Any]:
        return prefs

    async def day_flow(_hass: HomeAssistant, _entry_id: str, controller: Any) -> Any:
        at = controller.demand.now()
        return SimpleNamespace(periods=[SimpleNamespace(solar=0.25, heat=None, minutes=15, end=at)])

    monkeypatch.setattr(module, "async_energy_prefs", energy_prefs)
    monkeypatch.setattr(module, "async_cached_day_flow", day_flow)
    prefs.update(_solar("sensor.plain_yield"))
    live = (await ask(hass, 1, api_version=1, site_id=site.entry_id))["result"]
    assert live["sun_from"] == "power" and live["sun_kw"] == pytest.approx(2.4) and "sun" not in live["estimated"]
    for stats in (("sensor.vague_energy",), ("sensor.plain_yield", "sensor.vague_energy")):
        prefs.clear()
        prefs.update(_solar(*stats))
        stale = (await ask(hass, 2, api_version=1, site_id=site.entry_id))["result"]
        assert stale["sun_from"] == "recent" and stale["sun_kw"] == pytest.approx(1.0), stats
        assert "sun" in stale["estimated"], stats


def test_contract_fixtures() -> None:
    _fixture("villa_stale_parts.json", house_now(villa(**STALE_PARTS)))
    _fixture("villa_heat_over.json", house_now(villa(**HEAT_OVER)))
    _fixture("villa_car_lags.json", house_now(villa(**CAR_LAGS)))
    _fixture("villa_now.json", house_now(villa()))
    _fixture("villa_export.json", house_now(villa(grid_kw=-2.6, sun_kw=4.8, sun_from="recent", battery_kw=1.2, soc_pct=81.0, car_kw=0.0, heat_kw=0.6, car_connected=False)))
    _fixture("villa_car.json", house_now(villa(grid_kw=7.6, sun_kw=0.0, battery_kw=-1.5, car_kw=7.4, heat_kw=0.8)))
    _fixture(
        "villa_dc_battery.json",
        house_now(
            villa(grid_kw=0.2, sun_kw=0.0, sun_from="recent", battery_kw=-10.0, soc_pct=62.0, car_kw=0.0, heat_kw=1.4,
                  car_connected=False, battery_loss=(0.0346, 0.0431))
        ),
    )
    _fixture(
        "house_alone_now.json",
        house_now(
            villa(sun_kw=None, sun_from=None, battery_kw=None, soc_pct=None, car_kw=None, heat_kw=None, heat_from=None,
                  has_sun=False, has_battery=False, has_car=False, has_heat=False, grid_kw=1.8)
        ),
    )
