"""The heat card's commands (SpotNav 3.0, plan `plans/v3_heat_card.md`): `spotnav/update_heat` writes the site's heat
settings (`heat/config.py`) with a compare-and-set. Shadow only: nothing here or behind it writes to a heating device.

`spotnav/update_heat` (API v1):

    {type: "spotnav/update_heat", api_version: 1, site_id | charger_id,
     changes: {target_c?, mode?, away_until?, night?, indoor_sensors?, outdoor_sensor?, control_path?,
               control_entity?, control_entity_b?, hot_water_entity?, zones?, heat_meter_entity?},
     expected: {the same keys: the values the caller last saw}}

The answer is one envelope for every outcome: `{api_version, ok, error, field_errors, config}`, `config` re-read
after the write (the settings as `HeatConfig.as_dict()` plus `heat_meter_entity`, the heat pump meter's setting,
`null` for none). Refusals (`error`): `spotnav_conflict` (an `expected` value is not what is stored: nothing is
written), `spotnav_invalid_value` (with `field_errors`, `{field, code}`; nothing is written unless every field
passes), `spotnav_not_admin` (a caller who is not an administrator named anything but `target_c`, `mode` and
`away_until`: the dial and home/away are a thermostat's own controls, the rest is configuration). An unknown site,
charger or version is a WebSocket error as `spotnav/get_house` answers it.

The settings live in the site entry's data under `CONF_HEAT`, written once and without a reload (nothing in load
balancing reads them; the heat view reads them on each read). `heat_meter_entity` is the same setting the house
card's site settings write (`CONF_HEAT_METER_ENTITY`, a power or energy sensor, `null` or `""` clears it); the
house forecast reads it at its next refresh, which the write asks for.
"""

from __future__ import annotations

import logging
from typing import Any, Final

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import callback, HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from ..const import CONF_HEAT, CONF_HEAT_METER_ENTITY
from ..heat.config import (
    FIELDS,
    USER_FIELDS,
    FieldError,
    HeatConfig,
    apply_changes,
    comparable,
    read_config,
)
from ..runtime import site_controller_for
from ..site.heat_meter import sensor_kind
from .common import ERROR_NOT_ADMIN, is_admin, send_unsupported_version
from .power_month import PowerMonthRefusal, _site_of_request

_LOGGER = logging.getLogger(__name__)

HEAT_API_VERSION: Final = 1

ERROR_CONFLICT: Final = "spotnav_conflict"
ERROR_INVALID_VALUE: Final = "spotnav_invalid_value"

F_METER: Final = "heat_meter_entity"
WRITABLE: Final = (*FIELDS, F_METER)

#: Units a temperature sensor may have (one without a device class is told by its unit).
TEMPERATURE_UNITS: Final = frozenset({"°C", "°F", "K"})


def stored_config(entry: ConfigEntry) -> HeatConfig:
    """The site's heat settings as stored (defaults for anything missing)."""
    return read_config(entry.data.get(CONF_HEAT))


def stored_meter(entry: ConfigEntry) -> str | None:
    return entry.data.get(CONF_HEAT_METER_ENTITY) or None


def config_block(entry: ConfigEntry) -> dict[str, Any]:
    """The settings as answered: `HeatConfig.as_dict()` and the heat pump meter's setting."""
    return {**stored_config(entry).as_dict(), F_METER: stored_meter(entry)}


def is_temperature(hass: HomeAssistant, entity_id: str) -> bool | None:
    """Whether a sensor reads a temperature (its device class, else its unit); `None` when it is unknown."""
    state = hass.states.get(entity_id)
    if state is not None:
        if state.attributes.get("device_class") == "temperature":
            return True
        if state.attributes.get("unit_of_measurement") in TEMPERATURE_UNITS:
            return True
    entry = er.async_get(hass).async_get(entity_id)
    if entry is not None:
        if (entry.device_class or entry.original_device_class) == "temperature":
            return True
        if entry.unit_of_measurement in TEMPERATURE_UNITS:
            return True
    if state is None and entry is None:
        return None
    return False


def entity_check(hass: HomeAssistant) -> Any:
    """The judge's entity check (`heat/config.EntityCheck`) against this Home Assistant."""

    def check(entity_id: str, role: str) -> bool | None:
        known = hass.states.get(entity_id) is not None or er.async_get(hass).async_get(entity_id) is not None
        if not known:
            return None
        if role in ("indoor", "outdoor") and entity_id.startswith("sensor."):
            return bool(is_temperature(hass, entity_id))
        return True

    return check


