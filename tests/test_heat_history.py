"""The heat card's history (`heat/history.py`): use and cost per period, degree-days and their monthly trend, comfort,
what the shadow rule would have moved, the immersion heater and the hot-water runs with their likely cause, and the
contract fixtures the card reads (`tests/fixtures/heat_history/`, `SPOTNAV_WRITE_FIXTURES=1` rewrites them)."""

from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from custom_components.spotnav.api.heat_history import hourly_means, outdoor_source, trend_start
from custom_components.spotnav.heat.config import HeatConfig, Night
from custom_components.spotnav.heat.history import (
    BASE_C,
    CAUSE_DRAW,
    CAUSE_STANDBY,
    HISTORY_KEYS,
    HeatHistoryInputs,
    heat_history,
    hour_prices,
    run_causes,
)
from custom_components.spotnav.heat.meter_split import HotWaterRun
from custom_components.spotnav.heat.relay_effect import BlockResult, Effect
from custom_components.spotnav.heat.review import review_day
from custom_components.spotnav.site.history_period import PERIOD_DAY, PERIOD_MONTH, PERIOD_YEAR, period_of, utc_bounds

TZ = "Europe/Stockholm"
FIXTURES = Path(__file__).parent / "fixtures" / "heat_history"
NOW = datetime(2026, 11, 16, 13, 20, tzinfo=timezone.utc)
MONEY = ("SEK", "kr", "öre")
FIVE = timedelta(minutes=5)
CONFIG = HeatConfig(target_c=21.0, night=Night(False, 1.0, "23:00", "06:00"), indoor_sensors=("sensor.kok", "sensor.uppe"))


def _fixture(name: str, payload: dict[str, Any]) -> None:
    path = FIXTURES / name
    payload = json.loads(json.dumps(payload))
    if os.environ.get("SPOTNAV_WRITE_FIXTURES") == "1":
        FIXTURES.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    assert json.loads(path.read_text(encoding="utf-8")) == payload, f"{name} drifted from the backend"


def hours(first: datetime, end: datetime) -> list[datetime]:
    out = []
    moment = first
    while moment < end:
        out.append(moment)
        moment += timedelta(hours=1)
    return out


def outdoor_c(moment: datetime) -> float:
    """Colder in winter, a day's swing of two degrees."""
    month = moment.month
    base = {1: -2.0, 2: -1.0, 3: 2.0, 4: 6.0, 5: 11.0, 6: 15.0, 7: 18.0, 8: 17.0, 9: 13.0, 10: 9.0, 11: 5.0, 12: 1.0}[month]
    return base + (1.0 if 10 <= moment.hour < 16 else -1.0)


def heat_kwh(moment: datetime, per_dd: float = 0.12) -> float:
    """The heat pump's hourly energy: a kWh per degree-day figure on the outdoor temperature, plus hot water."""
    return max(0.0, BASE_C - outdoor_c(moment)) * per_dd + 0.1


def slots(first: datetime, end: datetime) -> list[tuple[datetime, datetime, float]]:
    out = []
    for moment in hours(first, end):
        local_hour = (moment + timedelta(hours=1)).hour
        price = 260.0 if local_hour in (17, 18, 19) else 90.0 if local_hour in (2, 3, 4, 16) else 140.0
        out.extend((moment + timedelta(minutes=15 * q), moment + timedelta(minutes=15 * (q + 1)), price) for q in range(4))
    return out


def five_day(day: date) -> dict[datetime, float]:
    """A day's 5-minute energy (kWh): heating cycles, two showers in the morning and a small standby run at night."""
    start, _ = utc_bounds(period_of(PERIOD_DAY, day), TZ)
    out = {}
    for index in range(288):
        out[start + index * FIVE] = (1.12 if index % 10 < 7 else 0.15) / 12
    for hour, minute, values in ((3, 0, (1.9, 1.9)), (6, 15, (2.8, 2.9, 3.0, 2.8, 2.6)), (7, 25, (2.7, 2.9, 2.8, 2.5))):
        first = start + timedelta(hours=hour, minutes=minute)
        for offset, kw in enumerate(values):
            out[first + offset * FIVE] = kw / 12
    return out


