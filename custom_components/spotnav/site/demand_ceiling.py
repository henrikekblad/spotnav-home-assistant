"""The site's soft ceiling under its demand charge, in shadow (plan v3_power_tariff §3, step 4): S_h and λ_h for the
next `HORIZON_H` hours, worked out from the month's measured peaks and the house's own forecast
(`planning/demand_cost.py`). No planner reads it yet: it is shown in the diagnostics and the house answer, and once a
day the debug log says what the plans as made would cost against it.

* **Inputs.** The demand-charge parts of the site's electricity price (`site_demand_components`, holidays set), the
  month's hours the tracker keeps (each at its floor, `demand_cost.part_cost`), the house's history (the tracker's
  two months, `HourMean.house_kw`: the import less the chargers) and the house forecast the import target counts on
  without the heat relay's change (`SiteHouseForecast.plan_kw_by_hour(heat=False)`), taken down to its
  `PREDICTED_QUANTILE` (p25) with the house forecast's learnt error band at that quantile; while no band is learnt,
  the forecast itself.
* **Hours.** The running hour on, `HORIZON_H` of them, within the running month only: a later month's hours start
  from nothing and get their ceiling once it begins (`threshold` is the month's).
* **Refreshed** when the inputs move: a new hour, a completed hour, a new house forecast or a change of the parts
  (`SiteDemandCeiling.refresh`, from the site's measurement tick); never more than once per hour otherwise.
* **The plans against it** (`plan_cost`): per day of the horizon, the highest hour of what the plans as made import
  (the house forecast as the plans count on it, the cars' installed plans and the battery's planned grid charge, in
  shadow) among the hours with a ceiling, and what raising the day's peak there would cost (`raise_cost`). Logged
  once per local day in the site's change log (`demand_ceiling`), kept for the debug bundle.

Nothing here decides anything a charger, battery or heat pump is told.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
import logging
from typing import Any, Final

from .. import time_zones
from ..planning.auto_settings import DemandComponent
from ..planning.demand_charge import hour_start, HourMean, month_of
from ..planning.demand_cost import (
    HourCeiling,
    part_cost,
    PartCost,
    PREDICTED_QUANTILE,
    raise_cost,
    soft_ceiling,
)

_LOGGER = logging.getLogger(__name__)

#: How many hours ahead the ceiling is worked out, the running one included.
HORIZON_H: Final = 48
HOUR: Final = timedelta(hours=1)


@dataclass(frozen=True, slots=True)
class CeilingState:
    """The site's soft ceiling at one moment (see the module)."""

    made_at: datetime
    month: tuple[int, int]
    parts: tuple[PartCost, ...]
    #: Per hour start (UTC) with a priced part counting it: S_h (kW) and λ_h (the area's major unit per kW).
    hours: Mapping[datetime, HourCeiling]
    #: The house forecast the predicted peaks were taken from (kW, at `PREDICTED_QUANTILE`), and whether a learnt
    #: band took it down to that quantile.
    house: Mapping[datetime, float]
    band: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "made_at": self.made_at.isoformat(),
            "month": f"{self.month[0]:04d}-{self.month[1]:02d}",
            "house_quantile": PREDICTED_QUANTILE,
            "band": "learnt" if self.band else "learning",
            "parts": [_part_dict(part) for part in self.parts],
            # Per hour: [start, S_h (kW), λ_h (per kW)].
            "hours": [
                [start.isoformat(), round(ceiling.kw, 3), round(ceiling.price, 3)]
                for start, ceiling in sorted(self.hours.items())
            ],
        }


def _part_dict(part: PartCost) -> dict[str, Any]:
    return {
        **part.component.as_dict(),
        "marginal": round(part.marginal, 3),
        "threshold_kw": None if part.threshold_kw is None else round(part.threshold_kw, 3),
        "free_kw": None if part.free_kw is None else round(part.free_kw, 3),
        "measured": {day.isoformat(): round(kw, 3) for day, kw in sorted(part.measured.items())},
        "predicted": {day.isoformat(): round(kw, 3) for day, kw in sorted(part.predicted.items())},
    }


