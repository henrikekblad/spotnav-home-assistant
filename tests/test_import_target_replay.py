"""A night of charging under the import target, through the real planner: the plan's windows, its watch (a window
held below its plan is planned again at its end), the site's hold and the departure.

The HALO world of `tests/test_import_target_hold.py` (a Charge Amps HALO on OCPP, three phases, 16 A, behind a 20 A
fuse, active control on) with the real `AutoPlannerController`/`AutoExecutor`/`ChargingController` chain and served
day-ahead prices: dear all evening, cheap from midnight to 04:00, a departure at 09:00. The car needs 30 kWh, which
its 11 kW plans in under three hours; a 6 kW import target with a 1 kW house leaves it under 5 kW, so the hold makes
every window deliver less than planned. Once the site has measured an hour, the plan counts on what the target leaves
the car (`SiteCapacityController.import_target_plan_kw`) and buys the hours that takes: every completed clock hour's
real mean import stays at or under the target, the target is never set aside, and the car has its energy by the
departure.
"""

from __future__ import annotations

import json
from datetime import date, time, timedelta

import pytest
from freezegun import freeze_time
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.spotnav.planning.auto_settings import STRATEGY_CHEAPEST

from .relay import AREA_TZ, SE4, StubTransport, TODAY, TOMORROW, day_body, index_listing
from .test_import_target_hold import _world
from .world import go_auto

pytestmark = pytest.mark.usefixtures("offline_relay")

REQUESTED_KWH = 30.0
DEPARTURE = time(9, 0)
REGISTER = "sensor.night_charger_energy_register"


def _day(when: date, cheap_hours: int) -> str:
    document = json.loads(day_body(SE4, when))
    document["tz"] = AREA_TZ[SE4]
    prices = document["prices"]
    document["prices"] = [0.10] * (cheap_hours * 4) + [0.40] * (len(prices) - cheap_hours * 4)
    return json.dumps(document)


def _serve(transport: StubTransport) -> None:
    transport.serve_area(SE4)
    transport.serve(transport.day_path(SE4, TODAY), 200, _day(TODAY, 0))
    transport.serve(transport.day_path(SE4, TOMORROW), 200, _day(TOMORROW, 4))
    transport.serve("/v1/index.json", 200, index_listing(SE4, [TODAY.isoformat(), TOMORROW.isoformat()]))


async def test_a_night_under_the_target_keeps_every_hour_under_it_and_the_car_is_ready_by_departure(
    hass: HomeAssistant, transport: StubTransport, monkeypatch
) -> None:
    _serve(transport)
    # 2026-09-22 20:00 in Stockholm (+02:00).
    with freeze_time("2026-09-22 18:00:00") as frozen:
        world = await _world(hass, monkeypatch, frozen, "night")
        world.register = REGISTER
        world.timers = True
        cc = world.halo.cc
        cc.energy_register_entity_id = REGISTER
        hass.states.async_set(
            REGISTER, "0.0", {"unit_of_measurement": "kWh", "device_class": "energy", "state_class": "total_increasing"}
        )
        await world.step()
        snapshot = await go_auto(
            hass, world.halo.charger.entry_id, strategy=STRATEGY_CHEAPEST, area_id=SE4, amps=16, phases=3,
            requested_kwh=REQUESTED_KWH, departure_enabled=True, departure=DEPARTURE,
        )
        assert snapshot.state in ("proposal_ready", "proposal_unpriced"), snapshot
        assert cc.plan is not None and cc.plan.departure is not None
        departure = dt_util.parse_datetime(cc.plan.departure)
        assert departure is not None
        # Planned at the car's own 11 kW before the site had measured an hour: under three hours of windows.
        assert sum((end - start for start, end in cc.plan.windows), timedelta()) < timedelta(hours=3)

        replanned = False
        while dt_util.utcnow() < departure + timedelta(minutes=5):
            await world.step(30.0)
            if dt_util.utcnow() == departure - timedelta(hours=8):
                replanned = True
                # Planned again since (the plan's own periodic look), at what the target leaves: 6 kW less the house's
                # 1 kW, so six hours of windows for the 30 kWh.
                assert cc.plan is not None
                assert sum((end - start for start, end in cc.plan.windows), timedelta()) >= timedelta(hours=5, minutes=45)

        assert replanned
        log = world.halo.controller.import_target_snapshot()["log"]
        # The plan counted on what the target leaves the car once the hour could be measured, and was not set aside:
        # the hold alone kept every hour under the target, and the plan bought the hours that took.
        assert not any(entry["action"] == "released" for entry in log)
        assert any(entry["action"] == "held" for entry in log)
        hours = world.complete_hours()
        assert len(hours) >= 12
        for hour in hours:
            assert world.hour_kw(hour) <= 6.0, f"{hour}: {world.hour_kw(hour):.2f} kW"
        assert world.delivered_kwh >= REQUESTED_KWH - 0.5, f"{world.delivered_kwh:.1f} kWh by the departure"
