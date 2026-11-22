"""`spotnav/get_plan` (API v1): the house card's Plan tab, every load SpotNav plans for one site on one rolling time
axis against the ceiling (plan `plans/v3_flexible_loads_design.md`, step 2). Read-only.

`{api_version: 1, charger_id | site_id}`; any user who can see the card may read, as `spotnav/get_house`. The answer
is `site/plan_view.py:plan_view` (its docstring has the shape), built only from what the plans already hold: the
cars' installed plans (`ChargingPlan`), the battery plan in force (`battery_shadow.plan.plan`), the heat plan
(`heat_shadow.plan`), the water heater's windows (`WaterHeaterControl.status`), the zones' lowerings
(`ZonesControl.lowerings_ahead`), the house forecast with the heat plan's change (`heat_delta`), the prices with
their fees as the plans read them (`buy_slots`), the import target's hold per hour (`site_hour_caps`), the soft
ceiling under the demand charge (`demand_ceiling`), the main fuse, the month's measured hours
and each car's "does not fit" block (`site/does_not_fit.py`, from the import target's status). The one thing read
is the solar forecast (the integrations' own forecast platform, as the battery plan reads it, then corrected as the
plan corrects it). Nothing is planned anew and nothing is written. A part that cannot be read is left out, never the
whole answer.

Refusals (WebSocket errors) as `spotnav/get_house`: `spotnav_unsupported_api_version`, `spotnav_charger_required`,
the charger lookup's codes, `spotnav_unknown_site` and `spotnav_no_site`.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, callback

from ..const import CONF_CHARGER_ENTRY_IDS, CONF_SOLAR_FORECAST_ENTRIES
from ..core.electric import site_fuse_kw, stored_voltage_between_phases_v
from ..core.parse import aware_instant
from ..pricing.price_source import site_price_source
from ..runtime import controller_for, site_controller_for
from ..site.flexible_loads import has_power_tariff
from ..site.house_forecast import BASIS_GROSS
from ..site.does_not_fit import site_does_not_fit
from ..site.import_target import stored_import_target_kw
from ..site.plan_view import (
    CEILING_IMPORT_TARGET,
    CEILING_POWER_LEVEL,
    PLAN_API_VERSION,
    BatteryPlan,
    CarPlan,
    HeatPlan,
    PlanInputs,
    PriceHour,
    WaterHeaterPlan,
    ZonePlan,
    plan_view,
)
from .common import send_unsupported_version
from .power_month import ERROR_NO_SITE, PowerMonthRefusal, _site_of_request

_LOGGER = logging.getLogger(__name__)


def _guarded(what: str, read: Any, default: Any) -> Any:
    """`read()`, or `default` when it fails: the view shows the rest."""
    try:
        return read()
    except Exception:  # noqa: BLE001 - a part that cannot be read is left out, never the whole answer
        _LOGGER.debug("SpotNav plan view: the %s could not be read", what, exc_info=True)
        return default


def _name(hass: HomeAssistant, entity_id: str) -> str | None:
    state = hass.states.get(entity_id)
    return None if state is None else state.name


def _prices(controller: Any, now: datetime) -> tuple[PriceHour, ...]:
    return tuple(PriceHour(s.start, s.end, s.buy, s.sell) for s in controller.battery_shadow.plan.buy_slots(now))


def _house(controller: Any, now: datetime) -> tuple[dict[datetime, float], str, bool]:
    """The house forecast per hour with the heat plan's change (as the plans count on it), its basis, and whether
    its hours have the sun's production taken off."""
    forecast = controller.house_forecast
    held = forecast.forecast
    by_hour = {} if held is None else dict(held.by_hour())
    delta = forecast.heat_delta
    if delta is not None and by_hour:
        change = _guarded("heat plan's change", lambda: delta(now), {})
        by_hour = {start: max(0.0, kw + change.get(start, 0.0)) for start, kw in by_hour.items()}
    basis = forecast.basis
    with_sun = basis == BASIS_GROSS and bool(_guarded("house's rows", lambda: forecast.history.gross_with_sun(), False))
    return by_hour, basis, not with_sun


