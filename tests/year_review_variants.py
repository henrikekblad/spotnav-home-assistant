"""The year review's price variants (`tests/test_year_review_prices.py`): site shapes with 15-minute prices, the
clock changes, gaps, part days, ties, negative and zero sell prices, and charges across midnight. Each is a site's
inputs without prices and the slots the old whole-year path read."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import date, datetime, timedelta
import math

from custom_components.spotnav.battery.plan_shadow import PriceSlot
from custom_components.spotnav.year_review.compute import YearInputs

from tests.test_year_review import (
    TODAY,
    UTC,
    YEAR_START,
    ZONE,
    buy_at,
    car_only_site,
    full_site,
    session,
    sun_battery_site,
)

QUARTER = timedelta(minutes=15)


def quarters(first: date, end: date) -> list[datetime]:
    out = []
    moment = datetime(first.year, first.month, first.day, tzinfo=ZONE).astimezone(UTC)
    stop = datetime(end.year, end.month, end.day, tzinfo=ZONE).astimezone(UTC)
    while moment < stop:
        out.append(moment)
        moment += QUARTER
    return out


def wavy(moment: datetime) -> float:
    seconds = moment.timestamp()
    return 40.0 + 30.0 * math.sin(seconds / 3600.0 * 0.7) + (int(seconds // 900) * 7919 % 113) / 1.7


def quarter_slots(
    first: date, end: date, buy: Callable[[datetime], float], sell: Callable[[float], float]
) -> list[PriceSlot]:
    return [PriceSlot(start, start + QUARTER, buy(start), sell(buy(start))) for start in quarters(first, end)]


def _across_midnight(inputs: YearInputs) -> YearInputs:
    extra = [
        session(datetime(2026, 3, 28, 23, tzinfo=ZONE), [3.0, 3.0, 3.0, 3.0], plugged=False),
        session(datetime(2026, 10, 24, 22, tzinfo=ZONE), [2.0, 2.0, 2.0, 2.0, 2.0], plugged=True),
        session(datetime(2026, 6, 3, 10, tzinfo=ZONE), [6.0, 6.0, 6.0], plugged=True),
    ]
    return replace(inputs, sessions=[*inputs.sessions, *extra])


def _gappy(slots: list[PriceSlot]) -> list[PriceSlot]:
    gap = (date(2026, 5, 10), date(2026, 5, 20))
    half = datetime(2026, 6, 3, 12, tzinfo=ZONE)
    out = []
    for slot in slots:
        local = slot.start.astimezone(ZONE)
        if gap[0] <= local.date() <= gap[1] or (local.date() == half.date() and local >= half):
            continue
        out.append(slot)
    return out


def _mixed(first: date, end: date) -> list[PriceSlot]:
    """Hourly slots before 1 April, quarters after (a source that changed its resolution)."""
    switch = date(2026, 4, 1)
    hourly = [
        PriceSlot(hour, hour + timedelta(hours=1), wavy(hour), wavy(hour) * 0.41 - 3.7)
        for hour in quarters(first, switch)[::4]
    ]
    return hourly + quarter_slots(switch, end, wavy, lambda buy: buy * 0.41 - 3.7)


def variants() -> dict[str, tuple[YearInputs, list[PriceSlot]]]:
    new_year = date(2027, 1, 1)
    return {
        "quarter_wavy": (
            _across_midnight(full_site()),
            quarter_slots(YEAR_START, TODAY, wavy, lambda buy: buy * 0.41 - 3.7),
        ),
        "quarter_ties": (_across_midnight(full_site()), quarter_slots(YEAR_START, TODAY, buy_at, lambda buy: buy / 2)),
        "quarter_gaps_mixed": (_across_midnight(full_site()), _gappy(_mixed(YEAR_START, TODAY))),
        "zero_sell": (sun_battery_site(), quarter_slots(YEAR_START, TODAY, wavy, lambda _buy: 0.0)),
        "negative_sell": (sun_battery_site(), quarter_slots(YEAR_START, TODAY, wavy, lambda _buy: -1.0)),
        "new_years_day": (
            _across_midnight(full_site(new_year)),
            quarter_slots(date(2025, 12, 30), date(2027, 1, 2), wavy, lambda buy: buy * 0.41 - 3.7),
        ),
        "nord_pool_quarter": (
            full_site(),
            quarter_slots(TODAY - timedelta(days=55), TODAY + timedelta(days=2), wavy, lambda buy: buy - 5.0),
        ),
        "car_only_no_prices": (car_only_site(), []),
        "car_only_ties": (car_only_site(), quarter_slots(YEAR_START, TODAY, buy_at, lambda buy: buy / 2)),
    }
