"""The car refusing the current the charger offers: when SpotNav starts the charge in the car, or tells the person.

Pure: no Home Assistant imports, the clock is passed in. A car can be in a stopped state of its own (a Tesla stopped
from its app, or by a service linked to it): the charger offers current, the connector says the car asks for none,
and nothing the charger does clears it. Its integration's charge switch can (`vehicles/vehicle_charge_start.py`).

The car refuses (`CarRefusalWatch`) when, for `CAR_START_AFTER_S` in a row:

* the charge is one SpotNav runs (the plan's in a window, its top-off, a person's Start, the sun's, a catch-up after a
  missed departure, the car's minimum level) and its charge control is on;
* the charger offers current: not held by its own scheduler or load balancer, not paused by load balancing (nor its
  stop on its way), no phase switch settling, no Start unanswered;
* the car draws nothing, and the connector says the car asks for none (`SuspendedEV`, or the profile's value for it:
  Easee's `ready_to_charge` and `completed`). A charger side that idles is not the car.

Then, once per plug-in (`CarStartMemory`), when the car's level (a reading or the estimate) is at least
`CAR_START_MARGIN_PCT` below both the target and the car's own limit (`level_allows_start`: a full car, or one at its
own limit, refuses rightly and is never poked): the charge is started in the car, and the watch begins again. When the
car still refuses `CAR_START_AFTER_S` after that, or the car has no switch (or the start was used up for this
plug-in), the person is told once per plug-in and a status line says so until the car draws or is unplugged. The
car's end of the charge (`plan_car_ended.py`, `manual_pause.py`, the sun's `CAR_IDLE_S`) goes on as before, its idle
clock begun again at the start in the car.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Final

from .plan_car_ended import SUSPENDED_EV

#: How long (seconds) the car refuses the offered current before the charge is started in the car, and how long after
#: that start it may still refuse before the person is told.
CAR_START_AFTER_S: Final = 120.0

#: How far (percentage points) below both the target and the car's own limit the car's level must be: a car that
#: rounds up to its limit, or stops a point short of it, refuses rightly.
CAR_START_MARGIN_PCT: Final = 2.0

#: What the status line says (`CarStartMemory.line`).
LINE_STARTED: Final = "started"
LINE_REFUSES: Final = "refuses"


def start_ceiling_percent(target_percent: float | None, limit_percent: float | None) -> float:
    """The level the car is charged toward: the target, capped by the car's own limit (100 % when it states none)."""
    limit = 100.0 if limit_percent is None else limit_percent
    return limit if target_percent is None else min(target_percent, limit)


def level_allows_start(level_percent: float | None, target_percent: float | None, limit_percent: float | None) -> bool:
    """Whether the car's level is at least `CAR_START_MARGIN_PCT` below the target and the car's own limit. A level
    not known never allows it."""
    if level_percent is None:
        return False
    return level_percent <= start_ceiling_percent(target_percent, limit_percent) - CAR_START_MARGIN_PCT


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
        connector_status: str | None,
        held: bool,
        balancing_paused: bool,
        start_pending: bool,
        phase_switch_settling: bool,
    ) -> float | None:
        """One look. The seconds the car has refused so far (0 at the first look), `None` while it does not."""
        refusing = (
            owned
            and control_on
            and not held
            and not balancing_paused
            and not start_pending
            and not phase_switch_settling
            and drawing is not True
            and connector_status is not None
            and connector_status.lower() == SUSPENDED_EV.lower()
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
