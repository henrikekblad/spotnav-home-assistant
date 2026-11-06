"""A site's hours from Home Assistant's statistics (`site/hour_statistics.py`), the suggested import target, the
back-fill of the power view and the month's peak line: pure, with a fake recorder.

Stockholm's clock: CEST (UTC+2) until 2026-10-25 03:00, then CET (UTC+1); that night 02:00-03:00 runs twice.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from custom_components.spotnav.planning.demand_charge import (
    component_month,
    free_level_hours,
    free_levels_by_hour,
    HourMean,
)
from custom_components.spotnav.site.demand_tracker import (
    _hour_from_row,
    BACKFILL_DONE,
    BACKFILL_NO_STATISTICS,
    hour_row,
    SiteDemand,
)
from custom_components.spotnav.site.hour_statistics import (
    backfill_hours,
    ChargerSeries,
    KIND_CURRENT,
    KIND_ENERGY,
    KIND_POWER,
    MIN_DAYS,
    read_complete_hours,
    read_site_hours,
    REASON_NO_CHARGERS,
    REASON_TOO_FEW_DAYS,
    round_up,
    SiteSeries,
    StatHour,
    StatMeta,
    suggest_target,
    Term,
)
from custom_components.spotnav.site.import_target import effective_target
from custom_components.spotnav.site.power_history import power_month, PowerArchive

from .test_demand_charge import cet, PILOT, TZ

UTC = timezone.utc
GRID = "sensor.grid_power"
EXPORT = "sensor.grid_export"
CAR = "sensor.charger_energy"
CAR_POWER = "sensor.charger_power"
MEAN_W = StatMeta(unit="W", has_mean=True, has_sum=False)
MEAN_KW = StatMeta(unit="kW", has_mean=True, has_sum=False)
SUM_KWH = StatMeta(unit="kWh", has_mean=False, has_sum=True)
MIN_KW = 6 * 3 * 230 / 1000  # 4.14 kW: 6 A on three phases


def cest(year: int, month: int, day: int, hour: int) -> datetime:
    return datetime(year, month, day, hour, tzinfo=UTC) - timedelta(hours=2)


class FakeRecorder:
    """The recorder's metadata, hourly and 5-minute rows, as `statistics_during_period` answers them (`start` in
    seconds)."""

    def __init__(
        self,
        meta: Mapping[str, StatMeta],
        rows: Mapping[str, list[dict[str, Any]]],
        short: Mapping[str, list[dict[str, Any]]] | None = None,
    ) -> None:
        self.meta = dict(meta)
        self.rows_by_id = rows
        self.short_by_id = short or {}
        self.asked: list[tuple[set[str], set[str]]] = []
        self.asked_short: list[tuple[datetime, datetime]] = []

    async def metadata(self, entity_ids: set[str]) -> dict[str, StatMeta]:
        return {entity_id: self.meta[entity_id] for entity_id in entity_ids if entity_id in self.meta}

    async def rows(
        self, entity_ids: set[str], start: datetime, end: datetime, types: set[str]
    ) -> Mapping[str, Sequence[Mapping[str, Any]]]:
        self.asked.append((set(entity_ids), set(types)))
        return {
            entity_id: [
                row for row in self.rows_by_id.get(entity_id, []) if start.timestamp() <= row["start"] < end.timestamp()
            ]
            for entity_id in entity_ids
        }

    async def short_rows(
        self, entity_ids: set[str], start: datetime, end: datetime
    ) -> Mapping[str, Sequence[Mapping[str, Any]]]:
        self.asked_short.append((start, end))
        return {
            entity_id: [
                row for row in self.short_by_id.get(entity_id, []) if start.timestamp() <= row["start"] < end.timestamp()
            ]
            for entity_id in entity_ids
        }


def rows(starts: Sequence[datetime], value: Callable[[datetime], float | None], key: str = "mean") -> list[dict[str, Any]]:
    return [{"start": start.timestamp(), key: value(start)} for start in starts if value(start) is not None]


def hours_between(first: datetime, count: int) -> list[datetime]:
    return [first + timedelta(hours=index) for index in range(count)]


def series(*, chargers: tuple[ChargerSeries, ...] = (), grid: tuple[Term, ...] = (Term(GRID),)) -> SiteSeries:
    return SiteSeries(grid=grid, chargers=chargers)


ENERGY_CHARGER = ChargerSeries("charger_a", (Term(CAR, KIND_ENERGY),), KIND_ENERGY, MIN_KW)


# ----------------------------------------------------------------------------------------------- the read


async def test_grid_in_kw_less_a_charger_register_in_kwh_gives_the_house_and_the_units_are_converted() -> None:
    starts = hours_between(cest(2026, 10, 1, 0), 3)
    recorder = FakeRecorder(
        {GRID: MEAN_KW, CAR: SUM_KWH},
        {GRID: rows(starts, lambda start: 6.0), CAR: rows(starts, lambda start: 4.5, "change")},
    )
    read = await read_site_hours(recorder, series(chargers=(ENERGY_CHARGER,)), starts[0], starts[-1] + timedelta(hours=1))
    assert [(hour.grid_kw, hour.car_kw, hour.house_kw) for hour in read.hours] == [(6.0, 4.5, 1.5)] * 3
    assert read.grid_readable and read.chargers_without_statistics == ()
    # The mean of the power entity, the change of the register.
    assert recorder.asked == [({GRID, CAR}, {"mean", "change"})]


async def test_a_pair_in_watts_is_import_less_export_and_an_inverted_meter_is_negated() -> None:
    starts = hours_between(cest(2026, 10, 1, 12), 2)
    recorder = FakeRecorder(
        {GRID: MEAN_W, EXPORT: MEAN_W},
        {GRID: rows(starts, lambda start: 800.0), EXPORT: rows(starts, lambda start: 2000.0)},
    )
    pair = (Term(GRID, KIND_POWER), Term(EXPORT, KIND_POWER, -1.0))
    read = await read_site_hours(recorder, series(grid=pair), starts[0], starts[-1] + timedelta(hours=1))
    # Exported more than imported within the hour: the house's import is none, the grid's mean negative.
    assert [(hour.grid_kw, hour.house_kw) for hour in read.hours] == [(-1.2, 0.0), (-1.2, 0.0)]
    # A site without chargers knows its chargers draw nothing.
    assert all(hour.car_kw == 0.0 for hour in read.hours)


async def test_a_charger_without_statistics_is_named_and_the_grid_alone_stands_for_the_house() -> None:
    starts = hours_between(cest(2026, 10, 1, 0), 2)
    current = ChargerSeries("charger_b", (Term("sensor.l1", KIND_CURRENT),), KIND_CURRENT, MIN_KW)
    recorder = FakeRecorder({GRID: MEAN_W}, {GRID: rows(starts, lambda start: 3000.0)})
    read = await read_site_hours(recorder, series(chargers=(current,)), starts[0], starts[-1] + timedelta(hours=1))
    assert read.chargers_without_statistics == ("charger_b",)
    assert [(hour.car_kw, hour.house_kw, hour.known_house_kw) for hour in read.hours] == [(None, 3.0, None)] * 2


async def test_a_current_in_milliamps_is_turned_into_power_on_the_nominal_voltage() -> None:
    starts = hours_between(cest(2026, 10, 1, 0), 1)
    phases = tuple(Term(f"sensor.l{index}", KIND_CURRENT) for index in (1, 2, 3))
    charger = ChargerSeries("charger_b", phases, KIND_CURRENT, MIN_KW)
    meta = {GRID: MEAN_W, **{term.entity_id: StatMeta("mA", True, False) for term in phases}}
    recorder = FakeRecorder(
        meta, {GRID: rows(starts, lambda start: 9000.0), **{term.entity_id: rows(starts, lambda start: 10_000.0) for term in phases}}
    )
    read = await read_site_hours(recorder, series(chargers=(charger,)), starts[0], starts[0] + timedelta(hours=1))
    (hour,) = read.hours
    assert hour.car_kw == pytest.approx(6.9) and hour.house_kw == pytest.approx(2.1)


async def test_gaps_only_hours_every_grid_entity_has_count_and_a_charger_gap_leaves_the_house_to_the_grid() -> None:
    starts = hours_between(cest(2026, 10, 1, 0), 4)
    recorder = FakeRecorder(
        {GRID: MEAN_W, EXPORT: MEAN_W, CAR_POWER: MEAN_W},
        {
            GRID: rows(starts, lambda start: None if start == starts[1] else 5000.0),
            EXPORT: rows(starts, lambda start: None if start == starts[2] else 0.0),
            CAR_POWER: rows(starts, lambda start: None if start == starts[3] else 4000.0),
        },
    )
    charger = ChargerSeries("charger_a", (Term(CAR_POWER),), KIND_POWER, MIN_KW)
    pair = (Term(GRID), Term(EXPORT, KIND_POWER, -1.0))
    read = await read_site_hours(recorder, series(grid=pair, chargers=(charger,)), starts[0], starts[-1] + timedelta(hours=1))
    assert [(hour.start, hour.car_kw, hour.house_kw) for hour in read.hours] == [
        (starts[0], 4.0, 1.0),
        (starts[3], None, 5.0),
    ]


async def test_a_grid_entity_without_long_term_statistics_or_in_an_unknown_unit_reads_nothing() -> None:
    starts = hours_between(cest(2026, 10, 1, 0), 2)
    for meta in ({}, {GRID: StatMeta("W", has_mean=False, has_sum=True)}, {GRID: StatMeta("hp", True, False)}):
        recorder = FakeRecorder(meta, {GRID: rows(starts, lambda start: 1000.0)})
        read = await read_site_hours(recorder, series(), starts[0], starts[-1] + timedelta(hours=1))
        assert not read.grid_readable and read.hours == () and read.grid_missing == (GRID,)
        assert recorder.asked == []
    read = await read_site_hours(FakeRecorder({}, {}), series(grid=()), starts[0], starts[-1])
    assert not read.grid_readable


async def test_the_repeated_autumn_hour_is_two_hours() -> None:
    # 2026-10-25: 00:00 CEST is 22:00 UTC the day before; the day has 25 hours.
    first = datetime(2026, 10, 24, 22, tzinfo=UTC)
    starts = hours_between(first, 25)
    recorder = FakeRecorder({GRID: MEAN_W}, {GRID: rows(starts, lambda start: 1000.0 + start.hour * 10)})
    read = await read_site_hours(recorder, series(), starts[0], starts[-1] + timedelta(hours=1))
    assert len(read.hours) == 25
    found = suggest_target(read.hours, tz=TZ, components=(), car_min_kw=MIN_KW)
    assert found.reason == REASON_TOO_FEW_DAYS and found.days == 1 and found.hours == 25


# ---------------------------------------------------------------------------------------------- the suggestion


def house_days(days: int, peak: Callable[[int], float], *, base: float = 0.6, first: datetime | None = None) -> list[StatHour]:
    """`days` days of a house: `base` kW every hour, its daily peak at 19:00 (local, CEST)."""
    start = first or cest(2026, 8, 1, 0)
    hours = []
    for day in range(days):
        for local_hour in range(24):
            moment = start + timedelta(days=day, hours=local_hour)
            house = peak(day) if local_hour == 19 else base
            hours.append(StatHour(start=moment, grid_kw=house, car_kw=0.0))
    return hours


def test_the_suggestion_is_the_typical_daily_peak_plus_the_cars_minimum_rounded_up_not_the_highest() -> None:
    # Daily peaks 2.0-2.8 kW, and one sauna evening at 7.5 kW.
    peaks = {day: 2.0 + 0.1 * (day % 9) for day in range(30)}
    peaks[12] = 7.5
    found = suggest_target(house_days(30, peaks.__getitem__), tz=TZ, components=(), car_min_kw=MIN_KW)
    assert found.reason is None and found.counted == "all" and found.days == 30
    assert found.typical_kw == pytest.approx(2.4)
    assert (found.highest_kw, found.highest_start) == (7.5, cest(2026, 8, 13, 19))
    # 2.4 + 4.14 = 6.54, up to the next half kW.
    assert found.suggestion_kw == 7.0
    # The car in a typical evening or night hour: the suggestion less the house's median there (0.6 kW).
    assert found.car_kw == pytest.approx(6.4)
    (month,) = found.months
    assert (month.month, month.peak_kw, month.typical_kw, month.days) == ((2026, 8), 7.5, pytest.approx(2.4), 30)


def test_the_median_of_an_even_count_and_the_rounding() -> None:
    found = suggest_target(house_days(8, lambda day: 2.0 if day < 4 else 3.0), tz=TZ, components=(), car_min_kw=1.5)
    assert found.typical_kw == 2.5 and found.suggestion_kw == 4.0  # 4.0 is on the step: it stays
    assert round_up(4.01) == 4.5 and round_up(0.0) == 0.0 and round_up(6.54) == 7.0


def test_the_car_gets_at_least_its_minimum_in_the_estimate() -> None:
    found = suggest_target(house_days(10, lambda day: 2.0, base=3.0), tz=TZ, components=(), car_min_kw=MIN_KW)
    # The house's typical peak is its base here (3.0): 7.14 -> 7.5, less 3.0 is 4.5, above the minimum.
    assert found.suggestion_kw == 7.5 and found.car_kw == pytest.approx(4.5)
    busy = [StatHour(hour.start, 5.0 if hour.start.hour in (16, 17, 18, 19, 20) else 0.5, 0.0) for hour in house_days(10, lambda day: 0.5)]
    found = suggest_target(busy, tz=TZ, components=(), car_min_kw=MIN_KW)
    assert found.car_kw >= MIN_KW


def test_with_every_part_counting_its_own_hours_the_daily_peaks_are_taken_within_them() -> None:
    from custom_components.spotnav.planning.auto_settings import DemandComponent

    from .test_demand_charge import DAY

    day_only = (DemandComponent(top_n=1, window=DAY),)
    # The house peaks at 05:00 (outside 06-23) at 5 kW every day and at 19:00 at 2 kW.
    hours = [
        StatHour(hour.start, 5.0 if (hour.start + timedelta(hours=2)).hour == 5 else hour.grid_kw, 0.0)
        for hour in house_days(10, lambda day: 2.0)
    ]
    within = suggest_target(hours, tz=TZ, components=day_only, car_min_kw=MIN_KW)
    assert within.counted == "windows" and within.typical_kw == 2.0 and within.highest_kw == 2.0
    everywhere = suggest_target(hours, tz=TZ, components=(), car_min_kw=MIN_KW)
    assert everywhere.counted == "all" and everywhere.typical_kw == 5.0
    # The pilot's day and night parts together count every hour.
    both = suggest_target(hours, tz=TZ, components=PILOT, car_min_kw=MIN_KW)
    assert both.counted == "windows" and both.typical_kw == 5.0


def test_never_invented_too_few_days_or_no_charger() -> None:
    few = suggest_target(house_days(MIN_DAYS - 1, lambda day: 2.0), tz=TZ, components=(), car_min_kw=MIN_KW)
    assert few.reason == REASON_TOO_FEW_DAYS and few.suggestion_kw is None and few.days == MIN_DAYS - 1
    none = suggest_target(house_days(20, lambda day: 2.0), tz=TZ, components=(), car_min_kw=None)
    assert none.reason == REASON_NO_CHARGERS and none.suggestion_kw is None
    empty = suggest_target([], tz=TZ, components=(), car_min_kw=MIN_KW)
    assert empty.reason == REASON_TOO_FEW_DAYS and empty.days == 0


def test_the_cars_minimum_is_the_smallest_member_chargers() -> None:
    small = ChargerSeries("b", (), None, 6 * 1 * 230 / 1000)
    assert series(chargers=(ENERGY_CHARGER, small)).car_min_kw == pytest.approx(1.38)
    assert series().car_min_kw is None


# ------------------------------------------------------------------------------------------------- back-fill


class KeptStore:
    """A store that is only told when to write."""

    def __init__(self) -> None:
        self.writes = 0

    def changed(self) -> None:
        self.writes += 1


def demand_with_measured(first_measured: datetime, kw: float = 3.0, count: int = 3) -> SiteDemand:
    demand = SiteDemand(None, None, TZ, now=lambda: first_measured + timedelta(hours=count, minutes=10))
    demand._days_store = KeptStore()  # type: ignore[assignment]  # noqa: SLF001 - a site that keeps its days
    demand._loaded = True  # noqa: SLF001
    tracker = demand.tracker
    for index in range(count):
        tracker.hours.append(HourMean(start=first_measured + timedelta(hours=index), kw=kw, house_kw=1.0, floor_kw=kw))
    tracker.month = (first_measured + timedelta(hours=2)).year, (first_measured + timedelta(hours=2)).month
    return demand


def stat_hours(first: datetime, count: int, grid_kw: float, car_kw: float | None = 0.0) -> list[StatHour]:
    return [StatHour(first + timedelta(hours=index), grid_kw, car_kw) for index in range(count)]


def test_imported_hours_are_marked_and_never_incomplete() -> None:
    (hour,) = backfill_hours(stat_hours(cet(2026, 11, 1, 0), 1, -0.5, None))
    assert (hour.kw, hour.floor_kw, hour.house_kw, hour.imported, hour.incomplete) == (0.0, 0.0, None, True, False)


def test_the_back_fill_files_only_before_the_first_measured_hour_and_older_months_as_days() -> None:
    first_measured = cet(2026, 11, 3, 10)
    demand = demand_with_measured(first_measured)
    # Statistics from 1 August to past the first measured hours, at 9 kW: none may overwrite a measured hour.
    imported = backfill_hours(stat_hours(datetime(2026, 7, 31, 22, tzinfo=UTC), 95 * 24, 9.0, 1.0))
    filed = demand.apply_backfill(imported, PILOT, demand.now())
    tracker = demand.tracker
    measured = [hour for hour in tracker.hours if not hour.imported]
    assert [hour.kw for hour in measured] == [3.0, 3.0, 3.0]
    assert all(hour.start < first_measured for hour in tracker.hours if hour.imported)
    assert [hour.start for hour in tracker.hours] == sorted(hour.start for hour in tracker.hours)
    # November before the 3rd 10:00 as hours, October as the month before's hours, August and September as days.
    assert sum(hour.imported for hour in tracker.hours) == 2 * 24 + 10
    assert tracker.previous_month == (2026, 10) and len(tracker.previous) == 31 * 24 + 1
    assert set(demand.archive.months) == {(2026, 8), (2026, 9), (2026, 10), (2026, 11)}
    assert all(day.imported for day in demand.archive.months[(2026, 9)].days)
    assert filed == 2 * 24 + 10 + 31 * 24 + 1 + 30 * 24 + 31 * 24
    november = power_month(
        (2026, 11),
        tracker_months=demand.tracker_months(),
        archive=demand.archive,
        components=PILOT,
        tz=TZ,
        now=demand.now(),
        import_target_kw=6.0,
    )
    assert [day["imported"] for day in november["days"]] == [True, True, True]
    assert november["hours"][0]["imported"] and not november["hours"][-1]["imported"]
    # The imported 9 kW hours never lift the target: no peak line anywhere.
    assert all(hour["month_peak_kw"] is None for hour in november["hours"])


def test_imported_hours_never_lift_the_import_target_but_count_in_the_months_peaks() -> None:
    demand = demand_with_measured(cet(2026, 11, 3, 10))
    demand.apply_backfill(backfill_hours(stat_hours(cet(2026, 11, 1, 0), 24, 9.0)), PILOT, demand.now())
    hours = demand.tracker.month_hours(demand.now())
    month = component_month(PILOT[0], hours, TZ, demand.now())
    assert month.bar_kw == 9.0  # billed approximately: shown
    free = [component_month(component, free_level_hours(hours), TZ, demand.now()).free_kw for component in PILOT]
    assert effective_target(6.0, free).kw == 6.0
    assert all(level is None or level <= 3.0 for level in free_levels_by_hour(PILOT, hours, TZ).values())


async def test_the_back_fill_runs_once_and_a_site_without_statistics_says_so() -> None:
    first_measured = cet(2026, 11, 3, 10)
    demand = demand_with_measured(first_measured)
    assert demand.backfill_due()
    starts = hours_between(cet(2026, 11, 1, 0), 48)
    recorder = FakeRecorder({GRID: MEAN_W}, {GRID: rows(starts, lambda start: 2000.0)})
    status = await demand.async_backfill(recorder, series(), PILOT)
    assert status == BACKFILL_DONE and demand.archive.backfill is not None
    assert demand.archive.backfill["hours"] == 48 and not demand.backfill_due()
    assert not demand.backfill_wanted_again(demand.now() + timedelta(days=3))
    # Kept across a restart with the days.
    stored = demand.archive.as_dict()
    again = SiteDemand(None, None, TZ)
    again.archive.restore(stored)
    assert again.archive.backfill == demand.archive.backfill
    # A site whose grid has no statistics: marked, and asked again by the view only a day later.
    bare = demand_with_measured(first_measured)
    assert await bare.async_backfill(FakeRecorder({}, {}), series(), PILOT) == BACKFILL_NO_STATISTICS
    assert not bare.backfill_due() and not bare.backfill_wanted_again(bare.now() + timedelta(hours=2))
    assert bare.backfill_wanted_again(bare.now() + timedelta(days=1, hours=1))


def five_minutes(hour: datetime, count: int = 12) -> list[datetime]:
    """The first `count` 5-minute starts of the hour starting at `hour`."""
    return [hour + timedelta(minutes=5 * index) for index in range(count)]


async def test_twelve_five_minute_rows_for_every_grid_entity_make_an_hour_complete_at_their_mean() -> None:
    complete, short_by_one, half_missing = cet(2026, 11, 2, 17), cet(2026, 11, 2, 18), cet(2026, 11, 2, 19)
    import_rows = [
        *rows(five_minutes(complete), lambda start: 7000.0 if start.minute < 30 else 6000.0),
        *rows(five_minutes(short_by_one, 11), lambda start: 9000.0),
        *rows(five_minutes(half_missing), lambda start: 5000.0),
    ]
    export_rows = [
        *rows(five_minutes(complete), lambda start: 100.0),
        *rows(five_minutes(short_by_one), lambda start: 0.0),
        *rows(five_minutes(half_missing, 6), lambda start: 0.0),
    ]
    recorder = FakeRecorder({GRID: MEAN_W, EXPORT: MEAN_W}, {}, {GRID: import_rows, EXPORT: export_rows})
    pair = series(grid=(Term(GRID), Term(EXPORT, KIND_POWER, -1.0)))
    found = await read_complete_hours(recorder, pair, cet(2026, 11, 1, 0), cet(2026, 11, 3, 0))
    assert found == {complete: pytest.approx(6.4)}
    # A grid entity without statistics: nothing is complete.
    assert await read_complete_hours(FakeRecorder({GRID: MEAN_W}, {}, {GRID: import_rows}), pair, complete, short_by_one) == {}


def backfill_recorder(first: datetime, count: int, hourly_kw: Callable[[datetime], float], short: dict[datetime, tuple[int, float]]) -> FakeRecorder:
    """Hourly statistics of the grid, and for the hours in `short` that many 5-minute rows at that kW."""
    starts = hours_between(first, count)
    five = [row for hour, (count_rows, kw) in short.items() for row in rows(five_minutes(hour, count_rows), lambda start, kw=kw: kw * 1000)]
    return FakeRecorder({GRID: MEAN_W}, {GRID: rows(starts, lambda start: hourly_kw(start) * 1000)}, {GRID: five})


async def test_a_complete_hour_of_the_running_month_lifts_the_import_target_and_eleven_rows_do_not() -> None:
    # The owner-like pilot: installed on the 3rd at 10:00, a 6.0 kW target. On the 2nd at 17:00 the house drew
    # 6.4 kW, every 5-minute row there; at 19:00 a 9 kW hourly mean with one 5-minute row missing.
    first_measured = cet(2026, 11, 3, 10)
    demand = demand_with_measured(first_measured)
    peak, short_hour = cet(2026, 11, 2, 17), cet(2026, 11, 2, 19)
    hourly = {peak: 6.5, short_hour: 9.0}
    recorder = backfill_recorder(
        cet(2026, 11, 1, 0), 58, lambda start: hourly.get(start, 2.0), {peak: (12, 6.4), short_hour: (11, 9.0)}
    )
    assert await demand.async_backfill(recorder, series(), PILOT) == BACKFILL_DONE
    # Only the running month's 5-minute statistics are asked for, up to the running hour.
    assert recorder.asked_short == [(cet(2026, 11, 1, 0), cet(2026, 11, 3, 13))]
    hours = {hour.start: hour for hour in demand.tracker.month_hours(demand.now())}
    assert hours[peak].complete and hours[peak].kw == hours[peak].floor_kw == pytest.approx(6.4)
    assert not hours[short_hour].complete and hours[short_hour].kw == 9.0
    free = [
        component_month(component, free_level_hours(hours.values()), TZ, demand.now()).free_kw for component in PILOT
    ]
    found = effective_target(6.0, free)
    # Lifted to the complete hour's 6.4 kW, not to the 9 kW hour, and held under it less its margin once.
    assert found.lifted and found.kw == pytest.approx(6.4)
    assert found.hold_kw == pytest.approx(6.4 - max(0.1, 0.02 * 6.4)) == pytest.approx(6.272)
    # The power view's line of the month's peak starts after it.
    levels = free_levels_by_hour(PILOT, list(hours.values()), TZ)
    assert levels[cet(2026, 11, 2, 18)] == pytest.approx(6.4)


async def test_without_five_minute_statistics_every_imported_hour_stays_out_of_the_free_level() -> None:
    demand = demand_with_measured(cet(2026, 11, 3, 10))
    recorder = backfill_recorder(cet(2026, 11, 1, 0), 58, lambda start: 6.4, {})
    assert await demand.async_backfill(recorder, series(), PILOT) == BACKFILL_DONE
    hours = demand.tracker.month_hours(demand.now())
    assert not any(hour.complete for hour in hours)
    free = [component_month(component, free_level_hours(hours), TZ, demand.now()).free_kw for component in PILOT]
    assert effective_target(6.0, free).kw == 6.0


def test_a_complete_hour_of_the_month_before_never_counts() -> None:
    demand = demand_with_measured(cet(2026, 11, 3, 10))
    october = cet(2026, 10, 31, 17)
    imported = backfill_hours(stat_hours(october, 1, 8.0), {october: 8.0})
    assert imported[0].complete
    assert demand.apply_backfill(imported, PILOT, demand.now()) == 1
    (kept,) = demand.tracker.previous
    assert kept.imported and not kept.complete
    assert free_level_hours(demand.tracker.previous) == []


def _old_row_length_rule(raw: Any) -> bool:
    """2.4.5's `_hour_from_row` length rule: a row of any other length was dropped."""
    return isinstance(raw, list) and len(raw) in (4, 6, 7, 8)


