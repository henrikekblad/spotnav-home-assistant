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

import pytest

from custom_components.spotnav.battery.plan_model import (
    ACTION_DISCHARGE_HOUSE,
    ACTION_EXPORT_DISCHARGE,
    ACTION_GRID_CHARGE,
    ACTION_HOLD,
    ACTION_SELF_CONSUME,
)
from custom_components.spotnav.battery.plan_shadow import (
    KIND_BATTERY,
    Bucket,
    DayReview,
    PlannedSlot,
    PriceSlot,
    Run,
)
from custom_components.spotnav.battery.stored_value import DayMoney, StoredValue
from custom_components.spotnav.planning.demand_charge import HourMean, hour_start
from custom_components.spotnav.site.energy_flow import EnergyHour, energy_flow
from custom_components.spotnav.site.forecast_log import (
    KIND_HEAT,
    KIND_HOUSE,
    KIND_HOUSE_SPLIT,
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
    HEAT_KEYS,
    BatteryInputs,
    HeatInputs,
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


def heat_kw(start: datetime) -> float:
    """The heat pump's own share of the house: a little more than half, most in the evening."""
    return round(house_kw(start) * (0.62 if 16 <= local_hour(start) <= 20 else 0.5), 3)


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


def seeded_log(now: datetime, *, sun: bool, battery: bool, heat: bool = False) -> ForecastLog:
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
        if heat:
            rows.append((KIND_HEAT, heat_kw(start) * 1.05, heat_kw(start)))
            rows.append((KIND_HOUSE_SPLIT, house_kw(start) * 0.97, house_kw(start)))
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
    for back in range(12, 0, -1):
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
                actual=Run(sc - 610.0 - 20 * back + (300.0 if back in (9, 4) else 0.0), 40.0),
                plan=Run(sc - 690.0 - 10 * back, 40.0),
                self_consumption=Run(sc, 40.0),
                modes={"balanced": Run(sc - 690.0 - 10 * back, 40.0), "max_profit": Run(sc - 735.0, 40.0)},
                soc_mae=4.2,
                mode="balanced",
                # Against no battery (the day would have cost 500 more), the sun's value, and a few days that lack one.
                no_battery_cost=None if back == 11 else sc + 500.0,
                sun_gave=800.0 + 20 * back,
                sun_quarters=None if back == 7 else 60 if back == 5 else 96,
            )
        )
    return reviews


def buy_price(start: datetime) -> float:
    """Öre per kWh with every fee: cheap at night, a morning bump and an evening peak."""
    hour = local_hour(start)
    if hour < 6:
        return 95.0 + 5 * hour
    if 7 <= hour <= 9:
        return 210.0
    if 16 <= hour <= 20:
        return 330.0 - 10 * abs(hour - 18)
    return 160.0


def price_slots(now: datetime) -> list[PriceSlot]:
    first = hour_start(now, TZ).replace(hour=23) - timedelta(days=1)
    quarter = timedelta(minutes=15)
    return [
        PriceSlot(
            first + i * quarter,
            first + (i + 1) * quarter,
            buy_price(first + i * quarter),
            buy_price(first + i * quarter) * 0.62 - 35,
        )
        for i in range(48 * 4)
    ]


def quarters_today(now: datetime) -> list[Bucket]:
    """Today's quarter hours so far: the battery charges from the grid 02-05, covers the morning and the evening,
    and keeps the noon sun; a little is sold at noon."""
    start = hour_start(now, TZ).replace(hour=23) - timedelta(days=1)
    out = []
    while start + timedelta(minutes=15) <= now:
        hour = local_hour(start)
        house = house_kw(start) / 4
        sun = sun_kwh(start) / 4
        battery = 0.0
        if 2 <= hour < 5:
            battery = 2.25
        elif 6 <= hour <= 9 or hour >= 16:
            battery = -house
        elif 11 <= hour <= 13:
            battery = max(0.0, sun - house) * 0.6
        net = house - sun + battery
        out.append(
            Bucket(
                start=start,
                buy=buy_price(start),
                sell=buy_price(start) * 0.62 - 35,
                import_kwh=round(max(0.0, net), 4),
                export_kwh=round(max(0.0, -net), 4),
                battery_kwh=round(battery, 4),
                covered_s=900.0,
            )
        )
        start += timedelta(minutes=15)
    return out


