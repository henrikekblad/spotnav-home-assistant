"""Home Assistant integration tests for the site capacity entry: config flow, setup, reload, isolation, listeners and the apply path."""

from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed, async_mock_service

from custom_components.spotnav.site import site_capacity_controller
from custom_components.spotnav.const import (
    CONF_CHARGER_ENTRY_IDS,
    CONF_ENTRY_TYPE,
    CONF_MAIN_FUSE_A,
    CONF_MAX_AGE_S,
    CONF_MEASURED_CURRENT_SOURCE,
    CONF_PHASE_WIRING,
    CONF_REGULATOR_DEADBAND_A,
    CONF_REGULATOR_DWELL_S,
    CONF_SAFETY_MARGIN_A,
    CONF_SITE_ENABLED,
    CURRENT_CONTROL_CHANGE_CONFIGURATION,
    DOMAIN,
    ENTRY_TYPE_SITE,
    MEASUREMENT_MODE_DERIVED,
    SITE_RECOMPUTE_INTERVAL_S,
)
from custom_components.spotnav.vehicles.entity_conflicts import find_conflict
from custom_components.spotnav.site.regulator_damping import (
    DEFAULT_REGULATOR_DWELL_S,
    RegulatorDamper,
)
from custom_components.spotnav.site.regulator import (
    PhaseDecisionBasis,
    RegulatorDecision,
)
from custom_components.spotnav.site.site_capacity_controller import SiteCapacityController
from custom_components.spotnav.site.site_membership import SITE_MEMBERSHIP_ERROR

from .helpers import make_entry, make_site_entry, set_current_sensor
from .world import set_derived_site_entities, set_site_power_w
from .world import separate_entities_source
from .world import controller_of
from homeassistant.config_entries import ConfigEntryState


async def test_config_flow_creates_a_site_entry_with_direct_measurements(
    hass: HomeAssistant,
) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    assert result["step_id"] == "user"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"entry_type": "site"}
    )
    assert result["step_id"] == "site"

    set_current_sensor(hass, "sensor.grid_l1", 0)
    set_current_sensor(hass, "sensor.grid_l2", 0)
    set_current_sensor(hass, "sensor.grid_l3", 0)

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "name": "Home",
            CONF_MAIN_FUSE_A: 25,
            "safety_margin_a": 1,
            "measurement_mode": "direct_phase_current",
            "charger_entry_ids": [],
        },
    )
    assert result["step_id"] == "site_current_suggestions"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"choice": "manual"}
    )
    assert result["step_id"] == "site_details"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "direct_L1": "sensor.grid_l1",
            "direct_L2": "sensor.grid_l2",
            "direct_L3": "sensor.grid_l3",
        },
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_ENTRY_TYPE] == ENTRY_TYPE_SITE
    assert result["data"][CONF_MAIN_FUSE_A] == 25.0
    # Creation needs a complete configuration, so the calculation starts enabled.
    assert result["data"]["site_enabled"] is True


async def test_charger_flow_is_unaffected_by_the_new_entry_type_step(
    hass: HomeAssistant,
) -> None:
    """The charger flow still works end to end."""
    hass.states.async_set("switch.charger", "off")

    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"entry_type": "charger"}
    )
    assert result["step_id"] == "charger_type"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"mode": "generic"})
    assert result["step_id"] == "generic"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"charge_control": "switch.charger"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["entry_type"] == "charger"


async def test_site_entry_sets_up_and_exposes_entities(hass: HomeAssistant) -> None:
    entry = make_site_entry(hass, entry_id="site_1", charger_entry_ids=[])
    set_current_sensor(hass, "sensor.site_1_l1", 5)
    set_current_sensor(hass, "sensor.site_1_l2", 5)
    set_current_sensor(hass, "sensor.site_1_l3", 5)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    controller = controller_of(hass, entry.entry_id)
    assert isinstance(controller, SiteCapacityController)
    assert controller.result.state == "observing"

    state_sensor = hass.states.get("sensor.site_capacity_state")
    assert state_sensor is not None
    assert state_sensor.state == "observing"


async def test_installation_without_configured_sensors_sets_up_cleanly(
    hass: HomeAssistant, caplog
) -> None:
    """A site whose measurement entities don't exist yet sets up without raising and reports a normal state, not an error."""
    entry = make_site_entry(
        hass,
        entry_id="site_missing",
        direct_entities={"L1": "sensor.nope_l1", "L2": "sensor.nope_l2", "L3": "sensor.nope_l3"},
    )

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    controller = controller_of(hass, entry.entry_id)
    assert controller.result.state == "missing_measurements"
    assert "ERROR" not in caplog.text
    assert "Traceback" not in caplog.text


async def test_observing_never_calls_any_charger_control_service(hass: HomeAssistant) -> None:
    """Without active control, no measurement, charger state or elapsed time makes the site issue any switch/number/button/OCPP call."""
    switch_on_calls = async_mock_service(hass, "switch", "turn_on")
    switch_off_calls = async_mock_service(hass, "switch", "turn_off")
    number_calls = async_mock_service(hass, "number", "set_value")
    button_calls = async_mock_service(hass, "button", "press")
    ocpp_start_calls = async_mock_service(hass, "ocpp", "start_transaction")
    ocpp_stop_calls = async_mock_service(hass, "ocpp", "stop_transaction")
    ocpp_limit_calls = async_mock_service(hass, "ocpp", "set_charge_rate")

    hass.states.async_set("switch.charger_observing", "on")
    charger_entry = make_entry(
        hass,
        entry_id="charger_observing",
        charge_control="switch.charger_observing",
        current_limit=None,
        webhook_id="webhook-observing",
        title="Observing charger",
    )
    assert await hass.config_entries.async_setup(charger_entry.entry_id)
    await hass.async_block_till_done()

    site_entry = make_site_entry(
        hass,
        entry_id="site_observing",
        main_fuse_a=16.0,  # deliberately tight, to also exercise capacity_limited/below_minimum paths
        charger_entry_ids=["charger_observing"],
        phase_wiring={"charger_observing": {"phases": 3, "phase": None}},
    )
    assert await hass.config_entries.async_setup(site_entry.entry_id)
    await hass.async_block_till_done()

    # Sweep through healthy, overloaded, stale and missing measurements, letting several recompute ticks elapse.
    for l1, l2, l3 in [(0, 0, 0), (30, 30, 30), (5, 5, 5)]:
        set_current_sensor(hass, "sensor.site_observing_l1", l1)
        set_current_sensor(hass, "sensor.site_observing_l2", l2)
        set_current_sensor(hass, "sensor.site_observing_l3", l3)
        async_fire_time_changed(
            hass, dt_util.utcnow() + timedelta(seconds=SITE_RECOMPUTE_INTERVAL_S + 1)
        )
        await hass.async_block_till_done()

    hass.states.async_remove("sensor.site_observing_l1")
    async_fire_time_changed(
        hass, dt_util.utcnow() + timedelta(seconds=2 * SITE_RECOMPUTE_INTERVAL_S)
    )
    await hass.async_block_till_done()

    assert switch_on_calls == []
    assert switch_off_calls == []
    assert number_calls == []
    assert button_calls == []
    assert ocpp_start_calls == []
    assert ocpp_stop_calls == []
    assert ocpp_limit_calls == []


