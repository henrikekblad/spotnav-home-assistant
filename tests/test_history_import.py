"""The import of earlier charges from hourly statistics: grouping, pricing, overlap, run-once, re-pricing."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest
from homeassistant.core import HomeAssistant

from custom_components.spotnav.const import CONF_ENERGY_REGISTER_ENTITY
from custom_components.spotnav.diagnostics import entry_diagnostics
from custom_components.spotnav.planning.planner import FiscalChoice
from custom_components.spotnav.pricing.relay_contract import parse_day
from custom_components.spotnav.runtime import charger_data
from custom_components.spotnav.sessions.costing import spot_intervals
from custom_components.spotnav.sessions.history_import import (
    build_sessions,
    FETCH_GAP_S,
    group_hours,
    Hour,
    hours_from_rows,
    HistoryImporter,
    migrate_legacy,
)
from custom_components.spotnav.sessions.inputs import PriceMarket
from custom_components.spotnav.sessions.model import (
    SOURCE_IMPORTED_HOURLY,
    SOURCE_REGISTER,
    STARTED_UNKNOWN,
)
from custom_components.spotnav.sessions.store import SessionStore
from custom_components.spotnav.sessions.summary import summarize

from .helpers import make_entry
from .sessions_helpers import priced, session, STOCKHOLM, UTC

CHARGER = "entry_a"
REGISTER = "sensor.charger_energy"
TODAY = datetime(2026, 9, 22, 12, 0, tzinfo=UTC)
MARKET_AREA = SimpleNamespace(currency="SEK", major_unit="kr", minor_unit="öre", tz="Europe/Stockholm")


def at(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=STOCKHOLM).astimezone(UTC) + timedelta(
        hours=hour, minutes=minute
    )


def document(day: date, prices_eur: list[float]):
    """A day document, hourly (24 prices) or 15-minute (96), SEK at a rate of 10."""
    res = 60 if len(prices_eur) == 24 else 15
    return parse_day(
        {
            "v": 1, "area": "SE3", "date": day.isoformat(), "tz": "Europe/Stockholm",
            "start": datetime(day.year, day.month, day.day, tzinfo=STOCKHOLM).isoformat(),
            "res": res, "unit": "EUR/kWh", "prices": prices_eur, "fx": {"SEK": 10.0},
            "fx_date": "2026-01-01", "fx_src": "ECB", "src": "test",
            "published": "2026-01-01T00:00:00+01:00", "retrieved": "2026-01-01T00:00:01+01:00",
        },
        area_id="SE3",
        day=day,
    )


class Repository:
    """Holds some day documents; `later` ones appear only when the relay is asked (one request each)."""

    def __init__(self, held: dict[date, object] | None = None, later: dict[date, object] | None = None) -> None:
        self.held = dict(held or {})
        self.later = dict(later or {})
        self.asked: list[date] = []

    async def async_restore(self) -> None:
        return None

    def day_snapshot(self, area_id: str, day: date):
        return SimpleNamespace(document=self.held.get(day), index_authority="listed")

    async def async_get_archive_day(self, area_id: str, day: date):
        """The relay's archive: asked whatever the index lists; a day it lacks is `None` and is not kept."""
        self.asked.append(day)
        return self.later.get(day)


def market_of(repository: Repository) -> PriceMarket:
    return PriceMarket(repository, "SE3", MARKET_AREA, STOCKHOLM, FiscalChoice())  # type: ignore[arg-type]


class Statistics:
    """The synthetic recorder: the hours the register gained, and how often it was asked."""

    def __init__(self, hours: list[Hour]) -> None:
        self.hours = hours
        self.calls: list[tuple[str, datetime, datetime]] = []

    async def __call__(self, entity: str, start: datetime, end: datetime) -> list[Hour]:
        self.calls.append((entity, start, end))
        return [hour for hour in self.hours if start <= hour.start < end]


@pytest.fixture
async def store(hass: HomeAssistant) -> SessionStore:
    sessions = SessionStore(hass)
    sessions.set_fiscal_resolver(lambda charger_id, area_id: FiscalChoice())
    await sessions.async_load()
    return sessions


def importer_for(
    hass: HomeAssistant,
    store: SessionStore,
    statistics: Statistics,
    repository: Repository,
    *,
    register: str | None = REGISTER,
    clock: list[datetime] | None = None,
    sleeps: list[float] | None = None,
) -> HistoryImporter:
    moment = clock if clock is not None else [TODAY]

    async def sleep(seconds: float) -> None:
        if sleeps is not None:
            sleeps.append(seconds)

    return HistoryImporter(
        hass, CHARGER, store,
        register_entity=lambda: register,
        fetch_hours=statistics,
        market=lambda: market_of(repository),
        now=lambda: moment[0],
        sleep=sleep,
        recorder_ready=lambda: True,
    )


