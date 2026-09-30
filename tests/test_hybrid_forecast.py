"""Hybrid mode's forecast adapter (`planning/hybrid_forecast.py`).

`_async_forecast_platforms`, the seam to Home Assistant's platform loader, is monkeypatched to a
fixed `{domain: async_get_solar_forecast}` mapping: the tests cover `async_read_forecast_wh`'s own
logic (summing, per-source failure isolation, touching nothing but that read). Sources are real
`MockConfigEntry`s so `async_get_entry`/`.domain` resolve as in production.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.spotnav.planning import hybrid_forecast
from custom_components.spotnav.planning.hybrid_forecast import async_read_forecast_wh
from custom_components.spotnav.runtime import domain_data

UTC = timezone.utc
HOUR_0 = datetime(2026, 9, 28, 10, 0, tzinfo=UTC)
HOUR_1 = datetime(2026, 9, 28, 11, 0, tzinfo=UTC)
NOW = datetime(2026, 9, 28, 9, 0, tzinfo=UTC)


def _entry(hass: HomeAssistant, *, domain: str, entry_id: str) -> MockConfigEntry:
    entry = MockConfigEntry(domain=domain, entry_id=entry_id, title=f"{domain} {entry_id}")
    entry.add_to_hass(hass)
    return entry


@pytest.fixture(autouse=True)
def _fresh_platform_cache(hass: HomeAssistant) -> None:
    """Every test gets its own cache -- `_async_forecast_platforms`'s own module docstring
    is explicit that it discovers (and here, is monkeypatched) exactly once per `hass`."""
    domain_data(hass).forecast_platforms = None


def _install_platforms(monkeypatch: pytest.MonkeyPatch, platforms: dict[str, Any]) -> None:
    async def _fake(_hass: HomeAssistant) -> dict[str, Any]:
        return platforms

    monkeypatch.setattr(hybrid_forecast, "_async_forecast_platforms", _fake)


async def test_no_entries_configured_means_no_forecast(hass: HomeAssistant) -> None:
    result = await async_read_forecast_wh(hass, [], now=NOW)
    assert result.wh_hours is None
    assert result.sources_read == ()


async def test_sums_two_readable_sources(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two sources, overlapping hours, summed -- not the last one winning."""
    entry_a = _entry(hass, domain="solcast_solar", entry_id="roof_a")
    entry_b = _entry(hass, domain="forecast_solar", entry_id="roof_b")

    async def forecast_a(_hass: HomeAssistant, entry_id: str) -> dict[str, Any]:
        assert entry_id == entry_a.entry_id
        return {"wh_hours": {HOUR_0.isoformat(): 1000.0, HOUR_1.isoformat(): 2000.0}}

    async def forecast_b(_hass: HomeAssistant, entry_id: str) -> dict[str, Any]:
        assert entry_id == entry_b.entry_id
        return {"wh_hours": {HOUR_0.isoformat(): 500.0}}

    _install_platforms(
        monkeypatch, {"solcast_solar": forecast_a, "forecast_solar": forecast_b}
    )

    result = await async_read_forecast_wh(hass, [entry_a.entry_id, entry_b.entry_id], now=NOW)

    assert result.wh_hours == {HOUR_0: 1500.0, HOUR_1: 2000.0}
    assert set(result.sources_read) == {entry_a.entry_id, entry_b.entry_id}


