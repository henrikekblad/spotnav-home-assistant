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
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from freezegun import freeze_time
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed, async_mock_service

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
    #: The charger's energy register (kWh), kept with what the car drew, where the test reads one.
    register: str | None = None
    delivered_kwh: float = 0.0
    #: Fire Home Assistant's timers on every step (the plan's windows and its watch run on them).
    timers: bool = False
    #: When a person released the import target (`_released_by_hand`).
    released_at: datetime | None = None
    #: The phone's notify calls and the plan's departure, where a test sets them.
    calls: list = field(default_factory=list)
    departure: datetime | None = None

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
        self.delivered_kwh += 3 * _V * self.car_a * seconds / 3600.0 / 1000.0
        self.frozen.tick(timedelta(seconds=seconds))
        self.halo.advance(seconds)
        if self.register is not None:
            self.halo.hass.states.async_set(
                self.register,
                str(round(self.delivered_kwh, 4)),
                {"unit_of_measurement": "kWh", "device_class": "energy", "state_class": "total_increasing"},
                force_update=True,
            )
        if self.timers:
            async_fire_time_changed(self.halo.hass, dt_util.utcnow())
            await self.halo.hass.async_block_till_done()
            self.follow_commands()
        self.show()
        await self.halo.hass.async_block_till_done()
        await self.halo.tick()
        self.follow_commands()
        self.show()
        await self.halo.hass.async_block_till_done()
        self.meter_reports()

    def meter_reports(self) -> None:
        """The meter reports what the commands did at once (a real one reports every few seconds; this world steps by
        half minutes): the hour tracker reads it now, as the meter's energy here counts it from now."""
        controller = self.halo.controller
        controller.result = controller._calculate()  # noqa: SLF001
        controller.demand._last_observed = None  # noqa: SLF001
        controller._observe_demand()  # noqa: SLF001

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
    world.meter_reports()


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
            # Under the target less its margin (the larger of 0.1 kW and 2 %): 5.88 kW.
            assert world.hour_kw(hour) <= 5.88, f"{hour}: {world.hour_kw(hour):.2f} kW"
        # Held, never paused: 5.88 kW less the house's 1 kW leaves 7 A on three phases.
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
        # What the dashboard's status is composed from (the composition: `tests/test_status_compose.py`).
        from custom_components.spotnav.api import dashboard as dashboard_api

        facts = dashboard_api.status_facts(dashboard_api.capture_dashboard(hass, world.halo.charger))
        assert facts.load_balancing is not None and facts.load_balancing.cause == LIMIT_CAUSE_IMPORT_TARGET
        assert facts.load_balancing.proposed_current_a == 7.0
        assert facts.import_target is not None and facts.import_target.target_kw == 6.0
        assert 4.0 < facts.import_target.hour_kw <= 6.0
        # Each hour is marked held for the power view, none paused.
        tracker = world.halo.controller.demand.tracker
        assert [(hour.held, hour.paused) for hour in tracker.hours] == [(True, False), (True, False)]


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
        # The power view marks the first hour held and paused; the running one is held again.
        tracker = world.halo.controller.demand.tracker
        assert [(hour.start, hour.held, hour.paused) for hour in tracker.hours] == [(_HOUR, True, True)]
        assert tracker.running is not None and (tracker.running.held, tracker.running.paused) == (True, False)


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
        auto_identity="0123456789abcdef0123456789abcdef",
    )


async def _tight_night(hass: HomeAssistant, monkeypatch, frozen, name: str, choice: str = "ask") -> _World:
    """The car held at 7 A with 16.5 kWh to deliver in 90 minutes: the target makes it miss its departure."""
    from dataclasses import replace as replace_settings

    from custom_components.spotnav.runtime import domain_data

    from .test_notifications import _choose

    world = await _world(hass, monkeypatch, frozen, name)
    charger = world.halo.charger.entry_id
    await domain_data(hass).auto_store.async_update(
        charger, mutate=lambda settings: replace_settings(settings, if_not_ready=choice)
    )
    world.calls = async_mock_service(hass, "notify", "mobile_app_pixel")
    await _choose(hass, charger, targets=("mobile_app_pixel",), events=("plan_at_risk",))
    await world.step()
    now = dt_util.utcnow()
    departure = now + timedelta(hours=1, minutes=30)
    world.halo.cc.plan = _plan(now, departure, [(now, departure)])
    world.departure = departure
    await _charging(world)
    return world


def _told(world: _World) -> list[dict]:
    return [call.data for call in world.calls if call.data.get("message") != "clear_notification"]


async def test_ask_holds_the_target_and_tells_once_with_a_button(hass: HomeAssistant, monkeypatch) -> None:
    with freeze_time(_HOUR + timedelta(seconds=5)) as frozen:
        world = await _tight_night(hass, monkeypatch, frozen, "ask")
        await world.run(10)
        charger = world.halo.charger.entry_id
        # Nothing set aside by itself: the target holds, and the departure is said to be at risk.
        assert world.car_a == 7.0
        snapshot = world.halo.controller.import_target_snapshot()
        assert snapshot["released"] == {} and snapshot["released_by_plan"] == {}
        assert snapshot["at_risk"][charger]["choice"] == "ask"
        status = world.halo.controller.import_target_status(charger)
        assert status is not None and status.at_risk is not None and status.at_risk.missing_kwh > 5
        told = _told(world)
        assert len(told) == 1, "told once, not on every recompute"
        assert "6.0 kW" in told[0]["message"] and "kWh short" in told[0]["message"]
        actions = told[0]["data"]["actions"]
        stamp = int(world.departure.timestamp())
        assert actions == [{"action": f"SPOTNAV_IMPORT_TARGET_RELEASE_{charger}_{stamp}", "title": "Release the import target tonight"}]
        assert told[0]["data"]["tag"] == f"spotnav_{charger}_plan_at_risk"
        # The phone's button sets the target aside until the departure.
        hass.bus.async_fire("mobile_app_notification_action", {"action": actions[0]["action"]})
        await hass.async_block_till_done()
        await world.run(3)
        assert world.car_a == 16.0
        status = world.halo.controller.import_target_status(charger)
        assert status is not None and status.released_for == world.departure.isoformat()
        assert not status.released_automatically and status.at_risk is None
        # The notification is taken off the phones.
        assert any(call.data.get("message") == "clear_notification" for call in world.calls)


