"""Great Britain's Octopus Energy rates through the "Octopus Energy" integration (HACS, BottlecapDave).

SpotNav never calls Octopus itself: it reads the rates the integration the person added has already fetched for
their own meter. What the integration exposes (version 19.2, `electricity/rates_*_day.py`,
`coordinators/__init__.py`, documented in `_docs/entities/electricity.md`; checked again against main at commit
91d0fd8d47aa18dafa6a343e812dcfdec100f533, version 19.3.0, `coordinators/electricity_rates.py` and
`api_client/__init__.py` `rates_to_thirty_minute_increments`):

* per electricity meter three event entities, platform `octopus_energy`, unique ids
  `octopus_energy_electricity_{serial}_{mpan}_previous_day_rates`, `…_current_day_rates` and
  `…_next_day_rates`; an export meter has `_export` before the suffix (its rates are what a sold kWh earns, see
  [OctopusProvider.export_rates]). They are found in the entity registry by platform and unique id, never by
  entity id, which the person may rename;
* each one's attributes, set by the integration's rates coordinator (every 15 minutes) and restored after a
  restart: `rates`, a list of `{"start", "end", "value_inc_vat"}` (aware datetimes in Home Assistant's zone,
  or ISO strings when restored; the value in pounds per kWh including VAT), plus `tariff_code`, `mpan` and
  `serial_number`. "Previous", "current" and "next" are calendar days of Home Assistant's zone;
* every tariff's rates are thirty-minute slots: the integration cuts a fixed or time-of-use rate (Flux's three
  bands, Go, Cosy, a fixed or variable tariff, Economy 7's day and night) into half-hours over the whole window it
  holds, yesterday to the end of tomorrow, so a tariff known in advance has tomorrow's rates at any hour. On
  Intelligent Octopus Go the integration moves the car's dispatches into the off-peak rate as they are planned;
* the tariff code names the customer's tariff and region, `E-1R-AGILE-24-10-01-C` or
  `E-1R-FLUX-IMPORT-23-02-14-C`: product and GSP group letter `C`. Any single- or two-register electricity import
  tariff is read (not a product with `OUTGOING` or `EXPORT` in it, which is what an export meter is paid).

A GB area's day is the relay's: the Paris day, 23:00 to 23:00 UK time (`routes.cet_market_tz`), which is how
Agile is published (about 16:00 UK time for 23:00 to 23:00; a tariff known in advance is asked for from early
morning, [PUBLICATION_KNOWN]). So the three entities' rates are pooled by instant
and the Paris day cut from them: tomorrow's Paris day needs the last half-hours of the current day's rates and
the first 23 hours of the next day's. Yesterday's Paris day starts an hour before the oldest rate the integration
keeps, so no past day is served ([HISTORY_REACH]).

The region comes from the customer's meter, not from a choice: an area is served by the meter in its region;
when the person chose another GB region than their meter's and all their import meters are in one region, the
meter's rates are used for the chosen area (they are what the person pays), and [OctopusProvider.region_mismatch]
says so for the card's notice. Several import meters in different regions, none of them the chosen one, serve
nothing: there is no telling which meter the person meant.

An export meter's rates (Agile Outgoing, Flux export, Outgoing fixed) are what a sold kWh earns in each slot
([OctopusProvider.export_rates], `pricing/export_rates.py`): the export meter on the same smart meter as the
import meter read for the area (they share a serial number), else the first in that meter's region. They are kept
in pence, never converted, and remembered while Home Assistant runs for [EXPORT_KEPT] after the integration has
let them go, so a review of a few days back still prices a sold kWh at its own rate.

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
from ..export_rates import ExportRates
from ..relay_contract import AreaPublication, PriceDocument
from . import DayFetch, FailureCode, market_today, ProviderKey
from .rates import DayRate, FX_SRC, RateSource

_LOGGER = logging.getLogger(__name__)

DOMAIN: Final = "octopus_energy"

#: Agile's next day appears at about 16:00 UK time; the integration asks every 15 minutes.
PUBLICATION: Final = AreaPublication(local_time=time(16, 0), tz="Europe/London")

#: A tariff whose rates are known in advance (Flux, Go, Cosy, fixed): the integration holds tomorrow's rates from
#: its first refresh of the day, so tomorrow is asked for from early morning, when no meter read is on Agile.
PUBLICATION_KNOWN: Final = AreaPublication(local_time=time(1, 0), tz="Europe/London")

#: The integration keeps rates from the start of yesterday (UK): not a whole past Paris day.
HISTORY_REACH: Final = timedelta(0)

#: The slot every tariff's rates come in (the integration cuts them into half-hours).
SLOT: Final = timedelta(minutes=30)

#: Where the file says it came from (`src`), as the relay writes it.
SRC: Final = "octopus.energy"

#: The three event entities of one meter, by the end of their unique id.
KINDS: Final = ("previous_day_rates", "current_day_rates", "next_day_rates")

#: How long an export rate is remembered once the integration no longer holds it.
EXPORT_KEPT: Final = timedelta(days=40)

#: `E-1R-<product>-<region>` or `E-2R-…`: a single- or two-register electricity tariff (the integration's own
#: pattern, `REGEX_TARIFF_PARTS`, narrowed to electricity).
_TARIFF: Final = re.compile(r"^E-[12]R-(?P<product>[A-Z0-9-]+)-(?P<region>[A-Z])$")

#: What a product an export meter is paid on has in its code (`AGILE-OUTGOING-…`, `OUTGOING-VAR-…`, `FLUX-EXPORT-…`).
_EXPORT_PRODUCT: Final = ("OUTGOING", "EXPORT")

#: A price beyond this (in pounds per kWh) is not a rate.
_MAX_ABS_RATE: Final = 100.0


class _Refusal(Exception):
    def __init__(self, code: FailureCode) -> None:
        super().__init__(code)
        self.code: FailureCode = code


@dataclass(frozen=True, slots=True)
class OctopusMeter:
    """One electricity meter: its region, its product (`AGILE-24-10-01`), whether it is an export meter, its
    serial number (an import and an export meter on one smart meter share it) and its three rate entities
    (kind → entity id)."""

    key: str
    region: str
    entities: tuple[tuple[str, str], ...]
    product: str = ""
    export: bool = False
    serial: str | None = None

    def entity_ids(self) -> list[str]:
        return [entity_id for _, entity_id in self.entities]

    @property
    def agile(self) -> bool:
        return self.product.startswith("AGILE")


def _tariff_parts(tariff_code: Any) -> tuple[str, str] | None:
    """(product, region) of an electricity tariff code, else `None`."""
    if not isinstance(tariff_code, str):
        return None
    match = _TARIFF.match(tariff_code.strip())
    if match is None:
        return None
    return match.group("product"), match.group("region")


def _is_export_product(product: str) -> bool:
    return any(word in product for word in _EXPORT_PRODUCT)


def import_region(tariff_code: Any) -> str | None:
    """The GSP group letter of an electricity import tariff code (Agile, Flux, Go, Cosy, fixed…), else `None`."""
    parts = _tariff_parts(tariff_code)
    if parts is None or _is_export_product(parts[0]):
        return None
    return parts[1]


def export_region(tariff_code: Any) -> str | None:
    """The GSP group letter of an export tariff code (Agile Outgoing, Flux export, Outgoing fixed…), else `None`."""
    parts = _tariff_parts(tariff_code)
    if parts is None or not _is_export_product(parts[0]):
        return None
    return parts[1]


def _region_of(tariff_code: Any, export: bool) -> str | None:
    return export_region(tariff_code) if export else import_region(tariff_code)


def _meter_key(unique_id: Any) -> tuple[str, str, bool] | None:
    """(meter key, kind, whether an export meter) of a meter's rate event entity, from its unique id; else `None`."""
    if not isinstance(unique_id, str) or not unique_id.startswith("octopus_energy_electricity_"):
        return None
    for kind in KINDS:
        suffix = "_" + kind
        if unique_id.endswith(suffix):
            key = unique_id[: -len(suffix)]
            return key, kind, key.endswith("_export")
    return None


