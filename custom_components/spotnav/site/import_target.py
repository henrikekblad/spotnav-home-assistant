"""Holding a site's chargers under its import target: pure, no Home Assistant.

A site's import target (`CONF_IMPORT_TARGET_KW`, kW) is an hourly mean of import the person wants to stay under. The
site's hour tracker (`site/demand_tracker.py`) measures the running clock hour; this module turns it into what the
site's chargers may still draw, and the site controller (`SiteCapacityController._hold_to_import_target`) takes the
smaller of that and what the fuse allows.

* **The level.** The import target as set, lifted to the month's free level where a demand-charge part counts the
  running hour (`effective_target`): an hour may reach a peak the month has already paid for.
* **The margin.** The hold keeps the hour under that level less a margin (`held_level_kw`: the larger of
  `TARGET_MARGIN_KW` and `TARGET_MARGIN_SHARE` of it, never below nothing), applied once to the final level, for a
  meter that reads differently from the operator's and the hour's last minutes. What a person is shown is the level
  itself (the target as set, the month's peak); the diagnostics show both.
* **Budget.** `held level x 1 h - import so far this hour` is what the rest of the hour may still import. Spread over the
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
* **A slow charger** (`TargetMember.write_interval_s` above `SLOW_WRITE_INTERVAL_S`: a current written at most every
  few minutes, Zaptec every 15) is held by energy. While its next write must wait (`write_wait_s`) it keeps drawing
  what it draws now (`draws_a`, no more than it is set to, `set_a`: its own reading lags a write); a lowering is sized so that this wait and then the lower current still end the hour
  under the held level, and where no current at or above its minimum can do that (the wait runs past the hour's end,
  or too little time is left after it), it is paused instead (`PAUSE_SLOW_CHARGER`): a pause is a command, not a
  current, and is not held back by the interval. A raise needs no rule of its own within the hour: the share is a
  power the rest of the hour can carry, so a whole interval at it never breaks the hour. Across the hour's end it
  does: the hour-end lock starts a whole interval and a dwell before the end (`raise_lock_s`) and keeps such a charger
  where it is set, a resume included (`SiteCapacityController._hold_to_import_target`). A charger the target may not
  hold is never paused here.
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

from ..const import REGULATOR_DWELL_S
from .demand_tracker import GAP_MAX_S, HOUR_S, RunningHour

#: The nominal phase voltage a charger's amps are turned into power with (as the regulator's steps are).
HOLD_VOLTAGE_V: Final = 230.0
#: The least time left of the hour the budget is spread over: the division explodes in the last seconds.
MIN_REMAINING_S: Final = 60.0
#: In the hour's last ten minutes the target raises no charger above what it is given (`SiteCapacityController.
#: _hold_to_import_target`): what is left of the budget is spread over too little time to be worth a raise that the
#: next hour takes back, and the lowering that follows waits for the dwell, past the next hour's start.
RAISE_LOCK_S: Final = 600.0
#: A charger whose current may be written only less often than this is held by energy (`share_budget`): one written
#: at least every two dwells (a 90 s cloud charger) follows the regulator closely enough, and pausing it for its
#: wait would cost more than the few watt-hours the wait carries.
SLOW_WRITE_INTERVAL_S: Final = 2 * REGULATOR_DWELL_S
#: A slow charger's wait that ends within this of the hour's end, carrying less than `SLOW_TINY_WH` beyond its share,
#: pauses nothing (the margin takes it): no pause and resume for a last-minute blip.
SLOW_TINY_REST_S: Final = 60.0
SLOW_TINY_WH: Final = 10.0
#: Why the target pauses a charger it could otherwise hold at its minimum: its next current write comes too late
#: for the hour (diagnostics only).
PAUSE_SLOW_CHARGER: Final = "slow_charger"


def raise_lock_s(write_interval_s: float) -> float:
    """How long before the hour's end the target raises a charger it held no further (`RAISE_LOCK_S`): for a charger
    whose current may be written only every `write_interval_s`, a whole interval and a dwell, so a raise is always
    followed in the same hour by the lowering that may take it back."""
    return max(RAISE_LOCK_S, max(0.0, write_interval_s) + REGULATOR_DWELL_S)
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
class EffectiveTarget:
    """The level the running hour is held to (`effective_target`): the import target as set (`set_kw`), the month's
    free level for the running hour (`free_kw`, `None` where no demand-charge part counts it), the higher of the two
    (`kw`, what a person is shown), and that level less the margin (`hold_kw`, what the budget holds the hour under)."""

    set_kw: float
    free_kw: float | None
    kw: float

    @property
    def hold_kw(self) -> float:
        """The level less its margin, once (`held_level_kw`): what the hour is held under."""
        return held_level_kw(self.kw)

    @property
    def lifted(self) -> bool:
        """Whether the month's free level lifts the target this hour."""
        return self.kw > self.set_kw


