"""The installation's public holidays for the grid fee's and the demand charge's hours: no setting.

A high-load window on some days of the week (`planning/planner.py:HighLoadWindow`) leaves a public holiday out, as
the operators bill ("helgfri vardag"): the time-of-use grid fee's high rate and a demand charge's window part both.
The dates are read here once a day and set on each window when it is resolved (`holiday_dates`), never stored.

* **Source, in this order** (`HolidaySource.async_refresh`):

  1. a **Workday** binary sensor (Home Assistant's core Workday integration), so its own settings count (its
     country and province, its categories, its added and removed holidays). Chosen among the enabled ones without
     a day offset: the one for Home Assistant's country when there are several, else the only one. A day it calls
     no workday although its weekday is one of its work days (`workday.check_date`) is a holiday; its other days
     off (a Saturday, a Sunday) are left to the window's own days.
  2. else the **holiday calendar** the house load forecast reads (`site/house_forecast.py:pick_holiday_calendar`,
     the core Holiday integration), its all-day events. Christmas Eve is added for Sweden: Swedish grid operators
     (Göteborg Energi) count it as a red day while the Holiday integration lists it only among the de facto
     holidays, which it leaves out unless chosen.
  3. else **none**: a holiday counts as the weekday it falls on, as before (the safe side: an hour too many).

* **Span**: `SPAN_BACK_DAYS` before today to `SPAN_AHEAD_DAYS` after, enough for the running month's hours (what is
  measured) and the plans ahead. Read once a local day, again at once when the chosen entity changes, and retried
  each hour after a failed read (the last dates are kept meanwhile).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Iterable, Mapping
from datetime import date, datetime, time as dt_time, timedelta
from typing import Any, Final

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_time_change
from homeassistant.helpers.start import async_at_started
from homeassistant.util import dt as dt_util

from .house_forecast import holiday_dates as calendar_dates
from .house_forecast import HOLIDAY_PLATFORM, pick_holiday_calendar

_LOGGER = logging.getLogger(__name__)

WORKDAY_PLATFORM: Final = "workday"
SPAN_BACK_DAYS: Final = 40
SPAN_AHEAD_DAYS: Final = 40
READ_TIMEOUT_S: Final = 20.0
#: The Workday sensor's work days when its attribute cannot be read (its own default).
DEFAULT_WORKDAYS: Final = ("mon", "tue", "wed", "thu", "fri")
WEEKDAY_KEYS: Final = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

SOURCE_WORKDAY: Final = "workday"
SOURCE_CALENDAR: Final = "calendar"
SOURCE_NONE: Final = "none"


# ------------------------------------------------------------------------------------------------------- pure


def span(today: date) -> tuple[date, date]:
    """The first and last date read around `today`, both included."""
    return today - timedelta(days=SPAN_BACK_DAYS), today + timedelta(days=SPAN_AHEAD_DAYS)


def days_in(first: date, last: date) -> list[date]:
    return [first + timedelta(days=offset) for offset in range((last - first).days + 1)]


def work_weekdays(raw: Any) -> frozenset[int]:
    """A Workday sensor's `workdays` attribute as ISO weekdays (1 = Monday); its default when unreadable. The
    `holiday` key it may hold is no weekday and is left out."""
    keys = raw if isinstance(raw, (list, tuple)) else DEFAULT_WORKDAYS
    days = frozenset(WEEKDAY_KEYS.index(key) + 1 for key in keys if key in WEEKDAY_KEYS)
    return days or frozenset(WEEKDAY_KEYS.index(key) + 1 for key in DEFAULT_WORKDAYS)


def workday_holidays(answers: Mapping[date, bool], work_days: frozenset[int]) -> frozenset[date]:
    """The dates a Workday sensor calls no workday although their weekday is one of its work days."""
    return frozenset(day for day, workday in answers.items() if not workday and day.isoweekday() in work_days)


def calendar_holidays(dates: Iterable[date], country: str | None, first: date, last: date) -> frozenset[date]:
    """A holiday calendar's dates, with Christmas Eve added for Sweden (see the module)."""
    found = set(dates)
    if country == "SE":
        for year in range(first.year, last.year + 1):
            eve = date(year, 12, 24)
            if first <= eve <= last:
                found.add(eve)
    return frozenset(found)


def workday_answer(response: Any, entity_id: str) -> bool | None:
    """`workday.check_date`'s answer for one entity, `None` when it is not one."""
    body = response.get(entity_id) if isinstance(response, Mapping) else None
    answer = body.get("workday") if isinstance(body, Mapping) else None
    return answer if isinstance(answer, bool) else None


# ------------------------------------------------------------------------------------------------ Home Assistant


