"""The year's review for one site: the shell around `year_review/compute.py` and `year_review/store.py`.

**When it runs**: every night at `RUN_AT` local time the day parts of the last `MERGE_DAYS` days are merged into the
store (all year: the parts whose sources keep fewer days than a year are kept so), and each finished day's price part
(`_async_merge_prices`); from 10 to 31 December and on 1
January (`compute.in_run_window`) the review is then worked out again, the year's final one on 1 January, kept through
January. Once at start-up, `STARTUP_DELAY_S` after the site started: inside that window the review when the store holds
none of the year, else the price parts only. And on `spotnav.show_year_review` (`async_show`). Never on a read: the
cards and the service read the kept review (`answer`).

**The price parts** (`compute.PriceDay`, about 500 bytes a day): each finished local day's prices as the review reads
them, worked out from the held day documents with that day's fees (`pricing/period_prices.period_price_slots` over a
few days, on the loop: it reads the price source and the held documents, no I/O) and kept in the store, so the year's
slots are never read at once. A day without a part is worked out once, `PRICE_DAYS` back (the longest price history
held); a day whose part covers less than the whole day again while it is `PRICE_RETRY_DAYS` recent; a part kept
before it had the hourly buy prices (`PriceDay.buys` `None`) once more, and where the day's prices are no longer held
it is kept as it was, marked looked at (`buys` `()`), so it is not read again. At most
`PRICE_BATCH` days in one loop turn, yielding between: the first run after the feature arrives fills up to a year of
days (Nord Pool's 55 days, ENTSO-E's or the relay's up to 365) in about fifty short turns, every later night one day.
Nothing is written while yesterday has no price held (the prices not settled after a start, or no price source): a
missing day is then not taken for one without prices.

**What is read**, on the event loop only from memory, the statistics through the house card's own readers:

* The year's energy by the hour: `api/house_history.history_with_periods` (the house card's year tab, its answer and
  its hours kept; the Energy dashboard's meters and the chargers' registers from the recorder's long-term statistics
  in the recorder's executor, `recorder.get_instance(hass).async_add_executor_job`).
* What SpotNav saved per day: `api/saving.async_day_savings` (the sessions with their hours from the same statistics,
  cached once settled; the battery's day money and the heat's reviews in memory), `MERGE_CHUNK` consecutive days a
  loop turn, yielding between. The review merges only the days the store has not merged yet (`store.merged`, kept
  across restarts) and the last `MERGE_DAYS` again, so a restart in December does not merge the whole year again.
* The sessions (the session store, costed with the current settings, its own cache), the year store's day parts (the
  prices among them), the battery's day money and capacity, the site's set-up day and its chargers' first live
  sessions.

The computation over the year's hours and sessions runs in an executor job (`hass.async_add_executor_job`); the store
is written off the loop. One run at a time for the installation (a lock): sites one after another. A run that fails
keeps the review before it.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import replace
from datetime import date, datetime, timedelta
import logging
from typing import TYPE_CHECKING, Any, Final

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_call_later, async_track_time_change
from homeassistant.util import dt as dt_util

from .. import time_zones
from ..const import CONF_CHARGER_ENTRY_IDS, DOMAIN
from ..runtime import domain_data, entry_task
from ..site.history_period import PERIOD_DAY, PERIOD_YEAR, Period, local_date, midnight_utc
from .compute import (
    STATE_COMPUTING,
    STATE_NONE,
    STATE_READY,
    DayParts,
    PriceDay,
    YearInputs,
    compute_review,
    day_parts_of,
    estimate_days,
    in_run_window,
    in_show_window,
    price_days_of,
    review_answer,
    review_year,
    year_bounds,
)
from .store import YearReviewStore

if TYPE_CHECKING:
    from ..battery.plan_shadow import PriceSlot
    from ..sessions.model import ChargeSession

_LOGGER = logging.getLogger(__name__)

#: The nightly run, local time: after the nightly reviews (the battery's, the sun's, the heat's) and the sessions'.
RUN_AT: Final = (4, 40)
#: The days back the nightly merge lays over the store (the shortest-kept source, the heat's review, keeps 35).
MERGE_DAYS: Final = 40
#: The start-up run waits this long after the site started (the recorder and the prices settle first).
STARTUP_DELAY_S: Final = 300.0
#: How far back a day without a price part is worked out (the longest price history held, a year of day documents).
PRICE_DAYS: Final = 366
#: A day whose price part covers less than the whole day is worked out again while it is this recent (late prices).
PRICE_RETRY_DAYS: Final = 7
#: The days of price parts worked out in one loop turn.
PRICE_BATCH: Final = 7
#: The days of savings merged in one loop turn (a month: the sessions' price slots and hours of that month).
MERGE_CHUNK: Final = 31
#: How long the service's showing offers the banner and the Support row.
FORCED_FOR: Final = timedelta(days=7)
_LOCK: Final = f"{DOMAIN}_year_review_lock"


class YearReviewRefusal(Exception):
    """A review that cannot be worked out: `code` says why (`no_year`, `not_loaded`)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _lock(hass: HomeAssistant) -> asyncio.Lock:
    lock = hass.data.get(_LOCK)
    if lock is None:
        lock = hass.data[_LOCK] = asyncio.Lock()
    return lock


