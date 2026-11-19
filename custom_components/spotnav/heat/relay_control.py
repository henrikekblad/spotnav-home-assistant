"""SpotNav's own switching of the heat pump's relay, pure (plan `plans/v3_heating_plan.md` step 5, the owner's
decisions of 2026-11-16): the fixed rules (`decide`) and the check at the first start (`Check`). The shell that reads
and writes is `heat/control.py`.

The relay path only (and SG Ready's first contact): **closed** lowers the pump's curve by the shift set in the pump,
**open** is the pump's own normal curve. Open is the fail-safe state: every exit path ends there.

**The rules** (fixed, no settings), `decide`, on SpotNav's own plan (`heat/shadow_rule.band`, the running hour's
planned lowered run: `planned_run_end`):

* A lowering starts only where the plan lowers the running hour (a dear or tight hour, or the house too warm:
  `shadow_rule.REASON_ROOM_WARM`, the same rules for all; an hour lowered for both is one lowering) and its run ahead leaves at least `MIN_LOWER` (no
  quarter blips), at least `MIN_NORMAL` after SpotNav's last lowering ended, while the lowered time of the last 24
  hours (a rolling day) leaves `MIN_LOWER` under `MAX_DAY`, and the day's writes leave room for its end too
  (`MAX_WRITES_DAY`).
* A lowering lasts at least `MIN_LOWER` and at most `MAX_RUN` in a row, never past `MAX_DAY` in 24 hours; it ends
  when the plan stops lowering once `MIN_LOWER` has passed.
* A safety reason ends a lowering at once, also before `MIN_LOWER`, and starts none: outdoors at or below
  `NO_LOWER_AT_C` (`too_cold`), a counted room below the comfort band (`room_cold`), data that is not good
  (`stale`: no fresh plan, no outdoor reading), SG Ready's second contact on (`sg_ready_b`), a hot-water boost
  someone started on a path that blocks the compressor (`hot_water`: a block may stop hot water too). After data that was not
  good, a lowering starts only once it has been good for `GOOD_FOR` (`settling`).
* Pre-heat is planning only on the relay path: one contact can lower, not raise.

**SG Ready's raise** (`decide_raise`, the second contact B, only where SG Ready has both contacts): the four states
as (A, B) are (1,0) blocked or lowered, (0,0) normal, (0,1) recommended raise (the pump raises its curve or hot water
within its own limits) and (1,1) forced raise, which SpotNav never uses. B closes only in the plan's raise hours
(`RAISE_ACTIONS`: the cheap hour before a lowered run, and the warm-up before the return), never together with A:
the shell switches through (0,0) and confirms each write before the next. A raise lasts at least `MIN_RAISE` and at
most `MAX_RAISE_RUN` in a row, at most `MAX_RAISE_DAY` in 24 hours, starts at least `MIN_NORMAL` after the last raise
ended, and leaves `RAISE_KEEPS_WRITES` of the day's writes (shared with lowering) for the lowering it comes before.
A raise block (`RAISE_*`) ends a raise at once and starts none: the house at or above the band's high edge or the
room-warm rule on, the cooler night or away (outside the return's warm-up), data that is not good, the raise's own
check failed, a contact whose state is only assumed. **The raise check** (`RaiseCheck`) watches the first raises: the calculated supply up by
`SUPPLY_RISE_C`, or the power up to `POWER_RISE_SHARE` of the baseline, within `EFFECT_GRACE`, on `SEEN` readings in
a row. The supply alone can fail it (`failed:raise_not_observed`: raising is set aside, lowering goes on); the power
alone passes on `POWER_PASSES` raises and never fails.

**The check** (`Check`), before control runs, at the first start and after the relay entity changes: at a moment
when the pump heats (its meter between its running level and a hot-water run) or its calculated supply temperature
reads, `BASELINE` of readings with the relay open, then the relay closed: within `EFFECT_GRACE` the calculated supply
must fall by `SUPPLY_DROP_C`, or the meter's power to `POWER_DROP_SHARE` of the baseline (and by `POWER_DROP_MIN_KW`),
on `SEEN` readings in a row. Then the relay opens: within `RETURN_GRACE` the supply must come back within
`SUPPLY_BACK_C`, or the power to `POWER_BACK_SHARE` of the baseline. Outcomes: `passed` (on the supply, or on the
power alone on `POWER_PASSES` tries, or one where closings already measured show an effect); `failed` (no effect, or
no return, where the calculated supply reads: a curve follows its contact at once); `not_judgeable:<why>` (a
hot-water run began, the readings went, a safety reason or a block came, the power alone showed no effect or no
return or passed only once: the compressor cycles by itself): tried again at a later moment, at most
`MAX_TRIES_DAY` a day, at least `RETRY_AFTER` apart, doubled for each try in a row without a verdict up to
`MAX_RETRY_AFTER`. A try's closing and opening count toward the day's writes (`MAX_WRITES_DAY`): none starts without
room for both. A moment that stops suiting during the baseline is no try (nothing was written).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Final

from .shadow_rule import ACTION_LOWER, ACTION_PREHEAT, ACTION_RETURN, BandHour

MIN_LOWER: Final = timedelta(minutes=45)
MAX_RUN: Final = timedelta(hours=3)
MIN_NORMAL: Final = timedelta(hours=1)
MAX_DAY: Final = timedelta(hours=6)
MAX_WRITES_DAY: Final = 6
NO_LOWER_AT_C: Final = -15.0
GOOD_FOR: Final = timedelta(minutes=10)

REASON_PLANNED: Final = "planned"
REASON_MIN_LOWER: Final = "min_lower"
REASON_NORMAL: Final = "normal"
REASON_MAX_RUN: Final = "max_run"
REASON_DAY_CAP: Final = "day_cap"
REASON_REST: Final = "rest"
REASON_TOO_SHORT: Final = "too_short"
REASON_WRITE_CAP: Final = "write_cap"
SAFETY_TOO_COLD: Final = "too_cold"
SAFETY_ROOM_COLD: Final = "room_cold"
SAFETY_STALE: Final = "stale"
SAFETY_SG_READY_B: Final = "sg_ready_b"
SAFETY_SETTLING: Final = "settling"
#: A hot-water boost someone started is on, on a path that lowers by blocking the compressor: never cut off.
SAFETY_HOT_WATER: Final = "hot_water"

BASELINE: Final = timedelta(minutes=5)
EFFECT_GRACE: Final = timedelta(minutes=20)
RETURN_GRACE: Final = timedelta(minutes=30)
SUPPLY_DROP_C: Final = 1.5
SUPPLY_BACK_C: Final = 0.5
POWER_DROP_SHARE: Final = 0.7
POWER_DROP_MIN_KW: Final = 0.15
POWER_BACK_SHARE: Final = 0.8
SEEN: Final = 2
RETRY_AFTER: Final = timedelta(hours=1)
MAX_TRIES_DAY: Final = 3
#: The longest wait between tries: each try in a row without a verdict doubles `RETRY_AFTER` up to it.
MAX_RETRY_AFTER: Final = timedelta(hours=24)
#: Power-only tries (at least `RETRY_AFTER` apart, so not on one compressor cycle) that must each show the effect and
#: its return before the check passes; one is enough where measured closings already show an effect.
POWER_PASSES: Final = 2

PHASE_WAIT: Final = "wait"
PHASE_BASELINE: Final = "baseline"
PHASE_CLOSED: Final = "closed"
PHASE_OPENED: Final = "opened"

PASSED: Final = "passed"
FAILED: Final = "failed"
NOT_JUDGEABLE: Final = "not_judgeable"

# ---- SG Ready's raise (contact B)

#: The plan's actions B is closed in: the cheap hour before a lowered run (heat bought cheap and carried into the dear
#: hours), and the warm-up before the household's return (the one raise allowed while away).
RAISE_ACTIONS: Final = frozenset({ACTION_PREHEAT, ACTION_RETURN})
#: A raise is shorter on the compressor than a block (it keeps running, a little harder), so a shorter least run than
#: `MIN_LOWER` is enough to avoid blips, and it lets a raise start part way into a pre-heat hour.
MIN_RAISE: Final = timedelta(minutes=30)
#: The pump's own limits keep a raise safe, but a long raise only stores heat the house loses again: three hours in a
#: row at most, as for lowering.
MAX_RAISE_RUN: Final = timedelta(hours=3)
#: A rolling day's raised time at most: more is no longer heat moved in time, it is just more heat.
MAX_RAISE_DAY: Final = timedelta(hours=6)
#: Writes a raise leaves of the day's `MAX_WRITES_DAY` (shared with lowering): the closing and opening of the lowering
#: that follows the pre-heat, the saving the raise is for.
RAISE_KEEPS_WRITES: Final = 2

RAISE_PLANNED: Final = "raise_planned"
RAISE_MIN: Final = "raise_min"
RAISE_NONE: Final = "raise_none"
RAISE_MAX_RUN: Final = "raise_max_run"
RAISE_DAY_CAP: Final = "raise_day_cap"
RAISE_REST: Final = "raise_rest"
RAISE_TOO_SHORT: Final = "raise_too_short"
RAISE_WRITE_CAP: Final = "raise_write_cap"
#: The house at or above the band's high edge, or the room-warm rule on: no pre-heat into a warm house.
RAISE_WARM: Final = "raise_warm"
RAISE_NIGHT: Final = "raise_night"
RAISE_AWAY: Final = "raise_away"
RAISE_STALE: Final = "raise_stale"
RAISE_SETTLING: Final = "raise_settling"
#: The raise check saw no raise on the supply: raising is set aside (lowering goes on).
RAISE_NOT_OBSERVED: Final = "raise_not_observed"
#: A lowering holds A (or B's state is not known): B waits; the two are never closed together.
RAISE_INTERLOCK: Final = "raise_interlock"
#: A contact whose state is assumed (`assumed_state`: optimistic MQTT, KNX without a state address, a template
#: switch) cannot show that it opened: no raise at all (in the diagnostics only).
RAISE_ASSUMED_STATE: Final = "raise_assumed_state"

#: The calculated supply's rise that shows a raise (°C): smaller than a block's fall, since a recommended raise moves
#: the curve less than a block does on most pumps.
SUPPLY_RISE_C: Final = 1.0
#: The power that shows a raise: this share of the baseline, and at least `POWER_RISE_MIN_KW` more.
POWER_RISE_SHARE: Final = 1.2
POWER_RISE_MIN_KW: Final = 0.15

STATE_HEATING: Final = "heating"
STATE_HOT_WATER: Final = "hot_water"


# ------------------------------------------------------------------------------------------------ the rules


def planned_run_end(plan: Sequence[BandHour], now: datetime) -> datetime | None:
    """The end of the plan's lowered run that holds `now` (its hours in a row), `None` when the running hour is not
    lowered."""
    ordered = sorted(plan, key=lambda hour: hour.start)
    end: datetime | None = None
    for hour in ordered:
        hour_end = hour.start + timedelta(hours=1)
        if end is None:
            if hour.start <= now < hour_end and hour.action == ACTION_LOWER:
                end = hour_end
        elif hour.start == end and hour.action == ACTION_LOWER:
            end = hour_end
        elif hour.start >= end:
            break
    return end


@dataclass(frozen=True, slots=True)
class Situation:
    now: datetime
    #: The end of the plan's lowered run that holds now (`planned_run_end`).
    plan_until: datetime | None
    #: Since when SpotNav's own lowering runs (`None` while the relay is open).
    held_since: datetime | None
    #: When SpotNav's last lowering ended.
    last_end: datetime | None
    #: SpotNav's lowered time over the last 24 hours, the running lowering's part included.
    lowered_24h: timedelta
    writes_today: int
    #: A reason that ends any lowering at once and starts none (`SAFETY_*`; `settling` starts none only).
    safety: str | None = None
    #: The day's writes at most on the path steered (`paths.max_writes`).
    max_writes: int = MAX_WRITES_DAY


@dataclass(frozen=True, slots=True)
class Decision:
    closed: bool
    reason: str
    #: A lowering's latest end as the rules see it now.
    until: datetime | None = None


def decide(s: Situation) -> Decision:
    """Whether the relay is to be closed now, and why (see the module)."""
    day_left = max(timedelta(0), MAX_DAY - s.lowered_24h)
    if s.held_since is not None:
        if s.safety is not None and s.safety != SAFETY_SETTLING:
            return Decision(False, s.safety)
        if day_left <= timedelta(0):
            return Decision(False, REASON_DAY_CAP)
        if s.now - s.held_since >= MAX_RUN:
            return Decision(False, REASON_MAX_RUN)
        hold_until = min(s.held_since + MAX_RUN, s.now + day_left)
        if s.plan_until is None or s.plan_until <= s.now:
            if s.now - s.held_since < MIN_LOWER:
                return Decision(True, REASON_MIN_LOWER, min(hold_until, s.held_since + MIN_LOWER))
            return Decision(False, REASON_NORMAL)
        return Decision(True, REASON_PLANNED, min(hold_until, s.plan_until))
    if s.safety is not None:
        return Decision(False, s.safety)
    if s.plan_until is None or s.plan_until <= s.now:
        return Decision(False, REASON_NORMAL)
    if s.last_end is not None and s.now - s.last_end < MIN_NORMAL:
        return Decision(False, REASON_REST)
    until = min(s.plan_until, s.now + MAX_RUN, s.now + day_left)
    if until - s.now < MIN_LOWER:
        return Decision(False, REASON_TOO_SHORT)
    if s.writes_today + 2 > s.max_writes:
        return Decision(False, REASON_WRITE_CAP)
    return Decision(True, REASON_PLANNED, until)


def closings_ahead(
    plan: Sequence[BandHour],
    now: datetime,
    *,
    held_until: datetime | None,
    last_end: datetime | None,
    lowerings: Sequence[tuple[datetime, datetime | None]],
    writes: Mapping[str, int],
    day_of: Callable[[datetime], str],
    safety: str | None = None,
    max_writes: int = MAX_WRITES_DAY,
) -> list[tuple[datetime, datetime]]:
    """The relay's closings to expect from `now` on by the rules (`decide`) on the plan as it stands: the running one
    to `held_until`, then each lowered run of the plan from the end of the rest after the last, at most `MAX_RUN` in
    a row (a longer run again after the rest), cut to what the rolling `MAX_DAY` leaves, none shorter than
    `MIN_LOWER` or past the day's `MAX_WRITES_DAY`. None while a safety reason holds (ended now, none started)."""
    if safety is not None and safety != SAFETY_SETTLING:
        return []
    out: list[tuple[datetime, datetime]] = []
    done = [(begin, end if end is not None else (held_until or now)) for begin, end in lowerings]
    counts = dict(writes)
    if held_until is not None:
        # The running one's opening is a write still to come.
        out.append((now, held_until))
        last_end = held_until
        counts[day_of(held_until)] = counts.get(day_of(held_until), 0) + 1
    runs: list[list[datetime]] = []
    for hour in sorted(plan, key=lambda item: item.start):
        end = hour.start + timedelta(hours=1)
        if hour.action != ACTION_LOWER or end <= now:
            continue
        if runs and runs[-1][1] == hour.start:
            runs[-1][1] = end
        else:
            runs.append([max(hour.start, now), end])
    for run_start, run_end in runs:
        moment = run_start
        while True:
            if last_end is not None:
                moment = max(moment, last_end + MIN_NORMAL)
            if moment >= run_end:
                break
            day_left = MAX_DAY - lowered_within(done + out, moment - timedelta(days=1), moment)
            until = min(run_end, moment + MAX_RUN, moment + max(timedelta(0), day_left))
            day = day_of(moment)
            if until - moment < MIN_LOWER or counts.get(day, 0) + 2 > max_writes:
                break
            out.append((moment, until))
            counts[day] = counts.get(day, 0) + 2
            last_end = moment = until
    return out


