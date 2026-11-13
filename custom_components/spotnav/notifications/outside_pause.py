"""A pause SpotNav did not command: kept quiet, counted, never fought.

An aggregator may pause a charge that SpotNav runs: Tibber Grid Rewards pauses an EV charge 0-5 times a session,
about ten minutes each, from its cloud through the charger's own (`plans/research_integrations/
commercial_energy_gap_2026-11-14.md`, Tibber and opportunity 2). Such a pause is short, bounded and ends by itself,
so SpotNav keeps its plan, tells no one and does not replan: nothing about it is for a person to act on. It is
logged and counted (the charger's diagnostics).

An **outside pause** is trouble in an open window of the plan, after the charge ran in that window, while the plan
still expects it (so not a pause of SpotNav's own: load balancing, a person's Stop, a window's end and the target
all take the expectation away), with the charger reachable and enabled and the car plugged in, and not the car
itself declining current (`vehicle_not_requesting` without the charger saying it holds the charge). Two shapes:
the charger says it holds the charge (`charger_held`: Easee's `awaiting_smart_start`, OCPP's `SuspendedEVSE`), or
the charge simply stopped (`stopped`). The car ending a charge by itself is still found by the plan's own watch
(`execution/plan_car_ended.py`), which never counts a charger-held pause as the car.

`unexpected_stop.UnexpectedStopDetector` gives an outside pause `MAX_S` instead of its short grace before it tells
`plan_stopped`: one that lasts longer is no aggregator's short pause and is told as before.

Pure: no Home Assistant import; times are aware datetimes from the caller.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, Literal

#: The longest an outside pause is let be before it is trouble to tell (s): Tibber's pauses are about ten minutes.
MAX_S: Final = 20 * 60.0
#: Pauses shorter than this (s) are a reading's flicker, not counted.
MIN_S: Final = 30.0
#: The last pauses kept for the diagnostics.
RECENT_MAX: Final = 10

How = Literal["charger_held", "stopped"]
#: How a pause ended: the charge resumed, the plan stopped expecting it (its window ended, the plan moved on), it
#: lasted past `MAX_S` (then told as trouble), or other trouble took its place (the charger went away, the car
#: declines current). The last two are not counted as outside pauses.
Outcome = Literal["resumed", "plan_moved_on", "too_long", "other_trouble"]


@dataclass(frozen=True, slots=True)
class PauseEnd:
    since: datetime
    at: datetime
    how: How
    outcome: Outcome

    @property
    def seconds(self) -> float:
        return (self.at - self.since).total_seconds()

    def as_dict(self) -> dict[str, Any]:
        return {
            "since": self.since.isoformat(),
            "at": self.at.isoformat(),
            "how": self.how,
            "outcome": self.outcome,
            "minutes": round(self.seconds / 60.0, 1),
        }


class OutsidePauses:
    """The open outside pause and the tally (since the start: diagnostics only)."""

    def __init__(self) -> None:
        self._since: datetime | None = None
        self._how: How | None = None
        self.total = 0
        self.too_long = 0
        self._day: str | None = None
        self.today = 0
        self.recent: deque[PauseEnd] = deque(maxlen=RECENT_MAX)

    @property
    def open_since(self) -> datetime | None:
        return self._since

    def begin(self, now: datetime, how: How) -> None:
        if self._since is None:
            self._since, self._how = now, how
        elif how == "charger_held":
            # The charger said so after all: the clearer shape wins.
            self._how = how

    def end(self, now: datetime, outcome: Outcome) -> PauseEnd | None:
        """Close the open pause, if any: counted unless it was a flicker or lasted too long."""
        if self._since is None or self._how is None:
            return None
        ended = PauseEnd(self._since, now, self._how, outcome)
        self._since = self._how = None
        if ended.seconds < MIN_S and outcome != "too_long":
            return None
        self.recent.append(ended)
        if outcome == "too_long":
            self.too_long += 1
            return ended
        if outcome == "other_trouble":
            return ended
        day = now.date().isoformat()
        if day != self._day:
            self._day, self.today = day, 0
        self.today += 1
        self.total += 1
        return ended

    def as_dict(self, now: datetime | None = None) -> dict[str, Any]:
        today = self.today if now is None or self._day == now.date().isoformat() else 0
        return {
            "open_since": None if self._since is None else self._since.isoformat(),
            "open_how": self._how,
            "today": today,
            "total": self.total,
            "too_long": self.too_long,
            "recent": [pause.as_dict() for pause in self.recent],
        }
