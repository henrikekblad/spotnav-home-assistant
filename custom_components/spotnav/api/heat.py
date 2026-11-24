"""The heat card's commands (SpotNav 3.0, plan `plans/v3_heat_card.md`): `spotnav/get_heat` reads the card's view of
a site, `spotnav/update_heat` writes the site's heat settings (`heat/config.py`) with a compare-and-set. Nothing here writes
to a heating device; `control_enabled` is the person's opt-in that lets the heat control (`heat/control.py`) steer
the pump through the chosen path, and a write makes it look at once (turned off, it gives back what it holds).

`spotnav/get_heat` (API v1): `{api_version: 1, site_id | charger_id}`, any user who can see the card; the answer is
`heat/view.py:heat_view` (its docstring has the shape; `writable` and `choices` only for an administrator). Read from
what Home Assistant holds: the settings, the chosen sensors' states and their last half hour from the recorder
(`heat/rooms.py`, read again every `ROOM_REFRESH_S`) and their 5-minute statistics since midnight for the day's
indoor curve (`heat/indoor_day.py`, read again once per new 5-minute row), the zones' climate entities and a power sensor on each one's
device, the heat pump's facts (`heat/detect.py`), the heat pump meter's 5-minute rows of the last day
(`heat/meter_split.py`, read again once per new 5-minute row), the prices with their fees as the battery plan holds
them, the cars' measured hours and installed plans, and the site's heat shadow (`heat/runtime.py`: the relay, its
closings and learned effect, the joint plan's tight hours, the hours kept as planned, yesterday's review, the
automations that reference the relay or a zone). The day band is SpotNav's plan (`heat/shadow_rule.py`); the heat
shadow logs each change of what it would do now (in the diagnostics). Refusals as `spotnav/get_house`.

`spotnav/update_heat` (API v1):

    {type: "spotnav/update_heat", api_version: 1, site_id | charger_id,
     changes: {target_c?, mode?, away_until?, away_delta_c?, night?, indoor_sensors?, indoor_offsets?, outdoor_sensor?,
               control_path?, control_entity?, control_entity_b?, hot_water_entity?, zones?, control_enabled?,
               water_heater?, heat_meter_entity?},
     expected: {the same keys: the values the caller last saw},
     enable_control_entities?: true}

`enable_control_entities` is the person's confirm of the opt-in's question (the status's `control.enable_entities`):
before anything is written, the stored control path's entities that are disabled in their integration are enabled
and that integration reloaded once (`heat/optin.py`). An administrator only, never over the webhook;
`spotnav_enable_failed` when it could not be done (nothing is written then).

The answer is one envelope for every outcome: `{api_version, ok, error, field_errors, config}`, `config` re-read
after the write (the settings as `HeatConfig.as_dict()` plus `heat_meter_entity`, the heat pump meter's setting,
`null` for none). Refusals (`error`): `spotnav_conflict` (an `expected` value is not what is stored: nothing is
written), `spotnav_invalid_value` (with `field_errors`, `{field, code}`; nothing is written unless every field
passes), `spotnav_not_admin` (a caller who is not an administrator named anything but `target_c`, `mode` and
`away_until`: the dial and home/away are a thermostat's own controls, the rest is configuration). An unknown site,
charger or version is a WebSocket error as `spotnav/get_house` answers it.

`spotnav/heat_boost` (API v1): the Boost button, any user who can see the card (a household control like the dial):

    {type: "spotnav/heat_boost", api_version: 1, site_id | charger_id, action: "start" | "stop"}
    -> {api_version, ok, error, boost}

`boost` is `get_heat`'s `hot_water.boost` after the press (`null`: the pump has no boost that ends by itself). A
start goes out through the pump's own boost or the settings' hot-water input (`heat/dhw_runtime.HotWater.async_press`)
and is read back in the background (`boost.state` "starting" until then). Refusals (`error`): `spotnav_not_supported`
(no boost route, or a stop with no way to end it early), `spotnav_busy` (a start is pending), `spotnav_rate_limited`
(one start per ten minutes per site), `spotnav_no_headroom` (a phase is already over the main fuse less its margin:
turn something off and press again), `spotnav_write_failed` (the write was not taken), `spotnav_not_own` (a stop of a
run that is not the household's: SpotNav's planned boost or the pump's own run). The webhook's `heat_boost` is the
same body without `type`.

The settings live in the site entry's data under `CONF_HEAT`, written once and without a reload (nothing in load
balancing reads them; the heat view reads them on each read). `heat_meter_entity` is the same setting the house
card's site settings write (`CONF_HEAT_METER_ENTITY`, a power or energy sensor, `null` or `""` clears it); the
house forecast reads it at its next refresh, which the write asks for.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import Any, Final

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import callback, HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.util import dt as dt_util

from .. import time_zones
from ..battery.plan_runtime import site_car_windows
from ..const import CONF_HEAT, CONF_HEAT_METER_ENTITY, DOMAIN
from ..heat.config import (
    F_AWAY_DELTA,
    F_INDOOR_OFFSETS,
    F_NIGHT,
    F_ZONES,
    FIELDS,
    PATH_SG_READY_API,
    USER_FIELDS,
    FieldError,
    HeatConfig,
    apply_changes,
    comparable,
    read_config,
)
from ..heat import optin as heat_optin
from ..heat.control import raise_entity
from ..heat.detect import other_controllers, read_heat_pumps, sg_ready_api_fits
from ..heat.facts import HeatPumpFacts
from ..heat.meter_split import levels, split_day
from ..heat.indoor_day import LIVE_STEP_MIN, indoor_day, means_by_start
from ..heat.rooms import WINDOW, Indoor, SensorReading, indoor
from ..heat.shadow_rule import day_hours
from ..heat.view import (
    HEAT_API_VERSION,
    KIND_AIR,
    KIND_FLOOR,
    HeatViewInputs,
    MeterInputs,
    Outdoor,
    RelayInputs,
    ZoneReading,
    heat_view,
)
from ..notifications.heat_settings import F_HEAT_NOTIFICATIONS
from ..notifications.phones import PhoneError
from ..notifications.targets import available_targets, encoded_available
from ..pool.detect import Candidate, circulations, heat_pumps, read_seen, suggest, water_sensors
from ..pricing.price_source import site_price_source
from ..sauna.detect import SaunaCandidate, saunas
from ..spa.detect import SpaCandidate, spas
from ..runtime import domain_data, site_controller_for
from ..site.heat_meter import (
    HEAT_WORDS,
    KIND_POWER,
    SOURCE_SETTING,
    STATUS_NONE,
    async_heat_hours,
    needs_setting,
    sensor_kind,
)
from ..site.house_forecast import to_celsius
from .common import ERROR_NOT_ADMIN, ERROR_NOT_PERMITTED_OVER_WEBHOOK, is_admin, send_unsupported_version
from ..site.heat_zones import async_zone_periods, summed_periods
from .house import async_cached_car_hours, five_slot, fuse_kw
from .power_month import ERROR_NO_SITE, PowerMonthRefusal, _site_of_request

_LOGGER = logging.getLogger(__name__)

_CACHE: Final = f"{DOMAIN}_heat_reads"
#: The indoor sensors' history is read again after this many seconds.
ROOM_REFRESH_S: Final = 120
#: An indoor sensor is offered when it reads within this band now (°C).
INDOOR_CHOICE: Final = (10.0, 30.0)

ERROR_CONFLICT: Final = "spotnav_conflict"
ERROR_INVALID_VALUE: Final = "spotnav_invalid_value"
ERROR_ENABLE_FAILED: Final = "spotnav_enable_failed"
#: The person's answer to the stored offset's line (`heat/control.HeatControl.async_offset_home`) was not carried out.
ERROR_OFFSET_HOME_FAILED: Final = "spotnav_offset_home_failed"

F_METER: Final = "heat_meter_entity"
WRITABLE: Final = (*FIELDS, F_METER)

#: Units a temperature sensor may have (one without a device class is told by its unit).
TEMPERATURE_UNITS: Final = frozenset({"°C", "°F", "K"})


def stored_config(entry: ConfigEntry) -> HeatConfig:
    """The site's heat settings as stored (defaults for anything missing)."""
    return read_config(entry.data.get(CONF_HEAT))


