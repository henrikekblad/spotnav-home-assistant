"""Holding a site's chargers under its import target, through the real site controller (`_hold_to_import_target`).

The owner's HALO world (`tests/test_lb_start_credit.py`): a Charge Amps HALO on OCPP (three phases, 16 A asked for)
behind a 20 A main fuse with no safety margin, the site derived from its meter, active control on, with the regulator,
its damper and yield stepping as they run. The wall clock (`freezegun`) moves with the stepper's and the damper's
clocks, and a small world model closes the loop: the car draws what was last written to it while its charge control
is on, and the meter reads the house plus the car on every phase. Each clock hour's real mean import is integrated
beside the site's own tracker, so the assertions are on what the meter would bill, not on what SpotNav believes.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from freezegun import freeze_time
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.spotnav.const import CONF_IMPORT_TARGET_KW
from custom_components.spotnav.execution.controller import ChargingPlan
from custom_components.spotnav.site.site_capacity_controller import (
    LIMIT_CAUSE_IMPORT_TARGET,
    REASON_IMPORT_TARGET_HELD,
    REASON_IMPORT_TARGET_PAUSED,
)

from .test_lb_start_credit import _halo, _Halo

_V = 230.0
#: 2026-11-02 13:00:00 in Stockholm (CET, +01:00).
_HOUR = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)


@dataclass
class _World:
    """The HALO world with a moving wall clock and a meter that follows the car (see the module)."""

    halo: _Halo
    frozen: Any
    house_a: float = 1000.0 / (3 * _V)
    #: What the meter showed, per clock hour (UTC start): watt-seconds and seconds.
    energy: dict[datetime, list[float]] = field(default_factory=lambda: defaultdict(lambda: [0.0, 0.0]))
    #: Extra power (W, per phase) the meter reads besides the house and the car: a battery's charge, the sun.
    extra_a: float = 0.0
    #: The home battery's power (W, positive while it charges), reported with the meter where the site has one.
    battery_w: float | None = None
    #: The meter stops reporting (a gap in the hour).
    silent: bool = False
    turn_offs: int = 0
    turn_ons: int = 0

    @property
    def car_a(self) -> float:
        if self.halo.hass.states.get(f"switch.{self.halo.prefix}").state != "on":
            return 0.0
        written = self.halo.written()
        if not written:
            return 16.0
        return float(written[-1].split(",")[0].split(".")[1])

    def site_w(self) -> float:
        return 3 * _V * (self.house_a + self.extra_a + self.car_a)

    def show(self) -> None:
        """The meter and the charger report now, changed or not (a meter that reports every few seconds)."""
        hass = self.halo.hass
        amps = self.house_a + self.extra_a + self.car_a
        if self.battery_w is not None:
            hass.states.async_set(
                f"sensor.{self.halo.site.removesuffix('_site')}_battery_power",
                str(round(self.battery_w)),
                {"unit_of_measurement": "W"},
                force_update=True,
            )
        if self.silent:
            return
        for phase in ("l1", "l2", "l3"):
            hass.states.async_set(
                f"sensor.{self.halo.site}_power_{phase}", str(round(amps * _V, 3)), {"unit_of_measurement": "W"},
                force_update=True,
            )
            hass.states.async_set(
                f"sensor.{self.halo.site}_reactive_{phase}", "0", {"unit_of_measurement": "var"}, force_update=True
            )
            hass.states.async_set(
                f"sensor.{self.halo.site}_voltage_{phase}", str(_V), {"unit_of_measurement": "V"}, force_update=True
            )
            hass.states.async_set(
                f"sensor.{self.halo.prefix}_{phase}", str(self.car_a), {"unit_of_measurement": "A"}, force_update=True
            )

    def follow_commands(self) -> None:
        """The charger answers a stop or a start at once (its charge control goes off or on)."""
        if len(self.halo.turn_off) > self.turn_offs:
            self.turn_offs = len(self.halo.turn_off)
            self.halo.switch("off")
        if len(self.halo.turn_on) > self.turn_ons:
            self.turn_ons = len(self.halo.turn_on)
            self.halo.switch("on")

    async def step(self, seconds: float = 30.0) -> None:
        """`seconds` pass at the meter's present reading, then one recompute and its apply pass."""
        now = dt_util.utcnow()
        watts = max(0.0, self.site_w())
        moment = now
        end = now + timedelta(seconds=seconds)
        while moment < end:
            hour = moment.replace(minute=0, second=0, microsecond=0)
            until = min(end, hour + timedelta(hours=1))
            span = (until - moment).total_seconds()
            self.energy[hour][0] += watts * span
            self.energy[hour][1] += span
            moment = until
        self.frozen.tick(timedelta(seconds=seconds))
        self.halo.advance(seconds)
        self.show()
        await self.halo.hass.async_block_till_done()
        await self.halo.tick()
        self.follow_commands()
        self.show()
        await self.halo.hass.async_block_till_done()

    async def run(self, minutes: float, seconds: float = 30.0) -> None:
        for _ in range(int(minutes * 60 / seconds)):
            await self.step(seconds)

    def hour_kw(self, hour: datetime) -> float:
        joules, span = self.energy[hour]
        return joules / span / 1000.0

    def complete_hours(self) -> list[datetime]:
        return [hour for hour, (_, span) in self.energy.items() if span >= 3500.0]

    @property
    def decision(self):
        return self.halo.controller.regulator_decisions[self.halo.charger.entry_id]


