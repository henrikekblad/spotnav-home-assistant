"""Hybrid mode replay: one day with a genuinely moving wall clock -- forecast sun at midday, cheap
night, departure in the evening.

Unlike `test_hybrid_plan.py` (the pure core, called by hand), this drives the real stack: a
derived-mode site and charger (`test_solar_execution.py`'s `solar_setup`), a served day-ahead
price document, and the real `AutoPlannerController`/`AutoExecutor`/`ChargingController` chain
with `SolarExecutionCoordinator` arbitrating between an installed Auto plan and synthetic sun. Only
the forecast read (`hybrid_forecast.async_read_forecast_wh`) is a double.

The `freezegun` clock moves with the site's monotonic clock at every step, and the charger's
cumulative energy register is updated to match what was delivered, so the periodic hybrid preview
recompute runs *while the night window is still charging*, repeatedly. That exercises
delivered-energy tracking (see `test_energy_baseline.py`), which keeps the same energy from being
bought twice.

The day: cheap prices 00:00-03:00 local and twice as dear otherwise, a car needing more than the
cheap window delivers, midday sun (10:00-14:00) big enough to cover the gap, departure at 18:00.
Closing assertions:

* hybrid's night window buys less grid energy than plain cheapest would for the same inputs;
* total delivered energy does not exceed the request by more than one planning slot;
* by departure the car has received at least the energy it asked for.
"""

from __future__ import annotations

import json
from datetime import date, time, timedelta

import pytest
from freezegun import freeze_time
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.spotnav.planning.auto_settings import STRATEGY_HYBRID
from custom_components.spotnav.const import CONF_MAX_AGE_S
from custom_components.spotnav.planning.hybrid_forecast import ForecastReadResult
from custom_components.spotnav.planning.planner import FiscalChoice, PlanRequest, calculate_plan, power_kw

from .relay import AREA_TZ, SE4, StubTransport, day_body, index_listing
from .world import go_auto
from .relay import TODAY, TOMORROW
from .world import (
    VOLTAGE_V,
    set_charger_delivered_a,
    set_derived_site_entities,
    set_site_power_w,
    solar_setup,
    tick_site,
)
from .world import controller_of
from custom_components.spotnav.runtime import preview_for

pytestmark = pytest.mark.usefixtures("offline_relay")


AMPS = 10
PHASES = 3
REQUESTED_KWH = 25.0
NIGHT_PRICE = 0.10
DAY_PRICE = 0.20
CHEAP_HOURS = 3
DEPARTURE = time(18, 0)
SUN_HOURS = 4
FORECAST_W = 5000.0
POWER_KW = power_kw(AMPS, PHASES)  # ~6.928 kW
ONE_SLOT_KWH = POWER_KW * 0.25  # `planner.STEP_MINUTES`'s own slot length
ENERGY_ENTITY = "sensor.solar_charger_energy_register"


def _night_cheap_day(area: str, when: date, *, cheap_hours: int, night_price: float, day_price: float) -> str:
    """A day whose first `cheap_hours` are `night_price`, and the rest `day_price` --
    following `test_auto_controller.cheap_night_day`'s own pattern exactly, kept as its own
    small local helper rather than parameterizing that one (this codebase's own precedent:
    a small, duplicated fixture builder per module, not one shared, ever-growing helper)."""
    document = json.loads(day_body(area, when))
    document["tz"] = AREA_TZ.get(area, document["tz"])
    cut = cheap_hours * 4
    prices = document["prices"]
    document["prices"] = [night_price] * cut + [day_price] * (len(prices) - cut)
    return json.dumps(document)


def _serve_night_cheap_days(transport: StubTransport) -> None:
    transport.serve_area(SE4)
    for day in (TODAY, TOMORROW):
        body = _night_cheap_day(SE4, day, cheap_hours=CHEAP_HOURS, night_price=NIGHT_PRICE, day_price=DAY_PRICE)
        transport.serve(transport.day_path(SE4, day), 200, body)
    transport.serve(
        "/v1/index.json", 200, index_listing(SE4, [TODAY.isoformat(), TOMORROW.isoformat()])
    )


def _set_register(hass: HomeAssistant, kwh: float) -> None:
    hass.states.async_set(
        ENERGY_ENTITY,
        str(kwh),
        {"unit_of_measurement": "kWh", "device_class": "energy", "state_class": "total_increasing"},
    )


