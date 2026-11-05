"""The installation's record of charge sessions: two HA `Store`s, bounded, surviving restarts.

Closed sessions are kept per charger for `RETENTION_DAYS` (two years) and at most `MAX_PER_CHARGER`,
oldest dropped first. A session still open is stored too (written a few seconds after each change), so
a restart can resume it (`recorder.SessionRecorder`). Readers (the sensors, the dashboard block, the
WebSocket command) read memory; nothing here awaits.

The open sessions change about once a minute while a car charges, so they have a small store of their own
(`OPEN_STORAGE_KEY`). The big one (`STORAGE_KEY`: the finished sessions, the import markers and, as when it was
the only one, the open sessions as they stood) is written only when a session ends, an import ran or a charger
went, and turned into JSON in the executor.

Nothing may be lost between the two. A session that ends is kept in the small store's `closing` list until a write
of the big store that holds it has landed; only then does the small store let it go. On load, a `closing` session
the big store lacks is taken into the finished ones. For each charger the open session is the more recent of the
two stores' (its last sample, else its start): an older version run in between writes only the big one. An open
session is dropped when it is among the charger's finished sessions or began before the newest of them ended; a
crash before the big write landed leaves it open in the small store and among no finished ones, so it resumes.
A record from before the small store existed has none: its open sessions are read from the big one.

What is stored is energy and raw spot prices per interval, never a cost. `closed` and `open_session`
answer sessions with the cost made from the person's *current* fiscal settings (the resolver the
installation sets, `set_fiscal_resolver`); `open_raw` is the recorder's own, stored object. A record that
only has the cost it was written with keeps it (`cost_basis: "stored"`).
"""

from __future__ import annotations

import asyncio
import copy
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, tzinfo
from typing import Any, Final

from homeassistant.const import EVENT_HOMEASSISTANT_FINAL_WRITE
from homeassistant.core import callback, CALLBACK_TYPE, CoreState, Event, HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from ..const import DOMAIN
from ..planning.planner import FiscalChoice
from ..execution.power_energy import configured_register_entity
from .model import ChargeSession
from .summary import month_key, month_summary, previous_month

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION: Final = 1
STORAGE_KEY: Final = f"{DOMAIN}_sessions"
OPEN_STORAGE_VERSION: Final = 1
OPEN_STORAGE_KEY: Final = f"{DOMAIN}_sessions_open"
RETENTION_DAYS: Final = 730
MAX_PER_CHARGER: Final = 5000
#: How long after a change the document is written; a flush on Home Assistant's stop covers the rest.
SAVE_DELAY_S: Final = 15


#: The person's current fiscal choice for a charger and the market a session is priced in (`None`
#: area: the charger's current one); `None` when it cannot be resolved.
FiscalResolver = Callable[[str, "str | None"], "FiscalChoice | None"]


@dataclass(slots=True)
class _PricedCache:
    """One charger's finished sessions as last costed: valid while the store's revision for the charger and the
    fiscal choice of every market they are priced in are what they were then (a closed session never changes)."""

    revision: int
    choices: dict[tuple[str, "str | None"], "FiscalChoice | None"]
    sessions: tuple[ChargeSession, ...]
    #: Month summaries made from `sessions`, by (zone, `YYYY-MM`).
    months: dict[tuple[tzinfo, str], dict[str, Any]] = field(default_factory=dict)


