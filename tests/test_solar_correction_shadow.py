"""The solar forecast correction's Home Assistant shell (`site/solar_correction_shadow.py`): its sources found with no
setting, a week of hourly refreshes on a real site, the nightly update in shadow, the diagnostics and a restart."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.spotnav.const import CONF_SOLAR_FORECAST_ENTRIES
from custom_components.spotnav.diagnostics import async_get_config_entry_diagnostics
from custom_components.spotnav.planning.hybrid_forecast import ForecastReadResult
from custom_components.spotnav.runtime import site_controller_for
from custom_components.spotnav.site import solar_correction_shadow as shell
from custom_components.spotnav.site.solar_correction import (
    CURTAILED_ZERO_EXPORT,
    KIND_CLEAR,
    STATUS_OFF_CURTAILED,
    STATUS_SHADOW,
)
from custom_components.spotnav.site.solar_correction_shadow import (
    STATUS_NO_FORECAST,
    array_kwp,
    as_power,
    hourly_cloud,
    negative_hours,
    pick_weather_entity,
)
from custom_components.spotnav.site.energy_statistics import energy_statistic_ids, summed
from custom_components.spotnav.site.forecast_log import KIND_SUN, KIND_SUN_CORRECTED

from .test_demand_site import pilot_site

pytestmark = pytest.mark.usefixtures("offline_relay")


# ---------------------------------------------------------------------------------------------- the sources, pure


def test_the_energy_dashboards_solar_and_export_statistics_are_found() -> None:
    prefs = {
        "energy_sources": [
            {"type": "solar", "stat_energy_from": "sensor.pv_a"},
            {"type": "solar", "stat_energy_from": "sensor.pv_b"},
            {"type": "solar", "stat_energy_from": "sensor.pv_a"},
            {"type": "grid", "stat_energy_from": "sensor.import", "stat_energy_to": "sensor.export"},
            {"type": "grid", "flow_from": [], "flow_to": [{"stat_energy_to": "sensor.export_old"}]},
            {"type": "battery", "stat_energy_from": "sensor.battery_out"},
            "junk",
        ]
    }

    assert energy_statistic_ids(prefs) == (("sensor.pv_a", "sensor.pv_b"), ("sensor.export", "sensor.export_old"))
    assert energy_statistic_ids(None) == ((), ())


def test_statistics_are_summed_only_where_every_source_has_the_hour() -> None:
    h0, h1 = datetime(2026, 6, 1, 10, tzinfo=UTC), datetime(2026, 6, 1, 11, tzinfo=UTC)
    rows = {
        "a": [{"start": h0.timestamp(), "change": 1.0}, {"start": h1.timestamp(), "change": 2.0}],
        "b": [{"start": h0, "change": 0.5}, {"start": h1.timestamp(), "change": None}],
    }

    assert summed(rows, ["a", "b"]) == {h0: 1.5}
    assert summed(rows, ["a"]) == {h0: 1.0, h1: 2.0}
    assert summed(rows, []) == {}
    assert as_power({h0: 0.25}) == {h0: 3.0}


def test_cloud_cover_is_read_per_hour() -> None:
    response = {
        "weather.forecast_home": {
            "forecast": [
                {"datetime": "2026-06-01T10:00:00+02:00", "cloud_coverage": 75},
                {"datetime": "2026-06-01T10:30:00+02:00", "cloud_coverage": 5},
                {"datetime": "2026-06-01T11:00:00+02:00", "cloud_coverage": "x"},
                {"datetime": "bad", "cloud_coverage": 10},
                "junk",
            ]
        }
    }

    assert hourly_cloud(response, "weather.forecast_home") == {datetime(2026, 6, 1, 8, tzinfo=UTC): 75.0}
    assert hourly_cloud(None, "weather.forecast_home") == {}
    assert pick_weather_entity(["weather.a", "weather.forecast_home"]) == "weather.forecast_home"
    assert pick_weather_entity(["weather.a", "weather.b"]) is None


@dataclass
class _Interval:
    utc_start: datetime
    eur_per_kwh: float


@dataclass
class _Document:
    intervals: tuple[_Interval, ...]

    def pieces(self) -> tuple[_Document, ...]:
        return (self,)


def test_an_hour_with_a_negative_quarter_is_a_negative_hour() -> None:
    hour = datetime(2026, 6, 1, 12, tzinfo=UTC)
    document = _Document(
        tuple(_Interval(hour + timedelta(minutes=15 * i), price) for i, price in enumerate((0.01, -0.002, 0.0, 0.03)))
        + (_Interval(hour + timedelta(hours=1), 0.05),)
    )

    assert negative_hours([document]) == {hour: True, hour + timedelta(hours=1): False}


def test_the_arrays_size_only_when_every_entry_states_it() -> None:
    assert array_kwp([{"modules_power": 6020}]) == pytest.approx(6.02)
    assert array_kwp([{"modules_power": 4000}, {"modules_power": 2000}]) == pytest.approx(6.0)
    assert array_kwp([{"modules_power": 4000}, {"api_key": "x"}]) is None
    assert array_kwp([None]) is None
    assert array_kwp([]) is None


# ---------------------------------------------------------------------------------------------- on a site

SUN_HOURS = range(8, 16)  # UTC


def _world(monkeypatch: pytest.MonkeyPatch, correction: Any, *, actual_share: float, export_share: float = 0.5) -> None:
    """A forecast of 2 kWh in each sun hour every day, and production `actual_share` of it, through the recorder's
    hourly and 5-minute statistics of one Energy dashboard solar source and one export meter."""

    async def read(_hass: HomeAssistant, entries: Any, *, now: datetime) -> ForecastReadResult:
        first = (now - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        hours = {first + timedelta(hours=i): (2000.0 if (first + timedelta(hours=i)).hour in SUN_HOURS else 0.0) for i in range(96)}
        return ForecastReadResult(wh_hours=hours, sources_read=tuple(entries))

    async def energy_ids() -> tuple[tuple[str, ...], tuple[str, ...]]:
        return ("sensor.pv",), ("sensor.export",)

    async def statistics(ids: Any, start: datetime, end: datetime, period: str) -> dict[str, list[dict[str, Any]]]:
        step = timedelta(hours=1) if period == "hour" else timedelta(minutes=5)
        rows: dict[str, list[dict[str, Any]]] = {statistic_id: [] for statistic_id in ids}
        moment = start
        while moment < end:
            sunny = moment.hour in SUN_HOURS
            hourly = 2.0 * actual_share if sunny else 0.0
            for statistic_id in ids:
                share = export_share if statistic_id == "sensor.export" else 1.0
                if period == "hour":
                    change = hourly * share
                else:
                    # Not flat within the hour: a real array is no plateau.
                    change = hourly * share / 12 * (0.9 + 0.2 * ((moment.minute // 5) % 2))
                rows[statistic_id].append({"start": moment.timestamp(), "change": change})
            moment += step
        return rows

    monkeypatch.setattr(shell, "async_read_forecast_wh", read)
    monkeypatch.setattr(correction, "_async_energy_ids", energy_ids)
    monkeypatch.setattr(correction, "_async_statistics", statistics)


async def _hours(correction: Any, first: datetime, count: int) -> datetime:
    moment = first
    for _ in range(count):
        await correction.async_refresh(moment)
        moment += timedelta(hours=1)
    return moment


async def test_a_week_on_a_site_learns_in_shadow_logs_both_and_reviews_them(
    hass: HomeAssistant, freezer, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry = await pilot_site(hass, freezer, **{CONF_SOLAR_FORECAST_ENTRIES: ["forecast_solar_1"]})
    controller = site_controller_for(hass, entry.entry_id)
    correction = controller.solar_correction
    _world(monkeypatch, correction, actual_share=0.75)

    start = datetime(2026, 9, 23, 0, 5, tzinfo=UTC)
    await _hours(correction, start, 8 * 24)

    state = correction.state
    assert state.status == STATUS_SHADOW
    assert state.curtailed is None
    assert 0.75 < state.factors[KIND_CLEAR] < 1.0
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    block = diagnostics["solar_correction"]
    assert block["status"] == STATUS_SHADOW
    assert block["sources"]["solar_statistics"] == ["sensor.pv"]
    assert block["observed_max_kwh"] == pytest.approx(1.5)
    assert block["inverter_ceiling_kw"] is None
    assert block["clipped_hours"] == []
    sunny = [hour for hour in block["next_hours"] if hour["raw_kwh"] > 0]
    assert sunny and all(hour["corrected_kwh"] < hour["raw_kwh"] for hour in sunny)
    assert all(hour["corrected_kwh"] >= 1.5 for hour in sunny)
    # The running hour is shown too, so the card's sun has no gap at it.
    last_refresh = correction.last_refresh
    assert block["next_hours"][0]["start"] == last_refresh.replace(minute=0, second=0, microsecond=0).isoformat()
    log = block["log"]
    assert set(log["kinds"]) == {KIND_SUN, KIND_SUN_CORRECTED}
    assert log["kinds"][KIND_SUN]["reviews"] and log["kinds"][KIND_SUN_CORRECTED]["reviews"]
    raw_week, corrected_week = log["last_week"][KIND_SUN], log["last_week"][KIND_SUN_CORRECTED]
    # The correction earns its place: the corrected series hits closer than the raw one.
    assert corrected_week["mae"] < raw_week["mae"]
    assert len(block["correction"]["history"]) >= 5


async def test_a_zero_export_site_is_left_alone(
    hass: HomeAssistant, freezer, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry = await pilot_site(hass, freezer, **{CONF_SOLAR_FORECAST_ENTRIES: ["forecast_solar_1"]})
    correction = site_controller_for(hass, entry.entry_id).solar_correction
    _world(monkeypatch, correction, actual_share=0.75, export_share=0.0)

    await _hours(correction, datetime(2026, 9, 23, 0, 5, tzinfo=UTC), 6 * 24)

    block = correction.snapshot()
    assert block["status"] == STATUS_OFF_CURTAILED
    assert block["curtailed"] == CURTAILED_ZERO_EXPORT
    assert correction.state.factors[KIND_CLEAR] == 1.0
    assert all(hour["corrected_kwh"] == hour["raw_kwh"] for hour in block["next_hours"])


async def test_without_a_forecast_it_does_nothing(hass: HomeAssistant, freezer) -> None:
    entry = await pilot_site(hass, freezer)
    controller = site_controller_for(hass, entry.entry_id)
    await controller.solar_correction.async_refresh(datetime(2026, 9, 23, 10, 5, tzinfo=UTC))

    block = controller.solar_correction.snapshot()
    assert block["status"] == STATUS_NO_FORECAST
    assert block["log"]["kinds"] == {}


async def test_the_factors_and_the_log_survive_a_restart(
    hass: HomeAssistant, freezer, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry = await pilot_site(hass, freezer, **{CONF_SOLAR_FORECAST_ENTRIES: ["forecast_solar_1"]})
    controller = site_controller_for(hass, entry.entry_id)
    _world(monkeypatch, controller.solar_correction, actual_share=0.5)
    await _hours(controller.solar_correction, datetime(2026, 9, 23, 0, 5, tzinfo=UTC), 5 * 24)
    kept = controller.solar_correction.state.as_dict()
    logged = controller.solar_correction.log.as_dict()
    assert kept["factors"][KIND_CLEAR] < 1.0

    monkeypatch.undo()
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    again = site_controller_for(hass, entry.entry_id)
    assert again is not controller
    assert again.solar_correction.state.as_dict()["factors"] == kept["factors"]
    assert again.solar_correction.log.as_dict()["kinds"] == logged["kinds"]


async def test_the_sun_log_kept_apart_before_moves_into_the_sites_log_once(
    hass: HomeAssistant, freezer, hass_storage: dict[str, Any]
) -> None:
    entry = await pilot_site(hass, freezer, **{CONF_SOLAR_FORECAST_ENTRIES: ["forecast_solar_1"]})
    controller = site_controller_for(hass, entry.entry_id)
    await controller.solar_correction.async_shutdown()
    made = dt_util.utcnow()
    start = made.replace(minute=0, second=0, microsecond=0) - timedelta(hours=3)
    # What wave 2 kept in the correction's own store: its state and its own log.
    hass_storage[shell.store_key(entry.entry_id)] = {
        "version": 1,
        "key": shell.store_key(entry.entry_id),
        "data": {
            "version": 1,
            "log": {
                "version": 1,
                "tz": "Europe/Stockholm",
                "kinds": {
                    KIND_SUN: {"hours": [[start.isoformat(), 2.0, made.isoformat(), 1.5]], "reviews": []},
                    KIND_SUN_CORRECTED: {"hours": [[start.isoformat(), 1.6, made.isoformat(), 1.5]], "reviews": []},
                },
            },
            "correction": controller.solar_correction.state.as_dict(),
        },
    }
    moved = shell.SiteSolarCorrection(hass, controller)
    await moved.async_start()
    await moved.async_shutdown()

    log = controller.house_forecast.log
    assert log.hours(KIND_SUN)[start].actual == 1.5
    assert log.hours(KIND_SUN_CORRECTED)[start].forecast == 1.6
    assert moved.log is log
    # The correction's store keeps its state only from now on.
    assert "log" not in hass_storage[shell.store_key(entry.entry_id)]["data"]


def test_five_minute_rows_become_quarter_hour_power() -> None:
    from custom_components.spotnav.site.solar_correction_shadow import quarter_power

    base = datetime(2026, 9, 23, 10, 0, tzinfo=UTC)
    rows = {base + timedelta(minutes=5 * index): 0.1 for index in range(4)}  # 10:00-10:20; the last quarter has one row
    assert quarter_power(rows) == {base: pytest.approx(1.2), base + timedelta(minutes=15): pytest.approx(1.2)}
    assert quarter_power({}) == {}


async def test_the_quarters_are_read_in_the_executor_once_per_five_minutes_and_empty_without_statistics(
    hass: HomeAssistant, freezer, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry = await pilot_site(hass, freezer, **{CONF_SOLAR_FORECAST_ENTRIES: ["forecast_solar_1"]})
    correction = site_controller_for(hass, entry.entry_id).solar_correction
    calls: list[tuple[str, datetime]] = []
    rows: dict[str, list[dict[str, Any]]] = {}

    async def energy_ids() -> tuple[tuple[str, ...], tuple[str, ...]]:
        return ("sensor.pv",), ()

    async def statistics(ids: Any, start: datetime, end: datetime, period: str) -> dict[str, list[dict[str, Any]]]:
        calls.append((period, end))
        return rows

    monkeypatch.setattr(correction, "_async_energy_ids", energy_ids)
    monkeypatch.setattr(correction, "_async_statistics", statistics)
    step = datetime(2026, 9, 23, 10, 5, tzinfo=UTC)
    rows["sensor.pv"] = [{"start": (step - timedelta(minutes=5)).timestamp(), "change": 0.1}]
    await correction.async_refresh_quarters(step)
    assert calls == [("5minute", step)]
    assert correction.quarters == {step.replace(minute=0): pytest.approx(1.2)}
    rows.clear()
    await correction.async_refresh_quarters(step + timedelta(minutes=5))
    assert correction.quarters == {}


async def test_a_running_read_of_the_quarters_is_cancelled_at_shutdown(
    hass: HomeAssistant, freezer, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    entry = await pilot_site(hass, freezer, **{CONF_SOLAR_FORECAST_ENTRIES: ["forecast_solar_1"]})
    correction = site_controller_for(hass, entry.entry_id).solar_correction
    started = asyncio.Event()

    async def energy_ids() -> tuple[tuple[str, ...], tuple[str, ...]]:
        started.set()
        await asyncio.Event().wait()  # a read that never ends
        return (), ()

    monkeypatch.setattr(correction, "_async_energy_ids", energy_ids)
    correction._quarters_for = None
    correction._quarters_task = None
    correction._tick_quarters(datetime(2026, 9, 23, 10, 7, tzinfo=UTC))
    task = correction._quarters_task
    assert task is not None
    await started.wait()
    await correction.async_shutdown()
    await asyncio.sleep(0)
    assert task.cancelled()
