"""The site's shutdown cancels its timer and listeners first, then closes each part; one part that fails to close
does not leave the others (or the timer) running."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant

from custom_components.spotnav.battery.runtime import BatteryShadow
from custom_components.spotnav.loads.runtime import SwitchLoadShadow
from custom_components.spotnav.runtime import site_controller_for
from custom_components.spotnav.site import hour_statistics
from custom_components.spotnav.site.demand_tracker import SiteDemand
from custom_components.spotnav.site.solar_correction_shadow import SiteSolarCorrection

from tests.world import setup_charger_and_site

pytestmark = pytest.mark.usefixtures("offline_relay")


async def test_a_part_that_fails_to_close_does_not_keep_the_rest_running(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    _charger, site = await setup_charger_and_site(hass)
    assert site is not None
    controller = site_controller_for(hass, site.entry_id)
    assert controller is not None
    closed: list[str] = []

    real_battery = BatteryShadow.async_shutdown

    async def broken(self: Any) -> None:
        await real_battery(self)
        raise RuntimeError("cannot close")

    def closing(name: str, real: Any) -> Any:
        async def close(self: Any) -> None:
            closed.append(name)
            await real(self)

        return close

    monkeypatch.setattr(BatteryShadow, "async_shutdown", broken)
    monkeypatch.setattr(SwitchLoadShadow, "async_shutdown", closing("switch_load", SwitchLoadShadow.async_shutdown))
    monkeypatch.setattr(
        SiteSolarCorrection, "async_shutdown", closing("solar_correction", SiteSolarCorrection.async_shutdown)
    )

    assert await hass.config_entries.async_unload(site.entry_id)
    await hass.async_block_till_done()

    assert site.state is ConfigEntryState.NOT_LOADED
    assert closed == ["switch_load", "solar_correction"]
    assert controller._timer_cancel is None and controller._state_listener_cancel is None


async def test_a_reload_cancels_a_backfill_still_reading_the_statistics(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The back-fill reads months of statistics in the background: a reload ends the old controller's before its
    demand stores close, so it never files into them nor runs on beside the new controller's."""
    _charger, site = await setup_charger_and_site(hass)
    assert site is not None
    controller = site_controller_for(hass, site.entry_id)
    assert controller is not None
    started = asyncio.Event()
    ended: list[str] = []

    async def reading(self: Any, *_args: Any) -> str:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            ended.append("cancelled")
            raise
        ended.append("finished")
        return "done"

    monkeypatch.setattr(hour_statistics, "recorder_ready", lambda _hass: True)
    monkeypatch.setattr(hour_statistics, "site_series", lambda _hass, _config: object())
    monkeypatch.setattr(SiteDemand, "async_backfill", reading)
    assert controller.async_start_backfill()
    await started.wait()

    assert await hass.config_entries.async_reload(site.entry_id)
    await hass.async_block_till_done()

    assert ended == ["cancelled"]
    assert controller._backfill_task is None  # noqa: SLF001
    assert site_controller_for(hass, site.entry_id) is not controller
