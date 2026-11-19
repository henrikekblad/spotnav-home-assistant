"""A charge the plan expected that is not happening: the one judgement behind `plan_stopped`.

Pure: no Home Assistant imports, the clock is passed in. `UnexpectedStopDetector.observe` is given one
`ExpectationFacts` per observation and answers with the reason to notify, once, or `None`.

Expected means the controller's `plan_expects_charge`: a window of the installed plan is open now and
nothing has taken the charge away on purpose. So none of these is ever a notification:

* a window's planned end, or the plan ending (target reached, energy delivered, the plan cleared);
* a person's Stop in this window, Auto paused, solar or hybrid running the charger;
* load balancing pausing the charge for want of headroom (its own status line says so);
* the car unplugged, or a charger that can tell whether a car is there saying nothing either way (a
  fault, an offline charger, a Wallbox's `Ready`) before anything charged in the window: a start that
  was not taken may simply be no car;
* a car that takes no current because it sits at its own charge limit, below the plan's target.

While a charge is expected, it is in trouble when the charger is unavailable, or it is not charging, or
the vehicle is not taking current (`charge_progress.py`). Trouble must last `GRACE_S` (longer than a
Start the charger has not answered yet may take; nothing is told while one is unanswered), and it is
told once while it lasts, whatever its reason turns into: a charge that recovers and stops again is
told again, after the notifier's own rate limit. The notifier keeps the open window's judgement across a restart
(`window_state`, `restore`), so a restart neither tells it twice nor loses that the window had charged.

A pause SpotNav did not command after the charge ran in the window (an aggregator's, `outside_pause.py`) is given
`outside_pause.MAX_S` instead of `GRACE_S`: it is counted, and told only if it lasts longer than that.

Reasons, first match:

* `charger_unavailable`: the charge control cannot be read or reached;
* `charger_disabled`: the charger's own enable switch is off;
* `held_by_charger`: the charger's own scheduler or load balancer holds the charge;
* `vehicle_not_requesting`: started, and the vehicle takes no current;
* `stopped`: it charged in this window and stopped, by something other than SpotNav;
* `not_started`: it has not charged in this window (the start failed or was not taken).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final

from .outside_pause import MAX_S as OUTSIDE_PAUSE_MAX_S, OutsidePauses, PauseEnd

#: How long trouble must last before it is told: past an unanswered Start (30 s) and a charger's slow
#: report, and short enough to matter for a window of an hour.
GRACE_S: Final = 180.0

REASON_CHARGER_UNAVAILABLE: Final = "charger_unavailable"
REASON_CHARGER_DISABLED: Final = "charger_disabled"
REASON_HELD_BY_CHARGER: Final = "held_by_charger"
REASON_VEHICLE_NOT_REQUESTING: Final = "vehicle_not_requesting"
REASON_STOPPED: Final = "stopped"
REASON_NOT_STARTED: Final = "not_started"


@dataclass(frozen=True, slots=True)
class ExpectationFacts:
    now: datetime
    #: `ChargingController.plan_expects_charge`.
    expected: bool
    #: What identifies the window open now (the plan and the window's start), `None` outside one.
    window: str | None
    charging: bool
    available: bool = True
    #: An accepted Start the charger has not answered yet.
    start_pending: bool = False
    vehicle_not_requesting: bool = False
    held_by_charger: bool = False
    charger_disabled: bool = False
    #: The charger can tell whether a vehicle is there, and says nothing either way now.
    vehicle_unknown: bool = False
    #: A target plan's car is at its own charge limit, so it rightly takes no current.
    at_vehicle_limit: bool = False


class UnexpectedStopDetector:
    """Turns successive `ExpectationFacts` into at most one reason per stretch of trouble."""

    def __init__(self, grace_s: float = GRACE_S, pause_grace_s: float = OUTSIDE_PAUSE_MAX_S) -> None:
        self._grace_s = grace_s
        self._pause_grace_s = pause_grace_s
        # The grace of the trouble now: `pause_grace_s` while it is an outside pause.
        self._current_grace_s = grace_s
        self.pauses = OutsidePauses()
        self._pause_ends: list[PauseEnd] = []
        # This stretch of trouble outlasted an outside pause's grace: it is plain trouble until the charge recovers.
        self._pause_spent = False
        self._window: str | None = None
        self._charged_in_window = False
        self._trouble_since: datetime | None = None
        self._told = False

    @property
    def window_state(self) -> dict[str, Any] | None:
        """What a restart must not lose of the window open now: whether it charged (the reason a stop is told
        with) and whether its trouble was told (not told twice). `None` outside a window or before either."""
        if self._window is None or not (self._charged_in_window or self._told):
            return None
        return {"window": self._window, "charged": self._charged_in_window, "told": self._told}

    def restore(self, state: Any) -> None:
        """Take back a `window_state` kept across a restart; anything that is not one is ignored. Its window
        must still be the one open for it to count (`observe` starts afresh otherwise)."""
        if not isinstance(state, dict) or not isinstance(state.get("window"), str):
            return
        self._window = state["window"]
        self._charged_in_window = state.get("charged") is True
        self._told = state.get("told") is True
        self._trouble_since = None

    @property
    def due_at(self) -> datetime | None:
        """When trouble seen now would be told, so a caller can look again then; `None` without one."""
        if self._trouble_since is None:
            return None
        return self._trouble_since + timedelta(seconds=self._current_grace_s)

    def take_pause_ends(self) -> list[PauseEnd]:
        """The outside pauses that ended since the last call (for the log)."""
        ends, self._pause_ends = self._pause_ends, []
        return ends

    def _end_pause(self, now: datetime, outcome: str) -> None:
        ended = self.pauses.end(now, outcome)  # type: ignore[arg-type]
        if ended is not None:
            self._pause_ends.append(ended)

    def observe(self, facts: ExpectationFacts) -> str | None:
        self._current_grace_s = self._grace_s
        if facts.window != self._window:
            self._end_pause(facts.now, "plan_moved_on")
            self._pause_spent = False
            self._window = facts.window
            self._charged_in_window = False
            self._trouble_since = None
            self._told = False
        if not facts.expected or facts.window is None:
            self._end_pause(facts.now, "plan_moved_on")
            self._pause_spent = False
            self._trouble_since = None
            return None
        not_requesting = facts.vehicle_not_requesting and not facts.at_vehicle_limit
        healthy = facts.available and facts.charging and not not_requesting
        if facts.available and not facts.charging and facts.vehicle_unknown and not self._charged_in_window:
            # Perhaps no car at all: not a start that failed.
            self._trouble_since = None
            return None
        if healthy:
            self._end_pause(facts.now, "resumed")
            self._pause_spent = False
            self._charged_in_window = True
            self._trouble_since = None
            # Told once per trouble: a recovery lets the next one be told.
            self._told = False
            return None
        if self._trouble_since is None:
            self._trouble_since = facts.now
        outside_pause = (
            self._charged_in_window
            and facts.available
            and not facts.charger_disabled
            and (facts.held_by_charger or not not_requesting)
            and not self._pause_spent
        )
        if outside_pause:
            self.pauses.begin(self._trouble_since, "charger_held" if facts.held_by_charger else "stopped")
            self._current_grace_s = self._pause_grace_s
        elif not self._pause_spent:
            # Other trouble (the charger went away, the car declines): no aggregator's pause any more.
            self._end_pause(facts.now, "other_trouble")
        if facts.start_pending and facts.available:
            # The clock runs (the grace period is longer than an unanswered Start lasts), nothing is told.
            return None
        if (facts.now - self._trouble_since).total_seconds() < self._current_grace_s:
            return None
        if outside_pause:
            self._end_pause(facts.now, "too_long")
            self._pause_spent = True
        if self._told:
            return None
        self._told = True
        return self._reason(facts)

    def _reason(self, facts: ExpectationFacts) -> str:
        if not facts.available:
            return REASON_CHARGER_UNAVAILABLE
        if facts.charger_disabled:
            return REASON_CHARGER_DISABLED
        if facts.held_by_charger:
            return REASON_HELD_BY_CHARGER
        if facts.vehicle_not_requesting and not facts.at_vehicle_limit:
            return REASON_VEHICLE_NOT_REQUESTING
        return REASON_STOPPED if self._charged_in_window else REASON_NOT_STARTED
