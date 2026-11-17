"""The charger card's sun: the dashboard's additive `sun` block, for the price chart.

The block is present where the site can run Sol/Hybrid (`site_solar_capable`: PV power or export readable), else
absent. Where the site has a solar forecast (the same source the house card's sun series reads, `api/house.py`) it
carries the forecast beyond now. Without one it holds the measured production only: the hours from the solar shadow's
history (the Energy dashboard's statistics, which do not depend on the forecast log) and the quarters. SpotNav's own
solar forecast is shadow and never drawn here. `hours` holds the sun's mean power per local hour of today and tomorrow, in kW
(a kWh over an hour is its mean kW): before the running hour the measured production (the forecast log's outcomes,
filled from the Energy dashboard's statistics, `measured` true), from the running hour on the corrected forecast the
plan uses (`measured` false). An hour with no figure is left out, never sent as zero. The forecast reaches only
`SHOWN_HOURS` ahead (`site/solar_correction_shadow.py`), so tomorrow is partly there. `quarters` holds today's measured
mean power per quarter hour up to now (from the recorder's 5-minute statistics, at most `MAX_QUARTERS`; empty where
they are missing), finer than the hourly measured figures, which the card then replaces for those hours.
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
from ..site.solar_capability import site_solar_capable
from ..site.sun_geometry import key_start
from .house import _production, _sun

SUN_KEYS: Final = ("hours", "quarters")
#: Added to the block only where the site's peak production is known (kW, the most one hour has produced).
PEAK_KEY: Final = "peak_kw"
QUARTER_KEYS: Final = ("start", "kw")
#: A day has at most 100 quarters of the sun measured so far (25 hours); the block sends at most this many.
MAX_QUARTERS: Final = 96
HOUR_KEYS: Final = ("start", "kw", "measured")


def sun_block_from(
    now: datetime,
    tz: str,
    measured: Mapping[datetime, float],
    forecast: Mapping[datetime, float],
    quarters: Mapping[datetime, float] | None = None,
    *,
    observed_max_kwh: float | None = None,
    array_kwp: float | None = None,
    history_max_kwh: float | None = None,
) -> dict[str, Any]:
    """The block from the measured hours (kWh by UTC hour start) and the corrected forecast, for today and tomorrow,
    and today's measured quarter hours (mean kW by UTC quarter start, none where the 5-minute statistics are missing).
    `peak_kw` is the site's peak production (`peak_kw`), left out where none is known."""
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
    first = day_hour_starts(today, tz)[0]
    recent = [
        {"start": start.isoformat(), "kw": round(max(0.0, value), 2)}
        for start, value in sorted((quarters or {}).items())
        if first <= start <= now
    ]
    block: dict[str, Any] = {"hours": hours, "quarters": recent[-MAX_QUARTERS:]}
    peak = peak_kw(observed_max_kwh, array_kwp, history_max_kwh)
    if peak is not None:
        block[PEAK_KEY] = peak
    return block


def peak_kw(observed_max_kwh: float | None, array_kwp: float | None, history_max_kwh: float | None = None) -> float | None:
    """The most the site's sun produces in an hour, in kW, for the card to scale the sun against.

    The highest hour measured is the most robust (it is what the site really does). The long history (the stored year
    of the Energy dashboard's hours) is the best source: the correction's own window (`observed_max_kwh`) can be only
    a short recent stretch, in winter a fraction of the summer peak. The larger of the two wins. Only where nothing has
    been measured yet, the array's size from the forecast integration (an upper bound, so a cautious scale). `None`
    where none is a positive figure."""
    measured = [c for c in (history_max_kwh, observed_max_kwh) if c is not None and c > 0]
    if measured:
        return round(max(measured), 2)
    if array_kwp is not None and array_kwp > 0:
        return round(array_kwp, 2)
    return None


def _measured_hours(controller: Any) -> dict[datetime, float]:
    """The completed hours' production (kWh by UTC hour start) the solar shadow read from the Energy dashboard."""
    return {key_start(key): value / 100.0 for key, value in controller.solar_shadow.history.pv.items()}


def capture_sun(hass: HomeAssistant, charger_entry_id: str, now: datetime, tz: str | None) -> dict[str, Any] | None:
    """The `sun` block of the site this charger belongs to, or `None` without a market or where the site cannot run
    Sol/Hybrid and has no solar forecast."""
    from .dashboard import site_binding

    if tz is None:
        return None
    site = site_binding(hass, charger_entry_id)
    controller = None if site is None else site_controller_for(hass, site.entry_id)
    if controller is None:
        return None
    sun = _sun(hass, controller)
    correction = controller.solar_correction
    quarters = correction.quarters
    history = _measured_hours(controller)
    peak = {
        "observed_max_kwh": correction.observed_max_kwh,
        "array_kwp": correction.array_kwp,
        "history_max_kwh": max(history.values(), default=None),
    }
    if sun is not None:
        return sun_block_from(now, tz, _production(controller), sun.corrected, quarters, **peak)
    if not site_solar_capable(controller.config):
        return None
    return sun_block_from(now, tz, history, {}, quarters, **peak)
