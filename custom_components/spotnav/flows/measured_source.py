"""Choosing how a charger's own current is measured: candidates, device scope and the manual-entry step."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Literal

import voluptuous as vol
from homeassistant.helpers import entity_registry as er, selector

from ..const import CONF_CHARGE_CONTROL
from ..site.measurement_source import PhaseMeasurementSource, source_to_dict
from ..site.site_capacity import normalized_ampere_unit
from ..vehicles.discovery import (
    _async_historical_phase_matches,
    _HISTORY_LOOKBACK_DAYS,
    async_discover_charger_current_sources,
    DiscoveryCandidate,
)
from .labels import (
    DEFAULT_MANUAL_UNIT,
    MANUAL_ATTRIBUTE_KEY,
    MANUAL_CHOICE,
    MANUAL_SOURCE_ATTRIBUTES_ERROR,
    MANUAL_SOURCE_DEVICE_ERROR,
    MANUAL_SOURCE_ENTITY_ERROR,
    MANUAL_SOURCE_UNVERIFIED_ERROR,
    MANUAL_UNIT_CHOICES,
    PHASES,
    SITE_MANUAL_SOURCE_DEVICE_ERROR,
    SKIP_CHOICE,
)


_LOGGER = logging.getLogger(__name__)


def charger_device_id(hass, charger_entry_id: str) -> str | None:
    """The Home Assistant device backing a charger's charge-control entity, if resolvable; used only
    to scope discovery to that charger's device (see vehicles/discovery.py).
    """
    charger_entry = hass.config_entries.async_get_entry(charger_entry_id)
    if charger_entry is None:
        return None
    charge_control = charger_entry.data.get(CONF_CHARGE_CONTROL)
    if not charge_control:
        return None
    registry = er.async_get(hass)
    entity_entry = registry.async_get(charge_control)
    return entity_entry.device_id if entity_entry else None


@dataclass(frozen=True, slots=True)
class _DeviceScope:
    """Which devices a manually entered measured-current entity may belong to (the boundary automatic
    discovery draws).

    `kind == "only"`: the entity must be on one of `device_ids` (a charger's own device); an empty
    `device_ids` allows nothing. `kind == "all_except"`: the entity must be on none of `device_ids`
    (every associated charger's device, for the site's total); an entity with no device stays eligible.
    `device_error` is the stable translated error code for a violation, worded per scope.
    """

    kind: Literal["only", "all_except"]
    device_error: str
    device_ids: frozenset[str] = frozenset()

    def allows(self, device_id: str | None) -> bool:
        """Whether an entity on `device_id` is inside this scope."""
        if self.kind == "only":
            return device_id is not None and device_id in self.device_ids
        return device_id is None or device_id not in self.device_ids


def charger_device_ids(hass, charger_entry_ids: list[str]) -> set[str]:
    """Every one of these chargers' devices that can be resolved: the single computation, so discovery
    and the manual-entry scopes agree on what is off limits.
    """
    return {
        device_id
        for charger_entry_id in charger_entry_ids
        if (device_id := charger_device_id(hass, charger_entry_id)) is not None
    }


def charger_device_scope(hass, charger_entry_id: str) -> _DeviceScope:
    """The scope one charger's own measured current is limited to: its own device."""
    device_id = charger_device_id(hass, charger_entry_id)
    return _DeviceScope(
        kind="only",
        device_error=MANUAL_SOURCE_DEVICE_ERROR,
        device_ids=frozenset({device_id}) if device_id else frozenset(),
    )


def site_device_scope(hass, charger_entry_ids: list[str]) -> _DeviceScope:
    """The scope the site's total current may draw from: everything except every associated charger's
    device (the set `async_discover_site_current_sources` is called with).
    """
    return _DeviceScope(
        kind="all_except",
        device_error=SITE_MANUAL_SOURCE_DEVICE_ERROR,
        device_ids=frozenset(charger_device_ids(hass, charger_entry_ids)),
    )


def _scoped_sensor_entities(hass, scope: _DeviceScope) -> list[str]:
    """Every enabled `sensor` entity a manual-entry picker for `scope` may offer.

    Only a narrowing for the picker: the same membership test runs server-side on the submitted value
    (see `async_manual_source_step`).
    """
    registry = er.async_get(hass)
    entries = (
        er.async_entries_for_device(registry, next(iter(scope.device_ids)))
        if scope.kind == "only" and scope.device_ids
        else registry.entities.values()
    )
    return sorted(
        entry.entity_id
        for entry in entries
        if entry.domain == "sensor"
        and entry.disabled_by is None
        and scope.allows(entry.device_id)
    )


async def async_charger_measured_candidates(
    hass, charger_entry_ids: list[str]
) -> dict[str, list[DiscoveryCandidate]]:
    """Measured-current candidates for every charger in `charger_entry_ids`, keyed by entry id.

    Resolved once per flow step and handed to both `site_details_schema` and `parse_site_details`,
    keeping them synchronous (discovery awaits Recorder access). A charger with no candidates is
    absent from the result, which is normal; manual entry is what that case is for.
    """
    candidates: dict[str, list[DiscoveryCandidate]] = {}
    for charger_entry_id in charger_entry_ids:
        device_id = charger_device_id(hass, charger_entry_id)
        if not device_id:
            continue
        found = await async_discover_charger_current_sources(hass, charger_device_id=device_id)
        if found:
            candidates[charger_entry_id] = found
    return candidates


def matching_candidate_id(candidates: list, stored_source: dict | None) -> str | None:
    """Which currently discovered candidate's mapping matches a charger's stored measured-current
    source, so the options flow pre-selects it. A stored source matching none shows as unselected
    and is not cleared unless the user changes the field (see `parse_site_details`).
    """
    if not stored_source:
        return None
    for candidate in candidates:
        if source_to_dict(candidate.mapping) == stored_source:
            return candidate.candidate_id
    return None


def manual_attributes_source(stored_source: dict | None) -> bool:
    """Whether a stored source is exactly what the manual form can re-create: one entity, a full
    three-phase attribute mapping and an explicit "A"/"mA" override.

    Anything else (separate entities, or `trust_entity_unit_for_attributes`, which the form never
    writes) must not be re-selected as "manual", or an untouched save would rewrite it.
    """
    if not isinstance(stored_source, dict) or stored_source.get("kind") != "attributes":
        return False
    if stored_source.get("trust_entity_unit_for_attributes"):
        return False
    if not stored_source.get("entity_id"):
        return False
    if stored_source.get("attribute_unit_override") not in MANUAL_UNIT_CHOICES:
        return False
    attributes = stored_source.get("attributes")
    return isinstance(attributes, dict) and set(attributes) == set(PHASES)


def default_measured_choice(
    candidates: list[DiscoveryCandidate], stored_source: dict | None
) -> str | None:
    """Which option a charger's measured-current dropdown starts on.

    A stored source matching a discovered candidate keeps its id; one with the manual form's shape is
    pre-selected as "manual"; anything else is left unselected, so an untouched form keeps the stored
    value (`parse_site_details` changes a source only for a value actually chosen). "skip" stays an
    explicit choice.
    """
    if stored_source is None:
        return SKIP_CHOICE
    matched = matching_candidate_id(candidates, stored_source)
    if matched is not None:
        return matched
    if manual_attributes_source(stored_source):
        return MANUAL_CHOICE
    return None


def manual_charger_entry_ids(details: dict[str, Any], charger_entry_ids: list[str]) -> list[str]:
    """Every charger whose submitted `measured_source_<id>` was "manual", in the site's charger order:
    the queue `async_step_charger_manual_source` walks. Shared by the create and options flows.
    """
    return [
        charger_entry_id
        for charger_entry_id in charger_entry_ids
        if details.get(f"measured_source_{charger_entry_id}") == MANUAL_CHOICE
    ]


@dataclass(frozen=True, slots=True)
class _ManualSourceStep:
    """What one render or submission of the manual form resolved to.

    `source` is set only for a complete submission that passes every check (and was confirmed when
    unverified); otherwise `errors` is non-empty and the caller re-shows `schema`.
    """

    schema: vol.Schema
    errors: dict[str, str]
    description_placeholders: dict[str, str]
    source: dict[str, Any] | None


def _manual_source_defaults(
    hass, *, stored_source: dict[str, Any] | None, submitted: dict[str, Any] | None
) -> dict[str, Any]:
    """Pre-fill values for one charger's manual form.

    Precedence: what the user just submitted, then the charger's stored source, then "L1"/"L2"/"L3"
    with unit "A". The unit follows the stored override, else the entity's live
    `unit_of_measurement` only when `normalized_ampere_unit` recognizes it, else "A"; only "A"/"mA"
    are offered, since anything else would make the source permanently invalid.
    """
    entity_id = (submitted or {}).get("entity_id") or (stored_source or {}).get("entity_id")
    values: dict[str, Any] = {}
    if entity_id:
        values["entity_id"] = entity_id
    stored_attributes = (stored_source or {}).get("attributes") or {}
    for phase in PHASES:
        key = MANUAL_ATTRIBUTE_KEY.format(phase=phase)
        values[key] = (submitted or {}).get(key) or stored_attributes.get(phase) or phase
    stored_unit = (stored_source or {}).get("attribute_unit_override")
    if stored_unit in MANUAL_UNIT_CHOICES:
        values["unit"] = stored_unit
    else:
        state = hass.states.get(entity_id) if entity_id else None
        live_unit = (
            normalized_ampere_unit(state.attributes.get("unit_of_measurement"))
            if state is not None
            else None
        )
        values["unit"] = live_unit or DEFAULT_MANUAL_UNIT
    return values


def _manual_source_schema(
    hass,
    *,
    scope: _DeviceScope,
    defaults: dict[str, Any],
    warn_unverified: bool,
) -> vol.Schema:
    """The manual measured-current form for one source (a charger's or the site's).

    The picker offers the `sensor` entities `scope` allows (a narrowing, see `_scoped_sensor_entities`),
    attribute names default to the commonly used ones, and the unit is "A"/"mA". `warn_unverified`
    adds the `confirm_unverified` checkbox, shown only on the re-render after an unverifiable submission.
    """
    entity_field: Any = vol.Required("entity_id")
    if defaults.get("entity_id"):
        # A suggested value pre-fills the picker without restoring a cleared one as a schema default would.
        entity_field = vol.Optional(
            "entity_id", description={"suggested_value": defaults["entity_id"]}
        )
    schema_dict: dict[Any, Any] = {
        entity_field: selector.EntitySelector(
            selector.EntitySelectorConfig(
                domain="sensor",
                include_entities=_scoped_sensor_entities(hass, scope),
            )
        )
    }
    for phase in PHASES:
        key = MANUAL_ATTRIBUTE_KEY.format(phase=phase)
        schema_dict[vol.Optional(key, default=defaults[key])] = str
    schema_dict[vol.Optional("unit", default=defaults["unit"])] = vol.In(MANUAL_UNIT_CHOICES)
    if warn_unverified:
        schema_dict[vol.Optional("confirm_unverified", default=False)] = bool
    return vol.Schema(schema_dict)


async def _async_manual_source_verified(
    hass, *, entity_id: str, attributes: dict[str, str]
) -> bool:
    """Best-effort check that a manually entered mapping describes something real: every attribute name
    is present on the entity's live state, or the entity has a complete phase-attribute set in the
    last `_HISTORY_LOOKBACK_DAYS` days of Recorder history.

    Not an exact-name history check: the only history reader (`vehicles/discovery.py`) reports
    mappings it can recognize, while unrecognized names are what this path is for. So an unverified
    entry yields a soft warning, never a refusal; a wrong mapping still fails measurement validation.
    """
    state = hass.states.get(entity_id)
    if state is not None and all(
        state.attributes.get(name) is not None for name in attributes.values()
    ):
        return True
    matches = await _async_historical_phase_matches(hass, [entity_id])
    return entity_id in matches


async def async_manual_source_step(
    hass,
    *,
    scope: _DeviceScope,
    stored_source: dict[str, Any] | None,
    user_input: dict[str, Any] | None,
) -> _ManualSourceStep:
    """Render, or validate one submission of, a manual-entry form. Checks run server-side, in order:

    1. The entity must exist in the registry and be a `sensor`.
    2. It must be inside `scope` (`_DeviceScope`); enforced here because a replayed or hand-built
       submission never went through the picker.
    3. All three attribute names must be non-empty (a hard error).
    4. The mapping is verified against live state, then Recorder history
       (`_async_manual_source_verified`). Failing both is not an error: the form re-renders with
       `MANUAL_SOURCE_UNVERIFIED_ERROR` and a `confirm_unverified` checkbox, and only a submission
       with it set is accepted.

    The caller stores the returned `source` under its own config key.
    """
    defaults = _manual_source_defaults(hass, stored_source=stored_source, submitted=user_input)
    schema = _manual_source_schema(
        hass, scope=scope, defaults=defaults, warn_unverified=False
    )
    if user_input is None:
        return _ManualSourceStep(schema, {}, {}, None)

    errors: dict[str, str] = {}
    entity_id = user_input.get("entity_id")
    registry_entry = (
        er.async_get(hass).async_get(entity_id) if isinstance(entity_id, str) else None
    )
    if registry_entry is None or registry_entry.domain != "sensor":
        errors["entity_id"] = MANUAL_SOURCE_ENTITY_ERROR
    elif not scope.allows(registry_entry.device_id):
        errors["entity_id"] = scope.device_error
    attributes = {
        phase: str(user_input.get(MANUAL_ATTRIBUTE_KEY.format(phase=phase)) or "").strip()
        for phase in PHASES
    }
    for phase, name in attributes.items():
        if not name:
            errors[MANUAL_ATTRIBUTE_KEY.format(phase=phase)] = MANUAL_SOURCE_ATTRIBUTES_ERROR
    if errors:
        return _ManualSourceStep(schema, errors, {}, None)

    submitted_unit = user_input.get("unit")
    unit = submitted_unit if submitted_unit in MANUAL_UNIT_CHOICES else defaults["unit"]
    verified = await _async_manual_source_verified(hass, entity_id=entity_id, attributes=attributes)
    if not verified and user_input.get("confirm_unverified") is not True:
        return _ManualSourceStep(
            _manual_source_schema(
                hass, scope=scope, defaults=defaults, warn_unverified=True
            ),
            {"base": MANUAL_SOURCE_UNVERIFIED_ERROR},
            {"lookback_days": str(_HISTORY_LOOKBACK_DAYS)},
            None,
        )
    return _ManualSourceStep(
        schema,
        {},
        {},
        source_to_dict(
            PhaseMeasurementSource(
                kind="attributes",
                entity_id=entity_id,
                attributes=attributes,
                attribute_unit_override=unit,
            )
        ),
    )