class OctopusProvider:
    """Day files from the rates the Octopus Energy integration holds for the customer's import meter."""

    key: ProviderKey = "octopus"
    #: What the card and the app call it, and where it is (attribution).
    name: str = "Octopus Energy"
    url: str = "https://octopus.energy/"
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
        self._meters: tuple[OctopusMeter, ...] = ()
        self._exports: tuple[OctopusMeter, ...] = ()
        #: Every export rate read so far (UTC start → pence per kWh), by export meter key.
        self._export_seen: dict[str, dict[datetime, float]] = {}
        #: The last [export_rates] answer and what it was read from (the entities' last updates, the day).
        self._export_cache: tuple[Any, ExportRates | None] | None = None

    # -------------------------------------------------------------- the meters

    @property
    def meters(self) -> tuple[OctopusMeter, ...]:
        """The import meters found at the last [refresh]."""
        return self._meters

    @property
    def export_meters(self) -> tuple[OctopusMeter, ...]:
        """The export meters found at the last [refresh]."""
        return self._exports

    @property
    def publication(self) -> AreaPublication:
        """When tomorrow is asked for: Agile's afternoon, or early morning when every import meter read is on a
        tariff known in advance."""
        if self._meters and not any(meter.agile for meter in self._meters):
            return PUBLICATION_KNOWN
        return PUBLICATION

    @callback
    def refresh(self) -> bool:
        """Look for the meters again (registry and states); whether what is served changed."""
        found = tuple(self._find_meters())
        meters = tuple(meter for meter in found if not meter.export)
        exports = tuple(meter for meter in found if meter.export)
        changed = meters != self._meters or exports != self._exports
        self._meters = meters
        self._exports = exports
        return changed

    def _find_meters(self) -> Iterable[OctopusMeter]:
        registry = er.async_get(self._hass)
        grouped: dict[str, dict[str, str]] = {}
        exports: set[str] = set()
        for entry in registry.entities.values():
            if entry.platform != DOMAIN or entry.domain != "event" or entry.disabled:
                continue
            found = _meter_key(entry.unique_id)
            if found is None or not self._entry_loaded(entry.config_entry_id):
                continue
            key, kind, export = found
            grouped.setdefault(key, {})[kind] = entry.entity_id
            if export:
                exports.add(key)
        for key in sorted(grouped):
            entities = grouped[key]
            export = key in exports
            current = entities.get("current_day_rates")
            state = None if current is None else self._hass.states.get(current)
            parts = None if state is None else _tariff_parts(state.attributes.get("tariff_code"))
            if parts is None or _is_export_product(parts[0]) != export:
                continue
            serial = state.attributes.get("serial_number") if state is not None else None
            yield OctopusMeter(
                key=key,
                region=parts[1],
                entities=tuple(sorted(entities.items())),
                product=parts[0],
                export=export,
                serial=serial if isinstance(serial, str) and serial else None,
            )

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

    def meter_for(self, area_id: str) -> OctopusMeter | None:
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

    def export_meter_for(self, area_id: str) -> OctopusMeter | None:
        """The export meter whose rates are a sold kWh's price for the area: the one sharing the import meter's
        serial number, else the first in the import meter's region; `None` without either."""
        meter = self.meter_for(area_id)
        if meter is None or not self._exports:
            return None
        if meter.serial is not None:
            for export in self._exports:
                if export.serial == meter.serial:
                    return export
        return next((export for export in self._exports if export.region == meter.region), None)

    def export_rates(self, area_id: str) -> ExportRates | None:
        """What a sold kWh earns per slot for the area (pence per kWh), from its export meter's rates; `None`
        without an export meter, or when its rates cannot be read."""
        export = self.export_meter_for(area_id)
        if export is None:
            return None
        now = self._now()
        stamp = (
            export.key,
            tuple(getattr(self._hass.states.get(entity_id), "last_updated", None) for entity_id in export.entity_ids()),
            now.date(),
        )
        cached = self._export_cache
        if cached is not None and cached[0] == stamp:
            return cached[1]
        seen = self._export_seen.setdefault(export.key, {})
        try:
            pool = self._pool(export)
        except _Refusal as err:
            _LOGGER.debug("Octopus export rates for %s: %s", area_id, err.code)
            pool = {}
        seen.update({start: round(value * 100, 4) for start, value in pool.items()})
        oldest = now - EXPORT_KEPT
        for start in [start for start in seen if start < oldest]:
            del seen[start]
        rates = ExportRates(dict(seen), SLOT) if seen else None
        self._export_cache = (stamp, rates)
        return rates

    # -------------------------------------------------------------- what the router and repository ask

    def has_route(self, area_id: str) -> bool:
        """Whether the area is a GB region (it has an Octopus route in the bundled catalogue)."""
        routes = self._bundled.routes.get(area_id)
        return routes is not None and routes.octopus is not None

    def serves(self, area_id: str) -> bool:
        """Whether an import meter's rates are there for the area."""
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

    def _pool(self, meter: OctopusMeter) -> dict[datetime, float]:
        """Every rate the meter's three entities hold, by start instant (UTC), in pounds per kWh.

        An entity whose tariff is of another region or kind (an import tariff on an export meter) is left out; the
        day the customer changed tariff, an entity still under the old code holds what the meter was charged then,
        and is read. Two entities that disagree about one slot refuse the pool.
        """
        pool: dict[datetime, float] = {}
        seen = False
        for _, entity_id in meter.entities:
            state = self._hass.states.get(entity_id)
            if state is None:
                continue
            if _region_of(state.attributes.get("tariff_code"), meter.export) != meter.region:
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
