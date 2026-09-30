"""Tests for the generic phase measurement source model and reader."""

from __future__ import annotations

from datetime import timedelta

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.spotnav.site.measurement_source import (
    PhaseMeasurementSource,
    read_phase_measurement,
    source_from_dict,
    source_to_dict,
)
from custom_components.spotnav.site.site_capacity import classify_current


async def test_separate_entities_representation_reads_all_three_phases(hass: HomeAssistant) -> None:
    hass.states.async_set("sensor.grid_current_l1", "10", {"unit_of_measurement": "A"})
    hass.states.async_set("sensor.grid_current_l2", "11", {"unit_of_measurement": "A"})
    hass.states.async_set("sensor.grid_current_l3", "12", {"unit_of_measurement": "A"})
    source = PhaseMeasurementSource(
        kind="separate_entities",
        entity_ids={
            "L1": "sensor.grid_current_l1",
            "L2": "sensor.grid_current_l2",
            "L3": "sensor.grid_current_l3",
        },
    )

    result = read_phase_measurement(hass, source, classify_current)

    assert result["L1"].value == 10.0
    assert result["L2"].value == 11.0
    assert result["L3"].value == 12.0
    assert all(v.problem is None for v in result.values())


async def test_attributes_representation_reads_all_three_phases(hass: HomeAssistant) -> None:
    hass.states.async_set(
        "sensor.charger_current",
        "unknown",
        {"L1": 6.0, "L2": 6.5, "L3": 7.0},
    )
    source = PhaseMeasurementSource(
        kind="attributes",
        entity_id="sensor.charger_current",
        attributes={"L1": "L1", "L2": "L2", "L3": "L3"},
        attribute_unit_override="A",
    )

    result = read_phase_measurement(hass, source, classify_current)

    assert result["L1"].value == 6.0
    assert result["L2"].value == 6.5
    assert result["L3"].value == 7.0


async def test_alternative_attribute_names_are_supported(hass: HomeAssistant) -> None:
    # A grid meter exposing "Phase A/B/C", not "L1/L2/L3".
    hass.states.async_set(
        "sensor.grid_meter",
        "unknown",
        {"Phase A": 4.0, "Phase B": 4.5, "Phase C": 5.0},
    )
    source = PhaseMeasurementSource(
        kind="attributes",
        entity_id="sensor.grid_meter",
        attributes={"L1": "Phase A", "L2": "Phase B", "L3": "Phase C"},
        attribute_unit_override="A",
    )

    result = read_phase_measurement(hass, source, classify_current)

    assert result["L1"].value == 4.0
    assert result["L2"].value == 4.5
    assert result["L3"].value == 5.0


async def test_explicit_attribute_mapping_is_used_verbatim_not_inferred(hass: HomeAssistant) -> None:
    hass.states.async_set(
        "sensor.charger",
        "unknown",
        {"current_l1": 1.0, "current_l2": 2.0, "current_l3": 3.0, "L1": 999.0},
    )
    # Explicitly mapped to the "current_l*" attributes -- even though an
    # "L1" attribute also happens to exist, it must be ignored since it was
    # not the configured mapping.
    source = PhaseMeasurementSource(
        kind="attributes",
        entity_id="sensor.charger",
        attributes={"L1": "current_l1", "L2": "current_l2", "L3": "current_l3"},
        attribute_unit_override="A",
    )

    result = read_phase_measurement(hass, source, classify_current)

    assert result["L1"].value == 1.0


async def test_missing_unit_is_invalid_for_both_representations(hass: HomeAssistant) -> None:
    hass.states.async_set("sensor.no_unit_l1", "10")  # no unit_of_measurement
    separate = PhaseMeasurementSource(
        kind="separate_entities", entity_ids={"L1": "sensor.no_unit_l1", "L2": "x", "L3": "y"}
    )
    result = read_phase_measurement(hass, separate, classify_current)
    assert result["L1"].value is None
    assert result["L1"].problem == "invalid"

    hass.states.async_set("sensor.no_unit_attrs", "unknown", {"L1": 10.0})
    attrs = PhaseMeasurementSource(
        kind="attributes", entity_id="sensor.no_unit_attrs", attributes={"L1": "L1"}
    )
    # No attribute_unit_override and trust_entity_unit_for_attributes=False
    # (the default) -> unit is unknown, never guessed.
    result = read_phase_measurement(hass, attrs, classify_current)
    assert result["L1"].value is None
    assert result["L1"].problem == "invalid"


async def test_trust_entity_unit_for_attributes_opts_in_explicitly(hass: HomeAssistant) -> None:
    hass.states.async_set(
        "sensor.uniform_unit", "10", {"unit_of_measurement": "A", "L1": 5.0, "L2": 6.0, "L3": 7.0}
    )
    source = PhaseMeasurementSource(
        kind="attributes",
        entity_id="sensor.uniform_unit",
        attributes={"L1": "L1", "L2": "L2", "L3": "L3"},
        trust_entity_unit_for_attributes=True,
    )

    result = read_phase_measurement(hass, source, classify_current)

    assert result["L1"].value == 5.0
    assert result["L1"].problem is None