def _store_with_complete_hour() -> SiteDemand:
    demand = demand_with_measured(cet(2026, 11, 3, 10))
    peak = cet(2026, 11, 2, 17)
    demand.apply_backfill(
        backfill_hours(stat_hours(cet(2026, 11, 2, 0), 34, 2.0), {peak: 6.4}), PILOT, demand.now()
    )
    return demand


def test_the_complete_flag_is_kept_beside_the_rows_and_survives_a_round_trip() -> None:
    demand = _store_with_complete_hour()
    peak = cet(2026, 11, 2, 17)
    stored = demand.tracker.month_dict()
    assert stored["complete"] == [peak.isoformat()]
    assert all(len(row) in (7, 8) for row in (*stored["hours"], *stored["previous"]))
    again = SiteDemand(None, None, TZ)
    assert again.tracker.restore(stored, None) is False
    assert again.tracker.hours == demand.tracker.hours
    assert [hour.start for hour in again.tracker.hours if hour.complete] == [peak]
    # A row without its start listed is not complete; a measured hour listed never is.
    stored["complete"] = [cet(2026, 11, 3, 10).isoformat()]
    bare = SiteDemand(None, None, TZ)
    bare.tracker.restore(stored, None)
    assert not any(hour.complete for hour in bare.tracker.hours)


