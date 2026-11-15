"""The export-limit rule in shadow on a site with the owner's Sigenergy plant (`brands.json`, the export limit as on
the owner's registry 2026-11-15: `number.sigen_plant_grid_export_limitation`, disabled by default): what it would
write when the sell price goes below zero, what the hand-back would write when it turns positive, what it counts,
and that nothing is ever written."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.spotnav.battery.runtime import SECTION_CURTAIL, read_export_facts
from custom_components.spotnav.battery.shadow import PART_EXPORT_CURTAIL
from custom_components.spotnav.diagnostics import async_get_config_entry_diagnostics
from custom_components.spotnav.execution import solar_execution
from custom_components.spotnav.runtime import site_controller_for
from tests.test_battery_runtime import _service_calls, _site

pytestmark = pytest.mark.usefixtures("offline_relay")

EXPORT_LIMIT = "number.sigen_plant_grid_export_limitation"


def _export_row(disabled: bool = False) -> dict[str, Any]:
    return {
        "entity_id": EXPORT_LIMIT, "unique_id": "ENTRY_sigen_plant_plant_grid_point_maximum_export_limitation",
        "device": "battery", "state": "11.5", "unit": "kW", "device_class": "power", "disabled": disabled,
    }


def _prices(monkeypatch: pytest.MonkeyPatch, controller: Any, sells: list[float | None]) -> None:
    """The sell price each look sees, in turn, a 3 kW export and a healthy site measurement."""
    queue = list(sells)
    shadow = controller.battery_shadow
    inputs = shadow.inputs
    monkeypatch.setattr(shadow, "inputs", lambda now: replace(inputs(now), measurement_ok=True))
    monkeypatch.setattr(controller.battery_shadow.plan, "sell_at", lambda _now: queue.pop(0) if queue else None)
    monkeypatch.setattr(solar_execution, "site_net_grid_w", lambda _site: -3000.0)


async def test_a_free_plant_would_get_a_zero_export_limit_and_the_value_found_back(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    site, controller = await _site(
        hass, extra=(_export_row(),), **{"sensor.sigen_plant_ems_work_mode": "max_self_consumption"}
    )
    calls = _service_calls(hass)
    shadow = controller.battery_shadow
    _prices(monkeypatch, controller, [10.0, -12.0, -12.0, -12.0, 4.0])
    now = dt_util.now()
    for index in range(5):
        shadow.tick(now + timedelta(seconds=10 * index))
    await hass.async_block_till_done()

    entries = [entry for entry in shadow.book.entries if entry.part == PART_EXPORT_CURTAIL]
    assert [(e.action, e.reason) for e in entries] == [
        ("would_write", "sell_below_zero"), ("would_restore", "sell_not_below_zero")
    ]
    assert entries[0].entity_id == EXPORT_LIMIT and entries[0].value == 0.0 and entries[0].read == "11.5"
    assert entries[0].detail["restores"][0]["value"] == 11.5
    assert entries[1].detail["restore"] == {"export_limit": 11.5}
    assert all(entry.written is False for entry in entries)
    count = next(iter(shadow.curtail.days.values()))
    assert count.kwh == pytest.approx(3.0 * 20 / 3600) and count.saving == pytest.approx(count.kwh * 12.0)
    assert count.blocked_kwh == 0.0
    assert [call for call in calls if call.data.get("domain") in ("number", "select", "switch", "sigen")] == []

    diagnostics = (await async_get_config_entry_diagnostics(hass, site))["battery"]
    curtail = diagnostics["export_curtail"]
    assert curtail["control"] == "sigenergy" and curtail["curtailing"] is False and curtail["sell_now"] == 4.0
    assert curtail["totals"]["kwh"] == pytest.approx(count.kwh, abs=1e-4)
    assert diagnostics["parts"][PART_EXPORT_CURTAIL] == "shadow"
    assert shadow.book.section(SECTION_CURTAIL)["days"]


async def test_a_plant_in_ai_mode_is_only_watched_and_its_export_counted_apart(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    _site_entry, controller = await _site(hass, extra=(_export_row(),))
    shadow = controller.battery_shadow
    _prices(monkeypatch, controller, [-12.0, -12.0])
    now = dt_util.now()
    shadow.tick(now)
    shadow.tick(now + timedelta(seconds=10))

    [entry] = [entry for entry in shadow.book.entries if entry.part == PART_EXPORT_CURTAIL]
    assert entry.action == "not_possible" and entry.reason == "self_managed:EMS work mode:ai_mode"
    count = next(iter(shadow.curtail.days.values()))
    assert count.blocked_kwh == pytest.approx(count.kwh) and count.kwh > 0


async def test_an_export_limit_disabled_in_home_assistant_is_named_as_the_reason(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    _site_entry, controller = await _site(
        hass, extra=(_export_row(disabled=True),), **{"sensor.sigen_plant_ems_work_mode": "max_self_consumption"}
    )
    _prices(monkeypatch, controller, [-12.0])
    controller.battery_shadow.tick(dt_util.now())
    [entry] = [e for e in controller.battery_shadow.book.entries if e.part == PART_EXPORT_CURTAIL]
    assert (entry.action, entry.reason) == ("not_possible", "control_disabled:export_limit")


async def test_the_plant_is_found_from_the_grid_meter_on_a_site_without_a_battery(hass: HomeAssistant) -> None:
    from custom_components.spotnav.const import CONF_BATTERY_AGGREGATE_POWER_ENTITY, CONF_GRID_POWER_SOURCE
    from tests.test_battery_runtime import _sigenergy

    _sigenergy(hass, extra=(_export_row(),), **{"sensor.sigen_plant_ems_work_mode": "max_self_consumption"})
    config = {CONF_GRID_POWER_SOURCE: {"power": "sensor.sigen_plant_battery_power"}}
    facts, reads, control = read_export_facts(hass, config)
    assert control is not None and control.brand == "sigenergy"
    assert facts.management.state == "free"
    assert reads["export_limit"].entity_id == EXPORT_LIMIT and reads["export_limit"].state == "11.5"
    # Nothing on a brand without an export control, and nothing without any site entity.
    assert read_export_facts(hass, {CONF_BATTERY_AGGREGATE_POWER_ENTITY: "sensor.nothing"})[2] is None
    assert read_export_facts(hass, {})[2] is None


async def test_the_rule_counts_survive_a_reload(hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch) -> None:
    site, controller = await _site(
        hass, extra=(_export_row(),), **{"sensor.sigen_plant_ems_work_mode": "max_self_consumption"}
    )
    _prices(monkeypatch, controller, [-12.0, -12.0])
    now = dt_util.now()
    controller.battery_shadow.tick(now)
    controller.battery_shadow.tick(now + timedelta(seconds=10))
    kept = controller.battery_shadow.curtail.as_dict()

    assert await hass.config_entries.async_reload(site.entry_id)
    await hass.async_block_till_done()

    reloaded = site_controller_for(hass, site.entry_id)
    assert reloaded is not None and reloaded.battery_shadow.curtail.as_dict() == kept
