"""The price history's backfill: the past days an area's profile and statistics need, asked for gently.

The history profile (`pricing/price_profile.py`) is built from the held market-day files of the last 28 market
days and needs 21 of them. An installation that starts on a provider (a new install, a `nordpool` entry added, an
ENTSO-E key set, a new area, Finland moving from the relay's Helsinki days to CET days) holds only what the refresh
manager fetched since: today and tomorrow. [HistoryBackfill] fills the rest from the area's provider, in the
background, on the calendar the area is read in now:

* **The window**: the profile's 28 market days ending yesterday (plus the day before for an area whose display day
  is cut from two files, so every display day of the window is whole), minus the days held. A provider that keeps
  only so much (`history_reach`: Nord Pool about two months) is never asked beyond it.
* **A year, for statistics**, only from a provider that keeps years and answers a range in one request (ENTSO-E,
  `async_fetch_days`): after the window, the market days back to [DEEP_DAYS], one request of up to a month every
  [DEEP_GAP_S]: about a dozen requests per area, once. The store keeps [price_repository.HISTORY_KEEP_DAYS], so
  what is filled stays. A provider asked one day at a time (Nord Pool, the relay) fills the window only.
* **Pace.** One request at a time for the whole installation, one area after the other (every window before any
  year). A provider asked one day at a time gets one request per day, [HISTORY_GAP_S] apart (the session import's
  pause); a range provider one request per run of missing days (at most a month), [HISTORY_GAP_S] apart in the
  window. Days held are never asked again; a day the provider answered without prices is not asked again for
  [MISS_RETRY] (a year's day for [DEEP_MISS_RETRY]).
* **Today and tomorrow first.** Nothing starts before the first refresh cycle at least [START_DELAY_S] after Home
  Assistant has started;
  an area waits until its today is held; every request waits for a refresh cycle in flight, and none is made while
  an active area is inside its publication window still waiting for tomorrow.
* **Failures** (a provider that cannot be reached, a rate limit) stop that area's run and try it again later
  ([BACKOFF_STEPS], at least the provider's own `retry_after`); the rest of the queue goes on. A change of provider
  starts over on the new one. No notice: nothing here is the person's to act on.

Logs: one info line when an area's fill starts and one when it ends (or pauses), never one per day.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from functools import partial
from typing import Any, Final, Literal, Protocol

from homeassistant.core import callback, HomeAssistant
from homeassistant.helpers.event import async_track_point_in_utc_time
from homeassistant.helpers.start import async_at_started
from homeassistant.util import dt as dt_util

from .price_profile import profile_window
from .price_repository import HISTORY_KEEP_DAYS, PriceRepository
from .providers import market_today, PriceProvider

_LOGGER = logging.getLogger(__name__)

#: The pause before each request after the first (the session history import's pause).
HISTORY_GAP_S: Final = 2.0

#: How far back statistics are filled from a provider that keeps years, and the pause between its requests.
DEEP_DAYS: Final = 365
DEEP_GAP_S: Final = 120.0

#: How long after Home Assistant has started the backfill begins (start-up is busy enough).
START_DELAY_S: Final = 60.0

#: How long to wait before looking again while an area waits for tomorrow inside its publication window.
BUSY_WAIT_S: Final = 60.0

#: The store is written after this many new days (and at the end of a run), not after each one.
PERSIST_EVERY: Final = 7

#: A day the provider answered without prices is not asked again for this long.
MISS_RETRY: Final = timedelta(days=1)
DEEP_MISS_RETRY: Final = timedelta(days=7)

#: After a failed request, an area's next try; the last step repeats.
BACKOFF_STEPS: Final[tuple[timedelta, ...]] = (
    timedelta(minutes=10),
    timedelta(minutes=30),
    timedelta(hours=2),
    timedelta(hours=6),
)

Phase = Literal["window", "year"]


class HistoryGate(Protocol):
    """What the backfill asks of the refresh manager, so today and tomorrow always come first."""

    async def async_wait_idle(self) -> None:
        """Return when no refresh cycle is in flight."""

    def history_busy(self) -> bool:
        """Whether an active area is inside its publication window still waiting for tomorrow."""

    def history_wanted(self, area_id: str) -> bool:
        """Whether an area is still in use and its today is held (its turn may start)."""


@dataclass(slots=True)
class _AreaState:
    """One area's retry state: how many failures in a row, and when it may be tried again (by which provider)."""

    step: int = 0
    next_try: datetime | None = None
    provider: str | None = None
    cancel_retry: Callable[[], None] | None = None


@dataclass(slots=True)
class _Run:
    stored: int = 0
    missing: int = 0


def _days(first: date, last: date) -> list[date]:
    """`first` to `last`, newest first."""
    count = (last - first).days + 1
    return [last - timedelta(days=offset) for offset in range(max(count, 0))]


