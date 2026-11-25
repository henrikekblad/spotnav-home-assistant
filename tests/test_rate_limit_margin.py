"""Room kept free after a write to a rate-limited charger (`regulator.rate_limit_margin_a`): beta reports showed
`safety_stop: rate_limited` once or twice a day on an Alfen over modbus and twice in a night on a Peblar, each a house
load that stepped up right after a current write the charger takes only every 10 s. A write to such a charger now
keeps `RATE_LIMIT_MARGIN_W` (some 6.5 A at 230 V) free on every phase it draws on, so a step within it fits until the
next write is allowed; a larger one still stops the charge.

The end-to-end cases drive the night of 2026-11-23 (`test_field_safety_stop_23_november.py`): an Alfen over modbus,
a 22 A limit, a pass every five seconds."""

from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant

from custom_components.spotnav.site.regulator import (
    RATE_LIMIT_MARGIN_W,
    _decide_phase,
    rate_limit_margin_a,
)
from custom_components.spotnav.site.site_capacity import PhaseValue

from .test_field_safety_stop_23_november import _BATTERY_RISES, _BATTERY_STARTS, _HOUSE, _the_night


def test_the_margin_is_kept_for_a_charger_with_an_interval_only() -> None:
    assert rate_limit_margin_a(10.0) == pytest.approx(RATE_LIMIT_MARGIN_W / 230.0)
    assert 6.0 < rate_limit_margin_a(10.0) < 7.0
    assert rate_limit_margin_a(60.0) == rate_limit_margin_a(10.0)
    # A short interval lets the next pass lower in time; OCPP's must-lower is read back, never held by its budget.
    assert rate_limit_margin_a(0.0) == 0.0 and rate_limit_margin_a(2.0) == 0.0
    assert rate_limit_margin_a(10.0, confirms_must_lower=True) == 0.0


def _phase(headroom: float, current: float, requested: float, margin: float, min_a: float = 6.0) -> tuple[float | None, str]:
    return _decide_phase(
        safe_uncredited_headroom_a=headroom,
        signed_active_power_w=3000.0,
        signed_active_power_age_s=1.0,
        signed_active_power_reason=None,
        voltage_v=230.0,
        requested_current_a=requested,
        measured_current=PhaseValue(current, 1.0, None),
        confirmed_dir="net_import",
        max_age_s=60.0,
        zero_margin_w=500.0,
        help_margin_factor=0.5,
        min_current_a=min_a,
        write_margin_a=margin,
    )


def test_a_raise_stops_short_of_the_fuse_by_the_margin_and_a_lowering_goes_further() -> None:
    margin = rate_limit_margin_a(10.0)
    # Room for 10 A more at 6 A: without the margin 16, with it 6 + 10 - 6.5 -> 9.
    assert _phase(10.0, 6.0, 16.0, 0.0) == (16.0, "increase_within_safe_uncredited_margin")
    assert _phase(10.0, 6.0, 16.0, margin) == (9.0, "increase_within_safe_uncredited_margin")
    # Already within the margin: held where it is, never lowered by it.
    assert _phase(3.0, 10.0, 16.0, margin)[0] == 10.0
    # Not drawing yet, room for 11 A: started at 6 A rather than kept from starting by the margin.
    assert _phase(11.0, 0.0, 16.0, margin) == (6.0, "increase_within_safe_uncredited_margin")
    # An overload of 2 A at 16 A: 14 without the margin, 7 with it, the minimum at most.
    assert _phase(-2.0, 16.0, 16.0, 0.0) == (14.0, "reducing_current_due_to_active_import_overload")
    assert _phase(-2.0, 16.0, 16.0, margin) == (7.0, "reducing_current_due_to_active_import_overload")
    assert _phase(-5.0, 12.0, 16.0, margin) == (6.0, "reducing_current_due_to_active_import_overload")
    # Below the minimum the fuse alone pauses, as without the margin.
    assert _phase(-8.0, 12.0, 16.0, margin) == _phase(-8.0, 12.0, 16.0, 0.0)


@pytest.mark.parametrize("signed", [False, True], ids=["direction_unknown", "signed_site"])
async def test_the_night_s_step_right_after_a_write_fits_without_a_safety_stop(
    hass: HomeAssistant, monkeypatch, freezer, signed: bool
) -> None:
    night = await _the_night(hass, monkeypatch, freezer, f"margin{int(signed)}", signed=signed, margin=True)
    stops, writes = night.stops, len(night.writes)
    # The battery's grid charge, then five seconds later more: the second step inside the charger's 10 s.
    night.house = _BATTERY_STARTS
    await night.step()
    night.house = _BATTERY_RISES
    for _ in range(6):
        await night.step()
    assert night.stops == stops and night.on, night.log()
    # One cut, deep enough for both steps: the charger's minimum.
    assert night.writes[writes:] == [6], f"{night.writes}\n{night.log()}"


async def test_a_step_after_the_charger_s_interval_is_lowered_as_before(hass: HomeAssistant, monkeypatch, freezer) -> None:
    night = await _the_night(hass, monkeypatch, freezer, "after", amps=16, margin=True)
    stops, writes = night.stops, len(night.writes)
    night.house = (7.5, 1.2, 1.0)
    await night.step()
    first = night.writes[writes:]
    assert len(first) == 1 and 6 <= first[0] <= 8, f"{night.writes}\n{night.log()}"
    # The interval is over: the next step is lowered by a write, as without the margin.
    await night.run(15.0)
    night.house = (15.5, 1.2, 1.0)
    for _ in range(4):
        await night.step()
    assert night.stops == stops and night.on, night.log()
    assert night.writes[-1] == 6 and night.car_a() == 6.0, f"{night.writes}\n{night.log()}"


async def test_a_step_beyond_the_margin_inside_the_interval_still_stops_the_charge(
    hass: HomeAssistant, monkeypatch, freezer
) -> None:
    night = await _the_night(hass, monkeypatch, freezer, "beyond", amps=16, margin=True)
    writes = len(night.writes)
    # A first step: lowered with the margin kept (16 -> about 8); then 8 A more within the 10 s: past the margin, and
    # the lowering it needs (to 6 A) is not taken yet, so the charge is stopped, the last resort as before.
    await night.overloaded_until_stopped(((7.5, 1.2, 1.0), (15.5, 1.2, 1.0)))
    assert len(night.writes) == writes + 1 and 6 < night.writes[-1] <= 8, f"{night.writes}\n{night.log()}"


async def test_without_the_margin_the_night_s_step_stops_the_charge(hass: HomeAssistant, monkeypatch, freezer) -> None:
    night = await _the_night(hass, monkeypatch, freezer, "before")
    await night.overloaded_until_stopped((_BATTERY_STARTS, _BATTERY_RISES))
    night.house = _HOUSE
