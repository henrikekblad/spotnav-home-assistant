"""Config-flow tests for the *site's own* current source, entered by hand.

The charger side is in `test_config_flow_manual_measured_source.py`; this covers the site's own
total current entered as **one entity plus its per-phase attribute names**, in both the create and
options flows, and the suggestions step always being shown when reached -- a site with nothing
discoverable is exactly the case manual entry exists for.

Recorder-backed tests here need the plugin's `recorder_mock`, and that fixture
must be resolved *before* `hass` (the plugin's own `recorder_db_url` asserts
`hass` has not started yet). The project-wide `auto_enable_custom_integrations`
fixture in `tests/conftest.py` depends on `hass`, so it would otherwise always
win that ordering race; the module-level override below is shadowed into place
for this file only, so no existing test module's fixture order changes.
"""

from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType, InvalidData
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_mock_service
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.spotnav.flows import SpotNavChargingConfigFlow
from custom_components.spotnav.flows.labels import (
    MANUAL_SOURCE_UNVERIFIED_ERROR,
    SITE_MANUAL_SOURCE_DEVICE_ERROR,
)
from custom_components.spotnav.const import (
    CONF_BATTERY_AGGREGATE_POWER_ENTITY,
    CONF_CHARGER_ENTRY_IDS,
    CONF_DERIVED_ENTITIES,
    CONF_DIRECT_ENTITIES,
    CONF_ENTRY_TYPE,
    CONF_MAIN_FUSE_A,
    CONF_MAX_AGE_S,
    CONF_REGULATOR_DEADBAND_A,
    CONF_REGULATOR_DWELL_S,
    CONF_MEASURED_CURRENT_SOURCE,
    CONF_MEASUREMENT_MODE,
    CONF_PHASE_WIRING,
    CONF_SAFETY_MARGIN_A,
    CONF_SITE_CURRENT_SOURCE,
    CONF_SITE_ENABLED,
    DEFAULT_MAX_AGE_S,
    DEFAULT_REGULATOR_DEADBAND_A,
    DEFAULT_REGULATOR_DWELL_S,
    DOMAIN,
    ENTRY_TYPE_SITE,
    MEASUREMENT_MODE_DIRECT,
)
from custom_components.spotnav.vehicles.discovery import _HISTORY_LOOKBACK_DAYS

from .helpers import create_ocpp_charger_device, make_entry, make_ocpp_config_entry, make_site_entry


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(recorder_db_url: str, enable_custom_integrations) -> None:
    """The `tests/conftest.py` fixture, with `recorder_db_url` resolved
    first -- see this module's docstring for why that ordering is required
    (and why it is confined to this file).
    """
    yield


def _manual_payload(
    entity_id: str,
    *,
    l1: str = "L1",
    l2: str = "L2",
    l3: str = "L3",
    unit: str | None = None,
    confirm: bool = False,
) -> dict:
    """One submission of a manual-entry form, exactly as the frontend would
    send it; `unit=None` omits the field so the form's own default applies,
    and `confirm` is only sent once the warning form has offered it (Home
    Assistant re-validates every submission against the schema that rendered
    the current step). Mirrors the charger-side file's own local helper.
    """
    payload: dict = {
        "entity_id": entity_id,
        "attribute_L1": l1,
        "attribute_L2": l2,
        "attribute_L3": l3,
    }
    if unit is not None:
        payload["unit"] = unit
    if confirm:
        payload["confirm_unverified"] = True
    return payload


def _field(schema, name: str):
    return next(key for key in schema.schema if str(key) == name)


def _default(schema, name: str):
    default = _field(schema, name).default
    return default() if callable(default) else default


def _suggested(schema, name: str):
    return (_field(schema, name).description or {}).get("suggested_value")


def _choice_values(schema) -> list[str]:
    """The stored values of a `choice` dropdown, in order."""
    options = schema.schema[_field(schema, "choice")].config["options"]
    return [option["value"] for option in options]


def _site_basics(charger_entry_ids: list[str]) -> dict:
    return {
        "name": "Manual site",
        "main_fuse_a": 25,
        "safety_margin_a": 1,
        "measurement_mode": "direct_phase_current",
        "charger_entry_ids": list(charger_entry_ids),
    }


