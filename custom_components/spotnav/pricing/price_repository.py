"""The shared price repository: one fetch/cache stream per resource.

There is one repository per Home Assistant instance, published on `SpotNavData` by
`async_setup_price_repository` and shared by every charger entry; a second charger adds no request,
cache or timer.

Four facts are kept apart, because collapsing any two makes a price layer lie:

1. last good: the newest valid document for a resource with its acquisition time. Nothing that
   failed to validate replaces it or relabels it as fresh.
2. the current attempt: when it happened and why it failed (a stable code), recorded beside last
   good, never instead of it.
3. authority: which days exist, by the area's provider's publication schedule and its last answer
   (`providers.schedule_authority`): today and earlier when not held, tomorrow from just before its expected
   publication, and an empty answer ("not published yet") is remembered in memory until the next attempt.
4. the body's validity: a document failing the contract is `invalid`, and the day keeps its last good.

Coalescing: concurrent callers for one resource share one request, owned by the repository (a task
on the HA loop), not by its first caller. A cancelled caller stops waiting and nothing else: the
request completes, is cached, and the other waiters get it. A failed request removes itself from
the in-flight table so the next caller starts fresh. No lock is held across network I/O; the only
lock guards the store write (local disk), so two completions cannot lose each other's resources.

The restore is single-flight the same way: one repository-owned task reads the store and adopts what
it finds, callers await it under `shield`, and adoption is ordered by acquisition time so a late
store read never overwrites a newer fetch. A failed restore is logged and treated as an absent store.

Every zone a stored document names is resolved off the event loop before it is parsed, so the parsers and
everything after them read zones from `time_zones`' cache and never build one from disk on the loop.

Only explicit `refresh` operations live here: no cadence, backoff or timers (see the manager). Prices
are stored as published. How a document is asked for is the providers' (`pricing/providers/`): a day file
comes from the area's provider ([ProviderRouter]); an area with no provider has no source, nothing is asked
for it, and the attempt is recorded as `source_missing` (no request is made). The area list is the one bundled
with the integration (`pricing/areas.json`, [area_entry]), with the market calendar and publication of the
area's provider.

Day files are cached per **market** day. Everything a caller asks by date ([day_snapshot], [async_get_day],
[async_get_archive_day]) is a **display** date in the area's `tz`: for an area whose two calendars agree that is
exactly one file, and for one whose do not (Great Britain, Portugal) the display day is cut from the files that
cover it (`pricing/market_day.py`). The file-level calls ([file_snapshot], [async_get_file]) are for the refresh
manager, which decides which files to ask for.

The store (schema 2) keeps each day file under its area, its own market calendar and its market day
(`"FI|Europe/Helsinki|2026-10-30"`), so files of one area on two calendars (Helsinki days kept from before 2.0
and CET days from Nord Pool) are held side by side; a lookup reads the one dated on the calendar the area is read
in now, compared by instant (a Stockholm-dated and a Berlin-dated file start at the same moment). Files are kept
for [HISTORY_KEEP_DAYS] and pruned on every write. The day files delivered by the SpotNav relay before 2.0 stay
held, labelled `"provider": "relay"`, until retention drops them: they are prices the installation already has,
and the profile and statistics keep their history. A schema 1 store (`"AREA|date"`) is read the same way and
rewritten as schema 2 on the next write, losing no day; the relay's area list, index and profiles that older
stores kept beside the days are no longer read and are dropped on that write.

The history profile is built here from the held market-day files (`pricing/price_profile.py`): [profile_for]
reads the files of the 28-day window on the area's market calendar, keyed by market date, and keeps the result
until the window or a file in it changes. The past files of that window (and, from a provider that keeps years,
a year for statistics) are fetched by the history backfill (`pricing/price_history.py`) through
[async_fetch_history], which never asks for a held day or one older than the provider keeps (`history_reach`).
"""

from __future__ import annotations

import asyncio
import copy
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from typing import Any, Final, Literal

from homeassistant.core import callback, HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from ..const import DOMAIN
from ..runtime import domain_data
from .. import time_zones
from .area_catalogue import async_load_bundled, BundledCatalogue
from .market_day import compose_display_day, local_midnight, market_days_for, principal_market_day
from .price_profile import build_profile, profile_window
from .price_store import async_setup_price_store
from .fx_rates import FxRates
from .providers import (
    Authority,
    DayFetch,
    FailureCode,
    market_today,
    PriceProvider,
    ProviderRouter,
    schedule_authority,
    schedule_open,
    TRANSPORT_CODES,
)
from .providers.entsoe import EntsoeProvider
from .providers.nordpool import NordPoolProvider
from .providers.octopus import OctopusProvider
from .providers.pvpc import PvpcProvider
from .providers.notices import SourceStatus
from .relay_contract import (
    AreaCatalogue,
    AreaEntry,
    parse_day,
    PriceDocument,
    PriceProfile,
    RelayParseError,
)


_LOGGER = logging.getLogger(__name__)

#: Archive files held in memory for one import run's neighbouring display days (a London day shares a
#: Paris file with the next); never persisted, never the live cache.
ARCHIVE_MEMO_SIZE: Final = 8

#: Home Assistant's own version of the store file, kept at 1 so an older release still opens it (and then
#: ignores a schema it does not know, starting with an empty cache).
STORAGE_VERSION: Final = 1
STORAGE_KEY: Final = f"{DOMAIN}_relay_cache"

#: The layout of the stored document: 2 keys day files by market calendar; 1 is the layout before it
#: (`"AREA|date"`), read once and rewritten. A `relay` part (the relay's area list and index, written before
#: 2.0) is ignored and not written again.
STORAGE_SCHEMA: Final = 2
LEGACY_SCHEMA: Final = 1

#: Day files older than this many days (before today, UTC) are dropped on the next write: enough for the
#: history profile's four weeks with a year of slack for statistics, and a bound on the store's size.
HISTORY_KEEP_DAYS: Final = 400

