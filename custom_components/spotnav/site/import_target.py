"""Holding a site's chargers under its import target: pure, no Home Assistant.

A site's import target (`CONF_IMPORT_TARGET_KW`, kW) is an hourly mean of import the person wants to stay under. The
site's hour tracker (`site/demand_tracker.py`) measures the running clock hour; this module turns it into what the
site's chargers may still draw, and the site controller (`SiteCapacityController._hold_to_import_target`) takes the
smaller of that and what the fuse allows.

* **Budget.** `target x 1 h - import so far this hour` is what the rest of the hour may still import. Spread over the
  time left it is a power; less the house's own load now (`house_w`, the site's net import less what its chargers
  draw, signed: a solar surplus makes it negative and leaves the chargers more) it is what the chargers may draw:
  `allowed_w = budget / remaining_h - house_w`, clamped to `[0, ceiling_w]`. The division explodes in the last
  minutes; the time left counts as at least `MIN_REMAINING_S`, and the clamp (the fuse's own ceiling) bounds it.
* **Import so far** is the tracker's energy over the time it measured, scaled up to the time the hour has run
  (`last_at - start`): a short gap counts at the hour's own mean, never as nothing. Under `battery_first` a home
  battery's discharge counts as import too (`extra_wh`, scaled the same way over its own measured time).
* **Not applied** (`hour_budget` gives a reason, no budget) while the hour cannot be known well enough to hold a
  charger for it: no running hour, no live reading now, more than `GAP_MAX_S` without a reading this hour, or the
  house's load now unknown. Nothing is then held for the target; the fuse rules are untouched either way.
* **Sharing** (`share_budget`): chargers in the site's own order (`allocation_order`: priority, then the order they
  joined). Each takes what it would draw (the fuse's proposal) while that fits what is left; else it is held to the
  whole amps that fit, never below its minimum. Below its minimum the hour can no longer end at or under the target
  with it running at its minimum for the rest of the hour (`allowed_w` is exactly that projection), so it is paused
  until the next clock hour (`ACTION_PAUSED`); a charger already paused this hour stays paused. A switch-only charger
  runs only while its whole switched power fits what is left. A charger the target may not hold (a person's Start, a
  charge the departure needs, a decision the fuse made) is counted at what it draws and never held. In the hour's
  last `RAISE_LOCK_S` nothing is raised above what a charger is given: only lowerings and pauses still act.
* **The departure** (`departure_shortfall_kwh`, `departure_at_risk`): above the car's minimum level the target holds
  a plan's charge even when that makes it miss its departure. What it still needs before its departure (what the
  plan expects of its windows' time left, `plan_remaining_kwh`, plus what the running window is already behind)
  against what the held power delivers by then (this hour and every later hour at what the target leaves beside the
  house's typical load) tells whether the departure is at risk: decided on every recompute, with a margin either way.
  A person is then told, and may set the target aside for that charge until its departure.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
import math
from typing import Final

from .demand_tracker import GAP_MAX_S, HOUR_S, RunningHour

#: The nominal phase voltage a charger's amps are turned into power with (as the regulator's steps are).
HOLD_VOLTAGE_V: Final = 230.0
#: The least time left of the hour the budget is spread over: the division explodes in the last seconds.
MIN_REMAINING_S: Final = 60.0
#: In the hour's last ten minutes the target raises no charger above what it is given (`SiteCapacityController.
#: _hold_to_import_target`): what is left of the budget is spread over too little time to be worth a raise that the
#: next hour takes back, and the lowering that follows waits for the dwell, past the next hour's start.
RAISE_LOCK_S: Final = 600.0
#: How much more (kWh) a plan must still need than the held power delivers by its departure to count as at risk, and
#: how much it must then have to spare to count as safe again.
RISK_ENTER_KWH: Final = 0.5
RISK_LEAVE_KWH: Final = -0.5

#: Why the import target is not applied this hour (`hour_budget`).
NOT_APPLIED_NO_HOUR: Final = "no_hour"
NOT_APPLIED_NO_READING: Final = "no_reading"
NOT_APPLIED_GAP: Final = "gap"
NOT_APPLIED_HOUSE_UNKNOWN: Final = "house_unknown"
NOT_APPLIED_BATTERY_GAP: Final = "battery_gap"

#: What the target does to one charger (`share_budget`).
ACTION_FREE: Final = "free"
ACTION_HELD: Final = "held"
ACTION_PAUSED: Final = "paused"
ACTION_UNHELD: Final = "unheld"


@dataclass(frozen=True, slots=True)
class HourBudget:
    """The running hour against the target (see the module)."""

    target_kw: float
    hour_start: datetime
    hour_end: datetime
    #: The hour's import so far (kWh), gaps counted at the hour's own mean.
    so_far_kwh: float
    #: The hour's mean import over the time measured so far (kW), as shown beside the target.
    hour_kw: float
    remaining_s: float
    house_w: float
    #: What is left of the hour's budget spread over the time left (W), before the house's load is taken off.
    rate_w: float
    #: What the site's chargers may draw for the rest of the hour (W).
    allowed_w: float
    #: The house alone, without any charger, ends the hour above the target.
    house_over: bool


def hour_budget(
    *,
    target_kw: float,
    running: RunningHour | None,
    now: datetime,
    house_w: float | None,
    ceiling_w: float,
    extra_wh: float = 0.0,
    extra_covered_s: float | None = None,
) -> tuple[HourBudget | None, str | None]:
    """The running hour's budget at `now`, or `(None, reason)` while it is not applied (see the module).

    `extra_wh` is energy counted as import beside the grid's (a discharging battery under `battery_first`), measured
    over `extra_covered_s` (`None`: no such energy is counted)."""
    if running is None or not running.start <= now < running.start + timedelta(hours=1):
        return None, NOT_APPLIED_NO_HOUR
    if running.last_at is None or running.last_grid_w is None:
        return None, NOT_APPLIED_NO_READING
    measured_span_s = max(running.covered_s, (running.last_at - running.start).total_seconds())
    if running.covered_s <= 0 and measured_span_s > 0:
        return None, NOT_APPLIED_NO_READING
    if running.gap_s > GAP_MAX_S:
        return None, NOT_APPLIED_GAP
    if house_w is None or not math.isfinite(house_w):
        return None, NOT_APPLIED_HOUSE_UNKNOWN
    # The hour's first reading, taken right at its start (the last hour's reading carried over): nothing so far.
    so_far_wh = 0.0 if measured_span_s <= 0 else running.import_wh * measured_span_s / running.covered_s
    if extra_covered_s is not None:
        if measured_span_s - extra_covered_s > GAP_MAX_S:
            return None, NOT_APPLIED_BATTERY_GAP
        if extra_covered_s > 0:
            so_far_wh += extra_wh * measured_span_s / extra_covered_s
    hour_end = running.start + timedelta(hours=1)
    remaining_s = max(MIN_REMAINING_S, (hour_end - now).total_seconds())
    target_wh = target_kw * 1000.0
    rate_w = (target_wh - so_far_wh) * HOUR_S / remaining_s
    allowed_w = min(max(0.0, rate_w - house_w), max(0.0, ceiling_w))
    house_over = so_far_wh + max(0.0, house_w) * remaining_s / HOUR_S > target_wh
    return (
        HourBudget(
            target_kw=target_kw,
            hour_start=running.start,
            hour_end=hour_end,
            so_far_kwh=so_far_wh / 1000.0,
            hour_kw=0.0 if measured_span_s <= 0 else so_far_wh / measured_span_s * 3.6,
            remaining_s=remaining_s,
            house_w=house_w,
            rate_w=rate_w,
            allowed_w=allowed_w,
            house_over=house_over,
        ),
        None,
    )


@dataclass(frozen=True, slots=True)
class TargetMember:
    """One charger as the budget is shared (see `share_budget`)."""

    charger_entry_id: str
    phases: int
    min_a: float
    #: What it would draw (A per phase): the fuse's proposal, what it draws now where the target does not touch it,
    #: what a paused charge would resume at, or its whole switched current.
    wants_a: float
    #: Whether the target may hold it (a person's Start, a charge the departure needs, a decision about the fuse: no).
    holdable: bool = True
    switch_only: bool = False
    #: The target paused it this hour already: it stays paused until the next.
    paused_this_hour: bool = False


@dataclass(frozen=True, slots=True)
class TargetShare:
    """What the target does to one charger: `cap_a` is the most it is given now (`None`: nothing held), `room_a` the
    whole amps the budget left when its turn came (`None` for a charger the target does not hold): no step of yield
    stepping, no resume and no probe gives it more."""

    action: str
    cap_a: float | None = None
    room_a: float | None = None


def member_w(phases: int, amps: float, voltage_v: float = HOLD_VOLTAGE_V) -> float:
    return max(0.0, amps) * max(1, phases) * voltage_v


def share_budget(
    allowed_w: float, members: Sequence[TargetMember], voltage_v: float = HOLD_VOLTAGE_V
) -> dict[str, TargetShare]:
    """Share `allowed_w` among `members`, in the order given (the site's `allocation_order`; see the module)."""
    left = max(0.0, allowed_w)
    shares: dict[str, TargetShare] = {}
    for member in members:
        phases = max(1, member.phases)
        if not member.holdable:
            shares[member.charger_entry_id] = TargetShare(ACTION_UNHELD)
            left = max(0.0, left - member_w(phases, member.wants_a, voltage_v))
            continue
        if member.paused_this_hour:
            shares[member.charger_entry_id] = TargetShare(ACTION_PAUSED, 0.0, 0.0)
            continue
        room_a = float(math.floor(left / (phases * voltage_v) + 1e-9))
        if member.wants_a <= 0:
            shares[member.charger_entry_id] = TargetShare(ACTION_FREE, None, room_a)
            continue
        wanted_w = member_w(phases, member.wants_a, voltage_v)
        if wanted_w <= left + 1e-6:
            shares[member.charger_entry_id] = TargetShare(ACTION_FREE, None, room_a)
            left -= wanted_w
            continue
        if member.switch_only or room_a < member.min_a:
            shares[member.charger_entry_id] = TargetShare(ACTION_PAUSED, 0.0, 0.0)
            continue
        shares[member.charger_entry_id] = TargetShare(ACTION_HELD, room_a, room_a)
        left -= member_w(phases, room_a, voltage_v)
    return shares


def plan_remaining_kwh(
    windows: Iterable[tuple[datetime, datetime]], power_kw: float, now: datetime, until: datetime
) -> float:
    """What a plan still expects to deliver between `now` and `until`: its windows' time left there, at its power."""
    seconds = 0.0
    for start, end in windows:
        overlap = (min(end, until) - max(start, now)).total_seconds()
        if overlap > 0:
            seconds += overlap
    return max(0.0, power_kw) * seconds / HOUR_S


def departure_shortfall_kwh(
    *,
    need_kwh: float,
    now: datetime,
    departure: datetime,
    hour_end: datetime,
    held_kw: float,
    later_kw: float,
) -> float | None:
    """How much (kWh) a held charge misses its departure by: `need_kwh` less what `held_kw` for the rest of this hour
    and `later_kw` for every hour after it deliver by `departure` (negative: to spare). `None` once the departure has
    passed or nothing is needed (see the module)."""
    if departure <= now or need_kwh <= 0:
        return None
    this_hour_s = max(0.0, (min(hour_end, departure) - now).total_seconds())
    later_s = max(0.0, (departure - max(hour_end, now)).total_seconds())
    deliverable = max(0.0, held_kw) * this_hour_s / HOUR_S + max(0.0, later_kw) * later_s / HOUR_S
    return need_kwh - deliverable


def departure_at_risk(shortfall_kwh: float | None, *, at_risk: bool) -> bool:
    """Whether the departure is at risk under the target: a shortfall above `RISK_ENTER_KWH`, and once at risk until
    the held power has the departure's energy to spare again (`RISK_LEAVE_KWH`), so it does not flap at the edge."""
    if shortfall_kwh is None:
        return False
    return shortfall_kwh > (RISK_LEAVE_KWH if at_risk else RISK_ENTER_KWH)


def lowering_floor_a(
    *,
    phases: Sequence[str],
    measured_a: Mapping[str, float | None],
    signed_power_w: Mapping[str, float | None],
    voltage_v: Mapping[str, float | None],
    margin_a: Mapping[str, float | None] | None = None,
) -> float | None:
    """The lowest current a charger may be lowered to for the target without its lowering raising the current the
    fuse carries on any phase it draws on: each phase's net import must stay an import (`P - d x U >= 0`), so its
    magnitude only falls. Where the direction comes from a signed current (`margin_a` given) the drop also stays within
    the phase's margin, as the regulator's own decrease does (`|net - d| <= |net| + d`). `None` when a phase cannot
    be judged: nothing is then lowered for the target."""
    floors: list[float] = []
    for phase in phases:
        measured = measured_a.get(phase)
        power = signed_power_w.get(phase)
        voltage = voltage_v.get(phase) or HOLD_VOLTAGE_V
        if measured is None or power is None or not math.isfinite(power) or voltage <= 0:
            return None
        drop = max(0.0, power / voltage)
        if margin_a is not None:
            margin = margin_a.get(phase)
            if margin is None:
                return None
            drop = min(drop, max(0.0, margin))
        floors.append(measured - drop)
    return max(floors) if floors else None
