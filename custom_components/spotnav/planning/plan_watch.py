"""Watching a plan's windows: plan again when the charge falls behind, and learn the car's power.

Once a minute while a window of the installed Auto plan is open (`AutoPlannerController._on_watch_tick`), the energy
delivered since the window opened is compared with what the plan expected by now: the power it counted on
(`PlanRequest.planned_kw`) times the time since the window opened, never past the point where a plan that stops at its
need expects it met. Delivered is the count the need uses (the energy register, or the energy integrated from the
charger's power); without a count nothing is judged, as the plan itself is then the only estimate.

* **Mid-window.** `BEHIND_KWH` or more behind, on two checks at least `CONFIRM_S` apart, plans the remainder again,
  at most once per `REPLAN_EVERY_S`. The replan counts from what was delivered, as every replan does, and may add or
  extend periods; a change to the window that is charging waits for its boundary, as always. From a replan on, the
  window is judged against the new plan: expected and delivered count from that moment.
* **Window end.** A window that delivered less than `WINDOW_SHARE` of what the plan expected of it plans again just
  after it ends, whatever held it back.
* **SpotNav's own holds.** A shortfall SpotNav made itself (load balancing pausing or limiting the current, a phase
  switch, a current written below the plan's, which covers the battery band and the sun) is not planned again
  mid-window: the plan cannot know better while it lasts, which may be the next minute. Once any check in a window
  saw such a hold, that window is judged at its end only, where more time is bought for what is left.
* **Not judged.** A window in which the car ended the charge itself (its own path plans again) or whose start is
  still settling (`START_GRACE_S`, a car waking up).
* **Register lag.** A register that reports every few minutes lags what the car drew. The plan's expectation is
  taken up to the moment of the reading being judged (its `last_changed`) while that is recent (`STALE_S`), else up
  to now, so a lagging register is not read as a car falling behind and a register that stopped moving is.
* **Quiet.** A replan here is an ordinary replan: a plan that moved during a plug-in is not told again
  (`notifications/notifier.py`), so no notice comes of it.

Diagnostics keep the last check (`BehindCheck`).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final

#: How far behind (kWh) the charge must be, and on two checks how far apart (s), to plan again mid-window.
BEHIND_KWH: Final = 0.5
CONFIRM_S: Final = 60.0
#: The least time (s) between two replans for a charge behind mid-window.
REPLAN_EVERY_S: Final = 300.0
#: A window that delivered less than this share of what the plan expected of it plans again at its end.
WINDOW_SHARE: Final = 0.8
#: How often (s) an open window is checked.
CHECK_S: Final = 60.0
#: The first seconds of a window are not judged: a car wakes and ramps up.
START_GRACE_S: Final = 120.0
#: A reading older than this (s) is judged against now, not against when it was taken.
STALE_S: Final = 600.0
#: A window share below this (kWh) is too little to judge at the end (a window watched for a moment).
MIN_SHARE_KWH: Final = 0.5

DECISION_ON_PLAN: Final = "on_plan"
DECISION_BEHIND: Final = "behind_unconfirmed"
DECISION_REPLAN: Final = "replan"
DECISION_RECENT: Final = "replanned_recently"
DECISION_HELD: Final = "held_by_spotnav"
DECISION_NO_COUNT: Final = "no_count"
DECISION_SETTLING: Final = "start_settling"
DECISION_CAR_ENDED: Final = "car_ended"
DECISION_END_OK: Final = "window_end_ok"
DECISION_END_REPLAN: Final = "window_end_replan"


@dataclass(frozen=True, slots=True)
class WindowFacts:
    """What one check reads: the open window and what the plan counted on, the count, and SpotNav's holds."""

    window: tuple[datetime, datetime] | None
    #: The power the plan counted on, kW.
    power_kw: float
    #: When a plan that stops at its need expects it met inside this window, else `None`.
    need_met_at: datetime | None = None
    #: The count now (kWh), and when it last changed.
    reading_kwh: float | None = None
    reading_at: datetime | None = None
    #: Why SpotNav holds the current back now (`balancing`, `balancing_limit`, `phases`, `current`), or `None`.
    held: str | None = None
    #: The car ended the charge in this window.
    car_ended: bool = False


@dataclass(frozen=True, slots=True)
class BehindCheck:
    """One check, as the diagnostics show it."""

    at: datetime
    window_start: datetime
    window_end: datetime
    expected_kwh: float
    delivered_kwh: float | None
    decision: str
    held: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "at": self.at.isoformat(),
            "window_start": self.window_start.isoformat(),
            "window_end": self.window_end.isoformat(),
            "expected_kwh": round(self.expected_kwh, 3),
            "delivered_kwh": None if self.delivered_kwh is None else round(self.delivered_kwh, 3),
            "decision": self.decision,
            "held": self.held,
        }


@dataclass(slots=True)
class _Watched:
    start: datetime
    end: datetime
    since: datetime
    base_kwh: float | None
    power_kw: float
    need_met_at: datetime | None
    last_kwh: float | None = None
    held: str | None = None
    car_ended: bool = False
    behind_since: datetime | None = None

    def expected_kwh(self, until: datetime) -> float:
        stop = min(until, self.end if self.need_met_at is None else min(self.need_met_at, self.end))
        return max(0.0, (stop - self.since).total_seconds() / 3600.0) * self.power_kw


