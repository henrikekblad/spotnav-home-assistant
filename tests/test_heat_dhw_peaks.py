"""The hot-water peaks learner (`heat/dhw_peaks.py`)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from custom_components.spotnav.heat.dhw_peaks import (
    DEFAULT_PEAKS,
    DRAW_LEAD,
    Draw,
    Peak,
    draws_from_runs,
    draws_from_state,
    draws_from_tank,
    learn_peaks,
    peak_instant,
)

ZONE = ZoneInfo("Europe/Stockholm")


@dataclass(frozen=True)
class Run:
    start: datetime
    end: datetime
    kwh: float


def at(day: int, hh: int, mm: int) -> datetime:
    return datetime(2026, 11, 1, hh, mm, tzinfo=ZONE) + timedelta(days=day)


def owner_nights(days: int = 21, jitter: bool = True) -> list[Run]:
    """The owner's meter as seen: a morning shower run near 06:10, a second near 08:20 and a small 20:20 standby run."""
    runs = []
    for day in range(days):
        shift = (day % 3 - 1) * 10 if jitter else 0
        for hh, mm, kwh in ((6, 10, 0.85), (8, 20, 0.8), (20, 20, 0.5)):
            start = at(day, hh, mm) + timedelta(minutes=shift)
            runs.append(Run(start, start + timedelta(minutes=20), kwh))
    return runs


def test_standby_runs_are_no_draws_and_draws_lead_the_run() -> None:
    runs = owner_nights(1, jitter=False)
    draws = draws_from_runs(runs)
    assert [draw.at for draw in draws] == [at(0, 6, 10) - DRAW_LEAD, at(0, 8, 20) - DRAW_LEAD]


def test_own_runs_and_those_after_them_are_left_out() -> None:
    runs = owner_nights(1, jitter=False)
    draws = draws_from_runs(runs, own=[at(0, 5, 0)])
    # 06:10 is within two hours after SpotNav's 05:00 start; 08:20 is not.
    assert [draw.at for draw in draws] == [at(0, 8, 20) - DRAW_LEAD]


def test_defaults_before_the_warm_start() -> None:
    draws = draws_from_runs(owner_nights(6))
    assert learn_peaks(draws, ZONE, days_seen=6) == list(DEFAULT_PEAKS)


def test_owner_mornings_learned_evening_default_kept() -> None:
    draws = draws_from_runs(owner_nights())
    peaks = learn_peaks(draws, ZONE, days_seen=21)
    learned = [peak for peak in peaks if peak.learned]
    # 06:10 and 08:20 runs are 2 h 10 min apart: one morning cluster; the 20:20 standby run is no peak.
    assert len(learned) == 1
    morning = learned[0]
    assert time(5, 30) <= morning.ready_by <= time(6, 0)
    assert morning.ready_by.minute % 15 == 0
    assert morning.until > morning.ready_by
    # The morning's draws per day: both runs fall in the cluster (within 90 min of its centre) or the first only.
    assert 0.8 <= morning.kwh <= 1.7
    # The evening default stays: nothing learned there.
    assert [peak for peak in peaks if not peak.learned] == [DEFAULT_PEAKS[1]]


def test_two_peaks_far_apart_are_both_learned() -> None:
    runs = []
    for day in range(14):
        for hh, mm in ((6, 30), (19, 0)):
            start = at(day, hh, mm)
            runs.append(Run(start, start + timedelta(minutes=30), 1.4))
    peaks = learn_peaks(draws_from_runs(runs), ZONE, days_seen=14)
    assert [peak.learned for peak in peaks] == [True, True]
    assert peaks[0].ready_by == time(6, 0)
    assert peaks[1].ready_by == time(18, 30)
    assert peaks[0].kwh == 1.4
    ready, until, run = peak_instant(peaks[0], date(2026, 11, 20), ZONE)
    assert ready < until and ready <= run


def test_a_small_cluster_is_no_peak() -> None:
    runs = [Run(at(day, 7, 0), at(day, 7, 30), 2.0) for day in range(14)]
    runs += [Run(at(day, 13, 0), at(day, 13, 20), 0.7) for day in range(3)]
    peaks = learn_peaks(draws_from_runs(runs), ZONE, days_seen=14)
    learned = [peak.ready_by for peak in peaks if peak.learned]
    assert learned == [time(6, 30)]


def test_hold_keeps_the_last_learned_peak() -> None:
    old = Peak(time(5, 45), time(6, 30), 1.2, True, time(6, 20))
    # Boost days: the morning's runs are SpotNav's own and left out, nothing learned there now.
    peaks = learn_peaks([], ZONE, days_seen=21, previous=[old], hold=True)
    assert old in peaks
    assert learn_peaks([], ZONE, days_seen=21, previous=[old], hold=False) == list(DEFAULT_PEAKS)


def test_stable_day_to_day() -> None:
    runs = owner_nights(28)
    found = set()
    for end in range(21, 28):
        window = [run for run in runs if at(end - 21, 0, 0) <= run.start < at(end, 0, 0)]
        peaks = learn_peaks(draws_from_runs(window), ZONE, days_seen=21)
        found.add(tuple(peak.ready_by for peak in peaks))
    assert len(found) == 1


def test_tank_falls_are_draws_sized_by_the_reload() -> None:
    base = at(0, 6, 0)
    readings = [(base, 52.0), (base + timedelta(minutes=5), 50.0), (base + timedelta(minutes=10), 46.0), (base + timedelta(minutes=40), 45.5)]
    reload = Run(base + timedelta(minutes=30), base + timedelta(minutes=60), 1.3)
    draws = draws_from_tank(readings, lambda _moment: False, [reload])
    assert draws == [Draw(base, 1.3)]
    # While the pump makes hot water a fall is no draw.
    assert draws_from_tank(readings, lambda _moment: True, [reload]) == []


def test_state_runs_sized_by_the_boost_power() -> None:
    start = at(0, 6, 0)
    draws = draws_from_state([(start, start + timedelta(minutes=30)), (start, start + timedelta(minutes=5))], 3.0)
    assert draws == [Draw(start - DRAW_LEAD, 1.5)]


def test_stored_round_trip() -> None:
    peak = DEFAULT_PEAKS[0]
    assert Peak.from_stored(peak.stored()) == peak
    assert Peak.from_stored({"ready_by": "x"}) is None
    assert peak.as_dict() == {"ready_by": "05:00", "kwh": 1.0, "learned": False}
