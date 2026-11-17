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
    IMPLAUSIBLE_KEYS,
    KIND_HEAT,
    LOAD_KEYS,
    LOADS_KEYS,
    MAX_BELONGS,
    MOVED_KEYS,
    SOURCE_HEAT_PUMP,
    SOURCE_ZONE,
    DashboardMeter,
    MeterRead,
    counted_meters,
    house_loads,
    merged_belongs,
    meter_kwh,
    plausible_kwh,
    split_belongs,
    stored_belongs,
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


def test_a_heat_zone_s_sensor_or_device_is_counted_only_in_the_heat() -> None:
    zone_energy = DashboardMeter("sensor.golvvarme_tvattstuga_energy", "Golvvärme tvättstuga", device_id="dev_golv")
    # Another sensor of the thermostat's own device (its power, as the dashboard's device): the heat's too.
    zone_other = DashboardMeter("sensor.golvvarme_tvattstuga_kwh_2", "Golv", device_id="dev_golv")
    shelly = DashboardMeter("sensor.shelly_golv_energy", "Shelly golvvärme", device_id="dev_shelly")
    counted = counted_meters(
        [DISH, zone_energy, zone_other, shelly],
        heat_entities={"sensor.golvvarme_tvattstuga_energy"},
        heat_devices={"dev_golv", ""},
    )
    # The Shelly in the fuse box on the floor heating's circuit cannot be tied by itself: the household chooses.
    assert counted == [DISH, shelly]


def test_where_a_meter_belongs_is_the_household_s_choice() -> None:
    nas = DashboardMeter("sensor.nas_energy", "NAS")
    shelly = DashboardMeter("sensor.shelly_golv_energy", "Shelly golvvärme")
    belongs = stored_belongs(
        {"meter_belongs": {"sensor.shelly_golv_energy": "heat", "sensor.nas_energy": "none", "sensor.x": "house", 3: "car"}}
    )
    assert belongs == {"sensor.shelly_golv_energy": "heat", "sensor.nas_energy": "none"}
    assert stored_belongs({}) == {} and stored_belongs({"meter_belongs": ["x"]}) == {}
    shown, moved = split_belongs([DISH, nas, shelly], belongs)
    assert shown == [DISH] and moved == [(nas, "none"), (shelly, "heat")]
    # Merging: the house brings one back, anything else is refused whole.
    assert merged_belongs(belongs, {"sensor.nas_energy": "house", "sensor.laddbox": "car"}) == {
        "sensor.shelly_golv_energy": "heat",
        "sensor.laddbox": "car",
    }
    for bad in ({}, [], {"sensor.x": "kitchen"}, {"": "heat"}, {"sensor.x": None}):
        assert merged_belongs(belongs, bad) is None
    full = {f"sensor.m{index}": "none" for index in range(MAX_BELONGS)}
    assert merged_belongs(full, {"sensor.one_more": "heat"}) is None


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


# ------------------------------------------------------------------------------------------------ hours that cannot be


START = datetime(2026, 10, 1, tzinfo=UTC)


def _hours(values: list[float]) -> dict[datetime, float]:
    return {START + timedelta(hours=index): value for index, value in enumerate(values)}


def test_a_glitch_hour_is_dropped_and_ovrigt_stays_right() -> None:
    house = _hours([1.2, 0.9, 1.1, 1.0])
    nas = _hours([0.05, 378.0, 0.05, 0.05])
    read = plausible_kwh(nas, {}, START, START + timedelta(hours=4), limits=house, cap_kwh=13.8)
    assert (round(read.kwh, 3), read.hours, read.dropped, read.left_out) == (0.15, 4, 1, False)
    meter = DashboardMeter("sensor.smart_energy_switch_electric_consumption_kwh", "NAS")
    answer = house_loads(
        site_id="site", period="month", first="2026-10-01", end="2026-11-01",
        through=START + timedelta(hours=4), house_kwh=4.2, loads=[(meter, read.kwh), (DISH, 1.0)],
        implausible=[(meter, read), (DISH, MeterRead(1.0, 4, 0))],
    )
    assert answer["implausible"] == [
        {"statistic_id": "sensor.smart_energy_switch_electric_consumption_kwh", "name": "NAS", "hours": 1, "left_out": False}
    ]
    assert tuple(answer["implausible"][0]) == IMPLAUSIBLE_KEYS
    assert (answer["meters_kwh"], answer["other_kwh"], answer["over_kwh"]) == (1.15, 3.05, 0.0)


