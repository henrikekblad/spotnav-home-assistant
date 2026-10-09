"""A plan charge load balancing paused while the home battery charges from the grid: the field night of 2026-10-09.

A Charge Amps HALO on OCPP (3 phases, 16 A asked for) behind a 20 A main fuse with no safety margin, the site
derived from a Sigenergy meter, a Sigenergy battery that plans its own grid charge in the same cheap quarter-hours,
`car_first` with yield stepping on. 02:15:00 the plan's window started the car at 16 A; 02:15:07 the battery began
its own grid charge (4.7 kW, then 12.2 kW) and the site rose to the fuse; 02:15:12-17 protection stepped the car down
and paused it. Then nothing for two and a half hours: the HALO sends no meter values while no transaction runs, so its
current read the same 0 A, unchanged and unreported, older than the site's 120 s bound, and every regulator decision
was "charger_measurement_unusable". The balancing resume and the battery probe both sat behind that decision, and the
probe also refused a grid the battery held a few tenths over the fuse (an overload decision, though within the band
the battery's own regulation is allowed). At 04:45 the battery was done and the car charged at once.

Driven through `SiteCapacityController` with the real regulator, explicit passes, and the stepper's, the damper's
and the dwell's clocks moved by hand (`tests/test_lb_start_credit.py`'s HALO world, with the battery).
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from homeassistant.core import HomeAssistant

from custom_components.spotnav.site.battery_probe import PROBE_BACKOFF_INITIAL_S, PROBE_BACKOFF_MAX_S
from custom_components.spotnav.site.site_capacity import DirectPhaseMeasurement

from .test_lb_start_credit import _halo, _Halo, _site_a

_BATTERY_SUFFIX = "battery_power"
_V = 230.0
_FUSE_A = 20.0


def _battery_entity(entry_id: str) -> str:
    return f"sensor.{entry_id}_{_BATTERY_SUFFIX}"


def _battery_w(halo: _Halo, watts: float) -> None:
    halo.hass.states.async_set(
        _battery_entity(halo.controller.entry_id.removesuffix("_site")), str(round(watts)), {"unit_of_measurement": "W"}
    )


def _silent_while_idle(monkeypatch, halo: _Halo) -> None:
    """The HALO sends no meter values while no transaction runs: its current stays the last value, unchanged and
    unreported, older than the site's 120 s bound, until the charger charges again."""
    read = type(halo.controller)._read_charger_measured_current  # noqa: SLF001

    def silent(self, wiring):
        values = read(self, wiring)
        if values is None or halo.cc.charging:
            return values
        return DirectPhaseMeasurement(
            l1=replace(values.l1, age_s=630.0, report_age_s=630.0),
            l2=replace(values.l2, age_s=630.0, report_age_s=630.0),
            l3=replace(values.l3, age_s=630.0, report_age_s=630.0),
        )

    monkeypatch.setattr(type(halo.controller), "_read_charger_measured_current", silent)


async def _night(hass: HomeAssistant, monkeypatch, entry_id: str = "night", *, battery_w: float = 4714.0) -> _Halo:
    """The owner's site at 02:15, the car plugged in, not charging; the battery begins to charge from the grid."""
    hass.states.async_set(_battery_entity(entry_id), str(round(battery_w)), {"unit_of_measurement": "W"})
    return await _halo(hass, monkeypatch, entry_id, l1=2.0, l2=0.9, l3=0.8, battery_entity=_battery_entity(entry_id))


async def _paused_by_the_battery(halo: _Halo) -> None:
    """02:15: the plan's window starts the car, the battery takes the fuse on top, and protection pauses the car.
    Afterwards the charger is off and reads nothing, and the battery holds the grid a few tenths over the fuse."""
    await halo.start()
    halo.switch("on")
    halo.reads(5.7)
    await halo.hass.async_block_till_done()
    _battery_w(halo, 12159.0)
    _site_a(halo.hass, halo.site, 25.7, 25.3, 24.5)
    await halo.tick()
    assert len(halo.turn_off) == 1, "far over the fuse: paused at once, as ever"
    halo.switch("off")
    halo.reads(0.0)
    _battery_w(halo, 12256.0)
    _site_a(halo.hass, halo.site, 20.2, 20.1, 20.0)
    await halo.hass.async_block_till_done()
    assert halo.cc.paused_by_balancing


def _outcomes(halo: _Halo, prefix: str = "probe_") -> list[str]:
    return [entry["outcome"] for entry in halo.log() if entry["outcome"].startswith(prefix)]


