"""Nord Pool's day-ahead prices through Home Assistant's own `nordpool` integration.

SpotNav never calls Nord Pool itself: it asks the integration the person added, through its service
`nordpool.get_prices_for_date`, which is that installation's own client. What the service takes and answers
(Home Assistant 2026.9, `homeassistant/components/nordpool/services.py`):

* `config_entry` (required; the integration allows one entry), `date` (any delivery day), `areas` (any code
  Nord Pool trades, whatever areas the entry itself was set up with) and `currency` (SpotNav always asks EUR);
* the answer is `{area: [{"start", "end", "price"}]}` for the CET delivery day, in EUR/MWh, one entry per
  interval: 96 quarter hours on an ordinary day (92 and 100 on the clock-change days), 24 hours for a day before
  Nord Pool's 15-minute market;
* an empty list when Nord Pool has nothing for the day (tomorrow before about 13:00): not published yet;
* `ServiceValidationError` with `connection_error` or `authentication_error`; the service's own schema refuses an
  area code it does not know (`vol.Invalid`), and Home Assistant raises another `ServiceValidationError` when the
  entry is not loaded;
* Nord Pool keeps about two months of history ([HISTORY_REACH]).

The answer becomes the relay's `"v": 1` day document (`pricing/day_document.py`), dated on the CET delivery day
(`routes.cet_market_tz`: the area's own zone where it keeps CET, `Europe/Brussels` for Finland, the Baltics,
Romania and Bulgaria, whose display days are then cut from two files by `market_day`), with the ECB rate for the
area's currency (`pricing/fx_rates.py`). The repository validates it with `parse_day` like any other file.

Nothing about the answer is logged or put in a code beyond a fixed label.
"""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Callable
from datetime import date, datetime, time, timedelta
from typing import Any, Final, Protocol

import voluptuous as vol
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceNotFound, ServiceValidationError
from homeassistant.util import dt as dt_util

from ..area_catalogue import BundledCatalogue
from ..day_document import build_day_document
from ..relay_contract import AreaPublication, PriceDocument
from . import DayFetch, FailureCode, ProviderKey

_LOGGER = logging.getLogger(__name__)

DOMAIN: Final = "nordpool"
SERVICE: Final = "get_prices_for_date"

#: Nord Pool's day-ahead results appear between about 12:42 and 12:57 CET; the window opens five minutes
#: before this.
PUBLICATION: Final = AreaPublication(local_time=time(12, 45), tz="Europe/Brussels")

#: How far back Nord Pool answers: 2026-09-01 was answered on 2026-10-30 and 2026-08-01 was not, so a little
#: under two months, kept on the safe side.
HISTORY_REACH: Final = timedelta(days=55)

#: One service call, bounded: the integration's own client has its timeout, this bounds the wait for it.
REQUEST_TIMEOUT_S: Final = 30.0

#: The steps a Nord Pool day comes in, in minutes.
RESOLUTIONS: Final = (15, 30, 60)

#: A rate missing when a day is built is asked of the ECB at once, but not more often than this.
FX_RETRY: Final = timedelta(minutes=15)

#: Where the file says it came from (`src`) and where its rate came from (`fx_src`).
SRC: Final = "nordpool"
FX_SRC: Final = "ECB"


class RateSource(Protocol):
    """What the provider needs from the exchange rates (`pricing/fx_rates.FxRates`)."""

    def rate_for(self, currency: str, day: date) -> tuple[float, date] | None: ...

    async def async_start(self) -> None: ...

    async def async_refresh(self) -> bool: ...


class _Refusal(Exception):
    def __init__(self, code: FailureCode) -> None:
        super().__init__(code)
        self.code: FailureCode = code