def _options_basics(charger_entry_ids: list[str], *, site_enabled: bool = True) -> dict:
    """One `site_init` submission for the options flow, mirroring what the
    site entries below already store (so a roundtrip save changes nothing
    else).
    """
    return {
        "main_fuse_a": 25.0,
        "safety_margin_a": 1.0,
        "measurement_mode": "direct_phase_current",
        "charger_entry_ids": list(charger_entry_ids),
        "site_enabled": site_enabled,
        "change_measurement": True,
        "max_age_s": DEFAULT_MAX_AGE_S,
    }


async def _to_site_current_suggestions(
    hass: HomeAssistant, *, charger_entry_ids: list[str]
) -> dict:
    """`user` -> `site` -> the site-current suggestions step (always shown in
    direct mode, whether or not anything was discovered).
    """
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"entry_type": "site"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _site_basics(charger_entry_ids)
    )
    assert result["step_id"] == "site_current_suggestions"
    return result


def _meter(
    hass: HomeAssistant,
    *,
    owner,
    unique_id: str,
    phase_attributes: dict | None = None,
    unit: str = "A",
) -> str:
    """A grid-meter-style current sensor (never on a charger's device), left
    idle unless `phase_attributes` says otherwise.
    """
    entry = er.async_get(hass).async_get_or_create(
        "sensor",
        "ocpp",
        f"{unique_id}_current",
        config_entry=owner,
        suggested_object_id=unique_id,
    )
    hass.states.async_set(
        entry.entity_id,
        "0",
        {"device_class": "current", "unit_of_measurement": unit, **(phase_attributes or {})},
    )
    return entry.entity_id


def _charger_with_sensor(
    hass: HomeAssistant, *, owner, unique_id: str, phase_attributes: dict | None = None
):
    """One charger device with its own current sensor: `(charger_entry, sensor)`."""
    switch_entity_id = create_ocpp_charger_device(
        hass,
        ocpp_entry=owner,
        device_unique_id=unique_id,
        switch_object_id=unique_id,
        current_attributes={},
    )
    charger = make_entry(
        hass,
        entry_id=unique_id,
        charge_control=switch_entity_id,
        current_limit=None,
        webhook_id=f"webhook-{unique_id}",
        title=unique_id,
    )
    sensor_entity_id = er.async_get(hass).async_get_entity_id(
        "sensor", "ocpp", f"{unique_id}_current"
    )
    assert sensor_entity_id is not None
    hass.states.async_set(
        sensor_entity_id,
        "0",
        {"device_class": "current", "unit_of_measurement": "A", **(phase_attributes or {})},
    )
    return charger, sensor_entity_id


def _site_entry_ids(hass: HomeAssistant) -> set[str]:
    return {entry.entry_id for entry in hass.config_entries.async_entries(DOMAIN)}


def _stored_site_source(hass: HomeAssistant, site_entry_id: str) -> dict | None:
    return hass.config_entries.async_get_entry(site_entry_id).data.get(CONF_SITE_CURRENT_SOURCE)


async def _record_past_session_with_phase_attributes(
    hass: HomeAssistant, entity_id: str, phase_attributes: dict
) -> None:
    """One past session, then idle again -- see `vehicles/discovery.py`."""
    hass.states.async_set(
        entity_id,
        "6.2",
        {"device_class": "current", "unit_of_measurement": "A", **phase_attributes},
    )
    await async_wait_recording_done(hass)
    hass.states.async_set(
        entity_id, "0", {"device_class": "current", "unit_of_measurement": "A"}
    )
    await async_wait_recording_done(hass)


# --- The boundary: a charger's own sensor can never be the site total -----


