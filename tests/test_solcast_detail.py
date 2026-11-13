"""Solcast's p10/p50/p90 (`site/solcast_detail.py`): the rows as the integration states them, how they are found in
Home Assistant, and their log kinds in the sun forecast's shell (`site/solar_correction_shadow.py`)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.spotnav.const import CONF_SOLAR_FORECAST_ENTRIES
from custom_components.spotnav.runtime import site_controller_for
from custom_components.spotnav.site import solar_correction_shadow as shell
from custom_components.spotnav.site.forecast_log import KIND_SUN, KIND_SUN_P10, KIND_SUN_P50, KIND_SUN_P90
from custom_components.spotnav.site.solcast_detail import (
    SOLCAST_DOMAIN,
    merge,
    read_detail,
    rows_by_hour,
    sensor_detail,
)

from .test_demand_site import pilot_site
from .test_solar_correction_shadow import _world

pytestmark = pytest.mark.usefixtures("offline_relay")


def _half_hours(day: str, offset: str, p50: float, p10: float, p90: float, hours: range = range(8, 12)) -> list[dict]:
    """Solcast's `detailedForecast`: 30-minute rows of mean kW (as on the owner's installation)."""
    return [
        {
            "period_start": f"{day}T{hour:02d}:{minute:02d}:00{offset}",
            "pv_estimate": p50,
            "pv_estimate10": p10,
            "pv_estimate90": p90,
        }
        for hour in hours
        for minute in (0, 30)
    ]


def _hourly(day: str, offset: str, p50: float, p10: float, p90: float, hours: range = range(8, 12)) -> list[dict]:
    return [
        {"period_start": f"{day}T{hour:02d}:00:00{offset}", "pv_estimate": p50, "pv_estimate10": p10, "pv_estimate90": p90}
        for hour in hours
    ]


def test_half_hourly_rows_are_half_an_hour_of_their_kw_summed_into_the_utc_hour() -> None:
    detail = rows_by_hour(_half_hours("2026-11-13", "+01:00", 4.0, 2.0, 5.0, range(10, 12)))

    ten = datetime(2026, 11, 13, 9, tzinfo=UTC)
    assert detail["p50"] == {ten: 4.0, ten + timedelta(hours=1): 4.0}
    assert detail["p10"][ten] == 2.0 and detail["p90"][ten] == 5.0


def test_hourly_rows_are_an_hour_of_their_kw() -> None:
    detail = rows_by_hour(_hourly("2026-11-13", "+01:00", 3.0, 1.0, 4.0, range(10, 11)))

    assert detail == {"p50": {datetime(2026, 11, 13, 9, tzinfo=UTC): 3.0}, "p10": {datetime(2026, 11, 13, 9, tzinfo=UTC): 1.0}, "p90": {datetime(2026, 11, 13, 9, tzinfo=UTC): 4.0}}


def test_a_sensor_prefers_its_hourly_list_and_falls_back_to_the_half_hourly_one() -> None:
    both = {"detailedHourly": _hourly("2026-11-13", "+01:00", 2.0, 1.0, 3.0), "detailedForecast": _half_hours("2026-11-13", "+01:00", 9.0, 9.0, 9.0)}
    only_half = {"detailedForecast": _half_hours("2026-11-13", "+01:00", 2.0, 1.0, 3.0)}

    first = datetime(2026, 11, 13, 7, tzinfo=UTC)
    assert sensor_detail(both)["p50"][first] == 2.0
    assert sensor_detail(only_half)["p50"][first] == 2.0
    assert sensor_detail({}) == {"p10": {}, "p50": {}, "p90": {}}


def test_bad_rows_and_fields_are_left_out_alone() -> None:
    rows = [
        {"period_start": "2026-11-13T10:00:00+01:00", "pv_estimate": 2.0, "pv_estimate10": "x", "pv_estimate90": float("nan")},
        {"period_start": "2026-11-13T11:00:00", "pv_estimate": 5.0},
        {"period_start": "bad", "pv_estimate": 5.0},
        {"period_start": "2026-11-13T12:00:00+01:00", "pv_estimate": -1.0, "pv_estimate10": True, "pv_estimate90": None},
        "junk",
        {"period_start": "2026-11-13T10:00:00+01:00", "pv_estimate": 99.0},
    ]

    detail = rows_by_hour(rows)

    nine, eleven = datetime(2026, 11, 13, 9, tzinfo=UTC), datetime(2026, 11, 13, 11, tzinfo=UTC)
    assert detail["p50"] == {nine: 2.0, eleven: 0.0}
    assert detail["p10"] == {} and detail["p90"] == {}
    assert rows_by_hour(None) == {"p10": {}, "p50": {}, "p90": {}}


def test_the_hourly_total_matches_the_sensors_state() -> None:
    # The owner's installation, 2026-11-13: `detailedForecast` summed as kW times half an hour is the sensor's kWh.
    kw = [0.0, 0.5, 1.5, 3.0, 5.1, 4.2, 3.0, 1.5, 0.4, 0.0]
    rows = [
        {"period_start": (datetime(2026, 11, 13, 6, tzinfo=UTC) + timedelta(minutes=30 * i)).isoformat(), "pv_estimate": value}
        for i, value in enumerate(kw)
    ]

    assert sum(rows_by_hour(rows)["p50"].values()) == pytest.approx(sum(kw) / 2)


def test_installations_add_up() -> None:
    hour = datetime(2026, 11, 13, 9, tzinfo=UTC)
    one = {"p10": {hour: 1.0}, "p50": {hour: 2.0}, "p90": {hour: 3.0}}

    assert merge([one, one])["p50"] == {hour: 4.0}


def _solcast(hass: HomeAssistant, *, entry_id: str = "solcast1", state: str = "20") -> str:
    entry = MockConfigEntry(domain=SOLCAST_DOMAIN, entry_id=entry_id)
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    ids = {}
    for unique_id, day, p50 in (("total_kwh_forecast_today", "2026-11-13", 4.0), ("total_kwh_forecast_tomorrow", "2026-11-14", 2.0)):
        entity = registry.async_get_or_create(
            "sensor", SOLCAST_DOMAIN, unique_id, config_entry=entry, suggested_object_id=f"my_sun_{unique_id}"
        )
        ids[unique_id] = entity.entity_id
        hass.states.async_set(
            entity.entity_id,
            state,
            {
                "detailedHourly": _hourly(day, "+01:00", p50, p50 / 2, p50 * 1.5, range(9, 12)),
                "detailedForecast": _half_hours(day, "+01:00", p50, p50 / 2, p50 * 1.5, range(9, 12)),
                "analysis": {"confidence": 0.5},
            },
        )
    return entry.entry_id


async def test_the_detail_is_found_by_unique_id_whatever_the_entities_are_called(hass: HomeAssistant) -> None:
    entry_id = _solcast(hass)
    other = MockConfigEntry(domain="forecast_solar", entry_id="fs")
    other.add_to_hass(hass)

    detail = read_detail(hass, ["fs", entry_id, "missing"])

    assert detail is not None
    today, tomorrow = datetime(2026, 11, 13, 9, tzinfo=UTC), datetime(2026, 11, 14, 9, tzinfo=UTC)
    assert detail["p50"][today] == 4.0 and detail["p10"][today] == 2.0 and detail["p90"][today] == 6.0
    assert detail["p50"][tomorrow] == 2.0
    assert len(detail["p50"]) == 6


async def test_nothing_without_a_solcast_entry_or_with_unavailable_sensors(hass: HomeAssistant) -> None:
    MockConfigEntry(domain="forecast_solar", entry_id="fs").add_to_hass(hass)
    assert read_detail(hass, ["fs"]) is None
    assert read_detail(hass, []) is None

    entry_id = _solcast(hass, state="unavailable")
    assert read_detail(hass, [entry_id]) is None


async def test_another_entry_s_sensors_are_not_read(hass: HomeAssistant) -> None:
    _solcast(hass, entry_id="solcast1")
    MockConfigEntry(domain=SOLCAST_DOMAIN, entry_id="solcast2").add_to_hass(hass)

    assert read_detail(hass, ["solcast2"]) is None


async def test_the_shell_logs_and_scores_the_three_estimates_beside_the_raw_forecast(
    hass: HomeAssistant, freezer, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry = await pilot_site(hass, freezer, **{CONF_SOLAR_FORECAST_ENTRIES: ["forecast_solar_1"]})
    correction = site_controller_for(hass, entry.entry_id).solar_correction
    _world(monkeypatch, correction, actual_share=0.75)

    def detail(_hass: HomeAssistant, entries: Any) -> dict[str, dict[datetime, float]]:
        first = datetime(2026, 9, 23, 0, tzinfo=UTC)
        hours = [first + timedelta(hours=i) for i in range(96)]
        sun = {h: 2.0 if h.hour in range(8, 16) else 0.0 for h in hours}
        return {"p10": {h: v * 0.5 for h, v in sun.items()}, "p50": sun, "p90": {h: v * 1.2 for h, v in sun.items()}}

    monkeypatch.setattr(shell, "read_detail", detail)

    moment = datetime(2026, 9, 23, 0, 5, tzinfo=UTC)
    for _ in range(3 * 24):
        await correction.async_refresh(moment)
        moment += timedelta(hours=1)

    block = correction.snapshot()
    kinds = block["log"]["kinds"]
    assert {KIND_SUN, KIND_SUN_P10, KIND_SUN_P50, KIND_SUN_P90} <= set(kinds)
    assert block["solcast_hours"] == 96
    week = block["log"]["last_week"]
    # Production is 1.5 kWh in a sun hour: a p10 of 1.0 is below it, a p90 of 2.4 above it.
    # (Night hours, forecast and outcome both zero, are neither under nor over.)
    assert week[KIND_SUN_P10]["under_share"] > 0.25
    assert week[KIND_SUN_P90]["under_share"] == 0.0
    assert week[KIND_SUN_P50]["mae"] == pytest.approx(week[KIND_SUN]["mae"])
    assert kinds[KIND_SUN_P10]["reviews"] and kinds[KIND_SUN_P90]["reviews"]


async def test_a_site_without_solcast_logs_no_solcast_kinds(
    hass: HomeAssistant, freezer, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry = await pilot_site(hass, freezer, **{CONF_SOLAR_FORECAST_ENTRIES: ["forecast_solar_1"]})
    correction = site_controller_for(hass, entry.entry_id).solar_correction
    _world(monkeypatch, correction, actual_share=0.75)

    await correction.async_refresh(datetime(2026, 9, 23, 10, 5, tzinfo=UTC))

    assert KIND_SUN_P50 not in correction.snapshot()["log"]["kinds"]
    assert correction.snapshot()["solcast_hours"] == 0
