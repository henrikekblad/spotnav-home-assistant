"""The house's per-phase peaks of the days before SpotNav kept any, read back from the recorder (SpotNav 3.0).

`site/phase_peaks.py` needs days of samples before the battery's grid charge cap may use what it learned. A site
that has had its sensors in Home Assistant for a while already has those days, in the recorder's raw state history
(ten days by default). This module reads them back once after the start (and once a day while the learning is not
done) and fills the hours the site's own store has no mark for.

**One reading.** Nothing here decides whether a sample counts. Each sensor's states are held as they stood at each
`SAMPLE_S` of the past (`StatesThen`), the site is asked to read itself on those states (`SiteAt`: the site's own
methods, run unchanged, with only the states they read replaced) and the result goes through the same
`phase_peaks.read_site` and the same `PhasePeaks.add` as a live sample: the site's own measurement with its rules of
freshness, the chargers' step rule, the battery's band, the plant's mismatch rule, the minute's `MIN_SAMPLES`.

**What the history cannot say, and the side taken.** The recorder keeps when a state last *changed*; when it was
last *reported* unchanged is not kept. A state is therefore held as reported when it changed and no later:

* a meter phase, the battery's power or the plant's per-phase power that stood still for longer than the site's
  maximum age reads as stale, exactly as a sensor that fell silent would live: the sample does not count (or, for
  the plant, counts by the battery's band at most). Minutes are lost, none is counted on a reading that may have
  been dead. A change-only integration is not taken as alive on its word either (`SiteAt._change_only_alive`).
* a charger's current that stood still for longer than that while reading `DRAWING_A` or more makes the sample not
  count (`phase_peaks._charger_draw`, `from_history`).
* a state held from before the first hour read (its age is not known) is taken as old.
* a sensor the recorder keeps no history of (excluded from it, or new) is a sensor Home Assistant does not know:
  where the site's measurement needs it (a phase's voltage or reactive power on a site that reckons its current
  from them) no sample counts at all, and nothing is put in its place. The run's one line in the log names such
  sensors, since including them in the recorder is what the person can do about it.

Only raw states are used. The long-term statistics (hourly or 5-minute means and maxima, each sensor's own) are
not: the largest value of one sensor and the largest of another are not a current the house drew at one moment.

**Bounded.** The recorder is read in its own executor (`get_instance(hass).async_add_executor_job`), `CHUNK_HOURS`
at a time, never in the event loop; the samples are replayed in the event loop in slices of `SLICE_S` with a pause
between them. A run stops at `MAX_ROWS` rows or `MAX_RUN_S` seconds, keeping what it has, and is cancelled at the
site's unload. Hours the store already has are not read again. One line in the log says what a run gave.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from functools import cached_property
import logging
import time
from types import FunctionType, MethodType
from typing import TYPE_CHECKING, Any, Final

from homeassistant.util import dt as dt_util

from .phase_peaks import SAMPLE_S, WINDOW_DAYS, PhasePeaks, entities, read_site

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .site_capacity_controller import SiteCapacityController

_LOGGER = logging.getLogger(__name__)

#: How long after the site's start the first run waits (s): the battery's integration and its facts (which name the
#: plant's per-phase entities) are there by then. And how long until the next while the learning is not done (s).
START_DELAY_S: Final = 180.0
RETRY_S: Final = 86400.0
#: The clock hours read from the recorder at a time.
CHUNK_HOURS: Final = 2
#: A run reads at most this many state rows and lasts at most this long (s); what it has by then is kept.
MAX_ROWS: Final = 5_000_000
MAX_RUN_S: Final = 1800.0
#: The replay works for this long in the event loop (s), then lets it run for `PAUSE_S`.
SLICE_S: Final = 0.005
PAUSE_S: Final = 0.02
#: Each read starts this long before its hours (s): the recorder gives the rows after its start and before its end,
#: so a row on the very boundary would be in neither read. A row read twice changes nothing.
OVERLAP_S: Final = 1.0
#: A state held from before the first hour read is taken as changed this long before it (s): old by any limit.
UNKNOWN_AGE_S: Final = 1e7

_HOUR_S: Final = 3600
_MISSING: Final = object()


class _Held:
    """One state as Home Assistant held it at a past moment: its timestamps moved so that its age, taken against the
    clock now, is the age it had then. Reported when it changed and no later (see the module)."""

    __slots__ = ("attributes", "entity_id", "last_changed", "last_reported", "last_updated", "state")

    def __init__(self, entity_id: str, state: Any, attributes: Any, at: datetime) -> None:
        self.entity_id = entity_id
        self.state = state
        self.attributes = attributes
        self.last_changed = self.last_updated = self.last_reported = at


class StatesThen:
    """The states of the entities read, as they stood at a past moment (`at`); `get` as `hass.states.get`. An entity
    with no state by then reads `None`, as one Home Assistant does not know."""

    def __init__(self) -> None:
        #: Per entity its rows `(changed at, state)`, oldest first, and the row in force at the moment looked at.
        self._rows: dict[str, list[tuple[float, Any]]] = {}
        self._index: dict[str, int] = {}
        self._then = 0.0
        self._wall = datetime.fromtimestamp(0, UTC)
        #: The entities that had a row at all so far.
        self.seen: set[str] = set()

    @property
    def empty(self) -> bool:
        return not any(self._rows.values())

    @staticmethod
    def rows(
        history: Mapping[str, Sequence[Any]], start: float, *, fresh_start: bool
    ) -> dict[str, list[tuple[float, Any]]]:
        """The recorder's states (`history`: entity -> states) as rows `(changed at, state)`, oldest first, for
        `load`. Touches every row, so it runs where the recorder was read (its executor), not in the event loop.
        With `fresh_start` (the hours before `start` were not read) a row from before `start`, the recorder's state
        in force at it, is given its age unknown (`UNKNOWN_AGE_S`)."""
        rows: dict[str, list[tuple[float, Any]]] = {}
        for entity_id, states in history.items():
            own: list[tuple[float, Any]] = []
            for state in states:
                changed = getattr(state, "last_updated", None)
                if not isinstance(changed, datetime):
                    continue
                at = changed.timestamp()
                own.append((start - UNKNOWN_AGE_S, state) if at < start and fresh_start else (at, state))
            own.sort(key=lambda row: row[0])
            rows[entity_id] = own
        return rows

    def load(self, history: Mapping[str, Sequence[Any]], start: float, *, fresh_start: bool) -> int:
        """`load_rows` of the recorder's states as they are (`rows`); for a history already at hand."""
        return self.load_rows(self.rows(history, start, fresh_start=fresh_start), fresh_start=fresh_start)

    def load_rows(self, rows: dict[str, list[tuple[float, Any]]], *, fresh_start: bool) -> int:
        """The next hours' rows (`rows`); how many. The state each entity was left in stays in force until its next
        row (a row read again, from the overlap of two reads, changes nothing: the newest row by then counts). With
        `fresh_start` nothing is carried over."""
        count = sum(len(own) for own in rows.values())
        self.seen.update(entity_id for entity_id, own in rows.items() if own)
        if not fresh_start:
            for entity_id, kept in self._rows.items():
                if kept:
                    rows[entity_id] = [kept[-1], *rows.get(entity_id, ())]
        self._rows = rows
        self._index = dict.fromkeys(rows, 0)
        return count

    def at(self, then: float, wall: datetime) -> None:
        """Look at the moment `then` (epoch seconds, never earlier than the last one looked at since `load`), with
        the ages taken against `wall` (the clock now)."""
        self._then = then
        self._wall = wall

    def get(self, entity_id: str) -> _Held | None:
        rows = self._rows.get(entity_id)
        if not rows:
            return None
        then = self._then
        index = self._index[entity_id]
        last = len(rows) - 1
        while index < last and rows[index + 1][0] <= then:
            index += 1
        self._index[entity_id] = index
        changed, state = rows[index]
        if changed > then:
            return None
        return _Held(entity_id, state.state, state.attributes, self._wall - timedelta(seconds=then - changed))


