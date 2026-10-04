"""Solar on what it can see: a direct site whose phase measurement is incomplete still runs on the total grid
power, a charger without its own current runs blind at the minimum, a single-phase charger of unknown
phase is reckoned on a stand-in phase, and what keeps solar from a basis is named in the status.

The field case (2026-10-04): a SolaX inverter in standby left L1 without a value, the total grid power read
158 W of export, and both chargers' own current had been declined in the site wiring.

No test sleeps: the coordinator's clock is hand-advanced and each tick is an explicit recompute.
"""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.spotnav.api import dashboard as dashboard_api
from custom_components.spotnav.api.entity_fields import site_measurement_info
from custom_components.spotnav.execution import solar_execution
from custom_components.spotnav.execution.solar_execution import (
    _build_observation,
    SolarExecutionCoordinator,
    solar_basis,
    STAND_IN_PHASE,
)
from custom_components.spotnav.const import CONF_CHARGER_ENTRY_IDS
from custom_components.spotnav.planning.status_compose import compose_status
from custom_components.spotnav.site.solar_surplus import SolarConfig, SolarController

from .helpers import make_site_entry, set_current_sensor
from .test_solar_total_power import _set_total, direct_solar_setup, TOTAL
from .world import controller_of, set_charger_delivered_a, tick_site

pytestmark = pytest.mark.usefixtures("offline_relay")


def _lose_l1(hass: HomeAssistant) -> None:
    """The inverter's L1 current goes unavailable; L2 and L3 keep reading 10 A."""
    hass.states.async_set("sensor.direct_site_l1", "unavailable", {"unit_of_measurement": "A"})


def _codes(hass: HomeAssistant, charger) -> list[dict]:
    """The composed status lines from the live capture, with planning left out (these chargers have no
    price area, which would block the whole block)."""
    capture = dashboard_api.capture_dashboard(hass, charger)
    facts = replace(dashboard_api.status_facts(capture), planning=None, has_settings=False)
    return compose_status(facts)["lines"]


# ---- a) an incomplete phase measurement ---------------------------------------------------------------


async def test_an_incomplete_measurement_keeps_each_reading_phase_s_headroom_and_never_raises_the_lost_one(
    hass: HomeAssistant,
) -> None:
    charger, _s, site, *_ = await direct_solar_setup(hass, source={"power": TOTAL}, site_amps=10.0)
    _lose_l1(hass)
    _set_total(hass, TOTAL, -4200.0)
    set_charger_delivered_a(hass, charger.entry_id, 4.0)
    site._recompute()
    assert site.result.state in ("missing_measurements", "invalid_measurements")

    observation = _build_observation(site, charger.entry_id, now=0.0)

    # 25 A fuse less 1 A margin: L2/L3 keep 14 A of headroom on top of the car's 4 A; L1 cannot be read,
    # so even with the total exporting the car goes no higher there than the minimum.
    assert observation.phase_cap_a == {"L1": 6.0, "L2": 18.0, "L3": 18.0}


async def test_without_export_a_phase_that_cannot_be_read_never_lets_the_current_rise(hass: HomeAssistant) -> None:
    charger, _s, site, *_ = await direct_solar_setup(hass, source={"power": TOTAL}, site_amps=10.0)
    _lose_l1(hass)
    _set_total(hass, TOTAL, 500.0)
    set_charger_delivered_a(hass, charger.entry_id, 4.0)
    site._recompute()

    observation = _build_observation(site, charger.entry_id, now=0.0)

    # What it draws, never above the minimum current.
    assert observation.phase_cap_a["L1"] == 6.0
    set_charger_delivered_a(hass, charger.entry_id, 9.0)
    site._recompute()
    assert _build_observation(site, charger.entry_id, now=0.0).phase_cap_a["L1"] == 9.0


async def test_solar_starts_on_the_total_with_an_incomplete_measurement_and_says_so(hass: HomeAssistant) -> None:
    charger, _s, site, coordinator, clock, turn_on_calls = await direct_solar_setup(
        hass, source={"power": TOTAL}, site_amps=3.0
    )
    _lose_l1(hass)
    _set_total(hass, TOTAL, -4500.0)

    await tick_site(hass, site)
    assert coordinator.state.state == "arming"
    clock.value = 125.0
    await tick_site(hass, site)

    assert coordinator.state.action == "start" and len(turn_on_calls) == 1
    # The export it sees: 4500 W over three phases at 230 V is 6.5 A, floored to 6 A.
    assert coordinator.state.requested_a == 6.0
    assert coordinator.state.basis.site_incomplete_phases == ("L1",)
    codes = [line["code"] for line in _codes(hass, charger)]
    assert codes[0] == "solar_charging"
    assert "solar_site_incomplete" in codes and "site_measurement_problem" in codes


