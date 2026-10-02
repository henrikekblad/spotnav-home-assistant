"""The site form: current-source suggestions, basic and detail schemas, and parsing a submitted site."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
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
    CONF_VOLTAGE_BETWEEN_PHASES_V,
    DEFAULT_VOLTAGE_BETWEEN_PHASES_V,
    DOMAIN,
    ENTRY_TYPE_CHARGER,
    MEASUREMENT_MODE_DERIVED,
    MEASUREMENT_MODE_DIRECT,
    VOLTAGE_BETWEEN_PHASES_CHOICES,
)
from ..site.measurement_source import source_to_dict
from ..site.site_detection import MeterCandidate
from ..site.site_membership import chargers_claimed_by_other_sites
from ..planning.grid_voltage import default_voltage_between_phases_v
from ..vehicles.choices import flow_language
from ..vehicles.discovery import DiscoveryCandidate
from .labels import (
    candidate_options,
    choice_option,
    MANUAL_ATTRIBUTES_CHOICE,
    MANUAL_CHOICE,
    MANUAL_CHOICES,
    MANUAL_ENTITIES_CHOICE,
    PHASES,
    SKIP_CHOICE,
)
from .site_confirm import candidate_label
from .unit_check import unit_error
from .measured_source import (
    charger_device_id,
    default_measured_choice,
    manual_attributes_source,
    matching_candidate_id,
)


_LOGGER = logging.getLogger(__name__)

# Derived mode's optional per-phase sources and the quantity (unit family) each must report. Without any of
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


def site_margin_errors(user_input: dict[str, Any]) -> dict[str, str]:
    """A safety margin at or above the main fuse leaves no current for any charger: refused."""
    fuse = user_input.get(CONF_MAIN_FUSE_A)
    margin = user_input.get(CONF_SAFETY_MARGIN_A, DEFAULT_SAFETY_MARGIN_A)
    if (
        isinstance(fuse, (int, float))
        and isinstance(margin, (int, float))
        and margin >= fuse
    ):
        return {CONF_SAFETY_MARGIN_A: "safety_margin_at_or_above_fuse"}
    return {}


def voltage_between_phases_selector() -> selector.SelectSelector:
    """The two choices of the voltage between phases, worded by the `voltage_between_phases` selector
    translation: 400 V (TN, the usual network) and 230 V (IT, much of Norway).
    """
    return selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=[str(volts) for volts in VOLTAGE_BETWEEN_PHASES_CHOICES],
            translation_key="voltage_between_phases",
            mode=selector.SelectSelectorMode.DROPDOWN,
        )
    )


def voltage_between_phases_from_form(user_input: dict[str, Any], default: float | None = None) -> int:
    """The submitted voltage between phases as the whole number stored; an absent or unknown value is
    `default`, else the TN default.
    """
    try:
        value = int(float(user_input.get(CONF_VOLTAGE_BETWEEN_PHASES_V)))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        value = None
    if value in VOLTAGE_BETWEEN_PHASES_CHOICES:
        return value
    return int(default if default is not None else DEFAULT_VOLTAGE_BETWEEN_PHASES_V)


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
                CONF_VOLTAGE_BETWEEN_PHASES_V,
                default=str(
                    voltage_between_phases_from_form(
                        defaults, default=default_voltage_between_phases_v(hass)
                    )
                ),
            ): voltage_between_phases_selector(),
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
    """One `site_details` submission, held on the flow while each associated charger's wiring is
    collected (`async_step_site_charger_wiring`, one step per charger); carries what both flows need to
    finish saving it. `charger_inputs` fills up as those steps are submitted, keyed by charger entry id.
    """

    details: dict[str, Any]
    charger_entry_ids: list[str]
    mode: str
    skip_direct_fields: bool
    charger_candidates: dict[str, list[DiscoveryCandidate]]
    charger_inputs: dict[str, dict[str, Any]] = field(default_factory=dict)


def charger_wiring_schema(
    hass,
    charger_entry_id: str,
    *,
    existing: dict[str, Any] | None = None,
    candidates: list[DiscoveryCandidate] | None = None,
) -> vol.Schema:
    """One charger's wiring: its phase count, the connected phase of a single-phase one and, when its
    device resolves (`charger_device_id`), an optional measured-current source. The keys are fixed
    (`phases`, `phase`, `measured_source`) so each has a translated label; which charger it is, is
    said by the step's description.

    The measured-current dropdown offers every discovered candidate, "manual" (one entity with
    per-phase attribute names, `async_step_charger_manual_source`), "manual_entities" (three entities,
    one per phase, `async_step_charger_manual_entities`) and "skip"; a charger with no device shows no
    such field. `candidates` is what the async caller resolved (`async_charger_measured_candidates`);
    the starting selection is `default_measured_choice`'s decision.
    """
    existing = existing or {}
    candidates = candidates or []
    schema_dict: dict[Any, Any] = {
        vol.Required("phases", default=existing.get("phases", 3)): vol.In([1, 3])
    }
    phase_field: Any = vol.Optional("phase")
    if existing.get("phase") is not None:
        phase_field = vol.Optional("phase", description={"suggested_value": existing["phase"]})
    schema_dict[phase_field] = vol.In(PHASES)
    if charger_device_id(hass, charger_entry_id) is not None:
        # Shown whenever the device resolves, even with no candidate: manual entry is what such a
        # charger needs. Without a device there is nothing to list or check.
        default_choice = default_measured_choice(
            candidates, existing.get(CONF_MEASURED_CURRENT_SOURCE)
        )
        field_marker: Any = vol.Optional("measured_source")
        if default_choice is not None:
            field_marker = vol.Optional("measured_source", default=default_choice)
        schema_dict[field_marker] = selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=candidate_options(
                    hass,
                    candidates,
                    manual_options=(
                        choice_option(hass, "manual_attributes", value=MANUAL_CHOICE),
                        choice_option(
                            hass, "manual_separate_entities", value=MANUAL_ENTITIES_CHOICE
                        ),
                    ),
                ),
                mode=selector.SelectSelectorMode.DROPDOWN,
            )
        )
    return vol.Schema(schema_dict)


def site_details_schema(
    hass,
    mode: str,
    *,
    direct_defaults: dict[str, str] | None = None,
    derived_defaults: dict[str, dict[str, str]] | None = None,
    skip_direct_fields: bool = False,
    flag_defaults: dict[str, bool] | None = None,
) -> vol.Schema:
    """The measurement entities for `mode`, plus the sign options.

    Shared by both flows; never includes derived-mode fields in direct mode or vice versa.
    `*_defaults` pre-fill an existing site's values. The pickers take any `sensor`, since meter
    readers without a device class are common; `site_details_unit_errors` checks the unit on submit.

    `skip_direct_fields` omits the direct-mode L1/L2/L3 pickers when the site's current was already
    resolved as a single generic source or "skip". Each charger's own wiring is a step of its own
    (`charger_wiring_schema`).
    """
    direct_defaults = direct_defaults or {}
    derived_defaults = derived_defaults or {}
    sensor_picker = selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor"))
    schema_dict: dict[Any, Any] = {}
    if not skip_direct_fields:
        if mode == MEASUREMENT_MODE_DIRECT:
            for phase in PHASES:
                key = f"direct_{phase}"
                field_marker: Any = vol.Required(key, default=direct_defaults[phase]) if direct_defaults.get(
                    phase
                ) else vol.Required(key)
                schema_dict[field_marker] = sensor_picker
        elif mode == MEASUREMENT_MODE_DERIVED:
            for phase in PHASES:
                existing_phase = derived_defaults.get(phase) or {}
                for sub_key in ("power", "voltage"):
                    key = f"derived_{phase}_{sub_key}"
                    field_marker = (
                        vol.Required(key, default=existing_phase[sub_key])
                        if existing_phase.get(sub_key)
                        else vol.Required(key)
                    )
                    schema_dict[field_marker] = sensor_picker
                for sub_key, _quantity in DERIVED_OPTIONAL_SUBKEYS:
                    key = f"derived_{phase}_{sub_key}"
                    # A stored value is the field's default, not a suggestion: an untouched
                    # submission must keep the more exact source rather than silently fall back to
                    # an estimate (clearing one is what the card's editor is for).
                    optional: Any = vol.Optional(key)
                    if existing_phase.get(sub_key):
                        optional = vol.Optional(key, default=existing_phase[sub_key])
                    schema_dict[optional] = sensor_picker
    flag_defaults = flag_defaults or {}
    schema_dict[
        vol.Optional(CONF_SITE_CURRENT_SIGNED, default=flag_defaults.get(CONF_SITE_CURRENT_SIGNED, False))
    ] = bool
    if mode == MEASUREMENT_MODE_DERIVED:
        schema_dict[
            vol.Optional(CONF_GRID_POWER_INVERTED, default=flag_defaults.get(CONF_GRID_POWER_INVERTED, False))
        ] = bool
    return vol.Schema(schema_dict)


def site_details_unit_errors(
    hass, details: dict[str, Any], mode: str, *, skip_direct_fields: bool
) -> dict[str, str]:
    """Field -> error code for every picked site sensor whose unit is not the quantity's: current in A or
    mA, power in W or kW, voltage in V, reactive power in var or kvar, apparent power in VA or kVA.
    Empty when the submission is fine (or has no entity fields)."""
    if skip_direct_fields:
        return {}
    checks: list[tuple[str, str]] = []
    if mode == MEASUREMENT_MODE_DIRECT:
        checks = [(f"direct_{phase}", "current") for phase in PHASES]
    elif mode == MEASUREMENT_MODE_DERIVED:
        for phase in PHASES:
            checks += [(f"derived_{phase}_power", "power"), (f"derived_{phase}_voltage", "voltage")]
            checks += [(f"derived_{phase}_{sub_key}", quantity) for sub_key, quantity in DERIVED_OPTIONAL_SUBKEYS]
    errors: dict[str, str] = {}
    for key, quantity in checks:
        code = unit_error(hass, details.get(key), quantity)
        if code is not None:
            errors[key] = code
    return errors


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


def parse_charger_wiring(
    existing: dict[str, Any] | None,
    inputs: dict[str, Any],
    candidates: list[DiscoveryCandidate] | None = None,
    manual_source: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One charger's stored wiring from its `site_charger_wiring` submission, merged onto `existing` so
    fields the form did not show survive a save.

    `candidates` must be what the form was built from; a `candidate_id` no longer among them is ignored
    rather than stored. `manual_source` is what the manual steps collected (already fully checked); a
    "manual" choice without one keeps what the charger had, and an untouched field changes nothing:
    only an explicit "skip" clears a stored source.
    """
    wiring = dict(existing or {})
    wiring["phases"] = inputs["phases"]
    wiring["phase"] = inputs.get("phase")
    choice = inputs.get("measured_source")
    if choice is None:
        return wiring
    if choice == SKIP_CHOICE:
        wiring.pop(CONF_MEASURED_CURRENT_SOURCE, None)
    elif choice in MANUAL_CHOICES:
        if manual_source is not None:
            wiring[CONF_MEASURED_CURRENT_SOURCE] = manual_source
    else:
        chosen = next((c for c in candidates or [] if c.candidate_id == choice), None)
        if chosen is not None:
            wiring[CONF_MEASURED_CURRENT_SOURCE] = source_to_dict(chosen.mapping)
    return wiring


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
    charger_inputs: dict[str, dict[str, Any]] | None = None,
) -> tuple[dict[str, dict[str, Any]], dict[str, str], dict[str, dict[str, str]]]:
    """The inverse of `site_details_schema`'s field naming plus every charger's wiring, shared by both
    flows. `charger_inputs` holds each charger's `site_charger_wiring` submission (see
    `parse_charger_wiring`) and `manual_sources` what the manual steps collected, both by charger
    entry id; the wiring merges onto `phase_wiring_defaults`.
    """
    phase_wiring_defaults = phase_wiring_defaults or {}
    charger_candidates = charger_candidates or {}
    manual_sources = manual_sources or {}
    charger_inputs = charger_inputs or {}
    phase_wiring: dict[str, dict[str, Any]] = {
        charger_entry_id: parse_charger_wiring(
            phase_wiring_defaults.get(charger_entry_id),
            charger_inputs[charger_entry_id],
            charger_candidates.get(charger_entry_id),
            manual_sources.get(charger_entry_id),
        )
        for charger_entry_id in charger_entry_ids
    }

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
            for sub_key, _quantity in DERIVED_OPTIONAL_SUBKEYS:
                value = details.get(f"derived_{phase}_{sub_key}")
                if value:
                    entry[sub_key] = value
            derived_entities[phase] = entry
    return phase_wiring, direct_entities, derived_entities
