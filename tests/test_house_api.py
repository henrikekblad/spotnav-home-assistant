"""`spotnav/get_house` end to end on the pilot's site (no battery, no solar forecast): the answer's shape, the same by
the site's own id, the house's measured hours in it, and the refusals."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
from homeassistant.core import HomeAssistant

from custom_components.spotnav.api.house import websocket_get_house
from custom_components.spotnav.runtime import site_controller_for
from custom_components.spotnav.site.house_view import AHEAD_HOURS, HOUR_KEYS, HOUSE_KEYS, PAST_HOURS

from .test_demand_site import pilot_site, run

pytestmark = pytest.mark.usefixtures("offline_relay")


class _Connection:
    def __init__(self) -> None:
        self.answers: list[dict[str, Any]] = []

    def send_result(self, message_id: int, result: Any) -> None:
        self.answers.append({"id": message_id, "success": True, "result": json.loads(json.dumps(result))})

    def send_error(self, message_id: int, code: str, message: str) -> None:
        self.answers.append({"id": message_id, "success": False, "error": {"code": code, "message": message}})


async def ask(hass: HomeAssistant, message_id: int, **request: Any) -> dict[str, Any]:
    connection = _Connection()
    websocket_get_house(hass, connection, {"id": message_id, "type": "spotnav/get_house", **request})  # type: ignore[arg-type]
    await hass.async_block_till_done()
    (answer,) = connection.answers
    return answer


async def test_the_pilot_site_is_answered(hass: HomeAssistant, freezer) -> None:
    site = await pilot_site(hass, freezer, import_target_kw=6.0)
    await run(hass, freezer, 60, 4000.0)
    await run(hass, freezer, 60, 3000.0)
    answer = await ask(hass, 1, api_version=1, charger_id="entry_a")
    assert answer["success"], answer
    result = answer["result"]
    assert tuple(result) == HOUSE_KEYS
    assert (result["site_id"], result["state"], result["time_zone"]) == (site.entry_id, "shadow", "Europe/Stockholm")
    assert result["sun"] is None and result["battery"] is None
    hours = result["hours"]
    assert len(hours) == PAST_HOURS + AHEAD_HOURS and all(tuple(row) == HOUR_KEYS for row in hours)
    # The site's measured hours, the house's part of each (`None` where the chargers could not be read).
    tracker = site_controller_for(hass, site.entry_id).demand.tracker  # type: ignore[union-attr]
    assert len(tracker.hours) == 2
    by_start = {row["start"]: row["house_kw"] for row in hours}
    for hour in tracker.hours:
        expected = None if hour.house_kw is None else round(hour.house_kw, 3)
        assert by_start[hour.start.isoformat()] == expected
    assert result["sources"]["solar_forecasts"] == []
    assert (await ask(hass, 2, api_version=1, site_id=site.entry_id))["result"] == result


async def test_refusals(hass: HomeAssistant, freezer) -> None:
    site = await pilot_site(hass, freezer, import_target_kw=6.0)
    cases = [
        ({"charger_id": "entry_a"}, "spotnav_unsupported_api_version"),
        ({"api_version": 2, "charger_id": "entry_a"}, "spotnav_unsupported_api_version"),
        ({"api_version": 1}, "spotnav_charger_required"),
        ({"api_version": 1, "charger_id": "nope"}, "spotnav_unknown_charger"),
        ({"api_version": 1, "charger_id": site.entry_id}, "spotnav_site_not_charger"),
        ({"api_version": 1, "site_id": "entry_a"}, "spotnav_unknown_site"),
        ({"api_version": 1, "site_id": 7}, "spotnav_unknown_site"),
    ]
    for index, (request, code) in enumerate(cases, start=1):
        answer = await ask(hass, index, **request)
        assert not answer["success"] and answer["error"]["code"] == code, request
    assert await hass.config_entries.async_unload(site.entry_id)
    answer = await ask(hass, 99, api_version=1, site_id=site.entry_id)
    assert not answer["success"] and answer["error"]["code"] == "spotnav_no_site"


def test_the_suns_production_comes_from_the_sun_forecasts_log() -> None:
    """The battery's day flow takes the sun's production from the outcomes the sun forecast's log holds (a field
    case: the answer read a production map the battery plan no longer keeps, and every battery site failed)."""
    from datetime import datetime, timezone
    from types import SimpleNamespace

    from custom_components.spotnav.api.house import _production
    from custom_components.spotnav.site.forecast_log import KIND_SUN, LoggedHour

    made = datetime(2026, 11, 13, 12, tzinfo=timezone.utc)
    measured = datetime(2026, 11, 13, 10, tzinfo=timezone.utc)
    waiting = datetime(2026, 11, 13, 11, tzinfo=timezone.utc)
    hours = {measured: LoggedHour(1.5, made, 1.2), waiting: LoggedHour(0.8, made)}
    log = SimpleNamespace(hours=lambda kind: hours if kind == KIND_SUN else {})
    controller = SimpleNamespace(house_forecast=SimpleNamespace(log=log))

    assert _production(controller) == {measured: 1.2}


async def test_the_cars_hours_are_the_flows_own_figures_for_the_charts_hours(hass: HomeAssistant, monkeypatch) -> None:
    """The chart's per-hour car figure comes from the chargers' registers (else the battery plan's measure), for the
    completed hours of the window only, and reads nothing without a register."""
    from datetime import datetime, timedelta, timezone
    from types import SimpleNamespace

    from custom_components.spotnav.api import house as house_api
    from custom_components.spotnav.const import CONF_CHARGER_ENTRY_IDS

    now = datetime(2026, 11, 13, 14, 30, tzinfo=timezone.utc)
    current = datetime(2026, 11, 13, 14, tzinfo=timezone.utc)
    controller = SimpleNamespace(
        demand=SimpleNamespace(tracker=SimpleNamespace(tz="UTC")),
        battery_shadow=SimpleNamespace(facts=SimpleNamespace(battery_entity_id=None)),
        config={CONF_CHARGER_ENTRY_IDS: ["c1"]},
    )
    monkeypatch.setattr(house_api, "controller_for", lambda _hass, _id: SimpleNamespace(energy_register_entity_id="sensor.reg"))
    reads: list[Any] = []

    async def fake_statistics(_hass, ids, start, end, period):
        reads.append((tuple(ids), start, end))
        before = current - timedelta(hours=1)
        return {"sensor.reg": [{"start": before, "change": 7.2}, {"start": start, "change": 0.5}]}

    monkeypatch.setattr(house_api, "async_statistics", fake_statistics)
    hours = await house_api.async_car_hours(hass, controller, now)
    assert hours[current - timedelta(hours=1)] == 7.2 and current not in hours
    assert reads[0][0] == ("sensor.reg",) and reads[0][2] == current
    controller.config = {}
    reads.clear()
    assert await house_api.async_car_hours(hass, controller, now) == {} and reads == []


async def test_todays_energy_flow_comes_from_the_energy_dashboard(hass: HomeAssistant, freezer, monkeypatch) -> None:
    """The flow reads the Energy dashboard's meters and the chargers' registers for today's completed hours, once per
    hour (or `FLOW_REFRESH_S`); without the dashboard's grid meter there is none."""
    from datetime import timedelta

    from custom_components.spotnav.api import house as house_api
    from custom_components.spotnav.planning.demand_charge import hour_start

    site = await pilot_site(hass, freezer, import_target_kw=6.0)
    await run(hass, freezer, 60, 4000.0)
    answer = await ask(hass, 1, api_version=1, site_id=site.entry_id)
    assert answer["result"]["energy_flow"] is None  # no Energy integration in the test

    controller = site_controller_for(hass, site.entry_id)
    now = controller.demand.now()  # type: ignore[union-attr]
    current = hour_start(now, controller.demand.tracker.tz)  # type: ignore[union-attr]
    prefs = {"energy_sources": [{"type": "grid", "stat_energy_from": "sensor.import", "stat_energy_to": "sensor.export"}]}
    reads: list[tuple] = []

    async def fake_prefs(_hass):
        return prefs

    async def fake_statistics(_hass, ids, start, end, period):
        reads.append((tuple(ids), start, end, period))
        last = current - timedelta(hours=1)
        rows = {"sensor.import": [{"start": last, "change": 3.0}], "sensor.export": [{"start": last, "change": 0.0}]}
        return {i: rows.get(i, [{"start": last, "change": 1.0}]) for i in ids}

    monkeypatch.setattr(house_api, "async_energy_prefs", fake_prefs)
    monkeypatch.setattr(house_api, "async_statistics", fake_statistics)
    hass.data.pop(house_api._FLOW_CACHE, None)  # noqa: SLF001
    first = (await ask(hass, 2, api_version=1, site_id=site.entry_id))["result"]["energy_flow"]
    assert reads and reads[0][3] == "hour" and reads[0][2] == current
    nodes = {node["id"]: node["kwh"] for node in first["nodes"]}
    assert nodes["source.grid"] == 3.0
    assert set(nodes) == {"source.grid", "use.house", "use.car", "use.grid"}
    # Asked again within the hour: read from the cache.
    await ask(hass, 3, api_version=1, site_id=site.entry_id)
    assert len(reads) == 1


async def test_a_past_day_is_asked_for_by_date(hass: HomeAssistant, freezer) -> None:
    """`date` asks for a past local day from `day.first` on; today is as without it; anything else is refused."""
    from datetime import timedelta

    site = await pilot_site(hass, freezer, import_target_kw=6.0)
    await run(hass, freezer, 60, 4000.0)
    now = site_controller_for(hass, site.entry_id).demand.now()  # type: ignore[union-attr]
    today = now.astimezone(__import__("zoneinfo").ZoneInfo("Europe/Stockholm")).date()
    plain = (await ask(hass, 1, api_version=1, site_id=site.entry_id))["result"]
    assert plain["day"]["today"] is True and plain["day"]["date"] == today.isoformat()
    same = (await ask(hass, 2, api_version=1, site_id=site.entry_id, date=today.isoformat()))["result"]
    assert same["day"] == plain["day"]
    yesterday = (today - timedelta(days=1)).isoformat()
    past = (await ask(hass, 3, api_version=1, site_id=site.entry_id, date=yesterday))["result"]
    assert past["day"]["date"] == yesterday and past["day"]["today"] is False
    assert len(past["hours"]) in (23, 24, 25)
    for index, bad in enumerate(["2026-13-01", 20261112, (today + timedelta(days=1)).isoformat(), plain["day"]["first"][:8] + "0x", (today - timedelta(days=30)).isoformat()]):
        answer = await ask(hass, 10 + index, api_version=1, site_id=site.entry_id, date=bad)
        assert not answer["success"] and answer["error"]["code"] == "spotnav_invalid_date", bad


async def test_the_recorded_soc_reads_the_shown_past_hours(hass: HomeAssistant, freezer, monkeypatch) -> None:
    """The SoC history asks the recorder for the shown past hours of the battery facts' SoC sensor, and nothing
    without a battery."""
    from datetime import timedelta
    from types import SimpleNamespace

    from custom_components.spotnav.api import house as house_api
    from custom_components.spotnav.planning.demand_charge import hour_start
    from custom_components.spotnav.site.house_view import PAST_HOURS

    site = await pilot_site(hass, freezer, import_target_kw=6.0)
    controller = site_controller_for(hass, site.entry_id)
    now = controller.demand.now()  # type: ignore[union-attr]
    assert await house_api.async_recorded_soc(hass, controller, now) == {}  # no battery on the pilot's site
    current = hour_start(now, controller.demand.tracker.tz)  # type: ignore[union-attr]
    reads = []

    async def fake(_hass, entity_id, start, end):
        reads.append((entity_id, start, end))
        return [{"start": current - timedelta(minutes=5), "mean": 55.0}], []

    monkeypatch.setattr(house_api, "async_soc_statistics", fake)
    with_battery = SimpleNamespace(
        battery_shadow=SimpleNamespace(facts=SimpleNamespace(battery_entity_id="sensor.p", sources={"soc": "sensor.soc"})),
        demand=controller.demand,  # type: ignore[union-attr]
    )
    soc = await house_api.async_recorded_soc(hass, with_battery, now)
    assert reads == [("sensor.soc", current - timedelta(hours=PAST_HOURS), current)]
    assert soc == {current - timedelta(hours=1): 55.0}


async def test_figures_that_cannot_be_read_are_left_out_of_the_answer(hass: HomeAssistant, freezer, monkeypatch) -> None:
    """Without a recorder the energy flow and the recorded SoC raise: the house view is answered without them."""
    from custom_components.spotnav.api import house as house_api

    site = await pilot_site(hass, freezer, import_target_kw=6.0)

    async def no_recorder(*_args: Any, **_kwargs: Any) -> Any:
        raise KeyError("recorder_instance")

    monkeypatch.setattr(house_api, "async_day_flow", no_recorder)
    monkeypatch.setattr(house_api, "async_recorded_soc", no_recorder)
    hass.data.pop(house_api._FLOW_CACHE, None)  # noqa: SLF001
    answer = await ask(hass, 1, api_version=1, site_id=site.entry_id)

    assert answer["success"] is True and answer["result"]["energy_flow"] is None


async def test_the_unload_forgets_the_sites_cached_flow(hass: HomeAssistant, freezer) -> None:
    from custom_components.spotnav.api import house as house_api

    site = await pilot_site(hass, freezer, import_target_kw=6.0)
    await ask(hass, 1, api_version=1, site_id=site.entry_id)
    assert any(key[0] == site.entry_id for key in hass.data[house_api._FLOW_CACHE])  # noqa: SLF001

    assert await hass.config_entries.async_unload(site.entry_id)
    await hass.async_block_till_done()

    assert not any(key[0] == site.entry_id for key in hass.data[house_api._FLOW_CACHE])  # noqa: SLF001


def _flow_controller(now_box: list) -> Any:
    from types import SimpleNamespace

    from custom_components.spotnav.site.heat_zones import NO_ZONES

    meter = SimpleNamespace(found=False, entity_id=None, kind=None)

    async def zones() -> Any:
        return NO_ZONES

    return SimpleNamespace(
        demand=SimpleNamespace(tracker=SimpleNamespace(tz="Europe/Stockholm"), now=lambda: now_box[0]),
        battery_shadow=SimpleNamespace(facts=SimpleNamespace(battery_entity_id=None, soc_pct=None)),
        house_forecast=SimpleNamespace(heat=SimpleNamespace(meter=meter, async_current_zones=zones)),
        config={},
    )


async def test_today_is_read_by_the_quarter_and_only_the_open_hour_again(hass: HomeAssistant, monkeypatch) -> None:
    """Within the recorder's short-term window the flow reads 5-minute rows up to the last finished one, keeps the
    finished hours and reads only the open hour again once a new row is in; a day before the window is read by the
    hour; today's first minutes are the empty flow."""
    from datetime import datetime, timedelta, timezone

    from custom_components.spotnav.api import house as house_api

    midnight = datetime(2026, 11, 15, 23, tzinfo=timezone.utc)  # 16 Nov, Stockholm
    now_box = [midnight + timedelta(hours=2, minutes=7)]
    controller = _flow_controller(now_box)
    prefs = {"energy_sources": [{"type": "grid", "stat_energy_from": "sensor.import", "stat_energy_to": "sensor.export"}]}
    reads: list[tuple] = []

    async def fake_prefs(_hass):
        return prefs

    async def fake_statistics(_hass, ids, start, end, period):
        reads.append((start, end, period))
        step = timedelta(minutes=5) if period == "5minute" else timedelta(hours=1)
        rows: dict[str, list] = {i: [] for i in ids}
        moment = start
        # The recorder has the rows that ended by now less a few seconds.
        while moment + step <= min(end, now_box[0] - timedelta(seconds=15)):
            rows["sensor.import"].append({"start": moment, "change": 0.1 if period == "5minute" else 1.2})
            rows["sensor.export"].append({"start": moment, "change": 0.0})
            moment += step
        return rows

    monkeypatch.setattr(house_api, "async_energy_prefs", fake_prefs)
    monkeypatch.setattr(house_api, "async_statistics", fake_statistics)
    monkeypatch.setattr(house_api, "short_term_days", lambda _hass: 10)
    hass.data.pop(house_api._FLOW_CACHE, None)  # noqa: SLF001
    hass.data.pop(house_api._PERIOD_CACHE, None)  # noqa: SLF001

    flow = await house_api.async_cached_energy_flow(hass, "site", controller)
    assert reads == [(midnight, now_box[0], "5minute")]
    assert flow["through"] == (midnight + timedelta(hours=2, minutes=5)).isoformat()
    assert flow["step_min"] == 15 and flow["hours"] == 9  # 8 quarters and the running one's first row
    grid = next(node["kwh"] for node in flow["nodes"] if node["id"] == "source.grid")
    assert grid == 2.5  # 25 rows of 0.1

    # The same 5-minute row: the cache answers.
    now_box[0] += timedelta(minutes=2)
    await house_api.async_cached_energy_flow(hass, "site", controller)
    assert len(reads) == 1
    # A new row: only the open hour is read again.
    now_box[0] += timedelta(minutes=3)
    flow = await house_api.async_cached_energy_flow(hass, "site", controller)
    assert reads[-1] == (midnight + timedelta(hours=2), now_box[0], "5minute")
    assert flow["through"] == (midnight + timedelta(hours=2, minutes=10)).isoformat()
    read = await house_api.async_cached_day_flow(hass, "site", controller)
    assert read is not None and len(read.periods) == 9 and read.periods[-1].minutes == 10

    # A day all of whose rows are within the last ten days is by the quarter; the one before it by the hour.
    reads.clear()
    from datetime import date

    await house_api.async_day_flow(hass, controller, now_box[0], date(2026, 11, 7))
    assert {period for _s, _e, period in reads} == {"5minute"}
    reads.clear()
    old = await house_api.async_energy_flow(hass, controller, now_box[0], date(2026, 11, 6))
    assert {period for _s, _e, period in reads} == {"hour"} and old["step_min"] == 60 and old["hours"] == 24

    # Today's first minutes: the empty flow until the first row is in.
    now_box[0] = midnight + timedelta(minutes=3)
    hass.data.pop(house_api._PERIOD_CACHE, None)  # noqa: SLF001
    empty = await house_api.async_energy_flow(hass, controller, now_box[0])
    assert (empty["hours"], empty["links"], empty["through"]) == (0, [], midnight.isoformat())
    now_box[0] = midnight + timedelta(minutes=5, seconds=40)
    first = await house_api.async_energy_flow(hass, controller, now_box[0])
    assert first["hours"] == 1 and first["through"] == (midnight + timedelta(minutes=5)).isoformat()


def test_control_state_says_whether_spotnav_controls_the_battery() -> None:
    """The house answer's `battery.control`: the control's own state, `None` without one or when it cannot be read."""
    from custom_components.spotnav.api.house import _control_state

    assert _control_state(SimpleNamespace()) is None
    assert _control_state(SimpleNamespace(battery_control=SimpleNamespace(state=lambda: "on"))) == "on"
    assert _control_state(SimpleNamespace(battery_control=SimpleNamespace(state=lambda: "shadow_only"))) == "shadow_only"

    def broken() -> str:
        raise RuntimeError("unreadable")

    assert _control_state(SimpleNamespace(battery_control=SimpleNamespace(state=broken))) is None
