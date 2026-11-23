"""The nightly sun review of a site without a home battery (SpotNav 3.0, shadow): the sun's value per quarter hour.

Shortly after local midnight (`RUN_AFTER`, so the recorder has compiled the day's last 5-minute row) each day the
site has not reviewed yet within the recorder's short-term statistics (`short_term_days`, 10 days by default) is
valued by the quarter hour (`site/sun_value.py`, basis `quarter`) in the executor: the Energy dashboard's grid
import, grid export (where it has one) and solar production from the 5-minute statistics, read once in the
recorder's executor and summed to quarters (`review_baselines.sun_by_quarter`, a quarter only with all three of its rows), each quarter
priced from the site's price slots (`pricing/period_prices.period_price_slots`: the area's held day prices, an hourly
price spread over its quarters, with the fees as set now, the same the battery review and the house card's cost use).

A day is kept when at least `MIN_DAY_SHARE` of its quarters are valued; yesterday short of that is tried again after
`RETRY` (at most `ATTEMPTS` times a day), an older day is left to the house card's hourly estimate. Kept in the site's
own store (`DayStore`, written off the loop): the days `DAYS_KEPT` days, their hours `HOURS_KEPT_DAYS` days, as the
house card's Earnings tab keeps its figures.

A site with a home battery is left out: its battery review values the sun (`DayReview.sun_value`), never both for one
day. So is a site without the dashboard's grid import or solar meter. The house card (`api/house_history.py`) reads
the kept days and values the days without one from the periods it reads itself (an hour estimate past the 5-minute
statistics). Runs from the site's readings (`tick`) as one tracked task, cancelled at the site's halt. Shadow:
nothing in it controls anything.
"""

from __future__ import annotations

import asyncio
import logging
from bisect import bisect_left, bisect_right
from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime, timedelta
from typing import Any, Final

from homeassistant.core import callback, HomeAssistant

from ..battery.plan_shadow import PriceSlot
from ..battery.review_baselines import sun_by_quarter
from ..const import DOMAIN
from ..debug_day import DayStore
from .energy_flow import EnergyHour
from .energy_statistics import async_energy_prefs, async_statistics, energy_flow_ids, short_term_days, summed
from .history_period import PERIOD_DAY, Period, local_date, midnight_utc, period_of
from .house_period import slot_prices
from .sun_value import BASIS_QUARTER, SunDay, day_values, period_value

_LOGGER = logging.getLogger(__name__)

#: How long after local midnight the night's review runs.
RUN_AFTER: Final = timedelta(minutes=20)
#: A day is kept with at least this share of its quarter hours valued.
MIN_DAY_SHARE: Final = 0.9
#: Yesterday short of that is tried again after this long, at most `ATTEMPTS` times a day.
RETRY: Final = timedelta(hours=1)
ATTEMPTS: Final = 4
#: What is kept: each day's figure, and its hours for the most recent days.
DAYS_KEPT: Final = 400
HOURS_KEPT_DAYS: Final = 40
QUARTER: Final = timedelta(minutes=15)

#: Why a run or a day was not reviewed (the diagnostics' `failures`, counted).
FAIL_NO_METERS: Final = "no_meters"
FAIL_NO_SHORT_TERM: Final = "no_short_term"
FAIL_NO_DATA: Final = "no_short_term_data"
FAIL_NO_PRICES: Final = "no_prices"
FAIL_PARTIAL: Final = "partial"
FAIL_ERROR: Final = "error"
#: What the last run found (`status`).
STATUS_OK: Final = "ok"
STATUS_BATTERY: Final = "battery"
STATUS_NO_SUN: Final = "no_sun"


def store_key(entry_id: str) -> str:
    return f"{DOMAIN}.sun_review.{entry_id}"


def review_day(
    day: date,
    tz: str,
    grid_in: Mapping[datetime, float],
    grid_out: Mapping[datetime, float] | None,
    solar: Mapping[datetime, float],
    slots: Sequence[PriceSlot],
) -> tuple[SunDay | None, str | None]:
    """A day valued by the quarter hour from the 5-minute energies (kWh per UTC start; `grid_out` `None` without an
    export meter) at `slots`: the day, or `None` and why (`FAIL_*`)."""
    start, end = midnight_utc(day, tz), midnight_utc(day + timedelta(days=1), tz)
    expected = int((end - start) / QUARTER)
    q_in = sun_by_quarter(grid_in, {})
    q_out = None if grid_out is None else sun_by_quarter(grid_out, {})
    q_sun = sun_by_quarter(solar, {})
    ordered = sorted(slots, key=lambda slot: slot.start)
    starts = [slot.start for slot in ordered]
    measured = 0
    valued: list[tuple[EnergyHour, float]] = []
    moment = start
    while moment < end:
        sun, imported = q_sun.get(moment), q_in.get(moment)
        exported = 0.0 if q_out is None else q_out.get(moment)
        if sun is not None and imported is not None and exported is not None:
            measured += 1
            period = EnergyHour(start=moment, grid_in=imported, grid_out=exported, solar=sun, minutes=15)
            # The slots that may overlap the quarter (a slot is at most a day long).
            near = ordered[bisect_left(starts, moment - timedelta(days=1)) : bisect_right(starts, period.end)]
            prices = slot_prices(period, near)
            value = None if prices is None else period_value(period, *prices)
            if value is not None:
                valued.append((period, value))
        moment += QUARTER
    if measured == 0:
        return None, FAIL_NO_DATA
    if not valued:
        return None, FAIL_NO_PRICES
    if len(valued) < MIN_DAY_SHARE * expected:
        return None, FAIL_PARTIAL
    found = day_values(valued, tz).get(day)
    return (found, None) if found is not None and found.basis == BASIS_QUARTER else (None, FAIL_PARTIAL)


