"""The site controller's reading of signed currents, import/export pairs, inversion, optional current,
apparent and reactive power, and the estimated state it exposes -- through real states and real
config entries (`site/site_capacity_controller.py`)."""

from __future__ import annotations

import math

import pytest
from homeassistant.core import HomeAssistant

from custom_components.spotnav.const import (
    CONF_BATTERY_AGGREGATE_POWER_ENTITY,
    CONF_BATTERY_DISCHARGE_POWER_ENTITY,
    CONF_BATTERY_POWER_INVERTED,
    CONF_GRID_POWER_INVERTED,
    CONF_SITE_CURRENT_SIGNED,
    MEASUREMENT_MODE_DERIVED,
)
from custom_components.spotnav.site.measurement_source import PhaseMeasurementSource, source_to_dict

from .helpers import make_site_entry, set_current_sensor
from .world import controller_of

PHASES = ("L1", "L2", "L3")
VOLTAGE = 230.0


def power(hass: HomeAssistant, entity_id: str, watts: float, unit: str = "W") -> None:
    hass.states.async_set(entity_id, str(watts), {"unit_of_measurement": unit})


def volts(hass: HomeAssistant, entity_id: str, value: float = VOLTAGE) -> None:
    hass.states.async_set(entity_id, str(value), {"unit_of_measurement": "V"})


