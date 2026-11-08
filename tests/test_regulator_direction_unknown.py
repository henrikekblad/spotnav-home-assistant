"""The regulator on a phase whose direction is not known (`regulator.magnitude_decision`): a charge is lowered on an
overload by the measured |I| as before, and raised again within the room |I| leaves, never above its request. A
raise of d on a phase reading |I| gives at most |I| + d whichever way the phase flows, so the room is `limit - |I|`
on every phase the charger draws on, export counted as load, not as room. Chargers share that room as raises are
shared elsewhere (`allocate_regulator_decisions`)."""

from __future__ import annotations

from custom_components.spotnav.site.regulator import (
    allocate_regulator_decisions,
    magnitude_decision,
    MAGNITUDE_LOWERING_REASON,
    MAGNITUDE_RAISE_REASON,
)
from custom_components.spotnav.site.site_capacity import PHASES, ChargerRequest

from .test_regulator_direct_signed import _direct_site, _single_phase, _three, _three_phase, MAX_AGE, V

RAISE = MAGNITUDE_RAISE_REASON
LOWERING = MAGNITUDE_LOWERING_REASON


def _unsigned(currents, *, fuse: float = 25.0, margin: float = 1.0):
    """A direct site whose currents carry no sign: the direction is never known."""
    return _direct_site(currents, fuse=fuse, margin=margin, signed=False, confirmed=())


def _charger(entry_id: str, measured: float, requested: float, *, order: int = 0, **kwargs) -> ChargerRequest:
    return ChargerRequest(
        charger_entry_id=entry_id,
        requested_current_a=requested,
        phases=3,
        measured_current_a=_three((measured, measured, measured)),
        order=order,
        **kwargs,
    )


def _allocate(site, requests, **kwargs):
    kwargs.setdefault("voltage_by_phase", {phase: None for phase in PHASES})
    kwargs.setdefault("confirmed_direction_by_phase", {phase: None for phase in PHASES})
    return allocate_regulator_decisions(site_result=site, requests=requests, max_age_s=MAX_AGE, **kwargs)


def test_a_charge_below_its_request_is_raised_within_the_room_the_magnitude_leaves() -> None:
    # 24 A limit; L3 reads 18 A with the car at 6 A on every phase: 6 A of room there, 14 A on the others.
    site = _unsigned((10.0, 10.0, 18.0))
    decision = magnitude_decision(site_result=site, request=_three_phase(6.0, 16.0), max_age_s=MAX_AGE)
    assert (decision.proposed_current_a, decision.reason, decision.limiting_phase) == (12.0, RAISE, "L3")
    assert set(decision.basis) == set(PHASES)
    assert decision.basis["L3"].measured_current_a == 6.0
    assert decision.basis["L3"].requested_current_a == 16.0


def test_an_exporting_phase_is_given_its_magnitude_as_room_not_its_export() -> None:
    """L3 exports 20 A, which the car's draw reduces, but with the sign unconfirmed that is 20 A of load against a
    25 A fuse: the raise is 5 A at most, where a known export would have left room for the whole request."""
    site = _direct_site((-4.0, -4.0, -20.0), fuse=25.0, margin=0.0, confirmed=())
    assert site.phase_direction_source["L3"] is None
    request = _single_phase("L3", 6.0, 16.0, basis="measured")
    decision = magnitude_decision(site_result=site, request=request, max_age_s=MAX_AGE)
    assert (decision.proposed_current_a, decision.reason, decision.limiting_phase) == (11.0, RAISE, "L3")


def test_no_raise_above_the_request() -> None:
    site = _unsigned((4.0, 4.0, 4.0))
    decision = magnitude_decision(site_result=site, request=_three_phase(6.0, 10.0), max_age_s=MAX_AGE)
    assert (decision.proposed_current_a, decision.reason) == (10.0, RAISE)
    # The request reached: nothing to raise.
    same = magnitude_decision(site_result=site, request=_three_phase(10.0, 10.0), max_age_s=MAX_AGE)
    assert (same.proposed_current_a, same.reason) == (None, "phase_direction_unknown")