def kept(days: Mapping[date, SunDay], today: date) -> dict[date, SunDay]:
    """`days` within what is kept (`DAYS_KEPT`), the hours only of the last `HOURS_KEPT_DAYS`."""
    out: dict[date, SunDay] = {}
    for day, item in sorted(days.items()):
        age = (today - day).days
        if age > DAYS_KEPT:
            continue
        out[day] = item if age <= HOURS_KEPT_DAYS or not item.hours else item.without_hours()
    return out


class SiteSunReview:
    """One site's nightly sun review (see the module). Never raises into the site."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str | None,
        demand: Any,
        *,
        has_battery: Callable[[], bool],
        prices: Callable[[Period], Sequence[PriceSlot]],
    ) -> None:
        self.hass = hass
        self._demand = demand
        self._has_battery = has_battery
        self._prices = prices
        #: The reviewed days, by local date.
        self.days: dict[date, SunDay] = {}
        self.status: str | None = None
        self.last_run: dict[str, Any] | None = None
        #: Why days or runs were not reviewed, counted since the store began.
        self.failures: dict[str, int] = {}
        #: Older days short of `MIN_DAY_SHARE`: left to the hourly estimate, not read again while the site runs.
        self._given_up: set[date] = set()
        self._done_for: date | None = None
        self._retry_at: datetime | None = None
        self._attempts = 0
        self._running = False
        self._closed = False
        self._loaded = entry_id is None
        self._task: asyncio.Task[None] | None = None
        self._store: DayStore | None = (
            None if entry_id is None else DayStore(hass, store_key(entry_id), self._stored, off_loop=True)
        )

    def _stored(self) -> dict[str, Any]:
        """A fresh snapshot (the store writes it off the loop)."""
        return {
            "days": {day.isoformat(): item.as_dict() for day, item in sorted(self.days.items())},
            "failures": dict(self.failures),
            "last_run": None if self.last_run is None else dict(self.last_run),
        }

    def restore(self, raw: Any) -> None:
        if not isinstance(raw, Mapping):
            return
        days = raw.get("days")
        for key, item in (days.items() if isinstance(days, Mapping) else ()):
            try:
                day = date.fromisoformat(key)
            except (TypeError, ValueError):
                continue
            found = SunDay.from_dict(day, item)
            if found is not None:
                self.days[day] = found
        failures = raw.get("failures")
        for key, count in (failures.items() if isinstance(failures, Mapping) else ()):
            if isinstance(key, str) and isinstance(count, int) and not isinstance(count, bool) and count > 0:
                self.failures[key] = count
        if isinstance(raw.get("last_run"), Mapping):
            self.last_run = dict(raw["last_run"])

    async def async_load(self) -> None:
        if self._store is not None:
            raw = await self._store.async_load()
            try:
                self.restore(raw)
            except Exception:  # noqa: BLE001 - a store that cannot be read starts again
                self.days = {}
                _LOGGER.debug("SpotNav sun review: the kept days could not be read", exc_info=True)
        self._loaded = True

    @callback
    def halt(self) -> None:
        """The site shuts down: no run from here (the store is closed by `async_close`)."""
        self._closed = True
        if self._task is not None and not self._task.done():
            self._task.cancel()

    async def async_close(self) -> None:
        self.halt()
        if self._store is not None:
            await self._store.async_close()

    @staticmethod
    async def async_remove_stored(hass: HomeAssistant, entry_id: str) -> None:
        """The site is gone for good: forget its reviewed days."""
        await DayStore.async_remove(hass, store_key(entry_id))

    @callback
    def tick(self) -> None:
        """Called on each reading of the site: start the night's run once the day is `RUN_AFTER` old (and a retry
        when due)."""
        if not self._loaded or self._running or self._closed:
            return
        now = self._demand.now()
        tz = self._demand.tracker.tz
        today = local_date(now, tz)
        if now - midnight_utc(today, tz) < RUN_AFTER:
            return
        if today == self._done_for and (self._retry_at is None or now < self._retry_at):
            return
        self._running = True
        # Short (one statistics read in the recorder's executor and a day's arithmetic per day): a tracked task.
        self._task = self.hass.async_create_task(self.async_review(), "spotnav_sun_review", eager_start=False)

    async def async_review(self) -> None:
        """Review the days not reviewed yet within the 5-minute statistics. Never raises."""
        self._running = True
        now = self._demand.now()
        tz = self._demand.tracker.tz
        today = local_date(now, tz)
        if today != self._done_for:
            self._done_for, self._attempts = today, 0
        self._attempts += 1
        self._retry_at = None
        reviewed: list[str] = []
        failed: dict[str, str] = {}
        try:
            status, retry = await self._async_run(now, tz, today, reviewed, failed)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a shadow review must never disturb the site
            _LOGGER.debug("SpotNav sun review: the run failed", exc_info=True)
            status, retry = FAIL_ERROR, True
            self._count(FAIL_ERROR)
        finally:
            self._running = False
        if retry and self._attempts < ATTEMPTS:
            self._retry_at = now + RETRY
        self.status = status
        self.last_run = {
            "at": now.isoformat(),
            "status": status,
            "reviewed": reviewed,
            "failed": failed,
            "attempt": self._attempts,
        }
        self.days = kept(self.days, today)
        if self._store is not None:
            self._store.changed()

    def _count(self, reason: str) -> None:
        self.failures[reason] = self.failures.get(reason, 0) + 1

    async def _async_run(
        self, now: datetime, tz: str, today: date, reviewed: list[str], failed: dict[str, str]
    ) -> tuple[str, bool]:
        """The run's status and whether to try again later; fills `reviewed` and `failed` (day: why)."""
        if self._has_battery():
            return STATUS_BATTERY, False
        ids = energy_flow_ids(await async_energy_prefs(self.hass))
        if not ids.grid_in:
            self._count(FAIL_NO_METERS)
            return FAIL_NO_METERS, False
        if not ids.solar:
            return STATUS_NO_SUN, False
        keep = short_term_days(self.hass)
        if keep is None:
            self._count(FAIL_NO_SHORT_TERM)
            return FAIL_NO_SHORT_TERM, False
        reach = now - timedelta(days=keep)
        yesterday = today - timedelta(days=1)
        wanted = [
            day
            for day in (today - timedelta(days=back) for back in range(keep, 0, -1))
            if midnight_utc(day, tz) >= reach and day not in self.days and day not in self._given_up
        ]
        if not wanted:
            return STATUS_OK, False
        rows = await async_statistics(
            self.hass, (*ids.grid_in, *ids.grid_out, *ids.solar), midnight_utc(wanted[0], tz), midnight_utc(today, tz), "5minute"
        )
        grid_in = summed(rows, ids.grid_in)
        grid_out = summed(rows, ids.grid_out) if ids.grid_out else None
        solar = summed(rows, ids.solar)
        prices = {day: self._prices(period_of(PERIOD_DAY, day)) for day in wanted}

        def review_days() -> list[tuple[date, SunDay | None, str | None]]:
            return [(day, *review_day(day, tz, grid_in, grid_out, solar, prices[day])) for day in wanted]

        # Up to ten days of quarter hours: in the executor, off the loop.
        results = await self.hass.async_add_executor_job(review_days)
        retry = False
        for day, found, reason in results:
            if found is not None:
                self.days[day] = found
                reviewed.append(day.isoformat())
                continue
            reason = reason or FAIL_PARTIAL
            failed[day.isoformat()] = reason
            self._count(reason)
            if day == yesterday:
                retry = True
            else:
                self._given_up.add(day)
        return failed.get(yesterday.isoformat(), STATUS_OK), retry

    def diagnostics(self) -> dict[str, Any]:
        """The debug bundle's view: the last run, what is kept by basis, the failures, the last week's days."""
        bases: dict[str, int] = {}
        for item in self.days.values():
            bases[item.basis] = bases.get(item.basis, 0) + 1
        last = sorted(self.days.items())[-7:]
        return {
            "status": self.status,
            "last_run": self.last_run,
            "days": len(self.days),
            "days_with_hours": sum(1 for item in self.days.values() if item.hours),
            "basis": bases,
            "failures": dict(self.failures),
            "given_up": sorted(day.isoformat() for day in self._given_up),
            "retry_at": None if self._retry_at is None else self._retry_at.isoformat(),
            "last_days": [
                {"date": day.isoformat(), "value": round(item.value, 2), "periods": item.periods, "basis": item.basis}
                for day, item in last
            ],
        }
