"""Recorder-history fallback tests for measurement-source discovery.

Some chargers expose per-phase attributes (`L1`/`L2`/`L3`) only *while a session is running*, so
discovery must be able to find them from *history*, not only live state.

Recorder-backed tests need `recorder_mock` (the plugin's in-memory Recorder), resolved *before*
`hass`: the plugin asserts `hass` has not been set up yet. The project-wide
`auto_enable_custom_integrations` fixture in `tests/conftest.py` depends on `hass`, so the
module-level override below shadows it for this file only.
"""

from __future__ import annotations

import time

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr, entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.spotnav.vehicles import discovery as discovery_module
from custom_components.spotnav.const import (
    CONF_MEASURED_CURRENT_SOURCE,
    CONF_PHASE_WIRING,
    DOMAIN,
)
from custom_components.spotnav.vehicles.discovery import (
    REASON_ATTRIBUTES_DEVICE_CLASS_AND_UNIT_MATCH,
    REASON_ATTRIBUTES_HISTORICAL_MATCH,
    async_discover_charger_current_sources,
    async_discover_site_current_sources,
    discover_charger_current_sources,
)

from .helpers import create_ocpp_charger_device, make_entry, make_ocpp_config_entry


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(recorder_db_url: str, enable_custom_integrations) -> None:
    """The `tests/conftest.py` fixture, with `recorder_db_url` resolved
    first -- see this module's docstring for why that ordering is required
    (and why it is confined to this file).
    """
    yield


def _owner_entry(hass: HomeAssistant, *, entry_id: str) -> MockConfigEntry:
    entry = MockConfigEntry(domain="test", entry_id=entry_id, title="Test owner")
    entry.add_to_hass(hass)
    return entry


def _device(hass: HomeAssistant, *, owner: MockConfigEntry, unique_id: str, name: str) -> str:
    device_registry = dr.async_get(hass)
    device = device_registry.async_get_or_create(
        config_entry_id=owner.entry_id, identifiers={("test", unique_id)}, name=name
    )
    return device.id


def _sensor(
    hass: HomeAssistant,
    *,
    owner: MockConfigEntry,
    device_id: str,
    unique_id: str,
    object_id: str,
    device_class: str = "current",
    unit: str = "A",
) -> str:
    """Register a current sensor on `device_id` and give it an *idle* state:
    a valid device class and unit, but no phase attributes at all.
    """
    entity_registry = er.async_get(hass)
    entry = entity_registry.async_get_or_create(
        "sensor",
        "test",
        unique_id,
        device_id=device_id,
        config_entry=owner,
        suggested_object_id=object_id,
    )
    hass.states.async_set(
        entry.entity_id, "0", {"device_class": device_class, "unit_of_measurement": unit, "connector_id": 1}
    )
    return entry.entity_id


async def _record_one_charging_session(
    hass: HomeAssistant, entity_id: str, *, phase_attributes: dict
) -> None:
    """Give `entity_id` one past charging session (phase attributes present),
    then leave it idle again (attributes gone) -- the exact shape a real
    OCPP charger's current-import sensor has.

    Waiting for Recorder after each state change is mandatory, not cosmetic:
    Recorder persists asynchronously, so a history lookup issued before it
    has caught up would see nothing at all and make these tests pass for the
    wrong reason.
    """
    hass.states.async_set(
        entity_id,
        "6.2",
        {"device_class": "current", "unit_of_measurement": "A", "connector_id": 1, **phase_attributes},
    )
    await async_wait_recording_done(hass)
    hass.states.async_set(
        entity_id, "0", {"device_class": "current", "unit_of_measurement": "A", "connector_id": 1}
    )
    await async_wait_recording_done(hass)


