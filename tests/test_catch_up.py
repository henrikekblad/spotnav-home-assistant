"""Charging on after a missed departure (3.0, `execution/catch_up.py`): a car still short of the plan's need and plugged
in when its departure passes is charged on at once, until the need is met, it is unplugged, a person stops or pauses
it, three hours have passed or the car ends the charge itself; nothing changes where the need was met, there was no
departure, the car had left or a person had paused Auto."""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from typing import Any

import pytest
from freezegun import freeze_time
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.spotnav.core.session import OWNER_PLAN
from custom_components.spotnav.execution import catch_up as rules
from custom_components.spotnav.execution.catch_up import CatchUp
from custom_components.spotnav.planning.auto_settings import PAUSE_UNTIL_RESUMED
from custom_components.spotnav.runtime import domain_data
from tests.relay import serve as serve_prices

from .test_dashboard_api import NOW
from .test_replug import _car, _delivered, _meter, _record_charger_commands, _turn_offs, _turn_ons, Car
from .world import charger_and_car

pytestmark = pytest.mark.usefixtures("offline_relay")

#: 10:00 local (CEST) on NOW's day, two hours after NOW.
DEPARTURE = datetime(2026, 9, 22, 8, 0, tzinfo=timezone.utc)
UNTIL = DEPARTURE + timedelta(hours=3)


async def _at(car: Car, frozen: Any, delta: timedelta) -> None:
    frozen.tick(delta)
    async_fire_time_changed(car.hass, dt_util.utcnow())
    await car.hass.async_block_till_done()


async def _target_car(hass: HomeAssistant, frozen: Any, **settings: Any) -> tuple[Car, str]:
    """A target car (40 % of a 77 kWh pack, target 80 %) plugged in at 08:00, with a 10:00 departure."""
    settings.setdefault("departure", time(10, 0))
    settings.setdefault("departure_enabled", True)
    charger, _, soc = await charger_and_car(hass, amps=16, phases=3, **settings)
    car = Car(hass, frozen, charger)
    await car.plug.set(True)
    await car.settle()
    return car, soc


def _set_soc(hass: HomeAssistant, entity_id: str, percent: float) -> None:
    state = hass.states.get(entity_id)
    assert state is not None
    hass.states.async_set(entity_id, str(percent), dict(state.attributes))


def _record(car: Car) -> CatchUp | None:
    return domain_data(car.hass).auto_store.catch_up(car.charger.entry_id)


def _codes(car: Car) -> list[str]:
    return [line["code"] for line in car.dashboard()["status"]["lines"]]


def _line(car: Car, code: str) -> dict[str, Any]:
    return next(line for line in car.dashboard()["status"]["lines"] if line["code"] == code)


async def _past_departure(car: Car, frozen: Any) -> None:
    """To one minute past the 10:00 departure."""
    await _at(car, frozen, DEPARTURE + timedelta(minutes=1) - dt_util.utcnow())


# The pure rules.


def test_only_a_planned_unmet_need_of_a_plugged_in_car_without_a_person_starts_one() -> None:
    ok = dict(planned=True, met=False, plugged_in=True, paused=False, person_charging=False)
    assert rules.starts(**ok)
    assert not rules.starts(**{**ok, "planned": False}), "no plan for that departure"
    assert not rules.starts(**{**ok, "met": True}), "the need was met"
    assert not rules.starts(**{**ok, "met": None}), "nothing can tell whether it was met"
    assert not rules.starts(**{**ok, "plugged_in": False}), "the car left"
    assert not rules.starts(**{**ok, "plugged_in": None}), "the charger cannot tell"
    assert not rules.starts(**{**ok, "paused": True}), "a person paused Auto (a Stop or a Start among them)"
    assert not rules.starts(**{**ok, "person_charging": True}), "Charge now runs past the departure by itself"


def test_an_energy_need_s_period_holds_what_is_missing_and_never_runs_past_three_hours() -> None:
    assert rules.LIMIT == timedelta(hours=3) and rules.CATCH_UP_LIMIT_S == 3 * 3600
    assert rules.energy_end(DEPARTURE, UNTIL, 5.5, 11.0) == DEPARTURE + timedelta(minutes=30)
    assert rules.energy_end(DEPARTURE, UNTIL, 100.0, 11.0) == UNTIL
    assert rules.energy_end(DEPARTURE, UNTIL, 5.0, 0.0) == UNTIL


