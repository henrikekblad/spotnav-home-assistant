"""Adversarial review of c82b79b (the probe timer set and the start status)."""

from __future__ import annotations

import random

from homeassistant.core import HomeAssistant

from custom_components.spotnav.site import site_capacity_controller as scc
from custom_components.spotnav.site.battery_probe import BatteryProbe

from .test_battery_fuse import _paused_site, _ramp


def test_a_status_that_flickers_unavailable_revives_a_stale_charging() -> None:
    """A stale 'Charging' at the start is rightly no sign; but one pass where the status is unreadable
    (unavailable/unknown, an OCPP reconnect) marks it as having left charging, and the same stale
    'Charging' then extends a car that never draws to 90 s."""
    probe = BatteryProbe()
    probe.start(0.0, 6.0, {p: 20.0 for p in ("L1", "L2", "L3")}, ("L1", "L2", "L3"),
                {p: 0.0 for p in ("L1", "L2", "L3")}, charger_charging=True)
    _ramp(probe, 10.0, charging=None)  # status unavailable for one pass (the controller passes None)
    verdict = _ramp(probe, 30.0, delivered=0.0, charging=True)
    assert (verdict.state, verdict.reason) == ("failed", "car_not_drawing")


async def test_every_check_due_is_run_within_a_second_of_its_time(hass: HomeAssistant, monkeypatch) -> None:
    """Randomised: checks asked for at random times from several probes; timers fired when due."""
    (controller, *_rest) = await _paused_site(hass, monkeypatch, "rv2timer")
    t = [1000.0]
    controller._yield_now = lambda: t[0]
    pending: list[list] = []

    def call_later(hass, delay, action):
        entry = [t[0] + delay, action, False]
        pending.append(entry)

        def cancel():
            entry[2] = True

        return cancel

    monkeypatch.setattr(scc, "async_call_later", call_later)
    runs: list[float] = []
    monkeypatch.setattr(controller, "_recompute", lambda **_k: runs.append(t[0]))
    rng = random.Random(12)
    asked: list[float] = []
    for _ in range(400):
        t[0] += rng.choice([0.0, 0.05, 0.3, 1.0, 3.0, 7.0])
        while True:
            due_now = sorted((e for e in pending if not e[2] and e[0] <= t[0]), key=lambda e: e[0])
            if not due_now:
                break
            entry = due_now[0]
            entry[2] = True
            saved = t[0]
            t[0] = entry[0]
            entry[1](None)
            t[0] = saved
        if rng.random() < 0.3:
            delay = rng.choice([5.0, 30.0, 59.0, 0.0, 0.4])
            controller._schedule_probe_check(delay)
            asked.append(round(t[0] + delay, 1))
    t[0] += 200
    while any(not e[2] for e in pending):
        entry = min((e for e in pending if not e[2]), key=lambda e: e[0])
        entry[2] = True
        t[0] = entry[0]
        entry[1](None)
    live = [e for e in pending if not e[2]]
    assert len(live) <= 1
    for due in asked:
        assert any(due <= run <= due + 1.05 for run in runs), (due, [r for r in runs if abs(r - due) < 5])
