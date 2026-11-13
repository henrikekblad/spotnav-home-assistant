"""The battery shell on a site: facts from the registry, shadow decisions in the log, diagnostics, the debug bundle
and the book across a reload. Nothing is ever written to the battery."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.spotnav.battery.hold import ARM_S, HoldMemory
from custom_components.spotnav.battery.runtime import BatteryShadow
from custom_components.spotnav.battery.shadow import PART_HOLD_FOR_CAR, ShadowEntry
from custom_components.spotnav.debug_bundle import async_build_debug_bundle
from custom_components.spotnav.diagnostics import async_get_config_entry_diagnostics
from custom_components.spotnav.runtime import site_controller_for

from tests.world import setup_charger_and_site

pytestmark = pytest.mark.usefixtures("offline_relay")

SIGEN = json.loads((Path(__file__).parent / "fixtures" / "battery" / "brands.json").read_text(encoding="utf-8"))["sigenergy"]


def _sigenergy(hass: HomeAssistant, **states: str) -> str:
    """The real plant's entities (`brands.json`), registered under `sigen` on its devices."""
    owner = MockConfigEntry(domain="sigen", entry_id="ENTRY")
    owner.add_to_hass(hass)
    devices = dr.async_get(hass)
    device_ids = {
        name: devices.async_get_or_create(
            config_entry_id=owner.entry_id, identifiers={("sigen", name)}, name=name, manufacturer="Sigenergy"
        ).id
        for name in ("battery", "inverter")
    }
    registry = er.async_get(hass)
    for row in SIGEN["entities"]:
        domain, object_id = row["entity_id"].split(".", 1)
        entry = registry.async_get_or_create(
            domain, "sigen", row["unique_id"], device_id=device_ids[row["device"]], config_entry=owner,
            suggested_object_id=object_id,
            disabled_by=er.RegistryEntryDisabler.INTEGRATION if row["disabled"] else None,
        )
        assert entry.entity_id == row["entity_id"]
        state = states.get(row["entity_id"], row["state"])
        if state is not None and not row["disabled"]:
            attributes: dict[str, Any] = {"unit_of_measurement": row["unit"]} if row["unit"] else {}
            hass.states.async_set(entry.entity_id, state, attributes)
    return SIGEN["battery"]


async def _site(hass: HomeAssistant, **states: str):
    battery = _sigenergy(hass, **states)
    _charger, site = await setup_charger_and_site(hass, battery_aggregate_power_entity=battery)
    assert site is not None
    controller = site_controller_for(hass, site.entry_id)
    assert controller is not None
    return site, controller


def _grid_charge(monkeypatch: pytest.MonkeyPatch, on: bool = True) -> None:
    monkeypatch.setattr(
        BatteryShadow, "_grid_charging", lambda self: ((("entry_a",), 7.0) if on else ((), None))
    )


def _service_calls(hass: HomeAssistant) -> list[Any]:
    calls: list[Any] = []
    hass.bus.async_listen("call_service", calls.append)
    return calls


async def test_the_sites_battery_facts_are_in_its_diagnostics_and_the_debug_bundle(hass: HomeAssistant) -> None:
    site, _controller = await _site(hass)

    battery = (await async_get_config_entry_diagnostics(hass, site))["battery"]

    facts = battery["facts"]
    assert facts["brand"] == "sigenergy" and facts["soc_pct"] == 44.6 and facts["capacity_kwh"] == 40.3
    assert facts["power_kw"] == 4.603 and facts["firmware"] == "V100R001C21SPC117"
    assert facts["management"]["state"] == "self_managed"
    assert facts["controls"]["hold"]["entity_id"] == "number.sigen_plant_ess_max_discharging_limit"
    assert battery["parts"] == {PART_HOLD_FOR_CAR: "shadow"}
    bundle = await async_build_debug_bundle(hass)
    assert bundle["sites"][0]["diagnostics"]["battery"]["facts"]["brand"] == "sigenergy"


async def test_a_battery_in_its_own_ai_mode_is_left_alone_and_nothing_is_written(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    _grid_charge(monkeypatch)
    _site_entry, controller = await _site(hass, **{"sensor.sigen_plant_battery_power": "-4.6"})
    calls = _service_calls(hass)
    shadow = controller.battery_shadow
    now = dt_util.now()

    for seconds in (0, 10, ARM_S, ARM_S + 10):
        hass.states.async_set("sensor.sigen_plant_battery_power", "-4.6", {"unit_of_measurement": "kW"}, force_update=True)
        shadow.tick(now + timedelta(seconds=seconds))
    await hass.async_block_till_done()

    [entry] = shadow.book.entries
    assert entry.action == "left_alone" and entry.reason == "self_managed:EMS work mode:ai_mode"
    assert entry.entity_id == "number.sigen_plant_ess_max_discharging_limit" and entry.read == "23.04"
    assert entry.written is False
    assert shadow.snapshot()["hold_for_car"]["battery_gave_car_kwh"]
    assert [call for call in calls if call.data.get("domain") in ("number", "select", "switch", "sigen")] == []


async def test_the_book_survives_a_reload_and_a_hold_found_is_restored_first(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    site, controller = await _site(hass)
    now = dt_util.now()
    controller.battery_shadow.book.set_memory(
        PART_HOLD_FOR_CAR, HoldMemory(held=True, held_since=now, snapshot="4.8").as_dict()
    )
    controller.battery_shadow.book.record(
        ShadowEntry(now, PART_HOLD_FOR_CAR, "would_write", "battery_discharges_into_grid_charge", value=0.0)
    )

    assert await hass.config_entries.async_reload(site.entry_id)
    await hass.async_block_till_done()

    reloaded = site_controller_for(hass, site.entry_id)
    assert reloaded is not None and reloaded is not controller
    entries = reloaded.battery_shadow.book.entries
    assert [(e.action, e.reason) for e in entries] == [
        ("would_write", "battery_discharges_into_grid_charge"),
        ("would_restore", "restart"),
    ]
    assert entries[-1].value == 4.8
    assert reloaded.battery_shadow.snapshot()["hold_for_car"]["held_in_shadow"] is False


async def test_a_site_without_a_battery_has_no_facts(hass: HomeAssistant) -> None:
    _charger, site = await setup_charger_and_site(hass)
    assert site is not None

    battery = (await async_get_config_entry_diagnostics(hass, site))["battery"]

    assert battery["facts"]["battery_entity_id"] is None and battery["log"] == []
