"""The phases a charge uses: the smaller of the charger's wiring and the planned vehicle's onboard charger.

Pins the combinations (a one-phase car on three-phase wiring, a three-phase car on one-phase wiring, no
vehicle), what the plan's power says, the migration of an older release's settings `phases`, and that the
settings contract still accepts a `phases` it ignores (an older app sends it in every full replacement).
"""

from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from freezegun import freeze_time
from homeassistant.core import HomeAssistant

from custom_components.spotnav.api import dashboard as dashboard_api
from custom_components.spotnav.api.settings import decode_settings
from custom_components.spotnav.const import CONF_CHARGER_PHASES, CONF_PHASE_WIRING
from custom_components.spotnav.planning.auto_settings import AutoSettings, AutoSettingsError
from custom_components.spotnav.planning.phases import (
    async_migrate_settings_phases,
    charger_wiring,
    charging_phases,
    effective_phases,
)
from custom_components.spotnav.runtime import domain_data
from custom_components.spotnav.vehicles import vehicle_properties
from tests.helpers import set_charger_phases
from tests.relay import SE4, serve
from tests.test_dashboard_api import NOW
from tests.test_settings_webhook import post, settings_payload, stored
from tests.world import add_car, charger_and_car, go_auto, setup_charger, setup_charger_and_site

pytestmark = pytest.mark.usefixtures("offline_relay")

DASHBOARD_FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "dashboard"


async def set_onboard(hass: HomeAssistant, vehicle_id: str, phases: int | None) -> None:
    store = domain_data(hass).decision_store
    await vehicle_properties.async_update_vehicle_properties(
        hass, store, vehicle_id, {vehicle_properties.KEY_ONBOARD_PHASES: phases}
    )


# ----------------------------------------------------------------------------- min(wiring, car)


@pytest.mark.parametrize(
    ("wiring", "onboard", "phases", "limited"),
    [
        (3, 1, 1, True),  # a one-phase car on three-phase wiring charges on one
        (1, 3, 1, False),  # a three-phase car on one-phase wiring charges on one
        (3, 3, 3, False),
        (1, 1, 1, False),
        (3, None, 3, False),  # nothing told about the car: three, so the wiring decides
    ],
)
async def test_a_charge_uses_the_smaller_of_the_wiring_and_the_onboard_charger(
    hass: HomeAssistant, wiring: int, onboard: int | None, phases: int, limited: bool
) -> None:
    charger, car, _ = await charger_and_car(hass)
    set_charger_phases(hass, charger.entry_id, wiring)
    await set_onboard(hass, car, onboard)

    result = charging_phases(hass, charger.entry_id)

    assert (result.phases, result.wiring, result.limited_by_vehicle) == (phases, wiring, limited)
    assert result.vehicle == (3 if onboard is None else onboard)
    assert effective_phases(hass, charger.entry_id) == phases


@pytest.mark.parametrize("wiring", [1, 3])
async def test_with_no_vehicle_the_charger_wiring_decides(hass: HomeAssistant, wiring: int) -> None:
    charger = await setup_charger(hass)
    set_charger_phases(hass, charger.entry_id, wiring)

    result = charging_phases(hass, charger.entry_id)

    assert (result.phases, result.wiring, result.vehicle, result.limited_by_vehicle) == (wiring, wiring, None, False)


async def test_a_charger_in_a_site_takes_the_sites_wiring_not_its_own_answer(hass: HomeAssistant) -> None:
    charger, site = await setup_charger_and_site(
        hass, phase_wiring={"entry_a": {"phases": 1, "phase": "L2"}}
    )
    assert site is not None and site.data[CONF_PHASE_WIRING]["entry_a"]["phases"] == 1
    # Its own answer is for a charger in no site, and is not read here.
    set_charger_phases(hass, charger.entry_id, 3)

    assert charger_wiring(hass, charger.entry_id) == 1
    assert effective_phases(hass, charger.entry_id) == 1


async def test_the_planned_vehicle_is_the_one_the_settings_name(hass: HomeAssistant) -> None:
    charger, first, _ = await charger_and_car(hass)
    second = add_car(hass, "Niro")
    await set_onboard(hass, second, 1)
    set_charger_phases(hass, charger.entry_id, 3)

    assert charging_phases(hass, charger.entry_id, vehicle_id=first).phases == 3
    assert charging_phases(hass, charger.entry_id, vehicle_id=second).phases == 1


