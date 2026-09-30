"""Recording doubles and whole-stack sessions: one charger (or one price stack) built from the real classes."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from typing import Any, Callable

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import async_mock_service

from custom_components.spotnav.planning.auto_controller import AutoPlannerController, LiveVehicleFacts
from custom_components.spotnav.execution.auto_execution import AutoExecutor
from custom_components.spotnav.planning.auto_settings import (
    async_setup_auto_settings,
    AutoSettings,
    AutoSettingsStore,
    DEFAULT_DEPARTURE,
)
from custom_components.spotnav.const import CONF_CHARGE_CONTROL, CONF_CURRENT_LIMIT, CONF_WEBHOOK_ID
from custom_components.spotnav.execution.controller import ChargingController
from custom_components.spotnav.pricing.price_refresh import async_setup_price_refresh, PriceRefreshManager
from custom_components.spotnav.pricing.price_repository import PriceRepository

from custom_components.spotnav.runtime import ChargerData

from .relay import BASE_URL, Clock, FakeScheduler, SE4, StoreDouble, StubTransport
from .world import ENTRY


async def start_executor(
    hass: HomeAssistant, controller: ChargingController, store: AutoSettingsStore, **seams: Any
) -> AutoExecutor:
    """One started authority boundary over a controller, without a config entry."""
    executor = AutoExecutor(hass, controller, store, **seams)
    await executor.async_start()
    return executor


async def start_preview(
    hass: HomeAssistant,
    entry_id: str,
    store: AutoSettingsStore,
    manager: PriceRefreshManager,
    **kwargs: Any,
) -> AutoPlannerController:
    """One started Auto preview controller, without a config entry."""
    preview = AutoPlannerController(hass, entry_id, store, manager, **kwargs)
    await preview.async_start()
    return preview


class Session:
    """One charger's whole stack: prices, settings, preview, boundary and controller.

    Built from the real classes, with three seams replaced: the wire (a stub transport), the
    clock, and the charging controller's own window timers (recorded, not waited for). The
    repository, the manager and the settings store are domain-level objects, exactly as
    production has them -- a second `Session` on the same `hass` therefore shares them, which
    is what makes the sharing and isolation tests real rather than simulated.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        transport: StubTransport,
        clock: Clock,
        timers: FakeScheduler,
        *,
        entry_id: str = ENTRY,
        charge_control: str = "switch.charger_a",
        current_limit: str | None = None,
    ) -> None:
        self.hass = hass
        self.transport = transport
        self.clock = clock
        self.timers = timers
        self.entry_id = entry_id
        self.charge_control = charge_control
        self.current_limit = current_limit
        self.repository: PriceRepository | None = None
        self.scheduler = FakeScheduler()
        self.manager: PriceRefreshManager | None = None
        self.store: AutoSettingsStore | None = None
        self.controller: ChargingController | None = None
        self.executor: AutoExecutor | None = None
        self.preview: AutoPlannerController | None = None
        self.on_calls: list[Any] = []
        self.off_calls: list[Any] = []
        self.vehicle_reader: Callable[[str], LiveVehicleFacts | None] | None = None

    async def start(self) -> None:
        self.hass.states.async_set(self.charge_control, "off")
        self.on_calls = async_mock_service(self.hass, "switch", "turn_on")
        self.off_calls = async_mock_service(self.hass, "switch", "turn_off")
        self.repository = PriceRepository(
            self.hass,
            base_url="https://relay.test",
            session=self.transport,
            now=self.clock,
            store=StoreDouble(),
        )
        self.manager = await async_setup_price_refresh(
            self.hass,
            self.repository,
            now=self.clock,
            jitter=lambda: 0.5,
            scheduler=self.scheduler,
        )
        self.store = await async_setup_auto_settings(self.hass)
        self.controller = ChargingController(
            self.hass,
            self.entry_id,
            {
                CONF_CHARGE_CONTROL: self.charge_control,
                CONF_CURRENT_LIMIT: self.current_limit or "",
                CONF_WEBHOOK_ID: "webhook-a",
            },
        )
        await self.controller.async_initialize()
        self.executor = await start_executor(
            self.hass, self.controller, self.store, now=self.clock
        )
        self.preview = await start_preview(
            self.hass,
            self.entry_id,
            self.store,
            self.manager,
            executor=self.executor,
            vehicle_reader=self.vehicle_reader,
            now=self.clock,
        )
        self.executor.attach_preview(self.preview)

    async def set_auto(self, **changes: Any) -> AutoPlannerController:
        """Write Auto settings and reconcile, through the preview's public path."""
        assert self.preview is not None
        changes.setdefault("area_id", SE4)
        changes.setdefault("amps", 10)
        changes.setdefault("phases", 1)
        # Asking for a departure is asking for the departure switch: the two are one
        # setting in every practical sense, and a test that passed one without the other
        # would silently be testing the no-departure horizon instead.
        changes.setdefault("departure_enabled", "departure" in changes)
        return await self.preview.async_apply_settings(
            mutate=lambda settings: replace(settings, **changes)
        )

    async def tick(self) -> None:
        """Fire the price timers, then let every callback finish."""
        self.scheduler.fire_all()
        await self.hass.async_block_till_done()

    async def boundary(self) -> None:
        """Fire the charging controller's own window timers -- the boundary, explicitly."""
        self.timers.fire_all()
        await self.hass.async_block_till_done()

    def settings(self) -> Any:
        assert self.store is not None
        return self.store.settings(self.entry_id)


