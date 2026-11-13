"""SpotNav's corrected solar forecast, offered to Home Assistant's Energy dashboard.

The Energy dashboard asks every integration with an `energy` platform for `async_get_solar_forecast(hass,
config_entry_id)` and draws what comes back (`{"wh_hours": {ISO time: Wh}}`, UTC or local-offset keys, one entry per
hour) for the solar source that names that config entry (core `energy/websocket_api.py`, checked against core of
2026-11-13: it asks only the entries a solar source names, skips a `None` answer, and lists every entry of a domain
with this platform in the picker).

It is a view, never a plan input:

* **Only a site's entry answers**, and only a site that names a solar forecast. A price or charger entry gets `None`
  (the dashboard's picker lists every SpotNav entry; the others simply show nothing).
* **Only the learnt correction**: while the correction is still learning (or the sun forecast was last read longer ago
  than `MAX_AGE`), the answer is `None`, so a figure SpotNav does not stand behind never reaches the dashboard
  unmarked. The raw figure is the source's own, which the dashboard can already show from that source.
* **Never read back**: SpotNav's own domain is left out of the platforms the Hybrid strategy and the correction read
  (`planning/hybrid_forecast.py`), and out of the site options' picker, so the corrected forecast can never be
  corrected again or planned on through this door.
"""

from __future__ import annotations

from datetime import timedelta

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .runtime import site_controller_for
from .site.surplus_forecast import MAX_AGE


async def async_get_solar_forecast(hass: HomeAssistant, config_entry_id: str) -> dict[str, dict[str, float]] | None:
    """The site's corrected solar forecast as Wh per UTC hour from the running hour on, or `None`."""
    controller = site_controller_for(hass, config_entry_id)
    if controller is None:
        return None
    correction = controller.solar_correction
    now = dt_util.utcnow()
    if (
        correction.sun_basis != "corrected"
        or correction.last_refresh is None
        or now - correction.last_refresh > MAX_AGE
    ):
        return None
    hours = {
        start.isoformat(): round(kwh * 1000.0, 1)
        for start, kwh in sorted(correction.sun_kwh.items())
        if start + timedelta(hours=1) > now
    }
    return {"wh_hours": hours} if hours else None

