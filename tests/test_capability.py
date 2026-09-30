"""Tests for the site capability/health snapshot."""

from __future__ import annotations

from custom_components.spotnav.vehicles import capability
from custom_components.spotnav.vehicles.capability import build_capability_snapshot
from custom_components.spotnav.site.site_capacity import (
    ChargerRequest,
    DirectPhaseMeasurement,
    PhaseValue,
    SiteCalculationConfig,
)

HEALTHY_AGE = 5.0
MAX_AGE = 120.0


def _direct(l1: float, l2: float, l3: float, age: float = HEALTHY_AGE) -> DirectPhaseMeasurement:
    return DirectPhaseMeasurement(
        l1=PhaseValue(l1, age), l2=PhaseValue(l2, age), l3=PhaseValue(l3, age)
    )


def _config(*, enabled: bool = False, direct=None, main_fuse_a: float | None = 25.0, mode="direct_phase_current") -> SiteCalculationConfig:
    return SiteCalculationConfig(
        enabled=enabled,
        main_fuse_a=main_fuse_a,
        safety_margin_a=1.0,
        measurement_mode=mode,
        max_age_s=MAX_AGE,
        direct=direct,
    )


def _charger(charger_entry_id="charger_a", requested_current_a=10.0, measured_current_a=None):
    return ChargerRequest(
        charger_entry_id=charger_entry_id,
        requested_current_a=requested_current_a,
        phases=3,
        measured_current_a=measured_current_a,
    )


def test_healthy_site_measurement_even_though_the_site_is_disabled() -> None:
    # enabled=False, but health is still visible, so a user can see the setup works before enabling.
    config = _config(enabled=False, direct=_direct(5.0, 5.0, 5.0))
    snapshot = build_capability_snapshot(
        config=config,
        requests=[_charger()],
        charger_has_charge_control={"charger_a": True},
        charger_has_current_control={"charger_a": False},
        membership_conflict_charger_ids=[],
    )

    assert snapshot.site_measurement.configured is True
    assert snapshot.site_measurement.health == "healthy"
    assert snapshot.load_balancing.calculation_available is True
    assert snapshot.load_balancing.calculation_enabled is False


def test_not_configured_when_main_fuse_is_missing() -> None:
    config = _config(main_fuse_a=None, direct=None)
    snapshot = build_capability_snapshot(
        config=config, requests=[], charger_has_charge_control={}, charger_has_current_control={},
        membership_conflict_charger_ids=[],
    )
    assert snapshot.site_measurement.configured is False
    assert snapshot.site_measurement.health == "not_configured"
    assert snapshot.load_balancing.calculation_available is False


def test_missing_site_measurement_health() -> None:
    empty = DirectPhaseMeasurement(
        l1=PhaseValue(None, problem="missing"),
        l2=PhaseValue(None, problem="missing"),
        l3=PhaseValue(None, problem="missing"),
    )
    config = _config(direct=empty)
    snapshot = build_capability_snapshot(
        config=config, requests=[], charger_has_charge_control={}, charger_has_current_control={},
        membership_conflict_charger_ids=[],
    )
    assert snapshot.site_measurement.health == "configured_missing"
    assert snapshot.load_balancing.calculation_available is False


def test_stale_site_measurement_health() -> None:
    config = _config(direct=_direct(5.0, 5.0, 5.0, age=MAX_AGE + 10.0))
    snapshot = build_capability_snapshot(
        config=config, requests=[], charger_has_charge_control={}, charger_has_current_control={},
        membership_conflict_charger_ids=[],
    )
    assert snapshot.site_measurement.health == "stale"


def test_invalid_site_measurement_health() -> None:
    invalid = DirectPhaseMeasurement(
        l1=PhaseValue(None, HEALTHY_AGE, problem="invalid"),
        l2=PhaseValue(5.0, HEALTHY_AGE),
        l3=PhaseValue(5.0, HEALTHY_AGE),
    )
    config = _config(direct=invalid)
    snapshot = build_capability_snapshot(
        config=config, requests=[], charger_has_charge_control={}, charger_has_current_control={},
        membership_conflict_charger_ids=[],
    )
    assert snapshot.site_measurement.health == "invalid"


def test_a_charger_reading_above_the_site_total_is_not_an_invalid_site_measurement() -> None:
    config = _config(main_fuse_a=25.0, direct=_direct(15.0, 15.0, 15.0))
    charger = _charger(measured_current_a=_direct(20.0, 20.0, 20.0))  # exceeds site total
    snapshot = build_capability_snapshot(
        config=config, requests=[charger], charger_has_charge_control={"charger_a": True},
        charger_has_current_control={"charger_a": False}, membership_conflict_charger_ids=[],
    )
    assert snapshot.site_measurement.health == "healthy"


def test_charger_measurement_not_configured_is_normal_not_an_error() -> None:
    config = _config(direct=_direct(5.0, 5.0, 5.0))
    snapshot = build_capability_snapshot(
        config=config, requests=[_charger(measured_current_a=None)],
        charger_has_charge_control={"charger_a": True}, charger_has_current_control={"charger_a": False},
        membership_conflict_charger_ids=[],
    )
    assert snapshot.charger_measurement["charger_a"].configured is False
    assert snapshot.charger_measurement["charger_a"].health == "not_configured"


def test_charger_measurement_healthy_when_configured_and_fresh() -> None:
    config = _config(direct=_direct(15.0, 15.0, 15.0))
    charger = _charger(measured_current_a=_direct(5.0, 5.0, 5.0))
    snapshot = build_capability_snapshot(
        config=config, requests=[charger], charger_has_charge_control={"charger_a": True},
        charger_has_current_control={"charger_a": False}, membership_conflict_charger_ids=[],
    )
    assert snapshot.charger_measurement["charger_a"].health == "healthy"


