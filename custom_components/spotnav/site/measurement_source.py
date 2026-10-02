"""Where and how to read one phase-based current quantity from Home Assistant.

A source is either one entity per phase ("separate_entities") or one entity
carrying all three phases as attributes ("attributes"), with configurable
attribute names since integrations disagree ("L1" vs "Phase A"). The mapping is
always stored explicitly, never inferred at read time
(`vehicles/discovery.py` may only suggest one). Validation is delegated to the
`classify_*` functions in `site_capacity.py`.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

from homeassistant.core import HomeAssistant, State
from homeassistant.util import dt as dt_util

from .site_capacity import normalized_ampere_unit, PhaseName, PhaseProblem, PHASES, PhaseValue


MeasurementSourceKind = Literal["separate_entities", "attributes"]


@dataclass(frozen=True, slots=True)
class PhaseMeasurementSource:
    """Where to read one quantity's L1/L2/L3 values from.

    `separate_entities`: `entity_ids` maps each phase to its own entity.

    `attributes`: `entity_id` carries all three phases and `attributes` maps
    each phase to that entity's attribute name (e.g. "current_l1", "Phase A").
    The unit is `attribute_unit_override` if set, else the entity's own unit
    only when `trust_entity_unit_for_attributes` is true (the entity's unit
    describes its state, not necessarily its attributes); otherwise unknown,
    which `classify_*` rejects.

    `signed_current`: the source reports export as a negative current (HomeWizard P1,
    Huawei, Fronius, SolarEdge Modbus, Deye external CT ...). Its magnitude is what the
    fuse carries, so each reading is taken as |value| before classifying; without the
    flag a negative reading stays invalid.
    """

    kind: MeasurementSourceKind
    entity_ids: Mapping[PhaseName, str] | None = None
    entity_id: str | None = None
    attributes: Mapping[PhaseName, str] | None = None
    attribute_unit_override: str | None = None
    trust_entity_unit_for_attributes: bool = False
    signed_current: bool = False


ClassifyFn = Callable[[Any, str | None], tuple[float | None, PhaseProblem | None]]


def read_phase_measurement(
    hass: HomeAssistant,
    source: PhaseMeasurementSource | None,
    classify: ClassifyFn,
) -> dict[PhaseName, PhaseValue]:
    """Read and classify all three phases of one source.

    Returns a `PhaseValue` per phase; `problem="missing"` for phases the source
    cannot provide (or when `source` is `None`). `age_s` comes from the entity's
    `last_updated`, `report_age_s` from `last_reported`; the latter lets
    `classify_phase_liveness` tell a flat reading from a silent link.

    For `attributes` sources all phases share the one entity's timestamps (HA
    has no per-attribute timestamps).
    """
    if source is None:
        return {phase: PhaseValue(None, None, problem="missing") for phase in PHASES}
    return {phase: _read_one_phase(hass, source, phase, classify) for phase in PHASES}


def _read_one_phase(
    hass: HomeAssistant,
    source: PhaseMeasurementSource,
    phase: PhaseName,
    classify: ClassifyFn,
) -> PhaseValue:
    raw, unit, age_s, report_age_s = _raw_phase_value(hass, source, phase)
    if source.signed_current:
        raw = _magnitude(raw)
    value, problem = classify(raw, unit)
    return PhaseValue(value, age_s, problem, report_age_s=report_age_s)


def _magnitude(raw: Any) -> Any:
    """`raw` as |value| when it reads as a finite number, else unchanged (so a missing or
    non-numeric value is still classified as such)."""
    if isinstance(raw, bool):
        return raw
    try:
        number = float(raw)
    except (TypeError, ValueError):
        return raw
    return abs(number) if math.isfinite(number) else raw


def _raw_phase_value(
    hass: HomeAssistant, source: PhaseMeasurementSource, phase: PhaseName
) -> tuple[Any, str | None, float | None, float | None]:
    if source.kind == "separate_entities":
        entity_id = (source.entity_ids or {}).get(phase)
        state = hass.states.get(entity_id) if entity_id else None
        if state is None:
            return None, None, None, None
        return (
            state.state,
            state.attributes.get("unit_of_measurement"),
            _age_s(state),
            _report_age_s(state),
        )

    state = hass.states.get(source.entity_id) if source.entity_id else None
    if state is None:
        return None, None, None, None
    attribute_name = (source.attributes or {}).get(phase)
    if not attribute_name or attribute_name not in state.attributes:
        return None, None, None, None
    raw = state.attributes.get(attribute_name)
    if source.attribute_unit_override:
        unit = source.attribute_unit_override
    elif source.trust_entity_unit_for_attributes:
        unit = state.attributes.get("unit_of_measurement")
    else:
        unit = None
    return raw, unit, _age_s(state), _report_age_s(state)


def _age_s(state: State) -> float:
    return (dt_util.utcnow() - state.last_updated).total_seconds()


def _report_age_s(state: State) -> float | None:
    """Seconds since HA last heard from the entity, changed or not
    (`last_reported`); `None` if the HA core does not expose it.
    """
    last_reported = getattr(state, "last_reported", None)
    if last_reported is None:
        return None
    return (dt_util.utcnow() - last_reported).total_seconds()


def combine_power_pair(
    positive: PhaseValue, negative: PhaseValue | None = None, *, invert: bool = False
) -> PhaseValue:
    """One signed power from a meter's entities: `positive - negative`, then negated if `invert`.

    `negative` is the export half of an import/export pair (both entities >= 0, as DSMR, SMA and
    the Nordic HAN readers report), `None` for a single signed entity. Both halves must be usable:
    a missing or invalid half makes the result missing or invalid, never a silent zero, and the
    result is as old as its older half. `invert` is for export-positive meters (Huawei, SolarEdge
    Modbus, GoodWe ...) and for a battery reporting discharge as positive.
    """
    halves = [positive] if negative is None else [positive, negative]
    problem = None
    for half in halves:
        if half.problem == "invalid":
            problem = "invalid"
        elif half.problem is not None and problem is None:
            problem = "missing"
        elif half.value is None and problem is None:
            problem = "missing"
    ages = [half.age_s for half in halves]
    report_ages = [half.report_age_s for half in halves]
    age_s = None if any(a is None for a in ages) else max(ages)
    report_age_s = None if any(a is None for a in report_ages) else max(report_ages)
    if problem is not None:
        return PhaseValue(None, age_s, problem, report_age_s=report_age_s)
    value = positive.value - (negative.value if negative is not None else 0.0)
    if invert:
        value = -value
    return PhaseValue(value, age_s, None, report_age_s=report_age_s)


def source_to_dict(source: PhaseMeasurementSource | None) -> dict[str, Any] | None:
    """Serialize a source for JSON storage in a config entry's `data`."""
    if source is None:
        return None
    stored: dict[str, Any] = {
        "kind": source.kind,
        "entity_ids": dict(source.entity_ids) if source.entity_ids else None,
        "entity_id": source.entity_id,
        "attributes": dict(source.attributes) if source.attributes else None,
        "attribute_unit_override": source.attribute_unit_override,
        "trust_entity_unit_for_attributes": source.trust_entity_unit_for_attributes,
    }
    if source.signed_current:
        # Written only when set, so a source stored before the flag existed still compares equal.
        stored["signed_current"] = True
    return stored


