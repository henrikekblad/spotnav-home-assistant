"""A home battery that limits the grid by power, on a morning of low mains voltage: the field morning of 2026-11-13.

The owner's site: a 20 A main fuse with no safety margin, the site derived from a Sigenergy meter (per-phase grid
power, reactive power and the inverter's phase voltage), a Charge Amps HALO on OCPP (3 phases, minimum 6 A), a
Sigenergy battery in AI mode charging from the grid (SoC 10 %, up to 14 kW), `car_first`, yield stepping on.

The Sigenergy limits the grid by power, about 4.6 kW per phase (20 A at 230 V), not by current. L1 read 224-225 V that
morning, so the grid it held read 20.3-20.66 A on L1 (L2/L3 about 20.2 A at about 227 V): just above the held band of
0.5 A. From 10:16:52 the car was stepped down 10, 9, 8, 7, 6 A and paused at 10:19:27; the probe at 6 A at 10:19:32 saw
the battery give way (12.7 to 9.8 kW) and still failed on L1 at 20.60 A (`battery_did_not_yield`). On earlier nights,
at a higher voltage, the same power read 20.0-20.1 A and everything worked.

On a phase whose measured voltage is low the band is wider by what that power draws there (`battery_probe.
held_band_extra_a`): at 224.9 V and a 20 A limit 0.954 A. The same currents at 230 V have no such reason and are
lowered as before, and a real excess beyond the widened band is lowered as ever.

Driven through `SiteCapacityController` with the real regulator and the real charger controller, a pass every five
seconds (`tests/test_field_night_1_november.py`'s world), the battery holding the grid currents the meter showed.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.spotnav.const import CONF_DERIVED_ENTITIES
from custom_components.spotnav.site.battery_probe import held_band_a

from .helpers import install_schedule
from .test_balancing_retry import _battery_entity, _battery_w
from .test_lb_start_credit import _halo, _Halo

#: The phase voltages that morning, and a nominal one.
_LOW_V = (224.9, 227.0, 227.0)
_NOMINAL_V = (230.0, 230.0, 230.0)
#: The grid currents the battery held (L1 20.43-20.60 A with the wobble, L2/L3 about 20.2 A).
_HELD_A = (20.52, 20.2, 20.2)
#: A real excess on L1, beyond the widened band.
_EXCESS_A = (21.5, 20.2, 20.2)
_HOUSE = (1.9, 0.8, 0.8)
_BATTERY_MAX_W = 14000.0
_FUSE_A = 20.0
_PLAN_A = 10
_STEP_S = 5.0
_READING_LAG_PASSES = 6
_WOBBLE = (0.03, 0.07, -0.09, 0.04, 0.02, 0.08, -0.03, 0.05)


class _Morning:
    """The site, the car and the battery, a pass every five seconds."""

    def __init__(self, halo: _Halo, freezer, voltages: tuple[float, float, float]) -> None:
        self.halo = halo
        self.freezer = freezer
        self.voltages = voltages
        #: `idle`, `holds` (the grid at `held_a` per phase, a pass behind the car, up to its maximum) or `fixed`
        #: (`fixed_w`, whatever the grid).
        self.battery = "idle"
        self.held_a = _HELD_A
        self.fixed_w = 0.0
        self.car_a = 0.0
        self.on = False
        self.offs = 0
        self.ons = 0
        self.wobble = 0
        self.battery_w = 0.0
        self.drawn: list[float] = []
        self.lowest_a: float | None = None
        #: Per phase, the highest site current since it was last set to `None`.
        self.site_high: list[float] | None = None
        self.trace: list[str] = []

    def assigned_a(self) -> float:
        written = self.halo.written()
        return float(written[-1].split(",")[0].split(".")[1]) if written else float(_PLAN_A)

    def battery_a(self, car_a: float) -> tuple[float, ...]:
        if self.battery == "idle":
            return (0.0, 0.0, 0.0)
        if self.battery == "fixed":
            return tuple(self.fixed_w / (3 * volts) for volts in self.voltages)
        room = [
            max(0.0, held + _WOBBLE[self.wobble] - house - car_a)
            for held, house in zip(self.held_a, _HOUSE, strict=True)
        ]
        watts = sum(amps * volts for amps, volts in zip(room, self.voltages, strict=True))
        scale = min(1.0, _BATTERY_MAX_W / watts) if watts else 1.0
        return tuple(round(amps * scale, 3) for amps in room)

    async def step(self) -> None:
        halo = self.halo
        hass = halo.hass
        car_before = self.car_a
        self.wobble = (self.wobble + 1) % len(_WOBBLE)
        if len(halo.turn_off) > self.offs:
            self.offs = len(halo.turn_off)
            if self.on:
                halo.switch("off")
                await hass.async_block_till_done()
                self.on = False
        if len(halo.turn_on) > self.ons:
            self.ons = len(halo.turn_on)
            if not self.on:
                halo.switch("on")
                self.on = True
        self.car_a = min(self.assigned_a(), float(_PLAN_A)) if self.on else 0.0
        if self.on:
            self.drawn = [*self.drawn[-(_READING_LAG_PASSES - 1) :], self.car_a]
            halo.reads(round(self.drawn[0] * 0.98 + _WOBBLE[self.wobble] / 4, 2))
            self.lowest_a = self.car_a if self.lowest_a is None else min(self.lowest_a, self.car_a)
        battery = self.battery_a(car_before)
        self.battery_w = sum(amps * volts for amps, volts in zip(battery, self.voltages, strict=True))
        _battery_w(halo, self.battery_w)
        site = [round(house + self.car_a + amps, 2) for house, amps in zip(_HOUSE, battery, strict=True)]
        self.site_high = site if self.site_high is None else [max(a, b) for a, b in zip(self.site_high, site)]
        for phase, amps, volts in zip(("l1", "l2", "l3"), site, self.voltages, strict=True):
            # The meter's power, reactive power and voltage, all reported as often as its power.
            hass.states.async_set(f"sensor.{halo.site}_power_{phase}", str(amps * volts), {"unit_of_measurement": "W"})
            hass.states.async_set(
                f"sensor.{halo.site}_reactive_{phase}", str(self.wobble % 2), {"unit_of_measurement": "var"}
            )
            hass.states.async_set(
                f"sensor.{halo.site}_voltage_{phase}", str(volts + self.wobble % 2 / 100), {"unit_of_measurement": "V"}
            )
        await hass.async_block_till_done()
        await halo.tick()
        decision = halo.controller.regulator_decisions.get(halo.charger.entry_id)
        stepper = halo.controller._yield_stepping_last.get(halo.charger.entry_id) or {}  # noqa: SLF001
        self.trace.append(
            f"car {self.car_a:g} battery {self.battery_w:.0f} W site {site} | {getattr(decision, 'reason', None)}"
            f" {getattr(decision, 'proposed_current_a', None)} {stepper.get('reason')} {stepper.get('state')}"
        )
        self.freezer.tick(timedelta(seconds=_STEP_S))
        halo.advance(_STEP_S)

    async def run(self, seconds: float) -> None:
        for _ in range(int(seconds / _STEP_S)):
            await self.step()

    def outcomes(self) -> list[str]:
        return [f"{entry['outcome']}:{entry.get('detail')}" for entry in self.halo.log() if "probe" in entry["outcome"]]


async def _the_morning(
    hass: HomeAssistant, monkeypatch, freezer, entry_id: str, voltages: tuple[float, float, float]
) -> _Morning:
    """The plan's window is open and the car charges at the plan's 10 A, the battery idle."""
    hass.states.async_set(_battery_entity(entry_id), "0", {"unit_of_measurement": "W"})
    halo = await _halo(
        hass, monkeypatch, entry_id, l1=_HOUSE[0], l2=_HOUSE[1], l3=_HOUSE[2], battery_entity=_battery_entity(entry_id)
    )
    now = dt_util.utcnow()
    window = {"start": (now - timedelta(hours=1)).isoformat(), "end": (now + timedelta(hours=4)).isoformat()}
    await install_schedule(halo.cc, {"periods": [window], "amps": _PLAN_A})
    await hass.async_block_till_done()
    morning = _Morning(halo, freezer, voltages)
    if not halo.turn_on:
        await halo.start(_PLAN_A)
    await morning.run(120.0)
    assert morning.on and morning.car_a == _PLAN_A, "\n".join(morning.trace[-12:])
    return morning


# -- 10:16: the battery holds the grid while the car charges


async def test_1016_the_battery_holding_its_power_at_a_low_voltage_does_not_lower_the_car(
    hass: HomeAssistant, monkeypatch, freezer
) -> None:
    """L1 at 20.43-20.60 A at 224.9 V is the battery's 4.6 kW, within the band widened to 0.954 A: the car keeps the
    plan's 10 A, never paused, and the decision log shows the band used per phase."""
    morning = await _the_morning(hass, monkeypatch, freezer, "nov13low", _LOW_V)
    morning.lowest_a = None
    morning.battery = "holds"
    morning.site_high = None
    await morning.run(10 * 60.0)

    assert max(morning.site_high) > _FUSE_A + 0.5, "the grid did read above today's band"
    assert len(morning.halo.turn_off) == 0, "never paused"
    assert morning.lowest_a == _PLAN_A, "\n".join(morning.trace[-36:])
    assert morning.outcomes() == []
    bands = morning.halo.log()[-1]["held_band_a"]
    assert bands["L1"] == pytest.approx(held_band_a(_FUSE_A, 224.9), abs=0.01)
    assert bands["L2"] == pytest.approx(held_band_a(_FUSE_A, 227.0), abs=0.01)


