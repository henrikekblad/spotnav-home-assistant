"""Weekdays on the daily departure: additive on the wire and in storage, honoured by planning.

* The record: written only when a weekday is left out; a record without the key is every day.
* The codec: a non-empty list of whole weekday numbers 1 (Monday) to 7 (Sunday); absent in a
  replacement body keeps what is stored; the webhook leaves it out of its settings record.
* Planning: on a day that is not in the set no departure applies and the plan runs to the next day
  that is, exactly as it does to a date the person chose.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

import pytest
from freezegun import freeze_time
from homeassistant.core import HomeAssistant

from custom_components.spotnav.api.settings import (
    OPTIONAL_SETTINGS_KEYS,
    decode_settings,
    departure_weekdays_from_wire,
    encode_settings,
    replacement_mutator,
)
from custom_components.spotnav.api.webhook import APP_UNREAD_SETTINGS
from custom_components.spotnav.planning.auto_settings import ALL_WEEKDAYS, AutoSettings, AutoSettingsError

from .harness import Harness
from .messages import read_settings_message, update_settings_message
from .relay import Clock, flat_day, index_listing, serve, serve_profile, SE4
from .world import admin, settings_of, setup_charger, ws_call

pytestmark = pytest.mark.usefixtures("offline_relay")

TODAY = "2026-09-22 06:00:00"
#: Thursday 2026-10-01, 20:00 in Stockholm (CEST, UTC+2).
THURSDAY = datetime(2026, 10, 1, 18, 0, tzinfo=timezone.utc)
MONDAY_DATE = date(2026, 10, 5)
MON, TUE, WED, THU, FRI, SAT, SUN = range(1, 8)


def body(**changes: Any) -> dict[str, Any]:
    encoded = encode_settings(AutoSettings(area_id="SE4", amps=10, phases=3))
    return {key: value for key, value in encoded.items() if key != "revision"} | changes


# ------------------------------------------------------------------------------- the record


def test_the_set_is_stored_only_when_a_weekday_is_left_out() -> None:
    every = AutoSettings(area_id="SE4", amps=10, phases=3)
    assert every.departure_weekdays == ALL_WEEKDAYS and "departure_weekdays" not in every.as_dict()
    assert AutoSettings.from_stored(every.as_dict()) == every.validated()

    workdays = replace(every, departure_weekdays=(FRI, MON, THU, TUE, WED))
    assert workdays.validated().departure_weekdays == (1, 2, 3, 4, 5), "kept in ascending order"
    assert workdays.validated().as_dict()["departure_weekdays"] == [1, 2, 3, 4, 5]
    assert AutoSettings.from_stored(workdays.validated().as_dict()) == workdays.validated()


@pytest.mark.parametrize("stored", [(), (0,), (8,), (1, 1), (True,), ("1",), (1.5,)])
def test_validation_refuses_what_is_not_a_set_of_weekdays(stored: Any) -> None:
    with pytest.raises(AutoSettingsError) as error:
        replace(AutoSettings(), departure_weekdays=stored).validated()
    assert error.value.code == "invalid_departure"


# ------------------------------------------------------------------------------- the codec


def test_the_wire_names_the_weekdays_and_marks_them_optional() -> None:
    assert "departure_weekdays" in OPTIONAL_SETTINGS_KEYS
    assert encode_settings(AutoSettings())["departure_weekdays"] == [1, 2, 3, 4, 5, 6, 7]
    assert decode_settings(body(departure_weekdays=[5, 1])).departure_weekdays == (1, 5)
    legacy = body()
    del legacy["departure_weekdays"]
    assert decode_settings(legacy).departure_weekdays == ALL_WEEKDAYS


@pytest.mark.parametrize("value", [[], [0], [8], [1, 1], [True], ["1"], [1.0], "12345", 5, None, {}])
def test_anything_else_is_refused_as_an_invalid_departure(value: Any) -> None:
    with pytest.raises(AutoSettingsError) as error:
        decode_settings(body(departure_weekdays=value))
    assert error.value.code == "invalid_departure"
    with pytest.raises(AutoSettingsError):
        departure_weekdays_from_wire(value)


def test_a_replacement_without_the_weekdays_keeps_the_stored_ones() -> None:
    current = replace(AutoSettings(area_id="SE4", amps=10, phases=3), departure_weekdays=(1, 2))
    legacy = body()
    del legacy["departure_weekdays"]
    kept = replacement_mutator(decode_settings(legacy), keep_departure_weekdays=True)(current)
    assert kept.departure_weekdays == (1, 2)
    changed = replacement_mutator(decode_settings(body(departure_weekdays=[6, 7])))(current)
    assert changed.departure_weekdays == (6, 7)


# ------------------------------------------------------------------------- the two transports


async def post_settings(client: Any, revision: int, settings_body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    response = await client.post(
        "/api/webhook/webhook-a",
        json={"version": 1, "action": "settings", "expected_revision": revision, "settings": settings_body},
    )
    return response.status, await response.json()


def test_the_app_does_not_read_the_weekdays_yet() -> None:
    assert "departure_weekdays" in APP_UNREAD_SETTINGS


async def test_the_card_reads_and_writes_the_set_and_the_webhook_leaves_it_out(
    hass: HomeAssistant, hass_client_no_auth, hass_ws_client
) -> None:
    from .helpers import webhook_dashboard

    entry = await setup_charger(hass)
    socket = await admin(hass, hass_ws_client)
    client = await hass_client_no_auth()
    before = settings_of(hass, entry.entry_id)

    with freeze_time(TODAY):
        written = await ws_call(
            socket,
            update_settings_message(
                entry.entry_id, before.revision, body(departure_weekdays=[1, 2, 3, 4, 5], area_id="SE4")
            ),
        )
    assert written["result"]["ok"] is True
    assert written["result"]["settings"]["departure_weekdays"] == [1, 2, 3, 4, 5]
    stored = settings_of(hass, entry.entry_id)
    assert stored.departure_weekdays == (1, 2, 3, 4, 5)

    with freeze_time(TODAY):
        dashboard = await webhook_dashboard(client, "webhook-a")
        # An app that has never heard of the field writes the record back without it: kept.
        app_body = body(amps=16, area_id="SE4")
        del app_body["departure_weekdays"]
        status, answer = await post_settings(client, stored.revision, app_body)
        read = await ws_call(socket, read_settings_message(entry.entry_id))

    assert "departure_weekdays" not in dashboard["settings"]
    assert status == 200 and answer["ok"] is True and "departure_weekdays" not in answer["settings"]
    assert settings_of(hass, entry.entry_id).departure_weekdays == (1, 2, 3, 4, 5)
    assert read["result"]["settings"]["departure_weekdays"] == [1, 2, 3, 4, 5]


async def test_the_card_refuses_an_empty_set_and_writes_nothing(hass: HomeAssistant, hass_ws_client) -> None:
    entry = await setup_charger(hass)
    socket = await admin(hass, hass_ws_client)
    before = settings_of(hass, entry.entry_id)
    with freeze_time(TODAY):
        refused = await ws_call(
            socket, update_settings_message(entry.entry_id, before.revision, body(departure_weekdays=[], area_id="SE4"))
        )
    assert refused["result"]["ok"] is False and refused["result"]["error"] == "invalid_departure"
    assert settings_of(hass, entry.entry_id).departure_weekdays == ALL_WEEKDAYS


# ------------------------------------------------------------------------------------ planning


def _publish(harness: Harness) -> None:
    """Thursday and Friday published at a flat price, with a flat history."""
    transport = harness.transport
    today = date(2026, 10, 1)
    days = (today, today + timedelta(days=1))
    serve(transport, days=days)
    for day in days:
        transport.serve(transport.day_path(SE4, day), 200, flat_day(SE4, day, price=0.10))
    transport.serve("/v1/index.json", 200, index_listing(SE4, [day.isoformat() for day in days]))
    serve_profile(transport, to=today.isoformat(), generated=f"{today.isoformat()}T14:05:00+02:00", cheap={})


@pytest.fixture(autouse=True)
def thursday_evening(clock: Clock) -> None:
    clock.now = THURSDAY


async def test_a_set_that_holds_the_next_day_changes_nothing(harness: Harness) -> None:
    """Thursday evening, departure 08:00: Friday is in the set, so it is the ordinary daily departure."""
    _publish(harness)
    every = await harness.auto(departure=time(8, 0), requested_kwh=10.0)
    every_plan = every.snapshot().proposal
    assert every_plan is not None and every_plan.has_plan

    harness_two = harness
    restricted = await harness_two.auto("entry-b", departure=time(8, 0), requested_kwh=10.0, departure_weekdays=(FRI, MON))
    assert restricted.snapshot().proposal.periods == every_plan.periods


async def test_a_departure_on_a_day_outside_the_set_plans_to_the_next_day_in_it(harness: Harness) -> None:
    """Friday and the weekend are out, so Thursday evening's departure is Monday 08:00: the plan is the one a
    departure dated that Monday makes."""
    _publish(harness)
    by_weekday = await harness.auto(departure=time(8, 0), requested_kwh=10.0, departure_weekdays=(MON, TUE))
    by_date = await harness.auto("entry-b", departure=time(8, 0), requested_kwh=10.0, departure_date=MONDAY_DATE)

    weekday_snapshot, dated_snapshot = by_weekday.snapshot(), by_date.snapshot()
    assert dated_snapshot.proposal is not None and dated_snapshot.proposal.has_plan
    assert weekday_snapshot.state == dated_snapshot.state and weekday_snapshot.reason == dated_snapshot.reason
    assert weekday_snapshot.proposal.periods == dated_snapshot.proposal.periods
    assert weekday_snapshot.history is not None, "the dated rules (history, waiting for prices) apply"

    daily = await harness.auto("entry-c", departure=time(8, 0), requested_kwh=10.0)
    assert daily.snapshot().history is None, "the ordinary daily departure takes no history"


async def test_a_date_the_person_chose_wins_over_the_set(harness: Harness) -> None:
    _publish(harness)
    chosen = await harness.auto(
        departure=time(8, 0), requested_kwh=10.0, departure_weekdays=(MON,), departure_date=date(2026, 10, 3)
    )
    plain = await harness.auto("entry-b", departure=time(8, 0), requested_kwh=10.0, departure_date=date(2026, 10, 3))
    assert chosen.snapshot().proposal.periods == plain.snapshot().proposal.periods
