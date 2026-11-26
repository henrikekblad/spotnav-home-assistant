"""The site confirmation step's human-readable summary of what detection found."""

from __future__ import annotations

from typing import Any

from homeassistant.helpers import entity_registry as er

from ..const import MEASUREMENT_MODE_DERIVED
from ..site.site_detection import BatteryCandidate, MeterCandidate, is_single_phase_candidate
from ..vehicles.choices import flow_language
from ..texts import table
from ..vehicles.discovery import DiscoveryCandidate
from .labels import PHASES

_MARKDOWN_SPECIALS = set("\\`*_[]<>#|~&")


def md_escape(value: object) -> str:
    """Escape a user-controlled name so that it renders literally in a Markdown description."""
    return "".join(f"\\{ch}" if ch in _MARKDOWN_SPECIALS else ch for ch in str(value))


def entity_name(hass, entity_id: str) -> str:
    """Friendly name: the state's, else the registry's (a disabled entity has no state yet), else the id."""
    state = hass.states.get(entity_id)
    if state is not None:
        return state.name
    entry = er.async_get(hass).async_get(entity_id)
    return (entry.name or entry.original_name) if entry and (entry.name or entry.original_name) else entity_id


def candidate_label(hass, candidate: MeterCandidate) -> str:
    """A detected meter as a person would say it, never with integration or mode codes."""
    text = table(flow_language(hass), "site_confirm")
    if candidate.mode == MEASUREMENT_MODE_DERIVED:
        has_current = any("current" in (values or {}) for values in (candidate.derived_entities or {}).values())
        key = "label_power_current" if has_current else "label_power"
    else:
        key = "label_current"
    label = text[key].format(name=candidate.title)
    # A meter measured on L1 alone makes the site single-phase: the offer says so.
    return text["label_single_phase"].format(label=label) if is_single_phase_candidate(candidate) else label


def source_name(hass, mapping: Any) -> str:
    if mapping.kind in ("attributes", "power") and mapping.entity_id:
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
    """Markdown with one block per item found, blocks separated by a blank line.

    `chargers` is `(charger name, phases, its measured-current candidate)`. Every name that comes from
    the user's entities or devices is escaped, so a `*` or `_` in it cannot break the formatting.
    """
    text = table(flow_language(hass), "site_confirm")
    blocks: list[str] = []
    derived = candidate.derived_entities or {}
    single_phase = is_single_phase_candidate(candidate)
    shown_phases = ("L1",) if single_phase else PHASES
    if candidate.mode == MEASUREMENT_MODE_DERIVED:
        has_current = any("current" in (derived.get(phase) or {}) for phase in PHASES)
        heading = text["power_current" if has_current else "power_current_calculated"]
        rows = []
        for phase in shown_phases:
            subs = derived.get(phase) or {}
            names = [
                md_escape(entity_name(hass, subs[sub])) for sub in ("power", "current", "voltage") if subs.get(sub)
            ]
            rows.append(f"- **{phase}:** " + " · ".join(names))
        blocks.append(f"**{text['meter']}** – {heading}\n\n" + "\n".join(rows))
        has_reactive = all("reactive_power" in (derived.get(phase) or {}) for phase in shown_phases)
        has_other = any(
            sub in (derived.get(phase) or {}) for phase in shown_phases for sub in ("current", "apparent_power")
        )
        if has_reactive:
            blocks.append(text["reactive_found"])
        elif not has_other:
            blocks.append(text["reactive_missing"])
    elif candidate.direct_entities:
        rows = [
            f"- **{phase}:** {md_escape(entity_name(hass, (candidate.direct_entities or {}).get(phase, '')))}"
            for phase in shown_phases
        ]
        blocks.append(f"**{text['meter']}** – {text['current']}\n\n" + "\n".join(rows))
    else:
        name = source_name(hass, candidate.current_source) if candidate.current_source else ""
        blocks.append(f"**{text['meter']}** – {text['current_entity']}\n\n- {md_escape(name)}")
    if single_phase:
        blocks.insert(1, text["single_phase"])
    blocks.append(
        text["battery"].format(name=md_escape(entity_name(hass, battery.entity_id)))
        if battery
        else text["no_battery"]
    )
    for name, phases, measured in chargers:
        blocks.append(
            text["charger_single_phase" if single_phase else "charger"].format(
                name=md_escape(name), phases=phases, source=md_escape(source_name(hass, measured.mapping))
            )
        )
    return "\n\n".join(blocks)


def charger_found_summary(
    hass, *, charge_control: str, current_number: str | None, energy_meter: str | None
) -> str:
    """Markdown list of what was found for an OCPP charger: control, how the current is set, energy meter."""
    text = table(flow_language(hass), "site_confirm")
    current = (
        text["ocpp_current_number"].format(name=md_escape(current_number))
        if current_number
        else text["ocpp_current_config"]
    )
    energy = text["ocpp_energy"].format(
        name=md_escape(energy_meter) if energy_meter else text["ocpp_energy_none"]
    )
    return "\n".join(
        f"- {line}" for line in (text["ocpp_control"].format(name=md_escape(charge_control)), current, energy)
    )


def site_join_summary(
    hass, measured: DiscoveryCandidate | None, *, single_phase_site: bool = False, dc_charger: bool = False
) -> str:
    """The wiring a charger would get when added to the site, or why it cannot be added automatically: its wiring
    cannot be told, or (`dc_charger`) it is a DC charger and the site is a single-phase one."""
    text = table(flow_language(hass), "site_confirm")
    if dc_charger:
        return text["join_dc_single_phase"]
    if measured is None:
        return text["join_unknown"]
    key = "join_wiring_single_phase" if single_phase_site else "join_wiring"
    return text[key].format(source=md_escape(source_name(hass, measured.mapping)))