def test_a_meter_that_is_always_plausible_is_unchanged() -> None:
    house = _hours([1.2, 0.9, 1.1, 1.0])
    dish = _hours([0.5, 0.0, 1.0, 1.15])  # 1.15 is over 1.0 but no gross excess
    read = plausible_kwh(dish, {}, START, START + timedelta(hours=4), limits=house, cap_kwh=13.8)
    assert (read.kwh, read.dropped) == (pytest.approx(2.65), 0)
    assert read.kwh == pytest.approx(meter_kwh([{"start": k, "change": v} for k, v in dish.items()], [], START, START + timedelta(hours=4)))
    # Without the flow's hours only the fuse bounds an hour; without either, nothing is dropped.
    assert plausible_kwh(_hours([20.0, 0.5]), {}, START, START + timedelta(hours=2), cap_kwh=13.8).dropped == 1
    assert plausible_kwh(_hours([20.0, 0.5]), {}, START, START + timedelta(hours=2)).dropped == 0


def test_a_meter_with_no_plausible_hour_is_left_out_with_its_line() -> None:
    house = _hours([1.0, 1.0])
    read = plausible_kwh(_hours([50.0, 60.0]), {}, START, START + timedelta(hours=2), limits=house)
    assert (read.kwh, read.dropped, read.left_out) == (0.0, 2, True)
    broken = DashboardMeter("sensor.nas", "NAS")
    answer = house_loads(
        site_id="site", period="day", first="2026-10-01", end="2026-10-02", through=START + timedelta(hours=2),
        house_kwh=2.0, loads=[(broken, 0.0), (DISH, 0.5)], implausible=[(broken, read)],
    )
    assert [row["name"] for row in answer["loads"]] == ["Diskmaskin"]
    assert answer["implausible"] == [{"statistic_id": "sensor.nas", "name": "NAS", "hours": 2, "left_out": True}]
    assert answer["other_kwh"] == 1.5


def test_the_running_hour_is_judged_by_its_five_minute_rows() -> None:
    fives = {START + timedelta(minutes=5 * index): 0.5 for index in range(6)}
    read = plausible_kwh({}, fives, START, START + timedelta(minutes=30), limits={START: 0.2})
    assert (read.hours, read.dropped) == (1, 1)
    assert plausible_kwh({}, fives, START, START + timedelta(minutes=30), limits={START: 2.0}).kwh == pytest.approx(3.0)


def test_only_a_gross_excess_over_the_site_s_use_is_a_broken_statistic() -> None:
    site = _hours([2.0, 0.3, 0.3, 0.3])
    # A dashboard device that also measures the car: as much as the site, counted.
    car_too = plausible_kwh(_hours([2.0, 0.3, 0.3, 0.3]), {}, START, START + timedelta(hours=4), limits=site)
    assert (car_too.kwh, car_too.dropped) == (pytest.approx(2.9), 0)
    # A meter that lags into the next hour: 1.2 kWh in a 0.3 kWh hour is four times it but only 0.9 kWh over it.
    lag = plausible_kwh(_hours([0.0, 1.2, 0.3, 0.3]), {}, START, START + timedelta(hours=4), limits=site)
    assert (lag.kwh, lag.dropped) == (pytest.approx(1.8), 0)
    # More than twice it but within a kWh of it, or over a kWh above it but under twice it: counted.
    assert plausible_kwh(_hours([3.9]), {}, START, START + timedelta(hours=1), limits={START: 2.0}).dropped == 0
    assert plausible_kwh(_hours([1.29]), {}, START, START + timedelta(hours=1), limits={START: 0.3}).dropped == 0
    # Both: broken.
    assert plausible_kwh(_hours([4.1]), {}, START, START + timedelta(hours=1), limits={START: 2.0}).dropped == 1
    assert plausible_kwh(_hours([1.31]), {}, START, START + timedelta(hours=1), limits={START: 0.3}).dropped == 1


