"""The debug bundle's day (bundle version 8): 24 hours of minute rows, the last five plug-ins and the last day's
warnings, kept across restarts in stores written at most every five minutes, read back as untrusted."""

from __future__ import annotations

from datetime import timedelta
import json
import logging
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.spotnav.const import CONF_CHARGER_PLATFORM, CONF_CHARGING_STATE
from custom_components.spotnav.debug_bundle import async_build_debug_bundle
from custom_components.spotnav.debug_day import (
    ChargerDay,
    format_row,
    LOG_STORE_KEY,
    parse_row,
    row_time,
    rows_from_store,
    SAMPLE_COUNT,
    SAVE_DELAY_S,
    SESSION_LOG_LENGTHS,
    SESSIONS_KEPT,
    store_key,
)
from custom_components.spotnav.log_buffer import attach_log_buffer, MAX_WARNINGS, SPOTNAV_LOGGER
from custom_components.spotnav.runtime import charger_data

from .helpers import make_entry
from .world import setup_charger_and_site

SWITCH = "switch.garage_connector"
STATUS = "sensor.garage_connector_status"


def _table(block: dict[str, Any]) -> list[dict[str, str]]:
    columns = block["columns"].split(";")
    return [dict(zip(columns, parse_row(row), strict=False)) for row in block["rows"]]


async def _minutes(hass: HomeAssistant, count: int) -> None:
    start = dt_util.utcnow()
    for minute in range(1, count + 1):
        async_fire_time_changed(hass, start + timedelta(seconds=61 * minute))
        await hass.async_block_till_done()


async def _restart(hass: HomeAssistant, *entries: Any) -> None:
    """An unload and a new setup of each entry: what is kept in memory is gone, what is stored is read again."""
    for entry in entries:
        assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    for entry in entries:
        assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


def _plug(event: str, at: str, previous: Any = False) -> dict[str, Any]:
    return {"at": at, "event": {"kind": event, "previous": previous}, "core": [], "today": []}


# ---------------------------------------------------------------------------------------------- rows


def test_a_row_is_compact_text_under_its_columns() -> None:
    row = format_row(["2026-10-31T12:00:00Z", 12.345, None, True, False, 16, 10.0, "a;b", float("nan")])
    assert row == "2026-10-31T12:00:00Z;12.35;;1;0;16;10;a,b;"
    assert parse_row(row)[2] == ""


def test_stored_rows_keep_only_well_formed_ones_of_the_last_day() -> None:
    now = dt_util.utcnow()
    fresh = format_row([row_time(now - timedelta(hours=1)), 1])
    old = format_row([row_time(now - timedelta(hours=25)), 2])
    rows = rows_from_store([old, fresh, 42, None, "not a row", "x" * 3000, {"time": 1}], now)
    assert list(rows) == [fresh]
    assert rows_from_store("garbage", now) == rows_from_store(None, now) and not rows_from_store(None, now)
    many = [format_row([row_time(now - timedelta(minutes=minute)), minute]) for minute in range(SAMPLE_COUNT + 50, 0, -1)]
    assert len(rows_from_store(many, now)) <= SAMPLE_COUNT


# ---------------------------------------------------------------------------------------------- plug-ins


