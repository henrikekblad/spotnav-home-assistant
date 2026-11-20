"""The made-up house of the screenshot tool: PV, grid and battery energy counters, a heat pump with its meter, room
temperatures, two switches, and two screenshot-only services:

* `demo_house.docs_seed`: imports the simulation's history (`model.py`) as the recorder's statistics, hourly for
  the whole span and five-minute for the last ten days, as the Energy dashboard's meters would have them.
* `demo_house.docs_scene` (`sun`, `house`, `heat`, `battery` in kW, battery charge positive; no field clears it):
  pins the live power sensors, so a picture does not depend on the time of day it is taken.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import device_registry as dr

from . import model

DOMAIN = "demo_house"
PLATFORMS = ["sensor", "switch"]

#: statistic id suffix -> (model counter or series, unit, unit class, kind)
COUNTERS = {
    "sensor.demo_solar_energy": "solar",
    "sensor.demo_grid_import_energy": "grid_in",
    "sensor.demo_grid_export_energy": "grid_out",
    "sensor.demo_battery_charge_energy": "batt_in",
    "sensor.demo_battery_discharge_energy": "batt_out",
    "sensor.demo_heat_pump_energy": "heat",
}
MEANS = {
    "sensor.demo_solar_power": ("sun", "kW", "power"),
    "sensor.demo_heat_pump_power": ("heat", "kW", "power"),
    "sensor.sigen_plant_ess_soc": ("soc", "%", None),
    "sensor.living_room_temperature": ("living", "°C", "temperature"),
    "sensor.bedroom_temperature": ("bedroom", "°C", "temperature"),
    "sensor.office_temperature": ("office", "°C", "temperature"),
    "sensor.outdoor_temperature": ("t_out", "°C", "temperature"),
}
SHORT_TERM_DAYS = 10


def _rows(series: list[float], mean: bool, base: float, first: int, last: int, per: int, m: model.Model):
    """The statistics rows of `per` steps each over steps `first`..`last` (exclusive)."""
    rows = []
    for a in range(first, last, per):
        b = a + per
        start = m.t[a]
        if mean:
            chunk = series[a:b]
            rows.append({"start": start, "mean": sum(chunk) / len(chunk), "min": min(chunk), "max": max(chunk)})
        else:
            rows.append({"start": start, "state": base + series[b - 1], "sum": series[b - 1]})
    return rows


async def _seed(hass: HomeAssistant, call: ServiceCall) -> None:
    from homeassistant.components.recorder import get_instance
    from homeassistant.components.recorder.db_schema import StatisticsShortTerm
    from homeassistant.components.recorder.models import StatisticMeanType
    from homeassistant.components.recorder.statistics import async_import_statistics
    from homeassistant.components.recorder.tasks import ImportStatisticsTask

    m = model.get()
    done = m.index(datetime.now(timezone.utc))  # steps before this one are complete
    hours_end = done - done % 12
    five_first = max(0, done - SHORT_TERM_DAYS * 288)
    five_first -= five_first % 12
    jobs = []
    for statistic_id, key in COUNTERS.items():
        meta = {"source": "recorder", "statistic_id": statistic_id, "name": None, "has_sum": True,
                "mean_type": StatisticMeanType.NONE, "unit_of_measurement": "kWh", "unit_class": "energy"}
        base = model.COUNTER_BASE[key]
        jobs.append((meta, _rows(m.counters[key], False, base, 0, hours_end, 12, m), _rows(m.counters[key], False, base, five_first, done, 1, m)))
    for statistic_id, (key, unit, unit_class) in MEANS.items():
        series = m.soc if key == "soc" else getattr(m, key, None)
        if series is None:
            index = {"living": 0, "bedroom": 1, "office": 2}.get(key)
            series = [r[index] for r in m.rooms]
        meta = {"source": "recorder", "statistic_id": statistic_id, "name": None, "has_sum": False,
                "mean_type": StatisticMeanType.ARITHMETIC, "unit_of_measurement": unit, "unit_class": unit_class}
        jobs.append((meta, _rows(series, True, 0.0, 0, hours_end, 12, m), _rows(series, True, 0.0, five_first, done, 1, m)))
    instance = get_instance(hass)
    for meta, hourly, five in jobs:
        async_import_statistics(hass, meta, hourly)
        instance.queue_task(ImportStatisticsTask(meta, five, StatisticsShortTerm))


async def _scene(hass: HomeAssistant, call: ServiceCall) -> None:
    model.SCENE.clear()
    for key in ("sun", "house", "heat", "soc"):
        if key in call.data:
            model.SCENE[key] = float(call.data[key])
    if "battery" in call.data:
        model.SCENE["batt"] = float(call.data["battery"])
    if model.SCENE:
        base = model.get().live()
        for key in ("sun", "house", "heat", "batt"):
            model.SCENE.setdefault(key, base[key])
    for entity in hass.data.get(DOMAIN, {}).get("entities", []):
        entity.refresh()


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    hass.data.setdefault(DOMAIN, {"entities": []})
    devices = dr.async_get(hass)
    for ident, name, kind in (("solar", "Solar inverter", "Inverter"), ("heat", "Heat pump", "Heat pump"), ("rooms", "Room sensors", "Sensor")):
        devices.async_get_or_create(config_entry_id=entry.entry_id, identifiers={(DOMAIN, ident)}, name=name,
                                    manufacturer="Demo", model=kind)
    async def seed(call: ServiceCall) -> None:
        await _seed(hass, call)

    async def scene(call: ServiceCall) -> None:
        await _scene(hass, call)

    hass.services.async_register(DOMAIN, "docs_seed", seed)
    hass.services.async_register(DOMAIN, "docs_scene", scene, schema=vol.Schema({
        vol.Optional("sun"): vol.Coerce(float), vol.Optional("house"): vol.Coerce(float),
        vol.Optional("heat"): vol.Coerce(float), vol.Optional("soc"): vol.Coerce(float), vol.Optional("battery"): vol.Coerce(float)}))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