def test_the_room_is_rounded_down_to_a_whole_amp_and_must_be_one() -> None:
    site = _unsigned((10.0, 10.0, 21.4))  # 2.6 A of room on L3
    decision = magnitude_decision(site_result=site, request=_three_phase(6.0, 16.0), max_age_s=MAX_AGE)
    assert decision.proposed_current_a == 8.0
    tight = _unsigned((10.0, 10.0, 23.4))  # 0.6 A: not a whole amp
    held = magnitude_decision(site_result=tight, request=_three_phase(6.0, 16.0), max_age_s=MAX_AGE)
    assert (held.proposed_current_a, held.reason) == (None, "phase_direction_unknown")


def test_room_another_writer_was_given_is_not_given_again() -> None:
    site = _unsigned((10.0, 10.0, 18.0))
    request = _three_phase(6.0, 16.0)
    held = magnitude_decision(
        site_result=site, request=request, max_age_s=MAX_AGE, increase_reserved_by_phase={"L3": 4.0}
    )
    assert held.proposed_current_a == 8.0  # 6 A of room on L3, 4 A of it the sun's until drawn
    gone = magnitude_decision(
        site_result=site, request=request, max_age_s=MAX_AGE, increase_reserved_by_phase={"L3": 6.0}
    )
    assert gone.proposed_current_a is None


def test_what_needs_the_direction_is_held() -> None:
    site = _unsigned((10.0, 10.0, 18.0))
    # A lowering that is not an overload: the writer's (the sun's own write), or nobody's.
    lowering = magnitude_decision(site_result=site, request=_three_phase(10.0, 6.0), max_age_s=MAX_AGE)
    assert (lowering.proposed_current_a, lowering.reason) == (None, "phase_direction_unknown")
    # A charger not drawing yet: a start gives it its current, a paused charge is resumed, not raised.
    idle = magnitude_decision(site_result=site, request=_three_phase(0.0, 16.0), max_age_s=MAX_AGE)
    assert idle.proposed_current_a is None
    # A charger that can only be switched has no current to raise.
    switched = magnitude_decision(
        site_result=site, request=_charger("switch", 6.0, 10.0, switch_only=True, min_current_a=10.0), max_age_s=MAX_AGE
    )
    assert switched.proposed_current_a is None
    # An unknown request.
    unknown = magnitude_decision(site_result=site, request=_three_phase(6.0, None), max_age_s=MAX_AGE)  # type: ignore[arg-type]
    assert unknown.proposed_current_a is None
    # The held reason is the caller's.
    at_start = magnitude_decision(
        site_result=site, request=_three_phase(10.0, 6.0), max_age_s=MAX_AGE, held_reason="phase_direction_unknown_at_charge_start"
    )
    assert at_start.reason == "phase_direction_unknown_at_charge_start"


def test_the_lowering_on_an_overload_is_unchanged_and_comes_first() -> None:
    site = _unsigned((10.0, 10.0, 32.0))  # 8 A over on L3 with the car at 16 A: lowered to 8 A
    decision = magnitude_decision(site_result=site, request=_three_phase(16.0, 20.0), max_age_s=MAX_AGE)
    assert (decision.proposed_current_a, decision.reason, decision.limiting_phase) == (8.0, LOWERING, "L3")
    # A charger that can only be switched is paused whole, under the lowering's reason.
    switched = magnitude_decision(
        site_result=site, request=_charger("switch", 10.0, 10.0, switch_only=True, min_current_a=10.0), max_age_s=MAX_AGE
    )
    assert (switched.proposed_current_a, switched.reason) == (0.0, LOWERING)
    # A phase known to export over the limit is never lowered on, nor is export counted as room for a raise.
    exporting = _direct_site((-4.0, -4.0, -30.0), fuse=25.0, margin=1.0, confirmed=("L3",))
    kept = magnitude_decision(site_result=exporting, request=_single_phase("L3", 10.0, 16.0, basis="measured"), max_age_s=MAX_AGE)
    assert kept.proposed_current_a is None