async def test_a_new_plug_in_keeps_the_previous_one(hass: HomeAssistant) -> None:
    day = ChargerDay(hass, "c1", strategy=lambda: "solar")
    await day.async_load()
    day.note_ownership(_plug("plug_in", "2026-10-31T08:00:00+00:00"))
    day.note_solar({"time": "t1", "action": "start", "reason": "surplus", "requested_a": None})
    day.note_ownership(
        {"at": "2026-10-31T09:00:00+00:00", "event": {"kind": "person_stop", "connected": True}, "core": [{"kind": "stop"}]}
    )
    day.note_ownership(_plug("unplug", "2026-10-31T17:00:00+00:00", True))
    day.note_ownership(_plug("plug_in", "2026-10-31T18:00:00+00:00"))
    day.note_phase_switch("sent", phases=1)

    first, second = day.sessions()
    assert first["plugged_in_at"] == "2026-10-31T08:00:00+00:00" and first["unplugged_at"] == "2026-10-31T17:00:00+00:00"
    assert first["strategy"] == "solar"
    assert first["solar"] == [{"time": "t1", "action": "start", "reason": "surplus"}]  # no empty fields kept
    assert [item["event"]["kind"] for item in first["actions"]] == ["person_stop"]
    assert second["plugged_in_at"] == "2026-10-31T18:00:00+00:00" and second["unplugged_at"] is None
    assert second["solar"] == [] and second["phase_switch"][0]["what"] == "sent"


async def test_the_first_connection_after_a_restart_is_the_same_plug_in(hass: HomeAssistant) -> None:
    day = ChargerDay(hass, "c1")
    await day.async_load()
    day.note_ownership(_plug("plug_in", "2026-10-31T08:00:00+00:00"))
    day.note_ownership(_plug("plug_in", "2026-10-31T12:00:00+00:00", previous=None))
    assert len(day.sessions()) == 1
    day.note_ownership(_plug("unplug", "2026-10-31T13:00:00+00:00", previous=None))
    day.note_ownership(_plug("plug_in", "2026-10-31T14:00:00+00:00", previous=None))
    assert len(day.sessions()) == 2


async def test_plug_ins_and_their_logs_are_bounded(hass: HomeAssistant) -> None:
    day = ChargerDay(hass, "c1")
    await day.async_load()
    for number in range(SESSIONS_KEPT + 3):
        day.note_ownership(_plug("plug_in", f"2026-10-{10 + number}T08:00:00+00:00"))
    sessions = day.sessions()
    assert len(sessions) == SESSIONS_KEPT
    assert sessions[0]["plugged_in_at"] == "2026-10-13T08:00:00+00:00"
    bound = SESSION_LOG_LENGTHS["regulator"]
    for number in range(bound + 7):
        day.note_regulator({"time": str(number), "charger_id": "c1", "to_a": 6})
    current = day.sessions()[-1]
    assert len(current["regulator"]) == bound and current["dropped"] == {"regulator": 7}
    assert current["regulator"][0] == {"time": "7", "to_a": 6}  # the oldest dropped; no charger id per entry


async def test_strategy_changes_and_the_car_are_followed_each_minute(hass: HomeAssistant) -> None:
    strategy = ["cheapest"]
    car: dict[str, Any] = {}
    day = ChargerDay(
        hass, "c1", strategy=lambda: strategy[0], vehicle=lambda _plugged: dict(car) or None
    )
    await day.async_load()
    day.note_ownership(_plug("plug_in", "2026-10-31T08:00:00+00:00"))
    strategy[0] = "solar"
    car.update(vehicle_id="vehicle_a", method="answered")
    day.minute()
    day.minute()
    session = day.sessions()[-1]
    assert session["strategy"] == "cheapest"
    assert [change["strategy"] for change in session["strategy_changes"]] == ["solar"]
    assert session["vehicle"] == {"vehicle_id": "vehicle_a", "method": "answered"}
    assert not day.rows, "no row of its own without a reader of one (a member of a site)"


# ---------------------------------------------------------------------------------------------- the store


