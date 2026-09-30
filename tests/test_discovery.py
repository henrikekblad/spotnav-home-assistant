"""Tests for measurement-source discovery/scoring.

Deliberately uses generic ("test"-domain) devices/entities throughout, not
OCPP-specific fixtures -- vehicles/discovery.py must work identically regardless of
which integration actually owns the underlying entities.
"""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.spotnav.vehicles.discovery import (
    REASON_ATTRIBUTES_DEVICE_CLASS_AND_UNIT_MATCH,
    REASON_ATTRIBUTES_UNIT_MATCH_ONLY,
    REASON_POSSIBLE_INVERTER_OUTPUT,
    REASON_SEPARATE_ENTITIES_DEVICE_CLASS_AND_UNIT_MATCH,
    REASON_SEPARATE_ENTITIES_DEVICE_CLASS_MATCH_ONLY,
    REASON_SEPARATE_ENTITIES_UNIT_MATCH_ONLY,
    discover_charger_current_sources,
    discover_site_current_sources,
)


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


def _entity(
    hass: HomeAssistant,
    *,
    owner: MockConfigEntry,
    device_id: str,
    unique_id: str,
    object_id: str,
    state: str,
    device_class: str | None = None,
    unit: str | None = None,
    extra_attributes: dict | None = None,
) -> str:
    entity_registry = er.async_get(hass)
    entry = entity_registry.async_get_or_create(
        "sensor",
        "test",
        unique_id,
        device_id=device_id,
        config_entry=owner,
        suggested_object_id=object_id,
    )
    attributes = dict(extra_attributes or {})
    if device_class is not None:
        attributes["device_class"] = device_class
    if unit is not None:
        attributes["unit_of_measurement"] = unit
    hass.states.async_set(entry.entity_id, state, attributes)
    return entry.entity_id


def _entity_no_device(
    hass: HomeAssistant,
    *,
    owner: MockConfigEntry,
    unique_id: str,
    object_id: str,
    state: str,
    device_class: str | None = None,
    unit: str | None = None,
) -> str:
    """A registered entity with no device at all -- exercises the
    device-less grouping rules in `_entity_group_key`.
    """
    entity_registry = er.async_get(hass)
    entry = entity_registry.async_get_or_create(
        "sensor", "test", unique_id, config_entry=owner, suggested_object_id=object_id,
    )
    attributes: dict = {}
    if device_class is not None:
        attributes["device_class"] = device_class
    if unit is not None:
        attributes["unit_of_measurement"] = unit
    hass.states.async_set(entry.entity_id, state, attributes)
    return entry.entity_id


# --- Attribute-based: one entity, three phase attributes ------------------


async def test_discovers_a_clear_high_confidence_attribute_candidate(hass: HomeAssistant) -> None:
    owner = _owner_entry(hass, entry_id="owner_attrs_clear")
    device_id = _device(hass, owner=owner, unique_id="grid_meter", name="Grid meter")
    _entity(
        hass, owner=owner, device_id=device_id, unique_id="grid_meter_current",
        object_id="grid_meter_current", state="unknown", device_class="current",
        unit="A", extra_attributes={"L1": 5.0, "L2": 6.0, "L3": 7.0},
    )

    candidates = discover_site_current_sources(hass)

    assert len(candidates) == 1
    assert candidates[0].confidence == "high"
    assert candidates[0].reason_code == REASON_ATTRIBUTES_DEVICE_CLASS_AND_UNIT_MATCH
    assert candidates[0].mapping.kind == "attributes"
    assert candidates[0].mapping.attributes == {"L1": "L1", "L2": "L2", "L3": "L3"}


async def test_attribute_candidate_with_alternative_phase_names(hass: HomeAssistant) -> None:
    owner = _owner_entry(hass, entry_id="owner_attrs_alt_names")
    device_id = _device(hass, owner=owner, unique_id="grid_meter2", name="Grid meter 2")
    _entity(
        hass, owner=owner, device_id=device_id, unique_id="grid_meter2_current",
        object_id="grid_meter2_current", state="unknown", device_class="current", unit="A",
        extra_attributes={"Phase A": 5.0, "Phase B": 6.0, "Phase C": 7.0},
    )

    candidates = discover_site_current_sources(hass)

    assert len(candidates) == 1
    assert candidates[0].mapping.attributes == {"L1": "Phase A", "L2": "Phase B", "L3": "Phase C"}