async def test_the_shortfall_is_told_again_only_when_it_grows_clearly(hass: HomeAssistant, monkeypatch) -> None:
    from custom_components.spotnav.site.site_capacity_controller import TargetRisk

    with freeze_time(_HOUR + timedelta(seconds=5)) as frozen:
        world = await _tight_night(hass, monkeypatch, frozen, "grows")
        await world.run(2)
        from custom_components.spotnav.runtime import charger_data

        notifier = charger_data(hass, world.halo.charger.entry_id).notifier
        departure = world.departure.isoformat()
        assert len(_told(world)) == 1
        notifier.import_target_risk(TargetRisk(departure=departure, missing_kwh=9.0, target_kw=6.0))
        notifier.import_target_risk(TargetRisk(departure=departure, missing_kwh=9.4, target_kw=6.0))
        await hass.async_block_till_done()
        assert len(_told(world)) == 1, "a few hundred watt-hours more is no news"
        notifier._last_sent.clear()  # noqa: SLF001
        notifier.import_target_risk(TargetRisk(departure=departure, missing_kwh=20.0, target_kw=6.0))
        await hass.async_block_till_done()
        assert len(_told(world)) == 2, "a shortfall that grew clearly is told again"
        notifier.import_target_risk(None)
        await hass.async_block_till_done()
        assert world.calls[-1].data["message"] == "clear_notification", "taken off once it can be met again"


async def test_release_lets_the_departure_win_by_itself(hass: HomeAssistant, monkeypatch) -> None:
    with freeze_time(_HOUR + timedelta(seconds=5)) as frozen:
        world = await _tight_night(hass, monkeypatch, frozen, "release", choice="release")
        await world.run(5)
        charger = world.halo.charger.entry_id
        assert world.car_a == 16.0
        status = world.halo.controller.import_target_status(charger)
        assert status is not None and status.released_for == world.departure.isoformat()
        assert status.released_automatically and status.at_risk is None
        assert _told(world) == [], "nothing to ask"
        # The plan counts on the car's own power: the departure wins.
        assert world.halo.controller.import_target_plan_kw(charger) is None


async def test_hold_keeps_the_target_and_only_says_what_will_be_missing(hass: HomeAssistant, monkeypatch) -> None:
    with freeze_time(_HOUR + timedelta(seconds=5)) as frozen:
        world = await _tight_night(hass, monkeypatch, frozen, "holdit", choice="hold")
        await world.run(10)
        charger = world.halo.charger.entry_id
        assert world.car_a == 7.0
        status = world.halo.controller.import_target_status(charger)
        assert status is not None and status.at_risk is not None and status.at_risk.choice == "hold"
        assert _told(world) == []
        from custom_components.spotnav.api import dashboard as dashboard_api

        facts = dashboard_api.status_facts(dashboard_api.capture_dashboard(hass, world.halo.charger))
        assert facts.import_target is not None and facts.import_target.releasable is False


async def test_a_release_holds_until_the_departure_and_survives_a_restart(hass: HomeAssistant, monkeypatch) -> None:
    from custom_components.spotnav.api.import_target import release_import_target
    from custom_components.spotnav.site.site_capacity_controller import SiteCapacityController

    with freeze_time(_HOUR + timedelta(seconds=5)) as frozen:
        world = await _tight_night(hass, monkeypatch, frozen, "wsrelease")
        await world.run(2)
        charger = world.halo.charger.entry_id
        # What the card's command and the app's webhook action do.
        assert release_import_target(hass, world.halo.charger) is None
        await world.run(2)
        assert world.car_a == 16.0
        # Home Assistant restarts: what was set aside is read back.
        controller = world.halo.controller
        await controller._target_release_store.async_save({"released": dict(controller._target_released)})  # noqa: SLF001
        fresh = SiteCapacityController(hass, controller.entry_id, dict(controller.config))
        await fresh._async_load_released()  # noqa: SLF001
        assert fresh._target_released == {charger: world.departure.isoformat()}  # noqa: SLF001
        # The departure passes: the set-aside ends.
        frozen.tick(timedelta(hours=2))
        world.halo.advance(7200.0)
        controller._prune_released(dt_util.utcnow())  # noqa: SLF001
        assert controller._target_released == {}  # noqa: SLF001


async def test_a_start_after_the_release_keeps_it_until_the_unplug(hass: HomeAssistant, monkeypatch) -> None:
    from custom_components.spotnav.site.site_capacity_controller import _unplug_release_since

    with freeze_time(_HOUR + timedelta(seconds=5)) as frozen:
        world = await _tight_night(hass, monkeypatch, frozen, "startafter")
        await world.run(2)
        charger = world.halo.charger.entry_id
        controller = world.halo.controller
        assert controller.async_release_import_target(charger) is None
        await world.run(2)
        assert controller._target_released == {charger: world.departure.isoformat()}  # noqa: SLF001
        # A person presses Start: the plan is dropped, the release is not.
        await _start_by_hand(world)
        assert world.halo.cc.plan is None
        await world.run(3)
        kept = controller._target_released[charger]  # noqa: SLF001
        assert _unplug_release_since(kept) is not None, kept
        assert world.car_a == 16.0, "still set aside: the Start charges at full power"
        status = controller.import_target_status(charger)
        assert status is not None and status.released_for == kept


async def test_a_hand_edited_release_store_never_breaks_a_recompute(hass: HomeAssistant, monkeypatch) -> None:
    from custom_components.spotnav.site.site_capacity_controller import (
        _unplug_release_since,
        SiteCapacityController,
        unplug_release_value,
    )

    with freeze_time(_HOUR + timedelta(seconds=5)) as frozen:
        world, controller, charger = await _released_by_hand(hass, monkeypatch, frozen, "handedit")
        await controller._target_release_store.async_save(  # noqa: SLF001
            {
                "released": {
                    charger: "until_unplug/2026-11-02T12:00:00",  # no offset: taken as UTC
                    "naive_departure": "2026-11-02T20:00:00",
                    "garbage": "until_unplug/garbage",
                    "bad_month": "2026-13-02T20:00:00+00:00",
                    "number": 7,
                }
            }
        )
        fresh = SiteCapacityController(hass, controller.entry_id, dict(controller.config))
        await fresh._async_load_released()  # noqa: SLF001
        released = fresh._target_released  # noqa: SLF001
        assert set(released) == {charger, "naive_departure"}
        assert released[charger] == unplug_release_value(datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc))
        assert dt_util.parse_datetime(released["naive_departure"]).tzinfo is not None
        assert _unplug_release_since("until_unplug/2026-11-02T12:00:00").tzinfo is not None
        # The live site, given the same hand-edited values, recomputes without raising.
        controller._target_released = {charger: "until_unplug/2026-11-02T12:00:00", "x": "2026-11-02T20:00:00"}  # noqa: SLF001
        controller._prune_released(dt_util.utcnow())  # noqa: SLF001
        await world.run(1)


