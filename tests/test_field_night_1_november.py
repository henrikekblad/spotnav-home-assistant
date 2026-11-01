"""A plan charge and a home battery that charges from the grid and holds it at the fuse: the field night of 2026-11-01.

The owner's debug bundle (v8) and diagnostics of that morning. A Charge Amps HALO on OCPP (3 phases, minimum 6 A, the
plan asking 16 A) behind a 20 A main fuse with no safety margin, the site derived from a Sigenergy meter, a Sigenergy
battery (`car_first`, yield stepping on). The house drew about 1.9/0.8/0.8 A.

03:15 the battery began its own grid charge while the car ran at 16 A, and from then on held the grid at 19.9-20.2 A
per phase itself (its inverter fills each phase unevenly: 20.09/20.08/19.90 A with the car at 11.9 A), growing to
12.5 kW as the car was lowered: whatever the car freed, it took. SpotNav lowered the car amp by amp to its minimum and
paused it at 03:18:34 with the site at 20.09 A: an unverified battery has the fuse as its ceiling, and only a probe of
the paused charge could show that it gives way. The pause was then sent again and again for two minutes, on the
HALO's last reading from before it. 04:07 the battery stopped and the site fell to about 2 A; nothing resumed the
charge. The HALO's current read 6.02/5.86/5.98 A, its last meter value before the pause, unreported, the whole time,
and the resume and the probe both took that for a car still drawing. At 05:15 the next window's charge was paused,
the charger reported its charge once more before it answered, the window claimed that report as the plan's charge,
and the claim forgot that load balancing held it back: again no resume, for two hours, and a notice that the charge
"did not start as planned".

The HALO's charge control still reads on for a moment after a stop; after the stop its current reads the last value,
unreported, until it charges again.

Driven through `SiteCapacityController` with the real regulator and the real charger controller, a pass every five
seconds, the stepper's, the damper's and Home Assistant's clocks moved together (`tests/test_lb_start_credit.py`'s
HALO world, with the battery and a plan whose window is open). Every meter reading wobbles by a few hundredths, as the
owner's do: a reading that never changes is one the site discounts.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.spotnav.core.events import BalancingPause, CommandResult, Recheck
from custom_components.spotnav.core.ownership import decide
from custom_components.spotnav.core.session import ChargeSession
from custom_components.spotnav.execution import ownership_shadow
from custom_components.spotnav.site.battery_probe import PROBE_BACKOFF_INITIAL_S

from .helpers import install_schedule
from .test_balancing_retry import _battery_entity, _battery_w
from .test_lb_start_credit import _halo, _Halo, _site_a

_V = 230.0
#: The house that night (03:00-04:00), and at 05:15.
_HOUSE = (1.9, 0.82, 0.8)
_HOUSE_0515 = (1.75, 0.78, 4.8)
#: The Sigenergy's own grid limit, and its largest charge.
_BATTERY_HOLDS_A = 20.06
_BATTERY_MAX_W = 12500.0
_FUSE_A = 20.0
_STEP_S = 5.0
_WOBBLE = (0.03, 0.07, -0.09, 0.04, 0.02, 0.08, -0.03, 0.05)


class _Night:
    """The site, the car and the battery, a pass every five seconds."""

    def __init__(self, halo: _Halo, freezer, house: tuple[float, float, float]) -> None:
        self.halo = halo
        self.freezer = freezer
        self.house = house
        #: `idle`, `holds` (charges whatever the fuse leaves) or `fixed` (`fixed_w`, whatever the grid).
        self.battery = "idle"
        self.fixed_w = 0.0
        self.car_a = 0.0
        self.on = False
        self.offs = 0
        self.ons = 0
        self.wobble = 0
        self.battery_w = 0.0
        self.lowest_a: float | None = None
        self.highest_site_a = 0.0

    def assigned_a(self) -> float:
        written = self.halo.written()
        return float(written[-1].split(",")[0].split(".")[1]) if written else 16.0

    def battery_a(self, car_a: float) -> tuple[float, ...]:
        """The battery's charge per phase. Holding the grid, it fills every phase up to its own limit, up to its own
        maximum, a pass behind the car."""
        if self.battery == "idle":
            return (0.0, 0.0, 0.0)
        if self.battery == "fixed":
            return (self.fixed_w / (3 * _V),) * 3
        holds = _BATTERY_HOLDS_A + _WOBBLE[self.wobble]
        room = [max(0.0, holds - house - car_a) for house in self.house]
        scale = min(1.0, _BATTERY_MAX_W / (sum(room) * _V)) if sum(room) else 1.0
        return tuple(round(amps * scale, 3) for amps in room)

    async def step(self) -> None:
        halo = self.halo
        hass = halo.hass
        car_before = self.car_a
        self.wobble = (self.wobble + 1) % len(_WOBBLE)
        if len(halo.turn_off) > self.offs:
            self.offs = len(halo.turn_off)
            if self.on:
                # The charger answers the stop: its charge reported once more while its control still reads on,
                # then off. Its current stays what it last read before the stop (as the HALO's did at 03:18, 05:15
                # and 07:56).
                hass.states.async_set(f"switch.{halo.prefix}", "on", {"answer": self.offs})
                await hass.async_block_till_done()
                halo.switch("off")
                await hass.async_block_till_done()
                self.on = False
        if len(halo.turn_on) > self.ons:
            self.ons = len(halo.turn_on)
            if not self.on:
                halo.switch("on")
                self.on = True
        # The car follows what is written within a pass; the battery reacts to the car a pass later.
        self.car_a = min(self.assigned_a(), 16.0) if self.on else 0.0
        if self.on:
            halo.reads(round(self.car_a * 0.98 + _WOBBLE[self.wobble] / 4, 2))
            self.lowest_a = self.car_a if self.lowest_a is None else min(self.lowest_a, self.car_a)
        battery = self.battery_a(car_before)
        self.battery_w = sum(battery) * _V
        _battery_w(halo, self.battery_w)
        flicker = _WOBBLE[(self.wobble + 3) % len(_WOBBLE)] / 3
        site = [round(house + flicker + self.car_a + amps, 2) for house, amps in zip(self.house, battery, strict=True)]
        self.highest_site_a = max(self.highest_site_a, *site)
        _site_a(hass, halo.site, *site)
        for phase in ("l1", "l2", "l3"):
            # The meter reports every phase's voltage and reactive power as often as its power.
            hass.states.async_set(
                f"sensor.{halo.site}_reactive_{phase}", str(self.wobble % 2), {"unit_of_measurement": "var"}
            )
            hass.states.async_set(
                f"sensor.{halo.site}_voltage_{phase}", str(_V + self.wobble % 2 / 100), {"unit_of_measurement": "V"}
            )
        await hass.async_block_till_done()
        await halo.tick()
        self.freezer.tick(timedelta(seconds=_STEP_S))
        halo.advance(_STEP_S)

    async def run(self, seconds: float) -> None:
        for _ in range(int(seconds / _STEP_S)):
            await self.step()

    def outcomes(self) -> list[str]:
        return [f"{entry['outcome']}:{entry.get('detail')}" for entry in self.halo.log() if "probe" in entry["outcome"]]

    def pauses_fed(self) -> int:
        return sum(1 for event in self.halo.cc.ownership_shadow.events if event["event"]["kind"] == "balancing_pause")

    async def probe_at_minimum_fails(self) -> None:
        """The battery holds the fuse until the car is held at its minimum as the probe; then it keeps its charge
        whatever the car takes and a load comes on L1: past what the car explains, the car is paused."""
        self.battery = "holds"
        for _ in range(120):
            await self.step()
            if self.outcomes() == ["probe_started:held_at_minimum"]:
                break
        assert self.outcomes() == ["probe_started:held_at_minimum"] and self.car_a == 6.0
        self.battery = "fixed"
        self.fixed_w = self.battery_w
        self.house = (self.house[0] + 1.6, self.house[1], self.house[2])
        await self.step()
        await self.step()


async def _the_night(
    hass: HomeAssistant, monkeypatch, freezer, entry_id: str, house: tuple[float, float, float] = _HOUSE
) -> _Night:
    """The plan's window is open, the car charges (at 16 A, as far as the house leaves room), the battery idle."""
    hass.states.async_set(_battery_entity(entry_id), "0", {"unit_of_measurement": "W"})
    halo = await _halo(
        hass, monkeypatch, entry_id, l1=house[0], l2=house[1], l3=house[2], battery_entity=_battery_entity(entry_id)
    )
    now = dt_util.utcnow()
    window = {"start": (now - timedelta(hours=1)).isoformat(), "end": (now + timedelta(hours=4)).isoformat()}
    await install_schedule(halo.cc, {"periods": [window], "amps": 16})
    await hass.async_block_till_done()
    night = _Night(halo, freezer, house)
    if not halo.turn_on:
        await halo.start()
    await night.run(300.0)
    assert night.on and night.car_a >= (16.0 if house == _HOUSE else 10.0)
    return night