async def test_phase_attributes_seen_only_in_history_are_discovered(
    recorder_mock, hass: HomeAssistant
) -> None:
    """At the discovery level: an idle charger whose
    phase attributes only ever appeared during a past charging session must
    still be offered as a candidate -- and clearly labelled as a
    *historical* match, never as a currently confirmed one.
    """
    owner = _owner_entry(hass, entry_id="owner_history_core")
    charger_device = _device(hass, owner=owner, unique_id="charger_history", name="Charger")
    entity_id = _sensor(
        hass, owner=owner, device_id=charger_device, unique_id="charger_history_current",
        object_id="charger_history_current",
    )
    await _record_one_charging_session(
        hass, entity_id, phase_attributes={"L1": 6.2, "L2": 6.1, "L3": 6.0}
    )

    candidates = await async_discover_charger_current_sources(
        hass, charger_device_id=charger_device
    )

    assert [candidate.candidate_id for candidate in candidates] == [entity_id]
    candidate = candidates[0]
    assert candidate.reason_code == REASON_ATTRIBUTES_HISTORICAL_MATCH
    assert candidate.mapping.attributes == {"L1": "L1", "L2": "L2", "L3": "L3"}
    assert candidate.observed_at is not None
    # The live-only path still finds nothing here -- the fallback is what
    # added this candidate, and it never contradicts the synchronous result.
    assert discover_charger_current_sources(hass, charger_device_id=charger_device) == []


async def test_a_live_match_is_still_reported_as_a_live_match(
    recorder_mock, hass: HomeAssistant
) -> None:
    """An entity whose phase attributes are valid *right now* must keep its
    existing live reason code, confidence and `observed_at=None` -- history
    having a match too must never relabel or duplicate it.
    """
    owner = _owner_entry(hass, entry_id="owner_history_live")
    device_id = _device(hass, owner=owner, unique_id="grid_live", name="Grid meter")
    entity_id = _sensor(
        hass, owner=owner, device_id=device_id, unique_id="grid_live_current",
        object_id="grid_live_current",
    )
    await _record_one_charging_session(
        hass, entity_id, phase_attributes={"L1": 5.0, "L2": 6.0, "L3": 7.0}
    )
    hass.states.async_set(
        entity_id,
        "9.0",
        {"device_class": "current", "unit_of_measurement": "A", "L1": 9.0, "L2": 9.1, "L3": 9.2},
    )
    await async_wait_recording_done(hass)

    candidates = await async_discover_site_current_sources(hass)

    assert [candidate.candidate_id for candidate in candidates] == [entity_id]
    candidate = candidates[0]
    assert candidate.reason_code == REASON_ATTRIBUTES_DEVICE_CLASS_AND_UNIT_MATCH
    assert candidate.confidence == "high"
    assert candidate.observed_at is None


async def test_an_entity_with_no_historical_phase_match_is_not_fabricated(
    recorder_mock, hass: HomeAssistant
) -> None:
    """A brand-new charger's sensor (history exists, but never with a full
    three-phase attribute set) must resolve to no candidates at all -- the
    fallback must never invent one from "some state row exists".
    """
    owner = _owner_entry(hass, entry_id="owner_history_none")
    device_id = _device(hass, owner=owner, unique_id="charger_new", name="New charger")
    entity_id = _sensor(
        hass, owner=owner, device_id=device_id, unique_id="charger_new_current",
        object_id="charger_new_current",
    )
    # A partial (two-phase) attribute set, once: still not a phase mapping.
    hass.states.async_set(
        entity_id,
        "6.0",
        {"device_class": "current", "unit_of_measurement": "A", "L1": 6.0, "L2": 6.0},
    )
    await async_wait_recording_done(hass)
    hass.states.async_set(
        entity_id, "0", {"device_class": "current", "unit_of_measurement": "A", "connector_id": 1}
    )
    await async_wait_recording_done(hass)

    assert discover_charger_current_sources(hass, charger_device_id=device_id) == []
    assert await async_discover_charger_current_sources(hass, charger_device_id=device_id) == []