def hour(day: date, h: int, kwh: float) -> Hour:
    return Hour(at(day, h), kwh)


D1 = date(2026, 9, 10)
D2 = date(2026, 9, 11)


# ---- grouping

def test_consecutive_hours_with_energy_are_one_charge_and_a_gap_ends_it() -> None:
    hours = [hour(D1, 1, 3.0), hour(D1, 2, 7.0), hour(D1, 3, 0.05), hour(D1, 4, 2.0), hour(D1, 6, 4.0),
             hour(D1, 7, 0.1), hour(D2, 23, 1.0), hour(date(2026, 9, 12), 0, 1.0)]

    groups = group_hours(hours)

    assert [[item.kwh for item in group] for group in groups] == [[3.0, 7.0], [2.0], [4.0, 0.1], [1.0, 1.0]]


def test_statistics_rows_become_hours_and_unusable_rows_are_skipped() -> None:
    start = at(D1, 1).timestamp()
    rows = [
        {"start": start, "change": 2.5},
        {"start": start * 1000, "change": 1.0},  # milliseconds
        {"start": at(D1, 2), "change": 4.0},  # a datetime
        {"start": start + 7200, "change": None},
        {"start": start + 10800, "change": -3.0},  # a register reset
        {"start": start + 14400, "change": float("nan")},
        {"start": start + 1800, "change": 1.0},  # not on the hour
        {"change": 1.0},
    ]

    hours = hours_from_rows(rows)

    assert sorted(item.kwh for item in hours) == [1.0, 2.5, 4.0]
    assert all(item.start.minute == 0 for item in hours)


# ---- pricing

def test_a_fifteen_minute_day_and_an_hourly_day_price_an_hour_alike() -> None:
    quarters = document(D1, [0.10, 0.20, 0.30, 0.40] * 24)
    hourly = document(D1, [0.25] * 24)
    groups = [[hour(D1, 5, 4.0), hour(D1, 6, 2.0)]]

    a = build_sessions(CHARGER, groups, spot_intervals((quarters,), "SEK"), market=None, energy_source=SOURCE_REGISTER)
    b = build_sessions(CHARGER, groups, spot_intervals((hourly,), "SEK"), market=None, energy_source=SOURCE_REGISTER)

    cost_a, cost_b = (priced(built.sessions)[0].cost_minor for built in (a, b))
    assert cost_a == pytest.approx(6.0 * 0.25 * 10.0 * 100.0)
    assert cost_a == pytest.approx(cost_b)
    assert a.priced_hours == b.priced_hours == 2 and a.unpriced_hours == 0


def test_an_hour_without_a_price_keeps_its_energy_and_has_no_cost() -> None:
    spot = spot_intervals((document(D1, [0.2] * 24),), "SEK")
    groups = [[hour(D1, 23, 3.0), hour(D2, 0, 3.0)]]  # the second hour is on a day with no file

    built = build_sessions(CHARGER, groups, spot, market=None, energy_source=SOURCE_REGISTER)

    (one,) = priced(built.sessions)
    assert one.energy_kwh == pytest.approx(6.0) and one.priced_kwh == pytest.approx(3.0)
    assert one.cost_minor == pytest.approx(3.0 * 0.2 * 10.0 * 100.0)
    assert (built.priced_hours, built.unpriced_hours) == (1, 1)
    nothing = build_sessions(CHARGER, groups, (), market=None, energy_source=SOURCE_REGISTER)
    assert priced(nothing.sessions)[0].cost_minor is None and nothing.unpriced_hours == 2


async def test_an_import_is_priced_with_the_settings_as_they_are_read(hass: HomeAssistant, store: SessionStore) -> None:
    repository = Repository(held={D1: document(D1, [0.2] * 24)})
    importer = importer_for(hass, store, Statistics([hour(D1, 5, 5.0)]), repository)

    await importer.async_run()

    ((plain),) = store.closed(CHARGER)
    store.set_fiscal_resolver(lambda charger_id, area_id: FiscalChoice(vat_enabled=True, vat_percent=25.0))
    ((with_vat),) = store.closed(CHARGER)
    assert plain.cost_minor == pytest.approx(5.0 * 200.0)
    assert with_vat.cost_minor == pytest.approx(5.0 * 250.0)


