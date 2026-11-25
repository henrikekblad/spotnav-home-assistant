"""A plan's charge stopped for safety is resumed when the room comes back: the night of 2026-11-23 (times UTC).

A user's debug report. An Alfen over modbus (a current number the charger takes at most every 10 s), three phases,
minimum 6 A, the plan asking 12 A, behind a 25 A main fuse with a 3 A margin (a 22 A limit). The site's phases are
magnitudes with no direction known, so load balancing lowers an overload by |I|. A home battery began its own grid
charge at 01:00:

    01:00:09  wrote 12 -> 7 (lowering_overload_on_current_magnitude_direction_unknown), site L1 25.35 A
    01:00:15  site L1 27.08 A, the car's lower current not yet shown; a write of 6 A is due 6 s after the last, which
              the charger's 10 s allows not yet: rate limited, so the charge is stopped (`safety_stop`)
    01:20     the site's L1 back at about 1 A; from 02:40 the battery done

Until 2.5.7 a safety stop held back only a person's charge: the plan's charge was left stopped, nothing held back for
the regulator to resume, until the next window's start at 04:30. Now the plan's charge is held back as a person's is:
resumed by the regulator once the safety gap (`SAFETY_RESUME_GAP_S`) is over, with every phase the charger draws on
measured and room for its minimum and one amp more through the dwell, within the limit on resumes (two in ten minutes,
then a 15-minute back-off).

Driven through `SiteCapacityController` with the real regulator and the real charger controller, a pass every five
seconds; the readings are made up in the shape of that night's, every reading wobbling a little as a meter's does.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.util import dt as dt_util

from custom_components.spotnav.const import (
    CONF_CHARGER_PLATFORM,
    CONF_GRID_POWER_SOURCE,
    CONF_SITE_CURRENT_SIGNED,
    CURRENT_CONTROL_NUMBER,
    REGULATOR_DWELL_S,
)
from custom_components.spotnav.core.ownership import SAFETY_RESUME_GAP_S
from custom_components.spotnav.site.site_capacity_controller import DIRECTION_UNKNOWN_REASONS, RESUME_BACKOFF_S

from .helpers import install_schedule, make_entry, make_site_entry, set_current_sensor
from .test_direct_signed_active_control import _wiring
from .world import controller_of

_V = 230.0
_STEP_S = 5.0
_AMPERE = {"unit_of_measurement": "A", "min": 0, "max": 16, "step": 1}
#: The house before the battery: room for the plan's 12 A on every phase.
_HOUSE = (1.4, 1.2, 1.0)
#: The battery's grid charge on L1: over the 22 A limit with the car at 12 A, a cut to 8 A...
_BATTERY_STARTS = (13.5, 1.2, 1.0)
#: ...and five seconds later more, which needs 6 A: a second write the charger takes only 10 s after the first.
_BATTERY_RISES = (15.0, 1.2, 1.0)
#: Far over the limit: no room for the car's minimum, the below-floor pause.
_BATTERY_FULL = (19.0, 1.2, 1.0)
_WOBBLE = (0.03, 0.07, -0.05, 0.04, 0.02, 0.06, -0.03, 0.05)


class _Night:
    """The site, the car at an Alfen over modbus and the house, a pass every five seconds."""

    def __init__(self, hass: HomeAssistant, freezer, name: str, signed: bool, amps: int = 12) -> None:
        self.hass = hass
        self.amps = amps
        self.freezer = freezer
        self.name = name
        self.signed = signed
        self.charger = f"{name}_charger"
        self.site = f"{name}_site"
        self.house = _HOUSE
        self.wobble = 0
        self.writes: list[int] = []
        self.starts = 0
        self.stops = 0
        self.trace: list[str] = []
        self.cc = None
        self.sc = None
        self.entry_id = ""
        hass.states.async_set(f"switch.{self.charger}", "off")
        hass.states.async_set(f"number.{self.charger}_limit", str(amps), _AMPERE)

    @property
    def on(self) -> bool:
        return self.hass.states.get(f"switch.{self.charger}").state == "on"

    @property
    def limit(self) -> int:
        return int(float(self.hass.states.get(f"number.{self.charger}_limit").state))

    def serve(self) -> None:
        """The charger's services, registered after the integration's platforms (which bring Home Assistant's own)."""
        self.hass.services.async_register("number", "set_value", self._set_value)
        self.hass.services.async_register("switch", "turn_on", self._turn_on)
        self.hass.services.async_register("switch", "turn_off", self._turn_off)

    async def _set_value(self, call: ServiceCall) -> None:
        self.writes.append(int(call.data["value"]))
        self.hass.states.async_set(call.data["entity_id"], str(call.data["value"]), _AMPERE)

    async def _turn_on(self, call: ServiceCall) -> None:
        self.starts += 1
        self.hass.states.async_set(f"switch.{self.charger}", "on")

    async def _turn_off(self, call: ServiceCall) -> None:
        self.stops += 1
        self.hass.states.async_set(f"switch.{self.charger}", "off")

    def car_a(self) -> float:
        return float(min(self.limit, self.amps)) if self.on else 0.0

    async def step(self) -> None:
        hass = self.hass
        self.wobble = (self.wobble + 1) % len(_WOBBLE)
        wobble = _WOBBLE[self.wobble]
        car = self.car_a()
        for suffix in ("l1", "l2", "l3"):
            set_current_sensor(hass, f"sensor.{self.charger}_{suffix}", round(car * 0.97 + wobble / 4, 2) if car else 0)
        site = [round(house + car + wobble, 2) for house in self.house]
        for suffix, amps in zip(("l1", "l2", "l3"), site, strict=True):
            set_current_sensor(hass, f"sensor.{self.site}_{suffix}", amps)
        hass.states.async_set(
            f"sensor.{self.site}_grid_power", str(round(sum(site) * _V, 1)), {"unit_of_measurement": "W"}
        )
        await hass.async_block_till_done()
        await self.sc._async_apply_active_control()  # noqa: SLF001
        await hass.async_block_till_done()
        decision = self.sc.regulator_decisions.get(self.entry_id)
        self.trace.append(
            f"car {car:g} site {site} on {self.on} | {getattr(decision, 'reason', None)}"
            f" {getattr(decision, 'proposed_current_a', None)}"
        )
        self.freezer.tick(timedelta(seconds=_STEP_S))

    async def run(self, seconds: float) -> None:
        for _ in range(int(seconds / _STEP_S)):
            await self.step()

    def log(self) -> str:
        return "\n".join(self.trace[-30:])

    async def overloaded_until_stopped(self, loads: tuple[tuple[float, float, float], ...]) -> None:
        """The house takes `loads` one after the other, a pass each, then the last until the charge is stopped."""
        stops = self.stops
        for load in loads:
            self.house = load
            await self.step()
        for _ in range(6):
            if self.stops > stops:
                break
            await self.step()
        assert self.stops == stops + 1 and not self.on, self.log()


async def _the_night(
    hass: HomeAssistant, monkeypatch, freezer, name: str, *, signed: bool = False, amps: int = 12, margin: bool = False
) -> _Night:
    """The plan's window is open (22:45 to 04:30) and its charge runs at `amps` (12 A that night). Without `margin`,
    load balancing as it was that night: no room kept free after a write to the rate-limited charger
    (`regulator.RATE_LIMIT_MARGIN_W`), so the night's safety stop comes as it came."""
    from custom_components.spotnav.execution.chargers.adapter import ChargerAdapter
    from custom_components.spotnav.site import regulator

    monkeypatch.setattr(ChargerAdapter, "vehicle_connected", lambda self: True)
    if not margin:
        monkeypatch.setattr(regulator, "RATE_LIMIT_MARGIN_W", 0.0)
    night = _Night(hass, freezer, name, signed, amps)
    entry = make_entry(
        hass,
        entry_id=night.charger,
        charge_control=f"switch.{night.charger}",
        current_limit=f"number.{night.charger}_limit",
        webhook_id=f"webhook-{name}",
        title="Alfen",
        current_control=CURRENT_CONTROL_NUMBER,
        extra={CONF_CHARGER_PLATFORM: "alfen_modbus"},
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    night.serve()
    night.entry_id = entry.entry_id
    cc = controller_of(hass, entry.entry_id)
    assert cc.adapter.policy.min_interval_s == 10.0
    night.cc = cc
    extra = (
        {CONF_SITE_CURRENT_SIGNED: True, CONF_GRID_POWER_SOURCE: {"power": f"sensor.{night.site}_grid_power"}}
        if signed
        else None
    )
    site_entry = make_site_entry(
        hass,
        entry_id=night.site,
        main_fuse_a=25.0,
        safety_margin_a=3.0,
        charger_entry_ids=[entry.entry_id],
        phase_wiring={entry.entry_id: _wiring(night.charger)},
        active_control_enabled=True,
        extra_data=extra,
    )
    for suffix in ("l1", "l2", "l3"):
        set_current_sensor(hass, f"sensor.{night.site}_{suffix}", _HOUSE[0])
        set_current_sensor(hass, f"sensor.{night.charger}_{suffix}", 0.0)
    hass.states.async_set(f"sensor.{night.site}_grid_power", "900", {"unit_of_measurement": "W"})
    assert await hass.config_entries.async_setup(site_entry.entry_id)
    await hass.async_block_till_done()
    night.sc = controller_of(hass, site_entry.entry_id)

    now = dt_util.utcnow()
    window = {"start": (now - timedelta(hours=2)).isoformat(), "end": (now + timedelta(hours=3)).isoformat()}
    await install_schedule(cc, {"periods": [window], "amps": amps})
    await hass.async_block_till_done()
    if not night.starts:
        assert await cc.async_start(amps=amps, cause="plan_window")
    await night.run(120.0)
    assert night.on and night.car_a() == float(amps), night.log()
    assert cc.session_runner.session.owner == "plan"
    return night


def _resumes_fed(night: _Night) -> int:
    return sum(1 for event in night.cc.session_runner.events if event["event"]["kind"] == "balancing_resume")


async def _stopped_for_safety(night: _Night) -> None:
    """01:00:09 the cut, 01:00:15 the second lowering the charger does not take yet: the safety stop."""
    writes = len(night.writes)
    await night.overloaded_until_stopped((_BATTERY_STARTS, _BATTERY_RISES))
    assert len(night.writes) == writes + 1, f"one cut, the second rate limited: {night.writes}\n{night.log()}"
    session = night.cc.session_runner.session
    assert session.balancing_paused and session.paused_origin == "plan" and session.held_for_safety, night.log() + str(night.writes)
    assert night.cc.paused_by_balancing and night.cc.paused_charge_origin == "plan_window"


@pytest.mark.parametrize("signed", [False, True], ids=["direction_unknown", "signed_site"])
async def test_the_plans_charge_stopped_for_safety_is_resumed_after_the_gap(
    hass: HomeAssistant, monkeypatch, freezer, signed: bool
) -> None:
    night = await _the_night(hass, monkeypatch, freezer, f"nov23{int(signed)}", signed=signed)
    cc = night.cc
    starts = night.starts
    await _stopped_for_safety(night)

    # The battery's grid charge ends a minute later: room for the car's 12 A on every phase from then on, and nothing
    # starts it within the five minutes of the safety gap.
    await night.run(60.0)
    night.house = _HOUSE
    await night.run(SAFETY_RESUME_GAP_S - 60.0 - 2 * _STEP_S)
    assert night.starts == starts and not night.on, night.log()
    assert cc.paused_by_balancing
    assert _resumes_fed(night) == 0, "the regulator does not ask for a resume the gap refuses, pass after pass"

    # Once the gap is over (the dwell ran within it): resumed, as the plan's charge, at its 12 A.
    await night.run(2 * _STEP_S + REGULATOR_DWELL_S)
    assert night.starts == starts + 1 and night.on, night.log()
    assert _resumes_fed(night) == 1
    assert cc.charge_origin == "plan_window"
    session = cc.session_runner.session
    assert session.owner == "plan" and not session.balancing_paused and not session.held_for_safety
    resumed = [
        entry
        for entry in night.sc.regulator_decision_log
        if entry["charger_id"] == night.entry_id and entry["outcome"] == "resumed"
    ]
    assert len(resumed) == 1
    if not signed:
        # The night's site: no direction known, so the resume reads the phases the charger draws on.
        assert resumed[0]["reason"] in DIRECTION_UNKNOWN_REASONS, resumed
    await night.run(120.0)
    assert night.starts == starts + 1 and night.on and night.car_a() == 12.0, night.log()


async def test_an_overload_that_keeps_coming_back_backs_the_resume_off_after_two(
    hass: HomeAssistant, monkeypatch, freezer
) -> None:
    """The safety stop, resumed after its gap; the battery takes the fuse again (the below-floor pause), resumed after
    the dwell; and again: a third resume waits out the 15-minute back-off, so the charger is not cycled."""
    night = await _the_night(hass, monkeypatch, freezer, "nov23again")
    starts = night.starts
    await _stopped_for_safety(night)
    night.house = _HOUSE
    await night.run(SAFETY_RESUME_GAP_S + REGULATOR_DWELL_S)
    assert night.starts == starts + 1 and night.on, night.log()

    for resumed in (2, 3):
        await night.overloaded_until_stopped((_BATTERY_FULL,))
        assert not night.cc.session_runner.session.held_for_safety, "a pause, not a safety stop"
        night.house = _HOUSE
        await night.run(REGULATOR_DWELL_S + 4 * _STEP_S)
        if resumed == 2:
            assert night.starts == starts + 2 and night.on, night.log()

    assert night.starts == starts + 2 and not night.on, "no third resume within ten minutes\n" + night.log()
    snapshot = night.sc.balancing_resume_snapshot()[night.entry_id]
    assert snapshot["backoff_remaining_s"] is not None, snapshot
    assert night.cc.paused_by_balancing
    await night.run(RESUME_BACKOFF_S - 12 * _STEP_S)
    assert night.starts == starts + 2, night.log()
    await night.run(24 * _STEP_S)
    assert night.starts == starts + 3 and night.on, "resumed once the back-off is over\n" + night.log()


async def test_a_second_safety_stop_in_the_plug_in_doubles_the_gap(hass: HomeAssistant, monkeypatch, freezer) -> None:
    """The safety stop, resumed after five minutes; the same overload again: the second safety stop of the plug-in is
    resumed no sooner than ten minutes after it, so a load that comes back every few minutes cannot cycle the charger
    every five."""
    night = await _the_night(hass, monkeypatch, freezer, "nov23twice")
    starts = night.starts
    await _stopped_for_safety(night)
    night.house = _HOUSE
    await night.run(SAFETY_RESUME_GAP_S + REGULATOR_DWELL_S)
    assert night.starts == starts + 1 and night.on, night.log()
    # The write interval has passed since the resume's start: the first cut goes out again, the second does not.
    await night.run(30.0)

    await _stopped_for_safety(night)
    assert night.cc.session_runner.session.safety_stops == 2
    night.house = _HOUSE
    await night.run(2 * SAFETY_RESUME_GAP_S - 2 * _STEP_S)
    assert night.starts == starts + 1 and not night.on, "not within the doubled gap\n" + night.log()
    assert _resumes_fed(night) == 1
    await night.run(2 * _STEP_S + REGULATOR_DWELL_S)
    assert night.starts == starts + 2 and night.on, night.log()


async def test_a_battery_probe_waits_out_the_safety_gap(hass: HomeAssistant, monkeypatch, freezer) -> None:
    """A charge held back while a home battery holds the grid at the fuse: within a safety stop's gap the battery probe
    does not try (the core would refuse its start, counted as a failed probe and backed off for many minutes); once
    the gap is over it probes (`tests/test_field_night_1_november.py`'s 03:18)."""
    from .test_field_night_1_november import _the_night as _halo_night

    night = await _halo_night(hass, monkeypatch, freezer, "nov23probe")
    cc = night.halo.cc
    night.battery = "fixed"
    night.fixed_w = 13000.0
    for _ in range(6):
        await night.step()
        if night.halo.turn_off:
            break
    await night.step()
    assert not night.on and cc.paused_by_balancing
    gap = {"running": True}
    monkeypatch.setattr(type(cc), "safety_gap_running", property(lambda self: gap["running"]))
    outcomes = len(night.outcomes())
    night.battery = "holds"
    await night.run(120.0)
    assert night.outcomes()[outcomes:] == [] and not night.on, night.outcomes()

    gap["running"] = False
    for _ in range(12):
        await night.step()
        if night.on:
            break
    assert night.outcomes()[outcomes:][:1] == ["probe_started:battery_charging_at_the_fuse"], night.outcomes()
    assert night.on


async def test_the_safety_gap_is_read_from_the_core_session(hass: HomeAssistant, monkeypatch, freezer) -> None:
    """`safety_gap_running` follows the core's gap: on from a safety stop, off once its gap is over."""
    night = await _the_night(hass, monkeypatch, freezer, "nov23gap")
    cc = night.cc
    assert not cc.safety_gap_running
    await _stopped_for_safety(night)
    assert cc.safety_gap_running
    stopped_at = cc.session_runner.session.safety_stopped_at
    freezer.move_to(stopped_at + timedelta(seconds=SAFETY_RESUME_GAP_S - 1))
    assert cc.safety_gap_running
    freezer.move_to(stopped_at + timedelta(seconds=SAFETY_RESUME_GAP_S))
    assert not cc.safety_gap_running