# -- 1. a paused plan charge is retried at the minimum while the battery holds the fuse


@pytest.mark.parametrize("reading", ["silent", "fresh"])
async def test_the_night_of_9_october_the_paused_charge_is_probed_and_the_battery_yields(
    hass: HomeAssistant, monkeypatch, reading: str
) -> None:
    halo = await _night(hass, monkeypatch, f"night{reading}")
    await _paused_by_the_battery(halo)
    if reading == "silent":
        _silent_while_idle(monkeypatch, halo)
    starts = len(halo.turn_on)

    halo.advance(5.0)
    await halo.tick()
    decision = halo.controller.regulator_decisions[halo.charger.entry_id]
    assert decision.reason == (
        "charger_measurement_unusable" if reading == "silent" else "reducing_current_due_to_active_import_overload"
    )
    # The probe: the car started again at its minimum, the plan's 16 A kept on record.
    assert len(halo.turn_on) == starts + 1, "the paused charge is tried again"
    assert halo.written()[-1] == "1.6,2.10"
    assert halo.cc.requested_current_a == 16
    assert _outcomes(halo) == ["probe_started"]

    # The car draws its minimum and the battery gives the same up: the grid stays at the fuse.
    halo.switch("on")
    halo.reads(5.7)
    _battery_w(halo, 12256.0 - 3 * _V * 5.7)
    _site_a(hass, halo.site, 20.1, 20.0, 20.0)
    await hass.async_block_till_done()
    halo.advance(31.0)
    await halo.tick()
    assert _outcomes(halo) == ["probe_started", "probe_succeeded"]
    assert len(halo.turn_off) == 1, "the car stays on"


async def test_the_car_climbs_after_the_probe_while_the_battery_gives_way(hass: HomeAssistant, monkeypatch) -> None:
    halo = await _night(hass, monkeypatch, "nightclimb")
    await _paused_by_the_battery(halo)
    _silent_while_idle(monkeypatch, halo)
    halo.advance(5.0)
    await halo.tick()
    assert _outcomes(halo) == ["probe_started"]

    # A battery that holds the grid at the fuse: whatever the car takes, it gives up (up to its own 12.3 kW).
    house_a = {"L1": 2.0, "L2": 0.9, "L3": 0.8}
    battery_max_a = 12256.0 / (3 * _V)
    halo.switch("on")
    await hass.async_block_till_done()
    highest = 0
    for _ in range(80):
        assigned = int(halo.controller._dampers[halo.charger.entry_id].last_written_a or 0)  # noqa: SLF001
        car_a = 0.95 * assigned if halo.cc.charging else 0.0
        battery_a = max(0.0, min(battery_max_a, _FUSE_A + 0.05 - max(house_a.values()) - car_a))
        halo.reads(car_a)
        _battery_w(halo, battery_a * 3 * _V)
        _site_a(hass, halo.site, *(house_a[phase] + battery_a + car_a for phase in ("L1", "L2", "L3")))
        await hass.async_block_till_done()
        halo.advance(31.0)
        await halo.tick()
        highest = max(highest, int(halo.controller._dampers[halo.charger.entry_id].last_written_a or 0))  # noqa: SLF001
    assert len(halo.turn_off) == 1, "never paused again"
    assert highest >= 12, f"the car climbs on what the battery gives up, reached {highest} A"


@pytest.mark.parametrize(
    ("case", "battery_w", "site_a"),
    [
        # No battery charging: the house itself is over the fuse.
        ("battery_idle", 0.0, (20.2, 20.1, 20.0)),
        # A battery too small to give way to the car's minimum (6 A x 3 x 230 V = 4.1 kW).
        ("battery_small", 3000.0, (20.2, 20.1, 20.0)),
        # Over the fuse beyond the half-amp band the battery's own regulation is allowed.
        ("over_the_band", 12256.0, (21.0, 20.1, 20.0)),
    ],
)
@pytest.mark.parametrize("reading", ["silent", "fresh"])
async def test_no_retry_on_an_overload_that_is_not_a_yielding_battery(
    hass: HomeAssistant, monkeypatch, case: str, battery_w: float, site_a: tuple[float, float, float], reading: str
) -> None:
    halo = await _night(hass, monkeypatch, f"nightno{case.replace('_', '')}{reading}")
    await _paused_by_the_battery(halo)
    if reading == "silent":
        _silent_while_idle(monkeypatch, halo)
    _battery_w(halo, battery_w)
    _site_a(hass, halo.site, *site_a)
    await hass.async_block_till_done()
    starts = len(halo.turn_on)
    for _ in range(4):
        halo.advance(61.0)
        await halo.tick()
    assert len(halo.turn_on) == starts, case
    assert _outcomes(halo) == []
    assert halo.cc.paused_by_balancing, "still held back, for when there is room"


