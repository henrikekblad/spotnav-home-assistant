"""Review D, beyond the reviewer's own tests: a newer decision for a charger whose write is on its way, and
what shutdown and turning active control off do to the operations in flight."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from homeassistant.core import HomeAssistant

from .test_override_review3 import _patch_site
from .test_start_reservation import _overload, _two_charger_site

pytestmark = pytest.mark.usefixtures("offline_relay")


def _lower(amps: float) -> Any:
    from custom_components.spotnav.site.regulator import RegulatorDecision

    return RegulatorDecision(
        proposed_current_a=amps, reason="reducing_current_due_to_active_import_overload", limiting_phase=None
    )


async def _spin(rounds: int = 20) -> None:
    for _ in range(rounds):
        await asyncio.sleep(0)


async def test_a_lower_current_decided_while_the_chargers_write_is_on_its_way_goes_out_when_it_returns(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D2: A's write of 8 A is a slow cloud call; meanwhile the regulator wants A at 6 A. That pass skips A (one
    operation per charger), and the 6 A goes out as soon as the 8 A write returns, with no new recompute."""
    from custom_components.spotnav.execution.controller import ChargingController, IN_EFFECT_OUTCOMES

    a, b, site, _, _ = await _two_charger_site(hass, site_a=4.0)
    _patch_site(monkeypatch, site)
    writes: list[tuple[str, int]] = []
    release = asyncio.Event()

    async def slow_for_a(self, amps: int, **kwargs):  # type: ignore[no-untyped-def]
        writes.append((self.entry_id, amps))
        if self is a and amps == 8:
            await release.wait()
        return next(iter(IN_EFFECT_OUTCOMES))

    monkeypatch.setattr(ChargingController, "_async_assign_current_outcome", slow_for_a)
    site.regulator_decisions = {a.entry_id: _overload(a.entry_id)}
    site._schedule_apply_active_control()  # noqa: SLF001
    await _spin()
    assert writes == [(a.entry_id, 8)]

    site.regulator_decisions = {a.entry_id: _lower(6.0)}
    site._schedule_apply_active_control()  # noqa: SLF001
    await _spin()
    assert writes == [(a.entry_id, 8)], "a second command to A while its first is on its way"

    release.set()
    await hass.async_block_till_done()
    assert writes == [(a.entry_id, 8), (a.entry_id, 6)]


