"""Great Britain's Agile Octopus rates through the "Octopus Energy" integration (HACS, BottlecapDave).

SpotNav never calls Octopus itself: it reads the rates the integration the person added has already fetched for
their own meter. What the integration exposes (version 19.2, `electricity/rates_*_day.py`,
`coordinators/__init__.py`, documented in `_docs/entities/electricity.md`):

* per electricity meter three event entities, platform `octopus_energy`, unique ids
  `octopus_energy_electricity_{serial}_{mpan}_previous_day_rates`, `…_current_day_rates` and
  `…_next_day_rates` (an export meter has `_export` before the suffix; it is never read). They are found in the
  entity registry by platform and unique id, never by entity id, which the person may rename;
* each one's attributes, set by the integration's rates coordinator (every 15 minutes) and restored after a
  restart: `rates`, a list of `{"start", "end", "value_inc_vat"}` (aware datetimes in Home Assistant's zone,
  or ISO strings when restored; the value in pounds per kWh including VAT), plus `tariff_code`, `mpan` and
  `serial_number`. "Previous", "current" and "next" are calendar days of Home Assistant's zone;
* the tariff code names the customer's tariff and region, `E-1R-AGILE-24-10-01-C`: product `AGILE-…` and GSP
  group letter `C`. Only an Agile import tariff is read (`AGILE` in the product, not `AGILE-OUTGOING`): any other
  Octopus tariff is not the Agile price the GB areas stand for.

A GB area's day is the relay's: the Paris day, 23:00 to 23:00 UK time (`routes.cet_market_tz`), which is how
Agile is published (about 16:00 UK time for 23:00 to 23:00). So the three entities' rates are pooled by instant
and the Paris day cut from them: tomorrow's Paris day needs the last half-hours of the current day's rates and
the first 23 hours of the next day's. Yesterday's Paris day starts an hour before the oldest rate the integration
keeps, so no past day is served ([HISTORY_REACH]).

The region comes from the customer's meter, not from a choice: an area is served by the meter in its region;
when the person chose another GB region than their meter's and all their Agile meters are in one region, the
meter's rates are used for the chosen area (they are what the person pays), and [OctopusProvider.region_mismatch]
says so for the card's notice. Several Agile meters in different regions, none of them the chosen one, serve
nothing: there is no telling which meter the person meant.

The rates are converted to EUR with the day's ECB rate, written beside them like the relay does; without a rate
the day is refused (`fx_missing`), as the relay refuses it, and asked for again. `src` is `octopus.energy`, as
the relay writes it. Nothing about the rates or the meter is logged.
"""

from __future__ import annotations

import logging
import math
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
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
from .rates import DayRate, FX_SRC, RateSource

_LOGGER = logging.getLogger(__name__)

DOMAIN: Final = "octopus_energy"

#: Agile's next day appears at about 16:00 UK time; the integration asks every 15 minutes.
PUBLICATION: Final = AreaPublication(local_time=time(16, 0), tz="Europe/London")

#: The integration keeps rates from the start of yesterday (UK): not a whole past Paris day.
HISTORY_REACH: Final = timedelta(0)

#: Agile's slot.
SLOT: Final = timedelta(minutes=30)

#: Where the file says it came from (`src`), as the relay writes it.
SRC: Final = "octopus.energy"

#: The three event entities of one meter, by the end of their unique id.
KINDS: Final = ("previous_day_rates", "current_day_rates", "next_day_rates")

#: `E-1R-<product>-<region>`: a single-rate electricity tariff (the integration's own pattern, narrowed).
_TARIFF: Final = re.compile(r"^E-1R-(?P<product>[A-Z0-9-]+)-(?P<region>[A-Z])$")

#: A price beyond this (in pounds per kWh) is not a rate.
_MAX_ABS_RATE: Final = 100.0


class _Refusal(Exception):
    def __init__(self, code: FailureCode) -> None:
        super().__init__(code)
        self.code: FailureCode = code


@dataclass(frozen=True, slots=True)
class AgileMeter:
    """One import meter on an Agile tariff: its region and its three rate entities (kind → entity id)."""

    key: str
    region: str
    entities: tuple[tuple[str, str], ...]

    def entity_ids(self) -> list[str]:
        return [entity_id for _, entity_id in self.entities]


def agile_region(tariff_code: Any) -> str | None:
    """The GSP group letter of an Agile import tariff code, else `None`."""
    if not isinstance(tariff_code, str):
        return None
    match = _TARIFF.match(tariff_code.strip())
    if match is None:
        return None
    product = match.group("product")
    if not product.startswith("AGILE") or "OUTGOING" in product:
        return None
    return match.group("region")


