"""An electricity price entry's arithmetic: its fees, its prices with fees, and the day's cheap and dear hours.

Pure: no `hass`, no clock, no storage. The money is the charger's own: the fees resolve through
`auto_controller.fiscal_choice_for` and every price goes through `planner.chart_intervals`, which applies
`planner.effective_minor_per_kwh`, so a price entry and a charger with the same settings show the same
number. Prices here are in the area's major unit per kWh (kr/kWh, EUR/kWh), the unit an automation reads.

The hours are ranked per local day of the area (`PricePoint.day`, the display date the relay's document
belongs to), by the price with fees, ties to the earlier interval. Intervals are chosen one by one
(`SELECTION_INDIVIDUAL`); `SELECTIONS` is where a later rule, such as one contiguous block, would go.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Callable, Final, Iterable, Mapping, Sequence

from ..planning.auto_controller import fiscal_choice_for
from ..planning.auto_settings import AreaAutoSettings, AutoSettings, FiscalOverride
from ..planning.planner import chart_intervals, FiscalChoice
from .price_source import fees_from_data
from .relay_contract import AreaEntry, PriceDocument


#: What one fee is set to: nothing, the area's suggestion, or a figure of the person's own.
FEE_OFF: Final = "off"
FEE_SUGGESTED: Final = "suggested"
FEE_MANUAL: Final = "manual"
FEE_MODES: Final = (FEE_OFF, FEE_SUGGESTED, FEE_MANUAL)

#: The two rankings, and their hours per day when nothing is restored.
CHEAP: Final = "cheap"
EXPENSIVE: Final = "expensive"
DEFAULT_HOURS: Final = {CHEAP: 6.0, EXPENSIVE: 4.0}
MAX_HOURS: Final = 24.0
HOURS_STEP: Final = 0.25

#: Intervals chosen one by one, wherever they fall in the day.
SELECTION_INDIVIDUAL: Final = "individual"

#: Decimals a price is stated with (the sensor's state and the action's answer).
PRICE_DECIMALS: Final = 4


@dataclass(frozen=True, slots=True)
class PricePoint:
    """One published interval: when (UTC), which local day it belongs to, and its two prices."""

    start: datetime
    end: datetime
    day: date
    spot: float
    price: float

    @property
    def minutes(self) -> float:
        return (self.end - self.start).total_seconds() / 60


def suggestion(area: AreaEntry | None, component: str) -> float | None:
    """The area's own suggestion for one fee, or `None` when it publishes none."""
    if area is None:
        return None
    return {"vat": area.vat_percent, "tax": area.suggested_tax, "transfer": area.suggested_grid_fee}[component]


def included(area: AreaEntry | None, component: str) -> bool:
    """Whether the area's published price already holds this fee (nothing is added for it)."""
    return area is not None and area.includes({"vat": "vat", "tax": "tax", "transfer": "grid_fee"}[component])


def fee_mode(override: FiscalOverride) -> str:
    """`off`, `suggested` or `manual`, the same reading as a charger's fee."""
    if not override.enabled:
        return FEE_OFF
    return FEE_SUGGESTED if override.value is None else FEE_MANUAL


def fee_override(mode: str, value: float | None) -> FiscalOverride:
    """The stored fee for a mode and a figure: a figure is kept only where it is the person's own."""
    if mode == FEE_SUGGESTED:
        return FiscalOverride(enabled=True, value=None)
    if mode == FEE_MANUAL:
        return FiscalOverride(enabled=True, value=None if value is None else float(value))
    return FiscalOverride(enabled=False, value=None)


def default_fee_mode(area: AreaEntry | None, component: str) -> str:
    """The flow's first answer for a fee: the area's suggestion where it has one, else nothing."""
    if included(area, component) or suggestion(area, component) is None:
        return FEE_OFF
    return FEE_SUGGESTED


def overrides_from_data(area_id: str, data: Mapping[str, Any]) -> AreaAutoSettings:
    """A price entry's stored fees as a charger's per-area record. A missing or odd fee is off."""
    return fees_from_data(area_id, data)


def fiscal_for(overrides: AreaAutoSettings, area: AreaEntry | None) -> FiscalChoice | None:
    """What the fees resolve to for the area, exactly as a charger's plan resolves them, or `None`
    when one cannot be (a suggestion the area does not publish)."""
    if area is None or area.id != overrides.area_id:
        return None
    return fiscal_choice_for(AutoSettings(area_id=area.id, overrides=(overrides,)), area)


def price_points(
    documents: Sequence[PriceDocument], *, currency: str, fiscal: FiscalChoice
) -> tuple[PricePoint, ...]:
    """Every interval of these documents with its spot price and its price with fees, in time order.

    Raises `planner.PlannerInputError` where a price cannot be converted (no rate for the currency).
    """
    if not documents:
        return ()
    return tuple(
        PricePoint(
            start=row.utc_start,
            end=row.utc_end,
            day=row.day,
            spot=row.raw_minor_per_kwh / 100,
            price=row.effective_minor_per_kwh / 100,
        )
        for row in chart_intervals(tuple(documents), currency=currency, fiscal=fiscal)
    )