async def test_site_manual_source_step_itself_rejects_an_entity_on_a_chargers_device(
    recorder_mock, hass: HomeAssistant
) -> None:
    """THE device-exclusion boundary test, at the step itself.

    The picker's `include_entities` list is only a narrowing, and Home
    Assistant re-validates a submission against the *rendered* schema before
    a step runs -- so this calls the step method directly with an entity on
    one of the site's own chargers' devices, bypassing that earlier layer,
    and proves the registry-based scope check rejects it on its own.
    """
    owner = make_ocpp_config_entry(hass, entry_id="owner_site_boundary")
    charger, charger_sensor = _charger_with_sensor(
        hass,
        owner=owner,
        unique_id="charger_site_boundary",
        phase_attributes={"L1": 9.0, "L2": 9.0, "L3": 9.0},
    )
    meter_entity_id = _meter(hass, owner=owner, unique_id="site_meter_boundary")
    registry = er.async_get(hass)
    # Sanity: the two are structurally identical, only their devices differ.
    assert (
        registry.async_get(charger_sensor).device_id
        != registry.async_get(meter_entity_id).device_id
    )

    flow = SpotNavChargingConfigFlow()
    flow.hass = hass
    flow.flow_id = "site-manual-source-direct-step"
    flow.handler = DOMAIN
    flow._site_basic = _site_basics([charger.entry_id])

    result = await flow.async_step_site_manual_source(_manual_payload(charger_sensor))

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "site_manual_source"
    assert result["errors"] == {"entity_id": SITE_MANUAL_SOURCE_DEVICE_ERROR}
    # Nothing was resolved for the site: no source, no choice.
    assert flow._site_current_source is None
    assert flow._site_current_source_choice is None

    # ...while the same submission naming a non-charger entity gets as far as
    # being verified (it simply is not verified yet).
    accepted = await flow.async_step_site_manual_source(_manual_payload(meter_entity_id))
    assert accepted["errors"] == {"base": MANUAL_SOURCE_UNVERIFIED_ERROR}
    assert flow._site_current_source is None


async def test_site_manual_source_via_the_flow_never_stores_a_chargers_entity(
    recorder_mock, hass: HomeAssistant
) -> None:
    """The same boundary end to end: a charger's own sensor is rejected,
    nothing is created, and the site can then be completed with a meter
    entity instead -- saved exactly as given.
    """
    owner = make_ocpp_config_entry(hass, entry_id="owner_site_boundary_flow")
    charger, charger_sensor = _charger_with_sensor(
        hass, owner=owner, unique_id="charger_site_boundary_flow"
    )
    meter_entity_id = _meter(hass, owner=owner, unique_id="site_meter_boundary_flow")

    result = await _to_site_current_suggestions(hass, charger_entry_ids=[charger.entry_id])
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"choice": "manual_attributes"}
    )
    assert result["step_id"] == "site_manual_source"
    entries_before = _site_entry_ids(hass)

    with pytest.raises(InvalidData):
        await hass.config_entries.flow.async_configure(
            result["flow_id"], _manual_payload(charger_sensor)
        )

    assert _site_entry_ids(hass) == entries_before

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _manual_payload(meter_entity_id)
    )
    assert result["errors"] == {"base": MANUAL_SOURCE_UNVERIFIED_ERROR}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _manual_payload(meter_entity_id, confirm=True)
    )
    assert result["step_id"] == "site_details"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["step_id"] == "site_charger_wiring"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert _stored_site_source(hass, result["result"].entry_id) == {
        "kind": "attributes",
        "entity_ids": None,
        "entity_id": meter_entity_id,
        "attributes": {"L1": "L1", "L2": "L2", "L3": "L3"},
        "attribute_unit_override": "A",
        "trust_entity_unit_for_attributes": False,
    }


# --- Verified vs unverifiable submissions ---------------------------------


async def test_a_verified_site_manual_source_saves_without_the_unverified_confirm(
    hass: HomeAssistant,
) -> None:
    """A meter whose live state really carries the submitted attribute names
    is verified: no warning, no checkbox, saved straight away.
    """
    owner = make_ocpp_config_entry(hass, entry_id="owner_site_live_verified")
    meter_entity_id = _meter(
        hass,
        owner=owner,
        unique_id="site_meter_live_verified",
        phase_attributes={"L1": 11.0, "L2": 12.0, "L3": 13.0},
    )

    result = await _to_site_current_suggestions(hass, charger_entry_ids=[])
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"choice": "manual_attributes"}
    )
    assert not any(str(key) == "confirm_unverified" for key in result["data_schema"].schema)

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _manual_payload(meter_entity_id)
    )
    assert result["step_id"] == "site_details"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert _stored_site_source(hass, result["result"].entry_id) == {
        "kind": "attributes",
        "entity_ids": None,
        "entity_id": meter_entity_id,
        "attributes": {"L1": "L1", "L2": "L2", "L3": "L3"},
        "attribute_unit_override": "A",
        "trust_entity_unit_for_attributes": False,
    }
    assert result["data"][CONF_DIRECT_ENTITIES] == {}


