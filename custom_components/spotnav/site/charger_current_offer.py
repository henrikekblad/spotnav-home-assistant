"""A charger's own measured-current sensors, as its platform profile names them, offered for the site wiring.

The profile knows where a charger reports the current it draws (`PlatformProfile.current_sensor_keys`, three
sensors, one per phase; or Easee's one `current` sensor carrying the phases as attributes). Many integrations
ship those sensors disabled (Peblar's `current_phase_1..3`), so discovery, which reads states, found nothing
and the site's wiring stayed empty. Here they are found in the entity registry, disabled ones included, the
way site detection finds a meter's (`site_detection.py`):

* `own_current_sensors`: the sensors on the charger's device, what to read from them, and which of them the
  integration disabled. A sensor a *person* disabled makes no offer at all: it is never enabled behind their
  back, and a source that cannot read is no help.
* `charger_current_offers`: one offer per member charger of a site whose wiring has no measured current,
  shown beside the detected meters with the same **Use**; applying one (`apply_charger_current_offer`)
  enables the sensors the integration disabled and writes the source into that charger's wiring.
* `sensors_text`: how the card names them ("Current phase 1–3").
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Final

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from ..const import CONF_CHARGER_ENTRY_IDS, CONF_MEASURED_CURRENT_SOURCE, CONF_PHASE_WIRING
from ..execution.charger_entities import EntityMatcher
from ..execution.charger_profiles import profile_for
from ..planning.grid_voltage import stored_voltage_between_phases_v
from .measurement_source import PhaseMeasurementSource, source_to_dict
from .site_capacity import PHASES, PhaseName

#: The prefix of an offer's id, followed by the charger's entry id (`apply_detection` takes it).
OFFER_PREFIX: Final = "charger_current:"
#: Network voltage between phases that selects a profile's IT attribute names (Norway's 230 V IT grid).
_IT_VOLTAGE_V: Final = 230
_TRAILING_NUMBER: Final = re.compile(r"^(.*?)(\d+)$")


@dataclass(frozen=True, slots=True)
class OwnCurrentSensors:
    """The current sensors a charger's own integration offers, per its platform profile."""

    #: The profile's name for the integration ("Peblar", "Easee").
    integration: str
    entity_ids: tuple[str, ...]
    #: The sensors' own names, without the device's ("Current phase 1").
    names: tuple[str, ...]
    #: The sensors their integration ships disabled: enabled when the offer is used.
    disabled_entity_ids: tuple[str, ...]
    source: PhaseMeasurementSource

    @property
    def sensors_text(self) -> str:
        return sensors_text(self.names)


@dataclass(frozen=True, slots=True)
class ChargerCurrentOffer:
    """One member charger's own sensors, offered for its wiring in the site editor."""

    candidate_id: str
    charger_entry_id: str
    charger_name: str
    sensors: OwnCurrentSensors

    @property
    def disabled_entity_ids(self) -> tuple[str, ...]:
        return self.sensors.disabled_entity_ids


def sensors_text(names: tuple[str, ...]) -> str:
    """The sensors' names for a sentence: "Current phase 1–3" when they differ only by a trailing running
    number, else the names joined by commas."""
    if len(names) > 1:
        parts = [_TRAILING_NUMBER.match(name) for name in names]
        if all(parts):
            prefixes = {part.group(1) for part in parts if part}
            numbers = [int(part.group(2)) for part in parts if part]
            if len(prefixes) == 1 and numbers == list(range(numbers[0], numbers[0] + len(numbers))):
                return f"{prefixes.pop()}{numbers[0]}–{numbers[-1]}"
    return ", ".join(names)


def _name(hass: HomeAssistant, entry: er.RegistryEntry) -> str:
    if entry.name or entry.original_name:
        return str(entry.name or entry.original_name)
    state = hass.states.get(entry.entity_id)
    return state.name if state is not None else entry.entity_id


def _usable(entry: er.RegistryEntry) -> bool:
    """Enabled, or disabled by its integration (which a Use may undo); never one a person disabled."""
    return entry.disabled_by in (None, er.RegistryEntryDisabler.INTEGRATION)


def _phase_map(names: tuple[str, ...], phases: int, phase: str | None) -> dict[PhaseName, str]:
    """A one-phase charger on a chosen grid phase reads its first sensor (or attribute) there; any other
    charger reads all three, as `profile_attribute_source` does."""
    if phases == 1 and phase in PHASES:
        return {phase: names[0]}  # type: ignore[dict-item]
    return dict(zip(PHASES, names))


