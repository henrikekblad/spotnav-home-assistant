"""Config-flow tests for manually entering one charger's measured-current source.

Automatic discovery can find nothing for a brand-new charger (or one whose current sensor names its
per-phase attributes in an unrecognised way). These tests cover the manual fallback: one step, one
charger at a time, entity + per-phase attribute names + an explicit ampere unit, with the same
device-membership boundary automatic discovery uses.

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
from pytest_homeassistant_custom_component.common import async_mock_service
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.spotnav.flows import SpotNavChargingConfigFlow
from custom_components.spotnav.flows.labels import (
    MANUAL_SOURCE_ATTRIBUTES_ERROR,
    MANUAL_SOURCE_DEVICE_ERROR,
    MANUAL_SOURCE_ENTITY_ERROR,
    MANUAL_SOURCE_UNVERIFIED_ERROR,
)
from custom_components.spotnav.const import (
    CONF_DIRECT_ENTITIES,
    CONF_MEASURED_CURRENT_SOURCE,
    CONF_PHASE_WIRING,
    CONF_SITE_CURRENT_SOURCE,
    DEFAULT_MAX_AGE_S,
    DOMAIN,
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


_UNUSED_SITE_CURRENT = {
    "direct_L1": "sensor.unused_l1",
    "direct_L2": "sensor.unused_l2",
    "direct_L3": "sensor.unused_l3",
}


def _manual_payload(
    entity_id: str,
    *,
    l1: str = "L1",
    l2: str = "L2",
    l3: str = "L3",
    unit: str | None = None,
    confirm: bool = False,
) -> dict:
    """One submission of the charger manual-entry form, exactly as the
    frontend would send it.

    `unit=None` deliberately omits the field, so the form's own default
    applies -- the realistic "user never touched it" case. `confirm` is only
    ever sent when it is `True`: Home Assistant re-validates every submission
    against the schema that rendered the *current* step, so sending the
    checkbox before the warning form has offered it is rejected as an unknown
    option (which is exactly what a user could not do either).
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
    # voluptuous stores a provided default as a zero-argument factory; an
    # unset one is the `...` sentinel.
    return default() if callable(default) else default


def _suggested(schema, name: str):
    return _field(schema, name).description.get("suggested_value")


def _charger_with_sensor(
    hass: HomeAssistant,
    *,
    owner,
    unique_id: str,
    phase_attributes: dict | None = None,
    unit: str = "A",
):
    """One OCPP-style charger device plus its own current sensor, left idle
    (no phase attributes) unless `phase_attributes` says otherwise. Returns
    `(charger_entry, sensor_entity_id)`.
    """
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
        {"device_class": "current", "unit_of_measurement": unit, **(phase_attributes or {})},
    )
    return charger, sensor_entity_id


async def _drive_create_flow_to_manual_step(
    hass: HomeAssistant, *, charger_entry_ids: list[str]
) -> dict:
    """`user` -> `site` -> (`site_current_suggestions` falling through) ->
    `site_details` -> the first charger's `site_charger_wiring` choosing "manual" -> its
    per-charger manual-entry step. Returns that step's flow result.
    """
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"entry_type": "site"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "name": "Manual site",
            "main_fuse_a": 25,
            "safety_margin_a": 1,
            "measurement_mode": "direct_phase_current",
            "charger_entry_ids": list(charger_entry_ids),
        },
    )
    # No site-current candidate exists in these tests (the only current-like
    # entities belong to charger devices, which site discovery excludes), so
    # the suggestions step is still shown -- its default ("manual", the
    # guided per-phase pickers) then reaches `site_details`.
    assert result["step_id"] == "site_current_suggestions"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["step_id"] == "site_details"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], _UNUSED_SITE_CURRENT)
    assert result["step_id"] == "site_charger_wiring"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"phases": 3, "measured_source": "manual"}
    )
    assert result["step_id"] == "charger_manual_source"
    return result


