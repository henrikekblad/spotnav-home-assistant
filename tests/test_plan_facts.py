"""The two facts the status line gained: what a plan saves against charging now (`PlanResult.now_cost`) and when
the sun is expected (hybrid's `sun_start`/`sun_end`, the solar strategy's forecast window), and how an app that
does not read them gets the status it knows."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from custom_components.spotnav.api.dashboard import solar_forecast_window
from custom_components.spotnav.api.webhook import _status_for_app
from custom_components.spotnav.planning.hybrid_plan import HybridConfig, PriceSlot, plan_hybrid
from custom_components.spotnav.planning.planner import calculate_plan, energy_per_slot_kwh

from .planning import build_request, parse_documents

STOCKHOLM = ZoneInfo("Europe/Stockholm")
PER_SLOT_KWH = energy_per_slot_kwh(16, 3)


def day(date: str, count: int, start: str, prices: dict[int, float]) -> dict[str, Any]:
    """One 15-minute day document: 1.0 EUR/kWh, with the given indexes priced otherwise."""
    return {
        "area": "SE4",
        "date": date,
        "tz": "Europe/Stockholm",
        "res": 15,
        "start": start,
        "unit": "EUR/kWh",
        "prices": [prices.get(index, 1.0) for index in range(count)],
    }


def plan(now: str, slots: int, documents: list[dict[str, Any]], departure: str | None = "07:00") -> Any:
    return calculate_plan(
        build_request(
            {
                "area_id": "SE4",
                "timezone": "Europe/Stockholm",
                "currency": "EUR",
                "now": now,
                "phases": 3,
                "amps": 16,
                "requested_kwh": slots * PER_SLOT_KWH - 0.01,
                "consumption_kwh_per_10km": 2.0,
                "max_periods": 2,
                "departure": departure,
            },
            parse_documents(documents),
        )
    )


# ------------------------------------------------------------------------------------------- charging now


def test_charging_now_is_the_same_slots_straight_from_the_first_usable_one() -> None:
    # 18:00 to 22:00 is dear (3.0), the night cheap (0.5): four slots now cost 12 x per slot, the plan 2 x.
    dear = {index: 3.0 for index in range(72, 88)}
    cheap = {index: 0.5 for index in range(0, 28)}
    documents = [
        day("2026-09-22", 96, "2026-09-22T00:00:00+02:00", dear),
        day("2026-09-23", 96, "2026-09-23T00:00:00+02:00", cheap),
    ]
    result = plan("2026-09-22T18:00:00+02:00", 4, documents)
    assert result.reason is None
    assert result.estimated_cost == pytest.approx(4 * PER_SLOT_KWH * 0.5)
    assert result.now_cost == pytest.approx(4 * PER_SLOT_KWH * 3.0)


def test_a_plan_that_starts_now_costs_what_charging_now_costs() -> None:
    cheap_now = {index: 0.2 for index in range(72, 76)}
    documents = [
        day("2026-09-22", 96, "2026-09-22T00:00:00+02:00", cheap_now),
        day("2026-09-23", 96, "2026-09-23T00:00:00+02:00", {}),
    ]
    result = plan("2026-09-22T18:00:00+02:00", 4, documents)
    assert result.now_cost == pytest.approx(result.estimated_cost)


def test_charging_now_counts_elapsed_quarter_hours_across_the_autumn_clock_change() -> None:
    # 2026-10-25 has 25 hours: 00:00+02:00 to 03:00+02:00 then 02:00+01:00 again. From 01:30+02:00 the next
    # eight slots run to 03:30 by elapsed time, through the repeated hour; indexes 6..13 are those eight.
    dear = {index: 4.0 for index in range(6, 14)}
    documents = [day("2026-10-25", 100, "2026-10-25T00:00:00+02:00", dear)]
    result = plan("2026-10-25T01:30:00+02:00", 8, documents, departure="12:00")
    assert result.reason is None
    assert result.now_cost == pytest.approx(8 * PER_SLOT_KWH * 4.0)
    assert result.estimated_cost == pytest.approx(8 * PER_SLOT_KWH * 1.0)


def test_no_plan_has_no_charging_now_figure() -> None:
    documents = [day("2026-09-22", 96, "2026-09-22T00:00:00+02:00", {})]
    result = plan("2026-09-22T23:00:00+02:00", 40, documents, departure=None)
    assert result.reason is not None
    assert result.now_cost is None


# ---------------------------------------------------------------------------------------------- the sun


NOW = datetime(2026, 6, 10, 6, 0, tzinfo=timezone.utc)


def sunny_day(start: datetime, hours: range, wh: float = 4000.0) -> dict[datetime, float]:
    return {start + timedelta(hours=hour): wh for hour in hours}


def test_hybrid_says_when_the_sun_it_counts_on_is_expected() -> None:
    result = plan_hybrid(
        now=NOW,
        departure=NOW + timedelta(hours=24),
        remaining_need_kwh=10.0,
        max_charge_kw=11.0,
        config=HybridConfig(solar_start_w=1400.0),
        price_slots=tuple(PriceSlot(NOW + timedelta(minutes=15 * i), 900.0, 5.0) for i in range(96)),
        forecast_wh=sunny_day(NOW.replace(hour=0), range(8, 15)),
    )
    assert result.credit_kwh > 0
    assert result.sun_start == datetime(2026, 6, 10, 8, 0, tzinfo=timezone.utc)
    assert result.sun_end == datetime(2026, 6, 10, 15, 0, tzinfo=timezone.utc)


LIMITS = (11.0, 1400.0)


def test_solar_tells_the_rest_of_todays_sun() -> None:
    forecast = sunny_day(NOW.replace(hour=0), range(8, 15))
    found = solar_forecast_window(forecast, now=NOW, tz="Europe/Stockholm", limits=LIMITS)
    assert found is not None
    kwh, start, end = found
    # 2.6 kW usable to the car for seven hours, trusted by half as hybrid does.
    assert kwh == pytest.approx(7 * 3.5 * 0.5)
    assert (start, end) == (NOW.replace(hour=8), NOW.replace(hour=15))


def test_after_todays_sun_solar_tells_tomorrows() -> None:
    evening = NOW.replace(hour=19)
    forecast = {**sunny_day(NOW.replace(hour=0), range(8, 15)), **sunny_day(NOW.replace(hour=0) + timedelta(days=1), range(9, 12))}
    found = solar_forecast_window(forecast, now=evening, tz="Europe/Stockholm", limits=LIMITS)
    assert found is not None
    _, start, end = found
    assert start == datetime(2026, 6, 11, 9, 0, tzinfo=timezone.utc)
    assert end == datetime(2026, 6, 11, 12, 0, tzinfo=timezone.utc)


def test_a_dull_forecast_or_none_at_all_tells_nothing() -> None:
    assert solar_forecast_window({}, now=NOW, tz="Europe/Stockholm", limits=LIMITS) is None
    dull = sunny_day(NOW.replace(hour=0), range(8, 15), wh=900.0)
    assert solar_forecast_window(dull, now=NOW, tz="Europe/Stockholm", limits=LIMITS) is None
    assert solar_forecast_window(sunny_day(NOW, range(3)), now=NOW, tz="Europe/Stockholm", limits=(0.0, 1400.0)) is None


def test_the_days_are_the_markets_across_the_spring_clock_change() -> None:
    # 2026-03-29 in Stockholm is 23 hours: its local midnight that ends it is 22:00Z.
    now = datetime(2026, 3, 29, 20, 0, tzinfo=timezone.utc)
    late = sunny_day(datetime(2026, 3, 29, 21, 0, tzinfo=timezone.utc), range(1))
    tomorrow = sunny_day(datetime(2026, 3, 30, 9, 0, tzinfo=timezone.utc), range(2))
    found = solar_forecast_window({**late, **tomorrow}, now=now, tz="Europe/Stockholm", limits=LIMITS)
    assert found is not None
    assert found[1] == datetime(2026, 3, 29, 21, 0, tzinfo=timezone.utc), "21:00Z is still the 29th locally"
    found = solar_forecast_window(tomorrow, now=now, tz="Europe/Stockholm", limits=LIMITS)
    assert found is not None and found[1].astimezone(STOCKHOLM).date().isoformat() == "2026-03-30"


# ------------------------------------------------------------------------------------------ the paired app


def test_an_app_that_does_not_read_the_new_facts_gets_the_lines_it_knows() -> None:
    saving = {"code": "plan_saving", "params": {"amount_minor": 750, "currency": "SEK"}}
    forecast = {"code": "solar_forecast", "params": {"kwh": 6.0, "start": "x", "end": "y"}}
    hybrid = {
        "code": "hybrid_grid",
        "params": {"grid_kwh": 1.0, "credit_kwh": 2.0, "window_start": None, "window_end": None, "sun_start": "a", "sun_end": "b"},
    }
    assert _status_for_app(saving, set()) is None
    assert _status_for_app(forecast, set()) is None
    assert _status_for_app(hybrid, set())["params"] == {"grid_kwh": 1.0, "credit_kwh": 2.0, "window_start": None, "window_end": None}
    for line in (saving, forecast, hybrid):
        assert _status_for_app(line, {"plan_facts"}) == line


# ------------------------------------------------------------------------------------------ on the dashboard


@pytest.mark.usefixtures("offline_relay")
async def test_the_solar_strategy_tells_its_sun_on_the_dashboard(hass, monkeypatch) -> None:
    from freezegun import freeze_time

    from custom_components.spotnav.api import dashboard as dashboard_api
    from custom_components.spotnav.planning.auto_settings import STRATEGY_SOLAR
    from custom_components.spotnav.planning.hybrid_forecast import ForecastReadResult

    from .test_dashboard_api import NOW as FIXTURE_NOW
    from .test_dashboard_fixtures import _forecast_entry, _set_strategy, _setup_charger, _setup_derived_site
    from .world import go_auto

    tomorrow = (datetime.fromisoformat(FIXTURE_NOW).replace(tzinfo=timezone.utc) + timedelta(days=1)).replace(hour=8, minute=0, second=0, microsecond=0)
    asked: list[Any] = []

    async def _fake_read(_hass: Any, entry_ids: Any) -> ForecastReadResult:
        asked.append(list(entry_ids))
        return ForecastReadResult(wh_hours=sunny_day(tomorrow, range(4), wh=10000.0), sources_read=tuple(entry_ids))

    monkeypatch.setattr(dashboard_api, "async_read_forecast_wh", _fake_read)
    with freeze_time(FIXTURE_NOW):
        roof = _forecast_entry(hass, entry_id="roof_solar", title="Roof")
        charger = await _setup_charger(hass, entry_id="solar_sun")
        await _set_strategy(hass, charger.entry_id, STRATEGY_SOLAR)
        await _setup_derived_site(
            hass, site_entry_id="solar_sun_site", charger_entry_id=charger.entry_id, solar_forecast_entries=[roof.entry_id]
        )
        await go_auto(hass, charger.entry_id, strategy=STRATEGY_SOLAR, amps=16, phases=3)
        forecast = await dashboard_api.async_solar_forecast_wh(hass, charger)
        body = dashboard_api.serialize_dashboard(
            dashboard_api.capture_dashboard(hass, charger, solar_forecast_wh=forecast), can_act=True
        )
    lines = body["status"]["lines"]
    assert asked == [[roof.entry_id]]
    found = [line for line in lines if line["code"] == "solar_forecast"]
    assert found, lines
    assert found[0]["params"]["start"] == tomorrow.isoformat()
    assert found[0]["params"]["end"] == (tomorrow + timedelta(hours=4)).isoformat()