def horizon(now: datetime, tz: str) -> list[datetime]:
    """The hour starts from the running one on, `HORIZON_H` of them, within the running month."""
    first = hour_start(now, tz)
    month = month_of(first, tz)
    return [start for start in (first + HOUR * offset for offset in range(HORIZON_H)) if month_of(start, tz) == month]


def ceiling_state(
    components: Sequence[DemandComponent],
    month_hours: Sequence[HourMean],
    history: Sequence[HourMean],
    house_by_hour: Mapping[datetime, float],
    now: datetime,
    tz: str,
    *,
    band: bool = False,
) -> CeilingState:
    """The soft ceiling over the horizon from the parts, the month's hours, the house's history and its forecast at
    `PREDICTED_QUANTILE` (see the module). Pure."""
    parts = tuple(part_cost(component, month_hours, house_by_hour, history, now, tz) for component in components)
    starts = horizon(now, tz)
    return CeilingState(
        made_at=now,
        month=month_of(hour_start(now, tz), tz),
        parts=parts,
        hours=soft_ceiling(parts, starts, tz),
        house=dict(house_by_hour),
        band=band,
    )


@dataclass(frozen=True, slots=True)
class DayCost:
    """One day of the horizon: the plans' highest import among its hours with a ceiling, and what it would cost."""

    day: date
    at: datetime
    peak_kw: float
    ceiling_kw: float
    price: float
    cost: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "date": self.day.isoformat(),
            "at": self.at.isoformat(),
            "peak_kw": round(self.peak_kw, 3),
            "ceiling_kw": round(self.ceiling_kw, 3),
            "price": round(self.price, 3),
            "cost": round(self.cost, 2),
        }


def plan_cost(
    ceilings: Mapping[datetime, HourCeiling], planned_kw: Callable[[datetime], float | None], tz: str
) -> list[DayCost]:
    """Per local day, the hour with a ceiling where the plans' import (`planned_kw`, `None`: not known) raises the
    day's peak the most, and what that costs (0 when it stays under). Days without a known hour are left out."""
    zone = time_zones.require_zone(tz)
    best: dict[date, DayCost] = {}
    for start, ceiling in sorted(ceilings.items()):
        kw = planned_kw(start)
        if kw is None:
            continue
        day = start.astimezone(zone).date()
        cost = raise_cost(ceiling, kw)
        found = best.get(day)
        if found is None or (cost, kw - ceiling.kw) > (found.cost, found.peak_kw - found.ceiling_kw):
            best[day] = DayCost(day, start, kw, ceiling.kw, ceiling.price, cost)
    return [best[day] for day in sorted(best)]


def p25_house(
    by_hour: Mapping[datetime, float], low: Callable[[float, datetime], float] | None
) -> dict[datetime, float]:
    """The forecast at its low quantile where a band is learnt (`low(value, start)`), else as it is."""
    if low is None:
        return dict(by_hour)
    return {start: low(kw, start) for start, kw in by_hour.items()}


