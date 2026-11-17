"""A plan charge paused twice at its window's start, the charger reporting its charge once more in between: the
field night of 2026-11-17.

A user's debug report. An Alfen behind a 25 A main fuse with a 5 A margin, a home battery that began its own grid
charge (about 11.8 A on two phases) in the minute the plan's window opened at 03:00. The car was started at 20 A, cut
to 8 A on the overload, and paused five seconds later. The charger kept its charge for the stop's whole answer time,
so once that had run out the regulator paused it again; while that second stop was on its way the charger reported
its charge once more, and the open window claimed the report as the plan's charge. The second pause's result then
took the claim, which sends nothing, for a start that ends what load balancing holds back, and forgot the pause: the
charger off, nobody's, nothing held back and nothing on its way. The regulator resumes only a charge it holds back,
and the window's start had taken, so nothing started the car again: from 03:53 the site had room for 19 A, and the
car stood until the next window's start at 04:30.

The events in their recorded order are decided again in `tests/test_core_ownership.py`
(`test_a_claim_between_two_pauses_of_one_charge_keeps_it_held_back`). Here the same order is driven through
`SiteCapacityController` with the real regulator and the real charger controller (`tests/
test_field_night_1_november.py`'s world), and the look at a charge load balancing stopped (`UNHELD_CHECK_S`) is
tested on a pause that was lost some other way.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from homeassistant.core import HomeAssistant, ServiceCall
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.spotnav.core import events as core_events
from custom_components.spotnav.execution.controller import (
    START_RETRY_AFTER_S,
    STOP_SETTLE_S,
    UNHELD_CHECK_S,
)

from .test_field_night_1_november import _HOUSE_0515, _Night, _the_night

#: The battery's grid charge that leaves no room for the car's minimum on L3 (`test_0515_...`).
_BATTERY_W = 7955.0
#: A house load on L3 that leaves no room for the car's minimum either (no battery: nothing to probe).
_HOUSE_FULL = (_HOUSE_0515[0], _HOUSE_0515[1], _HOUSE_0515[2] + 11.5)


def _charger_answers(night: _Night, answer: Any) -> None:
    """The charger's answer to a stop is `answer`'s from now on (the world's own is the HALO's: its charge reported
    once more at the next pass, then off)."""
    halo = night.halo

    async def turn_off(call: ServiceCall) -> None:
        halo.turn_off.append(call)
        night.offs = len(halo.turn_off)
        await answer()

    halo.hass.services.async_register("switch", "turn_off", turn_off)


async def _paused_twice_with_a_report_in_between(night: _Night) -> None:
    """03:00:29 to 03:00:45. The first stop is taken and the charge goes on for the stop's whole answer time; the
    second finds the charger reporting its charge once more while it is on its way, and then off."""
    halo = night.halo
    hass = halo.hass

    async def keeps_charging() -> None:
        return

    async def reports_once_more_then_off() -> None:
        hass.states.async_set(f"switch.{halo.prefix}", "on", {"answer": night.offs})
        halo.switch("off")
        night.on = False

    _charger_answers(night, keeps_charging)
    night.battery = "fixed"
    night.fixed_w = _BATTERY_W
    for _ in range(6):
        await night.step()
        if halo.turn_off:
            break
    assert len(halo.turn_off) == 1 and night.on, "paused once; the charger still charges"
    assert halo.cc.paused_by_balancing is False and halo.cc._balancing.paused  # noqa: SLF001

    _charger_answers(night, reports_once_more_then_off)
    for _ in range(int(STOP_SETTLE_S // 5) + 3):
        await night.step()
        if len(halo.turn_off) > 1:
            break
    assert len(halo.turn_off) == 2 and not night.on, "paused again once the first stop's time was up"


def _fed(night: _Night) -> list[str]:
    return [event["event"]["kind"] for event in night.halo.cc.session_runner.events]


async def test_0300_a_claim_between_two_pauses_keeps_the_charge_held_back_and_it_is_resumed(
    hass: HomeAssistant, monkeypatch, freezer
) -> None:
    night = await _the_night(hass, monkeypatch, freezer, "nov17claim", house=_HOUSE_0515)
    await _paused_twice_with_a_report_in_between(night)
    await hass.async_block_till_done()
    cc = night.halo.cc

    # The recorded order: the pause, the charge reported once more, the pause again, the claim decided again.
    kinds = [kind for kind in _fed(night) if kind in ("balancing_pause", "charger_reported_on", "recheck")]
    assert kinds[-4:] == ["balancing_pause", "charger_reported_on", "balancing_pause", "recheck"], _fed(night)
    session = cc.session_runner.session
    assert session.pending == () and session.owner == "none"
    assert cc.paused_by_balancing, "the claim, which sends nothing, did not end the pause"
    assert cc.paused_charge_origin == "plan_window"
    assert not cc.plan_expects_charge, "a balancing pause is no charge that failed to start: nothing is told"

    # The battery is done: the regulator resumes the charge within its dwell, as the plan's.
    night.battery = "idle"
    await night.run(90.0)
    assert len(night.halo.turn_on) == 2 and night.on, "\n".join(night.trace[-20:])
    assert cc.charge_origin == "plan_window"
    assert cc.window_start_retry["active"] is False, "the regulator resumed it, not the window's start again"


# -- a charge load balancing stopped that nothing holds back


async def _paused_for_the_house(hass: HomeAssistant, monkeypatch, freezer, entry_id: str) -> _Night:
    """Load balancing paused the plan's charge for a house load, and the charger answered: off, its current
    reading nothing (the Alfen's, where the HALO's keeps its last value)."""
    night = await _the_night(hass, monkeypatch, freezer, entry_id, house=_HOUSE_0515)
    night.house = _HOUSE_FULL
    for _ in range(6):
        await night.step()
        if night.halo.turn_off:
            break
    await night.step()
    cc = night.halo.cc
    assert len(night.halo.turn_off) == 1 and not night.on and cc.paused_by_balancing
    night.halo.reads(0.0)
    await hass.async_block_till_done()
    return night


async def _a_pause_that_was_lost(hass: HomeAssistant, monkeypatch, freezer, entry_id: str) -> _Night:
    """Load balancing paused the plan's charge, the charger answered, and the pause is on record no more (as the
    claim left it that night, here by the core's own event for a pause that is dropped): the charger off, nobody's,
    nothing held back, nothing on its way."""
    night = await _paused_for_the_house(hass, monkeypatch, freezer, entry_id)
    cc = night.halo.cc
    cc.session_runner.feed(core_events.BalancingDropped())
    session = cc.session_runner.session
    assert not session.balancing_paused and session.owner == "none" and session.pending == ()
    assert cc.plan_expects_charge and not cc.paused_by_balancing
    return night


async def _after(hass: HomeAssistant, freezer: Any, seconds: float) -> None:
    freezer.tick(timedelta(seconds=seconds))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


async def test_a_stopped_charge_nothing_holds_back_is_started_again_by_its_window(
    hass: HomeAssistant, monkeypatch, freezer, caplog: pytest.LogCaptureFixture
) -> None:
    """With room for the car: the window's start goes out again, capped by load balancing as every start is."""
    night = await _a_pause_that_was_lost(hass, monkeypatch, freezer, "nov17room")
    cc = night.halo.cc
    # The house load is gone; the site has room. Nothing resumes a charge that is not held back.
    night.house = _HOUSE_0515
    await night.run(60.0)
    assert len(night.halo.turn_on) == 1 and not night.on, "not resumed: nothing is held back"

    await _after(hass, freezer, UNHELD_CHECK_S)
    assert len(night.halo.turn_on) == 2, "the window's start, decided again"
    assert "nothing holds it back; the window's start is decided again" in caplog.text
    assert cc.window_start_retry["last"]["result"] == "sent"
    await night.step()
    assert night.on and cc.charge_origin == "plan_window"
    await night.run(START_RETRY_AFTER_S[-1] + 60.0)
    assert len(night.halo.turn_on) == 2 and len(night.halo.turn_off) == 1, "it charges: nothing more is sent"


