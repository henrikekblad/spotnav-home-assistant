"""A raise the charger's own reading does not show yet, and a house load that comes on right after it: the morning of
2026-11-01.

The owner's debug bundle of that morning. A Charge Amps HALO on OCPP (3 phases, minimum 6 A, the plan asking 16 A)
behind a 20 A main fuse with no safety margin, the site derived from a Sigenergy meter, the battery idle. The HALO's
meter values come about half a minute apart. A washing machine's heater (about 2 kW, on L1) went on and off about once
a minute. The house drew about 1.45 A on L1 and next to nothing on L2 and L3.

10:17:51-10:18:56 the heater was off, L1 at 9.5 A with the car at 8 A; after the regulator's dwell it wrote 8 to 16 A.
10:19:02 the heater was on again: the site read 24.06/16.02/15.72 A and the HALO still 8.07/7.85/8.02 A, its meter value
from before the raise. Reckoned at that reading, the car's raise was house load (24 - 8 = 16 A on L1), no room was left
for the car's minimum, and it was paused. With the car's real 16 A the house drew about 8 A, and lowering the car would
have done. It was resumed by itself at 10:22:16.

Driven through `SiteCapacityController` with the real regulator and the real charger controller, a pass every five
seconds (`tests/test_field_night_1_november.py`'s night, with the HALO's meter values half a minute apart, each showing
what the car drew when it was sent).
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.spotnav.execution import ownership_shadow

from .helpers import install_schedule
from .test_balancing_retry import _battery_entity
from .test_lb_start_credit import _halo, _Halo, _site_a

_V = 230.0
_FUSE_A = 20.0
_STEP_S = 5.0
#: The HALO sends a meter value every sixth pass, half a minute apart.
_METER_VALUE_PASSES = 6
#: The house on L1/L2/L3, and the heater on L1.
_HOUSE = (1.45, 0.1, 0.1)
_HEATER_A = 8.6
_WOBBLE = (0.03, 0.07, -0.09, 0.04, 0.02, 0.08, -0.03, 0.05)


class _Morning:
    """The site, the car and the heater, a pass every five seconds."""

    def __init__(self, halo: _Halo, freezer, house: tuple[float, float, float], car_max_a: float) -> None:
        self.halo = halo
        self.freezer = freezer
        self.house = house
        #: The most the car draws, whatever is written.
        self.car_max_a = car_max_a
        self.heater = False
        self.heater_a = _HEATER_A
        self.car_a = 0.0
        self.on = False
        self.offs = 0
        self.ons = 0
        self.passes = 0
        self.wobble = 0
        self.lowest_a: float | None = None
        #: Per phase, the highest site current since it was last set to `None`.
        self.peak: list[float] | None = None
        self.trace: list[str] = []

    def assigned_a(self) -> float:
        written = self.halo.written()
        return float(written[-1].split(",")[0].split(".")[1]) if written else 16.0

    async def step(self) -> None:
        halo = self.halo
        hass = halo.hass
        self.passes += 1
        self.wobble = (self.wobble + 1) % len(_WOBBLE)
        if len(halo.turn_off) > self.offs:
            self.offs = len(halo.turn_off)
            if self.on:
                halo.switch("off")
                self.on = False
        if len(halo.turn_on) > self.ons:
            self.ons = len(halo.turn_on)
            if not self.on:
                halo.switch("on")
                self.on = True
        # The car follows what is written within a pass; the HALO tells it only with its next meter value.
        self.car_a = min(self.assigned_a(), self.car_max_a) if self.on else 0.0
        if self.on:
            self.lowest_a = self.car_a if self.lowest_a is None else min(self.lowest_a, self.car_a)
        if self.passes % _METER_VALUE_PASSES == 0:
            halo.reads(round(self.car_a * 0.995 + _WOBBLE[self.wobble] / 4, 2))
        flicker = _WOBBLE[(self.wobble + 3) % len(_WOBBLE)] / 3
        heater = (self.heater_a if self.heater else 0.0, 0.0, 0.0)
        site = [
            round(house + extra + flicker + self.car_a, 2) for house, extra in zip(self.house, heater, strict=True)
        ]
        self.peak = list(site) if self.peak is None else [max(a, b) for a, b in zip(self.peak, site, strict=True)]
        _site_a(hass, halo.site, *site)
        for phase in ("l1", "l2", "l3"):
            hass.states.async_set(
                f"sensor.{halo.site}_reactive_{phase}", str(self.wobble % 2), {"unit_of_measurement": "var"}
            )
            hass.states.async_set(
                f"sensor.{halo.site}_voltage_{phase}", str(_V + self.wobble % 2 / 100), {"unit_of_measurement": "V"}
            )
        await hass.async_block_till_done()
        await halo.tick()
        decision = halo.controller.regulator_decisions.get(halo.charger.entry_id)
        entry = (halo.log() or [{}])[-1]
        self.trace.append(
            f"heater {'on ' if self.heater else 'off'} car {self.car_a:g} site {site}"
            f" | {getattr(decision, 'reason', None)} {getattr(decision, 'proposed_current_a', None)}"
            f" raise credit {entry.get('raise_credit_a')}"
            f" damper {halo.controller._dampers[halo.charger.entry_id].last_written_a}"  # noqa: SLF001
        )
        self.freezer.tick(timedelta(seconds=_STEP_S))
        halo.advance(_STEP_S)

    async def run(self, seconds: float) -> None:
        for _ in range(int(seconds / _STEP_S)):
            await self.step()

    async def until_raised(self, to_a: float, seconds: float = 180.0) -> None:
        """Passes until the regulator writes `to_a` or more (the dwell over)."""
        for _ in range(int(seconds / _STEP_S)):
            await self.step()
            if self.assigned_a() >= to_a:
                return
        raise AssertionError("never raised:\n" + "\n".join(str(e) for e in self.halo.log()[-12:]))


async def _the_morning(
    hass: HomeAssistant,
    monkeypatch,
    freezer,
    entry_id: str,
    *,
    house: tuple[float, float, float] = _HOUSE,
    car_max_a: float = 16.0,
) -> _Morning:
    """The plan's window is open and the car charges with the heater on: about 9 A, what L1 leaves."""
    hass.states.async_set(_battery_entity(entry_id), "0", {"unit_of_measurement": "W"})
    halo = await _halo(
        hass, monkeypatch, entry_id, l1=house[0], l2=house[1], l3=house[2], battery_entity=_battery_entity(entry_id)
    )
    now = dt_util.utcnow()
    window = {"start": (now - timedelta(hours=1)).isoformat(), "end": (now + timedelta(hours=4)).isoformat()}
    await install_schedule(halo.cc, {"periods": [window], "amps": 16})
    await hass.async_block_till_done()
    morning = _Morning(halo, freezer, house, car_max_a)
    if not halo.turn_on:
        await halo.start()
    await morning.run(300.0)
    assert morning.on, "\n".join(morning.trace[-30:])
    morning.heater = True
    await morning.run(120.0)
    assert morning.on and 6.0 <= morning.car_a <= 10.0, "\n".join(morning.trace[-30:])
    return morning