def test_a_saved_record_reads_back_and_a_record_that_would_run_longer_is_dropped() -> None:
    record = CatchUp(
        departure=DEPARTURE, until=UNTIL, end=UNTIL, requested_kwh=30.0, short_kwh=12.0,
        target_percent=80.0, level_percent=62.0,
    )
    assert CatchUp.from_stored(record.as_dict()) == record
    assert record.running(DEPARTURE) and not record.running(UNTIL)
    longer = {**record.as_dict(), "until": (DEPARTURE + timedelta(hours=4)).isoformat()}
    assert CatchUp.from_stored(longer) is None
    assert CatchUp.from_stored({**record.as_dict(), "ended": "whatever"}) is None
    ended = record.ended_short(level_percent=70.0, delivered_kwh=None)
    assert CatchUp.from_stored(ended.as_dict()) == ended and not ended.running(DEPARTURE)


# A target.


async def test_a_car_short_of_its_target_charges_on_at_once_after_the_departure_until_the_target(
    hass: HomeAssistant, transport: Any
) -> None:
    serve_prices(transport, rising=True)
    calls = _record_charger_commands(hass)
    with freeze_time(NOW) as frozen:
        car, soc = await _target_car(hass, frozen)
        await _past_departure(car, frozen)

        record = _record(car)
        assert record is not None and record.target_percent == 80.0 and record.level_percent == 40.0
        assert record.departure == DEPARTURE and record.end == record.until == UNTIL
        assert car.controller.charging, "charging at once, whatever the price"
        assert car.controller.session_runner.session.owner == OWNER_PLAN, "the plan's own charge"
        assert car.controller.charge_origin == "plan_window"
        snapshot = car.preview.snapshot()
        assert snapshot.proposal is not None and snapshot.proposal.periods == ((DEPARTURE, UNTIL),)
        assert car.controller.plan is not None and car.controller.plan.amps == 16, "the charge's full current"
        assert _line(car, "charging_after_departure")["params"] == {
            "target": 80.0, "level": 40.0, "until": UNTIL.isoformat(), "kwh": None, "requested_kwh": None,
        }

        offs = _turn_offs(calls)
        _set_soc(hass, soc, 80)
        await car.settle()
        assert _turn_offs(calls) == offs + 1, "the target's stop ends it, as ever"
        await car.preview.async_recalculate()
        assert _record(car) is None
        assert "charging_after_departure" not in _codes(car)
        assert not car.controller.charging


async def test_a_car_still_short_after_three_hours_stops_and_the_status_says_so_until_the_unplug(
    hass: HomeAssistant, transport: Any
) -> None:
    serve_prices(transport, rising=True)
    calls = _record_charger_commands(hass)
    with freeze_time(NOW) as frozen:
        car, soc = await _target_car(hass, frozen)
        await _past_departure(car, frozen)
        assert car.controller.charging
        _set_soc(hass, soc, 63)
        await car.settle()

        offs = _turn_offs(calls)
        await _at(car, frozen, UNTIL + timedelta(seconds=5) - dt_util.utcnow())
        assert _turn_offs(calls) == offs + 1, "its end stops the charge"
        assert not car.controller.charging
        record = _record(car)
        assert record is not None and record.ended == rules.ENDED_SHORT and record.level_percent == 63.0
        assert _line(car, "not_reached_after_departure")["params"] == {
            "target": 80.0, "level": 63.0, "kwh": None, "requested_kwh": None,
        }
        await _at(car, frozen, timedelta(minutes=30))
        assert _record(car) is not None, "said on while the car stays plugged in; the next departure's plan decides"

        await car.unplug()
        assert _record(car) is None
        assert "not_reached_after_departure" not in _codes(car)


async def test_an_unplug_ends_it(hass: HomeAssistant, transport: Any) -> None:
    serve_prices(transport, rising=True)
    _record_charger_commands(hass)
    with freeze_time(NOW) as frozen:
        car, _ = await _target_car(hass, frozen)
        await _past_departure(car, frozen)
        assert _record(car) is not None
        await car.unplug()
        assert _record(car) is None
        await car.replug()
        await _at(car, frozen, timedelta(minutes=10))
        assert _record(car) is None, "a new plug-in is no missed departure"


