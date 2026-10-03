"""A home battery's own limit on what the grid connection may import, set against SpotNav's.

Some battery integrations expose the limit their inverter keeps the grid import under. SpotNav keeps
the same fuse under its own limit (the main fuse minus the safety margin), so when the two differ the
smaller one decides and the other is a promise nothing keeps: "two limits on one fuse". This module only
finds the battery's limit and compares; it never writes either.

The Sigenergy integration (TypQxQ/Sigenergy-Local-Modbus, domain `sigen`) has the number
`plant_grid_maximum_import_limitation` ("Grid Import Limitation", entity id `number.<plant>_grid_import_limitation`),
in kW, disabled by default. The register is 32 bits wide in units of 1/1000 kW, and 0xFFFFFFFF / 1000 =
4294967.295 kW means "no limit". It is found by the unique id the integration gives it (the key at its
end), on the device of the site's battery entity; an entity a person left disabled is not read.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Final

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from ..const import CONF_BATTERY_AGGREGATE_POWER_ENTITY, CONF_BATTERY_DISCHARGE_POWER_ENTITY
from ..planning.first_run import site_limit_a
from ..planning.grid_voltage import stored_voltage_between_phases_v

#: Integration (platform) -> the end of the unique id of its grid import limit number.
IMPORT_LIMIT_UNIQUE_ID_SUFFIX: Final = {"sigen": "_plant_grid_maximum_import_limitation"}

#: At and above this the register says "no limit" (0xFFFFFFFF / 1000 kW, a little under it with rounding).
NO_LIMIT_KW: Final = 4_000_000.0

#: The two limits agree when they are within this many amperes per phase.
TOLERANCE_A: Final = 1.0

WARNING_CODE: Final = "battery_import_limit_differs"


@dataclass(frozen=True, slots=True)
class BatteryImportLimit:
    """The battery's grid import limit next to SpotNav's, per phase."""

    integration: str
    entity_id: str
    battery_a: float
    spotnav_a: float

    @property
    def differs(self) -> bool:
        return abs(self.battery_a - self.spotnav_a) > TOLERANCE_A


def _kilowatts(state_value: str, unit: object) -> float | None:
    try:
        value = float(state_value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value):
        return None
    if unit == "W":
        return value / 1000.0
    return value if unit in (None, "kW") else None


def battery_import_limit(hass: HomeAssistant, site: ConfigEntry) -> BatteryImportLimit | None:
    """The limit the site's battery sets on grid import, in amperes per phase beside SpotNav's, or `None`
    when the battery integration has none, it is disabled, unavailable or "no limit", or the site has no
    fuse to compare against.
    """
    spotnav_a = site_limit_a(site)
    if spotnav_a is None:
        return None
    registry = er.async_get(hass)
    for key in (CONF_BATTERY_AGGREGATE_POWER_ENTITY, CONF_BATTERY_DISCHARGE_POWER_ENTITY):
        entity_id = site.data.get(key)
        battery = registry.async_get(entity_id) if isinstance(entity_id, str) and entity_id else None
        suffix = None if battery is None else IMPORT_LIMIT_UNIQUE_ID_SUFFIX.get(battery.platform)
        if battery is None or suffix is None or battery.device_id is None:
            continue
        for candidate in er.async_entries_for_device(registry, battery.device_id):
            if (
                candidate.platform != battery.platform
                or candidate.domain != "number"
                or candidate.disabled_by is not None
                or not (candidate.unique_id or "").endswith(suffix)
            ):
                continue
            state = hass.states.get(candidate.entity_id)
            kilowatts = None if state is None else _kilowatts(state.state, state.attributes.get("unit_of_measurement"))
            if kilowatts is None or kilowatts >= NO_LIMIT_KW or kilowatts < 0:
                continue
            volts = stored_voltage_between_phases_v(site.data)
            return BatteryImportLimit(
                integration=battery.platform,
                entity_id=candidate.entity_id,
                battery_a=kilowatts * 1000.0 / (math.sqrt(3.0) * volts),
                spotnav_a=float(spotnav_a),
            )
    return None