class HeatWriteRefusal(Exception):
    def __init__(self, code: str, field_errors: list[FieldError] | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.field_errors = field_errors or []


def _same(name: str, stored: Any, expected: Any) -> bool:
    if name == F_METER and expected == "":
        expected = None
    return comparable(name, stored) == comparable(name, expected)


def write_heat(hass: HomeAssistant, site: ConfigEntry, changes: Any, expected: Any, *, admin: bool) -> None:
    """Judge and write one change of the site's heat settings, or raise `HeatWriteRefusal` (see the module). One
    synchronous step on the event loop from the compare to the write, so two writes cannot interleave."""
    if not isinstance(changes, dict) or not changes:
        raise HeatWriteRefusal(ERROR_INVALID_VALUE, [FieldError("changes", "invalid_changes")])
    if expected is None:
        expected = {}
    if not isinstance(expected, dict):
        raise HeatWriteRefusal(ERROR_INVALID_VALUE, [FieldError("expected", "invalid_expected")])
    unknown = [FieldError(name, "unknown_field") for name in (*changes, *expected) if name not in WRITABLE]
    if unknown:
        raise HeatWriteRefusal(ERROR_INVALID_VALUE, list(dict.fromkeys(unknown)))
    if not admin and set(changes) - USER_FIELDS:
        raise HeatWriteRefusal(ERROR_NOT_ADMIN)

    current = config_block(site)
    for name, value in expected.items():
        if not _same(name, current.get(name), value):
            raise HeatWriteRefusal(ERROR_CONFLICT)

    heat = stored_config(site)
    heat_changes = {name: value for name, value in changes.items() if name != F_METER}
    updated, errors = (
        apply_changes(heat, heat_changes, now=dt_util.utcnow(), check=entity_check(hass)) if heat_changes else (heat, [])
    )
    meter = stored_meter(site)
    if F_METER in changes:
        raw = changes[F_METER]
        if raw is None or raw == "":
            meter = None
        elif not isinstance(raw, str) or sensor_kind(hass, raw) is None:
            known = isinstance(raw, str) and hass.states.get(raw) is not None
            errors.append(FieldError(F_METER, "wrong_kind" if known else "entity_not_found"))
        else:
            meter = raw
    if errors:
        raise HeatWriteRefusal(ERROR_INVALID_VALUE, errors)

    data = dict(site.data)
    data[CONF_HEAT] = updated.as_dict()
    if meter is None:
        data.pop(CONF_HEAT_METER_ENTITY, None)
    else:
        data[CONF_HEAT_METER_ENTITY] = meter
    if data != dict(site.data):
        hass.config_entries.async_update_entry(site, data=data)
        _LOGGER.debug("SpotNav heat settings changed: %s", sorted(changes))
    if F_METER in changes and (controller := site_controller_for(hass, site.entry_id)) is not None:
        controller.house_forecast.request_refresh()


def _envelope(site: ConfigEntry | None, error: str | None = None, field_errors: list[FieldError] | None = None) -> dict[str, Any]:
    return {
        "api_version": HEAT_API_VERSION,
        "ok": error is None,
        "error": error,
        "field_errors": [item.as_dict() for item in field_errors or []],
        "config": None if site is None else config_block(site),
    }


@websocket_api.websocket_command(
    {
        vol.Required("type"): "spotnav/update_heat",
        vol.Optional("api_version"): object,
        vol.Optional("charger_id"): object,
        vol.Optional("site_id"): object,
        vol.Optional("changes"): object,
        vol.Optional("expected"): object,
    }
)
@callback
def websocket_update_heat(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """Change the site's heat settings (see the module)."""
    version = msg.get("api_version")
    if type(version) is not int or version != HEAT_API_VERSION:
        send_unsupported_version(connection, msg, HEAT_API_VERSION)
        return
    try:
        site = _site_of_request(hass, msg)
    except PowerMonthRefusal as refusal:
        connection.send_error(msg["id"], refusal.code, refusal.message)
        return
    try:
        write_heat(hass, site, msg.get("changes"), msg.get("expected"), admin=is_admin(connection))
    except HeatWriteRefusal as refusal:
        connection.send_result(msg["id"], _envelope(site, refusal.code, refusal.field_errors))
        return
    connection.send_result(msg["id"], _envelope(site))


def heat_settings_diagnostics(entry: ConfigEntry) -> dict[str, Any]:
    """The diagnostics block: the settings as stored and read (entity ids only, no states)."""
    return {"stored": entry.data.get(CONF_HEAT), "read": config_block(entry)}


@callback
def async_setup_heat_api(hass: HomeAssistant) -> None:
    """Register the heat card's commands once for the domain."""
    websocket_api.async_register_command(hass, websocket_update_heat)
