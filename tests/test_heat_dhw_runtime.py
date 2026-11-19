"""The site's hot water on the pilot's site (`heat/dhw_runtime.py`): the Boost button over the WebSocket and the
webhook, each brand's write read back, the plan in shadow with its day kept and reviewed, and the live boost behind
the opt-in and the gate."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest
from homeassistant.core import HomeAssistant, ServiceCall

from custom_components.spotnav.api.heat import websocket_heat_boost
from custom_components.spotnav.battery.plan_shadow import PriceSlot
from custom_components.spotnav.const import CONF_HEAT
from custom_components.spotnav.heat import dhw_runtime
from custom_components.spotnav.heat.adapters import CTC_BMS, MYUPLINK, NIBE_F
from custom_components.spotnav.heat.dhw_ledger import SOURCE_BUTTON, SOURCE_PLAN, Entry
from custom_components.spotnav.heat.dhw_peaks import Peak
from custom_components.spotnav.heat.meter_split import HotWaterRun
from custom_components.spotnav.runtime import site_controller_for
from .helpers import answers_of

from .test_demand_site import pilot_site
from .test_heat_api import _Connection, read

pytestmark = pytest.mark.usefixtures("offline_relay")

PULSE = "switch.larm_utgang_2"
LUX = "select.nibe_temporary_lux_48132"
LUX_OPTIONS = ["Off", "3 hrs", "6 hrs", "12 hrs", "One time increase"]


async def settle(hass: HomeAssistant) -> None:
    """Let the hot water's background write (and its read-back) finish."""
    await hass.async_block_till_done()
    for entry in hass.config_entries.async_entries("spotnav"):
        controller = site_controller_for(hass, entry.entry_id)
        hot_water = None if controller is None else controller.heat_shadow.hot_water
        for task in () if hot_water is None else (hot_water._pending, hot_water._side):  # noqa: SLF001
            if task is not None:
                await task
    await hass.async_block_till_done()


async def press(hass: HomeAssistant, action: Any, **request: Any) -> dict[str, Any]:
    connection = _Connection(admin=False)
    websocket_heat_boost(hass, connection, {"id": 1, "type": "spotnav/heat_boost", "api_version": 1, "action": action, **request})  # type: ignore[arg-type]
    # The command runs as a background task: wait for its answer.
    (answer,) = await answers_of(hass, connection)
    await settle(hass)
    return answer


@pytest.fixture(autouse=True)
def _quick_read_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dhw_runtime, "POLL_S", 0.01)
    monkeypatch.setattr(dhw_runtime, "READ_BACK", timedelta(seconds=0.05))
    monkeypatch.setattr(dhw_runtime, "READ_BACK_CLOUD", timedelta(seconds=0.05))
    monkeypatch.setattr(dhw_runtime, "PULSE", timedelta(seconds=0))

    async def no_pause(_seconds: float) -> None:
        return None

    # The frozen clock never lets a real wait end.
    monkeypatch.setattr(dhw_runtime, "_pause", no_pause)


def _pump(adapter: Any, entity_id: str, *, price_control: str = "off", priority: str | None = None) -> Any:
    capability = next(item for item in adapter.capabilities if item.kind == "hot_water_boost")
    status = SimpleNamespace(
        kind="hot_water_boost",
        entity_id=entity_id,
        enabled=True,
        available=True,
        volatile=capability.volatile,
        self_ending=capability.self_ending,
        duration_s=None if capability.duration is None else capability.duration.total_seconds(),
        start=capability.start,
        stop=capability.stop,
        verified=capability.verified,
    )
    return SimpleNamespace(
        capabilities=(status,), cloud=adapter.cloud, price_control=SimpleNamespace(state=price_control), priority=priority
    )


