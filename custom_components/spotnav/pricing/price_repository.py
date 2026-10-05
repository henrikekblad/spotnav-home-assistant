"""The shared SpotNav Relay price repository: one fetch/cache stream per resource.

There is one repository per Home Assistant instance, published on `SpotNavData` by
`async_setup_price_repository` and shared by every charger entry; a second charger adds no request,
cache or timer.

Four facts are kept apart, because collapsing any two makes a price layer lie:

1. last good: the newest valid document for a resource with its acquisition time. Nothing that
   failed to validate replaces it or relabels it as fresh.
2. the current attempt: when it happened and why it failed (a stable code), recorded beside last
   good, never instead of it.
3. authority: a parsed index is the relay's statement of which recent days exist. A day it does not
   list is authoritatively absent and not requested; an unreadable index is "authority
   unavailable", which is not proof of absence.
4. the body's validity: a document failing the contract is `invalid`, and the day keeps its last good.

Coalescing: concurrent callers for one resource share one request, owned by the repository (a task
on the HA loop), not by its first caller. A cancelled caller stops waiting and nothing else: the
request completes, is cached, and the other waiters get it. A failed request removes itself from
the in-flight table so the next caller starts fresh. No lock is held across network I/O; the only
lock guards the store write (local disk), so two completions cannot lose each other's resources.

The restore is single-flight the same way: one repository-owned task reads the store and adopts what
it finds, callers await it under `shield`, and adoption is ordered by acquisition time so a late
store read never overwrites a newer fetch. A failed restore is logged and treated as an absent store.

Only explicit `refresh` operations live here: no cadence, backoff or timers (see the manager). Prices
are stored as published.

Contract v2: the area list and the index are read from `/v2/…` first and from `/v1/…` when the relay
answers v2 with a 404 or a body this client cannot read; the index is always read in the version the
held area list came in. Day files keep their `/v1/<AREA>/<YYYY>/<MM-DD>.json` paths and are cached
per **market** day. Everything a caller asks by date ([day_snapshot], [async_get_day],
[async_get_archive_day]) is a **display** date in the area's `tz`: for an area whose two calendars
agree that is exactly one file, and for one whose do not (Great Britain, v2 Portugal) the display day
is cut from the files that cover it (`pricing/market_day.py`). The file-level calls ([file_snapshot],
[async_get_file]) are for the refresh manager, which decides which files to ask for.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Final, Literal
from urllib.parse import quote

from aiohttp import ClientError, ClientSession
from homeassistant.core import callback, HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from ..const import DOMAIN
from ..runtime import domain_data
from .market_day import compose_display_day, market_days_for, principal_market_day
from .relay_contract import (
    AreaCatalogue,
    document_version,
    loads_document,
    parse_catalogue,
    parse_day,
    parse_index,
    parse_profile,
    ParseCode,
    PriceDocument,
    PriceProfile,
    RelayIndex,
    RelayParseError,
)


_LOGGER = logging.getLogger(__name__)

#: The deployed relay; injected so tests can build a repository that cannot reach the network.
DEFAULT_BASE_URL: Final = "https://spotnav.sensnology.se"

#: Request timeout and body size limit. A day document is ~10 kB and an index ~1 kB; half a
#: megabyte is generous but bounds a hung connection or a mistaken URL.
REQUEST_TIMEOUT_S: Final = 20.0
MAX_RESPONSE_BYTES: Final = 512 * 1024

#: Bytes asked per read: small enough that a trickling stream yields to the event loop and meets
#: the overall timeout.
CHUNK_BYTES: Final = 64 * 1024


#: A history profile older than this (by its own `generated`) is not used, whatever the cache holds:
#: the relay withdraws a stale one itself, so an old copy means the relay could not be asked.
PROFILE_MAX_AGE: Final = timedelta(days=2)

#: After a failed profile request nobody asks again for this long (a calculation asks on every publication).
PROFILE_RETRY_AFTER: Final = timedelta(minutes=30)

#: The contract versions asked for, newest first: a relay that predates v2 answers it with a 404.
CONTRACT_VERSIONS: Final = (2, 1)

#: Archive files held in memory for one import run's neighbouring display days (a London day shares a
#: Paris file with the next); never persisted, never the live cache.
ARCHIVE_MEMO_SIZE: Final = 8

STORAGE_VERSION: Final = 1
STORAGE_KEY: Final = f"{DOMAIN}_relay_cache"

#: What a caller could not get, as a stable code: the transport half is here, the content half is
#: `relay_contract`'s, kept in one snapshot field.
FetchCode = Literal["timeout", "network", "http_status", "too_large"]
FailureCode = FetchCode | ParseCode

#: What the reader is looking at. `loading` is "nothing to show yet and a request is running"; a
#: refresh while a document is on screen is [DaySnapshot.refreshing].
DayState = Literal["loading", "ready", "incomplete", "stale", "unavailable", "invalid"]
CatalogueState = Literal["loading", "ready", "stale", "unavailable", "invalid"]

#: Where the index says a day is. "Not listed" is authoritative only when the index itself was read.
IndexAuthority = Literal["listed", "not_listed", "unknown"]

#: Where a snapshot's document came from; a restored one is not freshly fetched.
SnapshotSource = Literal["memory", "store", "network"]


@dataclass(frozen=True, slots=True)
class CatalogueSnapshot:
    """The relay's area list, and the state of the attempt to have one."""

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
class IndexSnapshot:
    """The relay's index, and the state of the attempt to have one."""

    state: CatalogueState
    index: RelayIndex | None
    source: SnapshotSource | None
    fetched_at: datetime | None
    attempt_at: datetime | None
    attempt_error: FailureCode | None
    refreshing: bool


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
    newest try. `index_authority` says whether the index listed this day; an unreadable index leaves
    it `unknown`.
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
    index_revision: str | None
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


