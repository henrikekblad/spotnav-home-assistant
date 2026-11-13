"""The house load forecast (`site/load_forecast.py`), pure: synthetic weeks of hour rows on Stockholm's clock."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone
import math
import random

import pytest

from custom_components.spotnav.planning.demand_charge import HourMean
from custom_components.spotnav.site.load_forecast import (
    BALANCE_C,
    forecast_house_load,
    HORIZON_H,
    LoadForecast,
    QUANTILE,
    SOURCE_FLAT,
    SOURCE_HOUR,
    SOURCE_NONE,
    SOURCE_PROFILE,
    TEMPERATURE_EXPLAINS_TOO_LITTLE,
    TEMPERATURE_TOO_FEW,
    weighted_quantile,
)

TZ = "Europe/Stockholm"
UTC = timezone.utc


def local(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    """A wall-clock time in Stockholm as UTC (CET, outside the DST changes the tests pick on purpose)."""
    from zoneinfo import ZoneInfo  # noqa: PLC0415

    return datetime(year, month, day, hour, minute, tzinfo=ZoneInfo(TZ)).astimezone(UTC)


def history(
    start: datetime,
    end: datetime,
    load: Callable[[datetime], float | None],
    outdoor: Callable[[datetime], float | None] = lambda _moment: None,
) -> list[HourMean]:
    """One row per UTC hour from `start` to `end`, the house's load and outdoor temperature by `load`/`outdoor`."""
    rows: list[HourMean] = []
    moment = start
    while moment < end:
        kw = load(moment)
        rows.append(HourMean(start=moment, kw=kw or 0.0, house_kw=kw, outdoor_c=outdoor(moment)))
        moment += timedelta(hours=1)
    return rows


def local_hour(moment: datetime) -> int:
    from zoneinfo import ZoneInfo  # noqa: PLC0415

    return moment.astimezone(ZoneInfo(TZ)).hour


def is_weekend(moment: datetime) -> bool:
    from zoneinfo import ZoneInfo  # noqa: PLC0415

    return moment.astimezone(ZoneInfo(TZ)).weekday() >= 5


def hour_of(forecast: LoadForecast, moment: datetime):
    return next(hour for hour in forecast.hours if hour.start == moment)


NOW = local(2026, 11, 16, 0) + timedelta(minutes=5)  # Monday 00:05


# ---------------------------------------------------------------------------------------------- the profile


def test_the_profile_is_per_day_type_and_local_hour() -> None:
    def load(moment: datetime) -> float:
        hour = local_hour(moment)
        if is_weekend(moment):
            return 3.0 if hour == 12 else 1.0
        return 4.0 if hour == 18 else 0.8

    rows = history(NOW - timedelta(days=28), NOW, load)
    forecast = forecast_house_load(rows, NOW, TZ)
    assert len(forecast.hours) == HORIZON_H
    assert forecast.hours[0].start == local(2026, 11, 16, 0)
    assert hour_of(forecast, local(2026, 11, 16, 18)).kw == pytest.approx(4.0)
    assert hour_of(forecast, local(2026, 11, 16, 12)).kw == pytest.approx(0.8)
    assert hour_of(forecast, local(2026, 11, 16, 3)).source == SOURCE_PROFILE
    # A Saturday forecast from the same history.
    saturday = local(2026, 11, 21, 0) + timedelta(minutes=5)
    weekend = forecast_house_load(rows, saturday, TZ)
    assert hour_of(weekend, local(2026, 11, 21, 12)).kw == pytest.approx(3.0)
    assert hour_of(weekend, local(2026, 11, 21, 18)).kw == pytest.approx(1.0)


