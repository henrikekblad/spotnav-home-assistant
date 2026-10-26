"""A single-phase charger with no phase chosen ("Automatic"): followed to the phase its own measured current
shows it drawing on, else reckoned on the phase with the least headroom.

The field case: a Tibber Pulse site (direct, per phase) with two Easee chargers set to one phase and no phase
chosen, behind an Easee Equalizer. The history shows each charger's one-phase charge on L3 one session, L1 the
next, L2 overnight: the Equalizer moves it. Until now such a charger got no load-balancing allocation at all
(`single_phase_charger_wiring_unknown`), and a fixed phase would be wrong half the time.
"""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.core import HomeAssistant

from custom_components.spotnav.const import CONF_MEASURED_CURRENT_SOURCE, MEASUREMENT_MODE_DERIVED
from custom_components.spotnav.diagnostics import entry_diagnostics
from custom_components.spotnav.site.auto_phase import (
    AMBIGUOUS,
    AUTO_PHASE_SETTLE_S,
    AutoPhaseTracker,
    drawn_phase,
)
from custom_components.spotnav.site.site_capacity import (
    calculate_site_capacity,
    ChargerRequest,
    DirectPhaseMeasurement,
    PhaseValue,
    resolve_worst_case_phases,
    SiteCalculationConfig,
)

from .helpers import make_site_entry, set_current_sensor, setup_two_chargers
from .world import controller_of, separate_entities_source, set_derived_site_entities

MAX_AGE = 120.0


def _amps(l1: float | None, l2: float | None, l3: float | None, age: float = 2.0) -> DirectPhaseMeasurement:
    def value(amps: float | None) -> PhaseValue:
        return PhaseValue(None, None, problem="missing") if amps is None else PhaseValue(amps, age)

    return DirectPhaseMeasurement(l1=value(l1), l2=value(l2), l3=value(l3))


def _site(l1: float, l2: float, l3: float) -> SiteCalculationConfig:
    """A direct site on a 25 A fuse with 1 A of margin: the headroom is 24 A less each phase's current."""
    return SiteCalculationConfig(
        enabled=True,
        main_fuse_a=25.0,
        safety_margin_a=1.0,
        measurement_mode="direct_phase_current",
        max_age_s=MAX_AGE,
        direct=_amps(l1, l2, l3),
    )


def _request(charger_entry_id: str = "easee", *, phase: str | None = None, basis: str = "chosen", **kwargs: Any):
    return ChargerRequest(
        charger_entry_id=charger_entry_id,
        requested_current_a=kwargs.pop("requested_current_a", 16.0),
        phases=kwargs.pop("phases", 1),
        phase=phase,  # type: ignore[arg-type]
        phase_basis=basis,  # type: ignore[arg-type]
        **kwargs,
    )


# ------------------------------------------------------------------------- which phase it draws on


def test_one_phase_drawing_alone_is_the_phase() -> None:
    assert drawn_phase(_amps(0.1, 0.0, 13.0), MAX_AGE, {"L1": 3.0, "L2": 4.0, "L3": 16.0}) == "L3"


def test_nothing_drawing_is_no_phase() -> None:
    assert drawn_phase(_amps(0.2, 0.0, 0.4), MAX_AGE, {"L1": 3.0, "L2": 4.0, "L3": 5.0}) is None


def test_two_phases_drawing_is_ambiguous() -> None:
    assert drawn_phase(_amps(0.0, 6.0, 7.0), MAX_AGE, {"L1": 3.0, "L2": 9.0, "L3": 10.0}) == AMBIGUOUS


def test_a_stale_reading_is_no_phase() -> None:
    assert drawn_phase(_amps(0.0, 0.0, 13.0, age=MAX_AGE + 1), MAX_AGE, {"L1": 3.0, "L2": 4.0, "L3": 16.0}) is None


def test_a_source_that_reads_one_phase_only_cannot_tell_the_phases_apart() -> None:
    assert drawn_phase(_amps(13.0, None, None), MAX_AGE, {"L1": 16.0, "L2": 4.0, "L3": 5.0}) is None