def lowered_within(lowerings: Sequence[tuple[datetime, datetime | None]], start: datetime, end: datetime) -> timedelta:
    """How long the lowerings (an open one runs to `end`) lie within `[start, end)`."""
    total = timedelta(0)
    for begin, finish in lowerings:
        stop = end if finish is None else min(finish, end)
        first = max(begin, start)
        if stop > first:
            total += stop - first
    return total


# ------------------------------------------------------------------------------------------------ the raise


def planned_raise_end(plan: Sequence[BandHour], now: datetime) -> datetime | None:
    """The end of the plan's raise run that holds `now` (its hours in a row whose action is in `RAISE_ACTIONS`),
    `None` when the running hour is no raise hour."""
    end: datetime | None = None
    for hour in sorted(plan, key=lambda item: item.start):
        hour_end = hour.start + timedelta(hours=1)
        if end is None:
            if hour.start <= now < hour_end and hour.action in RAISE_ACTIONS:
                end = hour_end
        elif hour.start == end and hour.action in RAISE_ACTIONS:
            end = hour_end
        elif hour.start >= end:
            break
    return end


@dataclass(frozen=True, slots=True)
class RaiseSituation:
    now: datetime
    #: The end of the plan's raise run that holds now (`planned_raise_end`).
    plan_until: datetime | None
    #: Since when SpotNav's own raise runs (`None` while B is open).
    held_since: datetime | None
    #: When SpotNav's last raise ended.
    last_end: datetime | None
    #: SpotNav's raised time over the last 24 hours, the running raise's part included.
    raised_24h: timedelta
    #: The day's writes, both contacts.
    writes_today: int
    #: A reason that ends any raise at once and starts none (`RAISE_*`; `raise_settling` starts none only).
    block: str | None = None
    #: The day's writes at most on the path steered (`paths.max_writes`).
    max_writes: int = MAX_WRITES_DAY


