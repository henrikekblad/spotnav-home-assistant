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
from typing import Any

from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.util import dt as dt_util

from custom_components.spotnav.site.site_capacity_controller import RaiseCredit, SiteCapacityController

from .helpers import install_schedule
from .test_balancing_retry import _battery_entity
from .test_lb_review_h import _tick, _two, _writes
from .test_lb_start_credit import _halo, _Halo, _site_a
from .world import set_charger_delivered_a

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
        #: Whether the car follows a write one pass late (a car ramps over a few seconds), and what was written then.
        self.lag = False
        self.prev_assigned = 16.0
        #: The phases the heater draws on (the field's is on L1 only), and every how many passes the HALO sends a
        #: meter value.
        self.heater_phases: tuple[str, ...] = ("L1",)
        self.meter_passes = _METER_VALUE_PASSES
        #: The site's current per phase on every pass.
        self.site_log: list[list[float]] = []
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
        assigned = self.assigned_a()
        followed = self.prev_assigned if self.lag else assigned
        self.prev_assigned = assigned
        self.car_a = min(followed, self.car_max_a) if self.on else 0.0
        if self.on:
            self.lowest_a = self.car_a if self.lowest_a is None else min(self.lowest_a, self.car_a)
        if self.passes % self.meter_passes == 0:
            halo.reads(round(self.car_a * 0.995 + _WOBBLE[self.wobble] / 4, 2))
        flicker = _WOBBLE[(self.wobble + 3) % len(_WOBBLE)] / 3
        heater = tuple(
            self.heater_a if self.heater and phase in self.heater_phases else 0.0 for phase in ("L1", "L2", "L3")
        )
        site = [round(house + extra + flicker + self.car_a, 2) for house, extra in zip(self.house, heater, strict=True)]
        self.site_log.append(site)
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


async def test_1019_a_load_after_a_raise_lowers_the_car_and_does_not_pause_it(
    hass: HomeAssistant, monkeypatch, freezer
) -> None:
    """The heater off, the car raised to 16 A; the heater on again in the next pass, before the HALO's next meter value:
    the car is lowered to what fits beside the heater in that pass, not paused, and the site is back within the fuse
    from the pass after."""
    morning = await _the_morning(hass, monkeypatch, freezer, "nov1lag")
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


async def test_a_car_that_draws_less_than_written_is_not_credited_with_the_raise(
    hass: HomeAssistant, monkeypatch, freezer
) -> None:
    """The car takes no more than 10 A, whatever is written. Raised to 14 A or more with the heater off, it goes to
    10 A: the site rises by 2 A where 6 were written, which is not the step, so nothing is credited. A larger load on
    L1 then comes on before the HALO's next meter value: the car is decided on its own reading, and the site is within
    the fuse from the next pass."""
    morning = await _the_morning(hass, monkeypatch, freezer, "nov1less", car_max_a=10.0)
    morning.heater = False
    await morning.until_raised(12.0)
    raised_at = len(morning.trace)

    morning.heater = True
    morning.heater_a = 11.6
    await morning.step()
    trace = "\n".join(morning.trace[raised_at - 3 :])
    assert not morning.halo.log()[-1]["raise_credit_a"], f"nothing credited:\n{trace}"
    morning.peak = None
    await morning.step()
    assert max(morning.peak) <= _FUSE_A, f"within the fuse from the next pass:\n{trace}"


async def test_a_load_on_every_phase_with_a_raise_the_car_does_not_take_is_never_the_car(
    hass: HomeAssistant, monkeypatch, freezer
) -> None:
    """The car takes no more than 10 A. Raised with the heater off, it goes to 10 A; an 11.6 A load on all three phases
    comes on before the HALO's next meter value. The site rose by far more than the step on every phase, so it is not
    the car's: nothing is credited, and the site is over the fuse for no more than the pass that saw the load."""
    morning = await _the_morning(hass, monkeypatch, freezer, "nov1three", car_max_a=10.0)
    morning.heater = False
    await morning.until_raised(12.0)
    raised_at = len(morning.trace)

    morning.heater = True
    morning.heater_a = 11.6
    morning.heater_phases = ("L1", "L2", "L3")
    passes_over = 0
    for _ in range(24):
        await morning.step()
        assert not morning.halo.log()[-1]["raise_credit_a"], "\n".join(morning.trace[raised_at - 2 :])
        if max(morning.site_log[-1]) > _FUSE_A + 0.3:
            passes_over += 1
    assert passes_over <= 1, "\n".join(morning.trace[raised_at - 2 :])