_ENTITY_ID_RE = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")


def _is_valid_entity_id(value: Any) -> bool:
    return isinstance(value, str) and bool(_ENTITY_ID_RE.match(value))


def _valid_phase_mapping(
    value: Any, *, values_must_be_entity_ids: bool
) -> dict[PhaseName, str] | None:
    """A stored `{phase: str}` mapping, or `None` if invalid.

    Must be a non-empty mapping; keys must be in `PHASES`; values non-empty
    strings (entity-ID shaped when `values_must_be_entity_ids`).
    """
    if not isinstance(value, Mapping):
        return None
    result: dict[PhaseName, str] = {}
    for key, val in value.items():
        if key not in PHASES:
            return None
        if not isinstance(val, str) or not val:
            return None
        if values_must_be_entity_ids and not _is_valid_entity_id(val):
            return None
        result[key] = val
    if not result:
        return None
    return result


def source_from_dict(data: Any) -> PhaseMeasurementSource | None:
    """The inverse of `source_to_dict`.

    Storage is untrusted: any invalid or ambiguous shape (bad kind, bad
    mappings, non-bool trust flag, non-ampere unit override, or fields foreign
    to the stored `kind`) yields `None` ("not configured") instead of raising.
    """
    if not isinstance(data, Mapping):
        return None
    kind = data.get("kind")
    if kind not in ("separate_entities", "attributes"):
        return None

    unit_override = data.get("attribute_unit_override")
    if unit_override is not None and normalized_ampere_unit(unit_override) is None:
        return None
    trust_flag = data.get("trust_entity_unit_for_attributes", False)
    if not isinstance(trust_flag, bool):
        return None
    signed_flag = data.get("signed_current", False)
    if not isinstance(signed_flag, bool):
        return None

    if kind == "separate_entities":
        if data.get("entity_id") is not None or data.get("attributes") is not None:
            return None
        entity_ids = _valid_phase_mapping(data.get("entity_ids"), values_must_be_entity_ids=True)
        if entity_ids is None:
            return None
        return PhaseMeasurementSource(
            kind="separate_entities",
            entity_ids=entity_ids,
            attribute_unit_override=unit_override,
            trust_entity_unit_for_attributes=trust_flag,
            signed_current=signed_flag,
        )

    # kind == "attributes"
    if data.get("entity_ids") is not None:
        return None
    entity_id = data.get("entity_id")
    if not _is_valid_entity_id(entity_id):
        return None
    attributes = _valid_phase_mapping(data.get("attributes"), values_must_be_entity_ids=False)
    if attributes is None:
        return None
    return PhaseMeasurementSource(
        kind="attributes",
        entity_id=entity_id,
        attributes=attributes,
        attribute_unit_override=unit_override,
        trust_entity_unit_for_attributes=trust_flag,
        signed_current=signed_flag,
    )


@dataclass(frozen=True, slots=True)
class GridPowerSource:
    """The meter's total grid power: one entity (`power`, signed) or an import/export pair
    (`power` is the import half, `power_export` the export half; both >= 0). Read through
    `combine_power_pair`, so a missing half makes the total missing, never zero."""

    power: str
    power_export: str | None = None

    @property
    def entity_ids(self) -> tuple[str, ...]:
        return (self.power,) if self.power_export is None else (self.power, self.power_export)


def grid_power_source_to_dict(source: GridPowerSource | None) -> dict[str, str] | None:
    """Serialize a total grid power source for a config entry's `data`."""
    if source is None:
        return None
    stored = {"power": source.power}
    if source.power_export is not None:
        stored["power_export"] = source.power_export
    return stored


def grid_power_source_from_dict(data: Any) -> GridPowerSource | None:
    """The inverse of `grid_power_source_to_dict`. Storage is untrusted: anything that is not a mapping
    with an entity-id shaped `power` (and, if present, an entity-id shaped `power_export`) is "not
    configured"."""
    if not isinstance(data, Mapping):
        return None
    power = data.get("power")
    if not _is_valid_entity_id(power):
        return None
    export = data.get("power_export")
    if export in (None, ""):
        return GridPowerSource(power=power)
    if not _is_valid_entity_id(export):
        return None
    return GridPowerSource(power=power, power_export=export)