async def _sun(hass: HomeAssistant, controller: Any, now: datetime) -> dict[datetime, float] | None:
    """The sun's forecast per hour (kW) as the battery plan counts on it: read, then corrected once learnt."""
    from ..battery.plan_runtime import corrected_solar_wh  # noqa: PLC0415 - it imports the site
    from ..planning.hybrid_forecast import async_read_forecast_wh  # noqa: PLC0415

    entries = list(controller.config.get(CONF_SOLAR_FORECAST_ENTRIES) or [])
    if not entries:
        return None
    try:
        read = await async_read_forecast_wh(hass, entries, now=now)
        if read.wh_hours is None:
            return None
        correction = controller.solar_correction
        wh, _basis = corrected_solar_wh(
            read.wh_hours, correction.state, observed_max_kwh=correction.observed_max_kwh, array_kwp=correction.array_kwp
        )
    except Exception:  # noqa: BLE001 - without the sun the view shows the rest
        _LOGGER.debug("SpotNav plan view: the solar forecast could not be read", exc_info=True)
        return None
    return {start: value / 1000.0 for start, value in wh.items()}


def _cars(hass: HomeAssistant, controller: Any) -> tuple[CarPlan, ...]:
    """Each member charger's installed plan, at the power the plans count on (`plan_windows`)."""
    from ..battery.plan_runtime import plan_windows  # noqa: PLC0415 - it imports the site

    volts = stored_voltage_between_phases_v(controller.config)
    found: list[CarPlan] = []
    for charger_entry_id in controller.config.get(CONF_CHARGER_ENTRY_IDS) or []:
        charger = controller_for(hass, charger_entry_id)
        if charger is None:
            continue
        plan = getattr(charger, "plan", None)
        entry = hass.config_entries.async_get_entry(charger_entry_id)
        found.append(
            CarPlan(
                id=charger_entry_id,
                name=None if entry is None else entry.title,
                windows=tuple(plan_windows(plan, volts)),
                live=bool(charger.is_commandable or charger.is_switch_only),
                departure=None if plan is None else aware_instant(plan.departure),
                does_not_fit=_guarded("does-not-fit", lambda c=charger_entry_id: site_does_not_fit(controller, c), None),
            )
        )
    return tuple(found)


def _battery(controller: Any) -> BatteryPlan | None:
    facts = controller.battery_shadow.facts
    if facts.battery_entity_id is None:
        return None
    control = getattr(controller, "battery_control", None)
    state = None if control is None else _guarded("battery control's state", control.state, None)
    slots = tuple((s.start, s.end, s.action, s.battery_kw) for s in controller.battery_shadow.plan.plan)
    return BatteryPlan(id=facts.battery_entity_id, name=facts.label, slots=slots, live=state == "on")


def _heat(hass: HomeAssistant, controller: Any) -> tuple[HeatPlan | None, tuple[ZonePlan, ...], WaterHeaterPlan | None]:
    from ..heat.shadow_rule import ACTION_LOWER  # noqa: PLC0415 - the heat side imports the site
    from ..heat.zones_control import STATE_RUNNING  # noqa: PLC0415

    shadow = getattr(controller, "heat_shadow", None)
    if shadow is None:
        return None, (), None
    config = shadow.config()
    band = list(shadow.plan or ())
    heat = None
    pump_id = config.control_entity or shadow.relay_id
    if band and pump_id:
        control = getattr(controller, "heat_control", None)
        status = None if control is None else _guarded("heat control", control.status, None)
        live = bool(status and status.get("enabled") and status.get("state") == "running")
        effect = getattr(shadow, "effect", None)
        heat = HeatPlan(
            id=pump_id,
            name=_name(hass, pump_id),
            hours=tuple((hour.start, hour.action, hour.reason) for hour in band),
            live=live,
            heat_kw=_guarded("heat pump's heat", shadow.heat_kw, lambda _hour: None),
            lower_share=0.0 if effect is None else float(effect.lower_share),
        )
    zones: list[ZonePlan] = []
    zones_control = getattr(controller, "zones_control", None)
    planned_lower = tuple((hour.start, hour.start + timedelta(hours=1)) for hour in band if hour.action == ACTION_LOWER)
    now = controller.demand.now()
    for zone in config.zones:
        state = None if zones_control is None else zones_control.zones.get(zone.entity_id)
        running = zone.control and state is not None and state.state == STATE_RUNNING
        lowered = (
            tuple(_guarded("zone's lowerings", lambda s=state: zones_control.lowerings_ahead(s, now), ()))
            if running
            else planned_lower
        )
        zones.append(ZonePlan(id=zone.entity_id, name=_name(hass, zone.entity_id), lowered=lowered, live=running))
    heater = None
    heater_control = getattr(controller, "water_heater_control", None)
    status = None if heater_control is None else _guarded("water heater", lambda: heater_control.status(config), None)
    if status is not None:
        windows = []
        for window in status.get("windows") or ():
            start, end = aware_instant(window.get("start")), aware_instant(window.get("end"))
            if start is not None and end is not None:
                windows.append((start, end, str(window.get("reason"))))
        kw = status.get("kw")
        heater = WaterHeaterPlan(
            id=status["entity_id"],
            name=status.get("name"),
            windows=tuple(windows),
            kw=float(kw) if isinstance(kw, (int, float)) else None,
            live=bool(status.get("live")),
        )
    return heat, tuple(zones), heater