class _StatesRead:
    """The live states, noting which entities are asked for."""

    def __init__(self, states: Any) -> None:
        self._states = states
        self.entity_ids: set[str] = set()

    def get(self, entity_id: str) -> Any:
        if isinstance(entity_id, str):
            self.entity_ids.add(entity_id)
        return self._states.get(entity_id)


class _HassAt:
    """Home Assistant with other states: everything else is its own."""

    def __init__(self, hass: Any, states: Any) -> None:
        self._hass = hass
        self.states = states

    def __getattr__(self, name: str) -> Any:
        return getattr(self._hass, name)


def _class_attribute(cls: type, name: str) -> Any:
    for base in cls.__mro__:
        if name in base.__dict__:
            return base.__dict__[name]
    return _MISSING


class SiteAt:
    """A site reading other states than Home Assistant's now: the recorder's of a past moment (`StatesThen`).

    Every method and property of the site's own class runs unchanged but bound to this object, whose `hass` holds
    those states, so whatever the site reads (its measurement, each charger's current, the battery's power) it reads
    there, by its own rules. Everything else (its configuration, the battery's facts, what it learned about its
    meter) is the site's own, now. Anything a method sets lands here, never on the site. Three things differ, each
    because the history cannot say otherwise (see the module):

    * `result` is the site's measurement on these states (`measurement_result`), not its last live one;
    * `from_history` tells the reading that a state's report time is not known (`phase_peaks._charger_draw`);
    * no integration is taken as alive on its word (`_change_only_alive`)."""

    from_history = True

    def __init__(self, site: Any, states: Any) -> None:
        self._site = site
        self._bound: dict[str, Any] = {}
        self.hass = _HassAt(site.hass, states)

    @property
    def result(self) -> Any:
        return self.measurement_result()

    def _change_only_alive(self, entity_id: str) -> bool:
        return False

    def __getattr__(self, name: str) -> Any:
        if name in ("_site", "_bound"):  # not set yet: never look further (it would recurse)
            raise AttributeError(name)
        bound = self._bound.get(name, _MISSING)
        if bound is not _MISSING:
            return bound
        site = self._site
        attribute = _class_attribute(type(site), name)
        if isinstance(attribute, FunctionType):
            bound = self._bound[name] = MethodType(attribute, self)
            return bound
        if isinstance(attribute, property) and attribute.fget is not None:
            return attribute.fget(self)
        if isinstance(attribute, cached_property):
            return attribute.func(self)
        return getattr(site, name)