def _drives(monkeypatch, drives: bool) -> None:
    monkeypatch.setattr(ownership_shadow, "CORE_OWNERSHIP_DEFAULT", drives)


# -- 03:15: the battery takes the fuse while the car charges


async def test_0315_the_car_is_not_paused_for_the_batterys_grid_charge(hass: HomeAssistant, monkeypatch, freezer) -> None:
    night = await _the_night(hass, monkeypatch, freezer, "nov1hold")
    night.battery = "holds"
    night.lowest_a = None
    await night.run(10 * 60.0)

    assert len(night.halo.turn_off) == 0, "never paused: held at its minimum as the probe instead"
    assert night.lowest_a is not None and night.lowest_a >= 6.0
    assert night.outcomes()[:2] == ["probe_started:held_at_minimum", "probe_succeeded:battery_yielded"]
    # Back at the plan's 16 A within ten minutes of the battery's start, the battery giving way.
    assert night.car_a == 16.0
    assert night.halo.controller._yield_stepping_last[night.halo.charger.entry_id]["state"] == "confirmed"  # noqa: SLF001

    # And it stays there while the battery holds the fuse (until 04:07).
    await night.run(40 * 60.0)
    assert len(night.halo.turn_off) == 0 and night.car_a == 16.0
    assert night.highest_site_a <= _FUSE_A + 5.0, "a step the battery took a pass to give way to, no more"