async def test_discovery_without_recorder_still_works_and_does_not_raise(
    hass: HomeAssistant,
) -> None:
    """No recorder integration configured (no `recorder_mock` here): discovery
    must fall back cleanly to live-state-only behavior -- no exception, and
    no historical candidate.
    """
    from homeassistant.helpers.recorder import DATA_INSTANCE

    assert DATA_INSTANCE not in hass.data

    owner = _owner_entry(hass, entry_id="owner_history_no_recorder")
    device_id = _device(hass, owner=owner, unique_id="charger_no_recorder", name="Charger")
    live_entity_id = _sensor(
        hass, owner=owner, device_id=device_id, unique_id="charger_no_recorder_live",
        object_id="charger_no_recorder_live",
    )
    history_only_entity_id = _sensor(
        hass, owner=owner, device_id=device_id, unique_id="charger_no_recorder_history",
        object_id="charger_no_recorder_history",
    )
    # This entity's phase attributes were only ever present during a past
    # session; with no recorder configured there is no history to find them
    # in, so it must not become a candidate -- while the live entity below
    # still is one.
    hass.states.async_set(
        history_only_entity_id, "0",
        {"device_class": "current", "unit_of_measurement": "A", "connector_id": 1},
    )
    hass.states.async_set(
        live_entity_id,
        "5.0",
        {"device_class": "current", "unit_of_measurement": "A", "L1": 5.0, "L2": 5.1, "L3": 5.2},
    )

    candidates = discover_charger_current_sources(hass, charger_device_id=device_id)
    fallback_candidates = await async_discover_charger_current_sources(
        hass, charger_device_id=device_id
    )

    assert [candidate.candidate_id for candidate in candidates] == [live_entity_id]
    # With no recorder there is no history to fall back to, so the
    # history-aware path must agree with the live-only one exactly.
    assert [candidate.candidate_id for candidate in fallback_candidates] == [live_entity_id]


async def test_phase_attributes_that_changed_without_the_value_are_still_found(
    recorder_mock, hass: HomeAssistant
) -> None:
    """Recorder drops "attributes only" rows unless it is explicitly asked not
    to, so `significant_changes_only=False` is a *correctness* requirement for
    this fallback, not a performance preference.

    A charger whose ampere reading stays at "0" while its phase attributes
    appear (rounding, or an integration that only publishes phases once a
    session starts) never produces a state *value* change to hang those
    attributes on -- the only row that carries them is an attributes-only
    change, which `significant_changes_only=True` filters out entirely.
    """
    owner = _owner_entry(hass, entry_id="owner_history_attributes_only")
    charger_device = _device(hass, owner=owner, unique_id="charger_attrs_only", name="Charger")
    entity_id = _sensor(
        hass, owner=owner, device_id=charger_device, unique_id="charger_attrs_only_current",
        object_id="charger_attrs_only_current",
    )
    await async_wait_recording_done(hass)
    # The value never changes from "0" -- only the attributes come and go.
    hass.states.async_set(
        entity_id,
        "0",
        {"device_class": "current", "unit_of_measurement": "A", "L1": 0.4, "L2": 0.3, "L3": 0.2},
    )
    await async_wait_recording_done(hass)
    hass.states.async_set(
        entity_id, "0", {"device_class": "current", "unit_of_measurement": "A", "connector_id": 1}
    )
    await async_wait_recording_done(hass)

    candidates = await async_discover_charger_current_sources(
        hass, charger_device_id=charger_device
    )

    assert [candidate.candidate_id for candidate in candidates] == [entity_id]
    assert candidates[0].reason_code == REASON_ATTRIBUTES_HISTORICAL_MATCH


# --- The site never reads history ------------------------------------------