#: What the reader is looking at. `loading` is "nothing to show yet and a request is running"; a
#: refresh while a document is on screen is [DaySnapshot.refreshing].
DayState = Literal["loading", "ready", "incomplete", "stale", "unavailable", "invalid"]
CatalogueState = Literal["loading", "ready", "stale", "unavailable", "invalid"]

#: What the schedule and the provider's last answer say about a day (`providers.schedule_authority`).
IndexAuthority = Authority

#: Where a snapshot's document came from; a restored one is not freshly fetched. `bundled` is the area list
#: shipped with the integration (`pricing/areas.json`), the only one there is.
SnapshotSource = Literal["memory", "store", "network", "bundled"]


@dataclass(frozen=True, slots=True)
class CatalogueSnapshot:
    """The area list (the bundled one), and whether there is one."""

    state: CatalogueState
    catalogue: AreaCatalogue | None
    source: SnapshotSource | None
    fetched_at: datetime | None
    attempt_at: datetime | None
    attempt_error: FailureCode | None
    refreshing: bool

    def area(self, area_id: str):
        return None if self.catalogue is None else self.catalogue.area(area_id)

    def meaningful_key(self) -> tuple[Any, ...]:
        """What a catalogue listener is told about, and not told twice.

        The key is the list of selectable markets: the catalogue's version, generation moment and area
        ids. `fetched_at` stays out (it moves on every fetch), so a revalidation producing the same
        catalogue is silent. The attempt state is in the key because a listener may show "offline".
        """
        if self.catalogue is None:
            return (self.state, None)
        return (
            self.state,
            self.catalogue.version,
            self.catalogue.generated,
            self.catalogue.area_ids,
        )


@dataclass(frozen=True, slots=True)
class DaySnapshot:
    """One `(area, date)`: what we can show, and what we last tried.

    From [PriceRepository.file_snapshot] the date is a market day and the document one file; from
    [PriceRepository.day_snapshot] it is a display date, and for a split-calendar area the document is
    cut from the files covering it while state, authority and attempt are those of its principal file
    (`market_day.principal_market_day`). `complete` then says whether that file is whole: a London day
    ending at 23:00 because the next Paris file is not out yet is as complete as it can be.

    `document` is last good only, replaced only by a validated document; `fetched_at` is its
    acquisition time, so a failed refresh leaves both alone. `attempt_at`/`attempt_error` describe the
    newest try. `index_authority` is what the schedule and the provider's last answer say about the day
    (`unknown` after a failed attempt or with no source).
    """

    area_id: str
    day: date
    state: DayState
    document: PriceDocument | None
    source: SnapshotSource | None
    fetched_at: datetime | None
    attempt_at: datetime | None
    attempt_error: FailureCode | None
    index_authority: IndexAuthority
    refreshing: bool
    #: Set for a composed display day; `None` means "the document covers its whole day" decides.
    complete: bool | None = None

    @property
    def interval_count(self) -> int | None:
        return None if self.document is None else self.document.interval_count

    @property
    def is_complete(self) -> bool:
        if self.complete is not None:
            return self.document is not None and self.complete
        return self.document is not None and self.document.covers_whole_day()


@dataclass(frozen=True, slots=True)
class HistoryFetch:
    """What one [PriceRepository.async_fetch_history] call brought.

    `stored` are the days now held; `missing` the days the provider answered without a usable file (nothing
    published for it, or a file the parser refused), with the code; `failed` the first transport failure (the
    provider could not be asked: back off), with the provider's `retry_after` when it named one; `switched` that
    the area moved to another provider while it was asked, so nothing was kept.
    """

    stored: tuple[date, ...] = ()
    missing: dict[date, FailureCode] = field(default_factory=dict)
    failed: FailureCode | None = None
    retry_after: timedelta | None = None
    switched: bool = False


@dataclass(slots=True)
class _Cached:
    """One cached resource: the raw document as published, and when it arrived.

    The raw mapping is what is stored and restored; the parsed model is rebuilt through the same
    parser, so a restored document is validated by the same rules as a fetched one.
    """

    document: Any
    fetched_at: datetime
    # The parsed model kept beside the raw document so a read does not re-parse.
    parsed: Any
    # How this document entered the process (network or store); a later read of it is `memory`,
    # a fact about the call.
    source: SnapshotSource = "network"
    # Which provider delivered a day file (`relay` for one kept from before 2.0); stored beside it.
    provider: str | None = None


def is_transport(code: FailureCode | None) -> bool:
    return code is not None and code in TRANSPORT_CODES


class _Refused(Exception):
    """A provider's answer that brought no file, carrying the provider's stable code."""

    def __init__(self, code: FailureCode) -> None:
        super().__init__(code)
        self.code: FailureCode = code


def source_of(cached: _Cached, *, explicit: SnapshotSource | None) -> SnapshotSource:
    """Where a shown document came from: `memory` means this call found it in the process and made no
    request; `network` and `store` mean the document itself entered that way.
    """
    if explicit is not None:
        return explicit
    return "store" if cached.source == "store" else "memory"