async def test_a_stopped_charge_nothing_holds_back_waits_for_room_as_a_balancing_pause(
    hass: HomeAssistant, monkeypatch, freezer
) -> None:
    """With no room for the car's minimum: the window's start is held back by load balancing, which makes it the
    pause the regulator resumes; nothing goes to the charger until there is room."""
    night = await _a_pause_that_was_lost(hass, monkeypatch, freezer, "nov17noroom")
    cc = night.halo.cc
    night.freezer.tick(timedelta(seconds=UNHELD_CHECK_S - 60.0))
    await night.run(60.0)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert len(night.halo.turn_on) == 1, "no start against the fuse"
    assert cc.paused_by_balancing and cc.paused_charge_origin == "plan_window", "held back for the regulator"
    assert not cc.plan_expects_charge

    night.house = _HOUSE_0515
    await night.run(90.0)
    assert len(night.halo.turn_on) == 2 and night.on, "\n".join(night.trace[-20:])
    assert cc.charge_origin == "plan_window"


async def test_a_charge_load_balancing_holds_back_is_left_to_the_regulator(
    hass: HomeAssistant, monkeypatch, freezer
) -> None:
    """The pause on record, as it should be: the look finds it and sends nothing, with room or without."""
    night = await _paused_for_the_house(hass, monkeypatch, freezer, "nov17held")
    cc = night.halo.cc
    await _after(hass, freezer, UNHELD_CHECK_S + 5.0)
    assert len(night.halo.turn_on) == 1 and cc.paused_by_balancing
    assert cc.window_start_retry["active"] is False and cc.window_start_retry["last"] is None


