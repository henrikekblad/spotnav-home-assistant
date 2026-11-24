"""Each pool, spa and sauna load's real power, learned (`loads/learned_kw.py`): the device's sensor once it followed a
switching, else the site's meter step at each switching; the entered kW overrides, the typical one only until
learned."""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

from custom_components.spotnav.loads.learned_kw import (
    LOAD_POOL_CIRCULATION,
    LOAD_SAUNA,
    LOAD_SPA,
    SOURCE_ENTERED,
    SOURCE_LEARNED,
    SOURCE_TYPICAL,
    Look,
    PowerLearner,
)

T0 = datetime.fromisoformat("2026-11-24T16:00:00+00:00")
S30 = timedelta(seconds=30)


def _sessions(learner: PowerLearner, load: str, base_w: float, kw: float, times: int, *, start: datetime = T0, jitter: float = 0.0) -> datetime:
    """`times` on/off cycles of a load seen only by the site's meter (the house at `base_w`, the load adding `kw`)."""
    now = start
    for index in range(times):
        extra = jitter * (1 if index % 2 else -1)
        for on, looks in ((False, 2), (True, 4), (False, 2)):
            for _ in range(looks):
                house = base_w + ((kw + extra) * 1000.0 if on else 0.0)
                learner.observe(now, {load: Look(on, house_w=house)})
                now += S30
    return now


def test_the_meter_step_learns_the_saunas_power_after_three_agreeing_switchings() -> None:
    learner = PowerLearner()
    _sessions(learner, LOAD_SAUNA, 1500.0, 8.7, 1)
    # One on and one off: two samples, not yet trusted.
    assert learner.effective_kw(LOAD_SAUNA, None) == (None, SOURCE_TYPICAL)
    _sessions(learner, LOAD_SAUNA, 1500.0, 8.7, 1, start=T0 + timedelta(hours=1))
    assert learner.effective_kw(LOAD_SAUNA, None) == (8.7, SOURCE_LEARNED)
    # The entered kW is the person's override.
    assert learner.effective_kw(LOAD_SAUNA, 9.0) == (9.0, SOURCE_ENTERED)


def test_one_wrong_step_does_not_move_the_figure() -> None:
    learner = PowerLearner()
    _sessions(learner, LOAD_SPA, 800.0, 3.0, 3)
    # A kettle in the same half minute as the next on: a step of 5 kW.
    now = T0 + timedelta(hours=3)
    learner.observe(now, {LOAD_SPA: Look(False, house_w=800.0)})
    learner.observe(now + S30, {LOAD_SPA: Look(True, house_w=800.0)})
    learner.observe(now + 2 * S30, {LOAD_SPA: Look(True, house_w=5800.0)})
    assert learner.effective_kw(LOAD_SPA, None) == (3.0, SOURCE_LEARNED)


def test_a_step_out_of_bounds_or_with_another_load_switching_is_no_sample() -> None:
    learner = PowerLearner()
    now = T0
    learner.observe(now, {LOAD_SPA: Look(False, house_w=500.0), LOAD_SAUNA: Look(False, house_w=500.0)})
    now += S30
    # Both switch on in the same look: the step is neither's.
    learner.observe(now, {LOAD_SPA: Look(True, house_w=500.0), LOAD_SAUNA: Look(True, house_w=500.0)})
    now += S30
    learner.observe(now, {LOAD_SPA: Look(True, house_w=12500.0), LOAD_SAUNA: Look(True, house_w=12500.0)})
    assert learner.loads[LOAD_SPA].steps == [] and learner.loads[LOAD_SAUNA].steps == []
    # A circulation pump "stepping" 20 kW is no sample.
    learner.observe(now, {LOAD_POOL_CIRCULATION: Look(False, house_w=500.0)})
    learner.observe(now + S30, {LOAD_POOL_CIRCULATION: Look(True, house_w=500.0)})
    learner.observe(now + 2 * S30, {LOAD_POOL_CIRCULATION: Look(True, house_w=20500.0)})
    assert learner.loads[LOAD_POOL_CIRCULATION].steps == []