TRANSPORT_CODES: Final[frozenset[str]] = frozenset({"timeout", "network", "http_status", "too_large"})


def is_transport(code: FailureCode | None) -> bool:
    return code is not None and code in TRANSPORT_CODES


def source_of(cached: _Cached, *, explicit: SnapshotSource | None) -> SnapshotSource:
    """Where a shown document came from: `memory` means this call found it in the process and made no
    request; `network` and `store` mean the document itself entered that way.
    """
    if explicit is not None:
        return explicit
    return "store" if cached.source == "store" else "memory"


async def _read_bounded(stream) -> bytes:
    """Read a body to EOF, refusing to hold more than [MAX_RESPONSE_BYTES] of it.

    One aiohttp `read(n)` may return fewer than `n` bytes short of EOF, so a chunked response would
    otherwise be rejected as malformed JSON. This accumulates until the stream ends and stops as soon as
    the limit would be exceeded (the caller's `async with` abandons the connection). The caller's timeout
    wraps the whole loop.
    """
    chunks: list[bytes] = []
    total = 0
    while True:
        # Ask for the rest of the allowance plus one byte to prove an overflow, in bounded slices.
        chunk = await stream.read(min(CHUNK_BYTES, MAX_RESPONSE_BYTES + 1 - total))
        if not chunk:
            break
        total += len(chunk)
        if total > MAX_RESPONSE_BYTES:
            raise _Failure("too_large")
        chunks.append(chunk)
    return b"".join(chunks)


class _Failure(Exception):
    """A fetch that did not produce a document, carrying its stable code (and the HTTP status, if any)."""

    def __init__(self, code: FetchCode, status: int | None = None) -> None:
        super().__init__(code)
        self.code: FetchCode = code
        self.status = status