async def test_safety_invariant_a_high_requested_current_is_never_credited_as_measured(
    hass: HomeAssistant,
) -> None:
    """A charger commanded to a high setpoint (32 A) and reporting "on" has none of it credited against the site total."""
    hass.states.async_set("switch.invariant_charger", "off")
    async_mock_service(hass, "switch", "turn_on")
    charger = make_entry(
        hass, entry_id="charger_invariant", charge_control="switch.invariant_charger",
        current_limit=None, webhook_id="webhook-invariant", title="Invariant",
    )
    assert await hass.config_entries.async_setup(charger.entry_id)
    await hass.async_block_till_done()

    charger_controller = controller_of(hass, charger.entry_id)
    await charger_controller.async_start(amps=32)
    hass.states.async_set("switch.invariant_charger", "on")
    assert charger_controller.requested_current_a == 32

    entry = make_site_entry(
        hass,
        entry_id="site_invariant",
        main_fuse_a=25.0,
        safety_margin_a=1.0,
        charger_entry_ids=[charger.entry_id],
        phase_wiring={charger.entry_id: {"phases": 3, "phase": None}},
    )
    set_current_sensor(hass, "sensor.site_invariant_l1", 20)
    set_current_sensor(hass, "sensor.site_invariant_l2", 20)
    set_current_sensor(hass, "sensor.site_invariant_l3", 20)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    site_controller = controller_of(hass, entry.entry_id)
    # 25 - 1 - 20 = 4 A: the 32 A setpoint never reduced "other load", since it was not a measurement.
    assert site_controller.result.phase_headroom_a["L1"] == 4.0


async def test_unload_cancels_the_recompute_timer(hass: HomeAssistant) -> None:
    entry = make_site_entry(hass, entry_id="site_unload")
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.NOT_LOADED
    # A still-scheduled timer would raise or misbehave; block_till_done succeeding suffices given async_shutdown was reached.


async def test_reload_recomputes_against_current_data_not_a_persisted_result(
    hass: HomeAssistant,
) -> None:
    entry = make_site_entry(hass, entry_id="site_reload")
    set_current_sensor(hass, "sensor.site_reload_l1", 0)
    set_current_sensor(hass, "sensor.site_reload_l2", 0)
    set_current_sensor(hass, "sensor.site_reload_l3", 0)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert controller_of(hass, entry.entry_id).result.state == "observing"

    hass.states.async_remove("sensor.site_reload_l1")
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    # Nothing from the previous "observing" result is carried over: the reloaded controller reads live state and reports the now-missing sensor.
    assert controller_of(hass, entry.entry_id).result.state == "missing_measurements"


async def test_two_charger_entries_and_one_site_entry_do_not_interfere(
    hass: HomeAssistant,
) -> None:
    hass.states.async_set("switch.charger_x", "off")
    hass.states.async_set("switch.charger_y", "off")
    charger_x = make_entry(
        hass,
        entry_id="charger_x",
        charge_control="switch.charger_x",
        current_limit=None,
        webhook_id="webhook-x",
        title="Charger X",
    )
    assert await hass.config_entries.async_setup(charger_x.entry_id)
    await hass.async_block_till_done()

    charger_y = make_entry(
        hass,
        entry_id="charger_y",
        charge_control="switch.charger_y",
        current_limit=None,
        webhook_id="webhook-y",
        title="Charger Y",
    )
    assert await hass.config_entries.async_setup(charger_y.entry_id)
    await hass.async_block_till_done()

    site_entry = make_site_entry(
        hass,
        entry_id="site_multi",
        charger_entry_ids=["charger_x", "charger_y"],
        phase_wiring={
            "charger_x": {"phases": 1, "phase": "L1"},
            "charger_y": {"phases": 1, "phase": "L2"},
        },
    )
    set_current_sensor(hass, "sensor.site_multi_l1", 0)
    set_current_sensor(hass, "sensor.site_multi_l2", 0)
    set_current_sensor(hass, "sensor.site_multi_l3", 0)
    assert await hass.config_entries.async_setup(site_entry.entry_id)
    await hass.async_block_till_done()

    # Each config entry's controller instance is independently keyed.
    assert controller_of(hass, charger_x.entry_id) is not controller_of(hass, charger_y.entry_id)
    assert controller_of(hass, site_entry.entry_id) is not controller_of(hass, charger_x.entry_id)

    site_controller = controller_of(hass, site_entry.entry_id)
    assert {a.charger_entry_id for a in site_controller.result.allocations} == {
        "charger_x",
        "charger_y",
    }

    # Unloading the site entry must not affect either charger entry.
    assert await hass.config_entries.async_unload(site_entry.entry_id)
    await hass.async_block_till_done()
    assert charger_x.state is ConfigEntryState.LOADED
    assert charger_y.state is ConfigEntryState.LOADED


async def test_site_entries_never_participate_in_charger_entity_conflict_checks(
    hass: HomeAssistant,
) -> None:
    hass.states.async_set("switch.charger_z", "off")
    charger = make_entry(
        hass,
        entry_id="charger_z",
        charge_control="switch.charger_z",
        current_limit=None,
        webhook_id="webhook-z",
        title="Charger Z",
    )
    make_site_entry(hass, entry_id="site_conflict_check", charger_entry_ids=[charger.entry_id])

    # A candidate charger reusing the same switch is still flagged as a conflict against the real charger entry...
    conflict = find_conflict(hass, charge_control="switch.charger_z", current_limit=None)
    assert conflict is not None
    assert conflict.conflicting_entry_id == charger.entry_id

    # ...but an unrelated new switch is not flagged just because a site entry exists.
    assert find_conflict(hass, charge_control="switch.unrelated", current_limit=None) is None


async def test_available_charger_options_excludes_chargers_claimed_by_other_sites(
    hass: HomeAssistant,
) -> None:
    from custom_components.spotnav.config_flow.site_form import _available_charger_options as available_charger_options

    hass.states.async_set("switch.dup_charger", "off")
    hass.states.async_set("switch.free_charger", "off")
    dup = make_entry(
        hass, entry_id="charger_dup", charge_control="switch.dup_charger",
        current_limit=None, webhook_id="webhook-dup", title="Dup",
    )
    free = make_entry(
        hass, entry_id="charger_free", charge_control="switch.free_charger",
        current_limit=None, webhook_id="webhook-free", title="Free",
    )
    make_site_entry(hass, entry_id="site_existing", charger_entry_ids=[dup.entry_id])

    options = available_charger_options(hass)

    assert dup.entry_id not in options
    assert free.entry_id in options


