"""The battery control's life: its launch and timer, the start on what was stored (a snapshot handed back first, one
whose recipe is gone set aside), the stop, the unload and a removed site's store."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_RESTORED
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from ..commands import (
    BrandControl,
    control_for,
    export_control_for,
)
from ..executor import (
    ExecState,
    Executor,
)
from ..ledger import Ledger
from ..monitor import Monitor
from ..runtime import TICK_S, read_export_facts
from .const import (
    HAND_BACK_DEADLINE_S,
    LOOK_WAIT_S,
    ORPHANED,
    STARTUP_LOOK_S,
    STARTUP_WAIT_S,
    START_RETRY_MAX_S,
    START_RETRY_S,
    STORE_VERSION,
    _METHOD_SERVICES,
)
from .stored import _orphans, async_hand_back_stored, store_key


_LOGGER = logging.getLogger(__package__)  # the shell's one logger, whichever of its modules logs


class Life:
    """The start, the timer and the stop. A mixin of `BatteryControl`, on its state."""

    # ---- life

    @callback
    def async_launch(self, entry: ConfigEntry | None) -> None:
        """Start in the background: the restore first, then the look timer. Never awaited by the site's setup."""
        name = f"spotnav_battery_control_start_{self.site.entry_id}"
        coro = self._async_start_then_look()
        self._start_task = (
            self.hass.async_create_background_task(coro, name) if entry is None
            else entry.async_create_background_task(self.hass, coro, name)
        )

    async def _async_start_then_look(self) -> None:
        delay = START_RETRY_S
        while True:
            try:
                await self.async_start()
                break
            except Exception:  # noqa: BLE001 - the site must run on; the start is tried again (a stored snapshot
                # must be handed back)
                self._look_failed("start")
            if self._closed:
                return
            await asyncio.sleep(delay)
            delay = min(delay * 2, START_RETRY_MAX_S)
            if self._closed:
                return
        if self._closed or self._halted:
            return
        self._timer_cancel = async_track_time_interval(self.hass, self._on_timer, timedelta(seconds=TICK_S))

    @callback
    def halt(self) -> None:
        """The site shuts down: the look timer ends (a look running ends in `async_shutdown`). Not closed yet: a
        running hardware test is still handed back through this control first."""
        self._halted = True
        if self._timer_cancel is not None:
            self._timer_cancel()
            self._timer_cancel = None

    def holds_anything(self) -> bool:
        """Whether the battery or the export limit is still held (a snapshot not handed back)."""
        return self._holds_anything()

    async def async_save(self) -> None:
        """Store the state as it stands (a hand-back cut short at Home Assistant's stop: the next start finishes it).
        Nothing before the store was read."""
        if self._loaded:
            await self._async_save()

    @callback
    def _on_timer(self, _now: datetime) -> None:
        if self._closed or self._halted or (self._look_task is not None and not self._look_task.done()):
            return  # a look still runs (a slow write): this one is skipped, the next timer comes
        self._look_task = self.hass.async_create_background_task(
            self.async_look(), f"spotnav_battery_control_look_{self.site.entry_id}"
        )

    async def async_start(self) -> None:
        """Load the state; a stored snapshot is handed back before anything else."""
        try:
            stored = await self._store.async_load()
        except Exception:  # noqa: BLE001 - a broken store is an empty one; nothing was written by it
            _LOGGER.warning("The battery control state could not be read")
            stored = None
        self._stored = dict(stored) if isinstance(stored, dict) else {}
        # With the store itself, before anything is awaited: a start cut short stores what it holds (`_data`).
        self._orphaned = _orphans(self._stored.get(ORPHANED))
        self._loaded = True
        self._migrate_mode()
        reports = self._stored.get("reports")
        # Storage is untrusted: a report that is no mapping is none (every reader takes a mapping or nothing).
        self.reports = {
            key: report for key, report in reports.items() if isinstance(key, str) and isinstance(report, dict)
        } if isinstance(reports, dict) else {}
        self._load_gate(self._stored.get("gate"))
        await self._async_check_codes()
        self._take_back("battery", control_for, self._stored)
        self._set_aside("battery", control_for, self._stored)
        state = ExecState.from_dict(self._stored.get("state"))
        self.facts = self._read_facts()
        self._grandfather_running()
        # The battery first (a grid charge left standing adds import), then the export limit.
        control = control_for(state.brand) or control_for(self.facts.control_brand)
        if control is not None:
            self.executor = Executor(
                control, Ledger.from_dict(self._stored.get("ledger")), Monitor.from_dict(self._stored.get("monitor")),
                state, watchdog_s=self._watchdog_s,
            )
            if state.holds_snapshot:
                await self._async_wait_for_controls(self.executor)
            self._log(self.executor.restart(dt_util.now()))
            if self.executor.state.phase == "restoring":
                await self._async_until_handed_back("restart")
        await self._async_start_export()
        self._started = True

    def _set_aside(self, key: str, recipe: Callable[[str | None], BrandControl | None], part: dict[str, Any]) -> None:
        """A stored snapshot whose brand has no recipe in this release (removed or renamed) is taken out of `part`
        and kept among the orphans (`ORPHANED`), stored again at every save: nothing can write it back now, and it
        is never run by another brand's recipe or written away. Said in the log with what to set by hand."""
        state = ExecState.from_dict(part.get("state"))
        if state.brand is None or not state.holds_snapshot or recipe(state.brand) is not None:
            return
        self._orphaned.append(
            {"key": key, **{name: part.pop(name) for name in ("state", "ledger", "monitor") if name in part}}
        )
        values = ", ".join(
            f"{entity_id}={state.restore_values.get(name, state.snapshot.get(name))}"
            for name, entity_id in state.entities.items()
        )
        _LOGGER.warning(
            "SpotNav battery control: this version has no recipe for %s, whose settings SpotNav still holds; they"
            " are kept and handed back by a version that has it. As found, to set by hand: %s", state.brand, values,
        )

    def _take_back(self, key: str, recipe: Callable[[str | None], BrandControl | None], part: dict[str, Any]) -> None:
        """An orphan whose brand has its recipe again goes back into `part` (where nothing is held there), so this
        start hands it back as any stored snapshot."""
        if ExecState.from_dict(part.get("state")).holds_snapshot:
            return
        for orphan in self._orphaned:
            if orphan["key"] == key and recipe(ExecState.from_dict(orphan.get("state")).brand) is not None:
                self._orphaned.remove(orphan)
                part |= {name: orphan[name] for name in ("state", "ledger", "monitor") if name in orphan}
                return

    async def _async_start_export(self) -> None:
        """The export limit's executor from the store; a stored snapshot is handed back first."""
        stored = self._stored.get("export") if isinstance(self._stored.get("export"), dict) else {}
        self.export.load(stored.get("gate"))
        self._take_back("export", export_control_for, stored)
        self._set_aside("export", export_control_for, stored)
        state = ExecState.from_dict(stored.get("state"))
        control = export_control_for(state.brand) or read_export_facts(self.hass, self.site.config)[2]
        if control is None:
            return
        self.export_executor = Executor(
            control, Ledger.from_dict(stored.get("ledger")), Monitor.from_dict(stored.get("monitor")), state=state,
            watchdog_s=self._watchdog_s,
        )
        if state.holds_snapshot:
            await self._async_wait_for_controls(self.export_executor)
        self._log(self.export_executor.restart(dt_util.now()))
        if self.export_executor.state.phase == "restoring":
            await self._async_export_until_handed_back("restart")

    def _controls_ready(self, executor: Executor) -> bool:
        """Whether every stored control's entity is loaded (not the registry's restored placeholder) and the service
        that writes it is registered."""
        for name, entity_id in executor.state.entities.items():
            state = self.hass.states.get(entity_id)
            if state is None or state.attributes.get(ATTR_RESTORED):
                return False
            try:
                role = executor.control.role(name)
            except KeyError:
                continue
            services = (
                [tuple(full.split(".", 1)) for full in role.services.values()] if role.method == "service"
                else [(role.method, service) for service in _METHOD_SERVICES.get(role.method, ())]
            )
            if not all(self.hass.services.has_service(domain, service) for domain, service in services):
                return False
        return True

    async def _async_wait_for_controls(self, executor: Executor) -> None:
        """Before a stored snapshot is restored: wait, up to `STARTUP_WAIT_S`, for its controls to load, so an
        integration that loads after SpotNav does not spend the restore's retries."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + STARTUP_WAIT_S
        while not self._controls_ready(executor):
            if loop.time() >= deadline:
                _LOGGER.info("Battery control: restoring before every control has loaded")
                return
            await asyncio.sleep(STARTUP_LOOK_S)

    async def async_stop(self, reason: str = "stop") -> None:
        """Hand the battery and the export limit back now (control turned off, the entry unloads). Only restores are
        written, whatever the gate says."""
        async with self._lock:
            await self._async_stop(reason)

    async def _async_stop(self, reason: str, *, export: bool = True) -> None:
        """`export` false: the battery alone (its options turned it off while the export limit's own switch still
        runs the limit)."""
        self.trial = None
        self._end_verification(dt_util.now())
        # The battery first (a grid charge adds import; a lower export limit never does), then the export limit.
        if self.executor is not None:
            await self._async_until_handed_back(reason)
        if export and self.export_executor is not None:
            await self._async_export_until_handed_back(reason)

    async def async_shutdown(self) -> None:
        """The site unloads or Home Assistant stops: before the rest of the site, the timer ends, a start still
        waiting is cancelled (its snapshot stays stored for the next start), a look in progress ends, then the
        hand-back runs, bounded. Once only: a second call (the site's own shutdown after Home Assistant's stop handed
        every battery back) returns at once."""
        if self._shut_down:
            return
        self._shut_down = True
        self._closed = True
        if self._timer_cancel is not None:
            self._timer_cancel()
            self._timer_cancel = None
        start = self._start_task
        if start is not None and not start.done():
            start.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await start
        look = self._look_task
        if look is not None and not look.done():
            _done, pending = await asyncio.wait({look}, timeout=LOOK_WAIT_S)
            for task in pending:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        if self._started or self._holds_anything():
            # Also when the start did not finish: an executor it made from the store may hold a snapshot.
            try:
                async with asyncio.timeout(HAND_BACK_DEADLINE_S * 2 + 5.0):
                    await self.async_stop("unload")
            except TimeoutError:
                _LOGGER.warning("SpotNav battery control: the hand-back on unload did not finish; it is finished at the next start")
            except Exception:  # noqa: BLE001 - the rest of the site must still shut down; the state is stored
                _LOGGER.warning("SpotNav battery control: the hand-back on unload failed", exc_info=True)
        if self._loaded:
            # Never before the store was read (a start cancelled early): its snapshot stays for the next start.
            with contextlib.suppress(Exception):
                await self._async_save()

    @staticmethod
    async def async_remove_stored(hass: HomeAssistant, entry_id: str, config: dict[str, Any] | None = None) -> None:
        """The site entry is deleted: a snapshot still stored (its unload's hand-back did not finish) is handed back
        first, on the site's `config`; the store goes only once nothing is held, else it is kept and a warning
        logged (its restore values are not thrown away)."""
        store: Store[dict[str, Any]] = Store(hass, STORE_VERSION, store_key(entry_id))
        try:
            stored = await store.async_load()
        except Exception:  # noqa: BLE001 - a broken store holds nothing that can be handed back
            _LOGGER.debug("async_remove_stored: ignored failure (a broken store holds nothing that can be handed back)", exc_info=True)
            stored = None
        data = dict(stored) if isinstance(stored, dict) else {}
        try:
            held = await async_hand_back_stored(hass, config or {}, data)
        except Exception:  # noqa: BLE001
            _LOGGER.warning("SpotNav battery control: the hand-back of a removed site failed", exc_info=True)
            held = True
        if held:
            _LOGGER.warning(
                "SpotNav battery control: the removed site's home battery could not be handed back; its stored"
                " state (%s) is kept", store_key(entry_id),
            )
            await store.async_save(data)
            return
        await store.async_remove()