def month_inputs(**extra: Any) -> HeatHistoryInputs:
    period = period_of(PERIOD_MONTH, date(2026, 11, 1))
    trend_first = datetime(2025, 11, 30, 23, tzinfo=timezone.utc)
    last = datetime(2026, 11, 16, 13, tzinfo=timezone.utc)
    every = hours(trend_first, last)
    five: dict[datetime, float] = {}
    for day in range(9, 16):
        five.update(five_day(date(2026, 11, day)))
    base = dict(
        site_id="site_villa",
        now=NOW,
        tz=TZ,
        period=period,
        config=CONFIG,
        meter=("sensor.varme_effekt", "Elmätare värme"),
        heat_hours={moment: heat_kwh(moment) for moment in every},
        five=five,
        prices=slots(datetime(2026, 10, 31, 23, tzinfo=timezone.utc), last + timedelta(hours=11)),
        money=MONEY,
        outdoor={moment: outdoor_c(moment) for moment in every},
        outdoor_source=("sensor.utomhus", "sensor"),
        indoor={
            "sensor.kok": ("Kök", {moment: (21.4, 21.0) for moment in every}),
            "sensor.uppe": ("Övervåning", {moment: (20.3 if (moment + timedelta(hours=1)).hour < 7 else 20.8, 20.1) for moment in every}),
        },
        immersion={moment: (2.0 if moment.day == 12 and moment.hour < 3 else 0.0) for moment in every},
    )
    base.update(extra)
    return HeatHistoryInputs(**base)  # type: ignore[arg-type]


def test_month_use_and_cost() -> None:
    answer = heat_history(month_inputs())
    assert tuple(answer) == HISTORY_KEYS
    energy = answer["energy"]
    start, _end = utc_bounds(period_of(PERIOD_MONTH, date(2026, 11, 1)), TZ)
    expected = sum(heat_kwh(moment) for moment in hours(start, datetime(2026, 11, 16, 13, tzinfo=timezone.utc)))
    assert energy["total_kwh"] == pytest.approx(expected, abs=0.01)
    assert energy["priced_hours"] == energy["hours"] == 15 * 24 + 14
    assert energy["split_days"] == 7 and energy["hot_water_kwh"] > 0
    assert len(answer["buckets"]) == 30
    day = next(bucket for bucket in answer["buckets"] if bucket["key"] == "2026-11-10")
    assert day["hot_water_kwh"] > 0 and day["cost"] > 0 and day["degree_days"] == pytest.approx(BASE_C - 5.0 + 0.0, abs=1.0)
    assert next(bucket for bucket in answer["buckets"] if bucket["key"] == "2026-11-20")["kwh"] is None


def test_degree_days_and_the_trend_across_new_year() -> None:
    answer = heat_history(month_inputs())
    degree = answer["degree_days"]
    assert degree["base_c"] == BASE_C and degree["days"] == 15
    # 0.12 kWh an hour per degree below the base, and 2.4 kWh a day of hot water spread over the degree-days.
    assert degree["kwh_per_degree_day"] == pytest.approx(2.88 + 2.4 / (BASE_C - 4.5), rel=0.01)
    trend = degree["trend"]
    assert [row["month"] for row in trend][0] == "2025-12" and trend[-1]["month"] == "2026-11"
    assert len(trend) == 12
    july = next(row for row in trend if row["month"] == "2026-07")
    # A warm July and August have hardly any degree-days: no figure rather than a jump.
    assert july["degree_days"] < 5 and july["kwh_per_degree_day"] is None
    assert next(row for row in trend if row["month"] == "2026-08")["kwh_per_degree_day"] is None
    january = next(row for row in trend if row["month"] == "2026-01")
    assert january["kwh_per_degree_day"] == pytest.approx(2.88 + 2.4 / (BASE_C + 2.5), rel=0.01)
    assert trend_start(period_of(PERIOD_YEAR, date(2027, 1, 1))) == date(2027, 1, 1)
    assert trend_start(period_of(PERIOD_MONTH, date(2027, 1, 1))) == date(2026, 2, 1)


def test_a_day_without_enough_outdoor_hours_has_no_degree_days() -> None:
    inputs = month_inputs()
    gappy = {moment: value for moment, value in inputs.outdoor.items() if not (moment.day == 10 and moment.hour < 8)}
    answer = heat_history(month_inputs(outdoor=gappy))
    assert next(bucket for bucket in answer["buckets"] if bucket["key"] == "2026-11-10")["degree_days"] is None
    assert answer["degree_days"]["days"] == 14


def test_comfort_the_coldest_room_and_hours_below() -> None:
    comfort = heat_history(month_inputs())["comfort"]
    assert comfort["coldest"]["entity_id"] == "sensor.uppe" and comfort["coldest"]["min_c"] == 20.1
    assert comfort["low_c"] == 20.5
    # The mornings before 07 are below the band: seven hours a day.
    assert comfort["hours_below"] == 15 * 7 + 7
    assert comfort["sensors"] == 2