def _site_entry_ids(hass: HomeAssistant) -> set[str]:
    return {entry.entry_id for entry in hass.config_entries.async_entries(DOMAIN)}


def _options_basics(charger_entry_ids: list[str], *, site_enabled: bool = True) -> dict:
    """One `site_init` submission for the options flow, mirroring what
    `make_site_entry` already stored (so a roundtrip save changes nothing
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


def _stored_measured_source(
    hass: HomeAssistant, site_entry_id: str, charger_entry_id: str
) -> dict:
    entry = hass.config_entries.async_get_entry(site_entry_id)
    return entry.data[CONF_PHASE_WIRING][charger_entry_id][CONF_MEASURED_CURRENT_SOURCE]


async def _record_past_session_with_phase_attributes(
    hass: HomeAssistant, entity_id: str, phase_attributes: dict
) -> None:
    """One past session, then idle again -- the shape a charger's own current
    sensor has while it is not charging (see `vehicles/discovery.py`).
    """
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


# --- The core case: a charger nothing can be verified about ----------------


async def test_manual_source_for_a_brand_new_charger_saves_after_the_unverified_confirm(
    recorder_mock, hass: HomeAssistant
) -> None:
    """A charger that is idle, has no phase attributes and no history at all.

    Entering its entity and the three attribute names must be possible, must warn that nothing
    could be verified,
    and must not save until that warning is explicitly confirmed -- then it
    stores exactly what was submitted, in the same shape the automatic
    candidate path stores.
    """
    owner = make_ocpp_config_entry(hass, entry_id="owner_manual_core")
    charger, sensor_entity_id = _charger_with_sensor(
        hass, owner=owner, unique_id="charger_manual_core"
    )
    entries_before = _site_entry_ids(hass)

    result = await _drive_create_flow_to_manual_step(hass, charger_entry_ids=[charger.entry_id])

    # Nothing has been verified yet, so the first render must not already
    # offer the "save anyway" confirm.
    assert not any(str(key) == "confirm_unverified" for key in result["data_schema"].schema)

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _manual_payload(sensor_entity_id)
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "charger_manual_source"
    assert result["errors"] == {"base": MANUAL_SOURCE_UNVERIFIED_ERROR}
    assert result["description_placeholders"] == {"lookback_days": str(_HISTORY_LOOKBACK_DAYS)}
    # ...and nothing was created or stored anywhere.
    assert _site_entry_ids(hass) == entries_before
    assert any(str(key) == "confirm_unverified" for key in result["data_schema"].schema)

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _manual_payload(sensor_entity_id, confirm=True)
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert _stored_measured_source(hass, result["result"].entry_id, charger.entry_id) == {
        "kind": "attributes",
        "entity_ids": None,
        "entity_id": sensor_entity_id,
        "attributes": {"L1": "L1", "L2": "L2", "L3": "L3"},
        "attribute_unit_override": "A",
        "trust_entity_unit_for_attributes": False,
    }


# --- The non-negotiable boundary: only this charger's own device ----------


async def test_manual_source_step_itself_rejects_an_entity_from_another_device(
    recorder_mock, hass: HomeAssistant
) -> None:
    """THE device-membership boundary test, at the step itself.

    The picker's `include_entities` list is only a narrowing: Home Assistant
    re-validates every submission against the rendered schema before a step
    runs, but that is a *different* layer from this integration's own rule.
    This test calls the step method directly with a foreign entity, bypassing
    that earlier layer entirely, and proves the registry-based device check
    inside the step rejects it on its own -- never storing a source built
    from another device's sensor.
    """
    owner = make_ocpp_config_entry(hass, entry_id="owner_manual_boundary")
    charger, charger_sensor = _charger_with_sensor(
        hass, owner=owner, unique_id="charger_manual_boundary"
    )
    other_owner = make_ocpp_config_entry(hass, entry_id="owner_manual_boundary_other")
    _, house_sensor = _charger_with_sensor(
        hass,
        owner=other_owner,
        unique_id="house_meter_boundary",
        phase_attributes={"L1": 9.0, "L2": 9.0, "L3": 9.0},
    )
    registry = er.async_get(hass)
    # Sanity: the two sensors are structurally identical and only tellable
    # apart by the device they belong to.
    assert (
        registry.async_get(house_sensor).device_id
        != registry.async_get(charger_sensor).device_id
    )
    assert hass.states.get(house_sensor).attributes["L1"] == 9.0

    flow = SpotNavChargingConfigFlow()
    flow.hass = hass
    flow.flow_id = "manual-source-direct-step"
    flow.handler = DOMAIN
    flow._init_charger_wiring()
    flow._wiring_queue = [charger.entry_id]

    result = await flow.async_step_charger_manual_source(_manual_payload(house_sensor))

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "charger_manual_source"
    assert result["errors"] == {"entity_id": MANUAL_SOURCE_DEVICE_ERROR}
    assert flow._manual_measured_sources == {}

    # The same check, in the order it actually runs: something that is not a
    # usable sensor entity at all is rejected as such first, and a blank
    # attribute name is a hard error -- there is nothing to "confirm anyway"
    # when the form cannot describe a mapping in the first place.
    not_a_sensor = await flow.async_step_charger_manual_source(
        _manual_payload("switch.not_a_sensor")
    )
    assert not_a_sensor["errors"] == {"entity_id": MANUAL_SOURCE_ENTITY_ERROR}
    blank_attribute = await flow.async_step_charger_manual_source(
        _manual_payload(charger_sensor, l2="")
    )
    assert blank_attribute["errors"] == {"attribute_L2": MANUAL_SOURCE_ATTRIBUTES_ERROR}
    assert flow._manual_measured_sources == {}


async def test_manual_source_via_the_flow_never_stores_an_entity_from_another_device(
    recorder_mock, hass: HomeAssistant
) -> None:
    """The same boundary, end to end through the flow manager: a foreign
    entity is rejected, nothing at all is stored, and a following valid
    submission is saved exactly as given -- the bad input is therefore
    neither silently accepted nor silently rewritten into something else.

    Going through `async_configure` with an entity outside the picker's own
    list trips Home Assistant's schema validation first (the websocket layer
    turns that into a form error; the plain API raises `InvalidData`), and
    the step's own registry check covers the case where that first layer
    would not -- see the direct-step test above.
    """
    owner = make_ocpp_config_entry(hass, entry_id="owner_manual_boundary_flow")
    charger, charger_sensor = _charger_with_sensor(
        hass, owner=owner, unique_id="charger_manual_boundary_flow"
    )
    other_owner = make_ocpp_config_entry(hass, entry_id="owner_manual_boundary_flow_other")
    _, house_sensor = _charger_with_sensor(
        hass, owner=other_owner, unique_id="house_meter_boundary_flow"
    )

    result = await _drive_create_flow_to_manual_step(hass, charger_entry_ids=[charger.entry_id])
    entries_before = _site_entry_ids(hass)

    with pytest.raises(InvalidData):
        await hass.config_entries.flow.async_configure(
            result["flow_id"], _manual_payload(house_sensor)
        )

    assert _site_entry_ids(hass) == entries_before

    # The site can then be completed with its own device's sensor instead --
    # the rejected input left nothing behind.
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _manual_payload(charger_sensor)
    )
    assert result["errors"] == {"base": MANUAL_SOURCE_UNVERIFIED_ERROR}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _manual_payload(charger_sensor, confirm=True)
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert (
        _stored_measured_source(hass, result["result"].entry_id, charger.entry_id)["entity_id"]
        == charger_sensor
    )


# --- Verified submissions never see the warning at all --------------------


async def test_a_live_verified_manual_source_saves_without_the_unverified_confirm(
    hass: HomeAssistant,
) -> None:
    """An entity whose live state really does carry the submitted attribute
    names is verified, so the warning never appears and the submission saves
    straight away -- no confirm checkbox anywhere in the flow.
    """
    owner = make_ocpp_config_entry(hass, entry_id="owner_manual_live_verified")
    charger, sensor_entity_id = _charger_with_sensor(
        hass,
        owner=owner,
        unique_id="charger_manual_live_verified",
        phase_attributes={"L1": 6.1, "L2": 6.2, "L3": 6.3},
    )

    result = await _drive_create_flow_to_manual_step(hass, charger_entry_ids=[charger.entry_id])
    assert not any(str(key) == "confirm_unverified" for key in result["data_schema"].schema)

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _manual_payload(sensor_entity_id)
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert _stored_measured_source(hass, result["result"].entry_id, charger.entry_id) == {
        "kind": "attributes",
        "entity_ids": None,
        "entity_id": sensor_entity_id,
        "attributes": {"L1": "L1", "L2": "L2", "L3": "L3"},
        "attribute_unit_override": "A",
        "trust_entity_unit_for_attributes": False,
    }


async def test_a_history_verified_manual_source_saves_without_the_unverified_confirm(
    recorder_mock, hass: HomeAssistant
) -> None:
    """The same, verified from Recorder history instead: an idle charger that
    charged in the past has nothing to confirm either.
    """
    owner = make_ocpp_config_entry(hass, entry_id="owner_manual_history_verified")
    charger, sensor_entity_id = _charger_with_sensor(
        hass, owner=owner, unique_id="charger_manual_history_verified"
    )
    await _record_past_session_with_phase_attributes(
        hass, sensor_entity_id, {"L1": 7.0, "L2": 7.0, "L3": 7.0}
    )

    result = await _drive_create_flow_to_manual_step(hass, charger_entry_ids=[charger.entry_id])
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _manual_payload(sensor_entity_id)
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert (
        _stored_measured_source(hass, result["result"].entry_id, charger.entry_id)["entity_id"]
        == sensor_entity_id
    )


# --- The unit choice ------------------------------------------------------


async def test_the_unit_field_defaults_to_a_recognized_ampere_unit_never_a_wilder_one(
    recorder_mock, hass: HomeAssistant
) -> None:
    """The unit field offers exactly "A"/"mA" and starts on the selected
    entity's own unit only when `normalized_ampere_unit` recognizes it: an
    entity reporting "W" must fall back to "A" (a raw "W" would make the
    saved source permanently invalid), while one reporting "mA" must keep
    "mA". Both are checked on the re-render, which is the first point at
    which a specific entity is actually known.
    """
    owner = make_ocpp_config_entry(hass, entry_id="owner_manual_units")
    watts_charger, watts_sensor = _charger_with_sensor(
        hass, owner=owner, unique_id="charger_manual_watts", unit="W"
    )
    milliamps_charger, milliamps_sensor = _charger_with_sensor(
        hass, owner=owner, unique_id="charger_manual_milliamps", unit="mA"
    )

    result = await _drive_create_flow_to_manual_step(
        hass, charger_entry_ids=[watts_charger.entry_id, milliamps_charger.entry_id]
    )

    # First queued charger: an entity whose own unit is "W".
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _manual_payload(watts_sensor)
    )
    assert result["errors"] == {"base": MANUAL_SOURCE_UNVERIFIED_ERROR}
    unit_field = result["data_schema"].schema[_field(result["data_schema"], "unit")]
    assert list(unit_field.container) == ["A", "mA"]
    assert _default(result["data_schema"], "unit") == "A"

    # Submitting without touching the unit keeps that default, and moves the
    # queue on to the second charger.
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _manual_payload(watts_sensor, confirm=True)
    )
    assert result["step_id"] == "site_charger_wiring"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"phases": 3, "measured_source": "manual"}
    )
    assert result["step_id"] == "charger_manual_source"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _manual_payload(milliamps_sensor)
    )
    assert result["errors"] == {"base": MANUAL_SOURCE_UNVERIFIED_ERROR}
    assert _default(result["data_schema"], "unit") == "mA"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _manual_payload(milliamps_sensor, confirm=True)
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    site_entry_id = result["result"].entry_id
    assert (
        _stored_measured_source(hass, site_entry_id, watts_charger.entry_id)[
            "attribute_unit_override"
        ]
        == "A"
    )
    assert (
        _stored_measured_source(hass, site_entry_id, milliamps_charger.entry_id)[
            "attribute_unit_override"
        ]
        == "mA"
    )


# --- Options-flow roundtrips ---------------------------------------------


async def test_options_flow_roundtrip_preserves_a_manually_entered_charger_source(
    recorder_mock, hass: HomeAssistant
) -> None:
    """Reopening the options flow for a site whose charger already has a
    *manually* entered measured-current source must not reset it to "skip"
    or to some unrelated candidate: the select comes back on "manual", the
    sub-form is pre-filled from the stored value, and a save without changes
    stores exactly the same source again.
    """
    owner = make_ocpp_config_entry(hass, entry_id="owner_manual_roundtrip")
    charger, sensor_entity_id = _charger_with_sensor(
        hass, owner=owner, unique_id="charger_manual_roundtrip"
    )
    # Attribute names discovery cannot recognize, and a unit override that
    # deliberately differs from the entity's own live unit ("A") -- so the
    # pre-fill can only be coming from the *stored* value.
    stored = {
        "kind": "attributes",
        "entity_ids": None,
        "entity_id": sensor_entity_id,
        "attributes": {"L1": "amps_a", "L2": "amps_b", "L3": "amps_c"},
        "attribute_unit_override": "mA",
        "trust_entity_unit_for_attributes": False,
    }
    entry = make_site_entry(
        hass,
        entry_id="site_manual_roundtrip",
        charger_entry_ids=[charger.entry_id],
        phase_wiring={charger.entry_id: {"phases": 3, "phase": None, CONF_MEASURED_CURRENT_SOURCE: stored}},
        direct_entities={},
        battery_aggregate_power_entity="",
    )
    data_before = dict(entry.data)
    async_mock_service(hass, "switch", "turn_on")
    async_mock_service(hass, "switch", "turn_off")
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], _options_basics([charger.entry_id])
    )

    # Nothing is stored for the site's own current, so the suggestions step
    # starts on "manual" and the guided pickers are shown, exactly as before.
    assert result["step_id"] == "site_current_suggestions"
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    assert result["step_id"] == "site_details"
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    assert result["step_id"] == "site_charger_wiring"
    assert _default(result["data_schema"], "measured_source") == "manual"

    result = await hass.config_entries.options.async_configure(result["flow_id"], {})

    assert result["step_id"] == "charger_manual_source"
    schema = result["data_schema"]
    assert _suggested(schema, "entity_id") == sensor_entity_id
    assert _default(schema, "attribute_L1") == "amps_a"
    assert _default(schema, "attribute_L2") == "amps_b"
    assert _default(schema, "attribute_L3") == "amps_c"
    assert _default(schema, "unit") == "mA"
    assert not any(str(key) == "confirm_unverified" for key in schema.schema)

    # The entity is submitted exactly as the picker shows it; the attribute
    # names and unit come from the form's own pre-filled defaults. This
    # mapping is legitimately unverifiable (nothing live or historical
    # carries "amps_a"), so the warning shows once and is confirmed -- then
    # the stored configuration must be unchanged.
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"entity_id": sensor_entity_id}
    )
    assert result["errors"] == {"base": MANUAL_SOURCE_UNVERIFIED_ERROR}

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"entity_id": sensor_entity_id, "confirm_unverified": True}
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    updated = hass.config_entries.async_get_entry(entry.entry_id)
    assert updated.data[CONF_PHASE_WIRING][charger.entry_id][CONF_MEASURED_CURRENT_SOURCE] == stored
    assert updated.data == data_before


async def test_options_flow_roundtrip_leaves_other_chargers_and_the_site_source_untouched(
    recorder_mock, hass: HomeAssistant
) -> None:
    """Editing one charger's manual source must not disturb anything else:
    another charger's automatically discovered candidate selection and the
    site's own current source both survive the same save completely
    unchanged -- the same discipline the automatic-candidate roundtrip test
    already applies.
    """
    owner = make_ocpp_config_entry(hass, entry_id="owner_manual_untouched")
    manual_charger, manual_sensor = _charger_with_sensor(
        hass, owner=owner, unique_id="charger_manual_untouched"
    )
    discovered_charger, discovered_sensor = _charger_with_sensor(
        hass,
        owner=owner,
        unique_id="charger_discovered_untouched",
        phase_attributes={"L1": 8.0, "L2": 8.0, "L3": 8.0},
    )
    manual_source = {
        "kind": "attributes",
        "entity_ids": None,
        "entity_id": manual_sensor,
        "attributes": {"L1": "amps_a", "L2": "amps_b", "L3": "amps_c"},
        "attribute_unit_override": "A",
        "trust_entity_unit_for_attributes": False,
    }
    discovered_source = {
        "kind": "attributes",
        "entity_ids": None,
        "entity_id": discovered_sensor,
        "attributes": {"L1": "L1", "L2": "L2", "L3": "L3"},
        "attribute_unit_override": "A",
        "trust_entity_unit_for_attributes": False,
    }
    site_source = {
        "kind": "attributes",
        "entity_ids": None,
        "entity_id": "sensor.site_total_untouched",
        "attributes": {"L1": "L1", "L2": "L2", "L3": "L3"},
        "attribute_unit_override": "A",
        "trust_entity_unit_for_attributes": False,
    }
    # A real, still-discoverable entity for that stored site source: the
    # options flow now offers the site's own current back as the candidate it
    # actually is, and can only do that for an entity that still exists.
    site_meter = er.async_get(hass).async_get_or_create(
        "sensor",
        "ocpp",
        "site_total_untouched_current",
        config_entry=owner,
        suggested_object_id="site_total_untouched",
    )
    assert site_meter.entity_id == site_source["entity_id"]
    hass.states.async_set(
        site_meter.entity_id,
        "3.0",
        {"device_class": "current", "unit_of_measurement": "A", "L1": 1.0, "L2": 1.0, "L3": 1.0},
    )
    entry = make_site_entry(
        hass,
        entry_id="site_manual_untouched",
        charger_entry_ids=[manual_charger.entry_id, discovered_charger.entry_id],
        phase_wiring={
            manual_charger.entry_id: {
                "phases": 3,
                "phase": None,
                CONF_MEASURED_CURRENT_SOURCE: manual_source,
            },
            discovered_charger.entry_id: {
                "phases": 3,
                "phase": None,
                CONF_MEASURED_CURRENT_SOURCE: discovered_source,
            },
        },
        # The site's current comes from its own generic source, so no
        # direct-mode entities exist -- the state a real generic site is in.
        direct_entities={},
        site_current_source=site_source,
        battery_aggregate_power_entity="",
    )
    data_before = dict(entry.data)
    async_mock_service(hass, "switch", "turn_on")
    async_mock_service(hass, "switch", "turn_off")
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        _options_basics([manual_charger.entry_id, discovered_charger.entry_id]),
    )

    # The site's stored source is still discoverable, so the suggestions step
    # offers it back as that candidate -- not as a manual shape -- and the
    # guided pickers stay hidden, exactly as for a generic-source site before.
    assert result["step_id"] == "site_current_suggestions"
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    assert result["step_id"] == "site_details"
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    assert result["step_id"] == "site_charger_wiring"
    assert _default(result["data_schema"], "measured_source") == "manual"

    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    assert result["step_id"] == "charger_manual_source"
    # This mapping is legitimately unverifiable (nothing live or historical
    # carries "amps_a"), so the warning shows once and must be confirmed --
    # only then is the site saved, with everything else unchanged.
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"entity_id": manual_sensor}
    )
    assert result["errors"] == {"base": MANUAL_SOURCE_UNVERIFIED_ERROR}
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"entity_id": manual_sensor, "confirm_unverified": True}
    )
    # The second charger has its own wiring step, starting on its discovered source.
    assert result["step_id"] == "site_charger_wiring"
    assert _default(result["data_schema"], "measured_source") == discovered_sensor
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})

    assert result["type"] is FlowResultType.CREATE_ENTRY
    updated = hass.config_entries.async_get_entry(entry.entry_id)
    phase_wiring = updated.data[CONF_PHASE_WIRING]
    assert phase_wiring[manual_charger.entry_id][CONF_MEASURED_CURRENT_SOURCE] == manual_source
    assert (
        phase_wiring[discovered_charger.entry_id][CONF_MEASURED_CURRENT_SOURCE]
        == discovered_source
    )
    assert updated.data[CONF_SITE_CURRENT_SOURCE] == site_source
    # Everything else is unchanged except `direct_entities`: a site whose current comes from a
    # generic source never shows those fields, so the options flow writes the empty mapping.
    assert updated.data[CONF_DIRECT_ENTITIES] == {}
    assert {
        key: value for key, value in updated.data.items() if key != CONF_DIRECT_ENTITIES
    } == {key: value for key, value in data_before.items() if key != CONF_DIRECT_ENTITIES}


# --- End to end: creating the charger itself, then configuring it --------


async def test_create_flow_end_to_end_creates_a_charger_and_its_manual_measured_source(
    hass: HomeAssistant,
) -> None:
    """The whole path a real user walks, not just the manual step in
    isolation: create an OCPP charger through the flow, then a site that
    includes it, choose "manual" for that charger's own measured current,
    enter its entity and attribute names, confirm the unverified warning,
    and end up with exactly that source stored under the site entry's
    `phase_wiring`.
    """
    ocpp_owner = make_ocpp_config_entry(hass, entry_id="owner_manual_e2e")
    async_mock_service(hass, "switch", "turn_on")
    async_mock_service(hass, "switch", "turn_off")
    switch_entity_id = create_ocpp_charger_device(
        hass,
        ocpp_entry=ocpp_owner,
        device_unique_id="charger_manual_e2e",
        switch_object_id="charger_manual_e2e",
        current_attributes={},
    )

    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"entry_type": "charger"}
    )
    async_mock_service(hass, "ocpp", "get_configuration", response={"value": None})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"mode": "detected"})
    assert result["step_id"] == "detect_device"
    charger_device_id = er.async_get(hass).async_get(switch_entity_id).device_id
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"device": charger_device_id}
    )
    assert result["step_id"] == "ocpp_entities"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"charge_control": switch_entity_id}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    charger_entry_id = result["result"].entry_id
    await hass.async_block_till_done()

    sensor_entity_id = er.async_get(hass).async_get_entity_id(
        "sensor", "ocpp", "charger_manual_e2e_current"
    )
    assert sensor_entity_id is not None

    result = await _drive_create_flow_to_manual_step(hass, charger_entry_ids=[charger_entry_id])
    # Attribute names discovery has never seen -- exactly the "automatic
    # detection cannot help me here" case this manual path exists for.
    payload = _manual_payload(sensor_entity_id, l1="amps_a", l2="amps_b", l3="amps_c")
    result = await hass.config_entries.flow.async_configure(result["flow_id"], payload)
    assert result["errors"] == {"base": MANUAL_SOURCE_UNVERIFIED_ERROR}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**payload, "confirm_unverified": True}
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert _stored_measured_source(hass, result["result"].entry_id, charger_entry_id) == {
        "kind": "attributes",
        "entity_ids": None,
        "entity_id": sensor_entity_id,
        "attributes": {"L1": "amps_a", "L2": "amps_b", "L3": "amps_c"},
        "attribute_unit_override": "A",
        "trust_entity_unit_for_attributes": False,
    }