def test_a_draw_the_site_does_not_bear_out_is_no_phase() -> None:
    # A one-phase charger whose single terminal is read as L1 while it is wired to L3: L1 of the site shows
    # nothing like 13 A.
    assert drawn_phase(_amps(13.0, 0.0, 0.0), MAX_AGE, {"L1": 2.0, "L2": 4.0, "L3": 16.0}) is None
    assert drawn_phase(_amps(13.0, 0.0, 0.0), MAX_AGE, None) is None


# ------------------------------------------------------------------------- following it, debounced


def test_a_phase_counts_once_it_has_held_for_the_settle_time() -> None:
    tracker = AutoPhaseTracker()

    assert tracker.observe("L3", 0.0) is None
    assert tracker.observe("L3", AUTO_PHASE_SETTLE_S - 0.1) is None
    assert tracker.observe("L3", AUTO_PHASE_SETTLE_S) == "L3"


def test_it_follows_the_phase_when_it_moves() -> None:
    tracker = AutoPhaseTracker()
    tracker.observe("L3", 0.0)
    assert tracker.observe("L3", 10.0) == "L3"

    # The Equalizer moved it: neither phase is trusted until the new one has held.
    assert tracker.observe("L2", 20.0) is None
    assert tracker.observe("L2", 25.0) is None
    assert tracker.observe("L2", 30.0) == "L2"


def test_a_brief_two_phase_reading_does_not_flip_the_phase() -> None:
    tracker = AutoPhaseTracker()
    tracker.observe("L3", 0.0)
    assert tracker.observe("L3", 10.0) == "L3"

    assert tracker.observe(AMBIGUOUS, 11.0) == "L3"
    assert tracker.observe(AMBIGUOUS, 15.0) == "L3"
    assert tracker.observe("L3", 16.0) == "L3"


def test_a_lasting_two_phase_reading_falls_back_to_the_worst_phase() -> None:
    tracker = AutoPhaseTracker()
    tracker.observe("L3", 0.0)
    tracker.observe("L3", 10.0)

    tracker.observe(AMBIGUOUS, 11.0)
    assert tracker.observe(AMBIGUOUS, 11.0 + AUTO_PHASE_SETTLE_S) is None


def test_a_stale_or_idle_reading_is_the_worst_phase_at_once() -> None:
    tracker = AutoPhaseTracker()
    tracker.observe("L3", 0.0)
    assert tracker.observe("L3", 10.0) == "L3"

    assert tracker.observe(None, 11.0) is None
    # And a new start settles again.
    assert tracker.observe("L3", 12.0) is None


def test_observing_one_reading_many_times_changes_nothing() -> None:
    tracker = AutoPhaseTracker()
    for _ in range(5):
        assert tracker.observe("L1", 0.0) is None
    assert tracker.observe("L1", AUTO_PHASE_SETTLE_S) == "L1"


# ------------------------------------------------------------------------- what load balancing allocates


def test_replay_a_charge_on_l3_then_on_l2_uses_that_phase_only() -> None:
    # L1 is heavily loaded (6 A of headroom), the others not: the charge on L3 is not held to L1's headroom.
    site = _site(18.0, 4.0, 3.0 + 13.0)
    on_l3 = _request(phase="L3", basis="measured", measured_current_a=_amps(0.0, 0.0, 13.0))

    allocation = calculate_site_capacity(site, [on_l3]).allocations[0]

    assert allocation.proposed_current_a == 8.0  # 24 - 16 on L3
    assert allocation.limiting_phase == "L3"

    # The next session the Equalizer puts it on L2.
    site = _site(18.0, 4.0 + 13.0, 3.0)
    on_l2 = _request(phase="L2", basis="measured", measured_current_a=_amps(0.0, 13.0, 0.0))

    allocation = calculate_site_capacity(site, [on_l2]).allocations[0]

    assert allocation.proposed_current_a == 7.0  # 24 - 17 on L2
    assert allocation.limiting_phase == "L2"


