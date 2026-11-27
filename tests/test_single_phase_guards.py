"""The guards around a single-phase site: no detection turns a three-phase site single-phase by itself, a site set
up as single-phase whose charger draws on several phases stops regulating and says so, and a charger's wiring step on
a single-phase site is fixed to one phase on L1."""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from custom_components.spotnav import texts
from custom_components.spotnav.const import (
    CONF_DERIVED_ENTITIES,
    CONF_DIRECT_ENTITIES,
    CONF_MEASUREMENT_MODE,
    DOMAIN,
    MEASUREMENT_MODE_DERIVED,
    MEASUREMENT_MODE_DIRECT,
    SINGLE_PHASE_SITE_CONFLICT_ISSUE,
)
from custom_components.spotnav.core.electric import site_phase_count
from custom_components.spotnav.flows.charger_wiring import ChargerWiringSteps
from custom_components.spotnav.flows.site_confirm import candidate_label, site_confirm_summary, site_join_summary
from custom_components.spotnav.flows.site_form import (
    PendingSiteDetails,
    charger_wiring_schema,
    parse_charger_wiring,
    pending_single_phase,
)
from custom_components.spotnav.site import site_detection as detection_module
from custom_components.spotnav.site.measurement_problem import UNHEALTHY_STATES
from custom_components.spotnav.site.regulator import measurement_lost
from custom_components.spotnav.site.site_capacity import PHASES, REASON_PHASE_CONFLICT, SINGLE_PHASE
from custom_components.spotnav.site.site_detection import (
    PHASE_CONFLICT_HOLD_S,
    Detection,
    MeterCandidate,
    apply_meter_candidate,
    is_single_phase_candidate,
    single_phase_candidate,
    site_detection,
)

from .helpers import make_site_entry, set_current_sensor
from .world import controller_of


def _meter(hass: HomeAssistant, integration: str, *, l2_v: str, l3_v: str, others: str = "0") -> MeterCandidate:
    for phase, volts, watts in (("l1", "231", "1200"), ("l2", l2_v, others), ("l3", l3_v, others)):
        hass.states.async_set(f"sensor.m_{phase}_power", watts, {"unit_of_measurement": "W"})
        hass.states.async_set(f"sensor.m_{phase}_voltage", volts, {"unit_of_measurement": "V"})
    return MeterCandidate(
        candidate_id=f"{integration}:meter",
        integration=integration,
        title="Meter",
        mode=MEASUREMENT_MODE_DERIVED,
        confidence="high",
        derived_entities={
            phase: {"power": f"sensor.m_{phase.lower()}_power", "voltage": f"sensor.m_{phase.lower()}_voltage"}
            for phase in PHASES
        },
    )


# ---- 1. Detection never turns a three-phase site single-phase by itself --------------------------


@pytest.mark.parametrize("integration", ["shelly", "victron_gx", "huawei_solar", "generic"])
async def test_a_meter_that_is_not_hass_victron_with_unread_l2_and_l3_stays_three_phase(
    hass: HomeAssistant, integration: str
) -> None:
    """L2 and L3 unknown or unavailable is a sensor that is down anywhere but on hass-victron."""
    for l2_v, l3_v, others in (("unknown", "unavailable", "0"), ("unavailable", "unknown", "unknown")):
        candidate = single_phase_candidate(hass, _meter(hass, integration, l2_v=l2_v, l3_v=l3_v, others=others))
        assert candidate.derived_entities is not None and set(candidate.derived_entities) == set(PHASES)
        assert not is_single_phase_candidate(candidate)
    # About 0 V is no supply for every integration.
    dead = single_phase_candidate(hass, _meter(hass, integration, l2_v="0", l3_v="0.3"))
    assert is_single_phase_candidate(dead)


async def test_hass_victron_with_unread_l2_and_l3_is_still_offered_single_phase(hass: HomeAssistant) -> None:
    candidate = single_phase_candidate(hass, _meter(hass, "victron", l2_v="unknown", l3_v="unavailable"))
    assert is_single_phase_candidate(candidate)


def _three_phase_site(hass: HomeAssistant):
    return make_site_entry(hass, entry_id="three", main_fuse_a=25.0)


