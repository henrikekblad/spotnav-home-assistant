"""The measurement-source model wired into `SiteCapacityController`: both site-current representations, per-charger optional measured current (all-or-nothing), and the capability snapshot."""

from __future__ import annotations

import time
from datetime import timedelta

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed, async_mock_service

from custom_components.spotnav.const import (
    CONF_BATTERY_AGGREGATE_POWER_ENTITY,
    CONF_MEASURED_CURRENT_SOURCE,
    DEFAULT_MAX_AGE_S,
    DOMAIN,
    MEASUREMENT_MODE_DERIVED,
    SITE_RECOMPUTE_INTERVAL_S,
)
from custom_components.spotnav.flows.labels import power_sensor_entity_options
from custom_components.spotnav.site.measurement_source import (
    PhaseMeasurementSource,
    source_to_dict,
)

from .helpers import make_entry, make_site_entry, set_current_sensor
from .world import separate_entities_source
from .world import controller_of


async def test_attribute_based_site_current_source_is_read_through_the_generic_model(
    hass: HomeAssistant,
) -> None:
    hass.states.async_set(
        "sensor.grid_meter",
        "unknown",
        {"unit_of_measurement": "A", "L1": 5.0, "L2": 6.0, "L3": 7.0},
    )
    source = PhaseMeasurementSource(
        kind="attributes", entity_id="sensor.grid_meter", attributes={"L1": "L1", "L2": "L2", "L3": "L3"},
        attribute_unit_override="A",
    )
    entry = make_site_entry(
        hass, entry_id="site_attrs", charger_entry_ids=[], site_current_source=source_to_dict(source)
    )

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    controller = controller_of(hass, entry.entry_id)
    assert controller.result.state == "observing"
    assert controller.result.measured_phase_current_a == {"L1": 5.0, "L2": 6.0, "L3": 7.0}


async def test_site_current_source_takes_precedence_over_legacy_direct_entities(
    hass: HomeAssistant,
) -> None:
    """When both are present, the explicit generic source wins over the site-current dict."""
    hass.states.async_set(
        "sensor.grid_meter2", "unknown", {"unit_of_measurement": "A", "L1": 1.0, "L2": 1.0, "L3": 1.0}
    )
    set_current_sensor(hass, "sensor.legacy_l1", 99)
    set_current_sensor(hass, "sensor.legacy_l2", 99)
    set_current_sensor(hass, "sensor.legacy_l3", 99)
    source = PhaseMeasurementSource(
        kind="attributes", entity_id="sensor.grid_meter2", attributes={"L1": "L1", "L2": "L2", "L3": "L3"},
        attribute_unit_override="A",
    )
    entry = make_site_entry(
        hass,
        entry_id="site_precedence",
        charger_entry_ids=[],
        direct_entities={"L1": "sensor.legacy_l1", "L2": "sensor.legacy_l2", "L3": "sensor.legacy_l3"},
        site_current_source=source_to_dict(source),
    )

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    controller = controller_of(hass, entry.entry_id)
    assert controller.result.measured_phase_current_a == {"L1": 1.0, "L2": 1.0, "L3": 1.0}


async def test_charger_measured_current_source_credits_headroom_when_all_phases_valid(
    hass: HomeAssistant,
) -> None:
    hass.states.async_set("switch.measured_charger", "off")
    async_mock_service(hass, "switch", "turn_on")
    charger = make_entry(
        hass, entry_id="charger_measured", charge_control="switch.measured_charger",
        current_limit=None, webhook_id="webhook-measured", title="Measured",
    )
    assert await hass.config_entries.async_setup(charger.entry_id)
    await hass.async_block_till_done()
    charger_controller = controller_of(hass, charger.entry_id)
    await charger_controller.async_start(amps=16)
    hass.states.async_set("switch.measured_charger", "on")

    for suffix in ("l1", "l2", "l3"):
        set_current_sensor(hass, f"sensor.measured_charger_{suffix}", 10)

    entry = make_site_entry(
        hass,
        entry_id="site_measured_credit",
        main_fuse_a=25.0,
        safety_margin_a=1.0,
        charger_entry_ids=[charger.entry_id],
        phase_wiring={
            charger.entry_id: {
                "phases": 3,
                "phase": None,
                CONF_MEASURED_CURRENT_SOURCE: separate_entities_source(
                        "sensor.measured_charger_l1", "sensor.measured_charger_l2", "sensor.measured_charger_l3"
                    ),
            }
        },
    )
    set_current_sensor(hass, "sensor.site_measured_credit_l1", 20)
    set_current_sensor(hass, "sensor.site_measured_credit_l2", 20)
    set_current_sensor(hass, "sensor.site_measured_credit_l3", 20)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    site_controller = controller_of(hass, entry.entry_id)
    # 20 A measured total minus the charger's own measured 10 A leaves 10 A "other load"; credited headroom would be 25-1-10=14 A, but that stays diagnostic only. The real headroom never credits any charger's current: 25 - 1 - 20 = 4 A.
    assert site_controller.result.calculated_headroom_after_ev_credit_a["L1"] == 14.0
    assert site_controller.result.phase_headroom_a["L1"] == 4.0


