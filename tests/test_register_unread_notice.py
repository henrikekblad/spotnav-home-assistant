"""A count that began without a reading and reads now is counted from now without saying the meter
cannot be read."""

from __future__ import annotations

from datetime import time
from typing import Any

import pytest
from freezegun import freeze_time
from homeassistant.core import HomeAssistant

from custom_components.spotnav.planning.auto_settings import EnergyBaseline
from custom_components.spotnav.runtime import domain_data
from tests.relay import serve as serve_prices

from .test_dashboard_api import NOW
from .test_replug import _car, _meter, _record_charger_commands, Car

pytestmark = pytest.mark.usefixtures("offline_relay")


def _notices(car: Car) -> list[dict[str, Any]]:
    return [line for line in car.dashboard()["status"]["lines"] if line["code"] == "remaining_need_estimated"]


async def test_a_count_begun_without_a_reading_counts_from_now_without_the_notice(
    hass: HomeAssistant, transport: Any
) -> None:
    serve_prices(transport, rising=True)
    _record_charger_commands(hass)
    with freeze_time(NOW) as frozen:
        car = await _car(hass, frozen, departure=time(20, 0), departure_enabled=True)
        key = car.baseline().departure_key
        # The epoch began while the register could not be read; 3.5 kWh were vouched for meanwhile.
        await domain_data(hass).auto_store.async_update(
            car.charger.entry_id,
            energy_baseline=EnergyBaseline(register_kwh=None, departure_key=key, delivered_kwh=3.5),
        )
        car.preview._live_baseline = None  # noqa: SLF001 - the stored record is the one in force
        _meter(hass, 1004.0)
        await hass.async_block_till_done()

        resolved: list[Any] = []
        counted = car.preview._manual_kwh_remaining  # noqa: SLF001

        async def spy(*args: Any) -> Any:
            resolved.append(await counted(*args))
            return resolved[-1]

        car.preview._manual_kwh_remaining = spy  # noqa: SLF001
        await car.preview.async_recalculate()

        assert resolved and resolved[0].delivered_energy_trustworthy is False, "hybrid credits no forecast sun"
        snapshot = car.preview.snapshot()
        assert snapshot.remaining_kwh == pytest.approx(6.5), "the kept remainder, counted from now"
        assert snapshot.energy_basis not in ("kept", "sessions")
        assert _notices(car) == []
        assert car.baseline().register_kwh == pytest.approx(1004.0)
        assert car.dashboard()["plan"]["remaining_kwh"] == pytest.approx(6.5)
