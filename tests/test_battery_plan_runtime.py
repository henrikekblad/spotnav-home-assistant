"""The battery plan's shell on a site (`battery/plan_runtime.py`): it plans in shadow from the site's inputs, says why
there is no plan, plans again on its triggers, keeps what the battery did, reviews the day and survives a reload.
Nothing is ever written to the battery.

The prices are given directly (`_read_prices`); the rest is the real site with the Sigenergy plant of
`tests/fixtures/battery/brands.json` (in its AI mode: the plan runs as a benchmark).
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.spotnav.battery import plan_runtime
from custom_components.spotnav.battery.plan_model import (
    ACTION_SELF_CONSUME,
    MODE_BALANCED,
    MODE_MAX_PROFIT,
    MODE_SELF_CONSUMPTION,
    MODES,
)
from custom_components.spotnav.battery.plan_runtime import CHECK_S, BatteryPlanShadow
from custom_components.spotnav.battery.plan_shadow import (
    BUCKET,
    EST_HOUSE_COUNTS_BATTERY,
    EST_MIN_SOC,
    EST_NO_SOLAR,
    KIND_BATTERY,
    MIN_OWN_HOURS,
    NO_CAPACITY,
    NO_HOUSE_FORECAST,
    NO_PRICES,
    REASON_CODES,
    TRIGGER_FORECAST,
    TRIGGER_PRICES,
    TRIGGER_START,
    PriceSlot,
    bucket_start,
)
from custom_components.spotnav.debug_bundle import async_build_debug_bundle
from custom_components.spotnav.diagnostics import async_get_config_entry_diagnostics
from custom_components.spotnav.planning.demand_charge import hour_start
from custom_components.spotnav.runtime import site_controller_for
from custom_components.spotnav.site.load_forecast import NO_TEMPERATURE, ForecastHour, LoadForecast

from tests.test_battery_runtime import _service_calls, _site


@pytest.fixture(autouse=True)
def _frozen_budget_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """The modes budget reads a frozen clock: how long the solve took on this machine never skips a mode."""
    monkeypatch.setattr(plan_runtime, "budget_clock", lambda: 0.0)

pytestmark = pytest.mark.usefixtures("offline_relay")


def _prices(now: datetime, *, evening: float = 400.0, count: int = 96) -> list[PriceSlot]:
    """Quarter hours from the running one: cheap first, a dear stretch from the 8th hour."""
    first = bucket_start(now)
    return [
        PriceSlot(first + BUCKET * i, first + BUCKET * (i + 1), evening if 32 <= i < 48 else 60.0, 30.0)
        for i in range(count)
    ]


def _house_forecast(controller: Any, now: datetime, kw: float = 1.5, hours: int = 30) -> None:
    tz = controller.demand.tracker.tz
    first = hour_start(now, tz)
    controller.house_forecast.forecast = LoadForecast(
        made_at=now,
        hours=tuple(ForecastHour(first + timedelta(hours=i), kw, "profile") for i in range(hours)),
        samples=100,
        temperature=NO_TEMPERATURE,
        holidays=False,
    )
    controller.house_forecast.refreshes += 1


def _give_prices(monkeypatch: pytest.MonkeyPatch, prices: list[PriceSlot], key: Any = 1) -> None:
    monkeypatch.setattr(BatteryPlanShadow, "_read_prices", lambda self, now: ("SE4", prices))
    monkeypatch.setattr(BatteryPlanShadow, "_prices_key", lambda self, now: key)


async def _planned(hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, **states: str):
    site, controller = await _site(hass, **states)
    now = dt_util.now()
    _give_prices(monkeypatch, _prices(now))
    _house_forecast(controller, now)
    controller.battery_shadow.tick(now)
    await hass.async_block_till_done()
    return site, controller, now


async def test_the_plan_runs_in_shadow_as_a_benchmark_beside_a_battery_in_its_own_ai_mode(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _service_calls(hass)
    site, controller, _now = await _planned(hass, monkeypatch)

    plan = (await async_get_config_entry_diagnostics(hass, site))["battery"]["plan"]

    assert plan["state"] == "shadow" and plan["benchmark"] is True and plan["management"] == "self_managed"
    assert plan["status"] == "planned" and plan["trigger"] == TRIGGER_START and plan["runs"] == 1
    inputs = plan["inputs"]
    assert inputs["battery"]["usable_kwh"] == 40.3 and inputs["soc_pct"] == 44.6
    assert inputs["house"] == "site_forecast" and inputs["slots"] > 80 and inputs["sell_credit"] == 0.0
    assert {EST_HOUSE_COUNTS_BATTERY, EST_MIN_SOC, EST_NO_SOLAR} <= set(inputs["estimates"])
    slots = plan["plan"]["slots"]
    assert all(reason["code"] in REASON_CODES for slot in slots for reason in slot["reasons"])
    assert plan["plan"]["savings"] >= 0
    # Balanced is the plan in force; every mode ran on the same inputs, Self-consumption as the baseline.
    assert plan["mode"] == inputs["mode"] == MODE_BALANCED and plan["modes_skipped"] == []
    assert set(plan["modes"]) == set(MODES) and plan["modes"][MODE_BALANCED] == plan["plan"]
    assert len(plan["modes"][MODE_MAX_PROFIT]["slots"]) == len(slots)
    assert plan["modes"][MODE_SELF_CONSUMPTION]["baseline"] is True
    # The modes differ in arbitrage, not in floor: each keeps the battery's own.
    assert plan["plan"]["min_soc_pct"] == 10.0 and plan["modes"][MODE_MAX_PROFIT]["min_soc_pct"] == 10.0
    assert inputs["wear"] > 0
    assert plan["review_totals"]["days"] == 0
    # The first quarter hour knows what the plan said about it.
    assert plan["quarters"][-1]["first_planned"] is None
    bundle = await async_build_debug_bundle(hass)
    assert bundle["sites"][0]["diagnostics"]["battery"]["plan"]["status"] == "planned"
    # The planned SoC at each hour's end is in the site's forecast log.
    assert controller.house_forecast.log.kinds[KIND_BATTERY].hours
    # Nothing was written to any device.
    assert [call for call in calls if call.data.get("domain") not in ("system_log", "logger")] == []


async def test_a_sell_credit_is_read_from_the_hook(hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch) -> None:
    site, controller = await _site(hass)
    hass.config_entries.async_update_entry(site, data={**site.data, plan_runtime.SELL_CREDIT_KEY: 4.5})
    assert controller.battery_shadow.plan._sell_credit() == 4.5


async def test_without_prices_there_is_no_plan_and_it_says_why(hass: HomeAssistant) -> None:
    _site_entry, controller = await _site(hass)
    now = dt_util.now()
    _house_forecast(controller, now)
    controller.battery_shadow.tick(now)
    await hass.async_block_till_done()
    plan = controller.battery_shadow.snapshot()["plan"]
    assert (plan["status"], plan["reason"], plan["plan"]) == ("no_plan", NO_PRICES, None)


async def test_without_a_house_forecast_there_is_no_plan(hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch) -> None:
    _site_entry, controller = await _site(hass)
    now = dt_util.now()
    _give_prices(monkeypatch, _prices(now))
    controller.battery_shadow.tick(now)
    await hass.async_block_till_done()
    assert controller.battery_shadow.snapshot()["plan"]["reason"] == NO_HOUSE_FORECAST


async def test_without_the_batterys_size_there_is_no_plan(hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch) -> None:
    _site_entry, controller = await _site(hass, **{"sensor.sigen_plant_rated_energy_capacity": "unavailable"})
    now = dt_util.now()
    _give_prices(monkeypatch, _prices(now))
    _house_forecast(controller, now)
    controller.battery_shadow.tick(now)
    await hass.async_block_till_done()
    assert controller.battery_shadow.snapshot()["plan"]["reason"] == NO_CAPACITY


async def test_it_plans_again_on_new_prices_and_a_forecast_refresh_and_not_in_between(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    _site_entry, controller, now = await _planned(hass, monkeypatch)
    shadow = controller.battery_shadow
    plan = shadow.plan

    shadow.tick(now + timedelta(seconds=10))
    shadow.tick(now + timedelta(seconds=CHECK_S + 1))
    await hass.async_block_till_done()
    assert plan.runs == 1

    _house_forecast(controller, now)
    shadow.tick(now + timedelta(seconds=2 * CHECK_S + 2))
    await hass.async_block_till_done()
    assert (plan.runs, plan.trigger) == (2, TRIGGER_FORECAST)

    _give_prices(monkeypatch, _prices(now, evening=500.0), key=2)
    shadow.tick(now + timedelta(seconds=3 * CHECK_S + 3))
    await hass.async_block_till_done()
    assert (plan.runs, plan.trigger) == (3, TRIGGER_PRICES)


async def test_one_plan_at_a_time(hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch) -> None:
    _site_entry, controller, now = await _planned(hass, monkeypatch)
    plan = controller.battery_shadow.plan
    plan._running = True
    _house_forecast(controller, now)
    controller.battery_shadow.tick(now + timedelta(seconds=CHECK_S + 1))
    await hass.async_block_till_done()
    assert (plan.runs, plan.skipped_busy) == (1, 1)


async def test_the_plan_is_solved_in_the_executor(hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch) -> None:
    jobs: list[Any] = []
    original = hass.async_add_executor_job

    def spy(target: Any, *args: Any) -> Any:
        jobs.append(getattr(target, "func", target))
        return original(target, *args)

    monkeypatch.setattr(hass, "async_add_executor_job", spy)
    await _planned(hass, monkeypatch)
    assert plan_runtime._solve in jobs


async def test_the_other_modes_are_skipped_when_the_first_plan_took_too_long(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    ticks = iter(range(0, 1000, 10))
    monkeypatch.setattr(plan_runtime, "budget_clock", lambda: float(next(ticks)))
    _site_entry, controller, _now = await _planned(hass, monkeypatch)
    plan = controller.battery_shadow.snapshot()["plan"]
    assert plan["status"] == "planned" and plan["modes_skipped"] == [MODE_MAX_PROFIT]
    assert plan["modes"][MODE_MAX_PROFIT] is None and plan["modes"][MODE_SELF_CONSUMPTION] is not None


async def test_the_site_mode_is_read_from_its_key(hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch) -> None:
    site, controller = await _site(hass)
    shadow = controller.battery_shadow.plan
    assert shadow._site_mode() == (MODE_BALANCED, False)
    hass.config_entries.async_update_entry(site, data={**site.data, plan_runtime.MODE_KEY: MODE_MAX_PROFIT})
    assert shadow._site_mode() == (MODE_MAX_PROFIT, False)
    hass.config_entries.async_update_entry(site, data={**site.data, plan_runtime.MODE_KEY: "export"})
    assert shadow._site_mode() == (MODE_BALANCED, True)


async def test_max_profit_as_the_site_mode_is_the_plan_in_force(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    site, controller = await _site(hass)
    hass.config_entries.async_update_entry(site, data={**site.data, plan_runtime.MODE_KEY: MODE_MAX_PROFIT})
    now = dt_util.now()
    _give_prices(monkeypatch, _prices(now))
    _house_forecast(controller, now)
    controller.battery_shadow.tick(now)
    await hass.async_block_till_done()
    plan = controller.battery_shadow.snapshot()["plan"]
    assert plan["mode"] == MODE_MAX_PROFIT and plan["plan"] == plan["modes"][MODE_MAX_PROFIT]
    assert plan["plan"]["min_soc_pct"] == 10.0
    shadow = controller.battery_shadow.plan
    assert set(shadow.mode_plans) == {MODE_BALANCED, MODE_MAX_PROFIT}
    assert [slot.action for slot in shadow.plan] == [slot.action for slot in shadow.mode_plans[MODE_MAX_PROFIT]]


async def test_the_quarter_hours_record_what_the_battery_did_and_the_day_is_reviewed(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    _site_entry, controller, now = await _planned(hass, monkeypatch)
    plan = controller.battery_shadow.plan
    tz = controller.demand.tracker.tz
    # Yesterday, measured: an idle battery at 44.6 %, 1 kW from the grid all day.
    from custom_components.spotnav import time_zones

    zone = time_zones.require_zone(tz)
    yesterday = now.astimezone(zone).date() - timedelta(days=1)
    start = datetime.combine(yesterday, datetime.min.time(), zone).astimezone(dt_util.UTC)
    moment = start
    while moment.astimezone(zone).date() == yesterday:
        bucket = plan.actuals.open(moment, price=PriceSlot(moment, moment + BUCKET, 100.0, 40.0), cap_kw=None, ran=None)
        bucket.import_kwh, bucket.covered_s = 0.25, 900.0
        bucket.soc_start = bucket.soc_end = 44.6
        bucket.ran_action = ACTION_SELF_CONSUME
        moment += BUCKET
    plan._quarter = None
    controller.battery_shadow.tick(now + timedelta(seconds=1))
    await hass.async_block_till_done()

    snapshot = controller.battery_shadow.snapshot()["plan"]
    [review] = snapshot["reviews"]
    assert review["date"] == yesterday.isoformat()
    assert review["actual"]["cost"] == pytest.approx(review["covered"] * 25.0)
    assert set(review) >= {"actual", "plan", "modes", "self_consumption", "saving", "mode_savings", "actual_saving"}
    assert snapshot["review_totals"]["days"] == 1
    assert len(plan.actuals.rows) >= 23


async def test_the_record_and_the_running_slot_survive_a_reload(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    site, controller, now = await _planned(hass, monkeypatch)
    kept = controller.battery_shadow.plan.plan
    assert kept

    assert await hass.config_entries.async_reload(site.entry_id)
    await hass.async_block_till_done()
    reloaded = site_controller_for(hass, site.entry_id)
    assert reloaded is not None and reloaded is not controller
    plan = reloaded.battery_shadow.plan
    assert [(s.start, s.end, s.action) for s in plan.plan] == [(s.start, s.end, s.action) for s in kept]
    assert plan.soc_at_plan == 44.6
    assert plan.actuals.buckets
    assert plan.mode == MODE_BALANCED and set(plan.mode_plans) == {MODE_BALANCED, MODE_MAX_PROFIT}
    kept_profit = controller.battery_shadow.plan.mode_plans[MODE_MAX_PROFIT]
    assert [s.action for s in plan.mode_plans[MODE_MAX_PROFIT]] == [s.action for s in kept_profit]

    _house_forecast(reloaded, now)
    reloaded.battery_shadow.tick(now + timedelta(seconds=5))
    await hass.async_block_till_done()
    snapshot = reloaded.battery_shadow.snapshot()["plan"]
    # The first plan after the reload weighed each mode's running slot against the one before.
    assert "kept" in snapshot["plan"] and "kept" in snapshot["modes"][MODE_MAX_PROFIT]


async def test_own_house_rows_replace_the_sites_forecast_once_there_are_enough(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    _site_entry, controller, now = await _planned(hass, monkeypatch)
    plan = controller.battery_shadow.plan
    tz = controller.demand.tracker.tz
    current = hour_start(now, tz)
    for index in range(1, MIN_OWN_HOURS + 1):
        plan.actuals.rows[current - timedelta(hours=index)] = 0.8
    _house_forecast(controller, now)
    controller.battery_shadow.tick(now + timedelta(seconds=CHECK_S + 1))
    await hass.async_block_till_done()
    inputs = controller.battery_shadow.snapshot()["plan"]["inputs"]
    assert inputs["house"] == "own_rows" and inputs["own_hours"] == MIN_OWN_HOURS
    assert EST_HOUSE_COUNTS_BATTERY not in inputs["estimates"]
    assert controller.battery_shadow.snapshot()["plan"]["plan"]["slots"][1]["house_kw"] == pytest.approx(0.8)


def _with_sun(monkeypatch: pytest.MonkeyPatch, controller: Any, now: datetime) -> None:
    """A solar forecast for the plan: 1 kWh in each of the next 30 hours."""
    from custom_components.spotnav.const import CONF_SOLAR_FORECAST_ENTRIES  # noqa: PLC0415
    from custom_components.spotnav.planning import hybrid_forecast  # noqa: PLC0415
    from custom_components.spotnav.planning.hybrid_forecast import ForecastReadResult  # noqa: PLC0415

    first = hour_start(now, controller.demand.tracker.tz)

    async def read(hass: Any, entries: Any, *, now: datetime) -> ForecastReadResult:
        return ForecastReadResult({first + timedelta(hours=i): 1000.0 for i in range(30)}, ("forecast_solar_1",))

    monkeypatch.setattr(hybrid_forecast, "async_read_forecast_wh", read)
    monkeypatch.setattr(controller, "config", {**controller.config, CONF_SOLAR_FORECAST_ENTRIES: ["forecast_solar_1"]})


async def _own_rows_with_sun(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, production: dict[datetime, float] | None
) -> dict[str, Any]:
    """The plan with own rows of 0.8 kW net of the sun and a solar forecast; `production` `{}` gives 0.5 kWh in
    every hour but the last, `None` no solar statistics at all."""
    _site_entry, controller, now = await _planned(hass, monkeypatch)
    plan = controller.battery_shadow.plan
    current = hour_start(now, controller.demand.tracker.tz)
    hours = [current - timedelta(hours=index) for index in range(1, MIN_OWN_HOURS + 3)]
    for start in hours:
        plan.actuals.rows[start] = 0.8
    _with_sun(monkeypatch, controller, now)

    async def ids(hass: Any) -> tuple[tuple[str, ...], tuple[str, ...]]:
        return (("sensor.pv",), ()) if production is not None else ((), ())

    async def statistics(hass: Any, wanted: Any, start: datetime, end: datetime, period: str) -> dict[str, Any]:
        assert period == "hour" and end <= now
        rows = [{"start": moment.timestamp(), "change": kwh} for moment, kwh in (production or {}).items()]
        return {"sensor.pv": rows}

    monkeypatch.setattr(plan_runtime, "async_energy_ids", ids)
    monkeypatch.setattr(plan_runtime, "async_statistics", statistics)
    if production == {}:
        production.update({start: 0.5 for start in hours[1:]})  # the last hour is not compiled yet
    _house_forecast(controller, now)
    controller.battery_shadow.tick(now + timedelta(seconds=CHECK_S + 1))
    await hass.async_block_till_done()
    return controller.battery_shadow.snapshot()["plan"]


async def test_own_house_rows_get_the_suns_production_back_before_the_forecast_is_added(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    from custom_components.spotnav.battery.plan_shadow import EST_SOLAR_IN_HOUSE  # noqa: PLC0415

    shown = await _own_rows_with_sun(hass, monkeypatch, {})
    inputs = shown["inputs"]
    assert inputs["house"] == "own_rows" and inputs["house_rows"] == plan_runtime.ROWS_GROSS
    assert inputs["solar_statistics"] == ["sensor.pv"]
    # The hour without a production figure yet is left out, never counted net of the sun.
    assert inputs["own_hours"] == MIN_OWN_HOURS + 1
    assert EST_SOLAR_IN_HOUSE not in inputs["estimates"]
    assert shown["plan"]["slots"][1]["house_kw"] == pytest.approx(1.3)


async def test_without_production_statistics_the_rows_stay_net_and_it_is_said(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    from custom_components.spotnav.battery.plan_shadow import EST_SOLAR_IN_HOUSE  # noqa: PLC0415

    inputs = (await _own_rows_with_sun(hass, monkeypatch, None))["inputs"]
    assert inputs["house"] == "own_rows" and inputs["house_rows"] == plan_runtime.ROWS_NET
    assert inputs["own_hours"] == MIN_OWN_HOURS + 2
    assert EST_SOLAR_IN_HOUSE in inputs["estimates"]


def test_gross_rows_need_enough_hours_with_a_production_figure() -> None:
    start = datetime(2026, 11, 1, tzinfo=dt_util.UTC)
    rows = [(start + timedelta(hours=index), 1.0) for index in range(MIN_OWN_HOURS + 1)]
    every = {moment: 0.25 for moment, _kw in rows}
    assert plan_runtime.gross_house_rows(rows, every) == [(moment, 1.25) for moment, _kw in rows]
    assert plan_runtime.gross_house_rows(rows, dict(list(every.items())[: MIN_OWN_HOURS - 1])) is None


async def test_the_plan_takes_the_plants_maximum_and_the_batterys_own_reserve(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    _site_entry, controller, _now = await _planned(hass, monkeypatch)
    battery = controller.battery_shadow.snapshot()["plan"]["inputs"]["battery"]
    # The real plant: 13.2 kW out of the whole plant beside a battery rated 20.6/23.04 kW; no reserve readable.
    assert battery["plant_max_kw"] == 13.2
    assert battery["max_charge_kw"] == 13.2 and battery["max_discharge_kw"] == 13.2
    assert battery["reserve_soc_pct"] is None and battery["min_soc_pct"] == 10.0


def test_a_readable_reserve_replaces_the_assumed_floor() -> None:
    from custom_components.spotnav.battery.facts import BatteryFacts  # noqa: PLC0415
    from custom_components.spotnav.battery.plan_shadow import battery_params  # noqa: PLC0415

    facts = BatteryFacts(
        battery_entity_id="sensor.battery",
        soc_pct=50.0,
        capacity_kwh=10.0,
        rated_charge_kw=5.0,
        rated_discharge_kw=5.0,
        plant_max_kw=8.0,
        reserve_soc_pct=20.0,
    )
    params, reason, estimates = plan_runtime.limited_params(*battery_params(facts), facts)
    assert reason is None and params is not None
    assert (params.max_charge_kw, params.max_discharge_kw, params.min_soc_pct) == (5.0, 5.0, 20.0)
    assert EST_MIN_SOC not in estimates
    # A reserve at the ceiling cannot be a floor: the assumed one stays, and says so.
    params, _reason, estimates = plan_runtime.limited_params(
        *battery_params(facts), replace(facts, reserve_soc_pct=100.0)
    )
    assert params is not None and params.min_soc_pct == 10.0 and EST_MIN_SOC in estimates


def test_the_plans_sun_is_corrected_only_once_the_correction_has_learnt() -> None:
    from custom_components.spotnav.site.solar_correction import (  # noqa: PLC0415
        KIND_CLEAR,
        KIND_CLOUDY,
        MIN_DAYS,
        SolarCorrection,
    )

    start = datetime(2026, 11, 1, 10, tzinfo=dt_util.UTC)
    raw = {start: 2000.0, start + timedelta(hours=1): 4000.0}
    learning = SolarCorrection(factors={KIND_CLEAR: 0.5, KIND_CLOUDY: 0.5})
    assert plan_runtime.corrected_solar_wh(raw, learning, observed_max_kwh=None, array_kwp=None) == (
        raw,
        plan_runtime.SOLAR_RAW,
    )
    learnt = SolarCorrection(factors={KIND_CLEAR: 0.5, KIND_CLOUDY: 0.5}, days={KIND_CLEAR: MIN_DAYS, KIND_CLOUDY: 0})
    sun, basis = plan_runtime.corrected_solar_wh(raw, learnt, observed_max_kwh=3.0, array_kwp=None)
    assert basis == plan_runtime.SOLAR_CORRECTED
    assert sun == {start: pytest.approx(1000.0), start + timedelta(hours=1): pytest.approx(2000.0)}
    # A factor above 1 is capped by the highest hour observed (and the array's size), as the correction caps it.
    high = SolarCorrection(factors={KIND_CLEAR: 2.0, KIND_CLOUDY: 2.0}, days={KIND_CLEAR: MIN_DAYS, KIND_CLOUDY: 0})
    sun, _basis = plan_runtime.corrected_solar_wh(raw, high, observed_max_kwh=3.0, array_kwp=None)
    assert sun == {start: pytest.approx(3000.0), start + timedelta(hours=1): pytest.approx(4000.0)}
    # Off on a curtailed site: the forecast as read.
    curtailed = SolarCorrection(
        factors={KIND_CLEAR: 0.5, KIND_CLOUDY: 0.5}, days={KIND_CLEAR: MIN_DAYS, KIND_CLOUDY: 0}, curtailed="export_limit"
    )
    assert plan_runtime.corrected_solar_wh(raw, curtailed, observed_max_kwh=None, array_kwp=None)[1] == "raw"


async def test_the_plan_counts_on_the_corrected_sun_and_says_so(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    from custom_components.spotnav.site.solar_correction import KIND_CLEAR, KIND_CLOUDY, MIN_DAYS  # noqa: PLC0415

    _site_entry, controller, now = await _planned(hass, monkeypatch)
    _with_sun(monkeypatch, controller, now)
    state = controller.solar_correction.state
    state.factors = {KIND_CLEAR: 0.5, KIND_CLOUDY: 0.5}
    state.days = {KIND_CLEAR: MIN_DAYS, KIND_CLOUDY: MIN_DAYS}
    _house_forecast(controller, now)
    controller.battery_shadow.tick(now + timedelta(seconds=CHECK_S + 1))
    await hass.async_block_till_done()
    shown = controller.battery_shadow.snapshot()["plan"]
    assert shown["inputs"]["solar"] == plan_runtime.SOLAR_CORRECTED
    assert shown["plan"]["slots"][1]["solar_kw"] == pytest.approx(0.5)


async def test_the_plans_own_house_forecast_is_the_median_without_the_inday_adjustment(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    from custom_components.spotnav.site import load_forecast  # noqa: PLC0415

    seen: list[dict[str, Any]] = []

    def spy(*args: Any, **kwargs: Any) -> Any:
        seen.append(kwargs)
        return load_forecast.forecast_house_load(*args, **kwargs)

    monkeypatch.setattr(plan_runtime, "forecast_house_load", spy)
    _site_entry, controller, now = await _planned(hass, monkeypatch)
    current = hour_start(now, controller.demand.tracker.tz)
    for index in range(1, MIN_OWN_HOURS + 1):
        controller.battery_shadow.plan.actuals.rows[current - timedelta(hours=index)] = 0.8
    _house_forecast(controller, now)
    controller.battery_shadow.tick(now + timedelta(seconds=CHECK_S + 1))
    await hass.async_block_till_done()
    assert controller.battery_shadow.snapshot()["plan"]["inputs"]["house"] == "own_rows"
    assert seen and seen[-1]["inday"] is False and seen[-1]["quantile"] == plan_runtime.HOUSE_QUANTILE == 0.5
