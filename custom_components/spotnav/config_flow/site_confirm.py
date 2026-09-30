"""The site confirmation step's human-readable summary of what detection found."""

from __future__ import annotations

from typing import Any

from homeassistant.helpers import entity_registry as er

from ..const import MEASUREMENT_MODE_DERIVED
from ..site.site_detection import BatteryCandidate, MeterCandidate
from ..vehicles.choices import flow_language
from ..vehicles.discovery import DiscoveryCandidate
from .labels import PHASES

_TEXT: dict[str, dict[str, str]] = {
    "en": {
        "power_current_calculated": "Measurement: power per phase, current calculated",
        "power_current": "Measurement: power and current per phase",
        "current": "Measurement: current per phase",
        "current_entity": "Measurement: one entity with the current per phase",
        "voltage": "Voltage",
        "join_wiring": "Phase wiring: 3 phases, measured current from {source}",
        "join_unknown": "SpotNav cannot tell how this charger is wired, so it is not added automatically. Add it from the site's settings.",
        "reactive_found": "Reactive power: found",
        "reactive_missing": "Reactive power: not found (current estimated with power factor 0.9)",
        "battery": "House battery: {name} (charging positive)",
        "no_battery": "House battery: none",
        "charger": "Charger {name}: {phases} phases, measured current from {source}",
        "label_power": "{name} – power per phase (current calculated)",
        "label_power_current": "{name} – power and current per phase",
        "label_current": "{name} – current per phase",
    },
    "sv": {
        "power_current_calculated": "Mätning: effekt per fas, ström beräknas",
        "power_current": "Mätning: effekt och ström per fas",
        "current": "Mätning: ström per fas",
        "current_entity": "Mätning: en entitet med strömmen per fas",
        "voltage": "Sp\u00e4nning",
        "join_wiring": "Fasinkoppling: 3 faser, uppm\u00e4tt str\u00f6m fr\u00e5n {source}",
        "join_unknown": "SpotNav kan inte avg\u00f6ra hur laddaren \u00e4r inkopplad, s\u00e5 den l\u00e4ggs inte till automatiskt. L\u00e4gg till den fr\u00e5n anl\u00e4ggningens inst\u00e4llningar.",
        "reactive_found": "Reaktiv effekt: hittad",
        "reactive_missing": "Reaktiv effekt: saknas (strömmen uppskattas med effektfaktor 0,9)",
        "battery": "Hemmabatteri: {name} (laddning positiv)",
        "no_battery": "Hemmabatteri: inget",
        "charger": "Laddare {name}: {phases} faser, uppmätt ström från {source}",
        "label_power": "{name} – effekt per fas (ström beräknas)",
        "label_power_current": "{name} – effekt och ström per fas",
        "label_current": "{name} – ström per fas",
    },
}


def entity_name(hass, entity_id: str) -> str:
    """Friendly name: the state's, else the registry's (a disabled entity has no state yet), else the id."""
    state = hass.states.get(entity_id)
    if state is not None:
        return state.name
    entry = er.async_get(hass).async_get(entity_id)
    return (entry.name or entry.original_name) if entry and (entry.name or entry.original_name) else entity_id


def candidate_label(hass, candidate: MeterCandidate) -> str:
    """A detected meter as a person would say it, never with integration or mode codes."""
    text = _TEXT[flow_language(hass)]
    if candidate.mode == MEASUREMENT_MODE_DERIVED:
        has_current = any("current" in (values or {}) for values in (candidate.derived_entities or {}).values())
        key = "label_power_current" if has_current else "label_power"
    else:
        key = "label_current"
    return text[key].format(name=candidate.title)


def source_name(hass, mapping: Any) -> str:
    if mapping.kind == "attributes" and mapping.entity_id:
        return entity_name(hass, mapping.entity_id)
    entity_ids = sorted((mapping.entity_ids or {}).values())
    return entity_name(hass, entity_ids[0]) if entity_ids else ""


def site_confirm_summary(
    hass,
    *,
    candidate: MeterCandidate,
    battery: BatteryCandidate | None,
    chargers: list[tuple[str, int, DiscoveryCandidate]],
) -> str:
    """One line per item found. `chargers` is `(charger name, phases, its measured-current candidate)`."""
    text = _TEXT[flow_language(hass)]
    lines: list[str] = []
    derived = candidate.derived_entities or {}
    if candidate.mode == MEASUREMENT_MODE_DERIVED:
        has_current = any("current" in (derived.get(phase) or {}) for phase in PHASES)
        lines.append(text["power_current" if has_current else "power_current_calculated"])
        per_phase = ", ".join(
            f"{phase}: {entity_name(hass, (derived.get(phase) or {}).get('power', ''))}"
            for phase in PHASES
        )
        lines[-1] += f" ({per_phase})"
        lines.append(
            f"{text['voltage']}: "
            + ", ".join(
                f"{phase} {entity_name(hass, (derived.get(phase) or {}).get('voltage', ''))}"
                for phase in PHASES
            )
        )
        has_reactive = all("reactive_power" in (derived.get(phase) or {}) for phase in PHASES)
        has_other = any(
            sub in (derived.get(phase) or {}) for phase in PHASES for sub in ("current", "apparent_power")
        )
        if has_reactive:
            lines.append(text["reactive_found"])
        elif not has_other:
            lines.append(text["reactive_missing"])
    elif candidate.direct_entities:
        per_phase = ", ".join(
            f"{phase}: {entity_name(hass, (candidate.direct_entities or {}).get(phase, ''))}" for phase in PHASES
        )
        lines.append(f"{text['current']} ({per_phase})")
    else:
        name = source_name(hass, candidate.current_source) if candidate.current_source else ""
        lines.append(f"{text['current_entity']} ({name})")
    lines.append(
        text["battery"].format(name=entity_name(hass, battery.entity_id)) if battery else text["no_battery"]
    )
    for name, phases, measured in chargers:
        lines.append(
            text["charger"].format(name=name, phases=phases, source=source_name(hass, measured.mapping))
        )
    return "\n".join(lines)


def site_join_summary(hass, measured: DiscoveryCandidate | None) -> str:
    """The wiring a charger would get when added to the site, or why it cannot be added automatically."""
    text = _TEXT[flow_language(hass)]
    if measured is None:
        return text["join_unknown"]
    return text["join_wiring"].format(source=source_name(hass, measured.mapping))
