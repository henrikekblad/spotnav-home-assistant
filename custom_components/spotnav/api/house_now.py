"""`spotnav/get_house_now` (API v1): the house card's live flows for its "Now" tab, read-only (SpotNav 3.0).

`{api_version: 1, charger_id | site_id}`, any user who can see the card. The answer is
`site/house_now.py:house_now` (its docstring has the shape), from what the site already reads every recompute: the
net grid power and the chargers' draw as solar reads them (`execution/solar_execution.py`), the battery's facts
(`battery/facts.py`), the Energy dashboard's solar power sensors (`stat_rate`) and the heat pump's meter where it is a
power sensor. Without a power sensor the sun and the heat take the last period of today's energy flow (cached per
5-minute row, `api/house.py:async_cached_day_flow`), so the card polls this every 30 s cheaply.

Refusals as `spotnav/get_house`'s (without `spotnav_invalid_date`).
"""

from __future__ import annotations

import logging
import math
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, callback

from ..const import CONF_CHARGER_ENTRY_IDS
from ..runtime import site_controller_for
from ..site.energy_statistics import async_energy_prefs
from ..site.heat_meter import KIND_POWER
from ..site.house_now import FROM_POWER, FROM_RECENT, HOUSE_NOW_API_VERSION, HouseNowInputs, house_now, recent_kw
from .common import send_unsupported_version
from .house import async_cached_day_flow
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


def summed_kw(hass: HomeAssistant, entity_ids: Sequence[str]) -> float | None:
    values = [state_kw(hass, entity_id) for entity_id in entity_ids]
    return None if not values or any(value is None for value in values) else sum(values)  # type: ignore[misc]


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
    facts = controller.battery_shadow.facts
    has_battery = facts.battery_entity_id is not None
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
    if has_heat and meter.found and not zones.sources and meter.kind == KIND_POWER and meter.entity_id:
        heat_kw = state_kw(hass, meter.entity_id)
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
            battery_kw=facts.power_kw if has_battery else None,
            soc_pct=facts.soc_pct if has_battery else None,
            car_kw=None if car_w is None else car_w / 1000.0,
            heat_kw=heat_kw,
            heat_from=heat_from,
            has_sun=has_sun,
            has_battery=has_battery,
            has_car=bool(chargers),
            has_heat=has_heat,
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
