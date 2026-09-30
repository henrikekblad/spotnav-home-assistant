"""The site form: current-source suggestions, basic and detail schemas, and parsing a submitted site."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import voluptuous as vol
from homeassistant.helpers import selector

from ..api.entity_fields import MAIN_FUSE_MIN_A
from ..const import (
    CONF_CHARGER_ENTRY_IDS,
    CONF_ENTRY_TYPE,
    CONF_GRID_POWER_INVERTED,
    CONF_MAIN_FUSE_A,
    CONF_MEASURED_CURRENT_SOURCE,
    CONF_MEASUREMENT_MODE,
    CONF_SAFETY_MARGIN_A,
    CONF_SITE_CURRENT_SIGNED,
    DOMAIN,
    ENTRY_TYPE_CHARGER,
    MEASUREMENT_MODE_DERIVED,
    MEASUREMENT_MODE_DIRECT,
)
from ..site.measurement_source import source_to_dict
from ..site.site_detection import MeterCandidate
from ..site.site_membership import chargers_claimed_by_other_sites
from ..vehicles.choices import flow_language
from ..vehicles.discovery import DiscoveryCandidate
from .labels import (
    candidate_options,
    choice_option,
    MANUAL_ATTRIBUTES_CHOICE,
    MANUAL_CHOICE,
    PHASES,
    SKIP_CHOICE,
)
from .site_confirm import candidate_label
from .measured_source import (
    charger_device_id,
    default_measured_choice,
    manual_attributes_source,
    matching_candidate_id,
)


_LOGGER = logging.getLogger(__name__)

# Derived mode's optional per-phase sources and the device class each is picked by. Without any of
# `reactive_power`, `apparent_power` and `current` the fuse check estimates the current from power.
DERIVED_OPTIONAL_SUBKEYS: tuple[tuple[str, str], ...] = (
    ("power_export", "power"),
    ("reactive_power", "reactive_power"),
    ("apparent_power", "apparent_power"),
    ("current", "current"),
)

#: The site's sign options, as fields of the details form.
DETECTED_CHOICE_PREFIX = "detected:"


def site_detected_schema(
    hass, candidates: list[MeterCandidate], *, default_choice: str, offer_enable: bool
) -> vol.Schema:
    """The detected-meter form: each candidate by its integration and device, or "manual". Offers
    to enable the candidates' disabled-by-default entities when there are any."""
    options = [
        selector.SelectOptionDict(
            value=candidate.candidate_id,
            label=candidate_label(hass, candidate),
        )
        for candidate in candidates
    ]
    options.append(choice_option(hass, "manual_separate_entities", value=MANUAL_CHOICE))
    fields: dict[Any, Any] = {
        vol.Required("choice", default=default_choice): selector.SelectSelector(
            selector.SelectSelectorConfig(options=options, mode=selector.SelectSelectorMode.DROPDOWN)
        )
    }
    if offer_enable:
        fields[vol.Optional("enable_disabled", default=True)] = bool
    return vol.Schema(fields)


def detected_defaults(candidate: MeterCandidate) -> tuple[dict[str, str], dict[str, dict[str, str]], dict[str, bool]]:
    """`(direct_defaults, derived_defaults, flags)` a chosen candidate pre-fills into the details
    form, flags keyed by stored config key."""
    return (
        dict(candidate.direct_entities or {}),
        {phase: dict(values) for phase, values in (candidate.derived_entities or {}).items()},
        {
            CONF_SITE_CURRENT_SIGNED: candidate.signed_current,
            CONF_GRID_POWER_INVERTED: candidate.power_inverted,
        },
    )



def site_default_current_choice(
    candidates: list[DiscoveryCandidate], stored_source: dict | None
) -> str:
    """Which option the site's own current-source dropdown starts on.

    A stored source still matching a discovered candidate keeps that candidate's id
    (`matching_candidate_id`); a manual-attributes source starts on `MANUAL_ATTRIBUTES_CHOICE` so its
    sub-form pre-fills; anything else starts on `MANUAL_CHOICE` (the guided per-phase pickers).
    Unlike a charger's optional dropdown, the site's is a required single value with those pickers
    as fallback.
    """
    matched = matching_candidate_id(candidates, stored_source)
    if matched is not None:
        return matched
    if manual_attributes_source(stored_source):
        return MANUAL_ATTRIBUTES_CHOICE
    return MANUAL_CHOICE