async def test_malformed_stores_are_ignored(hass: HomeAssistant, hass_storage: dict[str, Any]) -> None:
    hass_storage[store_key("entry_a")] = {
        "version": 1,
        "key": store_key("entry_a"),
        "data": {
            "sessions": [None, 3, {"opened_at": "never"}, {"opened_at": "2026-10-31T08:00:00+00:00", "solar": "x",
                         "regulator": [1, {"to_a": 6}], "dropped": {"regulator": -4}, "vehicle": "car"}],
            "rows": {"not": "a list"},
        },
    }
    hass_storage[store_key("site_entry_a")] = {"version": 1, "key": store_key("site_entry_a"), "data": ["garbage"]}
    hass_storage[LOG_STORE_KEY] = {"version": 1, "key": LOG_STORE_KEY, "data": {"warnings": [1, {"time": "x"}]}}

    await setup_charger_and_site(hass)

    bundle = await async_build_debug_bundle(hass)
    (session,) = [s for s in bundle["chargers"][0]["sessions"] if s["opened_at"] == "2026-10-31T08:00:00+00:00"]
    assert session["solar"] == [] and session["regulator"] == [{"to_a": 6}]
    assert session["dropped"] == {} and session["vehicle"] is None
    assert bundle["chargers"][0]["history_24h"]["rows"] == []
    assert bundle["log"]["warnings_24h"] == [] or all(isinstance(w, dict) for w in bundle["log"]["warnings_24h"])
    json.dumps(bundle)


async def test_a_restart_keeps_the_rows_the_plug_ins_and_the_warnings(hass: HomeAssistant) -> None:
    charger, site = await setup_charger_and_site(hass)
    await _minutes(hass, 3)
    day = charger_data(hass, charger.entry_id).debug_day
    day.note_ownership(_plug("plug_in", dt_util.utcnow().isoformat()))
    day.note_solar({"time": dt_util.utcnow().isoformat(), "action": "start", "reason": "surplus"})
    before = await async_build_debug_bundle(hass)

    await _restart(hass, charger, site)

    after = await async_build_debug_bundle(hass)
    rows = before["sites"][0]["history_24h"]["rows"]
    assert len(rows) == 3 and after["sites"][0]["history_24h"]["rows"][:3] == rows
    assert after["sites"][0]["history_24h"]["chargers"][charger.entry_id]["rows"][:3] == (
        before["sites"][0]["history_24h"]["chargers"][charger.entry_id]["rows"]
    )
    kept = after["chargers"][0]["sessions"]
    assert [s["solar"][0]["reason"] for s in kept if s["solar"]] == ["surplus"]
    await _minutes(hass, 1)
    assert len((await async_build_debug_bundle(hass))["sites"][0]["history_24h"]["rows"]) == 4


async def test_warnings_of_the_last_day_survive_a_restart(hass: HomeAssistant) -> None:
    buffer = attach_log_buffer()
    await buffer.async_attach_store(hass)
    logger = logging.getLogger(f"{SPOTNAV_LOGGER}.test")
    logger.info("an info line")
    logger.warning("the warning that matters")
    await buffer.async_flush()

    again = attach_log_buffer()
    await again.async_attach_store(hass)
    assert [w["message"] for w in again.warnings()] == ["the warning that matters"]
    assert not again.records(), "INFO and above as before: since the start only"

    for number in range(MAX_WARNINGS + 10):
        logger.warning("warning %s", number)
    assert len(again.warnings()) == MAX_WARNINGS
    again._warnings[0]["time"] = (dt_util.utcnow() - timedelta(hours=25)).isoformat()  # noqa: SLF001
    assert len(again.warnings()) == MAX_WARNINGS - 1


