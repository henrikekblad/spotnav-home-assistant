"""The car's minimum charge level on a real charger entry: the floor is the planned car's own, the plan is made for
the rest of the need (floor to target), the status leads with the floor charge, and a charge the floor started is
recorded as SpotNav's own, not as a car that came back."""

from __future__ import annotations

from typing import Any

import pytest
from freezegun import freeze_time
from homeassistant.core import callback, HomeAssistant

from custom_components.spotnav.api import dashboard as dashboard_api
from custom_components.spotnav.planning.status_compose import compose_status, StatusFacts, STATUS_CODES
from custom_components.spotnav.runtime import charger_data, domain_data, preview_for
from custom_components.spotnav.sessions.model import STARTED_PLAN_WINDOW
from custom_components.spotnav.sessions.recorder import started_by
from custom_components.spotnav.vehicles import vehicle_properties

from .test_dashboard_api import NOW
from .world import CAPACITY, charger_and_car, controller_of

pytestmark = pytest.mark.usefixtures("offline_relay")


def _follow_switches(hass: HomeAssistant) -> list[str]:
    calls: list[str] = []

    @callback
    def on_call(event: Any) -> None:
        service, data = event.data["service"], event.data.get("service_data", {})
        if service in ("turn_on", "turn_off"):
            targets = data.get("entity_id", [])
            for entity_id in [targets] if isinstance(targets, str) else targets:
                calls.append(service)
                hass.states.async_set(entity_id, "on" if service == "turn_on" else "off")

    hass.bus.async_listen("call_service", on_call)
    return calls


async def _floor(hass: HomeAssistant, car: str, percent: int | None) -> None:
    await vehicle_properties.async_update_vehicle_properties(
        hass, domain_data(hass).decision_store, car, {vehicle_properties.KEY_MIN: percent}
    )


def test_the_floor_line_and_its_place() -> None:
    assert STATUS_CODES["min_soc_charging"] == ("normal", ("percent",))
    facts = StatusFacts(now=NOW, has_settings=True, charging=True, min_soc_percent=30.0)
    lines = compose_status(facts)["lines"]
    assert lines[0] == {"code": "min_soc_charging", "params": {"percent": 30}}
    assert all(line["code"] != "charging_now" for line in lines)
    # A pause wins: no floor line while one holds.
    paused = compose_status(StatusFacts(now=NOW, has_settings=True, paused=True, pause_choice="until_resumed",
                                        min_soc_percent=30.0))["lines"]
    assert paused[0]["code"] == "paused"
    assert all(line["code"] != "min_soc_charging" for line in paused)


def test_a_floor_charge_is_recorded_as_spotnavs_own() -> None:
    assert started_by("min_soc", "cheapest") == STARTED_PLAN_WINDOW


@freeze_time(NOW)
async def test_a_car_below_its_floor_is_charged_now_planned_from_the_floor_and_said_so(hass: HomeAssistant) -> None:
    calls = _follow_switches(hass)
    charger, car, _soc = await charger_and_car(hass, soc_percent="20")
    controller = controller_of(hass, charger.entry_id)
    preview = preview_for(hass, charger.entry_id)
    before = preview.snapshot().proposal
    await _floor(hass, car, 40)
    await preview.async_recalculate()
    await hass.async_block_till_done()

    floor = charger_data(hass, charger.entry_id).min_soc
    assert floor is not None
    await floor.async_evaluate()
    await hass.async_block_till_done()

    assert controller.charge_origin == "min_soc" and "turn_on" in calls
    # Planned for the rest of the need: from the floor (40 %) to the target (80 %), not from 20 %.
    proposal = preview.snapshot().proposal
    assert proposal is not None and before is not None
    assert proposal.requested_kwh == pytest.approx((80 - 40) / 100 * CAPACITY / 0.9, abs=0.05)
    payload = dashboard_api.serialize_dashboard(dashboard_api.capture_dashboard(hass, charger), can_act=True)
    assert payload["status"]["lines"][0] == {"code": "min_soc_charging", "params": {"percent": 40}}


@freeze_time(NOW)
async def test_with_no_floor_nothing_changes(hass: HomeAssistant) -> None:
    charger, _car, _soc = await charger_and_car(hass, soc_percent="20")
    floor = charger_data(hass, charger.entry_id).min_soc
    await floor.async_evaluate()
    await hass.async_block_till_done()
    assert controller_of(hass, charger.entry_id).charge_origin != "min_soc"