def resolve_site_current_choice(
    candidates: list[DiscoveryCandidate], choice: str
) -> tuple[str, dict[str, Any] | None]:
    """`(choice_kind, serialized_source)` for one submitted site-current choice: `("candidate", mapping)`,
    or `(choice, None)` for "manual" (the per-phase pickers write `CONF_DIRECT_ENTITIES` /
    `CONF_DERIVED_ENTITIES`) and "skip". The manual-attributes choice is resolved by its own step.
    """
    if choice in (MANUAL_CHOICE, SKIP_CHOICE):
        return choice, None
    candidate = next(c for c in candidates if c.candidate_id == choice)
    return "candidate", source_to_dict(candidate.mapping)


def site_current_suggestions_schema(
    hass, candidates: list[DiscoveryCandidate], *, default_choice: str
) -> vol.Schema:
    """The site-current suggestions form: every discovered candidate (possibly none), both manual
    shapes and "skip", with `default_choice` preselected. Shared by the create and options flows.
    """
    options = candidate_options(
        hass,
        candidates,
        manual_options=(
            choice_option(hass, "manual_separate_entities", value=MANUAL_CHOICE),
            choice_option(hass, "manual_attributes", value=MANUAL_ATTRIBUTES_CHOICE),
        ),
    )
    return vol.Schema(
        {
            vol.Required("choice", default=default_choice): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=options, mode=selector.SelectSelectorMode.DROPDOWN
                )
            )
        }
    )


#: The safety margin a new site starts on.
DEFAULT_SAFETY_MARGIN_A = 1.0


def default_site_name(hass) -> str:
    return "Anl\u00e4ggning" if flow_language(hass) == "sv" else "Site"


def _default_kwarg(defaults: dict[str, Any], key: str) -> dict[str, Any]:
    """`{"default": defaults[key]}` if present, else `{}`: no guessed pre-fill for a new site."""
    return {"default": defaults[key]} if key in defaults else {}


def site_basic_schema(
    hass,
    *,
    defaults: dict[str, Any] | None = None,
    exclude_entry_id: str | None = None,
) -> vol.Schema:
    """Main fuse, safety margin, measurement mode and associated chargers, shared by both flows.
    `exclude_entry_id` (a site editing itself) excludes it from the duplicate-membership scan and
    keeps its own chargers selectable.
    """
    defaults = defaults or {}
    current_chargers = set(defaults.get(CONF_CHARGER_ENTRY_IDS, []))
    charger_options = _available_charger_options(
        hass, exclude_entry_id=exclude_entry_id, keep=current_chargers
    )
    # The only charger there is starts selected on a new site.
    charger_default = (
        list(charger_options)
        if CONF_CHARGER_ENTRY_IDS not in defaults and len(charger_options) == 1
        else defaults.get(CONF_CHARGER_ENTRY_IDS, [])
    )
    return vol.Schema(
        {
            vol.Optional("name", default=defaults.get("name", default_site_name(hass))): str,
            vol.Required(CONF_MAIN_FUSE_A, **_default_kwarg(defaults, CONF_MAIN_FUSE_A)): vol.All(
                vol.Coerce(float), vol.Range(min=MAIN_FUSE_MIN_A)
            ),
            vol.Optional(
                CONF_SAFETY_MARGIN_A, default=defaults.get(CONF_SAFETY_MARGIN_A, DEFAULT_SAFETY_MARGIN_A)
            ): vol.All(vol.Coerce(float), vol.Range(min=0)),
            vol.Required(
                CONF_MEASUREMENT_MODE,
                default=defaults.get(CONF_MEASUREMENT_MODE, MEASUREMENT_MODE_DIRECT),
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[MEASUREMENT_MODE_DIRECT, MEASUREMENT_MODE_DERIVED],
                    translation_key="measurement_mode",
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            ),
            vol.Optional(
                CONF_CHARGER_ENTRY_IDS, default=charger_default
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[
                        selector.SelectOptionDict(value=entry_id, label=title)
                        for entry_id, title in charger_options.items()
                    ],
                    multiple=True,
                    mode=selector.SelectSelectorMode.LIST,
                )
            ),
        }
    )


def _available_charger_options(
    hass, *, exclude_entry_id: str | None = None, keep: set[str] = frozenset()
) -> dict[str, str]:
    """Charger entry id -> title, excluding chargers claimed by a different site entry except those in
    `keep`. `site_membership_errors` remains the authoritative check on submit.
    """
    claimed = chargers_claimed_by_other_sites(hass, exclude_entry_id=exclude_entry_id)
    return {
        entry.entry_id: entry.title
        for entry in hass.config_entries.async_entries(DOMAIN)
        if entry.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_CHARGER
        and (entry.entry_id not in claimed or entry.entry_id in keep)
    }


@dataclass(frozen=True, slots=True)
class PendingSiteDetails:
    """One `site_details` submission, held on the flow while the "manual" charger entries it asked for
    are collected one at a time; carries what both flows need to finish saving it.
    """

    details: dict[str, Any]
    charger_entry_ids: list[str]
    mode: str
    skip_direct_fields: bool
    charger_candidates: dict[str, list[DiscoveryCandidate]]


