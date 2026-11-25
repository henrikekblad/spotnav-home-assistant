"""Room kept free after a write to a charger that load balancing had to stop because it would not take a lower current
yet (`regulator.rate_limit_margin_a`). Beta reports showed `safety_stop: rate_limited` once or twice a day on an Alfen
over modbus and twice in a night on a Peblar, each a house load that stepped up right after a current write the
charger takes only every 10 s. Once a charger has had such a stop, for a week each write keeps room free on every phase
it draws on, more for a longer lock (`rate_limit_margin_w`), so a step within it fits until the next write is allowed; a
larger one still stops the charge. A charger never so stopped charges exactly as before.

The end-to-end cases drive the night of 2026-11-23 (`test_field_safety_stop_23_november.py`): an Alfen over modbus,
a 22 A limit, a pass every five seconds."""

from __future__ import annotations

from datetime import timedelta

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.spotnav.diagnostics import _rate_limit_margin
from custom_components.spotnav.execution.controller import RATE_LIMITED_STOP_STORE_KEY
from custom_components.spotnav.site.regulator import (
    _decide_phase,
    RATE_LIMIT_MARGIN_DAYS,
    rate_limit_margin_a,
    rate_limit_margin_w,
)
from custom_components.spotnav.site.site_capacity import PhaseValue
from custom_components.spotnav.site.site_capacity_controller import SiteCapacityController

from .test_field_safety_stop_23_november import _BATTERY_RISES, _BATTERY_STARTS, _HOUSE, _the_night
from .world import controller_of


def test_the_margin_grows_with_the_charger_s_lock() -> None:
    assert rate_limit_margin_w(0.0) == 0.0 and rate_limit_margin_w(4.9) == 0.0
    assert rate_limit_margin_w(5.0) == 500.0 and rate_limit_margin_w(10.0) == 500.0
    assert rate_limit_margin_w(35.0) == pytest.approx(1000.0)
    assert rate_limit_margin_w(60.0) == 1500.0 and rate_limit_margin_w(90.0) == 1500.0 and rate_limit_margin_w(900.0) == 1500.0


def test_the_margin_holds_for_a_week_after_a_rate_limited_stop_only() -> None:
    now = dt_util.utcnow()
    assert rate_limit_margin_a(10.0, last_stop_at=None, now=now) == 0.0
    assert rate_limit_margin_a(10.0, last_stop_at=now - timedelta(days=1), now=now) == pytest.approx(500.0 / 230.0)
    assert rate_limit_margin_a(60.0, last_stop_at=now - timedelta(days=6, hours=23), now=now) == pytest.approx(1500.0 / 230.0)
    assert rate_limit_margin_a(10.0, last_stop_at=now - timedelta(days=RATE_LIMIT_MARGIN_DAYS), now=now) == 0.0
    # OCPP's must-lower is read back, never held by its budget.
    assert rate_limit_margin_a(10.0, last_stop_at=now, now=now, confirms_must_lower=True) == 0.0


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
    margin = 1500.0 / 230.0
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


async def test_a_charger_never_stopped_for_its_interval_charges_as_before(hass: HomeAssistant, monkeypatch, freezer) -> None:
    night = await _the_night(hass, monkeypatch, freezer, "nohistory", amps=16, margin=True)
    assert night.cc.rate_limited_stop_at is None
    assert SiteCapacityController._write_margin_a(night.cc) == 0.0  # noqa: SLF001
    assert _rate_limit_margin(night.cc) == {"last_rate_limited_stop": None, "active": False, "margin_a": 0.0}
    assert RATE_LIMITED_STOP_STORE_KEY not in night.cc._store_record()  # noqa: SLF001
    # A step: lowered exactly as far as the fuse needs (13 A on the measured draw), no room kept.
    writes = len(night.writes)
    night.house = (7.5, 1.2, 1.0)
    await night.step()
    assert night.writes[writes:] == [13], f"{night.writes}\n{night.log()}"


