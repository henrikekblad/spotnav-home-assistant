"""The debug bundle's day: what the last 24 hours and the last plug-ins did, kept across restarts.

Three field reports in a row lost the moment that mattered: the bundle was downloaded after an update's restart and
a new plug-in, and everything it would have shown lived in memory. What is kept here is for the debug bundle only;
nothing in it is read back by a decision.

* `DayStore`: one small Home Assistant `Store` of its own per keeper, written debounced. At most one write waits at a
  time, `SAVE_DELAY_S` (five minutes) after the first change since the last write; Home Assistant's final write at
  its stop writes one still waiting, and an unload writes it at once (`async_flush`). A minute's sample therefore
  never costs a write of its own. Nothing is written before the stored value was read. Never raises.
* Minute rows (`format_row`, `rows_from_store`): one compact text line a minute, values joined by `;` under a
  `columns` line naming them, so a day of them stays small however the file is indented. Rows older than
  `HISTORY_S` are dropped, also when read back.
* `ChargerDay`, per charger: the last `SESSIONS_KEPT` plug-ins (the current one included), each with when it was
  plugged in and unplugged, the car and how it was identified, the strategy at its start and its changes, and its
  own bounded logs (`SESSION_LOGS`, the oldest dropped first and counted): what solar decided, the phase switches,
  the commands sent to the charger, what a person did and what load balancing decided. A new plug-in opens a new
  entry; nothing is cleared. A charger in no site also keeps its own minute rows (a site keeps them for its members).

Storage is untrusted: a malformed record is dropped, never raised on. Removing the entry removes its store.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime, timedelta
import logging
import math
from typing import Any, Final

from homeassistant.core import callback, HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

STORE_VERSION: Final = 1
#: How long a change waits for its write (later changes meanwhile go with it).
SAVE_DELAY_S: Final = 300.0
#: How far back the minute rows and the kept warnings reach.
HISTORY_S: Final = 24 * 3600
#: Seconds between two minute rows, and the most rows a day holds.
SAMPLE_INTERVAL_S: Final = 60
SAMPLE_COUNT: Final = HISTORY_S // SAMPLE_INTERVAL_S
#: Plug-ins kept per charger, the current one included.
SESSIONS_KEPT: Final = 5
#: Entries kept per log of one plug-in (the oldest dropped first, and counted). Solar's are kept longest: at a
#: decision a minute they are five hours of a sunny day. The commands and load balancing's decisions are as frequent
#: and longer each; the minute rows state the current written and asked for over the whole day, so half do.
SESSION_LOG_LENGTH: Final = 300
SESSION_LOG_LENGTHS: Final = {
    "solar": SESSION_LOG_LENGTH,
    "phase_switch": SESSION_LOG_LENGTH,
    "commands": SESSION_LOG_LENGTH // 2,
    "actions": SESSION_LOG_LENGTH,
    "regulator": SESSION_LOG_LENGTH // 2,
}
#: A plug-in's logs: solar's decisions, phase switches, commands sent, a person's actions, load balancing's decisions.
SESSION_LOGS: Final = tuple(SESSION_LOG_LENGTHS)
#: The charge-ownership events kept as a person's or the app's actions (`ChargerDay.note_ownership`).
ACTION_KINDS: Final = frozenset(
    {"person_start", "person_stop", "direct_start", "direct_stop", "resume", "pause_choice", "strategy_change"}
)
#: The field and row separators of a minute row; a value that holds one has it replaced.
FIELD_SEPARATOR: Final = ";"
#: A stored row longer than this is not one of ours.
MAX_ROW_CHARS: Final = 2000


def store_key(entry_id: str) -> str:
    return f"{DOMAIN}.debug_day.{entry_id}"


#: The installation's kept warnings (`log_buffer.py`), not tied to an entry.
LOG_STORE_KEY: Final = f"{DOMAIN}.debug_day.log"


class DayStore:
    """A debounced `Store` (module docstring). `data` returns what to write; the owner calls `changed` after each
    change and `async_load` once before anything is written."""

    def __init__(self, hass: HomeAssistant, key: str, data: Callable[[], dict[str, Any]]) -> None:
        self._hass = hass
        self._store: Store[dict[str, Any]] = Store(hass, STORE_VERSION, key)
        self._data = data
        self._loaded = False
        self._dirty = False
        self._save_waits = False
        # Closed at an unload: nothing more is scheduled, so a removed entry's store is not written back later.
        self._closed = False

    async def async_load(self) -> Any:
        """The stored value (`None` when there is none or it cannot be read). Writes are allowed from now on."""
        stored: Any = None
        try:
            stored = await self._store.async_load()
        except Exception:  # noqa: BLE001 - a store that cannot be read starts again
            _LOGGER.debug("SpotNav debug day: %s could not be read", self._store.key, exc_info=True)
        self._loaded = True
        if self._dirty:
            self._schedule()
        return stored

    @callback
    def changed(self) -> None:
        """Something to write: one write waits, `SAVE_DELAY_S` from the first change since the last write."""
        self._dirty = True
        self._schedule()

    def _schedule(self) -> None:
        if not self._loaded or self._save_waits or self._closed:
            return
        self._save_waits = True
        try:
            self._store.async_delay_save(self._to_save, SAVE_DELAY_S)
        except Exception:  # noqa: BLE001 - a diagnostic store must never disturb a decision
            # No traceback: this also runs for a logged warning, and must not log one of its own.
            self._save_waits = False
            _LOGGER.debug("SpotNav debug day: could not schedule the write of %s", self._store.key)

    def _to_save(self) -> dict[str, Any]:
        self._save_waits = False
        self._dirty = False
        return self._data()

    async def async_flush(self) -> None:
        """Write a change still waiting (an unload, Home Assistant's stop). Never raises."""
        if not self._loaded or not self._dirty:
            return
        try:
            await self._store.async_save(self._to_save())
        except Exception:  # noqa: BLE001 - kept for the next write
            self._dirty = True
            _LOGGER.debug("SpotNav debug day: %s could not be written", self._store.key, exc_info=True)

    async def async_close(self) -> None:
        """An unload: write what waits, then schedule nothing more."""
        await self.async_flush()
        self._closed = True

    @staticmethod
    async def async_remove(hass: HomeAssistant, key: str) -> None:
        """The entry is gone for good: forget what it kept."""
        await Store(hass, STORE_VERSION, key).async_remove()


# ---------------------------------------------------------------------------------------------- minute rows


def row_time(moment: datetime) -> str:
    """A row's time: UTC to the second (`2026-10-31T12:34:05Z`)."""
    return dt_util.as_utc(moment).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def _value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, float):
        if not math.isfinite(value):
            return ""
        rounded = round(value, 2)
        return str(int(rounded)) if rounded == int(rounded) else repr(rounded)
    if isinstance(value, int):
        return str(value)
    return str(value).replace(FIELD_SEPARATOR, ",").replace("\n", " ")


