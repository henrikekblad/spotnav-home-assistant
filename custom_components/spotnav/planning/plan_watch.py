"""Watching a plan's windows: plan again when the charge falls behind, and learn the car's power.

Once a minute while a window of the installed Auto plan is open (`AutoPlannerController._on_watch_due`), the energy
delivered in the window is compared with what the plan expected of it: the power it counted on
(`PlanRequest.planned_kw`) over the same stretch of time, never past the point where a plan that stops at its need
expects it met. Delivered is the count the need uses (the energy register, or the energy integrated from the
charger's power); without a count nothing is judged, as the plan itself is then the only estimate.

* **Between readings.** A register posts its value every so often, and a value holds everything delivered up to the
  moment it was posted. So both sides are counted between two posts: delivered is the later value less the earlier
  one, expected is the plan's power over the time between them (from the window's start at the earliest). A register
  that posts every five minutes is then not a car falling behind, whatever the phase of its posts. A post counts when
  the value changed or the integration wrote the same value again (`last_reported`).
* **Mid-window.** `BEHIND_KWH` or more behind, on two checks at least `CONFIRM_S` apart, plans the remainder again,
  at most once per `REPLAN_EVERY_S`. The replan counts from what was delivered, as every replan does, and may add or
  extend periods; a change to the window that is charging waits for its boundary, as always. From a replan on, the
  window is judged against the new plan: expected and delivered count from that post. A replan that left the windows
  as they were doubles the wait before the next one (up to `MAX_REPLAN_EVERY_S`).
* **Window end.** A window that delivered less than `WINDOW_SHARE` of what the plan expected of it plans again just
  after it ends, whatever held it back. It is judged once the register has posted at or after the window's end, or
  `END_WAIT_S` after it at the latest (a register that posts only when its value changes posts nothing more when
  nothing more was delivered).
* **SpotNav's own holds.** A shortfall SpotNav made itself (load balancing pausing or limiting the current, a phase
  switch, a current written below the plan's, which covers the battery band and the sun) is not planned again
  mid-window: the plan cannot know better while it lasts, which may be the next minute. Once any check in a window
  saw such a hold, that window is judged at its end only, where more time is bought for what is left.
* **Not judged.** The first `START_GRACE_S` of a window (a car waking up); a check while the charger is not charging
  or the car is ending the charge (the plan's own path for a charge that does not run, or a car that is full); a
  window in which the car ended the charge itself (its own path plans again); a check whose last post is older than
  `STALE_S`, or that has no post inside the window yet (nothing is known about the window).
* **Quiet.** A replan here is an ordinary replan: a plan that moved during a plug-in is not told again
  (`notifications/notifier.py`), so no notice comes of it. The log says it once per window (`tell`), the rest at
  debug.

Diagnostics keep the last check (`BehindCheck`).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final

#: How far behind (kWh) the charge must be, and on two checks how far apart (s), to plan again mid-window.
BEHIND_KWH: Final = 0.5
CONFIRM_S: Final = 60.0
#: The least time (s) between two replans for a charge behind mid-window, and the most it backs off to when replans
#: change nothing.
REPLAN_EVERY_S: Final = 300.0
MAX_REPLAN_EVERY_S: Final = 3600.0
#: A window that delivered less than this share of what the plan expected of it plans again at its end.
WINDOW_SHARE: Final = 0.8
#: How often (s) an open window is checked.
CHECK_S: Final = 60.0
#: The first seconds of a window are not judged: a car wakes and ramps up.
START_GRACE_S: Final = 120.0
#: A last post older than this (s) says nothing about now: no judgement.
STALE_S: Final = 600.0
#: How long (s) after a window's end its judgement waits at most for the register's next post.
END_WAIT_S: Final = 1200.0
#: A window share below this (kWh) is too little to judge at the end (a window watched for a moment).
MIN_SHARE_KWH: Final = 0.5

DECISION_ON_PLAN: Final = "on_plan"
DECISION_BEHIND: Final = "behind_unconfirmed"
DECISION_REPLAN: Final = "replan"
DECISION_RECENT: Final = "replanned_recently"
DECISION_HELD: Final = "held_by_spotnav"
DECISION_NO_COUNT: Final = "no_count"
DECISION_STALE: Final = "no_recent_reading"
DECISION_AWAITING: Final = "awaiting_reading"
DECISION_SETTLING: Final = "start_settling"
DECISION_NOT_CHARGING: Final = "not_charging"
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
    #: The count now (kWh), and when it was last posted (`None`: now).
    reading_kwh: float | None = None
    reading_at: datetime | None = None
    #: Why SpotNav holds the current back now (`balancing`, `balancing_limit`, `phases`, `current`), or `None`.
    held: str | None = None
    #: The car ended the charge in this window.
    car_ended: bool = False
    #: The charger is charging, and the car is not ending the charge.
    charging: bool = True


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
    base_kwh: float | None
    #: When the base value was posted.
    base_at: datetime
    power_kw: float
    need_met_at: datetime | None
    last_kwh: float | None = None
    held: str | None = None
    car_ended: bool = False
    behind_since: datetime | None = None

    def expected_kwh(self, until: datetime) -> float:
        """The plan's energy from the base post (the window's start at the earliest) to `until`."""
        stop = min(until, self.end if self.need_met_at is None else min(self.need_met_at, self.end))
        begin = max(self.base_at, self.start)
        return max(0.0, (stop - begin).total_seconds() / 3600.0) * self.power_kw


class PlanWatch:
    """The behind-plan rule for one charger, fed one `WindowFacts` per check (see the module docstring)."""

    def __init__(self) -> None:
        self._watched: _Watched | None = None
        #: A window that ended and waits for the register's post to be judged.
        self._ending: _Watched | None = None
        self._replanned_at: datetime | None = None
        self._every_s = REPLAN_EVERY_S
        self._told: tuple[datetime, datetime] | None = None
        self.last_check: BehindCheck | None = None

    @property
    def watching(self) -> bool:
        return self._watched is not None or self._ending is not None

    @property
    def window_end(self) -> datetime | None:
        """The end of the window watched, or of the one that ended and waits for its judgement."""
        if self._watched is not None:
            return self._watched.end
        return None if self._ending is None else self._ending.end

    def tell(self, window: tuple[datetime, datetime]) -> bool:
        """Whether a replan for this window is the first one said in the log (one line per window)."""
        if self._told == window:
            return False
        self._told = window
        return True

    def note_replan_result(self, changed: bool) -> None:
        """A replan of ours moved the windows (back to `REPLAN_EVERY_S`) or left them as they were (twice the wait)."""
        self._every_s = REPLAN_EVERY_S if changed else min(self._every_s * 2, MAX_REPLAN_EVERY_S)

    def check(self, now: datetime, facts: WindowFacts) -> str | None:
        """One check. Returns `DECISION_REPLAN` or `DECISION_END_REPLAN` when the plan is to be made again."""
        watched = self._watched
        if watched is not None and (facts.window is None or facts.window != (watched.start, watched.end)):
            self._watched = None
            if now >= watched.end:
                self._ending = watched
            # A window replaced before its end (a new plan) is simply watched anew.
        decision = self._judge_end(now, facts) if self._ending is not None else None
        if facts.window is None:
            return decision
        if self._watched is None:
            start, end = facts.window
            self._watched = _Watched(
                start=start,
                end=end,
                base_kwh=facts.reading_kwh,
                base_at=facts.reading_at or now,
                power_kw=facts.power_kw,
                need_met_at=facts.need_met_at,
                last_kwh=facts.reading_kwh,
            )
            self._every_s = REPLAN_EVERY_S
        mid = self._judge_mid(now, self._watched, facts)
        return decision or mid

    def _judge_end(self, now: datetime, facts: WindowFacts) -> str | None:
        watched = self._ending
        assert watched is not None
        posted = facts.reading_at or now
        if facts.reading_kwh is not None and posted >= watched.end:
            reading: float | None = facts.reading_kwh
        elif (now - watched.end).total_seconds() >= END_WAIT_S:
            reading = facts.reading_kwh if facts.reading_kwh is not None else watched.last_kwh
        else:
            return None  # the register has not posted since the window ended
        self._ending = None
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
        posted = facts.reading_at or now
        if reading is None or watched.base_kwh is None or reading < watched.base_kwh:
            # No count, or one that went back (a register replaced, reset): counted afresh from here.
            watched.base_kwh, watched.base_at, watched.last_kwh = reading, posted, reading
            watched.behind_since = None
            self._note(now, watched, 0.0, None, DECISION_NO_COUNT)
            return None
        watched.last_kwh = reading
        expected = watched.expected_kwh(posted)
        delivered = reading - watched.base_kwh
        unjudged = None
        if watched.car_ended:
            unjudged = DECISION_CAR_ENDED
        elif not facts.charging:
            unjudged = DECISION_NOT_CHARGING
        elif (now - watched.start).total_seconds() < START_GRACE_S:
            unjudged = DECISION_SETTLING
        elif (now - posted).total_seconds() > STALE_S:
            unjudged = DECISION_STALE
        elif posted <= max(watched.base_at, watched.start):
            unjudged = DECISION_AWAITING
        if unjudged is not None:
            watched.behind_since = None
            self._note(now, watched, expected, delivered, unjudged)
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
        if self._replanned_at is not None and (now - self._replanned_at).total_seconds() < self._every_s:
            self._note(now, watched, expected, delivered, DECISION_RECENT)
            return None
        self._replanned_at = now
        self._note(now, watched, expected, delivered, DECISION_REPLAN)
        # The new plan counts from what was delivered: the window is judged against it from this post.
        watched.base_kwh, watched.base_at, watched.behind_since = reading, posted, None
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
    judge it, and then while its judgement waits for a post), else just after the next window opens; `None` with
    nothing ahead."""
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
