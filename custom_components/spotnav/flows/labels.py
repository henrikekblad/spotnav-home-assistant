"""Localized labels, choice values and dropdown builders shared by the config and options flows."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

from homeassistant.helpers import entity_registry as er, selector

from ..const import (
    CURRENT_CONTROL_CHANGE_CONFIGURATION,
    CURRENT_CONTROL_EASEE,
    CURRENT_CONTROL_NUMBER,
)
from ..texts import table
from ..vehicles.choices import entity_option, flow_language
from ..vehicles.discovery import DiscoveryCandidate


_LOGGER = logging.getLogger(__name__)


PHASES = ("L1", "L2", "L3")


SKIP_CHOICE = "skip"


# The site's own current entered by hand as three whole entities, one per phase (as the guided
# `direct_{phase}`/`derived_{phase}_*` pickers in `site_details` collect).
MANUAL_CHOICE = "manual"


# The other manual shape, for a charger's own measured current or the site's total: one entity
# carrying all three phases as attributes, with their names given explicitly.
MANUAL_ATTRIBUTES_CHOICE = "manual_attributes"


# A charger's measured current as three whole entities, one per phase (its own device only).
MANUAL_ENTITIES_CHOICE = "manual_entities"


#: Every choice that sends a charger's measured current to a manual step.
MANUAL_CHOICES = (MANUAL_CHOICE, MANUAL_ENTITIES_CHOICE)


# The only two units a manually entered attribute mapping may use (the spellings
# `normalized_ampere_unit` recognizes); anything else would yield an always-invalid source.
MANUAL_UNIT_CHOICES: tuple[str, str] = ("A", "mA")


DEFAULT_MANUAL_UNIT = "A"


# Stable form-error codes for the manual-entry steps (under config.error / options.error in the
# translations, like `vehicles/entity_conflicts.py`'s). The last is soft: it re-renders the form
# with a confirm checkbox, because a new charger may have nothing to verify a mapping against.
MANUAL_SOURCE_ENTITY_ERROR = "manual_source_entity_invalid"


MANUAL_SOURCE_DEVICE_ERROR = "manual_source_entity_wrong_device"


SITE_MANUAL_SOURCE_DEVICE_ERROR = "site_manual_source_entity_on_charger_device"


MANUAL_SOURCE_ATTRIBUTES_ERROR = "manual_source_attributes_required"


MANUAL_SOURCE_UNVERIFIED_ERROR = "manual_source_unverified"


MANUAL_ENTITIES_DUPLICATE_ERROR = "manual_entities_duplicate"


# One field per phase of the three-entity form, e.g. "entity_L1".
MANUAL_ENTITY_KEY = "entity_{phase}"


# One field per phase, e.g. "attribute_L1".
MANUAL_ATTRIBUTE_KEY = "attribute_{phase}"


# The labels below are SpotNav's own words (`i18n/<lang>.json`): a candidate's reason code, its
# confidence and representation (the raw code stays in diagnostics/logs), the fixed choices and the
# `CONF_CURRENT_CONTROL` answers (a select selector's options are built in code; the field keeps its
# translation key).


def display_number(value: float) -> float | int:
    """A number as the form shows it: one decimal at most, a whole number without ".0".

    `25 * 1.15` is `28.749999999999996`; a field must not show that.
    """
    rounded = round(float(value), 1)
    return int(rounded) if rounded == int(rounded) else rounded


def current_control_selector(
    hass, kinds: tuple[str, ...] = (CURRENT_CONTROL_CHANGE_CONFIGURATION,)
) -> selector.SelectSelector:
    """The `CONF_CURRENT_CONTROL` dropdown, localized like the other choices.

    `kinds` are the ways to set a current that apply: OCPP's `ChangeConfiguration` for an OCPP
    charger, the number and Easee's service for a detected one. "Do not set a current" is always first.
    """
    text = table(flow_language(hass), "current_control")
    allowed = (CURRENT_CONTROL_CHANGE_CONFIGURATION, CURRENT_CONTROL_NUMBER, CURRENT_CONTROL_EASEE)
    return selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=[
                selector.SelectOptionDict(value="", label=text["none"]),
                *(
                    selector.SelectOptionDict(value=kind, label=text[kind])
                    for kind in allowed
                    if kind in kinds
                ),
            ],
            mode=selector.SelectSelectorMode.DROPDOWN,
        )
    )


def choice_option(hass, text_key: str, *, value: str | None = None) -> Any:
    """One `SelectOptionDict` for a fixed choice: `value` is the stable stored value (default
    `text_key`), `text_key` picks the localized label. They differ because the same stored value
    means different things in different dropdowns (`"manual"` is three entities for the site, one
    entity plus attributes for a charger).
    """
    return selector.SelectOptionDict(
        value=value or text_key, label=table(flow_language(hass), "choice")[text_key]
    )


def power_sensor_entity_options(hass) -> list[Any]:
    """Dropdown options for the optional home-battery aggregate power entity: every enabled `sensor`
    with device class `power`, labelled with friendly name plus entity id (which tells apart
    identically named sensors).
    """
    registry = er.async_get(hass)
    entity_ids: list[str] = []
    for entity_id in sorted(hass.states.async_entity_ids("sensor")):
        state = hass.states.get(entity_id)
        if state is None or state.attributes.get("device_class") != "power":
            continue
        entry = registry.async_get(entity_id)
        if entry is not None and entry.disabled_by is not None:
            continue
        entity_ids.append(entity_id)
    return [entity_option(hass, entity_id) for entity_id in entity_ids]


def _candidate_label(hass, candidate: DiscoveryCandidate) -> str:
    """A human-readable dropdown label for one discovered candidate: names, entity ID(s), representation,
    resolved phase mapping and a short localized confidence and reason; never the raw reason code.
    Only the option's `value` (`candidate_id`) is written, and only once the user submits.
    """
    lang = flow_language(hass)
    mapping = candidate.mapping
    if mapping.kind == "attributes":
        state = hass.states.get(mapping.entity_id)
        name = state.name if state else mapping.entity_id
        entity_part = mapping.entity_id
        phase_part = ", ".join(f"{phase}={attr}" for phase, attr in sorted((mapping.attributes or {}).items()))
    else:
        entity_ids = mapping.entity_ids or {}
        first_id = sorted(entity_ids.values())[0] if entity_ids else candidate.candidate_id
        first_state = hass.states.get(first_id)
        name = first_state.name if first_state else first_id
        entity_part = ", ".join(sorted(entity_ids.values()))
        phase_part = ", ".join(f"{phase}={eid}" for phase, eid in sorted(entity_ids.items()))
    representation = table(lang, "candidate_representation")[mapping.kind]
    confidence_text = table(lang, "candidate_confidence")[candidate.confidence]
    reason_text = table(lang, "candidate_reason").get(candidate.reason_code, candidate.reason_code)
    return f"{name} ({entity_part}) — {representation}, {phase_part} — {confidence_text}: {reason_text}"


def candidate_options(
    hass,
    candidates: list[DiscoveryCandidate],
    *,
    manual_options: Sequence[Any] = (),
) -> list[Any]:
    """`SelectOptionDict` options for a discovery dropdown: one per candidate (`candidate_id` value,
    label from `_candidate_label`), the caller-built `manual_options`, and always "skip for now".
    """
    options = [
        selector.SelectOptionDict(value=candidate.candidate_id, label=_candidate_label(hass, candidate))
        for candidate in candidates
    ]
    options += list(manual_options)
    options.append(choice_option(hass, SKIP_CHOICE))
    return options