def format_row(values: Iterable[Any]) -> str:
    """One minute row: the values in their columns' order, `;` between them; `None` is empty, a bool 1 or 0."""
    return FIELD_SEPARATOR.join(_value(value) for value in values)


def parse_row(row: str) -> list[str]:
    """A row's values as text, in their columns' order (for a reader of the bundle and the tests)."""
    return row.split(FIELD_SEPARATOR)


def _row_moment(row: Any) -> datetime | None:
    if not isinstance(row, str) or not row or len(row) > MAX_ROW_CHARS:
        return None
    try:
        moment = datetime.strptime(row.split(FIELD_SEPARATOR, 1)[0], "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return None
    return moment.replace(tzinfo=dt_util.UTC)


def prune_rows(rows: deque[str], now: datetime) -> None:
    """Drop the rows older than `HISTORY_S` from the front of `rows` (oldest first)."""
    oldest = now - timedelta(seconds=HISTORY_S)
    while rows:
        moment = _row_moment(rows[0])
        if moment is not None and moment >= oldest:
            return
        rows.popleft()


def rows_from_store(raw: Any, now: datetime) -> deque[str]:
    """Stored rows, read as untrusted: only well-formed ones of the last `HISTORY_S`, oldest first."""
    oldest = now - timedelta(seconds=HISTORY_S)
    kept: list[tuple[datetime, str]] = []
    for row in raw if isinstance(raw, list) else ():
        moment = _row_moment(row)
        if moment is not None and oldest <= moment <= now + timedelta(seconds=SAMPLE_INTERVAL_S):
            kept.append((moment, row))
    kept.sort(key=lambda item: item[0])
    return deque((row for _moment, row in kept[-SAMPLE_COUNT:]), maxlen=SAMPLE_COUNT)


def history_block(columns: Iterable[str], rows: Iterable[str]) -> dict[str, Any]:
    """A minute table as the bundle states it."""
    return {"interval_s": SAMPLE_INTERVAL_S, "columns": FIELD_SEPARATOR.join(columns), "rows": list(rows)}


# ---------------------------------------------------------------------------------------------- the plug-ins


def _plain(value: Any, depth: int = 0, *, drop_none: bool = True) -> Any:
    """A JSON-safe copy (what is kept must survive the store's JSON). `None` values in a mapping are left out, to
    keep a log's entries short; with `drop_none` false a plug-in's own fields (one level down) keep theirs."""
    if depth > 8:
        return None
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {
            str(key): _plain(item, depth + 1, drop_none=drop_none)
            for key, item in value.items()
            if item is not None or (not drop_none and depth == 1)
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_plain(item, depth + 1, drop_none=drop_none) for item in value]
    return str(value)


def _per_phase(value: Mapping[str, Any]) -> str:
    """A current per phase as `L1/L2/L3` (empty for one not known)."""
    return "/".join(_value(value.get(phase)) for phase in ("L1", "L2", "L3"))


def _instant(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    parsed = dt_util.parse_datetime(value)
    return value if parsed is not None and parsed.tzinfo is not None else None


def _count(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _new_session(now: datetime, plugged_in_at: str | None, strategy: str | None) -> dict[str, Any]:
    return {
        "opened_at": now.isoformat(),
        "plugged_in_at": plugged_in_at,
        "unplugged_at": None,
        "vehicle": None,
        "strategy": strategy,
        "strategy_changes": [],
        **{name: [] for name in SESSION_LOGS},
        "dropped": {},
    }


def session_from_store(raw: Any) -> dict[str, Any] | None:
    """One stored plug-in, read as untrusted: `None` when it is not one; a malformed entry in a log is dropped."""
    if not isinstance(raw, dict) or _instant(raw.get("opened_at")) is None:
        return None
    vehicle = raw.get("vehicle")
    strategy = raw.get("strategy")
    session = _new_session(dt_util.utcnow(), None, strategy if isinstance(strategy, str) else None)
    session.update(
        opened_at=raw["opened_at"],
        plugged_in_at=_instant(raw.get("plugged_in_at")),
        unplugged_at=_instant(raw.get("unplugged_at")),
        vehicle=(
            {key: vehicle.get(key) for key in ("vehicle_id", "method") if isinstance(vehicle.get(key), str)}
            if isinstance(vehicle, dict)
            else None
        ),
    )
    changes = raw.get("strategy_changes")
    session["strategy_changes"] = [
        {"at": item["at"], "strategy": item["strategy"]}
        for item in (changes if isinstance(changes, list) else ())
        if isinstance(item, dict) and _instant(item.get("at")) is not None and isinstance(item.get("strategy"), str)
    ][-SESSION_LOG_LENGTH:]
    dropped = raw.get("dropped")
    for name in SESSION_LOGS:
        items = raw.get(name)
        session[name] = [item for item in (items if isinstance(items, list) else ()) if isinstance(item, dict)][
            -SESSION_LOG_LENGTHS[name]:
        ]
        count = _count(dropped.get(name)) if isinstance(dropped, dict) else 0
        if count:
            session["dropped"][name] = count
    return session


class ChargerDay:
    """One charger's last plug-ins and, while it is in no site, its minute rows (module docstring).

    `strategy` reads the charger's strategy, `vehicle` the car identified at a plug-in at or after the one given
    (`vehicle_id`, `method`, `None` when none was yet);
    `row` gives a minute row's values (without the time) while the charger is in no site, else `None`."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        *,
        strategy: Callable[[], str | None] = lambda: None,
        vehicle: Callable[[str | None], dict[str, Any] | None] = lambda _plugged_in_at: None,
        row: Callable[[], list[Any] | None] = lambda: None,
        now: Callable[[], datetime] = dt_util.utcnow,
    ) -> None:
        self._hass = hass
        self.entry_id = entry_id
        self._strategy = strategy
        self._vehicle = vehicle
        self._row = row
        self._now = now
        self._sessions: list[dict[str, Any]] = []
        self.rows: deque[str] = deque(maxlen=SAMPLE_COUNT)
        self._store = DayStore(hass, store_key(entry_id), self._to_store)
        self._cancel: Callable[[], None] | None = None

    # ------------------------------------------------------------------ kept on disk

    def _to_store(self) -> dict[str, Any]:
        return {"sessions": _plain(self._sessions, drop_none=False), "rows": list(self.rows)}

    async def async_load(self) -> None:
        """Read what was kept before the restart; what was noted meanwhile goes after it."""
        stored = await self._store.async_load()
        if not isinstance(stored, dict):
            return
        now = self._now()
        try:
            sessions = [s for s in map(session_from_store, stored.get("sessions") or []) if s is not None]
            rows = rows_from_store(stored.get("rows"), now)
        except Exception:  # noqa: BLE001 - what was kept is a diagnostic: a setup never fails on it
            _LOGGER.debug("SpotNav debug day: what %s kept could not be read", self.entry_id, exc_info=True)
            return
        noted, self._sessions = self._sessions, (sessions + self._sessions)[-SESSIONS_KEPT:]
        rows.extend(self.rows)
        self.rows = rows
        if noted:
            self._store.changed()

    def start(self) -> None:
        """Take a minute row every `SAMPLE_INTERVAL_S` (only while the charger is in no site)."""
        from homeassistant.helpers.event import async_track_time_interval

        self._cancel = async_track_time_interval(
            self._hass, self._async_minute, timedelta(seconds=SAMPLE_INTERVAL_S)
        )

    async def async_shutdown(self) -> None:
        if self._cancel is not None:
            self._cancel()
            self._cancel = None
        await self._store.async_close()

    async def async_flush(self) -> None:
        await self._store.async_flush()

    @staticmethod
    async def async_remove_stored(hass: HomeAssistant, entry_id: str) -> None:
        await DayStore.async_remove(hass, store_key(entry_id))

    # ------------------------------------------------------------------ every minute

    @callback
    def _async_minute(self, _now: datetime | None = None) -> None:
        self.minute()

    def minute(self) -> None:
        """The plug-in's car and strategy as they are now, and the charger's minute row when it is in no site."""
        try:
            changed = self._follow_session()
            values = self._row()
            if values is not None:
                now = self._now()
                self.rows.append(format_row([row_time(now), *values]))
                prune_rows(self.rows, now)
                changed = True
            if changed:
                self._store.changed()
        except Exception:  # noqa: BLE001 - a diagnostic must never disturb the charger
            _LOGGER.debug("SpotNav debug day: the minute of %s failed", self.entry_id, exc_info=True)

    def _follow_session(self) -> bool:
        session = self._open_session()
        if session is None:
            return False
        changed = False
        strategy = self._strategy()
        if strategy is not None and strategy != self._session_strategy(session):
            changed |= self._strategy_changed(session, strategy)
        vehicle = self._vehicle(session["plugged_in_at"])
        if session["unplugged_at"] is None and isinstance(vehicle, dict) and vehicle != session["vehicle"]:
            session["vehicle"] = dict(vehicle)
            changed = True
        return changed

    @staticmethod
    def _session_strategy(session: dict[str, Any]) -> str | None:
        changes = session["strategy_changes"]
        return changes[-1]["strategy"] if changes else session["strategy"]

    def _strategy_changed(self, session: dict[str, Any], strategy: str) -> bool:
        if session["strategy"] is None and not session["strategy_changes"]:
            session["strategy"] = strategy
            return True
        if strategy == self._session_strategy(session):
            return False
        session["strategy_changes"].append({"at": self._now().isoformat(), "strategy": strategy})
        del session["strategy_changes"][:-SESSION_LOG_LENGTH]
        return True

    # ------------------------------------------------------------------ the plug-ins

    def _open_session(self) -> dict[str, Any] | None:
        return self._sessions[-1] if self._sessions else None

    def _open(self, plugged_in_at: str | None) -> dict[str, Any]:
        session = _new_session(self._now(), plugged_in_at, self._safe(self._strategy))
        self._sessions.append(session)
        del self._sessions[:-SESSIONS_KEPT]
        return session

    @staticmethod
    def _safe(reader: Callable[[], Any]) -> Any:
        try:
            return reader()
        except Exception:  # noqa: BLE001
            return None

    def ensure_session(self, plugged_in_at: datetime | None) -> None:
        """At setup: a car plugged in with no plug-in kept (the first start of this release) opens one."""
        if plugged_in_at is not None and not self._sessions:
            self._open(plugged_in_at.isoformat())
            self._store.changed()

    def _current(self) -> dict[str, Any]:
        """The plug-in an entry belongs to: the last one (after its unplug, what happened until the next plug-in),
        or one opened now when none is kept."""
        session = self._open_session()
        return session if session is not None else self._open(None)

    def _note(self, log: str, entry: Mapping[str, Any]) -> None:
        try:
            session = self._current()
            items = session[log]
            items.append(entry)
            if len(items) > SESSION_LOG_LENGTHS[log]:
                over = len(items) - SESSION_LOG_LENGTHS[log]
                del items[:over]
                session["dropped"][log] = session["dropped"].get(log, 0) + over
            self._store.changed()
        except Exception:  # noqa: BLE001 - a diagnostic must never disturb a decision
            _LOGGER.debug("SpotNav debug day: could not note a %s entry", log, exc_info=True)

    def note_solar(self, entry: Mapping[str, Any]) -> None:
        """One entry of solar's decision log (`SolarExecutionCoordinator._record_decision`), without what the plug-in
        states once: the strategy (its `strategy` and `strategy_changes`), the state before (the entry before's `to`)
        and a plan hold that is not."""
        kept = {key: value for key, value in entry.items() if key not in ("strategy", "from")}
        if kept.get("held_by_plan") is False:
            del kept["held_by_plan"]
        self._note("solar", _plain(kept))

    def note_regulator(self, entry: Mapping[str, Any]) -> None:
        """One of load balancing's decisions for this charger (`SiteCapacityController._record_decision`), without the
        charger's id, and each current per phase as one text, `L1/L2/L3` in amps."""
        kept = {
            key: _per_phase(value) if isinstance(value, Mapping) else value
            for key, value in entry.items()
            if key != "charger_id"
        }
        self._note("regulator", _plain(kept))

    def note_command(self, record: dict[str, Any]) -> None:
        """One command sent through the adapter (`ChargerAdapter._logged`). Kept as the same record, so the status
        read after it, which lands later, is kept too."""
        self._note("commands", record)

    def note_phase_switch(self, what: str, *, phases: int | None = None, read_back: int | None = None) -> None:
        """A phase switch `sent`, `not_sent`, `confirmed`, `given_up` or `reconciled` (`ChargingController`)."""
        self._note(
            "phase_switch",
            _plain({"at": self._now().isoformat(), "what": what, "phases": phases, "read_back": read_back}),
        )

    def note_ownership(self, record: Mapping[str, Any]) -> None:
        """One event the charge-ownership core decided (`SessionRunner._decide`): a plug-in opens a new entry, an
        unplug closes the current one, and a person's or the app's action is kept in its `actions`."""
        try:
            event = record.get("event")
            if not isinstance(event, Mapping):
                return
            kind = event.get("kind")
            at = record.get("at") if isinstance(record.get("at"), str) else self._now().isoformat()
            if kind == "plug_in":
                self._plugged_in(at, event.get("previous"))
            elif kind == "unplug":
                session = self._open_session()
                if session is not None and session["unplugged_at"] is None:
                    session["unplugged_at"] = at
                    self._store.changed()
            elif kind in ACTION_KINDS:
                self._note(
                    "actions",
                    _plain(
                        {
                            "at": at,
                            "event": {key: value for key, value in event.items()},
                            "core": record.get("core") or None,
                            "today": record.get("today") or None,
                        }
                    ),
                )
                if kind == "strategy_change" and isinstance(event.get("strategy"), str):
                    self._strategy_changed(self._current(), event["strategy"])
        except Exception:  # noqa: BLE001 - a diagnostic must never disturb a decision
            _LOGGER.debug("SpotNav debug day: could not note an ownership event", exc_info=True)

    def _plugged_in(self, at: str, previous: Any) -> None:
        session = self._open_session()
        if previous is None and session is not None and session["unplugged_at"] is None:
            # The first connection known after a restart, and the plug-in kept is still open: the same plug-in.
            return
        self._open(at)
        self._store.changed()

    # ------------------------------------------------------------------ reading

    def sessions(self) -> list[dict[str, Any]]:
        """The kept plug-ins, oldest first, as plain JSON values."""
        return _plain(self._sessions, drop_none=False)
