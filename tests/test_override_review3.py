"""Review D: third-round review of the second-round override fixes (f53602b)."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from homeassistant.core import HomeAssistant, ServiceCall

from .test_start_reservation import _overload, _two_charger_site

pytestmark = pytest.mark.usefixtures("offline_relay")


def _patch_site(monkeypatch: pytest.MonkeyPatch, site: Any) -> None:
    monkeypatch.setattr(type(site), "_is_commandable_charger", staticmethod(lambda _controller: True))
    monkeypatch.setattr(
        "custom_components.spotnav.site.site_capacity_controller.applyability_failure", lambda **_kwargs: None
    )


async def test_d1_a_resume_of_one_charger_holds_anothers_overload_write(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R1 variant: the regulator's resume of a charge it paused (`_async_maybe_resume_paused_charge` ->
    `async_battery_probe_start`) is still awaited inline in the pass, under the boundary's and the operation
    lock, through the charger's start command. A's start is a slow cloud call: B's must-lower write later in
    the same pass waits for it."""
    from custom_components.spotnav.execution.controller import ChargingController, IN_EFFECT_OUTCOMES
    from custom_components.spotnav.site import site_capacity_controller as scc

    a, b, site, _, _ = await _two_charger_site(hass, site_a=4.0)
    _patch_site(monkeypatch, site)
    seen: list[str] = []

    async def recording(self, amps: int, **kwargs):  # type: ignore[no-untyped-def]
        seen.append(self.entry_id)
        return next(iter(IN_EFFECT_OUTCOMES))

    monkeypatch.setattr(ChargingController, "_async_assign_current_outcome", recording)

    release = asyncio.Event()
    original = scc.SiteCapacityController._async_maybe_resume_paused_charge  # noqa: SLF001

    async def resume(self, charger_entry_id, charger_controller, *args, **kwargs):  # type: ignore[no-untyped-def]
        if charger_controller is a:
            # Every condition of the resume holds for A (headroom on its phase, dwell over): its start is
            # a slow service call (Easee's start + resume through the cloud).
            await release.wait()
            return True
        return await original(self, charger_entry_id, charger_controller, *args, **kwargs)

    monkeypatch.setattr(scc.SiteCapacityController, "_async_maybe_resume_paused_charge", resume)
    site.regulator_decisions = {a.entry_id: _overload(a.entry_id), b.entry_id: _overload(b.entry_id)}

    apply = hass.async_create_task(site._async_apply_active_control())  # noqa: SLF001
    for _ in range(20):
        await asyncio.sleep(0)
    stalled = b.entry_id not in seen
    release.set()
    await apply
    assert not stalled, "B's overload write waited for A's resume (a slow start command)"


async def test_d2_one_chargers_slow_write_holds_the_next_pass_for_another_charger(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R1 variant: the pass awaits every write task before it ends, and `_schedule_apply_active_control`
    starts no pass while one runs. A's write is a slow cloud call; meanwhile B's overload grows (a new
    recompute asks B to go lower): no pass runs, so B's new must-lower write waits for A's call."""
    from custom_components.spotnav.execution.controller import ChargingController, IN_EFFECT_OUTCOMES
    from custom_components.spotnav.site.regulator import RegulatorDecision

    a, b, site, _, _ = await _two_charger_site(hass, site_a=4.0)
    _patch_site(monkeypatch, site)
    writes: list[tuple[str, int]] = []
    release = asyncio.Event()

    async def slow_for_a(self, amps: int, **kwargs):  # type: ignore[no-untyped-def]
        writes.append((self.entry_id, amps))
        if self is a:
            await release.wait()
        return next(iter(IN_EFFECT_OUTCOMES))

    monkeypatch.setattr(ChargingController, "_async_assign_current_outcome", slow_for_a)
    site.regulator_decisions = {a.entry_id: _overload(a.entry_id), b.entry_id: _overload(b.entry_id)}
    site._schedule_apply_active_control()  # noqa: SLF001
    for _ in range(20):
        await asyncio.sleep(0)
    assert (b.entry_id, 8) in writes

    # The next recompute: B must go down to 6 A at once.
    site.regulator_decisions = {
        b.entry_id: RegulatorDecision(
            proposed_current_a=6.0,
            reason="reducing_current_due_to_active_import_overload",
            limiting_phase=None,
        )
    }
    site._schedule_apply_active_control()  # noqa: SLF001
    for _ in range(20):
        await asyncio.sleep(0)
    stalled = (b.entry_id, 6) not in writes
    release.set()
    await hass.async_block_till_done()
    assert not stalled, "B's new must-lower write waited for A's slow write of the previous pass"


async def test_d3_easee_pause_is_not_sent_under_an_in_flight_regulator_limit_write(hass: HomeAssistant) -> None:
    """R1 regression: the regulator's write no longer takes the operation lock, and a stop takes no
    `_assign_lock`. A dynamic-limit write already on its way (one send, the usual case) when a stop goes
    out is not re-checked: Easee's `pause` (dynamic current 0) is sent while the limit write is in flight,
    and the limit, landing after it, lifts the pause. At 36f071e the stop waited for the write."""
    from .test_charger_controller_paths import _controller
    from .charger_helpers import Clock

    controller = await _controller(hass, "easee", clock=Clock())
    hass.states.async_set("sensor.easee_status", "charging", {"config_authorizationRequired": False})
    events: list[str] = []
    gate = asyncio.Event()

    async def limit(call: ServiceCall) -> None:
        events.append(f"limit {call.data['current']} sent")
        await gate.wait()
        events.append("limit landed")

    async def command(call: ServiceCall) -> None:
        events.append(call.data["action_command"])

    hass.services.async_register("easee", "set_charger_dynamic_limit", limit)
    hass.services.async_register("easee", "action_command", command)

    write = hass.async_create_task(controller.async_apply_regulated_current(10, must_lower=False))
    for _ in range(10):
        await asyncio.sleep(0)
    assert events == ["limit 10 sent"]
    stop = hass.async_create_task(controller.async_stop())
    for _ in range(10):
        await asyncio.sleep(0)
    gate.set()
    await write
    await stop
    assert events.index("pause") > events.index("limit landed"), (
        f"pause sent under an in-flight limit write: {events}"
    )
    await controller.async_shutdown()