async def test_never_calls_a_refresh(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The read reaches only `async_get_solar_forecast`, exactly once -- a coordinator behind
    it (as a real `forecast_solar`/Solcast entry would have) is never asked to refresh."""
    entry = _entry(hass, domain="forecast_solar", entry_id="roof")
    coordinator = AsyncMock()
    coordinator.data = {"wh_hours": {HOUR_0.isoformat(): 4000.0}}
    call_count = 0

    async def forecast(_hass: HomeAssistant, _entry_id: str) -> dict[str, Any]:
        nonlocal call_count
        call_count += 1
        # The real contract (`forecast_solar/energy.py`, verified in the module docstring):
        # read the coordinator's already-cached data, call nothing else on it.
        return dict(coordinator.data)

    _install_platforms(monkeypatch, {"forecast_solar": forecast})

    result = await async_read_forecast_wh(hass, [entry.entry_id], now=NOW)

    assert result.wh_hours == {HOUR_0: 4000.0}
    assert call_count == 1, "read exactly once, not polled"
    coordinator.async_refresh.assert_not_called()
    coordinator.async_request_refresh.assert_not_called()


async def test_an_unreadable_source_means_no_forecast_when_it_is_the_only_one(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No source, or none readable, both mean `forecast_wh=None` -- here, the one configured
    entry's integration has no registered platform at all."""
    entry = _entry(hass, domain="unrelated_integration", entry_id="not_solar")
    _install_platforms(monkeypatch, {})  # nothing implements the platform

    result = await async_read_forecast_wh(hass, [entry.entry_id], now=NOW)

    assert result.wh_hours is None
    assert result.sources_read == ()


@pytest.mark.parametrize(
    "break_it",
    ["returns_none", "raises", "malformed_wh_hours", "missing_entry", "naive_hour"],
)
async def test_an_unreadable_source_does_not_erase_a_readable_one(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, break_it: str
) -> None:
    """Each way one source can fail is isolated: the other source's forecast still comes
    through, summed on its own."""
    good = _entry(hass, domain="forecast_solar", entry_id="good_roof")
    bad_entry_id = "missing" if break_it == "missing_entry" else "bad_roof"
    if break_it != "missing_entry":
        _entry(hass, domain="forecast_solar", entry_id=bad_entry_id)

    # One function per domain, dispatching on `entry_id` -- exactly as the real adapter calls
    # it (`platforms[entry.domain](hass, entry_id)`, once per configured entry, whatever their
    # own domain): two entries sharing one domain (as two Solcast roofs would) share one
    # function, which is called once per entry and may behave differently for each.
    async def forecast(_hass: HomeAssistant, entry_id: str) -> Any:
        if entry_id == good.entry_id:
            return {"wh_hours": {HOUR_0.isoformat(): 3000.0}}
        if break_it == "returns_none":
            return None
        if break_it == "raises":
            raise RuntimeError("boom")
        if break_it == "malformed_wh_hours":
            return {"wh_hours": "not-a-mapping"}
        if break_it == "naive_hour":
            return {"wh_hours": {"2026-09-28T10:00:00": 999.0}}  # no tz offset
        raise AssertionError("unreachable for missing_entry")

    _install_platforms(monkeypatch, {"forecast_solar": forecast})

    result = await async_read_forecast_wh(hass, [good.entry_id, bad_entry_id], now=NOW)

    assert result.wh_hours == {HOUR_0: 3000.0}
    assert result.sources_read == (good.entry_id,)


# ---- resolution, offsets, history and a missing setup -----------------------------------------------


def slots(start: datetime, minutes: int, values: list[float], *, offset_hours: int = 0) -> dict[str, float]:
    """`{iso key: Wh}` for consecutive slots of `minutes`, keyed in a fixed UTC offset."""
    from datetime import timedelta

    zone = timezone(timedelta(hours=offset_hours))
    return {
        (start + timedelta(minutes=minutes * index)).astimezone(zone).isoformat(): value
        for index, value in enumerate(values)
    }


async def read_one(hass, monkeypatch, wh_hours: dict[str, float], *, now: datetime = NOW):
    entry = _entry(hass, domain="solcast_solar", entry_id="one_source")

    async def forecast(_hass: HomeAssistant, _entry_id: str) -> dict[str, Any]:
        return {"wh_hours": wh_hours}

    _install_platforms(monkeypatch, {"solcast_solar": forecast})
    return await async_read_forecast_wh(hass, [entry.entry_id], now=now)


async def test_half_hour_slots_add_up_to_the_hours_energy(hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch) -> None:
    """Solcast: 30-minute slots with UTC keys. Two 400 Wh slots are 800 Wh in that hour, which the
    planner (one key = one hour) reads as 800 W -- not 400 W, and not twice."""
    result = await read_one(hass, monkeypatch, slots(HOUR_0, 30, [400.0, 400.0, 600.0, 200.0]))

    assert result.wh_hours == {HOUR_0: 800.0, HOUR_1: 800.0}


async def test_quarter_hour_slots_with_local_offset_keys_land_in_the_right_utc_hour(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Forecast.Solar at 15 minutes, keyed in UTC+2."""
    result = await read_one(hass, monkeypatch, slots(HOUR_0, 15, [100.0, 200.0, 300.0, 400.0, 10.0], offset_hours=2))

    assert result.wh_hours == {HOUR_0: 1000.0, HOUR_1: 10.0}


async def test_hourly_keys_in_a_local_offset_are_normalised_to_utc(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = await read_one(hass, monkeypatch, slots(HOUR_0, 60, [500.0, 700.0], offset_hours=-5))

    assert result.wh_hours == {HOUR_0: 500.0, HOUR_1: 700.0}


async def test_a_half_hour_offset_zone_with_half_hour_slots_still_buckets_by_utc_hour(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    # India: +05:30, so local slot starts fall at :00 and :30 of the UTC hour too.
    from datetime import timedelta

    zone = timezone(timedelta(hours=5, minutes=30))
    keys = {
        (HOUR_0 + timedelta(minutes=30 * i)).astimezone(zone).isoformat(): 100.0 for i in range(4)
    }

    result = await read_one(hass, monkeypatch, keys)

    assert result.wh_hours == {HOUR_0: 200.0, HOUR_1: 200.0}


async def test_two_sources_of_different_resolution_sum_hour_by_hour(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    solcast = _entry(hass, domain="solcast_solar", entry_id="fine")
    forecast_solar = _entry(hass, domain="forecast_solar", entry_id="coarse")

    async def fine(_hass: HomeAssistant, _entry_id: str) -> dict[str, Any]:
        return {"wh_hours": slots(HOUR_0, 30, [300.0, 300.0])}

    async def coarse(_hass: HomeAssistant, _entry_id: str) -> dict[str, Any]:
        return {"wh_hours": slots(HOUR_0, 60, [1000.0, 2000.0])}

    _install_platforms(monkeypatch, {"solcast_solar": fine, "forecast_solar": coarse})

    result = await async_read_forecast_wh(hass, [solcast.entry_id, forecast_solar.entry_id], now=NOW)

    assert result.wh_hours == {HOUR_0: 1600.0, HOUR_1: 2000.0}


async def test_past_days_are_dropped_so_a_long_history_does_not_bloat_the_forecast(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Open-Meteo: hourly, with about 92 days of past slots. Only yesterday (UTC) onwards is kept."""
    from datetime import timedelta

    history = {
        (NOW.replace(hour=0) - timedelta(days=90) + timedelta(hours=h)).isoformat(): 1.0 for h in range(90 * 24)
    }
    history.update(slots(datetime(2026, 9, 27, 11, 0, tzinfo=UTC), 60, [10.0]))  # yesterday: kept
    history.update(slots(datetime(2026, 9, 26, 23, 0, tzinfo=UTC), 60, [99.0]))  # the day before: dropped
    history.update(slots(HOUR_0, 60, [2000.0, 3000.0]))

    result = await read_one(hass, monkeypatch, history)

    assert result.wh_hours is not None
    assert min(result.wh_hours) >= datetime(2026, 9, 27, 0, 0, tzinfo=UTC)
    assert result.wh_hours[datetime(2026, 9, 27, 11, 0, tzinfo=UTC)] == 10.0
    assert datetime(2026, 9, 26, 23, 0, tzinfo=UTC) not in result.wh_hours
    assert result.wh_hours[HOUR_0] == 2000.0 and result.wh_hours[HOUR_1] == 3000.0


async def test_a_source_with_only_past_slots_contributes_nothing(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = await read_one(hass, monkeypatch, slots(datetime(2026, 9, 1, 10, 0, tzinfo=UTC), 60, [500.0]))

    assert result.wh_hours is None
    assert result.sources_read == ()


async def test_an_integration_that_was_never_set_up_raises_keyerror_and_is_skipped(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Open-Meteo's `energy.py` raises `KeyError`, not `None`, when its domain was never set up."""
    broken = _entry(hass, domain="open_meteo_solar_forecast", entry_id="never_set_up")
    good = _entry(hass, domain="forecast_solar", entry_id="fine_roof")

    async def open_meteo(_hass: HomeAssistant, _entry_id: str) -> dict[str, Any]:
        raise KeyError("open_meteo_solar_forecast")

    async def forecast_solar(_hass: HomeAssistant, _entry_id: str) -> dict[str, Any]:
        return {"wh_hours": {HOUR_0.isoformat(): 1234.0}}

    _install_platforms(monkeypatch, {"open_meteo_solar_forecast": open_meteo, "forecast_solar": forecast_solar})

    result = await async_read_forecast_wh(hass, [broken.entry_id, good.entry_id], now=NOW)

    assert result.wh_hours == {HOUR_0: 1234.0}
    assert result.sources_read == (good.entry_id,)


async def test_half_hourly_and_hourly_energy_normalise_to_the_same_hours() -> None:
    """Equal energy at 30-minute and at hourly resolution is the same Wh per hour."""
    from datetime import timedelta

    from custom_components.spotnav.planning.hybrid_forecast import hourly_wh

    half_hourly = {HOUR_0: 1500.0, HOUR_0 + timedelta(minutes=30): 1500.0, HOUR_1: 500.0, HOUR_1 + timedelta(minutes=30): 500.0}
    hourly = {HOUR_0: 3000.0, HOUR_1: 1000.0}

    assert hourly_wh(half_hourly, now=NOW) == hourly
