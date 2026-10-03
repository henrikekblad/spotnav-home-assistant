"""The price maths of a session: energy per interval times the interval's effective price.

The effective price is the planner's own (`planner.effective_minor_per_kwh`, reached through
`chart_intervals`): the area's price, plus energy tax and grid fee, times VAT, with the person's
settings in force at the time the energy was delivered. Nothing here knows a price, a clock or a
charger; it splits an energy amount over the instants it was delivered in.

Energy delivered between two samples is spread evenly over the time between them, so a 15- or
60-minute price boundary inside the span is honoured by the overlap with each interval, and a
23- or 25-hour day needs no special case because every instant here is absolute.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime

from ..planning.planner import ChartInterval


@dataclass(frozen=True, slots=True)
class Costing:
    """What one stretch of energy cost: the part that had a published price, in the minor unit.

    `reference_cost_minor` prices that same energy at its day's average effective price.
    """

    priced_kwh: float = 0.0
    cost_minor: float = 0.0
    reference_cost_minor: float = 0.0

    def __add__(self, other: Costing) -> Costing:
        return Costing(
            self.priced_kwh + other.priced_kwh,
            self.cost_minor + other.cost_minor,
            self.reference_cost_minor + other.reference_cost_minor,
        )


NO_COST = Costing()


def day_averages(intervals: Sequence[ChartInterval]) -> dict[date, float]:
    """Each published day's average effective price (minor unit per kWh), weighted by interval length so
    a 23- or 25-hour day and a mixed 15/60-minute day average what the clock says."""
    weighted: dict[date, float] = {}
    seconds: dict[date, float] = {}
    for interval in intervals:
        length = (interval.utc_end - interval.utc_start).total_seconds()
        weighted[interval.day] = weighted.get(interval.day, 0.0) + interval.effective_minor_per_kwh * length
        seconds[interval.day] = seconds.get(interval.day, 0.0) + length
    return {day: weighted[day] / seconds[day] for day in weighted if seconds[day] > 0}


def cost_of(
    intervals: Sequence[ChartInterval], start: datetime, end: datetime, energy_kwh: float
) -> Costing:
    """The cost of `energy_kwh` delivered evenly from `start` to `end`.

    A span with no length is a point: its energy takes the price of the interval holding that instant.
    Energy outside every published interval is unpriced and left out, never given a guess.
    """
    if energy_kwh <= 0 or not intervals:
        return NO_COST
    averages = day_averages(intervals)
    span = (end - start).total_seconds()
    priced = cost = reference = 0.0
    if span <= 0:
        for interval in intervals:
            if interval.utc_start <= start < interval.utc_end:
                return Costing(
                    energy_kwh,
                    energy_kwh * interval.effective_minor_per_kwh,
                    energy_kwh * averages.get(interval.day, interval.effective_minor_per_kwh),
                )
        return NO_COST
    for interval in intervals:
        overlap = (min(end, interval.utc_end) - max(start, interval.utc_start)).total_seconds()
        if overlap <= 0:
            continue
        part = energy_kwh * overlap / span
        priced += part
        cost += part * interval.effective_minor_per_kwh
        reference += part * averages.get(interval.day, interval.effective_minor_per_kwh)
    return Costing(priced, cost, reference)