def test_idle_before_a_start_the_worst_phase_limits_it() -> None:
    site = _site(18.0, 4.0, 3.0)
    idle = _request(basis="worst_case", measured_current_a=_amps(0.0, 0.0, 0.0))

    allocation = calculate_site_capacity(site, [idle]).allocations[0]

    assert allocation.proposed_current_a == 6.0
    assert allocation.limiting_phase == "L1"


def test_reckoned_on_the_worst_phase_its_draw_is_the_largest_it_reads() -> None:
    # Drawing on L3 but not yet followed there (settling): reckoned on L1, the worst, drawing its 13 A there.
    request = _request(basis="worst_case", measured_current_a=_amps(0.1, 0.0, 13.0))

    (resolved,) = resolve_worst_case_phases([request], {"L1": 2.0, "L2": 20.0, "L3": 8.0})

    assert resolved.phase == "L1" and resolved.phase_basis == "worst_case"
    assert resolved.measured_current_a is not None
    assert resolved.measured_current_a.get("L1").value == 13.0


def test_an_explicitly_chosen_phase_is_honoured() -> None:
    site = _site(18.0, 4.0, 3.0)
    on_l2 = _request(phase="L2", measured_current_a=_amps(0.0, 0.0, 0.0))

    allocation = calculate_site_capacity(site, [on_l2]).allocations[0]

    assert allocation.proposed_current_a == 16.0


def test_a_three_phase_charger_is_unchanged() -> None:
    site = _site(18.0, 4.0, 3.0)
    three = _request(phases=3)

    allocation = calculate_site_capacity(site, [three]).allocations[0]

    assert allocation.proposed_current_a == 6.0
    assert allocation.limiting_phase == "L1"


def test_two_automatic_chargers_drawing_on_l3_share_its_headroom() -> None:
    # 2 A of house load on L3 plus 2 x 8 A of car: 6 A of headroom left on L3, the other phases are empty.
    site = _site(1.0, 1.0, 18.0)
    first = _request("easee_1", phase="L3", basis="measured", measured_current_a=_amps(0.0, 0.0, 8.0), order=0)
    second = _request("easee_2", phase="L3", basis="measured", measured_current_a=_amps(0.0, 0.0, 8.0), order=1)

    first_allocation, second_allocation = calculate_site_capacity(site, [first, second]).allocations

    assert first_allocation.proposed_current_a == 6.0
    assert first_allocation.limiting_phase == "L3"
    assert second_allocation.proposed_current_a == 0.0
    assert second_allocation.state == "below_minimum_current"


# ------------------------------------------------------------------------- the site's controller


def _wiring(prefix: str) -> dict[str, Any]:
    return {
        "phases": 1,
        "phase": None,
        "min_current_a": 6.0,
        CONF_MEASURED_CURRENT_SOURCE: separate_entities_source(
            f"sensor.{prefix}_l1", f"sensor.{prefix}_l2", f"sensor.{prefix}_l3"
        ),
    }


def _set(hass: HomeAssistant, prefix: str, l1: float, l2: float, l3: float) -> None:
    for phase, amps in (("l1", l1), ("l2", l2), ("l3", l3)):
        set_current_sensor(hass, f"sensor.{prefix}_{phase}", amps)


