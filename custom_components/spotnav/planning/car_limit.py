"""Noticing within minutes that the car takes clearly less than the charger gives it, so the plan counts on that.

A plan counts on the charger's current times its phases, or on the power learned of the car
(`planning/learned_power.py`). A car whose onboard charger takes less (an 11 kW car on a 20 A three-phase plan, a
cold battery) falls behind from the first minute, and a learned power needs a plug-in or more before it is trusted.
This watch measures, once a minute while a window of the plan charges (`AutoPlannerController._check_plan`), what
the car draws against what it is offered, and says so once that is clearly less:

* **Offered.** The power at the current in force on the charger now (its current-limit entity, else the plan's
  current for a charger whose current is not set), on today's phases and voltage. A current that load balancing,
  the import target or the sun holds below the plan's is what the car is offered: a car taking clearly less than a
  held current is at its own limit, while a car taking what a held current gives says nothing (it may take more).
* **Steady.** A stretch is checks a minute apart of the same plug-in and the same current in force, written no
  later than the stretch's first check. It measures from the first post of the count (the energy register, or the
  energy integrated from the charger's power) `SETTLE_S` after the stretch began or the current was last written
  (a car ramping up, a charger following a new current), over at least `DWELL_S` and `MIN_KWH` between posts.
* **Clearly less.** Below `LIMIT_SHARE` of the offered power. Not judged at all (the caller passes no offer): the
  charger not charging or the car ending the charge, the car's known level at or above `TAPER_PERCENT` (a car
  tapers by itself near full), a phase switch settling or forced, a guest car, no window of the plan open.
* **What it says.** The best power measured in a steady stretch of this plug-in, once a stretch of it was clearly
  below its offer: what the car can take, never less than it was seen to take. It is kept with the plug-in in the
  learned store (`LearnedPowerStore.set_limit`), so the plan made now and the next plug-in's count on it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

#: A car drawing less than this share of what it is offered is at its own limit.
LIMIT_SHARE: Final = 0.9
#: The seconds after a stretch begins or the current is written before the measure starts (a ramp).
SETTLE_S: Final = 120.0
#: The least seconds and energy (kWh) between two posts that make a measure.
DWELL_S: Final = 360.0
MIN_KWH: Final = 0.5
#: The longest gap (s) between two checks of one stretch (they come once a minute).
MAX_STEP_S: Final = 400.0


@dataclass(frozen=True, slots=True)
class LimitFacts:
    """What one check reads: the plug-in, the offer, and the count."""

    session: str
    #: The power at the current in force now (kW).
    offered_kw: float
    #: When the current in force was last written, if known.
    written_at: datetime | None
    #: The count (kWh) and when it last posted (`None`: now).
    reading_kwh: float
    reading_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class LimitMeasure:
    """One measure of a steady stretch, as the diagnostics show it."""

    at: datetime
    offered_kw: float
    drawn_kw: float
    minutes: float
    limited: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "at": self.at.isoformat(),
            "offered_kw": round(self.offered_kw, 3),
            "drawn_kw": round(self.drawn_kw, 3),
            "minutes": round(self.minutes, 1),
            "limited": self.limited,
        }


@dataclass(slots=True)
class _Stretch:
    session: str
    offered_kw: float
    written_at: datetime | None
    since: datetime
    last_check: datetime
    base_kwh: float | None = None
    base_at: datetime | None = None


class CarLimitWatch:
    """The car's own limit in one charger's plug-in in progress (see the module docstring)."""

    def __init__(self) -> None:
        self._stretch: _Stretch | None = None
        self._session: str | None = None
        #: The best power measured in this plug-in's steady stretches, their minutes, and whether one was limited.
        self._best_kw = 0.0
        self._minutes = 0.0
        self._limited = False
        self.last_measure: LimitMeasure | None = None

    def limit_kw(self, session: str) -> tuple[float, float] | None:
        """The car's limit in plug-in `session` and the minutes behind it, once one was seen, else `None`."""
        if session != self._session or not self._limited:
            return None
        return self._best_kw, self._minutes

    def check(self, now: datetime, facts: LimitFacts | None) -> tuple[float, float] | None:
        """One check (`None`: nothing to judge now, and the stretch ends). Returns the car's limit and its minutes
        when this check measured one (see `limit_kw`)."""
        if facts is None:
            self._stretch = None
            return None
        if facts.session != self._session:
            self._session, self._best_kw, self._minutes, self._limited = facts.session, 0.0, 0.0, False
        stretch = self._stretch
        if (
            stretch is None
            or stretch.session != facts.session
            or abs(stretch.offered_kw - facts.offered_kw) > 1e-6
            or stretch.written_at != facts.written_at
            or not 0 < (now - stretch.last_check).total_seconds() <= MAX_STEP_S
        ):
            stretch = self._stretch = _Stretch(
                session=facts.session,
                offered_kw=facts.offered_kw,
                written_at=facts.written_at,
                since=now if facts.written_at is None else max(now, facts.written_at),
                last_check=now,
            )
        stretch.last_check = now
        posted = facts.reading_at or now
        if stretch.base_at is None or stretch.base_kwh is None:
            settle_from = stretch.since if facts.written_at is None else max(stretch.since, facts.written_at)
            if (posted - settle_from).total_seconds() >= SETTLE_S:
                stretch.base_kwh, stretch.base_at = facts.reading_kwh, posted
            return None
        seconds = (posted - stretch.base_at).total_seconds()
        kwh = facts.reading_kwh - stretch.base_kwh
        if kwh < 0:
            # A count that went back (replaced, reset): measured afresh from here.
            stretch.base_kwh, stretch.base_at = facts.reading_kwh, posted
            return None
        if seconds < DWELL_S or kwh < MIN_KWH:
            return None
        drawn = kwh / (seconds / 3600.0)
        limited = drawn < LIMIT_SHARE * facts.offered_kw
        self.last_measure = LimitMeasure(
            at=now, offered_kw=facts.offered_kw, drawn_kw=drawn, minutes=seconds / 60.0, limited=limited
        )
        if drawn > facts.offered_kw * 1.5:
            return None  # more than the current in force could give: the count is not this charge's
        if drawn >= self._best_kw:
            self._best_kw, self._minutes = drawn, seconds / 60.0
        self._limited = self._limited or limited
        return self.limit_kw(facts.session)
