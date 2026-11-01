"""Load balancing's pause of a start the charger has not answered yet, and the readings a stopped charger sends after
its pause (`ChargingController._regulated_stop`, `_stop_locked`; `SiteCapacityController._stopped_charger_reading`).

The HALO world of `tests/test_lb_start_credit.py` (20 A fuse, no margin, an OCPP charger whose control answers a
command later), with Home Assistant's clock frozen and moved by hand.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.spotnav.execution.controller import STOP_SETTLE_S
from custom_components.spotnav.site.site_capacity_controller import charge_still_wanted

from .helpers import install_schedule
from .test_lb_start_credit import _halo, _Halo, _site_a

_HOUSE = (1.0, 1.0, 1.0)


async def _start_on_its_way(hass: HomeAssistant, monkeypatch, entry_id: str, *, manual: bool = False) -> _Halo:
    """The car started (the plan's window, or a person's Start), the charger not answering yet."""
    halo = await _halo(hass, monkeypatch, entry_id, l1=_HOUSE[0], l2=_HOUSE[1], l3=_HOUSE[2])
    if manual:
        assert await halo.cc.async_start(16, manual=True)
    else:
        now = dt_util.utcnow()
        window = {"start": (now - timedelta(minutes=1)).isoformat(), "end": (now + timedelta(hours=2)).isoformat()}
        await install_schedule(halo.cc, {"periods": [window], "amps": 16})
        if not halo.turn_on:
            await halo.start()
    await hass.async_block_till_done()
    assert len(halo.turn_on) == 1 and halo.cc.start_pending
    return halo


async def _overload(halo: _Halo) -> None:
    """A load of 39 A on L1: no room for the car's minimum, even were all of the start credited."""
    _site_a(halo.hass, halo.site, 40.0, _HOUSE[1], _HOUSE[2])
    await halo.hass.async_block_till_done()
    await halo.tick()


def _pass(halo: _Halo, freezer, seconds: float) -> None:
    freezer.tick(timedelta(seconds=seconds))
    halo.advance(seconds)


async def test_a_charger_that_answers_the_start_but_not_the_pause_is_paused_again(
    hass: HomeAssistant, monkeypatch, freezer
) -> None:
    halo = await _start_on_its_way(hass, monkeypatch, "onwayignored")
    await _overload(halo)
    assert len(halo.turn_off) == 1, [(e["outcome"], e["reason"], e["detail"], e["from_a"]) for e in halo.log()]
    # The start lands, the stop is lost: the car charges on.
    halo.switch("on")
    halo.reads(10.0)
    _site_a(hass, halo.site, 40.0, 11.0, 11.0)
    await hass.async_block_till_done()
    _pass(halo, freezer, 5.0)
    await halo.tick()
    assert len(halo.turn_off) == 1, "the stop on its way is not sent again at once"
    _pass(halo, freezer, STOP_SETTLE_S)
    halo.reads(10.0)
    await halo.tick()
    assert len(halo.turn_off) == 2, "sent again once the stop has had its time"


@pytest.mark.parametrize("manual", [False, True])
async def test_a_safety_stop_of_a_start_on_its_way_stops_it_as_a_running_charge(
    hass: HomeAssistant, monkeypatch, manual: bool
) -> None:
    """A safety stop (a current the charger would not take) of a start on its way goes out, and holds back what it
    holds back of a running charge: a person's charge, not the plan's (P3)."""
    halo = await _start_on_its_way(hass, monkeypatch, f"onwaysafety{int(manual)}", manual=manual)
    write = await halo.cc._regulated_stop("safety_stop", cause="refused")  # noqa: SLF001
    assert write.outcome == "stopped" and write.stop_sent
    assert len(halo.turn_off) == 1 and not halo.cc.start_pending
    assert halo.cc.paused_by_balancing is manual


async def test_a_pause_of_a_person_s_start_on_its_way_is_theirs_to_resume(hass: HomeAssistant, monkeypatch) -> None:
    halo = await _start_on_its_way(hass, monkeypatch, "onwayperson", manual=True)
    await _overload(halo)
    assert len(halo.turn_off) == 1
    assert halo.cc.paused_by_balancing
    assert halo.cc.paused_charge_origin == "manual", "the person's charge, held back"
    assert charge_still_wanted(hass, halo.charger.entry_id, halo.cc), "and the resume may give it back"


