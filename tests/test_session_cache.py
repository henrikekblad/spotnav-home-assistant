"""The costed sessions are made once per change, and say exactly what costing them on every read said.

`SessionStore.closed`, `month_summary` and `sessions_summary` cache what they cost. These tests hold the cached
answers against the path every reader took before (each session `priced` with the fiscal choice of its market,
then `summary.month_summary` / `sessions_summary` over all of them) on a long synthetic history, and pin when the
cache is made again: a finished session, a changed fee, never an open session's minute or a controller notify.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from freezegun import freeze_time
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.spotnav.planning.auto_settings import AreaAutoSettings, FiscalOverride
from custom_components.spotnav.planning.planner import FiscalChoice, HighLoadWindow
from custom_components.spotnav.runtime import charger_data, domain_data
from custom_components.spotnav.sessions import model as model_module
from custom_components.spotnav.sessions.costing import Slice
from custom_components.spotnav.sessions.model import ChargeSession, SOURCE_IMPORTED_HOURLY
from custom_components.spotnav.sessions.store import SAVE_DELAY_S, SessionStore
from custom_components.spotnav.sessions.summary import (
    available_months,
    month_key,
    month_summary,
    previous_month,
    sessions_csv,
    sessions_summary,
)

from .test_session_integration import charger, NOW, one_charge
from .world import entity_id, go_auto, SE4

UTC = timezone.utc
STOCKHOLM = ZoneInfo("Europe/Stockholm")
CHARGER = "entry_a"
END = datetime(2026, 11, 4, 22, tzinfo=UTC)

PLAIN = FiscalChoice(
    tax_enabled=True, tax_minor_per_kwh=42.8, transfer_enabled=True, transfer_minor_per_kwh=30.0,
    vat_enabled=True, vat_percent=25.0,
)
WINDOW = replace(
    PLAIN,
    transfer_high_enabled=True,
    transfer_high_minor_per_kwh=80.0,
    high_load_window=HighLoadWindow(tz="Europe/Stockholm"),
)


def history(days: int = 730) -> list[ChargeSession]:
    """One charge a day for `days` days: most priced in quarter hours in two markets, some imported, some with only
    a stored cost, some with no price at all, a few in another currency."""
    out = []
    first = END - timedelta(days=days)
    for index in range(days):
        start = first + timedelta(days=index, minutes=7 * (index % 9))
        slices = tuple(
            Slice(
                start + timedelta(minutes=15 * k),
                start + timedelta(minutes=15 * (k + 1)),
                0.25 + 0.01 * (k % 5),
                -0.01 + 0.003 * ((index + k) % 40),
                11.2,
                date(2026, 1, 1),
                0.07,
            )
            for k in range(32)
        )
        kind = index % 10
        stored_cost = kind == 7
        out.append(
            ChargeSession(
                id=f"{CHARGER}-{index}",
                charger_id=CHARGER,
                start=start,
                end=start + timedelta(hours=8),
                energy_kwh=9.6 + 0.1 * kind,
                energy_source="estimated" if kind == 3 else "register",
                priced_kwh=8.0 if stored_cost else 0.0,
                cost_minor=900.0 if stored_cost else None,
                reference_cost_minor=950.0 if stored_cost else None,
                currency="EUR" if kind == 9 else "SEK",
                major_unit="kr",
                minor_unit="öre",
                started_by="unknown" if kind == 5 else "manual",
                strategy="cheapest",
                vehicle_id=None,
                vehicle_name="Car" if kind % 2 else None,
                solar_kwh=1.0 if kind == 4 else 0.0,
                solar_known_kwh=4.0 if kind == 4 else 0.0,
                last_register_kwh=None,
                last_sample_at=None,
                source=SOURCE_IMPORTED_HOURLY if kind == 5 else None,
                intervals=() if stored_cost or kind == 8 else slices,
                area_id=None if stored_cost or kind == 8 else ("SE4" if kind == 6 else "SE3"),
            )
        )
    return out


def costed_as_before(store: SessionStore, fiscal_of) -> tuple[ChargeSession, ...]:
    """Every session priced on its own read, the way `closed` did it before the cache."""
    return tuple(
        item if not item.intervals else item.priced(fiscal_of(item.charger_id, item.area_id))
        for item in store.closed_raw(CHARGER)
    )


@pytest.fixture
async def store(hass: HomeAssistant):
    holder = {"fiscal": {"SE3": WINDOW, "SE4": PLAIN}}
    sessions = SessionStore(hass)

    def resolve(charger_id: str, area_id: str | None) -> FiscalChoice | None:
        return holder["fiscal"].get(area_id)

    sessions.set_fiscal_resolver(resolve)
    await sessions.async_load()
    sessions.replace_closed(CHARGER, history(), END)
    sessions.resolve = resolve  # type: ignore[attr-defined]
    sessions.holder = holder  # type: ignore[attr-defined]
    return sessions


def assert_same_as_before(store: SessionStore) -> None:
    before = costed_as_before(store, store.resolve)  # type: ignore[attr-defined]
    after = store.closed(CHARGER)
    assert after == before
    assert [item.public(STOCKHOLM) for item in after] == [item.public(STOCKHOLM) for item in before]
    months = available_months(before, STOCKHOLM)
    assert len(months) >= 24
    for key in months:
        assert store.month_summary(CHARGER, STOCKHOLM, key) == month_summary(before, STOCKHOLM, key)
    assert store.month_summary(CHARGER, STOCKHOLM, "2020-01") == month_summary(before, STOCKHOLM, "2020-01")
    for moment in (END, END - timedelta(days=40), END + timedelta(days=31)):
        assert store.sessions_summary(CHARGER, STOCKHOLM, moment) == sessions_summary(before, STOCKHOLM, moment)
    assert sessions_csv(after, STOCKHOLM, first=None, last=None) == sessions_csv(
        before, STOCKHOLM, first=None, last=None
    )


async def test_the_cached_figures_are_the_ones_costing_on_every_read_made(store: SessionStore) -> None:
    assert_same_as_before(store)


async def test_a_changed_fee_costs_the_history_again(store: SessionStore) -> None:
    first = store.month_summary(CHARGER, STOCKHOLM, month_key(END.date()))
    store.holder["fiscal"] = {  # type: ignore[attr-defined]
        "SE3": replace(WINDOW, tax_minor_per_kwh=10.0),
        "SE4": replace(PLAIN, vat_percent=0.0),
    }

    assert store.month_summary(CHARGER, STOCKHOLM, month_key(END.date())) != first
    assert_same_as_before(store)

    store.holder["fiscal"] = {}  # type: ignore[attr-defined]
    assert_same_as_before(store)


async def test_a_finished_session_costs_the_history_again_and_an_open_ones_minute_does_not(
    store: SessionStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = []
    real = model_module.price_slices

    def counted(slices, fiscal):
        calls.append(1)
        return real(slices, fiscal)

    monkeypatch.setattr(model_module, "price_slices", counted)
    store.closed(CHARGER)
    priced = sum(1 for item in store.closed_raw(CHARGER) if item.intervals)
    assert len(calls) == priced

    # Many readers, an open session's minutes, an import marker: nothing is costed again.
    calls.clear()
    template = store.closed_raw(CHARGER)[-1]
    running = replace(template, id="open", start=END + timedelta(hours=1), end=None)
    for _ in range(5):
        store.set_open(running, charger_id=CHARGER)
        store.closed(CHARGER)
        store.sessions_summary(CHARGER, STOCKHOLM, END)
        store.month_summary(CHARGER, STOCKHOLM, previous_month(END.date()))
    store.set_import_marker(CHARGER, {"version": 2})
    store.closed(CHARGER)
    assert calls == []

    # The session ends: the history is costed once more, then read from the cache again.
    store.close(replace(running, end=END + timedelta(hours=3)), END + timedelta(hours=3))
    store.closed(CHARGER)
    store.closed(CHARGER)
    assert len(calls) == priced + 1
    assert_same_as_before(store)


async def test_a_month_summary_handed_out_is_a_copy(store: SessionStore) -> None:
    key = month_key(END.date())
    summary = store.month_summary(CHARGER, STOCKHOLM, key)
    summary["sessions"] = -1

    assert store.month_summary(CHARGER, STOCKHOLM, key)["sessions"] != -1


async def test_a_removed_charger_drops_its_costed_sessions(store: SessionStore) -> None:
    assert store.closed(CHARGER)

    await store.async_remove_charger(CHARGER)

    assert store.closed(CHARGER) == ()
    assert store.month_summary(CHARGER, STOCKHOLM, month_key(END.date()))["sessions"] == 0


@pytest.mark.usefixtures("offline_relay")
async def test_the_sensors_cost_nothing_on_a_controller_notify(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    keys = (
        "sessions_energy_this_month", "sessions_cost_this_month", "sessions_energy_last_month",
        "sessions_cost_last_month", "sessions_last_energy", "sessions_last_cost",
    )
    with freeze_time(NOW) as frozen:
        entry = await charger(hass)
        await go_auto(hass)
        await one_charge(hass, frozen, entry, hours=1.0, kwh=6.0)
        await hass.async_block_till_done()

        def states():
            return {
                key: (hass.states.get(entity_id(hass, entry.entry_id, key)).state,
                      dict(hass.states.get(entity_id(hass, entry.entry_id, key)).attributes))
                for key in keys
            }

        shown = states()
        assert float(shown["sessions_last_energy"][0]) == pytest.approx(6.0)
        calls = []
        real = model_module.price_slices

        def counted(slices, fiscal):
            calls.append(1)
            return real(slices, fiscal)

        monkeypatch.setattr(model_module, "price_slices", counted)
        controller = charger_data(hass, entry.entry_id).controller
        for _ in range(3):
            controller._notify()  # noqa: SLF001 - what every controller change does
        await hass.async_block_till_done()

        assert calls == []
        assert states() == shown
        # The store's cache is what the sensors show.
        store = domain_data(hass).session_store
        last = store.closed(entry.entry_id)[-1]
        assert float(shown["sessions_last_cost"][0]) == pytest.approx(round(last.cost_minor / 100, 4))


@pytest.mark.usefixtures("offline_relay")
async def test_a_fee_changed_in_the_settings_shows_in_the_cost_sensors_at_once(hass: HomeAssistant) -> None:
    with freeze_time(NOW) as frozen:
        entry = await charger(hass)
        await go_auto(hass)
        await one_charge(hass, frozen, entry, hours=1.0, kwh=6.0)
        await hass.async_block_till_done()
        cost = entity_id(hass, entry.entry_id, "sessions_last_cost")
        before = float(hass.states.get(cost).state)

        await go_auto(hass, overrides=(AreaAutoSettings(area_id=SE4, transfer=FiscalOverride(True, 100.0)),))

        after = float(hass.states.get(cost).state)
        assert after != before
        store = domain_data(hass).session_store
        assert after == pytest.approx(round(store.closed(entry.entry_id)[-1].cost_minor / 100, 4))


# ---------------------------------------------------------------------------------------------- the save


def todays_document(sessions: list[ChargeSession], opened: ChargeSession | None, imports: dict) -> dict:
    """The document as the store wrote it before the save kept rows per session (2.4.2)."""
    return {
        "sessions": {CHARGER: [item.as_dict() for item in sessions]},
        "open": {} if opened is None else {CHARGER: opened.as_dict()},
        "imports": imports,
    }


async def test_a_record_of_todays_format_loads_and_is_written_back_unchanged(
    hass: HomeAssistant, hass_storage: dict
) -> None:
    sessions = sorted(history(), key=lambda item: item.start)
    template = sessions[-1]
    opened = replace(template, id="open", start=END + timedelta(hours=1), end=None, last_register_kwh=12.5,
                     last_sample_at=END + timedelta(hours=2), register_entity="sensor.charger_energy")
    imports = {CHARGER: {"version": 2, "entity_id": "sensor.charger_energy", "from": "2024-11-04T22:00:00+00:00",
                         "to": "2026-11-04T22:00:00+00:00", "sessions": 3, "priced_hours": 10, "unpriced_hours": 0,
                         "checked_day": "2026-11-04"}}
    written = todays_document(sessions, opened, imports)
    hass_storage["spotnav_sessions"] = {"version": 1, "data": written}

    store = SessionStore(hass)
    store.set_fiscal_resolver(lambda charger_id, area_id: PLAIN)
    with freeze_time(END):
        await store.async_load()
    assert list(store.closed_raw(CHARGER)) == sessions
    assert store.open_raw(CHARGER) == opened
    assert store.import_marker(CHARGER) == imports[CHARGER]

    await store.async_flush()
    assert hass_storage["spotnav_sessions"]["data"] == written

    assert hass_storage["spotnav_sessions_open"]["data"] == {"open": {CHARGER: opened.as_dict()}, "closing": {}}


async def test_a_record_from_before_the_small_store_resumes_its_open_session_from_the_big_one(
    hass: HomeAssistant, hass_storage: dict
) -> None:
    sessions = sorted(history(30), key=lambda item: item.start)
    opened = replace(sessions[-1], id="open", start=END + timedelta(hours=1), end=None)
    hass_storage["spotnav_sessions"] = {"version": 1, "data": todays_document(sessions, opened, {})}
    assert "spotnav_sessions_open" not in hass_storage

    store = SessionStore(hass)
    with freeze_time(END):
        await store.async_load()

    assert store.open_raw(CHARGER) == opened
    # The first minute after the update writes the small store only; the big one is left as it was.
    later = replace(opened, energy_kwh=opened.energy_kwh + 0.5)
    store.set_open(later, charger_id=CHARGER)
    await hass.async_block_till_done()
    with freeze_time(END + timedelta(seconds=SAVE_DELAY_S + 1)):
        async_fire_time_changed(hass, END + timedelta(seconds=SAVE_DELAY_S + 1))
        await hass.async_block_till_done()
    assert hass_storage["spotnav_sessions_open"]["data"] == {"open": {CHARGER: later.as_dict()}, "closing": {}}
    assert hass_storage["spotnav_sessions"]["data"] == todays_document(sessions, opened, {})


async def test_the_small_store_wins_and_a_session_it_holds_that_ended_is_dropped(
    hass: HomeAssistant, hass_storage: dict
) -> None:
    sessions = sorted(history(30), key=lambda item: item.start)
    stale = replace(sessions[0], id="stale-open", end=None)
    hass_storage["spotnav_sessions"] = {"version": 1, "data": todays_document(sessions, stale, {})}

    # The small store says no session is open: the big one's stale open session is not resumed.
    hass_storage["spotnav_sessions_open"] = {"version": 1, "data": {"open": {}}}
    store = SessionStore(hass)
    with freeze_time(END):
        await store.async_load()
    assert store.open_raw(CHARGER) is None

    # The small store still holds a session that the big one has among the finished ones: it ended.
    ended = sessions[-1]
    hass_storage["spotnav_sessions_open"] = {
        "version": 1, "data": {"open": {CHARGER: replace(ended, end=None).as_dict()}},
    }
    store = SessionStore(hass)
    with freeze_time(END):
        await store.async_load()
    assert store.open_raw(CHARGER) is None
    assert store.closed_raw(CHARGER)[-1] == ended
    await store.async_flush()
    assert hass_storage["spotnav_sessions_open"]["data"] == {"open": {}, "closing": {}}


async def test_an_open_sessions_minute_writes_only_the_small_store_and_a_close_writes_the_big_one_first(
    hass: HomeAssistant, hass_storage: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SessionStore(hass)
    await store.async_load()
    sessions = history(60)
    with freeze_time(END):
        store.replace_closed(CHARGER, sessions, END)
        async_fire_time_changed(hass, END + timedelta(seconds=SAVE_DELAY_S + 1))
        await hass.async_block_till_done()
    assert store._store._serialize_in_event_loop is False  # noqa: SLF001 - the rows are made in the executor
    big = hass_storage["spotnav_sessions"]["data"]

    made = []
    real = ChargeSession.as_dict

    def counted(self):
        made.append(self.id)
        return real(self)

    monkeypatch.setattr(ChargeSession, "as_dict", counted)
    running = replace(sessions[-1], id="open", start=END + timedelta(hours=1), end=None)
    moment = END
    for minute in range(3):
        moment = END + timedelta(minutes=minute + 1)
        with freeze_time(moment):
            store.set_open(replace(running, energy_kwh=minute + 1.0), charger_id=CHARGER)
            async_fire_time_changed(hass, moment + timedelta(seconds=SAVE_DELAY_S + 1))
            await hass.async_block_till_done()
    assert set(made) == {"open"}, "no finished session is turned into a row while a car charges"
    assert hass_storage["spotnav_sessions"]["data"] is big
    assert hass_storage["spotnav_sessions_open"]["data"]["open"][CHARGER]["energy_kwh"] == 3.0

    # The session ends: the small store holds it as ending until the big store has it, then forgets it.
    made.clear()
    done = replace(running, end=moment)
    with freeze_time(moment):
        store.close(done, moment)
        assert store._open_document()["closing"] == {CHARGER: [real(done)]}  # noqa: SLF001
        async_fire_time_changed(hass, moment + timedelta(seconds=SAVE_DELAY_S + 1))
        await hass.async_block_till_done()
    assert hass_storage["spotnav_sessions"]["data"]["sessions"][CHARGER][-1] == real(done)
    assert hass_storage["spotnav_sessions"]["data"]["open"] == {}
    with freeze_time(moment):
        async_fire_time_changed(hass, moment + timedelta(seconds=2 * SAVE_DELAY_S + 1))
        await hass.async_block_till_done()
    assert hass_storage["spotnav_sessions_open"]["data"] == {"open": {}, "closing": {}}


async def test_the_big_stores_snapshot_is_the_store_as_it_stood(hass: HomeAssistant) -> None:
    store = SessionStore(hass)
    await store.async_load()
    sessions = history(60)
    store.replace_closed(CHARGER, sessions, END)
    store.set_import_marker(CHARGER, {"version": 2, "unpriced_days": ["2026-01-01"]})
    closed, opened, imports = store._snapshot()  # noqa: SLF001

    running = replace(sessions[-1], id="open", start=END + timedelta(hours=1), end=None)
    store.set_open(running, charger_id=CHARGER)
    store.close(replace(running, end=END + timedelta(hours=2)), END + timedelta(hours=2))
    store._imports[CHARGER]["unpriced_days"].append("2026-01-02")  # noqa: SLF001

    assert len(closed[CHARGER]) == 60 and opened == {}
    assert imports == {CHARGER: {"version": 2, "unpriced_days": ["2026-01-01"]}}