def _netted_export_over_an_importing_l1(hass: HomeAssistant) -> None:
    """25 A fuse; a three-phase inverter gives 20 A per phase; the house draws 25 A on L1 and 2 A on L2/L3.
    L1 nets 5 A of import but cannot be read; L2/L3 export 18 A each; the total exports 7130 W."""
    hass.states.async_set("sensor.direct_site_l1", "unavailable", {"unit_of_measurement": "A"})
    set_current_sensor(hass, "sensor.direct_site_l2", 18.0)
    set_current_sensor(hass, "sensor.direct_site_l3", 18.0)
    _set_total(hass, TOTAL, (5 - 18 - 18) * 230.0)


def _asked_a(observation) -> float:
    solar = SolarController(SolarConfig(priority="battery_first", max_current_a=32.0))
    verdicts = [solar.observe(replace(observation, now=t)) for t in (0.0, 130.0, 260.0)]
    return max(verdict.requested_a or 0.0 for verdict in verdicts)


async def test_a_single_phase_car_on_the_unreadable_phase_is_never_raised_on_a_netted_export(
    hass: HomeAssistant,
) -> None:
    charger, _s, site, *_ = await direct_solar_setup(
        hass, source={"power": TOTAL}, site_amps=10.0, phases=1, phase="L1"
    )
    _netted_export_over_an_importing_l1(hass)
    set_charger_delivered_a(hass, charger.entry_id, 0.0)
    site._recompute()

    observation = _build_observation(site, charger.entry_id, now=0.0)

    assert observation.phase_cap_a == {"L1": 6.0}
    asked = _asked_a(observation)
    # At most the minimum: L1 carries 25 (house) - 20 (PV) + 6 = 11 A, under the 25 A fuse.
    assert asked == 6.0 and 25 - 20 + asked <= 25


async def test_a_three_phase_car_is_held_to_the_minimum_by_the_unreadable_phase_on_a_netted_export(
    hass: HomeAssistant,
) -> None:
    charger, _s, site, *_ = await direct_solar_setup(hass, source={"power": TOTAL}, site_amps=10.0)
    _netted_export_over_an_importing_l1(hass)
    set_charger_delivered_a(hass, charger.entry_id, 0.0)
    site._recompute()

    observation = _build_observation(site, charger.entry_id, now=0.0)

    # L2/L3 read 18 A and keep 24 - 18 = 6 A of headroom; L1 cannot be read and is held to the minimum.
    assert observation.phase_cap_a == {"L1": 6.0, "L2": 6.0, "L3": 6.0}
    assert _asked_a(observation) == 6.0


async def test_a_single_phase_car_of_unknown_phase_is_held_by_the_unreadable_phase(hass: HomeAssistant) -> None:
    charger, _s, site, *_ = await direct_solar_setup(
        hass, source={"power": TOTAL}, site_amps=10.0, phases=1, phase=None
    )
    _netted_export_over_an_importing_l1(hass)
    set_charger_delivered_a(hass, charger.entry_id, 0.0)
    site._recompute()

    observation = _build_observation(site, charger.entry_id, now=0.0)

    assert observation.phase_cap_a == {STAND_IN_PHASE: 6.0}
    assert _asked_a(observation) == 6.0


# ---- the field case: no charger current, unknown phase, L1 unavailable -----------------------------------