def test_the_profile_takes_the_weighted_upper_quantile_not_the_mean() -> None:
    # Every other evening draws 4 kW, the others 1 kW: the mean is 2.5 kW, the upper quantile counts on the 4 kW.
    def load(moment: datetime) -> float:
        day = moment.astimezone(UTC).toordinal()
        return 4.0 if local_hour(moment) == 18 and day % 2 == 0 else 1.0

    rows = history(NOW - timedelta(days=28), NOW, load)
    figure = hour_of(forecast_house_load(rows, NOW, TZ, inday=False), local(2026, 11, 16, 18)).kw
    assert figure == pytest.approx(4.0)
    assert weighted_quantile([1.0, 1.0, 1.0, 4.0], [1, 1, 1, 1], QUANTILE) == 1.0
    assert weighted_quantile([1.0, 1.0, 4.0, 4.0], [1, 1, 1, 1], QUANTILE) == 4.0
    assert weighted_quantile([1.0, 4.0], [3.0, 1.0], QUANTILE) == 1.0
    assert weighted_quantile([1.0, 4.0], [2.9, 1.0], QUANTILE) == 4.0


def test_a_changed_habit_shows_within_two_weeks() -> None:
    def since(days: float, before: float, after: float) -> list[HourMean]:
        switch = NOW - timedelta(days=days)
        return history(NOW - timedelta(days=42), NOW, lambda moment: after if moment >= switch else before)

    def figures(rows: list[HourMean]) -> set[float]:
        return {round(hour.kw, 3) for hour in forecast_house_load(rows, NOW, TZ, inday=False).hours}  # type: ignore[arg-type]

    # A higher load shows once it holds more than a quarter of the weight: within a week, not after three days.
    assert figures(since(3, 1.0, 3.0)) == {1.0}
    assert figures(since(7, 1.0, 3.0)) == {3.0}
    # A lower one shows once it holds three quarters: the upper quantile lets go of a high habit more slowly, but the
    # half-life still drops it within the six weeks (after about three and a half weeks).
    assert figures(since(14, 3.0, 1.0)) == {3.0}
    assert figures(since(22, 3.0, 1.0)) == {3.0}
    assert figures(since(25, 3.0, 1.0)) == {1.0}


def test_hours_older_than_six_weeks_and_imported_or_unread_hours_are_not_counted() -> None:
    rows = history(NOW - timedelta(days=60), NOW - timedelta(days=43), lambda _moment: 9.0)
    rows += [
        HourMean(start=row.start, kw=5.0, house_kw=5.0, imported=True)
        for row in history(NOW - timedelta(days=20), NOW - timedelta(days=10), lambda _m: 5.0)
    ]
    rows += history(NOW - timedelta(days=10), NOW, lambda moment: None if local_hour(moment) == 18 else 1.0)
    forecast = forecast_house_load(rows, NOW, TZ, flat_kw=2.0)
    assert forecast.samples == 10 * 24 - 10
    assert hour_of(forecast, local(2026, 11, 16, 3)).kw == pytest.approx(1.0)
    # 18:00 was never read: it is not 0 kW, it is the flat figure.
    evening = hour_of(forecast, local(2026, 11, 16, 18))
    assert (evening.kw, evening.source) == (2.0, SOURCE_FLAT)


# ---------------------------------------------------------------------------------------------- the ladder


def test_the_fallback_ladder_at_each_rung() -> None:
    # Weekday hours only, for two weeks: a Saturday's cell is empty, the same hour over every day type is there.
    rows = [row for row in history(NOW - timedelta(days=14), NOW, lambda _moment: 1.5) if not is_weekend(row.start)]
    saturday = local(2026, 11, 21, 0) + timedelta(minutes=5)
    forecast = forecast_house_load(rows, saturday, TZ, flat_kw=0.7)
    assert {hour.source for hour in forecast.hours if is_weekend(hour.start)} == {SOURCE_HOUR}
    assert all(hour.kw == pytest.approx(1.5) for hour in forecast.hours)
    # Two hours only: too few for any cell or hour, the flat figure.
    few = history(NOW - timedelta(hours=2), NOW, lambda _moment: 1.5)
    flat = forecast_house_load(few, NOW, TZ, flat_kw=0.7)
    assert {(hour.source, hour.kw) for hour in flat.hours} == {(SOURCE_FLAT, 0.7)}
    # Nothing at all: no figure, a plan uses the car's own power.
    none = forecast_house_load([], NOW, TZ)
    assert {(hour.source, hour.kw) for hour in none.hours} == {(SOURCE_NONE, None)}
    assert none.by_hour() == {}