class HolidaySource:
    """The installation's holiday dates (see the module). Never raises into a caller."""

    def __init__(self, hass: HomeAssistant, now: Callable[[], datetime] = dt_util.now) -> None:
        self.hass = hass
        self._now = now
        #: The dates the windows leave out; empty without a source or before the first read.
        self.dates: frozenset[date] = frozenset()
        self.source: str = SOURCE_NONE
        self.entity_id: str | None = None
        self._read_day: date | None = None
        self._read_entity: str | None = None
        self._running = False
        self._cancel: list[Callable[[], None]] = []
        #: The read in flight: a background task (a sensor or calendar that never answers delays nothing), cancelled
        #: at the stop.
        self._task: asyncio.Task[None] | None = None

    @callback
    def async_start(self) -> None:
        """Read once Home Assistant has started (the sensors and calendars exist then), then check each hour."""
        self._cancel.append(async_at_started(self.hass, self._on_started))
        self._cancel.append(async_track_time_change(self.hass, self._on_hour, minute=3, second=0))

    @callback
    def async_stop(self) -> None:
        for cancel in self._cancel:
            cancel()
        self._cancel.clear()
        if self._task is not None and not self._task.done():
            self._task.cancel()

    @callback
    def _on_started(self, _hass: HomeAssistant) -> None:
        self._start_read()

    @callback
    def _on_hour(self, _now: datetime) -> None:
        self._start_read()

    @callback
    def _start_read(self) -> None:
        if not self._running:
            self._task = self.hass.async_create_background_task(
                self.async_refresh(), "spotnav_holidays", eager_start=False
            )

    def _candidates(self, domain: str, platform: str, country_key: str) -> dict[str, str | None]:
        registry = er.async_get(self.hass)
        found: dict[str, str | None] = {}
        for entry in registry.entities.values():
            if entry.domain != domain or entry.platform != platform or entry.disabled_by is not None:
                continue
            if domain == "binary_sensor":
                state = self.hass.states.get(entry.entity_id)
                offset = None if state is None else state.attributes.get("days_offset")
                if offset not in (None, 0):
                    continue
            config = None
            if entry.config_entry_id is not None:
                config = self.hass.config_entries.async_get_entry(entry.config_entry_id)
            country = None
            if config is not None:
                country = config.options.get(country_key) or config.data.get(country_key)
            found[entry.entity_id] = country if isinstance(country, str) else None
        return found

    def workday_entity(self) -> str | None:
        """The Workday sensor the dates come from (see the module), `None` without one."""
        return pick_holiday_calendar(
            self._candidates("binary_sensor", WORKDAY_PLATFORM, "country"), self.hass.config.country
        )

    def calendar_entity(self) -> tuple[str | None, str | None]:
        """The holiday calendar the house forecast reads, and its country."""
        candidates = self._candidates("calendar", HOLIDAY_PLATFORM, "country")
        entity_id = pick_holiday_calendar(candidates, self.hass.config.country)
        return entity_id, None if entity_id is None else candidates.get(entity_id)

    async def async_refresh(self) -> None:
        """Read the dates when the day or the chosen entity changed, or the last read failed. Never raises."""
        if self._running:
            return
        self._running = True
        try:
            today = self._now().date()
            workday = self.workday_entity()
            calendar, country = (None, None) if workday is not None else self.calendar_entity()
            chosen = workday or calendar
            if chosen == self._read_entity and today == self._read_day:
                return
            first, last = span(today)
            if workday is not None:
                dates = await self._async_workday(workday, first, last)
                source = SOURCE_WORKDAY
            elif calendar is not None:
                dates = await self._async_calendar(calendar, country, first, last)
                source = SOURCE_CALENDAR
            else:
                dates, source = frozenset(), SOURCE_NONE
            if dates is None:
                return  # tried again within the hour; the last dates stand
            if (source, chosen, dates) != (self.source, self.entity_id, self.dates):
                _LOGGER.debug("SpotNav holidays: %d dates from %s", len(dates), chosen or "no source")
            self.dates, self.source, self.entity_id = dates, source, chosen
            self._read_day, self._read_entity = today, chosen
        except Exception:  # noqa: BLE001 - the windows keep the last dates
            _LOGGER.debug("SpotNav holidays: the dates could not be read", exc_info=True)
        finally:
            self._running = False

    async def _async_workday(self, entity_id: str, first: date, last: date) -> frozenset[date] | None:
        state = self.hass.states.get(entity_id)
        work_days = work_weekdays(None if state is None else state.attributes.get("workdays"))
        answers: dict[date, bool] = {}
        try:
            async with asyncio.timeout(READ_TIMEOUT_S):
                for day in days_in(first, last):
                    if day.isoweekday() not in work_days:
                        continue
                    response = await self.hass.services.async_call(
                        "workday",
                        "check_date",
                        {"entity_id": entity_id, "check_date": day},
                        blocking=True,
                        return_response=True,
                    )
                    answer = workday_answer(response, entity_id)
                    if answer is None:
                        return None
                    answers[day] = answer
        except Exception:  # noqa: BLE001 - tried again within the hour
            _LOGGER.debug("SpotNav holidays: %s could not be read", entity_id, exc_info=True)
            return None
        return workday_holidays(answers, work_days)

    async def _async_calendar(
        self, entity_id: str, country: str | None, first: date, last: date
    ) -> frozenset[date] | None:
        zone = dt_util.get_time_zone(self.hass.config.time_zone)
        start = datetime.combine(first, dt_time.min, zone)
        end = datetime.combine(last + timedelta(days=1), dt_time.min, zone)
        try:
            async with asyncio.timeout(READ_TIMEOUT_S):
                response = await self.hass.services.async_call(
                    "calendar",
                    "get_events",
                    {"entity_id": entity_id, "start_date_time": start.isoformat(), "end_date_time": end.isoformat()},
                    blocking=True,
                    return_response=True,
                )
        except Exception:  # noqa: BLE001 - tried again within the hour
            _LOGGER.debug("SpotNav holidays: %s could not be read", entity_id, exc_info=True)
            return None
        return calendar_holidays(calendar_dates(response, entity_id), country, first, last)

    def diagnostics(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "entity_id": self.entity_id,
            "dates": sorted(day.isoformat() for day in self.dates),
        }
