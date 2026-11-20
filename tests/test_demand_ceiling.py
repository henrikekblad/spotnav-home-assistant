"""The site's soft ceiling under its demand charge, in shadow (`site/demand_ceiling.py`): S_h and λ_h over the next
48 hours from the month's peaks and the house's own forecast, what the plans would cost against it, and the site
glue that refreshes it as its inputs move and logs the cost once a day.

Göteborg Energi's tidsindelad tariff (weekdays 07-20, November to March, mean of the 3 highest days at 135 kr/kW)
on Stockholm's clock; 2026-11-16 is a Monday.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from custom_components.spotnav.planning.auto_settings import DemandComponent
from custom_components.spotnav.planning.demand_charge import HourMean
from custom_components.spotnav.planning.demand_cost import HourCeiling
from custom_components.spotnav.planning.planner import HighLoadWindow
from custom_components.spotnav.site.demand_ceiling import (
    ceiling_state,
    HORIZON_H,
    horizon,
    p25_house,
    plan_cost,
    SiteDemandCeiling,
    with_cars,
)
from custom_components.spotnav.battery.demand_plan import peak_so_far

TZ = "Europe/Stockholm"
STOCKHOLM = ZoneInfo(TZ)
UTC = timezone.utc
GBG_WINDOW = HighLoadWindow(days="weekdays", from_month=11, to_month=3, from_hour=7, to_hour=20)
GBG_TID = DemandComponent(top_n=3, window=GBG_WINDOW, price_winter=135.0, price_summer=0.0)


def local(month: int, day: int, hour: int) -> datetime:
    return datetime(2026, month, day, hour, tzinfo=STOCKHOLM).astimezone(UTC)


def measured(day: int, hour: int, kw: float) -> HourMean:
    return HourMean(start=local(11, day, hour), kw=kw, floor_kw=kw, house_kw=kw)


NOW = local(11, 16, 6) + timedelta(minutes=30)
#: Three working days with peaks measured this month: 5, 4 and 3 kW.
MONTH = [measured(2, 18, 5.0), measured(3, 18, 4.0), measured(4, 18, 3.0), measured(5, 22, 9.0)]


def test_the_horizon_is_48_hours_within_the_running_month() -> None:
    assert len(horizon(NOW, TZ)) == HORIZON_H
    late = local(11, 30, 12)
    hours = horizon(late, TZ)
    assert hours[0] == late and hours[-1] == local(11, 30, 23)


def test_the_ceiling_is_the_third_highest_day_in_the_windows_hours_and_nothing_elsewhere() -> None:
    house = {local(11, 16, hour): 2.0 for hour in range(6, 24)}
    state = ceiling_state([GBG_TID], MONTH, [], house, NOW, TZ)
    (part,) = state.parts
    # λ = 135 / 3; T = the third highest of 5, 4, 3 and today's predicted 2 kW: 3 kW. The 9 kW night is not counted.
    assert part.marginal == pytest.approx(45.0)
    assert part.threshold_kw == pytest.approx(3.0)
    assert state.hours[local(11, 16, 9)] == HourCeiling(kw=pytest.approx(3.0), price=pytest.approx(45.0))
    assert local(11, 16, 6) not in state.hours and local(11, 16, 21) not in state.hours
    shown = state.as_dict()
    assert shown["band"] == "learning" and shown["house_quantile"] == 0.25
    assert shown["parts"][0]["measured"]["2026-11-02"] == 5.0
    # With the house's history the days beyond the forecast are its usual working-day peak, 4 kW: T rises to it.
    assert ceiling_state([GBG_TID], MONTH, MONTH, house, NOW, TZ).parts[0].threshold_kw == pytest.approx(4.0)
    assert [start for start, _kw, _price in shown["hours"]][0] == local(11, 16, 7).isoformat()


def test_a_summer_month_prices_nothing() -> None:
    july = local(7, 6, 10)
    state = ceiling_state([GBG_TID], [], [], {}, july, TZ)
    assert state.hours == {} and state.parts[0].marginal == 0


def test_the_plans_cost_is_the_days_worst_hour_over_its_ceiling() -> None:
    ceilings = {local(11, 16, 9): HourCeiling(3.0, 45.0), local(11, 16, 10): HourCeiling(3.0, 45.0)}
    planned = {local(11, 16, 9): 3.5, local(11, 16, 10): 4.0}
    (day,) = plan_cost(ceilings, planned.get, TZ)
    assert (day.at, day.peak_kw, day.cost) == (local(11, 16, 10), 4.0, pytest.approx(45.0))
    # Under the ceiling: the day's highest hour, at no cost.
    (calm,) = plan_cost(ceilings, {local(11, 16, 9): 2.0, local(11, 16, 10): 2.5}.get, TZ)
    assert (calm.at, calm.cost) == (local(11, 16, 10), 0.0)
    assert plan_cost(ceilings, lambda _start: None, TZ) == []


def test_the_cars_plans_are_in_the_predicted_peaks() -> None:
    # A car charging 11 kW from 18:30 to 20:00 today: the 18-19 hour gets half of it, 19-20 all of it.
    house = {local(11, 16, hour): 2.0 for hour in range(6, 24)}
    windows = [(local(11, 16, 18) + timedelta(minutes=30), local(11, 16, 20), 11.0)]
    loaded = with_cars(house, windows)
    assert loaded[local(11, 16, 18)] == pytest.approx(7.5) and loaded[local(11, 16, 19)] == pytest.approx(13.0)
    assert loaded[local(11, 16, 20)] == 2.0 and with_cars(house, []) == house
    # Today's predicted peak is the car's 13 kW, among the three highest days: T rises from 3 kW to 4 kW (the third
    # of 13, 5 and 4).
    state = ceiling_state([GBG_TID], MONTH, [], loaded, NOW, TZ)
    assert state.parts[0].predicted[local(11, 16, 12).astimezone(STOCKHOLM).date()] == pytest.approx(13.0)
    assert state.parts[0].threshold_kw == pytest.approx(4.0)
    # Each hour knows the day its peak counts toward.
    assert state.hours[local(11, 16, 9)].day == local(11, 16, 9).astimezone(STOCKHOLM).date()


def test_the_running_days_peak_so_far() -> None:
    month = [*MONTH, measured(16, 7, 3.5), measured(16, 8, 4.5)]
    state = ceiling_state([GBG_TID], month, [], {}, local(11, 16, 9), TZ)
    assert peak_so_far(state.parts, local(11, 16, 9), TZ) == pytest.approx(4.5)
    # A day that has only exported so far starts from 0.
    exported = ceiling_state([GBG_TID], [*MONTH, measured(16, 7, -2.0)], [], {}, local(11, 16, 9), TZ)
    assert peak_so_far(exported.parts, local(11, 16, 9), TZ) == 0.0
    # Outside the window's hours (a Sunday) no day counts: none.
    assert peak_so_far(state.parts, local(11, 15, 12), TZ) is None


def test_the_house_is_taken_down_to_its_low_quantile_only_with_a_band() -> None:
    by_hour = {local(11, 16, 9): 2.0}
    assert p25_house(by_hour, None) == by_hour
    assert p25_house(by_hour, lambda value, _start: value - 0.5) == {local(11, 16, 9): 1.5}


# ------------------------------------------------------------------ the site glue


class _Forecast:
    def __init__(self) -> None:
        self.forecast = SimpleNamespace(made_at=NOW)
        self.log = SimpleNamespace(hours=lambda _kind: {}, tz=TZ)
        self.holidays = None

    def plan_kw_by_hour(self, _now: datetime, *, heat: bool = True) -> dict[datetime, float]:
        return {local(11, 16, hour): 2.0 if not heat else 5.0 for hour in range(6, 24)}


def _site(components: tuple[DemandComponent, ...]) -> Any:
    tracker = SimpleNamespace(tz=TZ, hours=list(MONTH), previous=[], month_hours=lambda _now: list(MONTH))
    logged: list[dict[str, Any]] = []
    site = SimpleNamespace(
        hass=None,
        entry_id="site",
        demand=SimpleNamespace(tracker=tracker),
        house_forecast=_Forecast(),
        battery_shadow=SimpleNamespace(plan=None),
        _log_target=lambda charger, action, **fields: logged.append({"action": action, **fields}),
        logged=logged,
        components=components,
    )
    return site


@pytest.fixture
def patched(monkeypatch):
    def use(site: Any) -> SiteDemandCeiling:
        monkeypatch.setattr(
            "custom_components.spotnav.pricing.price_source.site_demand_components",
            lambda _hass, _entry_id: site.components,
        )
        monkeypatch.setattr("custom_components.spotnav.battery.plan_runtime.site_car_windows", lambda _hass, _site: [])
        return SiteDemandCeiling(site)

    return use


def test_the_site_refreshes_as_its_inputs_move_and_logs_the_cost_once_a_day(patched) -> None:
    site = _site((GBG_TID,))
    ceiling = patched(site)
    state = ceiling.refresh(NOW)
    assert state is not None and ceiling.reason is None
    # The plans count on 5 kW in the window's hours, a kW over the 4 kW ceiling (the usual working day's peak): 45 kr.
    (logged,) = site.logged
    assert logged["action"] == "demand_ceiling"
    monday = logged["days"][0]
    assert (monday["date"], monday["ceiling_kw"], monday["peak_kw"], monday["cost"]) == ("2026-11-16", 4.0, 5.0, 45.0)
    # The same inputs: the same state, nothing logged again.
    assert ceiling.refresh(NOW + timedelta(minutes=5)) is state
    site.demand.tracker.hours.append(measured(16, 6, 1.0))
    assert ceiling.refresh(NOW + timedelta(minutes=6)) is not state
    assert len(site.logged) == 1
    shown = ceiling.as_dict()
    assert shown["state"] == "shadow" and shown["plan_cost"][0]["cost"] == 45.0 and shown["hours"]


def test_a_site_without_parts_has_none_and_says_why(patched) -> None:
    ceiling = patched(_site(()))
    assert ceiling.refresh(NOW) is None
    assert ceiling.as_dict() == {"state": "shadow", "reason": "no_parts", "plan_cost": []}


async def test_a_measuring_site_shows_it_in_its_diagnostics_and_the_house_answer(hass, freezer, offline_relay) -> None:
    from custom_components.spotnav.api.house import house_answer
    from custom_components.spotnav.diagnostics import entry_diagnostics

    from .test_demand_site import pilot_site, run

    site = await pilot_site(hass, freezer, import_target_kw=6.0)
    await run(hass, freezer, 60, 4000.0)
    shown = entry_diagnostics(hass, site)["demand_ceiling"]
    # The pilot's day part (the highest hour, 23 kr/kW in September) and night part (8 kr/kW): every hour priced.
    assert shown["state"] == "shadow" and shown["reason"] is None
    assert len(shown["hours"]) == 48 and {price for _start, _kw, price in shown["hours"]} == {23.0, 8.0}
    assert house_answer(hass, site)["demand_ceiling"]["hours"] == shown["hours"]