async def test_site_discovery_never_reads_recorder_history(
    recorder_mock, hass: HomeAssistant, monkeypatch
) -> None:
    """The site's own measurement is live. A grid meter can update every second, so
    reading its history would be enormous and is never needed.
    """
    owner = _owner_entry(hass, entry_id="owner_history_site")
    device_id = _device(hass, owner=owner, unique_id="grid_history", name="Grid meter")
    entity_id = _sensor(
        hass, owner=owner, device_id=device_id, unique_id="grid_history_current",
        object_id="grid_history_current",
    )
    await _record_one_charging_session(
        hass, entity_id, phase_attributes={"L1": 11.0, "L2": 12.0, "L3": 13.0}
    )

    def _fail(*_args, **_kwargs):
        raise AssertionError("site discovery must not read history")

    monkeypatch.setattr(discovery_module, "_async_historical_phase_matches", _fail)
    monkeypatch.setattr(discovery_module, "_history_phase_matches", _fail)

    assert await async_discover_site_current_sources(hass) == []


async def test_a_slow_recorder_is_abandoned_and_live_candidates_are_still_returned(
    recorder_mock, hass: HomeAssistant, monkeypatch
) -> None:
    owner = _owner_entry(hass, entry_id="owner_history_slow")
    device_id = _device(hass, owner=owner, unique_id="charger_slow", name="Charger")
    idle = _sensor(
        hass, owner=owner, device_id=device_id, unique_id="charger_slow_idle",
        object_id="charger_slow_idle",
    )
    live = _sensor(
        hass, owner=owner, device_id=device_id, unique_id="charger_slow_live",
        object_id="charger_slow_live",
    )
    hass.states.async_set(
        live, "9.0",
        {"device_class": "current", "unit_of_measurement": "A", "L1": 9.0, "L2": 9.1, "L3": 9.2},
    )
    assert idle != live

    def _slow(*_args, **_kwargs):
        time.sleep(1.0)
        return {}

    monkeypatch.setattr(discovery_module, "_history_phase_matches", _slow)
    monkeypatch.setattr(discovery_module, "_HISTORY_TIMEOUT_SECONDS", 0.05)

    started = time.monotonic()
    candidates = await async_discover_charger_current_sources(hass, charger_device_id=device_id)

    assert time.monotonic() - started < 0.9
    assert [candidate.candidate_id for candidate in candidates] == [live]
    assert candidates[0].reason_code == REASON_ATTRIBUTES_DEVICE_CLASS_AND_UNIT_MATCH


async def test_an_entity_without_extra_attributes_is_never_looked_up_in_history(
    recorder_mock, hass: HomeAssistant, monkeypatch
) -> None:
    owner = _owner_entry(hass, entry_id="owner_history_plain")
    device_id = _device(hass, owner=owner, unique_id="charger_plain", name="Charger")
    entity_id = _sensor(
        hass, owner=owner, device_id=device_id, unique_id="charger_plain_current",
        object_id="charger_plain_current",
    )
    hass.states.async_set(entity_id, "0", {"device_class": "current", "unit_of_measurement": "A"})

    def _fail(*_args, **_kwargs):
        raise AssertionError("history must not be read")

    monkeypatch.setattr(discovery_module, "_async_historical_phase_matches", _fail)

    assert await async_discover_charger_current_sources(hass, charger_device_id=device_id) == []


# --- Device membership is the only thing separating the two --------------