async def test_1016_the_same_currents_at_the_nominal_voltage_are_lowered_as_before(
    hass: HomeAssistant, monkeypatch, freezer
) -> None:
    """The same currents at 230 V are no power-limited battery's: L1 above the band of 0.5 A lowers the car as before."""
    morning = await _the_morning(hass, monkeypatch, freezer, "nov13nominal", _NOMINAL_V)
    morning.lowest_a = None
    morning.battery = "holds"
    await morning.run(10 * 60.0)

    assert morning.lowest_a is not None and morning.lowest_a < _PLAN_A, "\n".join(morning.trace[-36:])
    assert morning.halo.log()[-1]["held_band_a"] == {"L1": 0.5, "L2": 0.5, "L3": 0.5}


async def test_1016_a_real_excess_beyond_the_widened_band_is_lowered_at_once(
    hass: HomeAssistant, monkeypatch, freezer
) -> None:
    """L1 at 21.5 A at 224.9 V is more than the battery's power at that voltage: lowered in the first passes, as ever."""
    morning = await _the_morning(hass, monkeypatch, freezer, "nov13excess", _LOW_V)
    morning.lowest_a = None
    morning.held_a = _EXCESS_A
    morning.battery = "holds"
    await morning.run(30.0)
    assert morning.lowest_a is not None and morning.lowest_a < _PLAN_A, "\n".join(morning.trace[-8:])


