"""How much of the charge-ownership core the runner has seen live, per event kind, kept across restarts.

The runner (`session_runner.py`) counts from its start and keeps only its last events, so what it saw before a
restart is gone. This tally is cumulative, per charger: for every event kind of the core (`core/events.py`, listed
with zeros until seen) how many events it decided, compared, disagreed on, found drift at or failed on, and when it
first and last saw one; `since` and `version` say when and under which release of the integration the tally began.
Counts that belong to no event (a later comparison of the execution boundary, a failure outside a feed) are
`unattributed`. It says when the core has seen every kind often enough to take over, and changes nothing else.

Besides those, per kind: `mirror_drift` (a field the core owns that today's state reads otherwise), `written_back`
(today's owner fields the core's owner overrode), `verdict_differs` (today's rule, on the same state, would have chosen
otherwise where the core's choice acted) and `failures` (a decision that could not be taken). A window's start also
counts what started it (`triggers`: timer, re-arm, plug-in, retry). So the gate before the owner moves to the core
(the core end state plan, step 7: every kind decided live with no disagreement, drift or failure since a release)
reads from the tally alone. It is per release: when the integration's version is not the one the stored tally began
under, a new tally begins, and the one before is kept under `previous` (one period back, never more).

Kept in a small store of its own (`store_key`), written debounced: at most one write waits at a time, written
`SAVE_DELAY_S` after the first change since the last one, at the controller's shutdown, or at Home Assistant's
final write. Nothing is written before the stored tally is read (`async_load`), which what was counted meanwhile
is added to. Only removing the entry removes it (`async_remove_stored`). Never raises into the runner.

Since the previous logic is gone (the core end state plan's deletion step), only `events`, `errors`, `failures` and
`triggers` still move: the comparison's counts (`compared`, `disagreements`, `drift`, `mirror_drift`, `written_back`,
`verdict_differs`) stay in the shape so the stored tally and the release before's under `previous` read as ever.
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
COVERAGE_FIELDS: Final = (
    "events",
    "compared",
    "disagreements",
    "drift",
    "errors",
    "mirror_drift",
    "written_back",
    "verdict_differs",
    "failures",
)
#: The kind whose events are also counted by what started them (`OwnershipCoverage.add_trigger`).
TRIGGERED_KIND: Final = "window_start"


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
    """The tally of one charger. Without a `store` it is kept in memory only (a runner built outside a controller)."""

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
        self._kinds[TRIGGERED_KIND]["triggers"] = {}
        self._unattributed = _zero()
        # The tally of the release before this one's began (`async_load`), as it was stored; `None`: none.
        self._previous: dict[str, Any] | None = None
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
        except Exception:  # noqa: BLE001 - the tally never breaks the runner
            _LOGGER.debug("SpotNav ownership coverage could not count %s %s", kind, field, exc_info=True)

    def add_trigger(self, kind: str, trigger: str) -> None:
        """One more event of `kind` started by `trigger` (only `TRIGGERED_KIND` keeps them). Never raises."""
        try:
            triggers = self._kinds.get(kind, {}).get("triggers")
            if not isinstance(triggers, dict):
                return
            triggers[trigger] = triggers.get(trigger, 0) + 1
            self._dirty = True
            self._schedule()
        except Exception:  # noqa: BLE001 - the tally never breaks the runner
            _LOGGER.debug("SpotNav ownership coverage could not count %s %s", kind, trigger, exc_info=True)

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
        stored_version = stored.get("version") if isinstance(stored, dict) else None
        if (
            isinstance(stored, dict)
            and _instant(stored.get("since")) is not None
            and version is not None
            and stored_version != version
        ):
            # Another release: its tally is kept as the previous period, and this release's begins now (with what was
            # counted meanwhile, which is this release's).
            self._previous = {key: value for key, value in stored.items() if key != "previous"}
            self._version = version
            self._dirty = True
        elif isinstance(stored, dict) and _instant(stored.get("since")) is not None:
            self._since = stored["since"]
            self._version = stored_version if isinstance(stored_version, str) else None
            previous = stored.get("previous")
            self._previous = previous if isinstance(previous, dict) else None
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
            triggers = counts.get("triggers")
            if isinstance(mine.get("triggers"), dict) and isinstance(triggers, dict):
                for trigger, count in triggers.items():
                    if isinstance(trigger, str) and _count(count):
                        mine["triggers"][trigger] = mine["triggers"].get(trigger, 0) + _count(count)
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
            "kinds": {
                kind: {key: dict(value) if isinstance(value, dict) else value for key, value in counts.items()}
                for kind, counts in self._kinds.items()
            },
            "unattributed": dict(self._unattributed),
            "previous": self._previous,
        }