async def _released_by_hand(hass: HomeAssistant, monkeypatch, frozen, name: str):
    """A person's Start held at 7 A, then released without a departure: the world, its site and the charger's id."""
    world = await _world(hass, monkeypatch, frozen, name)
    await world.step()
    await _start_by_hand(world)
    await world.run(3)
    assert world.car_a == 7.0, "a person's Start is held like any charge"
    controller = world.halo.controller
    charger = world.halo.charger.entry_id
    # No plan, no departure: released until the car is unplugged (by noon at the latest).
    world.released_at = dt_util.utcnow()
    assert controller.async_release_import_target(charger) is None
    await world.run(3)
    assert world.car_a == 16.0
    return world, controller, charger


async def test_a_release_without_a_departure_lasts_until_unplug_and_survives_a_restart(
    hass: HomeAssistant, monkeypatch
) -> None:
    from custom_components.spotnav.execution.chargers.adapter import ChargerAdapter
    from custom_components.spotnav.site.site_capacity_controller import (
        SiteCapacityController,
        unplug_release_value,
    )

    with freeze_time(_HOUR + timedelta(seconds=5)) as frozen:
        world, controller, charger = await _released_by_hand(hass, monkeypatch, frozen, "nodeparture")
        # Kept with the moment it was given.
        kept = unplug_release_value(world.released_at)
        assert controller.import_target_snapshot()["released"] == {charger: kept}
        status = controller.import_target_status(charger)
        assert status is not None and status.released_for == kept
        # A restart reads it back.
        await controller._target_release_store.async_save({"released": dict(controller._target_released)})  # noqa: SLF001
        fresh = SiteCapacityController(hass, controller.entry_id, dict(controller.config))
        await fresh._async_load_released()  # noqa: SLF001
        assert fresh._target_released == {charger: kept}  # noqa: SLF001
        # An hour on (before noon) it still holds; the unplug ends it.
        frozen.tick(timedelta(hours=1))
        world.halo.advance(3600.0)
        controller._prune_released(dt_util.utcnow())  # noqa: SLF001
        assert controller._target_released == {charger: kept}  # noqa: SLF001
        monkeypatch.setattr(ChargerAdapter, "vehicle_connected", lambda self: False)
        controller._prune_released(dt_util.utcnow())  # noqa: SLF001
        assert controller._target_released == {}  # noqa: SLF001


async def test_a_release_until_unplug_ends_at_noon_on_a_charger_without_a_plug_signal(
    hass: HomeAssistant, monkeypatch
) -> None:
    from custom_components.spotnav.execution.chargers.adapter import ChargerAdapter
    from custom_components.spotnav.site.site_capacity_controller import unplug_release_latest

    with freeze_time(_HOUR + timedelta(seconds=5)) as frozen:
        world, controller, charger = await _released_by_hand(hass, monkeypatch, frozen, "noplug")
        # The charger never reports the plug: neither plugged in nor out.
        monkeypatch.setattr(ChargerAdapter, "vehicle_connected", lambda self: None)
        noon = unplug_release_latest(world.released_at)
        assert dt_util.as_local(noon).hour == 12 and noon > dt_util.utcnow()
        assert noon - dt_util.utcnow() <= timedelta(days=1)
        controller._prune_released(noon - timedelta(minutes=1))  # noqa: SLF001
        assert charger in controller._target_released  # noqa: SLF001
        controller._prune_released(noon)  # noqa: SLF001
        assert controller._target_released == {}  # noqa: SLF001


def test_an_until_unplug_release_ends_at_the_first_local_noon_six_hours_after_it() -> None:
    from custom_components.spotnav.site.site_capacity_controller import unplug_release_latest

    zone = dt_util.get_default_time_zone()
    evening = datetime(2026, 11, 2, 22, 0, tzinfo=zone)
    assert unplug_release_latest(evening) == datetime(2026, 11, 3, 12, 0, tzinfo=zone)
    early = datetime(2026, 11, 3, 5, 30, tzinfo=zone)
    assert unplug_release_latest(early) == datetime(2026, 11, 3, 12, 0, tzinfo=zone)
    # At least six hours: one given at 09:30 or 11:59 lasts to the next day's noon.
    morning = datetime(2026, 11, 3, 9, 30, tzinfo=zone)
    assert unplug_release_latest(morning) == datetime(2026, 11, 4, 12, 0, tzinfo=zone)
    late = datetime(2026, 11, 3, 11, 59, tzinfo=zone)
    assert unplug_release_latest(late) == datetime(2026, 11, 4, 12, 0, tzinfo=zone)
    at_six = datetime(2026, 11, 3, 6, 0, tzinfo=zone)
    assert unplug_release_latest(at_six) == datetime(2026, 11, 3, 12, 0, tzinfo=zone)


async def test_a_release_until_unplug_ends_when_the_car_ends_the_charge(hass: HomeAssistant, monkeypatch) -> None:
    from custom_components.spotnav.execution.controller import ChargingController

    with freeze_time(_HOUR + timedelta(seconds=5)) as frozen:
        world, controller, charger = await _released_by_hand(hass, monkeypatch, frozen, "carended")
        # A charge the car ended before the release does not end it.
        before = _HOUR
        monkeypatch.setattr(ChargingController, "car_ended_at", property(lambda self: before))
        controller._prune_released(dt_util.utcnow())  # noqa: SLF001
        assert charger in controller._target_released  # noqa: SLF001
        # The car ends this charge by itself (full, or at its limit): the release is over.
        frozen.tick(timedelta(minutes=40))
        world.halo.advance(2400.0)
        ended = dt_util.utcnow()
        monkeypatch.setattr(ChargingController, "car_ended_at", property(lambda self: ended))
        controller._prune_released(dt_util.utcnow())  # noqa: SLF001
        assert controller._target_released == {}  # noqa: SLF001