# -- 10:19: the probe of the paused charge


async def _paused(morning: _Morning) -> None:
    """The battery starts far above the fuse on top of the car: the car is paused, as at 10:19:27."""
    morning.battery = "fixed"
    morning.fixed_w = 13000.0
    for _ in range(12):
        await morning.step()
        if morning.halo.turn_off:
            break
    await morning.step()
    assert len(morning.halo.turn_off) == 1 and not morning.on, "\n".join(morning.trace[-12:])
    assert morning.halo.cc.paused_by_balancing


async def test_1019_the_probe_at_6_a_succeeds_on_the_battery_s_power_at_a_low_voltage(
    hass: HomeAssistant, monkeypatch, freezer
) -> None:
    """The battery gives way to the car's 6 A and holds L1 at 20.43-20.60 A at 224.9 V: the probe succeeds, and the
    car charges on, stepping into the battery's share as on earlier nights."""
    morning = await _the_morning(hass, monkeypatch, freezer, "nov13probe", _LOW_V)
    await _paused(morning)
    morning.battery = "holds"
    for _ in range(12):
        await morning.step()
        if morning.on:
            break
    assert morning.outcomes()[-1] == "probe_started:battery_charging_at_the_fuse", "\n".join(morning.trace[-12:])
    morning.site_high = None
    await morning.run(60.0)
    assert "probe_succeeded:battery_yielded" in morning.outcomes(), "\n".join(morning.trace[-14:])
    assert not [outcome for outcome in morning.outcomes() if outcome.startswith("probe_failed")]
    await morning.run(5 * 60.0)
    assert morning.on and morning.car_a >= 6.0 and len(morning.halo.turn_off) == 1, "\n".join(morning.trace[-24:])
    # A step into the battery's share takes L1 no further than its allowance, widened by the band's widening alone.
    assert morning.site_high[0] <= _FUSE_A + 2.5 + held_band_a(_FUSE_A, 224.9) - 0.5 + 0.01, "\n".join(morning.trace)


