"""The two bands, the deadband and the dwell of `site/regulator_damping.py`.

Every test injects a clock, so nothing sleeps. Input is a measurement and a proposal, output a
decision: no Home Assistant, services or charger. The recorded sequence is a real session of 151
writes in two hours (93 in ten minutes) to a car drawing 0.57 A and following none of them.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from custom_components.spotnav.site.regulator_damping import (
    DEFAULT_REGULATOR_DEADBAND_A,
    DEFAULT_REGULATOR_DWELL_S,
    PROTECTION_BAND_A,
    RegulatorDamper,
    band_for_measurement,
)

# The assigned currents recorded for that session, as written (57 of the 93).
RECORDED_SESSION = [
    15, 16, 15, 16, 15, 16, 15, 14, 15, 14, 15, 16, 15, 16, 14, 15, 14, 15, 14,
    7, 9, 7, 9, 15, 7, 13, 12, 7, 9, 7, 16, 7, 15, 14, 8, 7, 9, 12,
    15, 9, 7, 14, 8, 7, 9, 7, 16, 8, 9, 7, 14, 8, 7, 9, 14, 10, 9,
]

# 93 commands in ten minutes: one every 6.45 s. Replaying at the recorded cadence makes the write count comparable.
RECORDED_STEP_S = 6.45

# The car's measured current for the whole session. It followed nothing, so every recorded value is that moment's `current + uncredited margin` ceiling.
RECORDED_CAR_CURRENT_A = 0.57


class _Clock:
    """A clock the test advances by hand."""

    def __init__(self) -> None:
        self._now = datetime(2026, 9, 20, 10, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> datetime:
        self._now += timedelta(seconds=seconds)
        return self._now


def _damper(**kwargs) -> RegulatorDamper:
    return RegulatorDamper(now=_Clock(), **kwargs)


@pytest.mark.parametrize(
    ("margin_a", "expected"),
    [
        (-5.0, "protection"),
        (0.0, "protection"),
        (PROTECTION_BAND_A, "protection"),
        (PROTECTION_BAND_A + 0.01, "regulation"),
        (12.0, "regulation"),
        (None, None),
    ],
)
def test_the_band_is_a_function_of_the_margin_alone(margin_a, expected) -> None:
    """The band depends on one number and nothing else: no clock, no state."""
    assert band_for_measurement(margin_a) == expected


def test_an_unreadable_margin_is_never_a_licence_to_write() -> None:
    damper = _damper()
    decision = damper.consider(proposed_current_a=12.0, margin_a=None)

    assert decision.write is False
    assert decision.reason == "margin_unusable"


def test_a_refusal_is_not_a_reason_to_write() -> None:
    """`None` from the decision means "I cannot say", never "keep what is there"."""
    damper = _damper()
    decision = damper.consider(proposed_current_a=None, margin_a=8.0)

    assert decision.write is False
    assert decision.reason == "no_proposal"


def test_the_recorded_session_produces_far_fewer_writes() -> None:
    """The same measurements, replayed, stop being 57 writes.

    Seven is the arithmetic ceiling: the session ran 57 x 6.45 s = 368 s, a write needs its level justified
    continuously for the 60 s dwell, and the first write is exempt, so at most `368 // 60 + 1` writes happen.
    The replay comes out at one because the recorded values never hold still for a minute.
    """
    clock = _Clock()
    damper = RegulatorDamper(now=clock)
    written: list[float] = []

    for value in RECORDED_SESSION:
        proposal = float(value)
        decision = damper.consider(
            proposed_current_a=proposal,
            margin_a=proposal - RECORDED_CAR_CURRENT_A,
        )
        if decision.write:
            written.append(decision.current_a)
        clock.advance(RECORDED_STEP_S)

    assert len(written) <= int(len(RECORDED_SESSION) * RECORDED_STEP_S // 60) + 1
    assert written == [15.0]


def test_damping_only_ever_writes_values_the_decision_produced() -> None:
    """Calmer, never bolder: every value written is one of the proposals itself, never an invented intermediate."""
    clock = _Clock()
    damper = RegulatorDamper(now=clock)
    proposals = [float(value) for value in RECORDED_SESSION]
    written: list[float] = []

    for proposal in proposals:
        decision = damper.consider(proposed_current_a=proposal, margin_a=proposal)
        if decision.write:
            written.append(decision.current_a)
        clock.advance(RECORDED_STEP_S)

    assert written  # not vacuously true: this replay does write
    assert set(written) <= set(proposals)


def test_the_first_write_of_a_session_is_immediate() -> None:
    """Nothing has been written, so there is no change to damp."""
    decision = _damper().consider(proposed_current_a=11.0, margin_a=9.0)

    assert decision.write is True
    assert decision.current_a == 11.0
    assert decision.reason == "first_write"


def test_a_one_amp_improvement_is_never_written() -> None:
    """One amp is exactly where the recorded session's dither lived."""
    clock = _Clock()
    damper = RegulatorDamper(now=clock)
    damper.consider(proposed_current_a=10.0, margin_a=9.0)

    decision = damper.consider(proposed_current_a=11.0, margin_a=9.0)
    assert decision.write is False
    assert decision.reason == "deadband"

    # It stays unwritten however long it is asked for.
    clock.advance(DEFAULT_REGULATOR_DWELL_S * 5)
    assert damper.consider(proposed_current_a=11.0, margin_a=9.0).write is False
    assert damper.last_written_a == 10.0


