"""The voltage between phases (400 V TN, 230 V IT): planner maths, where it is stored, the card's
field, and the questions the flows ask."""

from __future__ import annotations

import math
from dataclasses import replace
from datetime import datetime, timezone

import pytest
import voluptuous as vol
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.spotnav.const import (
    CONF_CHARGER_PHASES,
    CONF_VOLTAGE_BETWEEN_PHASES_V,
    DOMAIN,
)
from custom_components.spotnav.execution.solar_execution import nominal_phase_voltage_v
from custom_components.spotnav.planning.grid_voltage import (
    default_voltage_between_phases_v,
    voltage_between_phases_v,
)
from custom_components.spotnav.planning.planner import (
    energy_per_slot_kwh,
    PlanRequest,
    PlannerInputError,
    power_kw,
    slots_needed,
)

from .helpers import make_entry, make_site_entry
from .messages import field, update_entity_config_message
from .world import admin, setup_charger_and_site, ws_call


def test_three_phase_power_is_root_three_times_the_voltage_between_phases() -> None:
    assert power_kw(16, 3) == pytest.approx(math.sqrt(3) * 400 * 16 / 1000)
    assert power_kw(16, 3, 230.0) == pytest.approx(math.sqrt(3) * 230 * 16 / 1000)
    assert power_kw(16, 1, 230.0) == power_kw(16, 1) == pytest.approx(3.68)


def test_an_it_network_needs_more_slots_for_the_same_energy() -> None:
    tn = slots_needed(30.0, 16, 3)
    it = slots_needed(30.0, 16, 3, 230.0)
    assert it > tn
    assert energy_per_slot_kwh(16, 3, 230.0) == pytest.approx(energy_per_slot_kwh(16, 3) * 230 / 400)


def test_the_solar_per_phase_voltage_scales_with_the_voltage_between_phases() -> None:
    assert nominal_phase_voltage_v(400, 3) == 230.0
    assert nominal_phase_voltage_v(230, 3) == pytest.approx(230 * 230 / 400)
    assert nominal_phase_voltage_v(230, 1) == 230.0


def test_a_plan_request_carries_the_voltage_and_refuses_one_that_is_not_a_choice() -> None:
    request = PlanRequest(
        area_id="SE3", timezone="Europe/Stockholm", currency="SEK", major_unit="kr", minor_unit="öre",
        documents=(), now=datetime(2026, 10, 3, 12, tzinfo=timezone.utc), phases=3, amps=16,
        requested_kwh=10.0, consumption_kwh_per_10km=2.0, voltage_between_phases_v=230.0,
    )
    assert request.validated().voltage_between_phases_v == 230.0
    with pytest.raises(PlannerInputError) as refused:
        replace(request, voltage_between_phases_v=115.0).validated()
    assert refused.value.code == "invalid_voltage"


async def test_the_voltage_is_the_sites_else_the_chargers_else_400(hass: HomeAssistant) -> None:
    charger = make_entry(
        hass, entry_id="alone", charge_control="switch.alone", current_limit=None,
        webhook_id="hook-alone", title="Alone",
    )
    assert voltage_between_phases_v(hass, charger.entry_id) == 400.0

    hass.config_entries.async_update_entry(charger, data={**charger.data, CONF_VOLTAGE_BETWEEN_PHASES_V: 230})
    assert voltage_between_phases_v(hass, charger.entry_id) == 230.0

    make_site_entry(
        hass, entry_id="site", charger_entry_ids=[charger.entry_id],
        extra_data={CONF_VOLTAGE_BETWEEN_PHASES_V: 400},
    )
    assert voltage_between_phases_v(hass, charger.entry_id) == 400.0, "the site holds it for its chargers"


async def test_a_value_that_is_not_a_choice_is_the_default(hass: HomeAssistant) -> None:
    charger = make_entry(
        hass, entry_id="odd", charge_control="switch.odd", current_limit=None,
        webhook_id="hook-odd", title="Odd",
    )
    for stored in (True, "230", 110, None, 0):
        hass.config_entries.async_update_entry(charger, data={**charger.data, CONF_VOLTAGE_BETWEEN_PHASES_V: stored})
        assert voltage_between_phases_v(hass, charger.entry_id) == 400.0