@pytest.mark.usefixtures("offline_relay")
async def test_the_site_follows_two_automatic_chargers_to_l3_and_shares_it(hass: HomeAssistant) -> None:
    entry_a, entry_b, _turn_on, _turn_off = await setup_two_chargers(hass)
    _set(hass, "ca", 0.0, 0.0, 0.0)
    _set(hass, "cb", 0.0, 0.0, 0.0)
    _set(hass, "auto_site", 10.0, 4.0, 2.0)
    site_entry = make_site_entry(
        hass,
        entry_id="auto_site",
        charger_entry_ids=[entry_a.entry_id, entry_b.entry_id],
        phase_wiring={entry_a.entry_id: _wiring("ca"), entry_b.entry_id: _wiring("cb")},
    )
    assert await hass.config_entries.async_setup(site_entry.entry_id)
    await hass.async_block_till_done()
    site = controller_of(hass, site_entry.entry_id)
    clock = {"now": 1000.0}
    site._yield_now = lambda: clock["now"]  # noqa: SLF001
    site._recompute(schedule_apply=False)  # noqa: SLF001

    # Idle: both on the worst phase, L1.
    snapshot = site.charger_phase_snapshot()
    assert snapshot[entry_a.entry_id] == {"phases": 1, "phase": "L1", "phase_basis": "worst_case", "automatic": True}
    assert entry_diagnostics(hass, site_entry)["charger_phase"] == snapshot

    # Both start on L3 (8 A each over 2 A of house load); after the settle time they are followed there.
    for entry, switch in ((entry_a, "switch.charger_a"), (entry_b, "switch.charger_b")):
        assert await controller_of(hass, entry.entry_id).async_start(amps=16)
        hass.states.async_set(switch, "on")
    _set(hass, "ca", 0.0, 0.0, 8.0)
    _set(hass, "cb", 0.0, 0.0, 8.0)
    _set(hass, "auto_site", 10.0, 4.0, 18.0)
    await hass.async_block_till_done()
    site._recompute(schedule_apply=False)  # noqa: SLF001
    assert site.charger_phase_snapshot()[entry_a.entry_id]["phase_basis"] == "worst_case"

    clock["now"] += AUTO_PHASE_SETTLE_S
    site._recompute(schedule_apply=False)  # noqa: SLF001

    snapshot = site.charger_phase_snapshot()
    assert snapshot[entry_a.entry_id]["phase"] == "L3" and snapshot[entry_a.entry_id]["phase_basis"] == "measured"
    assert snapshot[entry_b.entry_id]["phase"] == "L3" and snapshot[entry_b.entry_id]["phase_basis"] == "measured"
    assert site.followed_phase(entry_a.entry_id) == "L3"
    # L3 has 24 - 18 = 6 A left for the two of them, L1 (14 A) does not limit them.
    allocations = {allocation.charger_entry_id: allocation for allocation in site.result.allocations}
    assert allocations[entry_a.entry_id].limiting_phase == "L3"
    proposed = [allocations[entry.entry_id].proposed_current_a for entry in (entry_a, entry_b)]
    assert sorted(proposed) == [0.0, 6.0]

    # A stale reading (the source unavailable): the worst phase again at once.
    hass.states.async_set("sensor.ca_l3", "unavailable")
    await hass.async_block_till_done()
    site._recompute(schedule_apply=False)  # noqa: SLF001
    assert site.charger_phase_snapshot()[entry_a.entry_id]["phase_basis"] == "worst_case"
    assert site.followed_phase(entry_a.entry_id) is None


@pytest.mark.usefixtures("offline_relay")
async def test_the_regulator_decides_for_an_automatic_charger(hass: HomeAssistant) -> None:
    # Was `charger_phase_wiring_unknown`, a hold for good: now reckoned on the worst phase, its limit there.
    entry_a, _entry_b, _turn_on, _turn_off = await setup_two_chargers(hass)
    _set(hass, "ca", 0.0, 0.0, 0.0)
    derived = set_derived_site_entities(hass, "auto_derived", 0.0, per_phase_watts={"L2": 15.0 * 230.0})
    site_entry = make_site_entry(
        hass,
        entry_id="auto_derived",
        main_fuse_a=20.0,
        safety_margin_a=0.0,
        charger_entry_ids=[entry_a.entry_id],
        phase_wiring={entry_a.entry_id: _wiring("ca")},
        measurement_mode=MEASUREMENT_MODE_DERIVED,
        derived_entities=derived,
    )
    assert await hass.config_entries.async_setup(site_entry.entry_id)
    await hass.async_block_till_done()
    assert await controller_of(hass, entry_a.entry_id).async_start(amps=16)
    hass.states.async_set("switch.charger_a", "on")
    await hass.async_block_till_done()
    site = controller_of(hass, site_entry.entry_id)
    site._recompute(schedule_apply=False)  # noqa: SLF001

    decision = site.regulator_decisions[entry_a.entry_id]

    assert decision.reason != "charger_phase_wiring_unknown"
    assert set(decision.basis) == {"L2"}

