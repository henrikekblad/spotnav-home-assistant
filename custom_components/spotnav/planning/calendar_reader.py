"""Reading a departure calendar's events from Home Assistant: the thin shell around `calendar_departure.py`.

One reader per charger's controller. `async_refresh` asks `calendar.get_events` (any calendar integration) for the
events from a day back to `CALENDAR_DAYS_AHEAD` days ahead and a day more, at most once per `REFRESH_S` per calendar;
`events` answers from what was last read, synchronously, so every place that plans can use it without waiting.

A calendar that cannot be read (gone, unavailable, the call fails or times out) keeps its last answer for
`KEEP_LAST_S` (a cloud calendar that misses one poll does not move the plan), then reads as unavailable: the daily
departure is used and the Plan dialog says so. Logged once per change, never a notification.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Final

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util

from .calendar_departure import CALENDAR_DAYS_AHEAD, CalendarEvent, events_from_response

_LOGGER = logging.getLogger(__name__)

#: How often a chosen calendar is read again (a new event planned for within this).
REFRESH_S: Final = 15 * 60
#: How long a calendar that stops answering keeps its last answer.
KEEP_LAST_S: Final = 60 * 60
#: The longest one read may take.
READ_TIMEOUT_S: Final = 20.0
#: How soon a calendar that does not exist yet is looked for again (its integration may still be starting).
MISSING_RETRY_S: Final = 60

_UNREADABLE_STATES: Final = frozenset({"unavailable"})


@dataclass(frozen=True, slots=True)
class _Read:
    entity_id: str
    at: datetime
    events: tuple[CalendarEvent, ...]


class CalendarReader:
    """One charger's departure calendar, read and remembered."""

    def __init__(self, hass: HomeAssistant, now: Callable[[], datetime] | None = None) -> None:
        self._hass = hass
        self._now: Callable[[], datetime] = now if now is not None else dt_util.utcnow
        self._last_good: _Read | None = None
        self._tried: tuple[str, datetime] | None = None
        self._failing: str | None = None
        #: The calendar the last read found no entity for.
        self._missing: str | None = None
        self._lock = asyncio.Lock()

    def events(self, entity_id: str) -> tuple[CalendarEvent, ...] | None:
        """The events last read from `entity_id`, or `None` when it cannot be read (never read, or failing past
        `KEEP_LAST_S`)."""
        good = self._last_good
        if good is None or good.entity_id != entity_id:
            return None
        if (self._now() - good.at).total_seconds() > KEEP_LAST_S:
            return None
        return good.events

    def due(self, entity_id: str) -> bool:
        """Whether `entity_id` should be read again now."""
        tried = self._tried
        return tried is None or tried[0] != entity_id or (self._now() - tried[1]).total_seconds() >= REFRESH_S

    def next_read_s(self, entity_id: str) -> int:
        """How long until `entity_id` is read again: sooner when it did not exist at the last read."""
        return MISSING_RETRY_S if self._missing == entity_id else REFRESH_S

    async def async_refresh(self, entity_id: str, *, force: bool = False) -> bool:
        """Read `entity_id` when due (or `force`); whether what `events` answers changed."""
        async with self._lock:
            if not force and not self.due(entity_id):
                return False
            before = self.events(entity_id)
            now = self._now()
            self._tried = (entity_id, now)
            read = await self._async_read(entity_id, now)
            if read is not None:
                if self._failing == entity_id:
                    _LOGGER.info("SpotNav reads the departure calendar %s again", entity_id)
                self._failing = None
                self._last_good = _Read(entity_id, now, read)
            elif self._failing != entity_id:
                self._failing = entity_id
                _LOGGER.warning(
                    "SpotNav cannot read the departure calendar %s; the daily departure is used until it can",
                    entity_id,
                )
            return self.events(entity_id) != before

    async def _async_read(self, entity_id: str, now: datetime) -> tuple[CalendarEvent, ...] | None:
        state = self._hass.states.get(entity_id)
        self._missing = entity_id if state is None else None
        if state is None or state.state in _UNREADABLE_STATES:
            return None
        # A day either side: the price area's zone may not be the installation's, and today is read in the area's.
        start = now - timedelta(days=1)
        end = now + timedelta(days=CALENDAR_DAYS_AHEAD + 2)
        try:
            async with asyncio.timeout(READ_TIMEOUT_S):
                response: Any = await self._hass.services.async_call(
                    "calendar",
                    "get_events",
                    {"start_date_time": start.isoformat(), "end_date_time": end.isoformat()},
                    target={"entity_id": entity_id},
                    blocking=True,
                    return_response=True,
                )
        except (HomeAssistantError, TimeoutError, ValueError) as err:
            _LOGGER.debug("Reading the departure calendar %s failed: %s", entity_id, err)
            return None
        except Exception:  # noqa: BLE001 - any calendar integration's failure is a calendar that cannot be read
            _LOGGER.debug("Reading the departure calendar %s failed", entity_id, exc_info=True)
            return None
        if not isinstance(response, dict) or not isinstance(response.get(entity_id), dict):
            return None
        try:
            return events_from_response(response[entity_id])
        except Exception:  # noqa: BLE001 - an answer that cannot be read is a calendar that cannot be read
            _LOGGER.debug("The departure calendar %s answered something unreadable", entity_id, exc_info=True)
            return None