def own_current_sensors(
    hass: HomeAssistant,
    device_id: str | None,
    *,
    phases: int = 3,
    phase: str | None = None,
    voltage_between_phases_v: float | None = None,
) -> OwnCurrentSensors | None:
    """The measured-current sensors the charger's platform profile names on `device_id`, disabled ones
    included, or `None` when its profile names none, they are not all there, or a person disabled one."""
    if not device_id:
        return None
    registry = er.async_get(hass)
    entries = er.async_entries_for_device(registry, device_id, include_disabled_entities=True)
    # Imported here: the flows import this package's modules.
    from ..flows.charger_detection import _is_current_sensor

    for platform in dict.fromkeys(entry.platform for entry in entries):
        profile = profile_for(platform)
        if profile is None:
            continue
        matcher = EntityMatcher(hass, [entry for entry in entries if entry.platform == platform], profile)
        attribute_names = (
            profile.current_attributes_it
            if voltage_between_phases_v == _IT_VOLTAGE_V and profile.current_attributes_it
            else profile.current_attributes
        )
        if profile.current_attribute_sensor_key and len(attribute_names) == len(PHASES):
            found = matcher.first("sensor", (profile.current_attribute_sensor_key,))
            if found is None:
                continue
            if not _usable(found):
                return None
            if phases == 1 and voltage_between_phases_v == _IT_VOLTAGE_V:
                return None  # a one-phase charger on an IT network uses two terminals: not mapped here
            return OwnCurrentSensors(
                integration=profile.name,
                entity_ids=(found.entity_id,),
                names=(_name(hass, found),),
                disabled_entity_ids=() if found.disabled_by is None else (found.entity_id,),
                source=PhaseMeasurementSource(
                    kind="attributes",
                    entity_id=found.entity_id,
                    attributes=_phase_map(tuple(attribute_names), phases, phase),
                    attribute_unit_override="A",
                ),
            )
        if len(profile.current_sensor_keys) != len(PHASES):
            continue
        sensors = [matcher.first("sensor", (key,)) for key in profile.current_sensor_keys]
        if any(sensor is None for sensor in sensors):
            continue
        chosen = [sensor for sensor in sensors if sensor is not None]
        if len({sensor.entity_id for sensor in chosen}) != len(PHASES):
            continue
        if not all(_is_current_sensor(hass, sensor) for sensor in chosen):
            continue
        if not all(_usable(sensor) for sensor in chosen):
            return None
        entity_ids = tuple(sensor.entity_id for sensor in chosen)
        return OwnCurrentSensors(
            integration=profile.name,
            entity_ids=entity_ids,
            names=tuple(_name(hass, sensor) for sensor in chosen),
            disabled_entity_ids=tuple(sensor.entity_id for sensor in chosen if sensor.disabled_by is not None),
            source=PhaseMeasurementSource(kind="separate_entities", entity_ids=_phase_map(entity_ids, phases, phase)),
        )
    return None


def charger_own_current_sensors(hass: HomeAssistant, charger_entry_id: str) -> OwnCurrentSensors | None:
    """`own_current_sensors` for a charger, read with its site's wiring (its phases, the network)."""
    # Imported here: the flows import this package's modules.
    from ..flows.measured_source import charger_device_id
    from ..planning.first_run import site_for_charger

    site = site_for_charger(hass, charger_entry_id)
    wiring = ((site.data.get(CONF_PHASE_WIRING) or {}).get(charger_entry_id) or {}) if site is not None else {}
    return own_current_sensors(
        hass,
        charger_device_id(hass, charger_entry_id),
        phases=1 if wiring.get("phases") == 1 else 3,
        phase=wiring.get("phase"),
        voltage_between_phases_v=None if site is None else stored_voltage_between_phases_v(site.data),
    )


def charger_current_offers(hass: HomeAssistant, site: ConfigEntry) -> list[ChargerCurrentOffer]:
    """An offer for every member charger whose wiring reads no measured current and whose own integration
    has the sensors for it."""
    wiring_by_charger = site.data.get(CONF_PHASE_WIRING) or {}
    offers: list[ChargerCurrentOffer] = []
    for charger_entry_id in site.data.get(CONF_CHARGER_ENTRY_IDS) or []:
        wiring = wiring_by_charger.get(charger_entry_id) or {}
        if not isinstance(wiring, dict) or wiring.get(CONF_MEASURED_CURRENT_SOURCE):
            continue
        sensors = charger_own_current_sensors(hass, charger_entry_id)
        if sensors is None:
            continue
        charger = hass.config_entries.async_get_entry(charger_entry_id)
        offers.append(
            ChargerCurrentOffer(
                candidate_id=f"{OFFER_PREFIX}{charger_entry_id}",
                charger_entry_id=charger_entry_id,
                charger_name=charger.title if charger is not None else charger_entry_id,
                sensors=sensors,
            )
        )
    return offers


def apply_charger_current_offer(data: dict[str, Any], offer: ChargerCurrentOffer) -> dict[str, Any]:
    """The site's data with the offer's source in that charger's wiring; a source a person had removed on
    purpose (`MEASURED_SOURCE_DECLINED`) is now chosen again."""
    from .site_join import MEASURED_SOURCE_DECLINED

    wiring_by_charger = dict(data.get(CONF_PHASE_WIRING) or {})
    wiring = {
        key: value
        for key, value in (wiring_by_charger.get(offer.charger_entry_id) or {}).items()
        if key != MEASURED_SOURCE_DECLINED
    }
    wiring[CONF_MEASURED_CURRENT_SOURCE] = source_to_dict(offer.sensors.source)
    wiring_by_charger[offer.charger_entry_id] = wiring
    return {**data, CONF_PHASE_WIRING: wiring_by_charger}
