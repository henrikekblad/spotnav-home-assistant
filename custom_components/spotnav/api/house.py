"""`spotnav/get_house` (API v1): the house card's view of one site, read-only and in shadow (SpotNav 3.0 preview).

`{api_version: 1, charger_id | site_id}`. Any user who can see the card may read, as the dashboard. The answer is
`site/house_view.py:house_view` (its docstring has the shape), built from what the site already holds: the house
load forecast and the forecast log with its nightly reviews (`site/house_forecast.py`), the sun's forecast raw and
corrected (`site/solar_correction_shadow.py`), and the battery's facts and plan in shadow (`battery/runtime.py`,
`battery/plan_runtime.py`). Nothing is fetched, computed anew or written.

Refusals (WebSocket errors): `spotnav_unsupported_api_version`, `spotnav_charger_required` (neither id), the charger
lookup's codes, `spotnav_unknown_site` and `spotnav_no_site` (the charger is in no site, or the site is not loaded).
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import callback, HomeAssistant

from ..battery.plan_model import DEFAULT_MODE
from ..const import CONF_SOLAR_FORECAST_ENTRIES
from ..pricing.price_source import site_price_source
from ..runtime import site_controller_for
from ..site.house_view import (
    HOUSE_API_VERSION,
    BatteryInputs,
    HouseInputs,
    HouseSources,
    SunInputs,
    house_view,
)
from ..site.house_forecast import BASIS_GROSS
from ..site.solar_correction_shadow import STATUS_NO_FORECAST
from ..battery.shadow import parse_aware_datetime
from .common import send_unsupported_version
from .power_month import ERROR_NO_SITE, PowerMonthRefusal, _site_of_request


def _sun(hass: HomeAssistant, controller: Any) -> SunInputs | None:
    correction = controller.solar_correction
    entries = list(controller.config.get(CONF_SOLAR_FORECAST_ENTRIES) or [])
    if not entries or correction.status == STATUS_NO_FORECAST:
        return None
    raw: dict[Any, float] = {}
    corrected: dict[Any, float] = {}
    for hour in correction.next_hours:
        start = parse_aware_datetime(hour.get("start"))
        if start is not None:
            raw[start] = float(hour["raw_kwh"])
            corrected[start] = float(hour["corrected_kwh"])
    names = []
    for entry_id in entries:
        entry = hass.config_entries.async_get_entry(entry_id)
        if entry is not None:
            names.append(entry.title)
    return SunInputs(status=correction.status, raw=raw, corrected=corrected, forecast_names=tuple(names))


def _battery(controller: Any) -> BatteryInputs | None:
    shadow = controller.battery_shadow
    facts = shadow.facts
    if facts.battery_entity_id is None:
        return None
    plan = shadow.plan
    inputs = plan.inputs or {}
    usable = (inputs.get("battery") or {}).get("usable_kwh")
    horizon = parse_aware_datetime(inputs.get("horizon_end"))
    return BatteryInputs(
        brand=facts.brand,
        label=facts.label,
        soc_pct=facts.soc_pct,
        usable_kwh=usable if isinstance(usable, (int, float)) else facts.capacity_kwh,
        management=facts.management.state,
        status=plan.status,
        reason=plan.reason,
        mode=plan.mode or DEFAULT_MODE,
        made_at=plan.made_at,
        horizon_end=horizon,
        plan=plan.plan,
        mode_views=dict(plan.mode_views),
        reviews=tuple(plan.reviews),
        estimates=tuple(inputs.get("estimates") or ()),
        quarters=tuple(plan.actuals.buckets.values()),
        prices=tuple(plan._prices),  # noqa: SLF001 - the slots the plan read, held between its runs
        production=dict(plan._production),  # noqa: SLF001
        stored=plan.stored,
    )


def house_answer(hass: HomeAssistant, site: Any) -> dict[str, Any]:
    """The answer for one site, or a `PowerMonthRefusal`."""
    controller = site_controller_for(hass, site.entry_id)
    if site.state is not ConfigEntryState.LOADED or controller is None:
        raise PowerMonthRefusal(ERROR_NO_SITE, "The site is not loaded")
    demand = controller.demand
    tracker = demand.tracker
    forecast = controller.house_forecast
    # The house figure the forecast is on: the whole load once it has its hours (a battery or a solar site), else
    # the import less the chargers.
    basis = forecast.basis
    if basis == BASIS_GROSS:
        actual = {hour.start: hour.house_kw for hour in forecast.history.gross_hours() if hour.house_kw is not None}
    else:
        actual = {
            hour.start: hour.house_kw
            for hour in (*tracker.previous, *tracker.hours)
            if hour.house_kw is not None and not hour.imported
        }
    source = site_price_source(hass, site.entry_id)
    area = None if source is None else source.area
    return house_view(
        HouseInputs(
            site_id=site.entry_id,
            now=demand.now(),
            tz=tracker.tz,
            log=forecast.log,
            forecast=forecast.forecast,
            house_actual=actual,
            basis=basis,
            sources=HouseSources(
                weather_entity=forecast.weather_entity_id,
                weather=forecast.weather_status,
                outdoor_source=forecast.outdoor_source,
                outdoor_sensor=forecast.outdoor_entity_id,
                holiday_calendar=forecast.holiday_entity_id,
            ),
            sun=_sun(hass, controller),
            battery=_battery(controller),
            money=None if area is None else (area.currency, area.major_unit, area.minor_unit),
        )
    )


@websocket_api.websocket_command(
    {
        vol.Required("type"): "spotnav/get_house",
        vol.Optional("api_version"): object,
        vol.Optional("charger_id"): object,
        vol.Optional("site_id"): object,
    }
)
@callback
def websocket_get_house(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """The house card's view of one site."""
    version = msg.get("api_version")
    if type(version) is not int or version != HOUSE_API_VERSION:
        send_unsupported_version(connection, msg, HOUSE_API_VERSION)
        return
    try:
        answer = house_answer(hass, _site_of_request(hass, msg))
    except PowerMonthRefusal as refusal:
        connection.send_error(msg["id"], refusal.code, refusal.message)
        return
    connection.send_result(msg["id"], answer)


@callback
def async_setup_house_api(hass: HomeAssistant) -> None:
    """Register the command once for the domain."""
    websocket_api.async_register_command(hass, websocket_get_house)
