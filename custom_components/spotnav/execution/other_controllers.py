"""Other Home Assistant integrations that switch or limit a charger by themselves.

Two controllers on one charger fight every cycle: SpotNav stops it and the other starts it again. evcc
and openWB are profiles of their own (`charger_profiles.ROLE_EXTERNAL_CONTROLLER`). This catalogue holds
the price-driven and load-balancing integrations that run beside a charger, each verified in its source
at the commit named in its row:

* `ev_smart_charging` (jonasbkarlsson, `cb6cc76`) writes `switch`/`input_boolean.turn_on|turn_off` to the
  entity of its `charger_entity` option while its "Smart charging activated" switch is on, and starts the
  charge again when it should be on but the charging-state entity says it is not.
* `evse_load_balancer` (dirkgroenen, `374cf77`) sets the current limit of the device in its
  `charger_device` setting from the household load.
* `peaqev` (elden1337, `363c324`) starts, stops and limits a charger of its `chargertype` through that
  integration's services. Its `chargerid` is an integration-specific id, not an entity or device, so it
  cannot be matched to a SpotNav charger: it is a warning for the installation.

Tibber's smart charging is not here: the core `tibber` integration (2026.9.2: `binary_sensor`, `notify` and
`sensor` platforms only, and read-only `data-api-chargers-read` / `data-api-vehicles-read` scopes) only reads
a Tibber-linked charger or vehicle and exposes no switch, number or service that charges, so Home Assistant
cannot see the smart charging at all. It runs in Tibber's cloud, which commands the charger (for an Easee,
through Easee's cloud). The Easee charger setup says so instead (`flows/flow.py`, "cloud").

Left out because their source makes no service call on a charger: `nordpool_planner` (dala318,
`e2651e3`) and `peaqnext` (elden1337, `cd99ba6`) only publish sensors a person wires into automations.

A controller whose setting names this charger's entity (or device) is reported for the charger; one that
names a different charger is not; one that names none (peaqev) is reported for the installation. The
setting is read as the integration does, from the entry's options first, then its data.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

SCOPE_CHARGER: Final = "charger"
SCOPE_INSTALLATION: Final = "installation"


@dataclass(frozen=True, slots=True)
class ControllerSpec:
    """One controller integration: how its config entry names the charger it controls."""

    domain: str
    name: str
    # Setting keys whose value is the entity id of the switch the controller drives.
    entity_keys: tuple[str, ...] = ()
    # Setting keys whose value is the device id of the charger the controller drives.
    device_keys: tuple[str, ...] = ()
    # `unique_id` of the controller's own on/off switch (`{entry_id}` is filled in): while it reads
    # "off" the controller does nothing, so it is not reported.
    active_switch_unique_id: str | None = None


CONTROLLERS: Final[tuple[ControllerSpec, ...]] = (
    ControllerSpec(
        domain="ev_smart_charging",
        name="EV Smart Charging",
        entity_keys=("charger_entity",),
        active_switch_unique_id="{entry_id}.switch.smartchargingactivated",
    ),
    ControllerSpec(domain="evse_load_balancer", name="EVSE Load Balancer", device_keys=("charger_device",)),
    ControllerSpec(domain="peaqev", name="PeaqEV"),
)


@dataclass(frozen=True, slots=True)
class OtherController:
    """A controller found beside a charger."""

    domain: str
    name: str
    scope: str


def _setting(options: Mapping[str, object], data: Mapping[str, object], key: str) -> object | None:
    """A setting as the controllers read it: the options win over the data, even when empty."""
    if key in options:
        return options[key]
    return data.get(key)


def _is_switched_off(hass: HomeAssistant, registry: er.EntityRegistry, spec: ControllerSpec, entry_id: str) -> bool:
    if spec.active_switch_unique_id is None:
        return False
    entity_id = registry.async_get_entity_id(
        "switch", spec.domain, spec.active_switch_unique_id.format(entry_id=entry_id)
    )
    state = hass.states.get(entity_id) if entity_id else None
    return state is not None and state.state == "off"


def other_controllers(
    hass: HomeAssistant, *, charge_control: str | None, device_id: str | None
) -> list[OtherController]:
    """The controllers beside the charger whose charge-control entity is `charge_control` and whose device
    is `device_id` (either may be unknown). A controller is listed once, for the charger when its setting
    names this charger and for the installation when it cannot name one at all."""
    registry = er.async_get(hass)
    found: list[OtherController] = []
    for spec in CONTROLLERS:
        for entry in hass.config_entries.async_entries(spec.domain):
            if entry.disabled_by is not None or _is_switched_off(hass, registry, spec, entry.entry_id):
                continue
            if not spec.entity_keys and not spec.device_keys:
                found.append(OtherController(spec.domain, spec.name, SCOPE_INSTALLATION))
                break
            if _names_charger(registry, spec, entry.options, entry.data, charge_control, device_id):
                found.append(OtherController(spec.domain, spec.name, SCOPE_CHARGER))
                break
    return found


def _names_charger(
    registry: er.EntityRegistry,
    spec: ControllerSpec,
    options: Mapping[str, object],
    data: Mapping[str, object],
    charge_control: str | None,
    device_id: str | None,
) -> bool:
    for key in spec.entity_keys:
        value = _setting(options, data, key)
        if not isinstance(value, str) or not value.strip():
            continue
        entity_id = value.strip()
        if charge_control and entity_id == charge_control:
            return True
        named = registry.async_get(entity_id)
        if device_id and named is not None and named.device_id == device_id:
            return True
    for key in spec.device_keys:
        value = _setting(options, data, key)
        if device_id and isinstance(value, str) and value == device_id:
            return True
    return False
