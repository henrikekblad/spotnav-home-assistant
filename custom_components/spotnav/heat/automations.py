"""The household's own automations that steer what the heat settings name (plan `plans/v3_heat_card.md`, the owner's
note of 2026-11-16, and `plans/v3_heating_plan.md` step 2): the heat pump's relay and the zones' thermostats.

What such an automation does cannot be understood, only noticed: an enabled automation that references a chosen
relay or zone (Home Assistant's own list of the entities an automation references, as its search of related items
shows) is named once in the heat card ("Automationen '…' styr reläet: stäng av den när SpotNav ska styra värmen").
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


def steering_automations(hass: HomeAssistant, entity_ids: Iterable[str]) -> list[SteeringAutomation]:
    """The enabled automations that reference any of `entity_ids` (see the module), in name order."""
    wanted = [entity_id for entity_id in dict.fromkeys(entity_ids) if entity_id]
    if not wanted or "automation" not in hass.config.components:
        return []
    try:
        from homeassistant.components.automation import automations_with_entity  # noqa: PLC0415 - only when loaded
    except ImportError:  # pragma: no cover - automation is part of the core
        return []
    found: dict[str, list[str]] = {}
    for entity_id in wanted:
        try:
            automations = automations_with_entity(hass, entity_id)
        except Exception:  # noqa: BLE001 - an automation component not ready yet: nothing is noticed this time
            _LOGGER.debug("SpotNav heat: the automations could not be read", exc_info=True)
            return []
        for automation in automations:
            found.setdefault(automation, []).append(entity_id)
    out: list[SteeringAutomation] = []
    for automation, targets in found.items():
        state = hass.states.get(automation)
        if state is None or state.state != "on":
            continue
        entity = _automation_entity(hass, automation)
        rule = None
        if entity is not None and getattr(entity, "referenced_blueprint", None) == BLUEPRINT_PATH:
            rule = blueprint_rule(getattr(entity, "raw_config", None))
        out.append(SteeringAutomation(automation, state.name, tuple(targets), rule))
    return sorted(out, key=lambda item: (item.name.casefold(), item.entity_id))