async def _site(hass: HomeAssistant, freezer: Any, monkeypatch: pytest.MonkeyPatch, pump: Any = None, **heat: Any) -> tuple[Any, Any]:
    site = await pilot_site(hass, freezer)
    # An outdoor reading: without one the house's safety reads stale and no planned boost starts.
    hass.states.async_set("sensor.ute_dhw", "4.0", {"unit_of_measurement": "°C", "device_class": "temperature"})
    hass.config_entries.async_update_entry(
        site, data={**site.data, CONF_HEAT: {"zones": [], "outdoor_sensor": "sensor.ute_dhw", **heat}}
    )
    controller = site_controller_for(hass, site.entry_id)
    assert controller is not None
    monkeypatch.setattr(dhw_runtime.HotWater, "_pump", lambda _self: pump)
    return site, controller


class Writes:
    """Service calls kept, the entity set as a pump would show it (`seen=False`: never shows it)."""

    def __init__(self, hass: HomeAssistant, seen: bool = True, fail: bool = False) -> None:
        self.hass = hass
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.seen = seen
        self.fail = fail
        for domain, service in (
            ("switch", "turn_on"), ("switch", "turn_off"), ("select", "select_option"), ("number", "set_value"),
        ):
            hass.services.async_register(domain, service, self._call)

    def _call(self, call: ServiceCall) -> None:
        if self.fail:
            raise RuntimeError("refused")
        self.calls.append((call.domain, call.service, dict(call.data)))
        if not self.seen:
            return
        entity_id = call.data["entity_id"]
        if isinstance(entity_id, list):
            entity_id = entity_id[0]
        old = self.hass.states.get(entity_id)
        attributes = {} if old is None else dict(old.attributes)
        value = {"turn_on": "on", "turn_off": "off"}.get(call.service) or call.data.get("option") or str(call.data.get("value"))
        self.hass.states.async_set(entity_id, value, attributes)


async def test_the_button_pulses_the_hot_water_input_once_per_ten_minutes(hass: HomeAssistant, freezer, monkeypatch) -> None:
    site, controller = await _site(hass, freezer, monkeypatch, hot_water_entity=PULSE)
    hass.states.async_set(PULSE, "off")
    writes = Writes(hass)
    answer = await press(hass, "start", site_id=site.entry_id)
    assert answer["success"] and answer["result"]["ok"] is True
    # On, then off again: a pulse; the pump ends the run itself.
    assert [(domain, service) for domain, service, _ in writes.calls] == [("switch", "turn_on"), ("switch", "turn_off")]
    hot_water = controller.heat_shadow.hot_water
    assert hot_water.ledger.last(SOURCE_BUTTON) is not None
    # Starting until the meter shows the run the pulse asked for.
    assert hot_water.boost()["state"] == "starting"
    monkeypatch.setattr(hot_water, "making_now", lambda: True)
    hot_water.tick(controller.demand.now())
    boost = (await read(hass, api_version=1, site_id=site.entry_id))["result"]["hot_water"]["boost"]
    assert (boost["state"], boost["source"], boost["stoppable"], boost["ends_estimated"]) == ("running", "user", False, True)
    again = await press(hass, "start", site_id=site.entry_id)
    assert again["result"]["error"] == "spotnav_rate_limited" and len(writes.calls) == 2
    # A pulse has no way to end it early.
    assert (await press(hass, "stop", site_id=site.entry_id))["result"]["error"] == "spotnav_not_supported"
    freezer.tick(timedelta(minutes=11))
    assert (await press(hass, "start", site_id=site.entry_id))["result"]["ok"] is True
    assert (await press(hass, "go", site_id=site.entry_id))["result"]["error"] == "spotnav_invalid_value"


async def test_nibe_one_time_increase_read_back_and_stopped(hass: HomeAssistant, freezer, monkeypatch) -> None:
    site, controller = await _site(hass, freezer, monkeypatch, _pump(NIBE_F, LUX))
    hass.states.async_set(LUX, "Off", {"options": LUX_OPTIONS})
    writes = Writes(hass)
    answer = (await press(hass, "start", site_id=site.entry_id))["result"]
    assert answer["ok"] and writes.calls[0] == ("select", "select_option", {"entity_id": LUX, "option": "One time increase"})
    boost = controller.heat_shadow.hot_water.boost()
    assert (boost["state"], boost["source"], boost["stoppable"]) == ("running", "user", True)
    assert controller.heat_shadow.hot_water.ledger.last().read_back is True
    stop = (await press(hass, "stop", site_id=site.entry_id))["result"]
    assert stop["ok"] and writes.calls[-1] == ("select", "select_option", {"entity_id": LUX, "option": "Off"})
    assert stop["boost"]["state"] == "idle"


