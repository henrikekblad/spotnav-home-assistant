"""`spotnav/get_house_now` (API v1): the house card's live flows for its "Now" tab, read-only (SpotNav 3.0).

`{api_version: 1, charger_id | site_id}`, any user who can see the card. The answer is
`site/house_now.py:house_now` (its docstring has the shape), from what the site already reads every recompute: the
net grid power and the chargers' draw as solar reads them (`execution/solar_execution.py`), the battery's facts
(`battery/facts.py`: its power and whether it is read on the DC side) with the inverter's
loss the battery plan knows (`battery/conversion_loss.py`), the battery's power read now, the Energy dashboard's solar
power sensors (`stat_rate`) or else the battery plant's own PV power where the plant is all of the sun
(`plant_is_all_sun`), else each solar source's PV power sensor on its inverter's device (`solar_live_ids`),
and the heat meter's and the heat zones' power sensors (`SiteHeatMeter.live_power_ids`). Without
a power sensor the sun and the heat take the last period of today's energy flow (cached per
5-minute row, `api/house.py:async_cached_day_flow`), so the card polls this every 30 s cheaply.

Refusals as `spotnav/get_house`'s (without `spotnav_invalid_date`).
"""

from __future__ import annotations

import logging
import math
import re
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, Final

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, callback

from ..const import CONF_CHARGER_ENTRY_IDS
from ..runtime import site_controller_for
from ..site.energy_statistics import async_energy_prefs
from ..site.house_now import FROM_POWER, FROM_RECENT, HOUSE_NOW_API_VERSION, HouseNowInputs, house_now, recent_kw
from .common import send_unsupported_version
from .house import async_cached_day_flow, battery_loss
from .power_month import ERROR_NO_SITE, PowerMonthRefusal, _site_of_request

_LOGGER = logging.getLogger(__name__)


def state_kw(hass: HomeAssistant, entity_id: str) -> float | None:
    """A power sensor's state in kW (its unit converted, W when it names none); `None` unread."""
    state = hass.states.get(entity_id)
    if state is None:
        return None
    try:
        value = float(state.state)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value):
        return None
    unit = state.attributes.get("unit_of_measurement") or "W"
    try:
        from homeassistant.util.unit_conversion import PowerConverter  # noqa: PLC0415 - kept beside its only use

        return float(PowerConverter.convert(value, unit, "kW"))
    except Exception:  # noqa: BLE001 - a unit that is no power unit is no reading
        _LOGGER.debug("state_kw: ignored failure (a unit that is no power unit is no reading)", exc_info=True)
        return None


def solar_power_ids(prefs: Any) -> tuple[str, ...]:
    """The Energy dashboard's solar power sensors (`stat_rate` of each solar source), only when every solar source
    has one (a part of the sun is never shown as all of it)."""
    sources = prefs.get("energy_sources") if isinstance(prefs, Mapping) else None
    ids: list[str | None] = []
    for source in sources if isinstance(sources, list) else []:
        if isinstance(source, Mapping) and source.get("type") == "solar":
            rate = source.get("stat_rate")
            ids.append(rate if isinstance(rate, str) and rate else None)
    return tuple(i for i in ids if i) if ids and all(ids) else ()


def plant_is_all_sun(hass: HomeAssistant, prefs: Any, battery_entity_id: str | None) -> bool:
    """Whether the battery plant's own PV power is all of the sun: every solar source's energy statistic is on the
    battery's device (a Sigenergy plant counts its PV with its battery), so no other inverter's sun is left out."""
    sources = prefs.get("energy_sources") if isinstance(prefs, Mapping) else None
    stats = [
        source.get("stat_energy_from")
        for source in (sources if isinstance(sources, list) else [])
        if isinstance(source, Mapping) and source.get("type") == "solar"
    ]
    if not stats or not battery_entity_id or not all(isinstance(stat, str) and stat for stat in stats):
        return False
    from homeassistant.helpers import entity_registry as er  # noqa: PLC0415 - kept beside its only use

    registry = er.async_get(hass)
    battery = registry.async_get(battery_entity_id)
    if battery is None or battery.device_id is None:
        return False
    return all((entry := registry.async_get(stat)) is not None and entry.device_id == battery.device_id for stat in stats)


