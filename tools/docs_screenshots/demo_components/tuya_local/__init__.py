"""Stand-in for Tuya Local with a pool: a pool heat pump (a climate with Tuya Local's `pool_heatpump` translation key,
which SpotNav's pool detection ranks first) and, on a device of its own, the pool's circulation pump (a switch named for
the pool, as a relay a person renamed).

The water is a small model shared by the two: the heat pump heats it while it is in `heat`, the circulation runs and the
water is under the setpoint; otherwise it cools slowly towards the air. Writes apply at once and read back on the next
state. Nothing reaches a network; the state starts over at every restart.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_interval

DOMAIN = "tuya_local"
PLATFORMS = ["climate", "switch"]
HEAT_C_H = 0.3
LOSS_C_H = 0.05
AIR_C = 12.0
TICK = timedelta(seconds=30)


class Pool:
    """The water, the heat pump's mode and setpoint and the circulation, shared by the entities."""

    def __init__(self) -> None:
        self.water_c = 26.5
        self.target_c = 28.0
        self.heat_on = True
        self.circulation_on = True
        self.last = time.monotonic()
        self._listeners: list[Callable[[], None]] = []

    def listen(self, listener: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener)

    def heating(self) -> bool:
        return self.heat_on and self.circulation_on and self.water_c < self.target_c

    def advance(self) -> None:
        now = time.monotonic()
        hours = (now - self.last) / 3600.0
        self.last = now
        if self.heating():
            self.water_c = min(self.target_c, self.water_c + HEAT_C_H * hours)
        elif self.water_c > AIR_C:
            self.water_c = max(AIR_C, self.water_c - LOSS_C_H * hours)

    def changed(self) -> None:
        for listener in list(self._listeners):
            listener()


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    pool = Pool()
    entry.runtime_data = pool

    @callback
    def tick(_now) -> None:
        pool.advance()
        pool.changed()

    entry.async_on_unload(async_track_time_interval(hass, tick, TICK))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