async def test_charger_measured_current_source_credits_nothing_if_one_phase_is_stale(
    hass: HomeAssistant,
) -> None:
    """The all-or-nothing rule of `site/site_capacity.py` survives the full controller wiring."""
    hass.states.async_set("switch.partial_charger", "off")
    async_mock_service(hass, "switch", "turn_on")
    charger = make_entry(
        hass, entry_id="charger_partial", charge_control="switch.partial_charger",
        current_limit=None, webhook_id="webhook-partial", title="Partial",
    )
    assert await hass.config_entries.async_setup(charger.entry_id)
    await hass.async_block_till_done()
    charger_controller = controller_of(hass, charger.entry_id)
    await charger_controller.async_start(amps=16)
    hass.states.async_set("switch.partial_charger", "on")

    set_current_sensor(hass, "sensor.partial_charger_l1", 10)
    set_current_sensor(hass, "sensor.partial_charger_l2", 10)
    # L3 entity does not exist at all: "missing", not just stale.

    entry = make_site_entry(
        hass,
        entry_id="site_measured_partial",
        main_fuse_a=25.0,
        safety_margin_a=1.0,
        charger_entry_ids=[charger.entry_id],
        phase_wiring={
            charger.entry_id: {
                "phases": 3,
                "phase": None,
                CONF_MEASURED_CURRENT_SOURCE: separate_entities_source(
                        "sensor.partial_charger_l1", "sensor.partial_charger_l2", "sensor.partial_charger_l3"
                    ),
            }
        },
    )
    set_current_sensor(hass, "sensor.site_measured_partial_l1", 20)
    set_current_sensor(hass, "sensor.site_measured_partial_l2", 20)
    set_current_sensor(hass, "sensor.site_measured_partial_l3", 20)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    site_controller = controller_of(hass, entry.entry_id)
    # No credit at all: 25 - 1 - 20 = 4 A, as if no measured source were configured.
    assert site_controller.result.phase_headroom_a["L1"] == 4.0


async def test_derived_mode_still_credits_nothing_even_with_a_charger_measured_source(
    hass: HomeAssistant,
) -> None:
    """Derived mode keeps stripping charger credit at the controller level, whatever populated `measured_current_a`."""
    hass.states.async_set("switch.derived_charger", "off")
    async_mock_service(hass, "switch", "turn_on")
    charger = make_entry(
        hass, entry_id="charger_derived", charge_control="switch.derived_charger",
        current_limit=None, webhook_id="webhook-derived", title="Derived",
    )
    assert await hass.config_entries.async_setup(charger.entry_id)
    await hass.async_block_till_done()
    charger_controller = controller_of(hass, charger.entry_id)
    await charger_controller.async_start(amps=16)
    hass.states.async_set("switch.derived_charger", "on")

    for suffix in ("l1", "l2", "l3"):
        set_current_sensor(hass, f"sensor.derived_charger_{suffix}", 10)

    derived_entities = {
        phase: {
            "power": f"sensor.derived_power_{phase.lower()}",
            "reactive_power": f"sensor.derived_reactive_{phase.lower()}",
            "voltage": f"sensor.derived_voltage_{phase.lower()}",
        }
        for phase in ("L1", "L2", "L3")
    }
    for phase in ("L1", "L2", "L3"):
        hass.states.async_set(
            derived_entities[phase]["power"], "4600", {"unit_of_measurement": "W"}
        )
        hass.states.async_set(
            derived_entities[phase]["reactive_power"], "0", {"unit_of_measurement": "var"}
        )
        hass.states.async_set(
            derived_entities[phase]["voltage"], "230", {"unit_of_measurement": "V"}
        )

    entry = make_site_entry(
        hass,
        entry_id="site_derived_measured",
        main_fuse_a=25.0,
        safety_margin_a=1.0,
        charger_entry_ids=[charger.entry_id],
        phase_wiring={
            charger.entry_id: {
                "phases": 3,
                "phase": None,
                CONF_MEASURED_CURRENT_SOURCE: separate_entities_source(
                        "sensor.derived_charger_l1", "sensor.derived_charger_l2", "sensor.derived_charger_l3"
                    ),
            }
        },
        measurement_mode=MEASUREMENT_MODE_DERIVED,
        derived_entities=derived_entities,
    )

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    site_controller = controller_of(hass, entry.entry_id)
    assert site_controller.result.state == "observing"
    # ~20 A apparent current per phase from 4600 W/230 V; no charger credit applies despite the configured source, so headroom reflects the full apparent current.
    assert site_controller.result.phase_headroom_a["L1"] < 25.0 - 1.0 - 10.0 + 0.5


