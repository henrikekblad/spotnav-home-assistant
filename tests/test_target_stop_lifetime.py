"""The target-stop record ends once it no longer describes the present.

The field case (1.14.1): an Easee charger with a Mazda MX-30 on `solar`, driver `target_soc`, target 50 %. The target
stopped the charge at 05:31Z on a reading of 53 %. Three hours later the status still read `solar_arming` beside
`target_reached{reading_age_s: 0}` ("Stopped at 53 % (just now)"), and after the car had driven and the sun charged
it at 11 A, "charging 11 A from surplus · Stopped at 53 % (just now)". The record lived until a new schedule or a
person's Start cleared it; now any charge, an unplug, another car, a lower level or a raised target ends it, and the
line carries the stop's own time.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from freezegun import freeze_time
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.spotnav.api import dashboard as dashboard_api
from custom_components.spotnav.const import CONF_CHARGE_CONTROL
from custom_components.spotnav.execution.controller import ChargingController
from custom_components.spotnav.execution.target_stop import (
    TARGET_STOP_LAPSE_MARGIN_PERCENT,
    SocReading,
    target_stop_lapse,
)
from custom_components.spotnav.planning.auto_settings import STRATEGY_SOLAR, TargetSocIntent
from custom_components.spotnav.planning.status_compose import (
    PlanningFacts,
    SolarFacts,
    StatusFacts,
    TargetFacts,
    compose_status,
)

from .helpers import install_schedule
from .test_replug import Plug, _obedient_switch

pytestmark = pytest.mark.usefixtures("offline_relay")

STOP = datetime(2026, 10, 10, 5, 31, tzinfo=timezone.utc)
SOC_ENTITY = "sensor.mx30_battery"
CAR = "mx30"


# --------------------------------------------------------------------------- the pure decision


def _record(**changes: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "at": STOP.isoformat(),
        "soc_percent": 53.0,
        "target_soc_percent": 50.0,
        "source": "vehicle",
        "vehicle_id": CAR,
        "reading_age_s": 0.0,
        "estimated": False,
        "basis": "reading",
        "car_ended": False,
    }
    record.update(changes)
    return record


def _lapse(record: dict[str, Any] | None = None, **facts: Any) -> str | None:
    values: dict[str, Any] = dict(
        vehicle_id=CAR, target_percent=50.0, soc_percent=53.0, soc_age_s=None, now=STOP + timedelta(hours=3)
    )
    values.update(facts)
    return target_stop_lapse(_record() if record is None else record, **values)


def test_a_record_that_still_describes_the_car_stays() -> None:
    assert _lapse() is None
    # The reading the stop was made on, three hours old now, is the same level.
    assert _lapse(soc_age_s=3 * 3600.0) is None
    assert _lapse(vehicle_id=None, target_percent=None, soc_percent=None) is None


def test_another_car_ends_it() -> None:
    assert _lapse(vehicle_id="ev6") == "other_vehicle"
    # A record without a car (the charger's own reading) says nothing about which car it was.
    assert _lapse(_record(vehicle_id=None), vehicle_id="ev6") is None


def test_a_target_raised_above_the_stop_level_ends_it() -> None:
    assert _lapse(target_percent=60.0) == "target_raised"
    assert _lapse(target_percent=53.0) is None, "at the stop level the car is still where the target wants it"
    assert _lapse(target_percent=40.0) is None, "a lower target leaves the stop true"
    # A record without a level is measured against the target it stopped at.
    assert _lapse(_record(soc_percent=None), target_percent=55.0) == "target_raised"


def test_a_fresher_level_below_the_target_ends_it() -> None:
    # The car drove: a reading taken after the stop says 21 %.
    assert _lapse(soc_percent=21.0, soc_age_s=600.0) == "level_dropped"
    # Within the margin of the target it stays (a cloud rounding, a little standby loss).
    within = 50.0 - TARGET_STOP_LAPSE_MARGIN_PERCENT
    assert _lapse(soc_percent=within, soc_age_s=600.0) is None
    assert _lapse(soc_percent=within - 0.5, soc_age_s=600.0) == "level_dropped"
    # A reading from before the stop is not news.
    assert _lapse(soc_percent=21.0, soc_age_s=4 * 3600.0) is None
    # An estimate carried from a reading after the stop counts as that reading.
    assert _lapse(soc_percent=21.0, soc_age_s=600.0) == "level_dropped"
    # Without an age nothing says it is fresher.
    assert _lapse(soc_percent=21.0, soc_age_s=None) is None
    # A record without a readable time cannot be compared with.
    assert _lapse(_record(at="garbage"), soc_percent=21.0, soc_age_s=600.0) is None


# --------------------------------------------------------------------------- the controller owns the lifetime


def _soc(hass: HomeAssistant, percent: float) -> None:
    hass.states.async_set(SOC_ENTITY, str(percent), {"unit_of_measurement": "%", "device_class": "battery"})


def _reader(hass: HomeAssistant):
    def read(_vehicle: str | None) -> SocReading | None:
        state = hass.states.get(SOC_ENTITY)
        if state is None:
            return None
        return SocReading(
            soc_percent=float(state.state),
            source="vehicle",
            entity_id=SOC_ENTITY,
            vehicle_id=CAR,
            age_s=(dt_util.utcnow() - state.last_updated).total_seconds(),
        )

    return read


async def _stopped(hass: HomeAssistant) -> tuple[ChargingController, Plug, list[Any], list[Any]]:
    """A plan with a 50 % target charging the car from 40 %; the car reports 53 % and the target stops it."""
    hass.states.async_set("switch.a", "off")
    _soc(hass, 40.0)
    starts, stops = _obedient_switch(hass)
    controller = ChargingController(hass, "entry_a", {CONF_CHARGE_CONTROL: "switch.a"}, soc_reader=_reader(hass))
    await controller.async_initialize()
    plug = Plug(hass, controller, "switch.a")
    await plug.set(False)
    await plug.set(True)
    now = dt_util.utcnow()
    await install_schedule(
        controller,
        {
            "start": (now - timedelta(minutes=10)).isoformat(),
            "end": (now + timedelta(hours=2)).isoformat(),
            "amps": 10,
            "target_soc_percent": 50.0,
            "vehicle_id": CAR,
        },
    )
    assert len(starts) == 1 and controller.charging
    _soc(hass, 53.0)
    await hass.async_block_till_done()
    assert controller.plan is None and len(stops) == 1
    assert controller.target_stop_record is not None
    return controller, plug, starts, stops


async def test_a_charge_the_sun_starts_ends_the_record(hass: HomeAssistant) -> None:
    with freeze_time(STOP) as frozen:
        controller, _, _, _ = await _stopped(hass)
        frozen.tick(timedelta(hours=3))
        assert await controller.async_start(10, cause="solar")
        assert controller.target_stop_record is None
        await controller.async_shutdown()


async def test_a_charge_seen_after_the_stop_ends_the_record(hass: HomeAssistant) -> None:
    with freeze_time(STOP) as frozen:
        controller, plug, _, _ = await _stopped(hass)
        # The charger still reporting the charge right after the stop is the stop on its way, not a new charge.
        await plug.set(True, control="on")
        assert controller.target_stop_record is not None
        await plug.set(True, control="off")
        frozen.tick(timedelta(minutes=10))
        # Charging again by itself (hybrid's sun, a resumed charge, the car's own timer): the record is over.
        await plug.set(True, control="on")
        assert controller.target_stop_record is None
        await controller.async_shutdown()


async def test_an_unplug_ends_the_record(hass: HomeAssistant) -> None:
    with freeze_time(STOP) as frozen:
        controller, plug, _, _ = await _stopped(hass)
        frozen.tick(timedelta(minutes=5))
        await plug.set(False)
        assert controller.target_stop_record is None
        await controller.async_shutdown()


async def test_the_record_ends_on_another_car_a_lower_level_or_a_raised_target(hass: HomeAssistant) -> None:
    with freeze_time(STOP) as frozen:
        controller, _, _, _ = await _stopped(hass)
        frozen.tick(timedelta(hours=1))
        # Nothing changed: it stays, and nothing is said to have lapsed.
        assert controller.lapse_target_stop(vehicle_id=CAR, target_percent=50.0, soc_percent=53.0, soc_age_s=3600.0) is None
        assert controller.target_stop_record is not None
        assert controller.lapse_target_stop(vehicle_id="ev6", target_percent=50.0) == "other_vehicle"
        assert controller.target_stop_record is None
        await controller.async_shutdown()

    for facts in (
        {"target_percent": 70.0},
        {"target_percent": 50.0, "soc_percent": 21.0, "soc_age_s": 60.0},
    ):
        with freeze_time(STOP) as frozen:
            controller, _, _, _ = await _stopped(hass)
            frozen.tick(timedelta(hours=1))
            assert controller.lapse_target_stop(vehicle_id=CAR, **facts) is not None
            assert controller.target_stop_record is None
            await controller.async_shutdown()


async def test_a_lapsed_record_is_not_back_after_a_restart(hass: HomeAssistant) -> None:
    with freeze_time(STOP) as frozen:
        controller, plug, _, _ = await _stopped(hass)
        frozen.tick(timedelta(minutes=5))
        await plug.set(False)
        await hass.async_block_till_done()
        await controller.async_shutdown()
        restarted = ChargingController(hass, "entry_a", {CONF_CHARGE_CONTROL: "switch.a"}, soc_reader=_reader(hass))
        await restarted.async_initialize()
        assert restarted.target_stop_record is None
        await restarted.async_shutdown()


# --------------------------------------------------------------------------- the field case, replayed


def _status(controller: ChargingController, solar: SolarFacts, *, charging: bool) -> dict[str, Any]:
    captured = dashboard_api.capture_target(controller)
    target = None
    if captured is not None:
        target = TargetFacts(
            stop_soc_percent=captured.stop_soc_percent,
            stop_basis=captured.stop_basis,
            stop_reading_age_s=captured.stop_reading_age_s,
            unverifiable_reason=captured.unverifiable_reason,
            stopped_at=captured.stopped_at,
        )
    return compose_status(
        StatusFacts(
            now=dt_util.utcnow(),
            has_settings=True,
            strategy="solar",
            planning=PlanningFacts(state="planning_unavailable", reason="solar_running"),
            price_state="ready",
            usable_price_rows=96,
            solar=solar,
            charging=charging,
            target=target,
        )
    )


async def test_the_field_case_arming_three_hours_later_then_charging_after_the_car_drove(hass: HomeAssistant) -> None:
    with freeze_time(STOP) as frozen:
        controller, plug, _, _ = await _stopped(hass)
        frozen.tick(timedelta(hours=3, minutes=2))

        # 08:33Z: the sun finds a surplus. The stop is told with its own time, not as "just now".
        block = _status(controller, SolarFacts(state="arming"), charging=False)
        assert block["lines"] == [
            {"code": "solar_arming", "params": {}},
            {
                "code": "target_reached",
                "params": {
                    "soc_percent": 53.0,
                    "basis": "reading",
                    "reading_age_s": 0,
                    "stopped_at": STOP.isoformat(),
                },
            },
        ]

        # The car drives away and comes back at about 21 %.
        frozen.tick(timedelta(hours=1))
        await plug.set(False)
        _soc(hass, 21.0)
        frozen.tick(timedelta(hours=1))
        await plug.set(True)
        assert controller.target_stop_record is None
        assert await controller.async_start(11, cause="solar")
        block = _status(controller, SolarFacts(state="on", requested_a=11), charging=True)
        assert [line["code"] for line in block["lines"]] == ["solar_charging"]
        await controller.async_shutdown()


async def test_the_field_case_without_an_unplug_the_lower_level_and_the_suns_start_end_it(
    hass: HomeAssistant,
) -> None:
    """The charger may not say the car left (a cloud gap): the car's lower level ends the record, and the sun's
    start would anyway."""
    with freeze_time(STOP) as frozen:
        controller, _, _, _ = await _stopped(hass)
        frozen.tick(timedelta(hours=4))
        _soc(hass, 21.0)
        reading = _reader(hass)(CAR)
        assert reading is not None
        assert (
            controller.lapse_target_stop(
                vehicle_id=CAR, target_percent=50.0, soc_percent=reading.soc_percent, soc_age_s=reading.age_s
            )
            == "level_dropped"
        )
        block = _status(controller, SolarFacts(state="on", requested_a=11), charging=True)
        assert [line["code"] for line in block["lines"]] == ["solar_charging"]
        await controller.async_shutdown()


# --------------------------------------------------------------------------- the Auto wiring


async def test_auto_ends_the_record_on_a_raised_target_or_a_lower_level_under_solar(hass: HomeAssistant) -> None:
    from custom_components.spotnav.runtime import domain_data

    from .world import controller_of, go_auto, real_controller_stop

    with freeze_time(STOP) as frozen:
        await real_controller_stop(hass, frozen)
        charger = hass.config_entries.async_get_entry("soc_charger")
        assert charger is not None
        controller = controller_of(hass, charger.entry_id)
        store = domain_data(hass).auto_store
        assert store is not None
        vehicle_id = store.settings(charger.entry_id).target.vehicle_id
        # Solar with the same target: nothing about the stop changed.
        await go_auto(hass, charger.entry_id, strategy=STRATEGY_SOLAR)
        assert controller.target_stop_record is not None
        # The target raised above the level the car stopped at.
        await go_auto(
            hass, charger.entry_id, strategy=STRATEGY_SOLAR, target=TargetSocIntent(vehicle_id=vehicle_id, target_percent=90)
        )
        assert controller.target_stop_record is None


async def test_auto_ends_the_record_when_the_car_reports_a_lower_level(hass: HomeAssistant) -> None:
    from .world import controller_of, go_auto, real_controller_stop

    with freeze_time(STOP) as frozen:
        await real_controller_stop(hass, frozen)
        charger = hass.config_entries.async_get_entry("soc_charger")
        assert charger is not None
        controller = controller_of(hass, charger.entry_id)
        await go_auto(hass, charger.entry_id, strategy=STRATEGY_SOLAR)
        assert controller.target_stop_record is not None
        frozen.tick(timedelta(hours=2))
        battery = hass.states.get("sensor.ev6_battery")
        assert battery is not None
        hass.states.async_set("sensor.ev6_battery", "21", battery.attributes)
        await hass.async_block_till_done()
        frozen.tick(timedelta(seconds=30))
        await hass.async_block_till_done()
        assert controller.target_stop_record is None