async def test_a_lowering_after_a_raise_is_never_raised_back_on_the_credit(
    hass: HomeAssistant, monkeypatch, freezer
) -> None:
    """The car takes the raise to 16 A; an 11.6 A load on all three phases comes on right after it, and the HALO's
    meter values come a minute apart. The car is lowered (or paused), never raised again on what the raise was
    credited with while the load is on, and the site stays within the fuse once lowered."""
    morning = await _the_morning(hass, monkeypatch, freezer, "nov1low")
    morning.meter_passes = 12
    morning.heater = False
    await morning.until_raised(16.0)
    raised_at = len(morning.trace)

    morning.heater = True
    morning.heater_a = 11.6
    morning.heater_phases = ("L1", "L2", "L3")
    await morning.step()
    lowered = morning.assigned_a()
    writes_before = len(morning.halo.written())
    morning.peak = None
    for _ in range(20):
        await morning.step()
        credited = morning.halo.log()[-1]["raise_credit_a"]
        assert not credited or max(credited.values()) <= morning.assigned_a() + 0.5, "\n".join(
            morning.trace[raised_at - 2 :]
        )
    trace = "\n".join(morning.trace[raised_at - 2 :])
    raises = [
        written
        for written in morning.halo.written()[writes_before:]
        if float(written.split(",")[0].split(".")[1]) > lowered
    ]
    assert not raises, f"raised again to {raises}:\n{trace}"
    assert max(morning.peak) <= _FUSE_A + 0.3, trace


async def test_the_credit_follows_a_lowering(hass: HomeAssistant, monkeypatch, freezer) -> None:
    """The field case with the HALO's meter values a minute apart: after the lowering to what fits beside the heater,
    the car is never credited with more than it was lowered to, and it is not raised again while the heater is on."""
    morning = await _the_morning(hass, monkeypatch, freezer, "nov1follow")
    morning.meter_passes = 12
    morning.heater = False
    await morning.until_raised(16.0)
    raised_at = len(morning.trace)

    morning.heater = True
    await morning.step()
    lowered = morning.assigned_a()
    trace = "\n".join(morning.trace[raised_at - 2 :])
    assert len(morning.halo.turn_off) == 0 and lowered <= 10.0, trace
    for _ in range(11):
        await morning.step()
        credited = morning.halo.log()[-1]["raise_credit_a"]
        trace = "\n".join(morning.trace[raised_at - 2 :])
        assert not credited or max(credited.values()) <= lowered + 0.01, trace
        assert morning.assigned_a() <= lowered, trace
    assert max(morning.site_log[-1]) <= _FUSE_A, trace


async def test_a_load_of_the_step_on_every_phase_costs_at_most_two_passes_more(
    hass: HomeAssistant, monkeypatch, freezer
) -> None:
    """The worst case left: the car takes no more than 9 A and is raised to 16 A, and at once a load of the step's
    size (7 A) comes on all three phases, with the heater on L1. The site's rise matches the step, so it is credited;
    the lowering on it does not bring the site down. A car takes a few seconds to come down, so the site reading one
    pass after the lowering still counts the raise (`RAISE_CREDIT_LOWERING_SETTLE_S`); the one after it ends the
    credit, and the car is decided on its own reading in that pass. The site is over the fuse for three passes at most,
    two more than without the credit: what telling a car that ramps from a load of exactly its step costs."""
    morning = await _the_morning(hass, monkeypatch, freezer, "nov1step", car_max_a=9.0)
    morning.heater = False
    await morning.until_raised(16.0)
    raised_at = len(morning.trace)

    step_a = 16.0 - morning.car_a
    morning.house = tuple(amps + step_a for amps in _HOUSE)
    morning.heater = True
    passes_over = 0
    for _ in range(24):
        await morning.step()
        if max(morning.site_log[-1]) > _FUSE_A + 0.3:
            passes_over += 1
    trace = "\n".join(morning.trace[raised_at - 2 :])
    assert any(entry["raise_credit_a"] for entry in morning.halo.log()[-30:]), f"the step was credited:\n{trace}"
    assert passes_over <= 3, trace