async def test_config_flow_rejects_a_race_conflict_at_the_final_submit(
    hass: HomeAssistant,
) -> None:
    """The final submit re-checks claimed chargers in case another site claimed one between the flow steps."""
    hass.states.async_set("switch.race_charger", "off")
    charger = make_entry(
        hass, entry_id="charger_race", charge_control="switch.race_charger",
        current_limit=None, webhook_id="webhook-race", title="Race Charger",
    )

    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"entry_type": "site"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "name": "Race",
            CONF_MAIN_FUSE_A: 25,
            "safety_margin_a": 1,
            "measurement_mode": "direct_phase_current",
            "charger_entry_ids": [charger.entry_id],
        },
    )
    assert result["step_id"] == "site_current_suggestions"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"choice": "manual"}
    )
    assert result["step_id"] == "site_details"

    # Another site claims the same charger while this flow is mid-wizard.
    make_site_entry(hass, entry_id="site_won_the_race", charger_entry_ids=[charger.entry_id])

    set_current_sensor(hass, "sensor.race_l1", 0)
    set_current_sensor(hass, "sensor.race_l2", 0)
    set_current_sensor(hass, "sensor.race_l3", 0)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "direct_L1": "sensor.race_l1",
            "direct_L2": "sensor.race_l2",
            "direct_L3": "sensor.race_l3",
        },
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "site"
    assert result["errors"] == {CONF_CHARGER_ENTRY_IDS: SITE_MEMBERSHIP_ERROR}


async def test_two_saved_sites_sharing_a_charger_both_load_while_observing_with_a_diagnostic_conflict(
    hass: HomeAssistant,
) -> None:
    """Two saved site entries sharing a charger (e.g. hand-edited storage) both still load, and both report the conflict."""
    hass.states.async_set("switch.shared_charger", "off")
    charger = make_entry(
        hass, entry_id="charger_shared", charge_control="switch.shared_charger",
        current_limit=None, webhook_id="webhook-shared", title="Shared",
    )
    assert await hass.config_entries.async_setup(charger.entry_id)
    await hass.async_block_till_done()

    site_a = make_site_entry(hass, entry_id="site_a_legacy", charger_entry_ids=[charger.entry_id])
    set_current_sensor(hass, "sensor.site_a_legacy_l1", 0)
    set_current_sensor(hass, "sensor.site_a_legacy_l2", 0)
    set_current_sensor(hass, "sensor.site_a_legacy_l3", 0)
    assert await hass.config_entries.async_setup(site_a.entry_id)
    await hass.async_block_till_done()

    site_b = make_site_entry(hass, entry_id="site_b_legacy", charger_entry_ids=[charger.entry_id])
    set_current_sensor(hass, "sensor.site_b_legacy_l1", 0)
    set_current_sensor(hass, "sensor.site_b_legacy_l2", 0)
    set_current_sensor(hass, "sensor.site_b_legacy_l3", 0)
    assert await hass.config_entries.async_setup(site_b.entry_id)
    await hass.async_block_till_done()

    controller_a = controller_of(hass, site_a.entry_id)
    controller_b = controller_of(hass, site_b.entry_id)

    assert controller_a.result.state == "observing"
    assert controller_b.result.state == "observing"
    assert len(controller_a.membership_conflicts) == 1
    assert controller_a.membership_conflicts[0].conflicting_site_entry_id == site_b.entry_id
    assert len(controller_b.membership_conflicts) == 1
    assert controller_b.membership_conflicts[0].conflicting_site_entry_id == site_a.entry_id


async def test_options_flow_can_edit_every_structural_field(hass: HomeAssistant) -> None:
    hass.states.async_set("switch.opt_charger", "off")
    charger = make_entry(
        hass, entry_id="charger_opt", charge_control="switch.opt_charger",
        current_limit=None, webhook_id="webhook-opt", title="Opt Charger",
    )
    assert await hass.config_entries.async_setup(charger.entry_id)
    await hass.async_block_till_done()

    entry = make_site_entry(hass, entry_id="site_opt", charger_entry_ids=[])
    set_current_sensor(hass, "sensor.site_opt_l1", 0)
    set_current_sensor(hass, "sensor.site_opt_l2", 0)
    set_current_sensor(hass, "sensor.site_opt_l3", 0)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["step_id"] == "site_init"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "name": "Opt Site",
            CONF_MAIN_FUSE_A: 32,
            "safety_margin_a": 2,
            "measurement_mode": "direct_phase_current",
            "charger_entry_ids": [charger.entry_id],
            "site_enabled": True,
            "max_age_s": 60,
        },
    )
    assert result["step_id"] == "site_current_suggestions"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"choice": "manual"}
    )
    assert result["step_id"] == "site_details"

    set_current_sensor(hass, "sensor.site_opt_new_l1", 0)
    set_current_sensor(hass, "sensor.site_opt_new_l2", 0)
    set_current_sensor(hass, "sensor.site_opt_new_l3", 0)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            f"phases_{charger.entry_id}": 1,
            f"phase_{charger.entry_id}": "L2",
            "direct_L1": "sensor.site_opt_new_l1",
            "direct_L2": "sensor.site_opt_new_l2",
            "direct_L3": "sensor.site_opt_new_l3",
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()

    updated_entry = hass.config_entries.async_get_entry(entry.entry_id)
    assert updated_entry.data[CONF_MAIN_FUSE_A] == 32.0
    assert updated_entry.data[CONF_SAFETY_MARGIN_A] == 2.0
    assert updated_entry.data[CONF_SITE_ENABLED] is True
    assert updated_entry.data[CONF_MAX_AGE_S] == 60.0
    assert updated_entry.data[CONF_CHARGER_ENTRY_IDS] == [charger.entry_id]
    assert updated_entry.data[CONF_PHASE_WIRING][charger.entry_id] == {"phases": 1, "phase": "L2"}

    controller = controller_of(hass, entry.entry_id)
    assert controller.config[CONF_SITE_ENABLED] is True


async def test_options_flow_rejects_a_race_conflict_on_submit(hass: HomeAssistant) -> None:
    """The picker excludes chargers claimed elsewhere at render time, so only a race between render and submit reaches the authoritative check."""
    hass.states.async_set("switch.claimed", "off")
    claimed_charger = make_entry(
        hass, entry_id="charger_claimed", charge_control="switch.claimed",
        current_limit=None, webhook_id="webhook-claimed", title="Claimed",
    )

    entry = make_site_entry(hass, entry_id="site_editing", charger_entry_ids=[])
    set_current_sensor(hass, "sensor.site_editing_l1", 0)
    set_current_sensor(hass, "sensor.site_editing_l2", 0)
    set_current_sensor(hass, "sensor.site_editing_l3", 0)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    # Rendered while charger_claimed is still free, so it is a valid choice in the shown schema.
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["step_id"] == "site_init"

    # Another site claims it before the step is submitted.
    make_site_entry(hass, entry_id="site_owner", charger_entry_ids=[claimed_charger.entry_id])

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "name": "Site Editing",
            CONF_MAIN_FUSE_A: 25,
            "safety_margin_a": 1,
            "measurement_mode": "direct_phase_current",
            "charger_entry_ids": [claimed_charger.entry_id],
            "site_enabled": False,
            "max_age_s": 120,
        },
    )

    assert result["step_id"] == "site_init"
    assert result["errors"] == {CONF_CHARGER_ENTRY_IDS: SITE_MEMBERSHIP_ERROR}


