"""FAKE stand-in for core Saunum: one sauna that does not exist, for looking at SpotNav's Bastu tab on a real Home
Assistant. Nothing reaches a network; the air is an in-memory model that starts over at every restart.

The shapes are the ones SpotNav reads for core `saunum` (`sauna/detect.py`, `spa/adapters.py`, `spa/control.py`):
a climate with hvac `off`/`heat` (a session), `hvac_action`, `temperature` and `current_temperature`, range
40-100 °C; a `door` binary sensor on the same device (the lock SpotNav respects); the session length as a number with
the translation key `sauna_duration`. A session ends by itself after that length and stops when the door opens, as
the real unit does; heat is refused while the door is open. The extra "Door open (fake control)" switch opens and
closes the fake door. Remove the folder to remove it.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.event import async_track_time_interval

DOMAIN = "saunum"
PLATFORMS = ["climate", "binary_sensor", "number", "switch"]
HEAT_C_MIN = 1.5
LOSS_C_MIN = 0.3
ROOM_C = 20.0
TICK = timedelta(seconds=15)


class FakeSauna:
    """The sauna's state, shared by its entities; each change calls the listeners."""

    def __init__(self) -> None:
        self.session = False
        self.session_started = 0.0
        self.door_open = False
        self.target_c = 80.0
        self.air_c = ROOM_C
        self.duration_min = 120.0
        self.last = time.monotonic()
        self._listeners: list[Callable[[], None]] = []

    def listen(self, listener: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener)

    def heating(self) -> bool:
        return self.session and self.air_c < self.target_c

    def advance(self) -> None:
        now = time.monotonic()
        minutes = (now - self.last) / 60.0
        self.last = now
        if self.heating():
            self.air_c = min(self.target_c, self.air_c + HEAT_C_MIN * minutes)
        elif not self.session and self.air_c > ROOM_C:
            self.air_c = max(ROOM_C, self.air_c - LOSS_C_MIN * minutes)
        if self.session and now - self.session_started >= self.duration_min * 60.0:
            self.session = False

    def changed(self) -> None:
        for listener in list(self._listeners):
            listener()


def device_info(entry: ConfigEntry) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name="Saunum Leil (fake)",
        manufacturer="Saunum",
        model="Leil Touch Panel",
        sw_version="fake",
    )


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    sauna = FakeSauna()
    entry.runtime_data = sauna

    @callback
    def tick(_now) -> None:
        sauna.advance()
        sauna.changed()

    entry.async_on_unload(async_track_time_interval(hass, tick, TICK))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