# ---- the import itself

async def test_earlier_charges_are_imported_as_marked_sessions_before_the_first_live_one(
    hass: HomeAssistant, store: SessionStore
) -> None:
    live_start = at(D2, 8, 30)
    store.close(session(live_start, charger_id=CHARGER), TODAY)
    hours = [hour(D1, 5, 5.0), hour(D1, 6, 5.0), hour(D2, 7, 2.0), hour(D2, 8, 2.0), hour(D2, 9, 2.0)]
    repository = Repository(held={D1: document(D1, [0.2] * 24), D2: document(D2, [0.3] * 24)})
    statistics = Statistics(hours)

    await importer_for(hass, store, statistics, repository).async_run()

    imported = [item for item in store.closed(CHARGER) if item.imported]
    assert [(item.start, item.end) for item in imported] == [(at(D1, 5), at(D1, 7)), (at(D2, 7), at(D2, 8))]
    first = imported[0]
    assert first.source == SOURCE_IMPORTED_HOURLY and first.started_by == STARTED_UNKNOWN
    assert first.solar_share is None and first.vehicle_name is None and first.energy_kwh == pytest.approx(10.0)
    assert first.currency == "SEK" and first.cost_minor == pytest.approx(10.0 * 200.0)
    assert first.public()["source"] == "imported_hourly"
    # The hour the live session began in, and everything after it, is the live session's.
    assert statistics.calls[0][2] == at(D2, 8)
    # A summary counts them with the rest.
    assert sum(item["sessions"] for item in summarize(store.closed(CHARGER), STOCKHOLM, by="month")) == 3


async def test_the_import_runs_once_per_charger(hass: HomeAssistant, store: SessionStore) -> None:
    repository = Repository(held={D1: document(D1, [0.2] * 24)})
    statistics = Statistics([hour(D1, 5, 5.0)])
    importer = importer_for(hass, store, statistics, repository)

    await importer.async_run()
    await importer.async_run()
    again = importer_for(hass, store, statistics, repository)
    await again.async_run()

    assert len(statistics.calls) == 1 and len([i for i in store.closed(CHARGER) if i.imported]) == 1
    assert again.status == "done"
    marker = store.import_marker(CHARGER)
    assert marker is not None and marker["sessions"] == 1 and marker["entity_id"] == REGISTER


async def test_the_marker_survives_a_restart(hass: HomeAssistant, store: SessionStore) -> None:
    importer = importer_for(hass, store, Statistics([hour(D1, 5, 5.0)]), Repository(held={D1: document(D1, [0.2] * 24)}))
    await importer.async_run()
    await store.async_flush()

    reloaded = SessionStore(hass)
    await reloaded.async_load()

    assert reloaded.import_marker(CHARGER) == store.import_marker(CHARGER)
    assert [i.source for i in reloaded.closed_raw(CHARGER)] == [SOURCE_IMPORTED_HOURLY]


async def test_unpriced_hours_are_priced_on_a_later_day_and_not_asked_twice_in_one(
    hass: HomeAssistant, store: SessionStore
) -> None:
    clock = [TODAY]
    repository = Repository(later={})
    statistics = Statistics([hour(D1, 5, 5.0), hour(D2, 5, 5.0)])
    importer = importer_for(hass, store, statistics, repository, clock=clock)

    await importer.async_run()

    assert all(item.cost_minor is None for item in store.closed(CHARGER)) and len(store.closed(CHARGER)) == 2
    assert store.import_marker(CHARGER)["unpriced_hours"] == 2  # type: ignore[index]
    asked = len(repository.asked)

    # The relay's backfill reaches the older day; the same day's restart does not look again.
    repository.later[D1] = document(D1, [0.2] * 24)
    await importer_for(hass, store, statistics, repository, clock=clock).async_run()
    assert len(repository.asked) == asked and all(item.cost_minor is None for item in store.closed(CHARGER))

    clock[0] = TODAY + timedelta(days=1)
    await importer_for(hass, store, statistics, repository, clock=clock).async_run()

    by_start = {item.start: item for item in store.closed(CHARGER)}
    assert by_start[at(D1, 5)].cost_minor == pytest.approx(5.0 * 200.0)
    assert by_start[at(D2, 5)].cost_minor is None and by_start[at(D2, 5)].energy_kwh == 5.0
    marker = store.import_marker(CHARGER)
    assert marker["priced_hours"] == 1 and marker["unpriced_hours"] == 1  # type: ignore[index]
    assert len([i for i in store.closed(CHARGER) if i.imported]) == 2, "re-pricing replaces, never duplicates"