async def test_signed_active_power_is_wired_from_a_real_negative_ha_reading(
    hass: HomeAssistant,
) -> None:
    """A negative-watt Home Assistant sensor (as reported during export) becomes a negative `phase_signed_active_power_w`, sign intact, through `_read_derived_entities` -> `calculate_site_capacity`.

    Purely diagnostic: `state` stays `observing`.
    """
    derived_entities = {
        phase: {
            "power": f"sensor.signed_power_{phase.lower()}",
            "reactive_power": f"sensor.signed_reactive_{phase.lower()}",
            "voltage": f"sensor.signed_voltage_{phase.lower()}",
        }
        for phase in ("L1", "L2", "L3")
    }
    for phase, watts in (("L1", "-4129"), ("L2", "-4275"), ("L3", "-4285")):
        hass.states.async_set(
            derived_entities[phase]["power"], watts, {"unit_of_measurement": "W"}
        )
        hass.states.async_set(
            derived_entities[phase]["reactive_power"], "0", {"unit_of_measurement": "var"}
        )
        hass.states.async_set(
            derived_entities[phase]["voltage"], "230", {"unit_of_measurement": "V"}
        )

    entry = make_site_entry(
        hass,
        entry_id="site_signed_power",
        main_fuse_a=25.0,
        safety_margin_a=1.0,
        charger_entry_ids=[],
        measurement_mode=MEASUREMENT_MODE_DERIVED,
        derived_entities=derived_entities,
    )

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    site_controller = controller_of(hass, entry.entry_id)
    assert site_controller.result.state == "observing"
    assert site_controller.result.phase_signed_active_power_w == {
        "L1": -4129.0,
        "L2": -4275.0,
        "L3": -4285.0,
    }
    assert all(
        reason is None for reason in site_controller.result.phase_signed_active_power_reason.values()
    )


async def test_capability_snapshot_reflects_a_healthy_configured_site(
    hass: HomeAssistant,
) -> None:
    entry = make_site_entry(hass, entry_id="site_capability", charger_entry_ids=[])
    set_current_sensor(hass, "sensor.site_capability_l1", 5)
    set_current_sensor(hass, "sensor.site_capability_l2", 5)
    set_current_sensor(hass, "sensor.site_capability_l3", 5)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    controller = controller_of(hass, entry.entry_id)
    snapshot = controller.capability_snapshot
    assert snapshot.site_measurement.health == "healthy"
    assert snapshot.load_balancing.active_available is False


async def test_site_state_entity_exposes_capability_attributes(hass: HomeAssistant) -> None:
    entry = make_site_entry(hass, entry_id="site_entity_capability", charger_entry_ids=[])
    set_current_sensor(hass, "sensor.site_entity_capability_l1", 5)
    set_current_sensor(hass, "sensor.site_entity_capability_l2", 5)
    set_current_sensor(hass, "sensor.site_entity_capability_l3", 5)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    state = hass.states.get("sensor.site_capacity_state")
    assert state.attributes["site_measurement_health"] == "healthy"
    assert state.attributes["active_load_balancing_available"] is False
    assert state.attributes["blocking_reasons"] == []


