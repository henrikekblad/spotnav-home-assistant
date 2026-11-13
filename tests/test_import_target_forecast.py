"""The house load forecast under the import target: what a plan counts on per hour, the departure's shortfall per hour
and the release reach's running hour. The hold itself stays on measurements.

The pure shortfall first, then the owner's HALO world (`tests/test_import_target_hold.py`) with a forecast put in place
of the hourly refresh.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from freezegun import freeze_time
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.spotnav.site.import_target import departure_shortfall_kwh
from custom_components.spotnav.site.load_forecast import (
    ForecastHour,
    LoadForecast,
    NO_TEMPERATURE,
    SOURCE_FLAT,
    SOURCE_PROFILE,
)

from .test_import_target_hold import _HOUR, _V, _charging, _plan, _world

HOUR = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)


# -- the pure shortfall, per hour


def test_a_later_hour_the_forecast_knows_is_reckoned_at_its_own_figure() -> None:
    now = HOUR + timedelta(minutes=30)
    common = {"now": now, "hour_end": HOUR + timedelta(hours=1), "held_kw": 4.8, "later_kw": 5.0}
    departure = HOUR + timedelta(hours=4)
    flat = departure_shortfall_kwh(need_kwh=17.0, departure=departure, **common)
    # No hour named, or an empty map: exactly the flat reckoning.
    assert departure_shortfall_kwh(need_kwh=17.0, departure=departure, later_kw_by_hour={}, **common) == flat
    same = {HOUR + timedelta(hours=index): 5.0 for index in range(1, 4)}
    assert departure_shortfall_kwh(need_kwh=17.0, departure=departure, later_kw_by_hour=same, **common) == pytest.approx(
        flat
    )
    # An evening hour that leaves 2 kW instead of 5: 3 kWh more short; an hour not named stays at 5.
    evening = {HOUR + timedelta(hours=2): 2.0}
    assert departure_shortfall_kwh(
        need_kwh=17.0, departure=departure, later_kw_by_hour=evening, **common
    ) == pytest.approx(flat + 3.0)
    # A departure inside a later hour counts that hour for the part before it.
    partial = {HOUR + timedelta(hours=3): 1.0}
    assert departure_shortfall_kwh(
        need_kwh=17.0, departure=HOUR + timedelta(hours=3, minutes=30), later_kw_by_hour=partial, **common
    ) == pytest.approx(17.0 - 2.4 - 10.0 - 0.5)
    # This hour stays at the held power whatever the map says of it.
    this_hour = {HOUR: 0.0}
    assert departure_shortfall_kwh(
        need_kwh=17.0, departure=departure, later_kw_by_hour=this_hour, **common
    ) == pytest.approx(flat)


# -- the site


def _forecast(now: datetime, house_kw: dict[int, float], *, default: float = 1.0, flat_hours=()) -> LoadForecast:
    """A forecast made `now` for 24 hours from the running hour: `house_kw` by hours from now (0 = the running hour),
    `default` elsewhere; `flat_hours` on the flat rung."""
    first = now.replace(minute=0, second=0, microsecond=0)
    hours = tuple(
        ForecastHour(
            first + timedelta(hours=index),
            house_kw.get(index, default),
            SOURCE_FLAT if index in flat_hours else SOURCE_PROFILE,
        )
        for index in range(24)
    )
    return LoadForecast(made_at=now, hours=hours, samples=500, temperature=NO_TEMPERATURE, holidays=False)


def _use(world, forecast: LoadForecast | None, monkeypatch) -> None:
    """Put `forecast` in place and keep the hourly refresh from replacing it."""
    house = world.halo.controller.house_forecast
    monkeypatch.setattr(house, "tick", lambda: None)
    house.forecast = forecast


MINIMUM_KW = 6 * 3 * _V / 1000.0


async def test_a_plan_counts_on_the_target_less_the_forecast_per_hour(hass: HomeAssistant, monkeypatch) -> None:
    with freeze_time(_HOUR + timedelta(seconds=5)) as frozen:
        world = await _world(hass, monkeypatch, frozen, "fcplan")
        charger = world.halo.charger.entry_id
        controller = world.halo.controller
        await world.step()
        await world.step()
        _use(world, None, monkeypatch)
        # No forecast: nothing per hour, the flat figure as before.
        assert controller.import_target_plan_kw_by_hour(charger) is None
        assert controller.import_target_plan_kw(charger) == pytest.approx(4.88, abs=0.01)
        now = dt_util.utcnow()
        _use(world, _forecast(now, {1: 0.4, 3: 4.0}, flat_hours=(5,)), monkeypatch)
        by_hour = controller.import_target_plan_kw_by_hour(charger)
        first = now.replace(minute=0, second=0, microsecond=0)
        assert by_hour is not None
        # The target less its margin (5.88 kW), less each hour's expected load; never below the car's minimum.
        assert by_hour[first + timedelta(hours=1)] == pytest.approx(5.48)
        assert by_hour[first + timedelta(hours=2)] == pytest.approx(4.88)
        assert by_hour[first + timedelta(hours=3)] == pytest.approx(MINIMUM_KW)
        # An hour on the flat rung is left to the flat figure, which stays the live one.
        assert first + timedelta(hours=5) not in by_hour
        assert controller.import_target_plan_kw(charger) == pytest.approx(4.88, abs=0.01)


async def test_on_a_battery_site_a_plan_counts_on_the_net_import_not_the_houses_whole_load(
    hass: HomeAssistant, monkeypatch
) -> None:
    """The import target is about the hour's net import: where the house figure is the whole load (a battery site),
    a plan, the shortfall and the release reach count on the import less the chargers (`import_forecast`)."""
    with freeze_time(_HOUR + timedelta(seconds=5)) as frozen:
        world = await _world(hass, monkeypatch, frozen, "fcgross")
        charger = world.halo.charger.entry_id
        controller = world.halo.controller
        await world.step()
        now = dt_util.utcnow()
        _use(world, _forecast(now, {}, default=3.0), monkeypatch)  # the whole load: the battery covers most of it
        house = controller.house_forecast
        house.import_forecast = _forecast(now, {}, default=0.4)  # what the meter will see
        first = now.replace(minute=0, second=0, microsecond=0)
        by_hour = controller.import_target_plan_kw_by_hour(charger)
        assert by_hour is not None and by_hour[first + timedelta(hours=1)] == pytest.approx(5.88 - 0.4)
        assert house.plan_kw_at(now) == pytest.approx(0.4)
        # Without the second forecast (a site without a battery) the one forecast is the import's.
        house.import_forecast = None
        assert controller.import_target_plan_kw_by_hour(charger)[first + timedelta(hours=1)] == pytest.approx(
            MINIMUM_KW if 5.88 - 3.0 < MINIMUM_KW else 5.88 - 3.0
        )


async def test_a_stale_forecast_or_no_target_gives_nothing_per_hour(hass: HomeAssistant, monkeypatch) -> None:
    with freeze_time(_HOUR + timedelta(seconds=5)) as frozen:
        world = await _world(hass, monkeypatch, frozen, "fcstale")
        charger = world.halo.charger.entry_id
        await world.step()
        now = dt_util.utcnow()
        _use(world, _forecast(now - timedelta(hours=4), {}), monkeypatch)
        assert world.halo.controller.import_target_plan_kw_by_hour(charger) is None
    with freeze_time(_HOUR + timedelta(hours=3, seconds=5)) as frozen:
        free = await _world(hass, monkeypatch, frozen, "fcnone", target_kw=None)
        await free.step()
        _use(free, _forecast(dt_util.utcnow(), {}), monkeypatch)
        assert free.halo.controller.import_target_plan_kw_by_hour(free.halo.charger.entry_id) is None


async def test_the_hold_itself_stays_on_measurements(hass: HomeAssistant, monkeypatch) -> None:
    """A forecast of an empty house changes nothing the hold does: the car is held to what the meter leaves."""
    with freeze_time(_HOUR + timedelta(seconds=5)) as frozen:
        world = await _world(hass, monkeypatch, frozen, "fchold")
        await world.step()
        _use(world, _forecast(dt_util.utcnow(), {0: 0.0, 1: 0.0}, default=0.0), monkeypatch)
        now = dt_util.utcnow()
        departure = now + timedelta(hours=6)
        world.halo.cc.plan = _plan(now, departure, [(now, departure)])
        await _charging(world)
        await world.run(10)
        assert world.car_a == 7.0, "held at what the target leaves beside the house the meter sees"


async def test_the_departure_s_shortfall_counts_each_later_hour_at_its_forecast(hass: HomeAssistant, monkeypatch) -> None:
    with freeze_time(_HOUR + timedelta(seconds=5)) as frozen:
        world = await _world(hass, monkeypatch, frozen, "fcshort")
        charger = world.halo.charger.entry_id
        controller = world.halo.controller
        await world.step()
        _use(world, None, monkeypatch)
        now = dt_util.utcnow()
        departure = now + timedelta(hours=3, minutes=30)
        world.halo.cc.plan = _plan(now, departure, [(now, departure)])
        await _charging(world)
        await world.run(5)
        status = controller.import_target_status(charger)
        assert status is not None and status.at_risk is not None
        flat = status.at_risk.missing_kwh
        # A forecast of 4 kW in the house over the next hours: the target leaves the car its minimum then, and the
        # departure misses more.
        _use(world, _forecast(dt_util.utcnow(), {1: 4.0, 2: 4.0, 3: 4.0}), monkeypatch)
        await world.run(2)
        status = controller.import_target_status(charger)
        assert status is not None and status.at_risk is not None
        assert status.at_risk.missing_kwh > flat + 1.0


async def test_the_release_reach_takes_the_running_hour_s_forecast(hass: HomeAssistant, monkeypatch) -> None:
    from dataclasses import replace

    from custom_components.spotnav.api import dashboard as dashboard_api
    from custom_components.spotnav.planning.status_compose import compose_status, PlanningFacts

    from .test_import_target_hold import _start_by_hand

    ready = {"has_settings": True, "planning": PlanningFacts(state="proposal_ready", reason="ready")}
    with freeze_time(_HOUR + timedelta(seconds=5)) as frozen:
        world = await _world(hass, monkeypatch, frozen, "fcreach")
        await world.step()
        _use(world, _forecast(dt_util.utcnow(), {0: 3.0}), monkeypatch)
        await _start_by_hand(world)
        await world.run(10)
        facts = dashboard_api.status_facts(dashboard_api.capture_dashboard(hass, world.halo.charger))
        lines = compose_status(replace(facts, **ready))["lines"]
        held = next(line for line in lines if line["code"] == "import_target_held")
        # The forecast's 3 kW in the running hour (the meter's typical 1 kW before it) and 16 A on three phases.
        assert held["params"]["release_peak_kw"] == 14.0
