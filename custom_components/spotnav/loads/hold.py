"""What SpotNav holds on a low-risk load's settings (the pool, the spa, the sauna; spec `plans/v3_pool_spa_spec.md`
section 6 and the owner's decisions of 2026-11-23). Pure: no Home Assistant.

One `Held` is one setting of one entity that SpotNav may set (a switch's on/off, a climate's mode or setpoint,
Balboa's preset). The owner's hard rules that live here:

* **Snapshot and restore.** While SpotNav holds nothing, the person's value (`person`) follows what is read: it is the
  snapshot taken when the opt-in starts acting. An exit writes it back only where the entity still reads SpotNav's
  last write (`ours`); a value someone else set is left and becomes the person's.
* **Read back.** Every write is pending until it reads back; not read within `read_back` (90 s local, 300 s cloud) it
  is a failure (`observe` says `failed`), counted by the caller, which pauses the load to the next local day. A write
  that shows late is still SpotNav's (`written` keeps it), so the exit after a failure writes the person's value back.
* **Hand back.** A value SpotNav did not write, read for `FOREIGN_CONFIRM`, is the person's (`observe` says
  `foreign`): nothing is written back, SpotNav holds nothing and the caller watches the load to the next local day.
* **Budgets** (`Budget`): writes per entity per local day of a stored setting and of on/off, and starts per day and per
  hour of a compressor or a pump; over budget the load keeps its state and waits.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final

from ..core.parse import aware_instant, finite_number

READ_BACK_LOCAL: Final = timedelta(seconds=90)
READ_BACK_CLOUD: Final = timedelta(seconds=300)
FOREIGN_CONFIRM: Final = timedelta(seconds=60)
#: Two setpoints closer than this are the same (a half-degree step, a °F round trip).
SAME_C: Final = 0.3

#: What a write spends: a stored setting (a setpoint, a preset, a cloud write) or an on/off.
KIND_SETTING: Final = "setting"
KIND_SWITCH: Final = "switch"

OBSERVED_CONFIRMED: Final = "confirmed"
OBSERVED_FAILED: Final = "failed"
OBSERVED_FOREIGN: Final = "foreign"

Value = str | float


def same(a: Value | None, b: Value | None) -> bool:
    """Whether two read or written values are the same setting (numbers within `SAME_C`)."""
    if a is None or b is None:
        return False
    if isinstance(a, str) or isinstance(b, str):
        return str(a) == str(b)
    return abs(float(a) - float(b)) < SAME_C


def _value(raw: Any) -> Value | None:
    if isinstance(raw, str):
        return raw
    return finite_number(raw)


@dataclass(slots=True)
class Pending:
    value: Value
    at: datetime
    #: Writing the person's value back (an exit), not one SpotNav holds.
    home: bool = False


class Held:
    """One setting SpotNav may hold (see the module)."""

    def __init__(self, key: str) -> None:
        self.key = key
        self.person: Value | None = None
        #: SpotNav's last write (it holds the setting while this is set), and the value it held before a write that may
        #: not have taken.
        self.written: Value | None = None
        self.prior: Value | None = None
        self.pending: Pending | None = None
        self.foreign_since: datetime | None = None
        #: When a write of the person's value last failed (retried at most every `home_retry`).
        self.home_failed_at: datetime | None = None

    def stored(self) -> dict[str, Any]:
        return {"person": self.person, "written": self.written, "prior": self.prior}

    @classmethod
    def from_stored(cls, key: str, raw: Any) -> Held:
        held = cls(key)
        if isinstance(raw, Mapping):
            held.person = _value(raw.get("person"))
            held.written = _value(raw.get("written"))
            held.prior = _value(raw.get("prior"))
        return held

    def holds(self) -> bool:
        return self.written is not None or self.prior is not None or (self.pending is not None and not self.pending.home)

    def ours(self, read: Value | None) -> bool:
        """Whether the value read is SpotNav's (its last write, or the one before a write that may not have taken)."""
        return same(read, self.written) or same(read, self.prior)

    def target(self, read: Value | None) -> Value | None:
        """What the setting is heading to: the write in flight, else what SpotNav holds, else what is read."""
        if self.pending is not None:
            return self.pending.value
        if self.written is not None:
            return self.written
        return read

    def observe(self, read: Value | None, now: datetime, read_back: timedelta) -> str | None:
        """Judge the value read against what SpotNav wrote (see the module). `None` while nothing changed."""
        pending = self.pending
        if pending is not None:
            if read is not None and same(read, pending.value):
                self.pending = None
                self.foreign_since = None
                if pending.home:
                    self.written = self.prior = None
                    self.home_failed_at = None
                else:
                    self.written, self.prior = pending.value, None
                return OBSERVED_CONFIRMED
            if now - pending.at >= read_back:
                self.pending = None
                if pending.home:
                    self.home_failed_at = now
                else:
                    # Maybe SpotNav's after all (a late read-back): the exit writes the person's value back over it.
                    self.prior, self.written = self.written, pending.value
                return OBSERVED_FAILED
            return None
        if read is None:
            return None
        if self.written is not None or self.prior is not None:
            if self.ours(read):
                self.foreign_since = None
                if not same(read, self.written):
                    # The write before showed after all.
                    self.written, self.prior = read, None
                return None
            self.foreign_since = self.foreign_since or now
            if now - self.foreign_since >= FOREIGN_CONFIRM:
                self.person = read
                self.written = self.prior = None
                self.foreign_since = None
                return OBSERVED_FOREIGN
            return None
        self.person = read
        return None

    def wrote(self, value: Value, now: datetime, *, home: bool = False) -> None:
        self.pending = Pending(value, now, home)
        self.foreign_since = None

    def drop(self, read: Value | None) -> None:
        """Hold nothing, without a write: the value read is the person's (the season closed, an exit where someone
        else's value reads)."""
        self.written = self.prior = None
        self.pending = None
        self.foreign_since = None
        if read is not None:
            self.person = read


