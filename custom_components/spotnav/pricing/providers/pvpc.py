"""Spain's regulated PVPC through Home Assistant's own `pvpc_hourly_pricing` integration.

SpotNav never calls Red Eléctrica itself: it reads the prices the integration the person added has already
downloaded. What the integration holds (Home Assistant 2026.9, `components/pvpc_hourly_pricing/`, library
`esios_api` 4.4.0):

* one config entry per tariff, its unique id the tariff's name: `2.0TD` (the peninsula, the Balearic and the
  Canary Islands; the archive's `PCB` column, the price the relay serves as `ES-PVPC`) or
  `2.0TD (Ceuta/Melilla)` (`CYM`);
* its coordinator (`entry.runtime_data`) keeps `data.sensors["PVPC"]`, a mapping of UTC instants to prices in
  EUR/kWh rounded to five decimals: today's Spanish day, and from about 20:15 Spanish time also tomorrow's (it
  asks for tomorrow on its half-hourly update from 20:00, and drops yesterday's prices once tomorrow's are held).
  The free archive it reads by default is hourly (23 and 25 prices on the clock-change days); with a personal
  ESIOS token it reads indicator 1001 instead, whose step is read from the instants (60 or 15 minutes);
* the instants are placed on the calendar day of Home Assistant's own zone (`hass.config.time_zone`): an
  installation on the Canary Islands sees each price at the same wall-clock hour as one in Madrid, one hour
  later in absolute time. So a day is collected by that zone's calendar and its prices are placed from midnight
  in Madrid, which is exactly the archive's day as the relay publishes it;
* the sensor's own attributes (`price_00h` … `price_23h`, `price_02h_d` on the long day, `price_next_day_…`)
  carry the same numbers keyed by local hour; they are not read, because hour keys cannot hold a quarter-hour
  day and collide on the clock-change days.

What the price includes is the catalogue's to say (`included: ["grid_fee"]`): energy, the 2.0TD tolls and
charges; never VAT or the electricity tax. The integration keeps no history beyond today ([HISTORY_REACH]).

The answer becomes the relay's `"v": 1` day document (`pricing/day_document.py`), dated on the Madrid day, in
EUR (no rate), `src` `esios.ree.es` as the relay writes it. Nothing about the data is logged.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable, Mapping
from datetime import date, datetime, time, timedelta
from typing import Any, Final

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from ... import time_zones
from ..area_catalogue import BundledCatalogue
from ..day_document import build_day_document
from ..relay_contract import AreaPublication, PriceDocument
from . import DayFetch, FailureCode, market_today, ProviderKey

_LOGGER = logging.getLogger(__name__)

DOMAIN: Final = "pvpc_hourly_pricing"

#: The coordinator's series of the PVPC price itself (`esios_api.const.KEY_PVPC`).
SERIES: Final = "PVPC"

#: Red Eléctrica publishes the next day at about 20:15 Spanish time; the integration asks from 20:00.
PUBLICATION: Final = AreaPublication(local_time=time(20, 15), tz="Europe/Madrid")

#: The integration holds today and tomorrow only: a past day cannot be asked for.
HISTORY_REACH: Final = timedelta(0)

#: The steps a PVPC day comes in, in minutes (the archive is hourly; the token's indicator may be finer).
RESOLUTIONS: Final = (15, 60)

#: Where the file says it came from (`src`), as the relay writes it.
SRC: Final = "esios.ree.es"

#: The zone the PVPC's day is defined in.
MADRID: Final = "Europe/Madrid"


class _Refusal(Exception):
    def __init__(self, code: FailureCode) -> None:
        super().__init__(code)
        self.code: FailureCode = code


class PvpcProvider:
    """Day files from the prices the `pvpc_hourly_pricing` integration holds; no request of its own."""

    key: ProviderKey = "pvpc"
    has_index = False
    publication: AreaPublication = PUBLICATION
    history_reach: timedelta | None = HISTORY_REACH

    def __init__(
        self,
        hass: HomeAssistant,
        bundled: BundledCatalogue,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._hass = hass
        self._bundled = bundled
        self._now = now if now is not None else dt_util.utcnow

    def serves(self, area_id: str) -> bool:
        """Whether the area is priced at a PVPC tariff (it has a PVPC route in the bundled catalogue)."""
        routes = self._bundled.routes.get(area_id)
        return routes is not None and routes.pvpc is not None

    def loaded(self) -> bool:
        """Whether a `pvpc_hourly_pricing` entry is loaded."""
        return any(entry.state is ConfigEntryState.LOADED for entry in self._hass.config_entries.async_entries(DOMAIN))

    def market_calendar(self, area_id: str) -> str | None:
        routes = self._bundled.routes.get(area_id)
        return None if routes is None else routes.cet_market_tz

    def wants_again(self, area_id: str, document: PriceDocument) -> bool:
        """A PVPC day is in EUR and needs no rate: never asked for twice."""
        return False

    def _entry(self, tariff: str) -> ConfigEntry | None:
        """The loaded entry of the area's tariff; else any loaded one (the person's own tariff: Ceuta and Melilla
        have had the same price as the peninsula on every day inspected)."""
        loaded = [
            entry for entry in self._hass.config_entries.async_entries(DOMAIN) if entry.state is ConfigEntryState.LOADED
        ]
        for entry in loaded:
            if entry.unique_id == tariff or entry.data.get("tariff") == tariff:
                return entry
        return loaded[0] if loaded else None

    async def async_fetch_day(self, area_id: str, market_day: date) -> DayFetch:
        """One Madrid day for the PVPC area; every expected failure is a code, never an exception."""
        routes = self._bundled.routes.get(area_id)
        entry = self._bundled.catalogue.area(area_id)
        if routes is None or routes.pvpc is None or entry is None:
            return DayFetch("failed", code="source_missing")
        config_entry = self._entry(routes.pvpc)
        if config_entry is None:
            return DayFetch("failed", code="pvpc_unavailable")
        try:
            series = _series(config_entry)
            points = _day_points(series, market_day, _home_zone(self._hass))
        except _Refusal as err:
            _LOGGER.debug("PVPC %s %s: %s", area_id, market_day, err.code)
            return DayFetch("failed", code=err.code)
        if not points:
            if market_day > market_today(self._now(), routes.cet_market_tz):
                return DayFetch("not_published", code="not_published")
            # Today (or earlier) is not held: the integration has not read it (yet), which is not a publication.
            return DayFetch("failed", code="pvpc_unavailable")
        try:
            start, res, prices = _day(points, market_day, routes.cet_market_tz)
        except _Refusal as err:
            _LOGGER.debug("PVPC %s %s: %s", area_id, market_day, err.code)
            return DayFetch("failed", code=err.code)
        document = build_day_document(
            area_id=area_id,
            market_day=market_day,
            tz=entry.tz,
            market_tz=routes.cet_market_tz,
            start=start,
            res=res,
            prices_eur_kwh=prices,
            fx=None,
            fx_date=None,
            fx_src=None,
            src=SRC,
            published=None,
            retrieved=self._now(),
        )
        return DayFetch("fetched", document=document)


def _home_zone(hass: HomeAssistant) -> Any:
    """The zone the integration placed its days in: Home Assistant's own, Madrid when it is not resolved."""
    return time_zones.zone(hass.config.time_zone) or time_zones.require_zone(MADRID)


