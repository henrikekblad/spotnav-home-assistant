"""A full car that refuses load balancing's battery probes has ended the plan's charge by itself.

The field night of 13-14 October: an OCPP HALO and a Kia EV6, the plan's target 91 %, the car full at about 02:45.
From 03:00 the home battery charged from the grid and took the fuse, load balancing paused the plan's charge, and the
battery probe started the full car at its minimum again and again (03:01, 03:12, 03:27, 04:00, 04:16); every probe
ended `car_not_drawing`. The plan's own watch (five minutes at SuspendedEV) does not run while balancing holds the
charge back, so the plan only began to wait for the car's level at 04:35, after the battery stopped.

Now two probes in a row that the car refused, with the charger's own reports agreeing (its reading at most 1 A all
along, never charging, its connector at SuspendedEV at the end), are the car ending the charge: the plan waits for the
car's level (`car_ended_waiting`), and no probe, no balancing resume and no window starts the car again. A car that
draws at the probe, and a probe that fails because the battery did not yield, change nothing.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.spotnav.core.events import CarEnded
from custom_components.spotnav.core.ownership import decide
from custom_components.spotnav.core.session import ChargeSession
from custom_components.spotnav.execution.target_stop import SocReading
from custom_components.spotnav.site.battery_probe import (
    BatteryProbe,
    PROBE_BACKOFF_INITIAL_S,
    PROBE_BACKOFF_MAX_S,
    PROBE_CAR_ENDED_GAP_S,
    PROBE_CAR_ENDED_STREAK,
)

from .helpers import install_schedule
from .test_balancing_retry import _battery_w, _night, _outcomes
from .test_lb_start_credit import _Halo, _site_a

PHASES = ("L1", "L2", "L3")
TARGET = 91.0


# -- 1. the probe's own rule (pure)


def _probe_once(
    probe: BatteryProbe,
    now: float,
    *,
    delivered: float | None = 0.0,
    site: float = 20.2,
    connector: str | None = "SuspendedEV",
    charging: bool | None = False,
    plug_in: object = 1,
    looks: int = 2,
    span: float = 30.0,
) -> str:
    """One probe from `now`: started, looked at `looks` times over `span` seconds (its window, or the wait for a
    car that shows it started), ended (and stopped)."""
    probe.start(
        now, 6.0, {p: 20.2 for p in PHASES}, PHASES, {p: 0.0 for p in PHASES}, charger_charging=False, plug_in=plug_in
    )
    verdict = None
    for look in range(looks):
        verdict = probe.evaluate(
            now + span * (look + 1) / looks,
            site_current_a={p: site for p in PHASES},
            limit_a={p: 20.0 for p in PHASES},
            delivered_a={p: delivered for p in PHASES},
            main_fuse_a=20.0,
            charger_charging=charging,
            connector_status=connector,
        )
    assert verdict is not None
    if verdict.state == "failed":
        probe.failed(now + span, verdict.reason)
        probe.stopped()
    elif verdict.state == "succeeded":
        probe.succeeded(now + span)
    return verdict.reason


def test_two_refusals_in_a_row_are_the_car_ending_the_charge() -> None:
    assert PROBE_CAR_ENDED_STREAK == 2
    probe = BatteryProbe()
    assert _probe_once(probe, 0.0) == "car_not_drawing"
    assert probe.last_refused and not probe.car_refused, "one probe is half a minute: a sleeping car may need it"
    assert _probe_once(probe, PROBE_BACKOFF_INITIAL_S + 30.0) == "car_not_drawing"
    assert probe.car_refused
    assert probe.snapshot(700.0)["car_refusals"] == 2
    probe.forget_refusals()
    assert not probe.car_refused
    assert _probe_once(probe, 2000.0) == "car_not_drawing"
    assert not probe.car_refused, "acted on: the next conclusion needs a new row"


@pytest.mark.parametrize(
    ("case", "kwargs"),
    [
        # The charger's reading is missing: `car_not_drawing` all the same, but nothing says the car took nothing.
        ("no_reading", {"delivered": None}),
        # The connector does not say the car asks for no current.
        ("no_connector", {"connector": None}),
        ("charger_holds", {"connector": "SuspendedEVSE"}),
        # The charger says it charges: waited for to 90 s, and still nothing then.
        ("charging_status", {"connector": "Charging", "charging": True, "looks": 3, "span": 91.0}),
    ],
)
def test_a_refusal_needs_the_chargers_own_reports_to_agree(case: str, kwargs: dict) -> None:
    probe = BatteryProbe()
    for start in (0.0, 630.0, 1560.0):
        assert _probe_once(probe, start, **kwargs) == "car_not_drawing", case
        assert not probe.last_refused and not probe.car_refused, case


def test_a_car_that_takes_a_little_is_no_refusal() -> None:
    """Half an amp is a car that began to draw (a slow EV6 ramping), however far from its minimum."""
    probe = BatteryProbe()
    for start in (0.0, 630.0):
        # It rises, is waited for to 90 s, and is still short then.
        probe.start(start, 6.0, {p: 20.2 for p in PHASES}, PHASES, {p: 0.0 for p in PHASES}, plug_in=1)
        for at in (31.0, 60.0, 91.0):
            verdict = probe.evaluate(
                start + at,
                site_current_a={p: 20.2 for p in PHASES},
                limit_a={p: 20.0 for p in PHASES},
                delivered_a={p: 0.5 for p in PHASES},
                main_fuse_a=20.0,
                charger_charging=False,
                connector_status="SuspendedEV",
            )
        assert (verdict.state, verdict.reason) == ("failed", "car_not_drawing")
        probe.failed(start + 91.0, verdict.reason)
        probe.stopped()
        assert not probe.last_refused
    assert not probe.car_refused


def test_any_other_outcome_breaks_the_row() -> None:
    probe = BatteryProbe()
    assert _probe_once(probe, 0.0) == "car_not_drawing"
    # The car draws its minimum and the battery does not yield: a failure for the grid, not the car.
    assert _probe_once(probe, 630.0, delivered=5.7, site=25.9, connector="Charging", charging=True) == (
        "battery_did_not_yield"
    )
    assert not probe.last_refused
    assert _probe_once(probe, 1560.0) == "car_not_drawing"
    assert not probe.car_refused, "the row starts again"
    probe.refused(2500.0, "start_refused")
    assert _probe_once(probe, 3500.0) == "car_not_drawing"
    assert not probe.car_refused


def test_a_row_is_one_plug_in_and_no_longer_than_twice_the_longest_back_off() -> None:
    assert PROBE_CAR_ENDED_GAP_S == 2 * PROBE_BACKOFF_MAX_S
    probe = BatteryProbe()
    assert _probe_once(probe, 0.0, plug_in=1) == "car_not_drawing"
    assert _probe_once(probe, 630.0, plug_in=2) == "car_not_drawing"
    assert not probe.car_refused, "another plug-in, perhaps another car"
    assert _probe_once(probe, 630.0 + PROBE_CAR_ENDED_GAP_S + 60.0, plug_in=2) == "car_not_drawing"
    assert not probe.car_refused, "too long between them to be in a row"


def test_a_car_that_draws_at_the_probe_is_no_refusal() -> None:
    probe = BatteryProbe()
    for start in (0.0, 630.0):
        assert _probe_once(probe, start, delivered=5.7, site=20.1, connector="Charging", charging=True) == (
            "battery_yielded"
        )
        assert not probe.last_refused and not probe.car_refused


# -- 2. the core: the plan's charge load balancing holds back


def test_the_core_records_the_end_of_a_plan_charge_balancing_holds_back() -> None:
    now = dt_util.utcnow()
    held = ChargeSession(plugged=True, owner="none", balancing_paused=True, paused_origin="plan")
    after, commands = decide(held, CarEnded(plan=True), now)
    assert after.car_ended_at == now
    assert all(command.kind == "keep" for command in commands), "nothing is started or stopped"
    persons = ChargeSession(plugged=True, owner="none", balancing_paused=True, paused_origin="person")
    assert decide(persons, CarEnded(plan=True), now)[0].car_ended_at is None, "not the plan's charge"


# -- 3. the night, through the site's regulator and the charger's controller


class _Car:
    """The car's level as SpotNav reads it: 43 % read at 22:50, carried by the delivered energy to 86.2 %."""

    def __init__(self) -> None:
        self.percent, self.estimated, self.read_at = 86.2, True, dt_util.utcnow() - timedelta(hours=6)

    def __call__(self, _vehicle: str | None) -> SocReading:
        age = (dt_util.utcnow() - self.read_at).total_seconds()
        return SocReading(self.percent, "vehicle", "sensor.ev6", None, age, estimated=self.estimated)

    def reads(self, percent: float) -> None:
        self.percent, self.estimated, self.read_at = percent, False, dt_util.utcnow()