#: How far below its level (the target as set, or the month's free level) the hold keeps the hour: a meter that reads
#: a little below the operator's, and the hour's last minutes the hold can no longer act on, must not carry the hour
#: over it (`held_level_kw`).
TARGET_MARGIN_KW: Final = 0.1
TARGET_MARGIN_SHARE: Final = 0.02


def held_level_kw(level_kw: float) -> float:
    """What the hour is held under for a level (kW): the level less the larger of `TARGET_MARGIN_KW` and
    `TARGET_MARGIN_SHARE` of it, never below nothing. Applied once, to the final level: the target as set, or the
    month's free level that lifts it."""
    return max(0.0, level_kw - max(TARGET_MARGIN_KW, TARGET_MARGIN_SHARE * level_kw))


def effective_target(set_kw: float, free_levels: Iterable[float | None]) -> EffectiveTarget:
    """The import target lifted to the month's free level: a demand charge bills the month's peaks, so an hour may
    reach what is already paid for without costing more. The free level is the lowest `free_kw`
    (`planning/demand_charge.component_month`) of the parts that count the running hour (`None` for one that does not):
    a highest-hour part's month peak; a top-N part's N-th highest day, or today's own peak while today is among them,
    and with fewer than N days today's own peak or nothing (0). An hour no part counts keeps the target. Only ever
    raises the target, to a level an hour has already reached this month (an incomplete hour never sets a peak). The
    margin is not taken here: `EffectiveTarget.hold_kw` takes it once from the level found. The caller gives the levels from each hour's floor (its import over the whole hour), never more
    than the meter billed."""
    counted = [level for level in free_levels if level is not None]
    free = min(counted) if counted else None
    if free is None:
        return EffectiveTarget(set_kw=set_kw, free_kw=None, kw=set_kw)
    return EffectiveTarget(set_kw=set_kw, free_kw=free, kw=max(set_kw, free))