#: Words that mark a power sensor as something other than the panels' production (a hybrid's battery, grid, load or
#: total output, which includes the battery's discharge): never taken as the sun.
_NOT_PV_WORDS: Final = frozenset(
    {"battery", "bat", "batt", "grid", "load", "consumption", "house", "home", "import", "export", "meter", "output",
     "charge", "discharge", "backup", "eps"}
)
_PV_WORD = re.compile(r"^(pv\d*|solar|photovoltaic)$")
#: Words left out when an energy sensor's name is matched to a power sensor's (`sensor.pv_energy` ↔ `sensor.pv_power`).
_STEM_ENERGY_WORDS: Final = frozenset(
    {"energy", "yield", "production", "produced", "generation", "kwh", "wh", "today", "daily", "total", "lifetime",
     "accumulated", "sum"}
)
_STEM_POWER_WORDS: Final = frozenset({"power", "w", "kw", "active", "current", "now", "instant", "realtime"})
#: A PV power below this (kW) is no production read with a little offset but a sensor of the wrong sign.
_PV_NEGATIVE_KW: Final = -0.05


def _words(entry: Any) -> list[str]:
    text = f"{entry.entity_id.split('.', 1)[-1]} {entry.name or ''} {entry.original_name or ''}".lower()
    return [word for word in re.split(r"[^a-z0-9]+", text) if word]


def _stem(entity_id: str, drop: frozenset[str]) -> tuple[str, ...]:
    return tuple(w for w in re.split(r"[^a-z0-9]+", entity_id.split(".", 1)[-1].lower()) if w and w not in drop)


def solar_live_power_id(hass: HomeAssistant, energy_id: str) -> str | None:
    """The power sensor that tells a solar energy sensor's production now, from the power sensors on its device
    (`site/heat_meter.device_power_sensors`): the one whose name pairs with the energy sensor's
    (`sensor.pv_energy` ↔ `sensor.pv_power`), else the device's only one unless its name says battery, grid, load or
    output, else the only one named pv or solar. `None` where none or more than one could be it: a hybrid's total
    output includes its battery, so it is never guessed."""
    from ..site.heat_meter import device_power_sensors  # noqa: PLC0415 - the site imports this module's neighbours

    found = device_power_sensors(hass, energy_id)
    if not found:
        return None
    stem = _stem(energy_id, _STEM_ENERGY_WORDS)
    named_stem = any(_PV_WORD.match(word) for word in stem)
    paired = [entry for entry in found if named_stem and _stem(entry.entity_id, _STEM_POWER_WORDS) == stem]
    if len(paired) == 1:
        return paired[0].entity_id
    plausible = [entry for entry in found if not _NOT_PV_WORDS.intersection(_words(entry))]
    if len(found) == 1:
        return plausible[0].entity_id if plausible else None
    named = [entry for entry in plausible if any(_PV_WORD.match(word) for word in _words(entry))]
    return named[0].entity_id if len(named) == 1 else None


def solar_live_ids(hass: HomeAssistant, prefs: Any) -> tuple[str, ...]:
    """A power sensor for every solar source: its `stat_rate`, else the one on its energy sensor's device
    (`solar_live_power_id`); empty when a source has none (a part of the sun is never shown as all of it)."""
    sources = prefs.get("energy_sources") if isinstance(prefs, Mapping) else None
    ids: list[str] = []
    for source in sources if isinstance(sources, list) else []:
        if not (isinstance(source, Mapping) and source.get("type") == "solar"):
            continue
        rate = source.get("stat_rate")
        stat = source.get("stat_energy_from")
        found = rate if isinstance(rate, str) and rate else None
        if found is None and isinstance(stat, str) and stat:
            found = solar_live_power_id(hass, stat)
        if found is None:
            return ()
        ids.append(found)
    return tuple(ids)