class PriceRepository:
    """The bundled area list and the dated documents, each fetched once.

    Constructed once per instance (see [async_setup_price_repository]). Every public method returns an
    immutable snapshot; callers never reach into the cache.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        *,
        store: Store[dict[str, Any]] | None = None,
        now=None,
        bundled: BundledCatalogue | None = None,
        router: ProviderRouter | None = None,
    ) -> None:
        self._hass = hass
        # Which provider an area's files come from; without a router, none for any area.
        self._router = router if router is not None else ProviderRouter()
        self._now = now if now is not None else dt_util.utcnow

        # Seams for tests (races and clock changes are timing); `None` means the real thing.
        self._store: Store[dict[str, Any]] = store if store is not None else Store(hass, STORAGE_VERSION, STORAGE_KEY)
        #: The area list shipped with the integration, the only one: never fetched, never stored.
        self._bundled = bundled
        # Day files per (area, market day), one per market calendar they are dated in (see [_held_file]).
        self._days: dict[tuple[str, date], dict[str, _Cached]] = {}
        # The last profile built per area, with what it was built from (see [profile_for]).
        self._profiles: dict[str, tuple[tuple[Any, ...], PriceProfile | None]] = {}
        # When a provider without an index last answered "not published yet", per (area, market calendar, day);
        # not stored (a restart asks once).
        self._not_published: dict[tuple[str, str, date], datetime] = {}
        # One entry per resource key ever attempted: when it was tried and how it failed (`None` on success).
        self._attempts: dict[tuple[str, ...], tuple[datetime, FailureCode | None]] = {}
        self._inflight: dict[tuple[str, ...], asyncio.Task[Any]] = {}
        # The single-flight restore task, created on the first ask and awaited by later ones.
        self._restore_task: asyncio.Task[None] | None = None
        # The last few archive files fetched (see [ARCHIVE_MEMO_SIZE]), oldest first.
        self._archive_memo: dict[tuple[str, date], PriceDocument] = {}
        # Guards the store write only (local disk, never a request).
        self._write_lock = asyncio.Lock()

    def provider_for(self, area_id: str) -> PriceProvider | None:
        """The provider an area's files come from now, or `None` when it has no source."""
        return self._router.provider_for(area_id)

    def source_status(self, area_id: str) -> SourceStatus:
        """The area's price source now and its notice (`providers/notices.py`)."""
        return self._router.source_status(area_id)

    def entsoe_provider(self) -> EntsoeProvider | None:
        """The ENTSO-E provider (for checking a key before it is stored); `None` without the bundled catalogue."""
        return self._router.entsoe

    def add_provider_listener(self, listener) -> Any:
        """Call [listener] whenever the choice of provider may have changed for some area; returns the remover."""
        return self._router.add_listener(listener)

    @callback
    def stop_following_providers(self) -> None:
        """Stop following the config entries the choice of provider depends on (Home Assistant is stopping)."""
        self._router.async_stop()

    def area_entry(self, area_id: str) -> AreaEntry | None:
        """The area as it is read now: the bundled catalogue's entry with the market calendar and publication of
        the area's provider. `None` for an area the catalogue does not list. An area with no provider keeps the
        entry's own calendar (the one its files kept from before 2.0 are dated on), so what it holds still shows.
        """
        catalogue = self._held_catalogue()
        entry = None if catalogue is None else catalogue.area(area_id)
        if entry is None:
            return None
        provider = self._router.provider_for(area_id)
        calendar = None if provider is None else provider.market_calendar(area_id)
        if provider is None or calendar is None:
            return entry
        return replace(entry, market_tz=calendar, publication=provider.publication)

    def calendars(self, area_id: str) -> tuple[str, str] | None:
        """`(tz, market_tz)` for an area whose display day is cut from market-day files, else `None`.

        From [area_entry]: an area no catalogue lists (or none held) is read one file per day.
        """
        entry = self.area_entry(area_id)
        if entry is None or not entry.split_calendar:
            return None
        return entry.tz, entry.market_tz

    def day_snapshot(self, area_id: str, day: date) -> DaySnapshot:
        """What is on record for a display date right now, without asking the network.

        Never fetches. Answers exactly what [async_get_day] would without fetching.
        """
        calendars = self.calendars(area_id)
        if calendars is None:
            return self.file_snapshot(area_id, day)
        return self._display_snapshot(area_id, day, *calendars)

    def file_snapshot(self, area_id: str, day: date) -> DaySnapshot:
        """What is on record for one market-day file right now, without asking the network."""
        return self._day_snapshot(area_id, day, authority=self._authority_now(area_id, day))

    def file_document(self, area_id: str, day: date) -> dict[str, Any] | None:
        """The held market-day file as published (the raw `"v": 1` document, a copy), or `None`. No I/O.

        The file read is the one [file_snapshot] shows: dated on the calendar the area is read in now.
        """
        cached = self._held_file(area_id, day)
        if cached is None or not isinstance(cached.document, dict):
            return None
        return copy.deepcopy(cached.document)

    def _display_snapshot(self, area_id: str, day: date, tz: str, market_tz: str) -> DaySnapshot:
        """A display date cut from the files covering it; its facts are its principal file's."""
        principal = self.file_snapshot(area_id, principal_market_day(day, tz, market_tz))
        files = [self.file_snapshot(area_id, key) for key in market_days_for(day, tz, market_tz)]
        document = None
        if principal.document is not None:
            document = compose_display_day(
                area_id, day, tz, (item.document for item in files if item.document is not None)
            )
        return DaySnapshot(
            area_id=area_id,
            day=day,
            state=principal.state,
            document=document,
            source=principal.source,
            fetched_at=principal.fetched_at,
            attempt_at=principal.attempt_at,
            attempt_error=principal.attempt_error,
            index_authority=principal.index_authority,
            refreshing=any(item.refreshing for item in files),
            complete=principal.is_complete,
        )

    def catalogue_snapshot(self) -> CatalogueSnapshot:
        """The area list (the bundled one), without any I/O: `ready`, or `unavailable` when the file could not be
        read."""
        catalogue = self._held_catalogue()
        return CatalogueSnapshot(
            state="ready" if catalogue is not None else "unavailable",
            catalogue=catalogue,
            source=None if catalogue is None else "bundled",
            fetched_at=None,
            attempt_at=None,
            attempt_error=None,
            refreshing=False,
        )

    async def async_get_day(self, area_id: str, day: date, *, refresh: bool = False) -> DaySnapshot:
        """One display date's document, each file behind it asked for under the schedule's rules.

        For a split-calendar area every file covering the date is asked for under the file rules, then the
        date is cut from them ([day_snapshot]); otherwise this is [async_get_file] for that one day.
        """
        await self.async_restore()
        calendars = self.calendars(area_id)
        if calendars is None:
            return await self.async_get_file(area_id, day, refresh=refresh)
        tz, market_tz = calendars
        for key in market_days_for(day, tz, market_tz):
            # Each file is asked for under the schedule (the file call keeps to it).
            await self.async_get_file(area_id, key, refresh=refresh)
        return self._display_snapshot(area_id, day, tz, market_tz)

    async def async_get_file(self, area_id: str, day: date, *, refresh: bool = False) -> DaySnapshot:
        """One market-day file's document: a held file is answered as it is (unless its provider wants it once
        more, see `wants_again`), a missing one is asked for when the schedule says it is out. An area with no
        provider answers what it holds; a missing file of a published day is recorded as `source_missing`, with no
        request.
        """
        await self.async_restore()
        provider = self._router.provider_for(area_id)
        if provider is None:
            return self._without_source(area_id, day)
        return await self._get_scheduled_file(area_id, day, provider, refresh=refresh)

    def _without_source(self, area_id: str, day: date) -> DaySnapshot:
        """A file of an area with no provider: what is held, or (a published day) the attempt recorded as
        `source_missing`, so the area reads as having tried and found no source. No I/O."""
        if self._held_file(area_id, day) is None:
            calendar = self._market_calendar(area_id)
            if calendar is not None and day <= market_today(self._now(), calendar):
                self._attempts[("day", area_id, day.isoformat())] = (self._now(), "source_missing")
        return self._day_snapshot(area_id, day, authority=self._authority_now(area_id, day))

    async def _get_scheduled_file(
        self, area_id: str, day: date, provider: PriceProvider, *, refresh: bool
    ) -> DaySnapshot:
        """One file from the area's provider: a held file is answered as it is (unless its provider wants
        it once more, see `wants_again`), a missing one is asked for when the schedule says it is out."""
        cached = self._held_file(area_id, day)
        again = cached is not None and provider.wants_again(area_id, cached.parsed)
        if cached is not None and not refresh and not again:
            return self._day_snapshot(area_id, day, authority="listed")
        if cached is None and not self.worth_asking(area_id, day):
            return self._day_snapshot(area_id, day, authority=self._authority_now(area_id, day))
        key = ("day", area_id, day.isoformat())

        async def load() -> DaySnapshot:
            return await self._load_day(area_id, day, provider)

        return await self._shared(key, load)

    def worth_asking(self, area_id: str, day: date) -> bool:
        """Whether a file is worth a request now: the area has a provider, the file is not held (or held and wanted
        once more) and it is out by the schedule. No I/O."""
        provider = self._router.provider_for(area_id)
        calendar = self._market_calendar(area_id)
        if provider is None or calendar is None:
            return False
        cached = self._held_file(area_id, day)
        if cached is not None and not provider.wants_again(area_id, cached.parsed):
            return False
        return schedule_open(day, now=self._now(), market_tz=calendar, publication=provider.publication)

    def file_authority(self, area_id: str, day: date) -> IndexAuthority:
        """What the schedule and the provider's last answer say about one market-day file right now. No I/O."""
        return self._authority_now(area_id, day)

    async def async_get_archive_day(self, area_id: str, day: date) -> PriceDocument | None:
        """One past display date's document, asked of the area's provider.

        For the one-off history import. Each file is validated like any other; an empty answer and every
        failure answer `None` (the caller remembers the miss for its run, so nothing is asked twice). The
        documents are returned and not cached: archive days never enter the live-day cache or the persisted
        store (a few are kept in memory so two neighbouring display days sharing a file ask for it once). A
        file the repository already holds is answered from it without a request. A split-calendar date is
        cut from its files, and is `None` when its principal file is missing.
        """
        await self.async_restore()
        calendars = self.calendars(area_id)
        if calendars is None:
            return await self._archive_file(area_id, day)
        tz, market_tz = calendars
        principal = principal_market_day(day, tz, market_tz)
        files: list[PriceDocument] = []
        for key in market_days_for(day, tz, market_tz):
            document = await self._archive_file(area_id, key)
            if document is None and key == principal:
                return None
            if document is not None:
                files.append(document)
        return compose_display_day(area_id, day, tz, files)

    async def _archive_file(self, area_id: str, day: date) -> PriceDocument | None:
        cached = self._held_file(area_id, day)
        if cached is not None:
            return cached.parsed
        remembered = self._archive_memo.get((area_id, day))
        if remembered is not None:
            return remembered
        if self._out_of_reach(area_id, day):
            # Older than the provider keeps: asking would only collect a refusal.
            self._attempts[("archive", area_id, day.isoformat())] = (self._now(), "history_out_of_reach")
            return None

        async def load() -> PriceDocument | None:
            return await self._load_archive_day(area_id, day)

        document = await self._shared(("archive", area_id, day.isoformat()), load)
        if document is not None:
            self._archive_memo[(area_id, day)] = document
            while len(self._archive_memo) > ARCHIVE_MEMO_SIZE:
                self._archive_memo.pop(next(iter(self._archive_memo)))
        return document

    def _out_of_reach(self, area_id: str, day: date) -> bool:
        provider = self._router.provider_for(area_id)
        reach = None if provider is None else provider.history_reach
        calendar = self._market_calendar(area_id)
        if reach is None or calendar is None:
            return False
        return day < market_today(self._now(), calendar) - reach

    def archive_reachable(self, area_id: str, day: date) -> bool:
        """Whether a past display date can be asked of the area's provider at all: its principal file is not older
        than the provider keeps (`history_reach`). No I/O; a held date is answered from the store either way."""
        calendars = self.calendars(area_id)
        principal = day if calendars is None else principal_market_day(day, *calendars)
        return not self._out_of_reach(area_id, principal)

    def holds_file(self, area_id: str, day: date) -> bool:
        """Whether the market-day file is held on the calendar the area is read in now. No I/O."""
        return self._held_file(area_id, day) is not None

    def history_reachable(self, area_id: str, day: date) -> bool:
        """Whether a past market-day file is within the area's provider's `history_reach`. No I/O."""
        return not self._out_of_reach(area_id, day)

    async def async_fetch_history(self, area_id: str, days: Sequence[date], *, persist: bool = True) -> HistoryFetch:
        """Past market-day files for the history store, asked of the area's provider (the backfill,
        `pricing/price_history.py`).

        A day held, not yet over (the market's today or later: the refresh manager's) or older than the provider
        keeps is not asked. One day is one `async_fetch_day`; several consecutive days are one `async_fetch_days`,
        for a provider that answers a range. Each file is validated like any other and kept under the provider that
        sent it; the store is written once (unless [persist] is false: the caller writes later with
        [async_persist_history]). A provider that no longer serves the area when the answer arrives has its files
        dropped (`switched`).
        """
        await self.async_restore()
        provider = self._router.provider_for(area_id)
        calendar = self._market_calendar(area_id)
        if provider is None or calendar is None:
            return HistoryFetch(failed="source_missing")
        today = market_today(self._now(), calendar)
        wanted = sorted(
            day
            for day in set(days)
            if day < today and self._held_file(area_id, day) is None and not self._out_of_reach(area_id, day)
        )
        if not wanted:
            return HistoryFetch()
        if len(wanted) == 1:
            fetches = {wanted[0]: await provider.async_fetch_day(area_id, wanted[0])}
        else:
            fetch_days = getattr(provider, "async_fetch_days", None)
            if fetch_days is None:
                raise ValueError("this provider answers one day at a time")
            fetches = await fetch_days(area_id, wanted)
        if self._router.provider_for(area_id) is not provider:
            return HistoryFetch(switched=True)
        stored: list[date] = []
        missing: dict[date, FailureCode] = {}
        failed: FailureCode | None = None
        retry_after: timedelta | None = None
        for day in wanted:
            # A day a range answer did not carry is a day the provider has nothing for.
            fetch = fetches.get(day) or DayFetch("not_published", code="not_published")
            if fetch.retry_after is not None:
                retry_after = max(retry_after or fetch.retry_after, fetch.retry_after)
            if fetch.outcome == "failed" and is_transport(fetch.code):
                failed = failed or fetch.code
                continue
            try:
                parsed, document = self._accept(fetch, area_id, day)
            except (RelayParseError, _Refused) as err:
                missing[day] = err.code
                continue
            self._days.setdefault((area_id, day), {})[parsed.tz] = _Cached(
                document=document, fetched_at=self._now(), parsed=parsed, provider=provider.key
            )
            stored.append(day)
        if stored and persist:
            await self._persist()
        return HistoryFetch(stored=tuple(stored), missing=missing, failed=failed, retry_after=retry_after)

    async def async_persist_history(self) -> None:
        """Write the store now (after [async_fetch_history] calls made with `persist=False`)."""
        await self._persist()

    def profile_for(self, area_id: str) -> PriceProfile | None:
        """The area's history profile, built from the held day files, or `None`. No I/O.

        The window is the 28 market days ending with the market's today; its files are the ones held on the
        area's market calendar, keyed by market date (never a display day cut from two files, which would
        count an hour twice). Fewer than 21 of them, or an area no catalogue lists, is no profile: the planner
        treats that like any other "no profile" and plans on published prices. Built once per window and set
        of files: a new day file or the next market day builds it again.
        """
        entry = self.area_entry(area_id)
        if entry is None:
            return None
        from_date, to_date = profile_window(self._now(), entry.market_tz)
        documents: dict[date, PriceDocument] = {}
        held: list[tuple[Any, ...]] = []
        day = from_date
        while day <= to_date:
            cached = self._held_file(area_id, day)
            if cached is not None:
                documents[day] = cached.parsed
                held.append((day, cached.parsed.tz, cached.fetched_at))
            day += timedelta(days=1)
        key = (entry.tz, entry.market_tz, from_date, to_date, tuple(held))
        built = self._profiles.get(area_id)
        if built is not None and built[0] == key:
            return built[1]
        profile = build_profile(area_id, entry.tz, entry.market_tz, self._now(), documents)
        self._profiles[area_id] = (key, profile)
        return profile

    async def async_get_profile(self, area_id: str) -> PriceProfile | None:
        """The area's history profile ([profile_for]) once the store has been read; never a request."""
        await self.async_restore()
        return self.profile_for(area_id)

    def profile_summary(self, area_id: str) -> dict[str, Any]:
        """The profile as diagnostics prints it: identity, window and coverage, never the price rows."""
        profile = self.profile_for(area_id)
        built = self._profiles.get(area_id)
        return {
            "area": area_id,
            "usable": profile is not None,
            # How many of the window's files are held (21 make a profile).
            "days_held": None if built is None else len(built[0][-1]),
            "generated": None if profile is None else _stamp(profile.generated),
            "from": None if profile is None else profile.from_date.isoformat(),
            "to": None if profile is None else profile.to_date.isoformat(),
            "weeks": None if profile is None else profile.weeks,
            "hour_entries": None if profile is None else len(profile.hours),
        }

    async def _shared(self, key: tuple[str, ...], load) -> Any:
        """One in-flight request per resource, whoever asks and however often.

        The work is a repository-owned task and every caller awaits it under `shield`: a cancelled caller
        stops waiting and nothing else. Completion removes the key, so a failed attempt leaves no poisoned entry.
        """
        task = self._inflight.get(key)
        # A finished task is not in flight: joining it would hand this caller the previous
        # attempt's result, including its failure, to a `refresh` that asked for a fresh one.
        if task is None or task.done():
            task = self._hass.async_create_task(load())
            self._inflight[key] = task
            task.add_done_callback(lambda finished, key=key: self._finished(key, finished))
        try:
            return await asyncio.shield(task)
        finally:
            # The awaiting caller is the first to know the attempt is over, so "nothing in flight"
            # must be true for it (no `loading` one turn too long). The done callback below covers
            # callers that were cancelled or never awaited; both are idempotent and never clear a
            # newer request for the key.
            if self._inflight.get(key) is task and task.done():
                self._inflight.pop(key, None)

    @callback
    def _finished(self, key: tuple[str, ...], task: asyncio.Task[Any]) -> None:
        # Only if this task is still the one on record; popping by key alone could unregister a newer request.
        if self._inflight.get(key) is task:
            self._inflight.pop(key, None)
        if not task.cancelled():
            # Read the exception even when no caller did, to avoid an "exception was never
            # retrieved" warning.
            task.exception()

    async def async_restore(self) -> None:
        """Read the store once, through the same parsers as a network response.

        Single-flight, and the awaiting half matters as much as the reading half: one repository-owned
        task reads and adopts, every caller awaits it under `shield`, the store is read at most once, and a
        cancelled caller does not cancel it. There is deliberately no flag set before the first `await`,
        which would let a second caller fetch fresh data that the delayed store read then overwrites.

        Anything unexpected in the stored content (missing file, another schema version, an invalid
        document, a naive timestamp) is ignored with one concise line; a failed store read is treated the
        same. Restoring is a convenience and never a reason to refuse to run or to leave a caller waiting.
        """
        task = self._restore_task
        if task is None or task.cancelled() or (task.done() and task.exception() is not None):
            # A restore that never completed or raised is not awaited again: start a fresh one,
            # so no later caller inherits a broken initialization.
            task = self._hass.async_create_task(self._restore_and_adopt())
            self._restore_task = task
            task.add_done_callback(self._restore_finished)
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover - _restore_and_adopt swallows its own
            _LOGGER.warning("Restoring the stored price days failed; continuing without them")

    @callback
    def _restore_finished(self, task: asyncio.Task[None]) -> None:
        if task.cancelled():
            # Only runtime shutdown gets here; clearing the handle keeps a next process from
            # awaiting a cancelled task.
            if self._restore_task is task:
                self._restore_task = None
            return
        if task.exception() is not None:
            _LOGGER.warning("Restoring the stored price days raised")

    async def _restore_and_adopt(self) -> None:
        try:
            raw = await self._store.async_load()
        except Exception as err:  # noqa: BLE001 - any store failure means "no store"
            _LOGGER.warning("Could not read the stored price days (%s); continuing without them", type(err).__name__)
            return
        # Every zone the stored documents name, resolved off the loop before they are parsed.
        await time_zones.async_resolve_document(raw)
        self._adopt(raw)

    def _adopt(self, raw: Any) -> None:
        """Take stored documents in; nothing valid is lost and an older copy never replaces a newer one."""

        if raw is None:
            return
        schema = raw.get("schema") if isinstance(raw, dict) else None
        if schema not in (STORAGE_SCHEMA, LEGACY_SCHEMA):
            _LOGGER.warning("Ignoring stored price days: unknown schema version")
            return
        legacy = schema == LEGACY_SCHEMA
        # The relay's area list, index and profiles that stores before 2.0 kept beside the days are not read: the
        # area list is the bundled one, and the next write leaves them out.
        days = raw.get("days")
        if isinstance(days, dict):
            for key, entry in days.items():
                restored = self._restore_day(key, entry, legacy=legacy)
                if restored is None:
                    continue
                where, cached = restored
                files = self._days.setdefault(where, {})
                # Adoption is by acquisition time, not arrival order, so a late restore cannot
                # replace something fetched since. Loaders await the restore before fetching, so
                # this is the invariant that keeps it true if a path ever forgets to.
                if _older_than(files.get(cached.parsed.tz), cached):
                    files[cached.parsed.tz] = cached

    def _restore_day(self, key: Any, entry: Any, *, legacy: bool) -> tuple[tuple[str, date], _Cached] | None:
        """One stored day file, filed under `AREA|market calendar|date` (schema 1: `AREA|date`, its calendar
        then read from the document itself, which is what makes the migration lossless).
        """
        if not isinstance(key, str) or not isinstance(entry, dict):
            return None
        parts = key.split("|")
        if len(parts) != (2 if legacy else 3):
            return None
        area_id, raw_day = parts[0], parts[-1]
        calendar = None if legacy else parts[1]
        try:
            day = date.fromisoformat(raw_day)
        except ValueError:
            return None
        document = entry.get("document")
        fetched_at = _aware(entry.get("fetched_at"))
        if document is None or fetched_at is None:
            _LOGGER.warning("Ignoring stored day %s: incomplete entry", key)
            return None
        try:
            # The identity is in the key and in the document and must agree; a document that does
            # not describe the day it is filed under is not shown.
            parsed = parse_day(document, area_id=area_id, day=day)
        except RelayParseError as err:
            _LOGGER.warning("Ignoring stored day %s: %s", key, err.code)
            return None
        if calendar is not None and calendar != parsed.tz:
            _LOGGER.warning("Ignoring stored day %s: dated on another calendar", key)
            return None
        provider = entry.get("provider")
        return (area_id, day), _Cached(
            document=document,
            fetched_at=fetched_at,
            parsed=parsed,
            source="store",
            provider=provider if isinstance(provider, str) and provider else "relay",
        )

    async def _load_day(self, area_id: str, day: date, provider: PriceProvider) -> DaySnapshot:
        """Ask [provider] for one file; the snapshot carries the schedule's answer after this attempt."""
        key = ("day", area_id, day.isoformat())
        self._attempts[key] = (self._now(), None)
        fetch = await provider.async_fetch_day(area_id, day)
        calendar = self._market_calendar(area_id)
        if fetch.outcome == "not_published":
            # Not out yet: no failure, nothing cached, and tomorrow is "not listed" until the next attempt.
            if calendar is not None:
                self._not_published[(area_id, calendar, day)] = self._now()
            return self._day_snapshot(area_id, day, authority=self._authority_now(area_id, day), settled=True)
        try:
            parsed, document = self._accept(fetch, area_id, day)
        except (RelayParseError, _Refused) as err:
            # A provider that lost the area while it was asked (its key rejected by this very answer) does not
            # speak for it any more: the new provider's attempt, maybe already made, stands.
            if self._router.provider_for(area_id) is provider:
                self._record_failure(key, err)
            return self._day_snapshot(area_id, day, authority=self._authority_now(area_id, day), settled=True)
        if calendar is not None:
            self._not_published.pop((area_id, calendar, day), None)
        self._days.setdefault((area_id, day), {})[parsed.tz] = _Cached(
            document=document, fetched_at=self._now(), parsed=parsed, provider=provider.key
        )
        await self._persist()
        return self._day_snapshot(
            area_id, day, authority=self._authority_now(area_id, day), source="network", settled=True
        )

    async def _load_archive_day(self, area_id: str, day: date) -> PriceDocument | None:
        key = ("archive", area_id, day.isoformat())
        self._attempts[key] = (self._now(), None)
        provider = self._router.provider_for(area_id)
        if provider is None:
            self._attempts[key] = (self._now(), "source_missing")
            return None
        try:
            parsed, _ = self._accept(await provider.async_fetch_day(area_id, day), area_id, day)
        except (RelayParseError, _Refused) as err:
            self._record_failure(key, err)
            return None
        return parsed

    def _accept(self, fetch: DayFetch, area_id: str, day: date) -> tuple[PriceDocument, Any]:
        """A provider's answer as a validated file and its raw document; anything else raises with its code."""
        if fetch.outcome != "fetched":
            raise _Refused(fetch.code or "network")
        return self._parse_file(fetch.document, area_id, day), fetch.document

    def _parse_file(self, document: Any, area_id: str, day: date) -> PriceDocument:
        """A fetched day file, validated, and refused when it is not dated on the calendar the area is read
        in: it could never be found by a lookup, so taking it would only ask for it again and again.
        """
        parsed = parse_day(document, area_id=area_id, day=day)
        calendar = self._market_calendar(area_id)
        if calendar is not None and not _dated_on(parsed, calendar):
            raise RelayParseError(
                "invalid_coverage", f"the day document is dated on {parsed.tz}, not on the area's {calendar}"
            )
        return parsed

    def _market_calendar(self, area_id: str) -> str | None:
        """The calendar an area's day files are dated in now (its provider's `market_tz`, [area_entry]); `None`
        for an area no catalogue lists.
        """
        entry = self.area_entry(area_id)
        return None if entry is None else entry.market_tz

    def _held_file(self, area_id: str, day: date) -> _Cached | None:
        """The held file for a market day on the calendar the area is read in, or `None`.

        Calendars are compared by the file's first instant, not by name: a file dated in Stockholm is the
        same day as one dated in Berlin, while a Helsinki file is another day than a CET one and is not read
        (it stays held, under its own key, until retention drops it). With no catalogue entry to say which
        calendar, the newest file held for the day is the answer.
        """
        files = self._days.get((area_id, day))
        if not files:
            return None
        calendar = self._market_calendar(area_id)
        candidates = [
            cached for cached in files.values() if calendar is None or _dated_on(cached.parsed, calendar)
        ]
        return max(candidates, key=lambda cached: cached.fetched_at, default=None)

    def _prune(self) -> None:
        """Drop the day files older than [HISTORY_KEEP_DAYS] (by market day, against today in UTC), and the
        "not published yet" answers for days already past."""
        today = self._now().astimezone(dt_util.UTC).date()
        oldest = today - timedelta(days=HISTORY_KEEP_DAYS)
        for where in [where for where in self._days if where[1] < oldest]:
            del self._days[where]
        # "Not published yet" only ever concerns tomorrow; yesterday's answers are of no use.
        for key in [key for key in self._not_published if key[2] < today - timedelta(days=1)]:
            del self._not_published[key]

    def _record_failure(self, key: tuple[str, ...], err: Exception) -> None:
        code: FailureCode = err.code if isinstance(err, (RelayParseError, _Refused)) else "network"
        self._attempts[key] = (self._now(), code)
        _LOGGER.debug("Price %s failed: %s", key[0], code)

    def _authority_now(self, area_id: str, day: date) -> IndexAuthority:
        """What the provider's schedule and its last answer say about a file, without any I/O; `unknown` for an
        area with no provider (a held file is still `listed`)."""
        provider = self._router.provider_for(area_id)
        calendar = self._market_calendar(area_id)
        if provider is None or calendar is None:
            return "listed" if self._held_file(area_id, day) is not None else "unknown"
        attempt_at, error = self._attempt(("day", area_id, day.isoformat()))
        return schedule_authority(
            day,
            held=self._held_file(area_id, day) is not None,
            now=self._now(),
            market_tz=calendar,
            publication=provider.publication,
            attempt_at=attempt_at,
            attempt_failed=error is not None,
            not_published_at=self._not_published.get((area_id, calendar, day)),
        )

    def _held_catalogue(self) -> AreaCatalogue | None:
        """The bundled area list, or `None` when it could not be read."""
        return None if self._bundled is None else self._bundled.catalogue

    def _day_snapshot(
        self,
        area_id: str,
        day: date,
        *,
        authority: IndexAuthority,
        source: SnapshotSource | None = None,
        settled: bool = False,
    ) -> DaySnapshot:
        key = ("day", area_id, day.isoformat())
        cached = self._held_file(area_id, day)
        attempt_at, error = self._attempt(key)
        refreshing = not settled and key in self._inflight

        if cached is None:
            state: DayState = "loading" if refreshing else (
                "invalid" if error is not None and not is_transport(error) else "unavailable"
            )
        elif error is not None:
            state = "stale"
        else:
            state = "ready" if cached.parsed.covers_whole_day() else "incomplete"

        return DaySnapshot(
            area_id=area_id,
            day=day,
            state=state,
            document=None if cached is None else cached.parsed,
            source=None if cached is None else source_of(cached, explicit=source),
            fetched_at=None if cached is None else cached.fetched_at,
            attempt_at=attempt_at,
            attempt_error=error,
            index_authority=authority,
            refreshing=refreshing,
        )

    def _attempt(self, key: tuple[str, ...]) -> tuple[datetime | None, FailureCode | None]:
        return self._attempts.get(key, (None, None))

    def _payload(self) -> dict[str, Any]:
        """Everything worth restoring, as one document, rebuilt from the caches on every write so the last
        writer writes the union.
        """
        return {
            "schema": STORAGE_SCHEMA,
            "days": {
                f"{area_id}|{calendar}|{day.isoformat()}": {**_entry(cached), "provider": cached.provider}
                for (area_id, day), files in self._days.items()
                for calendar, cached in files.items()
            },
        }

    async def _persist(self) -> None:
        """Write the cache, serialized against other writers and never against a request, after dropping
        the day files retention no longer keeps.
        """
        async with self._write_lock:
            self._prune()
            await self._store.async_save(self._payload())