# ----------------------------------------------------------------------------- the plan's power


@freeze_time(NOW)
async def test_the_plan_power_follows_the_effective_phases(hass: HomeAssistant, transport: Any) -> None:
    charger, car, _ = await charger_and_car(hass)
    serve(transport, flat=True)
    set_charger_phases(hass, charger.entry_id, 3)

    async def proposal_kw() -> float:
        await go_auto(hass, charger.entry_id, amps=16, requested_kwh=10.0, phases=3)
        response = dashboard_api.serialize_dashboard(dashboard_api.capture_dashboard(hass, charger), can_act=True)
        proposal = response["plan"]["proposal"]
        assert proposal is not None and proposal["phases"] == response["charging_phases"]["phases"]
        return proposal["power_kw"]

    assert await proposal_kw() == pytest.approx(11.085, abs=0.01)
    await set_onboard(hass, car, 1)
    await hass.async_block_till_done()
    assert await proposal_kw() == pytest.approx(3.68, abs=0.01)


# ----------------------------------------------------------------------------- the migration


async def _with_stored_phases(hass: HomeAssistant, entry_id: str, phases: int | None) -> None:
    store = domain_data(hass).auto_store
    assert store is not None
    await store.async_update(entry_id, mutate=lambda settings: replace(settings, phases=phases))


async def test_a_stored_phase_count_below_the_wiring_becomes_the_vehicles_onboard_charger(
    hass: HomeAssistant,
) -> None:
    charger, car, _ = await charger_and_car(hass)
    set_charger_phases(hass, charger.entry_id, 3)
    await _with_stored_phases(hass, charger.entry_id, 1)
    store = domain_data(hass).auto_store
    revision = store.settings(charger.entry_id).revision

    assert await async_migrate_settings_phases(hass, charger.entry_id) is True

    assert vehicle_properties.stored_properties(hass, car).onboard_phases == 1
    assert effective_phases(hass, charger.entry_id) == 1
    settings = store.settings(charger.entry_id)
    assert settings.phases is None, "and the old value is no longer read"
    assert settings.revision == revision, "forgetting it is not an edit: an installed plan stands"
    # Once: the second start finds nothing to move.
    assert await async_migrate_settings_phases(hass, charger.entry_id) is False
    assert charger.data.get(CONF_CHARGER_PHASES) == 3


async def test_with_no_vehicle_it_becomes_the_wiring_of_a_charger_in_no_site(hass: HomeAssistant) -> None:
    charger = await setup_charger(hass)
    assert CONF_CHARGER_PHASES not in charger.data
    await _with_stored_phases(hass, charger.entry_id, 1)

    assert await async_migrate_settings_phases(hass, charger.entry_id) is True

    assert charger.data[CONF_CHARGER_PHASES] == 1
    assert effective_phases(hass, charger.entry_id) == 1
    assert domain_data(hass).auto_store.settings(charger.entry_id).phases is None


async def test_a_value_that_does_not_lower_the_wiring_moves_nothing(hass: HomeAssistant) -> None:
    charger, car, _ = await charger_and_car(hass)
    set_charger_phases(hass, charger.entry_id, 1)
    await _with_stored_phases(hass, charger.entry_id, 1)

    assert await async_migrate_settings_phases(hass, charger.entry_id) is False

    assert vehicle_properties.stored_properties(hass, car).onboard_phases is None
    assert domain_data(hass).auto_store.settings(charger.entry_id).phases is None

    set_charger_phases(hass, charger.entry_id, 3)
    await _with_stored_phases(hass, charger.entry_id, 3)
    assert await async_migrate_settings_phases(hass, charger.entry_id) is False
    assert vehicle_properties.stored_properties(hass, car).onboard_phases is None


async def test_a_vehicle_that_already_has_an_answer_keeps_it(hass: HomeAssistant) -> None:
    charger, car, _ = await charger_and_car(hass)
    set_charger_phases(hass, charger.entry_id, 3)
    await set_onboard(hass, car, 3)
    await _with_stored_phases(hass, charger.entry_id, 1)

    assert await async_migrate_settings_phases(hass, charger.entry_id) is False

    assert vehicle_properties.stored_properties(hass, car).onboard_phases == 3
    assert domain_data(hass).auto_store.settings(charger.entry_id).phases is None


