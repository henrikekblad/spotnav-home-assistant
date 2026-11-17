"""The cars' energy per period from SpotNav's own charging sessions, for a charger whose energy register has no
long-term statistics (a charger that keeps none leaves the month and year flows without the car).

Pure: the sessions' energy is spread over the periods they overlap. A session's price intervals say when it drew
energy; where they hold nearly all of it they are used, else the energy is spread evenly from start to end. The
sessions are a complete record from the earliest session on, so a period from then without energy is a period
without a charge (0); a period before the first session is unknown (not in the answer).
"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Iterable, Sequence
from datetime import datetime, timedelta
from typing import Final

from ..sessions.model import ChargeSession

#: The share of a session's energy its price intervals must hold to be used for when it was drawn.
INTERVAL_SHARE: Final = 0.95


def _spans(session: ChargeSession) -> list[tuple[datetime, datetime, float]]:
    """Where the session's energy was drawn: `(start, end, kWh)`."""
    if session.energy_kwh <= 0:
        return []
    intervals = [(item.start, item.end, item.kwh) for item in session.intervals if item.kwh > 0 and item.end > item.start]
    if intervals and sum(kwh for _s, _e, kwh in intervals) >= INTERVAL_SHARE * session.energy_kwh:
        return intervals
    end = session.end
    if end is None:
        end = getattr(session, "last_sample_at", None) or session.start
    if end <= session.start:
        return [(session.start, session.start + timedelta(minutes=1), session.energy_kwh)]
    return [(session.start, end, session.energy_kwh)]


def car_energy_from_sessions(
    sessions: Iterable[ChargeSession], starts: Sequence[datetime], step: timedelta
) -> dict[datetime, float]:
    """The energy (kWh) of each period in `starts` (UTC period starts, each `step` long) the sessions drew; periods
    before the earliest session are left out."""
    sessions = list(sessions)
    if not sessions or not starts:
        return {}
    since = min(session.start for session in sessions)
    out = {start: 0.0 for start in starts if start + step > since}
    if not out:
        return {}
    ordered = sorted(out)
    for session in sessions:
        for begin, end, kwh in _spans(session):
            length = (end - begin).total_seconds()
            for start in ordered[max(0, bisect_right(ordered, begin - step)) :]:
                if start >= end:
                    break
                overlap = (min(end, start + step) - max(begin, start)).total_seconds()
                if overlap > 0:
                    out[start] += kwh * overlap / length
    return out
