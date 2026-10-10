"""The car ending a plan's charge by itself inside a window.

Pure: no Home Assistant imports, the clock is passed in. A person's Start has its own watch (`manual_pause.py`) and a
top-off its own idle end (`top_off.py`); this is the plan's charge in an open window. The car has ended it when, with
the plan's charge control on, it draws nothing for `PLAN_CAR_IDLE_S` in a row: by the measured current (at most
`top_off.DRAWING_ABOVE_A`, whatever the status says), else by the connector saying it is not charging (OCPP's
`SuspendedEV`: the car asks for no current).

Not the car: load balancing pausing the charge, the charger's own scheduler or load balancer holding it (or its
connector saying `SuspendedEVSE`), a Start not yet answered, a control that is off, a charge that is not the plan's,
and a car whose drawing cannot be read at all. Each of these starts the clock again.
"""

from __future__ import annotations

from datetime import datetime
from typing import Final

#: How long (seconds) the car draws nothing with the plan's charge on before it has ended the charge by itself: the
#: sun's `CAR_IDLE_S` and a person's charge's `MANUAL_CAR_IDLE_S`.
PLAN_CAR_IDLE_S: Final = 300.0

#: The connector statuses of a car that asks for no current, and of a charger that holds the car back itself
#: (OCPP's spelling).
SUSPENDED_EV: Final = "SuspendedEV"
SUSPENDED_EVSE: Final = "SuspendedEVSE"


class PlanChargeWatch:
    """Since when the car has drawn nothing in the plan's charge, and whether that was told."""

    def __init__(self) -> None:
        self._idle_since: datetime | None = None
        self._told = False

    def reset(self) -> None:
        self._idle_since = None
        self._told = False

    def observe(
        self,
        *,
        now: datetime,
        plan_charge: bool,
        control_on: bool,
        drawing: bool | None,
        connector_status: str | None,
        held: bool,
        balancing_paused: bool,
        start_pending: bool,
    ) -> bool:
        """One look at the plan's charge. `True` once per stretch of idling: the car ended it by itself."""
        idle = drawing is False or (drawing is None and connector_status == SUSPENDED_EV)
        not_the_car = (
            not plan_charge
            or not control_on
            or balancing_paused
            or held
            or start_pending
            or connector_status == SUSPENDED_EVSE
            or not idle
        )
        if not_the_car:
            self.reset()
            return False
        if self._idle_since is None:
            self._idle_since = now
        if self._told or (now - self._idle_since).total_seconds() < PLAN_CAR_IDLE_S:
            return False
        self._told = True
        return True