async def test_a_battery_that_does_not_yield_is_retried_every_quarter_hour_at_most(
    hass: HomeAssistant, monkeypatch
) -> None:
    assert (PROBE_BACKOFF_INITIAL_S, PROBE_BACKOFF_MAX_S) == (600.0, 900.0)
    halo = await _night(hass, monkeypatch, "nightstuck")
    await _paused_by_the_battery(halo)
    _silent_while_idle(monkeypatch, halo)

    backoffs: list[float] = []
    for attempt in range(4):
        halo.advance(5.0)
        await halo.tick()
        assert _outcomes(halo).count("probe_started") == attempt + 1, attempt
        # The battery does not move: the car's minimum lands on top of the fuse.
        halo.switch("on")
        halo.reads(5.7)
        _site_a(hass, halo.site, 25.9, 25.8, 25.7)
        await hass.async_block_till_done()
        halo.advance(31.0)
        await halo.tick()
        assert len(halo.turn_off) == attempt + 2, "stopped again within the window"
        backoffs.append(halo.controller.battery_probe_snapshot[halo.charger.entry_id]["backoff_remaining_s"])
        halo.switch("off")
        halo.reads(0.0)
        _site_a(hass, halo.site, 20.2, 20.1, 20.0)
        await hass.async_block_till_done()
        await halo.tick()
        assert halo.cc.paused_by_balancing, "a failed probe is a balancing pause again"
        # Nothing before the back-off is over.
        halo.advance(backoffs[-1] - 10.0)
        await halo.tick()
        assert _outcomes(halo).count("probe_started") == attempt + 1
        halo.advance(5.0)
    assert [round(value, -1) for value in backoffs] == [600.0, 900.0, 900.0, 900.0]


async def test_the_retry_time_is_known_while_the_probe_backs_off(hass: HomeAssistant, monkeypatch) -> None:
    from homeassistant.util import dt as dt_util

    halo = await _night(hass, monkeypatch, "nightretry")
    await _paused_by_the_battery(halo)
    _silent_while_idle(monkeypatch, halo)
    pause = halo.controller.balancing_pause(halo.charger.entry_id)
    assert pause is not None and pause.retry_at is None and pause.cause == "battery_shares_fuse"

    halo.advance(5.0)
    await halo.tick()
    halo.switch("on")
    halo.reads(5.7)
    _site_a(hass, halo.site, 25.9, 25.8, 25.7)
    await hass.async_block_till_done()
    halo.advance(31.0)
    await halo.tick()
    halo.switch("off")
    halo.reads(0.0)
    _site_a(hass, halo.site, 20.2, 20.1, 20.0)
    await hass.async_block_till_done()
    pause = halo.controller.balancing_pause(halo.charger.entry_id)
    assert pause is not None and pause.retry_at is not None
    assert abs((pause.retry_at - dt_util.utcnow()).total_seconds() - 600.0) < 5.0

    # The house, not a battery: the charge waits for room, with no time to name.
    _battery_w(halo, 0.0)
    await hass.async_block_till_done()
    assert halo.controller.balancing_pause(halo.charger.entry_id).cause == "house_consumption"

    # Not paused by balancing: nothing to say.
    halo.cc.forget_balancing_pause()
    assert halo.controller.balancing_pause(halo.charger.entry_id) is None


async def test_a_stale_idle_reading_no_longer_holds_back_the_resume_when_room_returns(
    hass: HomeAssistant, monkeypatch
) -> None:
    halo = await _night(hass, monkeypatch, "nightroom")
    await _paused_by_the_battery(halo)
    _silent_while_idle(monkeypatch, halo)
    starts = len(halo.turn_on)
    # 04:45: the battery is done and the house is quiet.
    _battery_w(halo, 1.0)
    _site_a(hass, halo.site, 1.8, 1.5, 4.5)
    await hass.async_block_till_done()
    await halo.tick()
    halo.advance(61.0)
    await halo.tick()
    assert len(halo.turn_on) == starts + 1
    assert halo.log()[-1]["outcome"] == "resumed"


# -- 2. a start while the battery charges from the grid goes in at the minimum