async def _world(
    hass: HomeAssistant,
    monkeypatch,
    frozen,
    name: str,
    *,
    target_kw: float | None = 6.0,
    house_w: float = 1000.0,
    battery_entity: str | None = None,
) -> _World:
    halo = await _halo(hass, monkeypatch, name, l1=0.0, l2=0.0, l3=0.0, battery_entity=battery_entity)
    entry = hass.config_entries.async_get_entry(halo.controller.entry_id)
    if target_kw is not None:
        hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_IMPORT_TARGET_KW: target_kw})
    world = _World(halo=halo, frozen=frozen, house_a=house_w / (3 * _V))
    world.show()
    await hass.async_block_till_done()
    return world


async def _charging(world: _World, amps: int = 16) -> None:
    """The plan's window starts the car, which draws what it is given."""
    await world.halo.start(amps)
    world.follow_commands()
    world.halo.switch("on")
    world.show()
    await world.halo.hass.async_block_till_done()


# -- one car, a 6 kW target, a 1 kW house


async def test_a_16_a_car_is_held_so_every_hour_stays_under_the_target(hass: HomeAssistant, monkeypatch) -> None:
    with freeze_time(_HOUR + timedelta(seconds=5)) as frozen:
        world = await _world(hass, monkeypatch, frozen, "held")
        await world.step()
        await _charging(world)
        await world.run(150)

        hours = world.complete_hours()
        assert len(hours) == 2
        for hour in hours:
            assert world.hour_kw(hour) <= 6.0, f"{hour}: {world.hour_kw(hour):.2f} kW"
        # Held, never paused: 6 kW less the house's 1 kW leaves 7 A on three phases.
        assert world.turn_offs == 0
        assert world.car_a == 7.0
        assert world.decision.reason == REASON_IMPORT_TARGET_HELD
        assert world.halo.controller.load_balancing_limit(world.halo.charger.entry_id) == (7.0, LIMIT_CAUSE_IMPORT_TARGET)
        status = world.halo.controller.import_target_status(world.halo.charger.entry_id)
        assert status is not None and status.held_a == 7.0 and status.paused_until is None
        snapshot = world.halo.controller.import_target_snapshot()
        assert snapshot["not_applied"] is None
        assert snapshot["chargers"][world.halo.charger.entry_id]["action"] == "held"
        assert any(entry["action"] == "held" for entry in snapshot["log"])


