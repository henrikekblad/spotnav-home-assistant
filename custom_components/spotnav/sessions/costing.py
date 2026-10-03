"""The price maths of a session: energy per interval, and a cost made from it when it is read.

A session never stores a finished cost. It stores, per price interval it drew energy in, the energy
(kWh) and the raw spot price as the relay published it (EUR per kWh) with the exchange rate and its
date, plus that day's average spot price (for the savings comparison). The cost is made at read time
(`price_slices`) from the person's *current* fiscal settings: the planner's own `effective_minor_per_kwh`,
so a corrected VAT, tax or grid fee corrects the whole history. A `Slice` carries its interval's start,
so settings that are dated can be applied per interval later.

Energy delivered between two samples is spread evenly over the time between them, so a 15- or
60-minute price boundary inside the span is honoured by the overlap with each interval, and a
23- or 25-hour day needs no special case because every instant here is absolute. Energy outside every
published interval has no slice (it stays in the session's energy, unpriced), never a guess.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any

from ..planning.planner import document_rate, effective_minor_per_kwh, FiscalChoice, PlannerInputError


@dataclass(frozen=True, slots=True)
class SpotInterval:
    """One published interval, raw: the relay's EUR per kWh, the day's exchange rate for the market's
    currency (1 for euro) and the date that rate is from."""

    utc_start: datetime
    utc_end: datetime
    day: date
    eur_per_kwh: float
    fx: float
    fx_date: date | None


@dataclass(frozen=True, slots=True)
class Slice:
    """The energy a session drew in one price interval, with the raw price that interval had."""

    start: datetime
    end: datetime
    kwh: float
    eur_per_kwh: float
    fx: float
    fx_date: date | None
    #: The interval's day's average spot price (EUR per kWh, weighted by interval length).
    day_avg_eur: float

    def as_row(self) -> list[Any]:
        return [
            int(self.start.timestamp()),
            int(self.end.timestamp()),
            self.kwh,
            self.eur_per_kwh,
            self.fx,
            self.day_avg_eur,
            None if self.fx_date is None else self.fx_date.isoformat(),
        ]

    @classmethod
    def from_row(cls, row: Any) -> Slice | None:
        """A stored row, or `None` when it is not exactly that shape."""
        if not isinstance(row, list) or len(row) != 7:
            return None
        start, end, kwh, eur, fx, average, fx_date = row
        for number in (start, end, kwh, eur, fx, average):
            if isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(number):
                return None
        if kwh < 0 or fx <= 0 or end <= start:
            return None
        moment = None
        if fx_date is not None:
            if not isinstance(fx_date, str):
                return None
            try:
                moment = date.fromisoformat(fx_date)
            except ValueError:
                return None
        return cls(
            datetime.fromtimestamp(start, timezone.utc),
            datetime.fromtimestamp(end, timezone.utc),
            float(kwh),
            float(eur),
            float(fx),
            moment,
            float(average),
        )


@dataclass(frozen=True, slots=True)
class Costing:
    """What a set of slices cost: the part that had a price, in the minor unit.

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


def spot_intervals(documents: Iterable[Any], currency: str) -> tuple[SpotInterval, ...]:
    """Every published interval of these day documents, raw, in instant order.

    Two documents pricing the same instant give one row, the first winning. A document without a usable
    rate for the currency prices nothing (its energy stays unpriced).
    """
    rows: dict[datetime, SpotInterval] = {}
    # A display day cut from two market files carries each file's own rate in its pieces.
    for document in (piece for whole in documents for piece in whole.pieces()):
        try:
            fx = document_rate(document, currency)
        except PlannerInputError:
            continue
        fx_date = None if currency.upper() == "EUR" else document.fx_date
        for interval in document.intervals:
            if interval.utc_start not in rows:
                rows[interval.utc_start] = SpotInterval(
                    interval.utc_start, interval.utc_end, document.day, interval.eur_per_kwh, fx, fx_date
                )
    return tuple(rows[start] for start in sorted(rows))


def day_averages_eur(spot: Sequence[SpotInterval]) -> dict[date, float]:
    """Each published day's average spot price (EUR per kWh), weighted by interval length so a 23- or
    25-hour day and a mixed 15/60-minute day average what the clock says."""
    weighted: dict[date, float] = {}
    seconds: dict[date, float] = {}
    for interval in spot:
        length = (interval.utc_end - interval.utc_start).total_seconds()
        weighted[interval.day] = weighted.get(interval.day, 0.0) + interval.eur_per_kwh * length
        seconds[interval.day] = seconds.get(interval.day, 0.0) + length
    return {day: weighted[day] / seconds[day] for day in weighted if seconds[day] > 0}


def split_energy(spot: Sequence[SpotInterval], start: datetime, end: datetime, energy_kwh: float) -> list[Slice]:
    """The slices of `energy_kwh` delivered evenly from `start` to `end`, one per interval it touched.

    A span with no length is a point: its energy takes the interval holding that instant.
    """
    if energy_kwh <= 0 or not spot:
        return []
    averages = day_averages_eur(spot)
    span = (end - start).total_seconds()
    if span <= 0:
        for interval in spot:
            if interval.utc_start <= start < interval.utc_end:
                return [_slice(interval, energy_kwh, averages)]
        return []
    out = []
    for interval in spot:
        overlap = (min(end, interval.utc_end) - max(start, interval.utc_start)).total_seconds()
        if overlap > 0:
            out.append(_slice(interval, energy_kwh * overlap / span, averages))
    return out


def _slice(interval: SpotInterval, kwh: float, averages: dict[date, float]) -> Slice:
    return Slice(
        interval.utc_start,
        interval.utc_end,
        kwh,
        interval.eur_per_kwh,
        interval.fx,
        interval.fx_date,
        averages.get(interval.day, interval.eur_per_kwh),
    )


def merge_slices(existing: Sequence[Slice], new: Iterable[Slice]) -> tuple[Slice, ...]:
    """`existing` with `new` added: energy in an interval already held is summed into its slice, so a
    session sampled every minute still holds one slice per interval."""
    by_start = {item.start: item for item in existing}
    for item in new:
        held = by_start.get(item.start)
        by_start[item.start] = item if held is None else Slice(
            held.start, held.end, held.kwh + item.kwh, held.eur_per_kwh, held.fx, held.fx_date, held.day_avg_eur
        )
    return tuple(by_start[start] for start in sorted(by_start))


def price_slices(slices: Sequence[Slice], fiscal: FiscalChoice | None) -> Costing:
    """What the slices cost with `fiscal` (the person's settings as they are now), in the minor unit.

    With no resolvable fiscal choice nothing is priced: a figure that cannot be known stays absent.
    """
    if fiscal is None:
        return NO_COST
    priced = cost = reference = 0.0
    for item in slices:
        if item.kwh <= 0:
            continue
        priced += item.kwh
        cost += item.kwh * effective_minor_per_kwh(item.eur_per_kwh * item.fx, fiscal)
        reference += item.kwh * effective_minor_per_kwh(item.day_avg_eur * item.fx, fiscal)
    return Costing(priced, cost, reference)