class Harness:
    """One repository, one refresh manager, one settings store, and one clock.

    Deliberately *not* a config entry: the states this module pins are about calculation,
    and the entry-level ones (unload, reload, deletion, the webhook) set up real entries
    through Home Assistant itself, below.
    """

    def __init__(self, hass: HomeAssistant, transport: StubTransport, clock: Clock) -> None:
        self.hass = hass
        self.transport = transport
        self.clock = clock
        self.repository = PriceRepository(
            hass, base_url=BASE_URL, session=transport, now=clock, store=StoreDouble()
        )
        self.scheduler = FakeScheduler()
        self.manager: PriceRefreshManager | None = None
        self.store: AutoSettingsStore | None = None
        self.vehicle_reader: Callable[[str], LiveVehicleFacts | None] | None = None
        self.previews: dict[str, AutoPlannerController] = {}

    async def start(self) -> None:
        self.manager = await async_setup_price_refresh(
            self.hass,
            self.repository,
            now=self.clock,
            jitter=lambda: 0.5,
            scheduler=self.scheduler,
        )
        self.store = await async_setup_auto_settings(self.hass)

    async def write(self, entry_id: str, **changes: Any) -> AutoSettings:
        """Store settings for a charger without involving a controller."""
        assert self.store is not None
        return await self.store.async_update(
            entry_id, mutate=lambda settings: replace(settings, **changes)
        )

    async def auto(self, entry_id: str = "entry-a", **changes: Any) -> AutoPlannerController:
        """Store Auto settings for one charger and start its preview controller.

        The departure is off unless a test asks for one: the fixture clock sits at exactly
        the visible default departure time (08:00 Stockholm), so leaving it on would make
        every scenario below test the deadline rule instead of the one it names.
        """
        changes.setdefault("area_id", SE4)
        changes.setdefault("amps", 10)
        changes.setdefault("phases", 1)
        changes.setdefault("departure_enabled", "departure" in changes)
        changes.setdefault("departure", DEFAULT_DEPARTURE)
        await self.write(entry_id, **changes)
        assert self.store is not None and self.manager is not None
        return await self._start_preview(entry_id)

    async def _start_preview(self, entry_id: str) -> AutoPlannerController:
        """The entry's preview controller: started once, then the same object."""
        assert self.store is not None and self.manager is not None
        if entry_id not in self.previews:
            self.previews[entry_id] = await start_preview(
                self.hass,
                entry_id,
                self.store,
                self.manager,
                now=self.clock,
                vehicle_reader=self.vehicle_reader,
            )
            # A config entry registered under this id (diagnostics reads the preview from it) is
            # given the preview the way a real setup would give it.
            entry = self.hass.config_entries.async_get_entry(entry_id)
            if entry is not None:
                entry.runtime_data = ChargerData(
                    controller=None, soc_reader=None, preview=self.previews[entry_id]
                )
        return self.previews[entry_id]

    async def preview(self, entry_id: str = "entry-a") -> AutoPlannerController:
        """Start a preview controller from whatever settings happen to be stored."""
        assert self.store is not None and self.manager is not None
        return await self._start_preview(entry_id)

    async def tick(self) -> None:
        """Fire every timer the manager holds, then let every callback finish."""
        self.scheduler.fire_all()
        await self.hass.async_block_till_done()

    @property
    def area(self):
        assert self.manager is not None
        return self.manager.area_snapshot(SE4)