def test_the_device_sensor_counts_only_once_it_followed_a_switching_and_then_comes_first() -> None:
    learner = PowerLearner()
    sensor = "sensor.spa_power"
    now = T0
    # Reading a draw while the spa reads on, but it has not followed a switching yet: no reading counts.
    for _ in range(6):
        learner.observe(now, {LOAD_SPA: Look(True, sensor, 2950.0, None)})
        now += S30
    assert learner.loads[LOAD_SPA].trusted_sensor is None and learner.effective_kw(LOAD_SPA, None)[0] is None
    # The spa turns off and the sensor falls to 0 W: trusted.
    learner.observe(now, {LOAD_SPA: Look(False, sensor, 2950.0, None)})
    now += S30
    learner.observe(now, {LOAD_SPA: Look(False, sensor, 0.0, None)})
    assert learner.loads[LOAD_SPA].trusted_sensor == sensor
    for _ in range(6):
        now += S30
        learner.observe(now, {LOAD_SPA: Look(True, sensor, 2900.0, None)})
    assert learner.effective_kw(LOAD_SPA, None) == (2.9, SOURCE_LEARNED)
    assert learner.learned(LOAD_SPA)[1] == "device"


def test_a_sensor_that_does_not_follow_an_off_loses_its_trust() -> None:
    learner = PowerLearner()
    sensor = "sensor.plug"
    now = T0
    learner.observe(now, {LOAD_SAUNA: Look(False, sensor, 0.0, None)})
    now += S30
    learner.observe(now, {LOAD_SAUNA: Look(True, sensor, 0.0, None)})
    now += S30
    learner.observe(now, {LOAD_SAUNA: Look(True, sensor, 8000.0, None)})
    assert learner.loads[LOAD_SAUNA].trusted_sensor == sensor
    learner.observe(now + S30, {LOAD_SAUNA: Look(False, sensor, 8000.0, None)})
    for step in range(2, 8):
        learner.observe(now + step * S30, {LOAD_SAUNA: Look(False, sensor, 8000.0, None)})
    assert learner.loads[LOAD_SAUNA].trusted_sensor is None


def test_what_is_learned_is_kept_and_read_as_untrusted() -> None:
    learner = PowerLearner()
    _sessions(learner, LOAD_SAUNA, 1500.0, 8.7, 2)
    again = PowerLearner()
    again.restore(learner.as_dict())
    assert again.effective_kw(LOAD_SAUNA, None) == (8.7, SOURCE_LEARNED)
    broken = PowerLearner()
    broken.restore({"sauna": {"steps": ["x", 99.0, None], "trusted_sensor": 3}, "fridge": {"steps": [1.0]}, "spa": "no"})
    assert broken.loads["sauna"].steps == [] and broken.loads["sauna"].trusted_sensor is None and "fridge" not in broken.loads


def test_the_shadow_hands_the_learned_power_to_everything_that_counts_on_it() -> None:
    from custom_components.spotnav.heat.config import read_config
    from custom_components.spotnav.pool.runtime import PoolSpaShadow

    config = read_config(
        {
            "sauna": {"entity_id": "switch.sauna", "family": "relay", "target_c": 80},
            "spa": {"entity_id": "climate.spa", "family": "balboa", "heater_kw": 3.5},
        }
    )
    shadow = PoolSpaShadow.__new__(PoolSpaShadow)
    shadow.power = PowerLearner()
    shadow.site = SimpleNamespace(heat_shadow=SimpleNamespace(config=lambda: config))
    assert shadow.config().sauna.heater_kw is None
    _sessions(shadow.power, LOAD_SAUNA, 1500.0, 8.7, 2)
    _sessions(shadow.power, LOAD_SPA, 1500.0, 2.8, 2)
    effective = shadow.config()
    assert effective.sauna.heater_kw == 8.7
    # The entered spa kW stays the override.
    assert effective.spa.heater_kw == 3.5
    assert shadow.power_of(LOAD_SAUNA, None, 9.0) == {"kw": 8.7, "kw_source": "learned"}
    assert shadow.power_of(LOAD_SPA, 3.5, 3.0) == {"kw": 3.5, "kw_source": "entered"}
    assert shadow.power_of(LOAD_POOL_CIRCULATION, None, 0.4) == {"kw": 0.4, "kw_source": "typical"}