def by_day(points: Iterable[PricePoint]) -> dict[date, list[PricePoint]]:
    days: dict[date, list[PricePoint]] = {}
    for point in points:
        days.setdefault(point.day, []).append(point)
    return days


def _individual(points: Sequence[PricePoint], hours: float, expensive: bool) -> frozenset[datetime]:
    """The day's cheapest (or dearest) intervals, one by one, until they make up the hours.

    An interval is taken while at least half of it fits in what is left, so a fraction of an hourly
    price is rounded to the nearest whole hour (half rounds up). 24 hours or more is the whole day,
    whatever its length.
    """
    if hours <= 0 or not points:
        return frozenset()
    if hours >= MAX_HOURS:
        return frozenset(point.start for point in points)
    target = hours * 60
    order = sorted(points, key=lambda point: ((-point.price if expensive else point.price), point.start))
    chosen: list[datetime] = []
    total = 0.0
    for point in order:
        if total + point.minutes / 2 > target + 1e-9:
            break
        chosen.append(point.start)
        total += point.minutes
    return frozenset(chosen)


#: Each selection rule by name. Only `individual` exists; a contiguous block would be a second entry.
SELECTIONS: Final[dict[str, Callable[[Sequence[PricePoint], float, bool], frozenset[datetime]]]] = {
    SELECTION_INDIVIDUAL: _individual,
}


@dataclass(frozen=True, slots=True)
class Ranking:
    """One ranking over every known day: the chosen intervals, and each day's threshold price (the
    dearest chosen for cheap, the cheapest chosen for expensive; `None` for a day with none chosen)."""

    kind: str
    chosen: frozenset[datetime]
    thresholds: Mapping[date, float | None]


def rank(
    points: Sequence[PricePoint], hours: float, kind: str, *, selection: str = SELECTION_INDIVIDUAL
) -> Ranking:
    """Rank every day of [points] on its own for [kind] (`cheap` or `expensive`)."""
    expensive = kind == EXPENSIVE
    chosen: set[datetime] = set()
    thresholds: dict[date, float | None] = {}
    for day, items in by_day(points).items():
        picked = SELECTIONS[selection](items, hours, expensive)
        chosen |= picked
        prices = [point.price for point in items if point.start in picked]
        thresholds[day] = None if not prices else (min(prices) if expensive else max(prices))
    return Ranking(kind=kind, chosen=frozenset(chosen), thresholds=thresholds)


def point_at(points: Sequence[PricePoint], now: datetime) -> PricePoint | None:
    """The interval that holds [now], or `None`."""
    for point in points:
        if point.start <= now < point.end:
            return point
    return None


def next_boundary(points: Sequence[PricePoint], now: datetime) -> datetime | None:
    """The next instant after [now] where an interval starts or ends, or `None` past the last one."""
    upcoming = [moment for point in points for moment in (point.start, point.end) if moment > now]
    return min(upcoming) if upcoming else None


def next_price_change(points: Sequence[PricePoint], now: datetime) -> datetime | None:
    """When the price with fees next differs from the current one (or the prices end), or `None`."""
    current = point_at(points, now)
    if current is None:
        return next_boundary(points, now)
    cursor = current
    for point in points:
        if point.start < current.end:
            continue
        if point.start != cursor.end or point.price != current.price:
            return cursor.end
        cursor = point
    return cursor.end


def next_switch(points: Sequence[PricePoint], ranking: Ranking, now: datetime) -> datetime | None:
    """When a ranking's binary sensor next turns on or off, within the known prices, or `None`.

    The end of the known prices counts as a change only for a sensor that is on (it goes unavailable).
    """
    current = point_at(points, now)
    state = current is not None and current.start in ranking.chosen
    cursor_end = None if current is None else current.end
    for point in points:
        if point.end <= now or point is current:
            continue
        if cursor_end is not None and point.start != cursor_end:
            # A gap: the sensor is off through it.
            if state:
                return cursor_end
            state = False
        on = point.start in ranking.chosen
        if on != state:
            return point.start
        cursor_end = point.end
    return cursor_end if state else None


def rounded(value: float) -> float:
    """A price as stated: [PRICE_DECIMALS] decimals, never `-0.0`."""
    result = round(value, PRICE_DECIMALS)
    return 0.0 if result == 0 else result


def resolution_minutes(points: Sequence[PricePoint]) -> int | None:
    """The finest interval length among [points], in minutes, or `None` with none."""
    lengths = [int(math.floor(point.minutes + 0.5)) for point in points]
    return min(lengths) if lengths else None