async def test_wrong_unit_is_invalid(hass: HomeAssistant) -> None:
    hass.states.async_set("sensor.wrong_unit_l1", "10", {"unit_of_measurement": "W"})
    source = PhaseMeasurementSource(
        kind="separate_entities", entity_ids={"L1": "sensor.wrong_unit_l1", "L2": "x", "L3": "y"}
    )

    result = read_phase_measurement(hass, source, classify_current)

    assert result["L1"].value is None
    assert result["L1"].problem == "invalid"


async def test_stale_parent_entity_is_reflected_in_every_phases_age(hass: HomeAssistant) -> None:
    # Time is injected via async_set's own `timestamp` parameter -- no
    # sleeps -- so this state is deterministically 200s old.
    stale_timestamp = (dt_util.utcnow() - timedelta(seconds=200)).timestamp()
    hass.states.async_set(
        "sensor.stale_parent",
        "unknown",
        {"L1": 5.0, "L2": 5.0, "L3": 5.0},
        timestamp=stale_timestamp,
    )
    source = PhaseMeasurementSource(
        kind="attributes",
        entity_id="sensor.stale_parent",
        attributes={"L1": "L1", "L2": "L2", "L3": "L3"},
        attribute_unit_override="A",
    )

    result = read_phase_measurement(hass, source, classify_current)

    assert result["L1"].age_s is not None
    assert 195.0 <= result["L1"].age_s <= 210.0

    # Confirm the (documented) limitation: all three phases share the one
    # entity's own last_updated -- there is no per-attribute timestamp.
    # (A sub-millisecond difference is just wall-clock jitter between the
    # three now() calls inside read_phase_measurement, not a real age
    # difference between phases.)
    assert abs(result["L1"].age_s - result["L2"].age_s) < 0.01
    assert abs(result["L1"].age_s - result["L3"].age_s) < 0.01


async def test_partially_missing_phase_attributes_report_missing_for_that_phase_only(
    hass: HomeAssistant,
) -> None:
    hass.states.async_set("sensor.partial", "unknown", {"L1": 5.0, "L2": 6.0})  # no L3 attribute
    source = PhaseMeasurementSource(
        kind="attributes",
        entity_id="sensor.partial",
        attributes={"L1": "L1", "L2": "L2", "L3": "L3"},
        attribute_unit_override="A",
    )

    result = read_phase_measurement(hass, source, classify_current)

    assert result["L1"].value == 5.0
    assert result["L2"].value == 6.0
    assert result["L3"].value is None
    assert result["L3"].problem == "missing"


async def test_none_source_is_missing_for_all_phases(hass: HomeAssistant) -> None:
    result = read_phase_measurement(hass, None, classify_current)
    assert all(v.value is None and v.problem == "missing" for v in result.values())


async def test_missing_entity_is_missing_for_all_phases(hass: HomeAssistant) -> None:
    source = PhaseMeasurementSource(
        kind="separate_entities",
        entity_ids={"L1": "sensor.does_not_exist_l1", "L2": "sensor.does_not_exist_l2", "L3": "sensor.does_not_exist_l3"},
    )
    result = read_phase_measurement(hass, source, classify_current)
    assert all(v.value is None and v.problem == "missing" for v in result.values())


# --- Serialization round-trip ---------------------------------------------


def test_source_to_dict_and_back_round_trips_separate_entities() -> None:
    source = PhaseMeasurementSource(
        kind="separate_entities",
        entity_ids={"L1": "sensor.a", "L2": "sensor.b", "L3": "sensor.c"},
    )
    restored = source_from_dict(source_to_dict(source))
    assert restored == source


def test_source_to_dict_and_back_round_trips_attributes() -> None:
    source = PhaseMeasurementSource(
        kind="attributes",
        entity_id="sensor.x",
        attributes={"L1": "Phase A", "L2": "Phase B", "L3": "Phase C"},
        attribute_unit_override="A",
        trust_entity_unit_for_attributes=False,
    )
    restored = source_from_dict(source_to_dict(source))
    assert restored == source


def test_source_to_dict_of_none_is_none() -> None:
    assert source_to_dict(None) is None


def test_source_from_dict_rejects_corrupt_or_legacy_shapes() -> None:
    assert source_from_dict(None) is None
    assert source_from_dict({}) is None
    assert source_from_dict({"kind": "not-a-real-kind"}) is None
    assert source_from_dict({"unexpected": "shape"}) is None


# --- Strict storage validation: every field, not just `kind` --------------


def test_source_from_dict_rejects_a_non_mapping_top_level_value() -> None:
    assert source_from_dict("separate_entities") is None
    assert source_from_dict(["separate_entities"]) is None
    assert source_from_dict(42) is None


