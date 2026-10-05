""""Fill": a manual need that is the battery's room at each calculation, so it follows the car.

* The record: `fill_to_limit`, false by default, written only when set; a non-boolean is refused.
* The codec: optional on the wire. A replacement that leaves it out keeps it, unless the same replacement
  moves `requested_kwh` (a client that does not know the field chose an amount, and an amount is not "fill").
  The webhook leaves it out of its settings record.
* Planning: with a level and a battery size the need is the room (to the car's own limit), capped as a manual
  amount at the room is, so the car ends the charge; without them the stored amount stands, and the status
  says so. A target ignores it.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest
from freezegun import freeze_time
from homeassistant.core import HomeAssistant

from custom_components.spotnav.api import dashboard as dashboard_api
from custom_components.spotnav.api.settings import (
    OPTIONAL_SETTINGS_KEYS,
    decode_settings,
    encode_settings,
    replacement_mutator,
)
from custom_components.spotnav.api.webhook import APP_UNREAD_SETTINGS
from custom_components.spotnav.planning.auto_settings import (
    AutoSettings,
    AutoSettingsError,
    DRIVER_MANUAL_KWH,
    TargetSocIntent,
)
from custom_components.spotnav.runtime import preview_for
from custom_components.spotnav.planning.status_compose import STATUS_CODES
from tests.relay import serve as serve_prices
from tests.test_dashboard_api import NOW

from .messages import read_settings_message, update_settings_message
from .world import admin, charger_and_car, settings_of, ws_call

pytestmark = pytest.mark.usefixtures("offline_relay")

CAPACITY = 77.0


def body(**changes: Any) -> dict[str, Any]:
    encoded = encode_settings(AutoSettings(area_id="SE4", amps=10, phases=3))
    return {key: value for key, value in encoded.items() if key != "revision"} | changes


# ------------------------------------------------------------------------------- the record


def test_the_choice_is_stored_only_when_it_is_set() -> None:
    plain = AutoSettings(area_id="SE4", amps=10)
    assert plain.fill_to_limit is False and "fill_to_limit" not in plain.as_dict()
    filled = replace(plain, fill_to_limit=True).validated()
    assert filled.as_dict()["fill_to_limit"] is True
    assert AutoSettings.from_stored(filled.as_dict()) == filled
    assert AutoSettings.from_stored(plain.as_dict()).fill_to_limit is False


@pytest.mark.parametrize("value", [1, 0, "true", None])
def test_validation_refuses_what_is_not_a_boolean(value: Any) -> None:
    with pytest.raises(AutoSettingsError) as error:
        replace(AutoSettings(), fill_to_limit=value).validated()
    assert error.value.code == "invalid_energy"


# ------------------------------------------------------------------------------- the codec


def test_the_wire_names_the_choice_and_marks_it_optional() -> None:
    assert "fill_to_limit" in OPTIONAL_SETTINGS_KEYS
    assert encode_settings(AutoSettings())["fill_to_limit"] is False
    assert decode_settings(body(fill_to_limit=True)).fill_to_limit is True
    legacy = body()
    del legacy["fill_to_limit"]
    assert decode_settings(legacy).fill_to_limit is False


@pytest.mark.parametrize("value", [1, "true", None, [], {}])
def test_anything_but_a_boolean_is_refused_on_the_wire(value: Any) -> None:
    with pytest.raises(AutoSettingsError) as error:
        decode_settings(body(fill_to_limit=value))
    assert error.value.code == "invalid_energy"


def test_a_replacement_without_the_choice_keeps_it_unless_it_moves_the_amount() -> None:
    current = replace(AutoSettings(area_id="SE4", amps=10, phases=3, requested_kwh=20.0), fill_to_limit=True)
    legacy = body(requested_kwh=20.0, amps=16)
    del legacy["fill_to_limit"]
    kept = replacement_mutator(decode_settings(legacy), keep_fill_to_limit=True)(current)
    assert kept.fill_to_limit is True and kept.amps == 16

    moved = body(requested_kwh=6.0)
    del moved["fill_to_limit"]
    cleared = replacement_mutator(decode_settings(moved), keep_fill_to_limit=True)(current)
    assert cleared.fill_to_limit is False and cleared.requested_kwh == 6.0

    named = replacement_mutator(decode_settings(body(requested_kwh=20.0, fill_to_limit=False)))(current)
    assert named.fill_to_limit is False


def test_the_status_codes_are_registered() -> None:
    assert STATUS_CODES["filling_to_limit"] == ("normal", ("kwh",))
    assert STATUS_CODES["fill_room_unknown"] == ("notice", ("kwh",))


# ------------------------------------------------------------------------- the two transports


def test_the_app_does_not_read_the_choice_yet() -> None:
    assert "fill_to_limit" in APP_UNREAD_SETTINGS


async def post_settings(client: Any, revision: int, settings_body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    response = await client.post(
        "/api/webhook/webhook-a",
        json={"version": 1, "action": "settings", "expected_revision": revision, "settings": settings_body},
    )
    return response.status, await response.json()


async def test_the_card_writes_the_choice_and_an_older_app_neither_sees_nor_clears_it(
    hass: HomeAssistant, hass_client_no_auth, hass_ws_client
) -> None:
    from .helpers import webhook_dashboard
    from .world import setup_charger

    entry = await setup_charger(hass)
    socket = await admin(hass, hass_ws_client)
    client = await hass_client_no_auth()
    before = settings_of(hass, entry.entry_id)

    written = await ws_call(
        socket,
        update_settings_message(entry.entry_id, before.revision, body(fill_to_limit=True, requested_kwh=30.0)),
    )
    assert written["result"]["ok"] is True and written["result"]["settings"]["fill_to_limit"] is True
    stored = settings_of(hass, entry.entry_id)
    assert stored.fill_to_limit is True

    # A stale revision is refused like any other field's.
    stale = await ws_call(socket, update_settings_message(entry.entry_id, before.revision, body(fill_to_limit=False)))
    assert stale["result"]["ok"] is False and stale["result"]["error"] == "revision_conflict"

    dashboard = await webhook_dashboard(client, "webhook-a")
    assert "fill_to_limit" not in dashboard["settings"]
    # The older app writes back the record it read, the amount unchanged: the choice is kept.
    app_body = body(amps=16, requested_kwh=30.0)
    del app_body["fill_to_limit"]
    status, answer = await post_settings(client, stored.revision, app_body)
    assert status == 200 and answer["ok"] is True and "fill_to_limit" not in answer["settings"]
    assert settings_of(hass, entry.entry_id).fill_to_limit is True
    read = await ws_call(socket, read_settings_message(entry.entry_id))
    assert read["result"]["settings"]["fill_to_limit"] is True

    # The older app moves its slider: an amount, so the choice goes.
    app_body = body(amps=16, requested_kwh=8.0)
    del app_body["fill_to_limit"]
    status, answer = await post_settings(client, settings_of(hass, entry.entry_id).revision, app_body)
    assert status == 200 and answer["ok"] is True
    assert settings_of(hass, entry.entry_id).fill_to_limit is False
    assert settings_of(hass, entry.entry_id).requested_kwh == 8.0

    # Opting in reads it.
    response = await client.post(
        "/api/webhook/webhook-a",
        json={"version": 1, "action": "dashboard", "api_version": 1, "reads": ["fill_to_limit"]},
    )
    assert (await response.json())["settings"]["fill_to_limit"] is False


# ------------------------------------------------------------------------------- planning


def _status(hass: HomeAssistant, charger: Any) -> dict[str, Any]:
    payload = dashboard_api.serialize_dashboard(dashboard_api.capture_dashboard(hass, charger), can_act=True)
    return {line["code"]: line["params"] for line in payload["status"]["lines"]}


async def test_fill_plans_the_room_and_follows_the_car(hass: HomeAssistant, transport: Any) -> None:
    serve_prices(transport, rising=True)
    with freeze_time(NOW):
        charger, _, soc_entity = await charger_and_car(
            hass, capacity=CAPACITY, soc_percent="89", driver=DRIVER_MANUAL_KWH, requested_kwh=30.0,
            target=TargetSocIntent(), amps=16, phases=3, fill_to_limit=True,
        )
        preview = preview_for(hass, charger.entry_id)
        await hass.async_block_till_done()
        snapshot = preview.snapshot()
        room = CAPACITY * 0.11 / 0.9
        assert snapshot.proposal is not None
        assert snapshot.proposal.requested_kwh == pytest.approx(room, abs=0.01)
        assert snapshot.room_limited and snapshot.fill == "battery"
        codes = _status(hass, charger)
        assert codes["filling_to_limit"] == {"kwh": round(room, 1)}
        assert "need_limited_by_room" not in codes and "fill_room_unknown" not in codes

        # The car is used and comes back at 80 %: the need is the new room, not what was planned before.
        hass.states.async_set(soc_entity, "80", {"device_class": "battery", "unit_of_measurement": "%"})
        await hass.async_block_till_done()
        await preview.async_recalculate()
        await hass.async_block_till_done()
        later = preview.snapshot()
        assert later.proposal is not None
        assert later.proposal.requested_kwh == pytest.approx(CAPACITY * 0.20 / 0.9, abs=0.01)


async def test_fill_without_a_battery_size_plans_the_stored_amount_and_says_so(
    hass: HomeAssistant, transport: Any
) -> None:
    serve_prices(transport, rising=True)
    with freeze_time(NOW):
        charger, _, _ = await charger_and_car(
            hass, capacity=None, soc_percent="89", driver=DRIVER_MANUAL_KWH, requested_kwh=12.0,
            target=TargetSocIntent(), amps=16, phases=3, fill_to_limit=True,
        )
        preview = preview_for(hass, charger.entry_id)
        await hass.async_block_till_done()
        snapshot = preview.snapshot()
        assert snapshot.proposal is not None and snapshot.proposal.requested_kwh == pytest.approx(12.0)
        assert not snapshot.room_limited and snapshot.fill == "unknown_room"
        codes = _status(hass, charger)
        assert codes["fill_room_unknown"] == {"kwh": 12.0}
        assert "filling_to_limit" not in codes


async def test_without_the_choice_the_amount_is_planned_as_before(hass: HomeAssistant, transport: Any) -> None:
    serve_prices(transport, rising=True)
    with freeze_time(NOW):
        charger, _, _ = await charger_and_car(
            hass, capacity=CAPACITY, soc_percent="40", driver=DRIVER_MANUAL_KWH, requested_kwh=6.0,
            target=TargetSocIntent(), amps=16, phases=3,
        )
        preview = preview_for(hass, charger.entry_id)
        await hass.async_block_till_done()
        snapshot = preview.snapshot()
        assert snapshot.proposal is not None and snapshot.proposal.requested_kwh == pytest.approx(6.0)
        assert snapshot.fill is None and not snapshot.room_limited
        codes = _status(hass, charger)
        assert "filling_to_limit" not in codes and "fill_room_unknown" not in codes


async def test_a_target_ignores_the_choice(hass: HomeAssistant, transport: Any) -> None:
    serve_prices(transport, rising=True)
    with freeze_time(NOW):
        charger, _, _ = await charger_and_car(hass, capacity=CAPACITY, soc_percent="40", fill_to_limit=True)
        preview = preview_for(hass, charger.entry_id)
        await hass.async_block_till_done()
        snapshot = preview.snapshot()
        assert snapshot.proposal is not None
        assert snapshot.proposal.requested_kwh == pytest.approx(CAPACITY * 0.40 / 0.9, abs=0.01)
        assert snapshot.fill is None


async def test_the_energy_entity_sets_an_amount_and_so_clears_the_choice(hass: HomeAssistant) -> None:
    from .world import call, entity_id, go_auto, setup_charger

    entry = await setup_charger(hass)
    await go_auto(hass, fill_to_limit=True)
    assert settings_of(hass, entry.entry_id).fill_to_limit is True
    energy = entity_id(hass, entry.entry_id, "requested_energy", "number")
    await call(hass, "number", "set_value", {"entity_id": energy, "value": 7.0})
    stored = settings_of(hass, entry.entry_id)
    assert stored.requested_kwh == 7.0 and stored.fill_to_limit is False