async def test_charger_proposed_current_entity_exposes_measured_and_setpoint(
    hass: HomeAssistant,
) -> None:
    hass.states.async_set("switch.entity_measured_charger", "off")
    async_mock_service(hass, "switch", "turn_on")
    charger = make_entry(
        hass, entry_id="charger_entity_measured", charge_control="switch.entity_measured_charger",
        current_limit=None, webhook_id="webhook-entity-measured", title="Entity measured",
    )
    assert await hass.config_entries.async_setup(charger.entry_id)
    await hass.async_block_till_done()
    charger_controller = controller_of(hass, charger.entry_id)
    await charger_controller.async_start(amps=16)
    hass.states.async_set("switch.entity_measured_charger", "on")

    for suffix in ("l1", "l2", "l3"):
        set_current_sensor(hass, f"sensor.entity_measured_charger_{suffix}", 8)

    entry = make_site_entry(
        hass,
        entry_id="site_entity_measured",
        charger_entry_ids=[charger.entry_id],
        phase_wiring={
            charger.entry_id: {
                "phases": 3,
                "phase": None,
                CONF_MEASURED_CURRENT_SOURCE: separate_entities_source(
                        "sensor.entity_measured_charger_l1", "sensor.entity_measured_charger_l2", "sensor.entity_measured_charger_l3"
                    ),
            }
        },
    )
    set_current_sensor(hass, "sensor.site_entity_measured_l1", 20)
    set_current_sensor(hass, "sensor.site_entity_measured_l2", 20)
    set_current_sensor(hass, "sensor.site_entity_measured_l3", 20)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(
        "sensor", DOMAIN, f"{entry.entry_id}_proposed_{charger.entry_id}"
    )
    assert entity_id is not None
    proposed = hass.states.get(entity_id)
    assert proposed is not None
    assert proposed.attributes["measured_charger_current_a"] == {"L1": 8.0, "L2": 8.0, "L3": 8.0}
    assert proposed.attributes["measured_charger_current_health"] == "healthy"
    assert proposed.attributes["setpoint_current_a"] is None


async def test_battery_aggregate_power_entity_feeds_the_two_tier_headroom_diagnostic(
    hass: HomeAssistant,
) -> None:
    entry = make_site_entry(
        hass,
        entry_id="site_battery",
        charger_entry_ids=[],
        measurement_mode=MEASUREMENT_MODE_DERIVED,
        derived_entities={
            phase: {
                "power": f"sensor.site_battery_power_{phase.lower()}",
                "reactive_power": f"sensor.site_battery_reactive_{phase.lower()}",
                "voltage": f"sensor.site_battery_voltage_{phase.lower()}",
            }
            for phase in ("L1", "L2", "L3")
        },
        battery_aggregate_power_entity="sensor.site_battery_power_total",
    )
    for phase in ("l1", "l2", "l3"):
        hass.states.async_set(
            f"sensor.site_battery_power_{phase}", "4600", {"unit_of_measurement": "W"}
        )
        hass.states.async_set(
            f"sensor.site_battery_reactive_{phase}", "0", {"unit_of_measurement": "var"}
        )
        hass.states.async_set(
            f"sensor.site_battery_voltage_{phase}", "230", {"unit_of_measurement": "V"}
        )
    hass.states.async_set("sensor.site_battery_power_total", "13800", {"unit_of_measurement": "W"})

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    controller = controller_of(hass, entry.entry_id)
    assert controller.result.state == "observing"
    assert abs(controller.result.phase_headroom_a["L1"] - 4.0) < 1e-6
    assert controller.result.battery_yield_basis == "assumed_equal_split"
    assert abs(controller.result.estimated_headroom_if_battery_yields_a["L1"] - 24.0) < 1e-6

    state = hass.states.get("sensor.site_capacity_state")
    assert state.attributes["battery_yield_basis"] == "assumed_equal_split"
    assert abs(state.attributes["estimated_headroom_if_battery_yields_a"]["L1"] - 24.0) < 1e-6
    assert abs(state.attributes["phase_headroom_a"]["L1"] - 4.0) < 1e-6


