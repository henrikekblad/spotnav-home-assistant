"""Home Assistant stopping shuts the sites' load balancing down, as it does the chargers' boundaries.

No config entry is unloaded when Home Assistant stops, so a site controller's `async_shutdown` used to run
never: its timer and listeners lived on, and an apply pass could still reach a charger while Home Assistant
was stopping. `__init__._async_stop` now shuts it down, sending nothing.
"""

from __future__ import annotations

from datetime import datetime

from freezegun import freeze_time
from homeassistant.core import HomeAssistant

from custom_components.spotnav import _async_stop

from .test_charger_closed_loop import _world
from .world import controller_of, set_site_current_a


async def test_stopping_home_assistant_shuts_the_site_down_and_sends_nothing(hass: HomeAssistant) -> None:
    with freeze_time(datetime(2026, 9, 30, 12, 0, 0)) as frozen:
        charger, entry, controller, site, prefix = await _world(hass, "st")
        await controller.async_start(amps=16)
        set_site_current_a(hass, prefix, 16.0)
        assert await hass.config_entries.async_setup(site.entry_id)
        await hass.async_block_till_done()
        site_controller = controller_of(hass, site.entry_id)
        writes, stops = len(charger.writes), len(charger.stops)

        await _async_stop(hass)

        assert site_controller._closed  # noqa: SLF001
        assert site_controller._timer_cancel is None and site_controller._state_listener_cancel is None  # noqa: SLF001
        # An overload after the stop: nothing decides on it any more.
        frozen.tick(20)
        set_site_current_a(hass, prefix, 22.0)
        await hass.async_block_till_done()
        assert (len(charger.writes), len(charger.stops)) == (writes, stops)
        assert controller.charging, "stopping Home Assistant stops no charge"
