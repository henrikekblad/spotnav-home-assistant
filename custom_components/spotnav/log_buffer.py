"""An in-memory ring buffer of SpotNav's own log records, for the debug bundle.

A handler on the `custom_components.spotnav` logger tree keeps the last records so a support
question does not depend on the person having raised the log level beforehand. Attaching it changes
no level: a record only reaches a handler when its logger is enabled for it, so DEBUG records exist
only if the person turned DEBUG on. Only SpotNav loggers are ever captured (the handler sits on that
tree, not on the root), and every buffer is bounded.

WARNING and above are also kept apart for `WARNINGS_S` (24 hours, at most `MAX_WARNINGS`), and across restarts in a
small store of their own (`debug_day.DayStore`, written at most every five minutes and at Home Assistant's stop): the
warning that explains a field report is often from before the update's restart that came before the download.
"""

from __future__ import annotations

import logging
import threading
import traceback
from collections import deque
from datetime import UTC, datetime, timedelta
from typing import Any, Final, TYPE_CHECKING

from homeassistant.util import dt as dt_util

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .debug_day import DayStore

#: The logger tree every SpotNav module logs under (`logging.getLogger(__name__)`).
SPOTNAV_LOGGER: Final = "custom_components.spotnav"

#: Records at INFO and above kept for the bundle.
MAX_RECORDS: Final = 300
#: DEBUG records kept apart, so a chatty DEBUG session cannot push the INFO and above ones out.
MAX_DEBUG_RECORDS: Final = 200
#: WARNING and above kept apart, for this long (seconds) and at most this many, across restarts.
WARNINGS_S: Final = 24 * 3600
MAX_WARNINGS: Final = 500
#: A traceback longer than this is cut (the end, where the error is, is kept).
MAX_TRACEBACK_CHARS: Final = 3000
MAX_MESSAGE_CHARS: Final = 1000


class SpotNavLogBuffer(logging.Handler):
    """Keeps the newest SpotNav records in two bounded deques, oldest dropped first."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self._records: deque[dict[str, Any]] = deque(maxlen=MAX_RECORDS)
        self._debug: deque[dict[str, Any]] = deque(maxlen=MAX_DEBUG_RECORDS)
        self._warnings: deque[dict[str, Any]] = deque(maxlen=MAX_WARNINGS)
        self._hass: HomeAssistant | None = None
        self._store: DayStore | None = None

    def emit(self, record: logging.LogRecord) -> None:
        # Never raise out of a logging call: a failure to format is a lost line, not a lost charge.
        try:
            entry = _entry(record)
        except Exception:  # noqa: BLE001
            return
        (self._records if record.levelno >= logging.INFO else self._debug).append(entry)
        if record.levelno >= logging.WARNING:
            self._warnings.append(entry)
            self._warning_kept()

    def _warning_kept(self) -> None:
        """Schedule the store's write, on the event loop whichever thread logged."""
        hass, store = self._hass, self._store
        if hass is None or store is None or hass.loop.is_closed():
            return
        try:
            if threading.get_ident() == hass.loop_thread_id:
                store.changed()
            else:
                hass.loop.call_soon_threadsafe(store.changed)
        except Exception:  # noqa: BLE001 - a closed loop at the very end: the line is kept in memory only
            return

    async def async_attach_store(self, hass: HomeAssistant) -> None:
        """Keep the warnings across restarts: read the stored ones (the ones logged since go after them)."""
        from .debug_day import DayStore, LOG_STORE_KEY

        store = DayStore(hass, LOG_STORE_KEY, lambda: {"warnings": self.warnings()})
        stored = await store.async_load()
        try:
            kept = _warnings_from_store(stored.get("warnings") if isinstance(stored, dict) else None)
        except Exception:  # noqa: BLE001 - kept warnings are a diagnostic: SpotNav's setup never fails on them
            kept = []
        self._warnings = deque([*kept, *self._warnings], maxlen=MAX_WARNINGS)
        self._hass, self._store = hass, store
        if self._warnings:
            store.changed()

    async def async_flush(self) -> None:
        if self._store is not None:
            await self._store.async_flush()

    def warnings(self) -> list[dict[str, Any]]:
        """The WARNING and above records of the last `WARNINGS_S`, oldest first, also from before a restart."""
        oldest = dt_util.utcnow() - timedelta(seconds=WARNINGS_S)
        return [entry for entry in self._warnings if (_moment(entry) or oldest) >= oldest]

    def records(self) -> list[dict[str, Any]]:
        """The INFO and above records, oldest first."""
        return list(self._records)

    def debug_records(self) -> list[dict[str, Any]]:
        """The DEBUG records, oldest first (empty unless DEBUG is enabled for SpotNav)."""
        return list(self._debug)

    def clear(self) -> None:
        self._records.clear()
        self._debug.clear()
        self._warnings.clear()


def _moment(entry: dict[str, Any]) -> datetime | None:
    value = entry.get("time")
    moment = dt_util.parse_datetime(value) if isinstance(value, str) else None
    return moment if moment is not None and moment.tzinfo is not None else None


def _warnings_from_store(raw: Any) -> list[dict[str, Any]]:
    """Stored warnings, read as untrusted: only well-formed ones of the last `WARNINGS_S`, oldest first."""
    oldest = dt_util.utcnow() - timedelta(seconds=WARNINGS_S)
    kept: list[dict[str, Any]] = []
    for item in raw if isinstance(raw, list) else ():
        if not isinstance(item, dict) or not all(isinstance(item.get(key), str) for key in ("level", "logger", "message")):
            continue
        moment = _moment(item)
        if moment is None or moment < oldest:
            continue
        entry = {
            "time": item["time"],
            "level": item["level"][:20],
            "logger": item["logger"][:200],
            "message": item["message"][:MAX_MESSAGE_CHARS],
        }
        if isinstance(item.get("exception"), str):
            entry["exception"] = item["exception"][-MAX_TRACEBACK_CHARS:]
        kept.append(entry)
    return kept[-MAX_WARNINGS:]


def _entry(record: logging.LogRecord) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "time": datetime.fromtimestamp(record.created, UTC).isoformat(),
        "level": record.levelname,
        "logger": record.name,
        "message": record.getMessage()[:MAX_MESSAGE_CHARS],
    }
    if record.exc_info:
        text = "".join(traceback.format_exception(*record.exc_info))
        entry["exception"] = text[-MAX_TRACEBACK_CHARS:]
    return entry


def attach_log_buffer() -> SpotNavLogBuffer:
    """Attach a fresh buffer to SpotNav's logger tree, replacing any earlier one, and return it.

    Idempotent across a second domain setup in one process (tests, a reload of the whole domain).
    """
    logger = logging.getLogger(SPOTNAV_LOGGER)
    for handler in list(logger.handlers):
        if isinstance(handler, SpotNavLogBuffer):
            logger.removeHandler(handler)
    buffer = SpotNavLogBuffer()
    logger.addHandler(buffer)
    return buffer


def detach_log_buffer(buffer: SpotNavLogBuffer | None) -> None:
    if buffer is not None:
        logging.getLogger(SPOTNAV_LOGGER).removeHandler(buffer)