async def test_the_floor_charge_is_never_held(hass: HomeAssistant, monkeypatch) -> None:
    with freeze_time(_HOUR + timedelta(seconds=5)) as frozen:
        world = await _world(hass, monkeypatch, frozen, "floor")
        await world.step()
        await _charging(world)
        await world.run(2)
        assert world.car_a <= 7.0, "held under the target less its margin"
        # The car's minimum level's charge (below it SpotNav charges at once): the floor wins over the target.
        monkeypatch.setattr(type(world.halo.cc), "charge_origin", property(lambda self: "min_soc"))
        await world.run(3)
        assert world.car_a == 16.0
        assert world.halo.controller.import_target_snapshot()["chargers"][world.halo.charger.entry_id]["action"] == (
            "unheld"
        )


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
        assert status is not None and status.released_for is None and status.at_risk is None


# -- a person's Start, and the fuse


async def _start_by_hand(world: _World) -> None:
    """A person's Start at 16 A, through the charger's authority boundary as the card's Start goes: it stores the
    manual pause (`PauseIntent` choice `manual`, action `start`) the status reads."""
    from custom_components.spotnav.runtime import executor_for

    executor = executor_for(world.halo.hass, world.halo.charger.entry_id)
    assert executor is not None
    await executor.async_manual_start(16)
    pause = executor._store.settings(world.halo.charger.entry_id).pause  # noqa: SLF001
    assert (pause.choice, pause.action) == ("manual", "start")
    world.follow_commands()
    world.halo.switch("on")
    world.show()
    await world.halo.hass.async_block_till_done()
    world.meter_reports()


async def test_a_persons_start_is_held_and_paused_like_a_plans_charge(hass: HomeAssistant, monkeypatch) -> None:
    from custom_components.spotnav.api import dashboard as dashboard_api
    from custom_components.spotnav.planning.status_compose import compose_status, PlanningFacts

    # This world's charger has no plan settings: its status is composed as for one that has them.
    ready = {"has_settings": True, "planning": PlanningFacts(state="proposal_ready", reason="ready")}

    with freeze_time(_HOUR + timedelta(seconds=5)) as frozen:
        world = await _world(hass, monkeypatch, frozen, "person")
        await world.step()
        await _start_by_hand(world)
        await world.run(10)
        charger = world.halo.charger.entry_id
        assert world.halo.cc.charge_origin == "manual"
        assert world.car_a == 7.0, "Start charges now, within the target"
        assert world.halo.controller.import_target_snapshot()["chargers"][charger]["action"] == "held"
        facts = dashboard_api.status_facts(dashboard_api.capture_dashboard(hass, world.halo.charger))
        lines = compose_status(replace(facts, **ready))["lines"]
        held = next(line for line in lines if line["code"] == "import_target_held")
        # The one way past the target, offered with the line: until the unplug (no departure), at an estimated peak.
        assert held["params"]["releasable"] is True and held["params"]["release_until"] is None
        assert held["params"]["release_peak_kw"] == 12.0  # the house's 1 kW and 16 A on three phases
        # The house takes 5 kW: paused until the next hour, and said so though no plan's window runs.
        world.house_a = 5000.0 / (3 * _V)
        await world.run(3)
        assert world.turn_offs == 1
        facts = dashboard_api.status_facts(dashboard_api.capture_dashboard(hass, world.halo.charger))
        lines = compose_status(replace(facts, **ready))["lines"]
        # Under the person's own "Charging manually" line, with the release offered.
        assert [line["code"] for line in lines[:2]] == ["paused", "import_target_paused"], lines
        assert lines[1]["params"]["releasable"] is True
        # No phone is told for a held Start: nothing about a departure is at risk.
        assert world.halo.controller.import_target_snapshot()["at_risk"] == {}


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


# -- a switch-only charger


async def _switch_only_hour(
    hass: HomeAssistant,
    monkeypatch,
    frozen,
    name: str,
    target_kw: float,
    *,
    minutes: int = 10,
    plug: bool = True,
    house_w: Any = None,
) -> Any:
    """A site of one switch-only charger drawing 10 A on three phases (6.9 kW) beside a 0.5 kW house, for `minutes`
    of readings every 30 seconds. `plug` false: a plain switch that cannot say whether a car is plugged in. `house_w`:
    the house's power (W) at each reading's index, where it changes."""
    from custom_components.spotnav.execution.chargers.adapter import ChargerAdapter

    from .test_switch_only_balancing import _charging as _switch_charging, _world as _switch_world

    world = await _switch_world(hass, monkeypatch, name, commandable=False)
    if not plug:
        monkeypatch.setattr(ChargerAdapter, "vehicle_connected", lambda self: None)
        monkeypatch.setattr(ChargerAdapter, "reports_connection", property(lambda self: False))
    entry = hass.config_entries.async_get_entry(world.site.entry_id)
    hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_IMPORT_TARGET_KW: target_kw})
    house_a = 500.0 / (3 * _V)
    await _switch_charging(world, "b", 10.0)
    for index in range(minutes * 2):
        on = hass.states.get(f"switch.{name}_b").state == "on"
        house = house_a if house_w is None else house_w(index) / (3 * _V)
        amps = house + (10.0 if on else 0.0)
        for phase in ("l1", "l2", "l3"):
            for kind, value, unit in (("power", amps * _V, "W"), ("reactive", 0, "var"), ("voltage", _V, "V")):
                hass.states.async_set(
                    f"sensor.{name}_site_{kind}_{phase}", str(value), {"unit_of_measurement": unit}, force_update=True
                )
            hass.states.async_set(
                f"sensor.{name}_b_{phase}", str(10.0 if on else 0.0), {"unit_of_measurement": "A"}, force_update=True
            )
        frozen.tick(timedelta(seconds=30))
        world.advance(30.0)
        await world.tick()
        if world.stops("b") > world.starts("b") - 1:
            world.switch("b", "off")
        elif world.starts("b") > 1:
            world.switch("b", "on")
    return world


async def test_a_switch_only_charger_runs_only_while_its_whole_power_fits_the_hour(
    hass: HomeAssistant, monkeypatch
) -> None:
    with freeze_time(_HOUR + timedelta(seconds=5)) as frozen:
        fits = await _switch_only_hour(hass, monkeypatch, frozen, "plugfits", 8.0)
        assert fits.stops("b") == 0
    with freeze_time(_HOUR + timedelta(hours=2, seconds=5)) as frozen:
        over = await _switch_only_hour(hass, monkeypatch, frozen, "plugover", 6.0)
        assert over.stops("b") == 1
        assert any(entry["reason"] == REASON_IMPORT_TARGET_PAUSED for entry in over.site.regulator_decision_log)
        pause = over.site.balancing_pause(over.entry_id("b"))
        assert pause is not None and pause.cause == LIMIT_CAUSE_IMPORT_TARGET


