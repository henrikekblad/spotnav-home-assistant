"""The switched load's shell on a site: idle without a load, the plan and the decisions in the shadow log, the
runtime and the days' tallies, the comfort floor read from a sensor, diagnostics, the debug bundle and the history
across a reload. Nothing is ever switched."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.spotnav.battery.shadow import PART_SWITCH_LOAD
from custom_components.spotnav.debug_bundle import async_build_debug_bundle
from custom_components.spotnav.diagnostics import async_get_config_entry_diagnostics
from custom_components.spotnav.loads.runtime import SwitchLoadShadow
from custom_components.spotnav.loads.switch_load import QUARTER, REASON_CHEAP, REASON_FLOOR, quarter_start
from custom_components.spotnav.planning.planner import FiscalChoice, PlanningSlot
from custom_components.spotnav.runtime import site_controller_for

from tests.world import setup_charger_and_site

pytestmark = pytest.mark.usefixtures("offline_relay")

SWITCH = "switch.water_heater"
LOAD = {"entity_id": SWITCH, "rated_kw": 3.0, "runtime_h": 1.0}


def _prices(monkeypatch: pytest.MonkeyPatch, first: datetime, cheap_hour: int = 0, hours: int = 30) -> None:
    """Hourly prices from `first` on (1 per kWh, 0.1 in `cheap_hour`), in place of the site's price entry."""
    slots = [
        PlanningSlot(first + timedelta(hours=hour) + QUARTER * quarter, 0.1 if hour == cheap_hour else 1.0, "test")
        for hour in range(hours)
        for quarter in range(4)
    ]
    monkeypatch.setattr(
        SwitchLoadShadow, "_read_prices", lambda self, now: (("test", cheap_hour), slots, FiscalChoice(), ())
    )


async def _site(hass: HomeAssistant, load: dict[str, Any] | None = LOAD, switch_state: str | None = "off"):
    if switch_state is not None:
        hass.states.async_set(SWITCH, switch_state)
    extra = {} if load is None else {"switch_load": dict(load)}
    _charger, site = await setup_charger_and_site(hass, extra_data=extra)
    assert site is not None
    controller = site_controller_for(hass, site.entry_id)
    assert controller is not None
    return site, controller


def _service_calls(hass: HomeAssistant) -> list[Any]:
    calls: list[Any] = []
    hass.bus.async_listen("call_service", calls.append)
    return calls


async def test_without_a_load_nothing_runs_and_the_diagnostics_say_none(hass: HomeAssistant) -> None:
    site, controller = await _site(hass, load=None)

    assert controller.switch_load.config is None and controller.switch_load._timer_cancel is None
    assert (await async_get_config_entry_diagnostics(hass, site))["switch_load"] is None


