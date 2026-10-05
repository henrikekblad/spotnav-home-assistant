"""One bare charger with its execution boundary: a plain switch that follows its commands, a settings
store, an `AutoExecutor` wired to the controller, a hand-moved plug and the controller's window timers
recorded (the `timers` fixture), so a window boundary is an explicit `fire`."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.spotnav.const import CONF_CHARGE_CONTROL
from custom_components.spotnav.execution.auto_execution import AutoExecutor
from custom_components.spotnav.execution.controller import ChargingController
from custom_components.spotnav.planning.auto_settings import async_setup_auto_settings, AutoSettingsStore

from .helpers import install_schedule
from .relay import FakeScheduler
from .test_replug import Plug, _obedient_switch

SWITCH = "switch.a"
ENTRY = "entry_a"


@dataclass
class World:
    hass: HomeAssistant
    controller: ChargingController
    executor: AutoExecutor
    store: AutoSettingsStore
    plug: Plug
    timers: FakeScheduler
    starts: list[Any] = field(default_factory=list)
    stops: list[Any] = field(default_factory=list)

    @property
    def pause(self) -> Any:
        return self.store.settings(ENTRY).pause

    async def fire(self, name: str) -> None:
        """Fire the live window timers whose callback is `name` (`_async_start_callback`, ...)."""
        for timer in list(self.timers.pending):
            if getattr(timer.action, "__name__", "") == name:
                timer.fire()
        await self.hass.async_block_till_done()

    async def switch(self, state: str) -> None:
        """The charger's control reports `state` by itself (not through a command)."""
        current = self.hass.states.get(SWITCH)
        self.hass.states.async_set(SWITCH, state, None if current is None else current.attributes)
        await self.hass.async_block_till_done()

    async def restart(self) -> World:
        """A restart after the entry was unloaded (the controller's own shutdown ran): a new controller and boundary
        over the same stores and states. `ha_restart` is Home Assistant's own stop, which unloads nothing."""
        await self.executor.async_shutdown()
        await self.controller.async_shutdown()
        return await self._start_again()

    async def ha_restart(self) -> World:
        """Home Assistant restarts as it really does: its stop (`__init__._async_stop`) shuts the boundary down and
        saves what the charge-ownership core has unsaved, but unloads no entry, so the controller's own shutdown
        never runs. What is on disk then is what the next start reads."""
        await self.executor.async_shutdown()
        await self.controller.async_flush_session()
        await self.hass.async_block_till_done()
        disk = await self.controller._store.async_load()  # noqa: SLF001
        # The process ends: the old controller's listeners are dropped, and nothing it would save reaches the disk.
        self.controller._cancel_session_save()  # noqa: SLF001
        await self.controller.async_shutdown()
        await self.controller._store.async_save(dict(disk) if disk else {})  # noqa: SLF001
        return await self._start_again()

    async def _start_again(self) -> World:
        controller = ChargingController(self.hass, ENTRY, {CONF_CHARGE_CONTROL: SWITCH})
        executor = AutoExecutor(self.hass, controller, self.store)
        plug = Plug.__new__(Plug)
        plug.hass, plug.control, plug.connected = self.hass, SWITCH, self.plug.connected
        controller.adapter.vehicle_connected = lambda: plug.connected  # type: ignore[method-assign]
        await executor.async_start()
        await controller.async_initialize()
        await executor.async_after_restore()
        await self.hass.async_block_till_done()
        return World(self.hass, controller, executor, self.store, plug, self.timers, self.starts, self.stops)

    async def shutdown(self) -> None:
        await self.executor.async_shutdown()
        await self.controller.async_shutdown()


def open_window(minutes_in: int = 10, minutes_left: int = 50, amps: int = 10) -> dict[str, Any]:
    now = dt_util.utcnow()
    return {
        "start": (now - timedelta(minutes=minutes_in)).isoformat(),
        "end": (now + timedelta(minutes=minutes_left)).isoformat(),
        "amps": amps,
    }


def later_window(hours: float = 2.0, amps: int = 10) -> dict[str, Any]:
    start = dt_util.utcnow() + timedelta(hours=hours)
    return {"start": start.isoformat(), "end": (start + timedelta(hours=1)).isoformat(), "amps": amps}


def two_windows() -> dict[str, Any]:
    """A plan whose first window is open now (ten minutes in, fifty to go) and whose second starts in two
    hours and lasts one."""
    now = dt_util.utcnow()
    first = (now - timedelta(minutes=10), now + timedelta(minutes=50))
    second = (now + timedelta(hours=2), now + timedelta(hours=3))
    return {
        "periods": [{"start": start.isoformat(), "end": end.isoformat()} for start, end in (first, second)],
        "amps": 10,
    }


async def pause_world(
    hass: HomeAssistant,
    timers: FakeScheduler,
    *,
    plan: dict[str, Any] | None = None,
    connected: bool | None = True,
    charging: bool = False,
) -> World:
    hass.states.async_set(SWITCH, "on" if charging else "off")
    starts, stops = _obedient_switch(hass)
    store = await async_setup_auto_settings(hass)
    controller = ChargingController(hass, ENTRY, {CONF_CHARGE_CONTROL: SWITCH})
    executor = AutoExecutor(hass, controller, store)
    await executor.async_start()
    await controller.async_initialize()
    await executor.async_after_restore()
    plug = Plug(hass, controller, SWITCH)
    await plug.set(False)
    if connected is not False:
        await plug.set(connected)
    if plan is not None:
        await install_schedule(controller, plan)
    await hass.async_block_till_done()
    return World(hass, controller, executor, store, plug, timers, starts, stops)