@dataclass(slots=True)
class SiteDemandCeiling:
    """The soft ceiling kept for one site (see the module). `site` is its `SiteCapacityController`."""

    site: Any
    state: CeilingState | None = None
    #: Why there is none (`no_parts`, `failed`), `None` while there is one.
    reason: str | None = "not_made"
    _key: Any = None
    _logged_day: date | None = None
    last_cost: list[DayCost] = field(default_factory=list)

    def refresh(self, now: datetime) -> CeilingState | None:
        """Work the ceiling out again when its inputs moved (see the module). Never raises."""
        try:
            return self._refresh(now)
        except Exception:  # noqa: BLE001 - a shadow figure never disturbs the site
            _LOGGER.debug("SpotNav site demand ceiling: could not be worked out", exc_info=True)
            self.state, self.reason, self._key = None, "failed", None
            return None

    def _refresh(self, now: datetime) -> CeilingState | None:
        from ..pricing.price_source import site_demand_components  # noqa: PLC0415 - it imports the planner

        site = self.site
        tracker = site.demand.tracker
        tz = tracker.tz
        components = site_demand_components(site.hass, site.entry_id)
        forecast = site.house_forecast
        made = getattr(getattr(forecast, "forecast", None), "made_at", None)
        key = (components, hour_start(now, tz), len(tracker.hours), made, tz)
        if key == self._key:
            return self.state
        self._key = key
        if not components:
            self.state, self.reason = None, "no_parts"
            return None
        low = _band_low(forecast, now)
        house = p25_house(forecast.plan_kw_by_hour(now, heat=False), low)
        self.state = ceiling_state(
            components,
            tracker.month_hours(now),
            [*tracker.previous, *tracker.hours],
            house,
            now,
            tz,
            band=low is not None,
        )
        self.reason = None
        self._log_day(now)
        return self.state

    def _log_day(self, now: datetime) -> None:
        """Once per local day: what the plans as made would cost against the ceiling, in the site's change log."""
        state = self.state
        if state is None or not state.hours:
            return
        tz = self.site.demand.tracker.tz
        today = now.astimezone(time_zones.require_zone(tz)).date()
        if self._logged_day == today:
            return
        self._logged_day = today
        self.last_cost = plan_cost(state.hours, planned_import(self.site, now), tz)
        self.site._log_target(  # noqa: SLF001 - the site's own debug log
            None,
            "demand_ceiling",
            days=[cost.as_dict() for cost in self.last_cost],
            parts=[
                {"threshold_kw": part["threshold_kw"], "marginal": part["marginal"]}
                for part in (_part_dict(part) for part in state.parts)
            ],
        )

    def as_dict(self) -> dict[str, Any]:
        """The diagnostics' and the house answer's `demand_ceiling`: `{state: "shadow", reason, ...the state,
        plan_cost}`."""
        found: dict[str, Any] = {"state": "shadow", "reason": self.reason}
        if self.state is not None:
            found.update(self.state.as_dict())
        found["plan_cost"] = [cost.as_dict() for cost in self.last_cost]
        return found


def _band_low(forecast: Any, now: datetime) -> Callable[[float, datetime], float] | None:
    """The house forecast's error band at `PREDICTED_QUANTILE` (its low figure), `None` while none is learnt."""
    from .forecast_band import FORM_ADDITIVE, HIGH_Q, learn, pairs_from_log  # noqa: PLC0415
    from .forecast_log import KIND_HOUSE_MEDIAN  # noqa: PLC0415

    band = learn(
        pairs_from_log(forecast.log.hours(KIND_HOUSE_MEDIAN)),
        now,
        forecast.log.tz,
        FORM_ADDITIVE,
        holidays=forecast.holidays,
        quantiles=(PREDICTED_QUANTILE, HIGH_Q),
    )
    if band is None:
        return None
    return lambda value, start: band.bounds(value, start)[0]


def planned_import(site: Any, now: datetime) -> Callable[[datetime], float | None]:
    """What the plans as made import in the hour starting at `start` (kW): the house forecast as the plans count on
    it, the cars' installed plans and the battery's planned grid charge; `None` for an hour the forecast does not
    know."""
    from ..battery.plan_runtime import site_car_windows  # noqa: PLC0415 - it imports the site
    from ..battery.plan_shadow import car_kw_in  # noqa: PLC0415
    from ..heat.runtime import battery_charge_by_hour  # noqa: PLC0415

    house = site.house_forecast.plan_kw_by_hour(now)
    try:
        windows = site_car_windows(site.hass, site)
    except Exception:  # noqa: BLE001 - no car plan: none counted
        windows = []
    plan = getattr(site.battery_shadow, "plan", None)
    battery = battery_charge_by_hour(getattr(plan, "plan", ()) or ())

    def at(start: datetime) -> float | None:
        kw = house.get(start)
        if kw is None:
            return None
        return kw + car_kw_in(start, start + HOUR, windows) + battery.get(start, 0.0)

    return at