@pytest.mark.parametrize(("country", "expected"), [("NO", 230.0), ("no", 230.0), ("SE", 400.0), (None, 400.0)])
async def test_norway_suggests_230(hass: HomeAssistant, country, expected) -> None:
    hass.config.country = country
    assert default_voltage_between_phases_v(hass) == expected


# --- the card's field ------------------------------------------------------------------------


async def test_a_site_charger_gets_the_voltage_field_from_the_site_only(hass: HomeAssistant, hass_ws_client) -> None:
    charger, site = await setup_charger_and_site(hass)
    client = await admin(hass, hass_ws_client)

    result = (
        await ws_call(client, update_entity_config_message(charger.entry_id, scope="site", changes={"voltage_between_phases_v": "230"}))
    )["result"]

    assert result["ok"] is True
    assert hass.config_entries.async_get_entry(site.entry_id).data[CONF_VOLTAGE_BETWEEN_PHASES_V] == 230
    descriptors = [item for item in result["config"]["fields"] if item["field"] == "voltage_between_phases_v"]
    assert [(item["scope"], item["value"], item["choices"]) for item in descriptors] == [("site", "230", ["400", "230"])]

    refused = (
        await ws_call(client, update_entity_config_message(charger.entry_id, scope="charger", changes={"voltage_between_phases_v": "400"}))
    )["result"]
    assert refused["ok"] is False
    assert refused["field_errors"] == [{"field": "voltage_between_phases_v", "code": "not_writable"}]


async def test_a_charger_with_no_site_holds_the_voltage_itself(hass: HomeAssistant, hass_ws_client) -> None:
    charger, _ = await setup_charger_and_site(hass, site=False)
    client = await admin(hass, hass_ws_client)

    result = (
        await ws_call(client, update_entity_config_message(charger.entry_id, scope="charger", changes={"voltage_between_phases_v": "230"}))
    )["result"]

    assert result["ok"] is True
    assert hass.config_entries.async_get_entry(charger.entry_id).data[CONF_VOLTAGE_BETWEEN_PHASES_V] == 230
    assert field(result, "voltage_between_phases_v")["scope"] == "charger"
    assert field(result, "voltage_between_phases_v")["value"] == "230"


@pytest.mark.parametrize("value", ["", "115", "abc", True, None, 230.5])
async def test_a_voltage_that_is_not_a_choice_is_refused(hass: HomeAssistant, hass_ws_client, value) -> None:
    charger, _ = await setup_charger_and_site(hass, site=False)
    client = await admin(hass, hass_ws_client)

    result = (
        await ws_call(client, update_entity_config_message(charger.entry_id, scope="charger", changes={"voltage_between_phases_v": value}))
    )["result"]

    assert result["ok"] is False
    assert result["field_errors"] == [{"field": "voltage_between_phases_v", "code": "invalid_value"}]


# --- the flows -------------------------------------------------------------------------------


async def _site_form(hass: HomeAssistant):
    flow = hass.config_entries.flow
    result = await flow.async_init(DOMAIN, context={"source": "user"})
    return await flow.async_configure(result["flow_id"], {"entry_type": "site"})


async def test_the_site_form_asks_the_voltage_and_suggests_230_in_norway(hass: HomeAssistant) -> None:
    hass.config.country = "NO"
    result = await _site_form(hass)
    key = next(key for key in result["data_schema"].schema if str(key) == CONF_VOLTAGE_BETWEEN_PHASES_V)
    assert key.default() == "230"
    assert result["data_schema"].schema[key].config["options"] == ["400", "230"]

    hass.config.country = "SE"
    result = await _site_form(hass)
    key = next(key for key in result["data_schema"].schema if str(key) == CONF_VOLTAGE_BETWEEN_PHASES_V)
    assert key.default() == "400"


