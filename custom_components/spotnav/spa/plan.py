"""A deadline heat load's shadow planner: the spa and the sauna (spec section 5.2 and the owner's decision of 2026-11-23,
"the sauna is built together with the spa"). Pure: no Home Assistant import, aware datetimes from the caller.

One shape for both: sessions at a time (weekly ones on their weekdays at a local time, and one one-off date), a target
temperature, a learnt rate of heating, and a "now" press. What differs is configuration (`Profile`):

* **The spa** is held at its idle temperature between baths (never below its floor: under the floor it heats at once,
  whatever the price), heats up in the cheapest or sunniest quarter hours of a bounded window before the bath
  (`[bath − 2·need − 1 h, bath]`, so an early heat-up loses little), and is held at the bath temperature to `HOLD`
  after it.
* **The sauna** is off between sessions (no idle, no floor), heats up in one block ending at the session (a sauna cools
  in minutes, so there is nothing to move), and is held to `HOLD` after it. It is never started but for a session the
  person booked or a "now" press.

**The rate** (`learnt_rate`): the median of the last `LEARN_RUNS` heating runs that rose at least `LEARN_MIN_C`, the first
`FIRST_STEP_C` after the heater came on not counted (MSpa's lesson: the reading jumps as the water at the sensor turns
over); the profile's seed before that. **Not ready** (`NotReady`): the session cannot be met because the time left is too
short (`rate`), the hours with room under the import target or the power level are too few (`import_target`,
`power_level`), or under the main fuse (`fuse`); with the temperature it reaches, what is short and the earliest later
whole local hour where it fits (at most `LATER_TRIES`). A session the person took "what fits" for, or allowed the peak
for, is planned so and not reported again.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any, Final
from zoneinfo import ZoneInfo

from .config import Bath

QUARTER: Final = timedelta(minutes=15)
HOUR: Final = timedelta(hours=1)
#: Sessions further ahead than this are not planned yet.
HORIZON: Final = timedelta(hours=36)
#: Held at the session's temperature this long after it (placeholder).
HOLD: Final = timedelta(hours=2)
LEARN_RUNS: Final = 10
LEARN_MIN_C: Final = 1.0
FIRST_STEP_C: Final = 0.5
LATER_TRIES: Final = 48
#: A reading this close under the target is ready.
READY_MARGIN_C: Final = 0.5

KIND_SPA: Final = "spa"
KIND_SAUNA: Final = "sauna"

SOURCE_WEEKLY: Final = "weekly"
SOURCE_ONCE: Final = "once"
SOURCE_NOW: Final = "now"

STATE_IDLE: Final = "idle"
STATE_HEATING: Final = "heating"
STATE_READY: Final = "ready"
STATE_LATE: Final = "late"

ACTION_HEAT: Final = "heat"
ACTION_HOLD: Final = "hold"
ACTION_IDLE: Final = "idle"
ACTION_OFF: Final = "off"
ACTION_FLOOR: Final = "floor"

REASON_SUN: Final = "sun"
REASON_CHEAP: Final = "cheap"
REASON_BATH: Final = "bath"
REASON_NOW: Final = "now"
REASON_FLOOR: Final = "floor"
REASON_WAITING: Final = "waiting"
REASON_NO_SESSION: Final = "no_session"

CAUSE_RATE: Final = "rate"
CAUSE_IMPORT_TARGET: Final = "import_target"
CAUSE_POWER_LEVEL: Final = "power_level"
CAUSE_FUSE: Final = "fuse"


@dataclass(frozen=True, slots=True)
class Profile:
    """What makes a deadline heat load a spa or a sauna."""

    kind: str
    #: The learnt rate's seed (°C per hour); for the sauna from its seed heat-up time.
    seed_rate_c_h: float
    #: Heat up in one block ending at the session (the sauna) rather than in the cheapest quarter hours (the spa).
    one_block: bool
    #: Held between sessions (the spa's idle temperature); off between them (the sauna).
    idles: bool


SPA: Final = Profile(KIND_SPA, seed_rate_c_h=1.5, one_block=False, idles=True)
#: The sauna's seed: 45 minutes from a room's 20 °C to 80 °C (placeholder).
SEED_SAUNA_HEATUP_MIN: Final = 45
SAUNA: Final = Profile(KIND_SAUNA, seed_rate_c_h=(80.0 - 20.0) / (SEED_SAUNA_HEATUP_MIN / 60.0), one_block=True, idles=False)


def sessions_ahead(
    weekly: Sequence[Bath], once: datetime | None, now: datetime, tz: str, *, horizon: timedelta = HORIZON
) -> list[tuple[datetime, str]]:
    """The sessions from now to `horizon`: each weekly one's next times on its weekdays and the one-off, in time order."""
    zone = ZoneInfo(tz)
    local_now = now.astimezone(zone)
    found: list[tuple[datetime, str]] = []
    for offset in range(0, math.ceil(horizon / timedelta(days=1)) + 2):
        day: date = local_now.date() + timedelta(days=offset)
        for bath in weekly:
            if day.weekday() not in bath.weekdays:
                continue
            minutes = bath.minute_of_day()
            moment = datetime.combine(day, time(minutes // 60, minutes % 60), zone)
            if now < moment <= now + horizon:
                found.append((moment, SOURCE_WEEKLY))
    if once is not None and now < once <= now + horizon:
        found.append((once, SOURCE_ONCE))
    return sorted(set(found))


def learnt_rate(runs: Sequence[tuple[datetime, datetime, float, float]], seed: float) -> tuple[float, bool]:
    """The rate learnt from the heating runs `(start, end, from_c, to_c)` (see the module), and whether it is learnt."""
    rates = []
    for start, end, from_c, to_c in runs:
        rise = to_c - from_c - FIRST_STEP_C
        hours = (end - start).total_seconds() / 3600.0
        if to_c - from_c >= LEARN_MIN_C and rise > 0 and hours > 0:
            rates.append(rise / hours)
    rates = rates[-LEARN_RUNS:]
    if not rates:
        return seed, False
    return round(statistics.median(rates), 3), True


@dataclass(frozen=True, slots=True)
class HeatHour:
    start: datetime
    buy: float
    sell: float
    #: The sun's surplus over the house (kW), the room under the ceiling (kW) and which ceiling it is.
    surplus_kw: float | None = None
    room_kw: float | None = None
    ceiling: str | None = None


@dataclass(frozen=True, slots=True)
class DeadlineInputs:
    now: datetime
    tz: str
    profile: Profile
    target_c: float
    #: The temperature now (°C), `None` while it cannot be read.
    current_c: float | None
    rate_c_h: float
    kw: float
    #: The sessions ahead `(at, source)` (`sessions_ahead`), the "now" press until when, and the priced hours.
    sessions: Sequence[tuple[datetime, str]]
    hours: Sequence[HeatHour]
    now_until: datetime | None = None
    #: The spa's idle and floor (°C); `None` for the sauna.
    hold_c: float | None = None
    floor_c: float | None = None
    #: Sessions the person took what fits for, allowed the peak for (by their time), and one moved later `{from: to}`.
    took_what_fits: frozenset[datetime] = frozenset()
    accept_peak: frozenset[datetime] = frozenset()
    later: Mapping[datetime, datetime] = field(default_factory=dict)
    #: The device's own longest run (the sauna's on time), `None` where it does not say.
    max_on: timedelta | None = None


@dataclass(frozen=True, slots=True)
class NotReady:
    at: datetime
    cause: str
    reach_c: float
    short_c: float
    short_kwh: float
    later: datetime | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "at": self.at.isoformat(),
            "cause": self.cause,
            "reach_c": round(self.reach_c, 1),
            "short_c": round(self.short_c, 1),
            "short_kwh": round(self.short_kwh, 2),
            "later": None if self.later is None else self.later.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class DeadlinePlan:
    made_at: datetime
    #: The next session `(at, source)` and the quarter hours planned to heat for it, with their reason.
    next_session: tuple[datetime, str] | None
    quarters: Mapping[datetime, str]
    #: Held at the target from the reach to `hold_until`.
    hold_until: datetime | None
    need_h: float
    ready_at: datetime | None
    state: str
    action: str
    reason: str
    until: datetime | None
    not_ready: NotReady | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "made_at": self.made_at.isoformat(),
            "next_session": None if self.next_session is None else [self.next_session[0].isoformat(), self.next_session[1]],
            "quarters": {start.isoformat(): why for start, why in sorted(self.quarters.items())},
            "hold_until": None if self.hold_until is None else self.hold_until.isoformat(),
            "need_h": self.need_h,
            "ready_at": None if self.ready_at is None else self.ready_at.isoformat(),
            "state": self.state,
            "action": self.action,
            "reason": self.reason,
            "until": None if self.until is None else self.until.isoformat(),
            "not_ready": None if self.not_ready is None else self.not_ready.as_dict(),
        }


def _quarter(moment: datetime) -> datetime:
    return moment.replace(minute=moment.minute - moment.minute % 15, second=0, microsecond=0)


def _hour_of(hours: Sequence[HeatHour], moment: datetime) -> HeatHour | None:
    return next((hour for hour in hours if hour.start <= moment < hour.start + HOUR), None)


def _cost(hour: HeatHour, kw: float) -> tuple[float, str]:
    if hour.surplus_kw is not None and hour.surplus_kw >= kw:
        return hour.sell, REASON_SUN
    return hour.buy, REASON_CHEAP


def _usable(hour: HeatHour | None, kw: float, *, peak: bool) -> tuple[bool, str | None]:
    """Whether the hour has room for the load, and the ceiling that keeps it out (an allowed peak lifts the import
    target and the power level, never the fuse)."""
    if hour is None or hour.room_kw is None or hour.room_kw >= kw:
        return True, None
    if peak and hour.ceiling in (CAUSE_IMPORT_TARGET, CAUSE_POWER_LEVEL):
        return True, None
    return False, hour.ceiling or CAUSE_FUSE


def _fits(inputs: DeadlineInputs, start: datetime, end: datetime, quarters_needed: int, *, peak: bool) -> tuple[int, str | None]:
    """The quarter hours with room in `[start, end)` and the ceiling that kept the others out."""
    found, cause = 0, None
    moment = _quarter(start)
    while moment < end:
        ok, why = _usable(_hour_of(inputs.hours, moment), inputs.kw, peak=peak)
        if ok:
            found += 1
        elif cause is None:
            cause = why
        moment += QUARTER
    return found, cause


def plan_deadline(inputs: DeadlineInputs) -> DeadlinePlan:
    """The heat-up for the next session, what it would do now and whether it will be ready (see the module)."""
    now = inputs.now
    profile = inputs.profile
    target = inputs.target_c
    current = inputs.current_c
    sessions = [(inputs.later.get(at, at), source) for at, source in inputs.sessions]
    if inputs.now_until is not None and inputs.now_until > now:
        sessions.insert(0, (now, SOURCE_NOW))
    sessions.sort(key=lambda item: item[0])
    next_session = sessions[0] if sessions else None
    rate = max(0.1, inputs.rate_c_h)
    start_c = current if current is not None else (inputs.hold_c if inputs.hold_c is not None else 20.0)
    need_h = max(0.0, (target - start_c) / rate)
    needed = math.ceil(need_h * 4 - 1e-9)
    first = _quarter(now)
    quarters: dict[datetime, str] = {}
    not_ready: NotReady | None = None
    hold_until: datetime | None = None
    if next_session is not None:
        at, source = next_session
        hold_until = (now if source == SOURCE_NOW else at) + HOLD
        if source == SOURCE_NOW and inputs.now_until is not None:
            hold_until = inputs.now_until
        peak = at in inputs.accept_peak
        if source == SOURCE_NOW:
            # At once, at any price; only the fuse comes first.
            moment = first
            for _index in range(needed):
                quarters[moment] = REASON_NOW
                moment += QUARTER
        elif profile.one_block:
            moment = _quarter(at - timedelta(hours=need_h))
            while moment < at:
                if moment >= first:
                    quarters[moment] = REASON_BATH
                moment += QUARTER
        else:
            window_start = max(first, _quarter(at - timedelta(hours=2 * need_h + 1)))
            candidates = []
            moment = window_start
            while moment < at:
                hour = _hour_of(inputs.hours, moment)
                ok, _why = _usable(hour, inputs.kw, peak=peak)
                if ok:
                    cost, why = (math.inf, REASON_BATH) if hour is None else _cost(hour, inputs.kw)
                    # Later rather than earlier at the same price: less to hold.
                    candidates.append((cost, -moment.timestamp(), moment, why))
                moment += QUARTER
            for _cost_value, _later, moment, why in sorted(candidates)[:needed]:
                quarters[moment] = why
        if inputs.max_on is not None and quarters:
            hold_until = min(hold_until, min(quarters) + inputs.max_on)
        if needed > 0 and source != SOURCE_NOW and at not in inputs.took_what_fits:
            available, cause = _fits(inputs, max(now, at - timedelta(hours=need_h)) if profile.one_block else now, at, needed, peak=peak)
            time_left = int((at - first) / QUARTER)
            if time_left < needed or available < needed:
                usable = min(available, time_left)
                reach = min(target, start_c + rate * usable / 4.0)
                why = CAUSE_RATE if time_left < needed else (cause or CAUSE_FUSE)
                later = None
                zone = ZoneInfo(inputs.tz)
                candidate = at.astimezone(zone).replace(minute=0, second=0, microsecond=0) + HOUR
                for _try in range(LATER_TRIES):
                    have, _c = _fits(inputs, now, candidate, needed, peak=peak)
                    if have >= needed and int((candidate - first) / QUARTER) >= needed:
                        later = candidate
                        break
                    candidate += HOUR
                not_ready = NotReady(at, why, reach, target - reach, (target - reach) * inputs.kw / rate, later)
    # Now.
    if inputs.floor_c is not None and current is not None and current <= inputs.floor_c:
        action, reason, until = ACTION_FLOOR, REASON_FLOOR, None
    elif first in quarters:
        action, reason = ACTION_HEAT, quarters[first]
        moment = first
        while moment in quarters:
            moment += QUARTER
        until = moment
    elif (
        next_session is not None and hold_until is not None and quarters and min(quarters) <= now < hold_until
    ) or (next_session is not None and next_session[1] == SOURCE_NOW):
        action, reason, until = ACTION_HOLD, REASON_BATH, hold_until
    else:
        action = ACTION_IDLE if profile.idles else ACTION_OFF
        reason = REASON_WAITING if next_session is not None else REASON_NO_SESSION
        until = min(quarters) if quarters else None
    # The state and when it is ready.
    if current is not None and current >= target - READY_MARGIN_C and action in (ACTION_HEAT, ACTION_HOLD):
        state = STATE_READY
    elif action in (ACTION_HEAT, ACTION_FLOOR) or (action == ACTION_HOLD and current is not None and current < target - READY_MARGIN_C):
        state = STATE_HEATING
    else:
        state = STATE_IDLE
    if next_session is not None and next_session[1] != SOURCE_NOW and now > next_session[0] and state != STATE_READY:
        state = STATE_LATE
    ready_at: datetime | None = None
    if state == STATE_HEATING and current is not None:
        ready_at = now + timedelta(hours=max(0.0, target - current) / rate)
    elif state == STATE_READY:
        ready_at = now
    elif quarters:
        ready_at = max(quarters) + QUARTER
    return DeadlinePlan(
        made_at=now,
        next_session=next_session,
        quarters=quarters,
        hold_until=hold_until,
        need_h=round(need_h, 2),
        ready_at=ready_at,
        state=state,
        action=action,
        reason=reason,
        until=until,
        not_ready=not_ready,
    )