def _soft(controller: Any) -> dict[datetime, float]:
    ceiling = getattr(controller, "demand_ceiling", None)
    if ceiling is None:
        return {}
    out: dict[datetime, float] = {}
    for row in ceiling.as_dict().get("hours") or ():
        start = aware_instant(row[0])
        if start is not None and isinstance(row[1], (int, float)):
            out[start] = float(row[1])
    return out


def _import_target_kw(controller: Any) -> float | None:
    """The import target as the hold reads it: from the site's entry, written without a reload (the controller's copy
    of the config may be older)."""
    read = getattr(controller, "_import_target_kw", None)
    return read() if callable(read) else stored_import_target_kw(controller.config)


def _month_peak(controller: Any, now: datetime) -> tuple[float, datetime] | None:
    hours = controller.demand.tracker.month_hours(now)
    if not hours:
        return None
    top = max(hours, key=lambda hour: hour.kw)
    return top.kw, top.start


async def async_plan_answer(hass: HomeAssistant, site: Any) -> dict[str, Any]:
    """The answer for one site, or a `PowerMonthRefusal`."""
    from ..battery.plan_runtime import site_hour_caps  # noqa: PLC0415 - it imports the site

    controller = site_controller_for(hass, site.entry_id)
    if site.state is not ConfigEntryState.LOADED or controller is None:
        raise PowerMonthRefusal(ERROR_NO_SITE, "The site is not loaded")
    now = controller.demand.now()
    tz = controller.demand.tracker.tz
    source = site_price_source(hass, site.entry_id)
    area = None if source is None else source.area
    house, basis, sun_in_house = _guarded("house forecast", lambda: _house(controller, now), ({}, "net", True))
    heat, zones, heater = _guarded("heat plan", lambda: _heat(hass, controller), (None, (), None))
    config = controller.config
    return plan_view(
        PlanInputs(
            site_id=site.entry_id,
            now=now,
            tz=tz,
            prices=_guarded("prices", lambda: _prices(controller, now), ()),
            money=None if area is None else (area.currency, area.major_unit, area.minor_unit),
            house=house,
            house_basis=basis,
            sun_in_house=sun_in_house,
            sun=await _sun(hass, controller, now),
            cars=_guarded("cars' plans", lambda: _cars(hass, controller), ()),
            battery=_guarded("battery plan", lambda: _battery(controller), None),
            heat=heat,
            zones=zones,
            water_heater=heater,
            hold_kw=_guarded("import target's hold", lambda: site_hour_caps(hass, controller, now), {}),
            hold_kind=CEILING_IMPORT_TARGET if _import_target_kw(controller) is not None else CEILING_POWER_LEVEL,
            soft_kw=_guarded("soft ceiling", lambda: _soft(controller), {}),
            fuse_kw=site_fuse_kw(config),
            power_tariff=bool(_guarded("power tariff", lambda: has_power_tariff(controller), False)),
            month_peak=_guarded("month's peak", lambda: _month_peak(controller, now), None),
        )
    )


@websocket_api.websocket_command(
    {
        vol.Required("type"): "spotnav/get_plan",
        vol.Optional("api_version"): object,
        vol.Optional("charger_id"): object,
        vol.Optional("site_id"): object,
    }
)
@websocket_api.async_response
async def websocket_get_plan(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """The house card's Plan tab for one site."""
    version = msg.get("api_version")
    if type(version) is not int or version != PLAN_API_VERSION:
        send_unsupported_version(connection, msg, PLAN_API_VERSION)
        return
    try:
        answer = await async_plan_answer(hass, _site_of_request(hass, msg))
    except PowerMonthRefusal as refusal:
        connection.send_error(msg["id"], refusal.code, refusal.message)
        return
    connection.send_result(msg["id"], answer)


@callback
def async_setup_plan_api(hass: HomeAssistant) -> None:
    """Register the command once for the domain."""
    websocket_api.async_register_command(hass, websocket_get_plan)