@pytest.mark.parametrize("signed", [False, True], ids=["direction_unknown", "signed_site"])
async def test_after_the_night_s_first_stop_the_same_step_is_cut_in_time(
    hass: HomeAssistant, monkeypatch, freezer, signed: bool
) -> None:
    night = await _the_night(hass, monkeypatch, freezer, f"second{int(signed)}", signed=signed, margin=True)
    cc = night.cc
    # No stop before: the night's first safety stop comes as it came, and is kept.
    await night.overloaded_until_stopped((_BATTERY_STARTS, _BATTERY_RISES))
    stopped_at = cc.rate_limited_stop_at
    assert stopped_at is not None
    await hass.async_block_till_done()
    assert cc._store_record()[RATE_LIMITED_STOP_STORE_KEY] == stopped_at.isoformat()  # noqa: SLF001
    margin = _rate_limit_margin(cc)
    assert margin["active"] is True and margin["margin_a"] == pytest.approx(500.0 / 230.0, abs=0.01)
    # Resumed once the battery is done and the safety gap is over.
    night.house = _HOUSE
    await night.run(600.0)
    assert night.on and night.car_a() == 12.0, night.log()
    # The battery again: this time the first cut keeps room for the second step, and the charge runs on.
    stops, writes = night.stops, len(night.writes)
    night.house = _BATTERY_STARTS
    await night.step()
    night.house = _BATTERY_RISES
    for _ in range(6):
        await night.step()
    assert night.stops == stops and night.on, night.log()
    assert night.writes[writes:] == [6], f"{night.writes}\n{night.log()}"


async def test_the_rate_limited_stop_is_kept_through_a_restart_and_forgotten_after_a_week(
    hass: HomeAssistant, monkeypatch, freezer
) -> None:
    night = await _the_night(hass, monkeypatch, freezer, "restart", margin=True)
    await night.overloaded_until_stopped((_BATTERY_STARTS, _BATTERY_RISES))
    stopped_at = night.cc.rate_limited_stop_at
    assert stopped_at is not None
    await hass.async_block_till_done()
    assert await hass.config_entries.async_reload(night.entry_id)
    await hass.async_block_till_done()
    cc = controller_of(hass, night.entry_id)
    assert cc is not night.cc and cc.rate_limited_stop_at == stopped_at
    assert SiteCapacityController._write_margin_a(cc) > 0.0  # noqa: SLF001
    freezer.tick(timedelta(days=RATE_LIMIT_MARGIN_DAYS) + timedelta(seconds=1))
    assert SiteCapacityController._write_margin_a(cc) == 0.0  # noqa: SLF001
    assert _rate_limit_margin(cc)["active"] is False


async def _stopped_before(hass: HomeAssistant, monkeypatch, freezer, name: str):
    night = await _the_night(hass, monkeypatch, freezer, name, amps=16, margin=True)
    night.cc._rate_limited_stop_at = dt_util.utcnow() - timedelta(days=1)  # noqa: SLF001
    return night


async def test_a_step_within_the_margin_right_after_a_write_fits(hass: HomeAssistant, monkeypatch, freezer) -> None:
    night = await _stopped_before(hass, monkeypatch, freezer, "within")
    stops, writes = night.stops, len(night.writes)
    night.house = (7.5, 1.2, 1.0)
    await night.step()
    assert night.writes[writes:] == [11], f"{night.writes}\n{night.log()}"  # 13 less the 2.2 A kept
    # 1.5 A more within the charger's 10 s: within the room kept.
    night.house = (9.0, 1.2, 1.0)
    for _ in range(4):
        await night.step()
    assert night.stops == stops and night.on and night.writes[writes:] == [11], f"{night.writes}\n{night.log()}"


async def test_a_step_after_the_charger_s_interval_is_lowered_as_before(hass: HomeAssistant, monkeypatch, freezer) -> None:
    night = await _stopped_before(hass, monkeypatch, freezer, "after")
    stops, writes = night.stops, len(night.writes)
    night.house = (7.5, 1.2, 1.0)
    await night.step()
    assert night.writes[writes:] == [11], f"{night.writes}\n{night.log()}"
    await night.run(15.0)
    night.house = (13.0, 1.2, 1.0)
    for _ in range(4):
        await night.step()
    assert night.stops == stops and night.on, night.log()
    assert night.writes[-1] < 11, f"{night.writes}\n{night.log()}"


async def test_a_step_beyond_the_margin_inside_the_interval_still_stops_the_charge(
    hass: HomeAssistant, monkeypatch, freezer
) -> None:
    night = await _stopped_before(hass, monkeypatch, freezer, "beyond")
    writes = len(night.writes)
    # 5.5 A more within the 10 s: past the room kept, and the lowering it needs is not taken yet: the last resort.
    await night.overloaded_until_stopped(((7.5, 1.2, 1.0), (13.0, 1.2, 1.0)))
    assert night.writes[writes:] == [11], f"{night.writes}\n{night.log()}"
