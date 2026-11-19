"""Without the recorder the statistics readers answer "no history" instead of raising."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from homeassistant.core import HomeAssistant

from custom_components.spotnav.site import energy_statistics
from custom_components.spotnav.site.heat_meter import STATUS_FOUND, HeatMeter, async_heat_hours

START = datetime(2026, 11, 1, tzinfo=timezone.utc)
END = START + timedelta(hours=3)


async def test_energy_statistics_are_empty_without_a_recorder(hass: HomeAssistant) -> None:
    assert "recorder" not in hass.config.components
    assert await energy_statistics.async_soc_statistics(hass, "sensor.soc", START, END) == ([], [])
    assert await energy_statistics.async_mean_max_statistics(hass, "sensor.p", START, END) == ([], 60)
    assert await energy_statistics.async_statistics(hass, ["sensor.e"], START, END, "hour") == {}


async def test_the_heat_meter_is_empty_without_a_recorder(hass: HomeAssistant) -> None:
    meter = HeatMeter(status=STATUS_FOUND, entity_id="sensor.heat", kind="power")
    assert await async_heat_hours(hass, meter, START, END) == {}


def test_the_notifier_instant_refuses_a_naive_time() -> None:
    from custom_components.spotnav.notifications.notifier import _instant

    assert _instant("2026-11-19T10:00:00") is None
    assert _instant("2026-11-19T10:00:00+01:00") is not None
    assert _instant(5) is None


def test_the_solar_number_refuses_infinity() -> None:
    from custom_components.spotnav.execution.solar_execution import _number

    assert _number(float("inf")) is None
    assert _number(float("-inf")) is None
    assert _number(float("nan")) is None
    assert _number(2) == 2.0