# --- Separate entities: three entities, one per phase ----------------------


async def test_discovers_a_clear_high_confidence_separate_entities_candidate(
    hass: HomeAssistant,
) -> None:
    owner = _owner_entry(hass, entry_id="owner_sep_clear")
    device_id = _device(hass, owner=owner, unique_id="grid_meter3", name="Grid meter 3")
    for phase_suffix in ("l1", "l2", "l3"):
        _entity(
            hass, owner=owner, device_id=device_id, unique_id=f"grid_current_{phase_suffix}",
            object_id=f"grid_current_{phase_suffix}", state="5", device_class="current", unit="A",
        )

    candidates = discover_site_current_sources(hass)

    assert len(candidates) == 1
    assert candidates[0].confidence == "high"
    assert candidates[0].reason_code == REASON_SEPARATE_ENTITIES_DEVICE_CLASS_AND_UNIT_MATCH
    assert candidates[0].mapping.kind == "separate_entities"
    assert set(candidates[0].mapping.entity_ids.values()) == {
        "sensor.grid_current_l1", "sensor.grid_current_l2", "sensor.grid_current_l3",
    }


# --- Ambiguity: multiple candidates, none auto-selected --------------------


async def test_multiple_ambiguous_candidates_are_all_returned_not_auto_selected(
    hass: HomeAssistant,
) -> None:
    owner = _owner_entry(hass, entry_id="owner_ambiguous")
    device_a = _device(hass, owner=owner, unique_id="meter_a", name="Meter A")
    device_b = _device(hass, owner=owner, unique_id="meter_b", name="Meter B")
    for device_id, prefix in ((device_a, "meter_a"), (device_b, "meter_b")):
        for phase_suffix in ("l1", "l2", "l3"):
            _entity(
                hass, owner=owner, device_id=device_id, unique_id=f"{prefix}_current_{phase_suffix}",
                object_id=f"{prefix}_current_{phase_suffix}", state="5",
                device_class="current", unit="A",
            )

    candidates = discover_site_current_sources(hass)

    # Both plausible candidates are surfaced; the function itself never
    # picks one -- that is the config flow's (human-confirmed) job.
    assert len(candidates) == 2
    assert {c.candidate_id for c in candidates} == {device_a, device_b}


# --- Inverter-output exclusion for site/grid discovery ---------------------


async def test_inverter_labelled_output_is_downgraded_for_site_current(hass: HomeAssistant) -> None:
    owner = _owner_entry(hass, entry_id="owner_inverter")
    device_id = _device(hass, owner=owner, unique_id="inverter1", name="Solar Inverter")
    _entity(
        hass, owner=owner, device_id=device_id, unique_id="inverter1_current",
        object_id="inverter_output_current", state="unknown", device_class="current", unit="A",
        extra_attributes={"L1": 5.0, "L2": 6.0, "L3": 7.0},
    )

    candidates = discover_site_current_sources(hass)

    assert len(candidates) == 1
    assert candidates[0].confidence == "low"
    assert candidates[0].reason_code == REASON_POSSIBLE_INVERTER_OUTPUT


async def test_inverter_output_is_not_downgraded_for_charger_current_discovery(
    hass: HomeAssistant,
) -> None:
    # The inverter-output caution is specific to *site/grid* discovery
    # (output current isn't proven to equal incoming grid current); it has
    # no bearing on a charger's own measured current.
    owner = _owner_entry(hass, entry_id="owner_inverter_charger")
    device_id = _device(hass, owner=owner, unique_id="inverter2", name="Solar Inverter")
    _entity(
        hass, owner=owner, device_id=device_id, unique_id="inverter2_current",
        object_id="inverter_output_current", state="unknown", device_class="current", unit="A",
        extra_attributes={"L1": 5.0, "L2": 6.0, "L3": 7.0},
    )

    candidates = discover_charger_current_sources(hass, charger_device_id=device_id)

    assert len(candidates) == 1
    assert candidates[0].confidence == "high"


