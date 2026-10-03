"""The session lifecycle: start, stop, pause, unplug, restart; energy sources; costing as it accrues."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta

import pytest
from homeassistant.core import HomeAssistant

from custom_components.spotnav.sessions.model import (
    SOURCE_ESTIMATED,
    SOURCE_INTEGRATED,
    SOURCE_REGISTER,
    STARTED_HYBRID,
    STARTED_MANUAL,
    STARTED_OTHER,
    STARTED_PLAN_WINDOW,
    STARTED_SOLAR,
)
from custom_components.spotnav.sessions.recorder import (
    END_DEBOUNCE_S,
    PriceBook,
    SessionFacts,
    SessionRecorder,
    started_by,
)
from custom_components.spotnav.sessions.store import MAX_PER_CHARGER, RETENTION_DAYS, SessionStore

from .sessions_helpers import day_intervals, session, STOCKHOLM, UTC

CHARGER = "entry_a"
DAY = date(2026, 9, 22)
MIDNIGHT = datetime(2026, 9, 22, tzinfo=STOCKHOLM).astimezone(UTC)


def at(hour: int, minute: int = 0, second: int = 0) -> datetime:
    return MIDNIGHT + timedelta(hours=hour, minutes=minute, seconds=second)


class World:
    """What the recorder reads, set by the test, with the recorder wired to it."""

    def __init__(self, hass: HomeAssistant, store: SessionStore, *, prices: list[float] | None = None) -> None:
        self.facts = SessionFacts(
            charging=False, connected=True, register_kwh=100.0, register_integrated=False,
            estimate_kw=None, strategy="cheapest", vehicle_id="car", vehicle_name="Car",
            solar_share=None,
        )
        self.cause: str | None = None
        self.clock = MIDNIGHT
        self.book: PriceBook | None = PriceBook(
            tuple(day_intervals(DAY, prices if prices is not None else [100.0] * 24)), "SEK", "kr", "öre"
        )
        self.store = store
        self.recorder = self.build(hass)

    def build(self, hass: HomeAssistant) -> SessionRecorder:
        return SessionRecorder(
            hass, CHARGER, self.store,
            facts=lambda: self.facts, prices=lambda now: self.book,
            consume_cause=self._consume, now=lambda: self.clock,
        )

    def _consume(self) -> str | None:
        cause, self.cause = self.cause, None
        return cause

    def set(self, **changes) -> None:
        self.facts = replace(self.facts, **changes)

    def at(self, moment: datetime, **changes) -> None:
        self.clock = moment
        if changes:
            self.set(**changes)
        self.recorder.evaluate(moment)


@pytest.fixture(autouse=True)
def stop_recorders(monkeypatch: pytest.MonkeyPatch):
    """Every recorder a test starts is shut down at its end, so no interval timer outlives the test."""
    started: list[SessionRecorder] = []
    original = SessionRecorder.async_start

    def tracked(self: SessionRecorder) -> None:
        started.append(self)
        original(self)

    monkeypatch.setattr(SessionRecorder, "async_start", tracked)
    yield
    for recorder in started:
        recorder.async_shutdown()


@pytest.fixture
async def store(hass: HomeAssistant) -> SessionStore:
    sessions = SessionStore(hass)
    await sessions.async_load()
    return sessions


async def test_a_charge_from_start_to_stop_is_one_session_with_energy_and_cost(
    hass: HomeAssistant, store: SessionStore
) -> None:
    world = World(hass, store, prices=[100.0] * 8 + [40.0] * 4 + [100.0] * 12)
    world.cause = "manual"

    world.at(at(8), charging=True)
    world.at(at(9), register_kwh=103.0)
    world.at(at(10), register_kwh=106.0)
    world.at(at(10, 5), charging=False, register_kwh=106.2)
    assert store.closed(CHARGER) == (), "a stop is not the end until it has lasted"
    world.at(at(10, 5) + timedelta(seconds=END_DEBOUNCE_S))

    (done,) = store.closed(CHARGER)
    assert done.start == at(8) and done.end == at(10, 5)
    assert done.energy_kwh == pytest.approx(6.2)
    assert done.energy_source == SOURCE_REGISTER
    assert done.started_by == STARTED_MANUAL
    assert done.vehicle_name == "Car" and done.strategy == "cheapest"
    assert done.currency == "SEK" and done.minor_unit == "öre"
    assert done.cost_minor == pytest.approx(6.2 * 40.0)
    assert done.average_price_minor_per_kwh == pytest.approx(40.0)
    assert store.open_session(CHARGER) is None and world.recorder.session is None


async def test_a_price_change_inside_a_session_prices_each_part_where_it_was_delivered(
    hass: HomeAssistant, store: SessionStore
) -> None:
    world = World(hass, store, prices=[100.0] * 8 + [40.0] + [100.0] * 15)

    world.at(at(8, 30), charging=True)
    world.at(at(9, 30), register_kwh=106.0)  # one sample straddles the 09:00 boundary
    world.at(at(9, 30), charging=False)
    world.at(at(9, 30) + timedelta(seconds=END_DEBOUNCE_S))

    (done,) = store.closed(CHARGER)
    assert done.cost_minor == pytest.approx(3.0 * 40.0 + 3.0 * 100.0)
    assert done.reference_cost_minor == pytest.approx(6.0 * (40.0 + 23 * 100.0) / 24)


async def test_fifteen_minute_data_is_priced_the_same_way(hass: HomeAssistant, store: SessionStore) -> None:
    world = World(hass, store)
    world.book = PriceBook(
        tuple(day_intervals(DAY, [10.0 * (i // 4) for i in range(96)], minutes=15)), "SEK", "kr", "öre"
    )

    world.at(at(1, 45), charging=True)
    world.at(at(2, 15), register_kwh=102.0)
    world.at(at(2, 15), charging=False)
    world.at(at(2, 15) + timedelta(seconds=END_DEBOUNCE_S))

    (done,) = store.closed(CHARGER)
    # 15 minutes at 10 (01:45-02:00), then 15 minutes at 20 (02:00-02:15), one kWh each.
    assert done.cost_minor == pytest.approx(10.0 + 20.0)


async def test_a_short_pause_is_the_same_session(hass: HomeAssistant, store: SessionStore) -> None:
    world = World(hass, store)
    world.at(at(8), charging=True)
    world.at(at(8, 30), register_kwh=103.0)
    world.at(at(8, 30, 1), charging=False)
    world.at(at(8, 31), charging=True, register_kwh=103.1)
    world.at(at(9), register_kwh=106.0)
    world.at(at(9, 1), charging=False)
    world.at(at(9, 1) + timedelta(seconds=END_DEBOUNCE_S))

    (done,) = store.closed(CHARGER)
    assert done.start == at(8) and done.energy_kwh == pytest.approx(6.0)


async def test_unplugging_ends_the_session_at_once(hass: HomeAssistant, store: SessionStore) -> None:
    world = World(hass, store)
    world.at(at(8), charging=True)
    world.at(at(8, 30), register_kwh=103.0)

    world.at(at(8, 40), charging=False, connected=False, register_kwh=103.5)

    (done,) = store.closed(CHARGER)
    assert done.end == at(8, 40) and done.energy_kwh == pytest.approx(3.5)


async def test_a_charge_nothing_took_is_not_a_session(hass: HomeAssistant, store: SessionStore) -> None:
    world = World(hass, store)
    world.at(at(8), charging=True)
    world.at(at(8, 10), charging=False)
    world.at(at(8, 10) + timedelta(seconds=END_DEBOUNCE_S))

    assert store.closed(CHARGER) == () and store.open_session(CHARGER) is None


async def test_a_register_that_resets_counts_the_energy_since_the_reset(
    hass: HomeAssistant, store: SessionStore
) -> None:
    world = World(hass, store)
    world.at(at(8), charging=True, register_kwh=500.0)
    world.at(at(8, 30), register_kwh=502.0)
    world.at(at(9), register_kwh=0.3)  # the charger's counter started over
    world.at(at(9, 30), register_kwh=1.0)
    world.at(at(9, 30), charging=False, connected=False)

    (done,) = store.closed(CHARGER)
    assert done.energy_kwh == pytest.approx(2.0 + 0.3 + 0.7)


async def test_a_register_that_is_unreadable_for_a_while_delivers_the_gap_when_it_returns(
    hass: HomeAssistant, store: SessionStore
) -> None:
    world = World(hass, store)
    world.at(at(8), charging=True)
    world.at(at(8, 30), register_kwh=None)
    world.at(at(9), register_kwh=104.0)
    world.at(at(9), charging=False, connected=False)

    (done,) = store.closed(CHARGER)
    assert done.energy_kwh == pytest.approx(4.0)


async def test_without_a_register_the_energy_is_estimated_from_the_current_and_says_so(
    hass: HomeAssistant, store: SessionStore
) -> None:
    world = World(hass, store)
    world.set(register_kwh=None, estimate_kw=7.0)
    world.at(at(8), charging=True)
    world.at(at(9))
    world.at(at(10))
    world.at(at(10), charging=False, connected=False)

    (done,) = store.closed(CHARGER)
    assert done.energy_source == SOURCE_ESTIMATED and done.estimated
    assert done.energy_kwh == pytest.approx(7.0 * 2)
    assert done.cost_minor == pytest.approx(14.0 * 100.0)
    assert done.public()["estimated"] is True


async def test_an_estimate_with_no_known_current_records_no_energy_rather_than_guessing(
    hass: HomeAssistant, store: SessionStore
) -> None:
    world = World(hass, store)
    world.set(register_kwh=None, estimate_kw=None)
    world.at(at(8), charging=True)
    world.at(at(9))
    world.at(at(9, 30), charging=False, connected=False)

    (done,) = store.closed(CHARGER)
    assert done.energy_kwh == 0 and done.cost_minor is None


async def test_an_integrated_plug_register_is_named_as_such(hass: HomeAssistant, store: SessionStore) -> None:
    world = World(hass, store)
    world.set(register_integrated=True)
    world.at(at(8), charging=True)
    world.at(at(9), register_kwh=102.0)
    world.at(at(9), charging=False, connected=False)

    assert store.closed(CHARGER)[0].energy_source == SOURCE_INTEGRATED


async def test_energy_with_no_prices_is_recorded_with_no_cost(hass: HomeAssistant, store: SessionStore) -> None:
    world = World(hass, store)
    world.book = None
    world.at(at(8), charging=True)
    world.at(at(9), register_kwh=105.0)
    world.at(at(9), charging=False, connected=False)

    (done,) = store.closed(CHARGER)
    assert done.energy_kwh == pytest.approx(5.0)
    assert done.cost_minor is None and done.currency is None and done.average_price_minor_per_kwh is None
    assert done.public()["cost"] is None


async def test_the_solar_share_is_kept_where_the_executor_knew_it(
    hass: HomeAssistant, store: SessionStore
) -> None:
    world = World(hass, store)
    world.at(at(11), charging=True, solar_share=1.0)
    world.at(at(12), register_kwh=104.0, solar_share=1.0)
    world.at(at(13), register_kwh=106.0, solar_share=0.5)
    world.at(at(14), register_kwh=108.0, solar_share=None)  # unknown: not counted either way
    world.at(at(14), charging=False, connected=False)

    (done,) = store.closed(CHARGER)
    assert done.energy_kwh == pytest.approx(8.0)
    assert done.solar_share == pytest.approx((4.0 + 1.0) / 6.0)


@pytest.mark.parametrize(
    ("hint", "strategy", "expected"),
    [
        ("manual", "hybrid", STARTED_MANUAL),
        ("plan_window", "cheapest", STARTED_PLAN_WINDOW),
        ("plan_window", "hybrid", STARTED_HYBRID),
        ("solar", "solar", STARTED_SOLAR),
        ("solar", "hybrid", STARTED_HYBRID),
        (None, "cheapest", STARTED_OTHER),
        ("other", "cheapest", STARTED_OTHER),
    ],
)
def test_how_a_session_started_follows_who_asked_and_the_strategy(
    hint: str | None, strategy: str, expected: str
) -> None:
    assert started_by(hint, strategy) == expected


async def test_a_charge_nobody_asked_for_started_by_other(hass: HomeAssistant, store: SessionStore) -> None:
    world = World(hass, store)
    world.at(at(8), charging=True)

    assert world.recorder.session is not None and world.recorder.session.started_by == STARTED_OTHER


# ------------------------------------------------------------------------------ restart


async def test_a_session_open_across_a_restart_is_resumed_when_the_charger_is_still_charging(
    hass: HomeAssistant, store: SessionStore
) -> None:
    world = World(hass, store)
    world.at(at(8), charging=True)
    world.at(at(8, 30), register_kwh=103.0)
    world.recorder.async_shutdown()
    await store.async_flush()

    reloaded = SessionStore(hass)
    await reloaded.async_load()
    world.store = reloaded
    resumed = world.build(hass)
    world.recorder = resumed
    resumed.async_start()
    assert resumed.session is not None and resumed.session.start == at(8)
    assert resumed.session.energy_kwh == pytest.approx(3.0)

    world.at(at(9, 30), register_kwh=108.0)
    world.at(at(9, 30), charging=False, connected=False)

    (done,) = reloaded.closed(CHARGER)
    assert done.start == at(8) and done.energy_kwh == pytest.approx(8.0)
    assert done.cost_minor == pytest.approx(8.0 * 100.0)


async def test_a_session_that_ended_while_down_is_closed_at_the_last_time_it_was_seen(
    hass: HomeAssistant, store: SessionStore
) -> None:
    world = World(hass, store)
    world.at(at(8), charging=True)
    world.at(at(8, 30), register_kwh=103.0)
    world.recorder.async_shutdown()

    resumed = world.build(hass)
    world.recorder = resumed
    world.clock = at(9)
    world.set(charging=False)
    resumed.async_start()  # the first look finds it not charging
    assert resumed.session is not None, "not closed on the first look: the states may not be back yet"
    world.at(at(11))  # still no energy
    world.at(at(11) + timedelta(seconds=END_DEBOUNCE_S))

    (done,) = store.closed(CHARGER)
    assert done.end == at(8, 30) and done.energy_kwh == pytest.approx(3.0)


async def test_energy_delivered_while_down_is_counted_and_ends_the_session_when_seen(
    hass: HomeAssistant, store: SessionStore
) -> None:
    world = World(hass, store)
    world.at(at(8), charging=True)
    world.at(at(8, 30), register_kwh=103.0)
    world.recorder.async_shutdown()

    resumed = world.build(hass)
    world.recorder = resumed
    world.clock = at(9)
    resumed.async_start()
    world.at(at(10), register_kwh=109.0, charging=False, connected=False)

    (done,) = store.closed(CHARGER)
    assert done.energy_kwh == pytest.approx(9.0)
    assert done.end == at(10)


async def test_the_stored_record_round_trips_through_storage(hass: HomeAssistant, store: SessionStore) -> None:
    store.close(session(at(8)), at(10))
    store.set_open(session(at(11)).__class__.from_dict({**session(at(11)).as_dict(), "end": None}), charger_id=CHARGER)
    await store.async_flush()

    reloaded = SessionStore(hass)
    await reloaded.async_load()

    assert [item.as_dict() for item in reloaded.closed(CHARGER)] == [session(at(8)).as_dict()]
    assert reloaded.open_session(CHARGER) is not None and reloaded.open_session(CHARGER).end is None


# ------------------------------------------------------------------------------ bounds


async def test_sessions_older_than_two_years_are_dropped_and_the_count_is_bounded(
    hass: HomeAssistant, store: SessionStore
) -> None:
    now = at(12)
    store.close(session(now - timedelta(days=RETENTION_DAYS + 5)), now)
    store.close(session(now - timedelta(days=RETENTION_DAYS - 5)), now)
    assert len(store.closed(CHARGER)) == 1

    for index in range(MAX_PER_CHARGER + 10):
        store.close(session(now - timedelta(minutes=index + 1), hours=0.01), now)
    assert len(store.closed(CHARGER)) == MAX_PER_CHARGER


async def test_unreadable_stored_records_are_ignored_not_fatal(
    hass: HomeAssistant, hass_storage: dict
) -> None:
    good = session(at(8)).as_dict()
    hass_storage["spotnav_sessions"] = {
        "version": 1,
        "minor_version": 1,
        "key": "spotnav_sessions",
        "data": {
            "sessions": {CHARGER: [good, {"id": "broken"}, "nonsense", {**good, "energy_kwh": "NaN"}]},
            "open": {CHARGER: {"nope": 1}},
        },
    }
    loaded = SessionStore(hass)
    await loaded.async_load()

    assert [item.id for item in loaded.closed(CHARGER)] == [good["id"]]
    assert loaded.open_session(CHARGER) is None


async def test_removing_a_charger_removes_its_sessions(hass: HomeAssistant, store: SessionStore) -> None:
    store.close(session(at(8)), at(10))
    store.close(session(at(8), charger_id="other"), at(10))

    await store.async_remove_charger(CHARGER)

    assert store.closed(CHARGER) == () and len(store.closed("other")) == 1