@pytest.mark.installation_questions
async def test_the_charger_flow_asks_the_phases_when_nothing_reads_them(hass: HomeAssistant) -> None:
    hass.config.country = "SE"
    hass.states.async_set("switch.garage", "off")
    flow = hass.config_entries.flow
    result = await flow.async_init(DOMAIN, context={"source": "user"})
    result = await flow.async_configure(result["flow_id"], {"entry_type": "charger"})
    result = await flow.async_configure(result["flow_id"], {"mode": "generic"})
    result = await flow.async_configure(result["flow_id"], {"charge_control": "switch.garage"})

    assert result["type"] is FlowResultType.FORM and result["step_id"] == "charger_installation"
    assert [str(key) for key in result["data_schema"].schema] == [CONF_CHARGER_PHASES]
    phases_key = next(iter(result["data_schema"].schema))
    assert phases_key.default is vol.UNDEFINED, "no preset answer: a person chooses"

    result = await flow.async_configure(result["flow_id"], {CONF_CHARGER_PHASES: "1"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_CHARGER_PHASES] == 1
    assert CONF_VOLTAGE_BETWEEN_PHASES_V not in result["data"]


@pytest.mark.installation_questions
async def test_the_charger_flow_asks_the_voltage_in_norway_when_there_is_no_site(hass: HomeAssistant) -> None:
    hass.config.country = "NO"
    hass.states.async_set("switch.garage", "off")
    flow = hass.config_entries.flow
    result = await flow.async_init(DOMAIN, context={"source": "user"})
    result = await flow.async_configure(result["flow_id"], {"entry_type": "charger"})
    result = await flow.async_configure(result["flow_id"], {"mode": "generic"})
    result = await flow.async_configure(result["flow_id"], {"charge_control": "switch.garage"})

    assert result["step_id"] == "charger_installation"
    assert {str(key) for key in result["data_schema"].schema} == {CONF_CHARGER_PHASES, CONF_VOLTAGE_BETWEEN_PHASES_V}
    voltage_key = next(key for key in result["data_schema"].schema if str(key) == CONF_VOLTAGE_BETWEEN_PHASES_V)
    assert voltage_key.default() == "230"

    result = await flow.async_configure(
        result["flow_id"], {CONF_CHARGER_PHASES: "3", CONF_VOLTAGE_BETWEEN_PHASES_V: "230"}
    )
    assert result["data"][CONF_CHARGER_PHASES] == 3 and result["data"][CONF_VOLTAGE_BETWEEN_PHASES_V] == 230


@pytest.mark.installation_questions
async def test_the_charger_flow_does_not_ask_the_voltage_when_a_site_holds_it(hass: HomeAssistant) -> None:
    hass.config.country = "NO"
    make_site_entry(hass, entry_id="site")
    hass.states.async_set("switch.garage", "off")
    flow = hass.config_entries.flow
    result = await flow.async_init(DOMAIN, context={"source": "user"})
    result = await flow.async_configure(result["flow_id"], {"entry_type": "charger"})
    result = await flow.async_configure(result["flow_id"], {"mode": "generic"})
    result = await flow.async_configure(result["flow_id"], {"charge_control": "switch.garage"})

    assert [str(key) for key in result["data_schema"].schema] == [CONF_CHARGER_PHASES]


async def test_the_charger_voltage_changes_the_plan_the_preview_builds(session) -> None:
    from datetime import time

    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from .relay import SE4, serve
    from .test_auto_execution import cheap_afternoon_day, TODAY, TOMORROW

    serve(session.transport)
    for day in (TODAY, TOMORROW):
        session.transport.serve(session.transport.day_path(SE4, day), 200, cheap_afternoon_day(SE4, day))
    MockConfigEntry(
        domain=DOMAIN, entry_id=session.entry_id, data={CONF_VOLTAGE_BETWEEN_PHASES_V: 230}
    ).add_to_hass(session.hass)

    await session.set_auto(departure=time(20, 0), phases=3)

    proposal = session.preview.snapshot().proposal
    assert proposal is not None
    assert proposal.power_kw == pytest.approx(power_kw(session.settings().amps, 3, 230.0))

