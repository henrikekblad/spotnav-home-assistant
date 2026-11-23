"""The sun's value on a site without a home battery (`site/sun_value.py`, `site/sun_review.py`, `site/house_period.py`):
the nightly review by the quarter hour from the 5-minute statistics, the hourly estimate for the other days, and the
house history's buckets and `sun` block; a battery site keeps its review's figure alone."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest
from homeassistant.core import HomeAssistant

from custom_components.spotnav.battery.plan_shadow import BUCKET, Bucket, PriceSlot
from custom_components.spotnav.battery.review_baselines import sun_gave
from custom_components.spotnav.site import sun_review
from custom_components.spotnav.site.energy_flow import EnergyHour
from custom_components.spotnav.site.history_period import PERIOD_DAY, PERIOD_MONTH, period_of, utc_bounds
from custom_components.spotnav.site.house_period import house_history
from custom_components.spotnav.site.sun_review import (
    DAYS_KEPT,
    FAIL_NO_DATA,
    FAIL_NO_METERS,
    HOURS_KEPT_DAYS,
    STATUS_BATTERY,
    STATUS_NO_SUN,
    SiteSunReview,
    kept,
    review_day,
)
from custom_components.spotnav.site.sun_value import BASIS_HOUR, BASIS_QUARTER, SunDay, day_values, period_value

from tests.test_house_history import TZ, flat_slots, hours_of, month_inputs, review

DAY = date(2026, 11, 15)
FIVE = timedelta(minutes=5)
QUARTER = timedelta(minutes=15)


def _five_minutes(day: date, figure: Any) -> dict[datetime, float]:
    """Each 5-minute row of the day: `figure(start)` kWh."""
    start, end = utc_bounds(period_of(PERIOD_DAY, day), TZ)
    out = {}
    moment = start
    while moment < end:
        out[moment] = figure(moment)
        moment += FIVE
    return out


def _local_hour(moment: datetime) -> int:
    return (moment + timedelta(hours=1)).hour  # CET in November


def _sun(moment: datetime) -> float:
    return 0.2 if 10 <= _local_hour(moment) < 14 else 0.0


def _import(moment: datetime) -> float:
    # At 11 the house sells in the first quarter and buys in the others.
    if _local_hour(moment) == 11:
        return 0.0 if moment.minute < 15 else 0.15
    return 0.05 if _sun(moment) else 0.1


def _export(moment: datetime) -> float:
    if _local_hour(moment) == 11:
        return 0.3 if moment.minute < 15 else 0.0
    return 0.05 if _sun(moment) else 0.0


def _priced(day: date) -> list[PriceSlot]:
    start, end = utc_bounds(period_of(PERIOD_DAY, day), TZ)
    # Hourly prices (an area without quarter-hour prices): spread over their quarters.
    return [PriceSlot(hour, hour + timedelta(hours=1), 150.0, 60.0) for hour in hours_of(start - timedelta(hours=2), end)]


# ------------------------------------------------------------------------------------------------ the quarter path


def test_a_quarter_day_sums_the_5_minute_rows_and_equals_sun_gave_on_the_same_buckets() -> None:
    grid_in, grid_out, solar = _five_minutes(DAY, _import), _five_minutes(DAY, _export), _five_minutes(DAY, _sun)
    found, reason = review_day(DAY, TZ, grid_in, grid_out, solar, _priced(DAY))
    assert reason is None and found is not None
    assert found.basis == BASIS_QUARTER and found.periods == 96
    # The same quarter hours as the battery review's buckets with no battery: the same function, the same figure.
    start, end = utc_bounds(period_of(PERIOD_DAY, DAY), TZ)
    buckets, sun = [], {}
    moment = start
    while moment < end:
        parts = [moment + index * FIVE for index in range(3)]
        buckets.append(
            Bucket(
                start=moment,
                buy=150.0,
                sell=60.0,
                import_kwh=sum(grid_in[part] for part in parts),
                export_kwh=sum(grid_out[part] for part in parts),
                covered_s=BUCKET.total_seconds(),
            )
        )
        sun[moment] = sum(solar[part] for part in parts)
        moment += QUARTER
    value, quarters = sun_gave(buckets, sun)
    assert found.value == pytest.approx(value) and quarters == 96
    assert sum(found.hours.values()) == pytest.approx(found.value)
    assert len(found.hours) == 4  # the sunny hours alone


def test_an_hour_that_sold_in_one_quarter_and_bought_in_another_differs_by_the_hour() -> None:
    grid_in, grid_out, solar = _five_minutes(DAY, _import), _five_minutes(DAY, _export), _five_minutes(DAY, _sun)
    found, _reason = review_day(DAY, TZ, grid_in, grid_out, solar, _priced(DAY))
    assert found is not None
    eleven = datetime(2026, 11, 15, 10, tzinfo=timezone.utc)  # 11 local
    start = eleven
    end = eleven + timedelta(hours=1)
    hour = EnergyHour(
        start=eleven,
        grid_in=sum(v for k, v in grid_in.items() if start <= k < end),
        grid_out=sum(v for k, v in grid_out.items() if start <= k < end),
        solar=sum(v for k, v in solar.items() if start <= k < end),
    )
    estimate = period_value(hour, 150.0, 60.0)
    assert estimate is not None
    # By the quarter: the first quarter sold 0.9 kWh, so 0.6 kWh of its sun is valued at the sell price (36), the
    # other three replace bought energy (3 x 0.6 x 150 = 270). By the hour the export nets against the import and
    # all 2.4 kWh of sun read as bought energy (360): the estimate overstates this hour.
    assert found.hours[eleven] == pytest.approx(306.0)
    assert estimate == pytest.approx(360.0)
    # A quiet hour (no export) reads the same either way.
    noon = datetime(2026, 11, 15, 11, tzinfo=timezone.utc)
    quiet = EnergyHour(
        start=noon,
        grid_in=sum(v for k, v in grid_in.items() if noon <= k < noon + timedelta(hours=1)),
        grid_out=sum(v for k, v in grid_out.items() if noon <= k < noon + timedelta(hours=1)),
        solar=sum(v for k, v in solar.items() if noon <= k < noon + timedelta(hours=1)),
    )
    assert found.hours[noon] == pytest.approx(period_value(quiet, 150.0, 60.0))


def test_a_day_needs_its_rows_and_its_prices() -> None:
    grid_in, solar = _five_minutes(DAY, _import), _five_minutes(DAY, _sun)
    # No export meter: export counts 0.
    found, _reason = review_day(DAY, TZ, grid_in, None, solar, _priced(DAY))
    assert found is not None and found.periods == 96
    assert review_day(DAY, TZ, {}, None, {}, _priced(DAY)) == (None, FAIL_NO_DATA)
    assert review_day(DAY, TZ, grid_in, None, solar, []) == (None, sun_review.FAIL_NO_PRICES)
    # Half the day's rows gone (the oldest day as the recorder purges it): not kept.
    half = {k: v for k, v in grid_in.items() if k.hour >= 12}
    assert review_day(DAY, TZ, half, None, solar, _priced(DAY)) == (None, sun_review.FAIL_PARTIAL)


def test_a_day_keeps_its_hours_40_days_and_itself_400() -> None:
    today = date(2026, 11, 23)
    hour = datetime(2026, 1, 1, tzinfo=timezone.utc)
    days = {
        today - timedelta(days=age): SunDay(today - timedelta(days=age), 10.0, 96, BASIS_QUARTER, {hour: 10.0})
        for age in (1, HOURS_KEPT_DAYS, HOURS_KEPT_DAYS + 1, DAYS_KEPT, DAYS_KEPT + 1)
    }
    out = kept(days, today)
    assert sorted((today - day).days for day in out) == [1, HOURS_KEPT_DAYS, HOURS_KEPT_DAYS + 1, DAYS_KEPT]
    assert [bool(out[today - timedelta(days=age)].hours) for age in (1, HOURS_KEPT_DAYS, HOURS_KEPT_DAYS + 1)] == [True, True, False]
    # Kept as written, read back the same.
    item = days[today - timedelta(days=1)]
    assert SunDay.from_dict(item.day, item.as_dict()) == item
    assert SunDay.from_dict(item.day, {"value": "x"}) is None


def test_day_values_mark_a_day_read_by_the_hour_as_an_estimate() -> None:
    start, end = utc_bounds(period_of(PERIOD_DAY, DAY), TZ)
    hours = [(EnergyHour(start=h, grid_in=0.1, grid_out=0.2, solar=0.5), 10.0) for h in hours_of(start, end)]
    quarters = [(EnergyHour(start=start + QUARTER * i, grid_in=0.1, grid_out=0.0, solar=0.1, minutes=15), 1.0) for i in range(4)]
    found = day_values([*hours, *quarters], TZ)[DAY]
    assert found.basis == BASIS_HOUR and found.value == pytest.approx(244.0)
    assert day_values(quarters, TZ)[DAY].basis == BASIS_QUARTER
    assert period_value(EnergyHour(start=start, grid_in=1.0, grid_out=0.0), 150.0, 60.0) is None


# ------------------------------------------------------------------------------------------------ the house history


def _no_battery(**extra: Any) -> Any:
    return month_inputs(reviews=None, has_battery=False, arbitrage_days={}, arbitrage_hours={}, flow_days={}, flow_hours={}, savings={}, **extra)


def _stored_day(day: date, value: float, hours: bool = True) -> SunDay:
    start, end = utc_bounds(period_of(PERIOD_DAY, day), TZ)
    per_hour = {h: value / 24 for h in hours_of(start, end)} if hours else {}
    return SunDay(day, value, 96, BASIS_QUARTER, per_hour)


def test_a_no_battery_month_has_the_reviewed_days_and_estimates_the_rest() -> None:
    stored = {date(2026, 11, 14): _stored_day(date(2026, 11, 14), 900.0), date(2026, 11, 15): _stored_day(date(2026, 11, 15), 800.0, hours=False)}
    answer = house_history(_no_battery(sun_stored=stored))
    by_key = {bucket["key"]: bucket for bucket in answer["buckets"]}
    assert by_key["2026-11-14"]["sun_gave"] == pytest.approx(900.0)
    assert by_key["2026-11-15"]["sun_gave"] == pytest.approx(800.0)
    # The 1st to the 13th from the month's hours: an estimate, the same formula on each hour.
    assert by_key["2026-11-03"]["sun_gave"] is not None and by_key["2026-11-03"]["sun_gave"] > 0
    # Today (the 16th) and later: none.
    assert by_key["2026-11-16"]["sun_gave"] is None and by_key["2026-11-20"]["sun_gave"] is None
    sun = answer["sun"]
    assert sun["days"] == 15 and sun["estimated_days"] == 13 and sun["period_days"] == 15
    assert sun["gave"] == pytest.approx(sum(bucket["sun_gave"] or 0.0 for bucket in answer["buckets"]), abs=0.05)
    assert answer["review"] is None


def test_a_no_battery_day_has_hourly_figures_and_the_battery_site_none() -> None:
    day = period_of(PERIOD_DAY, date(2026, 11, 14))
    answer = house_history(_no_battery(period=day, sun_stored={date(2026, 11, 14): _stored_day(date(2026, 11, 14), 960.0)}))
    hourly = [bucket["sun_gave"] for bucket in answer["buckets"]]
    assert all(value == pytest.approx(40.0) for value in hourly)
    assert answer["sun"] == {"gave": 960.0, "days": 1, "period_days": 1, "estimated_days": 0}
    # Without the night's review: the day's own hours (read by the hour here), an estimate.
    estimate = house_history(_no_battery(period=day, sun_stored={}))
    assert estimate["sun"]["estimated_days"] == 1
    assert sum(1 for bucket in estimate["buckets"] if bucket["sun_gave"]) == 4  # the sunny hours
    # A battery site: the review's figure, a whole day's, and no `sun` block (never both for one day).
    battery = house_history(month_inputs(period=day, reviews=[review(date(2026, 11, 14))]))
    assert battery["sun"] is None and all(bucket["sun_gave"] is None for bucket in battery["buckets"])
    assert battery["review"]["sun_gave"] == pytest.approx(700.0)


def test_no_sun_or_no_prices_means_no_sun_value() -> None:
    month = period_of(PERIOD_MONTH, date(2026, 11, 1))
    start, _end = utc_bounds(month, TZ)
    dark = [EnergyHour(start=h, grid_in=1.0, grid_out=0.0) for h in hours_of(start, start + timedelta(days=3))]
    answer = house_history(_no_battery(periods=dark, has_solar=False, sun_stored=None))
    assert answer["sun"] is None and all(bucket["sun_gave"] is None for bucket in answer["buckets"])
    unpriced = house_history(_no_battery(prices=(), sun_stored={}))
    assert unpriced["sun"] is None


# ------------------------------------------------------------------------------------------------ the nightly run


class _Demand:
    def __init__(self, now: datetime) -> None:
        self.moment = now
        self.tracker = SimpleNamespace(tz=TZ)

    def now(self) -> datetime:
        return self.moment


PREFS = {
    "energy_sources": [
        {"type": "grid", "stat_energy_from": "sensor.import", "stat_energy_to": "sensor.export"},
        {"type": "solar", "stat_energy_from": "sensor.solar"},
    ]
}


def _rows(days: list[date]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {"sensor.import": [], "sensor.export": [], "sensor.solar": []}
    for day in days:
        for statistic, figure in (("sensor.import", _import), ("sensor.export", _export), ("sensor.solar", _sun)):
            out[statistic] += [{"start": start, "change": kwh} for start, kwh in _five_minutes(day, figure).items()]
    return out


def _review(hass: HomeAssistant, now: datetime, *, battery: bool = False, entry_id: str | None = None) -> SiteSunReview:
    def prices(period: Any) -> list[PriceSlot]:
        start, end = utc_bounds(period, TZ)
        return flat_slots(start, end)

    return SiteSunReview(hass, entry_id, _Demand(now), has_battery=lambda: battery, prices=prices)


@pytest.fixture
def statistics(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, ...]]:
    reads: list[tuple[Any, ...]] = []

    async def prefs(_hass: Any) -> Any:
        return PREFS

    async def read(_hass: Any, ids: Any, start: datetime, end: datetime, period: str) -> Any:
        reads.append((tuple(ids), start, end, period))
        return _rows([date(2026, 11, day) for day in range(13, 23)])

    monkeypatch.setattr(sun_review, "async_energy_prefs", prefs)
    monkeypatch.setattr(sun_review, "async_statistics", read)
    monkeypatch.setattr(sun_review, "short_term_days", lambda _hass: 10)
    return reads


async def test_the_night_reviews_the_days_within_the_5_minute_statistics_once(
    hass: HomeAssistant, statistics: list[tuple[Any, ...]]
) -> None:
    now = datetime(2026, 11, 22, 23, 30, tzinfo=timezone.utc)  # 00:30 on the 23rd
    sun = _review(hass, now)
    await sun.async_load()
    sun.tick()
    await hass.async_block_till_done()
    # One read of the 5-minute rows over the days in reach (10 days back from 00:30: the 13th is partly purged, so the
    # 14th to the 22nd).
    assert len(statistics) == 1 and statistics[0][3] == "5minute"
    assert set(statistics[0][0]) == {"sensor.import", "sensor.export", "sensor.solar"}
    assert statistics[0][1] == datetime(2026, 11, 13, 23, tzinfo=timezone.utc)
    assert sorted(sun.days) == [date(2026, 11, day) for day in range(14, 23)]
    assert all(item.basis == BASIS_QUARTER for item in sun.days.values())
    assert sun.last_run is not None and sun.last_run["status"] == "ok"
    # Done for the day: nothing read again until the next night.
    sun.tick()
    await hass.async_block_till_done()
    assert len(statistics) == 1
    diagnostics = sun.diagnostics()
    assert diagnostics["days"] == 9 and diagnostics["basis"] == {"quarter": 9} and len(diagnostics["last_days"]) == 7


async def test_before_the_hour_after_midnight_nothing_runs_and_yesterday_short_is_tried_again(
    hass: HomeAssistant, statistics: list[tuple[Any, ...]], monkeypatch: pytest.MonkeyPatch
) -> None:
    sun = _review(hass, datetime(2026, 11, 22, 23, 5, tzinfo=timezone.utc))  # 00:05
    await sun.async_load()
    sun.tick()
    await hass.async_block_till_done()
    assert statistics == []

    async def empty(*_args: Any) -> Any:
        return {}

    monkeypatch.setattr(sun_review, "async_statistics", empty)
    sun._demand.moment = datetime(2026, 11, 22, 23, 30, tzinfo=timezone.utc)
    sun.tick()
    await hass.async_block_till_done()
    assert sun.days == {} and sun.last_run["failed"]["2026-11-22"] == FAIL_NO_DATA
    assert sun.diagnostics()["retry_at"] is not None
    # An older day without its rows is left to the hourly estimate; yesterday is tried again an hour on.
    assert "2026-11-21" in sun.diagnostics()["given_up"]
    monkeypatch.setattr(sun_review, "async_statistics", lambda *args: _async(_rows([date(2026, 11, 22)])))
    sun._demand.moment += timedelta(hours=1, minutes=1)
    sun.tick()
    await hass.async_block_till_done()
    assert list(sun.days) == [date(2026, 11, 22)]


async def _async(value: Any) -> Any:
    return value


async def test_a_battery_site_no_meters_or_no_sun_reviews_nothing(
    hass: HomeAssistant, statistics: list[tuple[Any, ...]], monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime(2026, 11, 22, 23, 30, tzinfo=timezone.utc)
    battery = _review(hass, now, battery=True)
    await battery.async_load()
    await battery.async_review()
    assert battery.status == STATUS_BATTERY and battery.days == {} and statistics == []

    monkeypatch.setattr(sun_review, "async_energy_prefs", lambda _hass: _async({"energy_sources": [{"type": "solar", "stat_energy_from": "sensor.solar"}]}))
    meterless = _review(hass, now)
    await meterless.async_review()
    assert meterless.status == FAIL_NO_METERS and meterless.failures == {FAIL_NO_METERS: 1}

    monkeypatch.setattr(sun_review, "async_energy_prefs", lambda _hass: _async({"energy_sources": [PREFS["energy_sources"][0]]}))
    sunless = _review(hass, now)
    await sunless.async_review()
    assert sunless.status == STATUS_NO_SUN and statistics == []


async def test_the_reviewed_days_survive_a_restart(hass: HomeAssistant, statistics: list[tuple[Any, ...]]) -> None:
    now = datetime(2026, 11, 22, 23, 30, tzinfo=timezone.utc)
    sun = _review(hass, now, entry_id="site_sun")
    await sun.async_load()
    await sun.async_review()
    await sun.async_close()
    again = _review(hass, now, entry_id="site_sun")
    await again.async_load()
    assert {day: item.as_dict() for day, item in again.days.items()} == {day: item.as_dict() for day, item in sun.days.items()}
    assert again.last_run == sun.last_run
    await again.async_close()
    await SiteSunReview.async_remove_stored(hass, "site_sun")