def decide_raise(s: RaiseSituation) -> Decision:
    """Whether B (the raise) is to be closed now, and why (see the module). The interlock with A is the shell's."""
    day_left = max(timedelta(0), MAX_RAISE_DAY - s.raised_24h)
    if s.held_since is not None:
        if s.block is not None and s.block != RAISE_SETTLING:
            return Decision(False, s.block)
        if day_left <= timedelta(0):
            return Decision(False, RAISE_DAY_CAP)
        if s.now - s.held_since >= MAX_RAISE_RUN:
            return Decision(False, RAISE_MAX_RUN)
        hold_until = min(s.held_since + MAX_RAISE_RUN, s.now + day_left)
        if s.plan_until is None or s.plan_until <= s.now:
            if s.now - s.held_since < MIN_RAISE:
                return Decision(True, RAISE_MIN, min(hold_until, s.held_since + MIN_RAISE))
            return Decision(False, RAISE_NONE)
        return Decision(True, RAISE_PLANNED, min(hold_until, s.plan_until))
    if s.block is not None:
        return Decision(False, s.block)
    if s.plan_until is None or s.plan_until <= s.now:
        return Decision(False, RAISE_NONE)
    if s.last_end is not None and s.now - s.last_end < MIN_NORMAL:
        return Decision(False, RAISE_REST)
    until = min(s.plan_until, s.now + MAX_RAISE_RUN, s.now + day_left)
    if until - s.now < MIN_RAISE:
        return Decision(False, RAISE_TOO_SHORT)
    if s.writes_today + 2 + RAISE_KEEPS_WRITES > s.max_writes:
        return Decision(False, RAISE_WRITE_CAP)
    return Decision(True, RAISE_PLANNED, until)