async def test_1019_the_probe_still_fails_on_the_same_currents_at_the_nominal_voltage(
    hass: HomeAssistant, monkeypatch, freezer
) -> None:
    """At 230 V the same L1 above the band of 0.5 A at the end of the window is a battery that did not yield, as
    before: no probe succeeds."""
    morning = await _the_morning(hass, monkeypatch, freezer, "nov13probenominal", _NOMINAL_V)
    await _paused(morning)
    morning.battery = "holds"
    await morning.run(5 * 60.0)
    outcomes = morning.outcomes()
    assert "probe_succeeded:battery_yielded" not in outcomes, "\n".join(morning.trace[-24:])
    assert all(
        outcome in ("probe_started:battery_charging_at_the_fuse", "probe_failed:battery_did_not_yield")
        for outcome in outcomes
    ), outcomes


# -- which voltages widen the band


async def test_only_a_measured_voltage_on_a_phase_read_as_for_headroom_widens_the_band(
    hass: HomeAssistant, monkeypatch, freezer
) -> None:
    """The site's own voltage on a fresh phase widens the band; a phase whose current is read stale, one with no voltage
    entity (the nominal voltage stands in) and a site measured directly keep the band as it was."""
    morning = await _the_morning(hass, monkeypatch, freezer, "nov13which", _LOW_V)
    controller = morning.halo.controller
    fresh = controller.result
    assert controller._held_band_voltages_v(fresh)["L1"] == pytest.approx(224.9, abs=0.02)  # noqa: SLF001
    assert controller._held_bands_a(fresh)["L1"] == pytest.approx(0.954, abs=0.01)  # noqa: SLF001

    stale = replace(fresh, phase_liveness={**fresh.phase_liveness, "L1": "no_recent_report"})
    assert controller._held_band_voltages_v(stale)["L1"] is None  # noqa: SLF001
    assert controller._held_bands_a(stale)["L1"] == 0.5  # noqa: SLF001
    unknown_age = replace(
        fresh,
        phase_liveness={**fresh.phase_liveness, "L1": "confirmed_unchanged"},
        phase_age_s={**fresh.phase_age_s, "L1": None},
    )
    assert controller._held_band_voltages_v(unknown_age)["L1"] is None  # noqa: SLF001

    direct = replace(fresh, measurement_mode="direct_phase_current")
    assert controller._held_bands_a(direct) == {"L1": 0.5, "L2": 0.5, "L3": 0.5}  # noqa: SLF001

    entities = controller.config[CONF_DERIVED_ENTITIES]
    monkeypatch.setitem(
        controller.config,
        CONF_DERIVED_ENTITIES,
        {**entities, "L1": {key: value for key, value in entities["L1"].items() if key != "voltage"}},
    )
    assert controller._held_band_voltages_v(fresh)["L1"] is None  # noqa: SLF001
    assert controller._held_band_voltages_v(fresh)["L2"] == pytest.approx(227.0, abs=0.02)  # noqa: SLF001
