"""The ECB reference rates Home Assistant holds itself: parsing, the relay's 7-day rule, the store and the
daily timer. No network: the ECB files are recorded fixtures (`tests/fixtures/ecb/`), served by the suite's stub
transport; time and timers are injected."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from homeassistant.core import HomeAssistant

from custom_components.spotnav.pricing.fx_rates import (
    DAILY_URL,
    ECB_ZONE,
    EcbTable,
    expected_table,
    FxParseError,
    FxRates,
    HISTORY_URL,
    MAX_RESPONSE_BYTES,
    MAX_TABLES,
    next_publication,
    parse_daily,
    parse_history,
    table_for,
    USER_AGENT,
)
from .relay import Clock, FakeScheduler, StoreDouble, StubTransport

FIXTURES = Path(__file__).parent / "fixtures" / "ecb"

#: 2026-10-30 17:00 in Frankfurt (CET): after that day's publication, the newest table in the history fixture.
AFTER_PUBLICATION = datetime(2026, 10, 30, 16, 0, tzinfo=timezone.utc)


def body(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


DAILY = "ecb_daily.xml"
HISTORY = "ecb_hist_90d_2026-10-30.xml"


class EcbTransport(StubTransport):
    """The stub transport, also recording the headers each request carried."""

    def __init__(self) -> None:
        super().__init__()
        self.headers: list[dict[str, str]] = []

    def get(self, url: str, **kwargs: Any):
        self.headers.append(kwargs.get("headers") or {})
        return super().get(url)

    def serve_file(self, url: str, name: str) -> None:
        self.serve_chunks(url, 200, [body(name)])


def history_xml(*tables: tuple[str, dict[str, str]]) -> bytes:
    cubes = "".join(
        f'<Cube time="{day}">' + "".join(f'<Cube currency="{c}" rate="{r}"/>' for c, r in rates.items()) + "</Cube>"
        for day, rates in tables
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<gesmes:Envelope xmlns:gesmes="http://www.gesmes.org/xml/2002-08-01" '
        'xmlns="http://www.ecb.int/vocabulary/2002-08-01/eurofxref">'
        f"<Cube>{cubes}</Cube></gesmes:Envelope>"
    ).encode()


@pytest.fixture
def transport() -> EcbTransport:
    return EcbTransport()


@pytest.fixture
def clock() -> Clock:
    return Clock(AFTER_PUBLICATION)


@pytest.fixture
def store() -> StoreDouble:
    return StoreDouble()


@pytest.fixture
def scheduler() -> FakeScheduler:
    return FakeScheduler()


@pytest.fixture
def rates(hass: HomeAssistant, transport, store, clock, scheduler) -> FxRates:
    return FxRates(hass, session=transport, store=store, now=clock, jitter=lambda: 0.5, scheduler=scheduler)


# ------------------------------------------------------------------ parsing


def test_the_daily_file_is_read_as_the_relay_reads_it():
    table = parse_daily(body(DAILY))
    assert table.day == date(2026, 9, 18)
    assert table.values["SEK"] == 11.2915
    assert table.values["NOK"] == 10.8095
    assert table.values["DKK"] == 7.4754
    assert table.values["PLN"] == 4.3635
    assert "EUR" not in table.values


def test_the_90_day_history_is_read_whole_newest_first():
    tables = parse_history(body(HISTORY))
    assert len(tables) == 65
    assert (tables[0].day, tables[-1].day) == (date(2026, 10, 30), date(2026, 8, 3))
    assert tables[0].values["SEK"] == 10.978
    assert tables[0].values["NOK"] == 10.312
    assert tables[0].values["DKK"] == 7.4735
    assert all(table.day.weekday() < 5 for table in tables)


@pytest.mark.parametrize(
    "raw",
    [
        b"not xml at all",
        b"<x/>",
        # Half a table is not a table: NOK and DKK are missing.
        history_xml(("2026-09-18", {"SEK": "11.2915"})),
        history_xml(("2026-09-18", {"SEK": "eleven", "NOK": "10.8", "DKK": "7.47"})),
        history_xml(("2026-09-18", {"SEK": "-1", "NOK": "10.8", "DKK": "7.47"})),
        history_xml(("2026-09-18", {"SEK": "nan", "NOK": "10.8", "DKK": "7.47"})),
        history_xml(("18 Sep 2026", {"SEK": "11.2", "NOK": "10.8", "DKK": "7.47"})),
    ],
)
def test_a_body_that_is_not_a_whole_table_is_refused(raw: bytes):
    with pytest.raises(FxParseError):
        parse_daily(raw)


def test_one_wrong_entry_fails_the_whole_history():
    raw = history_xml(("2026-10-02", {"GBP": "0.87"}), ("2026-10-01", {"GBP": "zero"}))
    with pytest.raises(FxParseError):
        parse_history(raw)


# ------------------------------------------------------------------ the rule


def _on(tables: list[EcbTable], day: str) -> str:
    table = table_for(tables, date.fromisoformat(day))
    return "none" if table is None else table.day.isoformat()


def test_the_relays_cases_for_a_days_table():
    # The relay's own history test (`ecb_test.go`), case for case.
    tables = parse_history(
        history_xml(
            ("2026-10-02", {"GBP": "0.8712", "SEK": "11.0"}),
            ("2026-10-01", {"GBP": "0.8700"}),
            ("2026-09-25", {"GBP": "0.8650"}),
            ("2024-02-09", {"GBP": "0.8550"}),
        )
    )
    assert _on(tables, "2026-10-04") == "2026-10-02"  # tomorrow, priced the afternoon before
    assert _on(tables, "2026-10-01") == "2026-10-01"
    assert _on(tables, "2026-09-28") == "2026-09-25"  # a weekend's Monday falls back to the last table
    assert _on(tables, "2026-09-24") == "none"  # nothing on or before it within a week
    assert _on(tables, "2024-02-10") == "2024-02-09"
    assert _on(tables, "2024-02-20") == "none"  # more than a week after the last table held


def test_weekends_and_the_week_bound_on_the_recorded_history():
    tables = parse_history(body(HISTORY))
    assert _on(tables, "2026-10-24") == "2026-10-23"  # Saturday: Friday's
    assert _on(tables, "2026-10-25") == "2026-10-23"  # Sunday: Friday's
    assert _on(tables, "2026-10-26") == "2026-10-26"  # Monday: its own
    assert _on(tables, "2026-10-31") == "2026-10-30"  # tomorrow: today's
    assert _on(tables, "2026-11-06") == "2026-10-30"  # exactly seven days on: still
    assert _on(tables, "2026-11-07") == "none"  # eight: no rate
    assert _on(tables, "2026-08-02") == "none"  # before the oldest table held


def test_target_holidays_fall_back_to_the_last_working_day():
    # Easter 2026: Good Friday 3 April and Easter Monday 6 April publish nothing.
    tables = parse_history(
        history_xml(("2026-04-07", {"SEK": "10.90"}), ("2026-04-02", {"SEK": "10.85"}), ("2026-03-31", {"SEK": "10.8"}))
    )
    assert _on(tables, "2026-04-03") == "2026-04-02"
    assert _on(tables, "2026-04-06") == "2026-04-02"
    assert _on(tables, "2026-04-07") == "2026-04-07"
    # Without the table after the holidays, the week bound still applies from the last one held.
    before = [table for table in tables if table.day < date(2026, 4, 7)]
    assert _on(before, "2026-04-09") == "2026-04-02"
    assert _on(before, "2026-04-10") == "none"


async def test_rate_for_answers_rate_and_its_date(hass, rates, store):
    store.payload = {"tables": [{"date": "2026-10-23", "values": {"SEK": 11.131, "NOK": 10.4665}}]}
    await rates.async_ensure_restored()
    assert rates.rate_for("SEK", date(2026, 10, 25)) == (11.131, date(2026, 10, 23))
    assert rates.rate_for("NOK", date(2026, 10, 30)) == (10.4665, date(2026, 10, 23))
    # Eight days on: no rate, which is what the planner reports as `missing_fx_rate`.
    assert rates.rate_for("SEK", date(2026, 10, 31)) is None
    # A currency the table does not carry, and EUR, which needs none.
    assert rates.rate_for("DKK", date(2026, 10, 25)) is None
    assert rates.rate_for("EUR", date(2026, 10, 25)) is None


# ------------------------------------------------------------------ fetching and the store


async def test_a_first_refresh_reads_the_history_and_stores_it(hass, rates, transport, store):
    transport.serve_file(HISTORY_URL, HISTORY)
    assert rates.rate_for("SEK", date(2026, 10, 31)) is None
    assert await rates.async_refresh() is True
    assert transport.calls == [HISTORY_URL]
    assert transport.headers[0]["User-Agent"] == USER_AGENT
    assert rates.rate_for("SEK", date(2026, 10, 31)) == (10.978, date(2026, 10, 30))
    assert rates.rate_for("SEK", date(2026, 10, 25)) == (11.131, date(2026, 10, 23))
    assert store.saves == 1
    assert len(store.payload["tables"]) == 65
    assert store.payload["tables"][0] == {
        "date": "2026-10-30",
        "values": parse_history(body(HISTORY))[0].values,
    }
    assert rates.fetched_at == AFTER_PUBLICATION
    assert rates.attempt_error is None


async def test_with_yesterdays_table_held_the_daily_file_is_enough(hass, rates, transport, store, clock):
    # The daily fixture is the table of Friday 2026-09-18; it is the Monday after, after the publication.
    clock.now = datetime(2026, 9, 21, 16, 0, tzinfo=timezone.utc)
    transport.serve_file(DAILY_URL, DAILY)
    store.payload = {"tables": [{"date": "2026-09-17", "values": {"SEK": 11.3, "NOK": 10.8, "DKK": 7.47}}]}
    # Two tables missing (Friday's and Monday's): the history.
    assert await rates.async_refresh() is False  # no history served: 404
    assert transport.calls == [HISTORY_URL]
    store.payload = {"tables": [{"date": "2026-09-18", "values": {"SEK": 11.3, "NOK": 10.8, "DKK": 7.47}}]}
    fresh = FxRates(hass, session=transport, store=store, now=clock)
    assert await fresh.async_refresh() is True
    assert transport.calls[-1] == DAILY_URL
    assert fresh.rate_for("SEK", date(2026, 9, 21)) == (11.2915, date(2026, 9, 18))


async def test_a_restart_converts_from_the_store_without_the_network(hass, rates, transport, store):
    transport.serve_file(HISTORY_URL, HISTORY)
    await rates.async_refresh()
    restarted = FxRates(hass, session=EcbTransport(), store=StoreDouble(store.payload))
    await restarted.async_ensure_restored()
    assert restarted.rate_for("SEK", date(2026, 10, 31)) == (10.978, date(2026, 10, 30))
    assert restarted.rate_for("PLN", date(2026, 10, 25)) == (4.3945, date(2026, 10, 23))
    assert restarted.newest == date(2026, 10, 30)
    assert restarted.fetched_at == AFTER_PUBLICATION


@pytest.mark.parametrize(
    "payload",
    [None, "junk", {"tables": "junk"}, {"tables": [None, {"date": "x", "values": {}}]}],
)
async def test_an_unreadable_store_is_an_empty_one(hass, store, payload):
    store.payload = payload
    rates = FxRates(hass, session=EcbTransport(), store=store)
    await rates.async_ensure_restored()
    assert rates.newest is None


async def test_a_wrong_stored_table_is_dropped_and_the_rest_kept(hass, store):
    store.payload = {
        "tables": [
            {"date": "2026-10-30", "values": {"SEK": "lots"}},
            {"date": "2026-10-29", "values": {"SEK": -1}},
            {"date": "2026-10-28", "values": {"SEK": True}},
            {"date": "2026-10-27", "values": {"SEK": 11.0795}},
        ]
    }
    rates = FxRates(hass, session=EcbTransport(), store=store)
    await rates.async_ensure_restored()
    assert rates.rate_for("SEK", date(2026, 10, 31)) == (11.0795, date(2026, 10, 27))


async def test_the_store_keeps_the_newest_tables_only(hass, rates, store):
    first = date(2026, 1, 1)
    store.payload = {
        "tables": [
            {"date": (first + timedelta(days=offset)).isoformat(), "values": {"SEK": 11.0}}
            for offset in range(MAX_TABLES + 20)
        ]
    }
    await rates.async_ensure_restored()
    assert rates.newest == first + timedelta(days=MAX_TABLES + 19)
    assert rates.rate_for("SEK", first + timedelta(days=19)) is None
    assert rates.rate_for("SEK", first + timedelta(days=20)) is not None


@pytest.mark.parametrize(
    ("serve", "code"),
    [
        (lambda t: None, "http_status"),  # the stub answers 404
        (lambda t: t.serve(DAILY_URL, 503, "busy"), "http_status"),
        (lambda t: t.serve(DAILY_URL, 200, "<html>maintenance</html>"), "not_ecb"),
        (lambda t: t.serve_chunks(DAILY_URL, 200, [b"<a>", b"</a>"], fail_after=1), "network"),
        (lambda t: t.serve_chunks(DAILY_URL, 200, [b"x" * (MAX_RESPONSE_BYTES + 1)]), "too_large"),
    ],
)
async def test_an_unreachable_ecb_keeps_the_held_table(hass, rates, transport, store, serve, code):
    store.payload = {"tables": [{"date": "2026-10-29", "values": {"SEK": 10.9995, "NOK": 10.3315, "DKK": 7.4736}}]}
    serve(transport)
    assert await rates.async_refresh() is False
    assert transport.calls == [DAILY_URL]
    assert rates.attempt_error == code
    assert rates.rate_for("SEK", date(2026, 10, 31)) == (10.9995, date(2026, 10, 29))
    assert store.saves == 0


async def test_concurrent_refreshes_share_one_request(hass, rates, transport):
    import asyncio

    transport.serve_file(HISTORY_URL, HISTORY)
    transport.hold(HISTORY_URL)
    first = asyncio.ensure_future(rates.async_refresh())
    second = asyncio.ensure_future(rates.async_refresh())
    await transport.entered[HISTORY_URL].wait()
    transport.release(HISTORY_URL)
    assert await first is True and await second is True
    assert transport.call_count(HISTORY_URL) == 1


# ------------------------------------------------------------------ the timer


def _berlin(day: str, clock_time: str) -> datetime:
    return datetime.fromisoformat(f"{day}T{clock_time}").replace(tzinfo=ECB_ZONE).astimezone(timezone.utc)


def test_the_publication_calendar():
    # Friday after the publication: Friday's table; the next request is Monday's.
    assert expected_table(_berlin("2026-10-30", "17:00")) == date(2026, 10, 30)
    assert next_publication(_berlin("2026-10-30", "17:00")) == _berlin("2026-11-02", "16:20")
    # Friday morning: Thursday's table is the newest there is.
    assert expected_table(_berlin("2026-10-30", "09:00")) == date(2026, 10, 29)
    assert next_publication(_berlin("2026-10-30", "09:00")) == _berlin("2026-10-30", "16:20")
    # Sunday: Friday's.
    assert expected_table(_berlin("2026-11-01", "12:00")) == date(2026, 10, 30)
    # Summer time: 16:20 in Frankfurt is 14:20 UTC.
    assert next_publication(_berlin("2026-09-21", "09:00")) == datetime(2026, 9, 21, 14, 20, tzinfo=timezone.utc)


async def test_start_behind_asks_soon_then_daily(hass, rates, transport, scheduler, clock):
    transport.serve_file(HISTORY_URL, HISTORY)
    await rates.async_start()
    [timer] = scheduler.pending
    assert timer.when == AFTER_PUBLICATION + timedelta(seconds=90)
    timer.fire()
    await rates._run_task
    assert transport.calls == [HISTORY_URL]
    [timer] = scheduler.pending
    # Monday 2 November 16:20 Frankfurt, plus half of the jitter span.
    assert timer.when == _berlin("2026-11-02", "16:35")


async def test_start_with_the_newest_table_waits_for_the_next_publication(hass, rates, store, scheduler, transport):
    store.payload = {"tables": [{"date": "2026-10-30", "values": {"SEK": 10.978}}]}
    await rates.async_start()
    [timer] = scheduler.pending
    assert timer.when == _berlin("2026-11-02", "16:35")
    assert transport.calls == []


async def test_failures_back_off_and_stop_cancels(hass, rates, transport, scheduler, clock):
    await rates.async_start()
    scheduler.pending[0].fire()
    await rates._run_task
    [timer] = scheduler.pending
    assert timer.when == clock.now + timedelta(seconds=63)  # 60 s, plus 5 % of it
    timer.fire()
    await rates._run_task
    [timer] = scheduler.pending
    assert timer.when == clock.now + timedelta(seconds=315)
    await rates.async_stop()
    assert scheduler.pending == []


async def test_a_table_that_has_not_moved_is_asked_again_hourly_until_the_evening(
    hass, rates, transport, store, scheduler, clock
):
    # Thursday's table held and served again on Friday at 17:00: late, or a holiday.
    thursday = history_xml(("2026-10-29", {"SEK": "10.9995", "NOK": "10.3315", "DKK": "7.4736"}))
    transport.serve_chunks(DAILY_URL, 200, [thursday])
    store.payload = {"tables": [{"date": "2026-10-29", "values": {"SEK": 10.9995}}]}
    await rates.async_start()
    scheduler.pending[0].fire()
    await rates._run_task
    [timer] = scheduler.pending
    assert timer.when == clock.now + timedelta(hours=1)
    clock.now = _berlin("2026-10-30", "21:30")
    timer.fire()
    await rates._run_task
    [timer] = scheduler.pending
    assert timer.when == _berlin("2026-11-02", "16:35")
