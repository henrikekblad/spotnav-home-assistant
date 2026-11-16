"""SpotNav's own switching of the heat pump's relay, pure (plan `plans/v3_heating_plan.md` step 5, the owner's
decisions of 2026-11-16): the fixed rules (`decide`) and the check at the first start (`Check`). The shell that reads
and writes is `heat/control.py`.

The relay path only (and SG Ready's first contact): **closed** lowers the pump's curve by the shift set in the pump,
**open** is the pump's own normal curve. Open is the fail-safe state: every exit path ends there.

**The rules** (fixed, no settings), `decide`, on SpotNav's own plan (`heat/shadow_rule.band`, the running hour's
planned lowered run: `planned_run_end`):

* A lowering starts only where the plan lowers the running hour and its run ahead leaves at least `MIN_LOWER` (no
  quarter blips), at least `MIN_NORMAL` after SpotNav's last lowering ended, while the day's lowered time leaves
  `MIN_LOWER` under `MAX_DAY`, and the day's writes leave room for its end too (`MAX_WRITES_DAY`).
* A lowering lasts at least `MIN_LOWER` and at most `MAX_RUN` in a row, never past `MAX_DAY` for the day; it ends
  when the plan stops lowering once `MIN_LOWER` has passed.
* A safety reason ends a lowering at once, also before `MIN_LOWER`, and starts none: outdoors at or below
  `NO_LOWER_AT_C` (`too_cold`), a counted room below the comfort band (`room_cold`), data that is not good
  (`stale`: no fresh plan, no outdoor reading), SG Ready's second contact on (`sg_ready_b`). After data that was not
  good, a lowering starts only once it has been good for `GOOD_FOR` (`settling`).
* Pre-heat is planning only: a relay can lower, not raise.

**The check** (`Check`), before control runs, at the first start and after the relay entity changes: at a moment
when the pump heats (its meter between its running level and a hot-water run) or its calculated supply temperature
reads, `BASELINE` of readings with the relay open, then the relay closed: within `EFFECT_GRACE` the calculated supply
must fall by `SUPPLY_DROP_C`, or the meter's power to `POWER_DROP_SHARE` of the baseline (and by `POWER_DROP_MIN_KW`),
on `SEEN` readings in a row. Then the relay opens: within `RETURN_GRACE` the supply must come back within
`SUPPLY_BACK_C`, or the power to `POWER_BACK_SHARE` of the baseline. Outcomes: `passed` (on the supply, or on the
power alone on `POWER_PASSES` tries, or one where closings already measured show an effect); `failed` (no effect, or
no return, where the calculated supply reads: a curve follows its contact at once); `not_judgeable:<why>` (a
hot-water run began, the readings went, a safety reason or a block came, the power alone showed no effect or no
return or passed only once: the compressor cycles by itself): tried again at a later moment, at least `RETRY_AFTER`
apart and at most `MAX_TRIES_DAY` a day. A moment that stops suiting during the baseline is no try (nothing was written).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Final

from .shadow_rule import ACTION_LOWER, BandHour

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
    #: SpotNav's lowered time today, the running lowering's part included.
    lowered_today: timedelta
    writes_today: int
    #: A reason that ends any lowering at once and starts none (`SAFETY_*`; `settling` starts none only).
    safety: str | None = None


@dataclass(frozen=True, slots=True)
class Decision:
    closed: bool
    reason: str
    #: A lowering's latest end as the rules see it now.
    until: datetime | None = None


def decide(s: Situation) -> Decision:
    """Whether the relay is to be closed now, and why (see the module)."""
    day_left = max(timedelta(0), MAX_DAY - s.lowered_today)
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
    if s.writes_today + 2 > MAX_WRITES_DAY:
        return Decision(False, REASON_WRITE_CAP)
    return Decision(True, REASON_PLANNED, until)


def lowered_within(lowerings: Sequence[tuple[datetime, datetime | None]], start: datetime, end: datetime) -> timedelta:
    """How long the lowerings (an open one runs to `end`) lie within `[start, end)`."""
    total = timedelta(0)
    for begin, finish in lowerings:
        stop = end if finish is None else min(finish, end)
        first = max(begin, start)
        if stop > first:
            total += stop - first
    return total


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
        check.last = raw.get("last") if isinstance(raw.get("last"), str) else None
        return check

    # ---- the steps

    def may_try(self, now: datetime) -> bool:
        recent = [moment for moment in self.tries if now - moment < timedelta(days=1)]
        return len(recent) < MAX_TRIES_DAY and (not recent or now - max(recent) >= RETRY_AFTER)

    def running(self) -> bool:
        return self.phase in (PHASE_CLOSED, PHASE_OPENED)

    def _end(self, outcome: str) -> CheckStep:
        self.phase, self.phase_at, self.seen, self.samples = PHASE_WAIT, None, 0, []
        self.last = outcome
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
        self, now: datetime, sample: Sample, relay_closed: bool | None, blocked: str | None = None, *, measured: bool = False
    ) -> CheckStep:
        """One look (see the module). `relay_closed` is the relay as read now; `blocked` a reason not to run now;
        `measured` whether closings already measured on the meter show an effect (one power-only pass is enough)."""
        if blocked is not None:
            ended = self.abort(blocked)
            return ended if ended is not None else CheckStep(False)
        if self.phase == PHASE_WAIT:
            if sample.usable and relay_closed is False and self.may_try(now):
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
