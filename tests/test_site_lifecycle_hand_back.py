"""A site's hand-back of its home battery and heat path runs in full before a reload sets the site up again, before a
removal forgets its stores, and within a bound at Home Assistant's stop.

Home Assistant waits at most ten seconds for an `async_on_unload` task; the hand-back can take far longer. It runs
from `async_unload_entry` (awaited in full), after every timer and part that could act has stopped, and a site's
setup waits for a shutdown of its copy before still in progress (`async_wait_for_shutdown`). Two copies never write
to the plant at once, and the old one never saves over the new one's stores.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant

import custom_components.spotnav as integration
from custom_components.spotnav.battery.control import BatteryControl, store_key
from custom_components.spotnav.heat.control import HeatControl
from custom_components.spotnav.heat.runtime import HeatShadow
from custom_components.spotnav.runtime import controller_for, site_controller_for
from custom_components.spotnav.site.site_capacity_controller import SHUTDOWNS_KEY

from tests.world import setup_charger_and_site

pytestmark = pytest.mark.usefixtures("offline_relay")


class Recorder:
    """What each copy of the site did, in order: `(copy, what)`; a copy is its controller's place in `copies`."""

    def __init__(self) -> None:
        self.events: list[tuple[int, str]] = []
        self.copies: list[Any] = []
        self.release = asyncio.Event()
        self.handing_back = asyncio.Event()

    def copy_of(self, part: Any) -> int:
        site = part.site
        for index, controller in enumerate(self.copies):
            if controller is site:
                return index
        self.copies.append(site)
        return len(self.copies) - 1

    def add(self, part: Any, what: str) -> None:
        self.events.append((self.copy_of(part), what))


def _record(monkeypatch: pytest.MonkeyPatch, recorder: Recorder, hass: HomeAssistant) -> None:
    """A battery hold and a heat lowering that take until the test releases them, each writing to the plant."""
    real_launch = BatteryControl.async_launch
    real_heat_start = HeatControl.async_start
    real_save = BatteryControl._async_save  # noqa: SLF001

    def launch(self: BatteryControl, entry: Any) -> None:
        recorder.add(self, "battery start")
        real_launch(self, entry)

    async def heat_start(self: HeatControl) -> None:
        recorder.add(self, "heat start")
        await real_heat_start(self)

    async def battery_hand_back(self: BatteryControl) -> None:
        controller = self.site
        # Everything else has stopped first: no plan tick, no hot-water boost, no look, no apply pass.
        assert controller._closed and controller._timer_cancel is None  # noqa: SLF001
        assert controller.battery_shadow._timer_cancel is None and controller.battery_shadow.plan._closed  # noqa: SLF001
        assert controller.heat_shadow._closed and controller.heat_shadow._timer_cancel is None  # noqa: SLF001
        assert controller.heat_shadow.hot_water._halted  # noqa: SLF001
        assert controller.switch_load._closed and controller.solar_shadow._closed  # noqa: SLF001
        assert self._halted and self._timer_cancel is None  # noqa: SLF001
        assert controller.heat_control._closed, "the heat control looks no more once the test is handed back"  # noqa: SLF001
        recorder.add(self, "battery write")
        recorder.handing_back.set()
        await recorder.release.wait()
        recorder.add(self, "battery write")
        await self.async_save()
        recorder.add(self, "battery handed back")

    async def heat_hand_back(self: HeatControl) -> None:
        recorder.add(self, "heat write")
        await asyncio.sleep(0)
        recorder.add(self, "heat handed back")

    async def save(self: BatteryControl) -> None:
        recorder.add(self, "battery save")
        await real_save(self)

    monkeypatch.setattr(BatteryControl, "async_launch", launch)
    monkeypatch.setattr(HeatControl, "async_start", heat_start)
    monkeypatch.setattr(BatteryControl, "async_shutdown", battery_hand_back)
    monkeypatch.setattr(HeatControl, "async_shutdown", heat_hand_back)
    monkeypatch.setattr(BatteryControl, "_async_save", save)
    real_unload_platforms = hass.config_entries.async_unload_platforms

    async def unload_platforms(entry: Any, platforms: Any) -> bool:
        if entry.domain == "spotnav" and entry.entry_id.startswith("site_"):
            recorder.events.append((-1, "platforms unloaded"))
        return await real_unload_platforms(entry, platforms)

    monkeypatch.setattr(hass.config_entries, "async_unload_platforms", unload_platforms)