async def test_a_start_not_read_back_fails_and_a_refused_write_too(hass: HomeAssistant, freezer, monkeypatch) -> None:
    site, controller = await _site(hass, freezer, monkeypatch, _pump(CTC_BMS, "number.ctc_extra_hot_water_1006"))
    hass.states.async_set("number.ctc_extra_hot_water_1006", "0")
    writes = Writes(hass, seen=False)
    assert (await press(hass, "start", site_id=site.entry_id))["result"]["ok"] is True
    assert writes.calls == [("number", "set_value", {"entity_id": "number.ctc_extra_hot_water_1006", "value": 1.0})]
    hot_water = controller.heat_shadow.hot_water
    assert hot_water.boost()["state"] == "failed" and hot_water.ledger.last().read_back is False
    freezer.tick(timedelta(minutes=11))
    writes.fail = True
    assert (await press(hass, "start", site_id=site.entry_id))["result"]["ok"] is True
    assert hot_water.boost()["state"] == "failed"


async def test_spotnavs_own_boost_is_not_the_buttons_to_stop(hass: HomeAssistant, freezer, monkeypatch) -> None:
    site, controller = await _site(hass, freezer, monkeypatch, _pump(MYUPLINK, "switch.f730_temporary_lux_50004"))
    hot_water = controller.heat_shadow.hot_water
    now = controller.demand.now()
    hot_water.ledger.add(Entry("switch.f730_temporary_lux_50004", "turn_on", now, SOURCE_PLAN, read_back=True))
    hass.states.async_set("switch.f730_temporary_lux_50004", "on")
    Writes(hass)
    answer = (await press(hass, "stop", site_id=site.entry_id))["result"]
    assert answer["error"] == "spotnav_not_own" and answer["boost"]["source"] == "plan"
    # The heat control reads a boost started by SpotNav's plan as its own, any other as the household's.
    hass.config_entries.async_update_entry(site, data={**site.data, CONF_HEAT: {"zones": [], "hot_water_entity": PULSE}})
    control = controller.heat_shadow.control
    config = controller.heat_shadow.config()
    hass.states.async_set(PULSE, "on")
    assert control._boost_source(config) == "user"  # noqa: SLF001
    hot_water.ledger.add(Entry(PULSE, "turn_on", hass.states.get(PULSE).last_changed, SOURCE_PLAN, read_back=True))
    assert control._boost_source(config) == "plan"  # noqa: SLF001


async def test_no_route_no_button(hass: HomeAssistant, freezer, monkeypatch) -> None:
    site, _controller = await _site(hass, freezer, monkeypatch)
    answer = (await press(hass, "start", site_id=site.entry_id))["result"]
    assert answer == {"api_version": 1, "ok": False, "error": "spotnav_not_supported", "boost": None}
    hot_water = (await read(hass, api_version=1, site_id=site.entry_id))["result"]["hot_water"]
    assert hot_water["boost"] is None and hot_water["level"] is None and hot_water["planned"] == []
    assert (await press(hass, "start", api_version=2, site_id=site.entry_id))["success"] is False


