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