class HistoryBackfill:
    """The installation's one backfill queue, fed by the refresh manager after each cycle ([request])."""

    def __init__(
        self,
        hass: HomeAssistant,
        repository: PriceRepository,
        gate: HistoryGate,
        *,
        now: Callable[[], datetime] | None = None,
        sleep: Callable[[float], Any] | None = None,
        scheduler: Callable[[HomeAssistant, Callable[[datetime], None], datetime], Callable[[], None]] | None = None,
    ) -> None:
        self._hass = hass
        self._repository = repository
        self._gate = gate
        self._now = now if now is not None else dt_util.utcnow
        self._sleep = sleep if sleep is not None else asyncio.sleep
        self._scheduler = scheduler if scheduler is not None else async_track_point_in_utc_time
        self._queue: list[tuple[str, Phase]] = []
        self._areas: dict[str, _AreaState] = {}
        # Days a provider answered without prices, per (area, calendar, day), with when.
        self._misses: dict[tuple[str, str, date], datetime] = {}
        self._task: asyncio.Task[None] | None = None
        # From when the worker may run; `None` until Home Assistant has started ([arm_after_start], [arm]).
        self._ready_at: datetime | None = None
        self._stopped = False
        # Whether this worker has asked anything yet (the first request needs no pause).
        self._asked = False
        self._cancel_arm: Callable[[], None] | None = None

    # ------------------------------------------------------------------ control

    @callback
    def arm_after_start(self) -> None:
        """Start working with the first cycle at least [START_DELAY_S] after Home Assistant has started (no timer of
        its own: the refresh manager's cycles, which come at least every hour, carry it)."""

        @callback
        def started(_hass: HomeAssistant) -> None:
            self._cancel_arm = None
            self._ready_at = self._now() + timedelta(seconds=START_DELAY_S)

        self._cancel_arm = async_at_started(self._hass, started)

    @callback
    def arm(self) -> None:
        """Start working now (what [arm_after_start] does once Home Assistant has settled)."""
        if self._stopped:
            return
        self._ready_at = self._now()
        self._start()

    @callback
    def request(self, area_ids: Iterable[str]) -> None:
        """Fill these areas' history when they need it; an area waiting out a failure is left to its retry.

        Cheap when nothing is missing: the worker finds that without a request."""
        if self._stopped:
            return
        now = self._now()
        for area_id in area_ids:
            state = self._areas.get(area_id)
            provider = self._repository.provider_for(area_id).key
            if (
                state is not None
                and state.next_try is not None
                and now < state.next_try
                and state.provider == provider
            ):
                continue
            if (area_id, "window") not in self._queue:
                self._queue.append((area_id, "window"))
        self._start()

    async def async_stop(self) -> None:
        """Stop for good: no further request, retry or timer."""
        self._stopped = True
        self._queue.clear()
        if self._cancel_arm is not None:
            self._cancel_arm()
            self._cancel_arm = None
        for state in self._areas.values():
            if state.cancel_retry is not None:
                state.cancel_retry()
                state.cancel_retry = None
        task = self._task
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def async_wait(self) -> None:
        """Wait until the queue is empty (for tests; the work runs on its own)."""
        while (task := self._task) is not None and not task.done():
            await asyncio.gather(task, return_exceptions=True)

    def summary(self, area_id: str) -> dict[str, Any]:
        """Diagnostics: whether the area waits for a retry, and until when."""
        state = self._areas.get(area_id)
        return {
            "queued": any(item[0] == area_id for item in self._queue),
            "failures": 0 if state is None else state.step,
            "next_try": None if state is None or state.next_try is None else state.next_try.isoformat(),
        }

    def _start(self) -> None:
        if self._ready_at is None or self._now() < self._ready_at or self._stopped or not self._queue:
            return
        if self._task is not None and not self._task.done():
            return
        self._asked = False
        self._task = self._hass.async_create_background_task(self._work(), "spotnav price history backfill")

    # ------------------------------------------------------------------ the work

    async def _work(self) -> None:
        while self._queue and not self._stopped:
            # Every area's window before any year.
            item = next((queued for queued in self._queue if queued[1] == "window"), self._queue[0])
            self._queue.remove(item)
            area_id, phase = item
            try:
                if await self._fill(area_id, phase) and phase == "window":
                    self._queue.append((area_id, "year"))
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001 - one area must not stop the rest
                _LOGGER.warning("Filling the price history of %s failed: %s", area_id, type(err).__name__)

    def _wanted(self, area_id: str, calendar: str, day: date, retry: timedelta) -> bool:
        """A day worth asking for: not held, within the provider's reach, not answered empty lately."""
        if self._repository.holds_file(area_id, day) or not self._repository.history_reachable(area_id, day):
            return False
        missed = self._misses.get((area_id, calendar, day))
        return missed is None or self._now() - missed >= retry

    def _plan(self, area_id: str, phase: Phase) -> tuple[PriceProvider, str, list[date]] | None:
        """The provider, its calendar and the days to ask for (newest first), or `None` for an area not listed."""
        entry = self._repository.area_entry(area_id)
        if entry is None:
            return None
        calendar = entry.market_tz
        provider = self._repository.provider_for(area_id)
        now = self._now()
        today = market_today(now, calendar)
        first, _ = profile_window(now, calendar)
        if entry.split_calendar:
            first -= timedelta(days=1)
        if phase == "window":
            return provider, calendar, [day for day in _days(first, today - timedelta(days=1))
                                        if self._wanted(area_id, calendar, day, MISS_RETRY)]
        if not _fills_a_year(provider):
            return provider, calendar, []
        oldest = today - timedelta(days=min(DEEP_DAYS, HISTORY_KEEP_DAYS - 1))
        return provider, calendar, [day for day in _days(oldest, first - timedelta(days=1))
                                    if self._wanted(area_id, calendar, day, DEEP_MISS_RETRY)]

    async def _fill(self, area_id: str, phase: Phase) -> bool:
        """One area's phase; `True` when it ended without a failure (nothing missing counts)."""
        if not self._gate.history_wanted(area_id):
            return False
        plan = self._plan(area_id, phase)
        if plan is None:
            return False
        provider, calendar, days = plan
        if not days:
            return True
        what = "window" if phase == "window" else "year"
        _LOGGER.info(
            "Filling the price history of %s from %s: %d day(s) of the %s", area_id, provider.key, len(days), what
        )
        run = _Run()
        gap = HISTORY_GAP_S if phase == "window" else DEEP_GAP_S
        pending = 0
        outcome: bool | None = None
        for batch in _batches(provider, days):
            if self._asked:
                await self._sleep(gap)
            await self._turn()
            if (
                self._stopped
                or not self._gate.history_wanted(area_id)
                or self._repository.provider_for(area_id) is not provider
            ):
                # No longer used, or another provider now: the next cycle asks again if it still matters.
                outcome = False
                break
            self._asked = True
            result = await self._repository.async_fetch_history(area_id, batch, persist=False)
            run.stored += len(result.stored)
            pending += len(result.stored)
            now = self._now()
            for day in result.missing:
                self._misses[(area_id, calendar, day)] = now
            run.missing += len(result.missing)
            if result.switched:
                outcome = False
                break
            if result.failed is not None:
                self._back_off(area_id, provider, result.failed, result.retry_after)
                outcome = False
                break
            self._areas.setdefault(area_id, _AreaState()).step = 0
            if pending >= PERSIST_EVERY:
                await self._repository.async_persist_history()
                pending = 0
        if pending:
            await self._repository.async_persist_history()
        if outcome is None:
            state = self._areas.get(area_id)
            if state is not None:
                state.next_try = None
            usable = self._repository.profile_for(area_id) is not None
            _LOGGER.info(
                "Filled the price history of %s (%s): %d day(s) stored, %d without prices; the profile is %s",
                area_id, what, run.stored, run.missing, "usable" if usable else "not usable yet",
            )
            return True
        return False

    async def _turn(self) -> None:
        """Wait until a request may go: no refresh cycle in flight and no area waiting for tomorrow."""
        while True:
            await self._gate.async_wait_idle()
            if not self._gate.history_busy():
                return
            await self._sleep(BUSY_WAIT_S)

    def _back_off(self, area_id: str, provider: PriceProvider, code: str, retry_after: timedelta | None) -> None:
        state = self._areas.setdefault(area_id, _AreaState())
        wait = BACKOFF_STEPS[min(state.step, len(BACKOFF_STEPS) - 1)]
        if retry_after is not None:
            wait = max(wait, retry_after)
        state.step += 1
        state.provider = provider.key
        state.next_try = self._now() + wait
        if state.cancel_retry is not None:
            state.cancel_retry()
        state.cancel_retry = self._scheduler(self._hass, partial(self._retry, area_id), state.next_try)
        _LOGGER.info(
            "Filling the price history of %s paused (%s); trying again in %d minute(s)",
            area_id, code, int(wait.total_seconds() // 60),
        )

    @callback
    def _retry(self, area_id: str, _fired: datetime) -> None:
        state = self._areas.get(area_id)
        if state is not None:
            state.cancel_retry = None
            state.next_try = None
        self.request((area_id,))


def _fills_a_year(provider: PriceProvider) -> bool:
    """Whether a provider is asked for a year: it keeps years and answers a range in one request."""
    return provider.history_reach is None and not provider.has_index and hasattr(provider, "async_fetch_days")


def _batches(provider: PriceProvider, days: list[date]) -> list[list[date]]:
    """The requests for [days] (newest first): one per day, or for a range provider one per run of consecutive
    days, at most its `history_range_days`, each oldest first."""
    span = int(getattr(provider, "history_range_days", 1) or 1) if hasattr(provider, "async_fetch_days") else 1
    batches: list[list[date]] = []
    for day in days:
        current = batches[-1] if batches else None
        if current is not None and len(current) < span and current[0] - timedelta(days=1) == day:
            current.insert(0, day)
        else:
            batches.append([day])
    return batches
