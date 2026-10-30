"""The ECB rate a provider without the relay writes into a day it builds (Nord Pool, ENTSO-E).

Both providers read prices in EUR and attach the area's own ECB reference rate (`pricing/fx_rates.py`), as the
relay does: the rate of the newest table at most a week before the day, with its date. The rates' daily timer is
started by the first day that needs one, so an installation priced in EUR never asks the ECB.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, timedelta
from typing import Final, Protocol

#: A rate missing when a day is built is asked of the ECB at once, but not more often than this.
FX_RETRY: Final = timedelta(minutes=15)

#: Where a day's rate came from (`fx_src`).
FX_SRC: Final = "ECB"


class RateSource(Protocol):
    """What a provider needs from the exchange rates (`pricing/fx_rates.FxRates`)."""

    def rate_for(self, currency: str, day: date) -> tuple[float, date] | None: ...

    async def async_start(self) -> None: ...

    async def async_refresh(self) -> bool: ...


class DayRate:
    """One provider's way to the rate of a day it builds."""

    def __init__(self, fx: RateSource | None, now: Callable[[], datetime]) -> None:
        self._fx = fx
        self._now = now
        self._started = False
        self._asked_at: datetime | None = None

    def arrived(self, currency: str | None, day: date, held: float | None) -> bool:
        """Whether a day built without its rate ([held] is `None`) could have it now."""
        if currency is None or currency == "EUR" or held is not None or self._fx is None:
            return False
        return self._fx.rate_for(currency, day) is not None

    async def async_rate(self, currency: str, market_day: date) -> tuple[dict[str, float] | None, date | None]:
        """The area's rate for the day and its table's date, asked of the ECB once (at most every [FX_RETRY])
        when none is held; `(None, None)` for EUR or when there is none."""
        if currency == "EUR" or self._fx is None:
            return None, None
        if not self._started:
            # The rates' daily timer runs only for an installation that needs a rate.
            self._started = True
            await self._fx.async_start()
        found = self._fx.rate_for(currency, market_day)
        now = self._now()
        if found is None and (self._asked_at is None or now - self._asked_at >= FX_RETRY):
            self._asked_at = now
            await self._fx.async_refresh()
            found = self._fx.rate_for(currency, market_day)
        if found is None:
            return None, None
        rate, table_day = found
        return {currency: rate}, table_day