@dataclass
class RaiseCheck:
    """Whether the pump answers B, judged on SpotNav's own first raises (no extra writes): kept by the shell across
    restarts as `as_dict`. `result` is `passed`, `failed` (raising set aside) or `None` while not judged yet."""

    result: str | None = None
    last: str | None = None
    power_passes: int = 0
    #: While a raise is watched: since when, and the baseline from the looks before it.
    since: datetime | None = None
    baseline_kw: float | None = None
    baseline_supply: float | None = None
    seen: int = 0
    supply_seen: bool = False
    power_seen: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {"result": self.result, "last": self.last, "power_passes": self.power_passes}

    @classmethod
    def from_dict(cls, raw: Any) -> RaiseCheck:
        check = cls()
        if not isinstance(raw, dict):
            return check
        check.result = raw.get("result") if raw.get("result") in (PASSED, FAILED) else None
        check.last = raw.get("last") if isinstance(raw.get("last"), str) else None
        count = raw.get("power_passes")
        check.power_passes = count if isinstance(count, int) and not isinstance(count, bool) else 0
        return check

    @property
    def watching(self) -> bool:
        return self.since is not None

    def begin(self, now: datetime, before: Sequence[Sample]) -> None:
        """A raise was confirmed: watch it, on a baseline from the looks before it (with B open). Nothing is watched
        once judged, or without a usable baseline (`not_judgeable:no_readings`)."""
        if self.result is not None:
            return
        usable = [sample for sample in before if sample.usable and sample.state != STATE_HOT_WATER]
        kws = [sample.kw for sample in usable if sample.kw is not None and sample.state == STATE_HEATING]
        supplies = [sample.supply_c for sample in usable if sample.supply_c is not None]
        if not kws and not supplies:
            self.last = f"{NOT_JUDGEABLE}:no_readings"
            return
        self.since, self.seen, self.supply_seen, self.power_seen = now, 0, False, False
        self.baseline_kw = sum(kws) / len(kws) if kws else None
        self.baseline_supply = sum(supplies) / len(supplies) if supplies else None

    def _end(self, outcome: str) -> str:
        self.since, self.seen = None, 0
        self.last = outcome
        if verdict(outcome) in (PASSED, FAILED):
            self.result = verdict(outcome)
        return outcome

    def abort(self, why: str) -> str | None:
        """The raise ended (or B opened) before a verdict; `None` when none was watched."""
        if self.since is None:
            return None
        return self._end(f"{NOT_JUDGEABLE}:{why}")

    def step(self, now: datetime, sample: Sample) -> str | None:
        """One look while the raise holds: the outcome when the watch ended, else `None`."""
        if self.since is None:
            return None
        if sample.state == STATE_HOT_WATER:
            return self._end(f"{NOT_JUDGEABLE}:hot_water")
        effect = False
        if self.baseline_supply is not None and sample.supply_c is not None:
            self.supply_seen = True
            effect = sample.supply_c >= self.baseline_supply + SUPPLY_RISE_C
        elif self.baseline_kw is not None and sample.kw is not None:
            self.power_seen = True
            effect = sample.kw >= self.baseline_kw * POWER_RISE_SHARE and sample.kw - self.baseline_kw >= POWER_RISE_MIN_KW
        self.seen = self.seen + 1 if effect else 0
        if self.seen >= SEEN:
            if self.baseline_supply is not None and sample.supply_c is not None:
                return self._end(PASSED)
            self.power_passes += 1
            if self.power_passes >= POWER_PASSES:
                return self._end(PASSED)
            return self._end(f"{NOT_JUDGEABLE}:power_once")
        if now - self.since < EFFECT_GRACE:
            return None
        if self.supply_seen:
            # The calculated supply follows the curve at once: no rise on it is a pump that does not answer B.
            return self._end(f"{FAILED}:{RAISE_NOT_OBSERVED}")
        if self.power_seen:
            # The power alone proves little (the compressor cycles by itself): never a failure, watched again.
            return self._end(f"{NOT_JUDGEABLE}:no_effect")
        return self._end(f"{NOT_JUDGEABLE}:no_readings")


