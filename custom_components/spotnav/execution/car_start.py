"""The car refusing the current the charger offers: when SpotNav starts the charge in the car, or tells the person.

Pure: no Home Assistant imports, the clock is passed in. A car can be in a stopped state of its own (a Tesla stopped
from its app, or by a service linked to it): the charger offers current, the connector says the car asks for none,
and nothing the charger does clears it. Its integration's charge switch can (`vehicles/vehicle_charge_start.py`).

The car refuses (`CarRefusalWatch`) when, for `CAR_START_AFTER_S` in a row:

* the charge is one SpotNav runs (the plan's in a window, its top-off, a person's Start, the sun's, a catch-up after a
  missed departure, the car's minimum level) and its charge control is on;
* the charger offers current: not held by its own scheduler or load balancer, not paused by load balancing (nor its
  stop on its way), no phase switch settling, no Start unanswered, and the current it offers, where SpotNav can read
  it, at least `CAR_START_MIN_OFFER_A` (a value that cannot be read holds nothing back; one read below it does);
* the car draws nothing, and the charger's status says it offers and the car refuses: OCPP's `SuspendedEV`, or the
  profile's verified `car_refuses_values` (Easee's `ready_to_charge` and `completed`). Any other charger: never.

Then, for a car with a charge switch SpotNav knows that is confirmed to be the car at this charger
(`vehicle_charge_start.car_here`), whose level is below both the target and the car's own limit by
`CAR_START_MARGIN_PCT` (`CAR_START_ESTIMATE_MARGIN_PCT` when the level is an estimate, or a reading older than
`CAR_START_READING_MAX_AGE_S`): the charge is started in the car, at most once per plug-in, and the watch begins again
when the call has answered. When the car still refuses `CAR_START_AFTER_S` after that answer, or the start cannot be
made (used up, too soon, the switch unavailable), and the level, read again, still allows it, the person is told once
per plug-in and a status line says so until the car draws or is unplugged. Any other car keeps what it had: the plain
"the car takes no current" and the car's end of the charge. The car's end (`plan_car_ended.py`, `manual_pause.py`,
the sun's `CAR_IDLE_S`) goes on as before, its idle clock begun again at the start in the car.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Final

#: How long (seconds) the car refuses the offered current before the charge is started in the car, and how long after
#: that start it may still refuse before the person is told.
CAR_START_AFTER_S: Final = 120.0

#: How far (percentage points) below both the target and the car's own limit the car's level must be: a car that
#: rounds up to its limit, or stops a point short of it, refuses rightly.
CAR_START_MARGIN_PCT: Final = 2.0
#: The margin for a level that is an estimate (carried forward by delivered energy) or an old reading: the estimate
#: may lag a car that stopped at its own limit by a few points.
CAR_START_ESTIMATE_MARGIN_PCT: Final = 5.0
#: A reading older than this is counted as an estimate.
CAR_START_READING_MAX_AGE_S: Final = 6 * 3600.0
#: The least current the charger must offer for the car's refusal to be the car's: below a charge's minimum (6 A)
#: the car may rightly not start.
CAR_START_MIN_OFFER_A: Final = 6.0

#: A start in the car whose call has not answered after this long counts as timed out (the call's own timeout is two
#: minutes; this bounds the store writes around it too).
CAR_START_CALL_GIVE_UP_S: Final = 5 * 60.0

#: What the status line says (`CarStartMemory.line`).
LINE_STARTED: Final = "started"
LINE_REFUSES: Final = "refuses"


def start_ceiling_percent(target_percent: float | None, limit_percent: float | None) -> float:
    """The level the car is charged toward: the target, capped by the car's own limit (100 % when it states none)."""
    limit = 100.0 if limit_percent is None else limit_percent
    return limit if target_percent is None else min(target_percent, limit)


def counts_as_estimate(estimated: bool, age_s: float | None) -> bool:
    """Whether a level is to be taken as an estimate: one carried forward, or a reading of unknown age or older than
    `CAR_START_READING_MAX_AGE_S`."""
    return estimated or age_s is None or age_s > CAR_START_READING_MAX_AGE_S


def level_allows_start(
    level_percent: float | None,
    target_percent: float | None,
    limit_percent: float | None,
    *,
    estimated: bool = False,
) -> bool:
    """Whether the car's level is at least `CAR_START_MARGIN_PCT` (`CAR_START_ESTIMATE_MARGIN_PCT` for an estimate)
    below the target and the car's own limit. A level not known never allows it."""
    if level_percent is None:
        return False
    margin = CAR_START_ESTIMATE_MARGIN_PCT if estimated else CAR_START_MARGIN_PCT
    return level_percent <= start_ceiling_percent(target_percent, limit_percent) - margin


class CarRefusalWatch:
    """Since when the car has refused the current the charger offers in a charge SpotNav runs."""

    def __init__(self) -> None:
        self._since: datetime | None = None

    def reset(self) -> None:
        self._since = None

    def observe(
        self,
        *,
        now: datetime,
        owned: bool,
        control_on: bool,
        drawing: bool | None,
        refuses_offer: bool,
        offered_a: float | None,
        held: bool,
        balancing_paused: bool,
        start_pending: bool,
        phase_switch_settling: bool,
    ) -> float | None:
        """One look. The seconds the car has refused so far (0 at the first look), `None` while it does not.

        `refuses_offer`: the charger's status says it offers and the car refuses (verified values only);
        `offered_a`: the current it offers as read, `None` when it cannot be read (no hold: only a value read below
        `CAR_START_MIN_OFFER_A` is)."""
        refusing = (
            owned
            and control_on
            and not held
            and not balancing_paused
            and not start_pending
            and not phase_switch_settling
            and drawing is not True
            and refuses_offer
            and (offered_a is None or offered_a >= CAR_START_MIN_OFFER_A)
        )
        if not refusing:
            self._since = None
            return None
        if self._since is None:
            self._since = now
        return (now - self._since).total_seconds()


@dataclass(slots=True)
class CarStartMemory:
    """One plug-in's start in the car and notice: whether the start was made (`started_at`), whether the person was
    told, and the status line now (`line`: `LINE_STARTED` from the start until the car draws or is told about,
    `LINE_REFUSES` from the notice until the car draws or is unplugged) with the level and target it was told with."""

    generation: int | None = None
    started_at: datetime | None = None
    told: bool = False
    line: str | None = None
    level_percent: float | None = None
    target_percent: float | None = None
    #: When the notice was decided (what the notifier tells once); `None` when it was told before a restart.
    told_at: datetime | None = None
    #: The car and the plug-in (the controller's plug-in instant) the start and the notice are for.
    vehicle_id: str | None = None
    plug_in: str | None = None
    #: When the call in the car answered (or timed out); `None` while it is on its way. The second wait counts from it.
    answered_at: datetime | None = None
    #: When the notice was told, kept across a restart (the status line's `since`, what an app tells once by).
    since: datetime | None = None

    def begin(self, generation: int) -> None:
        """A new plug-in (or the first look after a start): nothing made or told in it yet."""
        self.generation = generation
        self.started_at = None
        self.told = False
        self.line = None
        self.level_percent = None
        self.target_percent = None
        self.told_at = None
        self.vehicle_id = None
        self.plug_in = None
        self.answered_at = None
        self.since = None