def test_a_cell_with_two_samples_falls_to_the_hour_over_every_day_type() -> None:
    weekday = history(NOW - timedelta(days=14), NOW, lambda _moment: 1.0)
    rows = [row for row in weekday if not is_weekend(row.start)]
    # Two Saturday samples at 12:00 of 5 kW: too few for the weekend cell.
    rows += [HourMean(start=local(2026, 11, 7, 12), kw=5, house_kw=5), HourMean(start=local(2026, 11, 14, 12), kw=5, house_kw=5)]
    saturday = local(2026, 11, 21, 0) + timedelta(minutes=5)
    noon = hour_of(forecast_house_load(rows, saturday, TZ), local(2026, 11, 21, 12))
    assert noon.source == SOURCE_HOUR
    assert noon.kw == pytest.approx(1.0)


# ---------------------------------------------------------------------------------------------- holidays and DST


def test_a_holiday_counts_as_weekend_only_when_the_dates_are_given() -> None:
    rows = history(NOW - timedelta(days=28), NOW, lambda moment: 3.0 if is_weekend(moment) else 1.0)
    christmas_eve = date(2026, 11, 17)  # a Tuesday standing in for a holiday
    plain = forecast_house_load(rows, NOW, TZ)
    with_holidays = forecast_house_load(rows, NOW, TZ, holidays={christmas_eve})
    tuesday_noon = local(2026, 11, 17, 12)
    assert hour_of(plain, tuesday_noon).kw == pytest.approx(1.0)
    assert hour_of(with_holidays, tuesday_noon).kw == pytest.approx(3.0)
    assert plain.as_dict()["day_types"] == "weekday_weekend"
    assert with_holidays.as_dict()["day_types"] == "weekday_weekend_holiday"


def test_dst_days_key_on_the_local_hour() -> None:
    from zoneinfo import ZoneInfo  # noqa: PLC0415

    zone = ZoneInfo(TZ)
    # Every local 07:00 draws 4 kW, everything else 1 kW; history across the autumn change (25 October 2026).
    rows = history(
        datetime(2026, 10, 10, tzinfo=UTC),
        datetime(2026, 10, 27, tzinfo=UTC),
        lambda moment: 4.0 if moment.astimezone(zone).hour == 7 else 1.0,
    )
    now = datetime(2026, 10, 27, 0, 5, tzinfo=UTC)
    forecast = forecast_house_load(rows, now, TZ)
    sevens = [hour for hour in forecast.hours if hour.kw == pytest.approx(4.0)]
    assert [hour.start.astimezone(zone).hour for hour in sevens] == [7, 7]
    # The spring change: a 23-hour day forecast hour by hour on UTC, each at its own local hour.
    spring = datetime(2027, 3, 27, 23, 30, tzinfo=UTC)  # 00:30 CET on 28 March
    spring_rows = history(spring - timedelta(days=14), spring, lambda moment: 4.0 if moment.astimezone(zone).hour == 7 else 1.0)
    spring_forecast = forecast_house_load(spring_rows, spring, TZ)
    local_hours = [hour.start.astimezone(zone).hour for hour in spring_forecast.hours[:24]]
    assert 2 not in local_hours[:3]
    assert [hour.start.astimezone(zone).hour for hour in spring_forecast.hours if hour.kw == pytest.approx(4.0)] == [7, 7]


# ---------------------------------------------------------------------------------------------- temperature