class PriceRepository:
    """The relay's catalogue, index and dated documents, fetched once each.

    Constructed once per instance (see [async_setup_price_repository]). Every public method returns an
    immutable snapshot; callers never reach into the cache.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        *,
        base_url: str = DEFAULT_BASE_URL,
        session: ClientSession | None = None,
        store: Store[dict[str, Any]] | None = None,
        now=None,
    ) -> None:
        self._hass = hass
        self._base_url = base_url.rstrip("/")
        # `None` means the shared HA client when a request is made (`async_get_clientsession`
        # needs a running loop, and setup constructs this before anything is asked).
        self._session = session
        self._now = now if now is not None else dt_util.utcnow

        # Seams for tests (races and clock changes are timing); `None` means the real thing.
        self._store: Store[dict[str, Any]] = store if store is not None else Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self._catalogue: _Cached | None = None
        #: The index revision (`areas_rev`) the held area list belongs to, as noted by the refresh manager;
        #: `None` when unknown (nothing noted yet, or a store written before it was kept).
        self._catalogue_rev: str | None = None
        self._index: _Cached | None = None
        self._days: dict[tuple[str, date], _Cached] = {}
        # The history profile per area; the local day it was last asked for with a definite answer (a
        # document, or the relay's 404), so it is requested once per area per day; and the last failed try.
        self._profiles: dict[str, _Cached] = {}
        self._profile_checked: dict[str, date] = {}
        self._profile_failed_at: dict[str, datetime] = {}
        # One entry per resource key ever attempted: when it was tried and how it failed (`None` on success).
        self._attempts: dict[tuple[str, ...], tuple[datetime, FailureCode | None]] = {}
        self._inflight: dict[tuple[str, ...], asyncio.Task[Any]] = {}
        # The single-flight restore task, created on the first ask and awaited by later ones.
        self._restore_task: asyncio.Task[None] | None = None
        # The last few archive files fetched (see [ARCHIVE_MEMO_SIZE]), oldest first.
        self._archive_memo: dict[tuple[str, date], PriceDocument] = {}
        # Guards the store write only (local disk, never a request).
        self._write_lock = asyncio.Lock()

    def calendars(self, area_id: str) -> tuple[str, str] | None:
        """`(tz, market_tz)` for an area whose display day is cut from market-day files, else `None`.

        From the held catalogue: an area it does not list (or none held) is read one file per day.
        """
        catalogue = self._catalogue
        entry = None if catalogue is None else catalogue.parsed.area(area_id)
        if entry is None or not entry.split_calendar:
            return None
        return entry.tz, entry.market_tz

    def day_snapshot(self, area_id: str, day: date) -> DaySnapshot:
        """What is on record for a display date right now, without asking the network.

        Never fetches. Answers exactly what [async_get_day] would without fetching, including a day the
        index no longer lists but we still hold: `stale` (valid, no longer published).
        """
        calendars = self.calendars(area_id)
        if calendars is None:
            return self.file_snapshot(area_id, day)
        return self._display_snapshot(area_id, day, *calendars)

    def file_snapshot(self, area_id: str, day: date) -> DaySnapshot:
        """What is on record for one market-day file right now, without asking the network."""
        authority = self._authority_now(area_id, day)
        return self._day_snapshot(
            area_id, day, authority=authority, stale_override=authority == "not_listed"
        )

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
            index_revision=principal.index_revision,
            refreshing=any(item.refreshing for item in files),
            complete=principal.is_complete,
        )

    def catalogue_snapshot(self) -> CatalogueSnapshot:
        """What is on record for the area list right now, without any I/O."""
        attempt_at, error = self._attempt(("catalogue",))
        refreshing = ("catalogue",) in self._inflight
        return self._catalogue_snapshot(
            state=read_state(self._catalogue is not None, error, refreshing), settled=not refreshing
        )

    def index_snapshot(self) -> IndexSnapshot:
        """What is on record for the index right now, without any I/O."""
        attempt_at, error = self._attempt(("index",))
        refreshing = ("index",) in self._inflight
        return self._index_snapshot(
            state=read_state(self._index is not None, error, refreshing), settled=not refreshing
        )

    def catalogue_revision(self) -> str | None:
        """The index revision the held area list belongs to, or `None` when unknown."""
        return self._catalogue_rev

    async def async_note_catalogue_revision(self, revision: str) -> None:
        """Record that the held area list is the one of this index revision, and keep it in the store."""
        if self._catalogue is None or revision == self._catalogue_rev:
            return
        self._catalogue_rev = revision
        await self._persist()

    async def async_get_catalogue(self, *, refresh: bool = False) -> CatalogueSnapshot:
        """The relay's area list, from memory, from the store, or over the network."""
        await self.async_restore()
        if self._catalogue is not None and not refresh:
            return self._catalogue_snapshot(state="ready", source="memory")
        return await self._shared(("catalogue",), self._load_catalogue)

    async def async_get_index(self, *, refresh: bool = False) -> IndexSnapshot:
        """The relay's index: this client's authority on which days exist."""
        await self.async_restore()
        if self._index is not None and not refresh:
            return self._index_snapshot(state="ready", source="memory")
        return await self._shared(("index",), self._load_index)

    async def async_get_day(self, area_id: str, day: date, *, refresh: bool = False) -> DaySnapshot:
        """One display date's document, subject to the index's authority over each file behind it.

        For a split-calendar area every file covering the date is asked for under the file rules, then the
        date is cut from them ([day_snapshot]); otherwise this is [async_get_file] for that one day.
        """
        await self.async_restore()
        calendars = self.calendars(area_id)
        if calendars is None:
            return await self.async_get_file(area_id, day, refresh=refresh)
        tz, market_tz = calendars
        principal = principal_market_day(day, tz, market_tz)
        for key in market_days_for(day, tz, market_tz):
            # A later file nobody listed is not published yet: asking would only collect a 404.
            if key == principal or self._authority_now(area_id, key) == "listed":
                await self.async_get_file(area_id, key, refresh=refresh)
        return self._display_snapshot(area_id, day, tz, market_tz)

    async def async_get_file(self, area_id: str, day: date, *, refresh: bool = False) -> DaySnapshot:
        """One market-day file's document, subject to the index's authority.

        A day the index does not list is not requested (that is what the index prevents); a day whose index
        could not be read is requested, since an unread index is not proof of absence.
        """
        await self.async_restore()
        authority = await self._authority_for(area_id, day)
        cached = self._days.get((area_id, day))

        if authority == "not_listed":
            # Authoritative absence: held data is kept but reported as no longer published.
            return self._day_snapshot(area_id, day, authority=authority, stale_override=True)

        if cached is not None and not refresh:
            return self._day_snapshot(area_id, day, authority=authority)

        key = ("day", area_id, day.isoformat())

        async def load() -> DaySnapshot:
            return await self._load_day(area_id, day, authority=authority)

        return await self._shared(key, load)

    async def async_get_archive_day(self, area_id: str, day: date) -> PriceDocument | None:
        """One past display date's document, asked of the relay whatever the index lists (the index
        governs live days).

        For the one-off history import. Each file is validated like any other; the relay's 404 and every
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
        cached = self._days.get((area_id, day))
        if cached is not None:
            return cached.parsed
        remembered = self._archive_memo.get((area_id, day))
        if remembered is not None:
            return remembered

        async def load() -> PriceDocument | None:
            return await self._load_archive_day(area_id, day)

        document = await self._shared(("archive", area_id, day.isoformat()), load)
        if document is not None:
            self._archive_memo[(area_id, day)] = document
            while len(self._archive_memo) > ARCHIVE_MEMO_SIZE:
                self._archive_memo.pop(next(iter(self._archive_memo)))
        return document

    def profile_for(self, area_id: str) -> PriceProfile | None:
        """The area's history profile when one is held and recent enough to use, else `None`. No I/O.

        "No profile" covers every reason (never fetched, the relay's 404, an invalid body, too old): the
        planner treats them alike and plans on published prices.
        """
        cached = self._profiles.get(area_id)
        if cached is None:
            return None
        profile: PriceProfile = cached.parsed
        if self._now() - profile.generated >= PROFILE_MAX_AGE:
            return None
        return profile

    async def async_get_profile(self, area_id: str, today: date, *, refresh: bool = False) -> PriceProfile | None:
        """The area's history profile, requested at most once per local day (`today`).

        The relay's 404 is a definite "no profile" (a cached copy is dropped: the relay withdrew it) and, like a
        valid document, is not asked again until the next day. A transport failure or an invalid body keeps
        the last good copy and is retried after [PROFILE_RETRY_AFTER].
        """
        await self.async_restore()
        if not refresh:
            if self._profile_checked.get(area_id) == today:
                return self.profile_for(area_id)
            failed = self._profile_failed_at.get(area_id)
            if failed is not None and self._now() - failed < PROFILE_RETRY_AFTER:
                return self.profile_for(area_id)

        async def load() -> PriceProfile | None:
            return await self._load_profile(area_id, today)

        return await self._shared(("profile", area_id), load)

    def profile_summary(self, area_id: str) -> dict[str, Any]:
        """The profile as diagnostics prints it: identity, window and coverage, never the price rows."""
        cached = self._profiles.get(area_id)
        profile = None if cached is None else cached.parsed
        failed = self._profile_failed_at.get(area_id)
        checked = self._profile_checked.get(area_id)
        return {
            "area": area_id,
            "held": profile is not None,
            "usable": self.profile_for(area_id) is not None,
            "fetched_at": None if cached is None else _stamp(cached.fetched_at),
            "generated": None if profile is None else _stamp(profile.generated),
            "from": None if profile is None else profile.from_date.isoformat(),
            "to": None if profile is None else profile.to_date.isoformat(),
            "weeks": None if profile is None else profile.weeks,
            "hour_entries": None if profile is None else len(profile.hours),
            "checked_day": None if checked is None else checked.isoformat(),
            "last_failure_at": _stamp(failed),
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
            _LOGGER.warning("Restoring the stored relay cache failed; continuing without it")

    @callback
    def _restore_finished(self, task: asyncio.Task[None]) -> None:
        if task.cancelled():
            # Only runtime shutdown gets here; clearing the handle keeps a next process from
            # awaiting a cancelled task.
            if self._restore_task is task:
                self._restore_task = None
            return
        if task.exception() is not None:
            _LOGGER.warning("Restoring the stored relay cache raised")

    async def _restore_and_adopt(self) -> None:
        try:
            raw = await self._store.async_load()
        except Exception as err:  # noqa: BLE001 - any store failure means "no store"
            _LOGGER.warning("Could not read the stored relay cache (%s); continuing without it", type(err).__name__)
            return
        self._adopt(raw)

    def _adopt(self, raw: Any) -> None:
        """Take stored documents in; nothing valid is lost and an older copy never replaces a newer one."""

        if raw is None:
            return
        if not isinstance(raw, dict) or raw.get("schema") != STORAGE_VERSION:
            _LOGGER.warning("Ignoring stored relay cache: unknown schema version")
            return

        # A stored list and index say which version they are; each is re-read by that version's rules,
        # so a v1 cache from an older release restores as it was and is replaced by the next fetch.
        catalogue = self._restore_one(raw.get("catalogue"), "catalogue", _parse_versioned(parse_catalogue))
        if catalogue is not None and _older_than(self._catalogue, catalogue):
            self._catalogue = catalogue
            stored_rev = raw.get("catalogue_rev")
            self._catalogue_rev = stored_rev if isinstance(stored_rev, str) and stored_rev else None
        index = self._restore_one(raw.get("index"), "index", _parse_versioned(parse_index))
        if index is not None and _older_than(self._index, index):
            self._index = index
        if (
            self._catalogue is not None
            and self._index is not None
            and self._index.parsed.version != self._catalogue.parsed.version
        ):
            # The index is only ever read beside a list of its own version.
            self._index = None
        profiles = raw.get("profiles")
        if isinstance(profiles, dict):
            for area_id, entry in profiles.items():
                restored_profile = self._restore_profile(area_id, entry)
                if restored_profile is not None and _older_than(self._profiles.get(area_id), restored_profile):
                    self._profiles[area_id] = restored_profile
        days = raw.get("days")
        if isinstance(days, dict):
            for key, entry in days.items():
                restored = self._restore_day(key, entry)
                if restored is None:
                    continue
                where, cached = restored
                # Adoption is by acquisition time, not arrival order, so a late restore cannot
                # replace something fetched since. Loaders await the restore before fetching, so
                # this is the invariant that keeps it true if a path ever forgets to.
                if _older_than(self._days.get(where), cached):
                    self._days[where] = cached

    def _restore_one(self, entry: Any, what: str, parse) -> _Cached | None:
        if not isinstance(entry, dict):
            return None
        document = entry.get("document")
        fetched_at = _aware(entry.get("fetched_at"))
        if document is None or fetched_at is None:
            _LOGGER.warning("Ignoring stored %s: incomplete entry", what)
            return None
        try:
            parsed = parse(document)
        except RelayParseError as err:
            _LOGGER.warning("Ignoring stored %s: %s", what, err.code)
            return None
        return _Cached(document=document, fetched_at=fetched_at, parsed=parsed, source="store")

    def _restore_profile(self, area_id: Any, entry: Any) -> _Cached | None:
        if not isinstance(area_id, str) or not isinstance(entry, dict):
            return None
        return self._restore_one(
            entry, f"profile {area_id}", lambda document: parse_profile(document, area_id=area_id)
        )

    def _restore_day(self, key: Any, entry: Any) -> tuple[tuple[str, date], _Cached] | None:
        if not isinstance(key, str) or "|" not in key or not isinstance(entry, dict):
            return None
        area_id, _, raw_day = key.partition("|")
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
        return (area_id, day), _Cached(document=document, fetched_at=fetched_at, parsed=parsed, source="store")

    async def _fetch_versioned(self, name: str, parse, versions: tuple[int, ...]) -> tuple[Any, Any]:
        """`/v<n>/<name>` parsed as version n, for the first of [versions] the relay answers readably.

        A 404 or a body this client cannot read moves on to the next version; a transport failure does not
        (an unreachable relay is not an old one). The last version's failure is the one raised.
        """
        for position, version in enumerate(versions):
            last = position == len(versions) - 1
            try:
                document = await self._fetch_json(f"/v{version}/{name}")
                return document, parse(document, version=version)
            except _Failure as err:
                if last or err.status != 404:
                    raise
            except RelayParseError:
                if last:
                    raise
        raise AssertionError("unreachable")  # pragma: no cover

    async def _load_catalogue(self) -> CatalogueSnapshot:
        key = ("catalogue",)
        self._attempts[key] = (self._now(), None)
        try:
            document, parsed = await self._fetch_versioned("areas.json", parse_catalogue, CONTRACT_VERSIONS)
        except (RelayParseError, _Failure) as err:
            return self._after_failure(key, err, self._catalogue_snapshot)
        previous = None if self._catalogue is None else self._catalogue.parsed.version
        self._catalogue = _Cached(document=document, fetched_at=self._now(), parsed=parsed)
        self._catalogue_rev = None
        if previous is not None and previous != parsed.version and self._index is not None:
            # A list of another version: the index beside it must be read again in that version.
            self._index = None
        await self._persist()
        return self._catalogue_snapshot(state="ready", source="network", settled=True)

    async def _load_index(self) -> IndexSnapshot:
        key = ("index",)
        self._attempts[key] = (self._now(), None)
        # Always the version of the list held; with none held yet, newest first like the list.
        held = self._catalogue
        versions = CONTRACT_VERSIONS if held is None else (held.parsed.version,)
        try:
            document, parsed = await self._fetch_versioned("index.json", parse_index, versions)
        except (RelayParseError, _Failure) as err:
            return self._after_failure(key, err, self._index_snapshot)
        self._index = _Cached(document=document, fetched_at=self._now(), parsed=parsed)
        await self._persist()
        return self._index_snapshot(state="ready", source="network", settled=True)

    async def _load_day(self, area_id: str, day: date, *, authority: IndexAuthority) -> DaySnapshot:
        key = ("day", area_id, day.isoformat())
        self._attempts[key] = (self._now(), None)
        path = f"/v1/{quote(area_id, safe='')}/{day.year:04d}/{day.month:02d}-{day.day:02d}.json"
        try:
            document = await self._fetch_json(path)
            parsed = parse_day(document, area_id=area_id, day=day)
        except (RelayParseError, _Failure) as err:
            self._record_failure(key, err)
            return self._day_snapshot(area_id, day, authority=authority, settled=True)
        self._days[(area_id, day)] = _Cached(document=document, fetched_at=self._now(), parsed=parsed)
        await self._persist()
        return self._day_snapshot(area_id, day, authority=authority, source="network", settled=True)

    async def _load_archive_day(self, area_id: str, day: date) -> PriceDocument | None:
        key = ("archive", area_id, day.isoformat())
        self._attempts[key] = (self._now(), None)
        path = f"/v1/{quote(area_id, safe='')}/{day.year:04d}/{day.month:02d}-{day.day:02d}.json"
        try:
            document = await self._fetch_json(path)
            return parse_day(document, area_id=area_id, day=day)
        except (RelayParseError, _Failure) as err:
            self._record_failure(key, err)
            return None

    async def _load_profile(self, area_id: str, today: date) -> PriceProfile | None:
        key = ("profile", area_id)
        self._attempts[key] = (self._now(), None)
        path = f"/v1/{quote(area_id, safe='')}/profile.json"
        try:
            document = await self._fetch_json(path)
            parsed = parse_profile(document, area_id=area_id)
        except _Failure as err:
            if err.status == 404:
                # The relay's definite "no usable profile": nothing held is still true.
                self._profiles.pop(area_id, None)
                self._profile_failed_at.pop(area_id, None)
                self._profile_checked[area_id] = today
                await self._persist()
                return None
            self._record_failure(key, err)
            self._profile_failed_at[area_id] = self._now()
            return self.profile_for(area_id)
        except RelayParseError as err:
            self._record_failure(key, err)
            self._profile_failed_at[area_id] = self._now()
            self._profile_checked[area_id] = today
            return self.profile_for(area_id)
        self._profiles[area_id] = _Cached(document=document, fetched_at=self._now(), parsed=parsed)
        self._profile_failed_at.pop(area_id, None)
        self._profile_checked[area_id] = today
        await self._persist()
        return self.profile_for(area_id)

    def _after_failure(self, key: tuple[str, ...], err: Exception, snapshot):
        self._record_failure(key, err)
        cached = self._catalogue if key == ("catalogue",) else self._index
        if cached is not None:
            # Last good stays what a reader sees; the failure is recorded beside it (`stale`).
            return snapshot(state="stale", settled=True)
        if isinstance(err, _Failure):
            return snapshot(state="unavailable", settled=True)
        return snapshot(state="invalid", settled=True)

    def _record_failure(self, key: tuple[str, ...], err: Exception) -> None:
        code: FailureCode = err.code if isinstance(err, (RelayParseError, _Failure)) else "network"
        self._attempts[key] = (self._now(), code)
        _LOGGER.debug("Relay %s failed: %s", key[0], code)

    async def _authority_for(self, area_id: str, day: date) -> IndexAuthority:
        """What the index currently says about a day, reading it only if there is none.

        Same freshness rule as [index_snapshot]: only a `ready` index is authority. A retained last-good
        index after a failed refresh answers `unknown`, because a possibly hours-old list is not the relay
        saying what exists now; otherwise this would suppress a day the manager (which treats authority as
        `unknown`) is asking for.

        Only an index never asked for is read here, so a first day fetch is not blind; a failed one is not
        re-asked inside a day request (one failed global attempt would become one per day).
        """
        snapshot = self.index_snapshot()
        if snapshot.index is None and snapshot.attempt_at is None:
            snapshot = await self.async_get_index()
        return _authority_of(snapshot, area_id, day)

    def _authority_now(self, area_id: str, day: date) -> IndexAuthority:
        """The same answer without any I/O, by the same freshness rule."""
        return _authority_of(self.index_snapshot(), area_id, day)

    def _index_revision(self) -> str | None:
        return None if self._index is None else self._index.parsed.areas_rev

    def _catalogue_snapshot(
        self, *, state: CatalogueState, source: SnapshotSource | None = None, settled: bool = False
    ) -> CatalogueSnapshot:
        cached = self._catalogue
        attempt_at, error = self._attempt(("catalogue",))
        return CatalogueSnapshot(
            state=state,
            catalogue=None if cached is None else cached.parsed,
            source=None if cached is None else (source or cached.source),
            fetched_at=None if cached is None else cached.fetched_at,
            attempt_at=attempt_at,
            attempt_error=error,
            refreshing=not settled and ("catalogue",) in self._inflight,
        )

    def _index_snapshot(
        self, *, state: CatalogueState, source: SnapshotSource | None = None, settled: bool = False
    ) -> IndexSnapshot:
        cached = self._index
        attempt_at, error = self._attempt(("index",))
        return IndexSnapshot(
            state=state,
            index=None if cached is None else cached.parsed,
            source=None if cached is None else (source or cached.source),
            fetched_at=None if cached is None else cached.fetched_at,
            attempt_at=attempt_at,
            attempt_error=error,
            refreshing=not settled and ("index",) in self._inflight,
        )

    def _day_snapshot(
        self,
        area_id: str,
        day: date,
        *,
        authority: IndexAuthority,
        stale_override: bool = False,
        source: SnapshotSource | None = None,
        settled: bool = False,
    ) -> DaySnapshot:
        key = ("day", area_id, day.isoformat())
        cached = self._days.get((area_id, day))
        attempt_at, error = self._attempt(key)
        refreshing = not settled and key in self._inflight

        if cached is None:
            state: DayState = "loading" if refreshing else (
                "invalid" if error is not None and not is_transport(error) else "unavailable"
            )
        elif stale_override or error is not None:
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
            index_revision=self._index_revision(),
            refreshing=refreshing,
        )

    def _attempt(self, key: tuple[str, ...]) -> tuple[datetime | None, FailureCode | None]:
        return self._attempts.get(key, (None, None))

    def _client(self) -> ClientSession:
        if self._session is None:
            self._session = async_get_clientsession(self._hass)
        return self._session

    async def _fetch_json(self, path: str) -> Any:
        """One published document, bounded in time and size.

        A non-200 is a failure, not a document; the body must be JSON (a proxy's HTML is `not_json`). It
        is read to EOF in bounded slices under one overall timeout and never logged.
        """
        url = f"{self._base_url}{path}"
        try:
            async with asyncio.timeout(REQUEST_TIMEOUT_S):
                async with self._client().get(url) as response:
                    if response.status != 200:
                        raise _Failure("http_status", response.status)
                    body = await _read_bounded(response.content)
        except TimeoutError as err:
            raise _Failure("timeout") from err
        except ClientError as err:
            raise _Failure("network") from err
        return loads_document(body.decode("utf-8", errors="replace"))

    def _payload(self) -> dict[str, Any]:
        """Everything worth restoring, as one document, rebuilt from the caches on every write so the last
        writer writes the union.
        """
        return {
            "schema": STORAGE_VERSION,
            "catalogue": _entry(self._catalogue),
            "catalogue_rev": self._catalogue_rev,
            "index": _entry(self._index),
            "days": {f"{area_id}|{day.isoformat()}": _entry(cached) for (area_id, day), cached in self._days.items()},
            "profiles": {area_id: _entry(cached) for area_id, cached in self._profiles.items()},
        }

    async def _persist(self) -> None:
        """Write the cache, serialized against other writers and never against a request."""
        async with self._write_lock:
            await self._store.async_save(self._payload())

def _authority_of(snapshot: IndexSnapshot, area_id: str, day: date) -> IndexAuthority:
    """The one place a snapshot becomes an authority answer, shared by the reading and fetching paths so
    they cannot describe the same index differently.
    """
    if snapshot.state != "ready" or snapshot.index is None:
        return "unknown"
    area = snapshot.index.area(area_id)
    if area is None:
        return "not_listed"
    return "listed" if area.lists(day) else "not_listed"


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


def _parse_versioned(parse):
    """A restore parser for a stored area list or index: read by the version the document states."""

    def parsed(document: Any) -> Any:
        version = document_version(document)
        if version is None:
            raise RelayParseError("unsupported_version", "the stored document is not a version this client reads")
        return parse(document, version=version)

    return parsed


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


def index_summary(snapshot: IndexSnapshot) -> dict[str, Any]:
    index = snapshot.index
    return {
        "state": snapshot.state,
        "source": snapshot.source,
        "fetched_at": _stamp(snapshot.fetched_at),
        "attempt_at": _stamp(snapshot.attempt_at),
        "error": snapshot.attempt_error,
        "refreshing": snapshot.refreshing,
        "area_count": None if index is None else len(index.areas),
        "version": None if index is None else index.version,
        "areas_rev": None if index is None else index.areas_rev,
        "res_default": None if index is None else index.res_default,
        "generated": None if index is None else _stamp(index.generated),
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
        "index_revision": snapshot.index_revision,
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
    base_url: str = DEFAULT_BASE_URL,
    session: ClientSession | None = None,
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
    if data.price_repository is not None:
        await data.price_repository.async_restore()
        return data.price_repository
    repository = PriceRepository(hass, base_url=base_url, session=session, store=store, now=now)
    data.price_repository = repository
    await repository.async_restore()
    return repository