def test_a_meter_s_hour_is_judged_against_the_site_s_whole_use() -> None:
    from custom_components.spotnav.api.house_loads import _limits
    from custom_components.spotnav.site.energy_flow import EnergyHour

    # 9 kWh from the grid: 6 to the car, 2 to the heat, 1 to the house; a quarter of the next hour.
    hours = [
        EnergyHour(START, grid_in=9.0, grid_out=0.0, car=6.0, heat=2.0),
        EnergyHour(START + timedelta(hours=1), grid_in=0.5, grid_out=0.0, car=0.0, heat=0.0, minutes=15),
        EnergyHour(START + timedelta(hours=1, minutes=15), grid_in=0.25, grid_out=0.0, minutes=15),
    ]
    limits = _limits(hours)
    assert limits == {START: pytest.approx(9.0), START + timedelta(hours=1): pytest.approx(0.75)}
    # A device on the dashboard that measures the car too (8 kWh in the hour) is no broken statistic.
    read = plausible_kwh(_hours([8.0]), {}, START, START + timedelta(hours=1), limits=limits, cap_kwh=13.8)
    assert (read.kwh, read.dropped) == (8.0, 0)


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

    from custom_components.spotnav.site import heat_meter as heat_meter_module

    monkeypatch.setattr(heat_meter_module, "async_heat_hours", fake_heat)
    monkeypatch.setattr(loads_api, "async_heat_hours", fake_heat)
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

    # The month's hours are read once, by the history; the pie judges its meters' hours off the event loop.
    import threading

    period_reads: list[str] = []
    real_read_periods = history_api._read_periods

    async def counted_read_periods(*args, **kwargs):
        period_reads.append(args[3].kind)
        return await real_read_periods(*args, **kwargs)

    threads: list[str] = []
    real_limits = loads_api._limits

    def limits_spy(periods):
        threads.append(threading.current_thread().name)
        return real_limits(periods)

    monkeypatch.setattr(history_api, "_read_periods", counted_read_periods)
    monkeypatch.setattr(loads_api, "_limits", limits_spy)
    month = (await ask(hass, 3, api_version=1, site_id=site.entry_id, period="month"))["result"]
    assert period_reads == ["month"]
    assert threads and threading.main_thread().name not in threads
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