async def test_a_person_s_stop_ends_it_for_good(hass: HomeAssistant, transport: Any) -> None:
    serve_prices(transport, rising=True)
    calls = _record_charger_commands(hass)
    with freeze_time(NOW) as frozen:
        car, _ = await _target_car(hass, frozen)
        await _past_departure(car, frozen)
        assert car.controller.charging
        await car.preview.async_manual_action("stop")
        await hass.async_block_till_done()
        assert _record(car) is None
        assert not car.controller.charging
        await car.preview.async_manual_action("resume")
        await hass.async_block_till_done()
        ons = _turn_ons(calls)
        await _at(car, frozen, timedelta(minutes=10))
        assert _record(car) is None and _turn_ons(calls) == ons, "a Resume does not start it again"


async def test_a_pause_ends_it(hass: HomeAssistant, transport: Any) -> None:
    serve_prices(transport, rising=True)
    _record_charger_commands(hass)
    with freeze_time(NOW) as frozen:
        car, _ = await _target_car(hass, frozen)
        await _past_departure(car, frozen)
        assert _record(car) is not None
        await car.preview.async_pause(PAUSE_UNTIL_RESUMED)
        await hass.async_block_till_done()
        assert _record(car) is None


async def test_a_safety_stop_during_it_is_held_as_the_plan_s_charge(hass: HomeAssistant, transport: Any) -> None:
    """The catch-up is the plan's charge: load balancing's safety stop holds it back for the plan (2.5.7), to be
    resumed after its gap, and the catch-up goes on meanwhile."""
    serve_prices(transport, rising=True)
    _record_charger_commands(hass)
    with freeze_time(NOW) as frozen:
        car, _ = await _target_car(hass, frozen)
        await _past_departure(car, frozen)
        await car.settle()
        assert car.controller.charging and _record(car) is not None
        write = await car.controller._regulated_stop("safety_stop", cause="refused")  # noqa: SLF001
        await hass.async_block_till_done()
        assert write.stop_sent
        assert car.controller.paused_by_balancing
        assert car.controller.paused_charge_origin == "plan_window"
        session = car.controller.session_runner.session
        assert session.held_for_safety and session.paused_origin == OWNER_PLAN
        await car.preview.async_recalculate()
        record = _record(car)
        assert record is not None and record.ended is None, "a safety stop is none of its ends"


async def test_a_restart_goes_on_to_the_same_end_and_never_further(hass: HomeAssistant, transport: Any) -> None:
    serve_prices(transport, rising=True)
    _record_charger_commands(hass)
    with freeze_time(NOW) as frozen:
        car, _ = await _target_car(hass, frozen)
        await _past_departure(car, frozen)
        before = _record(car)
        assert before is not None
        await _at(car, frozen, timedelta(minutes=40))
        assert await hass.config_entries.async_reload(car.charger.entry_id)
        await hass.async_block_till_done()
        restarted = Car(hass, frozen, car.charger)
        restarted.plug.connected = True
        await restarted.preview.async_recalculate()
        assert _record(restarted) == before, "the same record: the same departure, the same end"
        snapshot = restarted.preview.snapshot()
        assert snapshot.proposal is not None and snapshot.proposal.periods == ((DEPARTURE, UNTIL),)


async def test_home_assistant_down_at_the_departure_starts_none_after_it(hass: HomeAssistant, transport: Any) -> None:
    """Only the departure's own appointment starts a catch-up: a start after the departure passed (Home Assistant was
    down then) never does."""
    serve_prices(transport, rising=True)
    _record_charger_commands(hass)
    with freeze_time(NOW) as frozen:
        car, _ = await _target_car(hass, frozen)
        await _at(car, frozen, timedelta(hours=1, minutes=55))
        assert await hass.config_entries.async_unload(car.charger.entry_id)
        await hass.async_block_till_done()
        frozen.tick(timedelta(minutes=10))
        assert await hass.config_entries.async_setup(car.charger.entry_id)
        await hass.async_block_till_done()
        restarted = Car(hass, frozen, car.charger)
        restarted.plug.connected = True
        await restarted.preview.async_recalculate()
        await _at(restarted, frozen, timedelta(minutes=5))
        assert _record(restarted) is None


# Where nothing changes.


async def test_a_met_target_at_the_departure_starts_nothing(hass: HomeAssistant, transport: Any) -> None:
    serve_prices(transport, rising=True)
    _record_charger_commands(hass)
    with freeze_time(NOW) as frozen:
        car, soc = await _target_car(hass, frozen)
        await _at(car, frozen, timedelta(minutes=30))
        _set_soc(hass, soc, 79.5)  # a reading within one percentage point is reached
        await car.settle()
        await _past_departure(car, frozen)
        assert _record(car) is None
        assert not car.controller.charging


