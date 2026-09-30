"""Load-balancing regulator (`site/regulator.py`) wired through `SiteCapacityController`; it must never call a charger-control service."""

from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed, async_mock_service

from custom_components.spotnav.const import (
    CONF_MEASURED_CURRENT_SOURCE,
    MEASUREMENT_MODE_DERIVED,
    SITE_RECOMPUTE_INTERVAL_S,
)

from .helpers import make_entry, make_site_entry, set_current_sensor
from .world import measured_site_world, set_derived_site_entities
from .world import separate_entities_source
from .world import controller_of

MAIN_FUSE_A = 20.0


async def _tick(hass: HomeAssistant) -> None:
    """Advance past the periodic recompute interval and let it run."""
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=SITE_RECOMPUTE_INTERVAL_S + 1))
    await hass.async_block_till_done()


async def test_decision_is_computed_end_to_end_for_a_real_charger(hass: HomeAssistant) -> None:
    charger, charger_controller, entry, site_controller, derived_entities = await measured_site_world(
        hass, "regulator", start_amps=16, delivered_a=6, site_watts=2000.0, main_fuse_a=MAIN_FUSE_A
    )
    decision = site_controller.regulator_decisions[charger.entry_id]
    assert decision.reason == "increase_within_safe_uncredited_margin"
    assert decision.proposed_current_a is not None
    assert decision.proposed_current_a > 6.0