def test_two_chargers_on_the_same_phases_share_the_room() -> None:
    """6 A of room on L3: the first charger in the allocation order takes it, the second is left nothing; with 10 A the
    second gets what the first left. The steps never add up to more than the room."""
    first, second = _charger("a", 6.0, 16.0, order=0), _charger("b", 6.0, 16.0, order=1)
    tight = _allocate(_unsigned((10.0, 10.0, 18.0)), [first, second], direction_unknown_in_charge={"a", "b"})
    assert (tight["a"].proposed_current_a, tight["a"].reason) == (12.0, RAISE)
    assert (tight["b"].proposed_current_a, tight["b"].reason) == (None, "phase_direction_unknown")

    roomy = _allocate(_unsigned((10.0, 10.0, 14.0)), [first, second], direction_unknown_in_charge={"a", "b"})
    assert roomy["a"].proposed_current_a == 16.0  # 10 A of room, the whole request
    assert roomy["b"].proposed_current_a is None  # nothing left
    shared = _allocate(
        _unsigned((10.0, 10.0, 14.0)),
        [_charger("a", 6.0, 12.0, order=0), _charger("b", 6.0, 16.0, order=1)],
        direction_unknown_in_charge={"a", "b"},
    )
    assert shared["a"].proposed_current_a == 12.0  # takes 6 of the 10
    assert shared["b"].proposed_current_a == 10.0  # the 4 left
    steps = sum(decision.proposed_current_a - 6.0 for decision in shared.values())
    assert steps <= 10.0


def test_a_held_magnitude_decision_blocks_no_phase_for_the_next_charger() -> None:
    """The first charger has nothing to do (its request reached); the second still gets the room."""
    site = _unsigned((10.0, 10.0, 18.0))
    decisions = _allocate(
        site, [_charger("a", 6.0, 6.0, order=0), _charger("b", 6.0, 16.0, order=1)], direction_unknown_in_charge={"a", "b"}
    )
    assert decisions["a"].proposed_current_a is None
    assert (decisions["b"].proposed_current_a, decisions["b"].reason) == (12.0, RAISE)


def test_only_a_charger_in_a_charge_is_decided_on_the_magnitude() -> None:
    site = _unsigned((10.0, 10.0, 18.0))
    request = _charger("a", 6.0, 16.0)
    outside = _allocate(site, [request])
    assert (outside["a"].proposed_current_a, outside["a"].reason) == (None, "phase_direction_unknown")
    inside = _allocate(site, [request], direction_unknown_in_charge={"a"})
    assert inside["a"].reason == RAISE


def test_a_charge_started_without_a_direction_stays_on_the_magnitude_once_confirmed() -> None:
    """Every phase imports and is confirmed now, but the charge started before that: the sun (or nobody) writes it,
    and the regulator's full rules never reach it. The room is still |I|'s: 8 A on L3."""
    site = _direct_site((10.0, 10.0, 16.0), fuse=25.0, margin=1.0, confirmed=PHASES)
    assert all(site.phase_direction_source[phase] for phase in PHASES)
    request = _charger("a", 6.0, 16.0)
    full = _allocate(site, [request], confirmed_direction_by_phase={phase: "net_import" for phase in PHASES}, voltage_by_phase={phase: V for phase in PHASES})
    assert full["a"].reason == "increase_within_safe_uncredited_margin"
    at_start = _allocate(site, [request], direction_unknown_at_start={"a"})
    assert (at_start["a"].proposed_current_a, at_start["a"].reason) == (14.0, RAISE)
    reached = _allocate(site, [_charger("a", 6.0, 6.0)], direction_unknown_at_start={"a"})
    assert (reached["a"].proposed_current_a, reached["a"].reason) == (None, "phase_direction_unknown_at_charge_start")


def test_a_signed_site_with_a_confirmed_direction_is_decided_as_before() -> None:
    """The magnitude's rules are a fallback: where `decide_charging_current` decides, it still does."""
    site = _direct_site((10.0, 10.0, 16.0), fuse=25.0, margin=1.0, confirmed=PHASES)
    decisions = _allocate(
        site,
        [_charger("a", 6.0, 16.0)],
        confirmed_direction_by_phase={phase: "net_import" for phase in PHASES},
        voltage_by_phase={phase: V for phase in PHASES},
        direction_unknown_in_charge={"a"},
    )
    assert decisions["a"].reason == "increase_within_safe_uncredited_margin"