async def test_shutdown_cancels_a_chargers_operation_on_its_way_and_nothing_is_written_after(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cancellation: a charger's step held up before its write (a slow resume) is cancelled by the site's
    shutdown and waited for; the write it was heading to never reaches the charger."""
    from custom_components.spotnav.execution.controller import ChargingController, IN_EFFECT_OUTCOMES
    from custom_components.spotnav.site import site_capacity_controller as scc

    a, b, site, _, _ = await _two_charger_site(hass, site_a=4.0)
    _patch_site(monkeypatch, site)
    writes: list[str] = []
    cancelled: list[str] = []

    async def recording(self, amps: int, **kwargs):  # type: ignore[no-untyped-def]
        writes.append(self.entry_id)
        return next(iter(IN_EFFECT_OUTCOMES))

    async def resume(self, charger_entry_id, *args, **kwargs):  # type: ignore[no-untyped-def]
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.append(charger_entry_id)
            raise
        return False

    monkeypatch.setattr(ChargingController, "_async_assign_current_outcome", recording)
    monkeypatch.setattr(scc.SiteCapacityController, "_async_maybe_resume_paused_charge", resume)
    site.regulator_decisions = {a.entry_id: _overload(a.entry_id), b.entry_id: _overload(b.entry_id)}
    apply = hass.async_create_task(site._async_apply_active_control())  # noqa: SLF001
    await _spin()

    await site.async_shutdown()
    assert sorted(cancelled) == sorted([a.entry_id, b.entry_id])
    assert apply.done()
    assert not site._charger_ops  # noqa: SLF001
    await hass.async_block_till_done()
    assert writes == []


async def test_turning_active_control_off_retires_every_chargers_operation_before_the_restore(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cancellation: a charger's write on its way when active control is turned off is cancelled and waited
    for, so it cannot land after the restore; a pass that only spawned it is gone too."""
    from custom_components.spotnav.execution.controller import ChargingController, IN_EFFECT_OUTCOMES

    a, b, site, _, _ = await _two_charger_site(hass, site_a=4.0)
    _patch_site(monkeypatch, site)
    events: list[str] = []

    async def slow(self, amps: int, **kwargs):  # type: ignore[no-untyped-def]
        events.append(f"{self.entry_id} write")
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            events.append(f"{self.entry_id} cancelled")
            raise
        return next(iter(IN_EFFECT_OUTCOMES))

    async def restore(self, *, lowered_by_balancing: bool):  # type: ignore[no-untyped-def]
        events.append(f"{self.entry_id} restore")
        from custom_components.spotnav.execution.controller import CurrentRestore, RESTORE_NOT_NEEDED

        return CurrentRestore(RESTORE_NOT_NEEDED, None, None, None)

    monkeypatch.setattr(ChargingController, "_async_assign_current_outcome", slow)
    monkeypatch.setattr(ChargingController, "async_restore_current", restore)
    site.regulator_decisions = {a.entry_id: _overload(a.entry_id)}
    site._schedule_apply_active_control()  # noqa: SLF001
    await _spin()
    assert events == [f"{a.entry_id} write"]

    async with site.transition_lock:
        await site.async_disable_active_control()
    assert events.index(f"{a.entry_id} cancelled") < events.index(f"{a.entry_id} restore")
    assert not site._charger_ops  # noqa: SLF001
    assert not site._apply_passes  # noqa: SLF001


async def test_a_charger_that_has_shut_down_takes_no_regulator_write_stop_or_resume(hass: HomeAssistant) -> None:
    """Cancellation: a regulator step that still holds a charger whose entry has shut down (unloaded while the
    step waited) sends it nothing: no current, no safety stop for a refused must-lower write, no resume."""
    from .charger_helpers import Clock
    from .test_charger_controller_paths import _controller

    controller = await _controller(hass, "easee", clock=Clock())
    hass.states.async_set("sensor.easee_status", "charging", {"config_authorizationRequired": False})
    calls: list[str] = []

    async def record(call: Any) -> None:
        calls.append(call.service)

    hass.services.async_register("easee", "set_charger_dynamic_limit", record)
    hass.services.async_register("easee", "action_command", record)
    await controller.async_shutdown()

    write = await controller.async_apply_regulated_current(10, must_lower=True)
    paused = await controller.async_apply_regulated_current(0, must_lower=True)
    resumed = await controller.async_battery_probe_start(8)
    await hass.async_block_till_done()
    assert calls == []
    assert not write.written and not paused.written
    assert resumed is False


async def test_a_start_reads_the_sites_allowance_only_after_a_lowering_write_on_its_way_has_landed(
    hass: HomeAssistant,
) -> None:
    """Raising after lowering: the regulator's must-lower write is on its way when a Start comes. The Start's
    allowance is read after that write has landed (under `_assign_lock`), not before, so the current the
    Start writes is never one decided before the lowering and sent after it."""
    from .charger_helpers import Clock
    from .test_charger_controller_paths import _controller

    controller = await _controller(hass, "easee", clock=Clock())
    hass.states.async_set("sensor.easee_status", "charging", {"config_authorizationRequired": False})
    events: list[str] = []
    gate = asyncio.Event()

    async def limit(call: Any) -> None:
        events.append(f"limit {call.data['current']} sent")
        await gate.wait()
        events.append("limit landed")

    async def command(call: Any) -> None:
        events.append(call.data["action_command"])

    hass.services.async_register("easee", "set_charger_dynamic_limit", limit)
    hass.services.async_register("easee", "action_command", command)

    def cap() -> float:
        events.append("allowance read")
        return 10.0

    controller.set_start_cap(cap, reserve=lambda _amps: None)
    write = hass.async_create_task(controller.async_apply_regulated_current(8, must_lower=True))
    await _spin(10)
    assert events == ["limit 8 sent"]
    start = hass.async_create_task(controller.async_start(16, manual=True))
    await _spin(10)
    gate.set()
    await write
    await start
    assert events.index("allowance read") > events.index("limit landed"), events
    await controller.async_shutdown()


async def test_a_persons_charge_stopped_for_safety_waits_five_minutes_from_the_last_safety_stop(
    hass: HomeAssistant, timers: Any, freezer: Any
) -> None:
    """P3: the five minutes are counted from the last safety stop, not from the last resume. Resumed five
    minutes after the first stop and stopped again five and a half minutes later, the charge is not resumed
    at once (five minutes after the resume), only five minutes after that second stop."""
    from datetime import timedelta

    from .pause_world import pause_world

    world = await pause_world(hass, timers)
    await world.executor.async_manual_start(10)
    controller = world.controller
    await controller._regulated_stop("safety_stop")  # noqa: SLF001
    await hass.async_block_till_done()
    assert not await controller.async_battery_probe_start(8), "resumed at once after a safety stop"
    freezer.tick(timedelta(minutes=5, seconds=1))
    assert await controller.async_battery_probe_start(8)
    await hass.async_block_till_done()

    freezer.tick(timedelta(minutes=5, seconds=30))
    await controller._regulated_stop("safety_stop")  # noqa: SLF001
    await hass.async_block_till_done()
    assert not await controller.async_battery_probe_start(8), "resumed within five minutes of the safety stop"
    freezer.tick(timedelta(minutes=5, seconds=1))
    assert await controller.async_battery_probe_start(8)
    await world.shutdown()


async def test_an_unplug_ends_a_safety_stops_hold_so_the_next_plug_in_is_not_held_by_it(
    hass: HomeAssistant, timers: Any
) -> None:
    """P3: a safety stop belongs to the plug-in it was made in. After an unplug and a new plug-in, a person's
    charge held back by the site's allowance is resumed when there is room, not held for the old stop."""
    from .pause_world import pause_world

    world = await pause_world(hass, timers)
    await world.executor.async_manual_start(10)
    controller = world.controller
    await controller._regulated_stop("safety_stop")  # noqa: SLF001
    await hass.async_block_till_done()
    await world.plug.set(False)
    await world.plug.set(True)
    await hass.async_block_till_done()

    room = {"a": 4.0}
    controller.set_start_cap(lambda: room["a"], reserve=lambda _amps: None)
    assert await controller.async_start(10, manual=True) is False
    assert controller.paused_by_balancing
    room["a"] = 16.0
    assert await controller.async_battery_probe_start(8), "held for the safety stop of the plug-in before"
    await world.shutdown()
