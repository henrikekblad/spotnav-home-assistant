"""Vehicle properties: battery size and consumption belong to the vehicle, not the charger.

Pins the one writer and `spotnav/update_vehicle`, the resolution order (reported, stored, missing),
the dashboard's `vehicles`/`target_vehicle_id`/`soc.efficiency`/`soc.vehicle_max_percent`, and the
need formula's capping -- with contract fixtures written by the backend itself
(`SPOTNAV_WRITE_FIXTURES=1` rewrites them).
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from freezegun import freeze_time
from homeassistant.core import HomeAssistant

from custom_components.spotnav.api import dashboard as dashboard_api
from custom_components.spotnav.vehicles import vehicle_properties
from custom_components.spotnav.planning.auto_settings import TargetSocIntent
from custom_components.spotnav.vehicles.discovery_decisions import (
    DECISION_DOMAIN_VEHICLE,
    DECISION_DOMAIN_VEHICLE_PROPERTIES,
)
from custom_components.spotnav.vehicles.soc_estimate import target_need_kwh
from tests.test_dashboard_api import NOW
from tests.world import setup_charger_and_site
from tests.world import admin, non_admin, ws_call
from tests.world import CAPACITY, add_car, charger_and_car
from custom_components.spotnav.runtime import domain_data
from custom_components.spotnav.runtime import charger_data

pytestmark = pytest.mark.usefixtures("offline_relay")

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "vehicle" / "v1"
DASHBOARD_FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "dashboard"


def update_message(
    charger_id: Any, vehicle_id: Any, changes: Any, expected: Any = None, api_version: Any = 1
) -> dict[str, Any]:
    message = {
        "type": "spotnav/update_vehicle",
        "api_version": api_version,
        "charger_id": charger_id,
        "vehicle_id": vehicle_id,
        "changes": changes,
    }
    if expected is not None:
        message["expected"] = expected
    return message


def stored(hass: HomeAssistant, vehicle_id: str) -> dict[str, Any] | None:
    return domain_data(hass).decision_store.confirmed_payload(DECISION_DOMAIN_VEHICLE_PROPERTIES, vehicle_id)


def dashboard(hass: HomeAssistant, charger: Any) -> dict[str, Any]:
    return dashboard_api.serialize_dashboard(
        dashboard_api.capture_dashboard(hass, charger), can_act=True
    )


# ------------------------------------------------------------------ the command


async def test_two_vehicles_on_one_charger_keep_separate_properties(
    hass: HomeAssistant, hass_ws_client
) -> None:
    charger, _ = await setup_charger_and_site(hass)
    first, second = add_car(hass, "Volvo"), add_car(hass, "Niro")
    client = await admin(hass, hass_ws_client)

    a = (await ws_call(client, update_message(charger.entry_id, first, {"capacity_kwh": 78.0})))["result"]
    b = (
        await ws_call(
            client,
            update_message(
                charger.entry_id, second, {"capacity_kwh": 64.8, "consumption_kwh_per_10km": 1.7}
            ),
        )
    )["result"]

    assert a["ok"] is True and b["ok"] is True
    assert stored(hass, first) == {"capacity_kwh": 78.0}
    assert stored(hass, second) == {"capacity_kwh": 64.8, "consumption_kwh_per_10km": 1.7}
    assert a["vehicle"]["capacity_kwh"] == 78.0 and a["vehicle"]["capacity_source"] == "stored"
    assert b["vehicle"]["consumption_kwh_per_10km"] == 1.7
    rows = {v["id"]: v for v in dashboard(hass, charger)["vehicles"]}
    assert rows[first]["capacity_kwh"] == 78.0 and rows[second]["capacity_kwh"] == 64.8
    # The first vehicle never set a consumption: it shows the charger's own fallback.
    assert rows[first]["consumption_kwh_per_10km"] == 2.0 and rows[second]["consumption_kwh_per_10km"] == 1.7


async def test_null_clears_a_property_and_an_empty_record_disappears(
    hass: HomeAssistant, hass_ws_client
) -> None:
    charger, _ = await setup_charger_and_site(hass)
    car = add_car(hass, "Volvo")
    client = await admin(hass, hass_ws_client)
    await ws_call(client, update_message(charger.entry_id, car, {"capacity_kwh": 78.0}))

    result = (await ws_call(client, update_message(charger.entry_id, car, {"capacity_kwh": None})))["result"]

    assert result["ok"] is True and stored(hass, car) is None
    assert result["vehicle"]["capacity_kwh"] is None and result["vehicle"]["capacity_source"] is None


async def test_the_sensor_decision_and_the_properties_do_not_erase_each_other(
    hass: HomeAssistant, hass_ws_client
) -> None:
    charger, _ = await setup_charger_and_site(hass)
    car = add_car(hass, "Volvo")
    client = await admin(hass, hass_ws_client)
    await ws_call(client, update_message(charger.entry_id, car, {"capacity_kwh": 78.0}))
    await domain_data(hass).decision_store.async_confirm(DECISION_DOMAIN_VEHICLE, car, {"soc_entity_id": "sensor.volvo_battery"})
    await domain_data(hass).decision_store.async_unconfirm(DECISION_DOMAIN_VEHICLE, car)
    assert stored(hass, car) == {"capacity_kwh": 78.0}


async def test_refusals_carry_stable_codes_and_write_nothing(
    hass: HomeAssistant, hass_ws_client
) -> None:
    charger, _ = await setup_charger_and_site(hass)
    car = add_car(hass, "Volvo")
    client = await admin(hass, hass_ws_client)

    for vehicle, changes, expected_errors in (
        ("ghost", {"capacity_kwh": 60}, [("vehicle_id", "unknown_vehicle")]),
        (car, {"capacity_kwh": 0.5}, [("capacity_kwh", "invalid_capacity")]),
        (car, {"capacity_kwh": 501}, [("capacity_kwh", "invalid_capacity")]),
        (car, {"capacity_kwh": True}, [("capacity_kwh", "invalid_capacity")]),
        (car, {"capacity_kwh": "60"}, [("capacity_kwh", "invalid_capacity")]),
        (car, {"consumption_kwh_per_10km": 0}, [("consumption_kwh_per_10km", "invalid_consumption")]),
        (car, {"consumption_kwh_per_10km": -1}, [("consumption_kwh_per_10km", "invalid_consumption")]),
        (car, {"colour": "red"}, [("colour", "unknown_field")]),
        (car, {}, [("changes", "invalid_changes")]),
        (car, None, [("changes", "invalid_changes")]),
    ):
        result = (await ws_call(client, update_message(charger.entry_id, vehicle, changes)))["result"]
        assert result["ok"] is False and result["error"] == "spotnav_invalid_value", changes
        assert [(e["field"], e["code"]) for e in result["field_errors"]] == expected_errors, changes
    assert stored(hass, car) is None

    for bounds in ({"capacity_kwh": 1}, {"capacity_kwh": 500}):
        assert (await ws_call(client, update_message(charger.entry_id, car, bounds)))["result"]["ok"]


async def test_a_stale_expectation_is_a_conflict_that_returns_the_current_row(
    hass: HomeAssistant, hass_ws_client
) -> None:
    charger, _ = await setup_charger_and_site(hass)
    car = add_car(hass, "Volvo")
    client = await admin(hass, hass_ws_client)
    await ws_call(client, update_message(charger.entry_id, car, {"capacity_kwh": 78.0}))

    conflict = (
        await ws_call(
            client, update_message(charger.entry_id, car, {"capacity_kwh": 80.0}, {"capacity_kwh": 60.0})
        )
    )["result"]
    assert conflict["ok"] is False and conflict["error"] == "spotnav_conflict"
    assert conflict["vehicle"]["capacity_kwh"] == 78.0 and stored(hass, car) == {"capacity_kwh": 78.0}

    fresh = (
        await ws_call(
            client,
            update_message(
                charger.entry_id, car, {"capacity_kwh": 80.0},
                {"capacity_kwh": 78.0, "consumption_kwh_per_10km": 2.0},
            ),
        )
    )["result"]
    assert fresh["ok"] is True and stored(hass, car) == {"capacity_kwh": 80.0}


async def test_only_admins_known_chargers_and_this_version(
    hass: HomeAssistant, hass_ws_client, hass_read_only_access_token: str
) -> None:
    charger, _ = await setup_charger_and_site(hass)
    car = add_car(hass, "Volvo")
    denied = (
        await ws_call(
            await non_admin(hass, hass_ws_client, hass_read_only_access_token),
            update_message(charger.entry_id, car, {"capacity_kwh": 60}),
        )
    )["result"]
    assert denied["error"] == "spotnav_not_admin" and denied["vehicle"] is None
    client = await admin(hass, hass_ws_client)
    missing = (await ws_call(client, update_message("nope", car, {"capacity_kwh": 60})))["result"]
    assert missing["error"] == "spotnav_unknown_charger"
    frame = await ws_call(client, update_message(charger.entry_id, car, {"capacity_kwh": 60}, api_version=9))
    assert frame["success"] is False and frame["error"]["code"] == "spotnav_unsupported_api_version"
    assert stored(hass, car) is None


# --------------------------------------------------------------------- resolution


@freeze_time(NOW)
async def test_a_vehicles_stored_capacity_drives_the_need_and_reported_wins(
    hass: HomeAssistant, offline_relay: None
) -> None:
    charger, car, _ = await charger_and_car(hass, capacity=None)
    assert dashboard(hass, charger)["soc"]["missing"] == ["capacity"]

    await vehicle_properties.async_update_vehicle_properties(
        hass, domain_data(hass).decision_store, car, {"capacity_kwh": 60.0}
    )
    await hass.async_block_till_done()
    payload = dashboard(hass, charger)
    assert payload["soc"]["capacity_kwh"] == 60.0 and payload["soc"]["missing"] == []
    assert payload["soc"]["need_kwh"] == pytest.approx(0.4 * 60.0 / 0.9, abs=0.01)
    assert payload["charger"]["capabilities"]["target_soc"] is True
    assert payload["vehicles"][0]["capacity_source"] == "stored"

    # A capacity the vehicle itself reports wins over the stored one.
    reader = charger_data(hass, charger.entry_id).soc_reader
    reader._facts_cache[car] = (dashboard_api.dt_util.utcnow(), 77.4, None)  # noqa: SLF001
    payload = dashboard(hass, charger)
    assert payload["soc"]["capacity_kwh"] == 77.4
    assert payload["vehicles"][0]["capacity_source"] == "reported"
    assert payload["vehicles"][0]["capacity_kwh"] == 77.4


@freeze_time(NOW)
async def test_consumption_is_the_vehicles_own_else_the_default(
    hass: HomeAssistant, offline_relay: None
) -> None:
    charger, car, _ = await charger_and_car(hass)
    other = add_car(hass, "Niro")
    store = domain_data(hass).decision_store
    assert vehicle_properties.consumption_kwh_per_10km(hass, car) is None
    await vehicle_properties.async_update_vehicle_properties(
        hass, store, car, {"consumption_kwh_per_10km": 1.5}
    )
    assert vehicle_properties.consumption_kwh_per_10km(hass, car) == 1.5
    assert vehicle_properties.consumption_kwh_per_10km(hass, other) is None
    assert vehicle_properties.consumption_kwh_per_10km(hass, None) is None

    # What the planner is handed: the planned vehicle's own figure, else the default.
    preview = dashboard_api.preview_for(hass, charger.entry_id)
    settings = domain_data(hass).auto_store.settings(charger.entry_id)
    assert preview._consumption_for(settings) == 1.5  # noqa: SLF001
    assert preview._consumption_for(replace(settings, target=TargetSocIntent())) == 2.0  # noqa: SLF001


def test_the_need_is_capped_at_the_vehicles_own_limit_and_never_negative() -> None:
    # (min(target, max) - soc) x capacity / efficiency
    assert target_need_kwh(soc_percent=40, capacity_kwh=80, target_percent=100, vehicle_max_percent=80) == (
        "ok", pytest.approx(0.4 * 80 / 0.9),
    )
    assert target_need_kwh(soc_percent=40, capacity_kwh=80, target_percent=60, vehicle_max_percent=80) == (
        "ok", pytest.approx(0.2 * 80 / 0.9),
    )
    assert target_need_kwh(soc_percent=90, capacity_kwh=80, target_percent=100, vehicle_max_percent=80) == (
        "already_at_target", 0.0,
    )
    assert target_need_kwh(soc_percent=70, capacity_kwh=80, target_percent=50) == ("already_at_target", 0.0)
    assert target_need_kwh(soc_percent=40, capacity_kwh=None, target_percent=80) == ("unknown_capacity", None)


# ---------------------------------------------------------------------- dashboard


def _pin_ids(payload: Any, ids: dict[str, str]) -> Any:
    text = json.dumps(payload)
    for real, stable in ids.items():
        text = text.replace(real, stable)
    return json.loads(re.sub(r'"[0-9a-f]{32}"', '"<id>"', text))


@freeze_time(NOW)
async def test_the_dashboard_states_every_vehicle_the_target_vehicle_and_the_formula_inputs(
    hass: HomeAssistant, offline_relay: None
) -> None:
    charger, ev6, _ = await charger_and_car(hass)
    niro = add_car(hass, "Niro")
    await vehicle_properties.async_update_vehicle_properties(
        hass, domain_data(hass).decision_store, niro, {"capacity_kwh": 64.8, "consumption_kwh_per_10km": 1.7}
    )
    reader = charger_data(hass, charger.entry_id).soc_reader
    reader._facts_cache[ev6] = (dashboard_api.dt_util.utcnow(), None, 80.0)  # noqa: SLF001

    payload = dashboard(hass, charger)

    assert payload["target_vehicle_id"] == ev6
    rows = {v["name"]: v for v in payload["vehicles"]}
    assert rows["EV6"] == {
        "id": ev6, "name": "EV6", "soc_entity_id": rows["EV6"]["soc_entity_id"],
        "capacity_kwh": CAPACITY, "capacity_source": "stored",
        "consumption_kwh_per_10km": 2.0, "max_percent": 80.0, "soc_percent": 40.0,
        "onboard_phases": 3, "suggested_onboard_phases": None,
        "identification": {
            "plug": {"entity_id": None, "name": None, "chosen": False, "candidates": []},
            "location": {"entity_id": None, "name": None, "chosen": False, "candidates": []},
        },
        "target_percent": 80.0,
    }
    assert rows["Niro"]["soc_percent"] == 55.0
    assert rows["Niro"]["capacity_source"] == "stored" and rows["Niro"]["capacity_kwh"] == 64.8
    assert rows["Niro"]["consumption_kwh_per_10km"] == 1.7 and rows["Niro"]["max_percent"] is None
    soc = payload["soc"]
    assert soc["efficiency"] == 0.9 and soc["vehicle_max_percent"] == 80.0
    assert len(soc["vehicles"]) == 2, "listed whenever more than one exists, chosen or not"
    assert soc["missing"] == []
    # The card's formula from the block's own numbers equals the backend's need (target 80 <= 80).
    card = max(0.0, (min(soc["target_percent"], soc["vehicle_max_percent"]) - soc["value"])) * (
        soc["capacity_kwh"] / soc["efficiency"] / 100
    )
    assert card == pytest.approx(soc["need_kwh"], abs=0.01)

    _write_or_compare(
        DASHBOARD_FIXTURE_DIR / "target_soc_two_vehicles.json",
        _pin_ids(payload, {ev6: "vehicle_ev6", niro: "vehicle_niro"}),
    )


@freeze_time(NOW)
async def test_a_target_above_the_vehicle_limit_needs_energy_only_to_the_limit(
    hass: HomeAssistant, offline_relay: None
) -> None:
    charger, car, _ = await charger_and_car(hass)
    preview = dashboard_api.preview_for(hass, charger.entry_id)
    await preview.async_apply_settings(mutate=lambda s: replace(s, target=replace(s.target, target_percent=100.0)))
    charger_data(hass, charger.entry_id).soc_reader._facts_cache[car] = (  # noqa: SLF001
        dashboard_api.dt_util.utcnow(), None, 80.0,
    )
    soc = dashboard(hass, charger)["soc"]
    assert soc["target_percent"] == 100.0 and soc["vehicle_max_percent"] == 80.0
    assert soc["need_kwh"] == pytest.approx(0.4 * CAPACITY / 0.9, abs=0.01)

    await preview.async_apply_settings(mutate=lambda s: replace(s, target=replace(s.target, target_percent=30.0)))
    assert dashboard(hass, charger)["soc"]["need_kwh"] == 0.0


# --------------------------------------------------------------- command fixtures


def _write_or_compare(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if os.environ.get("SPOTNAV_WRITE_FIXTURES") == "1" or os.environ.get(
        "SPOTNAV_WRITE_FIXTURES"
    ) == "1":
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return
    assert json.loads(path.read_text(encoding="utf-8")) == payload, f"{path.name} drifted from the backend"


async def _not_admin(hass, ws, token, charger_id, car) -> Any:
    client = await non_admin(hass, ws, token)
    return (await ws_call(client, update_message(charger_id, car, {"capacity_kwh": 70})))["result"]


async def test_the_update_vehicle_fixtures_are_the_commands_own_output(
    hass: HomeAssistant, hass_ws_client, hass_read_only_access_token: str
) -> None:
    charger, _ = await setup_charger_and_site(hass, "fixture_charger")
    car = add_car(hass, "Niro")
    client = await admin(hass, hass_ws_client)

    def pinned(frame: dict[str, Any]) -> Any:
        return _pin_ids(frame["result"], {car: "vehicle_niro", "fixture_charger": "charger"})

    produced: dict[str, Any] = {}
    produced["update_vehicle_refused.json"] = pinned(
        await ws_call(client, update_message(charger.entry_id, car, {"capacity_kwh": 900, "consumption_kwh_per_10km": 0}))
    )
    produced["update_vehicle_success.json"] = pinned(
        await ws_call(
            client,
            update_message(
                charger.entry_id, car, {"capacity_kwh": 64.8, "consumption_kwh_per_10km": 1.7},
                {"capacity_kwh": None, "consumption_kwh_per_10km": 2.0},
            ),
        )
    )
    produced["update_vehicle_conflict.json"] = pinned(
        await ws_call(client, update_message(charger.entry_id, car, {"capacity_kwh": 70}, {"capacity_kwh": 50}))
    )
    produced["update_vehicle_unknown_vehicle.json"] = pinned(
        await ws_call(client, update_message(charger.entry_id, "no-such-vehicle", {"capacity_kwh": 70}))
    )
    produced["update_vehicle_cleared.json"] = pinned(
        await ws_call(client, update_message(charger.entry_id, car, {"capacity_kwh": None}))
    )
    produced["update_vehicle_not_admin.json"] = _pin_ids(
        await _not_admin(hass, hass_ws_client, hass_read_only_access_token, charger.entry_id, car),
        {car: "vehicle_niro", "fixture_charger": "charger"},
    )
    assert produced["update_vehicle_success.json"]["ok"] is True
    assert produced["update_vehicle_conflict.json"]["error"] == "spotnav_conflict"
    for name, payload in produced.items():
        _write_or_compare(FIXTURE_DIR / name, payload)
    if os.environ.get("SPOTNAV_WRITE_FIXTURES") != "1":
        assert sorted(p.name for p in FIXTURE_DIR.glob("*.json")) == sorted(produced)
