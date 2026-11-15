"""The battery shell on a site: facts from the registry, shadow decisions in the log, diagnostics, the debug bundle
and the book across a reload. Nothing is ever written to the battery."""

from __future__ import annotations

import json
import logging
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.spotnav.battery.hold import ARM_S, HoldMemory
from custom_components.spotnav.battery.monitor import GRACE_S, READING_GAP_S
from custom_components.spotnav.battery.runtime import BatteryShadow
from custom_components.spotnav.battery.shadow import MIN_DIVERGENCE_S, PART_EXPORT_CURTAIL, PART_HOLD_FOR_CAR, ShadowEntry
from custom_components.spotnav.debug_bundle import async_build_debug_bundle
from custom_components.spotnav.diagnostics import async_get_config_entry_diagnostics
from custom_components.spotnav.runtime import site_controller_for

from tests.world import setup_charger_and_site

pytestmark = pytest.mark.usefixtures("offline_relay")

SIGEN = json.loads((Path(__file__).parent / "fixtures" / "battery" / "brands.json").read_text(encoding="utf-8"))["sigenergy"]


#: The plant's Remote EMS controls enabled, as the executor's hold needs them, and the charge limit 2.0.1 shows as
#: `unknown` while never set.
REMOTE_EMS = ("switch.sigen_plant_remote_ems_controlled_by_home_assistant", "select.sigen_plant_remote_ems_control_mode")
CHARGE_LIMIT_ROW = {
    "entity_id": "number.sigen_plant_ess_max_charging_limit", "unique_id": "ENTRY_sigen_plant_plant_ess_max_charging_limit",
    "device": "battery", "state": "unknown", "unit": "kW", "device_class": "power", "disabled": False,
}


def _sigenergy(
    hass: HomeAssistant, enable: tuple[str, ...] = (), extra: tuple[dict[str, Any], ...] = (), **states: str
) -> str:
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
    for row in [*SIGEN["entities"], *extra]:
        if row["entity_id"] in enable:
            row = row | {"disabled": False, "state": {"switch": "off", "select": "unavailable"}[row["entity_id"].split(".")[0]]}
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