async def test_the_webhook_twin(hass: HomeAssistant, freezer, monkeypatch, hass_client_no_auth) -> None:
    site, _controller = await _site(hass, freezer, monkeypatch, hot_water_entity=PULSE)
    hass.states.async_set(PULSE, "off")
    Writes(hass)
    client = await hass_client_no_auth()
    response = await client.post("/api/webhook/webhook-entry_a", json={"version": 1, "action": "heat_boost", "boost_action": "start"})
    body = json.loads(await response.text())
    assert response.status == 200 and body["ok"] is True and body["action"] == "heat_boost"
    response = await client.post("/api/webhook/webhook-entry_a", json={"version": 1, "action": "heat_boost", "boost_action": "start"})
    body = json.loads(await response.text())
    assert response.status == 429 and body["error"] == "spotnav_rate_limited"
    response = await client.post("/api/webhook/webhook-entry_a", json={"version": 1, "action": "heat_boost"})
    assert response.status == 400


# ------------------------------------------------------------------------------------------------ the plan


def _prices(controller: Any, now: datetime, cheap_hours: tuple[int, ...]) -> list[PriceSlot]:
    zone = controller.heat_shadow.hot_water.zone
    start = now.replace(minute=0, second=0, microsecond=0)
    out = []
    for index in range(36):
        hour = start + timedelta(hours=index)
        price = 40.0 if hour.astimezone(zone).hour in cheap_hours else 150.0
        out.append(PriceSlot(hour, hour + timedelta(hours=1), price, 30.0))
    return out


async def test_the_plan_in_shadow_kept_reviewed_and_shown(hass: HomeAssistant, freezer, monkeypatch) -> None:
    site, controller = await _site(hass, freezer, monkeypatch, hot_water_entity=PULSE)
    hass.states.async_set(PULSE, "off")
    writes = Writes(hass)
    hot_water = controller.heat_shadow.hot_water
    now = controller.demand.now()
    zone = hot_water.zone
    slots = _prices(controller, now, (2, 3))
    monkeypatch.setattr(controller.battery_shadow.plan, "buy_slots", lambda _now: slots)
    morning = Peak(datetime.min.time().replace(hour=6), datetime.min.time().replace(hour=7), 1.5, True, datetime.min.time().replace(hour=6, minute=30))
    hot_water.peaks = [morning]
    monkeypatch.setattr(hot_water, "_learn", lambda _now: None)
    await hot_water.async_hourly(now)
    assert len(hot_water.picks) == 1
    pick = hot_water.picks[0]
    assert pick.start.astimezone(zone).hour in (2, 3) and pick.peak.astimezone(zone).hour == 6
    tomorrow = pick.peak.astimezone(zone).date().isoformat()
    assert pick.peak.isoformat() in hot_water.days[tomorrow]["picks"]
    answer = (await read(hass, api_version=1, site_id=site.entry_id))["result"]
    shown = answer["hot_water"]
    assert shown["mode"] == "shadow" and shown["level"] == "B"
    assert shown["planned"][0]["start"] == pick.start.isoformat() and shown["planned"][0]["live"] is False
    assert "hot_water_pick" not in answer["band"]
    # The pump's own reload at 06:20 the next morning; the day is reviewed three hours after it ends.
    run_start = pick.peak + timedelta(minutes=20)
    hot_water.runs[run_start.isoformat()] = HotWaterRun(run_start, run_start + timedelta(minutes=30), 1.4)
    hot_water.seen.add(tomorrow)
    review_at = datetime.combine(date.fromisoformat(tomorrow) + timedelta(days=1), datetime.min.time(), tzinfo=zone) + timedelta(hours=4)
    monkeypatch.setattr("custom_components.spotnav.heat.dhw_runtime.period_price_slots", lambda *_args: slots)
    await hot_water._async_review(review_at.astimezone(timezone.utc))  # noqa: SLF001
    review = hot_water.days[tomorrow]["review"]
    assert (review["mode"], review["hits"], review["peaks"]) == ("shadow", 1, 1)
    assert review["per_peak"][0]["pump_run"] == run_start.isoformat() and review["saved"] > 0
    assert hot_water.gate_state()["days"] == 1 and not hot_water.gate_state()["open"]
    # Shadow: nothing was written.
    assert writes.calls == []
    assert controller.heat_shadow.diagnostics()["hot_water"]["mode"] == "shadow"
    # Kept across a restart.
    again = dhw_runtime.HotWater(hass, controller.heat_shadow)
    again.restore(json.loads(json.dumps(hot_water.stored())))
    assert again.days == hot_water.days and again.peaks == hot_water.peaks