def winter_world(seed: int, b: float, noise: float) -> tuple[Callable[[datetime], float], Callable[[datetime], float]]:
    """A house drawing 0.8 kW at night, 2.5 kW at 17-19, plus `b` kW per °C below 15 °C, with gaussian noise; a
    temperature that wanders between about -8 and +12 °C over days."""
    rng = random.Random(seed)
    temps: dict[datetime, float] = {}
    loads: dict[datetime, float] = {}

    def outdoor(moment: datetime) -> float:
        if moment not in temps:
            days = moment.timestamp() / 86400.0
            temps[moment] = 2.0 + 8.0 * math.sin(days / 3.1) + 3.0 * math.sin(days * 2 * math.pi) + rng.gauss(0, 0.5)
        return temps[moment]

    def load(moment: datetime) -> float:
        if moment not in loads:
            hour = local_hour(moment)
            base = 2.5 if 17 <= hour < 20 else 0.8 if hour < 7 or hour >= 22 else 1.2
            loads[moment] = max(0.1, base + b * max(0.0, BALANCE_C - outdoor(moment)) + rng.gauss(0, noise))
        return loads[moment]

    return load, outdoor


def test_the_temperature_term_is_fitted_and_raises_a_colder_hour() -> None:
    load, outdoor = winter_world(1, b=0.1, noise=0.1)
    rows = history(NOW - timedelta(days=42), NOW, load, outdoor)
    cold = {hour.start: -10.0 for hour in forecast_house_load(rows, NOW, TZ).hours}
    warm = {start: 10.0 for start in cold}
    cold_forecast = forecast_house_load(rows, NOW, TZ, temperatures=cold)
    warm_forecast = forecast_house_load(rows, NOW, TZ, temperatures=warm)
    assert cold_forecast.temperature.active
    assert cold_forecast.temperature.b_kw_per_c == pytest.approx(0.1, abs=0.02)
    night = local(2026, 11, 16, 3)
    gap = hour_of(cold_forecast, night).kw - hour_of(warm_forecast, night).kw
    assert gap == pytest.approx(2.0, abs=0.4)
    assert hour_of(cold_forecast, night).temperature_kw > 0
    # An hour without a forecast temperature gets no term.
    partial = forecast_house_load(rows, NOW, TZ, temperatures={night: -10.0})
    assert hour_of(partial, local(2026, 11, 16, 4)).temperature_kw == 0.0


def test_the_temperature_term_is_off_when_it_explains_too_little_or_too_few_hours_have_a_temperature() -> None:
    load, outdoor = winter_world(2, b=0.0, noise=0.3)
    rows = history(NOW - timedelta(days=42), NOW, load, outdoor)
    cold = {hour.start: -20.0 for hour in forecast_house_load(rows, NOW, TZ).hours}
    unheated = forecast_house_load(rows, NOW, TZ, temperatures=cold)
    assert not unheated.temperature.active
    assert unheated.temperature.reason == TEMPERATURE_EXPLAINS_TOO_LITTLE
    assert all(hour.temperature_kw == 0.0 for hour in unheated.hours)
    # A heated house, but only five days with a recorded temperature (the rows before 3.0 have none).
    load, outdoor = winter_world(3, b=0.1, noise=0.1)
    cutoff = NOW - timedelta(days=5)
    rows = history(NOW - timedelta(days=42), NOW, load, lambda moment: outdoor(moment) if moment >= cutoff else None)
    short = forecast_house_load(rows, NOW, TZ, temperatures=cold)
    assert (short.temperature.active, short.temperature.reason) == (False, TEMPERATURE_TOO_FEW)


def test_the_forecast_is_never_below_zero() -> None:
    load, outdoor = winter_world(4, b=0.15, noise=0.05)
    rows = history(NOW - timedelta(days=42), NOW, load, outdoor)
    hot = {hour.start: 40.0 for hour in forecast_house_load(rows, NOW, TZ).hours}
    # 40 °C counts as the balance point: the term only ever lowers to the warm end of the history.
    forecast = forecast_house_load(rows, NOW, TZ, temperatures=hot)
    assert all(hour.kw is not None and hour.kw >= 0 for hour in forecast.hours)