async def test_the_field_case_starts_blind_at_the_minimum_and_names_what_is_missing(hass: HomeAssistant) -> None:
    charger, _s, site, coordinator, clock, turn_on_calls = await direct_solar_setup(
        hass, source={"power": TOTAL}, phases=1, phase=None, site_amps=3.0, measured=False
    )
    _lose_l1(hass)
    _set_total(hass, TOTAL, -158.0)

    await tick_site(hass, site)
    clock.value = 200.0
    await tick_site(hass, site)
    # 158 W does not cover 6 A on one phase (1380 W): nothing starts.
    assert coordinator.state.state == "off" and not turn_on_calls
    assert coordinator.state.reason == "charger_measurement_missing"
    assert coordinator.state.basis.charger_current == "not_set"
    lines = _codes(hass, charger)
    assert [line["code"] for line in lines][:3] == [
        "solar_waiting_for_sun",
        "solar_charger_current_missing",
        "solar_site_incomplete",
    ]
    assert lines[1]["params"] == {"entity": None} and lines[2]["params"] == {"phases": ["L1"]}

    _set_total(hass, TOTAL, -1500.0)
    clock.value = 300.0
    await tick_site(hass, site)
    assert coordinator.state.state == "arming"
    clock.value = 425.0
    await tick_site(hass, site)

    assert coordinator.state.action == "start" and coordinator.state.requested_a == 6.0
    assert coordinator.state.reason == "unmeasured_start" and len(turn_on_calls) == 1


async def test_a_single_phase_charger_of_unknown_phase_is_reckoned_on_a_stand_in(hass: HomeAssistant) -> None:
    charger, _s, site, *_ = await direct_solar_setup(
        hass, source={"power": TOTAL}, phases=1, phase=None, site_amps=10.0
    )
    set_current_sensor(hass, "sensor.direct_site_l3", 20.0)
    for phase, amps in (("l1", 7.0), ("l2", 0.0), ("l3", 0.0)):
        set_current_sensor(hass, f"sensor.{charger.entry_id}_{phase}", amps)
    _set_total(hass, TOTAL, -2300.0)
    site._recompute()

    observation = _build_observation(site, charger.entry_id, now=0.0)

    assert observation.car_phases == (STAND_IN_PHASE,)
    assert observation.signed_grid_w == {"L1": -2300.0, "L2": 0.0, "L3": 0.0}
    # Its draw is the phase that reads most; the cap the lowest of all three (L3: 7 + 24 - 20).
    assert observation.car_delivered_a == {STAND_IN_PHASE: 7.0}
    assert observation.phase_cap_a == {STAND_IN_PHASE: 11.0}


# ---- b) what keeps solar from a basis, named -----------------------------------------------------------


async def test_an_unreadable_total_is_named_in_place_of_no_usable_reading(hass: HomeAssistant) -> None:
    charger, _s, site, coordinator, *_ = await direct_solar_setup(hass, source={"power": TOTAL})
    _set_total(hass, TOTAL, "unavailable")

    await tick_site(hass, site)

    assert coordinator.state.reason == "no_basis_off"
    assert coordinator.state.basis.problem == "grid_power_unreadable"
    assert _codes(hass, charger)[0] == {"code": "solar_no_grid_power", "params": {"entity": TOTAL}}


async def test_a_total_that_is_not_set_says_to_set_it(hass: HomeAssistant) -> None:
    charger, _s, site, *_ = await direct_solar_setup(hass, source=None)
    site._recompute()

    basis = solar_basis(site, charger.entry_id, _build_observation(site, charger.entry_id, now=0.0))

    assert basis.problem == "grid_power_not_set" and basis.problem_entity is None


async def test_a_configured_but_unreadable_charger_current_names_its_entity(hass: HomeAssistant) -> None:
    charger, _s, site, *_ = await direct_solar_setup(hass, source={"power": TOTAL})
    for phase in ("l1", "l2", "l3"):
        hass.states.async_set(f"sensor.{charger.entry_id}_{phase}", "unavailable")
    _set_total(hass, TOTAL, -2000.0)
    site._recompute()

    basis = solar_basis(site, charger.entry_id, _build_observation(site, charger.entry_id, now=0.0))

    assert basis.charger_current == "unreadable"
    assert basis.charger_current_entity == f"sensor.{charger.entry_id}_l1"


# ---- two blind chargers never claim the same export -------------------------------------------------


def _peer(state: str, on_since: float | None, *, blind: bool = True):
    basis = SimpleNamespace(charger_current="not_set" if blind else None)
    return SimpleNamespace(
        solar=SimpleNamespace(
            solar_activity=lambda _site: (state, on_since),
            state=SimpleNamespace(basis=basis),
        )
    )