async def _site(hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch) -> tuple[Any, Recorder]:
    recorder = Recorder()
    _record(monkeypatch, recorder, hass)
    _charger, site = await setup_charger_and_site(hass)
    assert site is not None
    controller = site_controller_for(hass, site.entry_id)
    assert controller is not None
    assert recorder.copies == [controller]
    return site, recorder


def _of(recorder: Recorder, copy: int) -> list[str]:
    return [what for index, what in recorder.events if index == copy]


async def test_a_reload_during_a_long_hand_back_waits_for_it_and_the_old_copy_writes_nothing_after(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    site, recorder = await _site(hass, monkeypatch)
    reload = hass.async_create_task(hass.config_entries.async_reload(site.entry_id))
    await recorder.handing_back.wait()
    # Long past what Home Assistant would wait for an unload task: the new copy has not started.
    for _ in range(20):
        await asyncio.sleep(0)
    assert not reload.done()
    assert len(recorder.copies) == 1
    recorder.release.set()
    assert await reload
    await hass.async_block_till_done()

    assert site.state is ConfigEntryState.LOADED
    assert len(recorder.copies) == 2, "a new copy runs"
    # Exactly one hand-back sequence, after the platforms went (a failed platform unload leaves the site running)
    # and before the new copy starts.
    assert _of(recorder, 0) == [
        "battery start", "heat start", "battery write", "battery write", "battery save", "battery handed back",
        "heat write", "heat handed back",
    ]
    first_new = next(i for i, (copy, _what) in enumerate(recorder.events) if copy == 1)
    last_old = max(i for i, (copy, _what) in enumerate(recorder.events) if copy == 0)
    platforms = recorder.events.index((-1, "platforms unloaded"))
    first_old_write = recorder.events.index((0, "battery write"))
    assert platforms < first_old_write and last_old < first_new
    assert not hass.data.get(SHUTDOWNS_KEY)


async def test_the_chargers_keep_their_start_cap_until_the_hand_back_has_ended(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The chargers stay live while the site hands back: a plan's start, a resend or a session's write in that time
    is still capped by the site. The cap goes only once the hand-back has ended."""
    site, recorder = await _site(hass, monkeypatch)
    charger = controller_for(hass, "entry_a")
    assert charger is not None and charger._start_cap is not None  # noqa: SLF001
    unload = hass.async_create_task(hass.config_entries.async_unload(site.entry_id))
    await recorder.handing_back.wait()
    assert charger._start_cap is not None, "the site still caps a start while it hands back"  # noqa: SLF001
    recorder.release.set()
    assert await unload
    assert charger._start_cap is None  # noqa: SLF001


async def test_home_assistant_s_stop_leaves_the_chargers_capped(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """At Home Assistant's stop nothing unloads and the chargers run on to the end: the site's cap stays with them.
    The entry's own unload afterwards lets go."""
    site, recorder = await _site(hass, monkeypatch)
    charger = controller_for(hass, "entry_a")
    assert charger is not None
    recorder.release.set()
    async with asyncio.timeout(5):
        await integration._async_stop(hass)  # noqa: SLF001
    assert charger._start_cap is not None  # noqa: SLF001
    assert await hass.config_entries.async_unload(site.entry_id)
    assert charger._start_cap is None  # noqa: SLF001


async def test_a_battery_hand_back_that_fails_still_ends_the_shutdown_and_lets_the_chargers_go(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A hand-back step that raises is logged and the rest runs on: the shutdown ends, and the chargers' start cap
    goes with it (what the battery still holds stays stored for the next start)."""
    site, recorder = await _site(hass, monkeypatch)
    charger = controller_for(hass, "entry_a")
    assert charger is not None and charger._start_cap is not None  # noqa: SLF001

    async def fails(self: BatteryControl) -> None:
        recorder.add(self, "battery write")
        raise RuntimeError("the plant refused")

    monkeypatch.setattr(BatteryControl, "async_shutdown", fails)
    caplog.set_level(logging.WARNING)
    assert await hass.config_entries.async_unload(site.entry_id)
    assert "handing the battery back failed" in caplog.text
    assert "heat handed back" in _of(recorder, 0), "the heat path is handed back all the same"
    assert charger._start_cap is None  # noqa: SLF001


@pytest.mark.parametrize("at_stop", [False, True])
async def test_a_shutdown_that_fails_lets_the_chargers_go_except_at_home_assistant_s_stop(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, at_stop: bool
) -> None:
    """A shutdown that raises is over all the same: the chargers' start caps must not point at a site that is gone,
    so they go. At Home Assistant's stop (a deadline) the chargers run on to the end and keep the cap."""
    site, recorder = await _site(hass, monkeypatch)
    controller = recorder.copies[0]
    charger = controller_for(hass, "entry_a")
    assert charger is not None and charger._start_cap is not None  # noqa: SLF001
    real_hand_back = type(controller)._async_hand_back  # noqa: SLF001

    async def fails(self: Any, deadline: float | None) -> bool:
        raise RuntimeError("hand-back failed")

    monkeypatch.setattr(type(controller), "_async_hand_back", fails)
    deadline = asyncio.get_running_loop().time() + 5.0 if at_stop else None
    with pytest.raises(RuntimeError):
        await controller.async_shutdown(deadline)
    assert (charger._start_cap is not None) is at_stop  # noqa: SLF001
    # Clean up: a fresh shutdown hands back for real and lets go.
    monkeypatch.setattr(type(controller), "_async_hand_back", real_hand_back)
    controller._shutdown_task = None  # noqa: SLF001
    recorder.release.set()
    assert await hass.config_entries.async_unload(site.entry_id)
    assert charger._start_cap is None  # noqa: SLF001


# The platforms that failed to unload (faked here) keep their timers.
@pytest.mark.parametrize("expected_lingering_timers", [True])
async def test_a_failed_platform_unload_leaves_the_site_running(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Home Assistant sets an entry whose platforms did not unload to FAILED_UNLOAD, which cannot be reloaded: the
    site is then not shut down, so its load balancing and the chargers' start cap stay until Home Assistant
    restarts."""
    site, recorder = await _site(hass, monkeypatch)
    controller = recorder.copies[0]
    charger = controller_for(hass, "entry_a")
    assert charger is not None

    async def fails(entry: Any, platforms: Any) -> bool:
        return False

    monkeypatch.setattr(hass.config_entries, "async_unload_platforms", fails)
    assert not await hass.config_entries.async_unload(site.entry_id)
    assert site.state is ConfigEntryState.FAILED_UNLOAD
    assert controller._shutdown_task is None and not controller._closed  # noqa: SLF001
    assert "battery write" not in _of(recorder, 0)
    assert charger._start_cap is not None  # noqa: SLF001
    recorder.release.set()
    await controller.async_shutdown()


async def test_the_old_copy_never_saves_over_the_new_copy_s_snapshot(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    """The new copy reads the store only after the old one's last save: what the old one handed back (or kept) is
    what the new one finds, and nothing the new one stores is written over afterwards."""
    site, recorder = await _site(hass, monkeypatch)
    old = recorder.copies[0]
    loads: list[tuple[int, Any]] = []
    real_start = BatteryControl.async_start

    async def start(self: BatteryControl) -> None:
        stored = hass_storage.get(store_key(site.entry_id))
        loads.append((recorder.copy_of(self), None if stored is None else stored.get("data")))
        await real_start(self)

    monkeypatch.setattr(BatteryControl, "async_start", start)
    # The old copy holds a snapshot it stores as it hands back.
    old.battery_control._loaded = True  # noqa: SLF001
    old.battery_control.reports = {"old": {"kept": True}}
    recorder.release.set()
    assert await hass.config_entries.async_reload(site.entry_id)
    await hass.async_block_till_done()
    new = site_controller_for(hass, site.entry_id)
    assert new is not old
    assert loads and loads[-1][0] == 1
    assert loads[-1][1]["reports"] == {"old": {"kept": True}}, "the new copy read what the old one saved last"
    saved = hass_storage[store_key(site.entry_id)]["data"]
    # Nothing of the old copy after the new one's start.
    first_new = next(i for i, (copy, _what) in enumerate(recorder.events) if copy == 1)
    assert all(copy != 0 for copy, _what in recorder.events[first_new:])
    assert hass_storage[store_key(site.entry_id)]["data"] == saved


async def test_a_setup_waits_for_a_shutdown_of_the_site_still_in_progress(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A copy whose shutdown was started elsewhere (a setup that failed, Home Assistant's stop) and still runs: the
    next setup waits for it, and is retried later when it does not end in time."""
    site, recorder = await _site(hass, monkeypatch)
    old = recorder.copies[0]
    shutdown = hass.async_create_task(old.async_shutdown())
    await recorder.handing_back.wait()
    monkeypatch.setattr(integration, "SITE_SHUTDOWN_WAIT_S", 0.05)
    # The unload awaits the same run (not a second one) while the test holds it; the setup after it waits.
    unload = hass.async_create_task(hass.config_entries.async_unload(site.entry_id))
    for _ in range(20):
        await asyncio.sleep(0)
    assert not unload.done()
    recorder.release.set()
    assert await unload
    await shutdown
    assert _of(recorder, 0).count("battery handed back") == 1

    # A run still in progress at the next setup: it waits, then is retried later.
    blocker = asyncio.Event()
    pending = hass.async_create_task(blocker.wait())
    hass.data.setdefault(SHUTDOWNS_KEY, {})[site.entry_id] = pending
    assert not await hass.config_entries.async_setup(site.entry_id)
    assert site.state is ConfigEntryState.SETUP_RETRY
    assert len(recorder.copies) == 1, "no new copy started beside the old one"
    blocker.set()
    await pending
    del hass.data[SHUTDOWNS_KEY][site.entry_id]
    assert await hass.config_entries.async_unload(site.entry_id)
    assert await hass.config_entries.async_setup(site.entry_id)
    await hass.async_block_till_done()
    assert site.state is ConfigEntryState.LOADED


async def test_removing_the_site_forgets_its_stores_only_after_the_hand_back(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    site, recorder = await _site(hass, monkeypatch)
    real_battery_remove = BatteryControl.async_remove_stored
    real_heat_remove = HeatControl.async_remove_stored

    async def battery_remove(hass: HomeAssistant, entry_id: str, config: Any = None) -> None:
        recorder.events.append((-1, "battery store removed"))
        await real_battery_remove(hass, entry_id, config)

    async def heat_remove(hass: HomeAssistant, entry_id: str) -> None:
        recorder.events.append((-1, "heat store removed"))
        await real_heat_remove(hass, entry_id)

    monkeypatch.setattr(BatteryControl, "async_remove_stored", staticmethod(battery_remove))
    monkeypatch.setattr(HeatControl, "async_remove_stored", staticmethod(heat_remove))
    removal = hass.async_create_task(hass.config_entries.async_remove(site.entry_id))
    await recorder.handing_back.wait()
    for _ in range(20):
        await asyncio.sleep(0)
    assert (-1, "battery store removed") not in recorder.events
    recorder.release.set()
    await removal
    await hass.async_block_till_done()
    handed_back = recorder.events.index((0, "heat handed back"))
    assert recorder.events.index((-1, "battery store removed")) > handed_back
    assert recorder.events.index((-1, "heat store removed")) > handed_back
    assert _of(recorder, 0).count("battery handed back") == 1


async def test_home_assistant_s_stop_bounds_the_hand_back_and_leaves_the_rest_for_the_next_start(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    site, recorder = await _site(hass, monkeypatch)
    controller = recorder.copies[0]
    saves: list[str] = []

    async def battery_save(self: BatteryControl) -> None:
        saves.append("battery")

    async def heat_save(self: HeatControl) -> None:
        saves.append("heat")

    monkeypatch.setattr(BatteryControl, "async_save", battery_save)
    monkeypatch.setattr(HeatControl, "async_save", heat_save)
    monkeypatch.setattr(integration, "SITE_STOP_HAND_BACK_S", 0.05)
    caplog.set_level(logging.WARNING)
    loop = asyncio.get_running_loop()
    started = loop.time()
    async with asyncio.timeout(5):
        await integration._async_stop(hass)  # noqa: SLF001
    assert loop.time() - started < 2
    # The battery never answered: cut at the deadline, the heat path not reached; both kept as they stand.
    assert "battery handed back" not in _of(recorder, 0) and "heat write" not in _of(recorder, 0)
    assert saves == ["battery", "heat"]
    assert "left for the next start" in caplog.text
    assert controller._shutdown_task.done()  # noqa: SLF001
    # The entry's own unload later finds the shutdown done.
    assert await hass.config_entries.async_unload(site.entry_id)
    assert _of(recorder, 0).count("battery write") == 1


async def test_a_water_heater_or_a_zone_that_does_not_answer_never_delays_the_heat_relay_s_opening(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The heat relay's opening, the water heater's on and the zones' setting back run side by side: at Home
    Assistant's stop a switch and a thermostat that never answer use up the deadline, and the relay is opened all the
    same; what they still hold is kept for the next start."""
    from custom_components.spotnav.heat.water_heater_control import WaterHeaterControl
    from custom_components.spotnav.heat.zones_control import ZonesControl

    _site_, recorder = await _site(hass, monkeypatch)
    controller = recorder.copies[0]
    recorder.release.set()
    saves: list[str] = []
    hung: list[str] = []

    async def heat_save(self: HeatControl) -> None:
        saves.append("heat")

    async def never(self: Any) -> None:
        hung.append(type(self).__name__)
        await asyncio.Event().wait()

    monkeypatch.setattr(HeatControl, "async_save", heat_save)
    monkeypatch.setattr(WaterHeaterControl, "async_shutdown", never)
    monkeypatch.setattr(ZonesControl, "async_shutdown", never)
    caplog.set_level(logging.WARNING)
    loop = asyncio.get_running_loop()
    async with asyncio.timeout(5):
        await controller.async_shutdown(loop.time() + 0.2)
    assert sorted(hung) == ["WaterHeaterControl", "ZonesControl"]
    assert _of(recorder, 0).count("heat handed back") == 1
    assert "left for the next start" in caplog.text and saves == ["heat"]


async def test_the_heat_control_s_store_is_saved_again_after_the_water_heater_and_the_zones_are_handed_back(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    from custom_components.spotnav.heat.water_heater_control import WaterHeaterControl
    from custom_components.spotnav.heat.zones_control import ZonesControl

    _site_, recorder = await _site(hass, monkeypatch)
    controller = recorder.copies[0]
    recorder.release.set()
    order: list[str] = []

    async def heat_save(self: HeatControl) -> None:
        order.append("heat saved")

    async def slow(self: Any) -> None:
        await asyncio.sleep(0.05)
        order.append(f"{type(self).__name__} handed back")

    monkeypatch.setattr(HeatControl, "async_save", heat_save)
    monkeypatch.setattr(WaterHeaterControl, "async_shutdown", slow)
    monkeypatch.setattr(ZonesControl, "async_shutdown", slow)
    await controller.async_shutdown()
    assert sorted(order[:2]) == ["WaterHeaterControl handed back", "ZonesControl handed back"] and order[2:] == ["heat saved"]
    assert _of(recorder, 0).count("heat handed back") == 1


async def test_home_assistant_s_stop_returns_within_its_total_bound_even_when_a_site_hangs(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    site, recorder = await _site(hass, monkeypatch)
    controller = recorder.copies[0]
    recorder.release.set()
    hang = asyncio.Event()

    async def hanging(self: HeatShadow) -> None:
        await hang.wait()

    monkeypatch.setattr(HeatShadow, "async_shutdown", hanging)
    monkeypatch.setattr(integration, "SITE_STOP_TOTAL_S", 0.2)
    flushed: list[str] = []
    store = getattr(integration.domain_data(hass), "session_store", None)
    if store is not None:
        real_flush = store.async_flush

        async def flush() -> None:
            flushed.append("sessions")
            await real_flush()

        monkeypatch.setattr(store, "async_flush", flush)
    caplog.set_level(logging.WARNING)
    async with asyncio.timeout(5):
        await integration._async_stop(hass)  # noqa: SLF001
    assert "did not finish within" in caplog.text
    if store is not None:
        assert flushed == ["sessions", "sessions"], "saved before the sites and again at the end"
    hang.set()
    await controller._shutdown_task  # noqa: SLF001
    assert await hass.config_entries.async_unload(site.entry_id)


async def test_a_battery_control_shut_down_before_its_store_was_read_writes_nothing(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """A start cancelled before it read the store (a reload right after setup) must not save an empty state over
    a snapshot the store still holds for the next start."""
    _charger, site = await setup_charger_and_site(hass)
    assert site is not None
    controller = site_controller_for(hass, site.entry_id)
    assert controller is not None
    key = store_key(site.entry_id)
    snapshot = {"version": 1, "minor_version": 1, "key": key, "data": {"state": {"phase": "holding", "snapshot": {"x": 1}}}}
    hass_storage[key] = snapshot
    control = BatteryControl(hass, controller)
    await control.async_shutdown()
    await control.async_save()
    assert hass_storage[key] == snapshot


_HELD = {"phase": "active", "brand": "sigenergy", "action": "grid_charge",
         "snapshot": {"mode": "x"}, "entities": {"mode": "select.not_loaded_yet"}}
_EXPORT_HELD = {"phase": "active", "brand": "sigenergy", "action": "curtail_export",
                "snapshot": {"export_limit": "10"}, "entities": {"export_limit": "number.not_loaded_yet"}}


def _held_store(key: str) -> dict[str, Any]:
    return {"version": 1, "minor_version": 1, "key": key,
            "data": {"state": dict(_HELD), "export": {"state": dict(_EXPORT_HELD)}}}


async def test_a_shutdown_while_the_battery_restarts_keeps_the_export_limit_s_snapshot(
    hass: HomeAssistant, hass_storage: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The start has made the battery's executor but not yet the export limit's (it waits for the plant or hands
    the battery back): a shutdown then keeps the export limit's stored snapshot for the next start."""
    _charger, site = await setup_charger_and_site(hass)
    controller = site_controller_for(hass, site.entry_id)
    assert controller is not None
    key = store_key(site.entry_id)
    hass_storage[key] = _held_store(key)
    control = BatteryControl(hass, controller)

    async def not_reachable(self: BatteryControl, reason: str) -> None:  # the plant cannot be written yet
        return None

    monkeypatch.setattr(BatteryControl, "_async_until_handed_back", not_reachable)
    control.async_launch(None)
    for _ in range(20):
        await asyncio.sleep(0)
    assert control._loaded and control.executor is not None and control.export_executor is None  # noqa: SLF001
    await control.async_shutdown()
    data = hass_storage[key]["data"]
    assert data["export"]["state"]["snapshot"] == {"export_limit": "10"}
    assert data["state"]["snapshot"] == {"mode": "x"}


async def test_a_shutdown_after_a_failed_start_keeps_the_battery_s_snapshot(
    hass: HomeAssistant, hass_storage: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The start raised after it read the store and waits to retry: a shutdown then keeps the battery's and the
    export limit's stored snapshots for the next start."""
    _charger, site = await setup_charger_and_site(hass)
    controller = site_controller_for(hass, site.entry_id)
    assert controller is not None
    key = store_key(site.entry_id)
    hass_storage[key] = _held_store(key)
    control = BatteryControl(hass, controller)

    def unreadable(self: BatteryControl) -> Any:
        raise RuntimeError("facts not readable")

    monkeypatch.setattr(BatteryControl, "_read_facts", unreadable)
    control.async_launch(None)
    for _ in range(20):
        await asyncio.sleep(0)
    assert control._loaded and control.executor is None  # noqa: SLF001
    await control.async_shutdown()
    data = hass_storage[key]["data"]
    assert data["state"]["snapshot"] == {"mode": "x"}
    assert data["export"]["state"]["snapshot"] == {"export_limit": "10"}