async def test_historical_fallback_never_crosses_the_device_membership_boundary(
    recorder_mock, hass: HomeAssistant
) -> None:
    """THE boundary test.

    A charger's own current sensor and a house/site meter's current sensor
    are structurally identical (same unit, same three phase attributes), so
    device membership is the *only* thing telling them apart. Two entities
    with equally convincing historical phase-attribute matches, on two
    different devices: the charger's device may only ever offer its own, and
    the site (excluding the charger's device) may only ever offer the meter
    -- in both directions, however convincing the other one's history looks.
    """
    owner = _owner_entry(hass, entry_id="owner_history_boundary")
    charger_device = _device(hass, owner=owner, unique_id="charger_boundary", name="Charger")
    house_device = _device(hass, owner=owner, unique_id="house_meter", name="House meter")

    charger_entity_id = _sensor(
        hass, owner=owner, device_id=charger_device, unique_id="charger_boundary_current",
        object_id="charger_boundary_current",
    )
    house_entity_id = _sensor(
        hass, owner=owner, device_id=house_device, unique_id="house_meter_current",
        object_id="house_meter_current",
    )
    # Identical shape, identical device class, identical unit -- and *both*
    # have a past charging/load session in history, then went idle.
    await _record_one_charging_session(
        hass, charger_entity_id, phase_attributes={"L1": 6.2, "L2": 6.1, "L3": 6.0}
    )
    await _record_one_charging_session(
        hass, house_entity_id, phase_attributes={"L1": 9.9, "L2": 9.8, "L3": 9.7}
    )

    charger_candidates = await async_discover_charger_current_sources(
        hass, charger_device_id=charger_device
    )
    site_candidates = await async_discover_site_current_sources(
        hass, excluded_device_ids={charger_device}
    )

    assert [candidate.candidate_id for candidate in charger_candidates] == [charger_entity_id]
    # The site is live-only: the house meter's history is never read.
    assert site_candidates == []
    assert house_entity_id


async def test_history_is_queried_once_and_only_for_device_filtered_entities(
    recorder_mock, hass: HomeAssistant, monkeypatch
) -> None:
    """Performance discipline, pinned directly: Recorder is asked *once*, for
    exactly the charger device's own entities that both failed the live check
    and could still be stored as a usable candidate -- never for an entity
    with a live match, never for one whose unit rule it out, never for one
    with no live state, and never for an entity on another device.
    """
    queried: list[list[str]] = []
    original = discovery_module._history_phase_matches

    def _spy(hass_arg, entity_ids, start_time):
        queried.append(list(entity_ids))
        return original(hass_arg, entity_ids, start_time)

    monkeypatch.setattr(discovery_module, "_history_phase_matches", _spy)

    owner = _owner_entry(hass, entry_id="owner_history_query_shape")
    charger_device = _device(hass, owner=owner, unique_id="charger_query_shape", name="Charger")
    other_device = _device(hass, owner=owner, unique_id="house_query_shape", name="House meter")
    idle_entity_id = _sensor(
        hass, owner=owner, device_id=charger_device, unique_id="charger_query_shape_current",
        object_id="charger_query_shape_current",
    )
    await _record_one_charging_session(
        hass, idle_entity_id, phase_attributes={"L1": 6.2, "L2": 6.1, "L3": 6.0}
    )
    # A live match (not a history case at all).
    hass.states.async_set(
        "sensor.charger_query_shape_live", "5.0",
        {"device_class": "current", "unit_of_measurement": "A", "L1": 5.0, "L2": 5.0, "L3": 5.0},
    )
    er.async_get(hass).async_get_or_create(
        "sensor", "test", "charger_query_shape_live", device_id=charger_device,
        config_entry=owner, suggested_object_id="charger_query_shape_live",
    )
    # A unit that actively contradicts "this is current".
    contradicting_entity_id = _sensor(
        hass, owner=owner, device_id=charger_device, unique_id="charger_query_shape_power",
        object_id="charger_query_shape_power", device_class="power", unit="W",
    )
    # A registry entry with no live state at all.
    er.async_get(hass).async_get_or_create(
        "sensor", "test", "charger_query_shape_missing", device_id=charger_device,
        config_entry=owner, suggested_object_id="charger_query_shape_missing",
    )
    # A perfectly history-worthy entity -- on somebody else's device.
    other_entity_id = _sensor(
        hass, owner=owner, device_id=other_device, unique_id="house_query_shape_current",
        object_id="house_query_shape_current",
    )
    await _record_one_charging_session(
        hass, other_entity_id, phase_attributes={"L1": 9.0, "L2": 9.0, "L3": 9.0}
    )

    candidates = await async_discover_charger_current_sources(
        hass, charger_device_id=charger_device
    )

    assert queried == [[idle_entity_id]]
    assert contradicting_entity_id not in queried[0]
    assert [candidate.candidate_id for candidate in candidates] == [idle_entity_id]


