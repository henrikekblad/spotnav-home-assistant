"""The hot-water plan: one self-ending boost in the cheapest allowed quarter before each peak, where it beats the
pump's own run (plan `plans/v3_dhw_plan.md` section 4). Pure, on quarter-hours (UTC quarter starts).

**Per peak** (`dhw_peaks.Peak` on a local day): the window runs from the previous peak's `until` plus
`AFTER_PREVIOUS` (no use heating before a draw), at most `MAX_LEAD` before `ready_by`, never before now, to
`ready_by` less the run's length.

**A candidate start's cost**: the peak's kWh at the run's mean price (the **sell** price in a quarter whose forecast
surplus covers the boost: sun first), plus the tank's standby loss (`LOSS_KWH_H`) from the run's end to `ready_by`
at the price of the pump's own run.

**Dropped** when any of the run's quarters has no price, lies in `blocked` (a heat lowering on a path that blocks hot
water too, a raise that already makes hot water), in `unsafe` (a heat safety reason: too cold outdoors), or has no
headroom: the joint load (`load_kw`: the house, the cars' plans, the battery's grid charge) plus the boosts already
placed plus `boost_kw` above `cap_kw`.

**Worth it**: the best candidate must beat the pump's own run (the peak's kWh at the price where its reload is
expected, `Peak.run_at`) by `min_gain` (0.10 SEK in the area's currency, `min_gain()`); else no boost, the pump does it itself. No price there: no pick.

**Budget**: at most `budget` picks per local day (the route's planned actions, `BUDGETS`) less those used already.

**Stability**: a pick starting within `FREEZE` (or already begun) is kept; an earlier one moves at most once per
`MOVE_SPACING` and only for a gain of `min_gain` over its cost now.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone, tzinfo
from typing import Any, Final

from .dhw_peaks import Peak, peak_instant

QUARTER: Final = timedelta(minutes=15)
MAX_LEAD: Final = timedelta(hours=8)
AFTER_PREVIOUS: Final = timedelta(minutes=30)
#: A ~270 l tank loses about 1 kWh a day.
LOSS_KWH_H: Final = 0.04
#: The least saving per run that makes a boost worth it: 0.10 SEK, in each area currency's major unit (rounded at
#: 2026 rates; every area currency has 100 minor units to the major one, `pricing/areas.json`). A currency not
#: listed takes the euro's figure.
MIN_GAIN_MAJOR: Final = {
    "SEK": 0.10,
    "NOK": 0.10,
    "DKK": 0.07,
    "EUR": 0.01,
    "CHF": 0.01,
    "GBP": 0.01,
    "PLN": 0.04,
    "CZK": 0.25,
    "RON": 0.05,
    "HUF": 3.5,
}
MINOR_PER_MAJOR: Final = 100.0
#: The SEK figure in the minor unit (öre), the planner's default.
MIN_GAIN: Final = MIN_GAIN_MAJOR["SEK"] * MINOR_PER_MAJOR


def min_gain(currency: str | None) -> float:
    """The least saving per run in the currency's minor unit (the unit the prices come in)."""
    major = MIN_GAIN_MAJOR.get((currency or "").upper(), MIN_GAIN_MAJOR["EUR"])
    return major * MINOR_PER_MAJOR
FREEZE: Final = timedelta(minutes=60)
MOVE_SPACING: Final = timedelta(hours=1)
DEFAULT_RUN: Final = timedelta(minutes=30)
DEFAULT_BOOST_KW: Final = 3.0

REASON_CHEAP: Final = "cheap"
REASON_SUN: Final = "sun"

#: Planned actions a day per write route (section 6): a cloud's quota allows the morning and the evening only.
ROUTE_CLOUD: Final = "cloud"
ROUTE_ONE_SHOT: Final = "one_shot"
ROUTE_VOLATILE: Final = "volatile"
ROUTE_PULSE: Final = "pulse"
BUDGETS: Final = {ROUTE_CLOUD: 2, ROUTE_ONE_SHOT: 3, ROUTE_VOLATILE: 3, ROUTE_PULSE: 3}