async def test_battery_export_near_the_fuse_refuses_a_real_decrease_end_to_end(
    hass: HomeAssistant,
) -> None:
    """Site nets to export near the fuse and the charger's request drops: the regulator must not propose the lower value."""
    hass.states.async_set("switch.export_charger", "off")
    async_mock_service(hass, "switch", "turn_on")
    charger = make_entry(
        hass, entry_id="charger_export", charge_control="switch.export_charger",
        current_limit=None, webhook_id="webhook-export", title="Export",
    )
    assert await hass.config_entries.async_setup(charger.entry_id)
    await hass.async_block_till_done()
    charger_controller = controller_of(hass, charger.entry_id)
    await charger_controller.async_start(amps=6)  # requesting a decrease from 10A
    hass.states.async_set("switch.export_charger", "on")

    for suffix in ("l1", "l2", "l3"):
        set_current_sensor(hass, f"sensor.export_charger_{suffix}", 10)

    # ~-4100 W/phase, a battery export magnitude of ~18 A/phase.
    derived_entities = set_derived_site_entities(hass, "export_site", -4100.0)
    entry = make_site_entry(
        hass,
        entry_id="site_export",
        main_fuse_a=MAIN_FUSE_A,
        safety_margin_a=1.0,
        charger_entry_ids=[charger.entry_id],
        phase_wiring={
            charger.entry_id: {
                "phases": 3,
                "phase": None,
                CONF_MEASURED_CURRENT_SOURCE: separate_entities_source(
                        "sensor.export_charger_l1", "sensor.export_charger_l2", "sensor.export_charger_l3"
                    ),
            }
        },
        measurement_mode=MEASUREMENT_MODE_DERIVED,
        derived_entities=derived_entities,
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    site_controller = controller_of(hass, entry.entry_id)
    decision = site_controller.regulator_decisions[charger.entry_id]
    assert decision.proposed_current_a == 10.0  # unchanged, NOT the requested 6.0
    assert decision.reason == "decrease_would_increase_net_current_kept_unchanged"


async def test_a_confirmed_decrease_requires_several_consistent_recomputes(
    hass: HomeAssistant,
) -> None:
    """Consecutive confirmation through real recompute cycles: a small in-margin reduction is only confirmed after enough consistent recomputes (`DEFAULT_MIN_CONSECUTIVE_CONFIRMATIONS`)."""
    charger, charger_controller, entry, site_controller, derived_entities = await measured_site_world(
        hass, "confirm", start_amps=9, delivered_a=10, site_watts=3000.0, main_fuse_a=MAIN_FUSE_A
    )
    first_decision = site_controller.regulator_decisions[charger.entry_id]
    assert first_decision.reason == "insufficient_consecutive_confirmation_kept_unchanged"
    assert first_decision.proposed_current_a == 10.0

    # Each new report re-confirms the same reading (an identical-value `async_set` bumps `last_reported` but not `last_updated`), followed by a recompute trigger so the controller observes it. More than the default minimum of 3; the exact count is covered by the pure-function tests.
    for _ in range(6):
        set_derived_site_entities(hass, "confirm_site", 3000.0)
        await _tick(hass)

    confirmed_decision = site_controller.regulator_decisions[charger.entry_id]
    assert confirmed_decision.reason == "decrease_confirmed_safe"
    assert confirmed_decision.proposed_current_a == 9.0


async def test_repeated_recomputes_without_a_new_sigenergy_report_never_confirm(
    hass: HomeAssistant,
) -> None:
    """Confirmations count only when a genuinely new report arrived, not because a recompute ran again."""
    charger, charger_controller, entry, site_controller, derived_entities = await measured_site_world(
        hass, "noreport", title="NoReport", start_amps=9, delivered_a=10, site_watts=3000.0, main_fuse_a=MAIN_FUSE_A
    )

    # Periodic recomputes with no measurement entity touched must never confirm.
    for _ in range(10):
        await _tick(hass)
        decision = site_controller.regulator_decisions[charger.entry_id]
        assert decision.reason == "insufficient_consecutive_confirmation_kept_unchanged"
        assert decision.proposed_current_a == 10.0

    # Also via a charger event that reuses the same data: still no new report.
    for _ in range(3):
        await charger_controller.async_start(amps=9)
        decision = site_controller.regulator_decisions[charger.entry_id]
        assert decision.reason == "insufficient_consecutive_confirmation_kept_unchanged"
        assert decision.proposed_current_a == 10.0


async def test_irregular_recompute_intervals_are_immune_to_the_relative_age_bug(
    hass: HomeAssistant, monkeypatch
) -> None:
    """"A new report arrived" is judged by absolute `last_reported`, not by a shrinking recompute-relative age.

    Recomputes fire at irregular offsets (periodic timer, state change, charger
    listener). A new report followed by an unusually long gap makes the relative age
    grow, so a relative comparison would miss it. `dt_util.utcnow` is patched only
    around the synchronous `_recompute()` calls, and `last_reported` is set with
    `hass.states.async_set(timestamp=...)`, as in
    `tests/test_site_capacity_measurement_wiring.py`.
    """
    charger, charger_controller, entry, site_controller, derived_entities = await measured_site_world(
        hass, "irregular", start_amps=9, delivered_a=10, site_watts=3000.0, main_fuse_a=MAIN_FUSE_A
    )

    real_now = dt_util.utcnow()

    def _report_all_phases(offset_s: float) -> None:
        """Give every phase's P/Q/V entity a fresh report at `real_now + offset_s`, keeping the inputs synchronized."""
        ts = (real_now + timedelta(seconds=offset_s)).timestamp()
        for phase in ("l1", "l2", "l3"):
            hass.states.async_set(
                f"sensor.irregular_site_power_{phase}", "3000",
                {"unit_of_measurement": "W"}, timestamp=ts,
            )
            hass.states.async_set(
                f"sensor.irregular_site_reactive_{phase}", "0",
                {"unit_of_measurement": "var"}, timestamp=ts,
            )
            hass.states.async_set(
                f"sensor.irregular_site_voltage_{phase}", "230",
                {"unit_of_measurement": "V"}, timestamp=ts,
            )

    fake_now = {"value": real_now}
    monkeypatch.setattr(
        "custom_components.spotnav.site.site_capacity_controller.dt_util.utcnow",
        lambda: fake_now["value"],
    )

    # T=0s: re-ground every phase's report at a known instant.
    _report_all_phases(0.0)
    fake_now["value"] = real_now
    site_controller._recompute()
    history_after_t0 = len(site_controller._direction_history["L1"])

    # T=2s: no new report, so a bare recompute must not count as new.
    fake_now["value"] = real_now + timedelta(seconds=2)
    site_controller._recompute()
    assert len(site_controller._direction_history["L1"]) == history_after_t0

    # A genuinely new report lands right away (~T=2s)...
    _report_all_phases(2.0)
    # ...but the next recompute runs much later (T=102s), an irregular long gap.
    fake_now["value"] = real_now + timedelta(seconds=102)
    site_controller._recompute()

    # The absolute-timestamp comparison still detects the new report regardless of the gap.
    assert len(site_controller._direction_history["L1"]) == history_after_t0 + 1


async def test_a_fresh_active_power_report_is_not_masked_by_a_stale_reactive_power_or_voltage_report(
    hass: HomeAssistant,
) -> None:
    """A fresh active-power report is not masked by a stale reactive-power or voltage report.

    `phase_report_age_s` is the `max()` of the P, Q and V ages, but confirmation only
    consumes signed active power, so the P entity's own `last_reported` is tracked.
    Masking could only make confirmation more conservative, never unsafe.
    """
    charger, charger_controller, entry, site_controller, derived_entities = await measured_site_world(
        hass, "indep", title="Independent", start_amps=9, delivered_a=10, site_watts=3000.0, main_fuse_a=MAIN_FUSE_A
    )
    history_before = {
        phase: len(site_controller._direction_history[phase]) for phase in ("L1", "L2", "L3")
    }

    # Only L1's active-power entity gets a fresh report; its Q/V siblings and L2/L3 are untouched.
    hass.states.async_set(
        "sensor.indep_site_power_l1", "3000", {"unit_of_measurement": "W"}
    )
    await hass.async_block_till_done()
    site_controller._recompute()

    # L1: the fresh P report is detected and confirmed although its Q/V siblings were not touched.
    assert len(site_controller._direction_history["L1"]) == history_before["L1"] + 1
    # L2/L3: nothing changed, so no new entry.
    assert len(site_controller._direction_history["L2"]) == history_before["L2"]
    assert len(site_controller._direction_history["L3"]) == history_before["L3"]


async def test_regulator_never_calls_a_charger_control_service(
    hass: HomeAssistant,
) -> None:
    """However decisions change across recomputes, no `switch`/`number` service is called because of them."""
    hass.states.async_set("switch.safety_charger", "off")
    turn_on_calls = async_mock_service(hass, "switch", "turn_on")
    turn_off_calls = async_mock_service(hass, "switch", "turn_off")
    charger = make_entry(
        hass, entry_id="charger_safety", charge_control="switch.safety_charger",
        current_limit=None, webhook_id="webhook-safety", title="Safety",
    )
    assert await hass.config_entries.async_setup(charger.entry_id)
    await hass.async_block_till_done()
    charger_controller = controller_of(hass, charger.entry_id)
    await charger_controller.async_start(amps=6)
    hass.states.async_set("switch.safety_charger", "on")

    for suffix in ("l1", "l2", "l3"):
        set_current_sensor(hass, f"sensor.safety_charger_{suffix}", 10)

    derived_entities = set_derived_site_entities(hass, "safety_site", -4100.0)
    entry = make_site_entry(
        hass,
        entry_id="site_safety",
        main_fuse_a=MAIN_FUSE_A,
        safety_margin_a=1.0,
        charger_entry_ids=[charger.entry_id],
        phase_wiring={
            charger.entry_id: {
                "phases": 3,
                "phase": None,
                CONF_MEASURED_CURRENT_SOURCE: separate_entities_source(
                        "sensor.safety_charger_l1", "sensor.safety_charger_l2", "sensor.safety_charger_l3"
                    ),
            }
        },
        measurement_mode=MEASUREMENT_MODE_DERIVED,
        derived_entities=derived_entities,
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    # `async_start` legitimately called `switch.turn_on` itself; no additional call may come from the regulator.
    calls_before = (len(turn_on_calls), len(turn_off_calls))

    for _ in range(5):
        await _tick(hass)

    assert (len(turn_on_calls), len(turn_off_calls)) == calls_before


async def test_two_chargers_sharing_a_phase_are_allocated_sequentially_end_to_end(
    hass: HomeAssistant,
) -> None:
    """Two chargers sharing all three phases, both asking to increase: the second (ascending entry id) sees the already-reduced headroom."""
    hass.states.async_set("switch.multi_a", "off")
    hass.states.async_set("switch.multi_b", "off")
    async_mock_service(hass, "switch", "turn_on")
    charger_a = make_entry(
        hass, entry_id="charger_multi_a", charge_control="switch.multi_a",
        current_limit=None, webhook_id="webhook-multi-a", title="Multi A",
    )
    assert await hass.config_entries.async_setup(charger_a.entry_id)
    await hass.async_block_till_done()
    await controller_of(hass, charger_a.entry_id).async_start(amps=16)

    # Register charger B only after A finished setup: the first entry for a domain bootstraps the component (see helpers.py `setup_two_chargers`).
    charger_b = make_entry(
        hass, entry_id="charger_multi_b", charge_control="switch.multi_b",
        current_limit=None, webhook_id="webhook-multi-b", title="Multi B",
    )
    assert await hass.config_entries.async_setup(charger_b.entry_id)
    await hass.async_block_till_done()
    await controller_of(hass, charger_b.entry_id).async_start(amps=16)
    hass.states.async_set("switch.multi_a", "on")
    hass.states.async_set("switch.multi_b", "on")

    # Both chargers off (0 A draw), each with its own measured-current source.
    for suffix in ("l1", "l2", "l3"):
        set_current_sensor(hass, f"sensor.multi_a_{suffix}", 0)
        set_current_sensor(hass, f"sensor.multi_b_{suffix}", 0)

    # Site total 8 A/phase gives measured_margin_a = 19 - 8 = 11 A/phase.
    derived_entities = set_derived_site_entities(hass, "multi_site", 8.0 * 230.0)
    entry = make_site_entry(
        hass,
        entry_id="site_multi",
        main_fuse_a=MAIN_FUSE_A,
        safety_margin_a=1.0,
        charger_entry_ids=[charger_a.entry_id, charger_b.entry_id],
        phase_wiring={
            charger_a.entry_id: {
                "phases": 3,
                "phase": None,
                CONF_MEASURED_CURRENT_SOURCE: separate_entities_source(
                        "sensor.multi_a_l1", "sensor.multi_a_l2", "sensor.multi_a_l3"
                    ),
            },
            charger_b.entry_id: {
                "phases": 3,
                "phase": None,
                CONF_MEASURED_CURRENT_SOURCE: separate_entities_source(
                        "sensor.multi_b_l1", "sensor.multi_b_l2", "sensor.multi_b_l3"
                    ),
            },
        },
        measurement_mode=MEASUREMENT_MODE_DERIVED,
        derived_entities=derived_entities,
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    site_controller = controller_of(hass, entry.entry_id)
    decision_a = site_controller.regulator_decisions[charger_a.entry_id]
    decision_b = site_controller.regulator_decisions[charger_b.entry_id]

    # The first charger takes the full 11 A; the second must not also be offered 11 A.
    assert decision_a.proposed_current_a == 11.0
    assert decision_b.proposed_current_a != 11.0
    assert decision_b.proposed_current_a == 0.0


async def test_a_membership_conflict_does_not_change_regulator_decisions_but_stays_gated(
    hass: HomeAssistant,
) -> None:
    """Two sites listing the same charger compute independent regulator decision but both report `membership_conflicts`.

    Nothing coordinates the two, which is why acting on either while conflicted would be unsafe.
    """
    hass.states.async_set("switch.conflict_charger", "off")
    async_mock_service(hass, "switch", "turn_on")
    charger = make_entry(
        hass, entry_id="charger_conflict_shared", charge_control="switch.conflict_charger",
        current_limit=None, webhook_id="webhook-conflict", title="Conflict",
    )
    assert await hass.config_entries.async_setup(charger.entry_id)
    await hass.async_block_till_done()
    await controller_of(hass, charger.entry_id).async_start(amps=16)
    hass.states.async_set("switch.conflict_charger", "on")

    for suffix in ("l1", "l2", "l3"):
        set_current_sensor(hass, f"sensor.conflict_charger_{suffix}", 0)

    def _wiring() -> dict:
        return {
            charger.entry_id: {
                "phases": 3,
                "phase": None,
                CONF_MEASURED_CURRENT_SOURCE: separate_entities_source(
                        "sensor.conflict_charger_l1", "sensor.conflict_charger_l2", "sensor.conflict_charger_l3"
                    ),
            }
        }

    # Two sites claiming the same charger, each with its own sensors and different site total, so the decisions can differ.
    site_a = make_site_entry(
        hass, entry_id="site_conflict_a", main_fuse_a=MAIN_FUSE_A, safety_margin_a=1.0,
        charger_entry_ids=[charger.entry_id], phase_wiring=_wiring(),
        measurement_mode=MEASUREMENT_MODE_DERIVED,
        derived_entities=set_derived_site_entities(hass, "conflict_site_a", 8.0 * 230.0),
    )
    assert await hass.config_entries.async_setup(site_a.entry_id)
    await hass.async_block_till_done()

    site_b = make_site_entry(
        hass, entry_id="site_conflict_b", main_fuse_a=MAIN_FUSE_A, safety_margin_a=1.0,
        charger_entry_ids=[charger.entry_id], phase_wiring=_wiring(),
        measurement_mode=MEASUREMENT_MODE_DERIVED,
        derived_entities=set_derived_site_entities(hass, "conflict_site_b", 14.0 * 230.0),
    )
    assert await hass.config_entries.async_setup(site_b.entry_id)
    await hass.async_block_till_done()

    controller_a = controller_of(hass, site_a.entry_id)
    controller_b = controller_of(hass, site_b.entry_id)

    # Both still run and both report the conflict.
    assert controller_a.result.state == "observing"
    assert controller_b.result.state == "observing"
    assert len(controller_a.membership_conflicts) == 1
    assert controller_a.membership_conflicts[0].conflicting_site_entry_id == site_b.entry_id
    assert len(controller_b.membership_conflicts) == 1
    assert controller_b.membership_conflicts[0].conflicting_site_entry_id == site_a.entry_id

    # Each computes its own decision: site A sees 11 A of headroom; site B, with a heavier meter, sees 5 A (below the 6 A minimum) and pauses. No service call is made from either.
    decision_a = controller_a.regulator_decisions[charger.entry_id]
    decision_b = controller_b.regulator_decisions[charger.entry_id]
    assert decision_a.proposed_current_a == 11.0  # 19 - 8
    assert decision_b.proposed_current_a == 0.0  # 19 - 14 = 5A, below the 6A minimum -> pause
    assert decision_a.proposed_current_a != decision_b.proposed_current_a


async def test_log_line_carries_every_phase_basis_not_just_the_limiting_phase(
    hass: HomeAssistant, caplog
) -> None:
    """`_log_regulator_decisions` logs every phase the charger uses, not only the limiting one.

    A 3-phase charger with distinct power per phase: all three signed-power values must appear in the log.
    """
    hass.states.async_set("switch.allphase_charger", "off")
    async_mock_service(hass, "switch", "turn_on")
    charger = make_entry(
        hass, entry_id="charger_allphase", charge_control="switch.allphase_charger",
        current_limit=None, webhook_id="webhook-allphase", title="AllPhase",
    )
    assert await hass.config_entries.async_setup(charger.entry_id)
    await hass.async_block_till_done()
    await controller_of(hass, charger.entry_id).async_start(amps=16)
    hass.states.async_set("switch.allphase_charger", "on")

    for suffix in ("l1", "l2", "l3"):
        set_current_sensor(hass, f"sensor.allphase_charger_{suffix}", 6)

    # Distinct per-phase active power (all net import), applied after setup so the decision changes and the "log on change" line fires.
    distinct = {"L1": 2100.0, "L2": 2400.0, "L3": 2700.0}
    derived_entities = set_derived_site_entities(hass, "allphase_site", 3000.0)
    entry = make_site_entry(
        hass,
        entry_id="site_allphase",
        main_fuse_a=MAIN_FUSE_A,
        safety_margin_a=1.0,
        charger_entry_ids=[charger.entry_id],
        phase_wiring={
            charger.entry_id: {
                "phases": 3,
                "phase": None,
                CONF_MEASURED_CURRENT_SOURCE: separate_entities_source(
                        "sensor.allphase_charger_l1", "sensor.allphase_charger_l2", "sensor.allphase_charger_l3"
                    ),
            }
        },
        measurement_mode=MEASUREMENT_MODE_DERIVED,
        derived_entities=derived_entities,
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    # Change the per-phase values so the recomputed decision differs.
    caplog.set_level(logging.DEBUG, logger="custom_components.spotnav.site.site_capacity_controller")
    set_derived_site_entities(hass, "allphase_site", 0.0, per_phase_watts=distinct)
    controller_of(hass, entry.entry_id)._recompute()
    await hass.async_block_till_done()

    log = caplog.text
    # Every phase's signed power must be present, not only L1's.
    for phase, watts in distinct.items():
        assert str(watts) in log, f"{phase}'s signed power {watts} missing from log:\n{log}"