def sun_kw_of(hass: HomeAssistant, entity_ids: Sequence[str]) -> float | None:
    """The sun now from PV power sensors found on their devices, summed; `None` (the last period is then taken) when
    one is unread, a total rather than a measurement, or of the wrong sign (below `_PV_NEGATIVE_KW`)."""
    total = 0.0
    for entity_id in entity_ids:
        state = hass.states.get(entity_id)
        if state is None or state.attributes.get("state_class") not in (None, "measurement"):
            return None
        value = state_kw(hass, entity_id)
        if value is None or value < _PV_NEGATIVE_KW:
            return None
        total += max(0.0, value)
    return total if entity_ids else None


def summed_kw(hass: HomeAssistant, entity_ids: Sequence[str]) -> float | None:
    values = [state_kw(hass, entity_id) for entity_id in entity_ids]
    return None if not values or any(value is None for value in values) else sum(values)  # type: ignore[misc]


def car_plugged(hass: HomeAssistant, charger_ids: Sequence[str]) -> tuple[bool | None, float | None]:
    """Whether a car is plugged into any of the chargers (`None` when none can be read) and the level of the first
    plugged-in car whose plan's vehicle can be read."""
    from ..execution.charger_connection import DISCONNECTED  # noqa: PLC0415 - kept beside its only use
    from ..runtime import controller_for  # noqa: PLC0415 - kept beside its only use

    plugged: bool | None = None
    soc: float | None = None
    for charger_id in charger_ids:
        controller = controller_for(hass, charger_id)
        if controller is None:
            continue
        try:
            state = controller.connection()[0]
        except Exception:  # noqa: BLE001 - a charger that cannot be read says nothing
            _LOGGER.debug("car_plugged: ignored failure (a charger that cannot be read says nothing)", exc_info=True)
            continue
        if state is None:
            continue
        connected = state != DISCONNECTED
        plugged = bool(plugged) or connected
        if connected and soc is None:
            try:
                reading = controller.target_reading()
            except Exception:  # noqa: BLE001 - without a reading the level stays unknown
                _LOGGER.debug("car_plugged: ignored failure (without a reading the level stays unknown)", exc_info=True)
                reading = None
            soc = None if reading is None else reading.soc_percent
    return plugged, soc


def _recent(periods: Sequence[Any], role: str, now: datetime) -> float | None:
    """The last period's mean power for `role` (`solar` or `heat`), `None` when it is too old or has none."""
    if not periods:
        return None
    last = periods[-1]
    return recent_kw(getattr(last, role, None), last.minutes, last.end, now)


