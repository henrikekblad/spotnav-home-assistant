"""The site's soft ceiling under its demand charge, in shadow (plan v3_power_tariff §3, step 4): S_h and λ_h for the
next `HORIZON_H` hours, worked out from the month's measured peaks and the house's own forecast
(`planning/demand_cost.py`). No planner reads it yet: it is shown in the diagnostics and the house answer, and once a
day the debug log says what the plans as made would cost against it.

* **Inputs.** The demand-charge parts of the site's electricity price (`site_demand_components`, holidays set), the
  month's hours the tracker keeps (each at its floor, `demand_cost.part_cost`), the house's history (the tracker's
  two months, `HourMean.house_kw`: the import less the chargers) and the house forecast the import target counts on
  without the heat relay's change (`SiteHouseForecast.plan_kw_by_hour(heat=False)`), taken down to its
  `PREDICTED_QUANTILE` (p25) with the house forecast's learnt error band at that quantile (while no band is learnt,
  the forecast itself), plus the cars' installed plans (`with_cars`): a fixed load the battery plan never moves, so
  a day a car will charge into its peak anyway is known as such and its hours are not shaved for nothing. Beyond
  the forecast, the cars' charges their weekday habit predicts (`planning/car_days.py`, from the session store's
  charges of the last four weeks, each plugged in to the charger's daily departure) at the least power each must
  take in the counted hours.
* **Hours.** The running hour on, `HORIZON_H` of them, within the running month only: a later month's hours start
  from nothing and get their ceiling once it begins (`threshold` is the month's).
* **Refreshed** when the inputs move: a new hour, a completed hour, a new house forecast, a change of the cars'
  plans or of the parts (`SiteDemandCeiling.refresh`, from the site's measurement tick); never more than once per
  hour otherwise.
* **The plans against it** (`plan_cost`): per day of the horizon, the highest hour of what the plans as made import
  (the house forecast as the plans count on it, the cars' installed plans and the battery's planned grid charge, in
  shadow) among the hours with a ceiling, and what raising the day's peak there would cost (`raise_cost`). Logged
  once per local day in the site's change log (`demand_ceiling`), kept for the debug bundle.

* **The car's ceiling** (`car_inputs`): the same ceiling made without the asking charger's own installed plan (its
  plan is what is being made), with the house's forecast as the car's plan counts on it and the other cars' plans per
  hour, for the car's plan under the ceiling in shadow (`planning/car_demand.py`). Made again only when the site's
  ceiling or the other cars' plans moved (one per charger kept): it runs on the event loop, beside every plan.

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
from ..planning.car_days import Charge, plugged_window, predicted_charges, WEEKS
from ..planning.car_demand import CarCeiling
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
    #: The house forecast the predicted peaks were taken from (kW, at `PREDICTED_QUANTILE`, the cars' plans added),
    #: and whether a learnt band took it down to that quantile.
    house: Mapping[datetime, float]
    band: bool
    #: The cars' charges predicted beyond the forecast (`planning/car_days.py`).
    charges: tuple[Charge, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "made_at": self.made_at.isoformat(),
            "month": f"{self.month[0]:04d}-{self.month[1]:02d}",
            "house_quantile": PREDICTED_QUANTILE,
            "band": "learnt" if self.band else "learning",
            "parts": [_part_dict(part) for part in self.parts],
            "car_charges": [charge.as_dict() for charge in self.charges],
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
    charges: Sequence[Charge] = (),
) -> CeilingState:
    """The soft ceiling over the horizon from the parts, the month's hours, the house's history, its forecast at
    `PREDICTED_QUANTILE` and the cars' predicted `charges` (see the module). Pure."""
    parts = tuple(
        part_cost(component, month_hours, house_by_hour, history, now, tz, charges) for component in components
    )
    starts = horizon(now, tz)
    return CeilingState(
        made_at=now,
        month=month_of(hour_start(now, tz), tz),
        parts=parts,
        hours=soft_ceiling(parts, starts, tz),
        house=dict(house_by_hour),
        band=band,
        charges=tuple(charges),
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


def with_cars(
    by_hour: Mapping[datetime, float], windows: Sequence[tuple[datetime, datetime, float]]
) -> dict[datetime, float]:
    """The house forecast per hour with the cars' planned charge in that hour added (`(start, end, kW)` windows)."""
    from ..battery.plan_shadow import car_kw_in  # noqa: PLC0415 - it imports the battery plan

    if not windows:
        return dict(by_hour)
    return {start: kw + car_kw_in(start, start + HOUR, windows) for start, kw in by_hour.items()}


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
    #: The cars' predicted charges, made once per hour start (`_predicted_charges`).
    _charges: tuple[datetime, tuple[Charge, ...]] | None = None
    #: Per charger, the ceiling made without its own installed plan and what it was made from (`_car_inputs`).
    _without: dict[str, tuple[Any, CeilingState]] = field(default_factory=dict)

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
        windows = _car_windows(site)
        charges = self._predicted_charges(now, tz)
        key = (components, hour_start(now, tz), len(tracker.hours), made, tuple(windows), charges, tz)
        if key == self._key:
            return self.state
        self._key = key
        if not components:
            self.state, self.reason = None, "no_parts"
            return None
        self.state = self._make(components, windows, charges, now)
        self.reason = None
        self._log_day(now)
        return self.state

    def _make(
        self,
        components: Sequence[DemandComponent],
        windows: Sequence[tuple[datetime, datetime, float]],
        charges: Sequence[Charge],
        now: datetime,
    ) -> CeilingState:
        site = self.site
        tracker = site.demand.tracker
        forecast = site.house_forecast
        low = _band_low(forecast, now)
        house = with_cars(p25_house(forecast.plan_kw_by_hour(now, heat=False), low), windows)
        return ceiling_state(
            components,
            tracker.month_hours(now),
            [*tracker.previous, *tracker.hours],
            house,
            now,
            tracker.tz,
            band=low is not None,
            charges=charges,
        )

    def _predicted_charges(self, now: datetime, tz: str) -> tuple[Charge, ...]:
        """The cars' charges predicted for the rest of the month, made again once per hour."""
        current = hour_start(now, tz)
        if self._charges is not None and self._charges[0] == current:
            return self._charges[1]
        try:
            history = past_charges(self.site, now)
        except Exception:  # noqa: BLE001 - no history read: no car day predicted
            _LOGGER.debug("SpotNav site demand ceiling: the cars' charges could not be read", exc_info=True)
            history = []
        found = tuple(predicted_charges(history, now, last_day_of_month(now, tz), tz))
        self._charges = (current, found)
        return found

    def car_inputs(self, charger_entry_id: str, now: datetime) -> CarCeiling | None:
        """What `charger_entry_id`'s plan under the ceiling is held under (see the module), `None` without a priced
        ceiling. Never raises."""
        try:
            return self._car_inputs(charger_entry_id, now)
        except Exception:  # noqa: BLE001 - a shadow figure never disturbs the plan
            _LOGGER.debug("SpotNav site demand ceiling: no ceiling for the car's plan", exc_info=True)
            return None

    def _car_inputs(self, charger_entry_id: str, now: datetime) -> CarCeiling | None:
        from ..battery.plan_shadow import car_kw_in  # noqa: PLC0415 - it imports the battery plan
        from ..pricing.price_source import site_demand_components  # noqa: PLC0415 - it imports the planner

        state = self.refresh(now)
        if state is None or not any(ceiling.price > 0 for ceiling in state.hours.values()):
            return None
        site = self.site
        tz = site.demand.tracker.tz
        others = _car_windows(site, exclude=charger_entry_id)
        if len(others) != len(_car_windows(site)):
            # The car's own installed plan is in the predicted peaks: made again without it, once per site ceiling and
            # other cars' plans.
            key = (self._key, tuple(others))
            kept = self._without.get(charger_entry_id)
            if kept is not None and kept[0] == key:
                state = kept[1]
            else:
                components = site_demand_components(site.hass, site.entry_id)
                state = self._make(components, others, state.charges, now)
                self._without[charger_entry_id] = (key, state)
        return CarCeiling(
            ceilings={start: ceiling for start, ceiling in state.hours.items() if ceiling.price > 0},
            house_kw=site.house_forecast.plan_kw_by_hour(now),
            others_kw={start: car_kw_in(start, start + HOUR, others) for start in state.hours},
            tz=tz,
        )

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


def _car_windows(site: Any, exclude: str | None = None) -> list[tuple[datetime, datetime, float]]:
    """The cars' installed plans as `(start, end, kW)` windows (all but `exclude`'s); none where they cannot be
    read."""
    from ..battery.plan_runtime import site_car_windows  # noqa: PLC0415 - it imports the site

    try:
        if exclude is None:
            return site_car_windows(site.hass, site)
        return site_car_windows(site.hass, site, exclude=exclude)
    except Exception:  # noqa: BLE001 - no car plan: none counted
        return []


def last_day_of_month(now: datetime, tz: str) -> date:
    """The running month's last local day."""
    year, month = month_of(hour_start(now, tz), tz)
    return date(year + month // 12, month % 12 + 1, 1) - timedelta(days=1)


def session_max_kw(session: Any) -> float:
    """The most a stored charge drew over one of its price intervals (kW), else its mean over the charge."""
    best = 0.0
    for piece in getattr(session, "intervals", ()) or ():
        hours = (piece.end - piece.start).total_seconds() / 3600.0
        if hours > 0:
            best = max(best, piece.kwh / hours)
    if best <= 0 and session.end is not None:
        hours = (session.end - session.start).total_seconds() / 3600.0
        best = session.energy_kwh / hours if hours > 0 else 0.0
    return best


def past_charges(site: Any, now: datetime) -> list[Charge]:
    """The site's chargers' finished charges of the last `WEEKS` weeks as `car_days.Charge`s: plugged in (else
    started) to the charger's daily departure after it, else to the charge's end."""
    from ..const import CONF_CHARGER_ENTRY_IDS  # noqa: PLC0415
    from ..runtime import domain_data, preview_for  # noqa: PLC0415 - the charger side imports the site

    store = domain_data(site.hass).session_store
    if store is None:
        return []
    tz = site.demand.tracker.tz
    since = now - timedelta(days=7 * WEEKS + 2)
    found: list[Charge] = []
    for charger_entry_id in site.config.get(CONF_CHARGER_ENTRY_IDS) or []:
        preview = preview_for(site.hass, charger_entry_id)
        departure = None if preview is None else preview.daily_departure()
        for session in store.closed_raw(charger_entry_id):
            if session.end is None or session.end < since or session.energy_kwh <= 0:
                continue
            plugged, leaves = plugged_window(session.plugged_in_at or session.start, session.end, departure, tz)
            max_kw = session_max_kw(session)
            if max_kw > 0:
                found.append(Charge(plugged, leaves, session.energy_kwh, max_kw))
    return found


def planned_import(site: Any, now: datetime) -> Callable[[datetime], float | None]:
    """What the plans as made import in the hour starting at `start` (kW): the house forecast as the plans count on
    it, the cars' installed plans and the battery's planned grid charge; `None` for an hour the forecast does not
    know."""
    from ..battery.plan_shadow import car_kw_in  # noqa: PLC0415
    from ..heat.runtime import battery_charge_by_hour  # noqa: PLC0415

    house = site.house_forecast.plan_kw_by_hour(now)
    windows = _car_windows(site)
    plan = getattr(site.battery_shadow, "plan", None)
    battery = battery_charge_by_hour(getattr(plan, "plan", ()) or ())

    def at(start: datetime) -> float | None:
        kw = house.get(start)
        if kw is None:
            return None
        return kw + car_kw_in(start, start + HOUR, windows) + battery.get(start, 0.0)

    return at

