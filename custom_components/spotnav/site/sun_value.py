"""The sun's value on a site without a home battery (SpotNav 3.0, shadow). Pure.

On a site with a home battery the battery plan's nightly review values the sun (`DayReview.sun_value`,
`battery/review_baselines.sun_gave`). A site with solar panels and no battery has no such review, so the sun's value
is worked out here from the Energy dashboard's meters alone, with the same formula and the battery's term 0: per
period the house's grid is `import - export`, and the sun's value is what that grid would have cost without the sun
(`grid + sun`) less what it cost, at the period's buy and sell price (`review_baselines.grid_cost`).

Two bases, said with each day (`basis`):

* `quarter`: per quarter hour, from the recorder's 5-minute statistics summed to quarters, priced at the quarter's
  price (an hourly price spread over its quarters). The nightly sun review (`site/sun_review.py`) stores these, and a
  day the house card reads by the quarter hour is valued so too.
* `hour`: per hour, from the long-term statistics at the hour's mean price, for a day before the nightly review or
  past the 5-minute statistics' reach. An estimate: import and export within an hour net against each other, so an
  hour that sold in one quarter and bought in another reads less than its quarters do.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Final

from ..battery.plan_shadow import BUCKET, Bucket
from ..battery.review_baselines import sun_gave
from ..planning.demand_charge import hour_start
from .energy_flow import EnergyHour
from .history_period import local_date

BASIS_QUARTER: Final = "quarter"
BASIS_HOUR: Final = "hour"


@dataclass(frozen=True, slots=True)
class SunDay:
    """One local day's sun value (minor units) over `periods` valued periods, on `basis`; `hours` per local hour's
    start (UTC), empty where they are no longer kept."""

    day: date
    value: float
    periods: int
    basis: str
    hours: Mapping[datetime, float] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "value": round(self.value, 3),
            "periods": self.periods,
            "basis": self.basis,
            "hours": {hour.isoformat(): round(value, 3) for hour, value in sorted(self.hours.items())},
        }

    @classmethod
    def from_dict(cls, day: date, raw: Any) -> SunDay | None:
        if not isinstance(raw, Mapping):
            return None
        value, periods, basis = raw.get("value"), raw.get("periods"), raw.get("basis")
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
            return None
        if not isinstance(periods, int) or isinstance(periods, bool) or periods < 0 or basis not in (BASIS_QUARTER, BASIS_HOUR):
            return None
        hours: dict[datetime, float] = {}
        raw_hours = raw.get("hours")
        for key, figure in (raw_hours.items() if isinstance(raw_hours, Mapping) else ()):
            try:
                moment = datetime.fromisoformat(key)
            except (TypeError, ValueError):
                continue
            if moment.tzinfo is not None and isinstance(figure, (int, float)) and not isinstance(figure, bool) and math.isfinite(figure):
                hours[moment] = float(figure)
        return cls(day, float(value), periods, basis, hours)

    def without_hours(self) -> SunDay:
        return SunDay(self.day, self.value, self.periods, self.basis)


def period_value(period: EnergyHour, buy: float, sell: float) -> float | None:
    """The sun's value in one period (minor units) at its buy and sell price: `review_baselines.sun_gave` on the
    period's meters with no battery; `None` without the sun's production."""
    if period.solar is None:
        return None
    bucket = Bucket(
        start=period.start,
        buy=buy,
        sell=sell,
        import_kwh=period.grid_in,
        export_kwh=period.grid_out,
        # Measured whole: the period's energies are taken as they are.
        covered_s=BUCKET.total_seconds(),
    )
    value, counted = sun_gave((bucket,), {period.start: period.solar})
    return value if counted else None


def day_values(valued: Iterable[tuple[EnergyHour, float]], tz: str) -> dict[date, SunDay]:
    """Each local day's sun value from its valued periods (`(period, value)`), summed per local hour (an hour with no
    value left out): `quarter` where every period of the day is a quarter hour or less, else `hour`."""
    sums: dict[date, dict[datetime, float]] = {}
    totals: dict[date, float] = {}
    counts: dict[date, int] = {}
    hourly: set[date] = set()
    for period, value in valued:
        day = local_date(period.start, tz)
        hours = sums.setdefault(day, {})
        totals[day] = totals.get(day, 0.0) + value
        counts[day] = counts.get(day, 0) + 1
        if value != 0:
            # An hour without the sun has no figure of its own (the night is no bar), the day still counts it.
            hour = hour_start(period.start, tz)
            hours[hour] = hours.get(hour, 0.0) + value
        if period.minutes > BUCKET.total_seconds() / 60:
            hourly.add(day)
    return {
        day: SunDay(day, totals[day], counts[day], BASIS_HOUR if day in hourly else BASIS_QUARTER, hours)
        for day, hours in sums.items()
    }