class Recording:
    """A listener that records what it was told."""

    def __init__(self, *, raises: bool = False) -> None:
        self.states: list[str] = []
        self.reasons: list[str] = []
        self.snapshots: list[Any] = []
        self.raises = raises

    def __call__(self, snapshot: Any) -> None:
        self.snapshots.append(snapshot)
        self.states.append(snapshot.state)
        self.reasons.append(snapshot.reason)
        if self.raises:
            raise RuntimeError("this listener is broken")


def assert_nothing_executed(spies: dict[str, Any]) -> None:
    """Nothing reached a charger, no service was called and no timer was armed."""
    assert spies["called"] == []
    for (domain, service), calls in spies["services"].items():
        assert calls == [], f"{domain}.{service} was called"
    assert spies["timers"] == []


class RecordedAppointments:
    """The one point-in-time scheduler this module uses, recorded rather than waited for."""

    def __init__(self) -> None:
        self.armed: list[dict[str, Any]] = []
        self.cancelled = 0

    def __call__(self, hass: HomeAssistant, action: Any, when: datetime) -> Any:
        entry = {"action": action, "when": when}
        self.armed.append(entry)

        def cancel() -> None:
            self.cancelled += 1
            if entry in self.armed:
                self.armed.remove(entry)

        return cancel

    async def fire(self, when: datetime | None = None) -> None:
        """Fire the one pending appointment, as the platform would.

        Firing removes the entry: a callback that has run is not waiting any more, which is exactly
        what the production code assumes when it clears its own handle at the top of the callback.
        """
        entry = self.armed.pop() if self.armed else None
        if entry is not None:
            # A point-in-time callback may be synchronous as well as async: the pause expiry
            # settles a stored intent, while the manual-Start acknowledgement bound only drops an
            # in-memory fact. Both are fired the same way here.
            result = entry["action"](entry["when"] if when is None else when)
            if result is not None:
                await result

    @property
    def when(self) -> datetime | None:
        return None if not self.armed else self.armed[-1]["when"]


class FlakyStore:
    """A store double whose next write can be made to fail, deterministically.

    The real `Store` writes to disk, and a disk that says no is the one failure the store's
    transaction has to survive: after it, memory must still describe the last document that
    was actually written.
    """

    def __init__(self, payload: Any = None) -> None:
        self.payload = payload
        self.loads = 0
        self.saves = 0
        self.fail_next = False
        self.fail_all = False

    async def async_load(self) -> Any:
        self.loads += 1
        return self.payload

    async def async_save(self, data: Any) -> None:
        if self.fail_all:
            raise RuntimeError("the disk said no")
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("the disk said no")
        self.saves += 1
        self.payload = data

    async def async_remove(self) -> None:
        self.payload = None
