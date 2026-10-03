"""The shared price refresh manager: when to ask, and who hears about the answer.

The repository (`pricing/price_repository.py`) fetches and validates; this module is the *when*:
one manager per Home Assistant instance, next to the repository on `SpotNavData`, owning every
price timer.

* The work is keyed by area, not consumer: two chargers on SE4 share one index request, one day
  request and one pair of timers, and unsubscribing one leaves the other's stream untouched.
* "Today" and "tomorrow" are the area's local dates, computed from the injected clock and the
  area's IANA timezone (never the host's zone), so 23- and 25-hour days come out right; date keys
  and the rollover timer are recomputed every time. They are **display** dates (`tz`); the files
  behind them are **market** days (`market_tz`, contract v2), so for an area whose two calendars
  differ a display day needs the files that cover it and its authority is its principal file's
  (`market_day.principal_market_day`): "tomorrow published" means the file holding most of
  tomorrow is in, and the hour the next file adds arrives with the next publication.
* Everything scheduled is cancelled and rescheduled from the injected `now` on every run, so a
  clock jump still leaves one timer per area. Only the exact area-local midnight rollover is not
  jittered; every interval and backoff is (see [jittered]).
* Each activation of an area carries a generation, and every completion checks it before publishing
  or scheduling, so a run that started before a re-subscribe, change or unsubscribe cannot resurrect
  the area. An in-flight refresh may still populate the shared repository, but cannot install a
  timer, notify for an area it no longer owns, or survive shutdown (which is terminal).
"""

from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from functools import partial
from typing import Any, Callable, Final, Literal

from homeassistant.core import callback, HomeAssistant
from homeassistant.helpers.event import async_track_point_in_utc_time
from homeassistant.util import dt as dt_util

from ..runtime import domain_data
from .market_day import market_days_for, principal_market_day
from .price_repository import (
    CatalogueSnapshot,
    DaySnapshot,
    IndexSnapshot,
    is_transport,
    PriceRepository,
)
from .relay_contract import AreaEntry


_LOGGER = logging.getLogger(__name__)


# Timing policy: every number that decides when something happens, named, so it can be tuned
# without touching the mechanism.

#: The zone the publication window is expressed in. Day-ahead publication is a common European
#: process, so the window is one European clock, not each area's or the host's.
PUBLICATION_WINDOW_ZONE: Final = "Europe/Brussels"

#: When the relay's day documents have been observed to appear (a midday process, not an ENTSO-E
#: guarantee). The window starts just before the earliest complete document observed and ends with
#: real margin after the latest; a later observation changes only these two constants. The relay
#: also publishes an early incomplete document shortly before noon, which is why "listed" is not
#: "usable" (see [refresh_interval]).
PUBLICATION_WINDOW_START: Final = time(12, 55)
PUBLICATION_WINDOW_END: Final = time(15, 0)

#: While tomorrow is still unusable inside that window, ask the index often.
PUBLICATION_POLL_SECONDS: Final = 5 * 60

#: Ordinary healthy cadence.
HEALTHY_POLL_SECONDS: Final = 30 * 60

#: A slower cadence when there is nothing to wait for: a valid index that lists tomorrow, with
#: tomorrow's document in hand.
SETTLED_POLL_SECONDS: Final = 60 * 60

#: Contract-invalid data must not hot-loop, whatever the failure count is.
INVALID_POLL_SECONDS: Final = 30 * 60

#: Transport-failure backoff, in seconds, capped at the last step.
BACKOFF_STEPS_SECONDS: Final[tuple[int, ...]] = (60, 120, 300, 900, 1800)

#: How long a catalogue is considered current.
CATALOGUE_MAX_AGE_SECONDS: Final = 24 * 60 * 60

#: +/- this fraction of an interval so installations do not synchronize; never applied to the
#: exact area-local midnight rollover.
JITTER_FRACTION: Final = 0.10


RefreshState = Literal[
    "loading",
    "ready",
    "waiting_for_tomorrow",
    "degraded",
    "incomplete",
    "stale",
    "unavailable",
    "invalid",
]

RefreshReason = Literal[
    "loading",
    "ready",
    "waiting_for_tomorrow",
    "not_listed",
    "incomplete_day",
    "last_good_retained",
    "index_unavailable",
    "source_unreachable",
    "invalid_contract",
    "tomorrow_unavailable",
    "tomorrow_stale",
    "tomorrow_invalid",
    "tomorrow_incomplete",
]

#: What a current index says about one date. `unknown` is distinct from `not_listed`: a failed
#: refresh means nobody is currently saying a day does not exist, and collapsing them would make a
#: UI wait for something nobody is looking for.
DayAuthority = Literal["listed", "not_listed", "unknown"]