def _open_gate(hot_water: Any, today: date) -> None:
    for index in range(1, 8):
        day = today - timedelta(days=index)
        hot_water.days[day.isoformat()] = {
            "peaks": [], "picks": {}, "live": [],
            "review": {"date": day.isoformat(), "mode": "shadow", "hits": 2, "peaks": 2, "saved": 20.0, "misses": 0},
        }


async def test_live_behind_the_opt_in_and_the_gate(hass: HomeAssistant, freezer, monkeypatch) -> None:
    site, controller = await _site(
        hass, freezer, monkeypatch, _pump(MYUPLINK, "switch.f730_temporary_lux_50004"), control_enabled=True
    )
    hass.states.async_set("switch.f730_temporary_lux_50004", "off")
    writes = Writes(hass, seen=False)
    hot_water = controller.heat_shadow.hot_water
    monkeypatch.setattr(hot_water, "_meter_fresh", lambda _now: True)
    monkeypatch.setattr(hot_water, "_meter_found", lambda: True)
    # Room on the fuse (its checks are `tests/test_heat_dhw_safety.py`).
    monkeypatch.setattr(hot_water, "_fuse_blocker", lambda: None)
    now = controller.demand.now()
    today = now.astimezone(hot_water.zone).date()
    from custom_components.spotnav.heat.dhw_plan import Pick

    pick = Pick(now, now + timedelta(minutes=30), 1.2, 40.0, "cheap", now + timedelta(hours=2), now + timedelta(hours=2, minutes=30), 150.0, 100.0)
    hot_water.picks = [pick]
    # Shadow until the gate opens: nothing written.
    hot_water.tick(now)
    await settle(hass)
    assert writes.calls == []
    _open_gate(hot_water, today)
    config = controller.heat_shadow.config()
    assert hot_water.mode(config, hot_water.route(config), hot_water._pump()) == "live"  # noqa: SLF001
    # A safety reason holds it back: too cold outdoors.
    hass.states.async_set("sensor.ute_dhw", "-20.0", {"unit_of_measurement": "°C", "device_class": "temperature"})
    hot_water.tick(now)
    await settle(hass)
    assert writes.calls == []
    hass.states.async_set("sensor.ute_dhw", "4.0", {"unit_of_measurement": "°C", "device_class": "temperature"})
    hot_water.tick(now)
    await settle(hass)
    assert writes.calls == [("switch", "turn_on", {"entity_id": "switch.f730_temporary_lux_50004"})]
    # Not read back on a cloud: no retry, planned hot water ends for the day.
    assert hot_water.ended_day == today.isoformat()
    hot_water.tick(now + timedelta(minutes=6))
    await settle(hass)
    assert len(writes.calls) == 1
    # The pump's own price control on: watch only.
    monkeypatch.setattr(dhw_runtime.HotWater, "_pump", lambda _self: _pump(MYUPLINK, "switch.f730_temporary_lux_50004", price_control="active"))
    assert hot_water.mode(config, hot_water.route(config), hot_water._pump()) == "watch"  # noqa: SLF001