async def test_a_history_verified_site_manual_source_saves_without_the_unverified_confirm(
    recorder_mock, hass: HomeAssistant
) -> None:
    """The same, verified from Recorder history: an idle meter that reported
    per-phase data earlier has nothing to confirm either.
    """
    owner = make_ocpp_config_entry(hass, entry_id="owner_site_history_verified")
    meter_entity_id = _meter(hass, owner=owner, unique_id="site_meter_history_verified")
    await _record_past_session_with_phase_attributes(
        hass, meter_entity_id, {"L1": 7.0, "L2": 7.0, "L3": 7.0}
    )

    result = await _to_site_current_suggestions(hass, charger_entry_ids=[])
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"choice": "manual_attributes"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _manual_payload(meter_entity_id)
    )
    assert result["step_id"] == "site_details"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert _stored_site_source(hass, result["result"].entry_id)["entity_id"] == meter_entity_id


async def test_an_unverifiable_site_manual_source_warns_and_requires_the_confirm(
    recorder_mock, hass: HomeAssistant
) -> None:
    """A brand-new meter -- nothing live matching, nothing in history -- still
    saves, but only once the warning has been explicitly confirmed, and with
    exactly the entity/attributes/unit submitted.
    """
    owner = make_ocpp_config_entry(hass, entry_id="owner_site_unverified")
    meter_entity_id = _meter(hass, owner=owner, unique_id="site_meter_unverified")

    result = await _to_site_current_suggestions(hass, charger_entry_ids=[])
    entries_before = _site_entry_ids(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"choice": "manual_attributes"}
    )
    payload = _manual_payload(meter_entity_id, l1="amps_a", l2="amps_b", l3="amps_c", unit="mA")
    result = await hass.config_entries.flow.async_configure(result["flow_id"], payload)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "site_manual_source"
    assert result["errors"] == {"base": MANUAL_SOURCE_UNVERIFIED_ERROR}
    assert result["description_placeholders"] == {"lookback_days": str(_HISTORY_LOOKBACK_DAYS)}
    assert _site_entry_ids(hass) == entries_before

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**payload, "confirm_unverified": True}
    )
    assert result["step_id"] == "site_details"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert _stored_site_source(hass, result["result"].entry_id) == {
        "kind": "attributes",
        "entity_ids": None,
        "entity_id": meter_entity_id,
        "attributes": {"L1": "amps_a", "L2": "amps_b", "L3": "amps_c"},
        "attribute_unit_override": "mA",
        "trust_entity_unit_for_attributes": False,
    }


# --- Options flow: the site's own source is editable, nothing else moves --


