"""Phase switching for solar as a charger setting (`api/entity_fields.py`'s `phase_switching`): listed only for a charger
whose integration can switch, wired for three phases, on a site with a solar source; written into the charger's entry
data without a reload, and read by the controller from there. And what the card and the app are told while the sun
holds the charge on one phase: the status line and the charge bar's power.
"""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.core import HomeAssistant

from custom_components.spotnav.api import dashboard as dashboard_api
from custom_components.spotnav.api.charge_bar import charge_bar, ProgressFacts
from custom_components.spotnav.api.webhook import _for_app, APP_READS_SOLAR_ONE_PHASE
from custom_components.spotnav.const import (
    CONF_GRID_POWER_SOURCE,
    CONF_MEASUREMENT_MODE,
    CONF_PHASE_SWITCHING,
    CONF_PHASE_WIRING,
    MEASUREMENT_MODE_DIRECT,
)
from custom_components.spotnav.planning.status_compose import _solar_line, STATUS_CODES
from custom_components.spotnav.runtime import controller_for

from .messages import field, get_message, update_entity_config_message
from .test_solar_phase_execution import _switched_down, _world
from .world import admin, setup_charger_and_site, ws_call

pytestmark = pytest.mark.usefixtures("offline_relay")


def _names(result: dict[str, Any]) -> list[str]:
    return [item["field"] for item in result["config"]["fields"]]


async def _set(client: Any, charger_id: str, value: Any, expected: Any = None) -> dict[str, Any]:
    message = update_entity_config_message(
        charger_id,
        scope="charger",
        expected={} if expected is None else {"phase_switching": expected},
        changes={"phase_switching": value},
    )
    return (await ws_call(client, message))["result"]


async def test_a_switchable_charger_offers_it_off_and_a_write_takes_effect_without_a_reload(
    hass: HomeAssistant, hass_ws_client, freezer: Any
) -> None:
    w = await _world(hass, freezer, option=False)
    charger_id = w.charger.entry_id
    client = await admin(hass, hass_ws_client)

    listed = field((await ws_call(client, get_message(charger_id)))["result"], "phase_switching")
    assert listed == {
        "field": "phase_switching", "scope": "charger", "kind": "flag", "required": False, "writable": True,
        "value": False,
    }
    assert w.controller.phase_switch_allowed() is False

    result = await _set(client, charger_id, True, expected=False)
    assert result["ok"] is True and field(result, "phase_switching")["value"] is True
    assert hass.config_entries.async_get_entry(charger_id).data[CONF_PHASE_SWITCHING] is True
    assert controller_for(hass, charger_id) is w.controller, "no reload"
    assert w.controller.phase_switching_option is True
    assert w.controller.phase_switch_allowed() is True

    result = await _set(client, charger_id, False, expected=True)
    assert result["ok"] is True and field(result, "phase_switching")["value"] is False
    assert CONF_PHASE_SWITCHING not in hass.config_entries.async_get_entry(charger_id).data, "stored only while on"
    assert w.controller.phase_switching_option is False


async def test_a_stale_value_is_a_conflict_and_a_non_boolean_is_refused(
    hass: HomeAssistant, hass_ws_client, freezer: Any
) -> None:
    w = await _world(hass, freezer, option=True)
    charger_id = w.charger.entry_id
    client = await admin(hass, hass_ws_client)

    stale = await _set(client, charger_id, True, expected=False)
    assert stale["error"] == "spotnav_conflict"
    refused = await _set(client, charger_id, "on")
    assert refused["error"] == "spotnav_invalid_value"
    assert refused["field_errors"] == [{"field": "phase_switching", "code": "invalid_value"}]
    assert hass.config_entries.async_get_entry(charger_id).data[CONF_PHASE_SWITCHING] is True


async def test_not_offered_without_a_solar_source_or_on_a_one_phase_wiring(
    hass: HomeAssistant, hass_ws_client, freezer: Any
) -> None:
    w = await _world(hass, freezer, option=False)
    charger_id = w.charger.entry_id
    site_entry = hass.config_entries.async_get_entry(w.site.entry_id)
    client = await admin(hass, hass_ws_client)
    original = dict(site_entry.data)

    one_phase = dict(original.get(CONF_PHASE_WIRING) or {})
    one_phase[charger_id] = {**(one_phase.get(charger_id) or {}), "phases": 1}
    hass.config_entries.async_update_entry(site_entry, data={**original, CONF_PHASE_WIRING: one_phase})
    assert "phase_switching" not in _names((await ws_call(client, get_message(charger_id)))["result"])
    refused = await _set(client, charger_id, True)
    assert refused["field_errors"] == [{"field": "phase_switching", "code": "not_writable"}]

    no_sun = {**original, CONF_MEASUREMENT_MODE: MEASUREMENT_MODE_DIRECT}
    no_sun.pop(CONF_GRID_POWER_SOURCE, None)
    hass.config_entries.async_update_entry(site_entry, data=no_sun)
    assert "phase_switching" not in _names((await ws_call(client, get_message(charger_id)))["result"])

    hass.config_entries.async_update_entry(site_entry, data=original)
    assert "phase_switching" in _names((await ws_call(client, get_message(charger_id)))["result"])