def test_the_credit_s_cap_follows_every_write_after_the_raise() -> None:
    """What is on the charger as the site read a phase: a raise from a reading after it on, a lowering or a pause only
    from a reading more than `RAISE_CREDIT_LOWERING_SETTLE_S` after it (the car comes down over a few seconds); before
    the raise there is nothing to credit."""
    credit = RaiseCredit(100.0, ((100.0, 16.0), (110.0, 6.0), (130.0, 0.0)), {}, {}, {})
    in_force = SiteCapacityController._raise_credit_in_force  # noqa: SLF001
    assert in_force(credit, 100.0) is None
    assert in_force(credit, 105.0) == 16.0
    assert in_force(credit, 110.0) == 16.0
    assert in_force(credit, 115.0) == 16.0, "the car has a few seconds to come down"
    assert in_force(credit, 115.5) == 6.0
    assert in_force(credit, 135.0) == 6.0
    assert in_force(credit, 136.0) == 0.0


#: The two chargers' site: a 40 A fuse, and a load on all three phases that leaves room for the two cars' minimum.
_TWO_FUSE_A = 40.0
_TWO_LOAD_A = 26.5


class _TwoCars:
    """Two HALO-like chargers on one site (`tests/test_lb_review_h.py`'s `_two`), their meter values half a minute
    apart, each car taking no more than its own maximum, a load on all three phases that comes and goes."""

    def __init__(self, two, freezer, car_max_a: dict[str, float]) -> None:
        self.t = two
        self.freezer = freezer
        self.car_max_a = car_max_a
        self.load_a = 0.0
        #: The phases the load draws on.
        self.load_phases: tuple[str, ...] = ("L1", "L2", "L3")
        self.car_a = {name: 0.0 for name in car_max_a}
        self.passes = 0
        self.seen = {"off": len(two.turn_off), "on": len(two.turn_on)}
        self.site_log: list[list[float]] = []
        self.trace: list[str] = []

    def written(self, name: str) -> float:
        values = _writes(self.t, name)
        return values[-1] if values else 16.0

    async def step(self) -> None:
        t = self.t
        hass = t.hass
        self.passes += 1
        for name, prefix in t.prefix.items():
            on = hass.states.get(f"switch.{prefix}").state == "on"
            self.car_a[name] = min(self.written(name), self.car_max_a[name]) if on else 0.0
            if self.passes % _METER_VALUE_PASSES == 0:
                set_charger_delivered_a(hass, prefix, round(self.car_a[name] * 0.995 + self.passes % 7 / 1000, 3))
        flicker = _WOBBLE[self.passes % len(_WOBBLE)] / 3
        site = [
            round(1.0 + (self.load_a if phase in self.load_phases else 0.0) + flicker + sum(self.car_a.values()), 2)
            for phase in ("L1", "L2", "L3")
        ]
        self.site_log.append(site)
        _site_a(hass, t.site, *site)
        for phase in ("l1", "l2", "l3"):
            hass.states.async_set(
                f"sensor.{t.site}_reactive_{phase}", str(self.passes % 2), {"unit_of_measurement": "var"}
            )
            hass.states.async_set(
                f"sensor.{t.site}_voltage_{phase}", str(_V + self.passes % 2 / 100), {"unit_of_measurement": "V"}
            )
        await hass.async_block_till_done()
        await _tick(t)
        # The chargers answer what was sent to their switches since the last pass.
        for calls, state in ((t.turn_off, "off"), (t.turn_on, "on")):
            for call in calls[self.seen[state] :]:
                hass.states.async_set(str(call.data.get("entity_id")), state)
            self.seen[state] = len(calls)
        await hass.async_block_till_done()
        credits = {
            name: t.controller._raise_credited_a.get(t.cc[name].entry_id)  # noqa: SLF001
            for name in t.prefix
        }
        self.trace.append(
            f"p{self.passes} load {self.load_a} cars {self.car_a} site {site[0]} written"
            f" {[self.written(name) for name in t.prefix]} open {len(t.controller._raise_credits)}"  # noqa: SLF001
            f" credited {credits}"
            f" {[(d.reason, d.proposed_current_a) for d in (t.controller.regulator_decisions.get(t.cc[name].entry_id) for name in t.prefix) if d]}"
            f" {[e['detail'] for e in t.controller.regulator_decision_log[-2:]]}"
        )
        self.freezer.tick(timedelta(seconds=_STEP_S))
        t.clock.advance(_STEP_S)
        t.damper_clock.advance(_STEP_S)

    async def run(self, seconds: float) -> None:
        for _ in range(int(seconds / _STEP_S)):
            await self.step()


