"""Detection and the readings of a charger's lifetime energy register ask one table.

DEFA Power's lifetime register (`meter_value`) is `state_class: total`. Detection accepted it, because the
profile names it; every reading refused it, because only `total_increasing` was read, so a DEFA charger had a
register that never gave a value. Both now ask `charger_profiles.lifetime_register_state_classes`: the
profile-named lifetime register is read with the classes its profile allows, any other `total` sensor (the
per-session `transaction_meter_value` among them) still is not.
"""

from __future__ import annotations

from homeassistant.core import HomeAssistant

from custom_components.spotnav.flows.charger_detection import detect_charger
from custom_components.spotnav.vehicles.soc_estimate import read_energy_register_kwh

from ..charger_helpers import adapter_for
from ..charger_shapes import register_shape, SHAPES


async def test_the_defa_register_detection_offers_is_the_one_its_readings_take(hass: HomeAssistant) -> None:
    ids = register_shape(hass, SHAPES["defa_power"])
    found = detect_charger(hass, ids["device_id"])

    assert found.energy_register == "sensor.defa_power_meter_value"
    assert read_energy_register_kwh(hass, found.energy_register) == 2210.5
    assert adapter_for(hass, found).energy_register_kwh() == 2210.5


async def test_a_session_counter_or_an_unnamed_total_sensor_is_still_refused(hass: HomeAssistant) -> None:
    register_shape(hass, SHAPES["defa_power"])
    assert read_energy_register_kwh(hass, "sensor.defa_power_transaction_meter_value") is None

    hass.states.async_set(
        "sensor.some_total", "12.0", {"device_class": "energy", "state_class": "total", "unit_of_measurement": "kWh"}
    )
    assert read_energy_register_kwh(hass, "sensor.some_total") is None