async def test_without_a_departure_nothing_is_missed(hass: HomeAssistant, transport: Any) -> None:
    serve_prices(transport, rising=True)
    _record_charger_commands(hass)
    with freeze_time(NOW) as frozen:
        car, _ = await _target_car(hass, frozen, departure_enabled=False)
        await _at(car, frozen, timedelta(hours=3))
        assert _record(car) is None


async def test_a_car_unplugged_before_the_departure_is_not_caught_up(hass: HomeAssistant, transport: Any) -> None:
    serve_prices(transport, rising=True)
    calls = _record_charger_commands(hass)
    with freeze_time(NOW) as frozen:
        car, _ = await _target_car(hass, frozen)
        await _at(car, frozen, timedelta(hours=1))
        await car.unplug()
        ons = _turn_ons(calls)
        await _past_departure(car, frozen)
        assert _record(car) is None and _turn_ons(calls) == ons


async def test_a_person_s_stop_before_the_departure_starts_nothing(hass: HomeAssistant, transport: Any) -> None:
    serve_prices(transport, rising=True)
    calls = _record_charger_commands(hass)
    with freeze_time(NOW) as frozen:
        car, _ = await _target_car(hass, frozen)
        await car.preview.async_manual_action("stop")
        await hass.async_block_till_done()
        ons = _turn_ons(calls)
        await _past_departure(car, frozen)
        assert _record(car) is None and _turn_ons(calls) == ons


async def test_a_pause_before_the_departure_starts_nothing(hass: HomeAssistant, transport: Any) -> None:
    serve_prices(transport, rising=True)
    _record_charger_commands(hass)
    with freeze_time(NOW) as frozen:
        car, _ = await _target_car(hass, frozen)
        await car.preview.async_pause(PAUSE_UNTIL_RESUMED)
        await hass.async_block_till_done()
        await _past_departure(car, frozen)
        assert _record(car) is None
        assert not car.controller.charging


# An amount of energy.


async def test_an_amount_short_at_the_departure_charges_until_it_is_delivered_and_counts_for_the_next(
    hass: HomeAssistant, transport: Any
) -> None:
    """10 kWh asked by 10:00, 4 delivered: the catch-up ends once the 6 missing kWh are counted after the departure, and
    the next departure's need counts them already."""
    serve_prices(transport, rising=True)
    calls = _record_charger_commands(hass)
    with freeze_time(NOW) as frozen:
        car = await _car(hass, frozen, kwh=10.0, departure=time(10, 0), departure_enabled=True)
        _meter(hass, 1000.0)
        await car.settle()
        await _delivered(hass, frozen, 1004.0, minutes=5)
        await car.preview.async_recalculate()
        await _past_departure(car, frozen)

        record = _record(car)
        assert record is not None and record.target_percent is None
        assert record.requested_kwh == pytest.approx(10.0) and record.short_kwh == pytest.approx(6.0)
        assert record.end == UNTIL, "an energy register ends it at the need, so it may run to its limit"
        assert car.controller.charging
        assert _line(car, "charging_after_departure")["params"] == {
            "target": None, "level": None, "until": UNTIL.isoformat(), "kwh": 4.0, "requested_kwh": 10.0,
        }

        await _delivered(hass, frozen, 1003.0 + 3.0, minutes=15)  # a step that is not yet enough
        assert _record(car) is not None
        offs = _turn_offs(calls)
        await _delivered(hass, frozen, 1010.5, minutes=20)
        await hass.async_block_till_done()
        assert _record(car) is None, "the missing energy is delivered"
        assert _turn_offs(calls) == offs + 1 and not car.controller.charging
        snapshot = car.preview.snapshot()
        assert snapshot.catch_up is None
        assert snapshot.remaining_kwh == pytest.approx(10.0 - 6.5), "the next departure counts what it delivered"


def test_an_app_that_does_not_read_it_gets_the_ordinary_charging_line() -> None:
    from custom_components.spotnav.api.webhook import _status_block_for_app

    status = {
        "tone": "normal",
        "lines": [
            {
                "code": "charging_after_departure",
                "params": {"target": 80.0, "level": 60.0, "until": UNTIL.isoformat(), "kwh": None, "requested_kwh": None},
            },
            {"code": "not_reached_after_departure", "params": {"target": 80.0, "level": 60.0, "kwh": None, "requested_kwh": None}},
        ],
    }
    assert _status_block_for_app(status, set())["lines"] == [
        {"code": "charging_now", "params": {"until": UNTIL.isoformat()}}
    ]
    assert _status_block_for_app(status, {"charging_after_departure"}) == status