def test_a_release_before_2_5_reads_every_row_of_the_new_document() -> None:
    stored = _store_with_complete_hour().tracker.month_dict()
    rows_kept = [*stored["hours"], *stored["previous"]]
    assert rows_kept and all(_old_row_length_rule(row) for row in rows_kept)
    # A downgrade writes the rows back without `complete`: the hours stay, only not complete.
    downgraded = {key: value for key, value in stored.items() if key != "complete"}
    again = SiteDemand(None, None, TZ)
    again.tracker.restore(downgraded, None)
    assert len(again.tracker.hours) == len(stored["hours"]) and not any(hour.complete for hour in again.tracker.hours)


def test_a_ninth_element_from_a_development_build_is_read_as_complete_and_written_back_as_eight() -> None:
    demand = _store_with_complete_hour()
    peak = cet(2026, 11, 2, 17)
    stored = demand.tracker.month_dict()
    del stored["complete"]
    stored["hours"] = [[*row, 1] if row[0] == peak.isoformat() else row for row in stored["hours"]]
    again = SiteDemand(None, None, TZ)
    assert again.tracker.restore(stored, None) is True
    assert [hour.start for hour in again.tracker.hours if hour.complete] == [peak]
    written = again.tracker.month_dict()
    assert written["complete"] == [peak.isoformat()] and all(len(row) <= 8 for row in written["hours"])
    # A malformed ninth element drops the row.
    eight = hour_row(HourMean(start=peak, kw=6.4, floor_kw=6.4, imported=True))
    assert _hour_from_row([*eight, 0]) is None and _hour_from_row([*eight, True]) is None
    read = _hour_from_row(eight)
    assert read is not None and read.imported and not read.complete