async def test_0315_a_battery_that_does_not_give_way_still_has_the_car_paused(
    hass: HomeAssistant, monkeypatch, freezer
) -> None:
    """The fuse first: a car held at its minimum as the probe is paused at once when the grid rises past what the car
    explains, and the probe backs off."""
    night = await _the_night(hass, monkeypatch, freezer, "nov1noyield")
    await night.probe_at_minimum_fails()
    assert night.outcomes() == ["probe_started:held_at_minimum", "probe_failed:excess_beyond_car"]
    assert len(night.halo.turn_off) == 1 and not night.on
    probe = night.halo.controller._battery_probes[night.halo.charger.entry_id]  # noqa: SLF001
    now = night.halo.clock()
    assert not probe.may_start(now) and probe.may_start(now + PROBE_BACKOFF_INITIAL_S)


# -- 03:18 to 04:07: a paused charge, the battery holding the fuse, and the battery done


@pytest.mark.parametrize("drives", [True, False])
async def test_0407_the_paused_charge_is_resumed_once_the_battery_is_done(
    hass: HomeAssistant, monkeypatch, freezer, drives: bool
) -> None:
    """Paused (a battery that did not give way to the probe), the charge stays the one load balancing holds back
    through the charger's last report of it, is paused once, and is resumed when the battery is done although the
    HALO's current still reads its last value from before the pause."""
    _drives(monkeypatch, drives)
    night = await _the_night(hass, monkeypatch, freezer, f"nov1resume{int(drives)}")
    await night.probe_at_minimum_fails()
    assert len(night.halo.turn_off) == 1 and not night.on
    cc = night.halo.cc
    pauses = night.pauses_fed()
    frozen = night.halo.hass.states.get(f"sensor.{night.halo.prefix}_l1").state
    assert float(frozen) > 5.0, "the HALO's current reads the car's draw from before the pause"

    # The battery holds the fuse for a while: no room, the probe backed off; the charge stays held back.
    night.battery = "holds"
    night.house = _HOUSE
    await night.run(5 * 60.0)
    assert cc.paused_by_balancing, "the charger's last report of the charge did not make it a new one"
    assert not cc.plan_expects_charge, "a balancing pause is no charge that failed to start: nothing is told"
    assert (len(night.halo.turn_off), len(night.halo.turn_on)) == (1, 1)
    assert night.pauses_fed() == pauses, "the stopped charger is not paused again on its reading from before"
    assert night.halo.hass.states.get(f"sensor.{night.halo.prefix}_l1").state == frozen

    # 04:07 the battery is done: the site has room, and the charge is given back within the resume's dwell.
    night.battery = "idle"
    await night.run(90.0)
    assert len(night.halo.turn_on) == 2 and night.on
    assert any(entry["outcome"] == "resumed" for entry in night.halo.log())
    assert cc.charge_origin == "plan_window"