async def _site(hass: HomeAssistant, enable: tuple[str, ...] = (), extra: tuple[dict[str, Any], ...] = (), **states: str):
    battery = _sigenergy(hass, enable, extra, **states)
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
    assert battery["parts"] == {PART_HOLD_FOR_CAR: "shadow", PART_EXPORT_CURTAIL: "shadow"}
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
    restore = {"control_mode": "Maximum Self Consumption", "remote_ems": False, "discharge_limit": 4.8}
    controller.battery_shadow.book.set_memory(
        PART_HOLD_FOR_CAR,
        HoldMemory(
            held=True, held_since=now, snapshot={"discharge_limit": "4.8"},
            entities={"discharge_limit": "number.sigen_plant_ess_max_discharging_limit"}, restore=restore,
        ).as_dict(),
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
    assert entries[-1].value == restore
    assert reloaded.battery_shadow.snapshot()["hold_for_car"]["held_in_shadow"] is False


async def test_a_site_without_a_battery_has_no_facts(hass: HomeAssistant) -> None:
    _charger, site = await setup_charger_and_site(hass)
    assert site is not None

    battery = (await async_get_config_entry_diagnostics(hass, site))["battery"]

    assert battery["facts"]["battery_entity_id"] is None and battery["log"] == []


def _set(hass: HomeAssistant, entity_id: str, state: str, unit: str) -> None:
    hass.states.async_set(entity_id, state, {"unit_of_measurement": unit}, force_update=True)


async def test_the_ledger_and_monitor_run_dry_beside_a_shadow_hold_and_survive_a_reload(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    _grid_charge(monkeypatch)
    site, controller = await _site(
        hass, REMOTE_EMS, (CHARGE_LIMIT_ROW,),
        **{"sensor.sigen_plant_battery_power": "-4.6", "sensor.sigen_plant_ems_work_mode": "max_self_consumption"},
    )
    calls = _service_calls(hass)
    shadow = controller.battery_shadow
    monkeypatch.setattr(BatteryShadow, "_battery_power", lambda self: (-4.6, 5.0))
    monkeypatch.setattr(BatteryShadow, "inputs", _with_headroom(BatteryShadow.inputs))
    now = dt_util.now()

    for seconds in (0, ARM_S):
        shadow.tick(now + timedelta(seconds=seconds))
    write = next(entry for entry in shadow.book.entries if entry.action == "would_write")
    # The executor's own hold: the limit, Remote EMS on, its mode; handed back with the never-set charge limit left.
    assert write.value == {"discharge_limit": 0.0, "remote_ems": True, "control_mode": "Maximum Self Consumption"}
    assert write.entity_id == "number.sigen_plant_ess_max_discharging_limit" and write.read == "23.04"
    assert [(row["role"], row["value"]) for row in write.detail["restore"]] == [
        ("discharge_limit", 23.04), ("control_mode", "Maximum Self Consumption"), ("remote_ems", False)
    ]
    assert write.detail["read_back"] == "possible"
    assert write.detail["read_back_roles"] == {"discharge_limit": "possible", "remote_ems": "possible", "control_mode": "idle"}

    # The battery keeps discharging, as nothing was written: the monitor shows that baseline once.
    for index in range(4):
        shadow.tick(now + timedelta(seconds=ARM_S + GRACE_S + index * READING_GAP_S))
    judged = [entry for entry in shadow.book.entries if entry.action == "would_judge"]
    assert [entry.reason for entry in judged] == ["baseline_no_effect"]

    # Something else sets the limit while nothing writes it.
    limit = "number.sigen_plant_ess_max_discharging_limit"
    later = now + timedelta(seconds=ARM_S + GRACE_S + 400)
    _set(hass, limit, "5.0", "kW")
    shadow.tick(later)
    shadow.tick(later + timedelta(seconds=MIN_DIVERGENCE_S))
    assert [entry.action for entry in shadow.book.entries].count("changed_outside") == 1

    dry = shadow.snapshot()["dry_run"]
    assert dry["ledger"]["claims"][limit]["given_up"] is True
    assert dry["monitor"]["today"] == {"writes": 0, "no_effect": 0, "partial": 0}

    assert await hass.config_entries.async_reload(site.entry_id)
    await hass.async_block_till_done()
    reloaded = site_controller_for(hass, site.entry_id)
    assert reloaded is not None
    entries = reloaded.battery_shadow.book.entries
    # The restart restores the hold first and, the limit having been changed outside SpotNav, would not write it.
    assert entries[-1].action == "would_restore" and entries[-1].detail["restore"] == "skipped:changed_outside_spotnav"
    assert reloaded.battery_shadow.snapshot()["dry_run"]["ledger"]["claims"] == {}
    assert [call for call in calls if call.data.get("domain") in ("number", "select", "switch", "sigen")] == []


def _with_headroom(inputs):
    def wrapped(self, now):
        from dataclasses import replace  # noqa: PLC0415

        return replace(inputs(self, now), headroom_a=25.0, measurement_ok=True)

    return wrapped


# --- something else drives the battery (`outside_control.py`) ----------------------------------------------------


def _issue(hass: HomeAssistant, site_id: str):
    from homeassistant.helpers import issue_registry as ir  # noqa: PLC0415

    return ir.async_get(hass).async_get_issue("spotnav", f"battery_outside_control_{site_id}")


async def test_a_set_up_checkwatt_makes_the_battery_watch_only_without_a_notice(hass: HomeAssistant) -> None:
    MockConfigEntry(domain="checkwatt", entry_id="CW").add_to_hass(hass)
    site, controller = await _site(hass, **{"sensor.sigen_plant_ems_work_mode": "max_self_consumption"})
    shadow = controller.battery_shadow

    shadow.tick(dt_util.now())
    await hass.async_block_till_done()

    assert shadow.facts.management.state == "externally_controlled"
    assert shadow.facts.management.reason == "integration:checkwatt"
    battery = (await async_get_config_entry_diagnostics(hass, site))["battery"]
    assert battery["outside_control"]["verdict"]["source"] == "CheckWatt"
    assert battery["plan"]["benchmark"] is True
    assert _issue(hass, site.entry_id) is None


async def test_settings_changed_three_times_are_logged_once_without_a_notice_and_end_by_themselves(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    from custom_components.spotnav.battery.outside_control import CLEAR_AFTER  # noqa: PLC0415

    site, controller = await _site(hass, **{"sensor.sigen_plant_ems_work_mode": "max_self_consumption"})
    shadow = controller.battery_shadow
    calls = _service_calls(hass)
    limit = "number.sigen_plant_ess_max_discharging_limit"
    now = dt_util.now()
    caplog.set_level(logging.INFO, logger="custom_components.spotnav.battery.runtime")

    for minute, value in enumerate(("23.04", "5.0", "23.04", "0.0", "5.0", "23.04")):
        _set(hass, limit, value, "kW")
        shadow.tick(now + timedelta(minutes=minute))
    await hass.async_block_till_done()

    assert shadow.facts.management.state == "externally_controlled"
    assert shadow.facts.management.reason == "settings_changed"
    battery = (await async_get_config_entry_diagnostics(hass, site))["battery"]
    assert battery["outside_control"]["verdict"]["reason"] == "settings_changed"
    # Logged once for the start, however long it lasts; no Repairs notice.
    started = [r for r in caplog.records if "outside SpotNav drives the battery" in r.getMessage()]
    assert len(started) == 1 and started[0].levelno == logging.INFO
    assert _issue(hass, site.entry_id) is None

    # A reload keeps the occurrence: it is not logged again, and the battery stays watch-only.
    caplog.clear()
    assert await hass.config_entries.async_reload(site.entry_id)
    await hass.async_block_till_done()
    reloaded = site_controller_for(hass, site.entry_id)
    assert reloaded is not None
    assert reloaded.battery_shadow.facts.management.state == "externally_controlled"
    assert not [r for r in caplog.records if "outside SpotNav" in r.getMessage()]

    reloaded.battery_shadow.tick(now + timedelta(minutes=6) + CLEAR_AFTER)
    await hass.async_block_till_done()
    assert reloaded.battery_shadow.facts.management.state == "free"
    ended = [r for r in caplog.records if "no longer drives the battery" in r.getMessage()]
    assert len(ended) == 1 and ended[0].levelno == logging.INFO
    assert _issue(hass, site.entry_id) is None
    assert [call for call in calls if call.data.get("domain") in ("number", "select", "switch", "sigen")] == []


async def test_a_left_over_outside_control_notice_is_deleted_at_start(hass: HomeAssistant) -> None:
    """An earlier version raised a Repairs notice for an occurrence; it is retired, so one left over goes."""
    from homeassistant.helpers import issue_registry as ir  # noqa: PLC0415

    site, controller = await _site(hass, **{"sensor.sigen_plant_ems_work_mode": "max_self_consumption"})
    ir.async_create_issue(
        hass,
        "spotnav",
        f"battery_outside_control_{site.entry_id}",
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key="battery_outside_control",
    )
    assert await hass.config_entries.async_reload(site.entry_id)
    await hass.async_block_till_done()
    assert _issue(hass, site.entry_id) is None
