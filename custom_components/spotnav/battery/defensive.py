"""The defensive battery plan's inputs (SpotNav 3.0, wave 6): the house and the sun as the learnt error band's
pessimistic side, where it matters. Pure: no Home Assistant, the clock is passed in.

The plan in force counts on the house's median and the sun as corrected. The defensive variant is the site's mode
planned on the same prices, battery and cars, with:

* **the house at its p90 in the dear hours**: the hours whose mean buy price is among the dearest `DEAR_SHARE` of
  the horizon's hours (`dear_hours`). A house that draws more than expected there is what costs: the battery runs
  short when the price is high. In the cheap hours a miss costs little, so the median stays.
* **the sun at its p10 in every hour**: less sun than expected leaves the battery short, whatever the hour.

The band is the site's own (`site/forecast_band.py`); a side whose band is still learning keeps its point forecast,
and with neither band there is no defensive plan. It is a shadow: planned beside the modes, reviewed the night after
on the measured load and sun, never in force and never written to any device. The backtest behind it
(`scripts/forecast_gap_backtest.py --runs ...,band=...`) is told in `plans/battery_plan_backtest/README.md`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Final

from ..planning.demand_charge import hour_start
from ..site.forecast_band import ErrorBand, quantile

#: The share of the horizon's hours, dearest first, in which the house is planned at its p90.
DEAR_SHARE: Final = 0.5

#: Why there is no defensive plan.
NO_BAND: Final = "band_learning"


@dataclass(frozen=True, slots=True)
class DefensiveInputs:
    """The defensive plan's house and sun, and what the band changed."""

    house_by_hour: dict[datetime, float]
    solar_wh_by_hour: dict[datetime, float] | None
    dear_hours: int
    #: The energy the band added to the house and took from the sun over the horizon (kWh; signed: a band whose p90
    #: lies below the median takes some away).
    house_kwh_added: float
    sun_kwh_removed: float
    house_band: bool
    sun_band: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "house_band": self.house_band,
            "sun_band": self.sun_band,
            "dear_hours": self.dear_hours,
            "house_kwh_added": round(self.house_kwh_added, 2),
            "sun_kwh_removed": round(self.sun_kwh_removed, 2),
        }


def dear_hours(
    prices: Sequence[Any], now: datetime, tz: str, share: float = DEAR_SHARE
) -> set[datetime]:
    """The local hours (their start, UTC) from the running one on whose mean buy price is among the dearest `share`
    of those hours. `prices` are the plan's price slots (`start`, `end`, `buy`)."""
    by_hour: dict[datetime, list[float]] = {}
    for price in prices:
        if price.end <= now:
            continue
        by_hour.setdefault(hour_start(price.start, tz), []).append(float(price.buy))
    if not by_hour:
        return set()
    means = {start: sum(values) / len(values) for start, values in by_hour.items()}
    cut = quantile(list(means.values()), 1.0 - share)
    return {start for start, mean in means.items() if mean >= cut}


def defensive_inputs(
    house_by_hour: Mapping[datetime, float],
    solar_wh_by_hour: Mapping[datetime, float] | None,
    house_band: ErrorBand | None,
    sun_band: ErrorBand | None,
    prices: Sequence[Any],
    now: datetime,
    tz: str,
) -> DefensiveInputs | None:
    """The house at its p90 in the dear hours and the sun at its p10 (see the module); `None` when there is nothing to
    change (no house band, and no sun band or no solar forecast). `house_by_hour` is keyed by the local hour's start, `solar_wh_by_hour` by the UTC hour's start."""
    if house_band is None and (sun_band is None or solar_wh_by_hour is None):
        return None
    house = dict(house_by_hour)
    dear: set[datetime] = set()
    added = 0.0
    if house_band is not None:
        dear = dear_hours(prices, now, tz)
        for start in dear:
            value = house.get(start)
            if value is None:
                continue
            _low, high = house_band.bounds(value, start)
            added += high - value
            house[start] = high
    solar = None if solar_wh_by_hour is None else dict(solar_wh_by_hour)
    removed = 0.0
    if solar is not None and sun_band is not None:
        for start, wh in solar.items():
            low, _high = sun_band.bounds(wh / 1000.0, start.astimezone(timezone.utc))
            removed += wh / 1000.0 - low
            solar[start] = low * 1000.0
    return DefensiveInputs(house, solar, len(dear), added, removed, house_band is not None, sun_band is not None)
