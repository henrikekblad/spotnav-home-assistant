"""A site measuring its hours for the demand charge and the import target, end to end: the meter's total grid power
read on each recompute, the `grid_tariff.demand` block, a restart mid-hour and the reasons it cannot measure.

Real config entries (a charger in a direct site with the meter's total grid power, and an electricity price SE4 with
the pilot's two components) on the offline relay, with the clock moved by hand from 2026-09-22 08:00 CEST.
"""

from __future__ import annotations

import json
import os
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.spotnav.api.grid_tariff import capture_grid_tariff, serialize_grid_tariff
from homeassistant.helpers import entity_registry as er

from custom_components.spotnav.api.grid_tariff import update_grid_tariff
from custom_components.spotnav.const import CONF_GRID_POWER_SOURCE, DOMAIN
from custom_components.spotnav.runtime import site_controller_for

from .test_grid_tariff_api import NO_DEMAND, PILOT
from .test_price_device import add_price, NOW
from .world import go_auto, setup_charger, setup_charger_and_site

pytestmark = pytest.mark.usefixtures("offline_relay")

GRID = "sensor.site_grid_power"
#: The whole `grid_tariff` block of the pilot's site at 10:20, which the card's tests read with no copy.
BLOCK_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "grid_tariff" / "pilot_demand.json"
STEP = timedelta(seconds=30)


def demand_of(hass: HomeAssistant, charger_id: str = "entry_a") -> dict[str, Any]:
    """The dashboard's `grid_tariff.demand` for the charger, as the serializer writes it now."""
    block = serialize_grid_tariff(capture_grid_tariff(hass, charger_id), can_act=True)
    assert block is not None
    return block["demand"]


async def run(hass: HomeAssistant, freezer: Any, minutes: int, watts: float) -> None:
    """The meter reports `watts` every 30 s for `minutes` (a hair apart, so each report is a change). A reading is
    held until the next, so each hour here begins with half a minute at the hour before's power."""
    for index in range(minutes * 2):
        freezer.tick(STEP)
        hass.states.async_set(GRID, str(watts + (index % 2) * 0.001), {"unit_of_measurement": "W"})
        async_fire_time_changed(hass)
        await hass.async_block_till_done()


async def pilot_site(hass: HomeAssistant, freezer: Any, **site: Any) -> Any:
    freezer.move_to(NOW)
    hass.states.async_set(GRID, "0", {"unit_of_measurement": "W"})
    _charger, entry = await setup_charger_and_site(
        hass, extra_data={CONF_GRID_POWER_SOURCE: {"power": GRID}, **site}
    )
    await go_auto(hass)
    await add_price(hass, demand_components=PILOT)
    return entry