async def test_without_a_target_nothing_is_held(hass: HomeAssistant, monkeypatch) -> None:
    with freeze_time(_HOUR + timedelta(seconds=5)) as frozen:
        world = await _world(hass, monkeypatch, frozen, "free", target_kw=None)
        await world.step()
        await _charging(world)
        await world.run(10)
        assert world.car_a == 16.0
        assert world.halo.controller.import_target_status(world.halo.charger.entry_id) is None


# -- the minimum, and one pause per hour


async def test_below_the_minimum_the_car_is_paused_once_until_the_next_hour_and_resumed_then(
    hass: HomeAssistant, monkeypatch
) -> None:
    with freeze_time(_HOUR + timedelta(seconds=5)) as frozen:
        world = await _world(hass, monkeypatch, frozen, "pause")
        await world.step()
        await _charging(world)
        await world.run(40)
        assert world.car_a == 7.0
        # The house takes 5 kW: the hour can no longer end under 6 kW with the car at its 6 A minimum.
        world.house_a = 5000.0 / (3 * _V)
        await world.run(5)
        assert world.turn_offs == 1
        assert world.halo.cc.paused_by_balancing
        assert any(entry["reason"] == REASON_IMPORT_TARGET_PAUSED for entry in world.halo.log())
        pause = world.halo.controller.balancing_pause(world.halo.charger.entry_id)
        assert pause is not None and pause.cause == LIMIT_CAUSE_IMPORT_TARGET
        assert pause.retry_at == _HOUR + timedelta(hours=1)
        status = world.halo.controller.import_target_status(world.halo.charger.entry_id)
        assert status is not None and status.paused_until == _HOUR + timedelta(hours=1)
        # The house falls back to 1 kW: the budget has room for the minimum again, but the car stays paused to the
        # end of the hour (one pause per hour, no flapping).
        world.house_a = 1000.0 / (3 * _V)
        await world.run(14)
        assert dt_util.utcnow() < _HOUR + timedelta(hours=1)
        assert world.car_a == 0.0 and world.turn_ons == 1
        # The next hour: resumed after the resume's dwell, and held to what the new hour leaves it.
        await world.run(5)
        assert world.turn_ons == 2
        await world.run(10)
        assert world.car_a == 7.0
        assert world.turn_offs == 1
        for hour in world.complete_hours():
            assert world.hour_kw(hour) <= 6.0


async def test_a_house_alone_over_the_target_pauses_the_car_and_says_so(hass: HomeAssistant, monkeypatch) -> None:
    with freeze_time(_HOUR + timedelta(seconds=5)) as frozen:
        world = await _world(hass, monkeypatch, frozen, "house", house_w=7000.0)
        await world.step()
        await _charging(world)
        await world.run(10)
        # Nothing beyond the car is held back: the car is paused, the house goes on above the target.
        assert world.turn_offs == 1 and world.car_a == 0.0
        status = world.halo.controller.import_target_status(world.halo.charger.entry_id)
        assert status is not None and status.house_over
        assert status.paused_until == _HOUR + timedelta(hours=1)


# -- the departure wins


def _plan(now: datetime, departure: datetime, windows: list[tuple[datetime, datetime]]) -> ChargingPlan:
    return ChargingPlan(
        start=windows[0][0].isoformat(),
        end=windows[-1][1].isoformat(),
        amps=16,
        phases=3,
        power_kw=11.04,
        periods=[{"start": start.isoformat(), "end": end.isoformat()} for start, end in windows],
        departure=departure.isoformat(),
    )


