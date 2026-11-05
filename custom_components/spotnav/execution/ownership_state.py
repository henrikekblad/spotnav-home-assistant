"""What the charging controller reads of charge ownership, one group at a time.

Pure: no Home Assistant imports. The charge session (`core/session.py`) is the truth; these frozen views are what the
controller's read-only accessors (`_balancing`, `_safety`, `_c7`, `_car_ended`, `_hold_state`) give, in the shapes the
controller and today's stored keys read them: load balancing's memory, the safety hold, C7's stops, the car-ended
record and the hold's memory. The owner is read as two fields (`ChargingController._owner_origin`/`_is_plan_charge`,
derived from the session's owner).

It also reads, from the controller's stored record, whether the charger's stopped state is one SpotNav made itself
(`own_stop_from_record`), for the charger's hand-back (`charger_handback.py`).
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


#: The controller's store key: the charger's stopped state is one SpotNav made itself (`ChargingController._own_stop`).
OWN_STOP_STORE_KEY = "spotnav_stopped"


def person_paused_in_record(saved: dict[str, Any]) -> bool:
    """Whether the stored record says a person's Stop or a pause they picked holds the charger: the core's session
    (`manual` with the action `stop`, or a `span_pause`), or the key an older release kept (`person_stopped`). Read
    leniently: a session that cannot be read says nothing."""
    if saved.get("person_stopped") is True:
        return True
    session = saved.get("charge_session")
    if not isinstance(session, dict):
        return False
    manual = session.get("manual")
    return (isinstance(manual, dict) and manual.get("action") == "stop") or session.get("span_pause") is not None


def own_stop_from_record(saved: Any) -> bool:
    """Whether the charger's stopped state is SpotNav's own, by the controller's stored record.

    The record's own flag (`OWN_STOP_STORE_KEY`) when it has one. A record from before the flag was kept says it only
    through the facts it already had: an Easee paused by SpotNav (`adapter_memory`) or a charge load balancing paused
    (`balancing_pause`), while no person's Stop or pause holds the charger. Anything else is not SpotNav's to undo.
    """
    if not isinstance(saved, dict):
        return False
    flag = saved.get(OWN_STOP_STORE_KEY)
    if isinstance(flag, bool):
        return flag
    if person_paused_in_record(saved):
        return False
    memory = saved.get("adapter_memory")
    easee_paused = isinstance(memory, dict) and memory.get("paused") is True
    return easee_paused or isinstance(saved.get("balancing_pause"), dict)
