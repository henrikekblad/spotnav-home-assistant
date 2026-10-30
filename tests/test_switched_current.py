"""The learned current of a charger that can only be switched on and off (`execution/switched_current.py`)."""

from __future__ import annotations

from custom_components.spotnav.execution.switched_current import (
    DEFAULT_SWITCHED_CURRENT_A,
    measured_amps,
    SwitchedCurrent,
)

THREE = ("L1", "L2", "L3")


def _site(l1: float, l2: float, l3: float) -> dict[str, float]:
    return {"L1": l1, "L2": l2, "L3": l3}


def _edge(learner: SwitchedCurrent, kind: str, before, after, *, at: float, phases=THREE, maximum_a: float = 16.0):
    learner.begin_edge(kind, now=at, phases=phases, baseline=before, maximum_a=maximum_a)
    learner.observe_site(after, now=at + 6.0)
    learner.observe_site(after, now=at + 10.0)


def test_before_anything_is_learned_the_configured_charging_current_is_used() -> None:
    learner = SwitchedCurrent()
    assert learner.value(10.0).amps == 10.0
    assert learner.value(10.0).source == "charging_current"
    assert learner.value(None).amps == DEFAULT_SWITCHED_CURRENT_A
    assert learner.value(None).source == "default"


def test_the_current_sensors_reading_is_learned_and_smoothed() -> None:
    learner = SwitchedCurrent()
    learner.observe_draw(1.0)  # the car not drawing yet: nothing learned
    assert not learner.learned
    learner.observe_draw(15.8)
    assert learner.value(10.0) == learner.value(None)
    assert learner.value(10.0).source == "measured"
    assert learner.value(10.0).amps == 15.8
    # A taper at the end of a charge pulls it down only slowly.
    learner.observe_draw(8.0)
    assert learner.value(None).amps > 15.0
    # A rise is taken quickly.
    learner.observe_draw(16.0)
    learner.observe_draw(16.0)
    assert learner.value(None).amps > 15.5


def test_a_smart_plugs_power_is_read_as_amps_over_its_phases() -> None:
    assert measured_amps(current_a=None, power_w=3680.0, phases=1) == (16.0, "power")
    amps, source = measured_amps(current_a=None, power_w=11040.0, phases=3)
    assert source == "power" and round(amps, 2) == 16.0
    # Current sensors win over a plug's power.
    assert measured_amps(current_a=10.0, power_w=11040.0, phases=3) == (10.0, "current")
    assert measured_amps(current_a=None, power_w=None, phases=3) == (None, None)


def test_a_clean_site_step_is_learned_once_an_off_edge_agrees_with_the_on_edge() -> None:
    learner = SwitchedCurrent()
    _edge(learner, "on", _site(3.0, 2.0, 4.0), _site(19.0, 18.2, 19.8), at=0.0)
    # One step is not trusted yet.
    assert learner.value(10.0).source == "charging_current"
    assert learner.snapshot(10.0)["unconfirmed_step_a"] is not None
    _edge(learner, "off", _site(19.5, 18.0, 20.0), _site(3.5, 2.0, 4.2), at=1000.0)
    value = learner.value(10.0)
    assert value.source == "site_step"
    assert 15.5 <= value.amps <= 16.5


def test_a_single_phase_step_needs_quiet_other_phases() -> None:
    learner = SwitchedCurrent()
    _edge(learner, "on", _site(3.0, 2.0, 4.0), _site(13.0, 2.2, 4.1), at=0.0, phases=("L1",))
    _edge(learner, "on", _site(3.0, 2.0, 4.0), _site(13.2, 2.0, 3.9), at=500.0, phases=("L1",))
    assert learner.value(16.0).source == "site_step"
    assert round(learner.value(16.0).amps) == 10


def test_a_noisy_step_is_ignored() -> None:
    learner = SwitchedCurrent()
    # The wired phases do not agree (another load came on with it).
    _edge(learner, "on", _site(3.0, 2.0, 4.0), _site(19.0, 25.0, 19.8), at=0.0)
    assert learner.snapshot(10.0)["last_step"]["outcome"] == "noisy"
    # A phase it is not wired to moved as well.
    _edge(learner, "on", _site(3.0, 2.0, 4.0), _site(13.0, 7.0, 4.0), at=500.0, phases=("L1",))
    assert learner.snapshot(10.0)["last_step"]["outcome"] == "noisy"
    # Readings that never hold still are no step at all.
    learner.begin_edge("on", now=1000.0, phases=THREE, baseline=_site(3.0, 2.0, 4.0), maximum_a=16.0)
    for second, value in enumerate([10.0, 14.0, 18.0, 12.0, 16.0]):
        learner.observe_site(_site(value, value, value), now=1006.0 + second)
    learner.observe_site(_site(19.0, 18.0, 19.0), now=1200.0)
    assert learner.snapshot(10.0)["last_step"]["outcome"] == "no_stable_reading"
    assert learner.value(10.0).source == "charging_current"
    assert learner.snapshot(10.0)["unconfirmed_step_a"] is None


def test_a_step_above_the_configured_maximum_is_ignored() -> None:
    learner = SwitchedCurrent()
    for at in (0.0, 500.0):
        _edge(learner, "on", _site(3.0, 2.0, 4.0), _site(28.0, 27.0, 29.0), at=at, maximum_a=16.0)
    assert learner.snapshot(10.0)["last_step"]["outcome"] == "above_maximum"
    assert learner.value(10.0).source == "charging_current"


def test_steps_that_disagree_are_not_trusted() -> None:
    learner = SwitchedCurrent()
    _edge(learner, "on", _site(3.0, 2.0, 4.0), _site(19.0, 18.0, 20.0), at=0.0)
    _edge(learner, "off", _site(13.0, 12.0, 14.0), _site(3.0, 2.0, 4.0), at=500.0)
    assert learner.value(10.0).source == "charging_current"


def test_the_learned_current_survives_a_restart() -> None:
    learner = SwitchedCurrent()
    learner.observe_draw(13.0)
    restored = SwitchedCurrent.from_store(learner.to_store())
    assert restored.value(None).amps == 13.0
    assert SwitchedCurrent.from_store(None).to_store() is None
    assert SwitchedCurrent.from_store({"measured_a": "x", "site_step_a": -1}).learned is False
