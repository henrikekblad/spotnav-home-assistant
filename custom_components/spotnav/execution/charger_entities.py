"""Reading a charger's entities from the registry, by platform and key.

Shared by the detected-charger config flow (`config_flow/charger_detection.py`) and the entity
configuration (`api/entity_fields.py`), which must agree on which entity is which and on which of
the charger's own modes is on. Nothing here writes anything.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .charger_profiles import (
    entity_matches_keys,
    OwnModeRule,
    PATH_EASEE,
    PATH_SELECT,
    PATH_SWITCH,
    PlatformProfile,
    profile_for,
)

#: The label of a charger's own enable switch that is off (`conflict.kind == "disabled"`).
DISABLED_LABEL: Final = "enabled"
CONFLICT_OWN_MODE: Final = "own_mode"
CONFLICT_DISABLED: Final = "disabled"


@dataclass(frozen=True, slots=True)
class OwnModeConflict:
    """One of the charger's own controllers that is on and would fight SpotNav's."""

    entity_id: str
    label: str
    state: str


def state_text(hass: HomeAssistant, entity_id: str) -> str | None:
    state = hass.states.get(entity_id)
    if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN, ""):
        return None
    return str(state.state).strip().lower()


class EntityMatcher:
    """The device's own-integration entities, searchable by domain and keys."""

    def __init__(self, hass: HomeAssistant, entries: list[er.RegistryEntry], profile: PlatformProfile) -> None:
        self.hass = hass
        self.entries = entries
        self.profile = profile

    def find(self, domain: str, keys: tuple[str, ...]) -> list[er.RegistryEntry]:
        """Matching entries, enabled ones first, each group in the order of `keys`."""
        found: list[tuple[int, int, er.RegistryEntry]] = []
        for entry in self.entries:
            if entry.domain != domain:
                continue
            for rank, key in enumerate(keys):
                if entity_matches_keys(
                    translation_key=entry.translation_key,
                    unique_id=entry.unique_id,
                    keys=(key,),
                    key_first=self.profile.key_first,
                ):
                    found.append((0 if entry.disabled_by is None else 1, rank, entry))
                    break
        found.sort(key=lambda item: (item[0], item[1]))
        return [entry for _, _, entry in found]

    def first(self, domain: str, keys: tuple[str, ...]) -> er.RegistryEntry | None:
        matches = self.find(domain, keys)
        return matches[0] if matches else None


def option_for(options: list[str], wanted: tuple[str, ...]) -> str | None:
    """The entity's option that `wanted` names, the first of `wanted` that it offers (so a profile can
    prefer `pause` to `off`), matched lower-case.
    """
    by_name = {option.strip().lower(): option for option in reversed(options)}
    for name in wanted:
        if name in by_name:
            return by_name[name]
    return None


def own_mode_conflicts(
    hass: HomeAssistant, entries: list[er.RegistryEntry], profile: PlatformProfile
) -> list[OwnModeConflict]:
    """The charger's own smart, solar and load-balancing modes that are on right now.

    An entity is a conflict while it reads anything but one of the rule's inactive values; one that
    cannot be read is not (nothing is claimed from silence).
    """
    matcher = EntityMatcher(hass, entries, profile)
    conflicts: list[OwnModeConflict] = []
    for rule in profile.own_modes:
        conflicts.extend(_rule_conflicts(hass, matcher, rule))
    return conflicts


def disabled_switches(
    hass: HomeAssistant, entries: list[er.RegistryEntry], profile: PlatformProfile
) -> list[OwnModeConflict]:
    """The charger's own "enabled" switch when it is off: the charger cannot start while it is, and
    SpotNav neither uses nor writes it. One that is unreadable or disabled in the registry is not
    reported (nothing is claimed from silence).
    """
    if not profile.enable_switch_keys:
        return []
    entry = EntityMatcher(hass, entries, profile).first("switch", profile.enable_switch_keys)
    if entry is None or entry.disabled_by is not None:
        return []
    text = state_text(hass, entry.entity_id)
    if text is None or text not in profile.enable_switch_off_values:
        return []
    return [OwnModeConflict(entry.entity_id, DISABLED_LABEL, text)]


def charger_entries(hass: HomeAssistant, charge_control: str, profile: PlatformProfile) -> list[er.RegistryEntry]:
    """The registry entries of the charger's device that belong to the profile's integration, found
    from its charge-control entity; empty when the entity or its device is unknown.
    """
    registry = er.async_get(hass)
    control = registry.async_get(charge_control)
    if control is None or control.device_id is None:
        return []
    return [
        candidate
        for candidate in er.async_entries_for_device(registry, control.device_id)
        if candidate.platform == profile.platform
    ]


def charger_is_disabled(hass: HomeAssistant, charge_control: str, platform: str | None) -> bool:
    """Whether the charger's own enable switch is off (see `disabled_switches`)."""
    profile = profile_for(platform)
    if profile is None or not profile.enable_switch_keys:
        return False
    return bool(disabled_switches(hass, charger_entries(hass, charge_control, profile), profile))


def _rule_conflicts(hass: HomeAssistant, matcher: EntityMatcher, rule: OwnModeRule) -> list[OwnModeConflict]:
    entry = next(
        (
            candidate
            for candidate in matcher.find(rule.domain, rule.keys)
            if not any(token in (candidate.unique_id or "").lower() for token in rule.exclude)
        ),
        None,
    )
    if entry is None or entry.disabled_by is not None:
        return []
    text = state_text(hass, entry.entity_id)
    if text is None or text in rule.inactive_values:
        return []
    return [OwnModeConflict(entry.entity_id, rule.label, text)]


#: Option names that, on a select the platform is not known for, unambiguously start or stop a charge.
_GENERIC_START_OPTIONS: Final = ("charge", "start", "active", "fast", "always_on", "max_charge")
_GENERIC_STOP_OPTIONS: Final = (
    "stop",
    "stopped",
    "off",
    "paused",
    "disabled",
    "always_off",
    "dont_charge",
    "don't charge",
)


def path_primary_entity(path: dict[str, Any] | None) -> str | None:
    """The entity a stored control path is identified by (`charge_control`), or `None` (Easee)."""
    if not path:
        return None
    return path.get("entity_id") or path.get("start_entity_id") or None


def control_path_for_entity(
    hass: HomeAssistant,
    charge_control: str,
    *,
    detected_path: dict[str, Any] | None,
    charge_control_is_identity: bool = False,
) -> dict[str, Any] | None:
    """The control path a chosen charge-control entity means, or `None` when it cannot be told.

    The detected path is kept when the person kept the entity it was found for (an Easee path, which
    has no entity of its own, is kept when `charge_control_is_identity`). Otherwise the entity's
    domain decides: a switch is a switch, a select whose options name a start and a stop
    unambiguously is a select; a button alone or a sensor says nothing about how to start a charge.
    """
    if detected_path is not None:
        if detected_path.get("kind") == PATH_EASEE:
            return detected_path if charge_control_is_identity else None
        if path_primary_entity(detected_path) == charge_control:
            return detected_path
    domain = charge_control.partition(".")[0]
    if domain == "switch":
        return {"kind": PATH_SWITCH, "entity_id": charge_control, "inverted": False}
    if domain == "select":
        state = hass.states.get(charge_control)
        options = [str(o) for o in (state.attributes.get("options") or [])] if state is not None else []
        start = option_for(options, _GENERIC_START_OPTIONS)
        stop = option_for(options, _GENERIC_STOP_OPTIONS)
        if start is not None and stop is not None:
            return {"kind": PATH_SELECT, "entity_id": charge_control, "start_option": start, "stop_option": stop}
    return None
