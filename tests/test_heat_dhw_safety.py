"""The hot water's safety review fixes (`plans/review_dhw_2026-11-19.md`): each finding reproduced on the pilot's
site, then held."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest
from homeassistant.core import HomeAssistant

from custom_components.spotnav.heat.adapters import CTC_BMS, MYUPLINK, NIBE_F
from custom_components.spotnav.heat.dhw_ledger import SOURCE_PLAN

from .test_heat_dhw_runtime import LUX, LUX_OPTIONS, Writes, _live_pick, _open_gate, _pump, _site, settle

pytestmark = pytest.mark.usefixtures("offline_relay")

CTC = "number.ctc_extra_hot_water_1006"
CLOUD = "switch.f730_temporary_lux_50004"


async def _live(hass: HomeAssistant, freezer: Any, monkeypatch: pytest.MonkeyPatch, pump: Any, **heat: Any) -> tuple[Any, Any, datetime]:
    """A site steering its hot water live, a pick due now, the meter fresh and room on the fuse."""
    site, controller = await _site(hass, freezer, monkeypatch, pump, control_enabled=True, **heat)
    hot_water = controller.heat_shadow.hot_water
    monkeypatch.setattr(hot_water, "_meter_fresh", lambda _now: True)
    monkeypatch.setattr(hot_water, "_margins", lambda: {"l1": 20.0, "l2": 20.0, "l3": 20.0})
    now = controller.demand.now()
    _open_gate(hot_water, now.astimezone(hot_water.zone).date())
    pick = _live_pick(hot_water, now, timedelta(0))
    hot_water.picks = [pick]
    return controller, hot_water, max(now, pick.start)


# ---- B1: a planned boost never writes over a boost already running


@pytest.mark.parametrize(
    ("adapter", "entity_id", "value", "attributes"),
    [
        (NIBE_F, LUX, "3 hrs", {"options": LUX_OPTIONS}),
        (CTC_BMS, CTC, "3", {}),
        (MYUPLINK, CLOUD, "on", {}),
    ],
)
async def test_b1_a_running_boost_is_never_written_over(
    hass: HomeAssistant, freezer, monkeypatch, adapter: Any, entity_id: str, value: str, attributes: dict[str, Any]
) -> None:
    controller, hot_water, at = await _live(hass, freezer, monkeypatch, _pump(adapter, entity_id))
    hass.states.async_set(entity_id, value, attributes)
    writes = Writes(hass)
    hot_water.tick(at)
    await settle(hass)
    assert writes.calls == []
    # The peak counts as filled: not tried again, no budget spent, not SpotNav's to shed.
    record = hot_water.days[hot_water.picks[0].peak.astimezone(hot_water.zone).date().isoformat()]
    assert record["filled"] == [hot_water.picks[0].peak.isoformat()] and record["live"] == []
    assert hot_water.ledger.used_on(at.astimezone(hot_water.zone).date(), hot_water.zone, SOURCE_PLAN) == 0
    assert hot_water.boost()["source"] == "user"
    hot_water.tick(at + timedelta(minutes=2))
    await settle(hass)
    assert writes.calls == []
    # Nor is it counted as SpotNav's in what the plans see.
    hot_water._delta_key = None  # noqa: SLF001
    assert hot_water.forecast_delta(at) == {}


async def test_b1_no_planned_boost_while_the_pump_makes_hot_water(hass: HomeAssistant, freezer, monkeypatch) -> None:
    controller, hot_water, at = await _live(hass, freezer, monkeypatch, _pump(NIBE_F, LUX))
    hass.states.async_set(LUX, "Off", {"options": LUX_OPTIONS})
    monkeypatch.setattr(hot_water, "making_now", lambda: True)
    writes = Writes(hass)
    hot_water.tick(at)
    await settle(hass)
    assert writes.calls == []


# ---- B2: the safety reasons are computed at the start, on every path


OUTDOOR = "sensor.ute"
ROOM = "sensor.vardagsrum"


@pytest.mark.parametrize(
    ("outdoor", "room", "reason"),
    [("-18.0", "21.0", "too_cold"), ("4.0", "19.0", "room_cold"), ("4.0", "unavailable", "stale")],
)
async def test_b2_a_safety_reason_holds_a_boost_without_a_steered_contact(
    hass: HomeAssistant, freezer, monkeypatch, outdoor: str, room: str, reason: str
) -> None:
    """A cloud pump with no relay: the heat control never runs, so its cached safety stays empty; the boost's own
    check still finds the reason."""
    hass.states.async_set(OUTDOOR, outdoor, {"unit_of_measurement": "°C", "device_class": "temperature"})
    hass.states.async_set(ROOM, room, {"unit_of_measurement": "°C", "device_class": "temperature"})
    controller, hot_water, at = await _live(
        hass, freezer, monkeypatch, _pump(MYUPLINK, CLOUD), outdoor_sensor=OUTDOOR, indoor_sensors=[ROOM]
    )
    hass.states.async_set(CLOUD, "off")
    writes = Writes(hass)
    assert controller.heat_shadow.control.safety is None
    assert controller.heat_shadow.control.safety_now(controller.heat_shadow.config(), at) == reason
    hot_water.tick(at)
    await settle(hass)
    assert writes.calls == []
    # Good readings: it starts.
    hass.states.async_set(OUTDOOR, "4.0", {"unit_of_measurement": "°C", "device_class": "temperature"})
    hass.states.async_set(ROOM, "21.0", {"unit_of_measurement": "°C", "device_class": "temperature"})
    hot_water.tick(at + timedelta(minutes=1))
    await settle(hass)
    assert writes.calls == [("switch", "turn_on", {"entity_id": CLOUD})]


async def test_b2_an_old_cached_reason_does_not_hold_a_boost(hass: HomeAssistant, freezer, monkeypatch) -> None:
    hass.states.async_set(OUTDOOR, "4.0", {"unit_of_measurement": "°C", "device_class": "temperature"})
    controller, hot_water, at = await _live(hass, freezer, monkeypatch, _pump(MYUPLINK, CLOUD), outdoor_sensor=OUTDOOR)
    hass.states.async_set(CLOUD, "off")
    controller.heat_shadow.control.safety = "too_cold"
    writes = Writes(hass)
    hot_water.tick(at)
    await settle(hass)
    assert writes.calls == [("switch", "turn_on", {"entity_id": CLOUD})]