@pytest.mark.parametrize(
    ("own", "peer", "allowed"),
    [
        # An earlier blind charger arming goes first; a later one yields.
        ("b", _peer("arming", None), False),
        ("a", _peer("arming", None), True),
        # A charger that reads its own current never yields.
        ("a", _peer("arming", None, blind=False), False),
        # A charger that started a moment ago is not in the grid reading yet.
        ("a", _peer("on", 990.0), False),
        ("a", _peer("on", 900.0), True),
        ("a", _peer("off", None), True),
    ],
)
def test_a_blind_start_waits_for_the_site_s_other_chargers(monkeypatch, own, peer, allowed) -> None:
    other = "b" if own == "a" else "a"
    site = SimpleNamespace(config={CONF_CHARGER_ENTRY_IDS: ["a", "b"]}, charger_priority=lambda _id: "normal")
    monkeypatch.setattr(solar_execution, "charger_data", lambda _hass, entry_id: peer if entry_id == other else None)
    coordinator = SolarExecutionCoordinator.__new__(SolarExecutionCoordinator)
    coordinator._hass = None
    coordinator._charger_entry_id = own

    assert coordinator._unmeasured_start_allowed(site, now=1000.0) is allowed


# ---- c) the meter's sensors unavailable together ------------------------------------------------------


async def _meter_site(hass: HomeAssistant, entry_id: str, platform: str | None):
    entry = make_site_entry(hass, entry_id=entry_id, main_fuse_a=25.0, charger_entry_ids=[])
    registry = er.async_get(hass)
    for phase in ("l1", "l2", "l3"):
        entity_id = f"sensor.{entry_id}_{phase}"
        if platform is not None:
            registry.async_get_or_create(
                "sensor", platform, f"{entry_id}_{phase}", suggested_object_id=f"{entry_id}_{phase}"
            )
        set_current_sensor(hass, entity_id, 4)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry, controller_of(hass, entry.entry_id)


async def test_an_inverter_phase_gone_unavailable_is_named_as_the_meter_in_standby(hass: HomeAssistant) -> None:
    entry, controller = await _meter_site(hass, "inv_site", "solax_modbus")
    hass.states.async_set("sensor.inv_site_l1", "unavailable")
    controller._recompute()

    problem = controller.measurement_problem
    assert problem is not None
    assert problem.unavailable_entities == ("sensor.inv_site_l1",) and problem.inverter is True
    [warning, *_] = site_measurement_info(hass, entry)["warnings"]
    assert warning["code"] == "measurement_unhealthy"
    assert warning["unavailable_entities"] == ["sensor.inv_site_l1"] and warning["inverter"] is True


async def test_every_phase_of_a_meter_unavailable_is_named_without_an_inverter(hass: HomeAssistant) -> None:
    _entry, controller = await _meter_site(hass, "meter_site", None)
    for phase in ("l1", "l2", "l3"):
        hass.states.async_set(f"sensor.meter_site_{phase}", "unknown")
    controller._recompute()

    problem = controller.measurement_problem
    assert problem is not None
    assert problem.unavailable_entities == ("sensor.meter_site_l1", "sensor.meter_site_l2", "sensor.meter_site_l3")
    assert problem.inverter is False


async def test_one_phase_of_a_plain_meter_or_a_bad_value_stays_the_generic_problem(hass: HomeAssistant) -> None:
    _entry, controller = await _meter_site(hass, "plain_site", None)
    hass.states.async_set("sensor.plain_site_l1", "unavailable")
    controller._recompute()
    assert controller.measurement_problem.unavailable_entities == ()

    _entry, inverter = await _meter_site(hass, "odd_site", "huawei_solar")
    hass.states.async_set("sensor.odd_site_l2", "not a number", {"unit_of_measurement": "A"})
    inverter._recompute()
    assert inverter.measurement_problem.unavailable_entities == ()


def test_a_phase_without_headroom_on_a_site_whose_measurement_is_not_unusable_gets_no_cap() -> None:
    """A site that is off or not configured has no headroom and is no fault in the measurement: no basis,
    as before, rather than the export rule."""
    result = SimpleNamespace(phase_headroom_a={}, phase_liveness={}, measured_phase_current_a={})
    kwargs = {"fuse_limit_a": 24.0, "min_current_a": 6.0}

    assert solar_execution._fuse_caps(result, {"L1": 0.0}, measurement_unusable=False, **kwargs) == {"L1": None}
    assert solar_execution._fuse_caps(result, {"L1": 0.0}, measurement_unusable=True, **kwargs) == {"L1": 6.0}
    assert solar_execution._fuse_caps(result, {"L1": 9.0}, measurement_unusable=True, **kwargs) == {"L1": 9.0}
