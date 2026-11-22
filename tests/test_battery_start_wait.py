"""The start-up wait (`battery/runtime.START_GRACE`): right after a restart a battery planner outside Home Assistant
(Predbat) has not shown its states yet, so a standing outside-control verdict does not end, and SpotNav's write path
and verification do not start, until the wait has passed. Seen on a SolaX site: Home Assistant restarted at 16:19,
`predbat.*` came back at 16:24, and SpotNav's verification wrote to the battery in between."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.spotnav.battery import runtime
from custom_components.spotnav.battery.control import TrialRefused
from custom_components.spotnav.battery.outside_control import Observation, OutsideControlWatch
from custom_components.spotnav.battery.runtime import START_GRACE, BatteryShadow
from tests.test_battery_control import _hold, _site, _started

pytestmark = [pytest.mark.usefixtures("offline_relay"), pytest.mark.start_wait]

T0 = datetime(2026, 11, 22, 16, 19, tzinfo=UTC)


def _predbat_verdict() -> OutsideControlWatch:
    """A watch as kept from before the restart: Predbat ran the battery."""
    watch = OutsideControlWatch()
    watch.observe(Observation(now=T0 - timedelta(hours=1), management="externally_controlled", planner="predbat"))
    assert watch.verdict.reason == "planner:predbat"
    return OutsideControlWatch.from_dict(watch.as_dict())


def _look(watch: OutsideControlWatch, at: datetime, *, planner: str | None, starting: bool) -> list[dict[str, Any]]:
    return watch.observe(Observation(now=at, management="free", planner=planner, starting=starting))


def test_ten_minutes_twice_the_planner_s_gap_seen() -> None:
    assert START_GRACE == timedelta(minutes=10)


def test_a_kept_verdict_holds_while_the_planner_is_away_in_the_wait_and_lasts_once_it_is_back() -> None:
    watch = _predbat_verdict()
    for minute in range(6):
        assert _look(watch, T0 + timedelta(minutes=minute), planner=None, starting=True) == []
        assert watch.verdict.active and watch.verdict.reason == "planner:predbat"
    assert _look(watch, T0 + timedelta(minutes=6), planner="predbat", starting=True) == []
    assert _look(watch, T0 + timedelta(minutes=11), planner="predbat", starting=False) == []
    assert watch.verdict.active and watch.verdict.since == T0 - timedelta(hours=1)


def test_a_planner_gone_for_longer_than_the_wait_ends_the_verdict_after_it_not_before() -> None:
    watch = _predbat_verdict()
    for minute in range(10):
        assert _look(watch, T0 + timedelta(minutes=minute), planner=None, starting=True) == []
    events = _look(watch, T0 + timedelta(minutes=10), planner=None, starting=False)
    assert [event["kind"] for event in events] == ["ended"]
    assert events[0]["after_start_wait"] is True and events[0]["reason"] == "planner:predbat"
    assert not watch.verdict.active
    # Outside the wait a planner that goes away ends a verdict at once, as before, and is no start-up end.
    assert _look(watch, T0 + timedelta(minutes=20), planner="predbat", starting=False)[0]["kind"] == "started"
    events = _look(watch, T0 + timedelta(minutes=21), planner=None, starting=False)
    assert events[0]["kind"] == "ended" and "after_start_wait" not in events[0]


async def test_the_wait_counts_from_home_assistant_s_start_when_that_comes_later(hass: HomeAssistant) -> None:
    shadow = BatteryShadow.__new__(BatteryShadow)
    shadow._wait_from = None
    assert shadow.start_wait(dt_util.now() + timedelta(days=1)), "before Home Assistant has started it waits"
    shadow._ha_started()
    now = dt_util.now()
    assert shadow.start_wait(now + START_GRACE - timedelta(seconds=1))
    assert not shadow.start_wait(now + START_GRACE + timedelta(seconds=1))
    assert hass.state is CoreState.running


async def test_a_restart_with_a_kept_predbat_verdict_writes_nothing_while_predbat_is_late(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    site, controller, plant = await _site(hass, monkeypatch, hass_storage)
    shadow = controller.battery_shadow
    now = dt_util.now()
    # The restart: the verdict kept from before, the wait counting from now, and Predbat not back yet.
    shadow.outside = _predbat_verdict()
    shadow._wait_from = now
    control = await _started(hass, site, controller, plan_source=_hold)
    control.reports.clear()  # a fresh check would be due: it must not start either
    caplog.set_level(logging.INFO, logger="custom_components.spotnav.battery.runtime")
    for second in range(0, 5 * 60, 30):
        at = now + timedelta(seconds=second)
        shadow.tick(at)
        await control.async_look(at)
    hass.states.async_set("predbat.status", "Idle")
    for second in range(5 * 60, 15 * 60, 30):
        at = now + timedelta(seconds=second)
        shadow.tick(at)
        await control.async_look(at)
    assert plant.calls == []
    assert shadow.outside.verdict.active and shadow.outside.verdict.reason == "planner:predbat"
    assert not [record for record in caplog.records if "outside" in record.getMessage()]


async def test_a_planner_that_never_comes_back_ends_the_verdict_once_after_the_wait(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    site, controller, _plant = await _site(hass, monkeypatch, hass_storage)
    shadow = controller.battery_shadow
    now = dt_util.now()
    shadow.outside = _predbat_verdict()
    shadow._wait_from = now
    caplog.set_level(logging.INFO, logger="custom_components.spotnav.battery.runtime")
    for minute in range(10):
        shadow.tick(now + timedelta(minutes=minute, seconds=30))
        assert shadow.outside.verdict.active, minute
    shadow.tick(now + START_GRACE + timedelta(seconds=10))
    shadow.tick(now + START_GRACE + timedelta(seconds=20))
    assert not shadow.outside.verdict.active
    ended = [record for record in caplog.records if "outside control ended after start-up wait" in record.getMessage()]
    assert len(ended) == 1 and ended[0].levelno == logging.INFO
    assert site.entry_id


async def test_a_fresh_install_writes_first_after_the_wait(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    site, controller, plant = await _site(hass, monkeypatch, hass_storage)
    now = dt_util.now()
    controller.battery_shadow._wait_from = now
    control = await _started(hass, site, controller, plan_source=_hold)
    assert control.hardware_test_refusal() == "starting"
    with pytest.raises(TrialRefused) as refused:
        await control.async_trial("hold", None, 5, force=True)
    assert refused.value.code == "starting"
    for second in range(0, int(START_GRACE.total_seconds()) - 10, 30):
        await control.async_look(now + timedelta(seconds=second))
        # A direct step is a hand-back in the wait too.
        await control.async_tick(now + timedelta(seconds=second))
    assert plant.calls == []
    assert control.status()["state"] == "on"
    await control.async_look(now + START_GRACE + timedelta(seconds=1))
    assert plant.calls, "the hold is written once the wait has passed"
    assert runtime.START_GRACE == START_GRACE
