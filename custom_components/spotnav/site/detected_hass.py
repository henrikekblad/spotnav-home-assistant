"""What SpotNav detected, read from Home Assistant for one site (`detected.py` builds the list).

Read only: it reads the registries, the states and what each part of the site already chose (the house forecast's
weather entity, outdoor sensor and holiday calendar, the battery's facts, the site's measurement). The meter and
battery offers come from the same scan the site editor uses (`api.entity_fields.site_detection`), the cars from the
same discovery the dashboard uses. Nothing is written and no part is asked to choose again.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from ..battery.coexist import coexistence_for
from ..const import CONF_CHARGER_ENTRY_IDS, CONF_GRID_POWER_SOURCE, CONF_MEASUREMENT_MODE, CONF_SOLAR_FORECAST_ENTRIES
from ..execution.charger_profiles import PROFILES
from .detected import (
    BatteryOffer,
    ChargerIntegration,
    ChargerRow,
    Detected,
    MeterOffer,
    OutdoorOffer,
    VehicleRow,
    answer,
    battery,
    chargers,
    holiday_calendar,
    meter,
    outdoor_sensor,
    production,
    solar_forecast,
    vehicles,
    weather,
)
from .house_forecast import DEFAULT_WEATHER, OUTDOOR_SENSORS, pick_weather_entity
from .measurement_source import grid_power_source_from_dict

_LOGGER = logging.getLogger(__name__)


def _chargers(hass: HomeAssistant, site: ConfigEntry) -> Detected:
    from ..runtime import controller_for  # noqa: PLC0415 - the runtime imports the site

    registry = er.async_get(hass)
    rows: list[ChargerRow] = []
    used_entries: set[str] = set()
    for entry_id in site.data.get(CONF_CHARGER_ENTRY_IDS) or []:
        entry = hass.config_entries.async_get_entry(entry_id)
        controller = controller_for(hass, entry_id)
        if entry is None or controller is None:
            continue
        control = registry.async_get(controller.charge_control) if controller.charge_control else None
        if control is not None and control.config_entry_id is not None:
            used_entries.add(control.config_entry_id)
        try:
            start_stop = controller.adapter.path.describe().get("kind")
        except Exception:  # noqa: BLE001 - a path that cannot describe itself is shown without it
            start_stop = None
        rows.append(
            ChargerRow(
                title=entry.title,
                platform=controller.adapter.platform,
                charge_control=controller.charge_control,
                current_limit=controller.current_limit,
                start_stop=start_stop,
            )
        )
    integrations = [
        ChargerIntegration(entry.domain, entry.title, PROFILES[entry.domain].role, entry.entry_id in used_entries)
        for entry in hass.config_entries.async_entries()
        if entry.domain in PROFILES
    ]
    return chargers(rows, integrations)


def _meter_and_battery(hass: HomeAssistant, site: ConfigEntry, controller: Any) -> tuple[Detected, Detected]:
    from ..api.entity_fields import site_detection  # noqa: PLC0415 - the API imports the site

    try:
        detection = site_detection(hass, site)
        meters = [
            MeterOffer(
                title=offer.title,
                integration=offer.integration,
                entity_ids=tuple(dict.fromkeys(item.entity_id for item in offer.entities)),
                disabled_entity_ids=offer.disabled_entity_ids,
            )
            for offer in detection.meters
        ]
        batteries = [BatteryOffer(offer.title, offer.integration, offer.entity_id) for offer in detection.batteries]
    except Exception:  # noqa: BLE001 - the registry scan must not cost the rest
        _LOGGER.debug("SpotNav: the site's detection could not be read for what was detected", exc_info=True)
        meters, batteries = [], []
    source = grid_power_source_from_dict(site.data.get(CONF_GRID_POWER_SOURCE))
    phases = {
        phase: None if value is None else round(value, 1)
        for phase, value in controller.result.phase_headroom_a.items()
    }
    found_meter = meter(
        site.data.get(CONF_MEASUREMENT_MODE),
        controller.load_balancing_entity_ids(),
        None if source is None else source.power,
        {"headroom_a": phases},
        meters,
    )
    facts = controller.battery_shadow.facts
    row = coexistence_for(facts.brand, facts.integration)
    coexist = None
    if row is not None:
        coexist = {
            "own_optimiser": row.own_optimiser,
            "aggregators": list(row.aggregators),
            "watch_only": row.watch_only,
        }
    return found_meter, battery(facts.as_dict(), coexist, batteries)


async def _sun(hass: HomeAssistant, site: ConfigEntry, controller: Any) -> tuple[Detected, Detected]:
    from ..planning.hybrid_forecast import async_forecast_capable_domains  # noqa: PLC0415

    configured = [
        entry.title
        for entry_id in site.data.get(CONF_SOLAR_FORECAST_ENTRIES) or []
        if (entry := hass.config_entries.async_get_entry(entry_id)) is not None
    ]
    try:
        domains = await async_forecast_capable_domains(hass)
    except Exception:  # noqa: BLE001 - the offers are left out, the choice is still shown
        domains = frozenset()
    capable = {entry.title: entry.domain for entry in hass.config_entries.async_entries() if entry.domain in domains}
    forecast = controller.house_forecast
    return (
        solar_forecast(configured, capable, controller.solar_correction.status),
        production(list(forecast.solar_statistics), forecast.production_status),
    )


def _house_sources(hass: HomeAssistant, controller: Any) -> list[Detected]:
    forecast = controller.house_forecast
    weather_ids = hass.states.async_entity_ids("weather")
    chosen_weather = forecast.weather_entity_id or pick_weather_entity(weather_ids)
    registry = er.async_get(hass)
    offers = [
        OutdoorOffer(entry.entity_id, entry.platform, entry.disabled_by is not None)
        for entry in registry.entities.values()
        if entry.domain == "sensor"
        and (pattern := OUTDOOR_SENSORS.get(entry.platform)) is not None
        and pattern.search(str(entry.unique_id or ""))
    ]
    return [
        weather(weather_ids, chosen_weather, DEFAULT_WEATHER, forecast.weather_status),
        outdoor_sensor(sorted(offers, key=lambda offer: offer.entity_id), forecast.outdoor_entity_id,
                       forecast.outdoor_source),
        holiday_calendar(forecast.holiday_candidates(), forecast.holiday_entity_id, hass.config.country),
    ]


def _vehicles(hass: HomeAssistant) -> Detected:
    from ..vehicles.vehicle_discovery import (  # noqa: PLC0415 - the vehicles import the runtime
        discover_ambiguous_vehicles,
        discover_vehicles,
        vehicle_soc_entity_id,
    )

    found = [VehicleRow(car.name, vehicle_soc_entity_id(hass, car.id)) for car in discover_vehicles(hass)]
    ambiguous = [(car.name, list(car.candidate_entity_ids)) for car in discover_ambiguous_vehicles(hass)]
    return vehicles(found, ambiguous)


async def async_detected(hass: HomeAssistant, site: ConfigEntry) -> dict[str, Any] | None:
    """The site's list (`detected.answer`), or `None` when the site is not loaded. One failing kind is left out
    (it reads `not_looked_for`); the rest is still shown."""
    from ..runtime import site_controller_for  # noqa: PLC0415

    controller = site_controller_for(hass, site.entry_id)
    if controller is None:
        return None
    items: list[Detected] = []
    steps: list[tuple[str, Any]] = [
        ("chargers", lambda: [_chargers(hass, site)]),
        ("meter_battery", lambda: list(_meter_and_battery(hass, site, controller))),
        ("house", lambda: _house_sources(hass, controller)),
        ("vehicles", lambda: [_vehicles(hass)]),
    ]
    for name, step in steps:
        try:
            items += step()
        except Exception:  # noqa: BLE001 - one kind must not cost the list
            _LOGGER.debug("SpotNav: what was detected could not read %s", name, exc_info=True)
    try:
        items += list(await _sun(hass, site, controller))
    except Exception:  # noqa: BLE001
        _LOGGER.debug("SpotNav: what was detected could not read the sun", exc_info=True)
    return answer(items)