class Budget:
    """Writes and starts per local day (and starts per hour) of one load (see the module)."""

    def __init__(self) -> None:
        #: `{day: {key: count}}`, the key a `Held` key's write kind (`<key>:<kind>`).
        self.writes: dict[str, dict[str, int]] = {}
        #: `{key: [start instants]}` of the last day.
        self.starts: dict[str, list[datetime]] = {}

    def stored(self) -> dict[str, Any]:
        return {
            "writes": {day: dict(counts) for day, counts in self.writes.items()},
            "starts": {key: [moment.isoformat() for moment in moments] for key, moments in self.starts.items()},
        }

    @classmethod
    def from_stored(cls, raw: Any) -> Budget:
        budget = cls()
        if not isinstance(raw, Mapping):
            return budget
        writes = raw.get("writes")
        if isinstance(writes, Mapping):
            for day, counts in writes.items():
                if isinstance(day, str) and isinstance(counts, Mapping):
                    budget.writes[day] = {
                        k: v for k, v in counts.items() if isinstance(k, str) and isinstance(v, int) and not isinstance(v, bool)
                    }
        starts = raw.get("starts")
        if isinstance(starts, Mapping):
            for key, moments in starts.items():
                if isinstance(key, str) and isinstance(moments, list):
                    budget.starts[key] = [m for m in (aware_instant(v) for v in moments) if m is not None]
        return budget

    def written(self, day: str, key: str, kind: str) -> int:
        return self.writes.get(day, {}).get(f"{key}:{kind}", 0)

    def may_write(self, day: str, key: str, kind: str, limit: int) -> bool:
        return self.written(day, key, kind) < limit

    def note_write(self, day: str, key: str, kind: str) -> None:
        counts = self.writes.setdefault(day, {})
        counts[f"{key}:{kind}"] = counts.get(f"{key}:{kind}", 0) + 1

    def started(self, key: str, since: datetime) -> int:
        return sum(1 for moment in self.starts.get(key, ()) if moment >= since)

    def note_start(self, key: str, now: datetime) -> None:
        self.starts.setdefault(key, []).append(now)

    def trim(self, keep_days: set[str], now: datetime) -> None:
        self.writes = {day: counts for day, counts in self.writes.items() if day in keep_days}
        oldest = now - timedelta(days=1, hours=1)
        self.starts = {key: [m for m in moments if m >= oldest] for key, moments in self.starts.items()}