async def test_a_switch_that_cannot_report_the_plug_is_resumed_the_next_hour(hass: HomeAssistant, monkeypatch) -> None:
    # A plain switch says nothing of a car: the target's pause ends at the next hour as for any charger, not never.
    # An 8 kW target; the house takes 3 kW for the hour's first ten minutes (the whole 6.9 kW no longer fits: paused
    # to the hour's end), then 0.5 kW, where it fits again from the next hour.
    with freeze_time(_HOUR + timedelta(seconds=5)) as frozen:
        world = await _switch_only_hour(
            hass, monkeypatch, frozen, "noplugsw", 8.0, minutes=70, plug=False,
            house_w=lambda index: 3000.0 if index < 20 else 500.0,
        )
        assert world.stops("b") == 1
        assert world.starts("b") >= 2, "resumed in the next hour"
        assert not world.cc("b").paused_by_balancing


# -- yield stepping never steps above the target, and never holds its lowering back


def _verdict(action: str, current_a: float | None, reason: str = "confirmed_step"):
    from custom_components.spotnav.execution.yield_stepping import YieldVerdict

    return YieldVerdict(
        action=action, current_a=current_a, reason=reason, urgent=False, state="confirmed", y=None, y_age_s=None,
        reference_a={},
    )


async def test_a_yield_step_is_taken_at_the_targets_ceiling_and_a_hold_never_keeps_its_lowering(
    hass: HomeAssistant, monkeypatch
) -> None:
    from custom_components.spotnav.site.import_target import TargetShare
    from custom_components.spotnav.site.regulator import RegulatorDecision

    with freeze_time(_HOUR + timedelta(seconds=5)) as frozen:
        world = await _world(hass, monkeypatch, frozen, "stepper")
        controller = world.halo.controller
        charger = world.halo.charger.entry_id
        under = controller._yield_verdict_under_target  # noqa: SLF001
        fuse = RegulatorDecision(10.0, "increase_within_safe_uncredited_margin", "L1")
        # Free under the target, with room for 12 A: a step to 14 A is taken at 12 A; one to 11 A as it is.
        controller._target_shares = {charger: TargetShare("free", None, 12.0)}  # noqa: SLF001
        assert under(charger, fuse, _verdict("write", 14.0), 10.0).current_a == 12.0
        assert under(charger, fuse, _verdict("write", 11.0), 10.0).current_a == 11.0
        # Room below the minimum: no step up is written at all (the raw proposal goes as it is)...
        controller._target_shares = {charger: TargetShare("free", None, 5.0)}  # noqa: SLF001
        assert under(charger, fuse, _verdict("write", 12.0), 10.0) is None
        # ... but a verdict that lowers the charger is always kept, an urgent revert above all.
        revert = replace(_verdict("write", 6.0, "revert_not_absorbed"), urgent=True)
        assert under(charger, fuse, revert, 10.0) is revert
        step_back = _verdict("write", 8.0, "transient_step_back")
        assert under(charger, fuse, step_back, 10.0) is step_back
        # The target's own lowering (7 A, below the 10 A written): a hold of the stepper's is set aside, a lower write
        # of its own is kept.
        held = RegulatorDecision(7.0, REASON_IMPORT_TARGET_HELD, "L1")
        controller._target_shares = {charger: TargetShare("held", 7.0, 7.0)}  # noqa: SLF001
        hold = _verdict("hold", None, "held_at_yield_setpoint")
        assert under(charger, held, hold, 10.0) is None
        assert under(charger, held, _verdict("write", 9.0), 10.0) is None
        assert under(charger, held, _verdict("write", 6.0), 10.0).current_a == 6.0
        # A decision the target held that is a raise of what is on the charger (12 A held, 7 A written) is no lowering:
        # the stepper's hold stands, as for any raise.
        raised = RegulatorDecision(12.0, REASON_IMPORT_TARGET_HELD, "L1")
        controller._target_shares = {charger: TargetShare("held", 12.0, 12.0)}  # noqa: SLF001
        assert under(charger, raised, hold, 7.0) is hold
        assert not controller._target_lowering(raised, 7.0)  # noqa: SLF001
        assert controller._target_lowering(held, 10.0)  # noqa: SLF001
        # Without a target nothing changes.
        controller._target_shares = {}  # noqa: SLF001
        assert under(charger, fuse, _verdict("write", 16.0), 10.0).current_a == 16.0


async def test_no_battery_probe_starts_a_car_the_target_holds_back(hass: HomeAssistant, monkeypatch) -> None:
    from .test_balancing_retry import _night, _outcomes, _paused_by_the_battery

    with freeze_time(_HOUR + timedelta(seconds=5)) as frozen:
        halo = await _night(hass, monkeypatch, "nightheld")
        entry = hass.config_entries.async_get_entry(halo.controller.entry_id)
        hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_IMPORT_TARGET_KW: 3.0})
        await _paused_by_the_battery(halo)
        # The battery fills the fuse at 20 A a phase: the hour is far over 3 kW, and the car stays paused.
        for _ in range(6):
            frozen.tick(timedelta(seconds=10))
            halo.advance(10.0)
            for phase, amps in (("l1", 20.2), ("l2", 20.1), ("l3", 20.0)):
                hass.states.async_set(
                    f"sensor.{halo.site}_power_{phase}", str(amps * _V), {"unit_of_measurement": "W"}, force_update=True
                )
            await halo.tick()
        assert halo.controller.import_target_snapshot()["chargers"][halo.charger.entry_id]["action"] == "paused"
        assert "probe_started" not in _outcomes(halo)


# -- an app that does not read the import target's lines


