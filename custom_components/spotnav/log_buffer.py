"""An in-memory ring buffer of SpotNav's own log records, for the debug bundle.

A handler on the `custom_components.spotnav` logger tree keeps the last records so a support
question does not depend on the person having raised the log level beforehand. Attaching it changes
no level: a record only reaches a handler when its logger is enabled for it, so DEBUG records exist
only if the person turned DEBUG on. Only SpotNav loggers are ever captured (the handler sits on that
tree, not on the root), and both buffers are bounded.
"""

from __future__ import annotations

import logging
import traceback
from collections import deque
from datetime import UTC, datetime
from typing import Any, Final

#: The logger tree every SpotNav module logs under (`logging.getLogger(__name__)`).
SPOTNAV_LOGGER: Final = "custom_components.spotnav"

#: Records at INFO and above kept for the bundle.
MAX_RECORDS: Final = 300
#: DEBUG records kept apart, so a chatty DEBUG session cannot push the INFO and above ones out.
MAX_DEBUG_RECORDS: Final = 200
#: A traceback longer than this is cut (the end, where the error is, is kept).
MAX_TRACEBACK_CHARS: Final = 3000
MAX_MESSAGE_CHARS: Final = 1000


class SpotNavLogBuffer(logging.Handler):
    """Keeps the newest SpotNav records in two bounded deques, oldest dropped first."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self._records: deque[dict[str, Any]] = deque(maxlen=MAX_RECORDS)
        self._debug: deque[dict[str, Any]] = deque(maxlen=MAX_DEBUG_RECORDS)

    def emit(self, record: logging.LogRecord) -> None:
        # Never raise out of a logging call: a failure to format is a lost line, not a lost charge.
        try:
            entry = _entry(record)
        except Exception:  # noqa: BLE001
            return
        (self._records if record.levelno >= logging.INFO else self._debug).append(entry)

    def records(self) -> list[dict[str, Any]]:
        """The INFO and above records, oldest first."""
        return list(self._records)

    def debug_records(self) -> list[dict[str, Any]]:
        """The DEBUG records, oldest first (empty unless DEBUG is enabled for SpotNav)."""
        return list(self._debug)

    def clear(self) -> None:
        self._records.clear()
        self._debug.clear()


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
