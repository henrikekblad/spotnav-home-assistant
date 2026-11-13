"""The house card's view (`site/house_view.py`): the hours past and ahead, the reviews, the battery's money side by
side, and the contract fixtures the card reads (`tests/fixtures/house/`, `SPOTNAV_WRITE_FIXTURES=1` rewrites them).

The world: a Swedish villa on 13 November 2026 at 17:20 local time, a week of forecasts logged and reviewed, a small
array in November sun and a 40 kWh battery running its own optimiser, planned beside it in shadow.
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from custom_components.spotnav.battery.plan_model import (
    ACTION_DISCHARGE_HOUSE,
    ACTION_EXPORT_DISCHARGE,
    ACTION_GRID_CHARGE,
    ACTION_HOLD,
    ACTION_SELF_CONSUME,
)
from custom_components.spotnav.battery.plan_shadow import KIND_BATTERY, DayReview, PlannedSlot, Run
from custom_components.spotnav.planning.demand_charge import HourMean, hour_start
from custom_components.spotnav.site.forecast_log import (
    KIND_HOUSE,
    KIND_SUN,
    KIND_SUN_CORRECTED,
    ForecastLog,
    LoggedHour,
)
from custom_components.spotnav.site.house_view import (
    AHEAD_HOURS,
    HOUR_KEYS,
    HOUSE_KEYS,
    PAST_HOURS,
    BatteryInputs,
    HouseInputs,
    HouseSources,
    SunInputs,
    hour_actions,
    house_view,
)
from custom_components.spotnav.site.load_forecast import forecast_house_load

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "house"
WRITE = os.environ.get("SPOTNAV_WRITE_FIXTURES") == "1"
TZ = "Europe/Stockholm"
CET = timedelta(hours=1)
NOW = datetime(2026, 11, 13, 17, 20, tzinfo=timezone.utc) - CET
SITE = "site_villa"


def _fixture(name: str, payload: dict[str, Any]) -> None:
    path = FIXTURE_DIR / name
    payload = json.loads(json.dumps(payload))
    if WRITE:
        FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    assert json.loads(path.read_text(encoding="utf-8")) == payload, f"{name} drifted from the backend"


def local_hour(start: datetime) -> int:
    return (start + CET).hour


def house_kw(start: datetime) -> float:
    """A villa with a heat pump: about 0.9 kW at night, a morning bump and an evening peak, a little day to day."""
    hour = local_hour(start)
    day = start.day
    base = 0.9 + 0.05 * (day % 3)
    if 6 <= hour <= 8:
        base += 0.9
    if 16 <= hour <= 20:
        base += 1.6 + 0.1 * (day % 2)
    return round(base, 3)


def sun_kwh(start: datetime) -> float:
    """A 6 kWp array in November: a short low day from 09 to 15."""
    hour = local_hour(start)
    if not 8 <= hour <= 15:
        return 0.0
    return round(max(0.0, 1.6 - 0.4 * abs(hour - 12)) * (0.6 + 0.1 * (start.day % 4)), 3)


def history(now: datetime, days: int = 21) -> list[HourMean]:
    first = hour_start(now, TZ) - timedelta(days=days)
    rows = []
    start = first
    while start < hour_start(now, TZ):
        rows.append(HourMean(start=start, kw=house_kw(start), house_kw=house_kw(start)))
        start += timedelta(hours=1)
    return rows


def seeded_log(now: datetime, *, sun: bool, battery: bool) -> ForecastLog:
    """Eight days of first forecasts and outcomes, reviewed, and the hours ahead logged as the forecasts were made."""
    log = ForecastLog(TZ)
    current = hour_start(now, TZ)
    start = current - timedelta(days=8)
    while start < current + timedelta(hours=AHEAD_HOURS):
        made = start - timedelta(hours=6)
        past = start < current
        rows = [(KIND_HOUSE, house_kw(start) * (1.12 if local_hour(start) >= 22 else 0.94), house_kw(start))]
        if sun:
            rows.append((KIND_SUN, sun_kwh(start) * 1.35, sun_kwh(start)))
            rows.append((KIND_SUN_CORRECTED, sun_kwh(start) * 1.08, sun_kwh(start)))
        if battery:
            planned = max(15.0, 75.0 - 2.2 * ((local_hour(start) - 5) % 24))
            rows.append((KIND_BATTERY, planned, planned - 2.5))
        for kind, forecast, actual in rows:
            body = log._kind(kind)  # noqa: SLF001
            assert body is not None
            body.hours[start] = LoggedHour(round(forecast, 3), made, round(actual, 3) if past else None)
        start += timedelta(hours=1)
    log.review(now)
    return log


def battery_plan(now: datetime) -> tuple[PlannedSlot, ...]:
    """The plan in force: quarter hours from now to tomorrow night; grid charging 02-05, selling tonight 18-20 and holding
    the next afternoon, covering the house otherwise."""
    slots = []
    start = now.replace(minute=15, second=0, microsecond=0)
    end_of_plan = hour_start(now, TZ) + timedelta(hours=30)
    soc = 48.3
    while start < end_of_plan:
        hour = local_hour(start)
        if 2 <= hour < 5:
            action, kw = ACTION_GRID_CHARGE, 9.0
        elif hour in (18, 19) and start.day == 13:
            action, kw = ACTION_EXPORT_DISCHARGE, -6.0
        elif 13 <= hour < 17 and start.day == 14:
            action, kw = ACTION_HOLD, 0.0
        elif 9 <= hour < 13:
            action, kw = ACTION_SELF_CONSUME, 0.4
        else:
            action, kw = ACTION_DISCHARGE_HOUSE, -house_kw(start)
        soc = min(100.0, max(10.0, soc + kw * 0.25 / 40.3 * 100))
        slots.append(PlannedSlot(start, start + timedelta(minutes=15), action, kw, round(soc, 2)))
        start += timedelta(minutes=15)
    return tuple(slots)


def battery_reviews(now: datetime) -> list[DayReview]:
    reviews = []
    today = (now + CET).date()
    for back in range(7, 0, -1):
        day = today - timedelta(days=back)
        sc = 4200.0 + 150 * (back % 3)
        reviews.append(
            DayReview(
                day=day,
                reviewed_at=datetime.combine(day + timedelta(days=1), datetime.min.time(), timezone.utc),
                quarters=96,
                covered=96,
                unplanned=0,
                start_soc_pct=40.0,
                end_value=120.0,
                usable_kwh=40.3,
                actual=Run(sc - 610.0 - 20 * back, 40.0),
                plan=Run(sc - 690.0 - 10 * back, 40.0),
                self_consumption=Run(sc, 40.0),
                modes={"balanced": Run(sc - 690.0 - 10 * back, 40.0), "max_profit": Run(sc - 735.0, 40.0)},
                soc_mae=4.2,
                mode="balanced",
            )
        )
    return reviews


def full_inputs(now: datetime = NOW) -> HouseInputs:
    rows = history(now)
    forecast = forecast_house_load(rows, now, TZ, flat_kw=1.2)
    current = hour_start(now, TZ)
    ahead = [current + timedelta(hours=offset) for offset in range(AHEAD_HOURS)]
    return HouseInputs(
        site_id=SITE,
        now=now,
        tz=TZ,
        log=seeded_log(now, sun=True, battery=True),
        forecast=forecast,
        house_actual={row.start: row.house_kw for row in rows if row.house_kw is not None},
        sources=HouseSources(
            weather_entity="weather.forecast_home",
            weather="ok",
            outdoor_source="sensor",
            outdoor_sensor="sensor.nibe_bt1_outdoor_temperature",
            holiday_calendar="calendar.sverige",
        ),
        sun=SunInputs(
            status="active",
            raw={start: round(sun_kwh(start) * 1.3, 3) for start in ahead},
            corrected={start: round(sun_kwh(start) * 1.05, 3) for start in ahead},
            forecast_names=("Forecast.Solar",),
        ),
        battery=BatteryInputs(
            brand="sigenergy",
            label="Sigenergy SigenStor",
            soc_pct=48.3,
            usable_kwh=40.3,
            management="self_managed",
            status="planned",
            mode="balanced",
            made_at=now - timedelta(minutes=20),
            horizon_end=current + timedelta(hours=30),
            plan=battery_plan(now),
            mode_views={
                "self_consumption": {"savings": 0.0},
                "balanced": {"savings": 812.4},
                "max_profit": {"savings": 905.15},
            },
            reviews=battery_reviews(now),
            estimates=("min_soc_assumed", "house_counts_battery"),
        ),
        money=("SEK", "kr", "öre"),
    )


def test_the_full_site_and_its_fixture() -> None:
    view = house_view(full_inputs())
    assert tuple(view) == HOUSE_KEYS
    hours = view["hours"]
    assert len(hours) == PAST_HOURS + AHEAD_HOURS
    assert all(tuple(row) == HOUR_KEYS for row in hours)
    current = hour_start(NOW, TZ).isoformat()
    index = [row["start"] for row in hours].index(current)
    assert index == PAST_HOURS
    past, ahead = hours[:index], hours[index:]
    # A past hour: the outcome beside the first forecast (the logged one), and no planned action.
    assert all(row["house_kw"] is not None and row["house_forecast_kw"] is not None for row in past)
    assert all(row["soc_pct"] is not None and row["battery_action"] is None for row in past)
    assert past[0]["house_forecast_kw"] == round(house_kw(datetime.fromisoformat(past[0]["start"])) * 0.94, 3)
    # From the running hour on: the latest forecasts and the plan, no outcome.
    assert all(row["house_kw"] is None and row["sun_kwh"] is None and row["soc_pct"] is None for row in ahead)
    assert all(row["house_forecast_kw"] is not None and row["soc_plan_pct"] is not None for row in ahead[1:])
    actions = {row["battery_action"] for row in ahead}
    assert {ACTION_GRID_CHARGE, ACTION_EXPORT_DISCHARGE, ACTION_HOLD} <= actions
    assert view["money"] == {"currency": "SEK", "major_unit": "kr", "minor_unit": "öre"}
    assert view["house"]["review"]["hours"] == 7 * 24 and view["house"]["counts_battery"] is True
    sun = view["sun"]
    assert sun["review"]["mae"] > sun["review_corrected"]["mae"] > 0
    battery = view["battery"]
    assert battery["review"]["days"] == 7
    assert battery["review"]["plan_saving"] > battery["review"]["own_saving"] > 0
    assert (
        battery["modes"]["balanced"]["reviewed_days"] == 7
        and battery["modes"]["self_consumption"]["reviewed_saving"] is None
    )
    assert battery["modes"]["max_profit"]["planned_saving"] == 905.15
    assert battery["estimates"] == ["house_counts_battery", "min_soc_assumed"]
    days = view["days"]
    assert len(days) == 7 and days[0]["date"] > days[-1]["date"]
    assert all(day["house_mae"] is not None and day["plan_saving"] is not None for day in days)
    assert view["sources"]["solar_forecasts"] == ["Forecast.Solar"]
    _fixture("villa_battery_sun.json", view)


def test_a_house_alone_still_learning() -> None:
    """No sun, no battery, and the forecast only two days old: nothing reviewed yet, the hours ahead forecast."""
    rows = history(NOW, days=2)
    log = ForecastLog(TZ)
    forecast = forecast_house_load(rows, NOW, TZ, flat_kw=1.0)
    log.record_forecast(KIND_HOUSE, forecast.by_hour(), NOW - timedelta(minutes=20))
    view = house_view(
        HouseInputs(
            site_id=SITE,
            now=NOW,
            tz=TZ,
            log=log,
            forecast=forecast,
            house_actual={row.start: row.house_kw for row in rows if row.house_kw is not None},
            sources=HouseSources(weather_entity="weather.forecast_home", weather="ok", outdoor_source="weather"),
            money=("SEK", "kr", "öre"),
        )
    )
    assert view["sun"] is None and view["battery"] is None and view["days"] == []
    assert view["house"]["review"] is None and view["house"]["counts_battery"] is False
    hours = view["hours"]
    # The past hours have their outcome but no logged forecast (none was made for them).
    assert all(row["house_kw"] is not None and row["house_forecast_kw"] is None for row in hours[:PAST_HOURS])
    assert all(row["sun_forecast_kwh"] is None and row["soc_plan_pct"] is None for row in hours)
    assert view["sources"]["holiday_calendar"] is None and view["sources"]["solar_forecasts"] == []
    _fixture("house_learning.json", view)


def test_the_strongest_action_in_an_hour() -> None:
    start = hour_start(NOW, TZ) + timedelta(hours=2)
    quarter = timedelta(minutes=15)
    slots = [
        PlannedSlot(start, start + quarter, ACTION_HOLD, 0.0, 50.0),
        PlannedSlot(start + quarter, start + 2 * quarter, ACTION_GRID_CHARGE, 5.0, 53.0),
        PlannedSlot(start + 2 * quarter, start + 3 * quarter, ACTION_EXPORT_DISCHARGE, -5.0, 50.0),
        PlannedSlot(start + 4 * quarter, start + 5 * quarter, ACTION_DISCHARGE_HOUSE, -1.0, 49.0),
    ]
    assert hour_actions(slots, TZ) == {start: ACTION_GRID_CHARGE}


def test_the_hours_follow_the_local_clock_over_the_autumn_change() -> None:
    """On the night clocks go back the window is still 36 one-hour rows, an hour apart in real time."""
    now = datetime(2026, 10, 25, 3, 30, tzinfo=timezone.utc)
    view = house_view(
        HouseInputs(
            site_id=SITE,
            now=now,
            tz=TZ,
            log=ForecastLog(TZ),
            forecast=None,
            house_actual={},
            sources=HouseSources(),
        )
    )
    starts = [datetime.fromisoformat(row["start"]) for row in view["hours"]]
    assert len(starts) == PAST_HOURS + AHEAD_HOURS
    assert all(b - a == timedelta(hours=1) for a, b in zip(starts, starts[1:], strict=False))
    assert view["house"] == {
        "made_at": None,
        "samples": 0,
        "temperature": None,
        "counts_battery": False,
        "review": None,
    }
    assert view["money"] is None


def test_reviewed_days_skip_parts_the_site_does_not_have() -> None:
    inputs = full_inputs()
    no_sun = house_view(
        HouseInputs(
            site_id=SITE,
            now=NOW,
            tz=TZ,
            log=inputs.log,
            forecast=inputs.forecast,
            house_actual=inputs.house_actual,
            sources=inputs.sources,
        )
    )
    assert all(
        day["sun_mae"] is None and day["soc_mae"] is None and day["plan_saving"] is None for day in no_sun["days"]
    )
    assert date.fromisoformat(no_sun["days"][0]["date"]) == (NOW + CET).date() - timedelta(days=1)