async def test_an_unlisted_past_day_is_fetched_from_the_archive_and_priced_hourly_or_quarterly(
    hass: HomeAssistant, store: SessionStore
) -> None:
    repository = Repository(later={D1: document(D1, [0.2] * 24), D2: document(D2, [0.3] * 96)})
    statistics = Statistics([hour(D1, 5, 5.0), hour(D2, 5, 5.0)])

    await importer_for(hass, store, statistics, repository).async_run()

    by_start = {item.start: item for item in store.closed(CHARGER)}
    assert by_start[at(D1, 5)].cost_minor == pytest.approx(5.0 * 200.0)
    assert by_start[at(D2, 5)].cost_minor == pytest.approx(5.0 * 300.0)
    assert sorted(repository.asked) == [D1, D2]
    marker = store.import_marker(CHARGER)
    assert marker["unpriced_hours"] == 0 and marker["version"] == 2  # type: ignore[index]


async def test_a_day_the_relay_lacks_stays_unpriced_and_is_asked_once_per_run(
    hass: HomeAssistant, store: SessionStore
) -> None:
    repository = Repository()
    statistics = Statistics([hour(D1, 5, 1.0), hour(D1, 6, 1.0), hour(D1, 20, 1.0)])

    await importer_for(hass, store, statistics, repository).async_run()

    assert repository.asked == [D1]
    assert all(item.cost_minor is None for item in store.closed(CHARGER))
    assert store.import_marker(CHARGER)["unpriced_hours"] == 3  # type: ignore[index]


async def test_a_version_one_marker_with_unpriced_hours_is_priced_at_once(
    hass: HomeAssistant, store: SessionStore
) -> None:
    statistics = Statistics([hour(D1, 5, 5.0)])
    repository = Repository()
    await importer_for(hass, store, statistics, repository).async_run()
    marker = store.import_marker(CHARGER)
    assert marker is not None
    store.set_import_marker(CHARGER, {**marker, "version": 1})
    repository.later[D1] = document(D1, [0.2] * 24)

    # The same day: a current marker would wait until tomorrow, a version-1 one does not.
    await importer_for(hass, store, statistics, repository).async_run()

    ((item),) = store.closed(CHARGER)
    assert item.cost_minor == pytest.approx(5.0 * 200.0)
    assert store.import_marker(CHARGER)["version"] == 2  # type: ignore[index]


async def test_a_charger_without_a_register_imports_nothing_and_tries_again_later(
    hass: HomeAssistant, store: SessionStore
) -> None:
    statistics = Statistics([hour(D1, 5, 5.0)])
    repository = Repository(held={D1: document(D1, [0.2] * 24)})

    none = importer_for(hass, store, statistics, repository, register=None)
    await none.async_run()

    assert none.status == "no_register" and statistics.calls == [] and store.closed(CHARGER) == ()
    assert store.import_marker(CHARGER) is None

    await importer_for(hass, store, statistics, repository).async_run()
    assert len(store.closed(CHARGER)) == 1


async def test_a_failing_statistics_read_never_escapes_and_leaves_no_marker(
    hass: HomeAssistant, store: SessionStore
) -> None:
    async def broken(*_args) -> list[Hour]:
        raise RuntimeError("database is locked")

    importer = importer_for(hass, store, Statistics([]), Repository())
    importer._fetch_hours = broken  # type: ignore[assignment]

    await importer.async_run()

    assert importer.status == "failed" and store.import_marker(CHARGER) is None


async def test_without_the_recorder_nothing_is_read(hass: HomeAssistant, store: SessionStore) -> None:
    statistics = Statistics([hour(D1, 5, 5.0)])
    importer = importer_for(hass, store, statistics, Repository())
    importer._recorder_ready = lambda: False

    await importer.async_run()

    assert importer.status == "no_recorder" and statistics.calls == []


async def test_day_files_are_asked_one_at_a_time_with_a_pause_and_held_ones_not_at_all(
    hass: HomeAssistant, store: SessionStore
) -> None:
    sleeps: list[float] = []
    repository = Repository(held={D1: document(D1, [0.2] * 24)}, later={D2: document(D2, [0.3] * 24)})
    statistics = Statistics([hour(D1, 5, 1.0), hour(D1, 20, 1.0), hour(D2, 5, 1.0), hour(date(2026, 9, 12), 5, 1.0)])

    await importer_for(hass, store, statistics, repository, sleeps=sleeps).async_run()

    assert repository.asked == [D2, date(2026, 9, 12)]
    assert sleeps == [FETCH_GAP_S, FETCH_GAP_S]