# ------------------------------------------------------------------------------------------------ the check


@dataclass(frozen=True, slots=True)
class Sample:
    """One look's readings: the meter's power now (kW), what the pump does by it (`heating`, `hot_water`, `idle`),
    and the calculated supply temperature (°C); `None` where not read."""

    kw: float | None = None
    state: str | None = None
    supply_c: float | None = None

    @property
    def usable(self) -> bool:
        return self.supply_c is not None or (self.kw is not None and self.state == STATE_HEATING)


@dataclass(frozen=True, slots=True)
class CheckStep:
    #: What the relay should be after this step.
    closed: bool
    #: `passed`, `failed` or `not_judgeable:<why>` when a try ended.
    outcome: str | None = None


@dataclass
class Check:
    """The check's state (see the module), kept by the shell across restarts as `as_dict`."""

    phase: str = PHASE_WAIT
    phase_at: datetime | None = None
    baseline_kw: float | None = None
    baseline_supply: float | None = None
    samples: list[tuple[float | None, float | None]] = field(default_factory=list)
    seen: int = 0
    tries: list[datetime] = field(default_factory=list)
    power_passes: int = 0
    #: Tries in a row that ended without a verdict (the back-off between tries).
    unjudged: int = 0
    last: str | None = None

    # ---- kept

    def as_dict(self) -> dict[str, Any]:
        return {
            "phase": self.phase,
            "phase_at": None if self.phase_at is None else self.phase_at.isoformat(),
            "baseline_kw": self.baseline_kw,
            "baseline_supply": self.baseline_supply,
            "tries": [moment.isoformat() for moment in self.tries],
            "power_passes": self.power_passes,
            "unjudged": self.unjudged,
            "last": self.last,
        }

    @classmethod
    def from_dict(cls, raw: Any) -> Check:
        """A kept check: its tries and counts; a try that was running when Home Assistant stopped starts again
        (the shell opened the relay at the start)."""
        check = cls()
        if not isinstance(raw, dict):
            return check
        for text in raw.get("tries") or []:
            try:
                check.tries.append(datetime.fromisoformat(text))
            except (TypeError, ValueError):
                continue
        count = raw.get("power_passes")
        check.power_passes = count if isinstance(count, int) and not isinstance(count, bool) else 0
        unjudged = raw.get("unjudged")
        check.unjudged = unjudged if isinstance(unjudged, int) and not isinstance(unjudged, bool) else 0
        check.last = raw.get("last") if isinstance(raw.get("last"), str) else None
        return check

    # ---- the steps

    def retry_after(self) -> timedelta:
        """The wait after the last try: `RETRY_AFTER`, doubled for each try in a row without a verdict."""
        return min(MAX_RETRY_AFTER, RETRY_AFTER * 2 ** min(self.unjudged, 8))

    def may_try(self, now: datetime) -> bool:
        recent = [moment for moment in self.tries if now - moment < timedelta(days=1)]
        if len(recent) >= MAX_TRIES_DAY:
            return False
        return not self.tries or now - max(self.tries) >= self.retry_after()

    def running(self) -> bool:
        return self.phase in (PHASE_CLOSED, PHASE_OPENED)

    def _end(self, outcome: str) -> CheckStep:
        self.phase, self.phase_at, self.seen, self.samples = PHASE_WAIT, None, 0, []
        self.last = outcome
        self.unjudged = self.unjudged + 1 if verdict(outcome) == NOT_JUDGEABLE else 0
        return CheckStep(False, outcome)

    def abort(self, why: str) -> CheckStep | None:
        """End a try for a reason outside it (the relay is opened); `None` when none was running."""
        if self.phase == PHASE_BASELINE:
            self.phase, self.phase_at, self.samples = PHASE_WAIT, None, []
            return None
        if not self.running():
            return None
        return self._end(f"{NOT_JUDGEABLE}:{why}")

    def step(
        self,
        now: datetime,
        sample: Sample,
        relay_closed: bool | None,
        blocked: str | None = None,
        *,
        measured: bool = False,
        may_start: bool = True,
    ) -> CheckStep:
        """One look (see the module). `relay_closed` is the relay as read now; `blocked` a reason not to run now;
        `measured` whether closings already measured on the meter show an effect (one power-only pass is enough);
        `may_start` whether the day's writes leave room for a try's closing and opening."""
        if blocked is not None:
            ended = self.abort(blocked)
            return ended if ended is not None else CheckStep(False)
        if self.phase == PHASE_WAIT:
            if may_start and sample.usable and relay_closed is False and self.may_try(now):
                self.phase, self.phase_at, self.samples = PHASE_BASELINE, now, [(sample.kw, sample.supply_c)]
            return CheckStep(False)
        assert self.phase_at is not None
        if self.phase == PHASE_BASELINE:
            if not sample.usable or relay_closed is not False:
                self.phase, self.phase_at, self.samples = PHASE_WAIT, None, []
                return CheckStep(False)
            self.samples.append((sample.kw, sample.supply_c))
            if now - self.phase_at < BASELINE:
                return CheckStep(False)
            kws = [kw for kw, _supply in self.samples if kw is not None]
            supplies = [supply for _kw, supply in self.samples if supply is not None]
            self.baseline_kw = sum(kws) / len(kws) if kws else None
            self.baseline_supply = sum(supplies) / len(supplies) if supplies else None
            self.phase, self.phase_at, self.seen, self.samples = PHASE_CLOSED, now, 0, []
            self.tries.append(now)
            self.tries = [moment for moment in self.tries if now - moment < timedelta(days=2)]
            return CheckStep(True)
        if self.phase == PHASE_CLOSED:
            if sample.state == STATE_HOT_WATER:
                return self._end(f"{NOT_JUDGEABLE}:hot_water")
            self.samples.append((sample.kw, sample.supply_c))
            self.seen = self.seen + 1 if relay_closed is True and self._effect(sample) else 0
            if self.seen >= SEEN:
                self.phase, self.phase_at, self.seen, self.samples = PHASE_OPENED, now, 0, []
                return CheckStep(False)
            if now - self.phase_at < EFFECT_GRACE:
                return CheckStep(True)
            if self.baseline_supply is not None and any(supply is not None for _kw, supply in self.samples):
                return self._end(f"{FAILED}:no_effect")
            if self.baseline_kw is not None and any(kw is not None for kw, _supply in self.samples):
                # The power alone proves little (the compressor cycles by itself): never a failure, tried again.
                return self._end(f"{NOT_JUDGEABLE}:no_effect")
            return self._end(f"{NOT_JUDGEABLE}:no_readings")
        # PHASE_OPENED
        self.seen = self.seen + 1 if relay_closed is False and self._back(sample) else 0
        if self.seen >= SEEN:
            if self.baseline_supply is not None and sample.supply_c is not None:
                return self._end(PASSED)
            self.power_passes += 1
            if self.power_passes >= POWER_PASSES or measured:
                return self._end(PASSED)
            return self._end(f"{NOT_JUDGEABLE}:power_once")
        if now - self.phase_at < RETURN_GRACE:
            return CheckStep(False)
        if self.baseline_supply is not None and sample.supply_c is not None:
            return self._end(f"{FAILED}:no_return")
        return self._end(f"{NOT_JUDGEABLE}:no_return")

    def _effect(self, sample: Sample) -> bool:
        if self.baseline_supply is not None and sample.supply_c is not None:
            # The supply follows the contact at once: where it reads, it alone is judged.
            return sample.supply_c <= self.baseline_supply - SUPPLY_DROP_C
        if self.baseline_kw is not None and sample.kw is not None:
            return sample.kw <= self.baseline_kw * POWER_DROP_SHARE and self.baseline_kw - sample.kw >= POWER_DROP_MIN_KW
        return False

    def _back(self, sample: Sample) -> bool:
        if self.baseline_supply is not None and sample.supply_c is not None:
            return sample.supply_c >= self.baseline_supply - SUPPLY_BACK_C
        if self.baseline_kw is not None and sample.kw is not None:
            return sample.kw >= self.baseline_kw * POWER_BACK_SHARE
        return False


def verdict(outcome: str | None) -> str | None:
    """`passed`, `failed` or `not_judgeable` from an outcome code."""
    return None if outcome is None else outcome.split(":", 1)[0]
