"""A person's Start under a manual pause: when the car ended the charge by itself.

Pure: no Home Assistant imports, the clock is passed in. A person's Start pauses Auto for the plug-in
session (`auto_settings.PAUSE_MANUAL`); the pause also ends when the car ends that charge by itself, by
the same reading the sun's rules use for a charge they run (`solar_execution._watch_charge`):

* the charge control went off after it was seen on, not by load balancing's pause, with the connector
  saying `Finishing` or `SuspendedEV` or the car having stopped drawing just before;
* the control still on and the car drawing nothing for `MANUAL_CAR_IDLE_S` in a row.

An unplug is not the car ending it (the unplug ends the pause on its own), nor is a control turned off
some other way while the car was drawing (the charger's own app, say): the charger is still the person's.
"""

from __future__ import annotations

from datetime import datetime
from typing import Final

from .charge_progress import SUSPENDED_EV
from .charger_connection import DISCONNECTED, FINISHED

#: How long (seconds) the car draws nothing with the charge control on before it has ended the charge by
#: itself: the sun's `CAR_IDLE_S`.
MANUAL_CAR_IDLE_S: Final = 300.0


class ManualChargeWatch:
    """What one person's charge has shown so far: whether its control was seen on, and since when the car
    has drawn nothing."""

    def __init__(self) -> None:
        self._seen_on = False
        self._idle_since: datetime | None = None

    def reset(self) -> None:
        self._seen_on = False
        self._idle_since = None

    def observe(
        self,
        *,
        now: datetime,
        control_on: bool,
        drawing: bool | None,
        held: bool,
        balancing_paused: bool,
        start_pending: bool,
        connection: str | None,
        connector_status: str | None,
    ) -> bool:
        """One look at the charge. `True` once: the car ended it by itself."""
        if balancing_paused:
            # Load balancing holds it back: its regulator resumes it, nothing the car did.
            self._idle_since = None
            return False
        if control_on:
            self._seen_on = True
            if held:
                # The charger holds the car back (its own scheduler or load balancer), not the car itself.
                drawing = None
            if drawing is not False:
                self._idle_since = None
                return False
            if self._idle_since is None:
                self._idle_since = now
            if (now - self._idle_since).total_seconds() < MANUAL_CAR_IDLE_S:
                return False
            self.reset()
            return True
        if not self._seen_on or start_pending:
            return False
        ended_by_car = connection != DISCONNECTED and (
            connection == FINISHED or connector_status == SUSPENDED_EV or self._idle_since is not None
        )
        self.reset()
        return ended_by_car