async def test_no_battery_entity_configured_reports_unknown_basis_through_the_entity(
    hass: HomeAssistant,
) -> None:
    entry = make_site_entry(hass, entry_id="site_no_battery", charger_entry_ids=[])
    set_current_sensor(hass, "sensor.site_no_battery_l1", 5)
    set_current_sensor(hass, "sensor.site_no_battery_l2", 5)
    set_current_sensor(hass, "sensor.site_no_battery_l3", 5)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    state = hass.states.get("sensor.site_capacity_state")
    assert state.attributes["battery_yield_basis"] == "unknown"
    assert all(v is None for v in state.attributes["estimated_headroom_if_battery_yields_a"].values())


async def test_options_flow_roundtrip_sets_and_clears_the_battery_entity(
    hass: HomeAssistant,
) -> None:
    entry = make_site_entry(hass, entry_id="site_battery_opts", charger_entry_ids=[])
    set_current_sensor(hass, "sensor.site_battery_opts_l1", 5)
    set_current_sensor(hass, "sensor.site_battery_opts_l2", 5)
    set_current_sensor(hass, "sensor.site_battery_opts_l3", 5)
    hass.states.async_set(
        "sensor.new_battery_power", "5000", {"unit_of_measurement": "W", "device_class": "power"}
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "main_fuse_a": 25.0,
            "safety_margin_a": 1.0,
            "measurement_mode": "direct_phase_current",
            "charger_entry_ids": [],
            "site_enabled": True,
            "change_measurement": True,
            "max_age_s": 120.0,
            "battery_aggregate_power_entity": "sensor.new_battery_power",
        },
    )
    assert result["step_id"] == "site_current_suggestions"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"choice": "manual"}
    )
    assert result["step_id"] == "site_details"
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()

    updated_entry = hass.config_entries.async_get_entry(entry.entry_id)
    assert updated_entry.data[CONF_BATTERY_AGGREGATE_POWER_ENTITY] == "sensor.new_battery_power"


def test_power_sensor_entity_options_label_identically_named_sensors_with_their_ids(
    hass: HomeAssistant,
) -> None:
    """Two power sensors with the same friendly name stay distinguishable in the home-battery picker: each label carries the entity id, and a non-power sensor on the same device is not offered."""
    hass.states.async_set(
        "sensor.sigen_plant_battery_power",
        "-1151",
        {"unit_of_measurement": "kW", "device_class": "power", "friendly_name": "Battery Power"},
    )
    hass.states.async_set(
        "sensor.sigen_plant_2_battery_power",
        "0",
        {"unit_of_measurement": "kW", "device_class": "power", "friendly_name": "Battery Power"},
    )
    hass.states.async_set(
        "sensor.sigen_battery_soc",
        "42",
        {"unit_of_measurement": "%", "device_class": "battery"},
    )

    labels = {option["value"]: option["label"] for option in power_sensor_entity_options(hass)}

    assert labels["sensor.sigen_plant_battery_power"] == (
        "Battery Power (sensor.sigen_plant_battery_power)"
    )
    assert labels["sensor.sigen_plant_2_battery_power"] == (
        "Battery Power (sensor.sigen_plant_2_battery_power)"
    )
    assert "sensor.sigen_battery_soc" not in labels