def test_an_app_that_does_not_read_the_targets_lines_gets_the_load_balancing_lines_it_knows() -> None:
    from custom_components.spotnav.api.webhook import _status_for_app

    held = {"code": "import_target_held", "params": {"limit_a": 7.0, "target_kw": 6.0, "hour_kw": 5.1}}
    paused = {
        "code": "import_target_paused",
        "params": {"until": "2026-11-02T13:00:00+00:00", "target_kw": 6.0, "house_over": False},
    }
    house = {"code": "import_target_house_over", "params": {"target_kw": 6.0, "set_kw": 6.0}}
    released = {"code": "import_target_released", "params": {"target_kw": 6.0, "departure": "2026-11-02T16:00:00+00:00"}}
    assert _status_for_app(held, set()) == {
        "code": "load_balancing_limited",
        "params": {"limit_a": 7.0, "phase": None, "cause": "import_target"},
    }
    assert _status_for_app(paused, set())["params"] == {"limit_a": 0, "phase": None, "cause": "import_target"}
    assert _status_for_app(paused, {"balancing_paused"}) == {
        "code": "balancing_paused",
        "params": {"retry_at": "2026-11-02T13:00:00+00:00", "cause": "import_target"},
    }
    assert _status_for_app(house, set()) is None
    assert _status_for_app(released, set()) is None
    for line in (held, paused, house, released):
        assert _status_for_app(line, {"import_target_status"}) == line


# -- what a plan counts on under the target


async def test_a_plan_counts_on_the_target_less_the_house_never_below_the_minimum(hass: HomeAssistant, monkeypatch) -> None:
    with freeze_time(_HOUR + timedelta(seconds=5)) as frozen:
        world = await _world(hass, monkeypatch, frozen, "plankw")
        charger = world.halo.charger.entry_id
        await world.step()
        await world.step()
        # The target less its margin (5.88 kW), less the house's kilowatt.
        assert world.halo.controller.import_target_plan_kw(charger) == pytest.approx(4.88, abs=0.01)
        # A kettle's half minute does not move it: the house's typical load does, not its load this instant.
        world.house_a = 4000.0 / (3 * _V)
        await world.step()
        assert world.halo.controller.import_target_plan_kw(charger) == pytest.approx(4.88, abs=0.1)
        # A house that stays at 4 kW leaves 2 kW: below the car's 6 A on three phases, all a held charge runs at.
        await world.run(30)
        assert world.halo.controller.import_target_plan_kw(charger) == pytest.approx(6 * 3 * _V / 1000.0)
    with freeze_time(_HOUR + timedelta(hours=3, seconds=5)) as frozen:
        free = await _world(hass, monkeypatch, frozen, "plannone", target_kw=None)
        await free.step()
        assert free.halo.controller.import_target_plan_kw(free.halo.charger.entry_id) is None


# -- review: the departure is decided on the house's typical load, every recompute


async def test_a_kettles_half_minute_never_sets_a_tight_plan_aside(hass: HomeAssistant, monkeypatch) -> None:
    with freeze_time(_HOUR + timedelta(seconds=5)) as frozen:
        world = await _world(hass, monkeypatch, frozen, "kettle")
        await world.step()
        now = dt_util.utcnow()
        # 30 kWh by a departure six hours on: the target's 5 kW (6 kW less the house's 1 kW) carries it, just.
        departure = now + timedelta(hours=6, minutes=30)
        world.halo.cc.plan = _plan(now, departure, [(now, now + timedelta(hours=2, minutes=43))])
        await _charging(world)
        await world.run(10)
        assert world.car_a == 7.0
        # The kettle: 3 kW more for half a minute.
        world.house_a = 4000.0 / (3 * _V)
        await world.step()
        world.house_a = 1000.0 / (3 * _V)
        await world.run(10)
        snapshot = world.halo.controller.import_target_snapshot()
        assert snapshot["at_risk"] == {}, snapshot["log"]
        assert not any(entry["action"] == "at_risk" for entry in snapshot["log"]), snapshot["log"]
        assert world.car_a == 7.0


# -- review: the last ten minutes keep what the target held, and claim nothing more


async def test_the_hours_end_keeps_a_held_car_where_it_was_without_claiming_the_budget_holds_it(
    hass: HomeAssistant, monkeypatch
) -> None:
    with freeze_time(_HOUR + timedelta(seconds=5)) as frozen:
        # A 13 kW target and a 6 kW house hold the car to 10 A (the fuse would give it 11).
        world = await _world(hass, monkeypatch, frozen, "hourend", target_kw=13.0, house_w=6000.0)
        await world.step()
        await _charging(world)
        await world.run(50)
        assert world.car_a == 10.0
        # Eight minutes left and the house gone quiet: the budget has room for the car's 16 A, the hour's end not.
        world.house_a = 0.0
        await world.run(4)
        charger = world.halo.charger.entry_id
        snapshot = world.halo.controller.import_target_snapshot()
        assert snapshot["chargers"][charger]["action"] == "free"
        assert world.car_a == 10.0
        assert snapshot["chargers"][charger]["kept_at_hour_end"] is True
        assert snapshot["chargers"][charger]["lowered"] is False
        assert world.halo.controller.load_balancing_limit(charger)[1] != LIMIT_CAUSE_IMPORT_TARGET
        status = world.halo.controller.import_target_status(charger)
        assert status is not None and status.held_a is None
        # The next hour raises it.
        await world.run(15)
        assert world.car_a > 10.0


async def test_the_hours_end_does_not_keep_a_car_the_fuse_lowered(hass: HomeAssistant, monkeypatch) -> None:
    with freeze_time(_HOUR + timedelta(minutes=50, seconds=5)) as frozen:
        # A 20 kW target never holds the car; a house of 12 A a phase on the 20 A fuse does, then goes.
        world = await _world(hass, monkeypatch, frozen, "fuseend", target_kw=20.0, house_w=12 * 3 * _V)
        await world.step()
        await _charging(world)
        await world.run(2)
        assert world.car_a <= 8.0
        world.house_a = 1000.0 / (3 * _V)
        await world.run(4)
        assert dt_util.utcnow() < _HOUR + timedelta(hours=1)
        assert world.car_a > 8.0, "the fuse's lowering is lifted at once, not kept to the hour's end"


# -- review: a charge the target does not hold, paused, takes nothing from the budget