class NordPoolProvider:
    """Day files from the `nordpool` integration's service, one call per file."""

    key: ProviderKey = "nordpool"
    has_index = False
    publication: AreaPublication = PUBLICATION
    history_reach: timedelta | None = HISTORY_REACH

    def __init__(
        self,
        hass: HomeAssistant,
        bundled: BundledCatalogue,
        fx: RateSource | None,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._hass = hass
        self._bundled = bundled
        self._fx = fx
        self._now = now if now is not None else dt_util.utcnow
        self._fx_started = False
        self._fx_asked_at: datetime | None = None

    def serves(self, area_id: str) -> bool:
        """Whether Nord Pool trades the area (it has a Nord Pool code in the bundled catalogue)."""
        routes = self._bundled.routes.get(area_id)
        return routes is not None and routes.nordpool is not None

    def market_calendar(self, area_id: str) -> str | None:
        routes = self._bundled.routes.get(area_id)
        return None if routes is None else routes.cet_market_tz

    def wants_again(self, area_id: str, document: PriceDocument) -> bool:
        """A day built without its rate is asked for once more when the rate has arrived since."""
        currency = self._currency(area_id)
        if currency is None or currency == "EUR" or document.fx_rate(currency) is not None or self._fx is None:
            return False
        return self._fx.rate_for(currency, document.day) is not None

    def _currency(self, area_id: str) -> str | None:
        entry = self._bundled.catalogue.area(area_id)
        return None if entry is None else entry.currency

    def _entry_id(self) -> str | None:
        for entry in self._hass.config_entries.async_entries(DOMAIN):
            if entry.state is ConfigEntryState.LOADED:
                return entry.entry_id
        return None

    async def async_fetch_day(self, area_id: str, market_day: date) -> DayFetch:
        """One CET delivery day for an area; every expected failure is a code, never an exception."""
        routes = self._bundled.routes.get(area_id)
        entry = self._bundled.catalogue.area(area_id)
        if routes is None or routes.nordpool is None or entry is None:
            return DayFetch("failed", code="nordpool_unavailable")
        config_entry = self._entry_id()
        if config_entry is None:
            return DayFetch("failed", code="nordpool_unavailable")
        try:
            response = await self._call(config_entry, routes.nordpool, market_day)
            rows = _rows(response, routes.nordpool)
        except _Refusal as err:
            _LOGGER.debug("Nord Pool %s %s: %s", area_id, market_day, err.code)
            return DayFetch("failed", code=err.code)
        if not rows:
            return DayFetch("not_published", code="not_published")
        try:
            start, res, prices = _day(rows)
        except _Refusal as err:
            _LOGGER.debug("Nord Pool %s %s: %s", area_id, market_day, err.code)
            return DayFetch("failed", code=err.code)
        fx, fx_date = await self._rate(entry.currency, market_day)
        document = build_day_document(
            area_id=area_id,
            market_day=market_day,
            tz=entry.tz,
            market_tz=routes.cet_market_tz,
            start=start,
            res=res,
            # EUR/MWh as Nord Pool states it; the file is EUR/kWh.
            prices_eur_kwh=[price / 1000.0 for price in prices],
            fx=fx,
            fx_date=fx_date,
            fx_src=FX_SRC if fx else None,
            src=SRC,
            published=None,
            retrieved=self._now(),
        )
        return DayFetch("fetched", document=document)

    async def _call(self, config_entry: str, code: str, market_day: date) -> Any:
        data = {"config_entry": config_entry, "date": market_day.isoformat(), "areas": [code], "currency": "EUR"}
        try:
            async with asyncio.timeout(REQUEST_TIMEOUT_S):
                return await self._hass.services.async_call(
                    DOMAIN, SERVICE, data, blocking=True, return_response=True
                )
        except TimeoutError as err:
            raise _Refusal("nordpool_error") from err
        except ServiceNotFound as err:
            raise _Refusal("nordpool_unavailable") from err
        except ServiceValidationError as err:
            raise _Refusal(_validation_code(err)) from err
        except vol.Invalid as err:
            raise _Refusal("nordpool_rejected") from err
        except HomeAssistantError as err:
            raise _Refusal("nordpool_error") from err

    async def _rate(self, currency: str, market_day: date) -> tuple[dict[str, float] | None, date | None]:
        """The area's ECB rate for the day, asked of the ECB once (at most every [FX_RETRY]) when none is held."""
        if currency == "EUR" or self._fx is None:
            return None, None
        if not self._fx_started:
            # The rates' daily timer runs only for an installation that needs a rate.
            self._fx_started = True
            await self._fx.async_start()
        found = self._fx.rate_for(currency, market_day)
        now = self._now()
        if found is None and (self._fx_asked_at is None or now - self._fx_asked_at >= FX_RETRY):
            self._fx_asked_at = now
            await self._fx.async_refresh()
            found = self._fx.rate_for(currency, market_day)
        if found is None:
            return None, None
        rate, table_day = found
        return {currency: rate}, table_day


def _validation_code(err: ServiceValidationError) -> FailureCode:
    """The integration's own refusal by its translation key; anything else from Home Assistant (an entry not
    loaded or not found) means the integration is not there to ask."""
    key = getattr(err, "translation_key", None)
    if key == "connection_error":
        return "nordpool_error"
    if key == "authentication_error":
        return "nordpool_rejected"
    return "nordpool_unavailable"


def _rows(response: Any, code: str) -> list[Any]:
    """The answer's list for the asked area; an answer for another area is refused, not read."""
    if not isinstance(response, dict) or code not in response or not isinstance(response[code], list):
        raise _Refusal("provider_refused")
    return response[code]


def _instant(raw: Any) -> datetime:
    if not isinstance(raw, str):
        raise _Refusal("provider_refused")
    parsed = dt_util.parse_datetime(raw)
    if parsed is None or parsed.tzinfo is None:
        raise _Refusal("provider_refused")
    return parsed.astimezone(dt_util.UTC)


def _price(raw: Any) -> float:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not math.isfinite(raw):
        raise _Refusal("provider_refused")
    return float(raw)


def _day(rows: list[Any]) -> tuple[datetime, int, list[float]]:
    """The first instant, the step and the prices of a day's rows, in order.

    The file is positional (`start + n x res`), so the rows must be one step each, back to back: a mixed step, a
    gap, an overlap or a repeated start is refused here. Where the day starts and ends is `parse_day`'s to check
    against the market calendar.
    """
    parsed: list[tuple[datetime, datetime, float]] = []
    for row in rows:
        if not isinstance(row, dict):
            raise _Refusal("provider_refused")
        parsed.append((_instant(row.get("start")), _instant(row.get("end")), _price(row.get("price"))))
    parsed.sort(key=lambda item: item[0])
    step = parsed[0][1] - parsed[0][0]
    minutes = step.total_seconds() / 60
    if minutes not in RESOLUTIONS:
        raise _Refusal("provider_refused")
    previous_end: datetime | None = None
    for start, end, _ in parsed:
        if end - start != step:
            raise _Refusal("provider_refused")
        if previous_end is not None and start != previous_end:
            raise _Refusal("provider_refused")
        previous_end = end
    return parsed[0][0], int(minutes), [price for _, _, price in parsed]

