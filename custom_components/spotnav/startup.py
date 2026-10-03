"""The start-up grace: for a few minutes after the integration loads, what is not yet known is
said to be unknown instead of being answered with a fallback.

Right after Home Assistant starts, a solar-forecast integration may not have loaded and a charger's
entities may still read `unavailable`. Without this the dashboard would assert "no forecast source,
planning like Cheapest" and show a charger that is merely silent as off with a Start offered. The
grace is capped (`STARTUP_GRACE_S` after the load) and ends early as soon as every source reports.
Pure: no `hass`, no clock.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

#: How long after the integration loads a missing source is still treated as "not yet".
STARTUP_GRACE_S: Final = 180

WAITING_FORECAST: Final = "forecast"
WAITING_CHARGER: Final = "charger"


@dataclass(frozen=True, slots=True)
class StartupState:
    """`active` while a source is still awaited inside the grace; `until` is the cap (set while
    active); `waiting_for` names what is awaited (`forecast`, `charger`).
    """

    active: bool
    until: datetime | None
    waiting_for: tuple[str, ...]


NOT_STARTING: Final = StartupState(active=False, until=None, waiting_for=())


def startup_state(
    *, now: datetime, started_at: datetime, forecast_pending: bool, charger_pending: bool
) -> StartupState:
    """Whether the dashboard is starting up. Over once the cap passes or nothing is awaited."""
    until = started_at + timedelta(seconds=STARTUP_GRACE_S)
    waiting = tuple(
        name
        for name, pending in ((WAITING_FORECAST, forecast_pending), (WAITING_CHARGER, charger_pending))
        if pending
    )
    if now >= until or not waiting:
        return NOT_STARTING
    return StartupState(active=True, until=until, waiting_for=waiting)