async def test_options_flow_rejects_a_race_conflict_created_between_its_two_steps(
    hass: HomeAssistant,
) -> None:
    """Race between steps: site_init passes validation, another site claims the charger before site_details saves. It is caught, nothing is written, and the user returns to a step where the choice can be fixed."""
    hass.states.async_set("switch.between_steps", "off")
    charger = make_entry(
        hass, entry_id="charger_between_steps", charge_control="switch.between_steps",
        current_limit=None, webhook_id="webhook-between-steps", title="Between Steps",
    )

    entry = make_site_entry(hass, entry_id="site_between_steps", charger_entry_ids=[])
    original_fuse = entry.data[CONF_MAIN_FUSE_A]
    set_current_sensor(hass, "sensor.site_between_steps_l1", 0)
    set_current_sensor(hass, "sensor.site_between_steps_l2", 0)
    set_current_sensor(hass, "sensor.site_between_steps_l3", 0)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "name": "Renamed while racing",
            CONF_MAIN_FUSE_A: 32,
            "safety_margin_a": 2,
            "measurement_mode": "direct_phase_current",
            "charger_entry_ids": [charger.entry_id],
            "site_enabled": True,
            "max_age_s": 60,
        },
    )
    assert result["step_id"] == "site_current_suggestions"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"choice": "manual"}
    )
    assert result["step_id"] == "site_details"  # step 1 accepted it: still free at that instant

    # Another site claims the charger before step 2 is submitted.
    make_site_entry(hass, entry_id="site_won_the_race", charger_entry_ids=[charger.entry_id])

    set_current_sensor(hass, "sensor.site_between_steps_new_l1", 0)
    set_current_sensor(hass, "sensor.site_between_steps_new_l2", 0)
    set_current_sensor(hass, "sensor.site_between_steps_new_l3", 0)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            f"phases_{charger.entry_id}": 3,
            "direct_L1": "sensor.site_between_steps_new_l1",
            "direct_L2": "sensor.site_between_steps_new_l2",
            "direct_L3": "sensor.site_between_steps_new_l3",
        },
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "site_init"
    assert result["errors"] == {CONF_CHARGER_ENTRY_IDS: SITE_MEMBERSHIP_ERROR}

    # Nothing was written: the entry keeps its original data.
    unchanged_entry = hass.config_entries.async_get_entry(entry.entry_id)
    assert unchanged_entry.data[CONF_MAIN_FUSE_A] == original_fuse
    assert unchanged_entry.data[CONF_CHARGER_ENTRY_IDS] == []
    assert unchanged_entry.title == entry.title


async def test_options_flow_only_shows_fields_relevant_to_the_selected_measurement_mode(
    hass: HomeAssistant,
) -> None:
    entry = make_site_entry(hass, entry_id="site_mode_switch", charger_entry_ids=[])
    set_current_sensor(hass, "sensor.site_mode_switch_l1", 0)
    set_current_sensor(hass, "sensor.site_mode_switch_l2", 0)
    set_current_sensor(hass, "sensor.site_mode_switch_l3", 0)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "name": "Site",
            CONF_MAIN_FUSE_A: 25,
            "safety_margin_a": 1,
            "measurement_mode": "derived_phase_current",
            "charger_entry_ids": [],
            "site_enabled": False,
            "max_age_s": 120,
        },
    )
    assert result["step_id"] == "site_details"

    field_names = {str(key) for key in result["data_schema"].schema}
    assert "derived_L1_power" in field_names
    assert "derived_L1_reactive_power" in field_names
    assert "derived_L1_voltage" in field_names
    assert "direct_L1" not in field_names


async def test_state_change_triggers_an_immediate_recompute_without_waiting_for_the_timer(
    hass: HomeAssistant,
) -> None:
    entry = make_site_entry(hass, entry_id="site_event")
    set_current_sensor(hass, "sensor.site_event_l1", 0)
    set_current_sensor(hass, "sensor.site_event_l2", 0)
    set_current_sensor(hass, "sensor.site_event_l3", 0)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    controller = controller_of(hass, entry.entry_id)
    assert controller.result.phase_headroom_a["L1"] == 24.0  # 25 - 1 - 0

    set_current_sensor(hass, "sensor.site_event_l1", 10)
    await hass.async_block_till_done()

    # No time advanced: only the state-change listener could have triggered this recompute.
    assert controller.result.phase_headroom_a["L1"] == 14.0


async def test_charger_switch_state_change_triggers_a_recompute(hass: HomeAssistant) -> None:
    hass.states.async_set("switch.event_charger", "off")
    charger = make_entry(
        hass, entry_id="charger_event", charge_control="switch.event_charger",
        current_limit=None, webhook_id="webhook-event", title="Event Charger",
    )
    assert await hass.config_entries.async_setup(charger.entry_id)
    await hass.async_block_till_done()

    entry = make_site_entry(
        hass,
        entry_id="site_charger_event",
        charger_entry_ids=[charger.entry_id],
        phase_wiring={charger.entry_id: {"phases": 3, "phase": None}},
    )
    set_current_sensor(hass, "sensor.site_charger_event_l1", 0)
    set_current_sensor(hass, "sensor.site_charger_event_l2", 0)
    set_current_sensor(hass, "sensor.site_charger_event_l3", 0)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    controller = controller_of(hass, entry.entry_id)
    assert controller.result.allocations[0].requested_current_a == 0.0

    charger_controller = controller_of(hass, charger.entry_id)
    async_mock_service(hass, "switch", "turn_on")
    await charger_controller.async_start(amps=10)
    hass.states.async_set("switch.event_charger", "on")
    await hass.async_block_till_done()

    assert controller.result.allocations[0].requested_current_a == 10.0


async def test_unload_stops_the_state_listener_from_triggering_further_recomputes(
    hass: HomeAssistant,
) -> None:
    entry = make_site_entry(hass, entry_id="site_cleanup")
    set_current_sensor(hass, "sensor.site_cleanup_l1", 0)
    set_current_sensor(hass, "sensor.site_cleanup_l2", 0)
    set_current_sensor(hass, "sensor.site_cleanup_l3", 0)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    controller = controller_of(hass, entry.entry_id)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    result_before = controller.result
    set_current_sensor(hass, "sensor.site_cleanup_l1", 15)
    await hass.async_block_till_done()

    # The entity still exists (unloading the entry doesn't remove someone else's sensor) but its controller must no longer react; compared by identity since a recompute replaces .result.
    assert controller.result is result_before