def test_duplicate_membership_blocks_calculation_availability() -> None:
    config = _config(direct=_direct(5.0, 5.0, 5.0))
    snapshot = build_capability_snapshot(
        config=config, requests=[_charger()], charger_has_charge_control={"charger_a": True},
        charger_has_current_control={"charger_a": False},
        membership_conflict_charger_ids=["charger_a"],
    )
    assert snapshot.load_balancing.calculation_available is False
    assert "duplicate_membership" in snapshot.load_balancing.blocking_reasons


def test_active_charger_with_unknown_requested_current_is_flagged() -> None:
    config = _config(direct=_direct(5.0, 5.0, 5.0))
    charger = _charger(requested_current_a=None)
    snapshot = build_capability_snapshot(
        config=config, requests=[charger], charger_has_charge_control={"charger_a": True},
        charger_has_current_control={"charger_a": False}, membership_conflict_charger_ids=[],
    )
    assert snapshot.load_balancing.has_unknown_active_request is True
    # The calculation overall is still usable -- an unknown request on one
    # charger doesn't invalidate the whole site's measurement setup.
    assert snapshot.load_balancing.calculation_available is True


def test_active_load_balancing_is_never_available() -> None:
    config = _config(direct=_direct(5.0, 5.0, 5.0))
    snapshot = build_capability_snapshot(
        config=config, requests=[], charger_has_charge_control={}, charger_has_current_control={},
        membership_conflict_charger_ids=[],
    )
    assert snapshot.load_balancing.active_available is False


def test_current_control_available_reflects_any_charger_with_a_limit() -> None:
    config = _config(direct=_direct(5.0, 5.0, 5.0))
    snapshot = build_capability_snapshot(
        config=config, requests=[_charger("charger_a"), _charger("charger_b")],
        charger_has_charge_control={"charger_a": True, "charger_b": True},
        charger_has_current_control={"charger_a": False, "charger_b": True},
        membership_conflict_charger_ids=[],
    )
    assert snapshot.current_control_available is True


# --- The active-control pair ------------------------------------------------


def _snapshot(**overrides):
    """One builder for the active-control tests, with everything that makes a
    site actively controllable already in place, so each test can spoil exactly
    one thing.

    `config` is `enabled=False` (site disabled) on purpose: the two pairs are
    independent questions, and the active pair must never be read off the calculation
    mode's.
    """
    kwargs = {
        "config": _config(enabled=False, direct=_direct(5.0, 5.0, 5.0)),
        "requests": [_charger()],
        "charger_has_charge_control": {"charger_a": True},
        "charger_has_current_control": {"charger_a": True},
        "membership_conflict_charger_ids": [],
        "charger_is_commandable": {"charger_a": True},
        "active_control_enabled": False,
    }
    kwargs.update(overrides)
    return build_capability_snapshot(**kwargs)


def test_active_available_with_a_closed_gate_healthy_measurement_and_a_commandable_charger() -> None:
    snapshot = _snapshot()

    assert snapshot.load_balancing.active_available is True
    # ...which is a different question from whether it is turned on.
    assert snapshot.load_balancing.active_enabled is False
    # And the calculation pair neither moves nor is consulted by any of this.
    assert snapshot.load_balancing.calculation_available is True
    assert snapshot.load_balancing.calculation_enabled is False


def test_active_unavailable_when_the_measurement_is_not_healthy() -> None:
    snapshot = _snapshot(
        config=_config(enabled=False, direct=_direct(5.0, 5.0, 5.0, age=MAX_AGE + 60.0))
    )

    assert snapshot.site_measurement.health == "stale"
    assert snapshot.load_balancing.active_available is False


def test_active_unavailable_on_a_membership_conflict() -> None:
    snapshot = _snapshot(membership_conflict_charger_ids=["charger_a"])

    assert snapshot.load_balancing.active_available is False


def test_active_unavailable_without_a_commandable_charger() -> None:
    """A charger that has a current-limit entity but never opted into the write
    path cannot be written to, so the site is not actively controllable -- the
    narrower notion, deliberately not "has some control entity"."""
    snapshot = _snapshot(
        charger_is_commandable={"charger_a": False},
        charger_has_current_control={"charger_a": True},
    )

    assert snapshot.current_control_available is True  # the coarser fact
    assert snapshot.load_balancing.active_available is False


def test_active_unavailable_when_commandability_was_not_reported() -> None:
    """`None` is "not reported", and an unreported capability is not announced
    as available."""
    snapshot = _snapshot(charger_is_commandable=None)

    assert snapshot.load_balancing.active_available is False


def test_active_unavailable_when_the_compile_time_gate_is_open(monkeypatch) -> None:
    monkeypatch.setattr(capability, "ACTIVE_CONTROL_READY", False)

    assert _snapshot().load_balancing.active_available is False


def test_active_enabled_follows_the_site_option_both_ways() -> None:
    assert _snapshot(active_control_enabled=True).load_balancing.active_enabled is True
    assert _snapshot(active_control_enabled=False).load_balancing.active_enabled is False


def test_the_active_pair_reports_both_combinations_independently() -> None:
    """"Available but not enabled" and "enabled but not available" must both be
    representable: they are the two situations one field could not tell
    apart."""
    available_but_not_enabled = _snapshot(active_control_enabled=False)
    assert available_but_not_enabled.load_balancing.active_available is True
    assert available_but_not_enabled.load_balancing.active_enabled is False

    enabled_but_not_available = _snapshot(
        active_control_enabled=True,
        config=_config(enabled=False, direct=_direct(5.0, 5.0, 5.0, age=MAX_AGE + 60.0)),
    )
    assert enabled_but_not_available.load_balancing.active_available is False
    assert enabled_but_not_available.load_balancing.active_enabled is True