async def test_a_site_charger_without_a_vehicle_has_no_wiring_of_its_own_to_lower(hass: HomeAssistant) -> None:
    charger, site = await setup_charger_and_site(hass, phase_wiring={"entry_a": {"phases": 3, "phase": None}})
    await _with_stored_phases(hass, charger.entry_id, 1)

    assert await async_migrate_settings_phases(hass, charger.entry_id) is False

    assert CONF_CHARGER_PHASES not in charger.data
    assert effective_phases(hass, charger.entry_id) == 3
    assert domain_data(hass).auto_store.settings(charger.entry_id).phases is None


# ----------------------------------------------------------------------------- the wire


def _body(**changes: Any) -> dict[str, Any]:
    from custom_components.spotnav.api.settings import encode_settings

    body = {k: v for k, v in encode_settings(AutoSettings(area_id=SE4, amps=10), 3).items() if k != "revision"}
    body.update(changes)
    return body


@pytest.mark.parametrize("phases", [1, 3, None])
def test_the_codec_accepts_a_phases_it_ignores(phases: int | None) -> None:
    decoded = decode_settings(_body(phases=phases))
    assert decoded.phases is None and decoded.amps == 10


def test_the_codec_accepts_a_replacement_without_phases() -> None:
    body = _body()
    del body["phases"]
    assert decode_settings(body).phases is None


@pytest.mark.parametrize("phases", [2, "3", 3.5, True])
def test_the_codec_still_refuses_a_phases_that_is_nothing_it_knew(phases: Any) -> None:
    with pytest.raises(AutoSettingsError):
        decode_settings(_body(phases=phases))


async def test_the_webhook_still_accepts_phases_and_ignores_it(hass: HomeAssistant, hass_client_no_auth) -> None:
    entry = await setup_charger(hass)
    client = await hass_client_no_auth()

    status, answer = await post(client, "webhook-a", settings_payload(stored(hass), area_id=SE4, amps=16, phases=1))

    assert status == 200 and answer["ok"] is True
    assert stored(hass, entry.entry_id).phases is None, "never stored"
    assert answer["settings"]["phases"] == 3, "and the effective count is what comes back"
    assert answer["settings"]["amps"] == 16


async def test_the_webhook_accepts_a_replacement_that_leaves_phases_out(hass: HomeAssistant, hass_client_no_auth) -> None:
    await setup_charger(hass)
    client = await hass_client_no_auth()
    payload = settings_payload(stored(hass), area_id=SE4, amps=16)
    del payload["settings"]["phases"]

    status, answer = await post(client, "webhook-a", payload)

    assert status == 200 and answer["ok"] is True and answer["settings"]["phases"] == 3


async def test_the_settings_answer_carries_what_the_car_limits_it_to(hass: HomeAssistant, hass_client_no_auth) -> None:
    charger, car, _ = await charger_and_car(hass)
    set_charger_phases(hass, charger.entry_id, 3)
    await set_onboard(hass, car, 1)
    client = await hass_client_no_auth()

    status, answer = await post(client, "webhook-soc", settings_payload(stored(hass, charger.entry_id), amps=16, phases=3))

    assert status == 200 and answer["settings"]["phases"] == 1


# ----------------------------------------------------------------------------- the dashboard


async def test_the_dashboard_names_the_phases_and_what_limits_them(hass: HomeAssistant) -> None:
    charger, car, _ = await charger_and_car(hass)
    set_charger_phases(hass, charger.entry_id, 3)
    await set_onboard(hass, car, 1)

    response = dashboard_api.serialize_dashboard(dashboard_api.capture_dashboard(hass, charger), can_act=True)

    assert response["charging_phases"] == {"phases": 1, "charger": 3, "vehicle": 1, "limited_by": "vehicle"}
    assert response["settings"] is None or response["settings"]["phases"] == 1
    assert [row["onboard_phases"] for row in response["vehicles"]] == [1]


