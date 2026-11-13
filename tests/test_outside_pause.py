"""A pause of the plan's charge SpotNav did not command (an aggregator's, such as Tibber Grid Rewards): not told,
the plan kept, counted (`notifications/outside_pause.py`, `unexpected_stop.UnexpectedStopDetector`)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from homeassistant.core import HomeAssistant

from custom_components.spotnav.diagnostics import async_get_config_entry_diagnostics
from custom_components.spotnav.notifications.outside_pause import MAX_S, MIN_S, OutsidePauses
from custom_components.spotnav.notifications.unexpected_stop import (
    GRACE_S,
    ExpectationFacts,
    UnexpectedStopDetector,
)
from tests.test_notifications import _charger, _later, _window_now
from tests.world import controller_of

T0 = datetime(2026, 11, 14, 20, 0, tzinfo=UTC)


def at(seconds: float, **facts: Any) -> ExpectationFacts:
    values: dict[str, Any] = {"expected": True, "window": "w1", "charging": False}
    values.update(facts)
    return ExpectationFacts(now=T0 + timedelta(seconds=seconds), **values)


def _charged(detector: UnexpectedStopDetector) -> None:
    assert detector.observe(at(0, charging=True)) is None


def test_a_ten_minute_pause_the_charger_holds_is_counted_not_told() -> None:
    detector = UnexpectedStopDetector()
    _charged(detector)
    for second in range(60, 660, 60):
        assert detector.observe(at(second, held_by_charger=True)) is None
    assert detector.due_at == T0 + timedelta(seconds=60 + MAX_S)
    assert detector.observe(at(660, charging=True)) is None
    [ended] = detector.take_pause_ends()
    assert (ended.how, ended.outcome, ended.seconds) == ("charger_held", "resumed", 600.0)
    tally = detector.pauses.as_dict(T0 + timedelta(seconds=700))
    assert tally["today"] == tally["total"] == 1 and tally["open_since"] is None and tally["recent"][0]["minutes"] == 10.0


def test_five_pauses_in_a_session_are_five_and_none_is_told() -> None:
    detector = UnexpectedStopDetector()
    _charged(detector)
    second = 0
    for _ in range(5):
        for _minute in range(10):
            second += 60
            assert detector.observe(at(second)) is None
        second += 60
        assert detector.observe(at(second, charging=True)) is None
    assert detector.pauses.total == 5 and detector.pauses.too_long == 0


def test_a_pause_that_outlasts_the_bound_is_told_once_and_not_counted() -> None:
    detector = UnexpectedStopDetector()
    _charged(detector)
    assert detector.observe(at(10, held_by_charger=True)) is None
    assert detector.observe(at(10 + MAX_S - 1, held_by_charger=True)) is None
    assert detector.observe(at(10 + MAX_S, held_by_charger=True)) == "held_by_charger"
    assert detector.observe(at(20 + MAX_S, held_by_charger=True)) is None
    assert detector.observe(at(40 * 60, held_by_charger=True)) is None
    assert detector.pauses.total == 0 and detector.pauses.too_long == 1


def test_a_flicker_is_not_counted_and_a_window_that_ends_in_a_pause_is() -> None:
    detector = UnexpectedStopDetector()
    _charged(detector)
    detector.observe(at(10))
    detector.observe(at(10 + MIN_S - 1, charging=True))
    assert detector.pauses.total == 0
    detector.observe(at(100))
    assert detector.observe(at(400, expected=False)) is None
    assert detector.pauses.total == 1 and detector.pauses.recent[-1].outcome == "plan_moved_on"


def test_other_trouble_in_a_pause_is_told_on_the_short_grace() -> None:
    detector = UnexpectedStopDetector()
    _charged(detector)
    assert detector.observe(at(10)) is None
    assert detector.observe(at(10 + GRACE_S)) is None
    assert detector.observe(at(20 + GRACE_S, available=False)) == "charger_unavailable"
    assert detector.pauses.total == 0 and detector.pauses.recent[-1].outcome == "other_trouble"


def test_a_car_that_declines_current_and_a_start_that_never_ran_keep_the_short_grace() -> None:
    detector = UnexpectedStopDetector()
    _charged(detector)
    assert detector.observe(at(10, charging=True, vehicle_not_requesting=True)) is None
    assert detector.observe(at(10 + GRACE_S, charging=True, vehicle_not_requesting=True)) == "vehicle_not_requesting"
    never = UnexpectedStopDetector()
    assert never.observe(at(0)) is None
    assert never.observe(at(GRACE_S)) == "not_started"
    assert never.pauses.total == 0


def test_today_resets_on_a_new_day() -> None:
    pauses = OutsidePauses()
    pauses.begin(T0, "stopped")
    pauses.end(T0 + timedelta(minutes=10), "resumed")
    assert pauses.as_dict(T0 + timedelta(days=1))["today"] == 0
    assert pauses.as_dict(T0 + timedelta(days=1))["total"] == 1


async def test_a_charge_paused_from_outside_and_resumed_is_not_told_and_is_in_the_diagnostics(
    hass: HomeAssistant, freezer: Any
) -> None:
    entry, calls = await _charger(hass)
    controller = controller_of(hass, entry.entry_id)
    hass.states.async_set("switch.charger_a", "on")
    await controller.async_install(_window_now())
    await hass.async_block_till_done()
    hass.states.async_set("switch.charger_a", "off")  # an aggregator pauses it from its cloud
    await hass.async_block_till_done()
    await _later(hass, freezer, 600)
    hass.states.async_set("switch.charger_a", "on")
    await hass.async_block_till_done()
    await _later(hass, freezer, MAX_S)

    assert calls == []
    assert controller.plan is not None, "the plan is kept"
    pauses = (await async_get_config_entry_diagnostics(hass, entry))["outside_pauses"]
    assert pauses["total"] == 1 and pauses["recent"][0]["how"] == "stopped"