def read_state(present: bool, error: FailureCode | None, refreshing: bool) -> CatalogueState:
    """The state of a resource as a reader sees it, with no attempt started.

    Same precedence as the fetch paths: last good wins, a failure beside it is `stale`, and only an
    empty cache is `loading`, `invalid` or `unavailable`.
    """
    if present:
        return "ready" if error is None else "stale"
    if refreshing:
        return "loading"
    if error is None or is_transport(error):
        return "unavailable"
    return "invalid"


def _dated_on(document: PriceDocument, calendar: str) -> bool:
    """Whether a day file starts at its date's midnight on [calendar], i.e. covers that calendar's day."""
    return document.start_instant == local_midnight(document.day, calendar).astimezone(dt_util.UTC)


def _older_than(existing: _Cached | None, candidate: _Cached) -> bool:
    """Whether [candidate] may replace [existing]: strictly newer, or nothing there (a tie keeps what is in hand)."""
    return existing is None or existing.fetched_at < candidate.fetched_at


def _entry(cached: _Cached | None) -> dict[str, Any] | None:
    if cached is None:
        return None
    return {"document": cached.document, "fetched_at": cached.fetched_at.isoformat()}


def _aware(value: Any) -> datetime | None:
    """A stored timestamp, or `None` when not trustworthy; a naive one is refused rather than read as host-local."""
    if not isinstance(value, str):
        return None
    parsed = dt_util.parse_datetime(value)
    if parsed is None or parsed.tzinfo is None:
        return None
    return parsed