def _connector(halo: _Halo, status: str) -> None:
    halo.hass.states.async_set(f"sensor.{halo.prefix}_connector_1_status_connector", status)


async def _plan_paused_by_the_battery(hass: HomeAssistant, monkeypatch, entry_id: str) -> tuple[_Halo, _Car]:
    """03:00: the plan's window has started the car, the battery takes the fuse and load balancing pauses the
    plan's charge."""
    halo = await _night(hass, monkeypatch, entry_id)
    car = _Car()
    halo.cc._soc_reader = car  # noqa: SLF001
    now = dt_util.utcnow()
    await install_schedule(
        halo.cc,
        {
            "periods": [
                {"start": (now - timedelta(minutes=5)).isoformat(), "end": (now + timedelta(hours=3)).isoformat()}
            ],
            "amps": 16,
            "target_soc_percent": TARGET,
        },
    )
    await hass.async_block_till_done()
    assert len(halo.turn_on) == 1, "the open window started the plan's charge"
    halo.switch("on")
    _connector(halo, "Charging")
    halo.reads(5.7)
    await hass.async_block_till_done()
    _battery_w(halo, 12159.0)
    _site_a(hass, halo.site, 25.7, 25.3, 24.5)
    await halo.tick()
    assert len(halo.turn_off) == 1, "far over the fuse: paused"
    halo.switch("off")
    _connector(halo, "SuspendedEVSE")
    halo.reads(0.0)
    _battery_w(halo, 12256.0)
    _site_a(hass, halo.site, 20.2, 20.1, 20.0)
    await hass.async_block_till_done()
    assert halo.cc.paused_by_balancing
    return halo, car