# ---------------------------------------------------------------------------------------------- backtest parity


def test_the_backtest_ordering_holds_p75_with_temperature_beats_the_flat_figure() -> None:
    """On a synthetic heated house, a day-ahead replay as in `plans/load_forecast_backtest/backtest.py`: each day
    forecast at midnight from the hours before it. The ordering the owner's backtest found must hold: p75 + temperature
    under-forecasts far less often than today's flat 24-h mean, above all by more than 0.5 kW at night, and the
    temperature term lowers the error of the profile alone."""
    load, outdoor = winter_world(7, b=0.1, noise=0.25)
    start = local(2026, 9, 1)
    rows = history(start, local(2026, 12, 2), load, outdoor)
    actual = {row.start: row.house_kw for row in rows}
    results: dict[str, list[tuple[int, float, float]]] = {"flat": [], "p75": [], "p75T": []}
    for offset in range(41):
        when = date(2026, 10, 20) + timedelta(days=offset)
        day = local(when.year, when.month, when.day)
        past = [row for row in rows if row.start < day]
        flat = sum(row.house_kw for row in past[-24:]) / 24  # type: ignore[misc]
        temperatures = {row.start: row.outdoor_c for row in rows if day <= row.start < day + timedelta(days=1)}
        p75 = forecast_house_load(past, day, TZ, horizon_h=24)
        p75t = forecast_house_load(past, day, TZ, temperatures=temperatures, horizon_h=24)  # type: ignore[arg-type]
        actual = {row.start: row.house_kw for row in rows}
        for name, forecast in (("p75", p75), ("p75T", p75t)):
            for hour in forecast.hours:
                results[name].append((local_hour(hour.start), actual[hour.start], hour.kw))  # type: ignore[arg-type]
        for hour in p75.hours:
            results["flat"].append((local_hour(hour.start), actual[hour.start], flat))  # type: ignore[arg-type]

    def big_under(name: str, hours: Callable[[int], bool] = lambda _hour: True) -> float:
        """The share of hours forecast more than 0.5 kW too low (what makes a plan too optimistic)."""
        picked = [(y, f) for hour, y, f in results[name] if hours(hour)]
        return sum(1 for y, f in picked if y - f > 0.5) / len(picked)

    def under(name: str) -> float:
        return sum(1 for _hour, y, f in results[name] if y > f) / len(results[name])

    def mae(name: str) -> float:
        return sum(abs(y - f) for _hour, y, f in results[name]) / len(results[name])

    def evening(hour: int) -> bool:
        return 16 <= hour < 20

    def night(hour: int) -> bool:
        return hour >= 22 or hour < 7

    # The profile alone already halves the large under-forecasts; the temperature term halves them again and cuts the
    # error of the profile alone. (Synthetic numbers: flat 17 %, p75 6 %, p75 + temperature 0 % of hours.)
    assert big_under("p75") < big_under("flat") / 2
    assert big_under("p75T") < big_under("p75") / 2
    assert big_under("p75T", evening) < big_under("flat", evening) / 2
    assert big_under("p75T", night) <= big_under("flat", night)
    assert under("p75T") < under("p75") < under("flat")
    assert mae("p75T") < mae("p75")


# ---------------------------------------------------------------------------------------------- in-day adjustment


def busy_today(today_kw: float, until_hour: int, *, day: tuple[int, int, int] = (2026, 11, 18)) -> list[HourMean]:
    """Four weeks at 1 kW, then today (a Wednesday) at `today_kw` from midnight until `until_hour`."""
    midnight = local(*day)
    rows = history(midnight - timedelta(days=28), midnight, lambda _moment: 1.0)
    return rows + history(midnight, midnight + timedelta(hours=until_hour), lambda _moment: today_kw)