@dataclass(frozen=True, slots=True)
class AreaPriceSnapshot:
    """Everything one area's consumers are told, as one immutable value.

    It references the repository's immutable day models; diagnostics prints the `*_summary` helpers
    instead, so a price array never reaches a dump. `state` is the combined state and `reason` its
    stable code; `today_state`/`tomorrow_state` are the per-day facts behind it (a failed tomorrow stays
    visible without making valid today data look broken). `next_attempt` is `None` while nothing is
    scheduled.
    """

    area_id: str
    state: RefreshState
    reason: RefreshReason
    today: date
    tomorrow: date
    today_snapshot: DaySnapshot
    tomorrow_snapshot: DaySnapshot
    today_state: str
    tomorrow_state: str
    today_authority: DayAuthority
    tomorrow_authority: DayAuthority
    waiting_for_tomorrow: bool
    index_state: str
    index_revision: str | None
    catalogue: AreaEntry | None
    fetched_at: datetime | None
    attempted_at: datetime | None
    next_attempt: datetime | None
    generation: int
    subscriber_count: int

    @property
    def today_document(self):
        return self.today_snapshot.document

    @property
    def tomorrow_document(self):
        return self.tomorrow_snapshot.document

    def meaningful_key(self) -> tuple[Any, ...]:
        """What a listener is told about, and not told twice: a change in data, state or reason, the area's
        clock rolling over, a document acquired or the next attempt moving; not a timer that found
        everything as it was.
        """
        return (
            self.area_id,
            self.state,
            self.reason,
            self.today,
            self.tomorrow,
            self.today_state,
            self.tomorrow_state,
            self.today_authority,
            self.tomorrow_authority,
            self.index_state,
            self.index_revision,
            self.generation,
            self.subscriber_count,
            None if self.today_snapshot.document is None else self.today_snapshot.document.prices,
            None if self.tomorrow_snapshot.document is None else self.tomorrow_snapshot.document.prices,
            self.next_attempt,
        )


@dataclass(frozen=True, slots=True)
class ManagerSnapshot:

    running: bool
    shutdown: bool
    areas: tuple[AreaPriceSnapshot, ...]
    catalogue: CatalogueSnapshot
    index: IndexSnapshot

    def area(self, area_id: str) -> AreaPriceSnapshot | None:
        for snapshot in self.areas:
            if snapshot.area_id == area_id:
                return snapshot
        return None


@dataclass(frozen=True, slots=True)
class CadenceInput:
    """Everything the cadence decision depends on, built from the injected clock and the repository's
    read-only snapshots so [refresh_interval] stays a pure function.
    """

    now: datetime
    area_today: date
    area_tomorrow: date
    today_state: str
    tomorrow_state: str
    tomorrow_listed: bool
    index_state: str
    tomorrow_ready: bool
    failing: bool
    backoff_step: int
    invalid: bool
    in_publication_window: bool



def local_dates(now: datetime, tz: str) -> tuple[date, date]:
    today = now.astimezone(dt_util.get_time_zone(tz)).date()
    return today, today + timedelta(days=1)


def next_local_midnight(now: datetime, tz: str) -> datetime:
    zone = dt_util.get_time_zone(tz)
    following = now.astimezone(zone).date() + timedelta(days=1)
    return datetime(following.year, following.month, following.day, tzinfo=zone)


def in_publication_window(now: datetime) -> bool:
    """Whether an instant falls inside the day-ahead publication window.

    Expressed in [PUBLICATION_WINDOW_ZONE] and not per-area: it is about when European day-ahead
    prices appear. A policy about when to look, never a claim that prices cannot appear outside it.
    """
    local = now.astimezone(dt_util.get_time_zone(PUBLICATION_WINDOW_ZONE)).time()
    return PUBLICATION_WINDOW_START <= local < PUBLICATION_WINDOW_END


def day_authority(index_snapshot: IndexSnapshot, area_id: str, day: date) -> DayAuthority:
    """What the relay's index currently says about one area's date.

    Only a current, valid (`ready`) index is authority. A `stale` index (last good retained after a
    failed attempt) is `unknown`: its list may be hours old, and treating it as current is how "not
    published yet" becomes an unchecked fact. It is still used to avoid a fetch (see the manager).
    """
    if index_snapshot.state != "ready" or index_snapshot.index is None:
        return "unknown"
    area = index_snapshot.index.area(area_id)
    if area is None:
        return "not_listed"
    return "listed" if area.lists(day) else "not_listed"


def retained_listing(index_snapshot: IndexSnapshot, area_id: str, day: date) -> bool:
    """Whether the index we hold lists a date, current or stale. Used only to decide whether asking for a
    day is worth a request (avoiding 404s); never for [day_authority].
    """
    if index_snapshot.index is None:
        return False
    area = index_snapshot.index.area(area_id)
    return area is not None and area.lists(day)


def backoff_delay(step: int) -> timedelta:
    index = min(max(step, 1), len(BACKOFF_STEPS_SECONDS)) - 1
    return timedelta(seconds=BACKOFF_STEPS_SECONDS[index])


def jittered(delay: timedelta, *, rand: float) -> timedelta:
    """`delay` scaled by up to +/- [JITTER_FRACTION] from an injected `rand` in `[0, 1)`, so a test
    gets one exact answer. Never zero.
    """
    fraction = 1.0 + JITTER_FRACTION * (2.0 * min(max(rand, 0.0), 1.0) - 1.0)
    return timedelta(seconds=max(delay.total_seconds() * fraction, 1.0))