async def test_grid_mentioned_alongside_inverter_is_not_downgraded(hass: HomeAssistant) -> None:
    owner = _owner_entry(hass, entry_id="owner_inverter_grid")
    device_id = _device(hass, owner=owner, unique_id="inverter3", name="Inverter Grid Meter")
    _entity(
        hass, owner=owner, device_id=device_id, unique_id="inverter3_current",
        object_id="inverter_grid_current", state="unknown", device_class="current", unit="A",
        extra_attributes={"L1": 5.0, "L2": 6.0, "L3": 7.0},
    )

    candidates = discover_site_current_sources(hass)

    assert len(candidates) == 1
    assert candidates[0].confidence == "high"


# --- Charger-current discovery is scoped to the charger's own device ------


async def test_charger_current_discovery_never_returns_a_different_devices_entities(
    hass: HomeAssistant,
) -> None:
    owner = _owner_entry(hass, entry_id="owner_scope")
    charger_device = _device(hass, owner=owner, unique_id="charger1", name="Charger 1")
    other_device = _device(hass, owner=owner, unique_id="other1", name="Other device")
    _entity(
        hass, owner=owner, device_id=charger_device, unique_id="charger1_current",
        object_id="charger_current", state="unknown", device_class="current", unit="A",
        extra_attributes={"L1": 6.0, "L2": 6.0, "L3": 6.0},
    )
    _entity(
        hass, owner=owner, device_id=other_device, unique_id="other1_current",
        object_id="other_current", state="unknown", device_class="current", unit="A",
        extra_attributes={"L1": 9.0, "L2": 9.0, "L3": 9.0},
    )

    candidates = discover_charger_current_sources(hass, charger_device_id=charger_device)

    assert len(candidates) == 1
    assert candidates[0].mapping.entity_id == "sensor.charger_current"


async def test_site_discovery_excludes_a_charger_devices_entities(hass: HomeAssistant) -> None:
    owner = _owner_entry(hass, entry_id="owner_exclude")
    charger_device = _device(hass, owner=owner, unique_id="charger2", name="Charger 2")
    grid_device = _device(hass, owner=owner, unique_id="grid4", name="Grid meter 4")
    _entity(
        hass, owner=owner, device_id=charger_device, unique_id="charger2_current",
        object_id="charger2_current", state="unknown", device_class="current", unit="A",
        extra_attributes={"L1": 6.0, "L2": 6.0, "L3": 6.0},
    )
    _entity(
        hass, owner=owner, device_id=grid_device, unique_id="grid4_current",
        object_id="grid4_current", state="unknown", device_class="current", unit="A",
        extra_attributes={"L1": 9.0, "L2": 9.0, "L3": 9.0},
    )

    candidates = discover_site_current_sources(hass, excluded_device_ids={charger_device})

    assert len(candidates) == 1
    assert candidates[0].mapping.entity_id == "sensor.grid4_current"


# --- No candidates is a normal result --------------------------------------


async def test_no_candidates_is_an_empty_list_not_an_error(hass: HomeAssistant) -> None:
    assert discover_site_current_sources(hass) == []
    assert discover_charger_current_sources(hass, charger_device_id="nonexistent") == []
    assert discover_charger_current_sources(hass, charger_device_id=None) == []


async def test_partial_phase_coverage_is_not_a_candidate(hass: HomeAssistant) -> None:
    owner = _owner_entry(hass, entry_id="owner_partial")
    device_id = _device(hass, owner=owner, unique_id="partial1", name="Partial meter")
    _entity(
        hass, owner=owner, device_id=device_id, unique_id="partial1_current",
        object_id="partial1_current", state="unknown", device_class="current", unit="A",
        extra_attributes={"L1": 5.0, "L2": 6.0},  # only two phases
    )

    assert discover_site_current_sources(hass) == []