def stored_meter(entry: ConfigEntry) -> str | None:
    return entry.data.get(CONF_HEAT_METER_ENTITY) or None


def config_block(entry: ConfigEntry, hass: HomeAssistant | None = None) -> dict[str, Any]:
    """The settings as answered: `HeatConfig.as_dict()` and the heat pump meter's setting. With `hass`,
    `heat_notifications` is read from the phones (`notifications/phone_store.py`), as a card before them shows it."""
    block = {**stored_config(entry).as_dict(), F_METER: stored_meter(entry)}
    phones = None if hass is None else domain_data(hass).phones
    if phones is not None:
        block[F_HEAT_NOTIFICATIONS] = phones.heat_view()
    return block


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
        if role in ("indoor", "outdoor", "tank", "pool_water") and entity_id.startswith("sensor."):
            return bool(is_temperature(hass, entity_id))
        if role == PATH_SG_READY_API:
            # The SG Ready API writes 0..2 to its mode: only the pump's own SG Ready number, never any number.
            return sg_ready_api_fits(hass, entity_id, "number")
        if role == f"{PATH_SG_READY_API}_b":
            return sg_ready_api_fits(hass, entity_id, "switch")
        return True

    return check


def setpoint_of(hass: HomeAssistant) -> Any:
    """An entity's setpoint now in °C (a climate's or a water heater's `temperature`), `None` when it has none: a new
    spa's idle temperature starts at it."""

    def read(entity_id: str) -> float | None:
        state = hass.states.get(entity_id)
        if state is None:
            return None
        value = state.attributes.get("temperature")
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            return None
        # Home Assistant shows a climate's and a water heater's temperatures in the system's unit.
        return to_celsius(float(value), hass.config.units.temperature_unit)

    return read