def stored_value(now: datetime) -> StoredValue:
    """The battery's stored value carried in from yesterday (15 kWh at 1,40 kr), today's quarter hours counted, and
    the month's earlier days' money."""
    stored = StoredValue(kwh=15.0, cost=15.0 * 140.0, last=quarters_today(now)[0].start - timedelta(minutes=15))
    for back in range(12, 0, -1):
        day = (now + CET).date() - timedelta(days=back)
        stored.days[day] = DayMoney(
            sold=20.0 + back, bought=4100.0 - 40 * back, arbitrage=900.0 - 25 * (back % 5), quarters=96
        )
    stored.run(quarters_today(now), TZ, None)
    return stored


def full_inputs(now: datetime = NOW) -> HouseInputs:
    rows = history(now)
    forecast = forecast_house_load(rows, now, TZ, flat_kw=1.2)
    current = hour_start(now, TZ)
    ahead = [current + timedelta(hours=offset) for offset in range(AHEAD_HOURS)]
    return HouseInputs(
        site_id=SITE,
        now=now,
        tz=TZ,
        log=seeded_log(now, sun=True, battery=True, heat=True),
        forecast=forecast,
        house_actual={row.start: row.house_kw for row in rows if row.house_kw is not None},
        basis="gross",
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
            estimates=("min_soc_assumed",),
            quarters=quarters_today(now),
            prices=price_slots(now),
            production={start: sun_kwh(start) for start in (current - timedelta(hours=h) for h in range(1, 20))},
            stored=stored_value(now),
        ),
        money=("SEK", "kr", "öre"),
        energy_flow=villa_flow(now),
        heat=HeatInputs(
            status="found",
            entity_id="sensor.varme_effekt",
            name="Värme Effekt",
            kind="power",
            source="setting",
            setting_offered=True,
            actual={row.start: heat_kw(row.start) for row in rows},
            forecast={start: round(heat_kw(start) * 1.02, 3) for start in ahead},
        ),
    )


#: A real site's 13 November 2026, local hours 00-16 (kWh, the Energy dashboard's hourly statistics): grid import,
#: export, sun, battery charge, battery discharge, the charger's register. A night grid charge at 03-04, the car from
#: 10 to 14 with the battery charging from the grid beside it.
VILLA_HOURS = (
    (1.65, 0, 0, 0.01, 0, 0),
    (1.32, 0, 0, 0, 0, 0),
    (1.53, 0, 0, 0, 0, 0),
    (4.52, 0, 0, 2.67, 0.02, 0),
    (4.76, 0, 0, 2.8, 0.01, 0),
    (1.46, 0, 0, 0, 0, 0),
    (0.01, 0.03, 0, 0, 1.8, 0),
    (0.02, 0.02, 0.01, 0, 1.57, 0),
    (0, 0.02, 0.59, 0.16, 1.38, 0),
    (0.01, 0.02, 0.82, 0.02, 1.31, 0),
    (3.59, 0.02, 0.53, 0.89, 0.45, 2.1),
    (12.69, 0, 1.64, 7.41, 0.02, 4.67),
    (13.8, 0, 2.49, 5.5, 0.01, 8.56),
    (13.81, 0, 0.86, 3.13, 0, 10.13),
    (13.72, 0, 0.47, 12.52, 0, 0),
    (3.99, 0, 0.09, 3.1, 0.48, 0),
    (0.01, 0.01, 0, 0, 1.7, 0),
)


def villa_flow(now: datetime) -> dict[str, Any] | None:
    midnight = datetime(2026, 11, 13, tzinfo=timezone.utc) - CET
    hours = [
        EnergyHour(midnight + timedelta(hours=i), g_in, g_out, sun, b_in, b_out, car, VILLA_HEAT[i])
        for i, (g_in, g_out, sun, b_in, b_out, car) in enumerate(VILLA_HOURS)
        if midnight + timedelta(hours=i + 1) <= now
    ]
    return energy_flow(hours, day=date(2026, 11, 13), has_solar=True, has_battery=True, soc_pct=48.3, has_heat=True)


