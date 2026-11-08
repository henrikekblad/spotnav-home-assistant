"""A derived site whose phases have a current entity but no voltage entity (a delta meter, as in HA core's
`solaredge_modbus`): the site's nominal phase voltage stands in, the meter's own current is the fuse's |I|, the
power gives the direction. A phase without a current still needs a voltage."""

from __future__ import annotations

import math

from homeassistant.core import HomeAssistant

from custom_components.spotnav.const import CONF_DERIVED_ENTITIES, CONF_MEASUREMENT_MODE, MEASUREMENT_MODE_DERIVED
from custom_components.spotnav.site.site_capacity import PHASES

from .helpers import make_site_entry
from .messages import field, get_message, register, update_entity_config_message
from .world import admin, controller_of, setup_charger_and_site, ws_call


def _meter(hass: HomeAssistant, prefix: str, powers, currents) -> dict[str, dict[str, str]]:
    entities: dict[str, dict[str, str]] = {}
    for phase, watts, amps in zip(PHASES, powers, currents):
        power_id, current_id = f"sensor.{prefix}_p_{phase}", f"sensor.{prefix}_i_{phase}"
        hass.states.async_set(power_id, str(watts), {"unit_of_measurement": "W"})
        hass.states.async_set(current_id, str(amps), {"unit_of_measurement": "A"})
        entities[phase] = {"power": power_id, "current": current_id}
    return entities


async def test_the_nominal_voltage_stands_in_and_the_current_is_the_fuses(hass: HomeAssistant) -> None:
    derived = _meter(hass, "d1", (-4600.0, 300.0, 700.0), (20.0, 1.4, 3.2))
    site = make_site_entry(
        hass,
        entry_id="delta_site",
        measurement_mode=MEASUREMENT_MODE_DERIVED,
        derived_entities=derived,
        extra_data={"voltage_between_phases_v": 400},
    )
    assert await hass.config_entries.async_setup(site.entry_id)
    await hass.async_block_till_done()
    controller = controller_of(hass, site.entry_id)

    measurement = controller._read_derived_entities()
    for phase in PHASES:
        reading = measurement.get(phase)
        assert reading.voltage_nominal is True
        assert reading.voltage_v.value == 400 / math.sqrt(3)
        assert reading.voltage_v.age_s == 0.0 and reading.voltage_v.problem is None

    controller._recompute(schedule_apply=False)
    result = controller.result
    # The phase that exports 4.6 kW reads its |I| of 20 A, from the meter's own current.
    assert result.phase_current_basis == {phase: "measured" for phase in PHASES}
    assert result.measured_phase_current_a["L1"] == 20.0
    assert result.measured_phase_current_a["L3"] == 3.2
    # The direction is the power's: L1 exports, L2 and L3 import.
    assert result.phase_direction_source == {phase: "active_power" for phase in PHASES}
    assert result.phase_voltage_v["L1"] == 400 / math.sqrt(3)


async def test_a_phase_with_a_voltage_entity_keeps_reading_it(hass: HomeAssistant) -> None:
    derived = _meter(hass, "d2", (100.0, 100.0, 100.0), (1.0, 1.0, 1.0))
    for phase in PHASES:
        hass.states.async_set(f"sensor.d2_v_{phase}", "231", {"unit_of_measurement": "V"})
        derived[phase]["voltage"] = f"sensor.d2_v_{phase}"
    site = make_site_entry(hass, entry_id="wye_site", measurement_mode=MEASUREMENT_MODE_DERIVED, derived_entities=derived)
    assert await hass.config_entries.async_setup(site.entry_id)
    await hass.async_block_till_done()

    reading = controller_of(hass, site.entry_id)._read_derived_entities().get("L1")
    assert reading.voltage_nominal is False and reading.voltage_v.value == 231.0


async def test_a_phase_without_current_or_voltage_is_unusable(hass: HomeAssistant) -> None:
    derived = _meter(hass, "d3", (100.0, 100.0, 100.0), (1.0, 1.0, 1.0))
    del derived["L2"]["current"]
    site = make_site_entry(hass, entry_id="bare_site", measurement_mode=MEASUREMENT_MODE_DERIVED, derived_entities=derived)
    assert await hass.config_entries.async_setup(site.entry_id)
    await hass.async_block_till_done()

    reading = controller_of(hass, site.entry_id)._read_derived_entities().get("L2")
    assert reading.voltage_nominal is False
    assert reading.voltage_v.value is None and reading.voltage_v.problem == "missing"


async def test_the_editor_accepts_a_phase_with_current_and_no_voltage_but_not_power_alone(
    hass: HomeAssistant, hass_ws_client
) -> None:
    charger, site = await setup_charger_and_site(hass)
    client = await admin(hass, hass_ws_client)
    changes = {"measurement_mode": MEASUREMENT_MODE_DERIVED}
    for phase in PHASES:
        changes[f"derived_{phase}_power"] = register(hass, "sensor", f"e_{phase}_p", device_class="power")
        changes[f"derived_{phase}_current"] = register(hass, "sensor", f"e_{phase}_i", device_class="current")

    ok = (await ws_call(client, update_entity_config_message(charger.entry_id, scope="site", changes=changes)))["result"]
    assert ok["ok"] is True
    data = hass.config_entries.async_get_entry(site.entry_id).data
    assert data[CONF_MEASUREMENT_MODE] == MEASUREMENT_MODE_DERIVED
    assert data[CONF_DERIVED_ENTITIES]["L2"] == {"power": changes["derived_L2_power"], "current": changes["derived_L2_current"]}
    # The editor says so: those voltage fields are no longer required, and the others still are.
    read = (await ws_call(client, get_message(charger.entry_id)))["result"]
    assert all(field(read, f"derived_{phase}_voltage")["required"] is False for phase in PHASES)

    # Power alone on a phase still needs a voltage.
    cleared = {"derived_L3_current": ""}
    refused = (await ws_call(client, update_entity_config_message(charger.entry_id, scope="site", changes=cleared)))["result"]
    assert refused["error"] == "spotnav_invalid_value"
    assert [item["field"] for item in refused["field_errors"]] == ["derived_L3_voltage"]
    read = (await ws_call(client, get_message(charger.entry_id)))["result"]
    assert field(read, "derived_L3_voltage")["required"] is False  # unchanged: nothing was written