# --- Confidence rules: device class and unit are scored, and combined, --
# --- exactly as documented -------------------------------------------------


async def test_device_class_current_with_watts_is_never_a_usable_current_candidate(
    hass: HomeAssistant,
) -> None:
    """A wrong/contradicting unit (W is not ampere-like) must never yield a
    working candidate, high-confidence or otherwise -- not just "not high".
    """
    owner = _owner_entry(hass, entry_id="owner_wrong_unit")
    device_id = _device(hass, owner=owner, unique_id="wrong_unit_meter", name="Wrong unit meter")
    _entity(
        hass, owner=owner, device_id=device_id, unique_id="wrong_unit_current",
        object_id="wrong_unit_current", state="unknown", device_class="current", unit="W",
        extra_attributes={"L1": 5.0, "L2": 6.0, "L3": 7.0},
    )

    assert discover_site_current_sources(hass) == []


async def test_valid_unit_without_current_device_class_is_medium_for_attributes(
    hass: HomeAssistant,
) -> None:
    owner = _owner_entry(hass, entry_id="owner_unit_only_attrs")
    device_id = _device(hass, owner=owner, unique_id="unit_only_meter", name="Unit only meter")
    _entity(
        hass, owner=owner, device_id=device_id, unique_id="unit_only_current",
        object_id="unit_only_current", state="unknown", unit="A",
        extra_attributes={"L1": 5.0, "L2": 6.0, "L3": 7.0},
    )

    candidates = discover_site_current_sources(hass)

    assert len(candidates) == 1
    assert candidates[0].confidence == "medium"
    assert candidates[0].reason_code == REASON_ATTRIBUTES_UNIT_MATCH_ONLY
    assert candidates[0].mapping.attribute_unit_override == "A"


async def test_current_device_class_without_any_unit_is_not_usable_for_attributes(
    hass: HomeAssistant,
) -> None:
    """Documented decision: for the attributes representation specifically,
    a missing parent unit can never be stored as a working
    `attribute_unit_override`, so no candidate is proposed at all --
    device_class alone is not enough to fabricate a unit. (For the
    separate-entities representation this same signal is a usable "medium"
    candidate instead, since each phase entity's own unit is re-read live
    at measurement time rather than stored -- see the next test.)
    """
    owner = _owner_entry(hass, entry_id="owner_device_class_only_attrs")
    device_id = _device(hass, owner=owner, unique_id="dc_only_meter", name="Device class only meter")
    _entity(
        hass, owner=owner, device_id=device_id, unique_id="dc_only_current",
        object_id="dc_only_current", state="unknown", device_class="current",
        extra_attributes={"L1": 5.0, "L2": 6.0, "L3": 7.0},
    )

    assert discover_site_current_sources(hass) == []


async def test_current_device_class_without_unit_is_medium_for_separate_entities(
    hass: HomeAssistant,
) -> None:
    owner = _owner_entry(hass, entry_id="owner_device_class_only_sep")
    device_id = _device(hass, owner=owner, unique_id="dc_only_sep_meter", name="DC only sep meter")
    for suffix in ("l1", "l2", "l3"):
        _entity(
            hass, owner=owner, device_id=device_id, unique_id=f"dc_only_sep_{suffix}",
            object_id=f"dc_only_sep_current_{suffix}", state="5", device_class="current",
        )

    candidates = discover_site_current_sources(hass)

    assert len(candidates) == 1
    assert candidates[0].confidence == "medium"
    assert candidates[0].reason_code == REASON_SEPARATE_ENTITIES_DEVICE_CLASS_MATCH_ONLY


async def test_valid_unit_without_device_class_is_medium_for_separate_entities(
    hass: HomeAssistant,
) -> None:
    owner = _owner_entry(hass, entry_id="owner_unit_only_sep")
    device_id = _device(hass, owner=owner, unique_id="unit_only_sep_meter", name="Unit only sep meter")
    for suffix in ("l1", "l2", "l3"):
        _entity(
            hass, owner=owner, device_id=device_id, unique_id=f"unit_only_sep_{suffix}",
            object_id=f"unit_only_sep_current_{suffix}", state="5", unit="A",
        )

    candidates = discover_site_current_sources(hass)

    assert len(candidates) == 1
    assert candidates[0].confidence == "medium"
    assert candidates[0].reason_code == REASON_SEPARATE_ENTITIES_UNIT_MATCH_ONLY