async def test_a_migrated_store_is_written_again_at_load() -> None:
    demand = _store_with_complete_hour()
    stored = demand.tracker.month_dict()
    stored["hours"] = [[*row, 1] if len(row) == 8 else row for row in stored["hours"]]

    class LoadedStore(KeptStore):
        def __init__(self, data: Any) -> None:
            super().__init__()
            self.data = data

        async def async_load(self) -> Any:
            return self.data

    again = SiteDemand(None, None, TZ)
    month_store, running_store = LoadedStore(stored), LoadedStore(None)
    again._month_store = month_store  # type: ignore[assignment]  # noqa: SLF001
    again._running_store = running_store  # type: ignore[assignment]  # noqa: SLF001
    await again.async_load()
    assert month_store.writes == 1


def test_a_site_that_never_measured_is_not_back_filled() -> None:
    demand = SiteDemand(None, None, TZ)
    demand._days_store = KeptStore()  # type: ignore[assignment]  # noqa: SLF001
    assert not demand.backfill_due()
    assert demand.apply_backfill(backfill_hours(stat_hours(cet(2026, 11, 1, 0), 3, 1.0)), PILOT, cet(2026, 11, 3, 0)) == 0


# --------------------------------------------------------------------------------------- the month's peak line


def test_each_hours_free_level_is_what_the_running_hour_would_have_read() -> None:
    hours = [
        HourMean(start=cet(2026, 11, 1, 0) + timedelta(hours=index), kw=1.0 + (index * 7 % 11) * 0.5, floor_kw=None)
        for index in range(24 * 4)
    ]
    levels = free_levels_by_hour(PILOT, hours, TZ)
    for index, hour in enumerate(hours):
        expected = [component_month(component, hours[:index], TZ, hour.start).free_kw for component in PILOT]
        counted = [level for level in expected if level is not None]
        assert levels[hour.start] == (min(counted) if counted else None)


