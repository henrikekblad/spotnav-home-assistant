"""The hot water's pure parts: the ledger (`heat/dhw_ledger.py`), the boost's route and state (`heat/dhw_boost.py`)
and the day review with the gate to live (`heat/dhw_review.py`)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from custom_components.spotnav.heat.adapters import CTC_BMS, HEISHAMON, MYUPLINK, NIBE_F, NIBE_S
from custom_components.spotnav.heat.dhw_boost import (
    BY_PLAN,
    BY_PUMP,
    BY_USER,
    BoostReading,
    Route,
    boost_route,
    boost_state,
    start_call,
    stop_call,
)
from custom_components.spotnav.heat.dhw_ledger import SOURCE_BUTTON, SOURCE_PLAN, Entry, Ledger
from custom_components.spotnav.heat.dhw_plan import Pick
from custom_components.spotnav.heat.dhw_review import DayPeak, back_off, gate, review_day

ZONE = ZoneInfo("Europe/Stockholm")
T0 = datetime(2026, 11, 20, 4, 0, tzinfo=timezone.utc)


@dataclass(frozen=True)
class Run:
    start: datetime
    end: datetime
    kwh: float


# ---- the ledger


def test_a_boost_without_a_write_is_the_households() -> None:
    ledger = Ledger()
    assert ledger.source_of_on("switch.boost", T0) is None
    ledger.add(Entry("switch.boost", "turn_on", T0, SOURCE_PLAN, read_back=True))
    assert ledger.source_of_on("switch.boost", T0 + timedelta(seconds=20)) == SOURCE_PLAN
    # Long after the write, or another entity: the household's.
    assert ledger.source_of_on("switch.boost", T0 + timedelta(minutes=30)) is None
    assert ledger.source_of_on("switch.other", T0 + timedelta(seconds=20)) is None
    # A write that was not seen is nobody's boost.
    refused = Ledger([Entry("switch.boost", "turn_on", T0, SOURCE_BUTTON, read_back=False)])
    assert refused.source_of_on("switch.boost", T0 + timedelta(seconds=20)) is None


def test_runs_budget_and_store() -> None:
    entry = Entry("select.lux", "One time increase", T0, SOURCE_BUTTON, read_back=True)
    ledger = Ledger([entry, Entry("select.lux", "One time increase", T0 + timedelta(hours=10), SOURCE_PLAN)])
    assert ledger.run_source(T0 + timedelta(minutes=3)) == SOURCE_BUTTON
    assert ledger.run_source(T0 + timedelta(hours=2)) is None
    day = T0.astimezone(ZONE).date()
    assert ledger.used_on(day, ZONE) == 1 and ledger.used_on(day, ZONE, SOURCE_BUTTON) == 1
    again = Ledger.from_stored(ledger.stored())
    assert again.entries == ledger.entries
    assert Ledger.from_stored([{"source": "other"}, "x"]).entries == []
    ledger.trim(T0 + timedelta(days=9))
    assert ledger.entries == []
    updated = Ledger([entry])
    updated.update(entry, read_back=False)
    assert updated.entries[0].read_back is False


# ---- the route and the writes, per brand by its recipe


def _found(adapter, index: int, entity_id: str, options=(), enabled=True):
    capability = [item for item in adapter.capabilities if item.kind == "hot_water_boost"][index]
    status = SimpleNamespace(
        kind="hot_water_boost",
        entity_id=entity_id,
        enabled=enabled,
        available=True,
        volatile=capability.volatile,
        self_ending=capability.self_ending,
        duration_s=None if capability.duration is None else capability.duration.total_seconds(),
        start=capability.start,
        stop=capability.stop,
    )
    return SimpleNamespace(capabilities=(status,), cloud=adapter.cloud), options


NIBE_OPTIONS = ("Off", "3 hrs", "6 hrs", "12 hrs", "One time increase")


def test_nibe_one_time_increase() -> None:
    pump, options = _found(NIBE_F, 0, "select.nibe_temporary_lux_48132", NIBE_OPTIONS)
    route = boost_route(pump, None)
    assert route is not None and route.kind == "one_shot" and route.stoppable
    assert start_call(route, options) == ("select", "select_option", {"entity_id": route.entity_id, "option": "One time increase"})
    assert stop_call(route, options) == ("select", "select_option", {"entity_id": route.entity_id, "option": "Off"})
    # A select without its option is not written.
    assert start_call(route, ("Off",)) is None


def test_myuplink_temporary_lux_on_a_cloud() -> None:
    pump, _ = _found(MYUPLINK, 0, "switch.f730_temporary_lux_50004")
    route = boost_route(pump, None)
    assert route is not None and route.kind == "cloud" and route.cloud
    assert start_call(route) == ("switch", "turn_on", {"entity_id": route.entity_id})
    assert stop_call(route) == ("switch", "turn_off", {"entity_id": route.entity_id})


def test_ctc_extra_hot_water_for_an_hour() -> None:
    pump, _ = _found(CTC_BMS, 0, "number.ctc_extra_hot_water_1006")
    route = boost_route(pump, None)
    assert route is not None and route.kind == "volatile" and route.duration == timedelta(hours=1)
    assert start_call(route) == ("number", "set_value", {"entity_id": route.entity_id, "value": 1.0})
    assert stop_call(route) == ("number", "set_value", {"entity_id": route.entity_id, "value": 0.0})


def test_heishamon_force_dhw() -> None:
    pump, _ = _found(HEISHAMON, 0, "switch.aquarea_force_dhw")
    route = boost_route(pump, None)
    assert route is not None and start_call(route) == ("switch", "turn_on", {"entity_id": route.entity_id})


def test_relay_pulse_and_no_route() -> None:
    route = boost_route(None, "switch.larm_utgang_2")
    assert route == Route("switch.larm_utgang_2", "pulse") and not route.stoppable
    assert start_call(route) == ("switch", "turn_on", {"entity_id": "switch.larm_utgang_2"})
    assert stop_call(route) is None
    assert start_call(Route("button.hot_water", "pulse")) == ("button", "press", {"entity_id": "button.hot_water"})
    # NIBE S's "more hot water" does not end by itself: no route, no button.
    pump, _ = _found(NIBE_S, 0, "number.s1155_more_hot_water_40698")
    assert boost_route(pump, None) is None
    # A disabled boost is no route; the settings' input then is.
    pump, _ = _found(NIBE_F, 0, "select.nibe_temporary_lux_48132", enabled=False)
    assert boost_route(pump, None) is None
    assert boost_route(pump, "switch.larm_utgang_2") is not None
    assert boost_route(None, "sensor.x") is None


# ---- the state shown


RUN = timedelta(minutes=30)


def test_boost_states() -> None:
    timer = Route("number.ctc", "volatile", "1", "0", timedelta(hours=1))
    assert boost_state(None, BoostReading(), T0, RUN) is None
    assert boost_state(timer, BoostReading(), T0, RUN)["state"] == "idle"
    assert boost_state(timer, BoostReading(pending=True), T0, RUN)["state"] == "starting"
    running = boost_state(timer, BoostReading(on=True, on_since=T0 - timedelta(minutes=10)), T0, RUN)
    assert running["state"] == "running" and running["source"] == BY_USER and running["stoppable"]
    assert running["ends"] == (T0 + timedelta(minutes=50)).isoformat() and not running["ends_estimated"]
    own = boost_state(timer, BoostReading(on=True, on_since=T0, ledger_source=SOURCE_PLAN), T0, RUN)
    assert own["source"] == BY_PLAN and not own["stoppable"]
    failed = boost_state(timer, BoostReading(failed_at=T0 - timedelta(minutes=1)), T0, RUN)
    assert failed["state"] == "failed"
    assert boost_state(timer, BoostReading(failed_at=T0 - timedelta(minutes=5)), T0, RUN)["state"] == "idle"
    pump = boost_state(timer, BoostReading(making=True), T0, RUN)
    assert (pump["state"], pump["source"], pump["ends"]) == ("running", BY_PUMP, None)
    one_time = Route("select.lux", "one_shot", "one time", "off")
    estimated = boost_state(one_time, BoostReading(on=True, on_since=T0 - timedelta(minutes=10)), T0, RUN)
    assert estimated["ends"] == (T0 + timedelta(minutes=20)).isoformat() and estimated["ends_estimated"]


def test_pulse_state_follows_the_meter() -> None:
    pulse = Route("switch.larm_utgang_2", "pulse")
    pressed = T0 - timedelta(minutes=3)
    shown = boost_state(pulse, BoostReading(pulse_at=pressed), T0, RUN)
    assert (shown["state"], shown["source"], shown["stoppable"]) == ("running", BY_USER, False)
    # The meter shows the run 20 minutes on; once it ends the press is over.
    assert boost_state(pulse, BoostReading(pulse_at=pressed, making=True), T0 + timedelta(minutes=20), RUN)["source"] == BY_USER
    assert boost_state(pulse, BoostReading(pulse_at=pressed), T0 + timedelta(minutes=25), RUN)["state"] == "idle"


# ---- the review and the gate


def _peaks(day: int = 0) -> list[DayPeak]:
    base = datetime(2026, 11, 20, tzinfo=ZONE) + timedelta(days=day)
    return [
        DayPeak((base + timedelta(hours=6)).astimezone(timezone.utc), (base + timedelta(hours=7)).astimezone(timezone.utc)),
        DayPeak((base + timedelta(hours=17)).astimezone(timezone.utc), (base + timedelta(hours=20)).astimezone(timezone.utc)),
    ]


def _pick(peak: DayPeak, hours_before: float, price: float) -> Pick:
    start = peak.ready - timedelta(hours=hours_before)
    return Pick(start, start + RUN, 1.2, price, "cheap", peak.ready, peak.ready + RUN, 150.0, 50.0)


def test_review_hits_saving_and_what_the_pump_did() -> None:
    peaks = _peaks()
    morning_run = Run(peaks[0].ready + timedelta(minutes=20), peaks[0].ready + timedelta(minutes=45), 1.0)
    standby_run = Run(peaks[1].ready + timedelta(hours=3), peaks[1].ready + timedelta(hours=3, minutes=10), 0.4)
    picks = [_pick(peaks[0], 2, 50.0)]
    review = review_day(
        date(2026, 11, 20), ZONE, peaks, picks, [morning_run, standby_run], lambda _q: 150.0, mode="shadow", cutoff=0.5
    )
    assert (review["hits"], review["peaks"], review["misses"]) == (1, 2, 0)
    assert review["per_peak"][0]["pump_run"] == morning_run.start.isoformat()
    assert review["per_peak"][1] == {
        "peak": "17:00", "hit": False, "pick": None, "pump_run": None, "miss": False, "cold_risk": False
    }
    assert review["moved_kwh"] == 1.0 and review["actual_cost"] == 150.0
    # 1 kWh at 50 plus 1.5 h of standby loss at the pick's price.
    assert review["would_cost"] == round(50.0 + 0.04 * 1.5 * 50.0, 2) and review["saved"] > 0


def test_a_live_reload_after_the_peak_is_a_miss() -> None:
    peaks = _peaks()
    reload = Run(peaks[0].ready + timedelta(minutes=40), peaks[0].ready + timedelta(minutes=60), 0.9)
    review = review_day(
        date(2026, 11, 20), ZONE, peaks, [_pick(peaks[0], 1, 50.0)], [reload], lambda _q: 100.0,
        mode="live", live_peaks=frozenset({peaks[0].ready}),
    )
    assert review["misses"] == 1 and review["per_peak"][0]["miss"] is True


def _review(day: date, hits: int, peaks: int = 2, saved: float = 10.0, mode: str = "shadow", misses: int = 0) -> dict:
    return {"date": day.isoformat(), "mode": mode, "hits": hits, "peaks": peaks, "saved": saved, "misses": misses}


def test_level_b_gate_a_week_each_weekday_ten_peaks_seventy_percent_and_a_saving() -> None:
    first = date(2026, 11, 16)
    week = [_review(first + timedelta(days=index), 2) for index in range(7)]
    shown = gate(week)
    assert shown["open"] and (shown["days"], shown["weekdays"], shown["peaks"], shown["hits"]) == (7, 7, 14, 14)
    assert not gate(week[:6])["open"]
    # Seven days but a weekday twice and one missing.
    assert not gate([*week[:6], _review(first + timedelta(days=7), 2)])["open"]
    # Too few hits, no saving, too few peaks.
    assert not gate([_review(first + timedelta(days=index), 1) for index in range(7)])["open"]
    assert not gate([_review(first + timedelta(days=index), 2, saved=-5.0) for index in range(7)])["open"]
    assert not gate([_review(first + timedelta(days=index), 1, peaks=1) for index in range(7)])["open"]
    # Live days are no shadow days.
    assert not gate([_review(first + timedelta(days=index), 2, mode="live") for index in range(7)])["open"]
    # Level A waits two weeks.
    assert not gate(week, "A")["open"]
    assert gate([_review(first + timedelta(days=index), 2) for index in range(14)], "A")["open"]


def test_three_misses_in_a_week_back_off() -> None:
    today = date(2026, 11, 30)
    reviews = [_review(today - timedelta(days=index), 2, mode="live", misses=1) for index in (1, 3, 5)]
    assert back_off(reviews, today)
    assert not back_off(reviews[:2], today)
    assert not back_off([_review(today - timedelta(days=9), 2, mode="live", misses=3)], today)