def site_details_schema(
    hass,
    charger_entry_ids: list[str],
    mode: str,
    *,
    phase_wiring_defaults: dict[str, dict[str, Any]] | None = None,
    direct_defaults: dict[str, str] | None = None,
    derived_defaults: dict[str, dict[str, str]] | None = None,
    skip_direct_fields: bool = False,
    charger_candidates: dict[str, list[DiscoveryCandidate]] | None = None,
    flag_defaults: dict[str, bool] | None = None,
) -> vol.Schema:
    """Per-charger phase wiring, plus the measurement entities for `mode`.

    Shared by both flows; never includes derived-mode fields in direct mode or vice versa.
    `*_defaults` pre-fill an existing site's values.

    `skip_direct_fields` omits the direct-mode L1/L2/L3 pickers when the site's current was already
    resolved as a single generic source or "skip".

    Each charger whose device can be resolved (`charger_device_id`) also gets an optional
    measured-current-source field: every discovered candidate
    (`async_discover_charger_current_sources`), "manual" (one entity on that device with per-phase
    attribute names, collected in `async_step_charger_manual_source`) and "skip". A charger with no
    device shows no field.

    `charger_candidates` is what the async caller resolved (`async_charger_measured_candidates`);
    none means only "manual"/"skip", so this builder does no discovery. The starting selection is
    `default_measured_choice`'s decision.
    """
    phase_wiring_defaults = phase_wiring_defaults or {}
    direct_defaults = direct_defaults or {}
    derived_defaults = derived_defaults or {}
    charger_candidates = charger_candidates or {}
    schema_dict: dict[Any, Any] = {}
    for charger_entry_id in charger_entry_ids:
        existing = phase_wiring_defaults.get(charger_entry_id) or {}
        schema_dict[
            vol.Required(f"phases_{charger_entry_id}", default=existing.get("phases", 3))
        ] = vol.In([1, 3])
        phase_field: Any = vol.Optional(f"phase_{charger_entry_id}")
        if existing.get("phase") is not None:
            phase_field = vol.Optional(
                f"phase_{charger_entry_id}", description={"suggested_value": existing["phase"]}
            )
        schema_dict[phase_field] = vol.In(PHASES)

        candidates = charger_candidates.get(charger_entry_id) or []
        device_id = charger_device_id(hass, charger_entry_id)
        if device_id is not None:
            # Shown whenever the device resolves, even with no candidate: manual entry is what such
            # a charger needs. Without a device there is nothing to list or check.
            default_choice = default_measured_choice(
                candidates, existing.get(CONF_MEASURED_CURRENT_SOURCE)
            )
            field = vol.Optional(f"measured_source_{charger_entry_id}")
            if default_choice is not None:
                field = vol.Optional(
                    f"measured_source_{charger_entry_id}", default=default_choice
                )
            schema_dict[field] = selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=candidate_options(
                        hass, candidates, manual_options=(choice_option(hass, MANUAL_CHOICE),)
                    ),
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            )
    if not skip_direct_fields:
        if mode == MEASUREMENT_MODE_DIRECT:
            for phase in PHASES:
                key = f"direct_{phase}"
                field: Any = vol.Required(key, default=direct_defaults[phase]) if direct_defaults.get(
                    phase
                ) else vol.Required(key)
                schema_dict[field] = selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor", device_class="current")
                )
        elif mode == MEASUREMENT_MODE_DERIVED:
            for phase in PHASES:
                existing_phase = derived_defaults.get(phase) or {}
                for sub_key, device_class in (("power", "power"), ("voltage", "voltage")):
                    key = f"derived_{phase}_{sub_key}"
                    field = (
                        vol.Required(key, default=existing_phase[sub_key])
                        if existing_phase.get(sub_key)
                        else vol.Required(key)
                    )
                    schema_dict[field] = selector.EntitySelector(
                        selector.EntitySelectorConfig(domain="sensor", device_class=device_class)
                    )
                for sub_key, device_class in DERIVED_OPTIONAL_SUBKEYS:
                    key = f"derived_{phase}_{sub_key}"
                    # A stored value is the field's default, not a suggestion: an untouched
                    # submission must keep the more exact source rather than silently fall back to
                    # an estimate (clearing one is what the card's editor is for).
                    optional: Any = vol.Optional(key)
                    if existing_phase.get(sub_key):
                        optional = vol.Optional(key, default=existing_phase[sub_key])
                    schema_dict[optional] = selector.EntitySelector(
                        selector.EntitySelectorConfig(domain="sensor", device_class=device_class)
                    )
    flag_defaults = flag_defaults or {}
    schema_dict[
        vol.Optional(CONF_SITE_CURRENT_SIGNED, default=flag_defaults.get(CONF_SITE_CURRENT_SIGNED, False))
    ] = bool
    if mode == MEASUREMENT_MODE_DERIVED:
        schema_dict[
            vol.Optional(CONF_GRID_POWER_INVERTED, default=flag_defaults.get(CONF_GRID_POWER_INVERTED, False))
        ] = bool
    return vol.Schema(schema_dict)