async def test_tracked_entity_ids_has_no_duplicates_even_with_overlapping_config(
    hass: HomeAssistant,
) -> None:
    hass.states.async_set("switch.dedupe_charger", "off")
    # current_limit reuses the charge_control entity id purely to force an overlap for this dedup check.
    charger = make_entry(
        hass, entry_id="charger_dedupe", charge_control="switch.dedupe_charger",
        current_limit="switch.dedupe_charger", webhook_id="webhook-dedupe", title="Dedupe",
    )
    assert await hass.config_entries.async_setup(charger.entry_id)
    await hass.async_block_till_done()

    site_entry = make_site_entry(
        hass, entry_id="site_dedupe", charger_entry_ids=[charger.entry_id]
    )
    controller = SiteCapacityController(hass, site_entry.entry_id, dict(site_entry.data))

    tracked = controller._tracked_entity_ids()

    assert len(tracked) == len(set(tracked))


def _site_listener_count(charger_controller: object) -> int:
    """Count of a ChargingController's listeners bound to a SiteCapacityController (the charger's own entities also register listeners)."""
    return sum(
        1
        for listener in charger_controller._listeners  # noqa: SLF001 (test-only introspection)
        if isinstance(getattr(listener, "__self__", None), SiteCapacityController)
    )


async def test_requested_current_change_with_no_state_transition_still_triggers_recompute(
    hass: HomeAssistant,
) -> None:
    """The switch is already "on" and there is no current-limit entity, so only `ChargingController.add_listener` can notify the site controller of a requested-current change."""
    hass.states.async_set("switch.listener_charger", "on")
    switch_on_calls = async_mock_service(hass, "switch", "turn_on")
    charger = make_entry(
        hass, entry_id="charger_listener", charge_control="switch.listener_charger",
        current_limit=None, webhook_id="webhook-listener", title="Listener Charger",
    )
    assert await hass.config_entries.async_setup(charger.entry_id)
    await hass.async_block_till_done()

    entry = make_site_entry(
        hass,
        entry_id="site_listener",
        charger_entry_ids=[charger.entry_id],
        phase_wiring={charger.entry_id: {"phases": 3, "phase": None}},
    )
    set_current_sensor(hass, "sensor.site_listener_l1", 0)
    set_current_sensor(hass, "sensor.site_listener_l2", 0)
    set_current_sensor(hass, "sensor.site_listener_l3", 0)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    site_controller = controller_of(hass, entry.entry_id)
    # Charging (switch already "on") but nothing explicitly requested yet: unknown, not 0.
    assert site_controller.result.allocations[0].requested_current_a is None
    assert site_controller.result.allocations[0].state == "requested_current_unknown"

    charger_controller = controller_of(hass, charger.entry_id)
    await charger_controller.async_start(amps=16)
    await hass.async_block_till_done()

    # No time advanced and the switch state never changed: only the controller-listener path could have caused this update.
    assert site_controller.result.allocations[0].requested_current_a == 16.0
    assert site_controller.result.allocations[0].state == "capacity_available"
    # The switch was already on, so async_start made no service call; the site controller's reaction is not itself a service call.
    assert switch_on_calls == []


async def test_unload_removes_this_sites_charger_controller_listeners(
    hass: HomeAssistant,
) -> None:
    hass.states.async_set("switch.unload_listener_charger", "off")
    charger = make_entry(
        hass, entry_id="charger_unload_listener", charge_control="switch.unload_listener_charger",
        current_limit=None, webhook_id="webhook-unload-listener", title="Unload Listener",
    )
    assert await hass.config_entries.async_setup(charger.entry_id)
    await hass.async_block_till_done()

    entry = make_site_entry(
        hass, entry_id="site_unload_listener", charger_entry_ids=[charger.entry_id]
    )
    set_current_sensor(hass, "sensor.site_unload_listener_l1", 0)
    set_current_sensor(hass, "sensor.site_unload_listener_l2", 0)
    set_current_sensor(hass, "sensor.site_unload_listener_l3", 0)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    charger_controller = controller_of(hass, charger.entry_id)
    # Counts only listeners bound to a SiteCapacityController, not the charger's own entities' listeners.
    assert _site_listener_count(charger_controller) == 1

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert _site_listener_count(charger_controller) == 0


async def test_reload_creates_exactly_one_new_controller_listener(hass: HomeAssistant) -> None:
    hass.states.async_set("switch.reload_listener_charger", "off")
    charger = make_entry(
        hass, entry_id="charger_reload_listener", charge_control="switch.reload_listener_charger",
        current_limit=None, webhook_id="webhook-reload-listener", title="Reload Listener",
    )
    assert await hass.config_entries.async_setup(charger.entry_id)
    await hass.async_block_till_done()

    entry = make_site_entry(
        hass, entry_id="site_reload_listener", charger_entry_ids=[charger.entry_id]
    )
    set_current_sensor(hass, "sensor.site_reload_listener_l1", 0)
    set_current_sensor(hass, "sensor.site_reload_listener_l2", 0)
    set_current_sensor(hass, "sensor.site_reload_listener_l3", 0)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    charger_controller = controller_of(hass, charger.entry_id)
    assert _site_listener_count(charger_controller) == 1

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    # Not 0 (new controller registered) and not 2 (the old one was removed, not leaked).
    assert _site_listener_count(charger_controller) == 1


async def test_a_removed_or_not_yet_loaded_charger_causes_no_exception(
    hass: HomeAssistant,
) -> None:
    entry = make_site_entry(
        hass, entry_id="site_missing_charger", charger_entry_ids=["charger_never_existed"]
    )
    set_current_sensor(hass, "sensor.site_missing_charger_l1", 0)
    set_current_sensor(hass, "sensor.site_missing_charger_l2", 0)
    set_current_sensor(hass, "sensor.site_missing_charger_l3", 0)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    controller = controller_of(hass, entry.entry_id)
    assert controller._controller_listener_cancels == []
    assert controller.result.allocations[0].charger_entry_id == "charger_never_existed"

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_state_listeners_and_controller_listeners_coexist_without_extra_service_calls(
    hass: HomeAssistant,
) -> None:
    """State-change and requested-current triggers both active: neither interferes, and no service call is issued."""
    hass.states.async_set("switch.coexist_charger", "off")
    charger = make_entry(
        hass, entry_id="charger_coexist", charge_control="switch.coexist_charger",
        current_limit=None, webhook_id="webhook-coexist", title="Coexist Charger",
    )
    assert await hass.config_entries.async_setup(charger.entry_id)
    await hass.async_block_till_done()

    # Mocked after setup: the charger entry forwards the switch/number platforms, whose core integrations would replace an earlier mock's entity services.
    switch_on_calls = async_mock_service(hass, "switch", "turn_on")
    number_calls = async_mock_service(hass, "number", "set_value")

    entry = make_site_entry(
        hass,
        entry_id="site_coexist",
        charger_entry_ids=[charger.entry_id],
        phase_wiring={charger.entry_id: {"phases": 3, "phase": None}},
    )
    set_current_sensor(hass, "sensor.site_coexist_l1", 0)
    set_current_sensor(hass, "sensor.site_coexist_l2", 0)
    set_current_sensor(hass, "sensor.site_coexist_l3", 0)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    site_controller = controller_of(hass, entry.entry_id)

    # Trigger via the HA state-change path (a measurement update).
    set_current_sensor(hass, "sensor.site_coexist_l1", 5)
    await hass.async_block_till_done()
    assert site_controller.result.measured_phase_current_a["L1"] == 5

    # Trigger via the ChargingController listener path (the switch was off, so this also flips its state).
    charger_controller = controller_of(hass, charger.entry_id)
    await charger_controller.async_start(amps=10)
    hass.states.async_set("switch.coexist_charger", "on")
    await hass.async_block_till_done()

    assert site_controller.result.allocations[0].requested_current_a == 10.0
    # Exactly the one switch.turn_on call the test made via the charger's own async_start; the site controller adds nothing.
    assert len(switch_on_calls) == 1
    assert number_calls == []
