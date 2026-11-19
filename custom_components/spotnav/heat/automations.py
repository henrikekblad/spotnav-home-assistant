"""The household's own automations that steer what the heat settings name (plan `plans/v3_heat_card.md`, the owner's
note of 2026-11-16, and `plans/v3_heating_plan.md` step 2): the heat pump's relay and the zones' thermostats.

What such an automation does cannot be understood, only noticed: an enabled automation whose actions reference a
chosen relay or zone (Home Assistant's own list of the entities its actions reference; a trigger or a condition on it
only reads it), or a script or a scene that references it, is named once in the heat card ("Automationen '…' styr
reläet: stäng av den när SpotNav ska styra värmen").
One exception is understood: SpotNav's own relay blueprint (`relay_by_price.yaml`), whose inputs say when it closes
the relay (`heat/relay_schedule.blueprint_rule`), so the forecast can count its lowered hours ahead.

Read only, on the event loop and cheap: the automation component's entities as they are now. Nothing is changed:
SpotNav never turns an automation off.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from homeassistant.core import HomeAssistant

from .relay_schedule import BLUEPRINT_PATH, BlueprintRule, blueprint_rule

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SteeringAutomation:
    """An enabled automation that references chosen heat entities."""

    entity_id: str
    name: str
    #: The chosen entities it references (the relay first, then the zones, in the settings' order).
    targets: tuple[str, ...]
    #: SpotNav's relay blueprint's rule, where it is one.
    rule: BlueprintRule | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "entity_id": self.entity_id,
            "name": self.name,
            "targets": list(self.targets),
            "blueprint": None if self.rule is None else self.rule.as_dict(),
        }


def _automation_entity(hass: HomeAssistant, entity_id: str) -> Any:
    try:
        from homeassistant.components.automation import DATA_COMPONENT  # noqa: PLC0415 - only when loaded
    except ImportError:  # pragma: no cover - automation is part of the core
        return None
    component = hass.data.get(DATA_COMPONENT)
    return None if component is None else component.get_entity(entity_id)


def _drives(entity: Any, entity_id: str) -> bool:
    """Whether the automation's actions reference `entity_id` (a trigger or a condition on it only reads it). An
    automation that cannot be looked into is taken to drive it."""
    script = getattr(entity, "action_script", None)
    find = getattr(type(script), "_find_referenced_entities", None)
    sequence = getattr(script, "sequence", None)
    if find is None or not isinstance(sequence, list):
        return True
    # Read from the actions themselves: the automation adds its triggers' and conditions' entities into the set its
    # action script keeps.
    referenced: set[str] = set()
    try:
        find(referenced, sequence)
    except Exception:  # noqa: BLE001 - not understood: taken to drive it
        _LOGGER.debug("drives: ignored failure (not understood: taken to drive it)", exc_info=True)
        return True
    return entity_id in referenced


def _scripts_and_scenes(hass: HomeAssistant, entity_id: str) -> list[str]:
    """The scripts and scenes that reference `entity_id` (each of them sets what it references)."""
    found: list[str] = []
    if "script" in hass.config.components:
        from homeassistant.components.script import scripts_with_entity  # noqa: PLC0415 - only when loaded

        found += scripts_with_entity(hass, entity_id)
    if "scene" in hass.config.components:
        from homeassistant.components.homeassistant.scene import scenes_with_entity  # noqa: PLC0415

        found += scenes_with_entity(hass, entity_id)
    return found


def steering_automations(hass: HomeAssistant, entity_ids: Iterable[str]) -> list[SteeringAutomation]:
    """The enabled automations whose actions reference any of `entity_ids`, and the scripts and scenes that do (see
    the module), in name order. An automation that only triggers on one or tests it in a condition reads it only."""
    wanted = [entity_id for entity_id in dict.fromkeys(entity_ids) if entity_id]
    if not wanted:
        return []
    found: dict[str, list[str]] = {}
    for entity_id in wanted:
        try:
            automations: list[str] = []
            if "automation" in hass.config.components:
                from homeassistant.components.automation import automations_with_entity  # noqa: PLC0415

                automations = [
                    automation
                    for automation in automations_with_entity(hass, entity_id)
                    if _drives(_automation_entity(hass, automation), entity_id)
                ]
            others = _scripts_and_scenes(hass, entity_id)
        except Exception:  # noqa: BLE001 - a component not ready yet: nothing is noticed this time
            _LOGGER.debug("SpotNav heat: the automations could not be read", exc_info=True)
            return []
        for automation in automations + others:
            found.setdefault(automation, []).append(entity_id)
    out: list[SteeringAutomation] = []
    for automation, targets in found.items():
        state = hass.states.get(automation)
        if automation.startswith("automation."):
            if state is None or state.state != "on":
                continue
            entity = _automation_entity(hass, automation)
            rule = None
            if entity is not None and getattr(entity, "referenced_blueprint", None) == BLUEPRINT_PATH:
                rule = blueprint_rule(getattr(entity, "raw_config", None))
            out.append(SteeringAutomation(automation, state.name, tuple(targets), rule))
        else:
            out.append(SteeringAutomation(automation, automation if state is None else state.name, tuple(targets)))
    return sorted(out, key=lambda item: (item.name.casefold(), item.entity_id))