def parse_site_flags(details: dict[str, Any], mode: str, stored: dict[str, Any] | None = None) -> dict[str, bool]:
    """The site's sign options from a `site_details` submission, `site_current_signed` always and
    `grid_power_inverted` in derived mode; an absent one keeps `stored`.

    A flag is returned only when it carries information: set, or already stored (so a round-trip
    save of an untouched entry stays a no-op and absent keeps reading as off).
    """
    stored = stored or {}
    keys = [CONF_SITE_CURRENT_SIGNED]
    if mode == MEASUREMENT_MODE_DERIVED:
        keys.append(CONF_GRID_POWER_INVERTED)
    flags: dict[str, bool] = {}
    for key in keys:
        value = bool(details.get(key, stored.get(key, False)))
        if value or key in stored:
            flags[key] = value
    if mode != MEASUREMENT_MODE_DERIVED and CONF_GRID_POWER_INVERTED in stored:
        flags[CONF_GRID_POWER_INVERTED] = bool(stored[CONF_GRID_POWER_INVERTED])
    return flags


def parse_site_details(
    hass,
    details: dict[str, Any],
    charger_entry_ids: list[str],
    mode: str,
    *,
    phase_wiring_defaults: dict[str, dict[str, Any]] | None = None,
    skip_direct_fields: bool = False,
    charger_candidates: dict[str, list[DiscoveryCandidate]] | None = None,
    manual_sources: dict[str, dict[str, Any]] | None = None,
) -> tuple[dict[str, dict[str, Any]], dict[str, str], dict[str, dict[str, str]]]:
    """The inverse of `site_details_schema`'s field naming, shared by both flows.

    Merges onto `phase_wiring_defaults` per charger so fields the schema did not show are preserved
    across a save.

    `charger_candidates` must be the mapping `site_details_schema` was built from; a `candidate_id`
    no longer among them is ignored rather than stored. `manual_sources` is what
    `async_step_charger_manual_source` collected (by charger entry id, already fully checked); a
    "manual" choice absent from it keeps what the charger had, and an untouched field changes
    nothing: only an explicit "skip" clears a stored source.
    """
    phase_wiring_defaults = phase_wiring_defaults or {}
    charger_candidates = charger_candidates or {}
    manual_sources = manual_sources or {}
    phase_wiring: dict[str, dict[str, Any]] = {}
    for charger_entry_id in charger_entry_ids:
        existing = dict(phase_wiring_defaults.get(charger_entry_id) or {})
        existing["phases"] = details[f"phases_{charger_entry_id}"]
        existing["phase"] = details.get(f"phase_{charger_entry_id}")
        measured_choice = details.get(f"measured_source_{charger_entry_id}")
        if measured_choice is not None:
            if measured_choice == SKIP_CHOICE:
                existing.pop(CONF_MEASURED_CURRENT_SOURCE, None)
            elif measured_choice == MANUAL_CHOICE:
                manual_source = manual_sources.get(charger_entry_id)
                if manual_source is not None:
                    existing[CONF_MEASURED_CURRENT_SOURCE] = manual_source
            else:
                candidates = charger_candidates.get(charger_entry_id) or []
                chosen = next(
                    (c for c in candidates if c.candidate_id == measured_choice), None
                )
                if chosen is not None:
                    existing[CONF_MEASURED_CURRENT_SOURCE] = source_to_dict(chosen.mapping)
        phase_wiring[charger_entry_id] = existing

    direct_entities: dict[str, str] = {}
    derived_entities: dict[str, dict[str, str]] = {}
    if skip_direct_fields:
        return phase_wiring, direct_entities, derived_entities
    if mode == MEASUREMENT_MODE_DIRECT:
        direct_entities = {phase: details[f"direct_{phase}"] for phase in PHASES}
    elif mode == MEASUREMENT_MODE_DERIVED:
        derived_entities = {}
        for phase in PHASES:
            entry = {
                "power": details[f"derived_{phase}_power"],
                "voltage": details[f"derived_{phase}_voltage"],
            }
            for sub_key, _device_class in DERIVED_OPTIONAL_SUBKEYS:
                value = details.get(f"derived_{phase}_{sub_key}")
                if value:
                    entry[sub_key] = value
            derived_entities[phase] = entry
    return phase_wiring, direct_entities, derived_entities