def _drives(monkeypatch, drives: bool) -> None:
    monkeypatch.setattr(ownership_shadow, "CORE_OWNERSHIP_DEFAULT", drives)


@pytest.mark.parametrize("drives", [True, False])
async def test_1019_a_load_after_a_raise_lowers_the_car_and_does_not_pause_it(
    hass: HomeAssistant, monkeypatch, freezer, drives: bool
) -> None:
    """The heater off, the car raised to 16 A; the heater on again in the next pass, before the HALO's next meter value:
    the car is lowered to what fits beside the heater in that pass, not paused, and the site is back within the fuse
    from the pass after."""
    _drives(monkeypatch, drives)
    morning = await _the_morning(hass, monkeypatch, freezer, f"nov1lag{int(drives)}")
    morning.heater = False
    await morning.until_raised(16.0)
    raised_at = len(morning.trace)

    morning.heater = True
    morning.peak = None
    await morning.step()
    overload_peak = morning.peak
    trace = "\n".join(morning.trace[raised_at - 3 :])
    assert overload_peak[0] > _FUSE_A + 5.0, "the heater on top of the car's 16 A: L1 well over the fuse"
    assert len(morning.halo.turn_off) == 0, f"lowered, not paused:\n{trace}"
    lowered = morning.assigned_a()
    assert 9.0 <= lowered <= 12.0, f"lowered to what fits beside the heater:\n{trace}"
    assert morning.halo.log()[-1]["raise_credit_a"], f"the raise its reading does not show yet was credited:\n{trace}"

    morning.peak = None
    await morning.step()
    assert max(morning.peak) <= _FUSE_A, f"within the fuse from the next pass:\n{trace}"

    # The heater goes on and off about once a minute for ten minutes: never paused.
    for _ in range(5):
        morning.heater = False
        await morning.run(65.0)
        morning.heater = True
        await morning.run(60.0)
    assert len(morning.halo.turn_off) == 0, "\n".join(morning.trace[raised_at:])


@pytest.mark.parametrize("drives", [True, False])
async def test_a_car_that_draws_less_than_written_still_fits(
    hass: HomeAssistant, monkeypatch, freezer, drives: bool
) -> None:
    """The car takes no more than 10 A, whatever is written. Raised to 14 A or more with the heater off, it goes to 10
    A, and the site shows those 10 A, not what was written. A larger load on L1 then comes on before the HALO's next meter value: the car is
    credited with what the site shows it took, never with what was written, and lowered to what fits at once."""
    _drives(monkeypatch, drives)
    morning = await _the_morning(hass, monkeypatch, freezer, f"nov1less{int(drives)}", car_max_a=10.0)
    morning.heater = False
    await morning.until_raised(12.0)
    raised_at = len(morning.trace)

    morning.heater = True
    morning.heater_a = 11.6
    await morning.step()
    trace = "\n".join(morning.trace[raised_at - 3 :])
    credited = morning.halo.log()[-1]["raise_credit_a"]
    assert credited and max(credited.values()) <= 10.0, f"credited what the site shows, not what was written:\n{trace}"
    assert len(morning.halo.turn_off) == 0 and morning.assigned_a() <= 6.0, f"lowered to what fits:\n{trace}"
    morning.peak = None
    await morning.step()
    assert max(morning.peak) <= _FUSE_A, f"within the fuse from the next pass:\n{trace}"