def refresh_interval(context: CadenceInput) -> timedelta:
    """How long until this area should be refreshed again: the whole policy, in precedence order.

    1. A failing area backs off through [BACKOFF_STEPS_SECONDS].
    2. A tomorrow that is not usable yet, inside the publication window, polls often. "Not usable",
       not "not listed", because an early incomplete document is listed but cannot be planned against.
    3. Nothing to wait for (valid index lists tomorrow, document in hand) is polled slowly.
    4. Otherwise the ordinary cadence.
    5. Invalid data raises the floor to [INVALID_POLL_SECONDS], whatever the failure count.

    Tomorrow's listing or a failed tomorrow attempt does not change today's answer; the per-day states
    are reported, not acted on.
    """
    if context.failing:
        delay = backoff_delay(context.backoff_step)
    elif context.tomorrow_listed and context.tomorrow_ready:
        delay = timedelta(seconds=SETTLED_POLL_SECONDS)
    elif context.in_publication_window and not context.tomorrow_ready:
        delay = timedelta(seconds=PUBLICATION_POLL_SECONDS)
    else:
        delay = timedelta(seconds=HEALTHY_POLL_SECONDS)
    if context.invalid:
        delay = max(delay, timedelta(seconds=INVALID_POLL_SECONDS))
    return delay


def combined_state(
    *,
    today_state: str,
    tomorrow_state: str,
    today_authority: DayAuthority,
    tomorrow_authority: DayAuthority,
) -> tuple[RefreshState, RefreshReason]:
    """The area's state and reason, from the two days' own states and authorities, in precedence order:

    1. `invalid`: today's body violates the contract and there is no last-good today.
    2. `loading`: nothing yet, and a request for today is running.
    3. `unavailable`: no usable today. The reason separates a current index saying today is not
       published (`not_listed`), no current index authority (`index_unavailable`), and an unreachable
       relay (`source_unreachable`).
    4. `stale`: today's last good is shown because its latest attempt failed.
    5. `incomplete`: today is usable but stops early (documents are immutable, so not re-requested).
    6. `waiting_for_tomorrow`: usable today and a current valid index says tomorrow is not published.
       A stale or missing index is never this (`degraded`/`index_unavailable`).
    7. `ready`: usable complete today and tomorrow.
    8. `degraded`: usable today, impaired horizon; the reason names how.

    `degraded` exists because `unavailable` would say today's usable prices cannot be used and `ready`
    would claim a horizon that is not there. Per-day states stay visible beside the combined one.
    """
    if today_state == "invalid":
        return "invalid", "invalid_contract"
    if today_state == "loading":
        return "loading", "loading"
    if today_state == "unavailable":
        if today_authority == "not_listed":
            return "unavailable", "not_listed"
        if today_authority == "unknown":
            return "unavailable", "index_unavailable"
        return "unavailable", "source_unreachable"
    if today_state == "stale":
        return "stale", "last_good_retained"
    if today_state == "incomplete":
        return "incomplete", "incomplete_day"

    if tomorrow_authority == "not_listed":
        return "waiting_for_tomorrow", "waiting_for_tomorrow"
    if tomorrow_state == "ready":
        return "ready", "ready"

    # Usable today, impaired horizon: the tomorrow state is the more specific fact where there is
    # one; the authority answers for a tomorrow nobody fetched or invalidated.
    if tomorrow_state == "invalid":
        return "degraded", "tomorrow_invalid"
    if tomorrow_state == "stale":
        return "degraded", "tomorrow_stale"
    if tomorrow_state == "incomplete":
        return "degraded", "tomorrow_incomplete"
    if tomorrow_authority == "unknown":
        return "degraded", "index_unavailable"
    return "degraded", "tomorrow_unavailable"


class PriceAreaUnavailable(RuntimeError):
    """A subscription was refused because the area's timezone is not known yet (guessing from the
    host's zone would make "today" the wrong day for up to an evening).
    """

    def __init__(self, area_id: str) -> None:
        super().__init__(f"the timezone of area {area_id!r} is not known yet")
        self.area_id = area_id


@dataclass(frozen=True, slots=True)
class _Subscription:
    """One owner's subscription to one area, identified by a token, so a stale handle is harmless: unsubscribe
    removes the owner only if this is still its current subscription.
    """

    token: int
    listener: Callable[[AreaPriceSnapshot], None] | None


@dataclass(slots=True)
class _Area:
    """One area's live record: mutable, never handed out. A run whose `generation` is stale may finish
    but not publish or schedule.
    """

    area_id: str
    tz: str
    generation: int = 0
    active: bool = False
    #: The zone a day file's calendar is in; equal to `tz` unless the v2 list names another.
    market_tz: str = ""
    owners: dict[str, _Subscription] = field(default_factory=dict)
    cancel_rollover: Callable[[], None] | None = None
    last_key: tuple[Any, ...] | None = None

    @property
    def subscriber_count(self) -> int:
        return len(self.owners)

    @property
    def split(self) -> bool:
        return bool(self.market_tz) and self.market_tz != self.tz

    def principal(self, day: date) -> date:
        """The market day whose file decides a display date (the date itself when the calendars agree)."""
        return principal_market_day(day, self.tz, self.market_tz) if self.split else day

    def files(self, day: date) -> tuple[date, ...]:
        """The market-day files covering a display date."""
        return market_days_for(day, self.tz, self.market_tz) if self.split else (day,)


