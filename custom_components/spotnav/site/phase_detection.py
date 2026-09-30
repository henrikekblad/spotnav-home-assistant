"""Advisory phase-count detection for one charger's config entry.

A UI hint only: a three-phase charger can charge a vehicle on one phase, so
the result must never override a manual setting. Detection is scoped to the
device owning the charge-control entity; if that device cannot be resolved the
result is `unknown` rather than a guess that might read another charger.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er


PhaseSource = Literal[
    "explicit_metadata",
    "voltage_attributes",
    "current_attributes",
    "l1_only",
    "unknown",
]
PhaseConfidence = Literal["high", "medium", "low", "none"]

# Deliberately narrow: other spellings ("l1_voltage", "phase_a") could be
# unrelated attributes.
_PHASE_KEYS = ("l1", "l2", "l3")

# Explicit phase metadata: a plain `phases` attribute with an integer value.
# Rarely present; picked up if an integration or template sensor exposes it.
_EXPLICIT_PHASES_ATTRIBUTE = "phases"

_VOLTAGE_DEVICE_CLASS = "voltage"
_CURRENT_DEVICE_CLASS = "current"


@dataclass(frozen=True, slots=True)
class PhaseDetectionResult:
    """One charger's advisory phase-count result."""

    detected_phases: int | None
    source: PhaseSource
    confidence: PhaseConfidence

    def as_fields(self) -> dict[str, object]:
        """The `detected_phases`/`phase_detection` dashboard fields."""
        return {
            "detected_phases": self.detected_phases,
            "phase_detection": {"source": self.source, "confidence": self.confidence},
        }


UNKNOWN = PhaseDetectionResult(detected_phases=None, source="unknown", confidence="none")


def present_phase_keys(attributes: object) -> set[str]:
    """Which of L1/L2/L3 (any case) are present as attribute keys.

    Only key names matter, never values: a phase reporting 0 is still a phase.
    """
    if not isinstance(attributes, dict):
        return set()
    present: set[str] = set()
    for key in attributes:
        if not isinstance(key, str):
            continue
        normalized = key.strip().lower()
        if normalized in _PHASE_KEYS:
            present.add(normalized)
    return present


def explicit_phase_count(attributes: object) -> int | None:
    """A trustworthy explicit phase count, if `attributes` states one plainly."""
    if not isinstance(attributes, dict):
        return None
    value = attributes.get(_EXPLICIT_PHASES_ATTRIBUTE)
    # bool is an int subclass; exclude it.
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value in (1, 2, 3):
        return value
    return None


def _device_class_phase_keys(
    hass: HomeAssistant,
    entity_ids: list[str],
    device_class: str,
) -> set[str]:
    """Strongest phase-key evidence from a sensor of this class on the device.

    Entities with no state are skipped; `unavailable` states are still read
    since their attributes may still describe the wiring.
    """
    best_keys: set[str] = set()
    for entity_id in entity_ids:
        state = hass.states.get(entity_id)
        if state is None:
            continue
        if state.attributes.get("device_class") != device_class:
            continue
        keys = present_phase_keys(state.attributes)
        if keys >= {"l1", "l2", "l3"}:
            return keys
        if len(keys) > len(best_keys):
            best_keys = keys
    return best_keys


def async_detect_phases(hass: HomeAssistant, charge_control_entity_id: str) -> PhaseDetectionResult:
    """Best-effort phase count: explicit metadata, then voltage, then current
    evidence, then an L1-only fallback, else unknown.
    """
    entity_registry = er.async_get(hass)
    charge_control_entry = entity_registry.async_get(charge_control_entity_id)

    control_state = hass.states.get(charge_control_entity_id)
    explicit = explicit_phase_count(control_state.attributes if control_state else None)
    if explicit is not None:
        return PhaseDetectionResult(explicit, "explicit_metadata", "high")

    device_id = charge_control_entry.device_id if charge_control_entry else None
    if not device_id:
        # No device link: never fall back to a global scan (could read another
        # charger's sensors).
        return UNKNOWN

    device_entity_ids = [
        entry.entity_id for entry in er.async_entries_for_device(entity_registry, device_id)
    ]

    for entity_id in device_entity_ids:
        state = hass.states.get(entity_id)
        if state is None:
            continue
        explicit = explicit_phase_count(state.attributes)
        if explicit is not None:
            return PhaseDetectionResult(explicit, "explicit_metadata", "high")

    voltage_keys = _device_class_phase_keys(hass, device_entity_ids, _VOLTAGE_DEVICE_CLASS)
    if voltage_keys >= {"l1", "l2", "l3"}:
        return PhaseDetectionResult(3, "voltage_attributes", "high")

    current_keys = _device_class_phase_keys(hass, device_entity_ids, _CURRENT_DEVICE_CLASS)
    if current_keys >= {"l1", "l2", "l3"}:
        # Less direct than voltage: a disconnected phase can still report a
        # 0 A key on some integrations.
        return PhaseDetectionResult(3, "current_attributes", "medium")

    if voltage_keys == {"l1"} or current_keys == {"l1"}:
        return PhaseDetectionResult(1, "l1_only", "low")

    return UNKNOWN
