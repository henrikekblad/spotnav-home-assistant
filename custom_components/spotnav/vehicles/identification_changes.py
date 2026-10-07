"""When a car's plug and position entities really changed or reported, as SpotNav saw it happen.

Home Assistant gives a state a new `last_changed` and `last_reported` whenever it is written anew: at Home
Assistant's start, when an integration re-creates or reloads an entity, and when an entity comes back from
`unavailable`. The car said nothing then. Read from the state alone, a plug sensor re-created with "on" at a
restart looks like a car plugged in that moment (a field case: a plug-in four and a half minutes after a restart
was decided by it).

So identification reads the times from here instead:

* **A change** is a written value that differs from the last valid value SpotNav saw for that entity (through an
  `unavailable` gap). The first value seen, at SpotNav's start or when the entity first appears, is only the
  baseline: nothing is known about when it changed.
* **A report** is a change, or the integration writing a valid entity again (`state_reported`, or new attributes
  over a valid state). A state written anew (no old state, or an old one that was `unavailable` or `unknown`) with
  the same value is neither.

A time is only given while the entity still holds the value it was seen with.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import callback, CALLBACK_TYPE, Event, EventStateChangedData, EventStateReportedData, HomeAssistant, State
from homeassistant.helpers.event import async_track_state_change_event, async_track_state_report_event


def _valid(state: State | None) -> bool:
    return state is not None and state.state not in (STATE_UNAVAILABLE, STATE_UNKNOWN, "")


@dataclass(slots=True)
class _Seen:
    #: The last valid value seen.
    value: str
    #: When it became that value, seen live (`None`: it was the baseline).
    changed: datetime | None = None
    #: When the integration last wrote it, seen live.
    reported: datetime | None = None


class SourceChanges:
    """The real changes and reports of the entities `watch` names."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass
        self._watched: frozenset[str] = frozenset()
        self._seen: dict[str, _Seen] = {}
        self._unsubscribe: list[CALLBACK_TYPE] = []

    def watch(self, entity_ids: Iterable[str]) -> None:
        """Watch these entities (and stop watching any other); a newly watched one starts from its baseline."""
        wanted = frozenset(entity_ids)
        if wanted == self._watched:
            return
        self._stop_listening()
        for gone in self._watched - wanted:
            self._seen.pop(gone, None)
        for added in wanted - self._watched:
            state = self._hass.states.get(added)
            if _valid(state):
                assert state is not None
                self._seen[added] = _Seen(state.state)
        self._watched = wanted
        if wanted:
            self._unsubscribe = [
                async_track_state_change_event(self._hass, wanted, self._on_changed),
                async_track_state_report_event(self._hass, wanted, self._on_reported),
            ]

    def stop(self) -> None:
        self._stop_listening()
        self._watched = frozenset()
        self._seen.clear()

    def _stop_listening(self) -> None:
        for remove in self._unsubscribe:
            remove()
        self._unsubscribe = []

    def changed(self, state: State | None) -> datetime | None:
        """When this entity really changed to the value it holds, or `None` when SpotNav did not see it."""
        seen = self._current(state)
        return None if seen is None else seen.changed

    def reported(self, state: State | None) -> datetime | None:
        """When this entity's integration last really wrote the value it holds, or `None`."""
        seen = self._current(state)
        return None if seen is None else seen.reported

    def _current(self, state: State | None) -> _Seen | None:
        if state is None:
            return None
        seen = self._seen.get(state.entity_id)
        return seen if seen is not None and seen.value == state.state else None

    @callback
    def _on_changed(self, event: Event[EventStateChangedData]) -> None:
        new, old = event.data["new_state"], event.data["old_state"]
        if new is None or not _valid(new):
            return
        seen = self._seen.get(new.entity_id)
        if seen is None:
            self._seen[new.entity_id] = _Seen(new.state)
        elif new.state != seen.value:
            seen.value, seen.changed, seen.reported = new.state, new.last_changed, new.last_reported
        elif _valid(old):
            seen.reported = new.last_reported

    @callback
    def _on_reported(self, event: Event[EventStateReportedData]) -> None:
        new = event.data["new_state"]
        seen = self._seen.get(new.entity_id)
        if seen is not None and seen.value == new.state:
            seen.reported = event.data["last_reported"]
