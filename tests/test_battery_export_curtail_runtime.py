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
    assert {row["brand"] for row in curtail["brands"] if row["mapped"]} == {
        "sigenergy", "victron", "sungrow", "solis", "goodwe", "solax"
    }
    assert {row["brand"] for row in curtail["commands"]} == {row["brand"] for row in curtail["brands"] if row["mapped"]}
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


# --- the export limit of the other brands is found from the site's grid meter ---------------------------------------

_EXPORT_ROWS: dict[str, list[dict[str, Any]]] = {
    "victron": [
        {"entity_id": "number.victron_ess_max_feed_in_power", "unique_id": "c0619ab1_system_0_system_ess_max_feed_in_power",
         "state": "-1", "unit": "W"},
    ],
    "goodwe": [
        {"entity_id": "number.goodwe_grid_export_limit", "unique_id": "goodwe-grid_export_limit-9010KETU000W0000",
         "state": "10000", "unit": "W"},
        {"entity_id": "switch.goodwe_grid_export_limit_switch", "unique_id": "grid_export_limit_switch-9010KETU000W0000",
         "state": "off", "unit": None},
    ],
    "solis": [
        {"entity_id": "number.solis_backflow_power", "unique_id": "solis_modbus_2403123456_solis_modbus_inverter_backflow_power",
         "state": "8000", "unit": "W"},
        {"entity_id": "sensor.solis_backflow_power", "unique_id": "solis_modbus_2403123456_solis_modbus_inverter_backflow_power",
         "state": "7900", "unit": "W"},
        {"entity_id": "switch.solis_grid_feed_in_power_limit_switch", "unique_id": "solis_modbus_2403123456_43073_4",
         "state": "on", "unit": None},
    ],
    "solax": [
        {"entity_id": "number.solax_export_control_user_limit", "unique_id": "SolaX_export_control_user_limit",
         "state": "6000", "unit": "W"},
    ],
    "sungrow": [
        {"entity_id": "number.export_power_limit", "unique_id": "uid_export_power_limit", "platform": "template",
         "state": "5000", "unit": "W"},
        {"entity_id": "switch.export_power_limit", "unique_id": "sg_export_power_limit_switch", "platform": "modbus",
         "state": "off", "unit": None},
        {"entity_id": "sensor.export_power_limit_min", "unique_id": "sg_export_power_limit_min", "platform": "modbus",
         "state": "0", "unit": "W"},
        # The package's own grid sensors are template entities.
        {"entity_id": "sensor.export_power", "unique_id": "sg_export_power", "platform": "template",
         "state": "1200", "unit": "W"},
    ],
}


def _brand(hass: HomeAssistant, name: str) -> str:
    """A brand's entities (`brands.json`) with its export limit's, registered as its integration does: under a config
    entry and a device, or (Sungrow's YAML package) under neither. Answers the brand's battery power entity."""
    import json
    from pathlib import Path

    from homeassistant.helpers import device_registry as dr
    from homeassistant.helpers import entity_registry as er
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    brand = json.loads((Path(__file__).parent / "fixtures" / "battery" / "brands.json").read_text(encoding="utf-8"))[name]
    registry = er.async_get(hass)
    package = brand["entities"][0].get("device", "x") is None
    owner = None
    device_id = None
    if not package:
        owner = MockConfigEntry(domain=brand["platform"], entry_id=f"ENTRY_{name}")
        owner.add_to_hass(hass)
        device_id = dr.async_get(hass).async_get_or_create(
            config_entry_id=owner.entry_id, identifiers={(brand["platform"], name)}, name=name,
            manufacturer=brand["manufacturer"],
        ).id
    for row in [*brand["entities"], *_EXPORT_ROWS[name]]:
        domain, object_id = row["entity_id"].split(".", 1)
        entry = registry.async_get_or_create(
            domain, row.get("platform", brand["platform"]), row["unique_id"], device_id=device_id, config_entry=owner,
            suggested_object_id=object_id,
            disabled_by=er.RegistryEntryDisabler.INTEGRATION if row.get("disabled") else None,
        )
        if row["state"] is not None and not row.get("disabled"):
            hass.states.async_set(entry.entity_id, row["state"], {"unit_of_measurement": row["unit"]} if row["unit"] else {})
    return brand["battery"]


@pytest.mark.parametrize(
    ("name", "roles"),
    [
        ("victron", {"export_limit": ("number.victron_ess_max_feed_in_power", "-1")}),
        ("goodwe", {"export_limit": ("number.goodwe_grid_export_limit", "10000"),
                    "export_enable": ("switch.goodwe_grid_export_limit_switch", "off")}),
        # Solis's limit is read by the sensor of its register, not by the number's own echo.
        ("solis", {"export_limit": ("number.solis_backflow_power", "7900"),
                   "export_enable": ("switch.solis_grid_feed_in_power_limit_switch", "on")}),
        ("solax", {"export_limit": ("number.solax_export_control_user_limit", "6000")}),
        ("sungrow", {"export_limit": ("number.export_power_limit", "5000"),
                     "export_enable": ("switch.export_power_limit", "off"),
                     "limit_min": ("sensor.export_power_limit_min", "0")}),
    ],
)
async def test_each_brands_export_limit_is_found_from_the_grid_meter_without_a_battery(
    hass: HomeAssistant, name: str, roles: dict[str, tuple[str, str]]
) -> None:
    from custom_components.spotnav.battery.runtime import export_control_found
    from custom_components.spotnav.const import CONF_GRID_POWER_SOURCE

    meter = _brand(hass, name)
    facts, reads, control = read_export_facts(hass, {CONF_GRID_POWER_SOURCE: {"power": meter}})
    assert control is not None and control.brand == name and facts.control_brand == name
    assert {role: (read.entity_id, read.state) for role, read in reads.items()} == roles
    assert all(read.found and read.enabled for read in reads.values())
    assert export_control_found(reads, control)


async def test_sungrows_package_is_found_from_one_of_its_template_sensors_too(hass: HomeAssistant) -> None:
    from custom_components.spotnav.battery.runtime import export_control_found
    from custom_components.spotnav.const import CONF_GRID_POWER_SOURCE

    _brand(hass, "sungrow")
    _facts, reads, control = read_export_facts(
        hass, {CONF_GRID_POWER_SOURCE: {"power": "sensor.nothing", "power_export": "sensor.export_power"}}
    )
    assert control is not None and control.brand == "sungrow"
    assert reads["export_limit"].entity_id == "number.export_power_limit"
    assert reads["export_enable"].entity_id == "switch.export_power_limit"
    assert export_control_found(reads, control)


async def test_goodwe_on_core_has_the_limit_and_no_switch(hass: HomeAssistant) -> None:
    from homeassistant.helpers import entity_registry as er

    from custom_components.spotnav.battery.runtime import export_control_found
    from custom_components.spotnav.const import CONF_GRID_POWER_SOURCE

    meter = _brand(hass, "goodwe")
    er.async_get(hass).async_remove("switch.goodwe_grid_export_limit_switch")
    _facts, reads, control = read_export_facts(hass, {CONF_GRID_POWER_SOURCE: {"power": meter}})
    assert control is not None and set(reads) == {"export_limit"}
    assert export_control_found(reads, control)
    # A brand's plant without the limit's entity has no export control to show.
    er.async_get(hass).async_remove("number.goodwe_grid_export_limit")
    _facts, reads, control = read_export_facts(hass, {CONF_GRID_POWER_SOURCE: {"power": meter}})
    assert not export_control_found(reads, control)