def test_the_deadband_is_two_sided() -> None:
    """Small reductions are ignored too: the rule is not "down fast"."""
    damper = _damper()
    damper.consider(proposed_current_a=10.0, margin_a=9.0)

    assert damper.consider(proposed_current_a=9.0, margin_a=9.0).reason == "deadband"


def test_a_two_amp_improvement_is_written_only_after_the_dwell() -> None:
    clock = _Clock()
    damper = RegulatorDamper(now=clock)
    damper.consider(proposed_current_a=10.0, margin_a=9.0)

    # Exactly at the deadband is a change: the deadband is "smaller than".
    assert damper.consider(proposed_current_a=12.0, margin_a=11.0).reason == "dwell_pending"

    clock.advance(DEFAULT_REGULATOR_DWELL_S - 1)
    assert damper.consider(proposed_current_a=12.0, margin_a=11.0).write is False

    clock.advance(1)
    decision = damper.consider(proposed_current_a=12.0, margin_a=11.0)
    assert decision.write is True
    assert decision.current_a == 12.0
    assert decision.reason == "dwell_elapsed"
    assert damper.last_written_a == 12.0


def test_an_improvement_that_disappears_within_the_dwell_is_never_written() -> None:
    """A change asked for, dropped before its minute is up, and asked again starts its own minute rather than inheriting the first."""
    clock = _Clock()
    damper = RegulatorDamper(now=clock)
    damper.consider(proposed_current_a=10.0, margin_a=9.0)

    assert damper.consider(proposed_current_a=13.0, margin_a=12.0).reason == "dwell_pending"
    clock.advance(30)
    # Back to what the charger is already set to: inside the deadband, so the candidate is dropped rather than left ticking.
    assert damper.consider(proposed_current_a=10.0, margin_a=9.0).reason == "deadband"
    clock.advance(60)
    assert damper.consider(proposed_current_a=13.0, margin_a=12.0).reason == "dwell_pending"
    clock.advance(30)
    assert damper.consider(proposed_current_a=13.0, margin_a=12.0).write is False
    assert damper.last_written_a == 10.0


def test_a_flickering_candidate_is_replaced_rather_than_accumulated() -> None:
    """Two alternating levels: neither may be written on the other's time."""
    clock = _Clock()
    damper = RegulatorDamper(now=clock)
    damper.consider(proposed_current_a=10.0, margin_a=9.0)

    for _ in range(5):
        damper.consider(proposed_current_a=13.0, margin_a=12.0)
        clock.advance(DEFAULT_REGULATOR_DWELL_S * 0.6)
        damper.consider(proposed_current_a=16.0, margin_a=15.0)
        clock.advance(DEFAULT_REGULATOR_DWELL_S * 0.6)

    assert damper.last_written_a == 10.0


def test_protection_writes_a_reduction_at_once_with_no_deadband_and_no_dwell() -> None:
    """Protection is immediate, one amp or not, at the value the decision computed.

    The same one-amp reduction is a `deadband` hold with a healthy margin: what makes it immediate
    is where the measurement is, not the size of the change.
    """
    damper = _damper()
    damper.consider(proposed_current_a=10.0, margin_a=9.0)

    decision = damper.consider(proposed_current_a=9.0, margin_a=PROTECTION_BAND_A - 0.5)

    assert decision.write is True
    assert decision.current_a == 9.0
    assert decision.reason == "protection"


def test_a_regulation_reduction_waits_and_a_protection_reduction_does_not() -> None:
    """The same 4 A reduction, judged only by the margin it was decided on."""
    waiting = _damper()
    waiting.consider(proposed_current_a=14.0, margin_a=12.0)
    held = waiting.consider(proposed_current_a=10.0, margin_a=11.0)
    assert held.write is False
    assert held.reason == "dwell_pending"

    immediate = _damper()
    immediate.consider(proposed_current_a=14.0, margin_a=12.0)
    written = immediate.consider(proposed_current_a=10.0, margin_a=0.5)
    assert written.write is True
    assert written.reason == "protection"


