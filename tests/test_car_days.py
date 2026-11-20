"""The cars' charges on the rest of the month from their history (`planning/car_days.py`) and the predicted day peaks
they raise beyond the house forecast (`planning/demand_cost.predicted_day_peaks`). Stockholm's clock; 2026-11-02 is
a Monday."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from custom_components.spotnav.planning.auto_settings import DemandComponent
from custom_components.spotnav.planning.car_days import (
    car_kw_by_hour,
    Charge,
    charge_day,
    least_counted_kw,
    plugged_window,
    predicted_charges,
)
from custom_components.spotnav.planning.demand_charge import counted_day, HourMean
from custom_components.spotnav.planning.demand_cost import predicted_day_peaks
from custom_components.spotnav.planning.planner import HighLoadWindow

TZ = "Europe/Stockholm"
STOCKHOLM = ZoneInfo(TZ)
UTC = timezone.utc
GBG_STD = DemandComponent(top_n=3, window=None, price_winter=49.0, price_summer=49.0)
GBG_TID = DemandComponent(
    top_n=3,
    window=HighLoadWindow(days="weekdays", from_month=11, to_month=3, from_hour=7, to_hour=20),
    price_winter=135.0,
    price_summer=0.0,
)


def at(*args: int) -> datetime:
    return datetime(*args, tzinfo=STOCKHOLM).astimezone(UTC)


def night(day: date, energy: float = 20.0, plug_h: int = 22, leave_h: int = 7, max_kw: float = 11.0) -> Charge:
    plugged = at(day.year, day.month, day.day, plug_h)
    nxt = day + timedelta(days=1)
    return Charge(plugged, at(nxt.year, nxt.month, nxt.day, leave_h), energy, max_kw)


def test_a_charge_after_midnight_belongs_to_the_evening_before() -> None:
    assert charge_day(at(2026, 11, 3, 1), TZ) == date(2026, 11, 2)
    assert charge_day(at(2026, 11, 3, 13), TZ) == date(2026, 11, 3)


def test_the_plugged_window_runs_to_the_departure_after_the_plug_in() -> None:
    plugged, ended = at(2026, 11, 2, 22), at(2026, 11, 3, 1)
    assert plugged_window(plugged, ended, time(7), TZ) == (plugged, at(2026, 11, 3, 7))
    # Without a departure: as it charged.
    assert plugged_window(plugged, ended, None, TZ) == (plugged, ended)
    # A charge running past the departure keeps its end.
    assert plugged_window(plugged, at(2026, 11, 3, 8), time(7), TZ)[1] == at(2026, 11, 3, 8)


def test_a_weekday_charged_on_in_two_of_four_weeks_is_predicted_for_the_rest_of_the_month() -> None:
    # Monday nights in October and early November: charged on three of the last four Mondays.
    history = [night(date(2026, 10, 12), 18.0), night(date(2026, 10, 19), 20.0), night(date(2026, 11, 2), 30.0)]
    # One Tuesday only: no habit.
    history.append(night(date(2026, 10, 27), 40.0))
    now = at(2026, 11, 4, 12)
    found = predicted_charges(history, now, date(2026, 11, 30), TZ)
    assert [charge_day(charge.plugged_at, TZ) for charge in found] == [
        date(2026, 11, 9),
        date(2026, 11, 16),
        date(2026, 11, 23),
        date(2026, 11, 30),
    ]
    first = found[0]
    assert first.plugged_at == at(2026, 11, 9, 22)
    assert first.leaves_at == at(2026, 11, 10, 7)
    assert first.energy_kwh == pytest.approx(20.0)
    assert first.max_kw == 11.0


def test_charges_older_than_four_weeks_or_tiny_make_no_habit() -> None:
    history = [night(date(2026, 9, 28)), night(date(2026, 10, 5)), night(date(2026, 10, 26), 0.5)]
    assert predicted_charges(history, at(2026, 11, 4, 12), date(2026, 11, 30), TZ) == []


def test_the_least_power_fills_uncounted_hours_first() -> None:
    charge = night(date(2026, 11, 9), energy=40.0, plug_h=18, leave_h=7)  # 13 h
    # The tidsindelad window counts 07-20 on weekdays: 18-20 counted (2 h), 11 h free at 11 kW: nothing to add.
    counts = lambda start: counted_day(GBG_TID, start, TZ) is not None  # noqa: E731
    assert least_counted_kw(charge, counts) == {}
    # Everything counted: spread over 13 hours.
    spread = least_counted_kw(charge, lambda _start: True)
    assert len(spread) == 13
    assert list(spread.values()) == pytest.approx([40.0 / 13] * 13)
    # More than the uncounted hours take: the rest over the counted ones, never above the top power.
    short = Charge(at(2026, 11, 9, 18), at(2026, 11, 9, 22), 30.0, 11.0)
    found = least_counted_kw(short, counts)
    assert sorted(found) == [at(2026, 11, 9, 18), at(2026, 11, 9, 19)]
    assert list(found.values()) == pytest.approx([(30.0 - 22.0) / 2] * 2)
    # Two charges in one hour add up.
    assert car_kw_by_hour([short, short], counts)[at(2026, 11, 9, 18)] == pytest.approx(8.0)


def test_a_predicted_car_day_beyond_the_forecast_raises_its_predicted_peak() -> None:
    now = at(2026, 11, 4, 12)
    # The house's history: 2 kW at 03:00 and 4 kW at 18:00 every working day.
    history = []
    for offset in range(1, 15):
        day = date(2026, 11, 4) - timedelta(days=offset)
        history.append(HourMean(start=at(day.year, day.month, day.day, 3), kw=2.0, house_kw=2.0, floor_kw=2.0))
        history.append(HourMean(start=at(day.year, day.month, day.day, 18), kw=4.0, house_kw=4.0, floor_kw=4.0))
    charge = Charge(at(2026, 11, 10, 1), at(2026, 11, 10, 6), 30.0, 11.0)
    without = predicted_day_peaks(GBG_STD, {}, history, now, TZ)
    with_car = predicted_day_peaks(GBG_STD, {}, history, now, TZ, [charge])
    assert without[date(2026, 11, 10)] == pytest.approx(4.0)
    # 30 kWh over five hours: 6 kW on the 2 kW night hours (the hours without history count as 0 house).
    assert with_car[date(2026, 11, 10)] == pytest.approx(6.0 + 2.0)
    assert with_car[date(2026, 11, 11)] == without[date(2026, 11, 11)]
    # A charge whose hours the forecast knows is the installed plans' business (`with_cars`), not added again.
    known = {at(2026, 11, 10, 0) + timedelta(hours=h): 1.0 for h in range(24)}
    assert predicted_day_peaks(GBG_STD, known, history, now, TZ, [charge])[date(2026, 11, 10)] == pytest.approx(1.0)
