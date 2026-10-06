"""Adversarial re-review of 61231cb: a connection that becomes unknown hides an unplug and a replug."""

from __future__ import annotations

from datetime import timedelta

import pytest
from freezegun import freeze_time
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.spotnav.const import CONF_CHARGE_CONTROL
from custom_components.spotnav.execution.controller import ChargingController

from .test_dashboard_api import NOW
from .test_replug import Plug

pytestmark = pytest.mark.usefixtures("offline_relay")


async def test_unknown_connection_gap_keeps_plugged_in_since(hass: HomeAssistant) -> None:
    with freeze_time(NOW) as frozen:
        hass.states.async_set("switch.a", "off")
        controller = ChargingController(hass, "entry_a", {CONF_CHARGE_CONTROL: "switch.a"})
        await controller.async_initialize()
        plug = Plug(hass, controller, "switch.a")
        await plug.set(False)
        await plug.set(True)  # car A plugged in, seen live
        seen = dt_util.utcnow()
        # The charger goes offline (connection unknown) for an hour: car A leaves, car B arrives.
        frozen.tick(timedelta(minutes=5))
        await plug.set(None)
        frozen.tick(timedelta(hours=1))
        await plug.set(True)  # back online: a car is connected - which one is not known
        # Continuity was not shown: the charger did not watch across the gap.
        assert controller.plugged_in_since != seen, "unknown gap counted as plugged in throughout"
        assert controller.plugged_in_for_count != seen, "car A's charges count for the car found after the gap"
        await controller.async_shutdown()
