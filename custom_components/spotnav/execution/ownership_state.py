"""What the charging controller reads and writes of charge ownership, one group at a time.

Pure: no Home Assistant imports. The controller still keeps each group in its own fields; these frozen views are
what its accessors (`_balancing`, `_safety`, `_c7`, `_car_ended`, `_hold_state`) give and take, so every read and
write of a group goes through one place. A group the core owns is read from the charge session instead of the
fields, in that accessor only, while the core drives: the safety hold and C7's stops (the core end state plan, step 3),
load balancing's memory (step 4) and the hold's memory (step 5) so far.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass(frozen=True, slots=True)
class BalancingMemory:
    """Load balancing's memory: whether the last thing done to the charge control was the regulator's pause, and
    what the charge it paused was (its origin, and whether it was the plan's), or `None`."""

    paused: bool
    charge: tuple[str | None, bool] | None


@dataclass(frozen=True, slots=True)
class SafetyHold:
    """The charge load balancing holds was stopped for safety, and when it last was."""

    held: bool
    stopped_at: datetime | None


@dataclass(frozen=True, slots=True)
class PersonHoldStops:
    """C7: the stops of a charge the charger began by itself under a person's Stop. One on its way, when they went
    out in this plug-in, when the last was tried, and whether they were given up."""

    stop_pending: bool
    stop_times: list[datetime]
    tried_at: datetime | None
    gave_up: bool


@dataclass(frozen=True, slots=True)
class CarEndedRecord:
    """When the car last ended a charge by itself, its level then, which car was read, and what the plan waits for
    (`ChargingController._car_ended_wait`)."""

    at: datetime | None
    soc: float | None
    vehicle: str | None
    wait: dict[str, Any] | None


@dataclass(frozen=True, slots=True)
class HoldState:
    """What the window hold remembers of ownership (`window_hold.WindowHold`): whether this session was held,
    whether a person overrode the hold, and whether the charge is SpotNav's."""

    held: bool
    overridden: bool
    owned: bool