async def test_a_departure_the_held_power_cannot_meet_sets_the_target_aside(hass: HomeAssistant, monkeypatch) -> None:
    with freeze_time(_HOUR + timedelta(seconds=5)) as frozen:
        world = await _world(hass, monkeypatch, frozen, "leave")
        await world.step()
        now = dt_util.utcnow()
        departure = now + timedelta(hours=1, minutes=30)
        world.halo.cc.plan = _plan(now, departure, [(now, departure)])
        await _charging(world)
        await world.run(5)
        # 16.5 kWh still planned; 4.8 kW for 90 minutes is 7.2: the car runs unheld, and the status says why.
        assert world.car_a == 16.0
        assert world.decision.reason != REASON_IMPORT_TARGET_HELD
        status = world.halo.controller.import_target_status(world.halo.charger.entry_id)
        assert status is not None and status.released_for == departure.isoformat() and status.held_a is None
        snapshot = world.halo.controller.import_target_snapshot()
        assert snapshot["released"] == {world.halo.charger.entry_id: departure.isoformat()}
        assert any(entry["action"] == "released" for entry in snapshot["log"])


async def test_a_departure_the_held_power_still_meets_keeps_the_hold(hass: HomeAssistant, monkeypatch) -> None:
    with freeze_time(_HOUR + timedelta(seconds=5)) as frozen:
        world = await _world(hass, monkeypatch, frozen, "stay")
        await world.step()
        now = dt_util.utcnow()
        departure = now + timedelta(hours=5)
        # 90 minutes of windows (16.5 kWh): the target's 5 kW over five hours carries it.
        world.halo.cc.plan = _plan(now, departure, [(now, now + timedelta(minutes=90))])
        await _charging(world)
        await world.run(5)
        assert world.car_a == 7.0
        status = world.halo.controller.import_target_status(world.halo.charger.entry_id)
        assert status is not None and status.released_for is None


# -- a person's Start, and the fuse


async def test_a_persons_start_is_not_held(hass: HomeAssistant, monkeypatch) -> None:
    with freeze_time(_HOUR + timedelta(seconds=5)) as frozen:
        world = await _world(hass, monkeypatch, frozen, "person")
        await world.step()
        assert await world.halo.cc.async_start(16, manual=True)
        world.follow_commands()
        world.halo.switch("on")
        world.show()
        await hass.async_block_till_done()
        await world.run(10)
        assert world.car_a == 16.0
        assert world.halo.controller.import_target_snapshot()["chargers"][world.halo.charger.entry_id]["action"] == (
            "unheld"
        )


async def test_the_fuse_still_wins_where_it_is_lower_than_the_target(hass: HomeAssistant, monkeypatch) -> None:
    with freeze_time(_HOUR + timedelta(seconds=5)) as frozen:
        # A 20 kW target leaves the car its 16 A; a house of 12 A a phase on the 20 A fuse leaves it 8.
        world = await _world(hass, monkeypatch, frozen, "fuse", target_kw=20.0, house_w=12 * 3 * _V)
        await world.step()
        await _charging(world)
        await world.run(10)
        assert world.car_a <= 8.0
        assert world.decision.reason not in (REASON_IMPORT_TARGET_HELD, REASON_IMPORT_TARGET_PAUSED)
        assert world.halo.controller.load_balancing_limit(world.halo.charger.entry_id)[1] != LIMIT_CAUSE_IMPORT_TARGET


async def test_an_overload_is_never_softened_by_the_target(hass: HomeAssistant, monkeypatch) -> None:
    with freeze_time(_HOUR + timedelta(seconds=5)) as frozen:
        world = await _world(hass, monkeypatch, frozen, "overload", target_kw=20.0)
        await world.step()
        await _charging(world)
        await world.run(2)
        # The house jumps to 18 A a phase: far over the 20 A fuse with the car at 16 A.
        world.house_a = 18.0
        world.show()
        await hass.async_block_till_done()
        await world.halo.tick()
        decision = world.decision
        assert decision.overload or decision.reason == "reducing_current_due_to_active_import_overload"
        assert decision.proposed_current_a is not None and decision.proposed_current_a < 16.0


# -- measurement


