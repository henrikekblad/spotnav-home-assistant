"""The Solcast integration's detailed forecast: p10, p50 and p90 per hour, as the integration states them.

The Energy dashboard contract (`planning/hybrid_forecast.py`) carries one series, the one the user's "use forecast
field" select picks (p50 by default). The integration also keeps all three estimates on its forecast sensors, so
that is read here, only to be logged and scored (`site/solar_correction_shadow.py`, kinds `sun_kwh_p10`, `sun_kwh_p50`,
`sun_kwh_p90`); nothing plans on it.

Checked against BJReplay/ha-solcast-solar (main, 2026-11-13) and the owner's installation:

* Its entity registry unique ids are the entity key: `total_kwh_forecast_today`, `total_kwh_forecast_tomorrow` and,
  when the user enables more days, `total_kwh_forecast_d3` to `_d7`. Entity ids vary with the user's naming, so the
  entities are found by unique id in this config entry, never by name.
* Each carries the attributes `detailedForecast` (half-hourly) and `detailedHourly`, lists of
  `{"period_start": ISO with offset, "pv_estimate": kW, "pv_estimate10": kW, "pv_estimate90": kW}`. The values are
  the mean power of the slot in kW, so a slot's kWh is the value times its length. Both are the dampened figures
  (what the Energy dashboard also gets). A site's own breakdown attributes (`detailedForecast_<site>`) are not used.
* The attributes are excluded from the recorder by the integration, not from the state machine: reading them costs
  nothing and fetches nothing.

Pure parsing is separate from the one read of Home Assistant's states.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta, timezone
import logging
import math
from typing import Any, Final

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

_LOGGER = logging.getLogger(__name__)

SOLCAST_DOMAIN: Final = "solcast_solar"
#: The unique ids of the integration's per-day forecast sensors (today, tomorrow, and the optional days 3-7).
SENSOR_UNIQUE_IDS: Final = (
    "total_kwh_forecast_today",
    "total_kwh_forecast_tomorrow",
    *(f"total_kwh_forecast_d{day}" for day in range(3, 8)),
)
ATTR_HOURLY: Final = "detailedHourly"
ATTR_HALF_HOURLY: Final = "detailedForecast"
#: The estimate fields of a row, by the quantile each stands for.
FIELDS: Final = {"p10": "pv_estimate10", "p50": "pv_estimate", "p90": "pv_estimate90"}

_UTC: Final = timezone.utc
_HOUR: Final = timedelta(hours=1)

#: `{quantile: {UTC hour start: kWh}}`.
Detail = dict[str, dict[datetime, float]]


def _number(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


def _start(raw: Any) -> datetime | None:
    if not isinstance(raw, str):
        return None
    try:
        moment = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return None if moment.tzinfo is None else moment.astimezone(_UTC)


def rows_by_hour(rows: Any) -> Detail:
    """One attribute's rows as kWh per UTC hour for each quantile. A slot's kWh is its kW times its length (the
    smallest gap between starts, an hour for a lone row); slots shorter than an hour are summed into the hour they
    start in. A row without a usable start or with a bad number in one field leaves that field out of its hour only;
    negative values count as zero."""
    parsed: list[tuple[datetime, Mapping[str, Any]]] = []
    for row in rows if isinstance(rows, list) else []:
        start = _start(row.get("period_start")) if isinstance(row, Mapping) else None
        if start is not None:
            parsed.append((start, row))
    parsed.sort(key=lambda item: item[0])
    gaps = [later - earlier for (earlier, _), (later, _) in zip(parsed, parsed[1:]) if later > earlier]
    step = min(gaps) if gaps else _HOUR
    step = step if step < _HOUR and _HOUR % step == timedelta(0) else _HOUR
    hours = step / _HOUR
    detail: Detail = {quantile: {} for quantile in FIELDS}
    seen: set[datetime] = set()
    for start, row in parsed:
        if start in seen:
            continue
        seen.add(start)
        hour = start.replace(minute=0, second=0, microsecond=0)
        for quantile, field in FIELDS.items():
            value = row.get(field)
            if _number(value):
                detail[quantile][hour] = detail[quantile].get(hour, 0.0) + max(0.0, float(value)) * hours
    return detail


def sensor_detail(attributes: Mapping[str, Any]) -> Detail:
    """One forecast sensor's attributes as kWh per hour: the hourly list when it has rows, else the half-hourly one."""
    hourly = rows_by_hour(attributes.get(ATTR_HOURLY))
    if any(hourly.values()):
        return hourly
    return rows_by_hour(attributes.get(ATTR_HALF_HOURLY))


def merge(details: Iterable[Detail]) -> Detail:
    """Several installations' hours as one: each quantile's kWh per hour adds up."""
    merged: Detail = {quantile: {} for quantile in FIELDS}
    for detail in details:
        for quantile, hours in detail.items():
            into = merged.setdefault(quantile, {})
            for hour, kwh in hours.items():
                into[hour] = into.get(hour, 0.0) + kwh
    return merged


def read_detail(hass: HomeAssistant, entry_ids: Iterable[str]) -> Detail | None:
    """The quantiles' kWh per UTC hour from the Solcast entries among `entry_ids`, from the forecast sensors' attributes
    as they stand; `None` when none of the entries is Solcast's or none gave a row. Within an installation the day
    sensors are taken in turn and an hour already given by an earlier one is kept (they cover different days);
    installations are summed. Nothing is fetched."""
    registry = er.async_get(hass)
    installations: list[Detail] = []
    for entry_id in entry_ids:
        entry = hass.config_entries.async_get_entry(entry_id)
        if entry is None or entry.domain != SOLCAST_DOMAIN:
            continue
        own: Detail = {quantile: {} for quantile in FIELDS}
        for unique_id in SENSOR_UNIQUE_IDS:
            entity_id = registry.async_get_entity_id("sensor", SOLCAST_DOMAIN, unique_id)
            registered = None if entity_id is None else registry.async_get(entity_id)
            if registered is None or registered.config_entry_id != entry_id:
                continue
            state = hass.states.get(entity_id)
            if state is None or state.state in ("unknown", "unavailable"):
                continue
            try:
                found = sensor_detail(state.attributes)
            except Exception:  # noqa: BLE001 - a malformed attribute leaves that sensor out
                _LOGGER.debug("SpotNav Solcast detail: %s could not be read", entity_id, exc_info=True)
                continue
            for quantile, hours in found.items():
                for hour, kwh in hours.items():
                    own[quantile].setdefault(hour, kwh)
        if any(own.values()):
            installations.append(own)
    if not installations:
        return None
    return merge(installations)
