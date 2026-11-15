"""Shadow mode: each part that would control more than the charger first only logs what it would have written.

A part is `shadow` (it decides and logs, writes nothing) or `on` (it writes). In this release a part cannot be
turned on: `ON_REACHABLE` is off, a stored `on` loads as `shadow`, and nothing offers a way to turn it on.
"All back to shadow" puts every part back in one step.

The hold for the car (`PART_HOLD_FOR_CAR`) decides when to hold; what it would write is the executor's action
`hold` (`executor.preview`), the same write route a later opt-in will turn on, so the shadow log shows exactly the
writes and hand-back that route would make.

The log is bounded by count and by age and survives a restart with the rest of the book (`as_dict`, `from_dict`).

Each entry is shaped for the confirmed-write ledger that comes with the first real write (Predbat's
`control_ledger.py`, `plans/research_integrations/predbat_lowhanging_2026-11-13.md` items 1 and 5): `read` is what
the entity read when the decision was made, and `written`/`read_back` stay `False`/`None` in shadow. Once a part
writes, the value it owns is the value read back after the write, never the value sent; an `unavailable` read never
contradicts it, and a difference counts as someone else's change only after `MIN_DIVERGENCE_S`. The control-failure
monitor (a hold that does not hold) adds its verdicts as entries of the same shape. Both keep their own state in
named sections of the book (`section`, `set_section`), stored with it.
Pure: no Home Assistant import; times are aware datetimes from the caller.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Final, Literal

PartState = Literal["shadow", "on"]
#: `changed_outside` is the ledger's dry run (the control changed while nothing wrote it); `would_judge` is the
#: control-failure monitor's dry run (what it could have judged).
ShadowAction = Literal["would_write", "would_restore", "left_alone", "not_possible", "changed_outside", "would_judge"]

PART_HOLD_FOR_CAR: Final = "hold_for_car"
#: A switched load on the site run in the cheapest hours (`loads/`); its book is the load's own, not the battery's.
PART_SWITCH_LOAD: Final = "switch_load"
#: No solar export while the sell price is below zero (`export_curtail.py`): the site's export limit at 0 kW.
PART_EXPORT_CURTAIL: Final = "export_curtail"
#: Every part, in the order they were added.
PARTS: Final[tuple[str, ...]] = (PART_HOLD_FOR_CAR, PART_SWITCH_LOAD, PART_EXPORT_CURTAIL)

#: Whether a part may be turned on at all. Off in this release: everything runs in shadow.
ON_REACHABLE: Final = False

LOG_MAX_ENTRIES: Final = 200
LOG_MAX_AGE: Final = timedelta(days=7)

#: How long a read must differ from the owned value before it counts as another system's change (for the ledger).
MIN_DIVERGENCE_S: Final = 240.0

_ACTIONS: Final = frozenset(
    {"would_write", "would_restore", "left_alone", "not_possible", "changed_outside", "would_judge"}
)


def parse_aware_datetime(value: Any) -> datetime | None:
    """An offset-bearing ISO-8601 string as a datetime; anything else, or a naive one, is `None`."""
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else None


@dataclass(frozen=True, slots=True)
class ShadowEntry:
    """One decision: what the part would have written (or why not), to which entity, when and why."""

    at: datetime
    part: str
    action: ShadowAction
    reason: str
    entity_id: str | None = None
    value: Any = None
    detail: Mapping[str, Any] = field(default_factory=dict)
    #: The entity's state when the decision was made.
    read: str | None = None
    #: Whether it was written, and what the entity read back afterwards: always `False` and `None` in shadow.
    written: bool = False
    read_back: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "at": self.at.isoformat(),
            "part": self.part,
            "action": self.action,
            "reason": self.reason,
            "entity_id": self.entity_id,
            "value": self.value,
            "detail": dict(self.detail),
            "read": self.read,
            "written": self.written,
            "read_back": self.read_back,
        }

    @classmethod
    def from_dict(cls, data: Any) -> ShadowEntry | None:
        if not isinstance(data, Mapping):
            return None
        at = parse_aware_datetime(data.get("at"))
        part, action, reason = data.get("part"), data.get("action"), data.get("reason")
        if at is None or part not in PARTS or action not in _ACTIONS or not isinstance(reason, str):
            return None
        entity_id = data.get("entity_id")
        detail = data.get("detail")
        return cls(
            at=at,
            part=part,
            action=action,
            reason=reason,
            entity_id=entity_id if isinstance(entity_id, str) else None,
            value=data.get("value"),
            detail=dict(detail) if isinstance(detail, Mapping) else {},
            read=_text(data.get("read")),
            written=data.get("written") is True,
            read_back=_text(data.get("read_back")),
        )


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) else None


class ShadowBook:
    """The parts' states, their remembered state between decisions and the bounded log."""

    def __init__(self) -> None:
        self._states: dict[str, PartState] = {part: "shadow" for part in PARTS}
        self._memory: dict[str, dict[str, Any]] = {}
        self._log: deque[ShadowEntry] = deque()
        self._sections: dict[str, dict[str, Any]] = {}

    def state(self, part: str) -> PartState:
        return self._states[part]

    def writes(self, part: str) -> bool:
        """Whether the part may write to a device now: never while `ON_REACHABLE` is off."""
        return ON_REACHABLE and self._states[part] == "on"

    def set_state(self, part: str, state: PartState) -> None:
        if part not in self._states:
            raise KeyError(part)
        if state == "on" and not ON_REACHABLE:
            raise ValueError("no part can be turned on in this release")
        self._states[part] = state

    def all_to_shadow(self) -> list[str]:
        """Every part back in shadow; the parts that were on."""
        changed = [part for part, state in self._states.items() if state != "shadow"]
        for part in changed:
            self._states[part] = "shadow"
        return changed

    def memory(self, part: str) -> dict[str, Any]:
        return dict(self._memory.get(part, {}))

    def set_memory(self, part: str, memory: Mapping[str, Any]) -> None:
        self._memory[part] = dict(memory)

    def section(self, name: str) -> dict[str, Any]:
        """A named piece of state kept with the book (the ledger's, the monitor's)."""
        return dict(self._sections.get(name, {}))

    def set_section(self, name: str, data: Mapping[str, Any]) -> None:
        self._sections[name] = dict(data)

    def record(self, entry: ShadowEntry) -> None:
        self._log.append(entry)
        self._prune(entry.at)

    def _prune(self, now: datetime) -> None:
        oldest = now - LOG_MAX_AGE
        while self._log and (len(self._log) > LOG_MAX_ENTRIES or self._log[0].at < oldest):
            self._log.popleft()

    @property
    def entries(self) -> list[ShadowEntry]:
        return list(self._log)

    def as_dict(self) -> dict[str, Any]:
        return {
            "parts": dict(self._states),
            "memory": {part: dict(memory) for part, memory in self._memory.items()},
            "log": [entry.as_dict() for entry in self._log],
            "sections": {name: dict(data) for name, data in self._sections.items()},
        }

    @classmethod
    def from_dict(cls, data: Any, now: datetime) -> ShadowBook:
        """A book from storage: unknown parts and broken rows are dropped, the log is pruned to `now`, and a
        part stored `on` loads `shadow` while no part can be on."""
        book = cls()
        if not isinstance(data, Mapping):
            return book
        parts = data.get("parts")
        if isinstance(parts, Mapping):
            for part, state in parts.items():
                if part in book._states and state == "on" and ON_REACHABLE:
                    book._states[part] = "on"
        memory = data.get("memory")
        if isinstance(memory, Mapping):
            for part, value in memory.items():
                if part in book._states and isinstance(value, Mapping):
                    book._memory[part] = dict(value)
        sections = data.get("sections")
        if isinstance(sections, Mapping):
            for name, value in sections.items():
                if isinstance(name, str) and isinstance(value, Mapping):
                    book._sections[name] = dict(value)
        log = data.get("log")
        rows: Iterable[Any] = log if isinstance(log, list) else ()
        entries = sorted(
            (entry for entry in (ShadowEntry.from_dict(row) for row in rows) if entry is not None),
            key=lambda entry: entry.at,
        )
        book._log.extend(entries)
        book._prune(now)
        return book
