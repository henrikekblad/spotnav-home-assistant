"""Let the car finish a charge to its own limit past the plan's last window: the top-off.

Pure: no Home Assistant imports. A car near its own charge limit tapers the current over the last half
hour or so, so a plan that charges to that limit (`ChargingController.charges_to_vehicle_limit`) can end
its last window while the car still draws. Then the charge is kept on (the *top-off*) until the car stops
drawing by itself, for `IDLE_S` in a row, or until the deadline: `LIMIT` past the window's end, and never
past the departure. Everything else about a charge is unchanged: a person's Stop ends it, load balancing
still caps and pauses it, and a plan below the car's limit (a lower target, an ordinary manual amount)
still ends with its window.

The controller owns the facts, the timers and the stop; this module owns only the two rules, so they can
be read, and tested, on their own.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Final

#: How long past the last window's end a top-off may run.
LIMIT: Final = timedelta(minutes=60)
#: How long the car must have stopped drawing, in a row, before the top-off ends as full.
IDLE_S: Final = 120.0
#: How often a running top-off looks again when nothing reports (a car at 0 A reports nothing new).
CHECK_INTERVAL: Final = timedelta(seconds=30)
#: Above this many amperes the car is drawing: well below the 6 A floor, above a sensor's noise.
DRAWING_ABOVE_A: Final = 1.0

#: The connector status of a car drawing (OCPP's spelling, as `charge_progress` reads it).
_CHARGING: Final = "Charging"


def car_drawing(
    *,
    connector_status: str | None,
    current_a: float | None,
    power_mode: bool = False,
    power_w: float | None = None,
    idle_power_w: float = 100.0,
) -> bool | None:
    """Whether the car draws current now: `True`, `False`, or `None` when nothing can tell.

    * a charger judged by its power (`power_mode`): above its idle power;
    * a finite measured current: above `DRAWING_ABOVE_A`, whatever the status says;
    * else the connector status: `Charging` draws, `SuspendedEV` (the car asks for nothing) and any other
      readable status do not;
    * nothing readable: `None`. A charge control that is merely on is no evidence: a top-off never starts
      on it, and a running one does not end on it.
    """
    if power_mode:
        if power_w is None or not math.isfinite(power_w):
            return None
        return power_w > idle_power_w
    if current_a is not None and math.isfinite(current_a):
        return current_a > DRAWING_ABOVE_A
    if connector_status is None:
        return None
    return connector_status == _CHARGING


def deadline(window_end: datetime, departure: datetime | None) -> datetime:
    """The latest a top-off may run to: `LIMIT` past the window's end, and never past the departure."""
    latest = window_end + LIMIT
    if departure is not None and departure < latest:
        return departure
    return latest
