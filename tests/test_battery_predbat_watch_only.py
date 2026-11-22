"""Predbat with its read-only switch on for ten minutes without a break does not control the battery, so it is no
outside control (`runtime.predbat_watch_only`): watched and shown in the diagnostics only. Changing the switch makes
Predbat write a reset to the inverter, so controlling counts at once: off, unknown, missing, on for less, and its
Monitor mode (Predbat may still write in it)."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.spotnav.battery import runtime
from custom_components.spotnav.battery.runtime import (
    PREDBAT_READ_ONLY,
    PREDBAT_READ_ONLY_HOLD,
    external_planner,
    predbat_watch_only,
    watched_planner,
)
from custom_components.spotnav.diagnostics import async_get_config_entry_diagnostics
from tests.test_battery_runtime import _site

pytestmark = pytest.mark.usefixtures("offline_relay")

HOLD_S = PREDBAT_READ_ONLY_HOLD.total_seconds()


class Clocks:
    """The loop clock (`runtime._monotonic`) and the wall clock, moved together by the test."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.loop = 1000.0
        self.wall = dt_util.utcnow()
        monkeypatch.setattr(runtime, "_monotonic", lambda _hass: self.loop)
        monkeypatch.setattr(runtime.dt_util, "utcnow", lambda: self.wall)

    def advance(self, seconds: float) -> None:
        self.loop += seconds
        self.wall += timedelta(seconds=seconds)


@pytest.fixture
def clocks(monkeypatch: pytest.MonkeyPatch) -> Clocks:
    return Clocks(monkeypatch)


def _predbat(hass: HomeAssistant, read_only: str | None, clocks: Clocks, mode: str = "Control charge & discharge") -> None:
    """Predbat's states; the read-only switch changed now on the test's wall clock."""
    hass.states.async_set("predbat.status", "Idle")
    hass.states.async_set("select.predbat_mode", mode)
    if read_only is None:
        hass.states.async_remove(PREDBAT_READ_ONLY)
        return
    old = hass.states.get(PREDBAT_READ_ONLY)
    if old is None or old.state != read_only:
        hass.states.async_set(PREDBAT_READ_ONLY, read_only, timestamp=clocks.wall.timestamp())


@pytest.mark.parametrize(
    ("read_only", "mode", "watched"),
    [
        ("on", "Control charge & discharge", True),
        ("on", "Monitor", True),
        ("off", "Monitor", False),
        ("off", "Control charge", False),
        ("unknown", "Control SOC only", False),
        ("unavailable", "Monitor", False),
        (None, "Monitor", False),
    ],
)
async def test_only_read_only_on_for_ten_minutes_is_watched(
    hass: HomeAssistant, clocks: Clocks, read_only: str | None, mode: str, watched: bool
) -> None:
    _predbat(hass, read_only, clocks, mode)
    assert not predbat_watch_only(hass), "never at once"
    assert external_planner(hass) == "predbat"
    clocks.advance(HOLD_S)
    assert predbat_watch_only(hass) is watched
    assert external_planner(hass) == (None if watched else "predbat")
    assert (watched_planner(hass) is not None and watched_planner(hass)["state"] == "watched_not_controlling") is watched


async def test_a_wall_clock_step_does_not_shorten_the_hold(hass: HomeAssistant, clocks: Clocks) -> None:
    _predbat(hass, "on", clocks)
    assert not predbat_watch_only(hass)
    clocks.wall += timedelta(hours=1)  # an NTP step forward: the loop clock has not moved
    assert not predbat_watch_only(hass)
    clocks.loop += HOLD_S
    assert predbat_watch_only(hass)


async def test_without_predbat_running_its_switch_marks_nothing(hass: HomeAssistant, clocks: Clocks) -> None:
    hass.states.async_set(PREDBAT_READ_ONLY, "on")
    clocks.advance(HOLD_S)
    assert external_planner(hass) is None and watched_planner(hass) is None


async def _diagnostics_block(hass: HomeAssistant, site: Any) -> Any:
    return (await async_get_config_entry_diagnostics(hass, site))["battery"]["outside_control"]["watched_planner"]