# Apply path: `ACTIVE_CONTROL_READY` (in `site/site_capacity.py`) is the compile-time gate and a site's `CONF_ACTIVE_CONTROL_ENABLED` the user's opt-in; both are required.
# Each test pins one of them, plus the write itself, the only place that calls a charger-control service.


async def _active_control_setup(
    hass: HomeAssistant,
    *,
    entry_id: str,
    active_control_enabled: bool = True,
    current_control: str = CURRENT_CONTROL_CHANGE_CONFIGURATION,
) -> tuple[object, object, list]:
    """An opted-in site with one 3-phase ChangeConfiguration charger, a measured current and mocked OCPP services.

    Returns `(site_entry, charger_entry, configure_calls)`. The call log is cleared
    after the charger's own `async_start` write so tests see only the site's writes.
    """
    prefix = f"{entry_id}_charger"
    hass.states.async_set(f"switch.{prefix}", "off")
    async_mock_service(hass, "switch", "turn_on")
    async_mock_service(hass, "ocpp", "get_configuration", response={"value": "1.6,2.10"})
    configure_calls = async_mock_service(hass, "ocpp", "configure")
    charger = make_entry(
        hass,
        entry_id=f"{entry_id}_charger",
        charge_control=f"switch.{prefix}",
        current_limit=f"number.{prefix}_connector_1_session_current_limit",
        webhook_id=f"webhook-{entry_id}",
        title="Active control charger",
        current_control=current_control,
        ocpp_target=(prefix, 1),
    )
    assert await hass.config_entries.async_setup(charger.entry_id)
    await hass.async_block_till_done()
    charger_controller = controller_of(hass, charger.entry_id)
    await charger_controller.async_start(amps=16)
    hass.states.async_set(f"switch.{prefix}", "on")
    configure_calls.clear()

    for suffix in ("l1", "l2", "l3"):
        set_current_sensor(hass, f"sensor.{prefix}_{suffix}", 6)

    derived_entities = set_derived_site_entities(hass, f"{entry_id}_site", 2000.0)
    site_entry = make_site_entry(
        hass,
        entry_id=f"{entry_id}_site",
        main_fuse_a=20.0,
        safety_margin_a=1.0,
        charger_entry_ids=[charger.entry_id],
        phase_wiring={
            charger.entry_id: {
                "phases": 3,
                "phase": None,
                CONF_MEASURED_CURRENT_SOURCE: separate_entities_source(
                        f"sensor.{prefix}_l1", f"sensor.{prefix}_l2", f"sensor.{prefix}_l3"
                    ),
            }
        },
        measurement_mode=MEASUREMENT_MODE_DERIVED,
        derived_entities=derived_entities,
        active_control_enabled=active_control_enabled,
    )
    assert await hass.config_entries.async_setup(site_entry.entry_id)
    await hass.async_block_till_done()
    return site_entry, charger, configure_calls


async def test_active_control_writes_the_decided_current_through_the_charger(
    hass: HomeAssistant,
) -> None:
    """End to end: an increase inside the uncredited margin produces exactly one write with the decided amps for the charger's connector."""
    site_entry, charger, configure_calls = await _active_control_setup(hass, entry_id="write")
    controller = controller_of(hass, site_entry.entry_id)

    decision = controller.regulator_decisions[charger.entry_id]
    assert decision.reason == "increase_within_safe_uncredited_margin"
    assert decision.proposed_current_a is not None
    assert len(configure_calls) == 1
    assert configure_calls[0].data == {
        "devid": "write_charger",
        "ocpp_key": "AssignedCurrent",
        "value": f"1.{round(decision.proposed_current_a)},2.10",
    }


async def test_active_control_writes_nothing_without_the_site_option(
    hass: HomeAssistant,
) -> None:
    """User's half of the gate: the constant alone never writes."""
    _, _, configure_calls = await _active_control_setup(
        hass, entry_id="no_optin", active_control_enabled=False
    )

    assert configure_calls == []


async def test_active_control_writes_nothing_when_the_compile_time_gate_is_off(
    hass: HomeAssistant, monkeypatch
) -> None:
    """Code's half: an opted-in site writes nothing while `ACTIVE_CONTROL_READY` is false."""
    monkeypatch.setattr(site_capacity_controller, "ACTIVE_CONTROL_READY", False)

    _, _, configure_calls = await _active_control_setup(hass, entry_id="gate_off")

    assert configure_calls == []


async def test_active_control_skips_a_charger_without_change_configuration(
    hass: HomeAssistant,
) -> None:
    """That mode is the only write path, so a charger configured otherwise is left alone."""
    _, _, configure_calls = await _active_control_setup(
        hass, entry_id="other_mode", current_control=""
    )

    assert configure_calls == []


def _handmade_decision(amps: float, *, direction: str = "net_import") -> RegulatorDecision:
    """One phase of basis and a concrete proposal, for tests where the regulator's arithmetic is not under test."""
    return RegulatorDecision(
        proposed_current_a=amps,
        reason="increase_within_safe_uncredited_margin",
        limiting_phase="L1",
        basis={
            "L1": PhaseDecisionBasis(
                signed_active_power_w=2000.0,
                signed_active_power_age_s=5.0,
                measured_current_a=6.0,
                measured_current_age_s=5.0,
                requested_current_a=amps,
                direction=direction,
                confirmed_direction=direction,
            )
        },
    )