def test_source_from_dict_rejects_entity_ids_that_is_not_a_mapping() -> None:
    assert source_from_dict({"kind": "separate_entities", "entity_ids": ["sensor.a", "sensor.b"]}) is None
    assert source_from_dict({"kind": "separate_entities", "entity_ids": "sensor.a"}) is None


def test_source_from_dict_rejects_attributes_that_is_not_a_mapping() -> None:
    assert source_from_dict(
        {"kind": "attributes", "entity_id": "sensor.x", "attributes": ["L1", "L2"]}
    ) is None


def test_source_from_dict_rejects_an_empty_phase_mapping() -> None:
    assert source_from_dict({"kind": "separate_entities", "entity_ids": {}}) is None
    assert source_from_dict({"kind": "attributes", "entity_id": "sensor.x", "attributes": {}}) is None


def test_source_from_dict_rejects_an_unknown_phase_key() -> None:
    assert source_from_dict(
        {"kind": "separate_entities", "entity_ids": {"L1": "sensor.a", "L4": "sensor.b"}}
    ) is None
    assert source_from_dict(
        {"kind": "attributes", "entity_id": "sensor.x", "attributes": {"L1": "a", "bogus": "b"}}
    ) is None


def test_source_from_dict_accepts_a_partial_phase_mapping() -> None:
    """A one-phase charger's own measured current legitimately only ever
    has one phase configured -- a partial mapping is not itself corrupt.
    """
    source = source_from_dict({"kind": "separate_entities", "entity_ids": {"L1": "sensor.a"}})
    assert source is not None
    assert source.entity_ids == {"L1": "sensor.a"}


def test_source_from_dict_rejects_non_string_phase_values() -> None:
    assert source_from_dict({"kind": "separate_entities", "entity_ids": {"L1": 123}}) is None
    assert source_from_dict({"kind": "separate_entities", "entity_ids": {"L1": None}}) is None
    assert source_from_dict({"kind": "separate_entities", "entity_ids": {"L1": ""}}) is None
    assert source_from_dict(
        {"kind": "attributes", "entity_id": "sensor.x", "attributes": {"L1": 123}}
    ) is None


def test_source_from_dict_rejects_an_entity_id_shaped_value_that_is_not_a_real_entity_id() -> None:
    # A valid attribute *name* is not required to look like an entity ID --
    # only entity_ids (separate_entities) and the attributes source's own
    # `entity_id` field are held to that shape.
    assert source_from_dict(
        {"kind": "separate_entities", "entity_ids": {"L1": "not-an-entity-id"}}
    ) is None
    assert source_from_dict({"kind": "attributes", "entity_id": "not-an-entity-id"}) is None
    assert source_from_dict({"kind": "attributes", "entity_id": ""}) is None
    assert source_from_dict({"kind": "attributes", "entity_id": None}) is None


def test_source_from_dict_accepts_a_non_entity_id_shaped_attribute_name() -> None:
    source = source_from_dict(
        {"kind": "attributes", "entity_id": "sensor.x", "attributes": {"L1": "Phase A"}}
    )
    assert source is not None
    assert source.attributes == {"L1": "Phase A"}


def test_source_from_dict_rejects_an_invalid_attribute_unit_override() -> None:
    assert source_from_dict(
        {"kind": "attributes", "entity_id": "sensor.x", "attributes": {"L1": "L1"},
         "attribute_unit_override": "volts"}
    ) is None
    assert source_from_dict(
        {"kind": "attributes", "entity_id": "sensor.x", "attributes": {"L1": "L1"},
         "attribute_unit_override": ""}
    ) is None


def test_source_from_dict_accepts_a_valid_attribute_unit_override() -> None:
    source = source_from_dict(
        {"kind": "attributes", "entity_id": "sensor.x", "attributes": {"L1": "L1"},
         "attribute_unit_override": "mA"}
    )
    assert source is not None
    assert source.attribute_unit_override == "mA"


def test_source_from_dict_rejects_a_non_bool_trust_flag() -> None:
    for bogus in ("true", 1, 0, None):
        assert source_from_dict(
            {"kind": "attributes", "entity_id": "sensor.x", "attributes": {"L1": "L1"},
             "trust_entity_unit_for_attributes": bogus}
        ) is None


def test_source_from_dict_rejects_foreign_fields_that_would_make_the_source_ambiguous() -> None:
    # separate_entities with attributes-representation fields also set.
    assert source_from_dict(
        {
            "kind": "separate_entities",
            "entity_ids": {"L1": "sensor.a"},
            "entity_id": "sensor.b",
        }
    ) is None
    assert source_from_dict(
        {
            "kind": "separate_entities",
            "entity_ids": {"L1": "sensor.a"},
            "attributes": {"L1": "L1"},
        }
    ) is None
    # attributes with separate_entities-representation fields also set.
    assert source_from_dict(
        {
            "kind": "attributes",
            "entity_id": "sensor.x",
            "attributes": {"L1": "L1"},
            "entity_ids": {"L1": "sensor.a"},
        }
    ) is None
