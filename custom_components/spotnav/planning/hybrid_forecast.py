"""Hybrid mode's forecast adapter.

Reads an hourly PV forecast from the integrations a site's
`CONF_SOLAR_FORECAST_ENTRIES` names, through the Energy dashboard's
cross-integration contract: a module-level
`async def async_get_solar_forecast(hass, config_entry_id)` returning
`{"wh_hours": {iso: wh}} | None` in `<component>/energy.py`.

`wh_hours` is not always hourly, so each source is normalized before summing:

* Solcast writes 30-minute slots with UTC keys, Forecast.Solar 60, 30 or 15 minutes depending on the
  account with local-offset keys, Open-Meteo hourly slots including about 92 days of past hours. A source's
  slot length is the smallest gap between its keys; slots shorter than an hour are summed into the UTC
  hour they start in (their Wh add up to that hour's Wh, which is its average watts), so the planner,
  which treats each key as one hour, neither double counts nor halves them.
* Slots that ended before the start of yesterday (UTC) are dropped, so a long history neither bloats the
  replan comparison nor is ever credited.
* A source whose integration was never set up (Open-Meteo raises `KeyError`) or raises anything else is
  skipped. Nothing is ever fetched: the read returns the integration's cached estimate.

* Discovery uses `async_process_integration_platforms` for the `"energy"`
  platform; it also picks up integrations that load later. It appends
  bookkeeping to `hass.data` per call, so it runs once per `hass`.
* The read returns the integration's cached estimate and never fetches; this
  module calls nothing else on it, so it cannot trigger a refresh.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone as dt_timezone
from typing import Any, Final

from homeassistant.core import HomeAssistant
from homeassistant.helpers.integration_platform import async_process_integration_platforms
from homeassistant.util import dt as dt_util

from ..runtime import domain_data


_LOGGER = logging.getLogger(__name__)

#: The Energy dashboard's platform name. Not imported from the `energy`
#: component, to avoid depending on it.
ENERGY_PLATFORM_NAME: Final = "energy"

#: Forecast hour keys are normalized to UTC before summing.
_UTC: Final = dt_timezone.utc

_HOUR: Final = timedelta(hours=1)

GetSolarForecast = Callable[[HomeAssistant, str], Awaitable[dict[str, Any] | None]]


async def _async_forecast_platforms(hass: HomeAssistant) -> dict[str, GetSolarForecast]:
    """The live `{integration domain: async_get_solar_forecast}` mapping, discovered
    once per `hass` and kept current by the loader's event subscription.
    """
    data = domain_data(hass)
    if data.forecast_platforms is not None:
        return data.forecast_platforms

    platforms: dict[str, GetSolarForecast] = {}
    # Stored before discovery completes: the loader's later callbacks must mutate
    # this same dict for late-loading integrations to be found.
    data.forecast_platforms = platforms

    def _collect(_hass: HomeAssistant, domain: str, platform: Any) -> None:
        get_forecast = getattr(platform, "async_get_solar_forecast", None)
        if get_forecast is not None:
            platforms[domain] = get_forecast

    await async_process_integration_platforms(
        hass, ENERGY_PLATFORM_NAME, _collect, wait_for_platforms=True
    )
    return platforms


async def async_forecast_capable_domains(hass: HomeAssistant) -> frozenset[str]:
    """Loaded integration domains that implement the solar-forecast platform, for
    the site options flow's picker. Reuses the cached discovery.
    """
    return frozenset((await _async_forecast_platforms(hass)).keys())


@dataclass(frozen=True, slots=True)
class ForecastReadResult:
    """What the forecast read produced, and which sources answered.

    `wh_hours` is `None` when no entries are configured or none was readable.
    `sources_read` names only entries that contributed (empty when `wh_hours`
    is `None`).
    """

    wh_hours: dict[datetime, float] | None
    sources_read: tuple[str, ...]


def hourly_wh(slots: dict[datetime, float], *, now: datetime) -> dict[datetime, float]:
    """One source's `{slot start (UTC): Wh}` as Wh per UTC hour.

    The slot length is the smallest gap between keys. A length that divides an hour (15 or 30
    minutes) is summed into the hour each slot starts in; anything else (hourly, or irregular) is
    kept as it is. Slots that ended before the start of yesterday (UTC) are dropped.
    """
    if not slots:
        return {}
    starts = sorted(slots)
    gaps = [(later - earlier) for earlier, later in zip(starts, starts[1:]) if later > earlier]
    step = min(gaps) if gaps else _HOUR
    cutoff = (now.astimezone(_UTC) - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    sub_hourly = step < _HOUR and _HOUR % step == timedelta(0)
    result: dict[datetime, float] = {}
    for start in starts:
        if start + (step if sub_hourly else _HOUR) <= cutoff:
            continue
        key = start.replace(minute=0, second=0, microsecond=0) if sub_hourly else start
        result[key] = result.get(key, 0.0) + slots[start]
    return result


async def async_read_forecast_wh(
    hass: HomeAssistant, entry_ids: Sequence[str], *, now: datetime | None = None
) -> ForecastReadResult:
    """The summed hourly PV forecast from every readable entry in `entry_ids`.

    Each entry is read independently; a missing entry, no platform, a `None`
    or raising read, or malformed `wh_hours` skips that source (logged at
    DEBUG). If none was readable, `wh_hours` is `None`. Keys are normalized to
    UTC and each source to Wh per hour (`hourly_wh`), so the same hour from
    sources in different offsets or resolutions sums as one.
    """
    if not entry_ids:
        return ForecastReadResult(wh_hours=None, sources_read=())
    read_at = now or dt_util.utcnow()

    platforms = await _async_forecast_platforms(hass)
    combined: dict[datetime, float] = {}
    sources_read: list[str] = []

    for entry_id in entry_ids:
        entry = hass.config_entries.async_get_entry(entry_id)
        if entry is None:
            _LOGGER.debug("HYBRID forecast source %s: no such config entry", entry_id)
            continue
        get_forecast = platforms.get(entry.domain)
        if get_forecast is None:
            _LOGGER.debug(
                "HYBRID forecast source %s (%s): no solar-forecast platform registered",
                entry_id,
                entry.domain,
            )
            continue
        try:
            forecast = await get_forecast(hass, entry_id)
        except Exception:  # noqa: BLE001 - one source's failure must not sink the others
            _LOGGER.debug(
                "HYBRID forecast source %s (%s): async_get_solar_forecast raised",
                entry_id,
                entry.domain,
                exc_info=True,
            )
            continue
        if forecast is None:
            _LOGGER.debug(
                "HYBRID forecast source %s (%s): no forecast available", entry_id, entry.domain
            )
            continue
        wh_hours = forecast.get("wh_hours") if isinstance(forecast, dict) else None
        if not isinstance(wh_hours, dict):
            _LOGGER.debug("HYBRID forecast source %s (%s): malformed wh_hours", entry_id, entry.domain)
            continue

        source_slots: dict[datetime, float] = {}
        for iso, wh in wh_hours.items():
            try:
                hour = datetime.fromisoformat(iso)
                if hour.tzinfo is None or hour.utcoffset() is None:
                    raise ValueError("naive forecast hour")
                value = float(wh)
            except (TypeError, ValueError):
                _LOGGER.debug(
                    "HYBRID forecast source %s (%s): unusable hour %r=%r",
                    entry_id,
                    entry.domain,
                    iso,
                    wh,
                )
                continue
            key = hour.astimezone(_UTC)
            source_slots[key] = source_slots.get(key, 0.0) + value
        hourly = hourly_wh(source_slots, now=read_at)
        if hourly:
            for hour_key, hour_wh in hourly.items():
                combined[hour_key] = combined.get(hour_key, 0.0) + hour_wh
            sources_read.append(entry_id)

    if not sources_read:
        return ForecastReadResult(wh_hours=None, sources_read=())
    return ForecastReadResult(wh_hours=combined, sources_read=tuple(sources_read))
