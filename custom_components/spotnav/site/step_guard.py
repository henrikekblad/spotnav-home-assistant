"""Steps toward the fuse without bouncing on it: the regulator's proposal against what was written last.

The regulator (`site/regulator.py`) proposes `charger reading + headroom` on every reading. Two
things make that bounce when it is written as it stands (the night of 2026-10-17: a car stepped
7 -> 14 A, was cut 14 -> 9 -> 7 A five seconds later, and the same again every minute):

* A charger reports its own current far less often than the site meter reports the site (an OCPP
  charger about once a minute). Right after a step up the site already carries the new draw while
  the charger still reports the old one, so `reading + headroom` comes out at about the old value:
  a lowering on a site that is not over anything.
* A step aimed at the very edge of the headroom lands over it as often as not, and is then cut.

So, per charger, between the regulator and the damper:

1. A step up stops `UP_MARGIN_A` short of the edge the regulator offered (never below what is on
   the charger on that account), and never goes back to a level that overshot within
   `OVERSHOOT_BACKOFF_S` (it stays `UP_MARGIN_A` below it).
2. A lowering on a charger reading taken before the last write could show (the write is "in
   flight", within `IN_FLIGHT_S` of it) is held while no phase is over its limit, and while one is
   over by no more than `TRIM_MAX_OVER_A` it takes off only what the site is over plus
   `UP_MARGIN_A`, from what was written. A charger draws at most what it was set to, so that much
   off the setting takes the excess off the site.
3. A second lowering on the same site reading as the last one is held: one reading, one cut.
4. An overload of more than `TRIM_MAX_OVER_A` always goes through at the regulator's own value,
   at once: none of the above holds back a real overload.

The limit is the regulator's (`main fuse - the site's safety margin`, whatever that margin is,
0 included); the margins here are on the step, not on the fuse. Pure: times are passed in seconds.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

# How far (A) short of the offered headroom a step up stops; one amp is the smallest step a charger
# takes, so the car settles one step below the edge instead of on it.
UP_MARGIN_A = 1.0

# An overload (A over the limit) above this is real, not a step's overshoot: cut at once and fully.
TRIM_MAX_OVER_A = 2.0

# How long (s) after a write a charger reading older than the write is taken as not showing it yet.
IN_FLIGHT_S = 90.0

# How long (s) after a write the charger is given to reach it before its reading counts.
CATCH_UP_S = 10.0

# How long (s) a level that overshot is not tried again.
OVERSHOOT_BACKOFF_S = 600.0

GuardRule = Literal[
    "up_margin",
    "overshoot_backoff",
    "held_step_in_flight",
    "trimmed_overshoot",
    "held_same_reading",
]


@dataclass(frozen=True, slots=True)
class GuardResult:
    """The proposal to go on with, and the rule that changed it (`None`: unchanged)."""

    proposed_a: float
    rule: GuardRule | None = None


class StepGuard:
    """One charger's memory of its last step up, its last cut and the level that overshot."""

    def __init__(self) -> None:
        self._last_up_at: float | None = None
        self._last_write_at: float | None = None
        self._last_cut_reading_at: float | None = None
        self._overshot_a: float | None = None
        self._overshot_until: float | None = None

    @property
    def overshot_a(self) -> float | None:
        return self._overshot_a

    def _in_flight(self, now: float, charger_read_at: float | None) -> bool:
        """Whether the charger's reading was taken before it could show the last write."""
        written = self._last_write_at
        return (
            written is not None
            and now - written <= IN_FLIGHT_S
            and (charger_read_at is None or charger_read_at < written + CATCH_UP_S)
        )

    def guard(
        self,
        *,
        now: float,
        proposed_a: float,
        previous_a: float | None,
        requested_a: float | None,
        margin_a: float | None,
        charger_read_at: float | None,
        site_read_at: float | None,
        min_current_a: float = 0.0,
    ) -> GuardResult:
        """The proposal to hand on to the damper. `margin_a` is the most restrictive phase's headroom
        under the limit (negative: over it); `*_read_at` are the times the readings were taken."""
        if previous_a is None or proposed_a == previous_a:
            return GuardResult(proposed_a)
        if proposed_a > previous_a:
            if previous_a < min_current_a:
                # A start from a pause has its own rules (the resume); nothing to stop short of here.
                return GuardResult(proposed_a)
            return self._guard_raise(now, proposed_a, previous_a, requested_a, min_current_a)
        over_a = 0.0 if margin_a is None else max(0.0, -margin_a)
        if over_a > TRIM_MAX_OVER_A or margin_a is None:
            return GuardResult(proposed_a)
        if (
            site_read_at is not None
            and self._last_cut_reading_at is not None
            and site_read_at <= self._last_cut_reading_at
        ):
            return GuardResult(previous_a, "held_same_reading")
        if not self._in_flight(now, charger_read_at):
            return GuardResult(proposed_a)
        if over_a <= 0.0:
            return GuardResult(previous_a, "held_step_in_flight")
        trimmed = previous_a - math.ceil(over_a + UP_MARGIN_A)
        if trimmed > proposed_a:
            return GuardResult(trimmed, "trimmed_overshoot")
        return GuardResult(proposed_a)

    def _guard_raise(
        self,
        now: float,
        proposed_a: float,
        previous_a: float,
        requested_a: float | None,
        min_current_a: float,
    ) -> GuardResult:
        target = proposed_a
        rule: GuardRule | None = None
        if requested_a is None or proposed_a < requested_a:
            # Held by the headroom, not by the request: stop short of the edge.
            target = proposed_a - UP_MARGIN_A
            rule = "up_margin"
        if (
            self._overshot_a is not None
            and self._overshot_until is not None
            and now < self._overshot_until
            and target > self._overshot_a - UP_MARGIN_A
        ):
            target = self._overshot_a - UP_MARGIN_A
            rule = "overshoot_backoff"
        if target <= previous_a or target < min_current_a:
            return GuardResult(previous_a, rule)
        return GuardResult(target, rule)

    def note_write(
        self,
        *,
        now: float,
        previous_a: float | None,
        written_a: float,
        site_read_at: float | None,
    ) -> None:
        """Remember a write that went out: a step up starts its flight, a cut marks its reading, and a
        cut made while a step was in flight marks the stepped level as one that overshot."""
        if previous_a is None or written_a == previous_a:
            return
        self._last_write_at = now
        if written_a > previous_a:
            self._last_up_at = now
            return
        if written_a < previous_a:
            self._last_cut_reading_at = site_read_at
            if self._last_up_at is not None and now - self._last_up_at <= IN_FLIGHT_S:
                self._overshot_a = previous_a
                self._overshot_until = now + OVERSHOOT_BACKOFF_S
            self._last_up_at = None

    def reset(self) -> None:
        self.__init__()
