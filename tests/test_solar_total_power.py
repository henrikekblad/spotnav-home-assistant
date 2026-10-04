"""Solar and hybrid from the meter's total grid power: a direct site (per-phase current only) with a
`grid_power_source` is solar-capable.

* The strategy picker, the dashboard rows, the capability snapshot and the executor agree on who may run
  solar, and a direct site without the total says why.
* The total is read as one signed entity or an import/export pair (a missing half is missing, never zero)
  and obeys `max_age_s`.
* The surplus split over the charger's phases, capped by each phase's fuse headroom, through the real
  site controller and the pure state machine.
* The entity-config API describes, validates and writes the new fields.

No test sleeps: the coordinator's clock is hand-advanced and each tick is an explicit recompute.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import async_mock_service

from custom_components.spotnav.api import dashboard as dashboard_api
from custom_components.spotnav.const import CONF_GRID_POWER_SOURCE, CONF_MEASURED_CURRENT_SOURCE
from custom_components.spotnav.execution.solar_execution import (
    _build_observation,
    _split_total,
    NOMINAL_PHASE_VOLTAGE_V,
    site_supports_solar,
)
from custom_components.spotnav.planning.auto_settings import STRATEGY_HYBRID, STRATEGY_SOLAR
from custom_components.spotnav.planning.strategy_options import strategy_options_for
from custom_components.spotnav.runtime import domain_data
from custom_components.spotnav.site.measurement_source import (
    GridPowerSource,
    grid_power_source_from_dict,
    grid_power_source_to_dict,
    PhaseMeasurementSource,
    source_to_dict,
)
from custom_components.spotnav.site.solar_capability import (
    REASON_NEEDS_SURPLUS_MEASUREMENT,
    REASON_NEEDS_TOTAL_GRID_POWER,
    solar_capability,
)
from custom_components.spotnav.site.solar_surplus import SolarConfig, SolarController, SolarObservation

from .helpers import make_entry, make_site_entry
from .messages import get_message, register, update_entity_config_message
from .world import admin, controller_of, SecondsClock, set_charger_delivered_a, tick_site, ws_call

pytestmark = pytest.mark.usefixtures("offline_relay")

PHASES = ("L1", "L2", "L3")
TOTAL = "sensor.grid_total_w"
EXPORT_HALF = "sensor.grid_export_w"


def _set_total(hass: HomeAssistant, entity_id: str, watts: float | str, unit: str = "W") -> None:
    hass.states.async_set(entity_id, str(watts), {"unit_of_measurement": unit})


def _set_site_currents(hass: HomeAssistant, entry_id: str, amps: float) -> None:
    for phase in PHASES:
        hass.states.async_set(f"sensor.{entry_id}_{phase.lower()}", str(amps), {"unit_of_measurement": "A"})


async def direct_solar_setup(
    hass: HomeAssistant,
    *,
    source: dict[str, str] | None = None,
    strategy: str = STRATEGY_SOLAR,
    phases: int = 3,
    phase: str | None = None,
    site_amps: float = 3.0,
    extra_data: dict[str, Any] | None = None,
    measured: bool = True,
):
    """A charger on `solar`, then a direct-mode site (per-phase current only) with the meter's total grid
    power. Returns `(charger, site_entry, site_controller, coordinator, clock, turn_on_calls)`."""
    prefix = "direct_charger"
    hass.states.async_set(f"switch.{prefix}", "off")
    charger = make_entry(
        hass,
        entry_id=prefix,
        charge_control=f"switch.{prefix}",
        current_limit=None,
        webhook_id="webhook-direct",
        title="Direct charger",
    )
    assert await hass.config_entries.async_setup(charger.entry_id)
    await hass.async_block_till_done()
    turn_on_calls = async_mock_service(hass, "switch", "turn_on")
    async_mock_service(hass, "switch", "turn_off")
    store = domain_data(hass).auto_store
    await store.async_update(charger.entry_id, mutate=lambda s: replace(s, strategy=strategy))
    clock = SecondsClock(0.0)
    coordinator = hass.config_entries.async_get_entry(charger.entry_id).runtime_data.solar
    coordinator._now = clock.now

    _set_site_currents(hass, "direct_site", site_amps)
    set_charger_delivered_a(hass, prefix, 0.0)
    _set_total(hass, TOTAL, 0.0)
    data: dict[str, Any] = dict(extra_data or {})
    if source is not None:
        data[CONF_GRID_POWER_SOURCE] = source
    site_entry = make_site_entry(
        hass,
        entry_id="direct_site",
        charger_entry_ids=[charger.entry_id],
        phase_wiring={
            charger.entry_id: {
                "phases": phases,
                "phase": phase,
                "min_current_a": 6.0,
                **(
                    {
                        CONF_MEASURED_CURRENT_SOURCE: source_to_dict(
                            PhaseMeasurementSource(
                                kind="separate_entities",
                                entity_ids={p: f"sensor.{prefix}_{p.lower()}" for p in PHASES},
                            )
                        )
                    }
                    if measured
                    else {"measured_source_declined": True}
                ),
            }
        },
        extra_data=data,
    )
    assert await hass.config_entries.async_setup(site_entry.entry_id)
    await hass.async_block_till_done()
    site_controller = controller_of(hass, site_entry.entry_id)
    for attr in ("_timer_cancel", "_state_listener_cancel"):
        cancel = getattr(site_controller, attr)
        if cancel is not None:
            cancel()
            setattr(site_controller, attr, None)
    turn_on_calls.clear()
    return charger, site_entry, site_controller, coordinator, clock, turn_on_calls


# ---- the capability rule ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("mode", "source", "capable", "reason"),
    [
        ("derived_phase_current", None, True, None),
        ("derived_phase_current", {"power": TOTAL}, True, None),
        ("direct_phase_current", {"power": TOTAL}, True, None),
        ("direct_phase_current", {"power": TOTAL, "power_export": EXPORT_HALF}, True, None),
        ("direct_phase_current", None, False, REASON_NEEDS_TOTAL_GRID_POWER),
        ("direct_phase_current", {}, False, REASON_NEEDS_TOTAL_GRID_POWER),
        ("direct_phase_current", {"power": "not an entity"}, False, REASON_NEEDS_TOTAL_GRID_POWER),
        ("direct_phase_current", {"power": TOTAL, "power_export": "bad"}, False, REASON_NEEDS_TOTAL_GRID_POWER),
        ("unavailable", {"power": TOTAL}, False, REASON_NEEDS_SURPLUS_MEASUREMENT),
        (None, None, False, REASON_NEEDS_SURPLUS_MEASUREMENT),
    ],
)
def test_the_capability_follows_the_mode_and_the_stored_total(mode, source, capable, reason) -> None:
    result = solar_capability(mode, source)

    assert (result.capable, result.reason) == (capable, reason)


def test_a_stored_total_round_trips_and_untrusted_storage_is_not_configured() -> None:
    pair = GridPowerSource(power=TOTAL, power_export=EXPORT_HALF)

    assert grid_power_source_from_dict(grid_power_source_to_dict(pair)) == pair
    assert grid_power_source_to_dict(GridPowerSource(power=TOTAL)) == {"power": TOTAL}
    assert grid_power_source_from_dict({"power": TOTAL, "power_export": ""}) == GridPowerSource(power=TOTAL)
    assert grid_power_source_to_dict(None) is None
    for bad in (None, "sensor.x", [], {"power_export": EXPORT_HALF}, {"power": 5}):
        assert grid_power_source_from_dict(bad) is None


# ---- the strategy picker and the dashboard ------------------------------------------------------


async def test_a_direct_site_with_the_total_offers_solar_and_hybrid(hass: HomeAssistant) -> None:
    charger, *_ = await direct_solar_setup(hass, source={"power": TOTAL})

    assert strategy_options_for(hass, charger.entry_id) == ["cheapest", "solar", "hybrid"]
    assert site_supports_solar(hass, charger.entry_id)
    capture = dashboard_api.capture_dashboard(hass, charger)
    strategy = dashboard_api.serialize_strategy(capture)
    assert [(row["strategy"], row["available"], row["reason"]) for row in strategy["available"]] == [
        ("cheapest", True, None),
        ("solar", True, None),
        ("hybrid", True, None),
    ]


async def test_a_direct_site_without_the_total_offers_cheapest_alone_and_says_why(hass: HomeAssistant) -> None:
    charger, _site, site_controller, *_ = await direct_solar_setup(hass, strategy="cheapest")

    assert strategy_options_for(hass, charger.entry_id) == ["cheapest"]
    assert not site_supports_solar(hass, charger.entry_id)
    snapshot = site_controller.capability_snapshot
    assert (snapshot.solar.capable, snapshot.solar.reason) == (False, REASON_NEEDS_TOTAL_GRID_POWER)
    capture = dashboard_api.capture_dashboard(hass, charger)
    rows = dashboard_api.serialize_strategy(capture)["available"]
    assert {(row["strategy"], row["available"], row["reason"]) for row in rows} == {
        ("cheapest", True, None),
        ("solar", False, REASON_NEEDS_TOTAL_GRID_POWER),
        ("hybrid", False, REASON_NEEDS_TOTAL_GRID_POWER),
    }


# ---- reading the total --------------------------------------------------------------------------


async def test_one_signed_entity_is_import_positive_and_inversion_negates_it(hass: HomeAssistant) -> None:
    _c, _s, site, *_ = await direct_solar_setup(hass, source={"power": TOTAL})
    _set_total(hass, TOTAL, -1500.0)

    assert site.grid_total_reading() == (-1500.0, "fresh")
    assert site.grid_power_snapshot()["export_w"] == 1500.0

    site.config[CONF_GRID_POWER_SOURCE] = {"power": TOTAL}
    site.config["grid_power_inverted"] = True
    assert site.grid_total_reading() == (1500.0, "fresh")
    assert site.grid_power_snapshot()["export_w"] == 0.0


async def test_a_kilowatt_total_is_read_in_watts(hass: HomeAssistant) -> None:
    _c, _s, site, *_ = await direct_solar_setup(hass, source={"power": TOTAL})
    _set_total(hass, TOTAL, -1.5, unit="kW")

    assert site.grid_total_reading() == (-1500.0, "fresh")


async def test_an_import_export_pair_is_import_minus_export(hass: HomeAssistant) -> None:
    _c, _s, site, *_ = await direct_solar_setup(hass, source={"power": TOTAL, "power_export": EXPORT_HALF})
    _set_total(hass, TOTAL, 200.0)
    _set_total(hass, EXPORT_HALF, 2700.0)

    assert site.grid_total_reading() == (-2500.0, "fresh")


@pytest.mark.parametrize("half_state", ["unavailable", None])
async def test_a_pair_with_a_missing_half_is_missing_never_zero_export(hass: HomeAssistant, half_state) -> None:
    _c, _s, site, *_ = await direct_solar_setup(hass, source={"power": TOTAL, "power_export": EXPORT_HALF})
    _set_total(hass, TOTAL, 200.0)
    if half_state is not None:
        _set_total(hass, EXPORT_HALF, half_state)

    value, state = site.grid_total_reading()

    assert value is None
    assert state in ("missing", "invalid")
    assert site.grid_power_snapshot()["export_w"] is None


async def test_a_wrong_unit_total_is_invalid_not_a_number(hass: HomeAssistant) -> None:
    _c, _s, site, *_ = await direct_solar_setup(hass, source={"power": TOTAL})
    _set_total(hass, TOTAL, 1000.0, unit="A")

    assert site.grid_total_reading() == (None, "invalid")


async def test_a_stale_total_is_unknown_and_a_flat_but_reporting_one_is_accepted(
    hass: HomeAssistant, freezer
) -> None:
    _c, _s, site, *_ = await direct_solar_setup(hass, source={"power": TOTAL})
    _set_total(hass, TOTAL, -900.0)

    freezer.tick(timedelta(seconds=600))
    assert site.grid_total_reading() == (None, "stale")
    assert site.grid_power_snapshot()["export_w"] is None

    # The source writes the same value again: unchanged, but demonstrably alive.
    _set_total(hass, TOTAL, -900.0)
    assert site.grid_total_reading() == (-900.0, "confirmed_unchanged")


async def test_no_total_configured_is_not_configured(hass: HomeAssistant) -> None:
    _c, _s, site, *_ = await direct_solar_setup(hass, strategy="cheapest")

    assert site.grid_total_power() is None
    assert site.grid_total_reading() == (None, "not_configured")
    assert site.grid_power_snapshot()["configured"] is False


# ---- the surplus split and cap ------------------------------------------------------------------


def test_the_total_is_split_over_the_phases_the_charger_uses() -> None:
    assert _split_total(-4200.0, PHASES) == {"L1": -1400.0, "L2": -1400.0, "L3": -1400.0}
    assert _split_total(-4200.0, ("L2",)) == {"L1": 0.0, "L2": -4200.0, "L3": 0.0}
    assert _split_total(None, PHASES) == {"L1": None, "L2": None, "L3": None}
    assert _split_total(-4200.0, ()) == {"L1": None, "L2": None, "L3": None}


async def test_the_observation_of_a_direct_site_splits_the_total_and_caps_by_headroom(hass: HomeAssistant) -> None:
    charger, _s, site, *_ = await direct_solar_setup(hass, source={"power": TOTAL}, site_amps=10.0)
    _set_total(hass, TOTAL, -4200.0)
    set_charger_delivered_a(hass, charger.entry_id, 4.0)
    site._recompute()

    observation = _build_observation(site, charger.entry_id, now=0.0)

    assert observation.signed_grid_w == {"L1": -1400.0, "L2": -1400.0, "L3": -1400.0}
    assert observation.voltage_v == {phase: NOMINAL_PHASE_VOLTAGE_V for phase in PHASES}
    # 25 A fuse, 1 A margin, 10 A measured on the phase: 14 A of headroom on top of the car's 4 A.
    assert observation.phase_cap_a == {"L1": 18.0, "L2": 18.0, "L3": 18.0}


async def test_a_single_phase_charger_gets_the_whole_total_on_its_phase_and_only_its_cap(
    hass: HomeAssistant,
) -> None:
    charger, _s, site, *_ = await direct_solar_setup(
        hass, source={"power": TOTAL}, phases=1, phase="L2", site_amps=10.0
    )
    _set_total(hass, TOTAL, -2300.0)
    set_charger_delivered_a(hass, charger.entry_id, 0.0)
    site._recompute()

    observation = _build_observation(site, charger.entry_id, now=0.0)

    assert observation.car_phases == ("L2",)
    assert observation.signed_grid_w == {"L1": 0.0, "L2": -2300.0, "L3": 0.0}
    assert observation.phase_cap_a == {"L2": 14.0}


async def test_a_stale_or_missing_total_is_no_basis_never_zero_export(hass: HomeAssistant) -> None:
    charger, _s, site, *_ = await direct_solar_setup(hass, source={"power": TOTAL}, site_amps=3.0)
    _set_total(hass, TOTAL, "unavailable")
    site._recompute()

    observation = _build_observation(site, charger.entry_id, now=0.0)
    verdict = SolarController(SolarConfig()).observe(observation)

    assert all(value is None for value in observation.signed_grid_w.values())
    assert verdict.reason == "no_basis_off"
    assert verdict.export_w is None and verdict.available_a is None


def _observation(*, cap: float | None, grid_w: float = -4200.0, phases=PHASES) -> SolarObservation:
    return SolarObservation(
        now=0.0,
        signed_grid_w=_split_total(grid_w, phases),
        voltage_v={p: 230.0 for p in PHASES},
        car_delivered_a={p: 0.0 for p in phases},
        battery_w=None,
        car_phases=phases,
        phase_cap_a=None if cap is None else {p: cap for p in phases},
    )


def test_the_cap_limits_the_figure_the_surplus_reasons_with() -> None:
    uncapped = SolarController(SolarConfig()).observe(_observation(cap=None))
    capped = SolarController(SolarConfig()).observe(_observation(cap=3.0))

    assert uncapped.available_a == pytest.approx(4200.0 / (3 * 230.0))
    assert capped.available_a == 3.0
    assert capped.available_w == pytest.approx(3.0 * 3 * 230.0)
    # Export is the grid fact, not the cap's.
    assert capped.export_w == 4200.0 and capped.net_grid_w == -4200.0


def test_a_cap_above_the_surplus_changes_nothing_and_a_missing_cap_is_no_basis() -> None:
    free = SolarController(SolarConfig()).observe(_observation(cap=30.0))
    unknown = SolarController(SolarConfig()).observe(
        replace(_observation(cap=None), phase_cap_a={"L1": 30.0, "L2": None, "L3": 30.0})
    )

    assert free.available_a == pytest.approx(4200.0 / (3 * 230.0))
    assert unknown.reason == "no_basis_off"


async def test_surplus_starts_the_charger_on_a_direct_site_from_the_total(hass: HomeAssistant) -> None:
    charger, _s, site, coordinator, clock, turn_on_calls = await direct_solar_setup(
        hass, source={"power": TOTAL, "power_export": EXPORT_HALF}
    )
    _set_total(hass, TOTAL, 0.0)
    _set_total(hass, EXPORT_HALF, 4200.0)

    await tick_site(hass, site)
    assert coordinator.state.state == "arming"
    assert coordinator.state.export_w == 4200.0
    assert coordinator.state.net_grid_w == -4200.0
    clock.value = 125.0
    await tick_site(hass, site)

    assert coordinator.state.action == "start"
    assert len(turn_on_calls) == 1
    assert coordinator.state.requested_a == 6.0


async def test_headroom_that_cannot_carry_the_minimum_keeps_solar_off_whatever_the_export(
    hass: HomeAssistant,
) -> None:
    # 24 A of house load on each phase leaves no headroom under a 25 A fuse with a 1 A margin.
    _c, _s, site, coordinator, clock, turn_on_calls = await direct_solar_setup(
        hass, source={"power": TOTAL}, site_amps=24.0
    )
    _set_total(hass, TOTAL, -6000.0)

    await tick_site(hass, site)
    clock.value = 200.0
    await tick_site(hass, site)

    assert coordinator.state.state == "off"
    assert not turn_on_calls


async def test_hybrid_reads_the_total_exactly_as_solar_does(hass: HomeAssistant) -> None:
    _c, _s, site, coordinator, clock, turn_on_calls = await direct_solar_setup(
        hass, source={"power": TOTAL}, strategy=STRATEGY_HYBRID
    )
    _set_total(hass, TOTAL, -4200.0)

    await tick_site(hass, site)
    clock.value = 125.0
    await tick_site(hass, site)

    assert coordinator.state.state == "on"


async def test_a_derived_site_keeps_its_per_phase_power_and_ignores_a_total(hass: HomeAssistant) -> None:
    from .world import set_site_power_w, solar_setup

    charger, site_entry, _controller, coordinator, clock, turn_on_calls, _off = await solar_setup(hass)
    site = controller_of(hass, site_entry.entry_id)
    site.config[CONF_GRID_POWER_SOURCE] = {"power": TOTAL}
    _set_total(hass, TOTAL, 5000.0)  # an import the per-phase figures contradict
    set_site_power_w(hass, "solar_site", -1400.0)
    site._recompute()
    observation = _build_observation(site, charger.entry_id, now=0.0)

    assert observation.phase_cap_a is None
    assert observation.signed_grid_w["L1"] == -1400.0
    assert observation.voltage_v["L1"] == 230.0
    assert site.grid_power_snapshot()["used_for_surplus"] is False


# ---- the entity-config API ----------------------------------------------------------------------


def _field(config: dict[str, Any], name: str) -> dict[str, Any]:
    return next(entry for entry in config["fields"] if entry["field"] == name)


async def test_the_site_fields_describe_the_total_as_two_optional_power_sensors(
    hass: HomeAssistant, hass_ws_client
) -> None:
    charger, *_ = await direct_solar_setup(hass, source={"power": TOTAL, "power_export": EXPORT_HALF})
    client = await admin(hass, hass_ws_client)

    config = (await ws_call(client, get_message(charger.entry_id)))["result"]["config"]

    for name, entity_id in (("grid_power_source_power", TOTAL), ("grid_power_source_power_export", EXPORT_HALF)):
        field = _field(config, name)
        assert field["scope"] == "site" and field["kind"] == "entity"
        assert field["required"] is False and field["writable"] is True
        assert field["allowed_domains"] == ["sensor"] and field["allowed_device_classes"] == ["power"]
        assert field["current"]["entity_id"] == entity_id


async def test_the_total_is_written_changed_and_cleared_through_the_api(
    hass: HomeAssistant, hass_ws_client
) -> None:
    charger, site_entry, *_ = await direct_solar_setup(hass)
    total = register(hass, "sensor", "api_total_w", unit_of_measurement="W", device_class="power")
    export = register(hass, "sensor", "api_export_w", unit_of_measurement="W", device_class="power")
    client = await admin(hass, hass_ws_client)

    async def update(changes: dict[str, Any], expected: dict[str, Any] | None = None) -> dict[str, Any]:
        answer = await ws_call(
            client, update_entity_config_message(charger.entry_id, scope="site", expected=expected, changes=changes)
        )
        return answer["result"]

    def stored() -> Any:
        return hass.config_entries.async_get_entry(site_entry.entry_id).data.get(CONF_GRID_POWER_SOURCE)

    result = await update({"grid_power_source_power": total}, {"grid_power_source_power": ""})
    assert result["ok"] is True
    assert stored() == {"power": total}
    assert _field(result["config"], "grid_power_source_power")["current"]["entity_id"] == total

    await update({"grid_power_source_power_export": export})
    assert stored() == {"power": total, "power_export": export}

    await update({"grid_power_source_power_export": ""})
    assert stored() == {"power": total}

    await update({"grid_power_source_power": ""})
    assert stored() is None


async def test_a_stale_expectation_of_the_total_is_a_conflict(hass: HomeAssistant, hass_ws_client) -> None:
    charger, site_entry, *_ = await direct_solar_setup(hass, source={"power": TOTAL})
    client = await admin(hass, hass_ws_client)

    answer = await ws_call(
        client,
        update_entity_config_message(
            charger.entry_id,
            scope="site",
            expected={"grid_power_source_power": "sensor.other"},
            changes={"grid_power_source_power": ""},
        ),
    )

    assert answer["result"]["error"] == "spotnav_conflict"
    assert hass.config_entries.async_get_entry(site_entry.entry_id).data[CONF_GRID_POWER_SOURCE] == {"power": TOTAL}


async def test_an_export_half_without_the_import_half_is_refused(hass: HomeAssistant, hass_ws_client) -> None:
    charger, site_entry, *_ = await direct_solar_setup(hass)
    export = register(hass, "sensor", "api_export_w", unit_of_measurement="W")
    client = await admin(hass, hass_ws_client)

    answer = await ws_call(
        client,
        update_entity_config_message(charger.entry_id, scope="site", changes={"grid_power_source_power_export": export}),
    )

    result = answer["result"]
    assert result["ok"] is False
    assert {"field": "grid_power_source_power", "code": "required"} in result["field_errors"]
    assert CONF_GRID_POWER_SOURCE not in hass.config_entries.async_get_entry(site_entry.entry_id).data


async def test_a_missing_entity_or_a_non_sensor_is_refused(hass: HomeAssistant, hass_ws_client) -> None:
    charger, *_ = await direct_solar_setup(hass)
    switch = register(hass, "switch", "not_a_sensor")
    client = await admin(hass, hass_ws_client)

    missing = await ws_call(
        client,
        update_entity_config_message(
            charger.entry_id, scope="site", changes={"grid_power_source_power": "sensor.nope"}
        ),
    )
    wrong = await ws_call(
        client,
        update_entity_config_message(charger.entry_id, scope="site", changes={"grid_power_source_power": switch}),
    )

    assert {"field": "grid_power_source_power", "code": "entity_not_found"} in missing["result"]["field_errors"]
    assert {"field": "grid_power_source_power", "code": "wrong_domain"} in wrong["result"]["field_errors"]
