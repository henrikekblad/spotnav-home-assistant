"""The sun with a charger that can only be switched on and off: it starts on a surplus that covers its whole learned
current and stops once the surplus, its own draw added back, stays below it (`SolarExecutionCoordinator._solar_config`).
"""

from __future__ import annotations

from types import SimpleNamespace

from custom_components.spotnav.execution.solar_execution import SolarExecutionCoordinator
from custom_components.spotnav.execution.switched_current import SwitchedCurrentValue
from custom_components.spotnav.site.solar_surplus import SolarController, SolarObservation

PHASES = ("L1", "L2", "L3")
V = 230.0


def _config(amps: float):
    coordinator = SolarExecutionCoordinator.__new__(SolarExecutionCoordinator)
    coordinator._controller = SimpleNamespace(  # noqa: SLF001
        is_switch_only=True, switched_current=lambda: SwitchedCurrentValue(amps, "measured")
    )
    coordinator._charger_entry_id = "fixed"  # noqa: SLF001
    return coordinator._solar_config(SimpleNamespace(config={}))  # noqa: SLF001


def _obs(now: float, *, export_a: float, car_a: float) -> SolarObservation:
    """`export_a` per phase the meter shows leaving the house (negative: importing), with the car drawing `car_a`."""
    return SolarObservation(
        now=now,
        signed_grid_w={phase: -export_a * V for phase in PHASES},
        voltage_v={phase: V for phase in PHASES},
        car_delivered_a={phase: car_a for phase in PHASES},
        battery_w=None,
        car_phases=PHASES,
    )


def test_a_fixed_16_a_charger_is_reckoned_at_16_a_whole() -> None:
    config = _config(16.0)
    assert (config.start_a, config.stop_a, config.min_current_a, config.max_current_a) == (16.0, 16.0, 16.0, 16.0)


def test_a_fixed_16_a_charger_does_not_start_on_a_surplus_for_6_a() -> None:
    solar = SolarController(_config(16.0))
    verdicts = [solar.observe(_obs(float(t), export_a=8.0, car_a=0.0)) for t in range(0, 900, 30)]
    assert {verdict.action for verdict in verdicts} == {"hold"}
    assert {verdict.state for verdict in verdicts} == {"off"}


def test_a_fixed_16_a_charger_starts_on_a_surplus_for_all_of_it_and_stops_when_it_falls_short() -> None:
    config = _config(16.0)
    solar = SolarController(config)
    verdicts = [solar.observe(_obs(float(t), export_a=16.5, car_a=0.0)) for t in range(0, 300, 30)]
    starts = [verdict for verdict in verdicts if verdict.action == "start"]
    assert len(starts) == 1 and starts[0].requested_a == 16.0

    # Running at 16 A: the surplus with its own draw added back is 16.5 A, then 14 A (the house imports 2 A).
    t = 300.0
    while t < 300.0 + config.verify_s + config.min_on_s:
        assert solar.observe(_obs(t, export_a=0.5, car_a=16.0)).action != "stop"
        t += 30.0
    short = []
    for _ in range(int(config.stop_delay_s // 30) + 2):
        short.append(solar.observe(_obs(t, export_a=-2.0, car_a=16.0)))
        t += 30.0
    stops = [verdict for verdict in short if verdict.action == "stop"]
    assert len(stops) == 1
    # Not before the stop delay.
    first_short_at = short[0]
    assert first_short_at.action != "stop"