async def test_options_flow_roundtrip_preserves_a_manual_attributes_site_source(
    recorder_mock, hass: HomeAssistant
) -> None:
    """Reopening the options flow for a site whose current source is already
    the manual-attributes shape must not reset it to anything else: the
    dropdown comes back on that choice, the sub-form is pre-filled from the
    stored value, and saving without changes leaves the entry identical.
    """
    owner = make_ocpp_config_entry(hass, entry_id="owner_site_options_roundtrip")
    meter_entity_id = _meter(hass, owner=owner, unique_id="site_meter_options_roundtrip")
    stored = {
        "kind": "attributes",
        "entity_ids": None,
        "entity_id": meter_entity_id,
        "attributes": {"L1": "amps_a", "L2": "amps_b", "L3": "amps_c"},
        # Deliberately different from the entity's own live unit ("A"), so the
        # pre-fill can only come from the stored value.
        "attribute_unit_override": "mA",
        "trust_entity_unit_for_attributes": False,
    }
    # Built explicitly rather than through `make_site_entry`, which
    # substitutes placeholder direct entities for an empty mapping -- this
    # test compares the whole entry's data before and after.
    entry = MockConfigEntry(
        domain=DOMAIN,
        entry_id="site_options_roundtrip",
        title="Site",
        data={
            CONF_ENTRY_TYPE: ENTRY_TYPE_SITE,
            CONF_SITE_ENABLED: True,
            CONF_MAIN_FUSE_A: 25.0,
            CONF_SAFETY_MARGIN_A: 1.0,
            CONF_MEASUREMENT_MODE: MEASUREMENT_MODE_DIRECT,
            CONF_CHARGER_ENTRY_IDS: [],
            CONF_PHASE_WIRING: {},
            CONF_DIRECT_ENTITIES: {},
            CONF_DERIVED_ENTITIES: {},
            CONF_MAX_AGE_S: DEFAULT_MAX_AGE_S,
            CONF_SITE_CURRENT_SOURCE: stored,
            CONF_BATTERY_AGGREGATE_POWER_ENTITY: "",
            # What the create path writes for active control's damping, so this
            # entry is exactly the shape a real site has (this test compares the
            # whole data dict before and after an options round-trip).
            CONF_REGULATOR_DEADBAND_A: DEFAULT_REGULATOR_DEADBAND_A,
            CONF_REGULATOR_DWELL_S: DEFAULT_REGULATOR_DWELL_S,
        },
    )
    entry.add_to_hass(hass)
    data_before = dict(entry.data)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], _options_basics([])
    )

    assert result["step_id"] == "site_current_suggestions"
    assert _default(result["data_schema"], "choice") == "manual_attributes"

    result = await hass.config_entries.options.async_configure(result["flow_id"], {})

    assert result["step_id"] == "site_manual_source"
    schema = result["data_schema"]
    assert _suggested(schema, "entity_id") == meter_entity_id
    assert _default(schema, "attribute_L1") == "amps_a"
    assert _default(schema, "attribute_L2") == "amps_b"
    assert _default(schema, "attribute_L3") == "amps_c"
    assert _default(schema, "unit") == "mA"

    # Submitted exactly as the form shows it: unverifiable under those names,
    # so the warning shows once and must be confirmed.
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"entity_id": meter_entity_id}
    )
    assert result["errors"] == {"base": MANUAL_SOURCE_UNVERIFIED_ERROR}
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"entity_id": meter_entity_id, "confirm_unverified": True}
    )
    assert result["step_id"] == "site_details"
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})

    assert result["type"] is FlowResultType.CREATE_ENTRY
    updated = hass.config_entries.async_get_entry(entry.entry_id)
    assert updated.data[CONF_SITE_CURRENT_SOURCE] == stored
    assert dict(updated.data) == data_before