def test_a_busy_day_scales_the_rest_of_today_and_fades_out_over_tomorrow() -> None:
    rows = busy_today(2.0, 6)
    now = local(2026, 11, 18, 6, 5)
    forecast = forecast_house_load(rows, now, TZ)
    factor = 1 + 0.95 * (2.0 - 1.0)
    assert forecast.inday.today_ratio == pytest.approx(2.0)
    assert forecast.inday.today_hours == 6
    noon = hour_of(forecast, local(2026, 11, 18, 12))
    assert (noon.raw_kw, noon.kw, noon.inday_factor) == (1.0, pytest.approx(factor), pytest.approx(factor))
    # Tomorrow starts at today's factor and falls linearly to none by its end.
    assert hour_of(forecast, local(2026, 11, 19, 0)).kw == pytest.approx(factor)
    assert hour_of(forecast, local(2026, 11, 19, 12)).kw == pytest.approx(1 + (factor - 1) / 2)
    assert hour_of(forecast, local(2026, 11, 19, 23)).kw == pytest.approx(1 + (factor - 1) / 24)
    assert hour_of(forecast, local(2026, 11, 20, 3)).kw == pytest.approx(1.0)
    # The raw figures are kept apart for the log.
    assert set(forecast.raw_by_hour().values()) == {1.0}


def test_the_adjustment_waits_for_three_hours_and_carries_yesterdays_factor_meanwhile() -> None:
    early = forecast_house_load(busy_today(2.0, 2), local(2026, 11, 18, 2, 5), TZ)
    assert early.inday.today_ratio is None
    assert {round(hour.kw, 6) for hour in early.hours} == {1.0}  # type: ignore[arg-type]
    # Yesterday ran at 2 kW all day: its factor is carried into today from full weight at midnight to none at its end.
    rows = busy_today(2.0, 24, day=(2026, 11, 17)) + history(local(2026, 11, 18), local(2026, 11, 18, 1), lambda _m: 1.0)
    carried = forecast_house_load(rows, local(2026, 11, 18, 1, 5), TZ)
    factor = carried.inday.yesterday_factor
    assert factor == pytest.approx(1.95)
    assert hour_of(carried, local(2026, 11, 18, 12)).inday_factor == pytest.approx(1 + (factor - 1) / 2)
    assert hour_of(carried, local(2026, 11, 19, 1)).inday_factor == 1.0


def test_the_factor_is_clamped_and_off_on_request() -> None:
    now = local(2026, 11, 18, 6, 5)
    assert forecast_house_load(busy_today(10.0, 6), now, TZ).inday.today_factor == 2.0
    assert forecast_house_load(busy_today(0.05, 6), now, TZ).inday.today_factor == 0.5
    off = forecast_house_load(busy_today(10.0, 6), now, TZ, inday=False)
    assert {hour.inday_factor for hour in off.hours} == {1.0}


def test_the_flat_rung_is_never_scaled_and_the_dst_day_fades_over_its_25_hours() -> None:
    few = history(local(2026, 11, 18), local(2026, 11, 18, 2), lambda _m: 5.0)
    flat = forecast_house_load(few, local(2026, 11, 18, 2, 5), TZ, flat_kw=0.7)
    assert {(hour.source, hour.kw) for hour in flat.hours} == {(SOURCE_FLAT, 0.7)}
    # 24 October 2026 busy from midnight; tomorrow is the 25-hour day.
    rows = busy_today(2.0, 8, day=(2026, 10, 24))
    forecast = forecast_house_load(rows, local(2026, 10, 24, 8, 5), TZ)
    factor = forecast.inday.today_factor
    last = local(2026, 10, 26) - timedelta(hours=1)
    assert hour_of(forecast, last).inday_factor == pytest.approx(1 + (factor - 1) / 25)