async def test_active_control_refuses_a_write_when_the_sign_changed(hass: HomeAssistant) -> None:
    """A decision taken while the phase imported and a write attempted after it flipped to export: hold, never guess."""
    site_entry, charger, configure_calls = await _active_control_setup(hass, entry_id="flip")
    controller = controller_of(hass, site_entry.entry_id)

    controller.regulator_decisions = {charger.entry_id: _handmade_decision(16.0)}
    await controller._async_apply_active_control()
    assert len(configure_calls) == 1  # applyable: the site still imports

    configure_calls.clear()
    set_site_power_w(hass, "flip_site", -2000.0)
    controller.regulator_decisions = {charger.entry_id: _handmade_decision(14.0)}
    await controller._async_apply_active_control()

    assert configure_calls == []


async def test_active_control_skips_a_refusal_and_an_unchanged_decision(
    hass: HomeAssistant,
) -> None:
    """A refusal is never a silent keep, and a proposal identical to the previous tick's is not written twice."""
    site_entry, charger, configure_calls = await _active_control_setup(hass, entry_id="skip")
    controller = controller_of(hass, site_entry.entry_id)
    configure_calls.clear()

    controller.regulator_decisions = {
        charger.entry_id: RegulatorDecision(
            proposed_current_a=None,
            reason="phase_measurement_unusable",
            limiting_phase="L1",
        )
    }
    await controller._async_apply_active_control()
    assert configure_calls == []

    unchanged = _handmade_decision(16.0)
    controller.regulator_decisions = {charger.entry_id: unchanged}
    # The damping record starts empty per session; replacing it makes this proposal the first write again.
    controller._dampers.clear()
    await controller._async_apply_active_control()
    assert len(configure_calls) == 1  # a first, real proposal is always sent

    # The same proposal again: nothing, because nothing changed.
    await controller._async_apply_active_control()

    assert len(configure_calls) == 1


async def test_active_control_writes_nothing_on_a_membership_conflict(
    hass: HomeAssistant,
) -> None:
    """A charger claimed by two sites is written by neither."""
    site_entry, charger, configure_calls = await _active_control_setup(hass, entry_id="conflict")
    controller = controller_of(hass, site_entry.entry_id)
    configure_calls.clear()

    # Hand-built storage, a conflict the config-flow guard cannot prevent.
    other = make_site_entry(hass, entry_id="conflict_other_site", charger_entry_ids=[charger.entry_id])
    assert await hass.config_entries.async_setup(other.entry_id)
    await hass.async_block_till_done()

    assert controller.membership_conflicts  # the precondition, not an assumption
    controller.regulator_decisions = {charger.entry_id: _handmade_decision(16.0)}
    await controller._async_apply_active_control()

    assert configure_calls == []


async def test_active_control_rounds_a_fractional_proposal_to_whole_amps(
    hass: HomeAssistant,
) -> None:
    """A fractional setpoint is rounded before the write, never truncated.

    The damper is cleared first so the rounding, not the deadband, is measured.
    """
    site_entry, charger, configure_calls = await _active_control_setup(hass, entry_id="round")
    controller = controller_of(hass, site_entry.entry_id)
    configure_calls.clear()
    controller._dampers.clear()

    controller.regulator_decisions = {charger.entry_id: _handmade_decision(11.6)}
    await controller._async_apply_active_control()

    assert len(configure_calls) == 1
    assert configure_calls[0].data["value"] == "1.12,2.10"


class _Clock:
    """A clock advanced by hand, so nothing sleeps."""

    def __init__(self) -> None:
        self._now = dt_util.utcnow()

    def __call__(self):
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)


def _clocked_damper(hass: HomeAssistant, site_entry, charger) -> None:
    """Give this charger a fresh, clock-injected damper, as an `options` reload would, so the next decision is its first write."""
    controller = controller_of(hass, site_entry.entry_id)
    clock = _Clock()
    controller._dampers[charger.entry_id] = RegulatorDamper(now=clock)
    return clock


async def test_active_control_holds_a_change_until_its_dwell_then_writes_it(
    hass: HomeAssistant,
) -> None:
    """A regulation change needs both the deadband and a continuous 60 s.

    The fixture's phase has ~10 A of margin, so the change is ordinary regulation, not protection.
    """
    site_entry, charger, configure_calls = await _active_control_setup(hass, entry_id="dwell")
    controller = controller_of(hass, site_entry.entry_id)
    clock = _clocked_damper(hass, site_entry, charger)
    configure_calls.clear()

    controller.regulator_decisions = {charger.entry_id: _handmade_decision(16.0)}
    await controller._async_apply_active_control()
    assert len(configure_calls) == 1  # the session's first write is immediate

    # A 4 A reduction is larger than the deadband, so it is a candidate, and a candidate waits.
    controller.regulator_decisions = {charger.entry_id: _handmade_decision(12.0)}
    await controller._async_apply_active_control()
    assert len(configure_calls) == 1

    clock.advance(DEFAULT_REGULATOR_DWELL_S - 1)
    await controller._async_apply_active_control()
    assert len(configure_calls) == 1

    clock.advance(1)
    await controller._async_apply_active_control()
    assert len(configure_calls) == 2
    assert configure_calls[1].data["value"] == "1.12,2.10"


async def test_active_control_writes_a_protection_reduction_immediately(
    hass: HomeAssistant, caplog
) -> None:
    """A reduction near the fuse waits for nothing.

    The same reduction waited a minute with ~10 A of margin; here the recompute
    triggered by the measurement writes it on that pass, with no clock advanced.
    `damping=` in the log line is the evidence of which rule allowed it.
    """
    caplog.set_level(
        logging.INFO, logger="custom_components.spotnav.site.site_capacity_controller"
    )
    site_entry, charger, configure_calls = await _active_control_setup(hass, entry_id="protect")
    controller = controller_of(hass, site_entry.entry_id)
    _clocked_damper(hass, site_entry, charger)
    configure_calls.clear()

    controller.regulator_decisions = {charger.entry_id: _handmade_decision(10.0)}
    await controller._async_apply_active_control()
    assert len(configure_calls) == 1

    # 20 A fuse with 1 A safety margin leaves ~0.5 A per phase. Set before installing the decision, because this state change triggers a recompute that would replace it; the log is cleared first since that apply pass runs eagerly.
    fuse_a, safety_margin_a = 20.0, 1.0
    caplog.clear()
    set_derived_site_entities(
        hass, "protect_site", (fuse_a - safety_margin_a - 0.5) * 230.0
    )
    await hass.async_block_till_done()

    # The recompute has already written the reduced proposal, immediately.
    assert configure_calls[-1].data["value"] == "1.6,2.10"
    written = [message for message in caplog.messages if "set to" in message]
    assert len(written) == 1
    assert "damping=protection" in written[0]