async def test_a_paused_charge_is_counted_at_what_it_resumes_at_and_the_floors_at_nothing(hass: HomeAssistant, monkeypatch) -> None:
    from types import SimpleNamespace

    from custom_components.spotnav.site import site_capacity_controller as module
    from custom_components.spotnav.site.import_target import HourBudget

    with freeze_time(_HOUR + timedelta(seconds=5)) as frozen:
        world = await _world(hass, monkeypatch, frozen, "personpaused")
        controller = world.halo.controller
        request = controller._build_requests(credit=False)[0]  # noqa: SLF001
        paused = SimpleNamespace(
            charge_origin=None, paused_charge_origin="manual", paused_by_balancing=True, requested_current_a=16
        )
        monkeypatch.setattr(module, "controller_for", lambda _hass, _entry_id: paused)
        budget = HourBudget(
            target_kw=6.0, hour_start=_HOUR, hour_end=_HOUR + timedelta(hours=1), so_far_kwh=0.0, hour_kw=0.0,
            remaining_s=3600.0, house_w=1000.0, rate_w=6000.0, allowed_w=5000.0, house_over=False,
        )
        member = controller._target_member(request, None, budget)  # noqa: SLF001
        assert member.holdable and member.wants_a == 16.0, "a person's Start paused is counted at what it resumes at"
        paused.paused_charge_origin = "min_soc"
        member = controller._target_member(request, None, budget)  # noqa: SLF001
        assert not member.holdable and member.wants_a == 0.0, "the floor's charge is not the target's, and draws nothing"
        paused.paused_charge_origin = "plan_window"
        member = controller._target_member(request, None, budget)  # noqa: SLF001
        assert member.holdable and member.wants_a == 16.0, "a plan's paused charge is counted at what it resumes at"


# -- review: a charger the hold cannot act on


async def test_a_charger_whose_phase_cannot_be_judged_is_said_and_not_planned_as_held(
    hass: HomeAssistant, monkeypatch
) -> None:
    with freeze_time(_HOUR + timedelta(seconds=5)) as frozen:
        world = await _world(hass, monkeypatch, frozen, "inert")
        controller = world.halo.controller
        charger = world.halo.charger.entry_id
        await world.step()
        await _charging(world)
        await world.run(2)
        assert controller.import_target_plan_kw(charger) is not None
        monkeypatch.setattr(type(controller), "_target_floor_a", lambda *_args: None)
        await world.run(2)
        snapshot = controller.import_target_snapshot()
        assert snapshot["chargers"][charger]["inert"] == "phase_cannot_be_judged"
        assert world.decision.reason not in (REASON_IMPORT_TARGET_HELD, REASON_IMPORT_TARGET_PAUSED)
        assert controller.import_target_plan_kw(charger) is None


async def test_a_charger_without_a_current_of_its_own_is_not_planned_as_held(hass: HomeAssistant, monkeypatch) -> None:
    with freeze_time(_HOUR + timedelta(seconds=5)) as frozen:
        world = await _world(hass, monkeypatch, frozen, "nosource")
        controller = world.halo.controller
        charger = world.halo.charger.entry_id
        await world.step()
        await world.step()
        assert controller.import_target_plan_kw(charger) is not None
        controller.config["phase_wiring"][charger].pop("measured_current_source")
        assert controller.import_target_plan_kw(charger) is None


# -- review: a failure of the target's own leaves the fuse's decisions as they are


async def test_a_failing_target_never_delays_an_overload_lowering(hass: HomeAssistant, monkeypatch) -> None:
    from custom_components.spotnav.site import site_capacity_controller as module

    with freeze_time(_HOUR + timedelta(seconds=5)) as frozen:
        world = await _world(hass, monkeypatch, frozen, "failing", target_kw=20.0)
        await world.step()
        await _charging(world)
        await world.run(2)

        def broken(*_args, **_kwargs):
            raise RuntimeError("the target's own bug")

        monkeypatch.setattr(module, "share_budget", broken)
        written = len(world.halo.written())
        world.house_a = 10.0  # 26 A on a 20 A fuse: lowered to 10 A at once
        world.show()
        await hass.async_block_till_done()
        await world.halo.tick()
        decision = world.decision
        assert decision.overload or decision.reason == "reducing_current_due_to_active_import_overload"
        assert len(world.halo.written()) > written, "the lowering went out in the same pass"
        assert world.car_a < 16.0
        assert world.halo.controller.import_target_snapshot()["not_applied"] == "error"


# -- the card's command and the app's action


async def test_the_card_and_the_app_can_ask_and_are_told_why_not(hass: HomeAssistant, hass_ws_client, hass_client_no_auth) -> None:
    from .world import setup_charger

    await setup_charger(hass)
    client = await hass_ws_client(hass)
    await client.send_json({"id": 1, "type": "spotnav/release_import_target", "api_version": 1, "charger_id": "entry_a"})
    answer = await client.receive_json()
    assert answer["result"] == {"api_version": 1, "ok": False, "error": "spotnav_no_site"}
    web = await hass_client_no_auth()
    response = await web.post("/api/webhook/webhook-a", json={"version": 1, "action": "release_import_target"})
    assert response.status == 400
    assert await response.json() == {
        "api_version": 1, "ok": False, "error": "spotnav_no_site", "action": "release_import_target",
    }


async def test_a_plan_at_risk_is_not_told_beside_the_targets_own_shortfall(hass: HomeAssistant, monkeypatch) -> None:
    from custom_components.spotnav.runtime import charger_data

    with freeze_time(_HOUR + timedelta(seconds=5)) as frozen:
        world = await _tight_night(hass, monkeypatch, frozen, "single")
        await world.run(2)
        assert len(_told(world)) == 1
        notifier = charger_data(hass, world.halo.charger.entry_id).notifier
        notifier._last_sent.clear()  # noqa: SLF001
        # The plan itself is found short of its departure meanwhile: one notification for this case, not two.
        monkeypatch.setattr(notifier._tracker, "observe", lambda _facts: [("plan_at_risk", {"time": "07:00"})])  # noqa: SLF001
        notifier._observe()  # noqa: SLF001
        await hass.async_block_till_done()
        assert len(_told(world)) == 1



# -- second review


class _SlowOff(_World):
    """A charger whose stop lands late: the switch stays on for a while after the stop went out."""

    hold_off: bool = False

    def follow_commands(self) -> None:
        if len(self.halo.turn_off) > self.turn_offs and not self.hold_off:
            self.turn_offs = len(self.halo.turn_off)
            self.halo.switch("off")
        if len(self.halo.turn_on) > self.turn_ons:
            self.turn_ons = len(self.halo.turn_on)
            self.halo.switch("on")