async def _probe(halo: _Halo, *, takes: float = 0.0, site: tuple[float, float, float] = (20.2, 20.1, 20.0)) -> None:
    """The back-off is over: the probe starts the car at its minimum; the car takes `takes`, the site reads `site`;
    the window ends and a failed probe's stop goes out."""
    hass = halo.hass
    halo.advance(5.0)
    await halo.tick()
    halo.switch("on")
    _connector(halo, "Charging" if takes else "SuspendedEV")
    halo.reads(takes)
    _site_a(hass, halo.site, *site)
    await hass.async_block_till_done()
    halo.advance(15.0)
    await halo.tick()
    halo.advance(16.0)
    await halo.tick()
    await hass.async_block_till_done()


async def _probe_stopped(halo: _Halo) -> None:
    halo.switch("off")
    _connector(halo, "SuspendedEV")
    halo.reads(0.0)
    _site_a(halo.hass, halo.site, 20.2, 20.1, 20.0)
    await halo.hass.async_block_till_done()
    await halo.tick()


async def test_the_night_of_14_october_a_full_car_ends_the_charge_after_two_refused_probes(
    hass: HomeAssistant, monkeypatch
) -> None:
    halo, car = await _plan_paused_by_the_battery(hass, monkeypatch, "fullcar")
    cc = halo.cc

    await _probe(halo)
    assert _outcomes(halo) == ["probe_started", "probe_failed"]
    assert halo.log()[-1]["detail"] == "car_not_drawing"
    await _probe_stopped(halo)
    assert cc.car_ended_at is None, "one probe is not enough"
    assert cc.paused_by_balancing

    halo.advance(PROBE_BACKOFF_INITIAL_S)
    await _probe(halo)
    await hass.async_block_till_done()
    assert _outcomes(halo) == ["probe_started", "probe_failed", "probe_started", "probe_failed", "probe_car_ended"]
    assert cc.car_ended_at is not None, "the car ended the plan's charge by itself"
    shadow = cc._shadow  # noqa: SLF001
    core_ended = shadow.session.car_ended_at
    assert core_ended is not None and abs((core_ended - cc.car_ended_at).total_seconds()) < 1.0, "the core's end too"
    assert any(record["event"]["kind"] == "car_ended" for record in shadow.events)
    assert cc.car_ended_wait == {"soc_percent": 86.2, "estimated": True, "target_percent": TARGET}
    assert cc.car_ended_holds_charge
    assert cc.plan is not None, "the plan waits for the car's level"
    await _probe_stopped(halo)

    # 03:27 to 04:16: no more probes, while the battery keeps the fuse.
    starts = len(halo.turn_on)
    for _ in range(6):
        halo.advance(PROBE_BACKOFF_MAX_S)
        await halo.tick()
    assert _outcomes(halo).count("probe_started") == 2
    assert len(halo.turn_on) == starts

    # 04:16: the battery stops; the room it leaves does not restart the car, and neither does the window.
    _battery_w(halo, 0.0)
    _site_a(hass, halo.site, 2.0, 0.9, 0.8)
    await hass.async_block_till_done()
    for _ in range(4):
        halo.advance(61.0)
        await halo.tick()
    await cc.async_rearm()
    await hass.async_block_till_done()
    assert len(halo.turn_on) == starts, "nothing starts a car that ended the charge"
    assert cc.car_ended_wait is not None

    # A fresh reading below the target ends the wait: the charge load balancing holds back is given back.
    car.reads(70.0)
    assert cc.car_ended_wait is None
    assert not cc.car_ended_holds_charge
    for _ in range(4):
        halo.advance(61.0)
        await halo.tick()
    assert len(halo.turn_on) == starts + 1, "resumed once there is room"