async def test_a_charger_that_cannot_switch_never_lists_it_and_can_only_turn_it_off(
    hass: HomeAssistant, hass_ws_client
) -> None:
    charger, _ = await setup_charger_and_site(hass)
    lone, _ = await setup_charger_and_site(hass, "lone", site=False)
    client = await admin(hass, hass_ws_client)

    for entry in (charger, lone):
        assert "phase_switching" not in _names((await ws_call(client, get_message(entry.entry_id)))["result"])
        refused = await _set(client, entry.entry_id, True)
        assert refused["error"] == "spotnav_invalid_value"
        assert refused["field_errors"] == [{"field": "phase_switching", "code": "not_writable"}]
    # Off is always allowed, so a setting left on where it no longer works can be cleared.
    assert (await _set(client, charger.entry_id, False))["ok"] is True


# -- what the card and the app are told while the charge is on one phase


def test_the_one_phase_line_and_its_read() -> None:
    assert STATUS_CODES["solar_charging_one_phase"] == ("normal", ("requested_a",))
    assert APP_READS_SOLAR_ONE_PHASE == "solar_one_phase"


def _one_phase_body() -> dict[str, Any]:
    return {
        "ok": True,
        "status": {"tone": "normal", "lines": [{"code": "solar_charging_one_phase", "params": {"requested_a": 12.5}}]},
    }


def test_an_older_app_gets_the_solar_charging_line_with_the_same_current() -> None:
    body = _one_phase_body()
    older = _for_app(body, {"action": "dashboard", "reads": ["car_ended_waiting"]})
    assert older["status"]["lines"] == [{"code": "solar_charging", "params": {"requested_a": 12.5}}]
    assert body["status"]["lines"][0]["code"] == "solar_charging_one_phase", "a copy"


def test_an_app_that_reads_it_gets_it() -> None:
    body = _one_phase_body()
    assert _for_app(body, {"action": "dashboard", "reads": ["solar_one_phase"]}) == body


def _charging(**overrides: Any) -> ProgressFacts:
    from datetime import datetime, timezone

    return ProgressFacts(
        now=datetime(2026, 6, 1, 12, tzinfo=timezone.utc),
        charging=True,
        connection="charging",
        strategy="solar",
        driver="target_soc",
        soc_percent=50.0,
        target_percent=80.0,
        vehicle_max_percent=80.0,
        room_kwh=20.0,
        phases=3,
        **overrides,
    )


@pytest.mark.parametrize(
    ("amps_field", "amps"), [("measured_current_a", 12.5), ("assigned_current_a", 12.5)]
)
def test_the_bars_power_is_reckoned_on_the_one_phase_the_charge_is_on(amps_field: str, amps: float) -> None:
    three = charge_bar(_charging(**{amps_field: amps}))
    one = charge_bar(_charging(**{amps_field: amps}, switched_one_phase=True))
    assert three is not None and three["power_kw"] == pytest.approx(8.66, abs=0.01)
    assert one is not None and one["power_kw"] == pytest.approx(2.88, abs=0.01)


async def test_the_dashboard_says_one_phase_and_reckons_the_power_on_it(hass: HomeAssistant, freezer: Any) -> None:
    w = await _switched_down(hass, freezer)
    switched_at = w.t
    w.draw(0.0)
    await w.run(switched_at + 20.0)
    await w.charger_takes(1)
    await w.run(switched_at + 60.0)
    w.draw(6.0)
    await w.run(switched_at + 200.0)
    assert w.controller.switched_phases == 1 and w.controller.requested_current_a == 13

    capture = dashboard_api.capture_dashboard(hass, w.charger)
    # (The fixture's charger has no price area, so its status block leads with that; the sun's line is decided here.)
    solar = dashboard_api.status_facts(capture).solar
    assert solar is not None and solar.one_phase is True
    # A steady tick's verdict carries no current: the line takes the one asked of the charger now.
    assert _solar_line(solar) == {"code": "solar_charging_one_phase", "params": {"requested_a": 13}}
    facts = dashboard_api.progress_facts(capture)
    assert facts.switched_one_phase is True
