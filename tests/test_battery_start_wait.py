"""The start-up wait (`battery/runtime.START_GRACE`): right after a restart a battery planner outside Home Assistant
(Predbat) has not shown its states yet, so a standing outside-control verdict does not end, and SpotNav's write path
and verification do not start, until the wait has passed. Seen on a SolaX site: Home Assistant restarted at 16:19,
`predbat.*` came back at 16:24, and SpotNav's verification wrote to the battery in between."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.spotnav.battery import runtime
from custom_components.spotnav.battery.control import BatteryControl, TrialRefused
from custom_components.spotnav.battery.outside_control import Observation, OutsideControlWatch
from custom_components.spotnav.battery.runtime import SECTION_OUTSIDE, START_GRACE, BatteryShadow, store_key
from custom_components.spotnav.runtime import site_controller_for
from tests.test_battery_control import _hold, _looks, _options, _site, _started

pytestmark = [pytest.mark.usefixtures("offline_relay"), pytest.mark.start_wait]

T0 = datetime(2026, 11, 22, 16, 19, tzinfo=UTC)
GRACE_S = START_GRACE.total_seconds()


class Clock:
    """The loop clock the wait is measured on (`runtime._monotonic`), moved by the test."""

    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self, _hass: HomeAssistant) -> float:
        return self.t


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    fake = Clock()
    monkeypatch.setattr(runtime, "_monotonic", fake)
    return fake


async def _restart(hass: HomeAssistant, shadow: BatteryShadow, clock: Clock) -> None:
    """A Home Assistant restart as the wait sees it: a new run, the wait counting from now."""
    hass.data.pop(runtime._RUN_START_KEY, None)
    shadow._begin_wait()
    assert shadow.start_wait()


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


async def test_the_wait_is_on_the_loop_clock_not_the_wall_clock(hass: HomeAssistant, clock: Clock) -> None:
    shadow = BatteryShadow.__new__(BatteryShadow)
    shadow.hass = hass
    shadow._wait_from = None
    assert shadow.start_wait(), "before Home Assistant has started it waits"
    shadow._begin_wait()
    # A forward step of the wall clock (NTP) ends nothing: only the loop clock counts.
    with pytest.MonkeyPatch.context() as scoped:
        scoped.setattr(dt_util, "now", lambda *_a: dt_util.utcnow() + timedelta(hours=3))
        assert shadow.start_wait()
    clock.t += GRACE_S - 1
    assert shadow.start_wait()
    clock.t += 2
    assert not shadow.start_wait()


async def test_a_restart_with_a_kept_predbat_verdict_writes_nothing_while_predbat_is_late(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any],
    caplog: pytest.LogCaptureFixture, clock: Clock,
) -> None:
    site, controller, plant = await _site(hass, monkeypatch, hass_storage)
    shadow = controller.battery_shadow
    now = dt_util.now()
    # The restart: the verdict kept from before, the wait counting from now, and Predbat not back yet.
    shadow.outside = _predbat_verdict()
    await _restart(hass, shadow, clock)
    control = await _started(hass, site, controller, plan_source=_hold)
    control.reports.clear()  # a fresh check would be due: it must not start either
    caplog.set_level(logging.INFO, logger="custom_components.spotnav.battery.runtime")
    for second in range(0, 15 * 60, 30):
        if second == 5 * 60:
            hass.states.async_set("predbat.status", "Idle")
        clock.t += 30
        at = now + timedelta(seconds=second)
        shadow.tick(at)
        await control.async_look(at)
    assert plant.calls == []
    assert shadow.outside.verdict.active and shadow.outside.verdict.reason == "planner:predbat"
    assert not [record for record in caplog.records if "outside" in record.getMessage()]


async def test_a_planner_that_never_comes_back_ends_the_verdict_once_after_the_wait(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any],
    caplog: pytest.LogCaptureFixture, clock: Clock,
) -> None:
    _site_entry, controller, _plant = await _site(hass, monkeypatch, hass_storage)
    shadow = controller.battery_shadow
    now = dt_util.now()
    shadow.outside = _predbat_verdict()
    await _restart(hass, shadow, clock)
    caplog.set_level(logging.INFO, logger="custom_components.spotnav.battery.runtime")
    for minute in range(10):
        shadow.tick(now + timedelta(minutes=minute))
        assert shadow.outside.verdict.active, minute
        clock.t += 59
    clock.t = shadow._wait_from + GRACE_S
    shadow.tick(now + timedelta(minutes=10))
    shadow.tick(now + timedelta(minutes=10, seconds=10))
    assert not shadow.outside.verdict.active
    ended = [record for record in caplog.records if "outside control ended after start-up wait" in record.getMessage()]
    assert len(ended) == 1 and ended[0].levelno == logging.INFO


async def test_a_fresh_install_writes_nothing_in_the_wait_and_checks_the_battery_after_it(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any], clock: Clock
) -> None:
    """No verdict, no planner, no report yet: the write path's own wait (`BatteryControl._start_wait`) is all that
    keeps the verification from writing at its first look."""
    site, controller, plant = await _site(hass, monkeypatch, hass_storage)
    await _restart(hass, controller.battery_shadow, clock)
    control = BatteryControl(hass, controller, plan_source=_hold)
    await control.async_start()
    _options(hass, site, enabled=True, shadow_only=False)
    assert control.report() is None and not controller.battery_shadow.outside.verdict.active
    assert control.hardware_test_refusal() == "starting"
    now = dt_util.now()
    for _ in range(int(GRACE_S // 30) - 1):
        clock.t += 30
        now = await _looks(control, now, 1, 30)
    assert plant.calls == [], "nothing is written in the wait"
    assert control.verification is None and control.state() == "waiting"
    assert control.status()["waiting_for"] == "start_wait"
    with pytest.raises(TrialRefused) as refused:
        await control.async_trial("hold", None, 5, force=True)
    assert refused.value.code == "starting"
    clock.t += 31
    await _looks(control, now, 3)
    assert control.verification is not None and control.state() == "verifying"
    assert plant.calls, "the check writes once the wait has passed"


async def test_a_written_hold_waits_for_the_wait_too(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any], clock: Clock
) -> None:
    site, controller, plant = await _site(hass, monkeypatch, hass_storage)
    await _restart(hass, controller.battery_shadow, clock)
    control = await _started(hass, site, controller, plan_source=_hold)
    now = dt_util.now()
    for second in range(0, int(GRACE_S) - 30, 30):
        clock.t += 30
        await control.async_look(now + timedelta(seconds=second))
        await control.async_tick(now + timedelta(seconds=second))  # a direct step is a hand-back in the wait too
    assert plant.calls == []
    clock.t += 60
    await control.async_look(now + START_GRACE + timedelta(seconds=1))
    assert plant.calls, "the hold is written once the wait has passed"


def _stored_predbat(hass_storage: dict[str, Any], entry_id: str) -> None:
    hass_storage[store_key(entry_id)] = {
        "version": 1, "minor_version": 1, "key": store_key(entry_id),
        "data": {"sections": {SECTION_OUTSIDE: _predbat_verdict().as_dict()}},
    }


async def test_a_restart_seeded_from_the_store_waits_from_home_assistant_s_start(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any], clock: Clock
) -> None:
    _site_entry, controller, _plant = await _site(hass, monkeypatch, hass_storage)
    hass.data.pop(runtime._RUN_START_KEY, None)
    _stored_predbat(hass_storage, controller.entry_id)
    hass.set_state(CoreState.starting)
    shadow = BatteryShadow(hass, controller)
    await shadow.async_start()
    assert shadow.outside.verdict.reason == "planner:predbat", "the kept verdict is back"
    clock.t += 3600
    assert shadow.start_wait() and shadow._wait_from is None, "no count before Home Assistant has started"
    hass.set_state(CoreState.running)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await hass.async_block_till_done()
    assert shadow._wait_from == clock.t
    shadow.tick(dt_util.now())
    assert shadow.outside.verdict.active
    clock.t += GRACE_S - 1
    assert shadow.start_wait()
    clock.t += 2
    assert not shadow.start_wait()
    shadow.halt()
    await shadow.async_shutdown()


async def test_a_reload_unsubscribes_from_the_start_and_skips_the_wait_late_in_a_run(
    hass: HomeAssistant, clock: Clock
) -> None:
    from tests.world import setup_charger_and_site

    hass.data.pop(runtime._RUN_START_KEY, None)
    hass.set_state(CoreState.starting)
    _charger, site = await setup_charger_and_site(hass)
    first = site_controller_for(hass, site.entry_id).battery_shadow
    assert first._unsub_started is not None
    assert await hass.config_entries.async_reload(site.entry_id)
    await hass.async_block_till_done()
    assert first._unsub_started is None, "the reload's halt unsubscribed"
    second = site_controller_for(hass, site.entry_id).battery_shadow
    hass.set_state(CoreState.running)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await hass.async_block_till_done()
    assert first._wait_from is None and second._wait_from == clock.t
    assert second.start_wait()

    # A reload within the first ten minutes of the run waits again from its own start.
    clock.t += 60
    assert await hass.config_entries.async_reload(site.entry_id)
    await hass.async_block_till_done()
    third = site_controller_for(hass, site.entry_id).battery_shadow
    assert third.start_wait() and third._wait_from == clock.t
    # A reload later in the run skips it: a planner's states do not vanish when SpotNav reloads.
    clock.t += GRACE_S
    assert await hass.config_entries.async_reload(site.entry_id)
    await hass.async_block_till_done()
    assert not site_controller_for(hass, site.entry_id).battery_shadow.start_wait()