async def test_sensor_exposes_measured_margin_and_ev_credit_estimate_as_distinct_values(
    hass: HomeAssistant,
) -> None:
    """`phase_headroom_a` is a calculated value, not "the real measured fuse margin". The sensor exposes all three headroom numbers, and in derived mode the diagnostic EV-credit estimate differs from the real headroom used for the proposal."""
    hass.states.async_set("switch.margin_charger", "off")
    async_mock_service(hass, "switch", "turn_on")
    charger = make_entry(
        hass, entry_id="charger_margin", charge_control="switch.margin_charger",
        current_limit=None, webhook_id="webhook-margin", title="Margin charger",
    )
    assert await hass.config_entries.async_setup(charger.entry_id)
    await hass.async_block_till_done()
    charger_controller = controller_of(hass, charger.entry_id)
    await charger_controller.async_start(amps=16)
    hass.states.async_set("switch.margin_charger", "on")

    for suffix in ("l1", "l2", "l3"):
        set_current_sensor(hass, f"sensor.margin_charger_{suffix}", 8.9)

    entry = make_site_entry(
        hass,
        entry_id="site_margin",
        main_fuse_a=25.0,
        safety_margin_a=1.0,
        charger_entry_ids=[charger.entry_id],
        phase_wiring={
            charger.entry_id: {
                "phases": 3,
                "phase": None,
                CONF_MEASURED_CURRENT_SOURCE: separate_entities_source(
                        "sensor.margin_charger_l1", "sensor.margin_charger_l2", "sensor.margin_charger_l3"
                    ),
            }
        },
        measurement_mode=MEASUREMENT_MODE_DERIVED,
        derived_entities={
            phase: {
                "power": f"sensor.site_margin_power_{phase.lower()}",
                "reactive_power": f"sensor.site_margin_reactive_{phase.lower()}",
                "voltage": f"sensor.site_margin_voltage_{phase.lower()}",
            }
            for phase in ("L1", "L2", "L3")
        },
    )
    for phase in ("l1", "l2", "l3"):
        hass.states.async_set(
            f"sensor.site_margin_power_{phase}", str(230.0 * 20.0), {"unit_of_measurement": "W"}
        )
        hass.states.async_set(
            f"sensor.site_margin_reactive_{phase}", "0", {"unit_of_measurement": "var"}
        )
        hass.states.async_set(
            f"sensor.site_margin_voltage_{phase}", "230", {"unit_of_measurement": "V"}
        )

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    state = hass.states.get("sensor.site_capacity_state")
    # Real headroom used for the proposal: 25 - 1 - 20 = 4 A (uncredited).
    assert abs(state.attributes["phase_headroom_a"]["L1"] - 4.0) < 1e-6
    assert state.attributes["measured_margin_a"]["L1"] == state.attributes["phase_headroom_a"]["L1"]
    # Diagnostic EV-credited estimate: 25 - 1 - (20 - 8.9) = 12.9 A, larger than the real headroom above.
    assert abs(state.attributes["calculated_headroom_after_ev_credit_a"]["L1"] - 12.9) < 1e-6
    assert (
        state.attributes["calculated_headroom_after_ev_credit_a"]["L1"]
        != state.attributes["phase_headroom_a"]["L1"]
    )


async def test_configured_battery_per_phase_source_without_confirmation_reports_unknown(
    hass: HomeAssistant,
) -> None:
    """A per-phase battery-current source alone (no aggregate power entity as direction confirmation) never produces a positive yield estimate, however positive and fresh the readings."""
    for suffix in ("l1", "l2", "l3"):
        set_current_sensor(hass, f"sensor.battery_current_{suffix}", 6.0)
    entry = make_site_entry(
        hass,
        entry_id="site_battery_unconfirmed",
        charger_entry_ids=[],
        battery_per_phase_source=separate_entities_source(
                        "sensor.battery_current_l1", "sensor.battery_current_l2", "sensor.battery_current_l3"
                    ),
    )
    set_current_sensor(hass, "sensor.site_battery_unconfirmed_l1", 5)
    set_current_sensor(hass, "sensor.site_battery_unconfirmed_l2", 5)
    set_current_sensor(hass, "sensor.site_battery_unconfirmed_l3", 5)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    controller = controller_of(hass, entry.entry_id)
    assert controller.result.battery_yield_basis == "unknown"
    assert all(v is None for v in controller.result.estimated_headroom_if_battery_yields_a.values())


async def test_configured_battery_per_phase_source_with_a_fresh_aggregate_confirmation_yields_an_estimate(
    hass: HomeAssistant,
) -> None:
    """An aggregate power entity configured alongside the per-phase current source doubles as the fresh signed direction confirmation, read live every recompute."""
    for suffix in ("l1", "l2", "l3"):
        set_current_sensor(hass, f"sensor.battery_current_confirmed_{suffix}", 6.0)
    hass.states.async_set(
        "sensor.battery_power_confirmed", "1000", {"unit_of_measurement": "W"}
    )
    entry = make_site_entry(
        hass,
        entry_id="site_battery_confirmed",
        charger_entry_ids=[],
        battery_per_phase_source=separate_entities_source(
                        "sensor.battery_current_confirmed_l1", "sensor.battery_current_confirmed_l2", "sensor.battery_current_confirmed_l3"
                    ),
        battery_aggregate_power_entity="sensor.battery_power_confirmed",
    )
    set_current_sensor(hass, "sensor.site_battery_confirmed_l1", 5)
    set_current_sensor(hass, "sensor.site_battery_confirmed_l2", 5)
    set_current_sensor(hass, "sensor.site_battery_confirmed_l3", 5)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    controller = controller_of(hass, entry.entry_id)
    assert controller.result.battery_yield_basis == "measured_per_phase"
    assert abs(controller.result.estimated_headroom_if_battery_yields_a["L1"] - (controller.result.phase_headroom_a["L1"] + 6.0)) < 1e-6