async def test_the_diagnostics_say_what_was_imported_and_priced(hass: HomeAssistant, store: SessionStore) -> None:
    repository = Repository(held={D1: document(D1, [0.2] * 24)})
    importer = importer_for(hass, store, Statistics([hour(D1, 5, 5.0), hour(D2, 5, 5.0)]), repository)
    assert importer.diagnostics()["status"] == "pending" and importer.diagnostics()["range"] is None

    await importer.async_run()

    report = importer.diagnostics()
    assert report["status"] == "done" and report["sessions"] == 2
    assert report["priced_hours"] == 1 and report["unpriced_hours"] == 1
    assert report["range"]["to"] == TODAY.replace(minute=0).isoformat()  # type: ignore[index]
    assert json.dumps(report)  # plain data


# ---- the records from before intervals

async def test_an_old_record_gets_its_intervals_when_the_day_files_are_held_and_keeps_its_cost_when_not(
    hass: HomeAssistant, store: SessionStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    held = Repository(held={D1: document(D1, [0.2] * 24)})
    monkeypatch.setattr(
        "custom_components.spotnav.sessions.history_import.price_market", lambda hass_, charger: market_of(held)
    )
    covered = session(at(D1, 5), hours=2.0, energy=10.0, cost_minor=1999.0, charger_id=CHARGER)
    uncovered = session(at(D2, 5), hours=2.0, energy=10.0, cost_minor=777.0, charger_id=CHARGER)
    other_currency = replace(session(at(D1, 9), charger_id=CHARGER, cost_minor=123.0), currency="EUR")
    for item in (covered, uncovered, other_currency):
        store.close(item, TODAY)

    migrated = await migrate_legacy(hass, store, CHARGER)

    assert migrated == 1
    by_start = {item.start: item for item in store.closed(CHARGER)}
    assert by_start[at(D1, 5)].cost_basis == "current_settings"
    assert by_start[at(D1, 5)].cost_minor == pytest.approx(10.0 * 0.2 * 10.0 * 100.0)
    assert by_start[at(D2, 5)].cost_basis == "stored" and by_start[at(D2, 5)].cost_minor == 777.0
    assert by_start[at(D1, 9)].cost_basis == "stored" and by_start[at(D1, 9)].cost_minor == 123.0


# ---- wired into a charger entry

@pytest.mark.history_import
@pytest.mark.usefixtures("offline_relay")
async def test_a_charger_schedules_the_import_after_start_and_reports_it_in_diagnostics(hass: HomeAssistant) -> None:
    hass.states.async_set("switch.charger_a", "off")
    hass.states.async_set(REGISTER, "100", {"device_class": "energy", "state_class": "total_increasing", "unit_of_measurement": "kWh"})
    entry = make_entry(
        hass, entry_id=CHARGER, charge_control="switch.charger_a", current_limit=None,
        webhook_id="webhook-a", title="a", extra={CONF_ENERGY_REGISTER_ENTITY: REGISTER},
    )

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    importer = charger_data(hass, CHARGER).history_import
    assert importer is not None and importer.status == "pending", "setup never waits for the import"
    assert entry_diagnostics(hass, entry)["history_import"]["status"] == "pending"
    assert importer._register_entity() == REGISTER
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_the_recorders_hourly_energy_is_asked_in_kwh_as_a_change(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    from homeassistant.components import recorder  # noqa: PLC0415
    from homeassistant.components.recorder import statistics  # noqa: PLC0415

    from custom_components.spotnav.sessions.history_import import recorder_hours  # noqa: PLC0415

    seen: list[tuple] = []

    def fake_statistics(hass_, start, end, ids, period, units, types):
        seen.append((ids, period, units, types))
        return {REGISTER: [{"start": at(D1, 5).timestamp(), "change": 2.0}]}

    class FakeInstance:
        async def async_add_executor_job(self, function, *args):
            return function(*args)

    monkeypatch.setattr(statistics, "statistics_during_period", fake_statistics)
    monkeypatch.setattr(recorder, "get_instance", lambda hass_: FakeInstance())

    hours = await recorder_hours(hass, REGISTER, at(D1, 0), at(D2, 0))

    assert [(item.start, item.kwh) for item in hours] == [(at(D1, 5), 2.0)]
    assert seen == [({REGISTER}, "hour", {"energy": "kWh"}, {"change"})]