async def test_detection_for_a_stored_three_phase_site_is_never_narrowed_to_l1(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A GX whose L2 and L3 read nothing is offered with all three phases to a site stored with three, and as L1
    only to a site stored as single-phase."""
    full = _meter(hass, "victron", l2_v="0", l3_v="0")
    monkeypatch.setattr(detection_module, "snapshot_registry", lambda _hass: ([], []))
    monkeypatch.setattr(detection_module, "detect_site", lambda *_a, **_k: Detection(meters=(full,)))

    three = _three_phase_site(hass)
    offered = site_detection(hass, three).meters[0]
    assert not is_single_phase_candidate(offered)
    assert site_phase_count(apply_meter_candidate(three.data, offered)) == 3

    single = make_site_entry(hass, entry_id="single", direct_entities={"L1": "sensor.single_l1"})
    assert is_single_phase_candidate(site_detection(hass, single).meters[0])


async def test_applying_a_single_phase_meter_keeps_a_stored_three_phase_site(hass: HomeAssistant) -> None:
    narrowed = single_phase_candidate(hass, _meter(hass, "victron", l2_v="0", l3_v="0"))
    assert is_single_phase_candidate(narrowed)
    three = _three_phase_site(hass)
    kept = apply_meter_candidate(three.data, narrowed)
    assert kept == dict(three.data)
    assert site_phase_count(kept) == 3

    # A site already single-phase takes it: nothing is reduced.
    single = {CONF_MEASUREMENT_MODE: MEASUREMENT_MODE_DIRECT, CONF_DIRECT_ENTITIES: {"L1": "sensor.single_l1"}}
    applied = apply_meter_candidate(single, narrowed)
    assert set(applied[CONF_DERIVED_ENTITIES]) == {"L1"} and site_phase_count(applied) == 1


async def test_a_single_phase_offer_says_so_in_every_language(hass: HomeAssistant) -> None:
    await texts.async_load(hass)
    narrowed = single_phase_candidate(hass, _meter(hass, "victron", l2_v="0", l3_v="0"))
    full = _meter(hass, "victron", l2_v="230", l3_v="230")
    assert candidate_label(hass, narrowed).endswith("(single-phase: L1 only)")
    assert "single-phase" not in candidate_label(hass, full)
    summary = site_confirm_summary(hass, candidate=narrowed, battery=None, chargers=[])
    assert "**Single-phase: L1 only.**" in summary
    assert "**L2:**" not in summary and "**L1:**" in summary
    assert "**L2:**" in site_confirm_summary(hass, candidate=full, battery=None, chargers=[])
    for language in ("da", "de", "en", "es", "fi", "fr", "nb", "nl", "sv"):
        table = texts.table(language, "site_confirm")
        assert "L1" in table["label_single_phase"] and "{label}" in table["label_single_phase"]
        assert "L1" in table["single_phase"]
        for key in ("join_wiring_single_phase", "charger_single_phase", "join_dc_single_phase"):
            assert "L1" in table[key], (language, key)


# ---- 2. A three-phase house set up with L1 only --------------------------------------------------

_CAR_SOURCE = {"kind": "separate_entities", "entity_ids": {phase: f"sensor.car_{phase.lower()}" for phase in PHASES}}


async def _single_phase_site(hass: HomeAssistant):
    entry = make_site_entry(
        hass,
        entry_id="single",
        main_fuse_a=25.0,
        direct_entities={"L1": "sensor.single_l1"},
        charger_entry_ids=["car"],
        phase_wiring={"car": {"phases": 3, "phase": None, "measured_current_source": _CAR_SOURCE}},
        title="Home",
    )
    set_current_sensor(hass, "sensor.single_l1", 12)
    for phase in ("l1", "l2", "l3"):
        set_current_sensor(hass, f"sensor.car_{phase}", 0)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    site = controller_of(hass, entry.entry_id)
    clock = {"now": 1000.0}
    site._yield_now = lambda: clock["now"]  # noqa: SLF001
    return entry, site, clock


def _car(hass: HomeAssistant, l1: float, l2: float, l3: float) -> None:
    for phase, amps in (("l1", l1), ("l2", l2), ("l3", l3)):
        set_current_sensor(hass, f"sensor.car_{phase}", amps)


def _issue(hass: HomeAssistant, entry_id: str = "single"):
    return ir.async_get(hass).async_get_issue(DOMAIN, f"{SINGLE_PHASE_SITE_CONFLICT_ISSUE}_{entry_id}")


async def test_a_charger_on_several_phases_of_a_single_phase_site_stops_the_site_and_says_so(
    hass: HomeAssistant,
) -> None:
    entry, site, clock = await _single_phase_site(hass)
    assert site.site_phases == SINGLE_PHASE and site.result.state == "observing"

    # The car draws on all three phases: nothing yet, the reading must last.
    _car(hass, 10, 10, 10)
    site._recompute(schedule_apply=False)  # noqa: SLF001
    assert site.result.state == "observing" and _issue(hass) is None
    clock["now"] += PHASE_CONFLICT_HOLD_S - 1
    _car(hass, 10.1, 10.1, 10.1)
    site._recompute(schedule_apply=False)  # noqa: SLF001
    assert site.result.state == "observing"

    clock["now"] += 2
    _car(hass, 10, 10, 10)
    site._recompute(schedule_apply=False)  # noqa: SLF001
    result = site.result
    assert result.state == "invalid_measurements" and result.state in UNHEALTHY_STATES
    assert result.reason == REASON_PHASE_CONFLICT
    # Held as for a lost meter: no phase's current, nothing proposed, and the regulator's lost-measurement rule holds.
    assert result.measured_phase_current_a == dict.fromkeys(PHASES)
    assert all(allocation.proposed_current_a is None for allocation in result.allocations)
    assert measurement_lost(site_result=result, phases=("L1",), max_age_s=120.0) is not None
    # No phase's sensor is at fault: the card's measurement line stays out of it, the Repairs issue speaks.
    assert site.measurement_problem is None
    issue = _issue(hass)
    assert issue is not None and issue.translation_key == SINGLE_PHASE_SITE_CONFLICT_ISSUE
    assert issue.translation_placeholders == {"site": "Home", "charger": "car"}
    assert issue.is_fixable is False

    # It stands while the settings do, also once the car draws nothing (held chargers must not clear it).
    _car(hass, 0, 0, 0)
    clock["now"] += 3600
    site._recompute(schedule_apply=False)  # noqa: SLF001
    assert site.result.reason == REASON_PHASE_CONFLICT and _issue(hass) is not None

    # Fixed: L2 and L3 added in the site's settings. The site regulates again and the issue is gone.
    for phase in ("l2", "l3"):
        set_current_sensor(hass, f"sensor.single_{phase}", 5)
    hass.config_entries.async_update_entry(
        entry,
        data={
            **entry.data,
            CONF_DIRECT_ENTITIES: {phase: f"sensor.single_{phase.lower()}" for phase in PHASES},
        },
    )
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert _issue(hass) is None
    fixed = controller_of(hass, entry.entry_id)
    assert fixed.site_phases == PHASES and fixed.result.state == "observing"


async def test_the_conflict_stands_until_a_setting_it_was_judged_on_changes(hass: HomeAssistant) -> None:
    """Judged on the settings' values, not the settings object: a change elsewhere (the chargers' order, kept in the
    same settings) leaves it standing, a corrected charger wiring in the same settings clears it."""
    _entry, site, clock = await _single_phase_site(hass)
    _car(hass, 10, 10, 10)
    site._recompute(schedule_apply=False)  # noqa: SLF001
    clock["now"] += PHASE_CONFLICT_HOLD_S + 1
    site._recompute(schedule_apply=False)  # noqa: SLF001
    assert site.result.reason == REASON_PHASE_CONFLICT
    site.config["priority"] = ["car"]
    clock["now"] += 1
    site._recompute(schedule_apply=False)  # noqa: SLF001
    assert site.result.reason == REASON_PHASE_CONFLICT
    site.config["phase_wiring"] = {"car": {"phases": 1, "phase": "L1", "measured_current_source": None}}
    clock["now"] += 1
    site._recompute(schedule_apply=False)  # noqa: SLF001
    assert site.result.reason != REASON_PHASE_CONFLICT and _issue(hass) is None


async def test_a_short_reading_on_several_phases_is_no_conflict(hass: HomeAssistant) -> None:
    _entry, site, clock = await _single_phase_site(hass)
    _car(hass, 10, 10, 10)
    site._recompute(schedule_apply=False)  # noqa: SLF001
    clock["now"] += PHASE_CONFLICT_HOLD_S / 2
    _car(hass, 10, 0, 0)
    site._recompute(schedule_apply=False)  # noqa: SLF001
    clock["now"] += PHASE_CONFLICT_HOLD_S
    _car(hass, 10, 8, 0)
    site._recompute(schedule_apply=False)  # noqa: SLF001
    # The hold started again with the second reading; a car on L1 alone never starts it.
    assert site.result.state == "observing" and _issue(hass) is None
    _car(hass, 16, 0.4, 0.2)
    clock["now"] += 10 * PHASE_CONFLICT_HOLD_S
    site._recompute(schedule_apply=False)  # noqa: SLF001
    assert site.result.state == "observing" and _issue(hass) is None


async def test_the_conflict_issue_is_worded_in_every_language(hass: HomeAssistant) -> None:
    import json
    from pathlib import Path

    folder = Path(detection_module.__file__).parent.parent / "translations"
    for language in ("da", "de", "en", "es", "fi", "fr", "nb", "nl", "sv"):
        issues = json.loads((folder / f"{language}.json").read_text(encoding="utf-8"))["issues"]
        text = issues[SINGLE_PHASE_SITE_CONFLICT_ISSUE]
        assert "{site}" in text["title"]
        assert all(part in text["description"] for part in ("{site}", "{charger}", "L1", "L2", "L3")), language


# ---- 3. The charger's wiring step on a single-phase site -----------------------------------------


def test_a_pending_site_is_single_phase_when_its_meter_measures_l1_alone() -> None:
    def pending(details: dict[str, Any], mode: str = MEASUREMENT_MODE_DIRECT, skip: bool = False) -> PendingSiteDetails:
        return PendingSiteDetails(
            details=details, charger_entry_ids=[], mode=mode, skip_direct_fields=skip, charger_candidates={}
        )

    assert pending_single_phase(pending({"direct_L1": "sensor.l1"}), None)
    assert not pending_single_phase(pending({f"direct_{phase}": "sensor.x" for phase in PHASES}), None)
    assert pending_single_phase(pending({"derived_L1_power": "sensor.p", "derived_L1_voltage": "sensor.v"},
                                        MEASUREMENT_MODE_DERIVED), None)
    source = {"kind": "attributes", "entity_id": "sensor.eq", "attributes": {"L1": "a", "L2": "b", "L3": "c"}}
    assert not pending_single_phase(pending({}, skip=True), source)
    assert pending_single_phase(pending({}, skip=True), {**source, "attributes": {"L1": "a"}})


async def test_the_wiring_step_on_a_single_phase_site_is_fixed_to_one_phase_on_l1(hass: HomeAssistant) -> None:
    await texts.async_load(hass)
    schema = charger_wiring_schema(hass, "car", existing={"phases": 3, "phase": "L2"}, single_phase_site=True)
    assert schema({}) == {"phases": 1, "phase": "l1"}
    for refused in ({"phases": 3}, {"phases": 1, "phase": "l2"}, {"phases": 1, "phase": "auto"}):
        with pytest.raises(Exception):  # noqa: B017, PT011 - voluptuous' own error
            schema(refused)
    # Elsewhere the choice is as before.
    assert charger_wiring_schema(hass, "car")({}) == {"phases": 3, "phase": "auto"}

    shown: list[dict[str, Any]] = []
    finished: list[bool] = []

    class Flow(ChargerWiringSteps):
        def async_show_form(self, **kwargs: Any) -> dict[str, Any]:
            shown.append(kwargs)
            return kwargs

        async def _finish_pending_site(self) -> dict[str, Any]:
            finished.append(True)
            return {}

        def _pending_single_phase(self) -> bool:
            return True

    flow = Flow()
    flow.hass = hass
    flow._init_charger_wiring()  # noqa: SLF001
    await flow._begin_charger_wiring(  # noqa: SLF001
        PendingSiteDetails(details={"direct_L1": "sensor.l1"}, charger_entry_ids=["car"],
                           mode=MEASUREMENT_MODE_DIRECT, skip_direct_fields=False, charger_candidates={})
    )
    assert shown[-1]["errors"] == {}
    assert shown[-1]["data_schema"]({}) == {"phases": 1, "phase": "l1"}
    await flow.async_step_site_charger_wiring({"phases": 1, "phase": "l1"})
    assert finished == [True]


def test_the_fixed_wiring_of_a_single_phase_site_is_not_stored_over_the_chargers_own() -> None:
    """The site runs every charger on L1 whatever is stored; what is stored is kept for the day L2 and L3 are added,
    so a charger that draws on three phases is never reckoned as a single-phase one on L1 then."""
    fixed = {"phases": 1, "phase": "l1"}
    assert parse_charger_wiring(None, fixed, single_phase_site=True) == {"phases": 3, "phase": None}
    stored = {"phases": 1, "phase": "L2", "measured_current_source": _CAR_SOURCE}
    assert parse_charger_wiring(stored, fixed, single_phase_site=True) == stored
    assert parse_charger_wiring(stored, fixed) == {**stored, "phase": "L1"}


async def test_joining_a_single_phase_site_says_one_phase(hass: HomeAssistant) -> None:
    await texts.async_load(hass)
    assert "single-phase" in site_join_summary(hass, None, single_phase_site=True, dc_charger=True)
    assert "DC charger" in site_join_summary(hass, None, single_phase_site=True, dc_charger=True)
    assert "cannot tell" in site_join_summary(hass, None, single_phase_site=True)
