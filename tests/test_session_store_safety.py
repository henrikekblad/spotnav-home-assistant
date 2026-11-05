"""The two session stores never lose a finished charge and never bring back one that is over.

The small store (`spotnav_sessions_open`) holds the open sessions and, until a write of the big one holding them
has landed, the ones that just ended (`closing`). These tests write, "crash" by loading a new store from what is on
disk at that moment, and check what comes back: a session that ended is finished whatever was written, a crash
before the big write resumes the open one, and an older version run in between (it writes only the big store) does
not make the small store's stale open session win.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from freezegun import freeze_time
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.spotnav.runtime import domain_data
from custom_components.spotnav.sessions.model import ChargeSession
from custom_components.spotnav.sessions.store import SAVE_DELAY_S, SessionStore

from .test_session_integration import charger, NOW, one_charge
from .world import go_auto

UTC = timezone.utc
T0 = datetime(2026, 11, 5, 10, 0, tzinfo=UTC)
A, B = "charger_a", "charger_b"
BIG, SMALL = "spotnav_sessions", "spotnav_sessions_open"


def session(charger_id: str, sid: str, start: datetime, end: datetime | None, kwh: float = 5.0) -> ChargeSession:
    return ChargeSession(
        id=sid, charger_id=charger_id, start=start, end=end, energy_kwh=kwh, energy_source="register",
        priced_kwh=0.0, cost_minor=None, reference_cost_minor=None, currency="SEK", major_unit="kr",
        minor_unit="öre", started_by="manual", strategy="cheapest", vehicle_id=None, vehicle_name=None,
        solar_kwh=0.0, solar_known_kwh=0.0, last_register_kwh=100.0 if end is None else None,
        last_sample_at=start if end is None else None, register_entity="sensor.reg" if end is None else None,
    )


def ended(item: ChargeSession, at: datetime) -> ChargeSession:
    return replace(item, end=at, last_register_kwh=None, register_entity=None, last_sample_at=None)


async def at(hass: HomeAssistant, moment: datetime) -> None:
    """Let every timer due by `moment` fire, and what it started finish."""
    with freeze_time(moment):
        async_fire_time_changed(hass, moment)
        await hass.async_block_till_done()


async def restarted(hass: HomeAssistant) -> SessionStore:
    """A store loaded from what is on disk now, as after a crash."""
    again = SessionStore(hass)
    await again.async_load()
    return again


async def two_charging(hass: HomeAssistant) -> tuple[SessionStore, ChargeSession, ChargeSession]:
    store = SessionStore(hass)
    await store.async_load()
    a_open = session(A, "A-1", T0 - timedelta(hours=2), None)
    b_open = session(B, "B-1", T0 - timedelta(hours=1), None)
    with freeze_time(T0 - timedelta(minutes=5)):
        store.set_open(a_open, charger_id=A)
        store.set_open(b_open, charger_id=B)
    await at(hass, T0 - timedelta(minutes=5) + timedelta(seconds=SAVE_DELAY_S + 1))
    return store, a_open, b_open


async def test_a_close_is_kept_when_other_writes_move_the_timers(hass: HomeAssistant, hass_storage: dict) -> None:
    """The review's scenario: A closes, B's minute moves the small write, a marker write follows, a crash at 20 s."""
    store, a_open, b_open = await two_charging(hass)
    with freeze_time(T0):
        store.close(ended(a_open, T0), T0)
    with freeze_time(T0 + timedelta(seconds=3)):
        store.set_open(replace(b_open, energy_kwh=6.0), charger_id=B)
    with freeze_time(T0 + timedelta(seconds=10)):
        store.set_import_marker(B, {"version": 2})
    await at(hass, T0 + timedelta(seconds=20))

    again = await restarted(hass)
    assert again.open_raw(A) is None
    assert [item.id for item in again.closed_raw(A)] == ["A-1"]
    assert again.open_raw(B) is not None and again.open_raw(B).energy_kwh == 6.0


async def test_a_close_whose_big_write_never_lands_is_finished_from_the_small_store(
    hass: HomeAssistant, hass_storage: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, a_open, _b_open = await two_charging(hass)

    async def lost(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(store._store, "async_save", lost)  # noqa: SLF001
    with freeze_time(T0):
        store.close(ended(a_open, T0), T0)
    await at(hass, T0 + timedelta(seconds=SAVE_DELAY_S + 1))
    assert hass_storage[SMALL]["data"]["open"].keys() == {B}
    assert [row["id"] for row in hass_storage[SMALL]["data"]["closing"][A]] == ["A-1"]
    assert A not in hass_storage.get(BIG, {}).get("data", {}).get("sessions", {})

    again = await restarted(hass)
    assert again.open_raw(A) is None
    assert [item.id for item in again.closed_raw(A)] == ["A-1"]
    # The next big write holds it, and only then does the small store let it go.
    await at(hass, T0 + timedelta(seconds=2 * SAVE_DELAY_S + 2))
    assert [row["id"] for row in hass_storage[BIG]["data"]["sessions"][A]] == ["A-1"]
    await at(hass, T0 + timedelta(seconds=3 * SAVE_DELAY_S + 3))
    assert hass_storage[SMALL]["data"]["closing"] == {}


async def test_a_crash_before_any_write_after_the_close_resumes_the_session(
    hass: HomeAssistant, hass_storage: dict
) -> None:
    store, a_open, _b_open = await two_charging(hass)
    with freeze_time(T0):
        store.close(ended(a_open, T0), T0)
    await at(hass, T0 + timedelta(seconds=5))

    again = await restarted(hass)
    assert again.open_raw(A) is not None and again.open_raw(A).id == "A-1"
    assert again.closed_raw(A) == ()


async def test_an_older_version_in_between_does_not_bring_a_stale_open_session_back(
    hass: HomeAssistant, hass_storage: dict
) -> None:
    """2.4.3 left Y open in the small store; 2.4.2 then closed Z and has W open in the big one."""
    y = session(A, "Y", T0 - timedelta(days=3), None)
    z = session(A, "Z", T0 - timedelta(days=2), T0 - timedelta(days=2) + timedelta(hours=3))
    w = session(A, "W", T0 - timedelta(hours=1), None, kwh=1.0)
    hass_storage[SMALL] = {"version": 1, "data": {"open": {A: y.as_dict()}}}
    hass_storage[BIG] = {
        "version": 1, "data": {"sessions": {A: [z.as_dict()]}, "open": {A: w.as_dict()}, "imports": {}},
    }

    with freeze_time(T0):
        store = await restarted(hass)
        assert store.open_raw(A) is not None and store.open_raw(A).id == "W"
        assert [item.id for item in store.closed_raw(A)] == ["Z"]
        # The small store is set right.
        await at(hass, T0 + timedelta(seconds=SAVE_DELAY_S + 1))
    assert hass_storage[SMALL]["data"]["open"][A]["id"] == "W"

    # Y alone, but older than the newest finished session: it is over, never resumed.
    hass_storage[BIG] = {"version": 1, "data": {"sessions": {A: [z.as_dict()]}, "open": {}, "imports": {}}}
    hass_storage[SMALL] = {"version": 1, "data": {"open": {A: y.as_dict()}}}
    with freeze_time(T0):
        assert (await restarted(hass)).open_raw(A) is None


async def test_the_small_stores_open_session_is_kept_when_it_is_the_newer(
    hass: HomeAssistant, hass_storage: dict
) -> None:
    older = session(A, "X", T0 - timedelta(hours=2), None)
    newer = replace(older, energy_kwh=9.0, last_sample_at=T0)
    hass_storage[BIG] = {"version": 1, "data": {"sessions": {}, "open": {A: older.as_dict()}, "imports": {}}}
    hass_storage[SMALL] = {"version": 1, "data": {"open": {A: newer.as_dict()}, "closing": {}}}

    with freeze_time(T0):
        store = await restarted(hass)
    assert store.open_raw(A) == newer


async def test_a_small_store_without_a_big_one_still_resumes(hass: HomeAssistant, hass_storage: dict) -> None:
    first = session(A, "F", T0 - timedelta(hours=1), None)
    hass_storage[SMALL] = {"version": 1, "data": {"open": {A: first.as_dict()}, "closing": {}}}

    with freeze_time(T0):
        assert (await restarted(hass)).open_raw(A) == first


@pytest.mark.usefixtures("offline_relay")
async def test_a_clean_stop_writes_the_big_store_current(hass: HomeAssistant, hass_storage: dict) -> None:
    with freeze_time(NOW) as frozen:
        entry = await charger(hass)
        await go_auto(hass)
        await one_charge(hass, frozen, entry, hours=1.0, kwh=6.0)
        store = domain_data(hass).session_store
        running = replace(store.closed_raw(entry.entry_id)[-1], id="next", end=None)
        store.set_open(running, charger_id=entry.entry_id)
        assert BIG not in hass_storage, "no big write has landed yet"

        hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
        await hass.async_block_till_done()

    data = hass_storage[BIG]["data"]
    assert [row["energy_kwh"] for row in data["sessions"][entry.entry_id]] == [pytest.approx(6.0)]
    assert data["open"][entry.entry_id]["id"] == "next"
    assert hass_storage[SMALL]["data"]["closing"] == {}