async def test_wrong_unit_on_one_separate_entity_is_never_a_usable_candidate(
    hass: HomeAssistant,
) -> None:
    owner = _owner_entry(hass, entry_id="owner_sep_wrong_unit")
    device_id = _device(hass, owner=owner, unique_id="sep_wrong_unit_meter", name="Sep wrong unit meter")
    _entity(
        hass, owner=owner, device_id=device_id, unique_id="sep_wrong_unit_l1",
        object_id="sep_wrong_unit_current_l1", state="5", device_class="current", unit="A",
    )
    _entity(
        hass, owner=owner, device_id=device_id, unique_id="sep_wrong_unit_l2",
        object_id="sep_wrong_unit_current_l2", state="5", device_class="current", unit="A",
    )
    _entity(
        hass, owner=owner, device_id=device_id, unique_id="sep_wrong_unit_l3",
        object_id="sep_wrong_unit_current_l3", state="5", device_class="current", unit="W",
    )

    assert discover_site_current_sources(hass) == []


# --- Attribute disambiguation: the right phase attribute is picked even --
# --- when a competing, incompatible-quantity attribute also matches -------


async def test_voltage_attribute_is_never_picked_over_a_current_attribute_for_the_same_phase(
    hass: HomeAssistant,
) -> None:
    owner = _owner_entry(hass, entry_id="owner_mixed_quantities")
    device_id = _device(hass, owner=owner, unique_id="mixed_meter", name="Mixed meter")
    _entity(
        hass, owner=owner, device_id=device_id, unique_id="mixed_current",
        object_id="mixed_current", state="unknown", device_class="current", unit="A",
        # Deliberately interleaved insertion order, and a competing
        # "voltage_l1" attribute that also contains a phase indicator.
        extra_attributes={
            "voltage_l1": 230.0,
            "current_l1": 5.0,
            "L3": 7.0,
            "voltage_l2": 231.0,
            "current_l2": 6.0,
            "voltage_l3": 229.0,
        },
    )

    candidates = discover_site_current_sources(hass)

    assert len(candidates) == 1
    assert candidates[0].mapping.attributes == {"L1": "current_l1", "L2": "current_l2", "L3": "L3"}


async def test_ambiguous_competing_phase_attributes_yield_no_candidate(
    hass: HomeAssistant,
) -> None:
    """Two equally-plausible, differently-named attributes both matching
    the same phase (neither is an exact name, neither is more "current
    specific" than the other) must not be guessed between -- the whole
    entity is simply not proposed.
    """
    owner = _owner_entry(hass, entry_id="owner_ambiguous_attrs")
    device_id = _device(hass, owner=owner, unique_id="ambiguous_meter", name="Ambiguous meter")
    _entity(
        hass, owner=owner, device_id=device_id, unique_id="ambiguous_current",
        object_id="ambiguous_current", state="unknown", device_class="current", unit="A",
        extra_attributes={
            "grid_current_l1": 5.0,
            "meter_current_l1": 5.1,
            "L2": 6.0,
            "L3": 7.0,
        },
    )

    assert discover_site_current_sources(hass) == []


async def test_attribute_disambiguation_is_independent_of_dict_insertion_order(
    hass: HomeAssistant,
) -> None:
    owner = _owner_entry(hass, entry_id="owner_order_independent")
    device_id = _device(hass, owner=owner, unique_id="order_meter", name="Order meter")
    # Attributes dicts preserve insertion order in Python -- insert the
    # "wrong" (non-exact) name first for each phase to prove the result
    # does not depend on it.
    _entity(
        hass, owner=owner, device_id=device_id, unique_id="order_current",
        object_id="order_current", state="unknown", device_class="current", unit="A",
        extra_attributes={
            "current_l1": 5.0,
            "L1": 5.0,
            "current_l2": 6.0,
            "L2": 6.0,
            "current_l3": 7.0,
            "L3": 7.0,
        },
    )

    candidates = discover_site_current_sources(hass)

    assert len(candidates) == 1
    # Exact phase names ("L1"/"L2"/"L3", tier 0) win over the
    # current-qualified names (tier 1), regardless of insertion order.
    assert candidates[0].mapping.attributes == {"L1": "L1", "L2": "L2", "L3": "L3"}


