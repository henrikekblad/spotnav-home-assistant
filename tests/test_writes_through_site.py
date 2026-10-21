"""Every current written outside the site's regulator respects the site and is known to it.

The case behind it: an Easee whose status blips through `unavailable` is sent its requested current again
(`_async_resend_current`), and an OCPP session limit that was not there at the start is written once it is
(`_async_write_after_start`). Both wrote the full request, past the site's allowance, and neither told the site:
its damper and step guard still held the older, lower value the regulator had written, so the next overload's
proposal could equal that value and read as "no change" while the charger ran above what the fuse allows.

Now (a) every such write is held to the site's allowance and told to the site (`note_charger_write`), and (b) an
overload is measured against the charger's own fresh reading when that is a step above what was written
(`charger_above_written`), for a write the site was never told of.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import async_mock_service

from custom_components.spotnav.execution.chargers.base import (
    WRITE_RESEND,
    WRITE_SESSION_START,
    WRITE_SOLAR,
)
from custom_components.spotnav.site.step_guard import CATCH_UP_S, charger_above_written

from .charger_helpers import enable_easee_limit_sensor
from .test_charger_controller_paths import _controller
from .test_lb_start_credit import _halo, _site_a

# -- the pure rule (b)


def test_a_fresh_reading_a_step_above_what_was_written_is_what_the_charger_carries() -> None:
    assert charger_above_written(written_a=10.0, measured_a=16.0, charger_read_at=115.0, written_at=100.0) == 16.0


@pytest.mark.parametrize(
    ("measured", "read_at", "written_at"),
    [
        (10.6, 200.0, 100.0),  # within a step of what was written: noise, not a lost limit
        (16.0, 100.0 + CATCH_UP_S - 1.0, 100.0),  # taken before the charger could follow the write
        (None, 200.0, 100.0),
        (16.0, None, 100.0),
    ],
)
def test_otherwise_what_was_written_stands(measured, read_at, written_at) -> None:
    assert charger_above_written(written_a=10.0, measured_a=measured, charger_read_at=read_at, written_at=written_at) is None


def test_nothing_written_means_nothing_to_compare() -> None:
    assert charger_above_written(written_a=None, measured_a=16.0, charger_read_at=200.0, written_at=None) is None


# -- (a) at the charger: the cap and the record


async def _easee(hass: HomeAssistant, allowance: float | None):
    controller = await _controller(hass, "easee")
    enable_easee_limit_sensor(hass, "0")
    hass.states.async_set("sensor.easee_status", "charging")
    await hass.async_block_till_done()
    limits = async_mock_service(hass, "easee", "set_charger_dynamic_limit")
    async_mock_service(hass, "easee", "action_command")
    notes: list[tuple[float, str]] = []
    controller.set_start_cap(lambda: allowance, record=lambda amps, reason: notes.append((amps, reason)))
    await controller.async_set_requested_current(16)
    await hass.async_block_till_done()
    return controller, limits, notes


async def _blip(hass: HomeAssistant) -> None:
    hass.states.async_set("sensor.easee_status", "unavailable")
    await hass.async_block_till_done()
    hass.states.async_set("sensor.easee_status", "charging")
    await hass.async_block_till_done()


async def test_an_easee_blip_resends_no_more_than_the_site_allows_and_tells_the_site(hass: HomeAssistant) -> None:
    controller, limits, notes = await _easee(hass, 9.4)
    await _blip(hass)
    # One amp lower first, so Easee's cache cannot skip it; then the site's 9 A, not the 16 A asked for.
    assert [call.data["current"] for call in limits] == [8, 9]
    assert notes == [(9.0, WRITE_RESEND)]
    await controller.async_shutdown()


async def test_an_easee_blip_with_no_room_resends_the_start_minimum_and_tells_the_site(hass: HomeAssistant) -> None:
    """Writing nothing would leave the charger on the limit it cleared; the least it takes is written, and the
    site's regulator, told of it, pauses the charge from there."""
    controller, limits, notes = await _easee(hass, 2.0)
    await _blip(hass)
    assert [call.data["current"] for call in limits] == [7]
    assert notes == [(7.0, WRITE_RESEND)]
    await controller.async_shutdown()


