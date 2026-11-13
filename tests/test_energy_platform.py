"""SpotNav's corrected solar forecast as an Energy dashboard source (`energy.py`), and the guard that keeps it from
being read back."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from homeassistant.core import HomeAssistant

from custom_components.spotnav import energy
from custom_components.spotnav.const import CONF_SOLAR_FORECAST_ENTRIES, DOMAIN
from custom_components.spotnav.planning.hybrid_forecast import (
    async_forecast_capable_domains,
    async_read_forecast_wh,
)
from custom_components.spotnav.runtime import site_controller_for

from .test_demand_site import pilot_site

pytestmark = pytest.mark.usefixtures("offline_relay")


async def _site(hass: HomeAssistant, freezer):
    entry = await pilot_site(hass, freezer, **{CONF_SOLAR_FORECAST_ENTRIES: ["solcast1"]})
    return entry, site_controller_for(hass, entry.entry_id).solar_correction


async def test_a_learnt_correction_is_offered_as_wh_per_utc_hour_from_the_running_hour(
    hass: HomeAssistant, freezer
) -> None:
    entry, correction = await _site(hass, freezer)
    now = datetime.now(UTC)
    hour = now.replace(minute=0, second=0, microsecond=0)
    correction.sun_kwh = {hour - timedelta(hours=1): 9.0, hour: 1.25, hour + timedelta(hours=1): 0.0}
    correction.sun_basis = "corrected"
    correction.last_refresh = now

    forecast = await energy.async_get_solar_forecast(hass, entry.entry_id)

    assert forecast == {"wh_hours": {hour.isoformat(): 1250.0, (hour + timedelta(hours=1)).isoformat(): 0.0}}


async def test_nothing_is_offered_while_learning_when_stale_or_for_other_entries(hass: HomeAssistant, freezer) -> None:
    entry, correction = await _site(hass, freezer)
    now = datetime.now(UTC)
    hour = now.replace(minute=0, second=0, microsecond=0)
    correction.sun_kwh = {hour: 1.0}
    correction.last_refresh = now

    correction.sun_basis = "raw"
    assert await energy.async_get_solar_forecast(hass, entry.entry_id) is None
    correction.sun_basis = "corrected"
    assert await energy.async_get_solar_forecast(hass, entry.entry_id) is not None
    correction.last_refresh = now - timedelta(hours=4)
    assert await energy.async_get_solar_forecast(hass, entry.entry_id) is None

    others = [other for other in hass.config_entries.async_entries(DOMAIN) if other.entry_id != entry.entry_id]
    assert others
    for other in others:
        assert await energy.async_get_solar_forecast(hass, other.entry_id) is None
    assert await energy.async_get_solar_forecast(hass, "no_such_entry") is None


async def test_the_site_without_a_solar_forecast_offers_nothing(hass: HomeAssistant, freezer) -> None:
    entry = await pilot_site(hass, freezer)

    assert await energy.async_get_solar_forecast(hass, entry.entry_id) is None


async def test_spotnav_never_reads_its_own_platform_back(hass: HomeAssistant, freezer) -> None:
    entry, correction = await _site(hass, freezer)
    now = datetime.now(UTC)
    correction.sun_kwh = {now.replace(minute=0, second=0, microsecond=0): 1.0}
    correction.sun_basis = "corrected"
    correction.last_refresh = now
    assert await energy.async_get_solar_forecast(hass, entry.entry_id) is not None

    assert DOMAIN not in await async_forecast_capable_domains(hass)
    result = await async_read_forecast_wh(hass, [entry.entry_id], now=now)
    assert result.wh_hours is None and result.sources_read == ()