#: The heat pump's meter on the same day (kWh): about 0.8 at night, more in the morning; one hour unread (in the house).
VILLA_HEAT = (0.82, 0.85, 0.8, 0.9, 0.95, 1.05, 1.1, 1.0, 0.75, 0.6, None, 0.55, 0.5, 0.55, 0.6, 0.7, 0.9)


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
    assert view["house"]["review"]["hours"] == 7 * 24 and view["house"]["counts_battery"] is False
    assert view["house"]["basis"] == "gross"
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
    assert battery["estimates"] == ["min_soc_assumed"]
    days = view["days"]
    assert len(days) == 7 and days[0]["date"] > days[-1]["date"]
    assert all(day["house_mae"] is not None and day["plan_saving"] is not None for day in days)
    assert view["sources"]["solar_forecasts"] == ["Forecast.Solar"]
    # Today so far, split as the battery's own app does, and the day's energy from sources to uses.
    today = view["today"]
    assert today["date"] == "2026-11-13" and today["mode"] == "balanced"
    assert today["bought"] > 0 and today["arbitrage"] > 0 and today["sold"] >= 0
    assert today["average_cost"] is not None and today["estimated"] is False
    assert view["calendar"]["days"][0]["arbitrage"] is not None and view["days"][0]["arbitrage"] is not None
    flow = view["flow"]
    sources, uses = flow["sources"], flow["uses"]
    assert sum(sources.values()) == pytest.approx(sum(uses.values()), abs=0.05)
    assert uses["battery"] == pytest.approx(3 * 4 * 2.25 + sum(0 for _ in ()), abs=1.0)
    # The month's reviewed days, plan beside the battery's own.
    calendar = view["calendar"]
    assert calendar["month"] == "2026-11" and len(calendar["days"]) == 12
    assert any(day["own_saving"] < 400 for day in calendar["days"])
    # Prices on every hour the plan or the quarter hours priced.
    assert all(row["buy"] is not None and row["sell"] is not None for row in hours)
    # Heat: measured in the past (part of the house, never added to it), forecast ahead, the day so far beside the
    # house over the same hours, and the split against the whole over the same hours of the week.
    assert all(row["heat_kw"] is not None and row["heat_kw"] < row["house_kw"] for row in past)
    assert all(row["heat_kw"] is None and row["heat_forecast_kw"] is not None for row in ahead)
    heat = view["heat"]
    assert tuple(heat) == HEAT_KEYS
    assert heat["status"] == "found" and heat["entity_id"] == "sensor.varme_effekt" and heat["setting_offered"] is True
    assert heat["day"]["date"] == "2026-11-13" and heat["day"]["hours"] == 17
    assert 0 < heat["day"]["heat_kwh"] < heat["day"]["house_kwh"]
    assert heat["review"]["hours"] == 7 * 24
    comparison = heat["comparison"]
    assert comparison["hours"] == 7 * 24 and comparison["split_mae"] < comparison["house_mae"]
    nodes = {node["role"]: node for node in view["energy_flow"]["nodes"] if node["side"] == "use"}
    assert nodes["heat"]["kwh"] > 0 and view["energy_flow"]["heat_hours"] == 16
    _fixture("villa_battery_sun.json", view)


