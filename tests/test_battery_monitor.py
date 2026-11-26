"""The control-failure monitor: the grace, three readings in a row, what the battery can deliver, once per slot,
counters, dry runs and a restart."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from custom_components.spotnav.battery import gate
from custom_components.spotnav.battery.monitor import (
    GRACE_S,
    READING_GAP_S,
    PROVE_HOLD_SOC_MIN_PCT,
    PROVE_MIN_KW,
    Capability,
    Command,
    Monitor,
    Reading,
    deliverable_kw,
    judge,
    proves,
)

T0 = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)
DAY = "2026-11-13"
BATTERY = Capability(rated_charge_kw=5.0, rated_discharge_kw=5.0)


def at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


def feed(
    monitor: Monitor, power_kw: float | None, seconds: list[float], *, soc: float = 50.0, cap: Capability = BATTERY,
    grid: float | None = None,
):
    return [monitor.observe(Reading(power_kw, 5.0, soc, grid), cap, at(s)) for s in seconds]


def after_grace(count: int, start: float = GRACE_S) -> list[float]:
    return [start + i * READING_GAP_S for i in range(count)]


def test_a_hold_that_holds_is_never_a_failure() -> None:
    monitor = Monitor()
    monitor.start(Command("hold", T0, before_kw=-3.0, written=True))

    assert feed(monitor, -0.1, after_grace(10)) == [None] * 10
    assert monitor.counters(DAY) == {"writes": 1, "no_effect": 0, "partial": 0}


def test_nothing_is_judged_inside_the_grace() -> None:
    monitor = Monitor()
    monitor.start(Command("hold", T0, before_kw=-3.0, written=True))

    assert feed(monitor, -3.0, [0, 60, 120, 180, 240, 290]) == [None] * 6
    assert monitor.counters(DAY)["no_effect"] == 0


def test_a_hold_that_does_not_hold_is_one_no_effect_after_three_readings() -> None:
    monitor = Monitor()
    monitor.start(Command("hold", T0, before_kw=-3.0, written=True))

    events = feed(monitor, -2.9, after_grace(6))

    assert events[:2] == [None, None]
    failure = events[2]
    assert failure is not None and failure.kind == "failure" and failure.outcome == "no_effect"
    assert len(failure.readings) == 3
    # Once per slot: later readings add nothing.
    assert events[3:] == [None, None, None]
    assert monitor.counters(DAY) == {"writes": 1, "no_effect": 1, "partial": 0}


def test_a_hold_that_halves_the_discharge_is_partial() -> None:
    monitor = Monitor()
    monitor.start(Command("hold", T0, before_kw=-3.0, written=True))

    events = feed(monitor, -1.5, after_grace(3))

    assert events[2] is not None and events[2].outcome == "partial"


def test_readings_closer_than_the_gap_are_not_judged() -> None:
    monitor = Monitor()
    monitor.start(Command("hold", T0, before_kw=-3.0, written=True))

    events = feed(monitor, -3.0, [GRACE_S + s for s in range(0, 120, 10)])

    # Ten-second ticks over two minutes give only readings at 0, 60 and 120 s past the grace: two so far.
    assert events == [None] * 12 and monitor.counters(DAY)["no_effect"] == 0


def test_an_ok_or_stale_reading_breaks_the_run() -> None:
    monitor = Monitor()
    monitor.start(Command("hold", T0, before_kw=-3.0, written=True))
    times = after_grace(8)

    feed(monitor, -3.0, times[:2])
    feed(monitor, -0.1, times[2:3])
    feed(monitor, -3.0, times[3:5])
    assert monitor.observe(Reading(-3.0, 600.0, 50.0), BATTERY, at(times[5])) is None
    assert monitor.observe(Reading(None, None, 50.0), BATTERY, at(times[6])) is None

    assert monitor.counters(DAY)["no_effect"] == 0


def test_a_charge_is_judged_against_what_the_battery_and_the_fuse_allow() -> None:
    command = Command("charge", T0, rate_kw=10.0, written=True)

    assert deliverable_kw(command, BATTERY, 50.0) == (5.0, None)
    assert deliverable_kw(command, Capability(rated_charge_kw=5.0, site_cap_kw=2.0), 50.0) == (2.0, None)
    assert deliverable_kw(command, Capability(rated_charge_kw=5.0, site_cap_kw=0.1), 50.0) == (None, "too_small_to_judge")
    assert deliverable_kw(command, BATTERY, 95.0) == (None, "soc_near_full")
    assert deliverable_kw(command, Capability(), 50.0) == (None, "rating_unknown")
    assert deliverable_kw(command, BATTERY, None) == (None, "soc_unknown")
    assert deliverable_kw(Command("discharge", T0, rate_kw=3.0), BATTERY, 4.0) == (None, "soc_near_empty")
    assert judge(command, 5.0, 4.9) == "ok"
    assert judge(command, 5.0, 2.0) == "partial"
    assert judge(command, 5.0, 1.0) == "no_effect"
    assert judge(command, 5.0, -1.0) == "no_effect"


def test_a_charge_limited_by_the_fuse_is_no_failure_at_that_limit() -> None:
    monitor = Monitor()
    monitor.start(Command("charge", T0, rate_kw=5.0, written=True))
    fuse = Capability(rated_charge_kw=5.0, site_cap_kw=1.5)

    assert feed(monitor, 1.4, after_grace(5), cap=fuse) == [None] * 5


def test_a_charge_near_full_is_not_judged() -> None:
    monitor = Monitor()
    monitor.start(Command("charge", T0, rate_kw=5.0, written=True))

    assert feed(monitor, 0.0, after_grace(5), soc=96.0) == [None] * 5
    assert monitor.watch is not None and monitor.watch.why_not == "soc_near_full"


def test_nothing_is_judged_after_the_slot_ends() -> None:
    monitor = Monitor()
    monitor.start(Command("hold", T0, before_kw=-3.0, slot_end=at(GRACE_S + 90), written=True))

    events = feed(monitor, -3.0, after_grace(4))

    assert events == [None] * 4 and monitor.watch is None and monitor.counters(DAY)["no_effect"] == 0


def test_a_new_command_starts_a_new_slot_and_counts_a_write() -> None:
    monitor = Monitor()
    monitor.start(Command("hold", T0, before_kw=-3.0, written=True))
    feed(monitor, -3.0, after_grace(3))
    monitor.start(Command("hold", at(1000), before_kw=-3.0, written=True))
    feed(monitor, -3.0, after_grace(3, 1000 + GRACE_S))

    assert monitor.counters(DAY) == {"writes": 2, "no_effect": 2, "partial": 0}
    assert monitor.writes_today(at(2000)) == 2


def test_a_dry_run_shows_the_baseline_and_never_counts() -> None:
    monitor = Monitor()
    monitor.start(Command("hold", T0, before_kw=-3.0))

    events = feed(monitor, -3.0, after_grace(5))

    event = events[2]
    assert event is not None and event.kind == "dry_run" and event.outcome == "baseline_no_effect"
    assert [row["power_kw"] for row in event.readings] == [-3.0, -3.0, -3.0]
    assert events[3:] == [None, None]
    assert monitor.counters(DAY) == {"writes": 0, "no_effect": 0, "partial": 0}
    assert monitor.stop(at(5000)) is None


def test_a_dry_run_that_ends_inside_the_grace_says_so() -> None:
    monitor = Monitor()
    monitor.start(Command("hold", T0, before_kw=-3.0))

    event = monitor.stop(at(120))

    assert event is not None and event.outcome == "not_judgeable:ended_within_grace"


def test_a_dry_run_that_could_not_judge_says_why() -> None:
    monitor = Monitor()
    monitor.start(Command("charge", T0, rate_kw=5.0))
    feed(monitor, 0.0, after_grace(3), cap=Capability())

    event = monitor.stop(at(2000))

    assert event is not None and event.outcome == "not_judgeable:rating_unknown"


def test_a_written_command_stopped_early_says_nothing() -> None:
    monitor = Monitor()
    monitor.start(Command("hold", T0, before_kw=-3.0, written=True))

    assert monitor.stop(at(10)) is None


def test_a_restart_keeps_the_watch_and_counters_but_not_the_run() -> None:
    monitor = Monitor()
    monitor.start(Command("hold", T0, before_kw=-3.0, slot_end=at(4000), written=True))
    times = after_grace(5)
    feed(monitor, -3.0, times[:2])

    restored = Monitor.from_dict(monitor.as_dict())

    assert restored.watch is not None and restored.watch.command.kind == "hold" and restored.watch.run == ()
    assert restored.counters(DAY)["writes"] == 1
    # Two readings before the restart and two after are no run of three.
    assert feed(restored, -3.0, times[2:4]) == [None, None]
    assert feed(restored, -3.0, times[4:5])[0] is not None


def test_broken_storage_is_dropped() -> None:
    restored = Monitor.from_dict({"watch": {"command": {"kind": "boost", "at": T0.isoformat()}}, "counters": {DAY: {"writes": True, "partial": 2}}})

    assert restored.watch is None and restored.counters(DAY) == {"writes": 0, "no_effect": 0, "partial": 2}
    assert Monitor.from_dict("nope").as_dict() == {"watch": None, "counters": {}}


# --- a command shown to act (`proves`) ------------------------------------------------------------------------------


def _held(monitor: Monitor, seconds: list[float], *, grid: float | None = 2.0, power: float = 0.0, soc: float = 50.0):
    return [monitor.observe(Reading(power, 5.0, soc, grid), BATTERY, at(s)) for s in seconds]


def test_three_ok_readings_of_a_written_charge_are_told_once_as_proved_and_the_watch_goes_on() -> None:
    monitor = Monitor()
    monitor.start(Command("charge", T0, 3.0, before_kw=-1.0, written=True))

    events = feed(monitor, 2.8, after_grace(8), grid=3.5)

    assert events[:2] == [None, None] and events[3:] == [None] * 5
    proved = events[2]
    assert proved is not None and proved.kind == "proved" and proved.outcome == "ok" and proved.expected_kw == 3.0
    assert [row["verdict"] for row in proved.readings] == ["ok"] * 3
    assert monitor.counters(DAY) == {"writes": 1, "no_effect": 0, "partial": 0}
    # A later failure is still counted.
    failure = feed(monitor, 0.0, after_grace(3, GRACE_S + 8 * READING_GAP_S))[2]
    assert failure is not None and failure.kind == "failure" and failure.outcome == "no_effect"


def test_a_small_charge_or_one_on_a_battery_that_already_charged_proves_nothing() -> None:
    small = Monitor()
    small.start(Command("charge", T0, PROVE_MIN_KW - 0.2, before_kw=-1.0, written=True))
    assert feed(small, 0.8, after_grace(6), grid=2.0) == [None] * 6
    # The sun charged it before the command: a charge afterwards may be the sun's still.
    sunny = Monitor()
    sunny.start(Command("charge", T0, 3.0, before_kw=1.5, written=True))
    assert feed(sunny, 3.0, after_grace(6), grid=2.0) == [None] * 6
    unknown = Monitor()
    unknown.start(Command("charge", T0, 3.0, before_kw=None, written=True))
    assert feed(unknown, 3.0, after_grace(6), grid=2.0) == [None] * 6


def test_a_charge_proves_only_while_the_site_draws_from_the_grid() -> None:
    # The sun came out after the command: a battery left alone charges from its surplus just so, with nothing
    # drawn from the grid (or the rest sold). Nor does a grid power that is not read show anything.
    for grid in (0.0, -1.5, 0.1, None, float("nan")):
        monitor = Monitor()
        monitor.start(Command("charge", T0, 3.0, before_kw=-1.0, written=True))
        assert feed(monitor, 3.0, after_grace(6), grid=grid) == [None] * 6, grid
    # One reading without an import breaks the run of three.
    monitor = Monitor()
    monitor.start(Command("charge", T0, 3.0, before_kw=-1.0, written=True))
    times = after_grace(6)
    assert feed(monitor, 3.0, times[:2], grid=3.0) == [None, None]
    assert feed(monitor, 3.0, times[2:3], grid=0.0) == [None]
    assert feed(monitor, 3.0, times[3:5], grid=3.0) == [None, None]
    proved = feed(monitor, 3.0, times[5:6], grid=3.0)[0]
    assert proved is not None and proved.kind == "proved"


def test_a_discharge_proves_when_the_house_needs_at_least_the_least_rate() -> None:
    monitor = Monitor()
    monitor.start(Command("discharge", T0, 5.0, before_kw=-1.5, written=True))
    house = Capability(rated_charge_kw=5.0, rated_discharge_kw=5.0, site_cap_kw=1.5)
    proved = feed(monitor, -1.4, after_grace(3), cap=house)[2]
    assert proved is not None and proved.kind == "proved" and proved.expected_kw == 1.5
    small = Monitor()
    small.start(Command("discharge", T0, 5.0, before_kw=-0.6, written=True))
    quiet = Capability(rated_charge_kw=5.0, rated_discharge_kw=5.0, site_cap_kw=0.6)
    assert feed(small, -0.6, after_grace(6), cap=quiet) == [None] * 6


def test_a_hold_proves_only_against_a_discharge_it_stopped_with_the_house_on_the_grid() -> None:
    monitor = Monitor()
    monitor.start(Command("hold", T0, before_kw=-2.0, written=True))
    proved = _held(monitor, after_grace(3))[2]
    assert proved is not None and proved.kind == "proved" and [row["grid_kw"] for row in proved.readings] == [2.0] * 3
    # An idle battery held: nothing was stopped.
    for before in (0.0, -0.1, 1.0, None):
        idle = Monitor()
        idle.start(Command("hold", T0, before_kw=before, written=True))
        assert _held(idle, after_grace(6)) == [None] * 6, before
    # The sun covers the house, the grid power is unknown, or the battery is at its floor: it would be idle anyway.
    # Nor below the level the verification starts at: a floor of its own that is not read stops it too.
    assert PROVE_HOLD_SOC_MIN_PCT == gate.SUITABLE_SOC_PCT[0]
    for grid, soc in ((0.0, 50.0), (-0.3, 50.0), (None, 50.0), (2.0, 4.0), (2.0, 12.0), (2.0, 29.0)):
        other = Monitor()
        other.start(Command("hold", T0, before_kw=-2.0, written=True))
        assert _held(other, after_grace(6), grid=grid, soc=soc) == [None] * 6, (grid, soc)


def test_an_idle_hold_that_charges_and_a_hold_that_spills_the_sun_prove_nothing() -> None:
    idle = Monitor()
    idle.start(Command("hold", T0, before_kw=-2.0, written=True, idle=True))
    events = _held(idle, after_grace(3), power=1.0)
    assert events[2] is not None and events[2].kind == "failure"
    spilling = Monitor()
    spilling.start(Command("hold", T0, before_kw=-2.0, written=True))
    events = _held(spilling, after_grace(3), grid=-2.0)
    assert events[2] is not None and events[2].kind == "failure" and events[2].outcome == "partial"


def test_a_reading_that_does_not_prove_breaks_the_run_of_ok() -> None:
    monitor = Monitor()
    monitor.start(Command("hold", T0, before_kw=-2.0, written=True))
    times = after_grace(8)
    assert _held(monitor, times[:2]) == [None, None]
    assert _held(monitor, times[2:3], grid=0.0) == [None]  # the sun came out: the run starts over
    assert _held(monitor, times[3:5]) == [None, None]
    assert monitor.observe(Reading(0.0, 600.0, 50.0, 2.0), BATTERY, at(times[5])) is None  # stale: over again
    assert _held(monitor, times[6:8]) == [None, None]


def test_a_dry_run_and_an_export_limit_never_prove() -> None:
    dry = Monitor()
    dry.start(Command("charge", T0, 3.0, before_kw=-1.0, written=False))
    events = feed(dry, 3.0, after_grace(4))
    assert [event.kind for event in events if event is not None] == ["dry_run"]
    curtail = Monitor()
    curtail.start(Command("curtail", T0, before_kw=-3.0, written=True))
    assert [curtail.observe(Reading(0.0, 0.0), Capability(), at(s)) for s in after_grace(6)] == [None] * 6
    assert not proves(Command("curtail", T0, before_kw=-3.0, written=True), 0.0, Reading(0.0, 0.0), Capability())
    assert not proves(Command("charge", T0, 3.0, before_kw=-1.0), 3.0, Reading(3.0, 5.0, 50.0, 3.0), BATTERY)
    assert proves(Command("charge", T0, 3.0, before_kw=-1.0, written=True), 3.0, Reading(3.0, 5.0, 50.0, 3.0), BATTERY)


def test_a_restart_never_completes_a_run_of_ok() -> None:
    monitor = Monitor()
    monitor.start(Command("charge", T0, 3.0, before_kw=-1.0, written=True))
    times = after_grace(6)
    assert feed(monitor, 3.0, times[:2], grid=3.0) == [None, None]

    restored = Monitor.from_dict(monitor.as_dict())

    assert restored.watch is not None and restored.watch.oks == () and not restored.watch.proved
    # Two readings before the restart and two after are no run of three; three after it are.
    assert feed(restored, 3.0, times[2:4], grid=3.0) == [None, None]
    proved = feed(restored, 3.0, times[4:5], grid=3.0)[0]
    assert proved is not None and proved.kind == "proved"
