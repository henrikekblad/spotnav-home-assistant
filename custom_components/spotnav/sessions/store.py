"""The installation's record of charge sessions: one HA `Store`, bounded, surviving restarts.

Closed sessions are kept per charger for `RETENTION_DAYS` (two years) and at most `MAX_PER_CHARGER`,
oldest dropped first. A session still open is stored too (written a few seconds after each change), so
a restart can resume it (`recorder.SessionRecorder`). Readers (the sensors, the dashboard block, the
WebSocket command) read memory; nothing here awaits.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any, Final

from homeassistant.core import callback, HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from ..const import DOMAIN
from .model import ChargeSession

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION: Final = 1
STORAGE_KEY: Final = f"{DOMAIN}_sessions"
RETENTION_DAYS: Final = 730
MAX_PER_CHARGER: Final = 5000
#: How long after a change the document is written; a flush on Home Assistant's stop covers the rest.
SAVE_DELAY_S: Final = 15


class SessionStore:
    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass
        self._store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self._closed: dict[str, list[ChargeSession]] = {}
        self._open: dict[str, ChargeSession] = {}
        self._listeners: dict[str, set[Callable[[], None]]] = {}

    async def async_load(self) -> None:
        raw = await self._store.async_load()
        if not isinstance(raw, dict):
            return
        dropped = 0
        sessions = raw.get("sessions")
        if isinstance(sessions, dict):
            for charger_id, items in sessions.items():
                if not isinstance(charger_id, str) or not isinstance(items, list):
                    continue
                kept = [ChargeSession.from_dict(item) for item in items]
                dropped += sum(1 for item in kept if item is None)
                self._closed[charger_id] = [item for item in kept if item is not None and item.end is not None]
        opened = raw.get("open")
        if isinstance(opened, dict):
            for charger_id, item in opened.items():
                session = ChargeSession.from_dict(item)
                if session is None or session.end is not None or session.charger_id != charger_id:
                    dropped += 1
                    continue
                self._open[charger_id] = session
        if dropped:
            # Counted, not quoted: a log line about stored data must not repeat it.
            _LOGGER.warning("Ignored %d unreadable stored charge session record(s)", dropped)
        self._prune(dt_util.utcnow())

    # ---- reads

    def closed(self, charger_id: str) -> tuple[ChargeSession, ...]:
        """The charger's finished sessions, oldest first."""
        return tuple(self._closed.get(charger_id, ()))

    def open_session(self, charger_id: str) -> ChargeSession | None:
        return self._open.get(charger_id)

    # ---- writes

    @callback
    def set_open(self, session: ChargeSession | None, *, charger_id: str) -> None:
        """Record (or, with `None`, forget) the charger's open session."""
        if session is None:
            self._open.pop(charger_id, None)
        else:
            self._open[charger_id] = session
        self._changed(charger_id)

    @callback
    def close(self, session: ChargeSession, now: datetime) -> None:
        """Move a finished session into the record and prune it."""
        self._open.pop(session.charger_id, None)
        self._closed.setdefault(session.charger_id, []).append(session)
        self._prune(now)
        self._changed(session.charger_id)

    async def async_remove_charger(self, charger_id: str) -> None:
        """A charger entry was deleted for good: its sessions go with it."""
        had = charger_id in self._closed or charger_id in self._open
        self._closed.pop(charger_id, None)
        self._open.pop(charger_id, None)
        if had:
            await self._store.async_save(self._document())
        self._notify(charger_id)

    async def async_flush(self) -> None:
        await self._store.async_save(self._document())

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
        return {
            "sessions": {
                charger_id: [item.as_dict() for item in items]
                for charger_id, items in self._closed.items()
                if items
            },
            "open": {charger_id: item.as_dict() for charger_id, item in self._open.items()},
        }

    def _changed(self, charger_id: str) -> None:
        self._store.async_delay_save(self._document, SAVE_DELAY_S)
        self._notify(charger_id)

    def _notify(self, charger_id: str) -> None:
        for listener in list(self._listeners.get(charger_id, ())):
            listener()
