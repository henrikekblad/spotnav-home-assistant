"""How often a measurement entity reports, and what that allows.

Home Assistant stamps `last_reported` on every state write, changed or not (2024.3+), and `last_changed`
only on a change. An integration that polls writes on every poll, so `last_reported` says the link is
alive although a flat reading never changes; one that writes only when a value changes (Easee's cloud,
EDL21, ZHA) leaves both stamps alone while nothing changes. `ReportCadence` keeps the last few
`last_reported` instants each measurement entity was seen with and estimates its update interval as the
median gap between them, from `MIN_INTERVALS` gaps on.

What the interval decides:

* Load balancing needs a reading that is never older than the site's maximum age. A meter whose interval
  is longer than half of it (`too_slow_for_load_balancing`) is fresh only now and then, so load balancing
  is held for it (`SiteCapacityController`), and the site lists it under "To check".
* Solar and planning react over minutes, so a slow meter still serves them: its readings are accepted up
  to `solar_age_limit_s`, two of its intervals (one missed report is tolerated), never less than the
  maximum age and never more than `SOLAR_AGE_CAP_S`, so a link that died stops counting within 20
  minutes.
* An integration known to write only on change (`site_detection.UPDATE_BEHAVIOUR`) whose entity is
  available and whose config entry is loaded is alive though silent; solar accepts its last value up to
  `CHANGE_ONLY_AGE_CAP_S`. Load balancing never does: its maximum age stays the rule.

Pure: the controller feeds the instants in, and nothing here reads Home Assistant.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable, Sequence
from datetime import datetime
from statistics import median
from typing import Final

from .site_capacity import classify_phase_liveness, PhaseLiveness

#: How many `last_reported` instants are kept per entity.
HISTORY_LENGTH: Final = 9
#: Gaps needed before an interval is estimated: one odd gap (a restart, a reconnect) cannot decide it.
MIN_INTERVALS: Final = 3
#: A meter is too slow for load balancing when its interval is longer than this share of the maximum age.
LOAD_BALANCING_SHARE: Final = 0.5
#: Solar accepts a slow meter's reading for this many of its intervals.
SOLAR_INTERVALS: Final = 2.0
#: The longest age solar accepts from a slow meter, in seconds.
SOLAR_AGE_CAP_S: Final = 1200.0
#: The longest age solar accepts from a change-only meter that is available, in seconds.
CHANGE_ONLY_AGE_CAP_S: Final = 1800.0


def median_interval_s(reports: Sequence[datetime]) -> float | None:
    """The median gap between consecutive instants (oldest first), or `None` with fewer than
    `MIN_INTERVALS` gaps."""
    gaps = [(later - earlier).total_seconds() for earlier, later in zip(reports, reports[1:])]
    gaps = [gap for gap in gaps if gap > 0]
    if len(gaps) < MIN_INTERVALS:
        return None
    return float(median(gaps))


def too_slow_for_load_balancing(interval_s: float | None, max_age_s: float) -> bool:
    """Whether a meter reporting every `interval_s` is too seldom fresh for load balancing."""
    return interval_s is not None and interval_s > max_age_s * LOAD_BALANCING_SHARE


def solar_age_limit_s(interval_s: float | None, max_age_s: float) -> float:
    """The oldest reading solar accepts from a meter reporting every `interval_s` (see the module
    docstring): the maximum age for a meter fast enough for load balancing."""
    if interval_s is None or not too_slow_for_load_balancing(interval_s, max_age_s):
        return max_age_s
    return max(max_age_s, min(SOLAR_AGE_CAP_S, SOLAR_INTERVALS * interval_s))


def solar_liveness(
    age_s: float | None,
    report_age_s: float | None,
    *,
    max_age_s: float,
    interval_s: float | None = None,
    change_only_alive: bool = False,
) -> PhaseLiveness:
    """`classify_phase_liveness` with solar's age limit for this meter; a change-only meter that is alive
    (available, its integration loaded) counts as `confirmed_unchanged` up to `CHANGE_ONLY_AGE_CAP_S`."""
    liveness = classify_phase_liveness(age_s, report_age_s, solar_age_limit_s(interval_s, max_age_s))
    if liveness in ("fresh", "confirmed_unchanged"):
        return liveness
    if change_only_alive and age_s is not None and age_s <= CHANGE_ONLY_AGE_CAP_S:
        return "confirmed_unchanged"
    return liveness


class ReportCadence:
    """The last `HISTORY_LENGTH` distinct `last_reported` instants of each entity, and its interval."""

    def __init__(self, history: int = HISTORY_LENGTH) -> None:
        self._history = history
        self._reports: dict[str, deque[datetime]] = {}

    def observe(self, entity_id: str, reported: datetime | None) -> None:
        """Note that `entity_id` was seen with `last_reported` = `reported`; a repeat or an older
        instant changes nothing."""
        if reported is None:
            return
        reports = self._reports.setdefault(entity_id, deque(maxlen=self._history))
        if reports and reported <= reports[-1]:
            return
        reports.append(reported)

    def interval_s(self, entity_id: str) -> float | None:
        return median_interval_s(list(self._reports.get(entity_id, ())))

    def slow(self, entity_ids: Iterable[str], max_age_s: float) -> dict[str, float]:
        """The entities too slow for load balancing, with their interval."""
        found: dict[str, float] = {}
        for entity_id in entity_ids:
            interval = self.interval_s(entity_id)
            if interval is not None and too_slow_for_load_balancing(interval, max_age_s):
                found[entity_id] = interval
        return found

    def reports(self, entity_id: str) -> list[datetime]:
        return list(self._reports.get(entity_id, ()))