def test_a_past_day_shows_its_own_hours_money_and_flow() -> None:
    """Yesterday, asked for: its 24 local hours, all measured beside their first forecasts, its money from the stored
    energy's day, its own energy flow, no bars of today's quarters; the day block says which day and how far back."""
    from dataclasses import replace

    yesterday = date(2026, 11, 12)
    midnight = datetime(2026, 11, 12, tzinfo=timezone.utc) - CET
    flow = energy_flow(
        [EnergyHour(midnight + timedelta(hours=i), *row) for i, row in enumerate(VILLA_HOURS)],
        day=yesterday,
        has_solar=True,
        has_battery=True,
        soc_pct=31.5,
    )
    view = house_view(replace(full_inputs(), day=yesterday, energy_flow=flow))
    assert tuple(view) == HOUSE_KEYS
    hours = view["hours"]
    assert [row["start"] for row in hours] == [(midnight + timedelta(hours=i)).isoformat() for i in range(24)]
    assert all(row["house_kw"] is not None and row["sun_kwh"] is not None and row["soc_pct"] is not None for row in hours)
    assert all(row["battery_action"] is None for row in hours)
    assert view["day"] == {"date": "2026-11-12", "today": False, "first": "2026-10-31", "has_data": True}
    assert view["today"]["date"] == "2026-11-12"
    assert view["flow"] is None and view["energy_flow"]["date"] == "2026-11-12"
    review = next(item for item in full_inputs().battery.reviews if item.day == yesterday)
    assert view["day_review"] == {
        "date": "2026-11-12",
        "mode": review.mode,
        "modes": {mode: round(review.mode_savings[mode], 2) if mode in review.mode_savings else None for mode in ("self_consumption", "balanced", "max_profit")},
        "own_saving": round(review.actual_saving, 2),
        "battery_saving": round(review.no_battery_cost - review.actual.cost, 2),
        "sun_gave": 820.0,
        "against_nothing": round(review.no_battery_cost - review.actual.cost + 820.0, 2),
        "covered_quarters": 96,
        "day_quarters": 96,
        "sun_quarters": 96,
    }
    _fixture("villa_past_day.json", view)
    # A day nothing was measured on says so.
    empty = house_view(replace(full_inputs(), day=date(2026, 11, 1), energy_flow=None, house_actual={}, log=ForecastLog(TZ), heat=None))
    assert empty["day"]["has_data"] is False
    # A day without a review has none.
    assert house_view(replace(full_inputs(), day=date(2026, 10, 31)))["day_review"] is None


def test_the_recorders_soc_fills_hours_the_log_has_no_level_for() -> None:
    """The battery plan's log wins; a past hour without a logged level takes the recorder's; hours ahead never do."""
    from dataclasses import replace

    inputs = full_inputs()
    current = hour_start(NOW, TZ)
    body = inputs.log._kind(KIND_BATTERY)  # noqa: SLF001
    gap = current - timedelta(hours=3)
    del body.hours[gap]
    logged = current - timedelta(hours=2)
    recorded = {gap: 37.26, logged: 99.0, current: 12.0}
    rows = {row["start"]: row for row in house_view(replace(inputs, soc_recorded=recorded))["hours"]}
    assert rows[gap.isoformat()]["soc_pct"] == 37.3
    assert rows[logged.isoformat()]["soc_pct"] != 99.0
    assert rows[current.isoformat()]["soc_pct"] is None


def test_the_day_block_on_today() -> None:
    assert house_view(full_inputs())["day_review"] is None
    assert house_view(full_inputs())["day"] == {"date": "2026-11-13", "today": True, "first": "2026-10-31", "has_data": True}


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
    # No heat pump meter: no heat block, no heat figures.
    assert view["heat"] is None and all(row["heat_kw"] is None and row["heat_forecast_kw"] is None for row in hours)
    _fixture("house_learning.json", view)


def test_an_ambiguous_heat_meter_shows_the_candidates_and_no_figures() -> None:
    from dataclasses import replace

    heat = HeatInputs(
        status="ambiguous",
        source="energy_dashboard",
        candidates=("sensor.heat_pump_power", "sensor.floor_heating_power"),
        setting_offered=True,
        actual={hour_start(NOW, TZ) - timedelta(hours=1): 1.0},
    )
    view = house_view(replace(full_inputs(), heat=heat))
    assert view["heat"]["status"] == "ambiguous" and view["heat"]["candidates"] == list(heat.candidates)
    assert view["heat"]["day"] is None and view["heat"]["review"] is None and view["heat"]["comparison"] is None
    assert all(row["heat_kw"] is None and row["heat_forecast_kw"] is None for row in view["hours"])


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
        "basis": "net",
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