async def test_an_easee_blip_with_no_site_resends_the_request(hass: HomeAssistant) -> None:
    controller, limits, notes = await _easee(hass, None)
    await _blip(hass)
    assert [call.data["current"] for call in limits] == [15, 16]
    assert notes == [(16.0, WRITE_RESEND)]
    await controller.async_shutdown()


async def test_the_suns_own_write_is_held_to_the_site_and_told_to_it(hass: HomeAssistant) -> None:
    controller = await _controller(hass, "wallbox")
    number = async_mock_service(hass, "number", "set_value")
    notes: list[tuple[float, str]] = []
    controller.set_start_cap(lambda: 8.0, record=lambda amps, reason: notes.append((amps, reason)))
    await controller.async_write_solar_current(12, cap_a=16)
    assert [call.data["value"] for call in number] == [8]
    assert notes == [(8.0, WRITE_SOLAR)]
    await controller.async_shutdown()


async def test_the_regulators_own_write_is_not_told_back_to_the_site(hass: HomeAssistant) -> None:
    controller = await _controller(hass, "wallbox")
    async_mock_service(hass, "number", "set_value")
    notes: list[tuple[float, str]] = []
    controller.set_start_cap(lambda: 8.0, record=lambda amps, reason: notes.append((amps, reason)))
    await controller.async_apply_regulated_current(10, must_lower=False)
    assert notes == []
    await controller.async_shutdown()


async def test_an_abb_start_that_is_itself_a_current_write_is_told_to_the_site(hass: HomeAssistant) -> None:
    controller = await _controller(hass, "abb_terra_ac")
    number = async_mock_service(hass, "number", "set_value")
    # Paused the ABB way: its limit at 0 A.
    hass.states.async_set("number.abb_terra_ac_current_limit", "0", {"unit_of_measurement": "A", "min": 0, "max": 32})
    hass.states.async_set("sensor.abb_terra_ac_charging_state", "State B2 - Connected")
    await hass.async_block_till_done()
    async_mock_service(hass, "button", "press")
    notes: list[tuple[float, str]] = []
    controller.set_start_cap(lambda: None, record=lambda amps, reason: notes.append((amps, reason)))
    assert await controller.async_start()
    assert number, "the start is a current write"
    assert notes and notes[-1] == (float(number[-1].data["value"]), WRITE_SESSION_START)
    await controller.async_shutdown()


# -- (a) and (b) on a site near its fuse


async def _at_ten(hass: HomeAssistant, monkeypatch, freezer, entry_id: str):
    """The HALO behind its 20 A fuse, asking for 16 A, held at 10 A by an overload on L3 (house 9.6 A)."""
    halo = await _halo(hass, monkeypatch, entry_id)
    await halo.start()
    halo.switch("on")
    await hass.async_block_till_done()
    freezer.tick(timedelta(seconds=15))
    halo.reads(13.0)
    _site_a(hass, halo.site, 14.63, 13.92, 22.6)
    await halo.tick()
    assert halo.written()[-1] == "1.10,2.10"
    freezer.tick(timedelta(seconds=15))
    halo.reads(10.0)
    _site_a(hass, halo.site, 11.63, 10.92, 19.6)
    await halo.tick()
    assert halo.written()[-1] == "1.10,2.10"
    assert halo.cc.requested_current_a == 16
    return halo


async def _draws(halo, freezer, amps: float) -> None:
    """The car follows the charger's limit, and the charger's own reading shows it."""
    freezer.tick(timedelta(seconds=15))
    halo.reads(amps)
    _site_a(halo.hass, halo.site, 1.63 + amps, 0.92 + amps, 9.6 + amps)
    await halo.tick()


