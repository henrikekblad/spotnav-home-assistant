"""Whether the site's grid reading has its sign set the wrong way, from what a charger's own steps do to it.

A grid meter that counts export as negative read with "shows export as positive" ticked (or the other way round)
turns export into import: solar finds no surplus and load balancing reads the house the wrong way. The setting cannot
be checked from one reading, but SpotNav moves the grid itself: when a charger it measures starts, stops or changes
its current by a clear step, the site's import must move the same way by about as much. That is the step test.

A step is the charger's own reading (watts: its measured current source, its own current sensors or its smart plug's
power) moving by at least `STEP_MIN_W` from the value it had settled on. The grid's value before it is the one in the
latest kept sample taken no later than the charger's last report of the old value (the draw cannot have moved before
that); the value after it is read once the charger's reading has held for `SETTLE_S` and the grid has reported since
the charger showed the change. A step counts only when:

* the charger's change is at least `STEP_MIN_W`, and it all fits in `MAX_STEP_WINDOW_S`, with no more than
  `MAX_GAP_S` between the charger's last report of the old value and its first of the new one;
* the grid reading is fresh on both sides (the caller passes a stale one as `None`), the home battery's power (when a
  battery sensor is set) is fresh on both sides and moved by no more than `QUIET_W`, and the other chargers' own
  readings together moved by no more than `QUIET_W`;
* the grid moved by at least `GRID_SHARE_MIN` and at most `GRID_SHARE_MAX` of the charger's change.

It is `inverted` when the grid moved against the charger, `consistent` when with it. Per tested reading (`KIND_POWER`:
the grid power, the `grid_power_inverted` setting; `KIND_CURRENT`: a direct site's signed phase currents, the
`site_current_signed` setting) the counted steps are tallied: the warning is raised once `RAISE_INVERTED` of the last
`RECENT` are inverted and none of them is consistent, and cleared after `CLEAR_CONSISTENT` consistent steps. A tally
is kept with the setting's value it was made under; once the setting is changed it is dropped, and with it the
warning. Nothing here writes a setting: it only says what the steps show.

Pure Python: times are epoch seconds passed in.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Final, Literal

#: The grid power (signed, positive = import), as the site reads it with `grid_power_inverted`.
KIND_POWER: Final = "grid_power"
#: A direct site's phase currents with their sign (`site_current_signed`), summed at the nominal voltage.
KIND_CURRENT: Final = "grid_current"
KINDS: Final = (KIND_POWER, KIND_CURRENT)
Kind = Literal["grid_power", "grid_current"]
Outcome = Literal["inverted", "consistent"]

NOMINAL_VOLTAGE_V: Final = 230.0
#: The smallest charger change that counts: 6 A on one phase.
STEP_MIN_W: Final = 6.0 * NOMINAL_VOLTAGE_V
#: A charger reading within this of its previous one has not moved.
STABLE_W: Final = 300.0
#: How long the charger's reading must hold its new value before the step is read.
SETTLE_S: Final = 20.0
#: The longest the whole step may take, from the grid's value before it to the one after it.
MAX_STEP_WINDOW_S: Final = 180.0
#: The longest gap between the charger's last report of its old value and its first of the new one.
MAX_GAP_S: Final = 90.0
#: How much the battery, and the other chargers together, may move during a step and it still count.
QUIET_W: Final = 400.0
#: The grid's change against the charger's, in size, for a step to count.
GRID_SHARE_MIN: Final = 0.5
GRID_SHARE_MAX: Final = 2.0
#: Samples are kept this long, at most one per `SAMPLE_SPACING_S`.
BUFFER_S: Final = MAX_STEP_WINDOW_S + 60.0
SAMPLE_SPACING_S: Final = 1.0

RAISE_INVERTED: Final = 3
RECENT: Final = 5
CLEAR_CONSISTENT: Final = 3


@dataclass(frozen=True, slots=True)
class Reading:
    """A value in watts and when its source last reported it (epoch seconds)."""

    value: float
    at: float


@dataclass(frozen=True, slots=True)
class Sample:
    """What the site read at one moment. `chargers`: each member charger's own draw (`None`: not known now);
    `grid`: each tested kind's signed watts (`None`: not fresh); `battery`: the home battery's power (`None`: not
    fresh), read only when `battery_known` (a battery sensor is set)."""

    at: float
    chargers: Mapping[str, Reading | None]
    grid: Mapping[str, Reading | None]
    battery: Reading | None = None
    battery_known: bool = False


@dataclass(frozen=True, slots=True)
class Change:
    """The warning for `kind` was raised or cleared (`cause`: `steps`, or `setting` when the setting changed)."""

    kind: str
    raised: bool
    inverted: int
    consistent: int
    cause: str = "steps"


@dataclass(slots=True)
class Tally:
    """One kind's counted steps: the last `RECENT`, whether the warning stands, and the consistent ones since it was
    raised. `flag` is the setting's value they were counted under."""

    flag: bool
    recent: deque[str] = field(default_factory=lambda: deque(maxlen=RECENT))
    warned: bool = False
    consistent_since: int = 0

    def counts(self) -> tuple[int, int]:
        return self.recent.count("inverted"), self.recent.count("consistent")

    def add(self, outcome: Outcome) -> bool:
        """Count one step; `True` when the warning was raised or cleared by it."""
        self.recent.append(outcome)
        inverted, consistent = self.counts()
        if self.warned:
            if outcome == "consistent":
                self.consistent_since += 1
                if self.consistent_since >= CLEAR_CONSISTENT:
                    self.warned = False
                    self.consistent_since = 0
                    return True
            return False
        if inverted >= RAISE_INVERTED and consistent == 0:
            self.warned = True
            self.consistent_since = 0
            return True
        return False

    def as_dict(self) -> dict[str, Any]:
        return {
            "flag": self.flag,
            "recent": list(self.recent),
            "warned": self.warned,
            "consistent_since": self.consistent_since,
        }

    @classmethod
    def from_dict(cls, raw: Any) -> Tally | None:
        if not isinstance(raw, dict) or not isinstance(raw.get("flag"), bool):
            return None
        recent = raw.get("recent")
        warned = raw.get("warned")
        since = raw.get("consistent_since")
        if not isinstance(recent, list) or not all(item in ("inverted", "consistent") for item in recent):
            return None
        if not isinstance(warned, bool) or not isinstance(since, int) or isinstance(since, bool) or since < 0:
            return None
        tally = cls(raw["flag"], deque(recent[-RECENT:], maxlen=RECENT), warned, since)
        return tally


