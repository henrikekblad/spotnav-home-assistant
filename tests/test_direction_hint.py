"""What the card says to a direct site that cannot know the phase direction (`site_detection.direction_hint`):
use the meter's per-phase power where its candidates have it, say what is wrong where the meter is known to report
the current as a size, and give the general advice otherwise."""

from __future__ import annotations

from homeassistant.core import HomeAssistant

from custom_components.spotnav.api.dashboard import _active_control_hint
from custom_components.spotnav.site.site_detection import direction_hint

from .site_registry import materialize
from .test_site_detection import (
    dsmr_meter,
    solaredge_modbus_core,
    solaredge_modbus_multi,
    tibber_pulse,
)
from .world import controller_of, setup_charger_and_site


def _hint(registry, object_ids: list[str]) -> str | None:
    detection = registry.detect()
    return direction_hint(detection, [f"sensor.{object_id}" for object_id in object_ids])


def test_a_meter_with_per_phase_power_is_pointed_at_it_whether_or_not_it_has_voltages() -> None:
    delta = [f"meter_1_current_phase_{ph}" for ph in "abc"]
    assert _hint(solaredge_modbus_core(wye=False), delta) == "use_power"
    assert _hint(solaredge_modbus_core(wye=True), delta) == "use_power"
    assert _hint(solaredge_modbus_multi(), [f"se_m1_current_{ph}" for ph in "abc"]) == "use_power"


def test_a_meter_known_to_report_magnitudes_with_nothing_to_derive_from_is_not_told_to_sign_it() -> None:
    # dsmr's per-phase currents have no sign and there is no per-phase voltage to derive from here.
    registry = dsmr_meter()
    registry.entities = [e for e in registry.entities if "voltage" not in e.unique_id and "active_power" not in e.unique_id]
    assert _hint(registry, [f"dsmr_current_l{n}" for n in "123"]) == "magnitude"


def test_a_signed_meter_and_an_unknown_one_get_the_general_advice() -> None:
    # Tibber's currents are signed, and nothing in the registry matches an entity picked by hand.
    assert _hint(tibber_pulse(), [f"tibber_current_l{n}" for n in "123"]) is None
    assert _hint(tibber_pulse(), ["some_other_sensor"]) is None
    assert direction_hint(solaredge_modbus_core().detect(), []) is None


async def test_the_dashboard_hint_is_only_for_the_direction_reason_and_reads_the_site_meters(
    hass: HomeAssistant,
) -> None:
    made = materialize(hass, solaredge_modbus_core(wye=False))
    direct = {phase: made[f"meter_1_current_phase_{ph}"] for phase, ph in zip(("L1", "L2", "L3"), "abc")}
    charger, site = await setup_charger_and_site(hass, direct_entities=direct)
    entry = hass.config_entries.async_get_entry(site.entry_id)
    controller = controller_of(hass, site.entry_id)

    assert _active_control_hint(hass, entry, controller, "site_direction_unknown") == "use_power"
    assert _active_control_hint(hass, entry, controller, None) is None
    assert _active_control_hint(hass, entry, controller, "no_commandable_charger") is None


async def test_the_dashboard_hint_falls_back_for_a_meter_picked_by_hand(hass: HomeAssistant) -> None:
    charger, site = await setup_charger_and_site(hass)
    entry = hass.config_entries.async_get_entry(site.entry_id)
    controller = controller_of(hass, site.entry_id)

    assert _active_control_hint(hass, entry, controller, "site_direction_unknown") is None
