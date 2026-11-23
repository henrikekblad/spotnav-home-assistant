"""The pool's shadow planner (spec section 5.1). Pure: no Home Assistant import, aware datetimes from the caller.

* **The filtering need** per day (`filter_need_h`): the person's `filter_hours`, else the water temperature over three
  in quarter hours, held between 2 and 12 hours (the owner's practice: about 8 hours at 25 °C, 3 to 4 at 10 to 12 °C;
  the common rule of thumb is a half, unverified). The temperature is the day's highest valid reading, yesterday's
  before today has one; without any, `DEFAULT_NEED_H`.
* **The heat need** in heat pump hours (`heat_need_h`): the way to the target at the learnt rise per hour, plus a day's
  learnt fall, while the water is under the target; nothing at or above it.
* **The placement** (`plan_pool`), in whole clock hours over the window (to the end of the prices held, at most
  `MAX_HOURS`): an hour's cost is the sell price where the sun's surplus over the house covers the load (what export
  would have earned), else the buy price with fees. The heat pump's hours are placed first, within the coming day, at
  the cost of the heat pump and the circulation together; the circulation's hours are those and the cheapest further
  hours until the rolling need is met (`switch_load.deadlines`, any 28 hours: the need is moved, never dropped).
  Hours whose room under the ceiling cannot take the load are not chosen (`blocked`). Runs are whole hours, at most
  `MAX_STARTS_DAY` a day, by a small dynamic programme (the hard need first, then the starts, then the cost). An
  **extra filtering** adds its hours on top, the cheapest before its end.
* **The season hint** (`season_hint`): the day's highest water reading at or below `SEASON_HINT_C` for
  `SEASON_HINT_DAYS` days in a row, August to December, once a year while the season is open.

The numbers marked placeholder in the spec stay placeholders here: a tester's pool says what they should be.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Final

from ..loads.switch_load import RunHistory, deadlines
from .config import PoolConfig

HOUR: Final = timedelta(hours=1)
MAX_HOURS: Final = 36
#: The heat pump's hours are placed within this much of now (its need is a day's).
HEAT_HORIZON_H: Final = 24
MIN_NEED_H: Final = 2.0
MAX_NEED_H: Final = 12.0
#: The need while no water temperature has been read (placeholder).
DEFAULT_NEED_H: Final = 4.0
#: Starts a day at most, the circulation's and the heat pump's alike (placeholder).
MAX_STARTS_DAY: Final = 4
#: Learnt rise in °C per heat pump hour, its seed (9 kW of heat into about 40 m³) and the runs it is learnt from.
SEED_RISE_C_H: Final = 0.2
SEED_FALL_C_DAY: Final = 0.5
LEARN_RUNS: Final = 10
LEARN_MIN_H: Final = 1.0
#: What the heat pump and the circulation draw where nothing says (only to cost the hours: placeholders).
DEFAULT_HEAT_PUMP_KW: Final = 1.5
DEFAULT_CIRCULATION_KW: Final = 0.4
SEASON_HINT_C: Final = 11.0
SEASON_HINT_DAYS: Final = 3
SEASON_HINT_MONTHS: Final = frozenset({8, 9, 10, 11, 12})
EXTRA_HOURS: Final = (2, 4)
EXTRA_SPAN: Final = timedelta(hours=24)

REASON_SUN: Final = "sun"
REASON_CHEAP: Final = "cheap"
REASON_NEED: Final = "need"
REASON_EXTRA: Final = "extra"

#: The dynamic programme's weights: an hour of need left short outweighs any price, a start over the cap any cost.
_SHORT: Final = 1e9
_OVER_START: Final = 1e6
#: A small cost per start, so equal hours join into one run.
_START: Final = 1e-3


def quarter_h(hours: float) -> float:
    return round(hours * 4.0) / 4.0


def filter_need_h(config: PoolConfig, water_c: float | None) -> tuple[float, str]:
    """The day's filtering need and where it comes from: `override` (the person's hours), `water` or `default`."""
    if config.filter_hours is not None:
        return config.filter_hours, "override"
    if water_c is None:
        return DEFAULT_NEED_H, "default"
    return min(MAX_NEED_H, max(MIN_NEED_H, quarter_h(water_c / 3.0))), "water"


def learnt_rise(runs: Sequence[tuple[datetime, datetime, float, float]]) -> tuple[float, bool]:
    """The rise per heat pump hour learnt from the last runs of at least an hour that warmed the water, and whether it
    is learnt (else the seed)."""
    rates = [
        (to_c - from_c) / ((end - start).total_seconds() / 3600.0)
        for start, end, from_c, to_c in runs
        if (end - start) >= timedelta(hours=LEARN_MIN_H) and to_c > from_c
    ][-LEARN_RUNS:]
    if not rates:
        return SEED_RISE_C_H, False
    return round(statistics.median(rates), 3), True


def learnt_fall(samples: Sequence[tuple[float, float, float]]) -> tuple[float, bool]:
    """The fall per day learnt from overnight samples `(from_c, to_c, hours)` without heat, and whether it is learnt."""
    rates = [(from_c - to_c) * 24.0 / hours for from_c, to_c, hours in samples if hours > 0][-LEARN_RUNS:]
    if not rates:
        return SEED_FALL_C_DAY, False
    return round(max(0.0, statistics.median(rates)), 3), True


def heat_need_h(target_c: float, water_c: float | None, rise_c_h: float, fall_c_day: float) -> float:
    """The heat pump hours wanted today (see the module); nothing without a reading or at the target."""
    if water_c is None or water_c >= target_c or rise_c_h <= 0:
        return 0.0
    return quarter_h((target_c - water_c + fall_c_day) / rise_c_h)


def season_hint(daily_max: Mapping[date, float], today: date, *, season_open: bool, hinted_year: int | None) -> bool:
    """Whether to hint that the season be closed (see the module)."""
    if not season_open or hinted_year == today.year or today.month not in SEASON_HINT_MONTHS:
        return False
    days = [today - timedelta(days=offset) for offset in range(1, SEASON_HINT_DAYS + 1)]
    return all(day in daily_max and daily_max[day] <= SEASON_HINT_C for day in days)


@dataclass(frozen=True, slots=True)
class PriceHour:
    start: datetime
    buy: float
    sell: float


@dataclass(frozen=True, slots=True)
class Extra:
    hours: int
    until: datetime

    def as_dict(self) -> dict[str, Any]:
        return {"hours": self.hours, "until": self.until.isoformat()}


@dataclass(frozen=True, slots=True)
class PoolInputs:
    now: datetime
    config: PoolConfig
    #: The window's hours (UTC hour starts, from the running one) with their buy and sell price (minor per kWh).
    prices: Sequence[PriceHour]
    #: The circulation's own shadow runs (what SpotNav would have run), for the rolling need.
    history: RunHistory
    water_c: float | None = None
    #: The day's highest valid water reading (yesterday's before today has one).
    day_water_c: float | None = None
    rise_c_h: float = SEED_RISE_C_H
    fall_c_day: float = SEED_FALL_C_DAY
    #: The sun's surplus over the house per hour (kW), where a forecast says.
    surplus_kw: Mapping[datetime, float] = field(default_factory=dict)
    #: The room under the ceiling per hour (kW), where known.
    room_kw: Mapping[datetime, float] = field(default_factory=dict)
    extra: Extra | None = None
    #: The local midnight that ends today (for today's figures).
    day_end: datetime | None = None
    heat_pump_kw: float | None = None
    circulation_kw: float | None = None


@dataclass(frozen=True, slots=True)
class PoolPlan:
    made_at: datetime
    #: `(start, end, reason)` runs, the circulation's (`extra` for the extra filtering's hours) and the heat pump's.
    circulation: tuple[tuple[datetime, datetime, str], ...]
    heat: tuple[tuple[datetime, datetime, str], ...]
    need_h: float
    need_source: str
    heat_h: float
    extra: Extra | None
    blocked_hours: tuple[datetime, ...]
    #: The hours of the need the window could not take (too few hours with room before a deadline).
    short_h: int = 0
    #: Per chosen hour: its reason, the circulation's and the heat pump's.
    circulation_hours: Mapping[datetime, str] = field(default_factory=dict)
    heat_hours: Mapping[datetime, str] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "made_at": self.made_at.isoformat(),
            "circulation": [[a.isoformat(), b.isoformat(), why] for a, b, why in self.circulation],
            "heat": [[a.isoformat(), b.isoformat(), why] for a, b, why in self.heat],
            "need_h": self.need_h,
            "need_source": self.need_source,
            "heat_h": self.heat_h,
            "extra": None if self.extra is None else self.extra.as_dict(),
            "blocked_hours": [start.isoformat() for start in self.blocked_hours],
            "short_h": self.short_h,
        }


def _keep(
    states: dict[tuple[int, bool, int], tuple[float, tuple[int, ...]]],
    key: tuple[int, bool, int],
    value: tuple[float, tuple[int, ...]],
) -> None:
    """Keep the cheaper way to reach a state."""
    held = states.get(key)
    if held is None or value[0] < held[0]:
        states[key] = value


def _place(
    costs: Sequence[float | None],
    target: int,
    *,
    forced: frozenset[int] = frozenset(),
    checks: Mapping[int, int] | None = None,
    max_starts: int,
) -> set[int]:
    """The hours to run (indexes into `costs`; `None`: not available): `target` hours in all (the forced ones among
    them), at least `checks[i]` of them up to and including hour `i`, at most `max_starts` runs, at the least cost. A
    need that cannot be met is met as far as it can (the weights in the module)."""
    checks = checks or {}
    count = len(costs)
    # A need due by an hour is still due after it: each hour it stays short weighs again, so a late need runs at once.
    due: list[int] = []
    running = 0
    for index in range(count):
        running = max(running, checks.get(index, 0))
        due.append(running)
    target = min(target, sum(1 for cost in costs if cost is not None) + 0)
    target = max(target, len(forced))
    # state: (chosen so far, the last hour chosen, starts) -> (cost, chosen hours)
    states: dict[tuple[int, bool, int], tuple[float, tuple[int, ...]]] = {(0, False, 0): (0.0, ())}
    for index in range(count):
        cost = costs[index]
        nxt: dict[tuple[int, bool, int], tuple[float, tuple[int, ...]]] = {}

        for (chosen, last, starts), (total, hours) in states.items():
            if index not in forced:
                _keep(nxt, (chosen, False, starts), (total, hours))
            if (cost is not None or index in forced) and chosen < target:
                start = 0 if last else 1
                extra = (cost or 0.0) + (_START if start else 0.0) + (_OVER_START if start and starts >= max_starts else 0.0)
                _keep(nxt, (chosen + 1, True, starts + start), (total + extra, (*hours, index)))
        need = due[index]
        if need > 0:
            nxt = {key: (total + _SHORT * max(0, min(need, target) - key[0]), hours) for key, (total, hours) in nxt.items()}
        states = nxt
    best = min(states.items(), key=lambda item: (item[1][0] + _SHORT * (target - item[0][0]), item[0][2]))
    return set(best[1][1])


def _runs(hours: Sequence[datetime], reasons: Mapping[datetime, str]) -> tuple[tuple[datetime, datetime, str], ...]:
    out: list[list[Any]] = []
    for start in sorted(reasons):
        why = reasons[start]
        if out and out[-1][1] == start and out[-1][2] == why:
            out[-1][1] = start + HOUR
        else:
            out.append([start, start + HOUR, why])
    return tuple((a, b, c) for a, b, c in out)


def plan_pool(inputs: PoolInputs) -> PoolPlan:
    """The pool's hours (see the module)."""
    config = inputs.config
    now = inputs.now
    need_h, source = filter_need_h(config, inputs.day_water_c)
    heat_h = (
        heat_need_h(config.target_c, inputs.water_c, inputs.rise_c_h, inputs.fall_c_day) if config.heat_pump is not None else 0.0
    )
    if not config.season_open:
        return PoolPlan(now, (), (), need_h, source, 0.0, None, ())
    prices = sorted(inputs.prices, key=lambda hour: hour.start)[:MAX_HOURS]
    hours = [hour.start for hour in prices]
    circ_kw = config.circulation_kw or inputs.circulation_kw or DEFAULT_CIRCULATION_KW
    pump_kw = config.heat_pump_kw or inputs.heat_pump_kw or DEFAULT_HEAT_PUMP_KW

    def cost(hour: PriceHour, kw: float) -> tuple[float, str]:
        surplus = inputs.surplus_kw.get(hour.start)
        if surplus is not None and surplus >= kw:
            return hour.sell * kw, REASON_SUN
        return hour.buy * kw, REASON_CHEAP

    def room_for(start: datetime, kw: float) -> bool:
        room = inputs.room_kw.get(start)
        return room is None or room >= kw

    blocked = tuple(start for start in hours if not room_for(start, circ_kw))
    days = max(1, math.ceil(len(hours) / 24))
    starts = MAX_STARTS_DAY * days

    # The heat pump first, within the coming day, where the circulation and the heat pump both fit.
    heat_reasons: dict[datetime, str] = {}
    heat_set: set[int] = set()
    if heat_h > 0 and config.heat_pump is not None:
        horizon = now + timedelta(hours=HEAT_HORIZON_H)
        both = circ_kw + (pump_kw if config.circulation_id is not None else 0.0)
        costs = [
            cost(hour, both)[0] if hour.start < horizon and room_for(hour.start, both) else None for hour in prices
        ]
        heat_set = _place(costs, math.ceil(heat_h - 1e-9), max_starts=starts)
        heat_reasons = {hours[i]: cost(prices[i], both)[1] for i in heat_set}

    # The circulation: the heat pump's hours and the rolling need (moved, never dropped).
    circ_reasons: dict[datetime, str] = {}
    short = 0
    if config.circulation_id is not None:
        steps = deadlines(inputs.history, now, need_h)
        checks: dict[int, int] = {}
        for moment, quarters in steps:
            if not hours:
                break
            # The last hour that ends by the moment; a moment inside the running hour binds it (as soon as can be).
            index = max((i for i, start in enumerate(hours) if start + HOUR <= moment), default=0)
            checks[index] = max(checks.get(index, 0), math.ceil(quarters / 4 - 1e-9))
        wanted = max([*checks.values(), len(heat_set)], default=len(heat_set))
        costs = [cost(hour, circ_kw)[0] if room_for(hour.start, circ_kw) else None for hour in prices]
        chosen = _place(costs, wanted, forced=frozenset(heat_set), checks=checks, max_starts=starts)
        short = max([0, *(need - sum(1 for i in chosen if i <= index) for index, need in checks.items())])
        free = [costs[i] for i in range(len(hours)) if i not in chosen and costs[i] is not None]
        cheapest_free = min(free, default=None)
        for i in sorted(chosen):
            reason = cost(prices[i], circ_kw)[1]
            if i not in heat_set and reason == REASON_CHEAP and cheapest_free is not None and (costs[i] or 0.0) > cheapest_free + 1e-9:
                reason = REASON_NEED
            circ_reasons[hours[i]] = reason
        extra = inputs.extra
        if extra is not None and extra.until > now:
            spare = sorted(
                (costs[i], i)
                for i in range(len(hours))
                if i not in chosen and costs[i] is not None and hours[i] < extra.until
            )
            for _cost, i in spare[: extra.hours]:
                circ_reasons[hours[i]] = REASON_EXTRA
    elif heat_set:
        # No circulation chosen: the heat pump's hours stand alone (the person's own schedule runs the circulation).
        pass
    return PoolPlan(
        made_at=now,
        circulation=_runs(hours, circ_reasons),
        heat=_runs(hours, heat_reasons),
        need_h=need_h,
        need_source=source,
        heat_h=heat_h,
        extra=inputs.extra if inputs.extra is not None and inputs.extra.until > now else None,
        blocked_hours=blocked,
        short_h=short,
        circulation_hours=circ_reasons,
        heat_hours=heat_reasons,
    )