def entities_read(site: SiteCapacityController, now: float) -> list[str]:
    """The entities one reading of the site happens to read now (`phase_peaks.read_site`). Not what the history is
    fetched by (a reading stops at the first entity that is not there, and what is there changes from moment to
    moment: `phase_peaks.entities` names them all by the configuration); for the tests, which check that list
    against this."""
    states = _StatesRead(site.hass.states)
    read_site(SiteAt(site, states), now)  # type: ignore[arg-type]
    return sorted(states.entity_ids)


class Replay:
    """The site's readings of the past, sample by sample, into marks of their own (`peaks`)."""

    def __init__(self, site: SiteCapacityController) -> None:
        self.states = StatesThen()
        self.site = SiteAt(site, self.states)
        self.peaks = PhasePeaks()
        self.samples = 0

    def sample(self, then: float) -> None:
        """One sample as the site read at `then`: the same reading and the same counting as a live one."""
        wall = dt_util.utcnow()
        self.states.at(then, wall)
        self.peaks.add(then, read_site(self.site, wall.timestamp()))  # type: ignore[arg-type]
        self.samples += 1

    def run(self, times: Iterable[float]) -> None:
        for then in times:
            self.sample(then)


def sample_times(start: float, end: float) -> range:
    """The moments sampled in `[start, end)`: every `SAMPLE_S`, on the clock's own multiples of it."""
    step = int(SAMPLE_S)
    first = -(-int(start) // step) * step
    return range(first, int(end), step)


class Backfill:
    """One site's reading back of its recorder history into its marks (see the module)."""

    def __init__(self, hass: HomeAssistant, peaks: PhasePeaks, changed: Callable[[], None]) -> None:
        self._hass = hass
        self._peaks = peaks
        self._changed = changed
        #: The timer of the next run, and the run going on (a background task: nothing waits for it).
        self._cancel: Callable[[], None] | None = None
        self._task: asyncio.Task[None] | None = None
        self._stopped = False
        #: What the last run gave, for the diagnostics; `None` before the first.
        self.last: dict[str, Any] | None = None
        self.runs = 0

    def start(self, site: SiteCapacityController) -> None:
        """Run in the background from now on: after `START_DELAY_S`, then every `RETRY_S` until the learning is
        done. Stopped by `async_stop`."""
        if self._cancel is None and self._task is None and not self._stopped:
            self._schedule(site, START_DELAY_S)

    def _schedule(self, site: SiteCapacityController, delay_s: float) -> None:
        from homeassistant.core import callback  # noqa: PLC0415 - kept beside its only use
        from homeassistant.helpers.event import async_call_later  # noqa: PLC0415

        @callback  # in the event loop: without it Home Assistant would run it in an executor thread
        def due(_now: datetime) -> None:
            self._cancel = None
            self._task = self._hass.async_create_background_task(
                self._async_due(site), name="spotnav phase peaks backfill"
            )

        self._cancel = async_call_later(self._hass, delay_s, due)

    async def async_stop(self) -> None:
        """No further run, and the one going on cancelled (a read already handed to the recorder ends by itself)."""
        self._stopped = True
        cancel, self._cancel = self._cancel, None
        if cancel is not None:
            cancel()
        task, self._task = self._task, None
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001 - it is over either way
            _LOGGER.debug("SpotNav phase peaks: the backfill ended badly", exc_info=True)

    async def _async_due(self, site: SiteCapacityController) -> None:
        """One run, and the next one scheduled unless the learning is done by now."""
        now = dt_util.utcnow().timestamp()
        if self._peaks.learned(now).ready:
            return
        try:
            await self.async_once(site, now)
        except Exception:  # noqa: BLE001 - the conservative rule counts meanwhile; tried again next time
            self.last = {"at": dt_util.utcnow().isoformat(), "stopped": "error"}
            _LOGGER.debug("SpotNav phase peaks: the recorder's history could not be read back", exc_info=True)
        if not self._stopped:
            self._schedule(site, RETRY_S)

    async def async_once(self, site: SiteCapacityController, now: float) -> dict[str, Any]:
        """One run up to `now`: every hour of the window the store has no mark for, oldest first. What it gave."""
        self.runs += 1
        started = time.monotonic()
        summary: dict[str, Any] = {
            "at": datetime.fromtimestamp(now, UTC).isoformat(), "entities": 0, "rows": 0, "samples": 0,
            "hours_read": 0, "hours_taken": 0, "stopped": None, "without_history": [],
        }
        self.last = summary
        entity_ids = entities(site)
        summary["entities"] = len(entity_ids)
        try:
            from homeassistant.components.recorder import get_instance, history  # noqa: PLC0415 - only when loaded

            recorder = get_instance(self._hass)
        except (ImportError, KeyError):
            summary["stopped"] = "no_recorder"
            return summary
        if not entity_ids:
            summary["stopped"] = "no_entities"
            return summary

        hass = self._hass
        replay = Replay(site)
        newest = int(now // _HOUR_S)
        first = newest - WINDOW_DAYS * 24
        # Nothing is asked for from before the recorder's oldest state, where it says when that is.
        oldest = getattr(getattr(recorder, "states_manager", None), "oldest_ts", None)
        if isinstance(oldest, (int, float)) and not isinstance(oldest, bool):
            first = max(first, min(int(oldest // _HOUR_S), newest))
        first -= first % CHUNK_HOURS
        last_sample = now - now % SAMPLE_S
        fresh_start = True
        for chunk in range(first, newest + 1, CHUNK_HOURS):
            start = float(chunk * _HOUR_S)
            end = min(start + CHUNK_HOURS * _HOUR_S, last_sample)
            if end <= start:
                break
            if self._peaks.covered(chunk, min(CHUNK_HOURS, newest + 1 - chunk)):
                fresh_start = True  # not read: nothing is carried over it
                continue
            include_start = fresh_start

            def read(start: float = start, end: float = end, include_start: bool = include_start) -> Any:
                states = history.get_significant_states(
                    hass, datetime.fromtimestamp(start - OVERLAP_S, UTC), datetime.fromtimestamp(end, UTC), entity_ids,
                    significant_changes_only=False, include_start_time_state=include_start, no_attributes=False,
                )
                return StatesThen.rows(states, start, fresh_start=include_start)

            rows = await recorder.async_add_executor_job(read)
            summary["rows"] += replay.states.load_rows(rows, fresh_start=fresh_start)
            fresh_start = False
            if not replay.states.empty:
                summary["hours_read"] += CHUNK_HOURS
                await self._async_replay(replay, start, end)
                summary["samples"] = replay.samples
            if summary["rows"] > MAX_ROWS:
                summary["stopped"] = "rows"
                break
            if time.monotonic() - started > MAX_RUN_S:
                summary["stopped"] = "time"
                break

        taken = self._peaks.merge_history(replay.peaks, now)
        summary["hours_taken"] = taken
        summary["seconds"] = round(time.monotonic() - started, 1)
        without = summary["without_history"] = [
            entity_id for entity_id in entity_ids if entity_id not in replay.states.seen
        ] if summary["rows"] else []
        if replay.samples:
            self._changed()
        if not taken and without and summary["hours_read"]:
            _LOGGER.info(
                "SpotNav site: the house's per-phase peaks could not be read back from the recorder (%d hours read, "
                "none with a minute that counts). The recorder keeps no history of %s: while the site's measurement "
                "reads them, the days before now cannot be reckoned and the peaks are learned from now on",
                summary["hours_read"], ", ".join(without),
            )
        if taken:
            learned = self._peaks.learned(now)
            _LOGGER.info(
                "SpotNav site: the house's per-phase peaks were read back from the recorder: %d hours taken of %d "
                "read (%d rows, %.0f s%s); %d days now count, %d of them from the history%s",
                taken, summary["hours_read"], summary["rows"], summary["seconds"],
                "" if summary["stopped"] is None else f", stopped at its limit of {summary['stopped']}",
                learned.days, learned.history_days,
                f". The recorder keeps no history of {', '.join(without)}" if without else "",
            )
        return summary

    async def _async_replay(self, replay: Replay, start: float, end: float) -> None:
        """The samples of `[start, end)`, in slices of `SLICE_S` with the event loop let run between them."""
        deadline = time.monotonic() + SLICE_S
        for then in sample_times(start, end):
            replay.sample(float(then))
            if time.monotonic() >= deadline:
                await asyncio.sleep(PAUSE_S)
                deadline = time.monotonic() + SLICE_S


__all__: Sequence[str] = (
    "Backfill", "CHUNK_HOURS", "MAX_ROWS", "MAX_RUN_S", "Replay", "SiteAt", "StatesThen", "entities_read",
    "sample_times",
)