def quarter_of(moment: datetime) -> datetime:
    """The UTC quarter-hour that holds `moment`."""
    seconds = int(moment.timestamp() // 900) * 900
    return datetime.fromtimestamp(seconds, timezone.utc)


def quarter_ceil(moment: datetime) -> datetime:
    start = quarter_of(moment)
    return start if start == moment else start + QUARTER


def quarters_of(slots: Sequence[tuple[datetime, datetime, float]]) -> dict[datetime, float]:
    """Price slots `(start, end, price)` (15 min or 1 h) as a price per quarter start."""
    out: dict[datetime, float] = {}
    for start, end, price in slots:
        moment = quarter_of(start)
        while moment < end:
            out[moment] = price
            moment += QUARTER
    return out


def hourly_to_quarters(by_hour: Mapping[datetime, float]) -> dict[datetime, float]:
    """A figure per UTC hour start (kW) as the same figure for each of its quarters."""
    return {hour + QUARTER * index: value for hour, value in by_hour.items() for index in range(4)}


@dataclass(frozen=True, slots=True)
class Pick:
    """A planned boost: its run, the peak it fills (`peak`: `ready_by`), and the pump's own run it replaces."""

    start: datetime
    end: datetime
    kwh: float
    #: The run's mean price as reckoned (minor unit per kWh): the sell price on sun.
    price: float
    reason: str
    peak: datetime
    own_at: datetime
    own_price: float
    #: What it saves against the pump's own run (minor unit), the standby loss counted.
    gain: float
    moved_at: datetime | None = None

    def stored(self) -> dict[str, Any]:
        return {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "kwh": round(self.kwh, 3),
            "price": round(self.price, 3),
            "reason": self.reason,
            "peak": self.peak.isoformat(),
            "own_at": self.own_at.isoformat(),
            "own_price": round(self.own_price, 3),
            "gain": round(self.gain, 3),
            "moved_at": None if self.moved_at is None else self.moved_at.isoformat(),
        }

    @classmethod
    def from_stored(cls, raw: Any) -> Pick | None:
        try:
            moved = raw.get("moved_at")
            return cls(
                datetime.fromisoformat(raw["start"]),
                datetime.fromisoformat(raw["end"]),
                float(raw["kwh"]),
                float(raw["price"]),
                str(raw["reason"]),
                datetime.fromisoformat(raw["peak"]),
                datetime.fromisoformat(raw["own_at"]),
                float(raw["own_price"]),
                float(raw["gain"]),
                None if moved is None else datetime.fromisoformat(moved),
            )
        except (AttributeError, KeyError, TypeError, ValueError):
            return None


@dataclass(frozen=True, slots=True)
class PlanInputs:
    now: datetime
    zone: tzinfo
    peaks: Sequence[Peak]
    #: Prices per quarter start (minor unit per kWh).
    buy: Mapping[datetime, float]
    sell: Mapping[datetime, float] = field(default_factory=dict)
    #: The forecast solar surplus per quarter (kW the site would export).
    surplus_kw: Mapping[datetime, float] = field(default_factory=dict)
    #: The joint load per quarter (kW): the house, the cars' plans, the battery's grid charge.
    load_kw: Mapping[datetime, float] = field(default_factory=dict)
    cap_kw: float | None = None
    blocked: frozenset[datetime] = frozenset()
    unsafe: frozenset[datetime] = frozenset()
    budget: int = BUDGETS[ROUTE_ONE_SHOT]
    #: Actions already used per local day (live boosts written, a button press on a cloud route).
    used: Mapping[date, int] = field(default_factory=dict)
    run_len: timedelta = DEFAULT_RUN
    boost_kw: float = DEFAULT_BOOST_KW
    previous: Sequence[Pick] = ()
    min_gain: float = MIN_GAIN


@dataclass(frozen=True, slots=True)
class _Instance:
    ready: datetime
    until: datetime
    own_at: datetime
    kwh: float


def _instances(inputs: PlanInputs) -> list[_Instance]:
    today = inputs.now.astimezone(inputs.zone).date()
    out = []
    for offset in (-1, 0, 1):
        day = today + timedelta(days=offset)
        for peak in inputs.peaks:
            ready, until, run = peak_instant(peak, day, inputs.zone)
            out.append(_Instance(ready.astimezone(timezone.utc), until.astimezone(timezone.utc), run.astimezone(timezone.utc), peak.kwh))
    return sorted(out, key=lambda item: item.ready)


class _Planner:
    def __init__(self, inputs: PlanInputs) -> None:
        self.inputs = inputs
        self.extra: dict[datetime, float] = {}
        self.count = math.ceil(inputs.run_len / QUARTER)

    def run_quarters(self, start: datetime) -> list[datetime]:
        return [start + QUARTER * index for index in range(self.count)]

    def allowed(self, start: datetime) -> bool:
        inputs = self.inputs
        for q in self.run_quarters(start):
            if q not in inputs.buy or q in inputs.blocked or q in inputs.unsafe:
                return False
            if inputs.cap_kw is not None:
                load = inputs.load_kw.get(q, 0.0) + self.extra.get(q, 0.0) + inputs.boost_kw
                if load > inputs.cap_kw + 1e-9:
                    return False
        return True

    def cost(self, start: datetime, item: _Instance, own_price: float) -> tuple[float, float, str]:
        """The run's cost with the standby loss, its mean price, and why (sun or cheap)."""
        inputs = self.inputs
        prices = []
        sunny = True
        for q in self.run_quarters(start):
            if inputs.surplus_kw.get(q, 0.0) >= inputs.boost_kw and q in inputs.sell:
                prices.append(inputs.sell[q])
            else:
                sunny = False
                prices.append(inputs.buy[q])
        mean = sum(prices) / len(prices)
        end = start + inputs.run_len
        loss = LOSS_KWH_H * max(0.0, (item.ready - end).total_seconds() / 3600.0) * own_price
        return item.kwh * mean + loss, mean, REASON_SUN if sunny else REASON_CHEAP

    def place(self, pick: Pick) -> None:
        for q in self.run_quarters(pick.start):
            self.extra[q] = self.extra.get(q, 0.0) + self.inputs.boost_kw


def plan(inputs: PlanInputs) -> list[Pick]:
    """The picks for the peaks ahead (see the module), in time order."""
    planner = _Planner(inputs)
    now = inputs.now
    instances = _instances(inputs)
    previous = {pick.peak: pick for pick in inputs.previous}
    per_day: dict[date, int] = dict(inputs.used)
    picks: list[Pick] = []

    # Frozen picks first: they hold their quarters whatever comes after.
    frozen: dict[datetime, Pick] = {}
    for item in instances:
        old = previous.get(item.ready)
        if old is not None and item.ready > now and old.start <= now + FREEZE:
            frozen[item.ready] = old
            planner.place(old)

    for index, item in enumerate(instances):
        if item.ready <= now:
            continue
        day = item.ready.astimezone(inputs.zone).date()
        if item.ready in frozen:
            picks.append(frozen[item.ready])
            per_day[day] = per_day.get(day, 0) + 1
            continue
        if per_day.get(day, 0) >= inputs.budget:
            continue
        own_price = inputs.buy.get(quarter_of(item.own_at))
        if own_price is None:
            continue
        lo = item.ready - MAX_LEAD
        if index > 0:
            lo = max(lo, instances[index - 1].until + AFTER_PREVIOUS)
        lo = quarter_ceil(max(lo, now))
        hi = item.ready - inputs.run_len
        best: tuple[float, float, str, datetime] | None = None
        moment = lo
        while moment <= hi:
            if planner.allowed(moment):
                cost, mean, reason = planner.cost(moment, item, own_price)
                if best is None or cost < best[0] - 1e-9:
                    best = (cost, mean, reason, moment)
            moment += QUARTER
        old = previous.get(item.ready)
        own_cost = item.kwh * own_price
        chosen: Pick | None = None
        if best is not None and own_cost - best[0] >= inputs.min_gain:
            cost, mean, reason, start = best
            chosen = Pick(start, start + inputs.run_len, item.kwh, mean, reason, item.ready, item.own_at, own_price, own_cost - cost, None)
        if old is not None and old.start > now and lo <= old.start <= hi and planner.allowed(old.start):
            old_cost, old_mean, old_reason = planner.cost(old.start, item, own_price)
            kept = replace(old, price=old_mean, reason=old_reason, own_price=own_price, gain=own_cost - old_cost)
            may_move = old.moved_at is None or now - old.moved_at >= MOVE_SPACING
            if own_cost - old_cost < inputs.min_gain:
                # No longer worth it where it stands: the best now, or none.
                chosen = None if chosen is None else replace(chosen, moved_at=now)
            elif (
                chosen is not None
                and chosen.start != old.start
                and may_move
                and old_cost - (own_cost - chosen.gain) >= inputs.min_gain
            ):
                chosen = replace(chosen, moved_at=now)
            else:
                chosen = kept
        elif chosen is not None and old is not None:
            chosen = replace(chosen, moved_at=now)
        if chosen is None:
            continue
        planner.place(chosen)
        picks.append(chosen)
        per_day[day] = per_day.get(day, 0) + 1
    return sorted(picks, key=lambda pick: pick.start)


__all__ = [
    "BUDGETS",
    "DEFAULT_BOOST_KW",
    "DEFAULT_RUN",
    "FREEZE",
    "MIN_GAIN",
    "Pick",
    "PlanInputs",
    "QUARTER",
    "REASON_CHEAP",
    "REASON_SUN",
    "ROUTE_CLOUD",
    "ROUTE_ONE_SHOT",
    "ROUTE_PULSE",
    "ROUTE_VOLATILE",
    "hourly_to_quarters",
    "min_gain",
    "plan",
    "quarter_of",
    "quarters_of",
]