class PriceRefreshManager:
    """All price refresh timers for this Home Assistant instance, in one object, owned by no config
    entry ([async_setup_price_refresh]).
    """

    def __init__(
        self,
        hass: HomeAssistant,
        repository: PriceRepository,
        *,
        now: Callable[[], datetime] | None = None,
        jitter: Callable[[], float] | None = None,
        scheduler: Callable[[HomeAssistant, Callable[[datetime], None], datetime], Callable[[], None]]
        | None = None,
    ) -> None:
        self._hass = hass
        self._repository = repository
        self._now: Callable[[], datetime] = now if now is not None else dt_util.utcnow
        self._jitter: Callable[[], float] = jitter if jitter is not None else random.random
        self._scheduler = scheduler if scheduler is not None else async_track_point_in_utc_time
        self._areas: dict[str, _Area] = {}
        # The catalogue and index are installation-global, so their poll is too: one run, one
        # timer, one backoff count and one next-attempt instant however many areas subscribe.
        self._global_run: asyncio.Task[None] | None = None
        self._global_generation = 0
        self._cancel_global: Callable[[], None] | None = None
        self._backoff_step = 0
        self._next_global_attempt: datetime | None = None
        self._next_token = 0
        self._catalogue_attempted_at: datetime | None = None
        self._last_areas_rev: str | None = None
        # Catalogue listeners are global too: an area selector needs the whole market list, which
        # no per-area subscription gives, and per-entity subscriptions would look like extra
        # price consumers.
        self._catalogue_listeners: list[Callable[[CatalogueSnapshot], None]] = []
        self._catalogue_key: tuple[Any, ...] | None = None
        self._started = True
        self._shutdown = False

    async def async_shutdown(self) -> None:
        """Cancel every timer, run and listener, and refuse new work (terminal). The generation is raised
        first so in-flight runs cannot schedule. The repository is untouched.
        """
        if self._shutdown:
            return
        self._shutdown = True
        runs: list[asyncio.Task[None]] = []
        self._global_generation += 1
        self._cancel("global")
        self._next_global_attempt = None
        if self._global_run is not None and not self._global_run.done():
            runs.append(self._global_run)
        for record in self._areas.values():
            record.generation += 1
            record.active = False
            self._cancel_rollover(record)
            record.owners.clear()
        if runs:
            for run in runs:
                run.cancel()
            await asyncio.gather(*runs, return_exceptions=True)
        self._areas.clear()
        # Listeners are entities that already deregister themselves; this guarantees a departing
        # manager holds nothing.
        self._catalogue_listeners.clear()
        self._catalogue_key = None

    def _cancel(self, which: str) -> None:
        if self._cancel_global is not None:
            self._cancel_global()
            self._cancel_global = None

    def _cancel_rollover(self, record: _Area) -> None:
        if record.cancel_rollover is not None:
            record.cancel_rollover()
            record.cancel_rollover = None

    async def async_subscribe(
        self,
        *,
        owner_id: str,
        area_id: str,
        listener: Callable[[AreaPriceSnapshot], None] | None = None,
        tz: str | None = None,
    ) -> Callable[[], None]:
        """Ask for an area's prices, and get a callable that stops asking.

        `(owner_id, area_id)` is idempotent: a repeat from the same owner does not count twice and
        replaces its listener. Subscribing the same owner to a different area moves it atomically: the old
        area loses the owner (and stops if it was the last) in the same synchronous step the new one gains
        it. Nothing here knows what a charger is.

        `tz` is for a caller that already knows the zone; otherwise it comes from the catalogue (refreshed
        once if missing, restored across restarts). Without a zone [PriceAreaUnavailable] is raised rather
        than guessed.
        """
        if self._shutdown:
            raise RuntimeError("the price refresh manager has been shut down")
        zone, market_zone = (tz, self._market_zone_for(area_id, tz)) if tz else await self._zone_for(area_id)

        previous = self._owner_area(owner_id)
        if previous is not None and previous != area_id:
            self._drop_owner(previous, owner_id)

        record = self._areas.get(area_id)
        if record is None:
            record = _Area(area_id=area_id, tz=zone, market_tz=market_zone)
            self._areas[area_id] = record
        elif (record.tz, record.market_tz) != (zone, market_zone):
            # The catalogue moved the area (a v1 list read as v2 names Portugal's display zone): one area
            # still has one pair of zones, the current one, and its midnight timer follows it.
            self._move_zones(record, zone, market_zone)
        subscription = self._new_subscription(listener)
        record.owners[owner_id] = subscription
        if not record.active:
            self._activate(record)
        # An already-active area needs no run, request or extra timer: a second consumer or a
        # replaced listener just gets the current state (`async_refresh_area` is for refreshing
        # sooner). The new listener is told directly, whatever deduplication would say, since it
        # has never seen the snapshot.
        self._deliver(record, {owner_id: subscription}, force=True)
        return _unsubscribe_for(self, area_id, owner_id, subscription.token)

    def _new_subscription(self, listener: Callable[[AreaPriceSnapshot], None] | None) -> _Subscription:
        self._next_token += 1
        return _Subscription(token=self._next_token, listener=listener)

    def _unsubscribe(self, area_id: str, owner_id: str, token: int) -> None:
        """Remove an owner only if this handle is still its current subscription.

        One comparison covers every case: a second call is a no-op, an old handle after a re-subscribe or a
        move does nothing, and the newest handle is idempotent (its token matches once).
        """
        record = self._areas.get(area_id)
        if record is None:
            return
        current = record.owners.get(owner_id)
        if current is not None and current.token == token:
            self._drop_owner(area_id, owner_id)

    def owner_area(self, owner_id: str) -> str | None:
        return self._owner_area(owner_id)

    async def _zone_for(self, area_id: str) -> tuple[str, str]:
        entry = self._repository.catalogue_snapshot().area(area_id)
        if entry is None:
            await self._repository.async_get_catalogue()
            entry = self._repository.catalogue_snapshot().area(area_id)
        if entry is None:
            raise PriceAreaUnavailable(area_id)
        return entry.tz, entry.market_tz

    def _market_zone_for(self, area_id: str, tz: str) -> str:
        """The market calendar of an area subscribed with a known display zone: the catalogue's when it
        lists the area in that zone, else the display zone itself."""
        entry = self._repository.catalogue_snapshot().area(area_id)
        return entry.market_tz if entry is not None and entry.tz == tz else tz

    def _move_zones(self, record: _Area, tz: str, market_tz: str) -> None:
        record.tz = tz
        record.market_tz = market_tz
        if record.active:
            self._schedule_rollover(record)

    def _sync_zones(self) -> None:
        """Follow a refreshed catalogue's zones for every area it still lists (never drop an area here)."""
        catalogue = self._repository.catalogue_snapshot()
        for record in self._areas.values():
            entry = catalogue.area(record.area_id)
            if entry is not None and (record.tz, record.market_tz) != (entry.tz, entry.market_tz):
                self._move_zones(record, entry.tz, entry.market_tz)

    def _owner_area(self, owner_id: str) -> str | None:
        for record in self._areas.values():
            if owner_id in record.owners:
                return record.area_id
        return None

    def _drop_owner(self, area_id: str, owner_id: str) -> None:
        record = self._areas.get(area_id)
        if record is None:
            return
        record.owners.pop(owner_id, None)
        if record.subscriber_count == 0:
            self._deactivate(record)
        else:
            self._notify(record)

    def _activate(self, record: _Area) -> None:
        """Start (or restart) an area's work: a rollover timer and a first run. The generation rises,
        retiring any run in flight, and the previous timer is cancelled first.
        """
        record.generation += 1
        record.active = True
        self._schedule_rollover(record)
        # A newly active area gives the global cycle work: join a run in flight rather than start a
        # second, so consecutive subscriptions still produce one catalogue and one index request.
        self._trigger_global_run()

    def _deactivate(self, record: _Area) -> None:
        """Stop an area's future work. A run in flight may finish (its data belongs in the shared cache)
        but not publish, notify or schedule.
        """
        record.generation += 1
        record.active = False
        self._cancel_rollover(record)
        record.last_key = None
        self._reschedule_global()

    async def async_refresh_area(self, area_id: str) -> AreaPriceSnapshot | None:
        """Refresh one area now, bypassing the interval but never the rules.

        Joins or triggers the global cycle rather than polling on its own (one index request per
        installation). `None` if nothing subscribes to the area or the manager is shut down.
        """
        record = self._areas.get(area_id)
        if record is None or not record.active or self._shutdown:
            return None
        task = self._trigger_global_run()
        if task is not None:
            await task
        return self._snapshot_for(record)

    def _trigger_global_run(self) -> asyncio.Task[None] | None:
        """Start the one global cycle, or return the one already running, so a tick, manual refresh,
        rollover and new subscription do not each poll the index.
        """
        if self._shutdown or not self._active_areas():
            return None
        if self._global_run is not None and not self._global_run.done():
            return self._global_run
        self._global_generation += 1
        task = self._hass.async_create_task(self._run_global(self._global_generation))
        self._global_run = task
        return task

    async def _run_global(self, generation: int) -> None:
        try:
            await self._refresh_global()
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001 - a cycle must never die silently
            _LOGGER.warning("Refreshing prices failed: %s", type(err).__name__)
        if self._stale_generation(generation):
            return
        self._complete_cycle()

    async def _refresh_global(self) -> None:
        """Catalogue (when due), one index, then every active area's days.

        The relay's own design: the catalogue changes rarely, the index is small and revalidated, and the
        immutable day documents it points at are fetched only when the index we hold lists them.
        """
        catalogue = self._repository.catalogue_snapshot()
        if catalogue.catalogue is None or self._catalogue_age() >= CATALOGUE_MAX_AGE_SECONDS:
            # `refresh=True`: the age rule and the absent case mean "ask again", and only an
            # explicit refresh does that (a default read answers from memory).
            await self._repository.async_get_catalogue(refresh=True)
            self._catalogue_attempted_at = self._now()

        index = await self._repository.async_get_index(refresh=True)
        revision = None if index.index is None else index.index.areas_rev
        if revision is not None and self._last_areas_rev is not None and revision != self._last_areas_rev:
            await self._repository.async_get_catalogue(refresh=True)
            self._catalogue_attempted_at = self._now()
        if revision is not None:
            self._last_areas_rev = revision
        self._sync_zones()

        if index.index is None:
            # No index and no retained copy: fanning out would ask the index once per area through
            # the same unavailable relay (a day fetch consults the index). The next cycle retries one
            # backoff step later and every area reports the same failure.
            return

        for record in self._active_areas():
            try:
                await self._refresh_area_days(record)
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001 - one area must not block the rest
                _LOGGER.warning("Refreshing %s failed: %s", record.area_id, type(err).__name__)

    async def _refresh_area_days(self, record: _Area) -> None:
        """Today and tomorrow for one area, subject to the index's authority.

        Today is attempted unless a current index says it is not published (a stale index is not
        authority). Tomorrow is attempted only when the index we hold lists it: the conservative use of a
        stale list, avoiding 404s without claiming anything about now.
        """
        today, tomorrow = local_dates(self._now(), record.tz)
        index = self._repository.index_snapshot()
        if not record.split:
            if day_authority(index, record.area_id, today) != "not_listed":
                await self._repository.async_get_file(record.area_id, today)
            if retained_listing(index, record.area_id, tomorrow):
                await self._repository.async_get_file(record.area_id, tomorrow)
            return
        # Two calendars: today's principal file follows today's rule; every other file behind today or
        # tomorrow is asked for only when the index we hold lists it (a file shared by the two days once).
        principal = record.principal(today)
        wanted = dict.fromkeys((*record.files(today), *record.files(tomorrow)))
        for key in wanted:
            if key == principal:
                if day_authority(index, record.area_id, key) != "not_listed":
                    await self._repository.async_get_file(record.area_id, key)
            elif retained_listing(index, record.area_id, key):
                await self._repository.async_get_file(record.area_id, key)

    def _catalogue_age(self) -> float:
        fetched_at = self._repository.catalogue_snapshot().fetched_at
        if fetched_at is None:
            return float("inf")
        return (self._now() - fetched_at).total_seconds()

    def _active_areas(self) -> list[_Area]:
        return [record for record in self._areas.values() if record.active]

    def _stale_generation(self, generation: int) -> bool:
        return self._shutdown or generation != self._global_generation

    def _complete_cycle(self) -> None:
        """Record a finished cycle's outcome, then book the next appointment.

        Only a finished cycle may change the backoff: it alone made network attempts. A subscription
        appearing or disappearing is not a failed attempt.
        """
        failing = any(self._is_failing(record) for record in self._active_areas())
        self._backoff_step = self._backoff_step + 1 if failing else 0
        self._reschedule_global()

    def _reschedule_global(self) -> None:
        """Recompute the shared appointment from the recorded state, and notify.

        The next global poll is the earliest cadence any active area requires (a publication wait makes the
        shared index poll impatient for everyone, since one request serves all). The backoff count is global
        too: a transport failure is about the installation's connection, and per-area counting would multiply
        attempts against the resource that is down. This reads the count, never writes it. With no active
        areas there is no timer, and after shutdown never again.
        """
        self._cancel("global")
        areas = self._active_areas()
        if self._shutdown or not areas:
            self._next_global_attempt = None
            return
        failing = self._backoff_step > 0
        delay = jittered(
            min(refresh_interval(self._cadence_input(record, failing=failing)) for record in areas),
            rand=self._jitter(),
        )
        when = self._now() + delay
        self._next_global_attempt = when
        self._cancel_global = self._scheduler(
            self._hass, partial(self._on_global_refresh, self._global_generation), when
        )
        for record in areas:
            self._notify(record)
        self._notify_catalogue()

    def schedule_at(self, when: datetime, action: Callable[[datetime], Any]) -> Callable[[], None]:
        """One appointment on the manager's scheduler for a consumer that must look again at an instant
        (e.g. the Auto planner's latest safe start). Refreshes nothing.
        """
        return self._scheduler(self._hass, action, when)

    def add_catalogue_listener(
        self, listener: Callable[[CatalogueSnapshot], None]
    ) -> Callable[[], None]:
        """A listener for meaningful catalogue changes; the returned handle is idempotent.

        One set on the manager (the catalogue is installation-global and an area selector needs the whole
        market list). Called synchronously on the event loop with the repository's snapshot, only when the
        option list changed (`CatalogueSnapshot.meaningful_key`).
        """
        self._catalogue_listeners.append(listener)
        # Delivered to the new listener only; the manager-global "already broadcast" key is left
        # alone, or a change between the last broadcast and this registration would be invisible to
        # the others for good.
        self._deliver_catalogue(self._repository.catalogue_snapshot(), [listener])

        def remove() -> None:
            if listener in self._catalogue_listeners:
                self._catalogue_listeners.remove(listener)

        return remove

    def _notify_catalogue(self) -> None:
        """Tell the catalogue listeners, once per meaningful change, at the end of every cycle and whenever
        the appointment is recomputed, so no listener misses a new list and none hears a same-again one.
        """
        if self._shutdown:
            return
        snapshot = self._repository.catalogue_snapshot()
        if snapshot.meaningful_key() == self._catalogue_key:
            return
        self._catalogue_key = snapshot.meaningful_key()
        self._deliver_catalogue(snapshot, list(self._catalogue_listeners))

    def _deliver_catalogue(
        self,
        snapshot: CatalogueSnapshot,
        listeners: list[Callable[[CatalogueSnapshot], None]],
    ) -> None:
        for listener in listeners:
            try:
                listener(snapshot)
            except Exception as err:  # noqa: BLE001 - one listener must not stop the rest
                _LOGGER.warning("A catalogue listener failed: %s", type(err).__name__)

    def _is_failing(self, record: _Area) -> bool:
        """Whether the latest attempt at any of one area's resources failed.

        A transport failure drives the backoff; contract-invalid bodies raise the invalid floor instead; a
        day the index does not list is no failure.
        """
        attempts = [
            self._repository.catalogue_snapshot().attempt_error,
            self._repository.index_snapshot().attempt_error,
        ]
        today, tomorrow = local_dates(self._now(), record.tz)
        for key in dict.fromkeys((*record.files(today), *record.files(tomorrow))):
            attempts.append(self._repository.file_snapshot(record.area_id, key).attempt_error)
        return any(is_transport(error) for error in attempts)

    def _cadence_input(self, record: _Area, *, failing: bool) -> CadenceInput:
        """Everything the cadence decision needs, from the injected clock and the cache.

        `tomorrow_listed` is the retained listing, not the tri-state authority: this decides how often to
        ask, and asking often while a publication is expected is the point. [day_authority] answers what the
        relay currently says.
        """
        now = self._now()
        today, tomorrow = local_dates(now, record.tz)
        today_snapshot = self._repository.day_snapshot(record.area_id, today)
        tomorrow_snapshot = self._repository.day_snapshot(record.area_id, tomorrow)
        index_snapshot = self._repository.index_snapshot()
        catalogue_snapshot = self._repository.catalogue_snapshot()
        return CadenceInput(
            now=now,
            area_today=today,
            area_tomorrow=tomorrow,
            today_state=today_snapshot.state,
            tomorrow_state=tomorrow_snapshot.state,
            tomorrow_listed=retained_listing(index_snapshot, record.area_id, record.principal(tomorrow)),
            index_state=index_snapshot.state,
            tomorrow_ready=tomorrow_snapshot.is_complete,
            failing=failing,
            backoff_step=max(self._backoff_step, 1),
            invalid=(
                today_snapshot.state == "invalid"
                or index_snapshot.state == "invalid"
                or catalogue_snapshot.state == "invalid"
            ),
            in_publication_window=in_publication_window(now),
        )

    @callback
    def _on_global_refresh(self, generation: int, _fired: datetime) -> None:
        """The shared timer fired: run, unless the manager has moved on since.

        `@callback` is load-bearing: Home Assistant is given `partial(self._on_global_refresh, generation)`
        and a `functools.partial` does not carry the marker (`is_callback_check_partial` walks the chain).
        Unmarked, it is dispatched to an executor thread, where `hass.async_create_task` in
        `_trigger_global_run()` raises before any run or next appointment exists and the chain dies.

        Nothing here may block. The fired instant is deliberately unused: decisions come from the injected
        clock, so a late timer or moved host clock still computes *now*.
        """
        self._cancel("global")
        if self._stale_generation(generation):
            return
        self._next_global_attempt = None
        self._trigger_global_run()

    def _schedule_rollover(self, record: _Area) -> None:
        """Install the area-local midnight timer: never jittered, recomputed from the injected clock."""
        generation = record.generation
        self._cancel_rollover(record)
        if self._shutdown or not record.active:
            return
        when = next_local_midnight(self._now(), record.tz)
        record.cancel_rollover = self._scheduler(
            self._hass, partial(self._on_rollover, record, generation), when
        )

    @callback
    def _on_rollover(self, record: _Area, generation: int, _fired: datetime) -> None:
        """The area's local midnight: publish the new date keys, then ask again.

        `@callback` for the same reason as [_on_global_refresh] (unmarked it would run in a worker, fail
        in `_trigger_global_run()` and lose the rollover with nothing left to recover it). Today and
        tomorrow are recomputed on every read, so the notification already carries the new keys; the
        refresh is the shared global cycle.
        """
        self._cancel_rollover(record)
        if self._shutdown or not record.active or record.generation != generation:
            return
        self._notify(record)
        self._schedule_rollover(record)
        self._trigger_global_run()

    async def async_ensure_catalogue(self) -> None:
        """Make sure a catalogue is held, once, for whoever needs the market list.

        The one demand the entity layer makes of the shared layer; it goes through the manager because the
        repository coalesces concurrent loads (two chargers still produce one fetch). Silent on failure;
        the snapshot's own state carries what happened.
        """
        if self._shutdown:
            return
        try:
            await self._repository.async_get_catalogue()
        except Exception as err:  # noqa: BLE001 - the snapshot records the failure
            _LOGGER.warning("Loading the area catalogue failed: %s", type(err).__name__)
        # Always, also on failure: an area selector has no area subscription, so the manager's
        # cycle may never carry a loaded or newly unavailable list to it. Identical snapshots stay silent.
        self._notify_catalogue()

    def catalogue_snapshot(self) -> CatalogueSnapshot:
        """The relay's area list as the repository holds it, for a selector at a closed market. No fetch."""
        return self._repository.catalogue_snapshot()

    async def async_profile(self, area_id: str):
        """The area's history profile, requested at most once per local day, or `None` ("no usable profile").

        Asked for by a consumer that needs it (a dated departure), not by the refresh cycle, so an
        installation that never plans a dated departure makes no profile request. Never raises.
        """
        entry = self._repository.catalogue_snapshot().area(area_id)
        if self._shutdown or entry is None:
            return None
        try:
            return await self._repository.async_get_profile(area_id, local_dates(self._now(), entry.tz)[0])
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001 - a missing profile only means planning on known prices
            _LOGGER.warning("Reading the history profile for %s failed: %s", area_id, type(err).__name__)
            return None

    def profile_summary(self, area_id: str) -> dict[str, Any]:
        """Diagnostics' view of the held profile (no price rows)."""
        return self._repository.profile_summary(area_id)

    def area_snapshot(self, area_id: str) -> AreaPriceSnapshot | None:
        record = self._areas.get(area_id)
        if record is None or not record.active:
            return None
        return self._snapshot_for(record)

    def manager_snapshot(self) -> ManagerSnapshot:
        """The manager's whole state: active areas and the shared documents' state."""
        return ManagerSnapshot(
            running=self._started and not self._shutdown,
            shutdown=self._shutdown,
            areas=tuple(
                self._snapshot_for(record)
                for record in sorted(self._areas.values(), key=lambda item: item.area_id)
                if record.active
            ),
            catalogue=self._repository.catalogue_snapshot(),
            index=self._repository.index_snapshot(),
        )

    def _snapshot_for(self, record: _Area) -> AreaPriceSnapshot:
        now = self._now()
        today, tomorrow = local_dates(now, record.tz)
        today_snapshot = self._repository.day_snapshot(record.area_id, today)
        tomorrow_snapshot = self._repository.day_snapshot(record.area_id, tomorrow)
        index_snapshot = self._repository.index_snapshot()

        # Authority is three-valued and taken from the index's freshness, not the presence of a
        # retained document: only a current valid index says something about now, and "waiting for
        # tomorrow" is claimed only for `not_listed`.
        today_authority = day_authority(index_snapshot, record.area_id, record.principal(today))
        tomorrow_authority = day_authority(index_snapshot, record.area_id, record.principal(tomorrow))
        waiting_for_tomorrow = tomorrow_authority == "not_listed"
        state, reason = combined_state(
            today_state=today_snapshot.state,
            tomorrow_state=tomorrow_snapshot.state,
            today_authority=today_authority,
            tomorrow_authority=tomorrow_authority,
        )
        catalogue = self._repository.catalogue_snapshot()
        entry: AreaEntry | None = catalogue.area(record.area_id)
        fetched_at = today_snapshot.fetched_at
        attempted_at = _latest(
            today_snapshot.attempt_at,
            index_snapshot.attempt_at,
            None if entry is None else catalogue.fetched_at,
        )
        return AreaPriceSnapshot(
            area_id=record.area_id,
            state=state,
            reason=reason,
            today=today,
            tomorrow=tomorrow,
            today_snapshot=today_snapshot,
            tomorrow_snapshot=tomorrow_snapshot,
            today_state=today_snapshot.state,
            tomorrow_state=tomorrow_snapshot.state,
            today_authority=today_authority,
            tomorrow_authority=tomorrow_authority,
            waiting_for_tomorrow=waiting_for_tomorrow,
            index_state=index_snapshot.state,
            index_revision=None if index_snapshot.index is None else index_snapshot.index.areas_rev,
            catalogue=entry,
            fetched_at=fetched_at,
            attempted_at=attempted_at,
            next_attempt=self._next_global_attempt,
            generation=record.generation,
            subscriber_count=record.subscriber_count,
        )

    def _notify(self, record: _Area) -> None:
        if self._shutdown or not record.active or record.subscriber_count == 0:
            return
        self._deliver(record, dict(record.owners))

    def _deliver(
        self,
        record: _Area,
        subscriptions: dict[str, _Subscription],
        *,
        force: bool = False,
    ) -> None:
        """Give a snapshot to some of an area's listeners.

        Synchronous, on the event loop, one immutable snapshot. One raising is logged and the rest still
        run; an unchanged meaningful key is not delivered. `force` is for a listener that has never heard
        anything (deduplication is about repetition, not first delivery).
        """
        if self._shutdown or not record.active or not subscriptions:
            return
        snapshot = self._snapshot_for(record)
        key = snapshot.meaningful_key()
        if not force and key == record.last_key:
            return
        record.last_key = key
        for subscription in subscriptions.values():
            listener = subscription.listener
            if listener is None:
                continue
            try:
                listener(snapshot)
            except Exception as err:  # noqa: BLE001 - one listener must not stop the rest
                _LOGGER.warning("A price listener for %s failed: %s", record.area_id, type(err).__name__)