def _meter_key(unique_id: Any) -> tuple[str, str] | None:
    """(meter key, kind) of an import meter's rate event entity, from its unique id; else `None`."""
    if not isinstance(unique_id, str) or not unique_id.startswith("octopus_energy_electricity_"):
        return None
    for kind in KINDS:
        suffix = "_" + kind
        if unique_id.endswith(suffix):
            key = unique_id[: -len(suffix)]
            if key.endswith("_export"):
                return None
            return key, kind
    return None


class OctopusProvider:
    """Day files from the rates the Octopus Energy integration holds for the customer's Agile meter."""

    key: ProviderKey = "octopus"
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
        self._now = now if now is not None else dt_util.utcnow
        self._rate = DayRate(fx, self._now)
        self._meters: tuple[AgileMeter, ...] = ()

    # -------------------------------------------------------------- the meters

    @property
    def meters(self) -> tuple[AgileMeter, ...]:
        """The Agile import meters found at the last [refresh]."""
        return self._meters

    @callback
    def refresh(self) -> bool:
        """Look for the Agile meters again (registry and states); whether what is served changed."""
        meters = tuple(self._find_meters())
        changed = meters != self._meters
        self._meters = meters
        return changed

    def _find_meters(self) -> Iterable[AgileMeter]:
        registry = er.async_get(self._hass)
        grouped: dict[str, dict[str, str]] = {}
        for entry in registry.entities.values():
            if entry.platform != DOMAIN or entry.domain != "event" or entry.disabled:
                continue
            found = _meter_key(entry.unique_id)
            if found is None or not self._entry_loaded(entry.config_entry_id):
                continue
            key, kind = found
            grouped.setdefault(key, {})[kind] = entry.entity_id
        for key in sorted(grouped):
            entities = grouped[key]
            current = entities.get("current_day_rates")
            state = None if current is None else self._hass.states.get(current)
            region = None if state is None else agile_region(state.attributes.get("tariff_code"))
            if region is None:
                continue
            yield AgileMeter(key=key, region=region, entities=tuple(sorted(entities.items())))

    def _entry_loaded(self, entry_id: str | None) -> bool:
        if entry_id is None:
            return False
        entry = self._hass.config_entries.async_get_entry(entry_id)
        return entry is not None and entry.domain == DOMAIN and entry.state is ConfigEntryState.LOADED

    def is_rate_entity(self, entity_id: str) -> bool:
        """Whether a state change of [entity_id] may change the meters (one of the integration's rate events)."""
        if not entity_id.startswith("event."):
            return False
        entry = er.async_get(self._hass).async_get(entity_id)
        return entry is not None and entry.platform == DOMAIN and _meter_key(entry.unique_id) is not None

    def meter_for(self, area_id: str) -> AgileMeter | None:
        """The meter whose rates an area is read from: the one in its region, else the only region's."""
        routes = self._bundled.routes.get(area_id)
        if routes is None or routes.octopus is None or not self._meters:
            return None
        for meter in self._meters:
            if meter.region == routes.octopus:
                return meter
        if len({meter.region for meter in self._meters}) == 1:
            return self._meters[0]
        return None

    def region_mismatch(self, area_id: str) -> str | None:
        """The meter's region when an area is read from a meter in another region (for the card's notice)."""
        routes = self._bundled.routes.get(area_id)
        meter = self.meter_for(area_id)
        if routes is None or meter is None or meter.region == routes.octopus:
            return None
        return meter.region

    # -------------------------------------------------------------- what the router and repository ask

    def serves(self, area_id: str) -> bool:
        """Whether an Agile meter's rates are there for the area."""
        return self.meter_for(area_id) is not None

    def market_calendar(self, area_id: str) -> str | None:
        routes = self._bundled.routes.get(area_id)
        return None if routes is None else routes.cet_market_tz

    def wants_again(self, area_id: str, document: PriceDocument) -> bool:
        """A day is never held without its rate (it is refused), so never asked for twice."""
        return False

    async def async_fetch_day(self, area_id: str, market_day: date) -> DayFetch:
        """One Paris day for a GB area; every expected failure is a code, never an exception."""
        routes = self._bundled.routes.get(area_id)
        entry = self._bundled.catalogue.area(area_id)
        if routes is None or routes.octopus is None or entry is None:
            return DayFetch("failed", code="source_missing")
        meter = self.meter_for(area_id)
        if meter is None:
            return DayFetch("failed", code="octopus_unavailable")
        zone = time_zones.require_zone(routes.cet_market_tz)
        start = datetime.combine(market_day, time(0), tzinfo=zone).astimezone(dt_util.UTC)
        end = datetime.combine(market_day + timedelta(days=1), time(0), tzinfo=zone).astimezone(dt_util.UTC)
        today = market_today(self._now(), routes.cet_market_tz)
        try:
            pool = self._pool(meter)
            prices = _cut(pool, start, end, future=market_day > today, past=market_day < today)
        except _Refusal as err:
            _LOGGER.debug("Octopus %s %s: %s", area_id, market_day, err.code)
            if err.code == "not_published":
                return DayFetch("not_published", code="not_published")
            return DayFetch("failed", code=err.code)
        fx, fx_date = await self._rate.async_rate(entry.currency, market_day)
        rate = None if not fx else fx.get(entry.currency)
        if entry.currency != "EUR" and not rate:
            # A EUR price is the contract, and a guessed rate would be a wrong price: refused, asked again.
            return DayFetch("failed", code="fx_missing")
        document = build_day_document(
            area_id=area_id,
            market_day=market_day,
            tz=entry.tz,
            market_tz=routes.cet_market_tz,
            start=start,
            res=int(SLOT.total_seconds() // 60),
            prices_eur_kwh=[price / rate if rate else price for price in prices],
            fx=fx,
            fx_date=fx_date,
            fx_src=FX_SRC if fx else None,
            src=SRC,
            published=None,
            retrieved=self._now(),
        )
        return DayFetch("fetched", document=document)

    def _pool(self, meter: AgileMeter) -> dict[datetime, float]:
        """Every Agile rate the meter's three entities hold, by start instant (UTC), in pounds per kWh.

        An entity whose rates belong to another tariff or region (the day the customer changed tariff) is left
        out; two entities that disagree about one slot refuse the pool.
        """
        pool: dict[datetime, float] = {}
        seen = False
        for _, entity_id in meter.entities:
            state = self._hass.states.get(entity_id)
            if state is None:
                continue
            if agile_region(state.attributes.get("tariff_code")) != meter.region:
                continue
            rates = state.attributes.get("rates")
            if rates is None:
                continue
            if not isinstance(rates, list):
                raise _Refusal("provider_refused")
            seen = True
            for row in rates:
                slot_start, value = _rate_row(row)
                held = pool.get(slot_start)
                if held is not None and held != value:
                    raise _Refusal("provider_refused")
                pool[slot_start] = value
        if not seen:
            raise _Refusal("octopus_unavailable")
        return pool


def _instant(raw: Any) -> datetime:
    if isinstance(raw, str):
        raw = dt_util.parse_datetime(raw)
    if not isinstance(raw, datetime) or raw.tzinfo is None:
        raise _Refusal("provider_refused")
    return raw.astimezone(dt_util.UTC)


def _rate_row(row: Any) -> tuple[datetime, float]:
    """One rate as (start, pounds per kWh); a slot that is not thirty minutes or a value that is not a number is
    refused."""
    if not isinstance(row, dict):
        raise _Refusal("provider_refused")
    slot_start = _instant(row.get("start"))
    slot_end = _instant(row.get("end"))
    if slot_end - slot_start != SLOT:
        raise _Refusal("provider_refused")
    value = row.get("value_inc_vat")
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise _Refusal("provider_refused")
    if abs(value) > _MAX_ABS_RATE:
        raise _Refusal("provider_refused")
    return slot_start, float(value)


def _cut(pool: dict[datetime, float], start: datetime, end: datetime, *, future: bool, past: bool) -> list[float]:
    """The Paris day's thirty-minute prices, in order.

    A day after the market's today with nothing held is "not published yet", and so is a strict leading part of
    today's or tomorrow's day (publication in progress, as the relay reads it). Today without its first slot means
    the integration has not got the earlier rates (yet): not there to read. A gap inside the day refuses it, and
    so does a past day that stops early.
    """
    slots: list[float] = []
    moment = start
    while moment < end and moment in pool:
        slots.append(pool[moment])
        moment += SLOT
    if moment >= end:
        return slots
    if not slots:
        raise _Refusal("not_published" if future else "octopus_unavailable")
    if past or any(moment < instant < end for instant in pool):
        raise _Refusal("provider_refused")
    raise _Refusal("not_published")