# Summaries for diagnostics: plain dicts of scalars, no price array and no response text.


def _stamp(moment: datetime | None) -> str | None:
    return None if moment is None else moment.isoformat()


def catalogue_summary(snapshot: CatalogueSnapshot) -> dict[str, Any]:
    catalogue = snapshot.catalogue
    return {
        "state": snapshot.state,
        "source": snapshot.source,
        "fetched_at": _stamp(snapshot.fetched_at),
        "attempt_at": _stamp(snapshot.attempt_at),
        "error": snapshot.attempt_error,
        "refreshing": snapshot.refreshing,
        "area_count": None if catalogue is None else len(catalogue.areas),
        "generated": None if catalogue is None else _stamp(catalogue.generated),
        "version": None if catalogue is None else catalogue.version,
    }


def day_summary(snapshot: DaySnapshot) -> dict[str, Any]:
    document = snapshot.document
    return {
        "area": snapshot.area_id,
        "date": snapshot.day.isoformat(),
        "state": snapshot.state,
        "source": snapshot.source,
        "refreshing": snapshot.refreshing,
        "fetched_at": _stamp(snapshot.fetched_at),
        "attempt_at": _stamp(snapshot.attempt_at),
        "error": snapshot.attempt_error,
        "index_authority": snapshot.index_authority,
        "interval_count": snapshot.interval_count,
        "resolution_minutes": None if document is None else document.resolution_minutes,
        "unit": None if document is None else document.unit,
        "complete": snapshot.is_complete,
        "covers_until": None if document is None else _stamp(document.covers_until),
        "published": None if document is None else _stamp(document.published),
        "retrieved": None if document is None else _stamp(document.retrieved),
        "src": None if document is None else document.src,
        "fx_date": None if document is None or document.fx_date is None else document.fx_date.isoformat(),
        "fx_src": None if document is None else document.fx_src,
        "fx_currencies": None if document is None else sorted(document.fx),
    }