# -- 05:15: the next window's charge paused while the battery stops


@pytest.mark.parametrize("drives", [True, False])
async def test_0515_a_charge_paused_as_the_battery_stops_is_resumed(
    hass: HomeAssistant, monkeypatch, freezer, drives: bool
) -> None:
    _drives(monkeypatch, drives)
    night = await _the_night(hass, monkeypatch, freezer, f"nov1late{int(drives)}", house=_HOUSE_0515)
    # The battery charges at 8 kW and does not hold the grid: L3 far over the fuse, the car is paused.
    night.battery = "fixed"
    night.fixed_w = 7955.0
    for _ in range(6):
        await night.step()
        if night.halo.turn_off:
            break
    assert len(night.halo.turn_off) == 1
    # 05:15:17 the battery stops; the charger answers the pause, reporting its charge once more on the way.
    night.battery = "idle"
    await night.step()
    cc = night.halo.cc
    assert not night.on and cc.paused_by_balancing, "the window's claim of the last report keeps the pause"
    assert not cc.plan_expects_charge, "a balancing pause is no charge that failed to start: nothing is told"

    # The house alone: the charge is resumed within the dwell, as the plan's, although the HALO's current still reads
    # the car's draw from before the pause.
    await night.run(90.0)
    assert len(night.halo.turn_on) == 2 and night.on
    assert cc.charge_origin == "plan_window"
    assert not [entry for entry in night.halo.log() if entry["outcome"] == "wrote" and entry["from_a"] == 0], (
        "nothing is written to the stopped charger on its reading from before the pause"
    )


# -- the core's claim


def test_a_claim_keeps_the_charge_load_balancing_holds_back() -> None:
    """A charger that reports its charge once more before it answers a balancing pause, inside a window: the claim
    makes the charge the plan's and sends nothing, and load balancing still holds it back (as today's claim leaves
    `_paused_by_balancing`)."""
    now = dt_util.utcnow()
    session = ChargeSession(plugged=True, owner="plan")
    session, _ = decide(session, BalancingPause(code="pause", was_on=True), now)
    session, _ = decide(session, CommandResult(command="stop", reason="balancing", executed=True), now)
    assert session.balancing_paused and session.paused_origin == "plan" and session.owner == "none"
    session, commands = decide(
        session,
        Recheck(what="claim", control_on=True, window_open=True, in_window=True, plan_present=True),
        now,
    )
    assert [(command.kind, command.reason) for command in commands] == [("start", "claim")]
    session, _ = decide(session, CommandResult(command="start", reason="claim", executed=True), now)
    assert session.owner == "plan"
    assert session.balancing_paused and session.paused_origin == "plan"
