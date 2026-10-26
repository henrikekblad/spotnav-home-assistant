"""Confirming a direct site's per-phase current sign against the meter's total (`site/current_sign.py`)."""

from __future__ import annotations

from custom_components.spotnav.site.current_sign import CurrentSignCheck
from custom_components.spotnav.site.regulator import (
    DEFAULT_MIN_CONSECUTIVE_CONFIRMATIONS,
    DEFAULT_ZERO_MARGIN_W,
)
from custom_components.spotnav.site.site_capacity import DIRECTION_NOMINAL_PHASE_VOLTAGE_V, PHASES

V = DIRECTION_NOMINAL_PHASE_VOLTAGE_V
# L1 and L2 exporting, L3 importing (a household with solar).
USER_SITE = (-4.9, -6.1, 12.4)


def _check() -> CurrentSignCheck:
    return CurrentSignCheck(
        zero_margin_w=DEFAULT_ZERO_MARGIN_W, min_agreements=DEFAULT_MIN_CONSECUTIVE_CONFIRMATIONS
    )


def _currents(l1, l2, l3):
    return {"L1": l1, "L2": l2, "L3": l3}


def test_phases_are_confirmed_after_three_agreeing_readings() -> None:
    check = _check()
    reading = _currents(4.0, 6.0, 10.0)
    total = 20.0 * V * 0.95
    for _ in range(DEFAULT_MIN_CONSECUTIVE_CONFIRMATIONS - 1):
        check.observe(reading, total)
        assert check.confirmed == frozenset()
    check.observe(reading, total)
    assert check.confirmed == frozenset(PHASES)


def test_a_phase_inside_the_zero_band_is_not_confirmed() -> None:
    check = _check()
    for _ in range(5):
        check.observe(_currents(1.0, 6.0, 10.0), 17.0 * V)
    assert check.confirmed == frozenset({"L2", "L3"})


def test_an_export_positive_meter_is_never_confirmed() -> None:
    """A source that counts export as positive (SolarEdge's meter) disagrees with the total."""
    check = _check()
    for _ in range(10):
        check.observe(_currents(-4.0, -6.0, -10.0), 20.0 * V)
        check.observe(_currents(4.0, 6.0, 10.0), -20.0 * V)
    assert check.confirmed == frozenset()


def test_a_contradiction_withdraws_every_phase() -> None:
    check = _check()
    for _ in range(3):
        check.observe(_currents(4.0, 6.0, 10.0), 20.0 * V)
    assert check.confirmed == frozenset(PHASES)
    check.observe(_currents(4.0, 6.0, 10.0), -20.0 * V)
    assert check.confirmed == frozenset()


def test_mixed_phases_neither_confirm_nor_withdraw() -> None:
    check = _check()
    for _ in range(5):
        check.observe(_currents(*USER_SITE), 1.4 * V + 600.0)
    assert check.confirmed == frozenset()
    for _ in range(3):
        check.observe(_currents(4.0, 6.0, 10.0), 20.0 * V)
    for _ in range(5):
        check.observe(_currents(-4.9, -6.1, 14.0), 3.0 * V + 600.0)
    assert check.confirmed == frozenset(PHASES)


def test_one_phase_against_the_others_and_the_total_is_withdrawn() -> None:
    check = _check()
    for _ in range(3):
        check.observe(_currents(4.0, 6.0, 10.0), 20.0 * V)
    check.observe(_currents(4.0, 6.0, -10.0), 9.0 * V)
    assert check.confirmed == frozenset({"L1", "L2"})


def test_a_reversed_phase_does_not_add_up_and_is_not_confirmed() -> None:
    """L3 truly imports 4 A but reads -4 A while L1 and L2 export: every sign agrees with the total, the sum
    does not."""
    check = _check()
    for _ in range(10):
        check.observe(_currents(-8.0, -8.0, -4.0), (-8.0 - 8.0 + 4.0) * V)
    assert check.confirmed == frozenset()


def test_a_missing_or_quiet_reading_is_skipped() -> None:
    check = _check()
    for _ in range(2):
        check.observe(_currents(4.0, 6.0, 10.0), 20.0 * V)
    check.observe(_currents(4.0, None, 10.0), 20.0 * V)
    check.observe(_currents(4.0, 6.0, 10.0), None)
    check.observe(_currents(4.0, 6.0, 10.0), 100.0)
    check.observe(_currents(4.0, 6.0, 10.0), 20.0 * V)
    assert check.confirmed == frozenset(PHASES)