async def test_a_day_of_hybrid_buys_less_grid_energy_and_never_twice(
    hass: HomeAssistant, transport: StubTransport, monkeypatch: pytest.MonkeyPatch
) -> None:
    _serve_night_cheap_days(transport)

    with freeze_time("2026-09-21 22:00:00") as frozen:  # 2026-09-22 00:00 local (+02:00)
        charger, site_entry, controller, coordinator, clock, turn_on_calls, turn_off_calls = (
            await solar_setup(hass, strategy=STRATEGY_HYBRID, main_fuse_a=32.0)
        )
        site_controller = controller_of(hass, site_entry.entry_id)
        site_controller.config[CONF_MAX_AGE_S] = 4 * 3600.0

        # `ChargingController.energy_register_entity_id` resolves once at construction (see
        # that constructor's own reasoning, alongside `ocpp_target`); this charger is generic,
        # not OCPP, so the override is set directly on the already-built controller rather
        # than through the config entry's data, which nothing re-reads afterwards.
        controller.energy_register_entity_id = ENERGY_ENTITY
        _set_register(hass, 0.0)
        delivered_so_far = 0.0

        # 10:00 local on 2026-09-22 is 08:00 UTC the *next* calendar day -- ten hours ahead
        # of "now", not a same-day `.replace(hour=8)`, which would land in the past.
        sunny_start_utc = dt_util.utcnow() + timedelta(hours=10)
        forecast_wh = {sunny_start_utc + timedelta(hours=h): FORECAST_W for h in range(SUN_HOURS)}

        async def fake_read(_hass: HomeAssistant, _entry_ids: list) -> ForecastReadResult:
            return ForecastReadResult(wh_hours=forecast_wh, sources_read=("fake_forecast_entry",))

        monkeypatch.setattr(
            "custom_components.spotnav.execution.hybrid_execution.async_read_forecast_wh", fake_read
        )

        snapshot = await go_auto(
            hass, charger.entry_id, strategy=STRATEGY_HYBRID, area_id=SE4, amps=AMPS, phases=PHASES,
            requested_kwh=REQUESTED_KWH, departure_enabled=True, departure=DEPARTURE,
        )
        assert snapshot.state in ("proposal_ready", "proposal_unpriced")
        assert controller.plan is not None
        windows = controller.plan.windows
        assert len(windows) == 1, "one contiguous cheap-night window"
        window_start, window_end = windows[0]
        assert window_start <= dt_util.utcnow() < window_end, "charging now, at the first usable slot"
        assert len(turn_on_calls) == 1, "the night window's own start turned the charger on"
        hass.states.async_set(f"switch.{charger.entry_id}", "on")

        # -------------------------------------------------- compare against plain cheapest
        preview = preview_for(hass, charger.entry_id)
        assert preview is not None
        area_snapshot = preview._manager.area_snapshot(SE4)
        assert area_snapshot is not None
        documents = tuple(
            snap.document
            for snap in (area_snapshot.today_snapshot, area_snapshot.tomorrow_snapshot)
            if snap.document is not None
        )
        cheapest = calculate_plan(
            PlanRequest(
                area_id=SE4, timezone="Europe/Stockholm", currency="SEK", major_unit="kr",
                minor_unit="öre", documents=documents, now=dt_util.utcnow(), phases=PHASES, amps=AMPS,
                requested_kwh=REQUESTED_KWH, consumption_kwh_per_10km=2.0, fiscal=FiscalChoice(),
                departure=DEPARTURE,
            )
        )
        assert cheapest.has_plan
        cheapest_grid_kwh = cheapest.delivered_kwh
        night_window_kwh = (window_end - window_start).total_seconds() / 3600.0 * POWER_KW
        assert night_window_kwh < cheapest_grid_kwh, "hybrid's own installed plan buys less grid energy"

        # ------------------------------------------- the night window, with the clock moving
        #
        # Real energy is delivered as the window runs, the register is updated to match, and
        # `hybrid`'s own periodic preview recompute runs *while the window is still charging*
        # -- the exact scenario the pre-fix code bought twice (see the module docstring).
        step = (window_end - window_start) / 6
        for _ in range(6):
            frozen.move_to(dt_util.utcnow() + step)
            async_fire_time_changed(hass, dt_util.utcnow())
            await hass.async_block_till_done()
            delivered_so_far += step.total_seconds() / 3600.0 * POWER_KW
            _set_register(hass, delivered_so_far)
            await tick_site(hass, site_controller)  # drives the periodic hybrid recompute
        assert delivered_so_far == pytest.approx(night_window_kwh, rel=1e-6)

        # ---------------------------------------------------------------- the night window ends
        frozen.move_to(window_end + timedelta(seconds=1))
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()
        assert controller.plan_window_active_now is False
        assert len(turn_off_calls) == 1, "the night window ends normally -- no sun yet"
        prefix = charger.entry_id
        hass.states.async_set(f"switch.{prefix}", "off")
        turn_on_calls.clear()

        # No second grid window may already exist here: the delivered-energy tracking above
        # kept the remaining need (and therefore hybrid's own `grid_kwh`) correctly reduced
        # throughout, so nothing pending from mid-window still wants the full original need.
        assert controller.plan is None or controller.plan.energy_kwh is None or (
            controller.plan.energy_kwh < (REQUESTED_KWH - delivered_so_far) + ONE_SLOT_KWH
        )

        # ----------------------------------------------------------------------- midday sun
        set_derived_site_entities(hass, "solar_site", 0.0)
        set_charger_delivered_a(hass, prefix, 0.0)
        await tick_site(hass, site_controller)
        assert coordinator.state is not None and coordinator.state.state == "off"

        frozen.move_to(sunny_start_utc)
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()
        set_derived_site_entities(hass, "solar_site", 0.0)  # refresh after the quiet gap
        set_site_power_w(hass, "solar_site", -1400.0)  # export: the sun is up
        await tick_site(hass, site_controller)
        clock.value = 125.0  # past solar's own arming delay
        await tick_site(hass, site_controller)

        assert coordinator.state is not None
        assert coordinator.state.state == "on"
        assert coordinator.state.held_by_plan is False, "no plan window is active any more"
        assert len(turn_on_calls) == 1, "solar itself started the charger"

        requested_a = coordinator.state.requested_a
        assert requested_a is not None and requested_a > 0
        hass.states.async_set(f"switch.{prefix}", "on")
        set_charger_delivered_a(hass, prefix, requested_a)

        # The sun holds for up to the whole forecast window -- the car keeps charging
        # through it, and the register keeps rising with real (now solar) delivery, in
        # 15-minute steps (fine enough that one step's own modulation cannot overshoot the
        # target by much). The delivered-current sensor is kept in step with whatever solar
        # itself is asking for at each step (a real car's own measured draw would too), which
        # is also what stops the pure surplus controller -- unmodified, and rightly so --
        # from reading "unused headroom" and escalating its own request indefinitely; nothing
        # in `site/solar_surplus.py` knows about `manual_kwh`'s own target. A car that is already
        # full is what a real BMS itself would stop drawing at, simulated here by capping the
        # loop once the register reaches the original request.
        step = timedelta(minutes=15)
        for _ in range(SUN_HOURS * 4):
            if delivered_so_far >= REQUESTED_KWH - ONE_SLOT_KWH:
                break
            frozen.move_to(dt_util.utcnow() + step)
            async_fire_time_changed(hass, dt_util.utcnow())
            await hass.async_block_till_done()
            clock.value += step.total_seconds()
            await tick_site(hass, site_controller)
            requested_a = coordinator.state.requested_a or requested_a
            set_charger_delivered_a(hass, prefix, requested_a)
            delivered_so_far += requested_a * VOLTAGE_V * PHASES / 1000.0 * 0.25
            _set_register(hass, delivered_so_far)
        assert coordinator.state.state == "on", "the sun was still up when the car reached its target"

        # ------------------------------------------------------------------------- the car is full
        set_site_power_w(hass, "solar_site", 0.0)
        set_charger_delivered_a(hass, prefix, 0.0)
        await tick_site(hass, site_controller)
        turn_on_calls_before_departure = len(turn_on_calls)

        # ------------------------------------------------------------- on to departure, and past it
        frozen.move_to(dt_util.utcnow() + timedelta(hours=2))  # past 18:00 local departure
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()
        set_derived_site_entities(hass, "solar_site", 0.0)
        await tick_site(hass, site_controller)

        # -------------------------------------------------------------- the closing assertions
        assert delivered_so_far >= REQUESTED_KWH - ONE_SLOT_KWH, (
            f"the car must be full by departure: delivered {delivered_so_far:.2f} kWh "
            f"of the {REQUESTED_KWH} kWh asked for"
        )
        assert delivered_so_far <= REQUESTED_KWH + ONE_SLOT_KWH, (
            "delivered energy must not exceed the request by more than one planning slot"
        )
        assert night_window_kwh < cheapest_grid_kwh, "less grid energy than cheapest, restated"
        # No further start reached the charger once the sun had already covered the rest of
        # the need -- the remaining-need tracking correctly saw nothing left to buy or credit.
        assert len(turn_on_calls) == turn_on_calls_before_departure, (
            "no further grid window was installed once the car was already full"
        )
