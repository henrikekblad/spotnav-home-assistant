"""Adversarial review of the slow-car probe extension (0229f40): each test fails on that commit."""

from __future__ import annotations

from homeassistant.core import HomeAssistant

from custom_components.spotnav.site import site_capacity_controller as scc

from .test_battery_fuse import _connector_status, _paused_site
from .world import set_charger_delivered_a


class _Timers:
    """Records every probe timer: (due at, on the yield clock) and whether it was cancelled."""

    def __init__(self, clock) -> None:
        self.clock = clock
        self.timers: list[dict] = []

    def __call__(self, hass, delay, action):
        timer = {"due": self.clock() + delay, "cancelled": False}
        self.timers.append(timer)

        def cancel() -> None:
            timer["cancelled"] = True

        return cancel

    def live(self) -> list[float]:
        return [t["due"] for t in self.timers if not t["cancelled"]]


async def test_an_extended_probe_does_not_cancel_another_chargers_window_check(
    hass: HomeAssistant, monkeypatch
) -> None:
    """Two chargers, one shared probe timer. Charger A's probe waits for a slow car; charger B's probe
    starts and arms its window-end check. A's next extended pass re-arms the shared timer to A's 90 s
    deadline and so cancels B's check: B (whose car may hold the site above the band) is then judged
    only when some other pass happens to come (the periodic one is 30 s)."""
    (controller, charger, calls, clock, _dc, site, prefix, cc, turn_off) = await _paused_site(
        hass, monkeypatch, "rvtimer"
    )
    timers = _Timers(clock)
    monkeypatch.setattr(scc, "async_call_later", timers)
    await controller._async_apply_active_control()  # A's probe starts at t=0
    hass.states.async_set(f"switch.{prefix}", "on")
    _connector_status(hass, prefix, "Charging")
    set_charger_delivered_a(hass, prefix, 0.5)
    clock.advance(31.0)
    await controller._async_apply_active_control()  # A extended
    assert controller.battery_probe_snapshot[charger.entry_id]["state"] == "probing"

    # Charger B's probe starts at t=35 and arms its own window end (30 s, + 1 s): due at t=66.
    clock.advance(4.0)
    controller._schedule_probe_check(30.0)
    b_due = clock() + 31.0
    assert b_due in timers.live()

    # A meter update at t=36 runs a pass: A, still waiting for its car, re-arms the shared timer.
    clock.advance(1.0)
    await controller._async_apply_active_control()
    assert any(due <= b_due for due in timers.live()), (
        f"B's window-end check (due {b_due}) was cancelled; live timers {timers.live()}"
    )
