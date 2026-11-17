"""A battery size nobody states, learned from charging (`learned_energy.learned_capacity_kwh`).

* "Fill" plans the room to the car's limit with it, and a target in percent becomes kWh with it.
* The status says the size is learned (`battery_size_learned`); `fill_room_unknown` stays for when nothing is.
* A stated size (a setting, the car's integration, a profile) always wins; an insane learned figure is ignored.
* The settings' vehicle row carries it as a hint, never as the stored value.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest
from freezegun import freeze_time
from homeassistant.core import HomeAssistant

from custom_components.spotnav.api import dashboard as dashboard_api
from custom_components.spotnav.planning.auto_settings import DRIVER_MANUAL_KWH, TargetSocIntent
from custom_components.spotnav.planning.learned_energy import EnergySample
from custom_components.spotnav.runtime import preview_for
from custom_components.spotnav.vehicles.soc_estimate import CHARGE_EFFICIENCY
from tests.relay import serve as serve_prices
from tests.test_dashboard_api import NOW

from .world import charger_and_car

pytestmark = pytest.mark.usefixtures("offline_relay")

START = datetime(2026, 9, 22, 1, tzinfo=timezone.utc)
#: One good charge of the user's car: 29.1 kWh from the wall for 35 points, about 0.83 kWh per percent.
SAMPLE_KWH, SAMPLE_DELTA = 29.1, 35.0
RATIO = SAMPLE_KWH / SAMPLE_DELTA
LEARNED_CAPACITY = RATIO * 100.0 * CHARGE_EFFICIENCY


def _learn(preview: Any, vehicle_id: str, *, kwh: float = SAMPLE_KWH, capacity: float | None = None) -> None:
    store = preview._learned_energy  # noqa: SLF001
    assert store.put(vehicle_id, EnergySample("plug_in:1", kwh, SAMPLE_DELTA, START), capacity)


def _payload(hass: HomeAssistant, charger: Any) -> dict[str, Any]:
    return dashboard_api.serialize_dashboard(dashboard_api.capture_dashboard(hass, charger), can_act=True)


def _codes(payload: dict[str, Any]) -> dict[str, Any]:
    return {line["code"]: line["params"] for line in payload["status"]["lines"]}


async def _fill(hass: HomeAssistant, capacity: float | None) -> tuple[Any, str, Any]:
    charger, device_id, _ = await charger_and_car(
        hass, capacity=capacity, soc_percent="11", driver=DRIVER_MANUAL_KWH, requested_kwh=22.4,
        target=TargetSocIntent(), amps=16, phases=3, fill_to_limit=True,
    )
    await hass.async_block_till_done()
    return charger, device_id, preview_for(hass, charger.entry_id)


async def test_fill_plans_the_room_with_the_learned_size_and_says_so(hass: HomeAssistant, transport: Any) -> None:
    serve_prices(transport, rising=True)
    with freeze_time(NOW):
        charger, device_id, preview = await _fill(hass, None)
        assert preview.snapshot().fill == "unknown_room"

        _learn(preview, device_id)
        await preview.async_recalculate()
        snapshot = preview.snapshot()
        room = RATIO * 89.0
        assert snapshot.proposal is not None
        assert snapshot.proposal.requested_kwh == pytest.approx(room, abs=0.01)
        assert snapshot.fill == "battery" and snapshot.room_limited
        assert snapshot.learned_capacity_kwh == pytest.approx(LEARNED_CAPACITY)
        payload = _payload(hass, charger)
        codes = _codes(payload)
        assert codes["filling_to_limit"] == {"kwh": round(room, 1)}
        assert codes["battery_size_learned"] == {"kwh": round(LEARNED_CAPACITY)}
        assert "fill_room_unknown" not in codes
        # The card's figures come from the same size; the settings row shows it as a hint only.
        assert payload["soc"]["capacity_kwh"] == pytest.approx(LEARNED_CAPACITY)
        assert payload["soc"]["room_kwh"] == pytest.approx(room, abs=0.01)
        row = next(row for row in payload["vehicles"] if row["id"] == device_id)
        assert row["capacity_kwh"] is None and row["capacity_source"] is None
        assert row["learned_capacity_kwh"] == pytest.approx(round(LEARNED_CAPACITY, 1))


async def test_without_anything_learned_fill_plans_the_stored_amount(hass: HomeAssistant, transport: Any) -> None:
    serve_prices(transport, rising=True)
    with freeze_time(NOW):
        charger, device_id, preview = await _fill(hass, None)
        snapshot = preview.snapshot()
        assert snapshot.proposal is not None and snapshot.proposal.requested_kwh == pytest.approx(22.4)
        assert snapshot.fill == "unknown_room" and snapshot.learned_capacity_kwh is None
        payload = _payload(hass, charger)
        codes = _codes(payload)
        assert codes["fill_room_unknown"] == {"kwh": 22.4} and "battery_size_learned" not in codes
        row = next(row for row in payload["vehicles"] if row["id"] == device_id)
        assert row["learned_capacity_kwh"] is None


async def test_a_stated_size_wins_over_the_learned_one(hass: HomeAssistant, transport: Any) -> None:
    serve_prices(transport, rising=True)
    with freeze_time(NOW):
        charger, device_id, preview = await _fill(hass, 80.7)
        _learn(preview, device_id, capacity=80.7)
        await preview.async_recalculate()
        snapshot = preview.snapshot()
        assert snapshot.proposal is not None
        assert snapshot.proposal.requested_kwh == pytest.approx(80.7 * 0.89 / CHARGE_EFFICIENCY, abs=0.01)
        assert snapshot.learned_capacity_kwh is None
        payload = _payload(hass, charger)
        assert "battery_size_learned" not in _codes(payload)
        row = next(row for row in payload["vehicles"] if row["id"] == device_id)
        assert row["capacity_kwh"] == 80.7 and row["learned_capacity_kwh"] is None


async def test_an_insane_learned_size_is_ignored(hass: HomeAssistant, transport: Any) -> None:
    serve_prices(transport, rising=True)
    with freeze_time(NOW):
        charger, device_id, preview = await _fill(hass, None)
        # 4.5 kWh per percent: fine for a 400 kWh battery someone once stated, never a size of its own.
        _learn(preview, device_id, kwh=4.5 * SAMPLE_DELTA, capacity=400.0)
        await preview.async_recalculate()
        snapshot = preview.snapshot()
        assert snapshot.fill == "unknown_room" and snapshot.learned_capacity_kwh is None
        codes = _codes(_payload(hass, charger))
        assert "fill_room_unknown" in codes and "battery_size_learned" not in codes


async def test_a_target_in_percent_with_the_learned_size(hass: HomeAssistant, transport: Any) -> None:
    serve_prices(transport, rising=True)
    with freeze_time(NOW):
        charger, device_id, _ = await charger_and_car(hass, capacity=None, soc_percent="20", amps=16, phases=3)
        preview = preview_for(hass, charger.entry_id)
        await hass.async_block_till_done()
        assert preview.snapshot().reason == "target_capacity_unknown"
        assert not dashboard_api.target_soc_capable(hass, charger.entry_id)

        _learn(preview, device_id)
        await preview.async_recalculate()
        snapshot = preview.snapshot()
        assert snapshot.proposal is not None
        assert snapshot.proposal.requested_kwh == pytest.approx(60.0 * RATIO, abs=0.01)
        assert snapshot.learned_capacity_kwh == pytest.approx(LEARNED_CAPACITY)
        assert dashboard_api.target_soc_capable(hass, charger.entry_id)
        payload = _payload(hass, charger)
        assert _codes(payload)["battery_size_learned"] == {"kwh": round(LEARNED_CAPACITY)}
        assert payload["soc"]["need_kwh"] == pytest.approx(60.0 * RATIO, abs=0.01)