async def test_a_minute_costs_no_write_of_its_own(hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every keeper asks the store for one delayed write, `SAVE_DELAY_S` away, however many minutes pass meanwhile."""
    charger, site = await setup_charger_and_site(hass, site=False)
    calls: list[tuple[str, float]] = []
    saves: list[str] = []
    original_delay, original_save = Store.async_delay_save, Store.async_save

    def delay_save(self: Store, data_func: Any, delay: float = 0) -> None:
        calls.append((self.key, delay))
        original_delay(self, data_func, delay)

    async def save(self: Store, data: Any) -> None:
        saves.append(self.key)
        await original_save(self, data)

    monkeypatch.setattr(Store, "async_delay_save", delay_save)
    monkeypatch.setattr(Store, "async_save", save)
    day = charger_data(hass, charger.entry_id).debug_day
    day._store._save_waits = False  # noqa: SLF001 - forget a write scheduled before the counting began
    for _ in range(4):
        day.minute()
    day.note_regulator({"to_a": 6})
    ours = [call for call in calls if call[0] == store_key(charger.entry_id)]
    assert ours == [(store_key(charger.entry_id), SAVE_DELAY_S)]
    assert store_key(charger.entry_id) not in saves
    assert len(day.rows) == 4, "a charger in no site keeps its own rows"


async def test_a_charger_in_no_site_has_its_own_day(hass: HomeAssistant) -> None:
    hass.states.async_set(SWITCH, "off")
    hass.states.async_set(STATUS, "Available")
    entry = make_entry(
        hass,
        entry_id="entry_alone",
        charge_control=SWITCH,
        current_limit=None,
        webhook_id="webhook-alone",
        title="Garage",
        extra={CONF_CHARGER_PLATFORM: "chargeamps", CONF_CHARGING_STATE: {"entity_id": STATUS, "charging_values": ["charging"]}},
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    # A real plug-in, an unplug and another plug-in, as the charger reports them.
    for status in ("Preparing", "Available", "Preparing"):
        hass.states.async_set(STATUS, status)
        await hass.async_block_till_done()
    await _minutes(hass, 2)

    section = (await async_build_debug_bundle(hass))["chargers"][0]
    rows = _table(section["history_24h"])
    assert len(rows) == 2 and rows[-1]["control"] == "off" and rows[-1]["connector"] == "preparing"
    assert {"current_a", "strategy", "solar_state", "phases_read", "setpoint_a", "requested_a"} <= set(rows[-1])
    plugged = [s for s in section["sessions"] if s["plugged_in_at"] is not None]
    assert len(plugged) >= 2 and plugged[-2]["unplugged_at"] is not None and plugged[-1]["unplugged_at"] is None


async def test_removing_the_entries_removes_their_stores(hass: HomeAssistant, hass_storage: dict[str, Any]) -> None:
    charger, site = await setup_charger_and_site(hass)
    await _minutes(hass, 1)
    charger_data(hass, charger.entry_id).debug_day.note_ownership(_plug("plug_in", dt_util.utcnow().isoformat()))
    await _restart(hass, charger, site)  # the unload writes what waits
    assert store_key(charger.entry_id) in hass_storage and store_key(site.entry_id) in hass_storage

    assert await hass.config_entries.async_remove(site.entry_id)
    assert await hass.config_entries.async_remove(charger.entry_id)
    await hass.async_block_till_done()
    assert store_key(charger.entry_id) not in hass_storage and store_key(site.entry_id) not in hass_storage


# ---------------------------------------------------------------------------------------------- size


def _bundle_size(bundle: dict[str, Any]) -> int:
    """The bundle's size as the card saves it (indented by two)."""
    return len(json.dumps(bundle, indent=2))


async def test_a_full_sunny_day_stays_under_the_size_aimed_for(
    hass: HomeAssistant, freezer: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One site, two chargers, a sunny day of solar at a decision, a current write and a load-balancing decision a
    minute on both, the day's minute rows full (every column with a value), and four quieter plug-ins before the
    current one on each."""
    from custom_components.spotnav.runtime import site_controller_for
    from custom_components.spotnav.site import site_history

    monkeypatch.setattr(site_history, "site_values", lambda _site: [-2345.6, "total", 12.34, 11.2, 3.45, 512.3])
    monkeypatch.setattr(
        site_history,
        "controller_values",
        lambda _hass, _id: ["on", True, "connected", "Charging", 63.4, "estimate", "solar", "on", "set_current",
                            "surplus_follows", 3, None, 10, 10],
    )

    from .helpers import make_site_entry
    from .messages import register

    freezer.move_to("2026-10-31T00:00:00+00:00")
    chargers = []
    for name in ("entry_a", "entry_b"):
        control = register(hass, "switch", f"{name}_control", f"Control {name}")
        chargers.append(
            make_entry(hass, entry_id=name, charge_control=control, current_limit=None, webhook_id=f"w-{name}", title=name)
        )
        assert await hass.config_entries.async_setup(name)
    site = make_site_entry(hass, entry_id="site_x", charger_entry_ids=["entry_a", "entry_b"])
    assert await hass.config_entries.async_setup(site.entry_id)
    await hass.async_block_till_done()
    site_controller = site_controller_for(hass, site.entry_id)

    def regulator(minute: int, charger: str) -> dict[str, Any]:
        return {"time": dt_util.utcnow().isoformat(), "charger_id": charger, "outcome": "written", "from_a": 9,
                "to_a": 10 + minute % 3, "proposed_a": 10.4, "requested_a": 10, "reason": "within_limits",
                "detail": "headroom", "yield_reason": None, "yield_state": None, "limiting_phase": "L1",
                "battery_power_w": 512.3, "site_current_a": {"L1": 12.3, "L2": 11.2, "L3": 3.4},
                "charger_current_a": {"L1": 10.1, "L2": 10.0, "L3": 0.0}, "start_credit_a": None}

    def command(minute: int) -> dict[str, Any]:
        return {"at": dt_util.utcnow().isoformat(), "kind": "current", "result": "sent", "error": None,
                "calls": [{"domain": "number", "service": "set_value", "entity_id": "number.garage_max_current",
                           "data": {"value": 10 + minute % 3}, "elapsed_s": 0.12, "outcome": "ok"}],
                "status_before": "Charging", "status_after": "Charging", "outcome": "written"}

    days = [charger_data(hass, entry.entry_id).debug_day for entry in chargers]
    solars = [charger_data(hass, entry.entry_id).solar for entry in chargers]
    for day in days:
        for number in range(4):
            day.note_ownership(_plug("plug_in", dt_util.utcnow().isoformat()))
            for minute in range(10):
                day.note_solar({"time": dt_util.utcnow().isoformat(), "to": "off", "action": "hold", "reason": "no_surplus"})
                day.note_command(command(minute))
                day.note_regulator(regulator(minute, day.entry_id))
                day.note_phase_switch("sent", phases=1)
            day.note_ownership(_plug("unplug", dt_util.utcnow().isoformat(), True))
        day.note_ownership(_plug("plug_in", dt_util.utcnow().isoformat()))
    for minute in range(24 * 60):
        freezer.tick(60)
        site_controller._history.sample(site_controller)  # noqa: SLF001
        for day, solar in zip(days, solars, strict=True):
            solar._record_decision(  # noqa: SLF001 - one decision a minute, as on a sunny day
                state="on", action="set_current", reason="surplus_follows", requested_a=10.0 + minute % 3,
                available_w=2345.6, export_w=2345.6, battery_w=512.3, net_grid_w=-2345.6, car_w=6900.0, priority="car",
            )
            day.note_command(command(minute))
            site_controller._decision_log.append(regulator(minute, day.entry_id))  # noqa: SLF001
            day.note_regulator(regulator(minute, day.entry_id))

    bundle = await async_build_debug_bundle(hass)
    size = _bundle_size(bundle)
    print(f"debug bundle of a full sunny day: {size} bytes")  # noqa: T201 - the measure the release notes quote
    assert len(bundle["sites"][0]["history_24h"]["rows"]) == SAMPLE_COUNT
    assert _table(bundle["sites"][0]["history_24h"]["chargers"]["entry_a"])[-1]["solar_reason"] == "surplus_follows"
    assert all(len(charger["sessions"]) == SESSIONS_KEPT for charger in bundle["chargers"])
    assert size < 1_500_000, size