@freeze_time(NOW)
async def test_the_limited_by_the_car_dashboard_is_pinned_as_a_fixture(hass: HomeAssistant, transport: Any) -> None:
    charger, car, _ = await charger_and_car(hass)
    serve(transport, flat=True)
    set_charger_phases(hass, charger.entry_id, 3)
    await set_onboard(hass, car, 1)
    await go_auto(hass, charger.entry_id, amps=16, requested_kwh=10.0, phases=3)
    payload = json.loads(
        json.dumps(dashboard_api.serialize_dashboard(dashboard_api.capture_dashboard(hass, charger), can_act=True))
    )
    text = json.dumps(payload).replace(car, "vehicle_ev6")
    import re

    payload = json.loads(re.sub(r'"[0-9a-f]{32}"', '"<id>"', text))
    assert payload["charging_phases"]["limited_by"] == "vehicle"
    path = DASHBOARD_FIXTURE_DIR / "target_soc_phases_limited_by_vehicle.json"
    if os.environ.get("SPOTNAV_WRITE_FIXTURES") == "1":
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return
    assert json.loads(path.read_text(encoding="utf-8")) == payload, f"{path.name} drifted from the backend"


# ----------------------------------------------------------------------------- the vehicle command


async def test_update_vehicle_writes_the_onboard_charger_under_compare_and_set(
    hass: HomeAssistant, hass_ws_client
) -> None:
    from tests.test_vehicle_properties import update_message
    from tests.world import admin, ws_call

    charger, car, _ = await charger_and_car(hass)
    client = await admin(hass, hass_ws_client)

    first = (await ws_call(client, update_message(charger.entry_id, car, {"onboard_phases": 1}, {"onboard_phases": 3})))["result"]
    assert first["ok"] is True and first["vehicle"]["onboard_phases"] == 1
    assert vehicle_properties.stored_properties(hass, car).onboard_phases == 1

    stale = (await ws_call(client, update_message(charger.entry_id, car, {"onboard_phases": 3}, {"onboard_phases": 3})))["result"]
    assert stale["ok"] is False and stale["error"] == "spotnav_conflict"

    refused = (await ws_call(client, update_message(charger.entry_id, car, {"onboard_phases": 2})))["result"]
    assert refused["ok"] is False and refused["error"] == "spotnav_invalid_value"
    assert refused["field_errors"] == [{"field": "onboard_phases", "code": "invalid_onboard_phases"}]
    assert vehicle_properties.stored_properties(hass, car).onboard_phases == 1

    cleared = (await ws_call(client, update_message(charger.entry_id, car, {"onboard_phases": None})))["result"]
    assert cleared["ok"] is True and cleared["vehicle"]["onboard_phases"] == 3
    assert vehicle_properties.stored_properties(hass, car).onboard_phases is None


async def test_a_charger_in_no_site_chooses_its_wiring_and_a_site_charger_cannot(
    hass: HomeAssistant, hass_ws_client
) -> None:
    from tests.messages import field, get_message, update_entity_config_message
    from tests.world import admin, ws_call

    charger = await setup_charger(hass)
    client = await admin(hass, hass_ws_client)

    read = (await ws_call(client, get_message(charger.entry_id)))["result"]
    descriptor = field(read, "charger_phases")
    assert descriptor["scope"] == "charger" and descriptor["choices"] == ["1", "3"]
    assert descriptor["value"] == "3" and descriptor["writable"] is True

    written = (
        await ws_call(
            client,
            update_entity_config_message(
                charger.entry_id, scope="charger", expected={"charger_phases": "3"}, changes={"charger_phases": "1"}
            ),
        )
    )["result"]
    assert written["ok"] is True, written
    await hass.async_block_till_done()
    assert hass.config_entries.async_get_entry(charger.entry_id).data[CONF_CHARGER_PHASES] == 1

    bad = (
        await ws_call(
            client, update_entity_config_message(charger.entry_id, scope="charger", changes={"charger_phases": "2"})
        )
    )["result"]
    assert bad["ok"] is False and bad["field_errors"] == [{"field": "charger_phases", "code": "invalid_value"}]

    site_charger, _ = await setup_charger_and_site(hass, "entry_s")
    read = (await ws_call(client, get_message(site_charger.entry_id)))["result"]
    assert all(item["field"] != "charger_phases" for item in read["config"]["fields"])
    refused = (
        await ws_call(
            client,
            update_entity_config_message(site_charger.entry_id, scope="charger", changes={"charger_phases": "1"}),
        )
    )["result"]
    assert refused["ok"] is False
    assert refused["field_errors"] == [{"field": "charger_phases", "code": "not_writable"}]