def _ocpp_per_charger(hass: HomeAssistant) -> list[ServiceCall]:
    """The OCPP integration's `get_configuration` and `configure` for charge points that accept every
    `AssignedCurrent` written, each its own: what is configured is what is read back. Returns the `configure` calls."""
    slots: dict[str, str] = {}
    calls: list[ServiceCall] = []

    async def get_configuration(call: ServiceCall) -> dict[str, Any]:
        return {"value": slots.get(str(call.data.get("devid")), "1.16,2.10")}

    async def configure(call: ServiceCall) -> None:
        calls.append(call)
        slots[str(call.data.get("devid"))] = call.data["value"]

    hass.services.async_register(
        "ocpp", "get_configuration", get_configuration, supports_response=SupportsResponse.ONLY
    )
    hass.services.async_register("ocpp", "configure", configure)
    return calls


async def _two_cars(hass: HomeAssistant, monkeypatch, freezer, *, credit: bool) -> tuple[_TwoCars, int]:
    """Both chargers started at their minimum under a load on every phase that leaves no room for more, the load off until both are raised in one pass, then
    on again before either charger's next meter value. Returns the cars and the passes the site was over the fuse."""
    if not credit:
        monkeypatch.setattr(SiteCapacityController, "_begin_raise_credit", lambda self, *args: None)
    two = await _two(hass, monkeypatch, fuse=_TWO_FUSE_A, base=1.0)
    two.configure = _ocpp_per_charger(hass)
    # A is set to First: an overload is cut from B first.
    first = two.cc["a"].entry_id
    monkeypatch.setattr(
        SiteCapacityController, "charger_priority", lambda self, entry_id: "first" if entry_id == first else "normal"
    )
    now = dt_util.utcnow()
    window = {"start": (now - timedelta(hours=1)).isoformat(), "end": (now + timedelta(hours=4)).isoformat()}
    for name in ("a", "b"):
        await install_schedule(two.cc[name], {"periods": [window], "amps": 16})
        await hass.async_block_till_done()
        hass.states.async_set(f"switch.{two.prefix[name]}", "on")
    await hass.async_block_till_done()
    cars = _TwoCars(two, freezer, {"a": 16.0, "b": 16.0})
    await cars.run(60.0)
    assert all(amps == 16.0 for amps in cars.car_a.values()), "\n".join(cars.trace)
    # Both set to their minimum by another path (the sun's own write, say), and a load that leaves no more room.
    for name in ("a", "b"):
        await two.cc[name].async_apply_regulated_current(6, must_lower=True)
        two.controller.note_charger_write(two.cc[name].entry_id, 6.0, "other_path")
    cars.load_a = _TWO_LOAD_A
    await cars.run(120.0)
    assert all(amps == 6.0 for amps in cars.car_a.values()), "\n".join(cars.trace[-30:])
    before = {name: cars.written(name) for name in two.prefix}
    # B's car takes no more than it draws now.
    cars.car_max_a["b"] = cars.car_a["b"]
    cars.load_a = 0.0
    for _ in range(36):
        await cars.step()
        if all(cars.written(name) > before[name] for name in two.prefix):
            break
    else:
        raise AssertionError("never both raised:\n" + "\n".join(cars.trace[-40:]))
    raised_at = len(cars.trace)
    # A load on L1 alone: the rise on L2 and L3 is A's step, which matches B's step as well.
    cars.load_a = _TWO_LOAD_A
    cars.load_phases = ("L1",)
    over = 0
    for _ in range(12):
        await cars.step()
        if max(cars.site_log[-1]) > _TWO_FUSE_A + 0.3:
            over += 1
    cars.trace = cars.trace[raised_at - 2 :]
    return cars, over


@pytest.mark.parametrize("credit", [True, False])
async def test_two_chargers_raised_in_one_pass_are_not_credited(
    hass: HomeAssistant, monkeypatch, freezer, credit: bool
) -> None:
    """Two chargers raised from 6 to 16 A in the same pass, A's car taking the raise and B's not; then a load on L1
    alone. On L2 and L3 the site rose by A's step, which is B's step too: whose rise it is cannot be told, so neither
    is credited while both raises are open (B credited with A's 10 A would be "lowered" to the 6 A it draws, and A
    kept at 16 over the fuse). The site is back within the fuse after the pass that saw the load, as without the
    credit (`credit=False`)."""
    cars, over = await _two_cars(hass, monkeypatch, freezer, credit=credit)
    trace = "\n".join(cars.trace)
    for line in cars.trace:
        if "open 2" in line:
            assert "credited {'a': {}, 'b': {}}" in line or "credited {'a': None, 'b': None}" in line, trace
    assert over <= 1, trace


