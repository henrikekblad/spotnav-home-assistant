"""Predbat in read-only or in Monitor does not control the battery, so it is no outside control (`runtime.
predbat_watch_only`): watched and shown in the diagnostics only. The moment it says it controls again, or its
switches read nothing, it marks again on the next look (unknown counts as controlling)."""

from __future__ import annotations

import logging
from datetime import timedelta

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.spotnav.battery.runtime import (
    PREDBAT_MODE,
    PREDBAT_READ_ONLY,
    external_planner,
    predbat_watch_only,
    watched_planner,
)
from custom_components.spotnav.diagnostics import async_get_config_entry_diagnostics
from tests.test_battery_runtime import _site

pytestmark = pytest.mark.usefixtures("offline_relay")


def _predbat(hass: HomeAssistant, read_only: str | None, mode: str | None = "Control charge & discharge") -> None:
    hass.states.async_set("predbat.status", "Idle")
    for entity_id, state in ((PREDBAT_READ_ONLY, read_only), (PREDBAT_MODE, mode)):
        if state is None:
            hass.states.async_remove(entity_id)
        else:
            hass.states.async_set(entity_id, state)


@pytest.mark.parametrize(
    ("read_only", "mode", "controlling"),
    [
        ("on", "Control charge & discharge", False),
        ("off", "Monitor", False),
        ("on", None, False),
        ("off", "Control charge", True),
        ("off", "Control SOC only", True),
        ("unknown", "unavailable", True),
        ("unavailable", None, True),
        (None, None, True),
    ],
)
async def test_read_only_or_monitor_is_watched_anything_else_controls(
    hass: HomeAssistant, read_only: str | None, mode: str | None, controlling: bool
) -> None:
    _predbat(hass, read_only, mode)
    assert predbat_watch_only(hass) is not controlling
    assert external_planner(hass) == ("predbat" if controlling else None)
    assert (watched_planner(hass) is None) is controlling


async def test_without_predbat_running_its_switches_mark_nothing(hass: HomeAssistant) -> None:
    hass.states.async_set(PREDBAT_READ_ONLY, "off")
    assert external_planner(hass) is None and watched_planner(hass) is None


async def test_the_verdict_follows_predbat_s_own_switches_at_the_next_look(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    site, controller = await _site(hass)
    shadow = controller.battery_shadow
    shadow._wait_from = dt_util.now() - timedelta(hours=1)  # the start-up wait has passed
    caplog.set_level(logging.INFO, logger="custom_components.spotnav.battery.runtime")
    now = dt_util.now()

    _predbat(hass, "on")
    shadow.tick(now)
    assert not shadow.outside.verdict.active
    assert shadow.facts.management.state != "externally_controlled"
    battery = (await async_get_config_entry_diagnostics(hass, site))["battery"]
    assert battery["outside_control"]["watched_planner"] == {
        "reason": "planner:predbat", "read_only": True, "mode": "Control charge & discharge",
        "state": "watched_not_controlling",
    }
    assert len([r for r in caplog.records if "watched, not counted" in r.getMessage()]) == 1

    # Read-only turned off: a verdict at once.
    _predbat(hass, "off")
    shadow.tick(now + timedelta(seconds=10))
    assert shadow.outside.verdict.active and shadow.outside.verdict.reason == "planner:predbat"
    assert shadow.facts.management.state == "externally_controlled"

    # Monitor: none (the verdict of a planner gone ends at once outside the start-up wait).
    _predbat(hass, "off", "Monitor")
    shadow.tick(now + timedelta(seconds=20))
    assert not shadow.outside.verdict.active

    # Unknown counts as controlling.
    _predbat(hass, "unknown", "unknown")
    shadow.tick(now + timedelta(seconds=30))
    assert shadow.outside.verdict.active
    battery = (await async_get_config_entry_diagnostics(hass, site))["battery"]
    assert battery["outside_control"]["watched_planner"] is None


@pytest.mark.start_wait
async def test_in_the_start_up_wait_a_read_only_switch_not_loaded_yet_counts_as_controlling(hass: HomeAssistant) -> None:
    _site_entry, controller = await _site(hass)
    shadow = controller.battery_shadow
    now = dt_util.now()
    shadow._wait_from = now
    _predbat(hass, None, None)
    shadow.tick(now)
    assert shadow.outside.verdict.reason == "planner:predbat"
    # Its read-only switch loads a minute later: the verdict ends only after the wait (the wait holds it).
    _predbat(hass, "on")
    shadow.tick(now + timedelta(minutes=1))
    assert shadow.outside.verdict.active
    shadow.tick(now + timedelta(minutes=11))
    assert not shadow.outside.verdict.active