async def test_a_pause_sent_holds_to_the_hours_end_though_the_charger_reports_off_late(
    hass: HomeAssistant, monkeypatch
) -> None:
    with freeze_time(_HOUR + timedelta(seconds=5)) as frozen:
        world = await _world(hass, monkeypatch, frozen, "race")
        world.__class__ = _SlowOff
        await world.step()
        await _charging(world)
        await world.run(40)
        assert world.car_a == 7.0
        world.house_a = 5000.0 / (3 * _V)
        world.hold_off = True
        for _ in range(8):
            if world.halo.turn_off:
                break
            await world.step()
        assert world.halo.turn_off, "the pause went out"
        # The surge ends before the charger reports off; then the stop lands.
        world.house_a = 1000.0 / (3 * _V)
        await world.step()
        world.hold_off = False
        world.follow_commands()
        world.show()
        await hass.async_block_till_done()
        await world.run(6)
        assert dt_util.utcnow() < _HOUR + timedelta(hours=1)
        snapshot = world.halo.controller.import_target_snapshot()
        assert snapshot["paused_hour"] == {world.halo.charger.entry_id: _HOUR.isoformat()}
        assert world.turn_ons == 1, "not resumed within the same hour"


async def test_a_wobbling_house_does_not_ratchet_the_held_current_down(hass: HomeAssistant, monkeypatch) -> None:
    with freeze_time(_HOUR + timedelta(seconds=5)) as frozen:
        world = await _world(hass, monkeypatch, frozen, "noise")
        await world.step()
        await _charging(world)
        await world.run(10)
        assert world.car_a == 7.0
        # The house wobbles 1.0 / 1.7 kW every half minute for half an hour, then is quiet at 1 kW.
        lowered = False
        for i in range(60):
            world.house_a = (1700.0 if i % 2 else 1000.0) / (3 * _V)
            await world.step()
            lowered = lowered or world.car_a < 7.0
        assert lowered, "the wobble does lower the car for a moment"
        world.house_a = 1000.0 / (3 * _V)
        await world.run(4)
        assert dt_util.utcnow() < _HOUR + timedelta(minutes=50)
        assert world.car_a >= 7.0, "the target's own 1 A lowering is given back within the hour"


async def test_release_keeps_the_target_aside_until_the_departure(hass: HomeAssistant, monkeypatch) -> None:
    with freeze_time(_HOUR + timedelta(seconds=5)) as frozen:
        world = await _tight_night(hass, monkeypatch, frozen, "toggle", choice="release")
        now = dt_util.utcnow()
        # 16.5 kWh of windows with a departure 3 h 10 away: held, 5 kW leaves it just short.
        departure = now + timedelta(hours=3, minutes=10)
        world.halo.cc.plan = _plan(now, departure, [(now, now + timedelta(minutes=90))])
        world.departure = departure
        trace = []
        for _ in range(2 * 90):
            await world.step()
            trace.append(world.car_a)
        changes = [amps for i, amps in enumerate(trace) if i == 0 or amps != trace[i - 1]]
        assert len(changes) <= 2, changes
        snapshot = world.halo.controller.import_target_snapshot()
        assert snapshot["released_by_plan"] == {world.halo.charger.entry_id: departure.isoformat()}


async def test_a_release_ends_with_a_new_departure_and_a_stale_button_is_refused(
    hass: HomeAssistant, monkeypatch
) -> None:
    from custom_components.spotnav.site.site_capacity_controller import ERROR_STALE_DEPARTURE

    with freeze_time(_HOUR + timedelta(seconds=5)) as frozen:
        world = await _tight_night(hass, monkeypatch, frozen, "newdeparture")
        await world.run(2)
        charger = world.halo.charger.entry_id
        controller = world.halo.controller
        told = int(world.departure.timestamp())
        assert controller.async_release_import_target(charger, for_departure=told) is None
        await world.run(2)
        assert world.car_a == 16.0
        # The plan is made again for another departure: the set-aside was not given for it.
        now = dt_util.utcnow()
        later = now + timedelta(hours=6)
        world.halo.cc.plan = _plan(now, later, [(now, now + timedelta(hours=1))])
        await world.run(2)
        assert controller.import_target_snapshot()["released"] == {}
        assert world.car_a < 16.0, "held again"
        # A phone's button for the old departure, pressed now, sets nothing aside.
        assert controller.async_release_import_target(charger, for_departure=told) == ERROR_STALE_DEPARTURE
        hass.bus.async_fire("mobile_app_notification_action", {"action": f"SPOTNAV_IMPORT_TARGET_RELEASE_{charger}_{told}"})
        await hass.async_block_till_done()
        assert controller.import_target_snapshot()["released"] == {}


async def test_a_plan_at_risk_held_back_behind_the_targets_notice_is_told_once_that_clears(
    hass: HomeAssistant, monkeypatch
) -> None:
    from custom_components.spotnav.runtime import charger_data

    with freeze_time(_HOUR + timedelta(seconds=5)) as frozen:
        world = await _tight_night(hass, monkeypatch, frozen, "behind")
        await world.run(2)
        notifier = charger_data(hass, world.halo.charger.entry_id).notifier
        assert len(_told(world)) == 1
        monkeypatch.setattr(notifier._tracker, "observe", lambda facts: [("plan_at_risk", {"time": "07:00"})])  # noqa: SLF001
        notifier._observe()  # noqa: SLF001
        monkeypatch.setattr(notifier._tracker, "observe", lambda facts: [])  # noqa: SLF001
        notifier._at_risk_now = True  # noqa: SLF001
        notifier._last_sent.clear()  # noqa: SLF001
        notifier.import_target_risk(None)
        await hass.async_block_till_done()
        messages = [call.data.get("message") for call in world.calls]
        # The target's notice comes down, and the plan's own is told in its place.
        assert messages[-2:] == ["clear_notification", "The charge will not be ready by the departure."], messages


async def test_a_shortfall_never_told_to_a_phone_does_not_hold_the_plans_notice_back(
    hass: HomeAssistant, monkeypatch
) -> None:
    from custom_components.spotnav.runtime import charger_data
    from custom_components.spotnav.site.site_capacity_controller import TargetRisk

    with freeze_time(_HOUR + timedelta(seconds=5)) as frozen:
        world = await _world(hass, monkeypatch, frozen, "untold")
        notifier = charger_data(hass, world.halo.charger.entry_id).notifier
        notifier.import_target_risk(TargetRisk(departure=(_HOUR + timedelta(hours=8)).isoformat(), missing_kwh=4.0, target_kw=6.0))
        assert notifier._target_told is None  # noqa: SLF001 - no phone chosen: nothing told
