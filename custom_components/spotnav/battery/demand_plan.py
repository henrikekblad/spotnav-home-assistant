"""The battery plan's demand-charge side (plan v3_power_tariff step 5), in shadow: the demand variant's inputs, its
shave decisions for the debug log, and what each way of driving the battery added to the month's demand charge in
the nightly review. Pure: no Home Assistant, the clock is passed in.

* **The variant's inputs** (`ceilings_minor`, `demand_house`): the site's soft ceiling per hour
  (`site/demand_ceiling.py`, S_h kW and λ_h in the area's major unit per kW) as the plan's `(kW, minor per kW)`;
  the house in the counted hours at the error band's high side (`demand_cost.BATTERY_QUANTILE`, p90: the battery
  must keep energy in hand for the peak), the median elsewhere; while no band is learnt, the median throughout.
* **Shaves** (`shave_rows`): the variant's slots that shave, as rows for the site's change log.
* **The review's demand cost** (`DemandBasis`, `day_demand_cost`): per priced part, the month's measured day peaks
  before the reviewed day; a run's cost for the day is the change in the month's charge its own day peak causes
  against them: `λ · (sum of the top N day peaks with the run's day − without it)`, λ = price / N, billed with the
  part's minimum and step (`DemandComponent.billed`); a quarter-hour part's day peak is its highest quarter. A run
  that keeps under the month's N-th highest day costs nothing; a part without a price this month costs nothing.
  Every run (the battery's own way, the plan in force, each mode, the variants, self-consumption) is measured against the same
  measured month: the change it alone would have caused.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Final, Protocol

from ..planning.auto_settings import DemandComponent
from ..planning.demand_charge import HourMean, counted_day, day_peaks, free_level_hours, hour_start, month_of
from ..planning.demand_cost import marginal_price
from .plan_model import ACTION_SHAVE, PlanSlot, SlotDecision

#: Minor units to the area currency's major one (every area currency has 100, `pricing/areas.json`).
MINOR_PER_MAJOR: Final = 100.0


class _Ceiling(Protocol):
    kw: float
    price: float
    day: date | None


class _Band(Protocol):
    def bounds(self, value: float, start: datetime) -> tuple[float, float]: ...


def ceilings_minor(hours: Mapping[datetime, _Ceiling] | None) -> dict[datetime, tuple[float, float, date | None]]:
    """The soft ceiling per hour as the plan takes it: `(S_h kW, λ_h minor per kW, the day its peak counts toward)`;
    hours without a price left out."""
    if not hours:
        return {}
    return {
        start: (ceiling.kw, ceiling.price * MINOR_PER_MAJOR, ceiling.day)
        for start, ceiling in hours.items()
        if ceiling.price > 0
    }


def peak_so_far(parts: Sequence[Any], now: datetime, tz: str) -> float | None:
    """The running day's highest counted hour measured so far (kW) among the priced parts (`demand_cost.PartCost`):
    the day the demand variant's first slot starts from, never below 0 (a day that only exported); `None` without
    one."""
    current = hour_start(now, tz)
    found = [
        part.measured[day]
        for part in parts
        if part.marginal > 0
        and (day := counted_day(part.component, current, tz)) is not None
        and day in part.measured
    ]
    return max(0.0, max(found)) if found else None


def demand_house(
    house_by_hour: Mapping[datetime, float], band: _Band | None, ceilings: Mapping[datetime, Any]
) -> dict[datetime, float]:
    """The house the demand variant plans on: the band's high side (p90) in the hours with a soft ceiling, the median
    elsewhere and while no band is learnt (see the module)."""
    house = dict(house_by_hour)
    if band is None:
        return house
    for start in ceilings:
        value = house.get(start)
        if value is not None:
            house[start] = max(0.0, band.bounds(value, start)[1])
    return house


def needed(ceilings: Mapping[datetime, Any], hour_caps: Mapping[datetime, float] | None, cap_kw: float | None) -> bool:
    """Whether the demand variant would plan anything the plan in force does not: a priced soft ceiling, or an hour
    held lower than the scalar cap (the power level, in its hours)."""
    if ceilings:
        return True
    return any(cap_kw is None or kw < cap_kw - 1e-9 for kw in (hour_caps or {}).values())


def shave_rows(decisions: Sequence[SlotDecision], slots: Sequence[PlanSlot]) -> list[list[Any]]:
    """The slots that shave: `[start, battery kW (discharge, positive), grid kW, S_h kW, λ_h minor per kW]`."""
    return [
        [
            decision.start.isoformat(),
            round(-decision.battery_kw, 3),
            round(decision.grid_kw, 3),
            None if slot.soft_cap_kw is None else round(slot.soft_cap_kw, 3),
            round(slot.peak_price, 1),
        ]
        for decision, slot in zip(decisions, slots)
        if decision.action == ACTION_SHAVE
    ]


# ------------------------------------------------------------------------------------------------ the review


@dataclass(frozen=True, slots=True)
class DemandBasis:
    """What a reviewed day's demand cost is measured against: the priced parts (windows with the site's holidays),
    each with the month's measured day peaks before the day (kW, at each hour's floor)."""

    tz: str
    parts: tuple[tuple[DemandComponent, float, Mapping[date, float]], ...]

    @property
    def priced(self) -> bool:
        return any(marginal > 0 for _component, marginal, _peaks in self.parts)


def demand_basis(
    components: Sequence[DemandComponent], hours: Iterable[HourMean], day: date, tz: str
) -> DemandBasis | None:
    """The basis for reviewing `day` from the site's parts and the tracker's hours (this month's and the previous):
    `None` with no part priced in `day`'s month. Only the hours billed in `day`'s month and counted under an earlier
    day enter."""
    month = (day.year, day.month)
    earlier = [
        hour
        for hour in free_level_hours(hours)
        if month_of(hour.start, tz) == month
    ]
    parts: list[tuple[DemandComponent, float, Mapping[date, float]]] = []
    for component in components:
        marginal = marginal_price(component, day.month) * MINOR_PER_MAJOR
        if marginal <= 0:
            continue
        peaks = {
            counted: peak.kw for counted, peak in day_peaks(component, earlier, tz).items() if counted < day
        }
        parts.append((component, marginal, peaks))
    return DemandBasis(tz, tuple(parts)) if parts else None


def _top_sum(values: Iterable[float], n: int) -> float:
    return sum(sorted(values, reverse=True)[:n])


def day_demand_cost(basis: DemandBasis, grid_by_quarter: Iterable[tuple[datetime, float]]) -> float:
    """What a run's quarter hours (start, grid kW: + import) add to the month's demand charge (minor units; see the
    module). Each hour is the mean of its quarters measured; a counted day's peak its highest counted hour."""
    by_hour: dict[datetime, list[float]] = {}
    quarters = list(grid_by_quarter)
    for start, kw in quarters:
        by_hour.setdefault(hour_start(start, basis.tz), []).append(kw)
    means = {start: sum(values) / len(values) for start, values in by_hour.items()}
    total = 0.0
    for component, marginal, before in basis.parts:
        peaks: dict[date, float] = {}
        # A quarter-hour part's value is each quarter's own (`DemandComponent.period_min`).
        values = quarters if component.quarter else list(means.items())
        for start, kw in values:
            counted = counted_day(component, hour_start(start, basis.tz), basis.tz)
            if counted is not None:
                peaks[counted] = max(kw, peaks.get(counted, kw))
        price = marginal * component.top_n
        for counted, kw in peaks.items():
            others = [value for day, value in before.items() if day != counted]
            with_day = _top_sum([*others, max(kw, before.get(counted, kw))], component.top_n)
            without = _top_sum([*others, *([before[counted]] if counted in before else [])], component.top_n)
            # The bill on the mean of the top N, with its minimum and step (`DemandComponent.billed`).
            total += max(
                0.0,
                component.billed(with_day / component.top_n, price)
                - component.billed(without / component.top_n, price),
            )
    return total