# --- End to end: the per-charger measured-source field --------------------


def _grid_meter_with_attributes(hass: HomeAssistant, *, owner, unique_id: str) -> str:
    """A site meter currently reporting valid phase attributes (the ordinary
    live case) -- needed so the create flow reaches `site_details` directly,
    exactly as in the existing config-flow discovery tests.
    """
    device_registry = dr.async_get(hass)
    entity_registry = er.async_get(hass)
    device = device_registry.async_get_or_create(
        config_entry_id=owner.entry_id, identifiers={("ocpp", unique_id)}, name=unique_id
    )
    entry = entity_registry.async_get_or_create(
        "sensor", "ocpp", f"{unique_id}_current", device_id=device.id, config_entry=owner,
        suggested_object_id=unique_id,
    )
    hass.states.async_set(
        entry.entity_id, "unknown",
        {"device_class": "current", "unit_of_measurement": "A", "L1": 5.0, "L2": 6.0, "L3": 7.0},
    )
    return entry.entity_id


async def test_site_details_offers_a_charger_measured_source_from_history_alone(
    recorder_mock, hass: HomeAssistant
) -> None:
    """End to end: a charger that is idle *now*
    but charged in the past, configured through the actual config flow. The
    per-charger `measured_source_<entry_id>` field must be offered at all,
    its candidate must be labelled as historical, and picking it must store
    the attribute mapping on accept (never auto-applied before that).
    """
    ocpp_owner = make_ocpp_config_entry(hass, entry_id="owner_charger_history_flow")
    switch_entity_id = create_ocpp_charger_device(
        hass,
        ocpp_entry=ocpp_owner,
        device_unique_id="charger_history_flow",
        switch_object_id="charger_history_flow",
        current_attributes={"L1": 6.0, "L2": 6.0, "L3": 6.0},
    )
    await async_wait_recording_done(hass)
    registry = er.async_get(hass)
    charger_current_entity_id = registry.async_get_entity_id(
        "sensor", "ocpp", "charger_history_flow_current"
    )
    # ...and now idle: still a current sensor, but with no phase attributes.
    hass.states.async_set(
        charger_current_entity_id, "0",
        {"device_class": "current", "unit_of_measurement": "A", "connector_id": 1},
    )
    await async_wait_recording_done(hass)

    charger = make_entry(
        hass, entry_id="charger_history_flow", charge_control=switch_entity_id,
        current_limit=None, webhook_id="webhook-history-flow", title="History charger",
    )
    _grid_meter_with_attributes(hass, owner=ocpp_owner, unique_id="grid_history_flow")

    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"entry_type": "site"})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "name": "Home",
            "main_fuse_a": 25,
            "safety_margin_a": 1,
            "measurement_mode": "direct_phase_current",
            "charger_entry_ids": [charger.entry_id],
        },
    )
    assert result["step_id"] == "site_current_suggestions"
    grid_entity_id = registry.async_get_entity_id("sensor", "ocpp", "grid_history_flow_current")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"choice": grid_entity_id}
    )
    assert result["step_id"] == "site_details"

    schema = result["data_schema"]
    field_name = f"measured_source_{charger.entry_id}"
    assert any(str(key) == field_name for key in schema.schema)
    field = next(key for key in schema.schema if str(key) == field_name)
    options = schema.schema[field].config["options"]
    candidate_option = next(o for o in options if o["value"] == charger_current_entity_id)
    # The label must say plainly that this match is historical, never imply a
    # currently confirmed measurement.
    assert "history" in candidate_option["label"].lower()

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {field_name: charger_current_entity_id}
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    stored = result["data"][CONF_PHASE_WIRING][charger.entry_id][CONF_MEASURED_CURRENT_SOURCE]
    assert stored["entity_id"] == charger_current_entity_id
    assert stored["attributes"] == {"L1": "L1", "L2": "L2", "L3": "L3"}
    assert stored["attribute_unit_override"] == "A"