class PlanWatch:
    """The behind-plan rule for one charger, fed one `WindowFacts` per check (see the module docstring)."""

    def __init__(self) -> None:
        self._watched: _Watched | None = None
        self._replanned_at: datetime | None = None
        self.last_check: BehindCheck | None = None

    @property
    def watching(self) -> bool:
        return self._watched is not None

    @property
    def window_end(self) -> datetime | None:
        return None if self._watched is None else self._watched.end

    def check(self, now: datetime, facts: WindowFacts) -> str | None:
        """One check. Returns `DECISION_REPLAN` or `DECISION_END_REPLAN` when the plan is to be made again."""
        decision: str | None = None
        watched = self._watched
        if watched is not None and (facts.window is None or facts.window != (watched.start, watched.end)):
            self._watched = None
            if now >= watched.end:
                decision = self._judge_end(now, watched, facts)
            # A window replaced before its end (a new plan) is simply watched anew.
        if facts.window is None:
            return decision
        if self._watched is None:
            start, end = facts.window
            self._watched = _Watched(
                start=start,
                end=end,
                since=now,
                base_kwh=facts.reading_kwh,
                power_kw=facts.power_kw,
                need_met_at=facts.need_met_at,
                last_kwh=facts.reading_kwh,
            )
        mid = self._judge_mid(now, self._watched, facts)
        return decision or mid

    def _judge_end(self, now: datetime, watched: _Watched, facts: WindowFacts) -> str | None:
        reading = facts.reading_kwh if facts.reading_kwh is not None else watched.last_kwh
        expected = watched.expected_kwh(watched.end)
        delivered = None if reading is None or watched.base_kwh is None else reading - watched.base_kwh
        if watched.car_ended or delivered is None or delivered < 0 or expected < MIN_SHARE_KWH:
            reason = DECISION_CAR_ENDED if watched.car_ended else DECISION_NO_COUNT if delivered is None else None
            if reason is not None:
                self._note(now, watched, expected, delivered, reason)
            return None
        if delivered < WINDOW_SHARE * expected:
            self._note(now, watched, expected, delivered, DECISION_END_REPLAN)
            self._replanned_at = now
            return DECISION_END_REPLAN
        self._note(now, watched, expected, delivered, DECISION_END_OK)
        return None

    def _judge_mid(self, now: datetime, watched: _Watched, facts: WindowFacts) -> str | None:
        if facts.held is not None and watched.held is None:
            watched.held = facts.held
        watched.car_ended = watched.car_ended or facts.car_ended
        reading = facts.reading_kwh
        if reading is None or watched.base_kwh is None or reading < watched.base_kwh:
            # No count, or one that went back (a register replaced, reset): counted afresh from here.
            watched.base_kwh, watched.since, watched.last_kwh = reading, now, reading
            watched.behind_since = None
            self._note(now, watched, 0.0, None, DECISION_NO_COUNT)
            return None
        watched.last_kwh = reading
        until = now
        if facts.reading_at is not None and (now - facts.reading_at).total_seconds() <= STALE_S:
            until = max(watched.since, min(now, facts.reading_at))
        expected = watched.expected_kwh(until)
        delivered = reading - watched.base_kwh
        if watched.car_ended:
            watched.behind_since = None
            self._note(now, watched, expected, delivered, DECISION_CAR_ENDED)
            return None
        if (now - watched.start).total_seconds() < START_GRACE_S:
            self._note(now, watched, expected, delivered, DECISION_SETTLING)
            return None
        if expected - delivered < BEHIND_KWH:
            watched.behind_since = None
            self._note(now, watched, expected, delivered, DECISION_ON_PLAN)
            return None
        if watched.held is not None:
            watched.behind_since = None
            self._note(now, watched, expected, delivered, DECISION_HELD)
            return None
        if watched.behind_since is None:
            watched.behind_since = now
            self._note(now, watched, expected, delivered, DECISION_BEHIND)
            return None
        if (now - watched.behind_since).total_seconds() < CONFIRM_S:
            self._note(now, watched, expected, delivered, DECISION_BEHIND)
            return None
        if self._replanned_at is not None and (now - self._replanned_at).total_seconds() < REPLAN_EVERY_S:
            self._note(now, watched, expected, delivered, DECISION_RECENT)
            return None
        self._replanned_at = now
        self._note(now, watched, expected, delivered, DECISION_REPLAN)
        # The new plan counts from what was delivered: the window is judged against it from here.
        watched.since, watched.base_kwh, watched.behind_since = now, reading, None
        return DECISION_REPLAN

    def _note(
        self, now: datetime, watched: _Watched, expected: float, delivered: float | None, decision: str
    ) -> None:
        self.last_check = BehindCheck(
            at=now,
            window_start=watched.start,
            window_end=watched.end,
            expected_kwh=expected,
            delivered_kwh=delivered,
            decision=decision,
            held=watched.held,
        )


def next_check_at(now: datetime, watch: PlanWatch, windows: list[tuple[datetime, datetime]]) -> datetime | None:
    """When to check next: in `CHECK_S` while a window is open or watched (or just after the watched one ends, to
    judge it), else just after the next window opens; `None` with nothing ahead."""
    step = timedelta(seconds=CHECK_S)
    settle = timedelta(seconds=2)
    candidates: list[datetime] = []
    if watch.window_end is not None:
        candidates.append(max(now + settle, min(now + step, watch.window_end + settle)))
    for start, end in windows:
        if start <= now < end:
            candidates.append(now + step)
        elif start > now:
            candidates.append(start + settle)
    return min(candidates) if candidates else None
