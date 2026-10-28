"""The car ending a plan's charge by itself inside a window.

Pure: no Home Assistant imports, the clock is passed in. A person's Start has its own watch (`manual_pause.py`) and a
top-off its own idle end (`top_off.py`); this is the plan's charge in an open window. The car has ended it when, with
the plan's charge control on, it draws nothing for `PLAN_CAR_IDLE_S` in a row: by the measured current (at most
`top_off.DRAWING_ABOVE_A`, whatever the status says), else by the connector saying it is not charging (OCPP's
`SuspendedEV`: the car asks for no current).

Not the car: load balancing pausing the charge, the charger's own scheduler or load balancer holding it (or its
connector saying `SuspendedEVSE`, in any case: a charger other than OCPP reports its status lower-cased), a Start not
yet answered, a control that is off, a charge that is not the plan's, and a car whose drawing cannot be read at all.
A phase switch that settles (`phase_switching.SETTLE_S`: the charger interrupts the charge itself) is not the car
either. Each of these starts the clock again. Before a charge has drawn at all since its start, only the car's own refusal
counts (its status says `SuspendedEV`, or the profile's value for it: a full car refusing at once): a start the charger
did not act on (a switch that reads on while the charger idles, `awaiting_start`, `ready`, no status) is no charge the
car ended, and the start is sent again instead (`START_RETRY_AFTER_S`).

While load balancing holds the plan's charge back this watch sees nothing; there the battery probe is the car's
test instead: two probes in a row the car refused (`site/battery_probe.py`, `car_refused`) end the charge the same
way (`ChargingController.note_car_refused_probe`).
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
        # The car has drawn in this charge (since the plan's charge control was last seen on): only then can it end it.
        self._drawn = False

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
        phase_switch_settling: bool = False,
    ) -> bool:
        """One look at the plan's charge. `True` once per stretch of idling: the car ended it by itself."""
        status = None if connector_status is None else connector_status.lower()
        if not plan_charge or not control_on:
            # The charge is over (or not the plan's): the next one has to draw before the car can end it.
            self._drawn = False
        elif drawing is True:
            self._drawn = True
        idle = drawing is False or (drawing is None and status == SUSPENDED_EV.lower())
        not_the_car = (
            not plan_charge
            or not control_on
            or balancing_paused
            or held
            or start_pending
            or phase_switch_settling
            or status == SUSPENDED_EVSE.lower()
            or not idle
            # Before the charge has drawn only the car's own refusal counts (`SuspendedEV`, or the profile's value
            # for it): a charger side that idles (`SuspendedEVSE`, ready, awaiting a start, no status) is a start
            # that did not take.
            or (not self._drawn and status != SUSPENDED_EV.lower())
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