def test_shadow_would_have_moved_the_dear_evening() -> None:
    shadow = heat_history(month_inputs())["shadow"]
    assert shadow["days"] == 15
    assert shadow["lowered_hours"] == 15 * 3
    # Three dear hours a day (260) moved to the cheap hour before them, which the rule preheats in (90).
    assert shadow["value"] > 0 and shadow["moved_kwh"] > 0
    assert shadow["value"] == pytest.approx(shadow["moved_kwh"] * 170.0, rel=0.01)


def kept_days(days: range) -> dict[date, dict[datetime, str]]:
    """SpotNav's plan as kept: lowered 16-18 local (one hour earlier than the rule would), normal otherwise."""
    out: dict[date, dict[datetime, str]] = {}
    for day in days:
        start, _ = utc_bounds(period_of(PERIOD_DAY, date(2026, 11, day)), TZ)
        out[date(2026, 11, day)] = {start + timedelta(hours=h): ("lower" if h in (16, 17, 18) else "normal") for h in range(24)}
    return out


def test_shadow_follows_the_kept_plan_and_the_learned_effect() -> None:
    effect = Effect(0.4, 0.5, 9)
    shadow = heat_history(month_inputs(kept=kept_days(range(12, 16)), effect=effect))["shadow"]
    assert shadow["days"] == 15 and shadow["kept_days"] == 4
    assert (shadow["lower_share"], shadow["rebound"], shadow["blocks"]) == (0.4, 0.5, 9)
    # The prior: half the heat taken away, all of it back.
    prior = heat_history(month_inputs())["shadow"]
    assert (prior["lower_share"], prior["rebound"], prior["blocks"], prior["kept_days"]) == (0.5, 1.0, 0, 0)
    # A kept day lowered 16-18 local: 16:00 is a cheap hour (90), so lowering there is worth less than the rule's.
    alone = heat_history(month_inputs(kept=kept_days(range(1, 16)), effect=Effect(0.5, 1.0, 0)))["shadow"]
    assert alone["value"] < prior["value"]


def test_the_days_review_is_shown_on_its_day() -> None:
    review = {"date": "2026-11-15", "relay": {"hours": 3.0}}
    day = heat_history(month_inputs(period=period_of(PERIOD_DAY, date(2026, 11, 15)), reviews={date(2026, 11, 15): review}))
    assert day["review"] == review
    assert heat_history(month_inputs(reviews={date(2026, 11, 15): review}))["review"] is None


def test_buckets_carry_their_share_of_the_shadow_value() -> None:
    answer = heat_history(month_inputs())
    by_key = {bucket["key"]: bucket for bucket in answer["buckets"]}
    shares = [bucket["shadow_value"] for bucket in answer["buckets"] if bucket["shadow_value"] is not None]
    assert len(shares) == 15 and sum(shares) == pytest.approx(answer["shadow"]["value"], abs=0.1)
    assert by_key["2026-11-10"]["shadow_value"] > 0
    assert by_key["2026-11-20"]["shadow_value"] is None  # a day to come is not evaluated
    year = heat_history(month_inputs(period=period_of(PERIOD_YEAR, date(2026, 1, 1))))
    november = next(bucket for bucket in year["buckets"] if bucket["key"] == "2026-11")
    assert november["shadow_value"] == pytest.approx(answer["shadow"]["value"], abs=0.1)
    assert next(bucket for bucket in year["buckets"] if bucket["key"] == "2026-01")["shadow_value"] is None


def test_immersion_hours_and_energy() -> None:
    immersion = heat_history(month_inputs())["immersion"]
    assert immersion == {"hours": 3, "kwh": 6.0}
    assert heat_history(month_inputs(immersion=None))["immersion"] is None


def test_missing_meter_outdoor_and_rooms() -> None:
    answer = heat_history(month_inputs(meter=None, heat_hours={}, five={}, outdoor_source=None, indoor={}, immersion=None))
    assert answer["energy"] is None and answer["buckets"] == [] and answer["shadow"] is None
    assert answer["degree_days"] is None and answer["comfort"] is None and answer["runs"] == []
    unpriced = heat_history(month_inputs(prices=(), money=None))
    assert unpriced["energy"]["cost"] is None and unpriced["energy"]["priced_hours"] == 0 and unpriced["shadow"] is None


def test_a_dst_day_has_25_hours_and_its_runs() -> None:
    period = period_of(PERIOD_DAY, date(2026, 10, 25))
    start, end = utc_bounds(period, TZ)
    every = hours(start, end)
    answer = heat_history(
        month_inputs(
            period=period,
            now=end + timedelta(days=1),
            heat_hours={moment: 1.0 for moment in every},
            outdoor={moment: 7.0 for moment in every},
            prices=slots(start, end),
            five=five_day(date(2026, 10, 25)),
        )
    )
    assert len(answer["buckets"]) == 25 and answer["energy"]["total_kwh"] == 25.0
    assert answer["degree_days"]["sum"] == 10.0 and answer["degree_days"]["trend"] == []
    assert len(answer["runs"]) == 3