async def test_the_pause_of_a_start_on_its_way_owes_it_no_current(hass: HomeAssistant, monkeypatch) -> None:
    """A current a session-bound number could not take at the start is written when the number appears; once the pause
    has stopped that start, the late answer must not write the plan's current into the paused charge."""
    halo = await _start_on_its_way(hass, monkeypatch, "onwaywrite")
    halo.cc._start_write_pending = True  # noqa: SLF001
    await _overload(halo)
    assert len(halo.turn_off) == 1
    assert halo.cc._start_write_pending is False  # noqa: SLF001


# -- the readings after a pause


async def _paused(hass: HomeAssistant, monkeypatch, freezer, entry_id: str) -> _Halo:
    """The car charges at 10 A by its own reading; a load on L1 pauses it."""
    halo = await _halo(hass, monkeypatch, entry_id, l1=_HOUSE[0], l2=_HOUSE[1], l3=_HOUSE[2])
    await halo.start()
    halo.switch("on")
    halo.reads(10.0)
    _site_a(hass, halo.site, 11.0, 11.0, 11.0)
    await hass.async_block_till_done()
    await halo.tick()
    _pass(halo, freezer, 5.0)
    _site_a(hass, halo.site, 36.0, 11.0, 11.0)
    halo.reads(10.0)
    await hass.async_block_till_done()
    await halo.tick()
    assert len(halo.turn_off) == 1
    return halo


def _reading(halo: _Halo) -> float:
    from custom_components.spotnav.const import CONF_PHASE_WIRING

    controller = halo.controller
    wiring = controller.config[CONF_PHASE_WIRING][halo.charger.entry_id]
    reading = controller._stopped_charger_reading(halo.charger.entry_id, wiring)  # noqa: SLF001
    return max(reading.get(phase).value for phase in ("L1", "L2", "L3"))


async def test_a_value_sent_with_the_stop_is_the_charge_before_it_and_one_later_is_the_car(
    hass: HomeAssistant, monkeypatch, freezer
) -> None:
    halo = await _paused(hass, monkeypatch, freezer, "afterpause")
    halo.switch("off")
    _pass(halo, freezer, 3.0)
    halo.reads(10.02)
    await hass.async_block_till_done()
    assert _reading(halo) == 0.0, "sent with the stop: the charge before the pause"
    _pass(halo, freezer, 8.0)
    halo.reads(10.04)
    await hass.async_block_till_done()
    assert _reading(halo) > 9.0, "sent 11 s after the pause: the car drawing"


async def test_a_pause_that_sends_nothing_does_not_move_the_window(hass: HomeAssistant, monkeypatch, freezer) -> None:
    """The charger ignores the stop and its control still reads on. A pass that pauses it again while the stop is on its
    way (here the damper forgot the pause, as a restart or active control turned off and on makes it) sends nothing,
    and the time of the pause stays: the meter values the charger keeps sending count as the car drawing from 10 s
    after the pause, not from 10 s after the last pass that found the stop on its way."""
    halo = await _paused(hass, monkeypatch, freezer, "slide")
    entry_id = halo.charger.entry_id
    paused_at = halo.controller._written_at[entry_id]  # noqa: SLF001
    _pass(halo, freezer, 5.5)
    halo.controller._dampers[entry_id].forget_write(None)  # noqa: SLF001
    halo.reads(10.01)
    await hass.async_block_till_done()
    await halo.tick()
    assert len(halo.turn_off) == 1, "the stop on its way is not sent again"
    assert halo.controller._dampers[entry_id].last_written_a == 0.0, "paused again"  # noqa: SLF001
    assert halo.controller._written_at[entry_id] == paused_at  # noqa: SLF001
    _pass(halo, freezer, 5.5)
    halo.reads(10.02)
    await hass.async_block_till_done()
    assert _reading(halo) > 9.0, "11 s after the pause: the car drawing"
