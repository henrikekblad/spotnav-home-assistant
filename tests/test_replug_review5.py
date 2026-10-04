"""Regressions from the fifth review: a drop that did not hold is forgotten (so a later glitch starts its own
count), energy delivered while Home Assistant was down is counted, the count is kept on disk as it moves,
and a regulator resume whose start command fails still waits as the charge it was.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from typing import Any

import pytest
from freezegun import freeze_time
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.spotnav.planning.auto_controller import advance_register, DROP_HOLD_S
from custom_components.spotnav.planning.auto_settings import EnergyBaseline
from custom_components.spotnav.runtime import domain_data
from tests.relay import serve as serve_prices

from .test_dashboard_api import NOW
from .test_replug import _car, _meter, _record_charger_commands, _switch_controller

pytestmark = pytest.mark.usefixtures("offline_relay")

T0 = datetime(2026, 9, 22, 20, 0, tzinfo=timezone.utc)
KW = 43.0


def _read(baseline: EnergyBaseline, reading: float, minutes: float, charged_s: float = 0.0):
    at = T0 + timedelta(minutes=minutes)
    return advance_register(baseline, reading, at, max_kw=KW, plugged_in_at=None, charged_s=charged_s, changed_at=at)


def test_a_drop_that_did_not_hold_is_forgotten_and_a_later_glitch_counts_alone() -> None:
    baseline = EnergyBaseline(register_kwh=1000.0, departure_key="k", started_at=T0, last_register_kwh=1000.0,
                              last_register_at=T0, charge_mark_s=0.0)
    step = _read(baseline, 0.0, 10)  # a reboot, idle
    step = _read(step.baseline, 1000.0, 11)  # back to its value
    assert step.baseline.pending_drop_at is None and step.baseline.pending_drop_count == 0

    step = _read(step.baseline, 0.0, 180)  # hours later, another reboot
    assert not step.accepted
    step = _read(step.baseline, 1000.0, 181, charged_s=60.0)
    step = _read(step.baseline, 1000.2, 182, charged_s=120.0)

    assert step.delivered_kwh == pytest.approx(0.2), "0.2 kWh charged, never the whole meter"


def test_a_pending_drop_read_again_much_later_completes_the_restart() -> None:
    """A drop nothing contradicted is still the same drop, however long its readings take to come."""
    baseline = EnergyBaseline(
        register_kwh=1000.0, departure_key="k", started_at=T0, last_register_kwh=1000.0, last_register_at=T0,
        charge_mark_s=0.0, pending_drop_kwh=0.0, pending_drop_at=T0, pending_drop_count=1,
    )
    step = _read(baseline, 0.1, DROP_HOLD_S * 4 / 60)

    assert step.accepted and step.baseline.pending_drop_at is None
    assert step.delivered_kwh == pytest.approx(0.1)


def test_a_session_register_read_every_ten_minutes_restarts_and_counts_on() -> None:
    baseline = EnergyBaseline(register_kwh=0.0, departure_key="k", started_at=T0, last_register_kwh=8.0,
                              last_register_at=T0, charge_mark_s=0.0)
    for i in range(1, 7):  # replugged unseen; the per-session register climbs 0 -> 6 kWh, read every 10 min
        at = T0 + timedelta(hours=2, minutes=10 * i)
        step = advance_register(baseline, 1.0 * i, at, max_kw=KW, plugged_in_at=None, charged_s=600.0 * i, changed_at=at)
        baseline = step.baseline

    assert step.delivered_kwh == pytest.approx(14.0), "8 kWh before the reset and the 6 since"
    assert baseline.pending_drop_at is None and baseline.rejected_kwh is None


async def test_energy_delivered_while_home_assistant_was_down_is_counted(hass: HomeAssistant, transport: Any) -> None:
    serve_prices(transport, rising=True)
    _record_charger_commands(hass)
    with freeze_time(NOW) as frozen:
        car = await _car(hass, frozen, kwh=10.0, departure=time(20, 0), departure_enabled=True)
        c = car.controller
        frozen.tick(timedelta(minutes=5))
        _meter(hass, 1001.0)
        await hass.async_block_till_done()
        stored = domain_data(hass).auto_store.energy_baseline(car.charger.entry_id)
        assert stored is not None and stored.last_register_kwh == pytest.approx(1001.0), "kept on disk as it moves"
        # Home Assistant goes down: the watch, the count in memory and the charge clock are gone.
        car.preview._drop_energy_watch()  # noqa: SLF001
        car.preview._live_baseline = None  # noqa: SLF001
        frozen.tick(timedelta(minutes=30))
        _meter(hass, 1006.0)  # 5 kWh charged meanwhile; the charge then ended
        hass.states.async_set("switch.wallbox", "off")
        await hass.async_block_till_done()
        c._charge_clock_s, c._charge_clock_since = 0.0, None  # noqa: SLF001 - the clock starts again at boot
        frozen.tick(timedelta(seconds=20))

        await car.preview.async_recalculate()  # the first calculation after boot reads it once

        assert car.preview.snapshot().remaining_kwh == pytest.approx(4.0)


async def test_the_count_is_written_at_most_once_a_minute(hass: HomeAssistant, transport: Any) -> None:
    serve_prices(transport, rising=True)
    _record_charger_commands(hass)
    with freeze_time(NOW) as frozen:
        car = await _car(hass, frozen, kwh=10.0, departure=time(20, 0), departure_enabled=True)
        store = domain_data(hass).auto_store
        frozen.tick(timedelta(minutes=5))
        _meter(hass, 1001.0)
        await hass.async_block_till_done()
        frozen.tick(timedelta(seconds=10))
        _meter(hass, 1001.1)
        await hass.async_block_till_done()
        assert store.energy_baseline(car.charger.entry_id).last_register_kwh == pytest.approx(1001.0)

        await car.settle(seconds=60)  # the minute is up

        assert store.energy_baseline(car.charger.entry_id).last_register_kwh == pytest.approx(1001.1)


async def test_a_regulator_resume_whose_start_fails_still_waits_as_the_plans(hass: HomeAssistant) -> None:
    from .helpers import install_schedule
    from .test_replug import _open_window

    controller, plug, _, _ = await _switch_controller(hass, None)
    await install_schedule(controller, _open_window())
    await plug.set(True, control="on")
    assert controller.charge_origin == "plan_window"
    await controller._regulated_stop("pause")  # noqa: SLF001
    await plug.set(True, control="off")

    async def broken(_amps: Any = None) -> bool:
        raise RuntimeError("the charger said no")

    working = controller.adapter.async_start
    controller.adapter.async_start = broken  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        await controller.async_battery_probe_start(8)
    assert controller.paused_by_balancing
    controller.adapter.async_start = working  # type: ignore[method-assign]

    assert await controller.async_battery_probe_start(8)
    assert controller.charge_origin == "plan_window" and controller._plan_charge  # noqa: SLF001
    await controller.async_shutdown()


async def test_the_count_is_trusted_again_once_a_drop_resolves(hass: HomeAssistant, transport: Any) -> None:
    from .test_replug import _delivered

    serve_prices(transport, rising=True)
    _record_charger_commands(hass)
    with freeze_time(NOW) as frozen:
        car = await _car(hass, frozen, kwh=10.0, departure=time(20, 0), departure_enabled=True)
        preview = car.preview
        settings = domain_data(hass).auto_store.settings(car.charger.entry_id)
        entry = preview._manager.catalogue_snapshot().area(settings.area_id)  # noqa: SLF001

        async def trusted() -> bool:
            resolution = await preview._manual_kwh_remaining(settings, dt_util.utcnow(), entry)  # noqa: SLF001
            return resolution.delivered_energy_trustworthy

        await _delivered(hass, frozen, 1001.0, minutes=5)
        assert await trusted()
        await _delivered(hass, frozen, 0.0, minutes=1)  # a reboot: in doubt
        assert not await trusted()
        await _delivered(hass, frozen, 1001.2, minutes=1)  # back: resolved
        assert await trusted()