async def test_the_cheap_hour_would_switch_it_on_and_nothing_is_written(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = quarter_start(dt_util.now())
    _prices(monkeypatch, now)
    site, controller = await _site(hass)
    calls = _service_calls(hass)
    load = controller.switch_load
    # A history begun 20 hours ago: the hour is due within 4 hours.
    load.history.tracked_since = now - timedelta(hours=20)
    load._plan_key = None

    load.tick(now + timedelta(seconds=5))
    await hass.async_block_till_done()

    assert load.decision is not None and load.decision.on and load.decision.reason == REASON_CHEAP
    entry = load.book.entries[-1]
    assert entry.part == PART_SWITCH_LOAD and entry.action == "would_write" and entry.value == "on"
    assert entry.entity_id == SWITCH and entry.read == "off" and entry.written is False
    assert not load.book.writes(PART_SWITCH_LOAD)
    assert [call for call in calls if call.data.get("domain") in ("switch", "input_boolean", "homeassistant")] == []
    assert hass.states.get(SWITCH).state == "off"

    diagnostics = (await async_get_config_entry_diagnostics(hass, site))["switch_load"]
    assert diagnostics["part"] == "shadow" and diagnostics["writes"] is False
    assert diagnostics["status"] == "planned" and diagnostics["config"]["daily_kwh"] == 3.0
    assert diagnostics["plan"]["periods"] == [[now.isoformat(), (now + timedelta(hours=1)).isoformat()]]
    assert diagnostics["now"]["reason"] == REASON_CHEAP
    bundle = await async_build_debug_bundle(hass)
    assert bundle["sites"][0]["diagnostics"]["switch_load"]["config"]["entity_id"] == SWITCH


async def test_the_time_it_would_run_is_its_history_and_each_day_is_tallied(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = quarter_start(dt_util.now())
    _prices(monkeypatch, now)
    _site_entry, controller = await _site(hass, switch_state="on")
    load = controller.switch_load
    load.history.tracked_since = now - timedelta(days=2)
    load._plan_key = None

    for seconds in range(0, 61, 10):
        load.tick(now + timedelta(seconds=seconds))

    assert load.history.on_seconds(now, now + timedelta(minutes=5)) == pytest.approx(60.0)
    [tally] = load.days.values()
    assert tally.shadow_s == pytest.approx(60.0) and tally.actual_s == pytest.approx(60.0)
    # 0.1 per kWh is 10 minor units; 3 kW for a minute.
    assert tally.shadow_minor == pytest.approx(10 * 3.0 / 60)


async def test_an_unreadable_switch_is_logged_as_not_possible(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = quarter_start(dt_util.now())
    _prices(monkeypatch, now)
    _site_entry, controller = await _site(hass, switch_state="unavailable")
    load = controller.switch_load

    load.tick(now + timedelta(seconds=5))

    entry = load.book.entries[-1]
    assert entry.action == "not_possible" and entry.reason == "switch_unavailable"


async def test_the_floor_reads_the_sensor_in_fahrenheit_too(hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch) -> None:
    now = quarter_start(dt_util.now())
    _prices(monkeypatch, now, cheap_hour=20)
    hass.states.async_set("sensor.tank", "104", {"unit_of_measurement": "°F"})
    load_settings = {**LOAD, "temperature_entity": "sensor.tank", "floor_c": 45.0}
    _site_entry, controller = await _site(hass, load=load_settings)
    load = controller.switch_load

    load.tick(now + timedelta(seconds=5))

    assert load._temperature_c() == pytest.approx(40.0)
    assert load.decision is not None and load.decision.on and load.decision.reason == REASON_FLOOR
    assert load.book.memory(PART_SWITCH_LOAD)["floor_active"] is True


async def test_the_history_survives_a_reload_and_another_switch_starts_anew(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = quarter_start(dt_util.now())
    _prices(monkeypatch, now)
    site, controller = await _site(hass)
    load = controller.switch_load
    load.history.note(now - timedelta(hours=3), now - timedelta(hours=2))
    load.tick(now + timedelta(seconds=1))

    assert await hass.config_entries.async_reload(site.entry_id)
    await hass.async_block_till_done()
    reloaded = site_controller_for(hass, site.entry_id).switch_load
    assert (now - timedelta(hours=3), now - timedelta(hours=2)) in reloaded.history.runs
    assert reloaded.book.entries

    hass.config_entries.async_update_entry(site, data={**site.data, "switch_load": {**LOAD, "entity_id": "switch.pool"}})
    assert await hass.config_entries.async_reload(site.entry_id)
    await hass.async_block_till_done()
    other = site_controller_for(hass, site.entry_id).switch_load
    assert other.config is not None and other.config.entity_id == "switch.pool"
    assert other.history.runs == []


async def test_removing_the_site_removes_the_loads_store(hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch) -> None:
    removed: list[str] = []

    async def remove(_hass: HomeAssistant, entry_id: str) -> None:
        removed.append(entry_id)

    monkeypatch.setattr(SwitchLoadShadow, "async_remove_stored", staticmethod(remove))
    site, _controller = await _site(hass)

    assert await hass.config_entries.async_remove(site.entry_id)

    assert removed == [site.entry_id]


async def test_the_room_is_the_import_cap_less_the_house_and_the_cars(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    from custom_components.spotnav.loads import runtime as load_runtime

    now = quarter_start(dt_util.now())
    hour = now.replace(minute=0)
    _site_entry, controller = await _site(hass)
    load = controller.switch_load
    monkeypatch.setattr(load_runtime, "site_import_cap_kw", lambda _hass, _site: 10.0)
    monkeypatch.setattr(
        load_runtime, "site_car_windows", lambda _hass, _site: [(hour, hour + timedelta(minutes=30), 8.0)]
    )
    monkeypatch.setattr(controller.house_forecast, "plan_kw_by_hour", lambda _now: {hour: 2.0})

    room = load._room_kw_by_hour(now, {hour, hour + timedelta(hours=1)})

    assert room == {hour: pytest.approx(4.0)}
    assert load.room_unknown_hours == 1 and load.import_cap_kw == 10.0