def _uncapped(halo, *, told: bool) -> None:
    """The site's allowance cannot be read at the moment of the write (a meter between two readings), so the write
    goes out at the full request; `told`: whether the site is told of it."""
    controller, charger_id = halo.controller, halo.charger.entry_id
    halo.cc.set_start_cap(
        lambda: None,
        record=(lambda amps, reason: controller.note_charger_write(charger_id, amps, reason)) if told else None,
    )


async def test_a_resend_at_full_current_is_lowered_by_the_next_overload(hass: HomeAssistant, monkeypatch, freezer) -> None:
    halo = await _at_ten(hass, monkeypatch, freezer, "blip_full")
    _uncapped(halo, told=True)
    await halo.cc._async_resend_current()  # noqa: SLF001
    assert halo.written()[-1] == "1.16,2.10"
    damper = halo.controller._dampers[halo.charger.entry_id]  # noqa: SLF001
    assert damper.last_written_a == 16.0, "the site knows what is on the charger"
    # L3 at 25.6 A: the overload's proposal is 10 A, the regulator's old value; it lowers the charger at once.
    await _draws(halo, freezer, 16.0)
    assert halo.written()[-1] == "1.10,2.10"
    assert [entry for entry in halo.log() if entry["outcome"] == "wrote"][-1]["from_a"] == 16


async def test_a_deferred_session_write_at_full_current_is_lowered_by_the_next_overload(
    hass: HomeAssistant, monkeypatch, freezer
) -> None:
    halo = await _at_ten(hass, monkeypatch, freezer, "deferred_full")
    _uncapped(halo, told=True)
    await halo.cc._async_write_after_start()  # noqa: SLF001
    assert halo.written()[-1] == "1.16,2.10"
    await _draws(halo, freezer, 16.0)
    assert halo.written()[-1] == "1.10,2.10"


async def test_a_write_the_site_was_never_told_of_is_still_lowered_on_the_chargers_own_reading(
    hass: HomeAssistant, monkeypatch, freezer
) -> None:
    """The defence in depth: the damper still holds 10 A, but the charger reads 16 A on a fresh reading."""
    halo = await _at_ten(hass, monkeypatch, freezer, "untold")
    _uncapped(halo, told=False)
    await halo.cc._async_resend_current()  # noqa: SLF001
    assert halo.written()[-1] == "1.16,2.10"
    damper = halo.controller._dampers[halo.charger.entry_id]  # noqa: SLF001
    assert damper.last_written_a == 10.0
    await _draws(halo, freezer, 16.0)
    assert halo.written()[-1] == "1.10,2.10"


async def test_a_resend_on_a_site_near_its_fuse_goes_out_at_what_the_site_allows(
    hass: HomeAssistant, monkeypatch, freezer
) -> None:
    halo = await _at_ten(hass, monkeypatch, freezer, "blip_capped")
    halo.configure.clear()
    await halo.cc._async_resend_current()  # noqa: SLF001
    # L3 has 0.4 A left with the car's 10 A on it: 10 A, never the 16 A asked for.
    assert halo.written() == ["1.10,2.10"]


async def test_a_slow_charger_after_the_regulators_cut_is_not_raised_by_the_check(
    hass: HomeAssistant, monkeypatch, freezer
) -> None:
    """The check never writes above what was written: a charger still drawing 16 A after a cut to 10 A (a car slow to
    follow) is held at 10 A, not raised to what the overload alone would allow."""
    halo = await _at_ten(hass, monkeypatch, freezer, "slow")
    halo.configure.clear()
    freezer.tick(timedelta(seconds=15))
    halo.reads(13.0)
    _site_a(hass, halo.site, 14.63, 13.92, 21.0)  # 1 A over: 13 + -1 = 12 A, above the 10 A written
    await halo.tick()
    assert all(value == "1.10,2.10" for value in halo.written()), halo.written()
