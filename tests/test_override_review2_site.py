"""Review C: the start reservation for a charger whose own current cannot be read."""

from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant

from .test_start_reservation import _two_charger_site
from .world import PHASES, set_site_current_a

pytestmark = pytest.mark.usefixtures("offline_relay")


async def test_c_unread_reservation_is_not_eaten_by_another_load(hass: HomeAssistant) -> None:
    """A starts at 10 A on a 20 A margin; its own current cannot be read and it has not drawn yet. The
    house turns on an 8 A load (a kettle): the margin falls to 12 A. That fall is counted as A's draw, so A
    holds only 2 A and B is given 10 A: when A draws its 10 A the site is 8 A over its fuse."""
    a, b, site, _, _ = await _two_charger_site(hass, site_a=0.0)  # 20 A of margin
    clock = {"now": 1000.0}
    site._yield_now = lambda: clock["now"]  # noqa: SLF001
    assert await a.async_start(10) is True
    for p in PHASES:
        hass.states.async_set(f"sensor.ca_{p.lower()}", "unavailable")
    await hass.async_block_till_done()
    assert site.start_allowance_a(b.entry_id) == pytest.approx(10.0)

    set_site_current_a(hass, "pair_site", 8.0)  # an unrelated 8 A load, A still draws nothing
    await hass.async_block_till_done()
    clock["now"] += 5

    allowance = site.start_allowance_a(b.entry_id)
    assert allowance == pytest.approx(2.0), f"B may take {allowance} A while A's 10 A are still on their way"


async def test_c_one_chargers_held_lock_stalls_anothers_overload_write(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Charger A's operation lock is held by a slow command (a cloud Start/Stop that takes its time to
    answer). The regulator's pass now writes A under that lock, so it waits there, and charger B's
    must-lower write for an overload, later in the same pass, waits with it."""
    import asyncio

    from custom_components.spotnav.execution.controller import ChargingController

    from .test_start_reservation import _overload

    a, b, site, _, _ = await _two_charger_site(hass, site_a=4.0)
    seen: list[str] = []
    original = ChargingController._async_assign_current_outcome  # noqa: SLF001

    async def recording(self, amps: int, **kwargs):  # type: ignore[no-untyped-def]
        seen.append(self.entry_id)
        from custom_components.spotnav.execution.controller import IN_EFFECT_OUTCOMES
        return next(iter(IN_EFFECT_OUTCOMES))  # the charger takes it

    monkeypatch.setattr(ChargingController, "_async_assign_current_outcome", recording)
    monkeypatch.setattr(type(site), "_is_commandable_charger", staticmethod(lambda _controller: True))
    monkeypatch.setattr(
        "custom_components.spotnav.site.site_capacity_controller.applyability_failure", lambda **_kwargs: None
    )
    site.regulator_decisions = {a.entry_id: _overload(a.entry_id), b.entry_id: _overload(b.entry_id)}

    release = asyncio.Event()

    async def slow_command() -> None:
        async with a._lock:  # noqa: SLF001 - a Start/Stop whose service call has not answered yet
            await release.wait()

    holder = hass.async_create_task(slow_command())
    await asyncio.sleep(0)
    apply = hass.async_create_task(site._async_apply_active_control())  # noqa: SLF001
    for _ in range(20):
        await asyncio.sleep(0)
    stalled = b.entry_id not in seen
    release.set()
    await holder
    await apply
    assert not stalled, "B's overload write waited for A's slow command"


async def test_one_chargers_slow_write_does_not_hold_anothers_overload_write(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R1: A's own write is a slow service call (a cloud that takes its time). B's must-lower write later in
    the same pass goes out meanwhile; the pass still waits for A's write to end."""
    import asyncio

    from custom_components.spotnav.execution.controller import ChargingController, IN_EFFECT_OUTCOMES

    from .test_start_reservation import _overload

    a, b, site, _, _ = await _two_charger_site(hass, site_a=4.0)
    seen: list[str] = []
    release = asyncio.Event()

    async def slow_for_a(self, amps: int, **kwargs):  # type: ignore[no-untyped-def]
        seen.append(self.entry_id)
        if self is a:
            await release.wait()
        return next(iter(IN_EFFECT_OUTCOMES))

    monkeypatch.setattr(ChargingController, "_async_assign_current_outcome", slow_for_a)
    monkeypatch.setattr(type(site), "_is_commandable_charger", staticmethod(lambda _controller: True))
    monkeypatch.setattr(
        "custom_components.spotnav.site.site_capacity_controller.applyability_failure", lambda **_kwargs: None
    )
    site.regulator_decisions = {a.entry_id: _overload(a.entry_id), b.entry_id: _overload(b.entry_id)}

    apply = hass.async_create_task(site._async_apply_active_control())  # noqa: SLF001
    for _ in range(20):
        await asyncio.sleep(0)
    assert seen == [a.entry_id, b.entry_id], "B's overload write waited for A's slow write"
    assert not apply.done(), "the pass waits for A's write"
    release.set()
    await apply