@pytest.mark.usefixtures("offline_relay")
async def test_the_heat_pie_the_choices_and_a_jumping_meter_end_to_end(hass: HomeAssistant, freezer, monkeypatch) -> None:
    """The heat pie holds the heat pump and each zone; a meter moved to heat leaves the house pie for the heat's (and
    its flow block), one moved nowhere is listed under the house pie; a jumping meter's impossible hour is dropped
    with its line; an unknown kind is refused."""
    from custom_components.spotnav.api import house as house_api
    from custom_components.spotnav.api import house_history as history_api
    from custom_components.spotnav.api import house_loads as loads_api
    from custom_components.spotnav.planning.demand_charge import hour_start
    from custom_components.spotnav.runtime import site_controller_for
    from custom_components.spotnav.site import heat_meter as heat_meter_module
    from custom_components.spotnav.site import heat_zones as zones_module
    from custom_components.spotnav.site.heat_zones import ZoneSource, ZoneSources

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
            {"stat_consumption": "sensor.nas_energi", "name": "NAS"},
            {"stat_consumption": "sensor.ups_energi", "name": "UPS"},
            {"stat_consumption": "sensor.shelly_golv", "name": "Shelly golvvärme"},
            {"stat_consumption": "sensor.golv_energy", "name": "Golv (dashboard)"},
        ],
    }
    changes = {
        "sensor.import": 4.0,
        "sensor.export": 0.0,
        "sensor.diskmaskin_energi": 0.5,
        # The plug whose total dropped and came back: 378 kWh in an hour of a 4 kWh house.
        "sensor.nas_energi": 378.0,
        "sensor.ups_energi": 0.1,
        "sensor.shelly_golv": 0.4,
        "sensor.golv_energy": 0.3,
    }

    async def fake_prefs(_hass):
        return prefs

    async def fake_statistics(_hass, ids, start, end, period):
        return {i: [{"start": last, "change": changes.get(i, 0.0)}] for i in ids}

    async def fake_heat(_hass, _meter, _start, _end, _period="hour"):
        return {last: 1.0} if _period == "hour" else {}

    async def fake_zones(_hass, zones, _start, _end, _period="hour", _cap=None):
        if _period != "hour":
            return {}
        return {source.sensor_id: {last: changes[source.sensor_id]} for source in zones.sources}

    for module in (house_api, history_api, loads_api, heat_meter_module):
        monkeypatch.setattr(module, "async_energy_prefs", fake_prefs)
    monkeypatch.setattr(house_api, "async_statistics", fake_statistics)
    monkeypatch.setattr(loads_api, "async_statistics", fake_statistics)
    monkeypatch.setattr(heat_meter_module, "async_heat_hours", fake_heat)
    monkeypatch.setattr(loads_api, "async_heat_hours", fake_heat)
    monkeypatch.setattr(zones_module, "async_zone_periods", fake_zones)
    monkeypatch.setattr(loads_api, "async_zone_periods", fake_zones)
    heat = controller.house_forecast.heat
    heat.meter = HeatMeter(STATUS_FOUND, "sensor.varmepump_energi", KIND_ENERGY)
    zone = ZoneSource("climate.golv", "Golvvärme tvättstuga", "sensor.golv_energy", KIND_ENERGY)

    def zones_now() -> ZoneSources:
        moved = tuple(
            ZoneSource(stat, "Shelly golvvärme", stat, KIND_ENERGY, "meter")
            for stat, where in stored_belongs(site.data).items()
            if where == "heat"
        )
        return ZoneSources((zone, *moved), (("climate.linnas_rum", "no_sensor"),))

    monkeypatch.setattr(heat, "current_zones", zones_now)

    house = (await ask(hass, 1, api_version=1, site_id=site.entry_id))["result"]
    # The zone's own dashboard device is the heat's; the house holds the import less the heat (1.0 + 0.3).
    assert house["kind"] == "house" and house["house_kwh"] == 2.7
    assert {row["name"]: row["kwh"] for row in house["loads"]} == {"Diskmaskin": 0.5, "UPS": 0.1, "Shelly golvvärme": 0.4}
    assert house["implausible"] == [{"statistic_id": "sensor.nas_energi", "name": "NAS", "hours": 1, "left_out": True}]
    assert (house["other_kwh"], house["moved"], house["belongs"]) == (1.7, [], {})

    one = (await ask(hass, 2, api_version=1, site_id=site.entry_id, kind="heat"))["result"]
    assert one["kind"] == "heat" and one["house_kwh"] == 1.3
    assert [(row["name"], row["kwh"], row["source"], row["changed"]) for row in one["loads"]] == [
        ("sensor.varmepump_energi", 1.0, "heat_pump", False),
        ("Golvvärme tvättstuga", 0.3, "zone", False),
    ]
    assert one["other_kwh"] == 0.0

    # The household moves the Shelly to heat and the UPS nowhere, through the site settings' compare-and-set; the
    # flow and the pies are read again at once.
    from custom_components.spotnav.api import site_settings as settings_api

    async def no_domains(_hass):
        return frozenset()

    monkeypatch.setattr(settings_api, "async_forecast_capable_domains", no_domains)
    await settings_api.async_update_site_settings(
        hass, None, site_id=site.entry_id, expected={"meter_belongs": {}},
        changes={"meter_belongs": {"sensor.shelly_golv": "heat", "sensor.ups_energi": "none"}},
    )
    assert site.data["meter_belongs"] == {"sensor.shelly_golv": "heat", "sensor.ups_energi": "none"}
    moved = (await ask(hass, 3, api_version=1, site_id=site.entry_id))["result"]
    assert [row["name"] for row in moved["loads"]] == ["Diskmaskin"]
    assert [(row["name"], row["belongs"]) for row in moved["moved"]] == [("Shelly golvvärme", "heat"), ("UPS", "none")]
    assert all(tuple(row) == MOVED_KEYS for row in moved["moved"])
    # The Shelly's energy left the house block for the heat's: the totals still add up.
    assert moved["house_kwh"] == 2.3
    two = (await ask(hass, 4, api_version=1, site_id=site.entry_id, kind="heat"))["result"]
    assert two["house_kwh"] == 1.7
    assert {row["name"]: (row["source"], row["changed"]) for row in two["loads"]} == {
        "sensor.varmepump_energi": ("heat_pump", False),
        "Shelly golvvärme": ("meter", True),
        "Golvvärme tvättstuga": ("zone", False),
    }
    assert sum(row["kwh"] for row in two["loads"]) == pytest.approx(1.7)

    refused = await ask(hass, 5, api_version=1, site_id=site.entry_id, kind="garage")
    assert not refused["success"] and refused["error"]["code"] == "spotnav_invalid_kind"


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
        "heat_villa_day.json",
        house_loads(
            site_id="site_villa", period="day", first="2026-11-17", end="2026-11-18", through=through,
            house_kwh=21.4, kind=KIND_HEAT,
            loads=[
                (DashboardMeter("sensor.varme_effekt", "Elmätare värme (Nibe + panna) Effekt", source=SOURCE_HEAT_PUMP), 18.9),
                (DashboardMeter("sensor.golvvarme_tvattstuga_energy", "Golvvärme tvättstuga", source=SOURCE_ZONE), 1.3),
                (DashboardMeter("sensor.golvvarme_toalett_nere_energy", "Golvvärme toalett nedervåning", source=SOURCE_ZONE), 0.7),
                (DashboardMeter("sensor.shelly_golv_energy", "Shelly golvvärme hall"), 0.5),
            ],
            belongs={"sensor.shelly_golv_energy": "heat", "sensor.nas_energy": "none"},
        ),
    )
    nas = DashboardMeter("sensor.smart_energy_switch_electric_consumption_kwh", "NAS")
    _fixture(
        "moved_and_implausible_month.json",
        house_loads(
            site_id="site_villa", period="month", first="2026-10-01", end="2026-11-01",
            through=datetime(2026, 11, 1, tzinfo=UTC), house_kwh=1030.0,
            loads=[(nas, 8.6), (DISH, 31.0), (DRYER, 44.2)],
            moved=[
                (DashboardMeter("sensor.shelly_golv_energy", "Shelly golvvärme hall"), "heat"),
                (DashboardMeter("sensor.ups_energy", "UPS"), "none"),
            ],
            belongs={"sensor.shelly_golv_energy": "heat", "sensor.ups_energy": "none"},
            implausible=[(nas, MeterRead(8.6, 744, 12))],
        ),
    )
    _fixture(
        "no_devices.json",
        house_loads(
            site_id="site_villa", period="year", first="2026-01-01", end="2027-01-01",
            through=datetime(2026, 11, 17, 13, tzinfo=UTC), house_kwh=6120.4, loads=[],
        ),
    )