@pytest.mark.parametrize(
    ("battery_before", "battery_now", "written"),
    [
        (4714.0, 4714.0, "1.6,2.10"),  # charging from the grid
        (-200.0, 150.0, "1.6,2.10"),  # rising toward a charge
        (0.0, 0.0, "1.16,2.10"),  # idle: the whole request, as ever
        (-3000.0, -3000.0, "1.16,2.10"),  # discharging
    ],
)
async def test_a_start_goes_in_at_the_minimum_while_the_battery_charges_or_rises(
    hass: HomeAssistant, monkeypatch, battery_before: float, battery_now: float, written: str
) -> None:
    halo = await _night(hass, monkeypatch, f"nightstart{int(battery_before)}{int(battery_now)}".replace("-", "m"), battery_w=battery_before)
    await halo.tick()
    halo.advance(20.0)
    _battery_w(halo, battery_now)
    await hass.async_block_till_done()
    await halo.tick()
    await halo.start()
    assert halo.written() == [written]
    assert halo.cc.requested_current_a == 16, "the plan's current stays what the car climbs to"


async def test_without_yield_stepping_a_start_takes_what_the_site_allows(hass: HomeAssistant, monkeypatch) -> None:
    halo = await _night(hass, monkeypatch, "nightnoyield")
    halo.controller.config["yield_stepping_enabled"] = False
    await halo.start()
    assert halo.written() == ["1.16,2.10"]


async def test_the_first_write_after_a_start_waits_at_the_minimum_once_the_battery_charges(
    hass: HomeAssistant, monkeypatch
) -> None:
    # 02:15:00: the battery has not begun yet, so the start takes the whole 16 A.
    halo = await _night(hass, monkeypatch, "nightfirst", battery_w=0.0)
    await halo.start()
    assert halo.written() == ["1.16,2.10"]
    halo.switch("on")
    await hass.async_block_till_done()
    # 02:15:05: it has, at 4.7 kW, and the car does not draw yet: the regulator's first word is the minimum, not 16 A.
    _battery_w(halo, 4714.0)
    await hass.async_block_till_done()
    await halo.tick()
    assert halo.written()[-1] == "1.6,2.10"
    first = [entry for entry in halo.log() if entry["outcome"] == "wrote"]
    assert first and first[0]["to_a"] == 6


async def test_the_first_write_is_unchanged_with_the_battery_idle(hass: HomeAssistant, monkeypatch) -> None:
    halo = await _night(hass, monkeypatch, "nightfirstidle", battery_w=0.0)
    await halo.start()
    halo.switch("on")
    await hass.async_block_till_done()
    await halo.tick()
    assert halo.written()[-1] == "1.16,2.10"


# -- 3. protection on a reading that predates its own write


async def test_one_reading_lowers_the_car_once_not_once_a_pass(hass: HomeAssistant, monkeypatch) -> None:
    """02:15:12-13: one site reading of 22.6/23.6/23.6 A stepped the car 16 -> 15 -> 11 -> 7 within 0.6 s: each pass
    credited the car with no more than the current just written, which that reading, taken before it, could not show."""
    halo = await _night(hass, monkeypatch, "nightratchet", battery_w=0.0)
    await halo.start()
    assert halo.written() == ["1.16,2.10"]
    halo.switch("on")
    await hass.async_block_till_done()
    # The site shows the car (its own reading still 0) and a load on top: 3.6 A over the fuse.
    _site_a(hass, halo.site, 22.6, 23.6, 23.6)
    await halo.tick()
    after_first = list(halo.written())
    assert after_first[-1] == "1.12,2.10", "lowered at once by what the reading is over: 16 - 3.6"
    # More passes on that same reading, nothing reported since: no further step.
    for _ in range(3):
        await halo.tick()
    assert halo.written() == after_first
    # A new reading, still over: lowered again from there.
    _site_a(hass, halo.site, 21.0, 22.0, 22.0)
    await hass.async_block_till_done()
    await halo.tick()
    assert halo.written()[-1] == "1.10,2.10"


# -- 4. what the dashboard says of it


async def test_the_dashboard_knows_the_charge_load_balancing_holds_back(hass: HomeAssistant, monkeypatch) -> None:
    from custom_components.spotnav.api import dashboard as dashboard_api

    halo = await _night(hass, monkeypatch, "nightdash")
    assert dashboard_api.capture_site(hass, halo.charger.entry_id).balancing_paused is False
    await _paused_by_the_battery(halo)
    site = dashboard_api.capture_site(hass, halo.charger.entry_id)
    assert (site.balancing_paused, site.balancing_retry_at, site.balancing_cause) == (
        True,
        None,
        "battery_shares_fuse",
    )
