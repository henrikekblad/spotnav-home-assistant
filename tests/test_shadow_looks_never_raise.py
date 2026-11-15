"""A look of the battery shadow or the switched load that fails never raises into the site and is logged once per
kind, not at every ten-second look; a switched load whose start fails still loads the site and keeps looking."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.spotnav.battery import plan_runtime, runtime
from custom_components.spotnav.loads import runtime as loads_runtime
from custom_components.spotnav.loads.switch_load import RunHistory, quarter_start
from custom_components.spotnav.runtime import site_controller_for

from tests.test_battery_plan_runtime import _planned
from tests.test_switch_load_runtime import _prices
from tests.test_switch_load_runtime import _site as _load_site

pytestmark = pytest.mark.usefixtures("offline_relay")


def _broken(*_args: Any, **_kwargs: Any) -> Any:
    raise RuntimeError("broken")


def _warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [record.getMessage() for record in caplog.records if record.levelno == logging.WARNING]


async def test_a_failing_battery_look_is_logged_once_per_kind(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _site_entry, controller, now = await _planned(hass, monkeypatch)
    shadow = controller.battery_shadow
    monkeypatch.setattr(runtime.BatteryShadow, "inputs", _broken)
    monkeypatch.setattr(plan_runtime.BatteryPlanShadow, "tick", _broken)
    caplog.clear()

    for seconds in range(10, 70, 10):
        shadow.tick(now + timedelta(seconds=seconds))

    assert _warnings(caplog) == [
        "SpotNav battery shadow: the hold look failed; it is tried again at each look",
        "SpotNav battery shadow: the plan look failed; it is tried again at each look",
    ]


async def test_a_failing_switched_load_look_is_logged_once(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _prices(monkeypatch, quarter_start(dt_util.now()))
    _site_entry, controller = await _load_site(hass)
    monkeypatch.setattr(loads_runtime.SwitchLoadShadow, "tick", _broken)
    caplog.clear()

    for _ in range(5):
        controller.switch_load._async_tick(dt_util.now())

    assert _warnings(caplog) == ["SpotNav switched load: the look failed; it is tried again at each look"]


@pytest.mark.parametrize("part", ["record", "look"])
async def test_a_switched_load_whose_start_fails_still_loads_the_site(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, part: str
) -> None:
    _prices(monkeypatch, quarter_start(dt_util.now()))
    site, _controller = await _load_site(hass)
    assert await hass.config_entries.async_unload(site.entry_id)
    if part == "record":
        monkeypatch.setattr(RunHistory, "from_dict", _broken)
    else:
        monkeypatch.setattr(loads_runtime.SwitchLoadShadow, "async_replan", _broken)

    await hass.config_entries.async_setup(site.entry_id)
    await hass.async_block_till_done()

    assert site.state is ConfigEntryState.LOADED
    controller = site_controller_for(hass, site.entry_id)
    assert controller is not None and controller.switch_load._timer_cancel is not None
