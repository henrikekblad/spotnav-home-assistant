"""An in-memory ring buffer of SpotNav's own log records, for the debug bundle.

A handler on the `custom_components.spotnav` logger tree keeps the last records so a support
question does not depend on the person having raised the log level beforehand. A record only exists
when its logger is enabled for it, and Home Assistant's default is WARNING, so the SpotNav logger is
made to report INFO whatever level it inherits (`_CapturingLogger`); its records then reach Home
Assistant's own handlers only at the level the person configured (`_Forwarder`), so the HA log looks
as before. A level the person set on the SpotNav logger is kept: DEBUG or INFO set there is used as
is. DEBUG records exist only if the person turned DEBUG on. Only SpotNav loggers are ever captured
(the handlers sit on that tree, not on the root), and every buffer is bounded.

**The design and its trade-off.** Python's logging drops a record before any handler sees it when its logger is not
enabled for the level, so a handler alone cannot keep INFO lines under Home Assistant's default WARNING. Two other
ways were weighed: lowering the SpotNav logger's level to INFO (Home Assistant's log would then show every INFO line,
and the person's own `logger:` setting would be overwritten), or a handler on the root logger (it sees only what is
already enabled). Instead the SpotNav logger's `level` is a property (`_CapturingLogger`) that reports at least INFO
while keeping the level the person set apart; the logger does not propagate, and `_Forwarder` stands in for the
propagation, handing a record to the parent's handlers only at or above the level the person configured
(`_user_level`). Verified with Home Assistant's own `logger` service (`logger.set_level`, `logger.set_default_level`,
which call `setLevel`): DEBUG set there gives DEBUG in both the HA log and the buffer, NOTSET falls back to the
default level for the HA log, and ERROR keeps the HA log at errors while the buffer still keeps INFO and up
(`tests/test_debug_bundle.py`). The cost: every SpotNav INFO record is created and formatted for the buffer whatever
the level, so INFO is for events, never for lines logged at each look of a 10 s loop (those log at DEBUG); and
`logging.getLogger(SPOTNAV_LOGGER).level` reads INFO where the person set a quieter level (`getEffectiveLevel` likewise), which
only code reading the level itself would notice.

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


#: The level the SpotNav logger reports at least, so the buffer receives these records.
CAPTURE_LEVEL: Final = logging.INFO
_USER_LEVEL: Final = "_spotnav_user_level"


class _CapturingLogger(logging.Logger):
    """The SpotNav logger reports at least `CAPTURE_LEVEL`, worked out on each read.

    `level` is what every child logger reads when it works out its own effective level, so it is a
    property: the level the person set (`setLevel`, also Home Assistant's `logger` settings) is kept
    apart and used as is when it is INFO or chattier; otherwise the logger reports INFO, or the
    chattier level it inherits (a DEBUG default level still gives DEBUG records).
    """

    @property  # type: ignore[override]
    def level(self) -> int:
        own = self.__dict__.get(_USER_LEVEL, logging.NOTSET)
        if own != logging.NOTSET and own <= CAPTURE_LEVEL:
            return own
        inherited = own or (self.parent.getEffectiveLevel() if self.parent is not None else logging.WARNING)
        return min(CAPTURE_LEVEL, inherited)

    @level.setter
    def level(self, value: int) -> None:
        self.__dict__[_USER_LEVEL] = value


def _capturing(logger: logging.Logger) -> None:
    """Make `logger` a `_CapturingLogger`, keeping the level it was set to."""
    if isinstance(logger, _CapturingLogger):
        return
    own = logger.__dict__.pop("level", logging.NOTSET)
    # A subclass of the logger's own class, so Home Assistant's `HassLogger.setLevel` keeps working.
    base = type(logger)
    logger.__class__ = type(f"SpotNavCapturing{base.__name__}", (_CapturingLogger, base), {})
    logger.__dict__[_USER_LEVEL] = own
    logger.manager._clear_cache()  # noqa: SLF001 - the cached isEnabledFor answers are stale now


#: The loggers by name, so a record's own logger is not looked up with `logging.getLogger` (under the logging module's
#: lock) for every record: a logger, once made, is the same object for the life of the process.
_LOGGERS: dict[str, logging.Logger] = {}


def _user_level(name: str) -> int:
    """The effective level `name` would have without the capture: what Home Assistant's log shows."""
    logger: logging.Logger | None = _LOGGERS.get(name)
    if logger is None:
        logger = _LOGGERS[name] = logging.getLogger(name)
    while logger is not None:
        if isinstance(logger, _CapturingLogger):
            level = logger.__dict__.get(_USER_LEVEL, logging.NOTSET)
        else:
            level = logger.level
        if level:
            return level
        logger = logger.parent
    return logging.NOTSET


class _Forwarder(logging.Handler):
    """Hands a SpotNav record on to Home Assistant's handlers only at the level the person configured.

    The SpotNav logger does not propagate; this handler stands in for the propagation.
    """

    def __init__(self, logger: logging.Logger) -> None:
        super().__init__(level=logging.NOTSET)
        #: The SpotNav logger; its `parent` is read per record (an attribute, no lookup), as logging sets it again when
        #: a logger between it and the root (`custom_components`) is made later.
        self._logger = logger

    def handle(self, record: logging.LogRecord) -> bool:  # no lock needed: the parent's handlers take their own
        if (parent := self._logger.parent) is not None and record.levelno >= _user_level(record.name):
            parent.callHandlers(record)
        return True

    def emit(self, record: logging.LogRecord) -> None:  # pragma: no cover - `handle` does the work
        return


def attach_log_buffer() -> SpotNavLogBuffer:
    """Attach a fresh buffer to SpotNav's logger tree, replacing any earlier one, and return it.

    Idempotent across a second domain setup in one process (tests, a reload of the whole domain).
    """
    logger = logging.getLogger(SPOTNAV_LOGGER)
    for handler in list(logger.handlers):
        if isinstance(handler, (SpotNavLogBuffer, _Forwarder)):
            logger.removeHandler(handler)
    _capturing(logger)
    logger.addHandler(_Forwarder(logger))
    logger.propagate = False
    buffer = SpotNavLogBuffer()
    logger.addHandler(buffer)
    return buffer