async def async_house_now(hass: HomeAssistant, site: Any) -> dict[str, Any]:
    """The answer for one site, or a `PowerMonthRefusal`."""
    controller = site_controller_for(hass, site.entry_id)
    if site.state is not ConfigEntryState.LOADED or controller is None:
        raise PowerMonthRefusal(ERROR_NO_SITE, "The site is not loaded")
    from ..execution.solar_execution import site_car_w, site_net_grid_w  # noqa: PLC0415 - it imports the site

    now = controller.demand.now()
    grid_w = site_net_grid_w(controller)
    chargers = list(controller.config.get(CONF_CHARGER_ENTRY_IDS) or [])
    car_w = site_car_w(hass, controller, idle_unread_as_zero=True) if chargers else None
    plugged, car_soc = car_plugged(hass, chargers)
    facts = controller.battery_shadow.facts
    has_battery = facts.battery_entity_id is not None
    battery_kw = facts.power_kw if has_battery else None
    if has_battery:
        # Read now, as the grid is, rather than at the battery's last look (every 10 s).
        reading = controller.battery_aggregate_power()
        if reading is not None and reading.value is not None:
            battery_kw = reading.value / 1000.0
    prefs = await async_energy_prefs(hass)
    solar_sources = [
        source
        for source in ((prefs or {}).get("energy_sources") or [] if isinstance(prefs, Mapping) else [])
        if isinstance(source, Mapping) and source.get("type") == "solar"
    ]
    has_sun = bool(solar_sources)
    meter = controller.house_forecast.heat.meter
    zones = await controller.house_forecast.heat.async_current_zones()
    has_heat = bool(meter.found or zones.sources)
    sun_kw = sun_from = heat_kw = heat_from = None
    if has_sun and (power_ids := solar_power_ids(prefs)):
        sun_kw = summed_kw(hass, power_ids)
        sun_from = None if sun_kw is None else FROM_POWER
    if (
        has_sun
        and sun_kw is None
        and facts.plant_pv_kw is not None
        and plant_is_all_sun(hass, prefs, facts.battery_entity_id)
    ):
        # The plant's own PV power, read with its battery every 10 s: live, where the last period may be 20 min old.
        sun_kw, sun_from = facts.plant_pv_kw, FROM_POWER
    if has_sun and sun_kw is None and (found_ids := solar_live_ids(hass, prefs)):
        # Each source's PV power sensor on its inverter's device: live, where the last period may be 20 min old.
        sun_kw = sun_kw_of(hass, found_ids)
        sun_from = None if sun_kw is None else FROM_POWER
    if has_heat and (heat_ids := controller.house_forecast.heat.live_power_ids(zones)):
        heat_kw = summed_kw(hass, heat_ids)
        heat_from = None if heat_kw is None else FROM_POWER
    if (has_sun and sun_kw is None) or (has_heat and heat_kw is None):
        try:
            read = await async_cached_day_flow(hass, site.entry_id, controller)
        except Exception:  # noqa: BLE001 - without the flow the figure stays unread
            _LOGGER.debug("SpotNav house now: the energy flow could not be read", exc_info=True)
            read = None
        periods = () if read is None else read.periods
        if has_sun and sun_kw is None and (sun_kw := _recent(periods, "solar", now)) is not None:
            sun_from = FROM_RECENT
        if has_heat and heat_kw is None and (heat_kw := _recent(periods, "heat", now)) is not None:
            heat_from = FROM_RECENT
    return house_now(
        HouseNowInputs(
            site_id=site.entry_id,
            at=now,
            grid_kw=None if grid_w is None else grid_w / 1000.0,
            sun_kw=sun_kw,
            sun_from=sun_from,
            battery_kw=battery_kw,
            soc_pct=facts.soc_pct if has_battery else None,
            car_kw=None if car_w is None else car_w / 1000.0,
            heat_kw=heat_kw,
            heat_from=heat_from,
            has_sun=has_sun,
            has_battery=has_battery,
            has_car=bool(chargers),
            has_heat=has_heat,
            car_connected=plugged,
            car_soc_pct=car_soc,
            battery_loss=battery_loss(controller) if has_battery else None,
        )
    )


@websocket_api.websocket_command(
    {
        vol.Required("type"): "spotnav/get_house_now",
        vol.Optional("api_version"): object,
        vol.Optional("charger_id"): object,
        vol.Optional("site_id"): object,
    }
)
@websocket_api.async_response
async def websocket_get_house_now(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """The house card's live flows for one site."""
    version = msg.get("api_version")
    if type(version) is not int or version != HOUSE_NOW_API_VERSION:
        send_unsupported_version(connection, msg, HOUSE_NOW_API_VERSION)
        return
    try:
        answer = await async_house_now(hass, _site_of_request(hass, msg))
    except PowerMonthRefusal as refusal:
        connection.send_error(msg["id"], refusal.code, refusal.message)
        return
    connection.send_result(msg["id"], answer)


@callback
def async_setup_house_now_api(hass: HomeAssistant) -> None:
    """Register the command once for the domain."""
    websocket_api.async_register_command(hass, websocket_get_house_now)
