"""Stand-in for Home Assistant's own Nord Pool integration: its `nordpool.get_prices_for_date` service only.

SpotNav gets day-ahead prices through that service (since 2.0 nothing comes from the SpotNav relay). This one
answers for any area and date in the shape the real service has (`{area: [{start, end, price}]}`, EUR/MWh, one row
per quarter hour of the CET delivery day): the recorded SE4 day from `tests/fixtures/relay` (`DOCS_PRICE_FIXTURE`),
repeated over the day's quarters. Tomorrow is a rotated, slightly scaled copy so the two days differ, and is left
out (an empty list, "not published yet") with `STUB_TOMORROW=0`.
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse

DOMAIN = "nordpool"
CET = ZoneInfo("Europe/Brussels")
STOCKHOLM = ZoneInfo("Europe/Stockholm")

SCHEMA = vol.Schema(
    {
        vol.Required("config_entry"): str,
        vol.Required("date"): vol.Coerce(date.fromisoformat),
        vol.Optional("areas"): vol.All(vol.Coerce(list), [str]),
        vol.Optional("currency"): str,
    }
)


def _recorded() -> list[float]:
    path = Path(os.environ["DOCS_PRICE_FIXTURE"])
    return json.loads(path.read_text(encoding="utf-8"))["prices"]


def _rows(day: date, prices: list[float]) -> list[dict[str, object]]:
    today = datetime.now(STOCKHOLM).date()
    if day > today and os.environ.get("STUB_TOMORROW", "1") == "0":
        return []
    if day > today + timedelta(days=1):
        return []
    shift, scale = (10, 0.85) if day > today else (0, 1.0)
    start = datetime(day.year, day.month, day.day, tzinfo=CET)
    end = start + timedelta(days=1)
    rows = []
    at = start
    index = 0
    while at < end:
        nxt = (at.astimezone(ZoneInfo("UTC")) + timedelta(minutes=15)).astimezone(CET)
        eur_kwh = prices[(index + shift) % len(prices)] * scale
        rows.append({"start": at.isoformat(), "end": nxt.isoformat(), "price": round(eur_kwh * 1000, 2)})
        at = nxt
        index += 1
    return rows


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    prices = await hass.async_add_executor_job(_recorded)

    async def get_prices_for_date(call: ServiceCall) -> ServiceResponse:
        rows = _rows(call.data["date"], prices)
        return {area: list(rows) for area in call.data.get("areas", ["SE4"])}

    hass.services.async_register(
        DOMAIN, "get_prices_for_date", get_prices_for_date, schema=SCHEMA, supports_response=SupportsResponse.ONLY
    )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    hass.services.async_remove(DOMAIN, "get_prices_for_date")
    return True