async def test_each_component_takes_the_peak_of_its_own_hours_and_the_running_hour_is_shown(
    hass: HomeAssistant, freezer
) -> None:
    site = await pilot_site(hass, freezer, import_target_kw=6.0)
    await run(hass, freezer, 60, 4000.0)  # 08:00-09:00 CEST
    await run(hass, freezer, 60, 7000.0)  # 09:00-10:00
    await run(hass, freezer, 20, 3000.0)  # 10:00-10:20

    block = serialize_grid_tariff(capture_grid_tariff(hass, "entry_a"), can_act=True)
    if os.environ.get("SPOTNAV_WRITE_FIXTURES") == "1":
        BLOCK_FIXTURE.parent.mkdir(parents=True, exist_ok=True)
        BLOCK_FIXTURE.write_text(json.dumps(block, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    assert json.loads(BLOCK_FIXTURE.read_text(encoding="utf-8")) == block
    demand = demand_of(hass)
    assert demand["components"] == PILOT
    assert (demand["available"], demand["reason"], demand["import_target_kw"]) == (True, None, 6.0)
    day, night = demand["per_component"]
    assert day["bar_kw"] == pytest.approx(6.975, abs=0.01)
    assert day["free_kw"] == pytest.approx(6.975, abs=0.01)
    assert (day["price_per_kw"], day["days_counted"]) == (23.0, 1)
    assert [(peak["date"], peak["start"]) for peak in day["peaks"]] == [("2026-09-22", "2026-09-22T07:00:00+00:00")]
    # The night component counted nothing yet, and the hour running now is not one of its hours.
    assert night == {"bar_kw": None, "free_kw": None, "price_per_kw": 8.0, "days_counted": 0, "peaks": []}
    hour = demand["hour"]
    assert hour["start"] == "2026-09-22T08:00:00+00:00"
    # Half a minute at 7 kW, then 19.5 minutes at 3 kW.
    assert hour["kw_so_far"] == pytest.approx(3.1, abs=0.01)
    assert hour["kwh_so_far"] == pytest.approx(1.033, abs=0.01)

    # A restart mid-hour keeps the month and the hour; the minutes it took are a gap, not a lost hour.
    assert await hass.config_entries.async_reload(site.entry_id)
    await hass.async_block_till_done()
    freezer.tick(timedelta(minutes=2))
    await run(hass, freezer, 38, 3000.0)  # to 11:00
    await run(hass, freezer, 1, 3000.0)
    tracker = site_controller_for(hass, site.entry_id).demand.tracker  # type: ignore[union-attr]
    assert [(round(hour.kw, 1), hour.incomplete) for hour in tracker.hours] == [
        (4.0, False),
        (7.0, False),
        (3.0, False),
    ]


async def test_a_direct_site_without_the_total_cannot_measure_and_says_so(
    hass: HomeAssistant, freezer
) -> None:
    freezer.move_to(NOW)
    charger, _site = await setup_charger_and_site(hass, extra_data={"import_target_kw": 6.0})
    await go_auto(hass)
    await add_price(hass, demand_components=PILOT)

    demand = demand_of(hass, charger.entry_id)
    assert {key: demand[key] for key in ("available", "reason", "per_component", "hour", "import_target_kw")} == {
        "available": False,
        "reason": "needs_total_grid_power",
        "per_component": [],
        "hour": None,
        "import_target_kw": 6.0,
    }


async def test_a_charger_in_no_site_has_nothing_measured(hass: HomeAssistant, freezer) -> None:
    await setup_charger(hass)
    freezer.move_to(NOW)
    await go_auto(hass)
    await add_price(hass, demand_components=PILOT)
    assert demand_of(hass) == {**NO_DEMAND, "components": PILOT}


def peak_sensor(hass: HomeAssistant, site_entry_id: str) -> str | None:
    return er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"{site_entry_id}_demand_peak")


async def test_the_peak_sensor_exists_only_while_a_component_does(hass: HomeAssistant, freezer) -> None:
    site = await pilot_site(hass, freezer)
    await run(hass, freezer, 61, 5000.0)

    entity_id = peak_sensor(hass, site.entry_id)
    assert entity_id is not None
    state = hass.states.get(entity_id)
    # The hour began with half a minute at 0 W.
    assert state is not None and float(state.state) == pytest.approx(4.958)
    assert state.attributes["unit_of_measurement"] == "kW"
    day, night = state.attributes["components"]
    assert (day["top_n"], day["bar_kw"], day["days_counted"]) == (1, 4.958, 1)
    assert night["bar_kw"] is None and night["window"]["from_hour"] == 23
    assert state.attributes["hour_start"] == "2026-09-22T07:00:00+00:00"

    # The last component removed: the sensor goes, from the registry too.
    stored = {"transfer_percent": {"enabled": False, "value": None}, "transfer_high": {"enabled": False, "value": None}}
    update_grid_tariff(hass, "entry_a", 0, {**stored, "high_load_window": None, "demand_components": []})
    await hass.async_block_till_done()
    assert peak_sensor(hass, site.entry_id) is None
    assert hass.states.get(entity_id) is None

    # And comes back with one.
    one = [{"window": None, "top_n": 3, "price_per_kw": {"winter": 49.0, "summer": 49.0}}]
    update_grid_tariff(hass, "entry_a", 1, {**stored, "high_load_window": None, "demand_components": one})
    await hass.async_block_till_done()
    entity_id = peak_sensor(hass, site.entry_id)
    assert entity_id is not None
    assert float(hass.states.get(entity_id).state) == pytest.approx(4.958)  # type: ignore[union-attr]


async def test_a_site_without_a_component_has_no_peak_sensor(hass: HomeAssistant, freezer) -> None:
    freezer.move_to(NOW)
    hass.states.async_set(GRID, "0", {"unit_of_measurement": "W"})
    _charger, site = await setup_charger_and_site(hass, extra_data={CONF_GRID_POWER_SOURCE: {"power": GRID}})
    await go_auto(hass)
    await add_price(hass)
    assert site is not None and peak_sensor(hass, site.entry_id) is None
