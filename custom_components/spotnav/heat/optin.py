"""The heat pump's control entities that ship disabled, and enabling them on the person's confirm (the heat side of
`battery/optin.py`, the same pattern).

`nibe_heatpump` creates every coil entity disabled in the entity registry, and `thermiagenesis` every number and
sensor (it then does not even poll their registers). When the person turns "Styr värmen" on,
the card lists the chosen path's disabled ones and asks; only on that confirm does `async_enable` clear `disabled_by`
for exactly those entities and reload the integration's config entry once (Home Assistant would do it itself 30 s
later). The check then runs as usual. Turning control off leaves them enabled. Nothing here runs without the confirm,
and never over a webhook.

`ENABLE_KEYS` is, per adapter brand and control path, the entities as `(domain, unique-id key)`: what the path writes
(the keys of the adapter's capabilities; a test keeps the two together) and the calculated supply the check reads.
They are looked for in the config entry of the chosen control entity, on its device first.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from typing import Any, Final

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .adapters import Match, adapters_for
from .config import PATH_OFFSET, PATH_SG_READY_API, HeatConfig

_LOGGER = logging.getLogger(__name__)

#: Per `(adapter brand, control path)`, the entities the path needs enabled: `(domain, key)`.
ENABLE_KEYS: Final[Mapping[tuple[str, str], tuple[tuple[str, str], ...]]] = {
    ("nibe_s", PATH_SG_READY_API): (
        ("switch", "43033"),
        ("number", "46009"),
        # Read by SpotNav: the calculated supply, what the check sees the block on.
        ("sensor", "31018"),
    ),
    ("nibe_s", PATH_OFFSET): (("number", "40031"), ("sensor", "31018")),
    ("nibe_f", PATH_OFFSET): (("number", "47011"), ("sensor", "43009")),
    # The comfort wheel, the calculated supply the check judges it by, and the demand in progress (the check waits
    # for heating).
    ("thermia_genesis", PATH_OFFSET): (
        ("number", "holding_comfort_wheel_setting"),
        ("sensor", "input_system_supply_line_calculated_set_point"),
        ("sensor", "input_first_prioritised_demand"),
    ),
}

#: The brands whose unique ids end in `_<key>` (`adapters._under`); the others' in `-<key>` (`adapters._dash`).
_UNDERSCORE_BRANDS: Final = frozenset({"thermia_genesis"})

#: The reasons an entity is disabled that a person's confirm may lift (not the config entry's or Home Assistant's).
_LIFTABLE: Final = (er.RegistryEntryDisabler.INTEGRATION, er.RegistryEntryDisabler.USER)


def _match(domain: str, key: str, brand: str) -> Match:
    """`<prefix>-<key>` as `adapters._dash`, or `<prefix>_<key>` as `adapters._under` for `_UNDERSCORE_BRANDS`."""
    separator = "_" if brand in _UNDERSCORE_BRANDS else "-"
    return Match((domain,), re.compile(rf"{separator}{re.escape(key.lower())}$"))


def disabled_control_entities(hass: HomeAssistant, config: HeatConfig) -> tuple[str | None, list[er.RegistryEntry]]:
    """The config entry of the chosen control entity and the path's entities in it that are disabled (and may be
    enabled), in the order of `ENABLE_KEYS`; `(None, [])` when the path has none, no control entity is chosen or
    nothing is disabled. Registry reads only."""
    registry = er.async_get(hass)
    control = registry.async_get(config.control_entity) if config.control_entity else None
    if control is None or control.config_entry_id is None:
        return None, []
    brand = None
    for adapter in adapters_for(control.platform):
        keys = ENABLE_KEYS.get((adapter.brand, config.control_path), ())
        if any(_match(domain, key, adapter.brand).matches(control.domain, str(control.unique_id or "")) for domain, key in keys):
            brand = adapter.brand
            break
    if brand is None:
        return None, []
    entries = [
        entry
        for entry in er.async_entries_for_config_entry(registry, control.config_entry_id)
        if entry.platform == control.platform
    ]
    found: list[er.RegistryEntry] = []
    for domain, key in ENABLE_KEYS[(brand, config.control_path)]:
        match = _match(domain, key, brand)
        candidates = sorted(
            (entry for entry in entries if match.matches(entry.domain, str(entry.unique_id or ""))),
            key=lambda entry: entry.entity_id,
        )
        own = [entry for entry in candidates if control.device_id is not None and entry.device_id == control.device_id]
        chosen = own[0] if own else (candidates[0] if len(candidates) == 1 else None)
        if chosen is not None and chosen.disabled_by in _LIFTABLE:
            found.append(chosen)
    return (control.config_entry_id, found) if found else (None, [])


def describe(hass: HomeAssistant, config: HeatConfig) -> dict[str, Any] | None:
    """What the card asks about: the integration and the disabled entities, `None` when there are none."""
    entry_id, entities = disabled_control_entities(hass, config)
    if entry_id is None or not entities:
        return None
    entry = hass.config_entries.async_get_entry(entry_id)
    return {
        "integration": (entry.title if entry is not None and entry.title else entities[0].platform),
        "entities": [
            {"entity_id": entity.entity_id, "name": entity.name or entity.original_name or entity.entity_id}
            for entity in entities
        ],
    }


async def async_enable(hass: HomeAssistant, config: HeatConfig) -> list[str]:
    """Enable exactly the disabled entities `disabled_control_entities` finds, then reload their config entry once
    (only when something was enabled). Answers the entity ids enabled. The caller has the person's confirm."""
    entry_id, entities = disabled_control_entities(hass, config)
    if entry_id is None or not entities:
        return []
    registry = er.async_get(hass)
    for entity in entities:
        registry.async_update_entity(entity.entity_id, disabled_by=None)
    _LOGGER.info("SpotNav heat control: enabled %s, reloading the integration", [e.entity_id for e in entities])
    await hass.config_entries.async_reload(entry_id)
    return [entity.entity_id for entity in entities]
