"""What the site says about a meter it reads wrongly or that reports seldom.

The field case (2026-10): a Tibber Pulse whose phase currents are signed (export on L2 and L3 read
negative) on a site set to read them unsigned, so every negative phase was rejected; and an Easee
Equalizer whose cloud integration writes a state only on a change, its values every 5-10 minutes, so its
import and export power were judged stale and solar had no basis.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.spotnav.api import dashboard as dashboard_api
from custom_components.spotnav.api.entity_fields import site_measurement_info
from custom_components.spotnav.planning.status_compose import compose_status
from custom_components.spotnav.site import site_capacity_controller as controller_module
from custom_components.spotnav.site.measurement_problem import measurement_problem
from custom_components.spotnav.site.meter_cadence import (
    CHANGE_ONLY_AGE_CAP_S,
    median_interval_s,
    ReportCadence,
    SOLAR_AGE_CAP_S,
    solar_age_limit_s,
    solar_liveness,
    too_slow_for_load_balancing,
)
from custom_components.spotnav.site.site_capacity import PHASES, SiteCapacityResult

from .test_solar_total_power import _set_total, direct_solar_setup, TOTAL
from .world import setup_charger_and_site

pytestmark = pytest.mark.usefixtures("offline_relay")

T0 = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)


def _history(*gaps_s: float) -> list[datetime]:
    moments = [T0]
    for gap in gaps_s:
        moments.append(moments[-1] + timedelta(seconds=gap))
    return moments


def _site_controller(hass: HomeAssistant, site_entry):
    return hass.config_entries.async_get_entry(site_entry.entry_id).runtime_data.controller


def _status_codes(hass: HomeAssistant, charger) -> list[dict]:
    capture = dashboard_api.capture_dashboard(hass, charger)
    facts = replace(dashboard_api.status_facts(capture), planning=None, has_settings=False)
    return compose_status(facts)["lines"]


def _set_pulse(hass: HomeAssistant, values: dict[str, str]) -> None:
    for phase, value in values.items():
        hass.states.async_set(
            f"sensor.site_entry_a_{phase.lower()}",
            value,
            {"unit_of_measurement": "A", "device_class": "current", "state_class": "measurement"},
        )


# ---- 1. a signed meter on a site that reads it unsigned ----------------------------------------------


def test_the_problem_names_the_phases_that_read_negative() -> None:
    result = SiteCapacityResult(
        state="invalid_measurements",
        reason="phase_measurement_invalid",
        measurement_mode="direct_phase_current",
        confidence="none",
        measured_phase_current_a={"L1": 4.5, "L2": None, "L3": None},
        phase_age_s={phase: 1.0 for phase in PHASES},
        phase_headroom_a={phase: None for phase in PHASES},
        limiting_phase=None,
        max_age_s=120.0,
        allocations=(),
    )
    entities = {"L1": "sensor.l1", "L2": "sensor.l2", "L3": "sensor.l3"}

    problem = measurement_problem(result, entities, negative=("L2", "L3"))
    assert problem is not None and problem.negative_phases == ("L2", "L3")
    # A healthy phase that happens to be listed is never named.
    assert measurement_problem(result, entities, negative=("L1",)).negative_phases == ()
    assert measurement_problem(result, entities).negative_phases == ()


async def test_negative_currents_on_an_unsigned_site_say_to_turn_on_signed_current(hass: HomeAssistant) -> None:
    charger, site_entry = await setup_charger_and_site(hass)
    _set_pulse(hass, {"L1": "4.467", "L2": "-2.445", "L3": "-2.756"})
    site = _site_controller(hass, site_entry)
    site._recompute()

    assert site.result.state == "invalid_measurements"
    assert site.measurement_problem.negative_phases == ("L2", "L3")
    [warning] = [w for w in site_measurement_info(hass, site_entry)["warnings"] if w["code"] == "measurement_unhealthy"]
    assert warning["negative_phases"] == ["L2", "L3"]
    lines = _status_codes(hass, charger)
    assert {"code": "site_current_negative", "params": {"phases": ["L2", "L3"]}} in lines
    assert "site_measurement_problem" not in [line["code"] for line in lines]


async def test_the_same_readings_on_a_signed_site_are_used_as_their_magnitude(hass: HomeAssistant) -> None:
    charger, site_entry = await setup_charger_and_site(hass, extra_data={"site_current_signed": True})
    _set_pulse(hass, {"L1": "4.467", "L2": "-2.445", "L3": "-2.756"})
    site = _site_controller(hass, site_entry)
    site._recompute()

    assert site.result.state not in ("invalid_measurements", "missing_measurements", "stale_measurements")
    assert site.result.measured_phase_current_a["L2"] == pytest.approx(2.445)
    assert site.measurement_problem is None
    assert all(w["negative_phases"] == [] for w in site_measurement_info(hass, site_entry)["warnings"])


async def test_a_missing_phase_is_not_called_negative(hass: HomeAssistant) -> None:
    charger, site_entry = await setup_charger_and_site(hass)
    _set_pulse(hass, {"L1": "4.4", "L2": "unavailable", "L3": "3.0"})
    site = _site_controller(hass, site_entry)
    site._recompute()

    assert site.measurement_problem.negative_phases == ()
    assert "site_current_negative" not in [line["code"] for line in _status_codes(hass, charger)]


# ---- 2. slow and change-only meters -------------------------------------------------------------------


def test_the_interval_is_the_median_gap_once_three_gaps_are_seen() -> None:
    assert median_interval_s(_history(420, 300)) is None
    assert median_interval_s(_history(420, 300, 600)) == 420.0
    # One odd gap (a reconnect) does not decide it.
    assert median_interval_s(_history(5, 5, 900, 5, 5)) == 5.0


def test_a_meter_is_too_slow_for_load_balancing_past_half_the_maximum_age() -> None:
    assert not too_slow_for_load_balancing(None, 120.0)
    assert not too_slow_for_load_balancing(60.0, 120.0)
    assert too_slow_for_load_balancing(61.0, 120.0)


def test_solar_accepts_a_slow_meter_for_two_of_its_intervals_within_a_cap() -> None:
    assert solar_age_limit_s(None, 120.0) == 120.0
    assert solar_age_limit_s(10.0, 120.0) == 120.0
    assert solar_age_limit_s(420.0, 120.0) == 840.0
    assert solar_age_limit_s(3600.0, 120.0) == SOLAR_AGE_CAP_S
    # Never shorter than the maximum age.
    assert solar_age_limit_s(70.0, 120.0) == 140.0
    assert solar_age_limit_s(61.0, 200.0) == 200.0


def test_solar_liveness_of_slow_and_change_only_meters() -> None:
    # The field case: 507 s since the last write, a meter seen to report about every 7 minutes.
    assert solar_liveness(507.0, 507.0, max_age_s=120.0) == "no_recent_report"
    assert solar_liveness(507.0, 507.0, max_age_s=120.0, interval_s=420.0) == "fresh"
    assert solar_liveness(900.0, 900.0, max_age_s=120.0, interval_s=420.0) == "no_recent_report"
    # A change-only meter that is available and loaded is alive though silent, up to its cap.
    assert solar_liveness(900.0, 900.0, max_age_s=120.0, change_only_alive=True) == "confirmed_unchanged"
    over = CHANGE_ONLY_AGE_CAP_S + 1
    assert solar_liveness(over, over, max_age_s=120.0, change_only_alive=True) == "no_recent_report"


def test_the_cadence_keeps_distinct_reports_in_order() -> None:
    cadence = ReportCadence(history=4)
    for moment in _history(420, 420, 420):
        cadence.observe("sensor.eq", moment)
        cadence.observe("sensor.eq", moment)  # the same write seen twice
    cadence.observe("sensor.eq", T0)  # an older instant
    cadence.observe("sensor.eq", None)
    assert len(cadence.reports("sensor.eq")) == 4
    assert cadence.interval_s("sensor.eq") == 420.0
    assert cadence.slow(["sensor.eq", "sensor.unknown"], 120.0) == {"sensor.eq": 420.0}
    for moment in _history(420, 420, 420, 5, 5, 5, 5):
        cadence.observe("sensor.fast", moment)
    # Only the last four instants count.
    assert cadence.interval_s("sensor.fast") == 5.0


async def _report_every(hass: HomeAssistant, freezer, site, gap_s: float, count: int, writes) -> None:
    for index in range(count):
        writes(index)
        site._recompute()
        freezer.tick(timedelta(seconds=gap_s))


async def test_a_slow_phase_meter_is_listed_to_check_and_holds_load_balancing(
    hass: HomeAssistant, freezer, monkeypatch
) -> None:
    charger, site_entry = await setup_charger_and_site(hass, active_control_enabled=True)
    site = _site_controller(hass, site_entry)
    await _report_every(
        hass, freezer, site, 420, 5, lambda i: _set_pulse(hass, {p: f"{5 + i}.0" for p in ("L1", "L2", "L3")})
    )

    assert site.report_interval_s("sensor.site_entry_a_l1") == 420.0
    assert set(site.load_balancing_slow_meters) == {f"sensor.site_entry_a_{p}" for p in ("l1", "l2", "l3")}
    slow = [w for w in site_measurement_info(hass, site_entry)["warnings"] if w["code"] == "meter_updates_slowly"]
    assert [w["interval_s"] for w in slow] == [420.0, 420.0, 420.0]
    # Named by the device, else the entity's friendly name.
    assert {w["device_name"] for w in slow} == {f"site entry a {p}" for p in ("l1", "l2", "l3")}

    held: list[tuple[str, str]] = []
    monkeypatch.setattr(
        site, "_log_active_control_outcome", lambda entry_id, _d, *, outcome, detail, **_k: held.append((outcome, detail))
    )
    real_decisions = site.regulator_decisions
    site.regulator_decisions = {charger.entry_id: SimpleNamespace(proposed_current_a=10.0)}
    try:
        await site._async_apply_active_control()
    finally:
        site.regulator_decisions = real_decisions
    assert held == [("held", controller_module.DETAIL_HELD_METER_TOO_SLOW)]


async def test_a_meter_fast_enough_is_not_listed(hass: HomeAssistant, freezer) -> None:
    charger, site_entry = await setup_charger_and_site(hass)
    site = _site_controller(hass, site_entry)
    await _report_every(
        hass, freezer, site, 10, 6, lambda i: _set_pulse(hass, {p: f"{5 + i}.0" for p in ("L1", "L2", "L3")})
    )

    assert site.report_interval_s("sensor.site_entry_a_l1") == 10.0
    assert site.slow_meters == {}
    codes = [w["code"] for w in site_measurement_info(hass, site_entry)["warnings"]]
    assert "meter_updates_slowly" not in codes


async def test_solar_reads_a_slow_total_for_longer_but_not_forever(hass: HomeAssistant, freezer) -> None:
    _c, _s, site, *_ = await direct_solar_setup(hass, source={"power": TOTAL})
    await _report_every(hass, freezer, site, 420, 5, lambda i: _set_total(hass, TOTAL, -900.0 - i))
    _set_total(hass, TOTAL, -1500.0)
    site._recompute()

    freezer.tick(timedelta(seconds=507))
    assert site.grid_total_reading() == (-1500.0, "confirmed_unchanged")
    freezer.tick(timedelta(seconds=400))
    assert site.grid_total_reading() == (None, "stale")
    # Load balancing does not read the total: nothing is held for it.
    assert TOTAL in site.slow_meters and site.load_balancing_slow_meters == {}


async def test_a_change_only_total_that_is_available_stays_live_for_solar(hass: HomeAssistant, freezer) -> None:
    easee = MockConfigEntry(domain="easee")
    easee.add_to_hass(hass)
    easee.mock_state(hass, ConfigEntryState.LOADED)
    er.async_get(hass).async_get_or_create(
        "sensor", "easee", "QP1_import_power", config_entry=easee, suggested_object_id="grid_total_w"
    )
    _c, _s, site, *_ = await direct_solar_setup(hass, source={"power": TOTAL})
    _set_total(hass, TOTAL, -900.0)

    freezer.tick(timedelta(seconds=600))
    assert site.grid_total_reading() == (-900.0, "confirmed_unchanged")
    hass.states.async_set(TOTAL, "unavailable")
    assert site.grid_total_reading()[1] == "missing"
    _set_total(hass, TOTAL, -900.0)
    freezer.tick(timedelta(seconds=CHANGE_ONLY_AGE_CAP_S + 1))
    assert site.grid_total_reading() == (None, "stale")


async def test_a_change_only_total_whose_integration_is_not_loaded_is_stale(hass: HomeAssistant, freezer) -> None:
    easee = MockConfigEntry(domain="easee")
    easee.add_to_hass(hass)
    easee.mock_state(hass, ConfigEntryState.SETUP_RETRY)
    er.async_get(hass).async_get_or_create(
        "sensor", "easee", "QP1_import_power", config_entry=easee, suggested_object_id="grid_total_w"
    )
    _c, _s, site, *_ = await direct_solar_setup(hass, source={"power": TOTAL})
    _set_total(hass, TOTAL, -900.0)

    freezer.tick(timedelta(seconds=600))
    assert site.grid_total_reading() == (None, "stale")