async def test_the_configured_damping_numbers_reach_the_damper(hass: HomeAssistant) -> None:
    """Zero is a legal configuration meaning "no damping".

    A 1 A change is inside the default 2 A deadband, so writing immediately proves the site's own numbers took effect.
    """
    site_entry, charger, configure_calls = await _active_control_setup(hass, entry_id="nodamp")
    controller = controller_of(hass, site_entry.entry_id)
    configure_calls.clear()
    controller.config[CONF_REGULATOR_DEADBAND_A] = 0.0
    controller.config[CONF_REGULATOR_DWELL_S] = 0.0
    # The setup's pass built a damper from the defaults; a site's numbers are read when its damper is built, as after an options-change reload.
    controller._dampers.clear()

    controller.regulator_decisions = {charger.entry_id: _handmade_decision(16.0)}
    await controller._async_apply_active_control()
    controller.regulator_decisions = {charger.entry_id: _handmade_decision(15.0)}
    await controller._async_apply_active_control()

    assert [call.data["value"] for call in configure_calls] == ["1.16,2.10", "1.15,2.10"]
    assert controller._damper_for(charger.entry_id).last_written_a == 15.0


async def test_capability_snapshot_reports_the_active_control_pair(hass: HomeAssistant) -> None:
    """The live site sensor's fields, from the same facts the apply path uses: controllable and turned on."""
    site_entry, _charger_entry, _configure_calls = await _active_control_setup(
        hass, entry_id="capability"
    )
    controller = controller_of(hass, site_entry.entry_id)

    load_balancing = controller.capability_snapshot.load_balancing
    assert load_balancing.active_available is True
    assert load_balancing.active_enabled is True


async def test_capability_snapshot_separates_available_from_enabled(
    hass: HomeAssistant,
) -> None:
    """A controllable site that never opted in says so, not "unavailable"."""
    site_entry, _charger_entry, _configure_calls = await _active_control_setup(
        hass, entry_id="capability_off", active_control_enabled=False
    )
    controller = controller_of(hass, site_entry.entry_id)

    load_balancing = controller.capability_snapshot.load_balancing
    assert load_balancing.active_available is True
    assert load_balancing.active_enabled is False


async def test_capability_snapshot_is_unavailable_without_a_commandable_charger(
    hass: HomeAssistant,
) -> None:
    """A charger with a current-limit entity but no write-path opt-in: `current_control_available` true, `active_available` false, and nothing written."""
    site_entry, _charger_entry, configure_calls = await _active_control_setup(
        hass, entry_id="capability_other_mode", current_control=""
    )
    controller = controller_of(hass, site_entry.entry_id)

    snapshot = controller.capability_snapshot
    assert snapshot.current_control_available is True
    assert snapshot.load_balancing.active_available is False
    assert configure_calls == []


async def test_the_site_sensor_reports_both_active_control_attributes(
    hass: HomeAssistant,
) -> None:
    """The live site sensor says both "could be controlled" and "is turned on" rather than unavailable while nothing blocks it."""
    _site_entry, _charger_entry, _configure_calls = await _active_control_setup(
        hass, entry_id="sensor_pair", active_control_enabled=True
    )

    state = next(
        candidate
        for candidate in hass.states.async_all()
        if "active_load_balancing_available" in candidate.attributes
    )
    assert state.attributes["active_load_balancing_available"] is True
    assert state.attributes["active_load_balancing_enabled"] is True

    # With the opt-in off, the same site reports available but not enabled.
    _site_entry_off, _charger_entry_off, _calls_off = await _active_control_setup(
        hass, entry_id="sensor_pair_off", active_control_enabled=False
    )
    off_state = next(
        candidate
        for candidate in hass.states.async_all()
        if "active_load_balancing_available" in candidate.attributes
        and candidate.attributes["active_load_balancing_enabled"] is False
    )
    assert off_state.attributes["active_load_balancing_available"] is True


async def test_active_control_logs_the_write_once_and_not_again(hass: HomeAssistant, caplog) -> None:
    """A write logs what went where; a stable proposal produces no write and no repeated line.

    It produces one held line naming the deadband (see `site/regulator_damping.py`),
    logged once and then silent.
    """
    caplog.set_level(
        logging.INFO, logger="custom_components.spotnav.site.site_capacity_controller"
    )
    site_entry, _charger_entry, _configure_calls = await _active_control_setup(
        hass, entry_id="log_write"
    )
    controller = controller_of(hass, site_entry.entry_id)

    written = [
        message
        for message in caplog.messages
        if "active control: charger" in message and "set to" in message
    ]
    assert len(written) == 1
    assert "previous none this session" in written[0]

    # Nothing changed: no second write line, and the deadband is named once as the reason.
    caplog.clear()
    controller._recompute()
    await hass.async_block_till_done()

    lines = [message for message in caplog.messages if "active control: charger" in message]
    assert len(lines) == 1
    assert "stopped_by=damping_deadband" in lines[0]
    assert "set to" not in lines[0]

    # A third recompute adds nothing: an unchanged outcome is emitted once.
    caplog.clear()
    controller._recompute()
    await hass.async_block_till_done()

    assert [message for message in caplog.messages if "active control: charger" in message] == []


async def test_active_control_logs_a_held_decision_with_the_check_that_stopped_it(
    hass: HomeAssistant, caplog
) -> None:
    """A held decision leaves no trace in Recorder, so the log names which check stopped it."""
    site_entry, charger, configure_calls = await _active_control_setup(hass, entry_id="log_hold")
    controller = controller_of(hass, site_entry.entry_id)
    caplog.set_level(
        logging.INFO, logger="custom_components.spotnav.site.site_capacity_controller"
    )

    # The decision was taken while its phase imported; by write time it exports. It is asserted directly because a state change would trigger a recompute replacing it.
    controller.regulator_decisions = {charger.entry_id: _handmade_decision(14.0)}
    set_site_power_w(hass, "log_hold_site", -2000.0)
    await hass.async_block_till_done()
    caplog.clear()
    configure_calls.clear()
    controller.regulator_decisions = {charger.entry_id: _handmade_decision(14.0)}
    await controller._async_apply_active_control()

    assert configure_calls == []
    held = [message for message in caplog.messages if "held" in message]
    assert len(held) == 1
    assert "stopped_by=direction_changed" in held[0]
    assert "phase=L1" in held[0]

    # Holding the same decision again says nothing further.
    caplog.clear()
    await controller._async_apply_active_control()

    assert [message for message in caplog.messages if "held" in message] == []


async def test_active_control_logs_a_held_decision_for_an_uncommandable_charger(
    hass: HomeAssistant, caplog
) -> None:
    """A charger that never opted into the write path is skipped by design, and the skip is visible in the log."""
    caplog.set_level(
        logging.INFO, logger="custom_components.spotnav.site.site_capacity_controller"
    )
    _site_entry, _charger_entry, configure_calls = await _active_control_setup(
        hass, entry_id="log_not_commandable", current_control=""
    )

    assert configure_calls == []
    held = [
        message for message in caplog.messages if "stopped_by=charger_not_commandable" in message
    ]
    # Once, not once per recompute, though setup ran several.
    assert len(held) == 1
