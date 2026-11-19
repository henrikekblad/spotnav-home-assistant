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

from custom_components.spotnav.api.house_now import solar_power_ids, state_kw, websocket_get_house_now
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
    # Meters read at different moments may cross below zero: never a negative house.
    assert house_now(villa(grid_kw=-3.0, battery_kw=0.0, sun_kw=1.0))["house_kw"] == 0.0


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


def test_contract_fixtures() -> None:
    _fixture("villa_now.json", house_now(villa()))
    _fixture("villa_export.json", house_now(villa(grid_kw=-2.6, sun_kw=4.8, sun_from="recent", battery_kw=1.2, soc_pct=81.0, car_kw=0.0, heat_kw=0.6, car_connected=False)))
    _fixture("villa_car.json", house_now(villa(grid_kw=7.6, sun_kw=0.0, battery_kw=-1.5, car_kw=7.4, heat_kw=0.8)))
    _fixture(
        "house_alone_now.json",
        house_now(
            villa(sun_kw=None, sun_from=None, battery_kw=None, soc_pct=None, car_kw=None, heat_kw=None, heat_from=None,
                  has_sun=False, has_battery=False, has_car=False, has_heat=False, grid_kw=1.8)
        ),
    )