async def async_setup_price_repository(
    hass: HomeAssistant,
    *,
    store: Store[dict[str, Any]] | None = None,
    now=None,
) -> PriceRepository:
    """Create the one repository and restore what it can from the store.

    Domain-scoped: no config entry owns it, so unloading one charger cannot take the prices from
    another. A second call returns the existing repository after awaiting its restore; the publish
    happens synchronously with the lookup, so two simultaneous setups cannot build two. A restore, not a
    fetch: a restart shows the last good documents without asking the network.
    """
    data = domain_data(hass)
    # The fixed zones and Home Assistant's own, before anything reads them on the loop.
    await time_zones.async_resolve_defaults(hass.config.time_zone)
    if data.price_repository is not None:
        await data.price_repository.async_restore()
        return data.price_repository
    # The bundled area list, read off the loop; without it no area is listed and none has a provider. Read
    # before the second lookup, so that lookup and the publish below stay one synchronous step.
    bundled = await async_load_bundled(hass)
    if data.price_repository is not None:
        await data.price_repository.async_restore()
        return data.price_repository
    # The ECB rates a provider needs for an area priced in another currency (started by the first day that needs a rate).
    if data.fx_rates is None:
        data.fx_rates = FxRates(hass)
    # The installation's ENTSO-E key lives in the price entries' store, loaded here when it is not yet.
    keys = await async_setup_price_store(hass)
    if data.price_repository is not None:
        await data.price_repository.async_restore()
        return data.price_repository
    # Nord Pool through Home Assistant for the areas it trades while a `nordpool` entry is loaded; else ENTSO-E
    # with the installation's key; none otherwise (the area's notice says what to add). Spain's PVPC and the GB
    # Agile regions are read from the PVPC and Octopus Energy integrations when the person has added them. The
    # bundled catalogue holds every provider's routes, so without it there is no provider.
    router = ProviderRouter(
        None if bundled is None else NordPoolProvider(hass, bundled, data.fx_rates, now=now),
        None if bundled is None else EntsoeProvider(hass, bundled, keys, data.fx_rates, now=now),
        pvpc=None if bundled is None else PvpcProvider(hass, bundled, now=now),
        octopus=None if bundled is None else OctopusProvider(hass, bundled, data.fx_rates, now=now),
    )
    repository = PriceRepository(hass, store=store, now=now, bundled=bundled, router=router)
    router.async_start(hass)
    data.price_repository = repository
    await repository.async_restore()
    return repository