async def test_battery_per_phase_source_stops_yielding_once_aggregate_confirmation_goes_negative(
    hass: HomeAssistant,
) -> None:
    """Direction is re-checked fresh, not latched: flipping the aggregate power entity's sign between recomputes flips the estimate to `unknown` though the per-phase current never changes."""
    for suffix in ("l1", "l2", "l3"):
        set_current_sensor(hass, f"sensor.battery_current_switch_{suffix}", 6.0)
    hass.states.async_set(
        "sensor.battery_power_switch", "1000", {"unit_of_measurement": "W"}
    )
    entry = make_site_entry(
        hass,
        entry_id="site_battery_switch",
        charger_entry_ids=[],
        battery_per_phase_source=separate_entities_source(
                        "sensor.battery_current_switch_l1", "sensor.battery_current_switch_l2", "sensor.battery_current_switch_l3"
                    ),
        battery_aggregate_power_entity="sensor.battery_power_switch",
    )
    set_current_sensor(hass, "sensor.site_battery_switch_l1", 5)
    set_current_sensor(hass, "sensor.site_battery_switch_l2", 5)
    set_current_sensor(hass, "sensor.site_battery_switch_l3", 5)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    controller = controller_of(hass, entry.entry_id)
    assert controller.result.battery_yield_basis == "measured_per_phase"

    # The battery switches to discharging; the per-phase current magnitude is unchanged (a real current sensor could not tell).
    hass.states.async_set(
        "sensor.battery_power_switch", "-1000", {"unit_of_measurement": "W"}
    )
    await hass.async_block_till_done()

    assert controller.result.battery_yield_basis == "unknown"
    assert all(v is None for v in controller.result.estimated_headroom_if_battery_yields_a.values())


# Phase liveness: `last_reported` of a real HA `State` drives the wiring, not just the pure function.


async def test_report_age_is_wired_from_real_ha_last_reported_not_guessed(
    hass: HomeAssistant,
) -> None:
    """A real HA `State`'s `last_reported`, bumped by any write (changed or not, `StateMachine.async_set_internal`), becomes `PhaseValue.report_age_s` and `SiteCapacityResult.phase_liveness`.

    L2 (re-confirmed unchanged) is distinguished from L1/L3 (untouched, so `last_reported` equals
    `last_updated`): "no_recent_report", not a claim that they are dead.
    """
    old_timestamp = time.time() - (DEFAULT_MAX_AGE_S + 600.0)
    for suffix in ("l1", "l2", "l3"):
        hass.states.async_set(
            f"sensor.site_liveness_{suffix}",
            "5",
            {"unit_of_measurement": "A"},
            timestamp=old_timestamp,
        )

    entry = make_site_entry(hass, entry_id="site_liveness", charger_entry_ids=[])
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    controller = controller_of(hass, entry.entry_id)
    assert controller.result.state == "stale_measurements"

    # Re-confirm L2 alone with an identical reading. HA fires no `EVENT_STATE_CHANGED` for it, so it triggers no recompute, like a polling integration's unchanged re-read; the periodic recompute timer next reads L2's fresher `last_reported`.
    hass.states.async_set("sensor.site_liveness_l2", "5", {"unit_of_measurement": "A"})

    async_fire_time_changed(
        hass, dt_util.utcnow() + timedelta(seconds=SITE_RECOMPUTE_INTERVAL_S + 1)
    )
    await hass.async_block_till_done()

    assert controller.result.state == "stale_measurements"  # L1/L3 still block: no_recent_report
    assert controller.result.phase_liveness == {
        "L1": "no_recent_report",
        "L2": "confirmed_unchanged",
        "L3": "no_recent_report",
    }