@dataclass(slots=True)
class _Pending:
    before_at: float
    before_w: float
    last_w: float
    last_change_at: float


@dataclass(slots=True)
class _Track:
    settled_w: float
    settled_at: float
    pending: _Pending | None = None


class GridSignCheck:
    """The step test for one site (module docstring). `flags` names the kinds tested now and their setting's value."""

    def __init__(self, flags: Mapping[str, bool]) -> None:
        self._flags = dict(flags)
        self._tallies: dict[str, Tally] = {kind: Tally(flag) for kind, flag in self._flags.items()}
        self._samples: deque[Sample] = deque()
        self._tracks: dict[str, _Track] = {}
        #: The kinds of the last counted step and their outcome, for the diagnostics.
        self.last_step: dict[str, Any] | None = None
        #: How many steps were counted since this check was made: the tally to keep changed when it grows.
        self.steps_counted = 0

    # -- the kept tally

    def load(self, stored: Any) -> list[Change]:
        """Take the tally kept before a restart. One counted under another value of its setting, or for a kind not
        tested now, is dropped; a warning it carried is reported cleared (`cause` `setting`)."""
        changes: list[Change] = []
        if not isinstance(stored, dict):
            return changes
        for kind, raw in stored.items():
            tally = Tally.from_dict(raw)
            if tally is None:
                continue
            if self._flags.get(kind) != tally.flag:
                if tally.warned:
                    inverted, consistent = tally.counts()
                    changes.append(Change(kind, False, inverted, consistent, "setting"))
                continue
            self._tallies[kind] = tally
        return changes

    def as_dict(self) -> dict[str, Any]:
        return {kind: tally.as_dict() for kind, tally in self._tallies.items()}

    def warned(self, kind: str) -> bool:
        tally = self._tallies.get(kind)
        return tally is not None and tally.warned

    def flag(self, kind: str) -> bool | None:
        return self._flags.get(kind)

    def snapshot(self) -> dict[str, Any]:
        """For the diagnostics: per tested kind its setting, the last counted steps and the warning."""
        return {
            "tallies": self.as_dict(),
            "last_step": self.last_step,
        }

    # -- the step test

    def observe(self, sample: Sample) -> list[Change]:
        """Take one sample; the warnings it raised or cleared."""
        self._keep(sample)
        changes: list[Change] = []
        for charger_id in list(self._tracks):
            if sample.chargers.get(charger_id) is None:
                del self._tracks[charger_id]
        for charger_id, reading in sample.chargers.items():
            if reading is None:
                continue
            track = self._tracks.get(charger_id)
            if track is None:
                self._tracks[charger_id] = _Track(reading.value, reading.at)
                continue
            pending = track.pending
            if pending is None:
                moved = abs(reading.value - track.settled_w)
                if moved < STEP_MIN_W:
                    if moved <= STABLE_W:
                        track.settled_at = max(track.settled_at, reading.at)
                    else:
                        track.settled_w, track.settled_at = reading.value, reading.at
                    continue
                if reading.at - track.settled_at > MAX_GAP_S:
                    track.settled_w, track.settled_at = reading.value, reading.at
                    continue
                pending = _Pending(track.settled_at, track.settled_w, reading.value, reading.at)
                track.pending = pending
            elif abs(reading.value - pending.last_w) > STABLE_W:
                pending.last_w, pending.last_change_at = reading.value, reading.at
            if sample.at - pending.before_at > MAX_STEP_WINDOW_S:
                track.pending = None
                track.settled_w, track.settled_at = reading.value, reading.at
                continue
            if sample.at - pending.last_change_at < SETTLE_S:
                continue
            changes += self._count(charger_id, pending, reading, sample)
            track.pending = None
            track.settled_w, track.settled_at = reading.value, reading.at
        return changes

    def _keep(self, sample: Sample) -> None:
        samples = self._samples
        if samples and sample.at - samples[-1].at < SAMPLE_SPACING_S:
            return
        samples.append(sample)
        while samples and sample.at - samples[0].at > BUFFER_S:
            samples.popleft()

    def _before(self, at: float) -> Sample | None:
        found = None
        for sample in self._samples:
            if sample.at > at:
                break
            found = sample
        return found

    def _count(self, charger_id: str, pending: _Pending, reading: Reading, after: Sample) -> list[Change]:
        before = self._before(pending.before_at)
        if before is None:
            return []
        charger_w = reading.value - pending.before_w
        if abs(charger_w) < STEP_MIN_W:
            return []
        if after.battery_known:
            if before.battery is None or after.battery is None:
                return []
            if abs(after.battery.value - before.battery.value) > QUIET_W:
                return []
        others = 0.0
        for other in set(before.chargers) | set(after.chargers):
            if other == charger_id:
                continue
            was, now = before.chargers.get(other), after.chargers.get(other)
            if was is None or now is None:
                return []
            others += now.value - was.value
        if abs(others) > QUIET_W:
            return []
        changes: list[Change] = []
        counted: dict[str, Any] = {}
        for kind, tally in self._tallies.items():
            was, now = before.grid.get(kind), after.grid.get(kind)
            if was is None or now is None or now.at < pending.last_change_at:
                continue
            grid_w = now.value - was.value
            if not GRID_SHARE_MIN * abs(charger_w) <= abs(grid_w) <= GRID_SHARE_MAX * abs(charger_w):
                continue
            outcome: Outcome = "consistent" if (grid_w > 0) == (charger_w > 0) else "inverted"
            counted[kind] = {"outcome": outcome, "charger_w": round(charger_w), "grid_w": round(grid_w)}
            if tally.add(outcome):
                inverted, consistent = tally.counts()
                changes.append(Change(kind, tally.warned, inverted, consistent))
        if counted:
            self.steps_counted += 1
            self.last_step = {"charger": charger_id, "at": after.at, **counted}
        return changes
