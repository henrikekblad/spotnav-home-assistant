"""The departure date in settings v1: additive on the wire and in storage, judged at every write.

* The record: written only when set, read back when present, and a stored record that never had it is
  an ordinary daily departure (so neither an upgrade nor a downgrade refuses a record).
* The codec: an ISO date or `null`; absent in a replacement body means "keep what is stored" (an older
  client must not clear what the card set), `null` clears; nothing else is read hopefully.
* The write rule (every transport goes through one controller method): not in the past, at most seven
  days ahead in the area's zone; a stored date that has gone by is ignored by planning and cleared by the
  next write; a date is judged only when it is new.
* The three paths: the card's WebSocket write, the app's webhook, and the entities.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, time
from typing import Any

import pytest
from freezegun import freeze_time
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError

from custom_components.spotnav.api.settings import (
    OPTIONAL_SETTINGS_KEYS,
    REQUIRED_SETTINGS_KEYS,
    SETTINGS_KEYS,
    decode_settings,
    departure_date_from_wire,
    encode_settings,
    replacement_mutator,
)
from custom_components.spotnav.planning.auto_settings import AutoSettings, AutoSettingsError

from .messages import update_settings_message
from .world import admin, call, entity_id, settings_of, setup_charger, ws_call

pytestmark = pytest.mark.usefixtures("offline_relay")

#: Home Assistant mints the WebSocket user's token at the real clock, so the sockets below freeze only
#: around the one write that is judged against "today".
TODAY = "2026-09-22 06:00:00"


# ------------------------------------------------------------------------------- the record


def test_the_date_is_stored_only_when_set_and_a_record_without_it_reads_as_daily() -> None:
    daily = AutoSettings(area_id="SE4", amps=10, phases=3)
    assert "departure_date" not in daily.as_dict()
    assert AutoSettings.from_stored(daily.as_dict()).departure_date is None

    dated = replace(daily, departure_date=date(2026, 9, 27))
    assert dated.as_dict()["departure_date"] == "2026-09-27"
    assert AutoSettings.from_stored(dated.as_dict()) == dated.validated()


@pytest.mark.parametrize("stored", [20260927, "2026-02-30", "Sunday", ["2026-09-27"]])
def test_a_stored_date_that_is_not_a_date_is_refused_by_name(stored: Any) -> None:
    record = AutoSettings(area_id="SE4", amps=10, phases=3).as_dict() | {"departure_date": stored}
    with pytest.raises(AutoSettingsError) as error:
        AutoSettings.from_stored(record)
    assert error.value.code == "invalid_departure"


def test_a_record_still_refuses_a_field_this_release_does_not_know() -> None:
    record = AutoSettings(area_id="SE4", amps=10, phases=3).as_dict() | {"departure_day": "2026-09-27"}
    with pytest.raises(AutoSettingsError) as error:
        AutoSettings.from_stored(record)
    assert error.value.code == "unknown_field"


def test_validation_wants_a_calendar_date_not_a_datetime() -> None:
    from datetime import datetime

    with pytest.raises(AutoSettingsError) as error:
        replace(AutoSettings(), departure_date=datetime(2026, 9, 27, 8, 0)).validated()
    assert error.value.code == "invalid_departure"


# ------------------------------------------------------------------------------- the codec


def body(**changes: Any) -> dict[str, Any]:
    encoded = encode_settings(AutoSettings(area_id="SE4", amps=10, phases=3))
    return {key: value for key, value in encoded.items() if key != "revision"} | changes


def test_the_wire_names_the_date_and_marks_it_optional() -> None:
    assert "departure_date" in SETTINGS_KEYS
    assert OPTIONAL_SETTINGS_KEYS == {"departure_date"}
    assert REQUIRED_SETTINGS_KEYS == SETTINGS_KEYS - {"departure_date"}
    assert encode_settings(AutoSettings())["departure_date"] is None
    assert encode_settings(replace(AutoSettings(), departure_date=date(2026, 9, 27)))["departure_date"] == "2026-09-27"


def test_a_body_may_leave_the_date_out_and_it_reads_as_none() -> None:
    legacy = body()
    del legacy["departure_date"]
    assert decode_settings(legacy).departure_date is None


def test_a_body_names_a_date_or_null() -> None:
    assert decode_settings(body(departure_date="2026-09-27")).departure_date == date(2026, 9, 27)
    assert decode_settings(body(departure_date=None)).departure_date is None


@pytest.mark.parametrize(
    "value",
    ["2026-9-27", "27/09/2026", "2026-09-27T08:00:00", "2026-02-30", "2026-13-01", "", " 2026-09-27", 20260927, True, [], {}],
)
def test_anything_else_is_refused_as_an_invalid_departure(value: Any) -> None:
    with pytest.raises(AutoSettingsError) as error:
        decode_settings(body(departure_date=value))
    assert error.value.code == "invalid_departure"
    with pytest.raises(AutoSettingsError):
        departure_date_from_wire(value)


def test_a_replacement_without_the_date_keeps_the_stored_one_and_null_clears_it() -> None:
    current = replace(AutoSettings(area_id="SE4", amps=10, phases=3), departure_date=date(2026, 9, 27))
    keep = replacement_mutator(decode_settings(body()), keep_departure_date=True)(current)
    assert keep.departure_date == date(2026, 9, 27)
    clear = replacement_mutator(decode_settings(body(departure_date=None)))(current)
    assert clear.departure_date is None
    change = replacement_mutator(decode_settings(body(departure_date="2026-09-28")))(current)
    assert change.departure_date == date(2026, 9, 28)


# ------------------------------------------------------------------ the card (WebSocket) path


async def write(client: Any, entry_id: str, revision: int, settings_body: dict[str, Any]) -> dict[str, Any]:
    frame = await ws_call(client, update_settings_message(entry_id, revision, settings_body))
    assert frame["success"] is True
    return frame["result"]


async def test_the_card_sets_a_date_and_clears_it_with_null(hass: HomeAssistant, hass_ws_client) -> None:
    entry = await setup_charger(hass)
    client = await admin(hass, hass_ws_client)
    before = settings_of(hass, entry.entry_id)

    with freeze_time(TODAY):
        result = await write(client, entry.entry_id, before.revision, body_of(before, departure_date="2026-09-27"))
    assert result["ok"] is True and result["settings"]["departure_date"] == "2026-09-27"
    assert settings_of(hass, entry.entry_id).departure_date == date(2026, 9, 27)

    after = settings_of(hass, entry.entry_id)
    cleared = await write(client, entry.entry_id, after.revision, body_of(after, departure_date=None))
    assert cleared["ok"] is True and cleared["settings"]["departure_date"] is None


def body_of(settings: AutoSettings, **changes: Any) -> dict[str, Any]:
    """A replacement body in the Stockholm area, whose zone judges the date."""
    encoded = encode_settings(settings)
    return {key: value for key, value in encoded.items() if key != "revision"} | {"area_id": "SE4"} | changes


async def test_an_older_client_that_never_names_the_date_does_not_clear_it(
    hass: HomeAssistant, hass_ws_client
) -> None:
    entry = await setup_charger(hass)
    client = await admin(hass, hass_ws_client)
    before = settings_of(hass, entry.entry_id)
    with freeze_time(TODAY):
        await write(client, entry.entry_id, before.revision, body_of(before, departure_date="2026-09-27"))
    dated = settings_of(hass, entry.entry_id)

    legacy = body_of(dated, amps=16)
    del legacy["departure_date"]
    with freeze_time(TODAY):
        result = await write(client, entry.entry_id, dated.revision, legacy)

    assert result["ok"] is True and result["settings"]["amps"] == 16
    assert result["settings"]["departure_date"] == "2026-09-27"


@pytest.mark.parametrize(
    "value", ["2026-09-21", "2026-09-30", "2026-01-01"], ids=["yesterday", "eight days ahead", "far past"]
)
async def test_a_new_date_outside_today_to_seven_days_is_refused_and_nothing_is_written(
    hass: HomeAssistant, hass_ws_client, value: str
) -> None:
    entry = await setup_charger(hass)
    client = await admin(hass, hass_ws_client)
    before = settings_of(hass, entry.entry_id)

    with freeze_time(TODAY):
        result = await write(client, entry.entry_id, before.revision, body_of(before, departure_date=value))

    assert result["ok"] is False and result["error"] == "invalid_departure"
    assert result["settings"]["revision"] == before.revision
    assert settings_of(hass, entry.entry_id).departure_date is None


async def test_today_and_seven_days_ahead_are_inside_the_limit(hass: HomeAssistant, hass_ws_client) -> None:
    entry = await setup_charger(hass)
    client = await admin(hass, hass_ws_client)
    for value in ("2026-09-22", "2026-09-29"):
        before = settings_of(hass, entry.entry_id)
        with freeze_time(TODAY):
            result = await write(client, entry.entry_id, before.revision, body_of(before, departure_date=value))
        assert result["ok"] is True, value


async def test_a_date_that_has_gone_by_is_cleared_by_the_next_write_but_not_refused(
    hass: HomeAssistant, hass_ws_client
) -> None:
    entry = await setup_charger(hass)
    client = await admin(hass, hass_ws_client)
    before = settings_of(hass, entry.entry_id)
    with freeze_time(TODAY):
        await write(client, entry.entry_id, before.revision, body_of(before, departure_date="2026-09-23"))
    dated = settings_of(hass, entry.entry_id)

    # Two days later the stored date is past. A client that writes the record back as it read it
    # (the card, the app) is not refused for it: the write forgets it.
    with freeze_time("2026-09-25 06:00:00"):
        result = await write(client, entry.entry_id, dated.revision, body_of(dated, amps=16))
    assert result["ok"] is True and result["settings"]["amps"] == 16
    assert result["settings"]["departure_date"] is None

    # A *new* past date is refused, even the very same text once it has been cleared.
    cleared = settings_of(hass, entry.entry_id)
    with freeze_time("2026-09-25 06:00:00"):
        refused = await write(client, entry.entry_id, cleared.revision, body_of(cleared, departure_date="2026-09-23"))
    assert refused["ok"] is False and refused["error"] == "invalid_departure"


async def test_the_date_is_judged_in_the_areas_zone_not_utc(hass: HomeAssistant, hass_ws_client) -> None:
    """23:30 UTC on the 21st is already the 22nd in Stockholm: the 22nd is today there, the 21st is past."""
    entry = await setup_charger(hass)
    client = await admin(hass, hass_ws_client)
    store_before = settings_of(hass, entry.entry_id)
    with freeze_time("2026-09-21 23:30:00"):
        ok = await write(
            client, entry.entry_id, store_before.revision, body_of(store_before, area_id="SE4", departure_date="2026-09-22")
        )
        assert ok["ok"] is True
        current = settings_of(hass, entry.entry_id)
        refused = await write(
            client, entry.entry_id, current.revision, body_of(current, departure_date="2026-09-21")
        )
    assert refused["ok"] is False and refused["error"] == "invalid_departure"


# ----------------------------------------------------------------------------- the webhook path


async def test_the_app_writes_the_date_over_the_webhook(hass: HomeAssistant, hass_client_no_auth) -> None:
    entry = await setup_charger(hass)
    client = await hass_client_no_auth()
    before = settings_of(hass, entry.entry_id)
    payload = {
        "version": 1,
        "action": "settings",
        "expected_revision": before.revision,
        "settings": body_of(before, departure_date="2026-09-27"),
    }

    with freeze_time(TODAY):
        response = await client.post("/api/webhook/webhook-a", json=payload)
    answer = await response.json()

    assert response.status == 200 and answer["ok"] is True
    assert answer["settings"]["departure_date"] == "2026-09-27"
    assert settings_of(hass, entry.entry_id).departure_date == date(2026, 9, 27)

    refused_payload = {**payload, "expected_revision": answer["settings"]["revision"]}
    refused_payload["settings"] = body_of(settings_of(hass, entry.entry_id), departure_date="2026-09-01")
    with freeze_time(TODAY):
        refused = await client.post("/api/webhook/webhook-a", json=refused_payload)
    refusal = await refused.json()
    assert refusal["ok"] is False and refusal["error"] == "invalid_departure"
    assert refusal["settings"]["departure_date"] == "2026-09-27"


async def test_the_dashboards_settings_record_carries_the_date(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    from .helpers import webhook_dashboard

    entry = await setup_charger(hass)
    client = await hass_client_no_auth()
    before = settings_of(hass, entry.entry_id)
    with freeze_time(TODAY):
        await client.post(
            "/api/webhook/webhook-a",
            json={
                "version": 1,
                "action": "settings",
                "expected_revision": before.revision,
                "settings": body_of(before, departure_date="2026-09-27"),
            },
        )
    dashboard = await webhook_dashboard(client, "webhook-a")
    assert dashboard["settings"]["departure_date"] == "2026-09-27"


# ------------------------------------------------------------------------------- the entities


async def test_the_date_entity_sets_the_date_and_the_button_clears_it(hass: HomeAssistant) -> None:
    entry = await setup_charger(hass)
    field = entity_id(hass, entry.entry_id, "departure_date", "date")
    clear = entity_id(hass, entry.entry_id, "clear_departure_date", "button")
    assert hass.states.get(field).state == "unknown", "no date is the ordinary daily departure"
    assert hass.states.get(clear).state == "unavailable", "nothing to clear"

    with freeze_time(TODAY):
        await call(hass, "date", "set_value", {"entity_id": field, "date": date(2026, 9, 27)})
    assert settings_of(hass, entry.entry_id).departure_date == date(2026, 9, 27)
    assert hass.states.get(field).state == "2026-09-27"
    assert hass.states.get(clear).state != "unavailable"

    await call(hass, "button", "press", {"entity_id": clear})
    assert settings_of(hass, entry.entry_id).departure_date is None
    assert hass.states.get(field).state == "unknown"


async def test_the_date_entity_refuses_a_past_date_by_a_stable_translation_key(hass: HomeAssistant) -> None:
    entry = await setup_charger(hass)
    field = entity_id(hass, entry.entry_id, "departure_date", "date")

    with freeze_time(TODAY), pytest.raises(ServiceValidationError) as error:
        await call(hass, "date", "set_value", {"entity_id": field, "date": date(2026, 9, 1)})

    assert error.value.translation_key == "invalid_departure"
    assert settings_of(hass, entry.entry_id).departure_date is None


async def test_changing_the_time_with_a_date_set_keeps_the_date(hass: HomeAssistant) -> None:
    entry = await setup_charger(hass)
    field = entity_id(hass, entry.entry_id, "departure_date", "date")
    clock = entity_id(hass, entry.entry_id, "departure_time", "time")
    with freeze_time(TODAY):
        await call(hass, "date", "set_value", {"entity_id": field, "date": date(2026, 9, 27)})
        await call(hass, "time", "set_value", {"entity_id": clock, "time": time(7, 35)})
    stored = settings_of(hass, entry.entry_id)
    assert (stored.departure, stored.departure_date) == (time(7, 35), date(2026, 9, 27))