def test_run_causes() -> None:
    start = datetime(2026, 11, 15, 5, tzinfo=timezone.utc)
    small = [HotWaterRun(start + timedelta(hours=k), start + timedelta(hours=k, minutes=10), 0.3) for k in range(5)]
    big = HotWaterRun(start + timedelta(hours=9), start + timedelta(hours=9, minutes=25), 1.1)
    causes = [row["cause"] for row in run_causes([small[0], big], [*small, big])]
    assert causes == [CAUSE_STANDBY, CAUSE_DRAW]
    assert [row["cause"] for row in run_causes([big], [big, small[0]])] == [None]


def test_hour_prices_need_the_whole_hour() -> None:
    start = datetime(2026, 11, 10, 10, tzinfo=timezone.utc)
    quarters = [(start + timedelta(minutes=15 * q), start + timedelta(minutes=15 * (q + 1)), 100.0 + 40 * q) for q in range(4)]
    assert hour_prices(quarters) == {start: pytest.approx(160.0)}
    assert hour_prices(quarters[:3]) == {}


def test_hourly_means_reads_statistics_rows() -> None:
    first = datetime(2026, 11, 10, 10, tzinfo=timezone.utc)
    rows = [{"start": first.timestamp(), "mean": 20.5, "min": 20.0}, {"start": first + timedelta(hours=1), "mean": None}, {"start": "x", "mean": 1}]
    assert hourly_means(rows, with_min=True) == {first: (20.5, 20.0)}


class _Forecast:
    def __init__(self, source: str, entity: str | None, rows: dict[datetime, tuple[float, float | None]]) -> None:
        self.outdoor_source = source
        self.outdoor_entity_id = entity
        self.weather_entity_id = "weather.home"
        self.history = type("H", (), {"rows": rows})()


class _Controller:
    def __init__(self, forecast: _Forecast) -> None:
        self.house_forecast = forecast


class _Pump:
    def __init__(self, sources: dict[str, str]) -> None:
        self.sources = sources


def test_outdoor_source_order() -> None:
    weather = _Controller(_Forecast("weather", None, {NOW: (1.0, 4.0)}))
    assert outdoor_source(HeatConfig(outdoor_sensor="sensor.ute"), weather, None) == ("sensor.ute", "sensor")
    assert outdoor_source(CONFIG, _Controller(_Forecast("sensor", "sensor.garage", {})), None) == ("sensor.garage", "sensor")
    assert outdoor_source(CONFIG, weather, _Pump({"outdoor_temp": "sensor.bt1"})) == ("sensor.bt1", "sensor")
    assert outdoor_source(CONFIG, weather, None) == ("weather.home", "house_forecast")
    assert outdoor_source(CONFIG, _Controller(_Forecast("weather", None, {NOW: (1.0, None)})), None) is None


def fixture_review() -> dict[str, Any]:
    """The 15th as the heat shadow reviewed it: the blueprint lowered 17-20 local, SpotNav would have lowered 16-19."""
    start, _ = utc_bounds(period_of(PERIOD_DAY, date(2026, 11, 15)), TZ)
    day = [start + timedelta(hours=h) for h in range(24)]
    prices = hour_prices(slots(start, start + timedelta(hours=24)))
    coldest = {start + timedelta(minutes=5 * i): (20.3 if 18 * 12 <= i < 18 * 12 + 6 else 20.7) for i in range(27 * 12)}
    return review_day(
        date(2026, 11, 15),
        day,
        prices=prices,
        heat_kwh={hour: heat_kwh(hour) for hour in day},
        relay=[(day[17], day[20])],
        plan_lowered=[day[16], day[17], day[18]],
        measured=[BlockResult(day[17], day[20], 1.6, 0.9, 2.1)],
        effect=Effect(0.46, 0.82, 7),
        coldest=coldest,
        low_c=20.5,
    )


def test_contract_fixtures() -> None:
    shadow = {"kept": kept_days(range(12, 16)), "effect": Effect(0.46, 0.82, 7), "reviews": {date(2026, 11, 15): fixture_review()}}
    _fixture("villa_month.json", heat_history(month_inputs(**shadow)))
    _fixture(
        "villa_day.json",
        heat_history(month_inputs(period=period_of(PERIOD_DAY, date(2026, 11, 15)), **shadow)),
    )
    _fixture(
        "meter_only_year.json",
        heat_history(month_inputs(period=period_of(PERIOD_YEAR, date(2026, 1, 1)), outdoor_source=None, outdoor={}, indoor={}, immersion=None)),
    )