class SessionStore:
    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass
        # Put together on the loop as a snapshot of lists (cheap), turned into rows and JSON in the executor, and
        # written by `_async_write_record` (not a delayed save: the small store must know when it landed).
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, STORAGE_KEY, serialize_in_event_loop=False
        )
        self._open_store: Store[dict[str, Any]] = Store(hass, OPEN_STORAGE_VERSION, OPEN_STORAGE_KEY)
        self._closed: dict[str, list[ChargeSession]] = {}
        self._open: dict[str, ChargeSession] = {}
        #: Per charger, what the one-off import of earlier charges did (`history_import`).
        self._imports: dict[str, dict[str, Any]] = {}
        self._listeners: dict[str, set[Callable[[], None]]] = {}
        self._fiscal: FiscalResolver | None = None
        #: How many times a list of finished sessions changed (any charger's: a prune reaches them all). The costed
        #: sessions are cached against it; an open session's minute-by-minute changes do not move it.
        self._closed_revision = 0
        self._priced_cache: dict[str, _PricedCache] = {}
        #: Per charger, the sessions that ended and are not yet in a big-store write that landed.
        self._closing: dict[str, list[ChargeSession]] = {}
        self._record_lock = asyncio.Lock()
        # A loop timer as `Store`'s own delayed save has (not a tracked Home Assistant timer), and a write at Home
        # Assistant's final write should the stop not have flushed.
        self._record_timer: asyncio.TimerHandle | None = None
        self._record_final: CALLBACK_TYPE | None = None
        #: The big store has changes no write has taken yet.
        self._record_dirty = False

    async def async_load(self) -> None:
        raw = await self._store.async_load()
        own = await self._open_store.async_load()
        raw = raw if isinstance(raw, dict) else {}
        own = own if isinstance(own, dict) else None
        dropped = 0
        sessions = raw.get("sessions")
        if isinstance(sessions, dict):
            for charger_id, items in sessions.items():
                if not isinstance(charger_id, str) or not isinstance(items, list):
                    continue
                kept = [ChargeSession.from_dict(item) for item in items]
                dropped += sum(1 for item in kept if item is None)
                self._closed[charger_id] = [item for item in kept if item is not None and item.end is not None]
        record_behind = small_behind = False
        closing = None if own is None else own.get("closing")
        if isinstance(closing, dict):
            for charger_id, items in closing.items():
                if not isinstance(charger_id, str) or not isinstance(items, list):
                    continue
                for item in items:
                    session = ChargeSession.from_dict(item)
                    if session is None or session.end is None or session.charger_id != charger_id:
                        dropped += 1
                        continue
                    if any(done.id == session.id for done in self._closed.get(charger_id, ())):
                        small_behind = True
                        continue
                    # It ended, and the big write that would have held it did not land: it is finished all the same.
                    self._closed.setdefault(charger_id, []).append(session)
                    self._closing.setdefault(charger_id, []).append(session)
                    record_behind = True
        candidates: dict[str, list[ChargeSession]] = {}
        #: Per charger, the open session the small store holds (what it should hold after this load).
        in_small: dict[str, ChargeSession] = {}
        sources = [(raw.get("open"), False)] + ([(own.get("open"), True)] if own is not None else [])
        for opened, small in sources:
            if not isinstance(opened, dict):
                continue
            for charger_id, item in opened.items():
                session = ChargeSession.from_dict(item)
                if session is None or session.end is not None or session.charger_id != charger_id:
                    dropped += 1
                    continue
                candidates.setdefault(charger_id, []).append(session)
                if small:
                    in_small[charger_id] = session
        for charger_id, found in candidates.items():
            session = max(found, key=_last_seen)
            done = self._closed.get(charger_id, ())
            newest_end = max((item.end for item in done if item.end is not None), default=None)
            if any(item.id == session.id for item in done) or (newest_end is not None and session.start < newest_end):
                # It ended before the restart (or an older version ended it and went on): not open any more.
                small_behind = True
                continue
            if session.register_entity is None and session.last_register_kwh is not None:
                # Read before any charger is set up (and may have its register replaced): the register its
                # last reading came from.
                session.register_entity = configured_register_entity(self._hass, charger_id)
            self._open[charger_id] = session
        if own is not None and any(self._open.get(charger_id) is not in_small.get(charger_id) for charger_id in candidates):
            small_behind = True
        imports = raw.get("imports")
        if isinstance(imports, dict):
            self._imports = {
                key: dict(value) for key, value in imports.items() if isinstance(key, str) and isinstance(value, dict)
            }
        if dropped:
            # Counted, not quoted: a log line about stored data must not repeat it.
            _LOGGER.warning("Ignored %d unreadable stored charge session record(s)", dropped)
        self._prune(dt_util.utcnow())
        self._closed_changed()
        if record_behind:
            self._schedule_record()
        if small_behind or record_behind:
            self._open_store.async_delay_save(self._open_document, SAVE_DELAY_S)

    # ---- reads

    def set_fiscal_resolver(self, resolver: FiscalResolver | None) -> None:
        self._fiscal = resolver

    def _priced(
        self,
        sessions: tuple[ChargeSession, ...],
        choices: dict[tuple[str, str | None], FiscalChoice | None] | None = None,
    ) -> tuple[ChargeSession, ...]:
        """The sessions with their costs made from the current settings (one lookup per market, kept in
        `choices` when given)."""
        if choices is None:
            choices = {}
        out = []
        for item in sessions:
            if not item.intervals:
                out.append(item)
                continue
            key = (item.charger_id, item.area_id)
            if key not in choices:
                choices[key] = None if self._fiscal is None else self._fiscal(*key)
            out.append(item.priced(choices[key]))
        return tuple(out)

    def priced(self, session: ChargeSession) -> ChargeSession:
        """`session` with its cost made from the current settings."""
        return self._priced((session,))[0]

    def closed(self, charger_id: str) -> tuple[ChargeSession, ...]:
        """The charger's finished sessions, oldest first, costed with the current settings.

        Costed once per change: the answer is cached until a list of finished sessions changes or the fiscal choice
        of a market these are priced in does (resolving the choices is cheap; costing every interval is not)."""
        return self._costed(charger_id).sessions

    def month_summary(self, charger_id: str, zone: tzinfo, key: str) -> dict[str, Any]:
        """`summary.month_summary` of the charger's finished sessions for the month `key`, made once per change."""
        cache = self._costed(charger_id)
        summary = cache.months.get((zone, key))
        if summary is None:
            summary = cache.months[(zone, key)] = month_summary(cache.sessions, zone, key)
        return dict(summary)

    def sessions_summary(self, charger_id: str, zone: tzinfo, now: datetime) -> dict[str, Any]:
        """`summary.sessions_summary` of the charger's finished sessions: this month and last month."""
        today = now.astimezone(zone).date()
        return {
            "this_month": self.month_summary(charger_id, zone, month_key(today)),
            "last_month": self.month_summary(charger_id, zone, previous_month(today)),
        }

    def _costed(self, charger_id: str) -> _PricedCache:
        revision = self._closed_revision
        cache = self._priced_cache.get(charger_id)
        if cache is not None and cache.revision == revision and all(
            (None if self._fiscal is None else self._fiscal(*key)) == choice for key, choice in cache.choices.items()
        ):
            return cache
        stored = tuple(self._closed.get(charger_id, ()))
        choices: dict[tuple[str, str | None], FiscalChoice | None] = {}
        sessions = self._priced(stored, choices)
        cache = self._priced_cache[charger_id] = _PricedCache(revision, choices, sessions)
        return cache

    def closed_raw(self, charger_id: str) -> tuple[ChargeSession, ...]:
        """The finished sessions as stored (no cost made)."""
        return tuple(self._closed.get(charger_id, ()))

    def open_session(self, charger_id: str) -> ChargeSession | None:
        """The open session, costed with the current settings (a copy: change nothing through it)."""
        session = self._open.get(charger_id)
        return None if session is None else self._priced((session,))[0]

    def open_raw(self, charger_id: str) -> ChargeSession | None:
        """The open session itself, the recorder's to accrue into."""
        return self._open.get(charger_id)

    def import_marker(self, charger_id: str) -> dict[str, Any] | None:
        """What the import of earlier charges recorded for the charger, or `None` before it ran."""
        marker = self._imports.get(charger_id)
        return None if marker is None else dict(marker)

    def first_live_start(self, charger_id: str) -> datetime | None:
        """When the charger's first session recorded live began (imported sessions do not count)."""
        starts = [item.start for item in self._closed.get(charger_id, ()) if not item.imported]
        opened = self._open.get(charger_id)
        if opened is not None:
            starts.append(opened.start)
        return min(starts) if starts else None

    # ---- writes

    @callback
    def replace_imported(
        self, charger_id: str, sessions: list[ChargeSession], marker: dict[str, Any], now: datetime
    ) -> None:
        """Make `sessions` the charger's imported sessions (dropping the earlier import's) and keep `marker`."""
        kept = [item for item in self._closed.get(charger_id, []) if not item.imported]
        self._closed[charger_id] = kept + list(sessions)
        self._imports[charger_id] = dict(marker)
        self._prune(now)
        self._closed_changed()
        self._changed(charger_id, record=True)

    @callback
    def replace_closed(self, charger_id: str, sessions: list[ChargeSession], now: datetime) -> None:
        """Replace the charger's finished sessions (a migration rewrote some of them)."""
        self._closed[charger_id] = list(sessions)
        self._prune(now)
        self._closed_changed()
        self._changed(charger_id, record=True)

    @callback
    def set_import_marker(self, charger_id: str, marker: dict[str, Any]) -> None:
        self._imports[charger_id] = dict(marker)
        self._changed(charger_id, record=True)

    @callback
    def set_open(self, session: ChargeSession | None, *, charger_id: str) -> None:
        """Record (or, with `None`, forget) the charger's open session."""
        if session is None:
            self._open.pop(charger_id, None)
        else:
            self._open[charger_id] = session
        self._changed(charger_id, opened=True)

    @callback
    def close(self, session: ChargeSession, now: datetime) -> None:
        """Move a finished session into the record and prune it."""
        self._open.pop(session.charger_id, None)
        self._closed.setdefault(session.charger_id, []).append(session)
        # Held in the small store until a big write that holds it has landed (`_async_write_record`).
        self._closing.setdefault(session.charger_id, []).append(session)
        self._prune(now)
        self._closed_changed()
        self._changed(session.charger_id, record=True, opened=True)

    async def async_remove_charger(self, charger_id: str) -> None:
        """A charger entry was deleted for good: its sessions go with it."""
        had = charger_id in self._closed or charger_id in self._open or charger_id in self._imports
        self._imports.pop(charger_id, None)
        self._closed.pop(charger_id, None)
        self._open.pop(charger_id, None)
        self._closing.pop(charger_id, None)
        self._closed_changed()
        if had:
            await self.async_flush()
        self._notify(charger_id)

    async def async_flush(self) -> None:
        """Write both stores now: the big one first, then the small one without what the big one now holds. Home
        Assistant's stop calls it, so the big store is current after a clean stop (an older version reads only it)."""
        self._record_dirty = True
        await self._async_write_record()
        await self._open_store.async_save(self._open_document())

    async def async_remove_stores(self) -> None:
        """The last charger went: the record goes too (both files), with anything still waiting to be written."""
        self._cancel_record_timer()
        self._record_dirty = False
        async with self._record_lock:
            await self._store.async_remove()
            await self._open_store.async_remove()

    # ---- listeners

    def add_listener(self, charger_id: str, listener: Callable[[], None]) -> Callable[[], None]:
        self._listeners.setdefault(charger_id, set()).add(listener)

        def remove() -> None:
            self._listeners.get(charger_id, set()).discard(listener)

        return remove

    # ---- internals

    def _prune(self, now: datetime) -> None:
        cutoff = now - timedelta(days=RETENTION_DAYS)
        for charger_id, items in self._closed.items():
            items.sort(key=lambda item: item.start)
            kept = [item for item in items if item.end is not None and item.end >= cutoff]
            self._closed[charger_id] = kept[-MAX_PER_CHARGER:]

    def _document(self) -> dict[str, Any]:
        """The big store's document (built at once; `_snapshot` defers the rows to the executor)."""
        return _rows(*self._snapshot())

    def _snapshot(self) -> tuple[dict[str, list[ChargeSession]], dict[str, Any], dict[str, Any]]:
        """What the big store's document is made of, taken on the loop and the writer's own: the lists of finished
        sessions copied (a finished session never changes; a migration replaces the object), the open sessions
        already as rows and the import markers copied whole."""
        return (
            {charger_id: list(items) for charger_id, items in self._closed.items() if items},
            self._open_document()["open"],
            copy.deepcopy(self._imports),
        )

    def _open_document(self) -> dict[str, Any]:
        return {
            "open": {charger_id: item.as_dict() for charger_id, item in self._open.items()},
            "closing": {
                charger_id: [item.as_dict() for item in items] for charger_id, items in self._closing.items() if items
            },
        }

    def _closed_changed(self) -> None:
        """A list of finished sessions changed: what was costed from them is stale."""
        self._closed_revision += 1
        self._priced_cache.clear()

    def _changed(self, charger_id: str, *, record: bool = False, opened: bool = False) -> None:
        """Save what changed: the finished sessions or the markers (`record`), an open session (`opened`)."""
        if record:
            self._schedule_record()
        if opened:
            self._open_store.async_delay_save(self._open_document, SAVE_DELAY_S)
        self._notify(charger_id)

    @callback
    def _schedule_record(self) -> None:
        """Write the big store `SAVE_DELAY_S` after the first change not yet written (later ones join it)."""
        self._record_dirty = True
        if self._record_timer is None:
            self._record_timer = self._hass.loop.call_later(SAVE_DELAY_S, self._record_due)
        if self._record_final is None:
            self._record_final = self._hass.bus.async_listen_once(EVENT_HOMEASSISTANT_FINAL_WRITE, self._final_write)

    @callback
    def _record_due(self) -> None:
        self._record_timer = None
        self._hass.async_create_background_task(self._async_write_record(), "spotnav session record write")

    async def _final_write(self, _event: Event) -> None:
        self._record_final = None
        await self._async_write_record()

    @callback
    def _cancel_record_timer(self) -> None:
        if self._record_timer is not None:
            self._record_timer.cancel()
            self._record_timer = None
        if self._record_final is not None:
            self._record_final()
            self._record_final = None

    async def _async_write_record(self) -> None:
        """Write the big store if it has changes, then let the small one drop the ended sessions it now holds."""
        self._cancel_record_timer()
        async with self._record_lock:
            if not self._record_dirty:
                return
            self._record_dirty = False
            snapshot = self._snapshot()
            written = {id(item) for items in self._closing.values() for item in items}
            try:
                document = await self._hass.async_add_executor_job(_rows, *snapshot)
                await self._store.async_save(document)
            except Exception:  # noqa: BLE001 - the small store keeps what ended; the next change tries again
                self._record_dirty = True
                _LOGGER.warning("Could not write the charge session record; it is tried again", exc_info=True)
                return
            landed = False
            for charger_id in list(self._closing):
                kept = [item for item in self._closing[charger_id] if id(item) not in written]
                landed = landed or len(kept) != len(self._closing[charger_id])
                if kept:
                    self._closing[charger_id] = kept
                else:
                    del self._closing[charger_id]
            if landed:
                if self._hass.state is CoreState.running:
                    self._open_store.async_delay_save(self._open_document, SAVE_DELAY_S)
                else:
                    # Stopping: no timer is left behind.
                    await self._open_store.async_save(self._open_document())
        if self._record_dirty and self._hass.state is CoreState.running:
            self._schedule_record()

    def _notify(self, charger_id: str) -> None:
        for listener in list(self._listeners.get(charger_id, ())):
            listener()


def _rows(
    closed: dict[str, list[ChargeSession]], opened: dict[str, Any], imports: dict[str, Any]
) -> dict[str, Any]:
    """The big store's document, in the shape it always had. Pure: the store calls it in the executor."""
    return {
        "sessions": {charger_id: [item.as_dict() for item in items] for charger_id, items in closed.items()},
        "open": opened,
        "imports": imports,
    }


def _last_seen(session: ChargeSession) -> datetime:
    """When an open session was last heard of: its last sample, else its start."""
    return session.last_sample_at or session.start
