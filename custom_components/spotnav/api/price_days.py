"""The webhook's `price_days` read: the charger's prices for today and tomorrow, as the relay serves them.

The paired app reads its charger's prices from Home Assistant instead of the relay. The answer carries the very
documents the relay publishes, so the app's relay parsers read it unchanged:

    {"ok": true, "action": "price_days", "v": 1,
     "area": <the area's v2 area-list entry, with the market calendar and publication of its provider now>,
     "provider": {"key", "name", "url", "notice": null},
     "state", "reason", "waiting_for_tomorrow",
     "publication": {"time": "HH:MM", "tz"},
     "files": {"YYYY-MM-DD": <the held "v": 1 market-day file>, ...},
     "profile": <the "v": 1 history profile> | null}

* The area is the charger's price source's (`pricing/price_source.py`): its electricity price entry, else its own
  settings.
* `files` are the held market-day files that cover today and tomorrow on the area's display calendar, keyed by
  market date: two for an area whose calendars agree, up to three for one whose files are dated on another
  calendar (a Finnish day from Nord Pool's CET files, a London day from Paris files). A file not held is absent.
* `state`, `reason` and `waiting_for_tomorrow` are the area's combined price state, as the dashboard's `prices`
  block states them (`pricing/price_refresh.py`, `combined_state`).
* `profile` is the history profile built from the held files (`pricing/price_profile.py`) in the relay's
  `profile.json` shape, `null` while fewer than 21 of the last 28 days are held.
* `provider.notice` is always `null` for now (notices come with the choice of provider).

Read-only and never a request: what is held is answered. Refusals are HTTP 400 with a stable code and the action:
`spotnav_no_price_area` (nothing names an area for this charger), `spotnav_unknown_price_area` (no area list held or
shipped lists it), `spotnav_no_prices` (no file for today or tomorrow is held), `spotnav_unsupported_api_version`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Final

from aiohttp import web
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .. import time_zones
from ..pricing.market_day import market_days_for, principal_market_day
from ..pricing.price_refresh import combined_state
from ..pricing.price_repository import PriceRepository
from ..pricing.price_source import price_source_for
from ..pricing.relay_contract import AreaEntry, PriceProfile
from ..runtime import ChargerConfigEntry, domain_data
from .common import ERROR_UNSUPPORTED_VERSION

ACTION: Final = "price_days"
#: The answer's own version (`"v"`), and the only `api_version` a request may name.
PRICE_DAYS_VERSION: Final = 1

ERROR_NO_PRICE_AREA: Final = "spotnav_no_price_area"
ERROR_UNKNOWN_PRICE_AREA: Final = "spotnav_unknown_price_area"
ERROR_NO_PRICES: Final = "spotnav_no_prices"

#: What each provider is called and where it is, for attribution. Until the providers state it themselves.
PROVIDER_NAMES: Final = {
    "relay": ("SpotNav relay", "https://spotnav.sensnology.se"),
    "nordpool": ("Nord Pool", "https://www.nordpoolgroup.com/"),
    "entsoe": ("ENTSO-E Transparency Platform", "https://transparency.entsoe.eu/"),
    "pvpc": ("Red Eléctrica (ESIOS) – PVPC", "https://www.esios.ree.es/es/pvpc"),
    "octopus": ("Octopus Energy (Agile)", "https://octopus.energy/smart/agile/"),
}


@dataclass(frozen=True, slots=True)
class PriceDaysRefusal:
    """Why there is no answer: a stable code."""

    code: str


def serialize_area(area: AreaEntry, provider_key: str) -> dict[str, Any]:
    """One area as the relay's v2 area list states it (`docs/prices.md`), read by the same parser.

    An optional figure that is not known is left out (absent and zero are different facts); `market_tz` is always
    written.
    """
    entry: dict[str, Any] = {"id": area.id}
    if area.eic is not None:
        entry["eic"] = area.eic
    entry.update(
        {
            "countries": list(area.countries),
            "name": area.name,
            "tz": area.tz,
            "market_tz": area.market_tz,
            "currency": area.currency,
            "major_unit": area.major_unit,
            "minor_unit": area.minor_unit,
        }
    )
    for key in ("vat_percent", "suggested_tax", "suggested_grid_fee"):
        value = getattr(area, key)
        if value is not None:
            entry[key] = value
    if area.included:
        entry["included"] = list(area.included)
    entry["publication"] = serialize_publication(area)
    entry["source"] = serialize_source(area, provider_key)
    return entry


def serialize_source(area: AreaEntry, provider_key: str) -> dict[str, str]:
    """Where the area's prices come from: the relay's own attribution while the relay serves them and its list
    states one (the upstream it read them from), else the provider's."""
    if provider_key == "relay" and area.source is not None:
        return {"name": area.source.name, "url": area.source.url}
    name, url = PROVIDER_NAMES.get(provider_key, PROVIDER_NAMES["relay"])
    return {"name": name, "url": url}