async def test_a_car_that_draws_at_the_probe_is_unaffected(hass: HomeAssistant, monkeypatch) -> None:
    halo, _car = await _plan_paused_by_the_battery(hass, monkeypatch, "drawscar")
    _battery_w(halo, 12256.0 - 3 * 230.0 * 5.7)
    await _probe(halo, takes=5.7, site=(20.1, 20.0, 20.0))
    assert _outcomes(halo) == ["probe_started", "probe_succeeded"]
    assert halo.cc.car_ended_at is None
    assert halo.cc.charging


async def test_a_battery_that_does_not_yield_is_unaffected(hass: HomeAssistant, monkeypatch) -> None:
    halo, _car = await _plan_paused_by_the_battery(hass, monkeypatch, "noyield")
    for attempt in range(4):
        # The car takes its minimum, the battery does not move: the grid lands on top of the fuse.
        await _probe(halo, takes=5.7, site=(25.9, 25.8, 25.7))
        assert halo.log()[-1]["detail"] == "battery_did_not_yield", attempt
        await _probe_stopped(halo)
        halo.advance(PROBE_BACKOFF_MAX_S)
    assert _outcomes(halo).count("probe_started") == 4
    assert "probe_car_ended" not in _outcomes(halo)
    assert halo.cc.car_ended_at is None
    assert halo.cc.paused_by_balancing, "still held back, for when there is room"


async def test_a_car_the_estimate_shows_full_ends_the_plan_at_the_refused_probes(hass: HomeAssistant, monkeypatch) -> None:
    halo, car = await _plan_paused_by_the_battery(hass, monkeypatch, "estfull")
    car.percent = 90.0  # within the estimate's three points of the 91 % target
    await _probe(halo)
    await _probe_stopped(halo)
    halo.advance(PROBE_BACKOFF_INITIAL_S)
    await _probe(halo)
    await hass.async_block_till_done()
    assert "probe_car_ended" in _outcomes(halo)
    assert halo.cc.plan is None, "full for the plan: a reached target"
    await _probe_stopped(halo)
    for _ in range(3):
        halo.advance(PROBE_BACKOFF_MAX_S)
        await halo.tick()
    assert _outcomes(halo).count("probe_started") == 2
