"""Zonneplan's dynamic tariff in the Netherlands through the "Zonneplan" integration (HACS, fsaris).

SpotNav never calls Zonneplan itself: it reads the prices the integration the person added has already fetched.
What the integration exposes (version 2026.10.0, `custom_components/zonneplan_one/`, `const.py`, `sensor.py`,
`coordinators/electricity_prices_data_coordinator.py`):

* per electricity connection one sensor, platform `zonneplan_one`, translation key and unique-id suffix
  `current_quarter_hourly_electricity_tariff` (unique id `{connection uuid}_current_quarter_hourly_electricity_tariff`,
  enabled by default). It is found in the entity registry by platform and unique id, never by entity id, which the
  integration sets and the person may rename;
* its `forecast` attribute, the prices coordinator's list (every 15 minutes) of
  `{"start_date", "end_date", "price_tax_included": {"amount"}, "price_tax_excluded": {"amount"}, …}`: the start
  and end as aware datetimes in `Europe/Amsterdam` (ISO strings are read the same), the amounts integers in
  1e-7 EUR per kWh (2 500 000 is 0.25 EUR/kWh; the sensor's `value_factor` scales its state only). The attribute is
  neither recorded nor restored: it is there once the integration has fetched after a start;
* `price_tax_included` is what the customer pays per kWh: the spot price, Zonneplan's purchase fee, the energy tax
  and 21 % VAT. That is the number read; `price_tax_excluded` is not.

The price is Zonneplan's public tariff, the same for every contract, so the area is one (`NL-ZONNEPLAN`) and every
connection's sensor tells the same prices: they are pooled by instant, and two that disagree about one slot refuse
the pool. What the price includes is the catalogue's to say (`included: ["vat", "tax"]`): nothing is added, and
the spot area `NL` keeps its own days, so all-in and spot prices never mix in one series or one profile.

A day is the Amsterdam calendar day, in the forecast's step (15 minutes, or 60 for an hourly forecast): 92, 96 or
100 quarter-hours. The forecast's range follows the moment it was fetched, not the day: tomorrow appears in the
afternoon, and a forecast may start at today's midnight or at the current slot. A today that starts at a slot
already past (the first fetch after Zonneplan was added, if the forecast starts at "now") is not a day file: a
file is positional from midnight and holds a number for every slot (`relay_contract.parse_day`, which the store
and the app read too), and SpotNav never invents a price. So that today is not held: the answer is `not_published`
with the code `zonneplan_partial_day`, which, like a day not out yet, is no failure and drives no backoff (the
installation-wide poll and the other areas keep their cadence). Tomorrow is held once the afternoon's forecast
holds it whole, but a charger on the area has no plan until the Amsterdam day turns and that file becomes today's:
the planner needs today's prices before it plans at all (`auto_controller._price_data_gate`), and says it waits for
prices meanwhile. The integration keeps no past day ([HISTORY_REACH]).

The answer becomes the relay's `"v": 1` day document (`pricing/day_document.py`), in EUR (no rate), `src`
`zonneplan.nl`. Nothing about the prices or the connection is logged.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable, Iterable
from datetime import date, datetime, time, timedelta
from typing import Any, Final

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import callback, HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from ... import time_zones
from ..area_catalogue import BundledCatalogue
from ..day_document import build_day_document
from ..relay_contract import AreaPublication, PriceDocument
from . import DayFetch, FailureCode, market_today, ProviderKey

_LOGGER = logging.getLogger(__name__)

DOMAIN: Final = "zonneplan_one"

#: The tariff sensor's key: its translation key and the end of its unique id.
TARIFF_KEY: Final = "current_quarter_hourly_electricity_tariff"

#: The tariff sensor's attribute that lists the prices.
FORECAST: Final = "forecast"

#: One `amount` unit in EUR per kWh.
AMOUNT_EUR: Final = 1e-7

#: Zonneplan's next day follows the day-ahead auction (13:00 Brussels, as the area NL); the integration asks every
#: 15 minutes.
PUBLICATION: Final = AreaPublication(local_time=time(13, 0), tz="Europe/Brussels")

#: The integration keeps no past day: a past day cannot be asked for.
HISTORY_REACH: Final = timedelta(0)

#: The steps a Zonneplan day comes in, in minutes.
RESOLUTIONS: Final = (15, 60)

#: Where the file says it came from (`src`).
SRC: Final = "zonneplan.nl"

#: A price beyond this (in EUR per kWh) is not a tariff.
_MAX_ABS_PRICE: Final = 100.0


class _Refusal(Exception):
    def __init__(self, code: FailureCode) -> None:
        super().__init__(code)
        self.code: FailureCode = code


class ZonneplanProvider:
    """Day files from the tariff the Zonneplan integration holds; no request of its own."""

    key: ProviderKey = "zonneplan"
    #: What the card and the app call it, and where it is (attribution).
    name: str = "Zonneplan"
    url: str = "https://www.zonneplan.nl/energie"
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
        self._entities: tuple[str, ...] = ()

    # -------------------------------------------------------------- the tariff sensors

    @property
    def entities(self) -> tuple[str, ...]:
        """The tariff sensors found at the last [refresh], in the order of their unique ids."""
        return self._entities

    @callback
    def refresh(self) -> bool:
        """Look for the tariff sensors again (registry and entries); whether what is served changed."""
        entities = tuple(self._find_entities())
        changed = entities != self._entities
        self._entities = entities
        return changed

    def _find_entities(self) -> Iterable[str]:
        registry = er.async_get(self._hass)
        found: list[tuple[str, str]] = []
        for config_entry in self._hass.config_entries.async_entries(DOMAIN):
            if config_entry.state is not ConfigEntryState.LOADED:
                continue
            for entry in er.async_entries_for_config_entry(registry, config_entry.entry_id):
                if entry.platform != DOMAIN or entry.domain != "sensor" or entry.disabled:
                    continue
                if isinstance(entry.unique_id, str) and entry.unique_id.endswith("_" + TARIFF_KEY):
                    found.append((entry.unique_id, entry.entity_id))
        return [entity_id for _, entity_id in sorted(found)]

    # -------------------------------------------------------------- what the router and repository ask

    def has_route(self, area_id: str) -> bool:
        """Whether the area is Zonneplan's tariff (it has a Zonneplan route in the bundled catalogue)."""
        routes = self._bundled.routes.get(area_id)
        return routes is not None and routes.zonneplan

    def serves(self, area_id: str) -> bool:
        """Whether Zonneplan's tariff is there for the area (a loaded entry with an enabled tariff sensor)."""
        return self.has_route(area_id) and bool(self._entities)

    def alternative_for(self, area_id: str) -> bool:
        """Whether the area is another area of a country Zonneplan's tariff area is in (NL beside NL-ZONNEPLAN)."""
        catalogue = self._bundled.catalogue
        area = catalogue.area(area_id)
        if area is None or self.has_route(area_id):
            return False
        countries = set(area.countries)
        return any(
            routes.zonneplan and (other := catalogue.area(other_id)) is not None and countries & set(other.countries)
            for other_id, routes in self._bundled.routes.items()
        )

    def market_calendar(self, area_id: str) -> str | None:
        routes = self._bundled.routes.get(area_id)
        return None if routes is None else routes.cet_market_tz

    def wants_again(self, area_id: str, document: PriceDocument) -> bool:
        """A Zonneplan day is in EUR and needs no rate: never asked for twice."""
        return False

    async def async_fetch_day(self, area_id: str, market_day: date) -> DayFetch:
        """One Amsterdam day for the Zonneplan area; every expected failure is a code, never an exception."""
        routes = self._bundled.routes.get(area_id)
        entry = self._bundled.catalogue.area(area_id)
        if routes is None or not routes.zonneplan or entry is None:
            return DayFetch("failed", code="source_missing")
        if not self._entities:
            return DayFetch("failed", code="zonneplan_unavailable")
        zone = time_zones.require_zone(routes.cet_market_tz)
        start = datetime.combine(market_day, time(0), tzinfo=zone).astimezone(dt_util.UTC)
        end = datetime.combine(market_day + timedelta(days=1), time(0), tzinfo=zone).astimezone(dt_util.UTC)
        now = self._now()
        today = market_today(now, routes.cet_market_tz)
        try:
            pool, step = _pool(self._forecasts())
            prices = _cut(pool, step, start, end, now=now, today=market_day == today, future=market_day > today)
        except _Refusal as err:
            _LOGGER.debug("Zonneplan %s %s: %s", area_id, market_day, err.code)
            if err.code in ("not_published", "zonneplan_partial_day"):
                # Not a failure: nothing to hold yet, asked again on the ordinary cadence.
                return DayFetch("not_published", code=err.code)
            return DayFetch("failed", code=err.code)
        document = build_day_document(
            area_id=area_id,
            market_day=market_day,
            tz=entry.tz,
            market_tz=routes.cet_market_tz,
            start=start,
            res=int(step.total_seconds() // 60),
            prices_eur_kwh=prices,
            fx=None,
            fx_date=None,
            fx_src=None,
            src=SRC,
            published=None,
            retrieved=now,
        )
        return DayFetch("fetched", document=document)

    def _forecasts(self) -> list[list[Any]]:
        """Every tariff sensor's forecast that is there; none at all is not there to read."""
        forecasts: list[list[Any]] = []
        for entity_id in self._entities:
            state = self._hass.states.get(entity_id)
            if state is None:
                continue
            forecast = state.attributes.get(FORECAST)
            if forecast is None:
                continue
            if not isinstance(forecast, list):
                raise _Refusal("provider_refused")
            forecasts.append(forecast)
        if not forecasts:
            raise _Refusal("zonneplan_unavailable")
        return forecasts


def _instant(raw: Any) -> datetime:
    if isinstance(raw, str):
        raw = dt_util.parse_datetime(raw)
    if not isinstance(raw, datetime) or raw.tzinfo is None:
        raise _Refusal("provider_refused")
    return raw.astimezone(dt_util.UTC)


def _row(row: Any) -> tuple[datetime, timedelta, float]:
    """One forecast entry as (start, step, EUR per kWh including tax); anything else is refused."""
    if not isinstance(row, dict):
        raise _Refusal("provider_refused")
    slot_start = _instant(row.get("start_date"))
    step = _instant(row.get("end_date")) - slot_start
    if step.total_seconds() / 60 not in RESOLUTIONS:
        raise _Refusal("provider_refused")
    included = row.get("price_tax_included")
    amount = included.get("amount") if isinstance(included, dict) else None
    if isinstance(amount, bool) or not isinstance(amount, (int, float)) or not math.isfinite(amount):
        raise _Refusal("provider_refused")
    price = amount * AMOUNT_EUR
    if abs(price) > _MAX_ABS_PRICE:
        raise _Refusal("provider_refused")
    return slot_start, step, price


def _pool(forecasts: list[list[Any]]) -> tuple[dict[datetime, float], timedelta]:
    """Every price the forecasts hold, by start instant (UTC), and their one step; two forecasts that disagree
    about one slot, or a mix of steps, refuse the pool."""
    pool: dict[datetime, float] = {}
    steps: set[timedelta] = set()
    for forecast in forecasts:
        for row in forecast:
            slot_start, step, price = _row(row)
            held = pool.get(slot_start)
            if held is not None and held != price:
                raise _Refusal("provider_refused")
            pool[slot_start] = price
            steps.add(step)
    if not pool:
        raise _Refusal("zonneplan_unavailable")
    if len(steps) != 1:
        raise _Refusal("provider_refused")
    return pool, steps.pop()


def _cut(
    pool: dict[datetime, float],
    step: timedelta,
    start: datetime,
    end: datetime,
    *,
    now: datetime,
    today: bool,
    future: bool,
) -> list[float]:
    """The Amsterdam day's prices, in order, from midnight.

    The day's prices must follow each other from midnight to the day's end. A day after today with nothing held,
    or held only in part, is "not published yet". Today whose forecast starts at a slot already begun (the forecast
    starts at the current slot) is `zonneplan_partial_day`: its earlier prices are not there, and none is made up.
    Today missing its end, today starting in the future and a past day are not there to read; a gap inside the day
    refuses it.
    """
    inside = sorted(instant for instant in pool if start <= instant < end)
    if not inside:
        raise _Refusal("not_published" if future else "zonneplan_unavailable")
    first = inside[0]
    if (first - start) % step:
        raise _Refusal("provider_refused")
    held: list[float] = []
    moment = first
    while moment < end and moment in pool:
        held.append(pool[moment])
        moment += step
    if moment < end:
        if any(moment < instant < end for instant in inside):
            raise _Refusal("provider_refused")
        raise _Refusal("not_published" if future else "zonneplan_unavailable")
    if first == start:
        return held
    if future:
        # Tomorrow without its first slots is not how Zonneplan publishes a day.
        raise _Refusal("provider_refused")
    if today and first <= now:
        raise _Refusal("zonneplan_partial_day")
    raise _Refusal("zonneplan_unavailable")