async def site_with(hass: HomeAssistant, entry_id: str, derived: dict, **kwargs):
    entry = make_site_entry(
        hass,
        entry_id=entry_id,
        charger_entry_ids=[],
        measurement_mode=MEASUREMENT_MODE_DERIVED,
        derived_entities=derived,
        **kwargs,
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry, controller_of(hass, entry.entry_id)


# ---- signed current ----------------------------------------------------------------------------


async def test_a_signed_direct_site_reads_an_exporting_phase_as_its_magnitude(hass: HomeAssistant) -> None:
    for phase, amps in zip(PHASES, (-12.0, 4.0, -3.5)):
        set_current_sensor(hass, f"sensor.hw_{phase.lower()}", amps)
    entry = make_site_entry(
        hass,
        entry_id="signed_direct",
        charger_entry_ids=[],
        direct_entities={phase: f"sensor.hw_{phase.lower()}" for phase in PHASES},
        extra_data={CONF_SITE_CURRENT_SIGNED: True},
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = controller_of(hass, entry.entry_id).result

    assert result.state == "observing"
    assert dict(result.measured_phase_current_a) == {"L1": 12.0, "L2": 4.0, "L3": 3.5}
    assert dict(result.phase_current_basis) == {phase: "measured" for phase in PHASES}


async def test_an_unsigned_direct_site_marks_a_negative_current_invalid(hass: HomeAssistant) -> None:
    for phase, amps in zip(PHASES, (-12.0, 4.0, 3.5)):
        set_current_sensor(hass, f"sensor.un_{phase.lower()}", amps)
    entry = make_site_entry(
        hass,
        entry_id="unsigned_direct",
        charger_entry_ids=[],
        direct_entities={phase: f"sensor.un_{phase.lower()}" for phase in PHASES},
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert controller_of(hass, entry.entry_id).result.state == "invalid_measurements"


async def test_the_signed_flag_also_applies_to_a_stored_attributes_source(hass: HomeAssistant) -> None:
    hass.states.async_set(
        "sensor.eq", "9", {"unit_of_measurement": "A", "L1": -9.0, "L2": 4.0, "L3": -1.0}
    )
    source = source_to_dict(
        PhaseMeasurementSource(
            kind="attributes", entity_id="sensor.eq", attributes={p: p for p in PHASES}, attribute_unit_override="A"
        )
    )
    entry = make_site_entry(
        hass,
        entry_id="signed_attrs",
        charger_entry_ids=[],
        site_current_source=source,
        direct_entities={},
        extra_data={CONF_SITE_CURRENT_SIGNED: True},
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert dict(controller_of(hass, entry.entry_id).result.measured_phase_current_a) == {
        "L1": 9.0,
        "L2": 4.0,
        "L3": 1.0,
    }


# ---- derived: pairs, inversion, optional sources ------------------------------------------------


def split_entities(hass: HomeAssistant, prefix: str, imports: dict, exports: dict) -> dict:
    derived = {}
    for phase in PHASES:
        low = phase.lower()
        derived[phase] = {
            "power": f"sensor.{prefix}_import_{low}",
            "power_export": f"sensor.{prefix}_export_{low}",
            "voltage": f"sensor.{prefix}_voltage_{low}",
        }
        power(hass, derived[phase]["power"], imports[phase], "kW")
        power(hass, derived[phase]["power_export"], exports[phase], "kW")
        volts(hass, derived[phase]["voltage"])
    return derived


async def test_an_import_export_pair_is_one_signed_power_and_the_current_is_estimated(hass: HomeAssistant) -> None:
    derived = split_entities(
        hass,
        "dsmr",
        imports={"L1": 2.3, "L2": 0.0, "L3": 0.0},
        exports={"L1": 0.0, "L2": 1.15, "L3": 0.0},
    )
    _entry, controller = await site_with(hass, "pair_site", derived)

    result = controller.result

    assert result.state == "observing"
    assert result.phase_signed_active_power_w["L1"] == pytest.approx(2300.0)
    assert result.phase_signed_active_power_w["L2"] == pytest.approx(-1150.0)
    assert result.phase_signed_active_power_w["L3"] == pytest.approx(0.0)
    assert result.current_estimated is True
    assert result.measured_phase_current_a["L2"] == pytest.approx(1150.0 / (VOLTAGE * 0.9))


async def test_a_pair_with_an_unavailable_half_is_not_read_as_zero(hass: HomeAssistant) -> None:
    derived = split_entities(
        hass, "gone", imports={"L1": 2.3, "L2": 1.0, "L3": 1.0}, exports={"L1": 0.0, "L2": 0.0, "L3": 0.0}
    )
    hass.states.async_set(derived["L1"]["power_export"], "unavailable", {"unit_of_measurement": "kW"})
    _entry, controller = await site_with(hass, "pair_gone", derived)

    assert controller.result.state == "missing_measurements"
    assert controller.result.phase_signed_active_power_w["L1"] is None


async def test_the_site_state_exposes_the_estimate_to_the_card_and_diagnostics(hass: HomeAssistant) -> None:
    derived = split_entities(
        hass, "attr", imports={"L1": 1.0, "L2": 1.0, "L3": 1.0}, exports={"L1": 0.0, "L2": 0.0, "L3": 0.0}
    )
    entry, _controller = await site_with(hass, "attr_site", derived)

    state = hass.states.get("sensor.site_capacity_state")
    if state is None:
        state = next(s for s in hass.states.async_all("sensor") if "current_estimated" in s.attributes)
    assert state.attributes["current_estimated"] is True
    assert state.attributes["estimated_power_factor"] == 0.9
    assert state.attributes["phase_current_basis"] == {phase: "estimated" for phase in PHASES}


async def test_grid_power_inversion_makes_an_export_positive_meter_import_positive(hass: HomeAssistant) -> None:
    derived = {}
    for phase, watts in zip(PHASES, (-2300.0, 1150.0, 0.0)):
        low = phase.lower()
        derived[phase] = {"power": f"sensor.hu_p_{low}", "voltage": f"sensor.hu_v_{low}"}
        power(hass, derived[phase]["power"], watts)
        volts(hass, derived[phase]["voltage"])
    _entry, controller = await site_with(
        hass, "invert_site", derived, extra_data={CONF_GRID_POWER_INVERTED: True}
    )

    signed = controller.result.phase_signed_active_power_w

    assert signed["L1"] == 2300.0  # the meter's export-positive -2300 W is an import of 2300 W
    assert signed["L2"] == -1150.0


async def test_measured_current_and_apparent_power_take_precedence_over_the_estimate(hass: HomeAssistant) -> None:
    derived = {}
    for phase in PHASES:
        low = phase.lower()
        derived[phase] = {
            "power": f"sensor.sh_p_{low}",
            "voltage": f"sensor.sh_v_{low}",
            "current": f"sensor.sh_i_{low}",
            "apparent_power": f"sensor.sh_s_{low}",
        }
        power(hass, derived[phase]["power"], 2000.0)
        volts(hass, derived[phase]["voltage"])
        set_current_sensor(hass, derived[phase]["current"], -9.5)  # signed, export negative
        hass.states.async_set(derived[phase]["apparent_power"], "2300", {"unit_of_measurement": "VA"})
    _entry, controller = await site_with(
        hass, "exact_site", derived, extra_data={CONF_SITE_CURRENT_SIGNED: True}
    )

    result = controller.result

    assert dict(result.phase_current_basis) == {phase: "measured" for phase in PHASES}
    assert result.measured_phase_current_a["L1"] == 9.5
    assert result.current_estimated is False

    # Without the measured current, S / U takes over.
    for phase in PHASES:
        hass.states.async_remove(derived[phase]["current"])
    controller._recompute()
    # A configured but vanished current entity is not silently replaced: the apparent power is
    # used only because it is also configured, never an estimate.
    assert dict(controller.result.phase_current_basis) == {phase: "apparent" for phase in PHASES}
    assert controller.result.measured_phase_current_a["L1"] == pytest.approx(2300.0 / VOLTAGE)


async def test_the_owners_p_q_v_site_stores_and_computes_exactly_as_before(hass: HomeAssistant) -> None:
    derived = {}
    for phase, watts in zip(PHASES, (3000.0, 500.0, -800.0)):
        low = phase.lower()
        derived[phase] = {
            "power": f"sensor.sg_p_{low}",
            "reactive_power": f"sensor.sg_q_{low}",
            "voltage": f"sensor.sg_v_{low}",
        }
        power(hass, derived[phase]["power"], watts)
        hass.states.async_set(derived[phase]["reactive_power"], "400", {"unit_of_measurement": "var"})
        volts(hass, derived[phase]["voltage"])
    _entry, controller = await site_with(hass, "sigen_like", derived)

    result = controller.result

    assert result.state == "observing"
    assert dict(result.phase_current_basis) == {phase: "reactive" for phase in PHASES}
    assert result.current_estimated is False
    assert result.measured_phase_current_a["L1"] == pytest.approx(math.hypot(3000.0, 400.0) / VOLTAGE)


async def test_a_state_change_of_an_export_half_recomputes_the_site(hass: HomeAssistant) -> None:
    derived = split_entities(
        hass, "live", imports={"L1": 1.0, "L2": 1.0, "L3": 1.0}, exports={"L1": 0.0, "L2": 0.0, "L3": 0.0}
    )
    _entry, controller = await site_with(hass, "live_site", derived)
    before = controller.result.phase_signed_active_power_w["L1"]

    power(hass, derived["L1"]["power_export"], 3.0, "kW")
    await hass.async_block_till_done()

    assert before == pytest.approx(1000.0)
    assert controller.result.phase_signed_active_power_w["L1"] == pytest.approx(-2000.0)


# ---- battery ------------------------------------------------------------------------------------


async def battery_site(hass: HomeAssistant, entry_id: str, **extra):
    derived = {}
    for phase in PHASES:
        low = phase.lower()
        derived[phase] = {"power": f"sensor.{entry_id}_p_{low}", "voltage": f"sensor.{entry_id}_v_{low}"}
        power(hass, derived[phase]["power"], 1000.0)
        volts(hass, derived[phase]["voltage"])
    return await site_with(hass, entry_id, derived, extra_data=extra)


async def test_a_discharge_positive_battery_is_negated_to_charge_positive(hass: HomeAssistant) -> None:
    power(hass, "sensor.bat_power", 1800.0)  # discharging 1.8 kW as the integration reports it
    _entry, controller = await battery_site(
        hass,
        "bat_inv",
        **{CONF_BATTERY_AGGREGATE_POWER_ENTITY: "sensor.bat_power", CONF_BATTERY_POWER_INVERTED: True},
    )

    assert controller.battery_aggregate_power().value == -1800.0


async def test_a_charge_discharge_pair_is_one_signed_battery_power(hass: HomeAssistant) -> None:
    power(hass, "sensor.bat_charge", 2500.0)
    power(hass, "sensor.bat_discharge", 0.0)
    _entry, controller = await battery_site(
        hass,
        "bat_pair",
        **{
            CONF_BATTERY_AGGREGATE_POWER_ENTITY: "sensor.bat_charge",
            CONF_BATTERY_DISCHARGE_POWER_ENTITY: "sensor.bat_discharge",
        },
    )
    assert controller.battery_aggregate_power().value == 2500.0

    power(hass, "sensor.bat_charge", 0.0)
    power(hass, "sensor.bat_discharge", 900.0)
    await hass.async_block_till_done()
    assert controller.battery_aggregate_power().value == -900.0


async def test_an_unavailable_half_of_the_battery_pair_is_unusable_not_zero(hass: HomeAssistant) -> None:
    power(hass, "sensor.bat2_charge", 2500.0)
    hass.states.async_set("sensor.bat2_discharge", "unavailable", {"unit_of_measurement": "W"})
    _entry, controller = await battery_site(
        hass,
        "bat_pair_gone",
        **{
            CONF_BATTERY_AGGREGATE_POWER_ENTITY: "sensor.bat2_charge",
            CONF_BATTERY_DISCHARGE_POWER_ENTITY: "sensor.bat2_discharge",
        },
    )

    value = controller.battery_aggregate_power()

    assert value is not None and value.value is None and value.problem == "missing"