async def test_editing_the_site_source_leaves_charger_sources_and_wiring_untouched(
    recorder_mock, hass: HomeAssistant
) -> None:
    """Editing the site's own current source must not disturb anything else:
    an associated charger's own manually entered measured-current source, and
    the site's phase wiring for it, both survive the same save unchanged --
    the mirror image of the charger-side roundtrip test.
    """
    owner = make_ocpp_config_entry(hass, entry_id="owner_site_edit_only")
    charger, charger_sensor = _charger_with_sensor(
        hass, owner=owner, unique_id="charger_site_edit_only"
    )
    old_meter_entity_id = _meter(hass, owner=owner, unique_id="site_meter_old")
    new_meter_entity_id = _meter(
        hass,
        owner=owner,
        unique_id="site_meter_new",
        phase_attributes={"L1": 4.0, "L2": 5.0, "L3": 6.0},
    )
    charger_source = {
        "kind": "attributes",
        "entity_ids": None,
        "entity_id": charger_sensor,
        "attributes": {"L1": "amps_a", "L2": "amps_b", "L3": "amps_c"},
        "attribute_unit_override": "A",
        "trust_entity_unit_for_attributes": False,
    }
    stored_site_source = {
        "kind": "attributes",
        "entity_ids": None,
        "entity_id": old_meter_entity_id,
        "attributes": {"L1": "amps_a", "L2": "amps_b", "L3": "amps_c"},
        "attribute_unit_override": "A",
        "trust_entity_unit_for_attributes": False,
    }
    phase_wiring = {
        charger.entry_id: {
            "phases": 3,
            "phase": None,
            CONF_MEASURED_CURRENT_SOURCE: charger_source,
        }
    }
    entry = make_site_entry(
        hass,
        entry_id="site_edit_only",
        charger_entry_ids=[charger.entry_id],
        phase_wiring=phase_wiring,
        direct_entities={},
        site_current_source=stored_site_source,
        battery_aggregate_power_entity="",
    )
    async_mock_service(hass, "switch", "turn_on")
    async_mock_service(hass, "switch", "turn_off")
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], _options_basics([charger.entry_id])
    )

    assert result["step_id"] == "site_current_suggestions"
    assert _default(result["data_schema"], "choice") == "manual_attributes"

    # A different entity, with the attribute names it really exposes: that
    # verifies, so the site's own source saves without any confirm step.
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    assert result["step_id"] == "site_manual_source"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], _manual_payload(new_meter_entity_id)
    )

    # The site's own step is done; the charger whose own source is
    # manual-attributes-shaped is collected exactly as it always was.
    assert result["step_id"] == "site_details"
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    assert result["step_id"] == "site_charger_wiring"
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    assert result["step_id"] == "charger_manual_source"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"entity_id": charger_sensor}
    )
    assert result["errors"] == {"base": MANUAL_SOURCE_UNVERIFIED_ERROR}
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"entity_id": charger_sensor, "confirm_unverified": True}
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    updated = hass.config_entries.async_get_entry(entry.entry_id)
    assert updated.data[CONF_SITE_CURRENT_SOURCE] == {
        "kind": "attributes",
        "entity_ids": None,
        "entity_id": new_meter_entity_id,
        "attributes": {"L1": "L1", "L2": "L2", "L3": "L3"},
        "attribute_unit_override": "A",
        "trust_entity_unit_for_attributes": False,
    }
    assert updated.data[CONF_PHASE_WIRING] == phase_wiring


async def test_the_existing_separate_entities_manual_path_is_unchanged_in_both_flows(
    hass: HomeAssistant,
) -> None:
    """The pre-existing "manual" choice -- three whole entities, one per
    phase -- keeps working: the create flow writes
    `CONF_DIRECT_ENTITIES` and no generic source, and re-saving that site
    through the options flow leaves everything identical.
    """
    result = await _to_site_current_suggestions(hass, charger_entry_ids=[])

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"choice": "manual"}
    )
    assert result["step_id"] == "site_details"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "direct_L1": "sensor.separate_l1",
            "direct_L2": "sensor.separate_l2",
            "direct_L3": "sensor.separate_l3",
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    site_entry_id = result["result"].entry_id
    assert result["data"][CONF_SITE_CURRENT_SOURCE] is None
    assert result["data"][CONF_DIRECT_ENTITIES] == {
        "L1": "sensor.separate_l1",
        "L2": "sensor.separate_l2",
        "L3": "sensor.separate_l3",
    }

    entry = hass.config_entries.async_get_entry(site_entry_id)
    data_before = dict(entry.data)
    result = await hass.config_entries.options.async_init(site_entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], _options_basics([], site_enabled=True)
    )

    # Nothing is stored, so "manual" (the guided pickers) is again the
    # starting choice, and the pickers come back pre-filled from
    # `CONF_DIRECT_ENTITIES`.
    assert result["step_id"] == "site_current_suggestions"
    assert _default(result["data_schema"], "choice") == "manual"
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    assert result["step_id"] == "site_details"
    assert _default(result["data_schema"], "direct_L1") == "sensor.separate_l1"
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})

    assert result["type"] is FlowResultType.CREATE_ENTRY
    updated = hass.config_entries.async_get_entry(site_entry_id)
    assert updated.data[CONF_SITE_CURRENT_SOURCE] is None
    assert updated.data[CONF_DIRECT_ENTITIES] == data_before[CONF_DIRECT_ENTITIES]
    # Everything else is unchanged too -- except `CONF_BATTERY_AGGREGATE_POWER_ENTITY`,
    # which the options flow materialises (as "") even for a site created without the key; pinned
    # here rather than asserted away.
    assert dict(updated.data) == {
        **data_before,
        CONF_BATTERY_AGGREGATE_POWER_ENTITY: "",
    }
