"""Review C: solar takes back a start load balancing held, resumed by the regulator after the sun went."""

from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant

pytestmark = pytest.mark.usefixtures("offline_relay")


async def test_c_a_held_solar_start_resumed_at_night_is_stopped_within_the_stop_delay(hass: HomeAssistant) -> None:
    """Solar's start is held back by load balancing; the sun goes; hours later the house load drops and
    the regulator resumes the paused charge as solar's. Solar adopts it as `on` from now, so `min_on_s`
    (600 s) runs before it may stop it: the car charges on grid that long, with no sun at any moment."""
    from .world import controller_of, set_charger_delivered_a, set_site_power_w, solar_setup, tick_site

    charger, site_entry, controller, coordinator, clock, turn_on_calls, turn_off_calls = await solar_setup(hass)
    site = controller_of(hass, site_entry.entry_id)
    prefix = charger.entry_id
    controller.set_start_cap(lambda: 3.0)
    set_site_power_w(hass, "solar_site", -1400.0)
    await tick_site(hass, site)
    clock.value = 125.0
    await tick_site(hass, site)
    assert controller.paused_by_balancing

    # The sun is gone for good.
    for t in range(200, 4000, 50):
        clock.value = float(t)
        set_site_power_w(hass, "solar_site", 1000.0)
        await tick_site(hass, site)
    assert controller.paused_by_balancing, "nothing ended solar's held wish when the sun went"

    controller.set_start_cap(None)
    assert await controller.async_battery_probe_start(6, capped=True)
    hass.states.async_set(f"switch.{prefix}", "on")
    set_charger_delivered_a(hass, prefix, 6.0)
    resumed_at = 4000.0
    stopped_at = None
    for t in range(4000, 6000, 10):
        clock.value = float(t)
        set_site_power_w(hass, "solar_site", 2400.0)
        await tick_site(hass, site)
        if turn_off_calls:
            stopped_at = float(t)
            break
    assert stopped_at is not None
    assert stopped_at - resumed_at <= 300.0, f"grid charge for {stopped_at - resumed_at:.0f} s with no sun"
