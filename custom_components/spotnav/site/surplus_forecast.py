"""The expected solar surplus (SpotNav 3.0, Predbat's "export triggers" idea): how much of the sun the house is not
expected to use in the coming hours, from the sun forecast (corrected once its correction has learnt, else as read)
less the house load forecast's median (`site/house_forecast.py`).

Pure. Per hour: `surplus = max(0, sun_kwh - house_kw * 1 h)`. Only hours where both figures are known are listed (an
hour the house profile does not know is left out, never guessed), and the windows count each hour by the share of it
that lies inside the window, so the running hour counts only for what is left of it.

It is a prediction, and says so in its name: several automations that act on it at once can overbook the same sun.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Final

from .. import time_zones

HOURS_SHOWN: Final = 24
#: The state: the surplus expected within this many hours from now (kWh).
STATE_HOURS: Final = 4
#: The nearest window, for starting a short load before the sun peaks.
NEAR_HOURS: Final = 2
#: A forecast older than this is not used (the hourly refresh did not run).
MAX_AGE: Final = timedelta(hours=3)

_HOUR: Final = timedelta(hours=1)


@dataclass(frozen=True, slots=True)
class SurplusHour:
    """One hour: the sun forecast, the house forecast and what is left over (kWh, `surplus` never negative)."""

    start: datetime
    sun_kwh: float
    house_kwh: float
    surplus_kwh: float


def expected_surplus(
    sun_kwh: Mapping[datetime, float], house_kw: Mapping[datetime, float], now: datetime, *, hours: int = HOURS_SHOWN
) -> list[SurplusHour]:
    """The hours from the running one on (UTC keys), at most `hours`, that both forecasts know, earliest first."""
    current = now.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
    shown: list[SurplusHour] = []
    for start in sorted(sun_kwh):
        if start < current or start >= current + hours * _HOUR or start not in house_kw:
            continue
        sun, house = max(0.0, sun_kwh[start]), max(0.0, house_kw[start])
        shown.append(SurplusHour(start, sun, house, max(0.0, sun - house)))
    return shown


def window_kwh(shown: list[SurplusHour], now: datetime, span: timedelta) -> float | None:
    """The surplus within `[now, now + span)`, each hour by its share inside the window; `None` when no hour of the
    window is known."""
    end = now + span
    total, counted = 0.0, False
    for hour in shown:
        overlap = min(end, hour.start + _HOUR) - max(now, hour.start)
        if overlap > timedelta(0):
            total += hour.surplus_kwh * (overlap / _HOUR)
            counted = True
    return total if counted else None


def remaining_today_kwh(shown: list[SurplusHour], now: datetime, tz: str) -> float | None:
    """The surplus from `now` to the end of the local day; `None` when no hour of it is known."""
    zone = time_zones.require_zone(tz)
    local = now.astimezone(zone)
    midnight = datetime.combine(local.date() + timedelta(days=1), datetime.min.time(), tzinfo=zone)
    return window_kwh(shown, now, midnight.astimezone(timezone.utc) - now.astimezone(timezone.utc))
