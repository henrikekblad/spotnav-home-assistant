"""How much of the charge-ownership core the shadow has seen live, per event kind, kept across restarts.

The shadow (`ownership_shadow.py`) counts from its start and keeps only its last events, so what it saw before a
restart is gone. This tally is cumulative, per charger: for every event kind of the core (`core/events.py`, listed
with zeros until seen) how many events it decided, compared, disagreed on, found drift at or failed on, and when it
first and last saw one; `since` and `version` say when and under which release of the integration the tally began.
Counts that belong to no event (a later comparison of the execution boundary, a failure outside a feed) are
`unattributed`. It says when the core has seen every kind often enough to take over, and changes nothing else.

Kept in a small store of its own (`store_key`), written debounced: at most one write waits at a time, written
`SAVE_DELAY_S` after the first change since the last one, at the controller's shutdown, or at Home Assistant's
final write. Nothing is written before the stored tally is read (`async_load`), which what was counted meanwhile
is added to. Only removing the entry removes it (`async_remove_stored`). Never raises into the shadow.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
import logging
from typing import Any, Final

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from ..const import DOMAIN
from ..core.events import EVENT_TYPES

_LOGGER = logging.getLogger(__name__)

STORE_VERSION: Final = 1
#: How long a change waits for its write (more changes meanwhile are written with it).
SAVE_DELAY_S: Final = 300.0
#: What is counted per event kind (and `unattributed`).
COVERAGE_FIELDS: Final = ("events", "compared", "disagreements", "drift", "errors")


def store_key(entry_id: str) -> str:
    return f"{DOMAIN}.ownership_coverage.{entry_id}"


def _zero() -> dict[str, Any]:
    return dict.fromkeys(COVERAGE_FIELDS, 0)


def _count(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _instant(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    parsed = dt_util.parse_datetime(value)
    return value if parsed is not None and parsed.tzinfo is not None else None


def _earliest(a: str | None, b: str | None) -> str | None:
    if a is None or b is None:
        return a or b
    return a if dt_util.parse_datetime(a) <= dt_util.parse_datetime(b) else b  # type: ignore[operator]


def _latest(a: str | None, b: str | None) -> str | None:
    if a is None or b is None:
        return a or b
    return a if dt_util.parse_datetime(a) >= dt_util.parse_datetime(b) else b  # type: ignore[operator]


class OwnershipCoverage:
    """The tally of one charger. Without a `store` it is kept in memory only (a shadow built outside a controller)."""

    def __init__(
        self,
        *,
        store: Store[dict[str, Any]] | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = store
        self._now = now if now is not None else dt_util.utcnow
        self._since = self._now().isoformat()
        self._version: str | None = None
        self._kinds: dict[str, dict[str, Any]] = {
            kind: {**_zero(), "first_seen": None, "last_seen": None} for kind in EVENT_TYPES
        }
        self._unattributed = _zero()
        # Whether the stored tally was read (no write before it), and whether a write waits.
        self._loaded = False
        self._dirty = False
        self._save_waits = False

    # ------------------------------------------------------------------ counting

    def add(self, kind: str | None, field: str) -> None:
        """One more `field` for `kind` (`None`: belongs to no event kind). Never raises."""
        try:
            counts = self._kinds.get(kind) if kind is not None else None
            if counts is None:
                counts = self._unattributed
            counts[field] += 1
            if counts is not self._unattributed and field == "events":
                seen = self._now().isoformat()
                if counts["first_seen"] is None:
                    counts["first_seen"] = seen
                counts["last_seen"] = seen
            self._dirty = True
            self._schedule()
        except Exception:  # noqa: BLE001 - the tally never breaks the shadow
            _LOGGER.debug("SpotNav ownership coverage could not count %s %s", kind, field, exc_info=True)

    def _schedule(self) -> None:
        if self._store is None or not self._loaded or self._save_waits:
            return
        self._save_waits = True
        self._store.async_delay_save(self._to_save, SAVE_DELAY_S)

    def _to_save(self) -> dict[str, Any]:
        self._save_waits = False
        self._dirty = False
        return self.as_dict()

    # ------------------------------------------------------------------ kept on disk

    async def async_load(self, version: str | None) -> None:
        """Read the stored tally and add what was counted meanwhile to it; a tally begun now is `version`'s."""
        stored: Any = None
        if self._store is not None:
            try:
                stored = await self._store.async_load()
            except Exception:  # noqa: BLE001 - a tally that cannot be read starts again
                _LOGGER.debug("SpotNav ownership coverage could not be read", exc_info=True)
        if isinstance(stored, dict) and _instant(stored.get("since")) is not None:
            self._since = stored["since"]
            raw_version = stored.get("version")
            self._version = raw_version if isinstance(raw_version, str) else None
            self._merge(stored)
        else:
            self._version = version
            self._dirty = True
        self._loaded = True
        if self._dirty:
            self._schedule()

    def _merge(self, stored: dict[str, Any]) -> None:
        kinds = stored.get("kinds")
        for kind, counts in (kinds.items() if isinstance(kinds, dict) else ()):
            mine = self._kinds.get(kind)
            if mine is None or not isinstance(counts, dict):
                # A kind the core no longer has, or a damaged one.
                continue
            for field in COVERAGE_FIELDS:
                mine[field] += _count(counts.get(field))
            mine["first_seen"] = _earliest(_instant(counts.get("first_seen")), mine["first_seen"])
            mine["last_seen"] = _latest(_instant(counts.get("last_seen")), mine["last_seen"])
        unattributed = stored.get("unattributed")
        if isinstance(unattributed, dict):
            for field in COVERAGE_FIELDS:
                self._unattributed[field] += _count(unattributed.get(field))

    async def async_flush(self) -> None:
        """Write a change still waiting (the controller's shutdown). Never raises."""
        if self._store is None or not self._loaded or not self._dirty:
            return
        try:
            await self._store.async_save(self._to_save())
        except Exception:  # noqa: BLE001 - kept for the next write
            self._dirty = True
            _LOGGER.debug("SpotNav ownership coverage could not be written", exc_info=True)

    @staticmethod
    async def async_remove_stored(hass: HomeAssistant, entry_id: str) -> None:
        """The entry is gone for good: forget its tally."""
        await Store(hass, STORE_VERSION, store_key(entry_id)).async_remove()

    # ------------------------------------------------------------------ reading

    def as_dict(self) -> dict[str, Any]:
        """For diagnostics, the debug bundle and the store."""
        return {
            "since": self._since,
            "version": self._version,
            "kinds": {kind: dict(counts) for kind, counts in self._kinds.items()},
            "unattributed": dict(self._unattributed),
        }
