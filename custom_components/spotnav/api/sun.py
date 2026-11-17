"""The charger card's sun: the dashboard's additive `sun` block, for the price chart.

The block is present only where the site has a solar forecast (the same source the house card's sun series reads,
`api/house.py`), else absent. `hours` holds the sun's mean power per local hour of today and tomorrow, in kW
(a kWh over an hour is its mean kW): before the running hour the measured production (the forecast log's outcomes,
filled from the Energy dashboard's statistics, `measured` true), from the running hour on the corrected forecast the
plan uses (`measured` false). An hour with no figure is left out, never sent as zero. The forecast reaches only
`SHOWN_HOURS` ahead (`site/solar_correction_shadow.py`), so tomorrow is partly there.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime, timedelta
from typing import Any, Final

from homeassistant.core import HomeAssistant

from .. import time_zones
from ..planning.demand_charge import hour_start
from ..runtime import site_controller_for
from ..site.house_view import day_hour_starts
from .house import _production, _sun

SUN_KEYS: Final = ("hours",)
HOUR_KEYS: Final = ("start", "kw", "measured")


def sun_block_from(
    now: datetime,
    tz: str,
    measured: Mapping[datetime, float],
    forecast: Mapping[datetime, float],
) -> dict[str, Any]:
    """The block from the measured hours (kWh by UTC hour start) and the corrected forecast, for today and tomorrow."""
    current = hour_start(now, tz)
    today: date = now.astimezone(time_zones.require_zone(tz)).date()
    hours: list[dict[str, Any]] = []
    for day in (today, today + timedelta(days=1)):
        for start in day_hour_starts(day, tz):
            past = start < current
            value = (measured if past else forecast).get(start)
            if value is None:
                continue
            hours.append({"start": start.isoformat(), "kw": round(max(0.0, value), 2), "measured": past})
    return {"hours": hours}


def capture_sun(hass: HomeAssistant, charger_entry_id: str, now: datetime, tz: str | None) -> dict[str, Any] | None:
    """The `sun` block of the site this charger belongs to, or `None` without a market or a solar forecast."""
    from .dashboard import site_binding

    if tz is None:
        return None
    site = site_binding(hass, charger_entry_id)
    controller = None if site is None else site_controller_for(hass, site.entry_id)
    if controller is None:
        return None
    sun = _sun(hass, controller)
    if sun is None:
        return None
    return sun_block_from(now, tz, _production(controller), sun.corrected)
