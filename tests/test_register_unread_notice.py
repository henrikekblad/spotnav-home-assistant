"""A register that is only briefly unread says nothing: after a restart, or a blink, the kept remainder is
planned quietly for half an hour; a reading that comes back plans again once, on the register; a meter that
stays unread says so at the half hour without waiting for anything else; and a count that began without a
reading and reads now is counted from now without saying the meter cannot be read.
"""

from __future__ import annotations

from datetime import time, timedelta
from typing import Any

import pytest
from freezegun import freeze_time
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.spotnav.const import CONF_ENERGY_REGISTER_ENTITY
from custom_components.spotnav.planning.auto_settings import EnergyBaseline
from custom_components.spotnav.runtime import domain_data
from tests.relay import serve as serve_prices

from .test_dashboard_api import NOW
from .test_replug import _car, _delivered, _meter, _record_charger_commands, Car
from .world import REGISTER

pytestmark = pytest.mark.usefixtures("offline_relay")


def _notices(car: Car) -> list[dict[str, Any]]:
    return [line for line in car.dashboard()["status"]["lines"] if line["code"] == "remaining_need_estimated"]


async def _restart(hass: HomeAssistant, frozen: Any, car: Car) -> Car:
    """Home Assistant starting again with the register not yet read (`unknown`), as an OCPP charger's
    register is until its first meter value."""
    entry = car.charger
    hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_ENERGY_REGISTER_ENTITY: REGISTER})
    await hass.async_block_till_done()
    hass.states.async_set(REGISTER, "unknown")
    frozen.tick(timedelta(minutes=1))
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    restarted = Car(hass, frozen, entry)
    restarted.plug.connected = True
    assert restarted.controller.energy_register_entity_id == REGISTER
    await restarted.preview.async_recalculate()
    return restarted


async def _charged_and_restarted(hass: HomeAssistant, transport: Any, frozen: Any) -> Car:
    serve_prices(transport, rising=True)
    _record_charger_commands(hass)
    car = await _car(hass, frozen, departure=time(20, 0), departure_enabled=True)
    await _delivered(hass, frozen, 1003.5, minutes=20)
    await car.preview.async_recalculate()
    assert car.preview.snapshot().remaining_kwh == pytest.approx(6.5)
    return await _restart(hass, frozen, car)


async def test_a_restart_with_the_register_unknown_plans_the_kept_remainder_without_a_notice(
    hass: HomeAssistant, transport: Any
) -> None:
    with freeze_time(NOW) as frozen:
        car = await _charged_and_restarted(hass, transport, frozen)

        snapshot = car.preview.snapshot()
        assert snapshot.remaining_kwh == pytest.approx(6.5), "the kept remainder, never the whole request"
        assert snapshot.energy_basis != "kept"
        assert _notices(car) == []
        assert car.dashboard()["plan"]["remaining_kwh"] == pytest.approx(6.5)


async def test_a_reading_that_comes_back_plans_again_once_on_the_register(
    hass: HomeAssistant, transport: Any
) -> None:
    with freeze_time(NOW) as frozen:
        car = await _charged_and_restarted(hass, transport, frozen)
        attempt = car.preview.snapshot().attempt

        frozen.tick(timedelta(minutes=2))
        _meter(hass, 1003.5)  # the first meter value after the restart
        await hass.async_block_till_done()

        snapshot = car.preview.snapshot()
        assert snapshot.attempt > attempt, "the reading itself planned again"
        assert snapshot.energy_basis == "register"
        assert snapshot.remaining_kwh == pytest.approx(6.5)
        assert _notices(car) == []

        attempt = snapshot.attempt
        await _delivered(hass, frozen, 1003.6, minutes=1)
        await _delivered(hass, frozen, 1003.7, minutes=1)
        assert car.preview.snapshot().attempt == attempt, "once per return, never per reading"


async def test_a_register_unread_for_half_an_hour_says_so_without_any_other_trigger(
    hass: HomeAssistant, transport: Any
) -> None:
    with freeze_time(NOW) as frozen:
        car = await _charged_and_restarted(hass, transport, frozen)
        assert _notices(car) == []

        frozen.tick(timedelta(minutes=29))
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()
        assert _notices(car) == [], "still within the half hour"

        frozen.tick(timedelta(minutes=2))
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()
        assert car.preview.snapshot().energy_basis == "kept"
        assert _notices(car) == [{"code": "remaining_need_estimated", "params": {"kwh": 6.5, "basis": "kept"}}]

        # And when it reads at last, the notice goes with the replan the reading asks for.
        _meter(hass, 1003.5)
        await hass.async_block_till_done()
        assert car.preview.snapshot().energy_basis == "register"
        assert _notices(car) == []


async def test_a_register_that_blinks_unread_while_running_is_quiet_for_the_half_hour(
    hass: HomeAssistant, transport: Any
) -> None:
    serve_prices(transport, rising=True)
    _record_charger_commands(hass)
    with freeze_time(NOW) as frozen:
        car = await _car(hass, frozen, departure=time(20, 0), departure_enabled=True)
        await _delivered(hass, frozen, 1003.5, minutes=20)
        await car.preview.async_recalculate()
        frozen.tick(timedelta(hours=2))  # idle: the last believed reading is long ago
        _meter(hass, None)
        await hass.async_block_till_done()
        await car.preview.async_recalculate()

        assert car.preview.snapshot().remaining_kwh == pytest.approx(6.5)
        assert _notices(car) == [], "counted from when it stopped reading, not from the last rise"


async def test_a_count_begun_without_a_reading_counts_from_now_without_the_notice(
    hass: HomeAssistant, transport: Any
) -> None:
    serve_prices(transport, rising=True)
    _record_charger_commands(hass)
    with freeze_time(NOW) as frozen:
        car = await _car(hass, frozen, departure=time(20, 0), departure_enabled=True)
        key = car.baseline().departure_key
        # The epoch began while the register could not be read; 3.5 kWh were vouched for meanwhile.
        await domain_data(hass).auto_store.async_update(
            car.charger.entry_id,
            energy_baseline=EnergyBaseline(register_kwh=None, departure_key=key, delivered_kwh=3.5),
        )
        car.preview._live_baseline = None  # noqa: SLF001 - the stored record is the one in force
        _meter(hass, 1004.0)
        await hass.async_block_till_done()

        resolved: list[Any] = []
        counted = car.preview._manual_kwh_remaining  # noqa: SLF001

        async def spy(*args: Any) -> Any:
            resolved.append(await counted(*args))
            return resolved[-1]

        car.preview._manual_kwh_remaining = spy  # noqa: SLF001
        await car.preview.async_recalculate()

        assert resolved and resolved[0].delivered_energy_trustworthy is False, "hybrid credits no forecast sun"
        snapshot = car.preview.snapshot()
        assert snapshot.remaining_kwh == pytest.approx(6.5), "the kept remainder, counted from now"
        assert snapshot.energy_basis not in ("kept", "sessions")
        assert _notices(car) == []
        assert car.baseline().register_kwh == pytest.approx(1004.0)
        assert car.dashboard()["plan"]["remaining_kwh"] == pytest.approx(6.5)