def test_the_power_view_draws_the_months_peak_where_it_lifted_the_target() -> None:
    # A 7.5 kW day hour on the 2nd; from then on the day part's free level (7.5) lifts a 6 kW target to 7.35 in the
    # day part's hours; the night part's own peak (3 kW) does not lift it.
    hours = [HourMean(start=cet(2026, 11, 1, 0) + timedelta(hours=index), kw=3.0, floor_kw=3.0) for index in range(72)]
    hours[24 + 18] = HourMean(start=cet(2026, 11, 2, 18), kw=7.5, floor_kw=7.5)
    hours[48 + 12] = HourMean(start=cet(2026, 11, 3, 12), kw=4.0, floor_kw=4.0)
    view = power_month(
        (2026, 11),
        tracker_months={(2026, 11): hours},
        archive=PowerArchive(),
        components=PILOT,
        tz=TZ,
        now=cet(2026, 11, 4, 0),
        import_target_kw=6.0,
    )
    by_start = {hour["start"]: hour["month_peak_kw"] for hour in view["hours"]}
    assert by_start["2026-11-02T18:00:00+01:00"] is None  # the hour that set it
    assert by_start["2026-11-02T19:00:00+01:00"] == 7.5
    assert by_start["2026-11-03T02:00:00+01:00"] is None  # night: the night part counts it
    assert by_start["2026-11-03T12:00:00+01:00"] == 7.5
    days = {day["date"]: day["month_peak_kw"] for day in view["days"]}
    assert days == {"2026-11-01": None, "2026-11-02": None, "2026-11-03": 7.5}