async def test_a_meter_gap_holds_nothing_for_the_rest_of_that_hour(hass: HomeAssistant, monkeypatch) -> None:
    with freeze_time(_HOUR + timedelta(seconds=5)) as frozen:
        world = await _world(hass, monkeypatch, frozen, "gap")
        await world.step()
        await _charging(world)
        await world.run(10)
        assert world.car_a == 7.0
        world.silent = True
        await world.run(12)
        world.silent = False
        await world.run(3)
        snapshot = world.halo.controller.import_target_snapshot()
        assert snapshot["not_applied"] == "gap"
        assert world.decision.reason not in (REASON_IMPORT_TARGET_HELD, REASON_IMPORT_TARGET_PAUSED)
        # The next hour is measured whole again: held again.
        await world.run(40)
        assert world.halo.controller.import_target_snapshot()["not_applied"] is None
        assert world.decision.reason == REASON_IMPORT_TARGET_HELD


async def test_with_load_balancing_off_the_target_is_not_applied(hass: HomeAssistant, monkeypatch) -> None:
    with freeze_time(_HOUR + timedelta(seconds=5)) as frozen:
        world = await _world(hass, monkeypatch, frozen, "off")
        world.halo.controller.config["active_control_enabled"] = False
        await world.step()
        snapshot = world.halo.controller.import_target_snapshot()
        assert snapshot["not_applied"] == "load_balancing_off"
        assert world.halo.controller.import_target_status(world.halo.charger.entry_id) is None


# -- a home battery under `battery_first`


@pytest.mark.parametrize(("priority", "held_a"), [("car_first", 7.0), ("battery_first", 0.0)])
async def test_a_discharging_battery_counts_as_import_only_under_battery_first(
    hass: HomeAssistant, monkeypatch, priority: str, held_a: float
) -> None:
    with freeze_time(_HOUR + timedelta(seconds=5)) as frozen:
        name = f"battery{priority.split('_')[0]}"
        world = await _world(
            hass, monkeypatch, frozen, name, house_w=4000.0, battery_entity=f"sensor.{name}_battery_power"
        )
        world.halo.controller.config["solar_priority"] = priority
        # The battery covers 3 kW of the house's 4 kW: the grid sees 1 kW.
        world.battery_w = -3000.0
        world.extra_a = -3000.0 / (3 * _V)
        world.show()
        await world.step()
        await _charging(world)
        await world.run(10)
        assert world.car_a == held_a


# -- a restart in the middle of an hour


async def test_a_restart_mid_hour_keeps_the_hour_and_the_hold(hass: HomeAssistant, monkeypatch) -> None:
    from custom_components.spotnav.site.demand_tracker import SiteDemand
    from custom_components.spotnav.site.regulator_damping import RegulatorDamper

    with freeze_time(_HOUR + timedelta(seconds=5)) as frozen:
        world = await _world(hass, monkeypatch, frozen, "restart")
        await world.step()
        await _charging(world)
        await world.run(30)
        assert world.car_a == 7.0
        controller = world.halo.controller
        before = controller.demand.tracker.running
        assert before is not None
        # Home Assistant stops: the hour kept so far is written, and the site's memory is gone.
        await controller.demand.async_close()
        frozen.tick(timedelta(minutes=3))
        world.halo.advance(180.0)
        controller.demand = SiteDemand(hass, controller.entry_id, controller.demand_zone())
        await controller.demand.async_load()
        controller._target_paused_hour.clear()  # noqa: SLF001
        controller._target_released.clear()  # noqa: SLF001
        controller._dampers[world.halo.charger.entry_id] = RegulatorDamper(now=world.halo.damper_clock)  # noqa: SLF001
        controller._yield_steppers.clear()  # noqa: SLF001
        restored = controller.demand.tracker.running
        assert restored is not None and restored.start == before.start
        assert restored.import_wh == pytest.approx(before.import_wh, abs=1.0)
        await world.run(60)
        for hour in world.complete_hours():
            assert world.hour_kw(hour) <= 6.0
        assert world.car_a == 7.0