async def test_a_live_boost_read_back_is_kept_and_a_local_one_tried_again(hass: HomeAssistant, freezer, monkeypatch) -> None:
    site, controller = await _site(hass, freezer, monkeypatch, _pump(NIBE_F, LUX), control_enabled=True)
    hass.states.async_set(LUX, "Off", {"options": LUX_OPTIONS})
    writes = Writes(hass, seen=False)
    hot_water = controller.heat_shadow.hot_water
    monkeypatch.setattr(hot_water, "_meter_fresh", lambda _now: True)
    monkeypatch.setattr(hot_water, "_meter_found", lambda: True)
    # Room on the fuse (its checks are `tests/test_heat_dhw_safety.py`).
    monkeypatch.setattr(hot_water, "_fuse_blocker", lambda: None)
    now = controller.demand.now()
    today = now.astimezone(hot_water.zone).date()
    _open_gate(hot_water, today)
    from custom_components.spotnav.heat.dhw_plan import Pick

    pick = Pick(now, now + timedelta(minutes=30), 1.2, 40.0, "cheap", now + timedelta(hours=2), now + timedelta(hours=2, minutes=30), 150.0, 100.0)
    hot_water.picks = [pick]
    hot_water.tick(now)
    await settle(hass)
    assert len(writes.calls) == 1 and hot_water.ended_day is None
    # Tried once more after five minutes; this time it is seen.
    writes.seen = True
    hot_water.tick(now + timedelta(minutes=2))
    await settle(hass)
    assert len(writes.calls) == 1
    hot_water.tick(now + timedelta(minutes=5))
    await settle(hass)
    assert len(writes.calls) == 2
    record = hot_water.days[pick.peak.astimezone(hot_water.zone).date().isoformat()]
    assert record["live"] == [pick.peak.isoformat()]
    assert hot_water.ledger.last(SOURCE_PLAN).read_back is True
    # Once written, never again for that peak.
    hot_water.tick(now + timedelta(minutes=6))
    await settle(hass)
    assert len(writes.calls) == 2


# ------------------------------------------------------------------------------------------------ the joint plan


def _live_pick(hot_water: Any, now: datetime, start_in: timedelta = timedelta(hours=1)) -> Any:
    from custom_components.spotnav.heat.dhw_plan import Pick

    start = now.replace(minute=0, second=0, microsecond=0) + start_in
    peak = start + timedelta(hours=2)
    return Pick(start, start + timedelta(minutes=30), 1.5, 40.0, "cheap", peak, peak + timedelta(minutes=20), 150.0, 100.0)


async def test_live_boosts_are_in_what_the_car_and_battery_plans_see(hass: HomeAssistant, freezer, monkeypatch) -> None:
    """A boost that will be written adds its power over its run and takes the pump's own reheat out where it was
    expected, in the house figure every plan reads; in shadow nothing changes."""
    site, controller = await _site(hass, freezer, monkeypatch, _pump(NIBE_F, LUX), control_enabled=True)
    hass.states.async_set(LUX, "Off", {"options": LUX_OPTIONS})
    shadow = controller.heat_shadow
    hot_water = shadow.hot_water
    now = controller.demand.now()
    pick = _live_pick(hot_water, now)
    hot_water.picks = [pick]
    hot_water.run_len = timedelta(minutes=30)
    hot_water.boost_kw = 3.0
    boost_hour = pick.start
    own_hour = pick.own_at.replace(minute=0)
    # Shadow: the plans see nothing of it.
    assert shadow.forecast_delta(now) == {}
    _open_gate(hot_water, now.astimezone(hot_water.zone).date())
    hot_water._delta_key = None  # noqa: SLF001 - the gate opened under the test
    delta = shadow.forecast_delta(now)
    assert delta[boost_hour] == pytest.approx(1.5)  # 3 kW for half the hour
    assert delta[own_hour] == pytest.approx(-1.5)  # 1.5 kWh of the pump's own reheat, moved
    # The house figure the car's import target and the battery plan read (`_with_heat`) carries it.
    forecast = controller.house_forecast
    seen = forecast._with_heat({boost_hour: 1.0, own_hour: 2.0}, now)  # noqa: SLF001
    assert seen == {boost_hour: pytest.approx(2.5), own_hour: pytest.approx(0.5)}
    # Its own planner does not count the boost twice: the load it plans against leaves its own change out.
    slots = _prices(controller, now, (now.astimezone(hot_water.zone).hour + 1,))
    monkeypatch.setattr(controller.battery_shadow.plan, "buy_slots", lambda _now: slots)
    monkeypatch.setattr(forecast, "plan_kw_by_hour", lambda _now, heat=True: forecast._with_heat({boost_hour: 1.0}, _now))  # noqa: SLF001
    inputs = hot_water.inputs(now, shadow.config(), hot_water.route())
    assert inputs.load_kw[boost_hour] == pytest.approx(1.0)