def _series(entry: ConfigEntry) -> Mapping[Any, Any]:
    """The coordinator's PVPC series; anything not shaped as the integration keeps it is not there to read."""
    coordinator = getattr(entry, "runtime_data", None)
    data = getattr(coordinator, "data", None)
    sensors = getattr(data, "sensors", None)
    if not isinstance(sensors, Mapping):
        raise _Refusal("pvpc_unavailable")
    series = sensors.get(SERIES)
    if series is None:
        raise _Refusal("pvpc_unavailable")
    if not isinstance(series, Mapping):
        raise _Refusal("provider_refused")
    return series


def _price(raw: Any) -> float:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not math.isfinite(raw):
        raise _Refusal("provider_refused")
    return float(raw)


def _day_points(series: Mapping[Any, Any], market_day: date, home: Any) -> list[tuple[datetime, float]]:
    """The series' prices on one calendar day of the integration's zone, in order."""
    points: list[tuple[datetime, float]] = []
    for instant, value in series.items():
        if not isinstance(instant, datetime) or instant.tzinfo is None:
            raise _Refusal("provider_refused")
        price = _price(value)
        if instant.astimezone(home).date() == market_day:
            points.append((instant.astimezone(dt_util.UTC), price))
    points.sort(key=lambda point: point[0])
    return points


def _day(points: list[tuple[datetime, float]], market_day: date, market_tz: str) -> tuple[datetime, int, list[float]]:
    """The Madrid day's first instant, the step and its prices.

    The prices must be one step apart, back to back, and exactly as many as the Madrid day holds (23, 24 or 25
    hours): the integration downloads a day whole, so anything else is not a day this client can place.
    """
    zone = time_zones.require_zone(market_tz)
    start = datetime.combine(market_day, time(0), tzinfo=zone)
    end = datetime.combine(market_day + timedelta(days=1), time(0), tzinfo=zone)
    length = end.astimezone(dt_util.UTC) - start.astimezone(dt_util.UTC)
    if len(points) < 2:
        raise _Refusal("provider_refused")
    step = points[1][0] - points[0][0]
    minutes = step.total_seconds() / 60
    if minutes not in RESOLUTIONS:
        raise _Refusal("provider_refused")
    for previous, current in zip(points, points[1:]):
        if current[0] - previous[0] != step:
            raise _Refusal("provider_refused")
    if step * len(points) != length:
        raise _Refusal("provider_refused")
    return start, int(minutes), [price for _, price in points]
