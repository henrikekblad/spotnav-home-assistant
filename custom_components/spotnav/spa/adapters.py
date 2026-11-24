"""How each spa and sauna family is set (spec section 6.2 and the sauna decision of 2026-11-23). Pure tables, read in
each integration's source (scratch clones of 2026-11-24; core components of Home Assistant 2026.9):

| Family | Heat on / held off | Setpoint | Source read |
|---|---|---|---|
| `balboa` (core, local) | `climate.set_preset_mode` `ready` / `rest` (its hvac `off` **is** Rest, `heat` is Ready) | the bath temperature, only when the dial changes, within the spa's range | `balboa/climate.py` `HEAT_HVAC_MODE_MAP`, presets from `HeatMode` |
| `smarttub` (core, cloud) | none (hvac is `heat` only) | between the idle temperature (never below the floor) and the bath temperature | `smarttub/climate.py` |
| `mspa` (HACS, cloud) | `climate.set_hvac_mode` `heat` / `off` | the dial | `mspa/climate.py`; its own heat schedule is the `datetime` "Scheduled for" (`mspa_scheduled_ready_at_<device>`) |
| `gecko` (HACS, local) | none: the `water_heater` only, its climate's presets are the water care and never written | as SmartTub | `gecko/water_heater.py` (target temperature only) |
| `bestway` (HACS, cloud) | `climate.set_hvac_mode` `heat` / `off` | the dial | `bestway/climate.py` |
| `intex_spa` (HACS, local) | `climate.set_hvac_mode` `heat` / `off` | the dial | `intex_spa/climate.py` |
| `tuya_intex` (`tuya_local`, local) | `water_heater.set_operation_mode` `electric` / `off` | the dial | `tuya_local/devices/intex_purespa_spa.yaml` |
| saunas `huum`, `harvia_sauna` (cloud), `saunum`, `tylo_sauna`, `sentio` (local) | `climate.set_hvac_mode` `heat` / `off` (Tylö's standby, `heat_cool`, is never written) | the session temperature, only when the dial changes | each `climate.py` |
| sauna `relay` | `turn_on` / `turn_off` of the switch | none (the heater's own thermostat) | - |

Filtration, filter cycles, jets, blowers, lights and the temperature ranges are never written.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

from ..sauna.config import (
    FAMILY_HARVIA,
    FAMILY_HUUM,
    FAMILY_RELAY,
    FAMILY_SAUNUM,
    FAMILY_SENTIO,
    FAMILY_TYLO,
)
from .config import (
    FAMILY_BALBOA,
    FAMILY_BESTWAY,
    FAMILY_GECKO,
    FAMILY_INTEX,
    FAMILY_MSPA,
    FAMILY_SMARTTUB,
    FAMILY_TUYA_INTEX,
)

#: How the heater is turned on and held off.
HEAT_PRESET: Final = "preset"
HEAT_HVAC: Final = "hvac"
HEAT_OPERATION: Final = "operation"
HEAT_SWITCH: Final = "switch"
#: How the setpoint is used: the household's dial only when it changes (`dial`), the setpoint itself is the handle
#: between the idle and the bath temperature (`hold`), or none.
SETPOINT_DIAL: Final = "dial"
SETPOINT_HOLD: Final = "hold"
SETPOINT_NONE: Final = "none"

#: MSpa's own heat schedule: the unique id of its "Scheduled for" `datetime` (`mspa/datetime.py`), a prefix.
MSPA_SCHEDULE_ID: Final = "mspa_scheduled_ready_at"
#: Saunum's session length (minutes, `saunum/number.py`) and Harvia's on time (minutes): the sauna's own longest run.
SAUNUM_DURATION_KEY: Final = "sauna_duration"
HARVIA_ON_TIME_KEY: Final = "on_time"
#: The Home Assistant domain each family is read through (the canary's), where it is not the family's own name.
FAMILY_DOMAINS: Final = {"tuya_intex": "tuya_local"}

#: Balboa's presets as its hvac mode shows them (`balboa/climate.py`).
_BALBOA_FROM_HVAC: Final = {"heat": "ready", "off": "rest", "auto": "ready_in_rest"}


@dataclass(frozen=True, slots=True)
class Route:
    family: str
    domain: str
    heat: str | None
    on: str = "heat"
    off: str = "off"
    setpoint: str = SETPOINT_DIAL
    cloud: bool = False


ROUTES: Final[dict[str, Route]] = {
    FAMILY_BALBOA: Route(FAMILY_BALBOA, "climate", HEAT_PRESET, on="ready", off="rest"),
    FAMILY_SMARTTUB: Route(FAMILY_SMARTTUB, "climate", None, setpoint=SETPOINT_HOLD, cloud=True),
    FAMILY_MSPA: Route(FAMILY_MSPA, "climate", HEAT_HVAC, cloud=True),
    FAMILY_GECKO: Route(FAMILY_GECKO, "water_heater", None, setpoint=SETPOINT_HOLD),
    FAMILY_BESTWAY: Route(FAMILY_BESTWAY, "climate", HEAT_HVAC, cloud=True),
    FAMILY_INTEX: Route(FAMILY_INTEX, "climate", HEAT_HVAC),
    FAMILY_TUYA_INTEX: Route(FAMILY_TUYA_INTEX, "water_heater", HEAT_OPERATION, on="electric"),
    FAMILY_HUUM: Route(FAMILY_HUUM, "climate", HEAT_HVAC, cloud=True),
    FAMILY_HARVIA: Route(FAMILY_HARVIA, "climate", HEAT_HVAC, cloud=True),
    FAMILY_SAUNUM: Route(FAMILY_SAUNUM, "climate", HEAT_HVAC),
    FAMILY_TYLO: Route(FAMILY_TYLO, "climate", HEAT_HVAC),
    FAMILY_SENTIO: Route(FAMILY_SENTIO, "climate", HEAT_HVAC),
    FAMILY_RELAY: Route(FAMILY_RELAY, "switch", HEAT_SWITCH, on="on", off="off", setpoint=SETPOINT_NONE),
}


def route_of(family: str, entity_id: str) -> Route | None:
    """The family's route, `None` for a family SpotNav only watches or an entity of another domain (a Gecko climate:
    its presets are the water care)."""
    route = ROUTES.get(family)
    if route is None:
        return None
    domain = entity_id.split(".", 1)[0]
    if route.heat == HEAT_SWITCH:
        return route if domain in ("switch", "input_boolean") else None
    return route if domain == route.domain else None


def heat_read(route: Route, state: str | None, attributes: dict[str, Any]) -> str | None:
    """The heater's on/off setting as read (Balboa's preset, a climate's mode, a water heater's operation, a switch)."""
    if state is None or state in ("unavailable", "unknown"):
        return None
    if route.heat == HEAT_PRESET:
        preset = attributes.get("preset_mode")
        return str(preset) if isinstance(preset, str) else _BALBOA_FROM_HVAC.get(state)
    return state


def heat_call(route: Route, entity_id: str, value: str) -> tuple[str, str, dict[str, Any]] | None:
    """The service call that sets the heater to `value`, `None` for a value SpotNav never writes."""
    if route.heat == HEAT_PRESET:
        return ("climate", "set_preset_mode", {"entity_id": entity_id, "preset_mode": value})
    if route.heat == HEAT_HVAC:
        if value not in (route.on, route.off):
            # Tylö's standby, a person's auto or cool: never written.
            return None
        return ("climate", "set_hvac_mode", {"entity_id": entity_id, "hvac_mode": value})
    if route.heat == HEAT_OPERATION:
        if value not in (route.on, route.off):
            return None
        return ("water_heater", "set_operation_mode", {"entity_id": entity_id, "operation_mode": value})
    if route.heat == HEAT_SWITCH:
        if value not in ("on", "off"):
            return None
        return (entity_id.split(".", 1)[0], "turn_on" if value == "on" else "turn_off", {"entity_id": entity_id})
    return None


def setpoint_call(route: Route, entity_id: str, value: float) -> tuple[str, str, dict[str, Any]] | None:
    if route.setpoint == SETPOINT_NONE:
        return None
    return (route.domain, "set_temperature", {"entity_id": entity_id, "temperature": value})