class HeatWriteRefusal(Exception):
    def __init__(self, code: str, field_errors: list[FieldError] | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.field_errors = field_errors or []


def _same(name: str, stored: Any, expected: Any) -> bool:
    if name == F_METER and expected == "":
        expected = None
    if name == F_ZONES and isinstance(stored, list) and isinstance(expected, list):
        # A caller that does not know a zone's `control` yet (an older app) compares the rest.
        known = {item.get("entity_id"): item.get("control", False) for item in stored if isinstance(item, Mapping)}
        expected = [
            {**item, "control": known.get(item.get("entity_id"), False)} if isinstance(item, Mapping) and "control" not in item else item
            for item in expected
        ]
    return comparable(name, stored) == comparable(name, expected)


#: What the charger webhook's secret may write (the owner's decisions of 2026-11-18): the dial, home/away, the
#: cooler night, the away setback and each zone's and each indoor sensor's offset. The control opt-in, the control path and every
#: entity choice are not here.
WEBHOOK_FIELDS: Final = frozenset({*USER_FIELDS, F_AWAY_DELTA, F_NIGHT, F_ZONES, F_INDOOR_OFFSETS})


def _own_sensors(stored: HeatConfig, changes: Mapping[str, Any]) -> bool:
    """Whether a webhook write's `indoor_offsets` (when named) names only the stored indoor sensors: an offset never
    brings a sensor in. The list is sparse as stored (a sensor left out, or at 0, has no offset; `null` clears them
    all), so it cannot take a sensor out either. A shape the judge will refuse passes here."""
    raw = changes.get(F_INDOOR_OFFSETS)
    if not isinstance(raw, list):
        return True
    return all(
        not isinstance(item, Mapping) or "entity_id" not in item or item["entity_id"] in stored.indoor_sensors
        for item in raw
    )


def _same_zones(stored: HeatConfig, changes: Mapping[str, Any]) -> bool:
    """Whether a webhook write's `zones` (when named) lists exactly the stored zones, in their order: only the
    offsets may differ. A shape the judge will refuse passes here, so it is answered as the invalid value it is."""
    if F_ZONES not in changes:
        return True
    raw = changes[F_ZONES]
    if not isinstance(raw, list) or any(not isinstance(item, Mapping) for item in raw):
        return raw is not None or not stored.zones
    if [item.get("entity_id") for item in raw] != [zone.entity_id for zone in stored.zones]:
        return False
    # The opt-in to set a thermostat is an administrator's: over the webhook a zone keeps its own.
    return all("control" not in item or item["control"] == zone.control for item, zone in zip(raw, stored.zones, strict=True))


def write_heat(
    hass: HomeAssistant, site: ConfigEntry, changes: Any, expected: Any, *, admin: bool, over_webhook: bool = False
) -> None:
    """Judge and write one change of the site's heat settings, or raise `HeatWriteRefusal` (see the module). One
    synchronous step on the event loop from the compare to the write, so two writes cannot interleave.
    `over_webhook`: the charger webhook's secret wrote it (the paired app): `WEBHOOK_FIELDS` only, `zones` only
    with the zones as stored (their offsets may change, never which entities they are) and `indoor_offsets` only
    for the stored indoor sensors; anything else
    (`control_enabled`, the sensors, the control path) is `spotnav_not_permitted_over_webhook`."""
    if not isinstance(changes, dict) or not changes:
        raise HeatWriteRefusal(ERROR_INVALID_VALUE, [FieldError("changes", "invalid_changes")])
    if expected is None:
        expected = {}
    if not isinstance(expected, dict):
        raise HeatWriteRefusal(ERROR_INVALID_VALUE, [FieldError("expected", "invalid_expected")])
    unknown = [FieldError(name, "unknown_field") for name in (*changes, *expected) if name not in WRITABLE]
    if unknown:
        raise HeatWriteRefusal(ERROR_INVALID_VALUE, list(dict.fromkeys(unknown)))
    if over_webhook:
        stored = stored_config(site)
        if set(changes) - WEBHOOK_FIELDS or not _same_zones(stored, changes) or not _own_sensors(stored, changes):
            raise HeatWriteRefusal(ERROR_NOT_PERMITTED_OVER_WEBHOOK)
    elif not admin and set(changes) - USER_FIELDS:
        raise HeatWriteRefusal(ERROR_NOT_ADMIN)

    current = config_block(site, hass)
    for name, value in expected.items():
        if not _same(name, current.get(name), value):
            raise HeatWriteRefusal(ERROR_CONFLICT)

    heat = stored_config(site)
    heat_changes = {name: value for name, value in changes.items() if name != F_METER}
    updated, errors = (
        apply_changes(heat, heat_changes, now=dt_util.utcnow(), check=entity_check(hass), setpoint_of=setpoint_of(hass))
        if heat_changes
        else (heat, [])
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

    if F_HEAT_NOTIFICATIONS in heat_changes and (phones := domain_data(hass).phones) is not None:
        # A card before the per-phone settings chose the heat card's phones: applied to the phones.
        hass.async_create_task(_async_heat_phones(phones, heat_changes[F_HEAT_NOTIFICATIONS]), eager_start=True)
    data = dict(site.data)
    data[CONF_HEAT] = updated.as_dict()
    if meter is None:
        data.pop(CONF_HEAT_METER_ENTITY, None)
    else:
        data[CONF_HEAT_METER_ENTITY] = meter
    changed = data != dict(site.data)
    if changed:
        hass.config_entries.async_update_entry(site, data=data)
        _LOGGER.debug("SpotNav heat settings changed: %s", sorted(changes))
        if F_ZONES in changes or F_METER in changes:
            # The zones and the meter decide what the flow and the pies count as heat: read them again at once.
            from .site_settings import _forget_energy_reads  # noqa: PLC0415 - site_settings imports this module's peers

            _forget_energy_reads(hass, site.entry_id)
    if changed:
        # The site's Away switch (`switch.py`) shows the new settings at once.
        from ..switch import heat_changed_signal  # noqa: PLC0415 - the platform imports this module

        async_dispatcher_send(hass, heat_changed_signal(site.entry_id))
    controller = site_controller_for(hass, site.entry_id)
    if F_METER in changes and controller is not None:
        controller.house_forecast.request_refresh()
    if controller is not None and (control := getattr(controller, "heat_control", None)) is not None:
        # The heat control looks at once: turned off or the relay changed, it opens what it holds right away.
        control.request_look()
    for name in ("zones_control", "water_heater_control", "pool_spa", "pool_spa_control"):
        # The zones and the water heater too: an opt-in turned off gives back at once (the pool and spa plan again).
        if controller is not None and (part := getattr(controller, name, None)) is not None:
            part.request_look()


async def _async_heat_phones(phones: Any, raw: Any) -> None:
    try:
        await phones.async_heat_write(raw)
    except PhoneError:
        _LOGGER.debug("SpotNav heat: heat_notifications could not be applied to the phones")


def write_envelope(
    site: ConfigEntry | None,
    error: str | None = None,
    field_errors: list[FieldError] | None = None,
    hass: HomeAssistant | None = None,
) -> dict[str, Any]:
    return {
        "api_version": HEAT_API_VERSION,
        "ok": error is None,
        "error": error,
        "field_errors": [item.as_dict() for item in field_errors or []],
        "config": None if site is None else config_block(site, hass),
    }


@websocket_api.websocket_command(
    {
        vol.Required("type"): "spotnav/update_heat",
        vol.Optional("api_version"): object,
        vol.Optional("charger_id"): object,
        vol.Optional("site_id"): object,
        vol.Optional("changes"): object,
        vol.Optional("expected"): object,
        vol.Optional("enable_control_entities"): bool,
        vol.Optional("offset_home"): vol.In(("restore", "keep")),
    }
)
@websocket_api.async_response
async def websocket_update_heat(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
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
    if (choice := msg.get("offset_home")) is not None:
        # The person's answer to the stored offset's line: put the household's own value back, or keep what it reads.
        if not is_admin(connection):
            connection.send_result(msg["id"], write_envelope(site, ERROR_NOT_ADMIN, hass=hass))
            return
        controller = site_controller_for(hass, site.entry_id)
        control = getattr(controller, "heat_control", None)
        failed = "no_control" if control is None else await control.async_offset_home(choice)
        if failed is not None:
            _LOGGER.info("SpotNav heat control: the offset's answer %s was not carried out: %s", choice, failed)
            connection.send_result(msg["id"], write_envelope(site, ERROR_OFFSET_HOME_FAILED, hass=hass))
            return
        if not msg.get("changes"):
            connection.send_result(msg["id"], write_envelope(site, hass=hass))
            return
    if msg.get("enable_control_entities") is True:
        # The person's confirm of the opt-in's question: an administrator's only.
        if not is_admin(connection):
            connection.send_result(msg["id"], write_envelope(site, ERROR_NOT_ADMIN, hass=hass))
            return
        try:
            await heat_optin.async_enable(hass, stored_config(site))
        except Exception:  # noqa: BLE001 - the answer says so; nothing is written
            _LOGGER.exception("SpotNav heat control: enabling the control entities failed")
            connection.send_result(msg["id"], write_envelope(site, ERROR_ENABLE_FAILED, hass=hass))
            return
    try:
        write_heat(hass, site, msg.get("changes"), msg.get("expected"), admin=is_admin(connection))
    except HeatWriteRefusal as refusal:
        connection.send_result(msg["id"], write_envelope(site, refusal.code, refusal.field_errors, hass=hass))
        return
    connection.send_result(msg["id"], write_envelope(site, hass=hass))


def heat_settings_diagnostics(hass: HomeAssistant, entry: ConfigEntry) -> dict[str, Any]:
    """The diagnostics block: the settings as stored and read (entity ids only, no states), what SpotNav's plan would
    have done, each change once, and the heat shadow (`heat/runtime.py`: the relay, its learned effect, the plan,
    the kept days and their reviews)."""
    controller = site_controller_for(hass, entry.entry_id)
    shadow = getattr(controller, "heat_shadow", None)
    control = getattr(controller, "heat_control", None)
    block = None if control is None else control.diagnostics()
    for name, attribute in (("zones", "zones_control"), ("water_heater", "water_heater_control")):
        # The zones' and the water heater's own control: each zone's kept state, whether it qualifies and why, the
        # writes and the log; the water heater's mode, power, need, last full heat, windows, the hard rule in force.
        part = getattr(controller, attribute, None)
        if block is not None and part is not None:
            try:
                block[name] = part.diagnostics()
            except Exception:  # noqa: BLE001 - the rest of the diagnostics stands
                _LOGGER.debug("SpotNav heat: the %s diagnostics could not be read", name, exc_info=True)
    pool_spa = getattr(controller, "pool_spa", None)
    return {
        "stored": entry.data.get(CONF_HEAT),
        "read": config_block(entry),
        "pool_spa": None if pool_spa is None else pool_spa.diagnostics(),
        "shadow_log": [] if shadow is None else shadow.log.as_list(),
        "shadow": None if shadow is None else shadow.diagnostics(),
        "control": block,
    }


# ------------------------------------------------------------------------------------------------ the read


def _name(hass: HomeAssistant, entity_id: str) -> str:
    state = hass.states.get(entity_id)
    if state is not None:
        return state.name
    entry = er.async_get(hass).async_get(entity_id)
    return (entry.name or entry.original_name or entity_id) if entry is not None else entity_id


def _area(hass: HomeAssistant, entity_id: str) -> str | None:
    """The entity's area, else its device's."""
    entry = er.async_get(hass).async_get(entity_id)
    if entry is None:
        return None
    if entry.area_id:
        return entry.area_id
    if entry.device_id is None:
        return None
    device = dr.async_get(hass).async_get(entry.device_id)
    return None if device is None else device.area_id


def _celsius_of(hass: HomeAssistant, entity_id: str) -> float | None:
    state = hass.states.get(entity_id)
    if state is None:
        return None
    if entity_id.startswith("weather."):
        return to_celsius(state.attributes.get("temperature"), state.attributes.get("temperature_unit"))
    return to_celsius(state.state, state.attributes.get("unit_of_measurement"))


def _float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def zone_kind(attributes: Mapping[str, Any]) -> str:
    """An air-to-air heat pump has fan or swing modes; an electric floor thermostat has neither."""
    return KIND_AIR if attributes.get("fan_modes") or attributes.get("swing_modes") else KIND_FLOOR


def _power_w(hass: HomeAssistant, entity_id: str) -> float | None:
    """A power sensor on the climate entity's own device (W), where it has one."""
    registry = er.async_get(hass)
    entry = registry.async_get(entity_id)
    if entry is None or entry.device_id is None:
        return None
    for other in er.async_entries_for_device(registry, entry.device_id):
        if not other.entity_id.startswith("sensor.") or (other.device_class or other.original_device_class) != "power":
            continue
        state = hass.states.get(other.entity_id)
        value = None if state is None else _float(state.state)
        if value is not None:
            return value * 1000.0 if state.attributes.get("unit_of_measurement") == "kW" else value
    return None


def _heating_now(state: Any) -> bool | None:
    if state is None or state.state in ("unavailable", "unknown"):
        return None
    action = state.attributes.get("hvac_action")
    if action is not None:
        return action in ("heating", "preheating")
    return None if state.state == "off" else None


def read_zones(hass: HomeAssistant, config: HeatConfig, control: Any = None) -> tuple[ZoneReading, ...]:
    """The zones as they read now, each with SpotNav's control of it (`ZonesControl.status`) where `control` is
    given."""
    zones: list[ZoneReading] = []
    for zone in config.zones:
        state = hass.states.get(zone.entity_id)
        attributes = {} if state is None else state.attributes
        available = state is not None and state.state not in ("unavailable", "unknown")
        zones.append(
            ZoneReading(
                entity_id=zone.entity_id,
                name=_name(hass, zone.entity_id),
                kind=zone_kind(attributes),
                offset_c=zone.offset_c,
                current_c=_float(attributes.get("current_temperature")),
                setpoint_c=_float(attributes.get("temperature")),
                hvac_mode=None if state is None else state.state,
                heating=_heating_now(state),
                power_w=_power_w(hass, zone.entity_id),
                available=available,
                control=None if control is None else control.status(zone.entity_id, config),
            )
        )
    return tuple(zones)


def read_outdoor(hass: HomeAssistant, config: HeatConfig, controller: Any, pump: HeatPumpFacts | None) -> Outdoor | None:
    """The chosen outdoor sensor, else the house forecast's own (the heat pump's sensor or the weather), else the
    heat pump's reading."""
    if config.outdoor_sensor is not None:
        return Outdoor(_celsius_of(hass, config.outdoor_sensor), config.outdoor_sensor, _name(hass, config.outdoor_sensor), "setting")
    forecast = controller.house_forecast
    value = forecast.outdoor_c()
    if value is not None:
        entity_id = forecast.outdoor_entity_id if forecast.outdoor_source == "sensor" else forecast.weather_entity_id
        return Outdoor(value, entity_id, None if entity_id is None else _name(hass, entity_id), "house_forecast")
    if pump is not None and pump.outdoor_temp_c is not None:
        return Outdoor(pump.outdoor_temp_c, pump.anchor_entity_id, pump.label, "pump")
    return None


async def _history(hass: HomeAssistant, entity_ids: tuple[str, ...], start: datetime, end: datetime) -> dict[str, list[tuple[datetime, float]]]:
    """The sensors' readings in `[start, end]` with the one in force at `start`, from the recorder (its executor)."""
    if not entity_ids:
        return {}
    if "recorder" not in hass.config.components:
        return {}
    from homeassistant.components.recorder import get_instance, history  # noqa: PLC0415 - only when loaded

    def read() -> dict[str, list[Any]]:
        return history.get_significant_states(
            hass, start, end, list(entity_ids), significant_changes_only=False, include_start_time_state=True
        )

    rows = await get_instance(hass).async_add_executor_job(read)
    out: dict[str, list[tuple[datetime, float]]] = {}
    for entity_id, states in rows.items():
        series: list[tuple[datetime, float]] = []
        for state in states:
            value = to_celsius(getattr(state, "state", None), getattr(state, "attributes", {}).get("unit_of_measurement"))
            changed = getattr(state, "last_changed", None)
            if value is not None and isinstance(changed, datetime):
                series.append((changed, value))
        out[entity_id] = series
    return out


async def _cached(hass: HomeAssistant, key: tuple[Any, ...], mark: Any, read: Any) -> Any:
    """`read()`, kept per site and kind until `mark` changes (one entry per site and kind)."""
    cache: dict[tuple[Any, ...], tuple[Any, Any]] = hass.data.setdefault(_CACHE, {})
    held = cache.get(key)
    if held is not None and held[0] == mark:
        return held[1]
    value = await read()
    cache[key] = (mark, value)
    return value


@callback
def forget_cached_heat(hass: HomeAssistant, site_entry_id: str) -> None:
    """Drop a site's cached reads (its unload)."""
    cache: dict[tuple[Any, ...], Any] | None = hass.data.get(_CACHE)
    if cache:
        for key in [key for key in cache if key[0] == site_entry_id]:
            del cache[key]


async def read_indoor(hass: HomeAssistant, site_id: str, config: HeatConfig, zones: tuple[ZoneReading, ...], now: datetime) -> Indoor:
    sensors = config.indoor_sensors
    mark = (sensors, int(now.timestamp() // ROOM_REFRESH_S))
    try:
        histories = await _cached(
            hass, (site_id, "rooms"), mark, lambda: _history(hass, sensors, now - WINDOW - timedelta(minutes=5), now)
        )
    except Exception:  # noqa: BLE001 - without the recorder no sensor is seen rising: all of them count
        _LOGGER.debug("SpotNav heat: the indoor sensors' history could not be read", exc_info=True)
        histories = {}
    readings = [
        SensorReading(
            entity_id,
            _name(hass, entity_id),
            _celsius_of(hass, entity_id),
            tuple(histories.get(entity_id, ())),
            _area(hass, entity_id),
            config.offset_of(entity_id),
        )
        for entity_id in sensors
    ]
    heating_areas = frozenset(
        area
        for zone in zones
        if zone.kind == KIND_AIR and zone.heating
        if (area := _area(hass, zone.entity_id)) is not None
    )
    return indoor(readings, now, heating_areas)


async def async_temperature_means(
    hass: HomeAssistant, entity_ids: tuple[str, ...], start: datetime, end: datetime, period: str
) -> dict[str, dict[datetime, float]]:
    """The temperature sensors' statistics means (°C) per period start in `[start, end)`, in the recorder's executor."""
    if not entity_ids or end <= start:
        return {}
    if "recorder" not in hass.config.components:
        return {}
    from homeassistant.components.recorder import get_instance  # noqa: PLC0415 - only when loaded
    from homeassistant.components.recorder.statistics import statistics_during_period  # noqa: PLC0415

    def read() -> dict[str, list[dict[str, Any]]]:
        return statistics_during_period(hass, start, end, set(entity_ids), period, {"temperature": "°C"}, {"mean"})

    rows = await get_instance(hass).async_add_executor_job(read)
    return {entity_id: means_by_start(series) for entity_id, series in rows.items()}


async def read_indoor_day(
    hass: HomeAssistant, site_id: str, config: HeatConfig, now: datetime, day_start: datetime, day_end: datetime
) -> dict[str, Any] | None:
    """Today's indoor curve up to now (`heat/indoor_day.py`) from the sensors' 5-minute statistics, read again once
    per new 5-minute row; `None` without indoor sensors."""
    if not config.indoor_sensors:
        return None
    sensors = config.indoor_sensors
    try:
        readings = await _cached(
            hass,
            (site_id, "indoor_day"),
            (sensors, day_start, five_slot(now)),
            lambda: async_temperature_means(hass, sensors, day_start, now, "5minute"),
        )
    except Exception:  # noqa: BLE001 - without the recorder the curve is empty, the rest is shown
        _LOGGER.debug("SpotNav heat: the indoor sensors' 5-minute statistics could not be read", exc_info=True)
        readings = {}
    names = {entity_id: _name(hass, entity_id) for entity_id in sensors}
    return indoor_day(config, readings, day_start, day_end, now, LIVE_STEP_MIN, names=names)


def _meter_kw_now(hass: HomeAssistant, entity_id: str | None, kind: str | None) -> float | None:
    if entity_id is None or kind != KIND_POWER:
        return None
    state = hass.states.get(entity_id)
    value = None if state is None else _float(state.state)
    if value is None:
        return None
    unit = state.attributes.get("unit_of_measurement")  # type: ignore[union-attr]
    return value / 1000.0 if unit == "W" else value * 1000.0 if unit == "MW" else value


async def read_meter(hass: HomeAssistant, site_id: str, controller: Any, now: datetime, day_start: datetime) -> MeterInputs | None:
    heat = controller.house_forecast.heat
    meter = heat.meter
    if meter.status == STATUS_NONE:
        return None
    periods: dict[datetime, float] = {}
    if meter.found:
        start = min(day_start, now - timedelta(hours=24))
        try:
            periods = await _cached(
                hass,
                (site_id, "meter"),
                (meter.entity_id, five_slot(now)),
                lambda: async_heat_hours(hass, meter, start, now, "5minute"),
            )
        except Exception:  # noqa: BLE001 - without the 5-minute rows there is no split, the rest is shown
            _LOGGER.debug("SpotNav heat: the heat meter's 5-minute rows could not be read", exc_info=True)
            periods = {}
    window_kw = {start: kwh * 12.0 for start, kwh in periods.items()}
    found = levels(window_kw)
    last = window_kw[max(window_kw)] if window_kw else None
    day_end = day_start + timedelta(days=1, hours=1)
    split = split_day(window_kw, day_start, day_end) if periods else None
    zone_periods: dict[datetime, float] | None = None
    zones = await heat.async_current_zones()
    if meter.found and zones.sources:
        try:
            zone_periods = summed_periods(
                await _cached(
                    hass,
                    (site_id, "zones"),
                    (zones.signature(), five_slot(now)),
                    lambda: async_zone_periods(hass, zones, day_start, now, "5minute", fuse_kw(controller)),
                )
            )
        except Exception:  # noqa: BLE001 - without the zones' rows today is the heat pump's alone
            _LOGGER.debug("SpotNav heat: the heat zones' 5-minute rows could not be read", exc_info=True)
    state = None if meter.entity_id is None else hass.states.get(meter.entity_id)
    return MeterInputs(
        status=meter.status,
        entity_id=meter.entity_id,
        name=None if state is None else state.name,
        source=meter.source,
        setting_offered=needs_setting(heat.detected) or meter.source == SOURCE_SETTING,
        candidates=tuple(meter.candidates),
        kw_now=_meter_kw_now(hass, meter.entity_id, meter.kind),
        last_period_kw=last,
        levels=found,
        split=split,
        periods={start: kwh for start, kwh in periods.items() if start >= day_start},
        zone_periods=zone_periods,
    )


def _car_hours(hass: HomeAssistant, controller: Any, measured: Mapping[datetime, float]) -> frozenset[datetime]:
    """Hours a car charged (measured, over 0.1 kWh) or is planned to (an installed plan's window)."""
    hours = {start for start, kwh in measured.items() if kwh > 0.1}
    try:
        windows = site_car_windows(hass, controller)
    except Exception:  # noqa: BLE001 - the band shows the rest
        _LOGGER.debug("car_hours: ignored failure (the band shows the rest)", exc_info=True)
        windows = []
    for start, end, _kw in windows:
        moment = start.replace(minute=0, second=0, microsecond=0)
        while moment < end:
            hours.add(moment)
            moment += timedelta(hours=1)
    return frozenset(hours)


def relay_inputs(hass: HomeAssistant, shadow: Any, now: datetime, day_start: datetime, zone_info: Any) -> RelayInputs | None:
    """The relay's part of the answer from the heat shadow (`heat/runtime.py`), `None` without a relay."""
    if shadow.relay_id is None:
        return None
    closed, since = shadow._relay_now()  # noqa: SLF001 - the shadow's own read of the relay now
    source, automation = shadow.relay_source()
    hours = day_hours(day_start, zone_info)
    return RelayInputs(
        entity_id=shadow.relay_id,
        name=_name(hass, shadow.relay_id),
        closed=closed,
        since=since,
        source=source,
        automation=None if automation is None else (automation.entity_id, automation.name),
        minutes=shadow.relay_minutes(hours, now),
        effect=shadow.effect,
    )


def _choice(hass: HomeAssistant, entity_id: str, **extra: Any) -> dict[str, Any]:
    state = hass.states.get(entity_id)
    value = None
    if state is not None and state.state not in ("unavailable", "unknown"):
        unit = state.attributes.get("unit_of_measurement")
        value = f"{state.state} {unit}" if unit else state.state
    return {"entity_id": entity_id, "name": _name(hass, entity_id), "value": value, **extra}


#: A climate is left out of the zones as a pool heat pump from this score: a known integration or a pool brand's
#: manufacturer (0.8 and up). Below it the pool detection lists any thermostat with a pool-like range (0.1 per
#: support, 0.2 offered), which would leave out every ordinary thermostat. A spa and a sauna are named or a
#: known integration's at any score, so every candidate they list counts.
POOL_PUMP_SURE: Final = 0.8


def leisure_climates(hass: HomeAssistant, config: HeatConfig) -> set[str]:
    """The climates and water heaters that are the pool's heat pump, a spa or a sauna: the ones the pool, spa and sauna
    detection would offer (any candidate they list, `ask` or not: the lowest score they list is a named, ranged one
    and a spa is not a room) and the ones chosen in the pool, spa and sauna config. They are never suggested as a
    zone or the setpoint; one the person already saved stays (`heat_choices` lists what is chosen first)."""
    out: set[str] = set()
    pool, spa, sauna = config.pool, config.spa, config.sauna
    if pool is not None and pool.heat_pump is not None:
        out.update(i for i in (pool.heat_pump.entity_id, pool.heat_pump.switch_id, pool.heat_pump.number_id) if i)
    if spa is not None:
        out.add(spa.entity_id)
    if sauna is not None:
        out.add(sauna.entity_id)
    try:
        seen = read_seen(hass)
        out.update(c.entity_id for c in heat_pumps(seen) if c.score >= POOL_PUMP_SURE)
        out.update(c.entity_id for c in spas(seen))
        out.update(c.entity_id for c in saunas(seen))
    except Exception:  # noqa: BLE001 - detection is extra: the choices stand without it
        _LOGGER.debug("SpotNav heat: the pool, spa and sauna could not be detected for the zones", exc_info=True)
    return out


def heat_choices(hass: HomeAssistant, config: HeatConfig, pump: HeatPumpFacts | None, meter: MeterInputs | None) -> dict[str, Any]:
    """What each setting may be set to, for an administrator (see `heat/view.py`): Home Assistant's entities that fit,
    never a configuration or diagnostic entity nor SpotNav's own, and always what is chosen now."""
    registry = er.async_get(hass)

    def visible(entity_id: str) -> bool:
        entry = registry.async_get(entity_id)
        return entry is None or (
            entry.platform != DOMAIN
            and entry.entity_category is None
            and entry.disabled_by is None
            and entry.hidden_by is None
        )

    leisure = leisure_climates(hass, config)
    groups: dict[str, list[str]] = {
        name: [] for name in ("indoor", "outdoor", "zones", "relay", "offset", "setpoint", "hot_water", "meter", "water_heater", "tank_sensor")
    }
    for state in hass.states.async_all():
        entity_id = state.entity_id
        if not visible(entity_id):
            continue
        domain = entity_id.split(".", 1)[0]
        if domain == "sensor" and is_temperature(hass, entity_id):
            value = _celsius_of(hass, entity_id)
            if value is not None and INDOOR_CHOICE[0] <= value <= INDOOR_CHOICE[1]:
                groups["indoor"].append(entity_id)
            groups["outdoor"].append(entity_id)
            groups["tank_sensor"].append(entity_id)
        elif domain == "weather":
            groups["outdoor"].append(entity_id)
        elif domain == "climate" and "heat" in (state.attributes.get("hvac_modes") or ()):
            if entity_id in leisure:
                continue
            if state.state != "unavailable":
                groups["zones"].append(entity_id)
            groups["setpoint"].append(entity_id)
        elif domain in ("switch", "input_boolean"):
            groups["relay"].append(entity_id)
            groups["hot_water"].append(entity_id)
            groups["water_heater"].append(entity_id)
        elif domain in ("button", "input_button"):
            groups["hot_water"].append(entity_id)
        elif domain in ("number", "input_number") and state.attributes.get("unit_of_measurement") in TEMPERATURE_UNITS:
            groups["offset"].append(entity_id)
        elif domain == "sensor" and meter is not None and meter.setting_offered:
            kind = sensor_kind(hass, entity_id)
            if kind == KIND_POWER and (HEAT_WORDS.search(state.name or "") or entity_id in meter.candidates):
                groups["meter"].append(entity_id)
    chosen = {
        "indoor": list(config.indoor_sensors),
        "outdoor": [config.outdoor_sensor] if config.outdoor_sensor else [],
        "zones": [zone.entity_id for zone in config.zones],
        "relay": [e for e in (config.control_entity, config.control_entity_b) if e and config.control_path in ("relay", "sg_ready", "sg_ready_api")],
        "offset": [config.control_entity] if config.control_path == "offset" and config.control_entity else [],
        "setpoint": [config.control_entity] if config.control_path == "setpoint" and config.control_entity else [],
        "hot_water": [config.hot_water_entity] if config.hot_water_entity else [],
        "meter": [meter.entity_id] if meter is not None and meter.entity_id else [],
        "water_heater": [config.water_heater.entity_id] if config.water_heater is not None else [],
        "tank_sensor": [config.water_heater.tank_sensor] if config.water_heater is not None and config.water_heater.tank_sensor else [],
    }
    if pump is not None:
        for capability in pump.capabilities:
            group = {"offset": "offset", "setpoint": "setpoint", "hot_water_boost": "hot_water", "sg_ready": "relay"}.get(capability.kind)
            if group is not None:
                chosen[group].append(capability.entity_id)
    out: dict[str, Any] = {}
    for name, found in groups.items():
        ids = list(dict.fromkeys([*chosen[name], *found]))
        if name == "meter" and (meter is None or not meter.setting_offered):
            out[name] = None
            continue
        items = [
            _choice(hass, entity_id, **({"kind": zone_kind((hass.states.get(entity_id).attributes if hass.states.get(entity_id) else {}))} if name == "zones" else {}))  # type: ignore[union-attr]
            for entity_id in ids
        ]
        out[name] = sorted(items, key=lambda item: (item["entity_id"] not in chosen[name], str(item["name"]).casefold()))
    try:
        out.update(pool_spa_choices(hass, config))
    except Exception:  # noqa: BLE001 - the pool's and the spa's detection is extra: the other choices stand
        _LOGGER.debug("SpotNav heat: the pool and spa could not be detected", exc_info=True)
        out.update({"pool": None, "spa": [], "sauna": [], "phones": encoded_available(available_targets(hass))})
    return out


def pool_spa_choices(hass: HomeAssistant, config: HeatConfig) -> dict[str, Any]:
    """The pool's, the spa's, the sauna's and the heat card's phones' choices (spec section 3): ranked candidates (`score`, `ask`
    below 0.9: asked once), the one-time grouping the pool block is filled from (`suggested`), what is chosen now
    always listed first. `pool` is `null` with nothing to offer and no pool chosen."""
    seen = read_seen(hass)
    pool = config.pool
    pumps = {c.entity_id: c for c in heat_pumps(seen)}
    if pool is not None and pool.heat_pump is not None and pool.heat_pump.entity_id not in pumps:
        pumps[pool.heat_pump.entity_id] = Candidate(
            pool.heat_pump.entity_id, 0.0, pool.heat_pump.shape, pool.heat_pump.switch_id, pool.heat_pump.number_id
        )
    device = next(iter(pumps.values())).device_id if pumps else None

    def rows(found: list[Candidate], chosen: str | None, *, full: bool) -> list[dict[str, Any]]:
        listed = {c.entity_id: c for c in found}
        if chosen is not None and chosen not in listed:
            listed[chosen] = Candidate(chosen, 0.0)
        items = [
            {**_choice(hass, c.entity_id), **({k: v for k, v in c.as_dict().items() if k != "entity_id"} if full else {"score": round(c.score, 2), "ask": c.ask})}
            for c in listed.values()
        ]
        return sorted(items, key=lambda item: (item["entity_id"] != chosen, -item["score"], str(item["name"]).casefold()))

    chosen_pump = None if pool is None or pool.heat_pump is None else pool.heat_pump.entity_id
    pool_block = None
    if pumps or pool is not None:
        pool_block = {
            "heat_pump": rows(list(pumps.values()), chosen_pump, full=True),
            "circulation": rows(circulations(seen, device), None if pool is None else pool.circulation_id, full=False),
            "water_sensor": rows(water_sensors(seen, device), None if pool is None else pool.water_sensor, full=False),
            "suggested": suggest(seen) if pool is None else None,
        }
    found_spas = {c.entity_id: c for c in spas(seen)}
    spa = config.spa
    if spa is not None and spa.entity_id not in found_spas:
        found_spas[spa.entity_id] = SpaCandidate(spa.entity_id, 0.0, spa.family)
    spa_rows = sorted(
        ({**_choice(hass, c.entity_id), **{k: v for k, v in c.as_dict().items() if k != "entity_id"}} for c in found_spas.values()),
        key=lambda item: (spa is None or item["entity_id"] != spa.entity_id, -item["score"], str(item["name"]).casefold()),
    )
    found_saunas = {c.entity_id: c for c in saunas(seen)}
    sauna = config.sauna
    if sauna is not None and sauna.entity_id not in found_saunas:
        found_saunas[sauna.entity_id] = SaunaCandidate(sauna.entity_id, 0.0, sauna.family, lock_id=sauna.lock_id)
    sauna_rows = sorted(
        ({**_choice(hass, c.entity_id), **{k: v for k, v in c.as_dict().items() if k != "entity_id"}} for c in found_saunas.values()),
        key=lambda item: (sauna is None or item["entity_id"] != sauna.entity_id, -item["score"], str(item["name"]).casefold()),
    )
    return {
        "pool": pool_block,
        "spa": spa_rows,
        "sauna": sauna_rows,
        "phones": encoded_available(available_targets(hass)),
    }


async def heat_answer(hass: HomeAssistant, site: ConfigEntry, *, writable: bool) -> dict[str, Any]:
    """The answer for one site, or a `PowerMonthRefusal`."""
    controller = site_controller_for(hass, site.entry_id)
    if site.state is not ConfigEntryState.LOADED or controller is None:
        raise PowerMonthRefusal(ERROR_NO_SITE, "The site is not loaded")
    now = controller.demand.now()
    tz = controller.demand.tracker.tz
    zone_info = time_zones.require_zone(tz)
    local = now.astimezone(zone_info)
    day_start = datetime(local.year, local.month, local.day, tzinfo=zone_info).astimezone(timezone.utc)
    config = stored_config(site)
    pumps = read_heat_pumps(hass)
    pump = pumps[0] if pumps else None
    zones_control = getattr(controller, "zones_control", None)
    try:
        zones = read_zones(hass, config, zones_control)
    except Exception:  # noqa: BLE001 - the zones' control is extra: the zones as read
        _LOGGER.debug("SpotNav heat: the zones' control could not be read", exc_info=True)
        zones = read_zones(hass, config)
    heater_control = getattr(controller, "water_heater_control", None)
    water_heater: dict[str, Any] | None = None
    not_full = False
    if heater_control is not None:
        try:
            water_heater = heater_control.status(config)
            not_full = heater_control.not_full()
        except Exception:  # noqa: BLE001 - the water heater is extra: the card shows the rest
            _LOGGER.debug("SpotNav heat: the water heater could not be read", exc_info=True)
    controlled = frozenset(
        [zone.entity_id for zone in config.zones if zone.control]
        + ([config.water_heater.entity_id] if config.water_heater is not None and config.water_heater.control else [])
    )
    pool_status: dict[str, Any] | None = None
    spa_status: dict[str, Any] | None = None
    sauna_status: dict[str, Any] | None = None
    pool_spa = getattr(controller, "pool_spa", None)
    if pool_spa is not None:
        try:
            pool_status = pool_spa.pool_status(config, now)
            spa_status = pool_spa.spa_status(config, now)
            sauna_status = pool_spa.sauna_status(config, now)
        except Exception:  # noqa: BLE001 - the pool, the spa and the sauna are extra: the card shows the rest
            _LOGGER.debug("SpotNav heat: the pool, the spa or the sauna could not be read", exc_info=True)
    rooms = await read_indoor(hass, site.entry_id, config, zones, now)
    meter = await read_meter(hass, site.entry_id, controller, now, day_start)
    following = local.date() + timedelta(days=1)
    day_end = datetime(following.year, following.month, following.day, tzinfo=zone_info).astimezone(timezone.utc)
    curve = await read_indoor_day(hass, site.entry_id, config, now, day_start, day_end)
    measured_car: Mapping[datetime, float] = {}
    try:
        measured_car = await async_cached_car_hours(hass, site.entry_id, controller) or {}
    except Exception:  # noqa: BLE001 - the band shows the rest
        _LOGGER.debug("SpotNav heat: the cars' hours could not be read", exc_info=True)
    slots = controller.battery_shadow.plan.buy_slots(now)
    source = site_price_source(hass, site.entry_id)
    area = None if source is None else source.area
    shadow = getattr(controller, "heat_shadow", None)
    relay, tight, outdoor_by_hour, kept, review, room_warm = None, frozenset(), {}, {}, None, False
    hot_water: dict[str, Any] | None = None
    automations: tuple[Any, ...] = ()
    control_status, executed, raised = None, {}, None
    control = getattr(controller, "heat_control", None)
    if control is not None:
        try:
            control_status = control.status()
            executed = control.executed_minutes(day_hours(day_start, zone_info), now)
            if raise_entity(config) is not None:
                raised = control.raised_minutes(day_hours(day_start, zone_info), now)
        except Exception:  # noqa: BLE001 - the card shows the rest
            _LOGGER.debug("SpotNav heat: the heat control could not be read", exc_info=True)
    if shadow is not None:
        try:
            relay = relay_inputs(hass, shadow, now, day_start, zone_info)
            tight, outdoor_by_hour = shadow.joint_inputs(now)
            kept = shadow.kept_actions(local.date())
            review = shadow.review_of(local.date() - timedelta(days=1))
            automations = tuple(shadow.automations)
            room_warm = shadow.room_warm.active
        except Exception:  # noqa: BLE001 - the heat shadow is extra: the card shows the rest
            _LOGGER.debug("SpotNav heat: the heat shadow could not be read", exc_info=True)
        try:
            hot_water = shadow.hot_water.view(now, config)
        except Exception:  # noqa: BLE001 - the hot water is extra: the card shows the rest
            _LOGGER.debug("SpotNav heat: the hot water could not be read", exc_info=True)
    answer = heat_view(
        HeatViewInputs(
            site_id=site.entry_id,
            now=now,
            tz=tz,
            writable=writable,
            config=config,
            config_block=config_block(site, hass),
            indoor=rooms,
            outdoor=read_outdoor(hass, config, controller, pump),
            zones=zones,
            pump=pump,
            other_controllers=other_controllers(hass),
            meter=meter,
            prices=tuple((slot.start, slot.end, slot.buy) for slot in slots),
            money=None if area is None else (area.currency, area.major_unit, area.minor_unit),
            car_hours=_car_hours(hass, controller, measured_car),
            choices=heat_choices(hass, config, pump, meter) if writable else None,
            tight=tight,
            outdoor_by_hour=outdoor_by_hour,
            kept=kept,
            relay=relay,
            automations=automations,
            names={entity_id: _name(hass, entity_id) for item in automations for entity_id in item.targets},
            review=review,
            control=control_status,
            executed=executed,
            raised=raised,
            indoor_day=curve,
            room_warm=room_warm,
            hot_water=hot_water,
            water_heater=water_heater,
            controlled=controlled,
            water_heater_not_full=not_full,
            pool=pool_status,
            spa=spa_status,
            sauna=sauna_status,
        )
    )
    return answer


@websocket_api.websocket_command(
    {
        vol.Required("type"): "spotnav/get_heat",
        vol.Optional("api_version"): object,
        vol.Optional("charger_id"): object,
        vol.Optional("site_id"): object,
    }
)
@websocket_api.async_response
async def websocket_get_heat(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """The heat card's view of one site (see `heat/view.py`)."""
    version = msg.get("api_version")
    if type(version) is not int or version != HEAT_API_VERSION:
        send_unsupported_version(connection, msg, HEAT_API_VERSION)
        return
    try:
        site = _site_of_request(hass, msg)
        answer = await heat_answer(hass, site, writable=is_admin(connection))
    except PowerMonthRefusal as refusal:
        connection.send_error(msg["id"], refusal.code, refusal.message)
        return
    connection.send_result(msg["id"], answer)


BOOST_ACTIONS: Final = ("start", "stop")


async def heat_boost_answer(hass: HomeAssistant, site: ConfigEntry, action: Any) -> dict[str, Any]:
    """The Boost button's envelope (see the module)."""
    controller = site_controller_for(hass, site.entry_id)
    shadow = getattr(controller, "heat_shadow", None)
    if action not in BOOST_ACTIONS:
        return {"api_version": HEAT_API_VERSION, "ok": False, "error": ERROR_INVALID_VALUE, "boost": None}
    if shadow is None:
        return {"api_version": HEAT_API_VERSION, "ok": False, "error": "spotnav_not_supported", "boost": None}
    error, boost = await shadow.hot_water.async_press(action)
    if error is None:
        _LOGGER.info("SpotNav hot water: the Boost button's %s", action)
    return {"api_version": HEAT_API_VERSION, "ok": error is None, "error": error, "boost": boost}


@websocket_api.websocket_command(
    {
        vol.Required("type"): "spotnav/heat_boost",
        vol.Optional("api_version"): object,
        vol.Optional("charger_id"): object,
        vol.Optional("site_id"): object,
        vol.Optional("action"): object,
    }
)
@websocket_api.async_response
async def websocket_heat_boost(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """The Boost button (see the module)."""
    version = msg.get("api_version")
    if type(version) is not int or version != HEAT_API_VERSION:
        send_unsupported_version(connection, msg, HEAT_API_VERSION)
        return
    try:
        site = _site_of_request(hass, msg)
    except PowerMonthRefusal as refusal:
        connection.send_error(msg["id"], refusal.code, refusal.message)
        return
    connection.send_result(msg["id"], await heat_boost_answer(hass, site, msg.get("action")))


@callback
def async_setup_heat_api(hass: HomeAssistant) -> None:
    """Register the heat card's commands once for the domain."""
    websocket_api.async_register_command(hass, websocket_update_heat)
    websocket_api.async_register_command(hass, websocket_get_heat)
    websocket_api.async_register_command(hass, websocket_heat_boost)
