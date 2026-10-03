"""A charge the plan expected that is not happening: the one judgement behind `plan_stopped`.

Pure: no Home Assistant imports, the clock is passed in. `UnexpectedStopDetector.observe` is given one
`ExpectationFacts` per observation and answers with the reason to notify, once, or `None`.

Expected means the controller's `plan_expects_charge`: a window of the installed plan is open now and
nothing has taken the charge away on purpose. So none of these is ever a notification:

* a window's planned end, or the plan ending (target reached, energy delivered, the plan cleared);
* a person's Stop in this window, Auto paused, solar or hybrid running the charger;
* load balancing pausing the charge for want of headroom (its own status line says so);
* the car unplugged.

While a charge is expected, it is in trouble when the charger is unavailable, or it is not charging, or
the vehicle is not taking current (`charge_progress.py`). Trouble must last `GRACE_S` (longer than a
Start the charger has not answered yet may take; nothing is told while one is unanswered), and it is
told once while it lasts, whatever its reason turns into: a charge that recovers and stops again is
told again, after the notifier's own rate limit.

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
from typing import Final

#: How long trouble must last before it is told: past an unanswered Start (30 s) and a charger's slow
#: report, and short enough to matter for a window of an hour.
GRACE_S: Final = 180.0

REASON_CHARGER_UNAVAILABLE: Final = "charger_unavailable"
REASON_CHARGER_DISABLED: Final = "charger_disabled"
REASON_HELD_BY_CHARGER: Final = "held_by_charger"
REASON_VEHICLE_NOT_REQUESTING: Final = "vehicle_not_requesting"
REASON_STOPPED: Final = "stopped"
REASON_NOT_STARTED: Final = "not_started"
STOP_REASONS: Final = (
    REASON_CHARGER_UNAVAILABLE,
    REASON_CHARGER_DISABLED,
    REASON_HELD_BY_CHARGER,
    REASON_VEHICLE_NOT_REQUESTING,
    REASON_STOPPED,
    REASON_NOT_STARTED,
)


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


class UnexpectedStopDetector:
    """Turns successive `ExpectationFacts` into at most one reason per stretch of trouble."""

    def __init__(self, grace_s: float = GRACE_S) -> None:
        self._grace_s = grace_s
        self._window: str | None = None
        self._charged_in_window = False
        self._trouble_since: datetime | None = None
        self._told = False

    @property
    def due_at(self) -> datetime | None:
        """When trouble seen now would be told, so a caller can look again then; `None` without one."""
        if self._trouble_since is None:
            return None
        return self._trouble_since + timedelta(seconds=self._grace_s)

    def observe(self, facts: ExpectationFacts) -> str | None:
        if facts.window != self._window:
            self._window = facts.window
            self._charged_in_window = False
            self._trouble_since = None
            self._told = False
        if not facts.expected or facts.window is None:
            self._trouble_since = None
            return None
        healthy = facts.available and facts.charging and not facts.vehicle_not_requesting
        if healthy:
            self._charged_in_window = True
            self._trouble_since = None
            # Told once per trouble: a recovery lets the next one be told.
            self._told = False
            return None
        if self._trouble_since is None:
            self._trouble_since = facts.now
        if facts.start_pending and facts.available:
            # The clock runs (the grace period is longer than an unanswered Start lasts), nothing is told.
            return None
        if (facts.now - self._trouble_since).total_seconds() < self._grace_s:
            return None
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
        if facts.vehicle_not_requesting:
            return REASON_VEHICLE_NOT_REQUESTING
        return REASON_STOPPED if self._charged_in_window else REASON_NOT_STARTED