def serialize_publication(area: AreaEntry) -> dict[str, str]:
    return {"time": area.publication.local_time.strftime("%H:%M"), "tz": area.publication.tz}


def serialize_provider(provider_key: str) -> dict[str, Any]:
    name, url = PROVIDER_NAMES.get(provider_key, (provider_key, None))
    return {"key": provider_key, "name": name, "url": url, "notice": None}


def serialize_profile(profile: PriceProfile) -> dict[str, Any]:
    """The history profile as the relay's `/v1/<area>/profile.json`, hours by weekday then hour."""
    return {
        "v": profile.version,
        "area": profile.area_id,
        "tz": profile.tz,
        "unit": profile.unit,
        "generated": profile.generated.isoformat(),
        "from": profile.from_date.isoformat(),
        "to": profile.to_date.isoformat(),
        "weeks": profile.weeks,
        "hours": [
            {"weekday": weekday, "hour": hour, "median": entry.median, "std": entry.std, "n": entry.n}
            for (weekday, hour), entry in sorted(profile.hours.items())
        ],
    }


def covering_market_days(today: date, tz: str, market_tz: str) -> tuple[date, ...]:
    """The market days whose files cover the display dates today and tomorrow, in order, each once."""
    days: list[date] = []
    for day in (today, today + timedelta(days=1)):
        for market_day in market_days_for(day, tz, market_tz):
            if market_day not in days:
                days.append(market_day)
    return tuple(days)


async def async_price_days_answer(
    repository: PriceRepository | None, area_id: str | None, now: datetime
) -> dict[str, Any] | PriceDaysRefusal:
    """The answer's fields for [area_id] at [now] (see the module), or why there is none. No request."""
    if not area_id:
        return PriceDaysRefusal(ERROR_NO_PRICE_AREA)
    if repository is None:
        return PriceDaysRefusal(ERROR_NO_PRICES)
    await repository.async_restore()
    area = repository.area_entry(area_id)
    if area is None:
        return PriceDaysRefusal(ERROR_UNKNOWN_PRICE_AREA)
    zone = time_zones.zone(area.tz)
    if zone is None:
        return PriceDaysRefusal(ERROR_UNKNOWN_PRICE_AREA)
    today = now.astimezone(zone).date()
    tomorrow = today + timedelta(days=1)

    files: dict[str, Any] = {}
    for market_day in covering_market_days(today, area.tz, area.market_tz):
        document = repository.file_document(area_id, market_day)
        if document is not None:
            files[market_day.isoformat()] = document
    if not files:
        return PriceDaysRefusal(ERROR_NO_PRICES)

    today_snapshot = repository.day_snapshot(area_id, today)
    tomorrow_snapshot = repository.day_snapshot(area_id, tomorrow)
    tomorrow_authority = repository.file_authority(
        area_id, principal_market_day(tomorrow, area.tz, area.market_tz)
    )
    state, reason = combined_state(
        today_state=today_snapshot.state,
        tomorrow_state=tomorrow_snapshot.state,
        today_authority=repository.file_authority(area_id, principal_market_day(today, area.tz, area.market_tz)),
        tomorrow_authority=tomorrow_authority,
    )
    provider_key = repository.provider_for(area_id).key
    profile = await repository.async_get_profile(area_id)
    return {
        "v": PRICE_DAYS_VERSION,
        "area": serialize_area(area, provider_key),
        "provider": serialize_provider(provider_key),
        "state": state,
        "reason": reason,
        "waiting_for_tomorrow": tomorrow_authority == "not_listed",
        "publication": serialize_publication(area),
        "files": files,
        "profile": None if profile is None else serialize_profile(profile),
    }


def _refused(code: str) -> web.Response:
    return web.json_response({"ok": False, "error": code, "action": ACTION}, status=400)


async def webhook_price_days(hass: HomeAssistant, entry: ChargerConfigEntry, payload: dict[str, Any]) -> web.Response:
    """The webhook's `price_days`: this charger's price source's held days and profile."""
    version = payload.get("api_version", PRICE_DAYS_VERSION)
    if type(version) is not int or version != PRICE_DAYS_VERSION:
        return _refused(ERROR_UNSUPPORTED_VERSION)
    source = price_source_for(hass, entry.entry_id)
    answer = await async_price_days_answer(
        domain_data(hass).price_repository, None if source is None else source.area_id, dt_util.utcnow()
    )
    if isinstance(answer, PriceDaysRefusal):
        return _refused(answer.code)
    return web.json_response({"ok": True, "action": ACTION, **answer})