def _unsubscribe_for(
    manager: PriceRefreshManager, area_id: str, owner_id: str, token: int
) -> Callable[[], None]:

    def unsubscribe() -> None:
        manager._unsubscribe(area_id, owner_id, token)

    return unsubscribe


def _latest(*moments: datetime | None) -> datetime | None:
    known = [moment for moment in moments if moment is not None]
    return max(known) if known else None



async def async_setup_price_refresh(
    hass: HomeAssistant,
    repository: PriceRepository,
    *,
    now: Callable[[], datetime] | None = None,
    jitter: Callable[[], float] | None = None,
    scheduler: Callable[[HomeAssistant, Callable[[datetime], None], datetime], Callable[[], None]]
    | None = None,
) -> PriceRefreshManager:
    """Create the one manager, next to the repository. Fetches nothing.

    Domain-scoped and owned by no config entry; its shutdown is driven by Home Assistant stopping. A
    second call returns the existing manager rather than installing a second set of timers.

    Deliberately no I/O: the catalogue is loaded by the first subscribe (or first area run), so an
    offline relay cannot fail domain setup and no test reaches the network by setting it up.
    """
    data = domain_data(hass)
    if data.price_refresh is not None:
        return data.price_refresh
    manager = PriceRefreshManager(hass, repository, now=now, jitter=jitter, scheduler=scheduler)
    data.price_refresh = manager
    return manager