async def test_a_raise_credit_never_raises_a_car_back_after_its_lowering(
    hass: HomeAssistant, monkeypatch, freezer
) -> None:
    """The field case, then the HALO tells nothing for minutes. Raised 9 to 16 A, the heater on: lowered on the credit
    to what fits. The heater goes off and a load of the step's size (7 A) comes on all three phases: the site's rise
    since the raise matches the step again, but what is on the charger is the lowered current, and so is the most the
    car is credited with. Reckoned at the 16 A of the raise, the house would read 7 A lighter on every phase and the
    car would be raised back to 16 A, L1 over the fuse."""
    morning = await _the_morning(hass, monkeypatch, freezer, "nov1cap")
    morning.heater = False
    await morning.until_raised(16.0)
    morning.meter_passes = 1000
    raised_at = len(morning.trace)

    morning.heater = True
    await morning.step()
    lowered = morning.assigned_a()
    trace = "\n".join(morning.trace[raised_at - 2 :])
    assert len(morning.halo.turn_off) == 0 and lowered <= 10.0, trace

    morning.heater = False
    step_a = 16.0 - lowered
    morning.house = tuple(amps + step_a for amps in _HOUSE)
    morning.peak = None
    for _ in range(16):
        await morning.step()
    trace = "\n".join(morning.trace[raised_at - 2 :])
    assert morning.assigned_a() <= lowered, f"raised back on the credit:\n{trace}"
    assert max(morning.peak) <= _FUSE_A, trace


async def test_the_field_case_with_the_car_one_pass_behind_each_write(
    hass: HomeAssistant, monkeypatch, freezer
) -> None:
    """The field case with a car that follows each write one site reading late: the site shows the raise to 16 A a
    pass after it, and the lowering a pass after that. The reading taken right after the lowering still shows the car
    at 16 A; it is still credited so, and the car is never paused."""
    morning = await _the_morning(hass, monkeypatch, freezer, "nov1ramp")
    morning.lag = True
    morning.prev_assigned = morning.assigned_a()
    morning.heater = False
    await morning.until_raised(16.0)
    raised_at = len(morning.trace)

    morning.heater = True
    morning.peak = None
    for _ in range(8):
        await morning.step()
    trace = "\n".join(morning.trace[raised_at - 2 :])
    assert len(morning.halo.turn_off) == 0, f"paused:\n{trace}"
    assert morning.assigned_a() <= 10.0 and max(morning.site_log[-1]) <= _FUSE_A, trace


async def test_1019_the_halo_s_last_meter_value_after_the_pause_does_not_hold_back_the_resume(
    hass: HomeAssistant, monkeypatch, freezer
) -> None:
    """Paused for a load the car's minimum does not fit beside, the HALO sends one more meter value a few seconds after
    the stop, still showing the car's current from before it (8.068/7.85/8.023 A at 10:19:05 on 2026-11-01), and then
    nothing. That value belongs to the charge before the pause: once the load is gone the charge is resumed after the
    resume's dwell, not two minutes later when the value is too old to use (10:22:16 that morning)."""
    morning = await _the_morning(hass, monkeypatch, freezer, "nov1final")
    morning.heater_a = 16.0
    for _ in range(4):
        await morning.step()
        if morning.halo.turn_off:
            break
    assert len(morning.halo.turn_off) == 1, "\n".join(morning.trace[-6:])
    drew_a = morning.trace and morning.lowest_a
    # The charger answers the stop, and a few seconds later sends its last meter value of the charge.
    await morning.step()
    assert not morning.on
    set_charger_delivered_a(hass, morning.halo.prefix, round(float(drew_a) * 0.995 + 0.01, 2))
    await hass.async_block_till_done()
    morning.heater = False
    starts = len(morning.halo.turn_on)
    passes = 0
    while len(morning.halo.turn_on) == starts and passes < 40:
        await morning.step()
        passes += 1
    assert len(morning.halo.turn_on) == starts + 1, "\n".join(morning.trace[-10:])
    assert passes * _STEP_S <= 75.0, f"resumed after {passes * _STEP_S:g} s:\n" + "\n".join(morning.trace[-20:])