async def test_a_car_that_starts_after_the_boost_was_placed(hass: HomeAssistant, freezer, monkeypatch) -> None:
    """The boost was placed with room under the fuse; a car then starts. The boost is not started without room on
    the tightest phase; once running, SpotNav's own boost is shed under fuse pressure and not started again that
    day. A household's boost is never shed."""
    site, controller = await _site(hass, freezer, monkeypatch, _pump(NIBE_F, LUX), control_enabled=True)
    hass.states.async_set(LUX, "Off", {"options": LUX_OPTIONS})
    writes = Writes(hass)
    hot_water = controller.heat_shadow.hot_water
    monkeypatch.setattr(hot_water, "_meter_fresh", lambda _now: True)
    monkeypatch.setattr(hot_water, "_meter_found", lambda: True)
    now = controller.demand.now()
    _open_gate(hot_water, now.astimezone(hot_water.zone).date())
    pick = _live_pick(hot_water, now, timedelta(0))
    hot_water.picks = [pick]
    hot_water.boost_kw = 3.0
    margins = {"l1": 20.0, "l2": 6.0, "l3": 18.0}
    monkeypatch.setattr(hot_water, "_margins", lambda: margins)
    monkeypatch.setattr(hot_water, "_site_state", lambda: "observing")
    at = max(now, pick.start)
    # 3 kW on one phase is 13 A: the car left 6 A on L2.
    hot_water.tick(at)
    await settle(hass)
    assert writes.calls == []
    margins["l2"] = 16.0
    hot_water.tick(at + timedelta(minutes=1))
    await settle(hass)
    assert writes.calls == [("select", "select_option", {"entity_id": LUX, "option": "One time increase"})]
    # The car takes more: a phase over the fuse for two looks ends SpotNav's own boost.
    margins["l2"] = -2.0
    hot_water.tick(at + timedelta(minutes=2))
    await settle(hass)
    assert len(writes.calls) == 1
    hot_water.tick(at + timedelta(minutes=3))
    await settle(hass)
    assert writes.calls[-1] == ("select", "select_option", {"entity_id": LUX, "option": "Off"})
    assert hot_water.shed_day == at.astimezone(hot_water.zone).date().isoformat()
    # Not started again that day, and the plans no longer count it.
    margins["l2"] = 16.0
    hot_water.picks = [_live_pick(hot_water, at, timedelta(minutes=5))]
    hot_water.tick(at + timedelta(minutes=5))
    await settle(hass)
    assert len(writes.calls) == 2
    hot_water._delta_key = None  # noqa: SLF001
    assert hot_water.forecast_delta(at + timedelta(minutes=5)) == {}
    # The household's own boost is never shed.
    hot_water.shed_day = None
    freezer.tick(timedelta(minutes=30))
    hass.states.async_set(LUX, "One time increase", {"options": LUX_OPTIONS})
    assert hot_water.boost()["source"] == "user"
    margins["l2"] = -5.0
    for minute in range(10, 14):
        hot_water.tick(at + timedelta(minutes=minute))
        await settle(hass)
    assert len(writes.calls) == 2


async def test_a_beta_site_skips_the_gate(hass: HomeAssistant, freezer, monkeypatch) -> None:
    site, controller = await _site(hass, freezer, monkeypatch, _pump(NIBE_F, LUX), control_enabled=True)
    hot_water = controller.heat_shadow.hot_water
    config = controller.heat_shadow.config()
    assert hot_water.mode(config, hot_water.route(config), hot_water._pump()) == "shadow"  # noqa: SLF001
    monkeypatch.setattr(dhw_runtime, "beta_gate_open", lambda _hass, _site: True)
    assert hot_water.gate_state()["open"] is True and hot_water.gate_state()["days"] == 0
    assert hot_water.mode(config, hot_water.route(config), hot_water._pump()) == "live"  # noqa: SLF001