class SiteYearReview:
    """One site's year review (see the module). Never raises into the site."""

    def __init__(self, hass: HomeAssistant, entry_id: str, controller: Any) -> None:
        self.hass = hass
        self.entry_id = entry_id
        self._controller = controller
        self.store = YearReviewStore(hass, entry_id)
        self.running = False
        self._closed = False
        self._cancels: list[Any] = []
        self._task: asyncio.Task[Any] | None = None
        #: The start-up's price parts outside the window: apart from `_task`, so a night that comes meanwhile still
        #: runs (after it: the lock).
        self._prices_task: asyncio.Task[Any] | None = None

    # ------------------------------------------------------------------------------------------------ the life

    @property
    def _tz(self) -> str:
        return self._controller.demand.tracker.tz

    def now(self) -> datetime:
        return self._controller.demand.now()

    def today(self) -> date:
        return local_date(self.now(), self._tz)

    async def async_start(self) -> None:
        """Read the store, count the fuse's episodes, and arm the nightly run (and the start-up one where due)."""
        today = self.today()
        await self.store.async_load(today)
        self._controller.overload_episodes.set_listener(self._on_episode)
        self._cancels.append(
            async_track_time_change(self.hass, self._at_run_time, hour=RUN_AT[0], minute=RUN_AT[1], second=0)
        )
        self._cancels.append(async_call_later(self.hass, STARTUP_DELAY_S, self._at_startup))

    async def async_close(self) -> None:
        self._closed = True
        for cancel in self._cancels:
            cancel()
        self._cancels = []
        self._controller.overload_episodes.set_listener(None)
        for task in (self._task, self._prices_task):
            if task is not None and not task.done():
                task.cancel()
        await self.store.async_close()

    @staticmethod
    async def async_remove_stored(hass: HomeAssistant, entry_id: str) -> None:
        await YearReviewStore.async_remove_stored(hass, entry_id)

    @callback
    def _on_episode(self, moment: datetime) -> None:
        self.store.add_fuse(local_date(moment, self._tz))

    @callback
    def _at_run_time(self, _now: datetime) -> None:
        self._start_task(self.async_nightly(), "spotnav year review night")

    @callback
    def _at_startup(self, _now: datetime) -> None:
        today = self.today()
        if in_run_window(today) and self.store.year != review_year(today):
            self._start_task(self.async_nightly(), "spotnav year review start-up")
        else:
            target = self.async_prices()
            if self._closed:
                target.close()
                return
            self._prices_task = entry_task(
                self.hass, self.entry_id, target, "spotnav year review prices", eager_start=False
            )

    def _start_task(self, target: Any, name: str) -> None:
        if self._closed or (self._task is not None and not self._task.done()):
            target.close()
            return
        self._task = entry_task(self.hass, self.entry_id, target, name, eager_start=False)

    async def async_nightly(self) -> None:
        """The night's merge, and in the window the review. Never raises."""
        today = self.today()
        try:
            async with _lock(self.hass):
                if in_run_window(today):
                    # Its merge covers the whole year.
                    await self._async_compute(review_year(today), today)
                else:
                    await self._async_merge([today - timedelta(days=back) for back in range(MERGE_DAYS, 0, -1)], today)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - the review before stays
            _LOGGER.warning("SpotNav site %s: the year's review could not be worked out", self.entry_id, exc_info=True)

    async def async_prices(self) -> None:
        """The price parts only (the start-up outside the window). Never raises."""
        try:
            async with _lock(self.hass):
                await self._async_merge_prices(self.today())
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - the parts are worked out again the next night
            _LOGGER.warning(
                "SpotNav site %s: the year's price parts could not be worked out", self.entry_id, exc_info=True
            )

    # ------------------------------------------------------------------------------------------------ the run

    async def async_run(self, year: int) -> dict[str, Any]:
        """Work the review of `year` out now (the service), or a `YearReviewRefusal`."""
        today = self.today()
        if year_bounds(year, today) is None:
            raise YearReviewRefusal("no_year", "The year has no day behind it yet")
        async with _lock(self.hass):
            review = await self._async_compute(year, today)
        if review is None:
            raise YearReviewRefusal("no_year", "The year has no day behind it yet")
        return review

    async def _async_merge(self, days: list[date], today: date) -> None:
        """Merge the savings of `days` (in order) into the store, `MERGE_CHUNK` consecutive days a loop turn."""
        from ..api.saving import async_day_savings  # noqa: PLC0415 - the API imports the site

        await self._async_merge_prices(today)
        if not days:
            return
        all_estimates = estimate_days(self._sessions(), time_zones.require_zone(self._tz))
        for chunk in _batches(days, MERGE_CHUNK):
            if self._closed:
                return
            savings, _since = await async_day_savings(self.hass, self.entry_id, self._controller, chunk)
            wanted = set(chunk)
            estimates = {day: value for day, value in all_estimates.items() if day in wanted}
            self.store.merge_parts(day_parts_of(savings, estimates), today, merged=chunk)
            await asyncio.sleep(0)

    def _merge_due(self, days: list[date], today: date) -> list[date]:
        """The days of `days` the review merges: those not merged yet, and the last `MERGE_DAYS` again (their
        sources may still change)."""
        merged = self.store.merged
        return [day for day in days if day not in merged or (today - day).days <= MERGE_DAYS]

    async def _async_merge_prices(self, today: date) -> None:
        """Work out the price part of each finished day that is due (see the module), `PRICE_BATCH` days a turn."""
        zone = time_zones.require_zone(self._tz)
        yesterday = today - timedelta(days=1)
        due = [
            day
            for day in (today - timedelta(days=back) for back in range(PRICE_DAYS, 0, -1))
            if self._price_due(day, today)
        ]
        # Newest first: yesterday's says whether the prices have settled.
        for batch in reversed(_batches(due, PRICE_BATCH)):
            if self._closed:
                return
            slots = self._read_price_slots(batch[0], batch[-1] + timedelta(days=1))
            if slots is None:
                return
            found = price_days_of(slots, zone)
            if yesterday in batch and not found.get(yesterday, PriceDay()).runs:
                return
            parts: dict[date, DayParts] = {}
            for day in batch:
                price = found.get(day, PriceDay())
                held = self.store.day_parts.get(day)
                held_price = None if held is None else held.price
                # A part worked out again never covers less than the one held.
                if held_price is None or price.covered_s > held_price.covered_s:
                    parts[day] = DayParts(price=price)
                elif held_price.buys is None:
                    # Kept before the buys were: the new part where it covers as much, else the old one looked at.
                    fresh = price if price.runs and price.covered_s >= held_price.covered_s else None
                    parts[day] = DayParts(price=fresh or replace(held_price, buys=()))
            self.store.merge_parts(parts, today)
            await asyncio.sleep(0)

    def _read_price_slots(self, first: date, end: date) -> list[PriceSlot] | None:
        """The price slots of the days `first` to before `end` (and a day either side) with the fees as set now,
        from the held day documents (on the loop: the price source and the documents are in memory); `None` without
        a price source, its area or its fees."""
        from ..pricing.period_prices import period_price_slots  # noqa: PLC0415 - it imports the planner
        from ..pricing.price_source import site_price_source  # noqa: PLC0415

        source = site_price_source(self.hass, self.entry_id)
        if source is None or source.area is None or source.fiscal is None:
            return None
        return period_price_slots(self.hass, self.entry_id, Period(PERIOD_DAY, first, end))

    def _price_due(self, day: date, today: date) -> bool:
        """A day without a price part, or a recent one whose part covers less than the whole day."""
        held = self.store.day_parts.get(day)
        price = None if held is None else held.price
        if price is None or price.buys is None:
            return True
        if (today - day).days > PRICE_RETRY_DAYS:
            return False
        length = (midnight_utc(day + timedelta(days=1), self._tz) - midnight_utc(day, self._tz)).total_seconds()
        return price.covered_s < length

    def _sessions(self) -> list[ChargeSession]:
        """The site's chargers' sessions, closed and open, costed with the current settings (the store's cache)."""
        store = domain_data(self.hass).session_store
        if store is None:
            return []
        out: list[ChargeSession] = []
        for charger_entry_id in self._controller.config.get(CONF_CHARGER_ENTRY_IDS) or []:
            out.extend(store.closed(charger_entry_id))
            if (open_session := store.open_session(charger_entry_id)) is not None:
                out.append(open_session)
        return out

    async def _async_compute(self, year: int, today: date) -> dict[str, Any] | None:
        bounds = year_bounds(year, today)
        if bounds is None:
            return None
        first, through = bounds
        self.running = True
        try:
            days = Period(PERIOD_YEAR, first, through + timedelta(days=1)).days()
            await self._async_merge(self._merge_due(days, today), today)
            inputs = await self._async_inputs(year, today, first, through)
            review = await self.hass.async_add_executor_job(compute_review, inputs)
        finally:
            self.running = False
        if review is not None and not self._closed:
            self.store.set_review(year, through, review, dt_util.utcnow())
        return review

    async def _async_inputs(self, year: int, today: date, first: date, through: date) -> YearInputs:
        """Everything the computation reads (see the module), gathered on the loop from memory and the readers."""
        from ..api.house_history import history_with_periods  # noqa: PLC0415 - the API imports the site
        from ..pricing.price_source import site_price_source  # noqa: PLC0415
        from ..site.energy_statistics import async_energy_prefs, energy_flow_ids  # noqa: PLC0415

        hass = self.hass
        controller = self._controller
        entry = hass.config_entries.async_get_entry(self.entry_id)
        if entry is None:
            raise YearReviewRefusal("not_loaded", "The site is not loaded")
        _answer, hours = await history_with_periods(hass, entry, PERIOD_YEAR, first.isoformat())
        try:
            ids = energy_flow_ids(await async_energy_prefs(hass))
        except Exception:  # noqa: BLE001 - without the dashboard's meters the pages from them are left out
            ids = None
        source = site_price_source(hass, self.entry_id)
        area = None if source is None else source.area
        battery = controller.battery_shadow
        stored = getattr(battery.plan, "stored", None) if battery.facts.battery_entity_id is not None else None
        money_days: Mapping[date, Any] = {} if stored is None else stored.days
        return YearInputs(
            year=year,
            today=today,
            tz=self._tz,
            money=None if area is None else (area.currency, area.major_unit, area.minor_unit),
            record_start=self._record_start(entry),
            hours=None if hours is None else list(hours),
            has_solar=bool(ids is not None and ids.solar),
            has_export=bool(ids is not None and ids.grid_out),
            has_battery=bool(ids is not None and (ids.battery_in or ids.battery_out)),
            sessions=self._sessions(),
            day_parts=dict(self.store.day_parts),
            # Copies of the finished days only (today's is still being counted): the executor never reads what the
            # loop may change.
            battery_days={
                day: replace(money, hours={}, hour_flows={}, flow=None if money.flow is None else replace(money.flow))
                for day, money in money_days.items()
                if first <= day <= through
            },
            capacity_kwh=battery.facts.capacity_kwh,
            fuse_since=self.store.fuse_since,
        )

    def _record_start(self, entry: Any) -> date | None:
        """The first day of SpotNav's own record: the site's set-up, or a charge recorded live before it."""
        starts: list[datetime] = []
        created = getattr(entry, "created_at", None)
        if isinstance(created, datetime):
            starts.append(created)
        store = domain_data(self.hass).session_store
        if store is not None:
            for charger_entry_id in self._controller.config.get(CONF_CHARGER_ENTRY_IDS) or []:
                first = store.first_live_start(charger_entry_id)
                if first is not None:
                    starts.append(first)
        return local_date(min(starts), self._tz) if starts else None

    # ------------------------------------------------------------------------------------------------ the reads

    def shown_year(self, now: datetime) -> int:
        """The year the cards offer: the service's while it is forced, else the year rule's."""
        if self.store.forced(now) and self.store.year is not None:
            return self.store.year
        return review_year(local_date(now, self._tz))

    def answer(self, user_id: str | None, *, phone: bool = False) -> dict[str, Any]:
        """`spotnav/get_year_review`'s answer for one person (`compute.review_answer`), from the kept review; with
        `phone`, for the paired phone whose key `user_id` is (the webhook's `get_year_review`), by the same rules."""
        now = self.now()
        year = self.shown_year(now)
        offered = in_show_window(local_date(now, self._tz)) or self.store.forced(now)
        ready = self.store.review is not None and self.store.year == year
        state = STATE_READY if ready else (STATE_COMPUTING if self.running else STATE_NONE)
        viewed, dismissed = self.store.seen(user_id, year, phone=phone)
        return review_answer(
            site_id=self.entry_id,
            year=year,
            state=state,
            banner=offered and not viewed and not dismissed,
            row=offered,
            review=self.store.review if ready else None,
        )

    async def async_show(self, year: int | None, reset: bool) -> dict[str, Any]:
        """The service: work the review out now, offer it for `FORCED_FOR`, forget who saw it (`reset`)."""
        now = self.now()
        chosen = review_year(local_date(now, self._tz)) if year is None else year
        review = await self.async_run(chosen)
        until = now + FORCED_FOR
        self.store.force(until)
        if reset:
            self.store.reset_users()
        return {"site_id": self.entry_id, "year": chosen, "forced_until": until.isoformat(), "review": review}


def _batches(days: list[date], size: int) -> list[list[date]]:
    """`days` (in order) as runs of consecutive days, `size` at most each."""
    out: list[list[date]] = []
    for day in days:
        if out and len(out[-1]) < size and out[-1][-1] + timedelta(days=1) == day:
            out[-1].append(day)
        else:
            out.append([day])
    return out