@pytest.mark.parametrize("case", ["person_stop", "charge_seen_since", "unplugged", "window_over", "command_on_its_way"])
async def test_a_stopped_charge_is_not_started_again_against_what_holds_it(
    hass: HomeAssistant, monkeypatch, freezer, case: str
) -> None:
    """Never against a person's Stop, never a charger somebody else has had since (a charge seen after the stop
    makes its stopped state theirs), never without the car, never outside the window, and never while a command of
    ours is still on its way."""
    night = await _a_pause_that_was_lost(hass, monkeypatch, freezer, f"nov17{case}")
    cc = night.halo.cc
    night.house = _HOUSE_0515
    if case == "person_stop":
        cc.session_runner.feed(core_events.PersonStop(connected=True))
        cc.note_person_stop()
        assert not cc.plan_expects_charge
    elif case == "charge_seen_since":
        # Started and stopped at the charger itself, after our stop had landed.
        freezer.tick(timedelta(seconds=STOP_SETTLE_S + 1.0))
        night.halo.switch("on")
        await hass.async_block_till_done()
        night.halo.switch("off")
        await hass.async_block_till_done()
    elif case == "unplugged":
        from custom_components.spotnav.execution.chargers.adapter import ChargerAdapter

        monkeypatch.setattr(ChargerAdapter, "vehicle_connected", lambda self: None)
    elif case == "window_over":
        monkeypatch.setattr(type(cc), "_open_window_end", lambda self: None)
    elif case == "command_on_its_way":
        monkeypatch.setattr(type(cc), "_core_start_pending", lambda self: True)
    turned_on = len(night.halo.turn_on)
    await _after(hass, freezer, UNHELD_CHECK_S + 5.0)
    for _ in range(4):
        await _after(hass, freezer, UNHELD_CHECK_S)
    assert len(night.halo.turn_on) == turned_on, case
    assert cc.window_start_retry["last"] is None, "the window's start was not decided again"