# --- Device-less separate-entity grouping requires a safe shared identity -


async def test_three_related_device_less_phase_entities_form_one_candidate(
    hass: HomeAssistant,
) -> None:
    owner = _owner_entry(hass, entry_id="owner_related_no_device")
    for suffix in ("l1", "l2", "l3"):
        _entity_no_device(
            hass, owner=owner, unique_id=f"related_grid_current_{suffix}",
            object_id=f"related_grid_current_{suffix}", state="5",
            device_class="current", unit="A",
        )

    candidates = discover_site_current_sources(hass)

    assert len(candidates) == 1
    assert set(candidates[0].mapping.entity_ids.values()) == {
        "sensor.related_grid_current_l1",
        "sensor.related_grid_current_l2",
        "sensor.related_grid_current_l3",
    }


async def test_three_unrelated_device_less_entities_from_different_integrations_do_not_group(
    hass: HomeAssistant,
) -> None:
    """Same textual prefix, but three different owning config entries --
    exactly the "different integration" case this grouping fix targets.
    """
    owner_a = _owner_entry(hass, entry_id="owner_unrelated_a")
    owner_b = _owner_entry(hass, entry_id="owner_unrelated_b")
    owner_c = _owner_entry(hass, entry_id="owner_unrelated_c")
    _entity_no_device(
        hass, owner=owner_a, unique_id="shared_prefix_l1", object_id="shared_prefix_l1",
        state="5", device_class="current", unit="A",
    )
    _entity_no_device(
        hass, owner=owner_b, unique_id="shared_prefix_l2", object_id="shared_prefix_l2",
        state="5", device_class="current", unit="A",
    )
    _entity_no_device(
        hass, owner=owner_c, unique_id="shared_prefix_l3", object_id="shared_prefix_l3",
        state="5", device_class="current", unit="A",
    )

    assert discover_site_current_sources(hass) == []


async def test_device_less_entities_with_mixed_name_prefixes_do_not_group(
    hass: HomeAssistant,
) -> None:
    owner = _owner_entry(hass, entry_id="owner_mixed_prefix")
    _entity_no_device(
        hass, owner=owner, unique_id="foo_l1", object_id="foo_l1",
        state="5", device_class="current", unit="A",
    )
    _entity_no_device(
        hass, owner=owner, unique_id="bar_l2", object_id="bar_l2",
        state="5", device_class="current", unit="A",
    )
    _entity_no_device(
        hass, owner=owner, unique_id="baz_l3", object_id="baz_l3",
        state="5", device_class="current", unit="A",
    )

    assert discover_site_current_sources(hass) == []


async def test_multiple_separate_three_phase_groups_without_device_are_both_found(
    hass: HomeAssistant,
) -> None:
    owner = _owner_entry(hass, entry_id="owner_two_groups")
    for prefix in ("meter_a", "meter_b"):
        for suffix in ("l1", "l2", "l3"):
            _entity_no_device(
                hass, owner=owner, unique_id=f"{prefix}_current_{suffix}",
                object_id=f"{prefix}_current_{suffix}", state="5",
                device_class="current", unit="A",
            )

    candidates = discover_site_current_sources(hass)

    assert len(candidates) == 2
    all_entity_ids = {eid for c in candidates for eid in c.mapping.entity_ids.values()}
    assert all_entity_ids == {
        "sensor.meter_a_current_l1", "sensor.meter_a_current_l2", "sensor.meter_a_current_l3",
        "sensor.meter_b_current_l1", "sensor.meter_b_current_l2", "sensor.meter_b_current_l3",
    }