@dataclass(frozen=True, slots=True)
class HourBudget:
    """The running hour against the target (see the module)."""

    #: The level a person is shown (the target as set, or the month's free level).
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
    #: What the hour is held under (kW): the level less its margin (`held_level_kw`).
    hold_kw: float = 0.0


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
    over `extra_covered_s` (`None`: no such energy is counted). `target_kw` is the level (the target as set, or the
    month's free level that lifts it); the hour is held under it less its margin (`held_level_kw`), taken here once."""
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
    hold_kw = held_level_kw(target_kw)
    target_wh = hold_kw * 1000.0
    rate_w = (target_wh - so_far_wh) * HOUR_S / remaining_s
    allowed_w = min(max(0.0, rate_w - house_w), max(0.0, ceiling_w))
    # The house alone against the level a person is shown (the line names it), the chargers held under the margin.
    house_over = so_far_wh + max(0.0, house_w) * remaining_s / HOUR_S > target_kw * 1000.0
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
            hold_kw=hold_kw,
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
    #: What it draws now (A per phase, the highest of its phases; 0 while paused).
    draws_a: float = 0.0
    #: The current it is set to (A; 0 while paused, `None` when unknown): where the hour-end lock keeps a slow charger.
    set_a: float | None = None
    #: Seconds until its current may be written again (0: now, or a charger whose current SpotNav does not write).
    write_wait_s: float = 0.0
    #: The least time between two writes of its current (its write policy's `min_interval_s`; 0 where none).
    write_interval_s: float = 0.0


@dataclass(frozen=True, slots=True)
class TargetShare:
    """What the target does to one charger: `cap_a` is the most it is given now (`None`: nothing held), `room_a` the
    whole amps the budget left when its turn came (`None` for a charger the target does not hold): no step of yield
    stepping, no resume and no probe gives it more."""

    action: str
    cap_a: float | None = None
    room_a: float | None = None
    #: Why, where a rule beyond the share itself decided (`PAUSE_SLOW_CHARGER`).
    reason: str | None = None


def hour_end_lock_a(
    member: TargetMember, *, held_cap_a: float | None, held_last_pass: bool, remaining_s: float
) -> float | None:
    """Where the hour's end keeps a charger the target held this hour (`held_cap_a`: the highest it held it to), or
    `None` while it may be raised (see the module). Within `raise_lock_s` of the hour's end: a charger held in the last
    pass is kept at its held cap; a slow one (`write_interval_s` above `RAISE_LOCK_S`) whenever it was held this
    hour, also when the budget freed it just before, and where it is set if that is lower (a raise would hold into
    the next hour), never below its minimum (a current set below it by hand is no reason to pause it)."""
    if held_cap_a is None or remaining_s >= raise_lock_s(member.write_interval_s):
        return None
    slow = member.write_interval_s > RAISE_LOCK_S
    if not (slow or held_last_pass):
        return None
    if slow and member.set_a is not None and member.set_a > 0:
        return max(member.min_a, min(held_cap_a, member.set_a))
    return held_cap_a


def member_w(phases: int, amps: float, voltage_v: float = HOLD_VOLTAGE_V) -> float:
    return max(0.0, amps) * max(1, phases) * voltage_v


@dataclass(frozen=True, slots=True)
class _SlowWait:
    """A slow charger's wait for its next write, against its share of the budget: it draws `stuck_w` for `wait_s` of
    the hour's `remaining_s` whatever it is given meanwhile."""

    stuck_w: float
    wait_s: float
    remaining_s: float

    def room_w(self, left_w: float) -> float | None:
        """What it may draw from its next write on (W), so that the wait and then this power end the hour within
        `left_w`; `None` when no power can (the wait runs past the hour's end, or takes the whole share)."""
        rest_s = self.remaining_s - self.wait_s
        if rest_s <= 1.0:
            return None
        room_w = (left_w * self.remaining_s - self.stuck_w * self.wait_s) / rest_s
        return room_w if room_w > 0 else None

    def mean_w(self, then_w: float) -> float:
        """Its mean power over the hour's rest: the wait at what it draws now, then `then_w`."""
        rest_s = max(0.0, self.remaining_s - self.wait_s)
        return (self.stuck_w * self.wait_s + then_w * rest_s) / max(1.0, self.remaining_s)


def _slow_wait(
    member: TargetMember, left_w: float, remaining_s: float | None, voltage_v: float
) -> _SlowWait | None:
    """A slow charger's wait for its next write where it matters (it draws more meanwhile than its share), else
    `None`. What it draws meanwhile is what it is set to where that is known and less than its reading: a charger's
    own reading lags a lowering just written (a cloud polled every minute or two), while the wait has begun."""
    if remaining_s is None or member.write_interval_s <= SLOW_WRITE_INTERVAL_S or member.write_wait_s <= 0:
        return None
    stuck_a = member.draws_a
    if member.set_a is not None and member.set_a > 0:
        stuck_a = min(stuck_a, member.set_a)
    stuck_w = member_w(member.phases, stuck_a, voltage_v)
    if stuck_w <= left_w + 1e-6:
        return None
    wait_s = min(member.write_wait_s, remaining_s)
    if remaining_s - wait_s <= SLOW_TINY_REST_S and (stuck_w - left_w) * wait_s / HOUR_S < SLOW_TINY_WH:
        return None
    return _SlowWait(stuck_w=stuck_w, wait_s=wait_s, remaining_s=remaining_s)


def share_budget(
    allowed_w: float,
    members: Sequence[TargetMember],
    voltage_v: float = HOLD_VOLTAGE_V,
    remaining_s: float | None = None,
) -> dict[str, TargetShare]:
    """Share `allowed_w` among `members`, in the order given (the site's `allocation_order`; see the module).
    `remaining_s` (the hour's time left) turns on the rules for slow chargers; without it none applies."""
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
        plain_room_a = float(math.floor(left / (phases * voltage_v) + 1e-9))
        # Where only a slow charger's own rules take its minimum away, the pause says so.
        slow_reason = PAUSE_SLOW_CHARGER if plain_room_a >= member.min_a and not member.switch_only else None
        wait = _slow_wait(member, left, remaining_s, voltage_v)
        room_w = left if wait is None else wait.room_w(left)
        if room_w is None:
            # Its next write comes too late for the hour: only a pause gives the car less in time.
            shares[member.charger_entry_id] = TargetShare(ACTION_PAUSED, 0.0, 0.0, slow_reason)
            continue
        room_a = float(math.floor(room_w / (phases * voltage_v) + 1e-9))
        if member.wants_a <= 0:
            shares[member.charger_entry_id] = TargetShare(ACTION_FREE, None, room_a)
            continue
        wanted_w = member_w(phases, member.wants_a, voltage_v)
        if wanted_w <= room_w + 1e-6:
            shares[member.charger_entry_id] = TargetShare(ACTION_FREE, None, room_a)
            left = max(0.0, left - (wanted_w if wait is None else wait.mean_w(wanted_w)))
            continue
        if member.switch_only or room_a < member.min_a:
            shares[member.charger_entry_id] = TargetShare(
                ACTION_PAUSED, 0.0, 0.0, None if room_a == plain_room_a else slow_reason
            )
            continue
        shares[member.charger_entry_id] = TargetShare(ACTION_HELD, room_a, room_a)
        held_w = member_w(phases, room_a, voltage_v)
        left = max(0.0, left - (held_w if wait is None else wait.mean_w(held_w)))
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


def battery_discharge_shares_w(discharge_w: float, lowered: Sequence[str]) -> dict[str, float]:
    """The battery's live discharge (W) shared out evenly among the chargers the target lowers in one pass (`lowered`),
    for `lowering_floor_a`'s `battery_discharge_w`: two cars never each count on the whole of it."""
    if discharge_w <= 0 or not math.isfinite(discharge_w) or not lowered:
        return {}
    each = discharge_w / len(lowered)
    return {charger_entry_id: each for charger_entry_id in lowered}


def lowering_floor_a(
    *,
    phases: Sequence[str],
    measured_a: Mapping[str, float | None],
    signed_power_w: Mapping[str, float | None],
    voltage_v: Mapping[str, float | None],
    margin_a: Mapping[str, float | None] | None = None,
    battery_discharge_w: float = 0.0,
    fuse_margin_a: Mapping[str, float | None] | None = None,
) -> float | None:
    """The lowest current a charger may be lowered to for the target without its lowering raising the current the
    fuse carries on any phase it draws on: each phase's net import must stay an import (`P - d x U >= 0`), so its
    magnitude only falls. Where the direction comes from a signed current (`margin_a` given) the drop also stays within
    the phase's margin, as the regulator's own decrease does (`|net - d| <= |net| + d`). `None` when a phase cannot
    be judged: nothing is then lowered for the target.

    `battery_discharge_w` (only under `battery_first`, with the home battery read live and discharging: the caller
    gives 0 otherwise): a drop of the car's power up to the battery's measured discharge only lowers that discharge,
    not the grid's phases, so the floor goes that much further down, spread evenly over the charger's phases
    (`discharge / sum of the phases' voltages`, in amps on each). The per-phase rule above still holds for the part of
    the drop the discharge does not cover. Only ever lowers the floor; a charger is never raised through it. That
    extra drop is never more than the smallest fuse margin over the charger's phases (`fuse_margin_a`, amps; one
    missing: no extra drop): should the battery not follow the car down (a fixed setpoint, one phase of a three-phase
    car, a battery at its limit), the export it leaves stays within the fuse."""
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
    if not floors:
        return None
    covered_a = 0.0
    if battery_discharge_w > 0 and math.isfinite(battery_discharge_w) and fuse_margin_a is not None:
        margins = [fuse_margin_a.get(phase) for phase in phases]
        if all(margin is not None and math.isfinite(margin) for margin in margins):
            volts = sum(voltage_v.get(phase) or HOLD_VOLTAGE_V for phase in phases)
            covered_a = battery_discharge_w / volts if volts > 0 else 0.0
            covered_a = min(covered_a, min(max(0.0, margin) for margin in margins))  # type: ignore[type-var]
    return max(floors) - covered_a
