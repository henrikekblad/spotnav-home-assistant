"""The spa's frost floor on a cold day with no bath (notifications review B3): water at the floor is heated past it
by `FLOOR_MARGIN_C` and then rested, the Rest is the floor's own write (never refused by the day's budget), so a spa
left at its lowest idle temperature over the winter never stays in Ready and heats to the bath temperature."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from homeassistant.core import HomeAssistant

from custom_components.spotnav.heat.config import read_config
from custom_components.spotnav.spa.config import DEFAULT_FLOOR_C
from custom_components.spotnav.spa.control import FLOOR_MARGIN_C
from custom_components.spotnav.spa.plan import HeatHour
from custom_components.spotnav.spa.runtime import Reading, new_spa

from .pool_spa_fakes import S, TZ, Devices, make_control

SPA = "climate.spa"
ZONE = ZoneInfo(TZ)
DAY = datetime(2026, 1, 12, tzinfo=ZONE)
STEP = 30 * S
UP_C = 0.0125  # per look while heating


def _heat(idle: float) -> dict[str, Any]:
    return {"spa": {"entity_id": SPA, "family": "balboa", "bath_c": 38.0, "idle_c": idle, "heater_kw": 3.0,
                    "control": True, "baths": []}}


@pytest.mark.parametrize(("idle", "loss_per_h"), [(10.0, 0.2), (10.0, 0.5), (10.5, 0.2), (11.0, 0.5), (12.0, 0.5)])
async def test_a_cold_day_with_no_bath_never_heats_past_the_floor_s_margin(
    hass: HomeAssistant, idle: float, loss_per_h: float
) -> None:
    devices = Devices(hass)
    water = idle + 0.4
    devices.add(SPA, "off", preset_mode="rest", temperature=38.0, current_temperature=water, min_temp=10.0, max_temp=40.0)
    heat = _heat(idle)
    control, shadow, _order = make_control(hass, heat)
    spa = new_spa()
    shadow.spa = spa
    hours = [HeatHour(DAY + timedelta(hours=i), 1.0, 0.5, None, None, None) for i in range(24 * 3)]
    config = read_config(heat).spa
    top = water
    moment = DAY
    while moment < DAY + timedelta(days=2):
        state = hass.states.get(SPA)
        heating = state is not None and state.attributes.get("preset_mode") == "ready"
        water = min(water + UP_C, 38.0) if heating else water - loss_per_h * STEP.total_seconds() / 3600
        top = max(top, water)
        shadow.raw = heat
        spa.look(moment, TZ, config, Reading(water, heating, None, None, False), hours)
        await control.async_look(moment)
        moment += STEP
    limit = max(idle, DEFAULT_FLOOR_C + FLOOR_MARGIN_C) + 0.5
    assert top <= limit, f"heated to {top:.1f} °C with no bath"
    assert water >= DEFAULT_FLOOR_C - 0.5