async def test_read_only_turned_on_keeps_the_verdict_ten_minutes_and_ends_it_after(
    hass: HomeAssistant, clocks: Clocks, caplog: pytest.LogCaptureFixture
) -> None:
    site, controller = await _site(hass)
    shadow = controller.battery_shadow
    caplog.set_level(logging.INFO, logger="custom_components.spotnav.battery.runtime")
    now = dt_util.now()
    _predbat(hass, "off", clocks)
    shadow.tick(now)
    assert shadow.outside.verdict.reason == "planner:predbat"

    _predbat(hass, "on", clocks)
    for second in range(0, int(HOLD_S) - 10, 30):
        clocks.advance(30)
        shadow.tick(now + timedelta(seconds=second))
        assert shadow.outside.verdict.active, second
        assert shadow.facts.management.state == "externally_controlled"
    assert (await _diagnostics_block(hass, site))["state"] == "read_only_pending"
    clocks.advance(30)
    shadow.tick(now + timedelta(seconds=HOLD_S + 30))
    assert not shadow.outside.verdict.active
    assert shadow.facts.management.state != "externally_controlled"
    block = await _diagnostics_block(hass, site)
    assert block["reason"] == "planner:predbat" and block["read_only"] is True
    assert block["state"] == "watched_not_controlling"
    assert len([r for r in caplog.records if "watched, not counted" in r.getMessage()]) == 1

    # Turned off again: a verdict at once.
    _predbat(hass, "off", clocks)
    shadow.tick(now + timedelta(seconds=HOLD_S + 40))
    assert shadow.outside.verdict.reason == "planner:predbat"
    assert await _diagnostics_block(hass, site) is None


async def test_read_only_flipped_off_inside_the_hold_never_ended_the_verdict(
    hass: HomeAssistant, clocks: Clocks
) -> None:
    _site_entry, controller = await _site(hass)
    shadow = controller.battery_shadow
    now = dt_util.now()
    _predbat(hass, "off", clocks)
    shadow.tick(now)
    since = shadow.outside.verdict.since
    _predbat(hass, "on", clocks)
    for minute in range(1, 6):
        clocks.advance(60)
        shadow.tick(now + timedelta(minutes=minute))
    _predbat(hass, "off", clocks)
    clocks.advance(60)
    shadow.tick(now + timedelta(minutes=6))
    # On again: the hold starts over.
    _predbat(hass, "on", clocks)
    for minute in range(7, 16):
        clocks.advance(60)
        shadow.tick(now + timedelta(minutes=minute))
    assert shadow.outside.verdict.active and shadow.outside.verdict.since == since
    assert not shadow.outside.history, "it never ended"


async def test_unknown_counts_as_controlling(hass: HomeAssistant, clocks: Clocks) -> None:
    _site_entry, controller = await _site(hass)
    shadow = controller.battery_shadow
    _predbat(hass, "unknown", clocks)
    clocks.advance(HOLD_S * 2)
    shadow.tick(dt_util.now())
    assert shadow.outside.verdict.reason == "planner:predbat"


@pytest.mark.start_wait
async def test_in_the_start_up_wait_a_read_only_switch_not_loaded_yet_counts_as_controlling(
    hass: HomeAssistant, clocks: Clocks
) -> None:
    _site_entry, controller = await _site(hass)
    shadow = controller.battery_shadow
    now = dt_util.now()
    hass.data.pop(runtime._RUN_START_KEY, None)
    shadow._begin_wait()
    _predbat(hass, None, clocks)
    shadow.tick(now)
    assert shadow.outside.verdict.reason == "planner:predbat"
    # Its read-only switch loads a minute later: watch-only needs its ten minutes, and the wait holds the verdict.
    _predbat(hass, "on", clocks)
    clocks.advance(60)
    shadow.tick(now + timedelta(minutes=1))
    assert shadow.outside.verdict.active
    clocks.advance(HOLD_S)
    shadow.tick(now + timedelta(minutes=11))
    assert not shadow.outside.verdict.active
