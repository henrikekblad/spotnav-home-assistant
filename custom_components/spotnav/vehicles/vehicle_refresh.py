"""Asking Home Assistant to re-read a vehicle's own entities.

A car's entities go quiet and the dashboard reports whatever they last said; this is how the app
asks "read it again". It calls only `homeassistant.update_entity`, which re-reads an entity from the
integration that owns it (cheap, generic, no effect on the car). It never calls a brand's own
force-update service, which wakes the car, drains its 12 V battery and can exhaust or lock an
account. So there is no brand, service name or entity-name pattern here, and a sleeping car simply
keeps reporting what it last knew.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Callable

from homeassistant.core import HomeAssistant

from ..runtime import domain_data
from .vehicle_discovery import vehicle_reading_entity_ids


_LOGGER = logging.getLogger(__name__)

#: The only service called: Home Assistant's generic "re-read these entities".
REFRESH_SERVICE_DOMAIN = "homeassistant"
REFRESH_SERVICE = "update_entity"

#: Minimum wait between refreshes of one vehicle. Every press is a real API call in the car's
#: integration, and the readings move on the scale of minutes.
REFRESH_MIN_INTERVAL_S = 60.0

#: One message for an id that names nothing, a non-vehicle device and a dismissed
#: device alike, so the action cannot probe which devices exist. Names no id.
UNKNOWN_VEHICLE = "Unknown vehicle"



class VehicleRefreshLimited(Exception):
    """Raised when this vehicle was asked to refresh too recently.

    Carries how long to wait so the caller can say when, not just no.
    """

    def __init__(self, retry_after_s: float) -> None:
        super().__init__("Vehicle was refreshed too recently")
        self.retry_after_s = retry_after_s


@dataclass(slots=True)
class VehicleRefreshLimiter:
    """One refresh interval per vehicle, in memory only.

    In memory because it is a courtesy to another integration: a restart costs at most one extra call.
    Per vehicle, since two cars are two vendor rate limits. The clock is injected so tests do not sleep.
    """

    min_interval_s: float = REFRESH_MIN_INTERVAL_S
    clock: Callable[[], float] = time.monotonic
    last_refresh_s: dict[str, float] = field(default_factory=dict)

    def retry_after_s(self, vehicle_id: str) -> float | None:
        """Seconds until this vehicle may be asked again, or `None` for now.

        Counted from the last *successful* refresh; a refusal does not extend it.
        """
        last = self.last_refresh_s.get(vehicle_id)
        if last is None:
            return None
        remaining = self.min_interval_s - (self.clock() - last)
        return remaining if remaining > 0 else None

    def note_refresh(self, vehicle_id: str) -> None:
        """Record that this vehicle has just been refreshed."""
        self.last_refresh_s[vehicle_id] = self.clock()


def limiter_for(hass: HomeAssistant) -> VehicleRefreshLimiter:
    """The integration-wide limiter, created on first use.

    Integration-wide, not per config entry: the same car can be refreshed through any charger's webhook,
    so the interval follows the car.
    """
    data = domain_data(hass)
    if data.refresh_limiter is None:
        data.refresh_limiter = VehicleRefreshLimiter()
    return data.refresh_limiter


async def async_refresh_vehicle(hass: HomeAssistant, vehicle_id: object) -> int:
    """Ask Home Assistant to re-read one vehicle's entities; return how many.

    Raises `ValueError` with `UNKNOWN_VEHICLE` for every id that is not a vehicle reported right now
    (one message for all), and `VehicleRefreshLimited` when asked too recently.

    One service call with every entity the reading depends on: most car integrations refresh a whole
    coordinator, so one call per entity would spend the vendor's rate limit several times for one
    answer. The call blocks, so `ok` means the re-read has happened and a dashboard read straight
    afterwards sees the fresh values.
    """
    entity_ids = vehicle_reading_entity_ids(hass, vehicle_id)
    if not entity_ids or not isinstance(vehicle_id, str):
        raise ValueError(UNKNOWN_VEHICLE)
    retry_after_s = limiter_for(hass).retry_after_s(vehicle_id)
    if retry_after_s is not None:
        raise VehicleRefreshLimited(retry_after_s)
    await hass.services.async_call(
        REFRESH_SERVICE_DOMAIN,
        REFRESH_SERVICE,
        {"entity_id": entity_ids},
        blocking=True,
    )
    limiter_for(hass).note_refresh(vehicle_id)
    # A count only: no entity id, no reading.
    _LOGGER.debug("Asked Home Assistant to re-read a vehicle (%d entities)", len(entity_ids))
    return len(entity_ids)
