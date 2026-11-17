"""The pie behind "House" in the house card's energy flow (`site/house_loads.py`, `api/house_loads.py`): which of the
Energy dashboard's individual devices count, their energy over the flow's span, "Övrigt" and its clamp, and the
contract fixtures the card reads (`tests/fixtures/house_loads/`, `SPOTNAV_WRITE_FIXTURES=1` rewrites them)."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from homeassistant.core import HomeAssistant

from custom_components.spotnav.site.heat_meter import KIND_ENERGY, STATUS_FOUND, HeatMeter
from custom_components.spotnav.site.house_loads import (
    LOAD_KEYS,
    LOADS_KEYS,
    DashboardMeter,
    counted_meters,
    house_loads,
    meter_kwh,
)

from .test_demand_site import pilot_site, run

FIXTURES = Path(__file__).parent / "fixtures" / "house_loads"
UTC = timezone.utc


def _fixture(name: str, payload: dict[str, Any]) -> None:
    path = FIXTURES / name
    payload = json.loads(json.dumps(payload))
    if os.environ.get("SPOTNAV_WRITE_FIXTURES") == "1":
        FIXTURES.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    assert json.loads(path.read_text(encoding="utf-8")) == payload, f"{name} drifted from the backend"


DISH = DashboardMeter("sensor.diskmaskin_energi", "Diskmaskin", device_id="dev_dish")
WASHER = DashboardMeter("sensor.tvattmaskin_energi", "Tvättmaskin", device_id="dev_washer")
DRYER = DashboardMeter("sensor.torktumlare_energi", "Torktumlare", device_id="dev_dryer")


# ------------------------------------------------------------------------------------------------ which meters


def test_the_chargers_and_the_heat_pumps_meters_are_left_out() -> None:
    charger_register = DashboardMeter("sensor.easee_lifetime_energy", "Easee", device_id="dev_easee")
    charger_other = DashboardMeter("sensor.easee_session_energy", "Easee session", device_id="dev_easee")
    by_power = DashboardMeter("sensor.laddbox_kwh", "Laddbox", rate_id="sensor.laddbox_power")
    heat = DashboardMeter("sensor.nibe_energy", "Värmepump", rate_id="sensor.nibe_power", device_id="dev_nibe")
    # The heat pump's meter is told by its sensor alone: a multi-channel meter's other circuits stay counted.
    clamp = DashboardMeter("sensor.shelly_ch2_energy", "Spis", device_id="dev_nibe")
    counted = counted_meters(
        [DISH, charger_register, charger_other, by_power, heat, clamp, WASHER],
        car_entities={"sensor.easee_lifetime_energy", "sensor.laddbox_power"},
        car_devices={"dev_easee"},
        heat_entities={"sensor.nibe_power"},
    )
    assert counted == [DISH, clamp, WASHER]


def test_a_nested_device_is_held_by_its_parent_and_a_statistic_counts_once() -> None:
    kitchen = DashboardMeter("sensor.kok_energi", "Kök")
    nested = DashboardMeter("sensor.diskmaskin_energi", "Diskmaskin", included_in="sensor.kok_energi")
    orphan = DashboardMeter("sensor.frys_energi", "Frys", included_in="sensor.borta")
    assert counted_meters([kitchen, nested, orphan, kitchen]) == [kitchen, orphan]


# ------------------------------------------------------------------------------------------------ figures


def test_whole_hours_are_read_by_the_hour_and_the_running_one_by_five_minutes() -> None:
    start = datetime(2026, 11, 16, 23, tzinfo=UTC)
    through = start + timedelta(hours=2, minutes=20)
    hours = [{"start": start, "change": 0.4}, {"start": start + timedelta(hours=1), "change": 0.6}]
    fives = [{"start": start + timedelta(minutes=5 * index), "change": 0.05} for index in range(28)]
    # Hours 1 and 2 by their hourly rows (1.0), the running hour's four rows (0.2); a row after `through` is left out.
    assert meter_kwh(hours, fives, start, through) == pytest.approx(1.2)
    # An hour with no hourly row yet is its 5-minute rows; a row before the span is not counted.
    early = [{"start": start - timedelta(hours=1), "change": 9.0}]
    assert meter_kwh(early + hours[:1], fives, start, through) == pytest.approx(0.4 + 12 * 0.05 + 4 * 0.05)
    assert meter_kwh([{"start": start, "change": None}, {"start": "x", "change": 1.0}], [], start, through) == 0.0


def test_ovrigt_is_the_house_less_the_meters() -> None:
    answer = house_loads(
        site_id="site", period="day", first="2026-11-17", end="2026-11-18",
        through=datetime(2026, 11, 17, 12, tzinfo=UTC), house_kwh=10.0,
        loads=[(WASHER, 0.8), (DISH, 1.25), (DRYER, 0.0)],
    )
    assert tuple(answer) == LOADS_KEYS and all(tuple(row) == LOAD_KEYS for row in answer["loads"])
    assert [row["name"] for row in answer["loads"]] == ["Diskmaskin", "Tvättmaskin", "Torktumlare"]
    assert (answer["meters_kwh"], answer["other_kwh"], answer["over_kwh"]) == (2.05, 7.95, 0.0)


def test_meters_over_the_house_clamp_ovrigt_at_zero() -> None:
    answer = house_loads(
        site_id="site", period="month", first="2026-11-01", end="2026-12-01",
        through=datetime(2026, 11, 17, 12, tzinfo=UTC), house_kwh=1.0, loads=[(DISH, 1.5)],
    )
    assert (answer["other_kwh"], answer["over_kwh"]) == (0.0, 0.5)
    # A rounding difference is no mismatch.
    near = house_loads(
        site_id="site", period="day", first="2026-11-17", end="2026-11-18", through=None, house_kwh=1.0,
        loads=[(DISH, 1.004)],
    )
    assert (near["other_kwh"], near["over_kwh"]) == (0.0, 0.0)


def test_no_devices_and_no_flow() -> None:
    alone = house_loads(
        site_id="site", period="year", first="2026-01-01", end="2027-01-01",
        through=datetime(2026, 11, 17, 12, tzinfo=UTC), house_kwh=4321.0, loads=[],
    )
    assert (alone["loads"], alone["meters_kwh"], alone["other_kwh"]) == ([], 0, 4321.0)
    blind = house_loads(
        site_id="site", period="day", first="2026-11-17", end="2026-11-18", through=None, house_kwh=None,
        loads=[(DISH, 0.3)],
    )
    assert (blind["house_kwh"], blind["other_kwh"], blind["over_kwh"], blind["through"]) == (None, None, 0.0, None)


# ------------------------------------------------------------------------------------------------ end to end


class _Connection:
    def __init__(self) -> None:
        self.answers: list[dict[str, Any]] = []

    def send_result(self, message_id: int, result: Any) -> None:
        self.answers.append({"id": message_id, "success": True, "result": json.loads(json.dumps(result))})

    def send_error(self, message_id: int, code: str, message: str) -> None:
        self.answers.append({"id": message_id, "success": False, "error": {"code": code, "message": message}})


async def ask(hass: HomeAssistant, message_id: int, **request: Any) -> dict[str, Any]:
    from custom_components.spotnav.api.house_loads import websocket_get_house_loads

    connection = _Connection()
    websocket_get_house_loads(hass, connection, {"id": message_id, "type": "spotnav/get_house_loads", **request})  # type: ignore[arg-type]
    await hass.async_block_till_done()
    (answer,) = connection.answers
    return answer


@pytest.mark.usefixtures("offline_relay")
async def test_the_pie_of_a_day_and_a_month_end_to_end(hass: HomeAssistant, freezer, monkeypatch) -> None:
    """The house block of the period's flow, the dashboard's devices less the charger's and the heat pump's, read by
    the hour over the flow's span, kept until the flow moves on; refusals as the history's."""
    from custom_components.spotnav.api import house as house_api
    from custom_components.spotnav.api import house_history as history_api
    from custom_components.spotnav.api import house_loads as loads_api
    from custom_components.spotnav.planning.demand_charge import hour_start
    from custom_components.spotnav.runtime import site_controller_for

    site = await pilot_site(hass, freezer, import_target_kw=6.0)
    await run(hass, freezer, 60, 4000.0)
    controller = site_controller_for(hass, site.entry_id)
    assert controller is not None
    current = hour_start(controller.demand.now(), controller.demand.tracker.tz)
    last = current - timedelta(hours=1)
    prefs = {
        "energy_sources": [{"type": "grid", "stat_energy_from": "sensor.import", "stat_energy_to": "sensor.export"}],
        "device_consumption": [
            {"stat_consumption": "sensor.diskmaskin_energi", "name": "Diskmaskin"},
            {"stat_consumption": "sensor.tvattmaskin_energi"},
            {"stat_consumption": "sensor.laddbox_energi", "name": "Laddbox"},
            {"stat_consumption": "sensor.varmepump_energi", "name": "Värmepump"},
        ],
    }
    hass.states.async_set("sensor.tvattmaskin_energi", "12.0", {"friendly_name": "Tvättmaskin"})
    changes = {
        "sensor.import": 3.0,
        "sensor.export": 0.0,
        "sensor.diskmaskin_energi": 0.5,
        "sensor.tvattmaskin_energi": 0.25,
        "sensor.laddbox_energi": 2.0,
        "sensor.varmepump_energi": 1.0,
    }
    reads: list[tuple[tuple[str, ...], str]] = []

    async def fake_prefs(_hass):
        return prefs

    async def fake_statistics(_hass, ids, start, end, period):
        reads.append((tuple(sorted(ids)), period))
        return {i: [{"start": last, "change": changes.get(i, 0.0)}] for i in ids}

    for module in (house_api, history_api, loads_api):
        monkeypatch.setattr(module, "async_energy_prefs", fake_prefs)
    monkeypatch.setattr(house_api, "async_statistics", fake_statistics)
    monkeypatch.setattr(loads_api, "async_statistics", fake_statistics)

    async def fake_heat(_hass, _meter, _start, _end, _period="hour"):
        return {last: 1.0}

    monkeypatch.setattr(house_api, "async_heat_hours", fake_heat)
    monkeypatch.setattr(loads_api, "controller_for", lambda _hass, _id: SimpleNamespace(energy_register_entity_id="sensor.laddbox_energi"))
    controller.house_forecast.heat.meter = HeatMeter(STATUS_FOUND, "sensor.varmepump_energi", KIND_ENERGY)

    answer = await ask(hass, 1, api_version=1, site_id=site.entry_id)
    assert answer["success"], answer
    result = answer["result"]
    assert tuple(result) == LOADS_KEYS
    assert (result["period"], result["through"]) == ("day", current.isoformat())
    assert [(row["name"], row["kwh"]) for row in result["loads"]] == [("Diskmaskin", 0.5), ("Tvättmaskin", 0.25)]
    # The flow's house block, with the heat pump's hour split out of it as the flow shows it.
    assert (result["house_kwh"], result["other_kwh"], result["over_kwh"]) == (2.0, 1.25, 0.0)
    loads_reads = [read for read in reads if "sensor.diskmaskin_energi" in read[0]]
    assert loads_reads == [(("sensor.diskmaskin_energi", "sensor.tvattmaskin_energi"), "hour")]
    # Asked again before the flow moves on: kept, nothing read.
    assert (await ask(hass, 2, api_version=1, charger_id="entry_a"))["result"] == result
    assert len([read for read in reads if "sensor.diskmaskin_energi" in read[0]]) == 1

    month = (await ask(hass, 3, api_version=1, site_id=site.entry_id, period="month"))["result"]
    assert month["period"] == "month" and month["date"].endswith("-01")
    assert month["house_kwh"] == 2.0 and [row["kwh"] for row in month["loads"]] == [0.5, 0.25]

    prefs["device_consumption"] = []
    alone = (await ask(hass, 4, api_version=1, site_id=site.entry_id))["result"]
    assert (alone["loads"], alone["other_kwh"]) == ([], 2.0)

    for index, (request, code) in enumerate(
        [
            ({"site_id": site.entry_id}, "spotnav_unsupported_api_version"),
            ({"api_version": 1, "site_id": site.entry_id, "period": "week"}, "spotnav_invalid_date"),
            ({"api_version": 1, "site_id": site.entry_id, "date": "2099-01-01"}, "spotnav_invalid_date"),
            ({"api_version": 1, "site_id": "nope"}, "spotnav_unknown_site"),
        ],
        start=10,
    ):
        refused = await ask(hass, index, **request)
        assert not refused["success"] and refused["error"]["code"] == code, request


# ------------------------------------------------------------------------------------------------ fixtures


def test_contract_fixtures() -> None:
    through = datetime(2026, 11, 17, 13, 20, tzinfo=UTC)
    _fixture(
        "villa_day.json",
        house_loads(
            site_id="site_villa", period="day", first="2026-11-17", end="2026-11-18", through=through,
            house_kwh=14.62,
            loads=[
                (DISH, 1.42),
                (WASHER, 0.96),
                (DRYER, 2.31),
                (DashboardMeter("sensor.frys_energi", "Frys"), 0.21),
                (DashboardMeter("sensor.router_energi", "Router"), 0.12),
            ],
        ),
    )
    _fixture(
        "meters_over_house.json",
        house_loads(
            site_id="site_villa", period="month", first="2026-11-01", end="2026-12-01",
            through=datetime(2026, 11, 17, 13, tzinfo=UTC), house_kwh=40.0, loads=[(DISH, 25.0), (DRYER, 18.5)],
        ),
    )
    _fixture(
        "no_devices.json",
        house_loads(
            site_id="site_villa", period="year", first="2026-01-01", end="2027-01-01",
            through=datetime(2026, 11, 17, 13, tzinfo=UTC), house_kwh=6120.4, loads=[],
        ),
    )