def test_a_raise_is_never_protection_however_close_the_limit_is() -> None:
    """A change away from the limit protects nothing.

    With the margin inside the band the decision's ceiling is at most an amp above the current
    draw, inside the deadband, so it is a hold rather than an immediate write.
    """
    damper = _damper()
    damper.consider(proposed_current_a=10.0, margin_a=9.0)

    decision = damper.consider(proposed_current_a=10.5, margin_a=0.5)

    assert decision.write is False
    assert decision.reason == "deadband"


def test_the_defaults_are_the_configured_ones() -> None:
    """The two numbers are configuration, so their defaults are the contract."""
    assert DEFAULT_REGULATOR_DEADBAND_A == 2.0
    assert DEFAULT_REGULATOR_DWELL_S == 60.0


def test_a_deadband_of_zero_can_still_write() -> None:
    """A deadband of zero means "no deadband", not "never write".

    The candidate-replacement guard must not use ">=": with a zero deadband it would replace the
    candidate on every pass, re-stamping its start time so the dwell could never elapse.
    """
    clock = _Clock()
    damper = RegulatorDamper(deadband_a=0.0, dwell_s=0.0, now=clock)
    damper.consider(proposed_current_a=10.0, margin_a=9.0)

    decision = damper.consider(proposed_current_a=11.0, margin_a=9.0)

    assert decision.write is True
    assert decision.current_a == 11.0


def test_a_deadband_of_zero_still_never_rewrites_the_value_already_there() -> None:
    """A zero deadband is not a licence to re-send what the charger already is: an identical proposal is not a change."""
    clock = _Clock()
    damper = RegulatorDamper(deadband_a=0.0, now=clock)
    damper.consider(proposed_current_a=10.0, margin_a=9.0)

    decision = damper.consider(proposed_current_a=10.0, margin_a=9.0)
    clock.advance(DEFAULT_REGULATOR_DWELL_S * 10)
    again = damper.consider(proposed_current_a=10.0, margin_a=9.0)

    assert decision.write is False
    assert again.write is False
    assert again.reason == "deadband"


def test_a_deadband_of_zero_still_needs_the_dwell_to_elapse() -> None:
    """No deadband does not mean no dwell: the two settings are independent."""
    clock = _Clock()
    damper = RegulatorDamper(deadband_a=0.0, now=clock)
    damper.consider(proposed_current_a=10.0, margin_a=9.0)

    assert damper.consider(proposed_current_a=11.0, margin_a=9.0).reason == "dwell_pending"
    clock.advance(DEFAULT_REGULATOR_DWELL_S - 1)
    assert damper.consider(proposed_current_a=11.0, margin_a=9.0).write is False
    clock.advance(1)
    assert damper.consider(proposed_current_a=11.0, margin_a=9.0).write is True


def test_urgent_writes_a_sub_deadband_reduction_immediately_with_no_dwell() -> None:
    """A one-amp reduction is ordinarily a `deadband` hold; `urgent=True` writes it at once with no dwell, as a verified revert or release needs."""
    clock = _Clock()
    damper = RegulatorDamper(now=clock)
    damper.consider(proposed_current_a=10.0, margin_a=9.0)

    decision = damper.consider(proposed_current_a=9.0, margin_a=9.0, urgent=True)

    assert decision.write is True
    assert decision.current_a == 9.0
    assert decision.reason == "urgent"
    assert damper.last_written_a == 9.0


def test_urgent_never_rewrites_an_identical_value() -> None:
    """Urgency accelerates a genuine change but does not license re-sending what the charger is already set to."""
    clock = _Clock()
    damper = RegulatorDamper(now=clock)
    damper.consider(proposed_current_a=10.0, margin_a=9.0)

    decision = damper.consider(proposed_current_a=10.0, margin_a=9.0, urgent=True)

    assert decision.write is False
    assert decision.reason == "deadband"
    assert damper.last_written_a == 10.0


def test_urgent_defaults_to_false_so_existing_callers_are_unaffected() -> None:
    """Callers that omit `urgent` still wait for the dwell on a two-amp change."""
    clock = _Clock()
    damper = RegulatorDamper(now=clock)
    damper.consider(proposed_current_a=10.0, margin_a=9.0)

    decision = damper.consider(proposed_current_a=12.0, margin_a=11.0)

    assert decision.write is False
    assert decision.reason == "dwell_pending"


def test_urgency_never_hastens_an_increase() -> None:
    """Nothing makes the regulator bolder faster: an urgent increase is damped like an ordinary one and written only after the dwell."""
    clock = [0.0]
    start = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)
    damper = RegulatorDamper(now=lambda: start + timedelta(seconds=clock[0]))
    damper.consider(proposed_current_a=6.0, margin_a=5.0)

    first = damper.consider(proposed_current_a=10.0, margin_a=5.0, urgent=True)
    assert first.write is False

    clock[0] = 61.0
    later = damper.consider(proposed_current_a=10.0, margin_a=5.0, urgent=True)
    assert later.write is True
    assert later.reason != "urgent"
