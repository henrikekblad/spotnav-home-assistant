"""The departure calendar at a charger: the record, the wire, the plan, the dashboard and the app.

* The record and the wire: `departure_calendar` is written only once chosen; `null` or a calendar entity id; a
  replacement without it keeps it; the webhook leaves it (and the dashboard block) out for an app that does not read it.
* Planning (`AutoPlannerController`): an event's departure plans exactly as a departure dated that day and time; a date
  the person chose wins; a guest car and the departure switch off read no calendar; a calendar that cannot be read
  leaves the daily departure; an event's target is used where it fits the driver.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Any

import pytest
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse

from custom_components.spotnav.api.dashboard import departure_calendar_block
from custom_components.spotnav.api.settings import (
    decode_settings,
    encode_settings,
    replacement_mutator,
)
from custom_components.spotnav.api.webhook import APP_DASHBOARD_BLOCK_READS, APP_UNREAD_SETTINGS, _for_app
from custom_components.spotnav.const import GUEST_VEHICLE_ID
from custom_components.spotnav.planning.auto_settings import (
    DRIVER_MANUAL_KWH,
    AutoSettings,
    AutoSettingsError,
    TargetSocIntent,
)
from custom_components.spotnav.planning.calendar_reader import KEEP_LAST_S, REFRESH_S

from .harness import Harness
from .relay import Clock, flat_day, hold_history, index_listing, serve, SE4

pytestmark = pytest.mark.usefixtures("offline_relay")

CALENDAR = "calendar.trips"
#: Thursday 2026-10-01, 20:00 in Stockholm (CEST, UTC+2).
THURSDAY = datetime(2026, 10, 1, 18, 0, tzinfo=timezone.utc)
FRIDAY = date(2026, 10, 2)
SATURDAY = date(2026, 10, 3)
MON = 1


def body(**changes: Any) -> dict[str, Any]:
    encoded = encode_settings(AutoSettings(area_id="SE4", amps=10, phases=3))
    return {key: value for key, value in encoded.items() if key != "revision"} | changes


# ------------------------------------------------------------------------------- the record and the wire


def test_the_calendar_is_stored_only_once_chosen() -> None:
    assert "departure_calendar" not in AutoSettings().as_dict()
    chosen = AutoSettings(departure_calendar=CALENDAR).validated()
    assert chosen.as_dict()["departure_calendar"] == CALENDAR
    assert AutoSettings.from_stored(chosen.as_dict()).departure_calendar == CALENDAR


@pytest.mark.parametrize("value", ["sensor.trips", "calendar.", "", 3, " calendar.trips", "calendar.a b"])
def test_what_is_not_a_calendar_is_refused(value: Any) -> None:
    with pytest.raises(AutoSettingsError) as caught:
        decode_settings(body(departure_calendar=value))
    assert caught.value.code == "invalid_departure"


def test_the_wire_reads_null_and_a_calendar() -> None:
    assert decode_settings(body(departure_calendar=None)).departure_calendar is None
    assert decode_settings(body(departure_calendar=CALENDAR)).departure_calendar == CALENDAR
    assert encode_settings(AutoSettings(departure_calendar=CALENDAR))["departure_calendar"] == CALENDAR


def test_a_replacement_without_the_calendar_keeps_it_and_null_clears_it() -> None:
    current = AutoSettings(area_id="SE4", amps=10, departure_calendar=CALENDAR).validated()
    left_out = {key: value for key, value in body().items() if key != "departure_calendar"}
    kept = replacement_mutator(decode_settings(left_out), keep_departure_calendar=True)(current)
    assert kept.departure_calendar == CALENDAR
    cleared = replacement_mutator(decode_settings(body(departure_calendar=None)))(current)
    assert cleared.departure_calendar is None


def test_calendar_only_is_stored_only_when_set_and_kept_when_left_out() -> None:
    assert "departure_calendar_only" not in AutoSettings().as_dict()
    chosen = AutoSettings(departure_calendar=CALENDAR, departure_calendar_only=True).validated()
    assert chosen.as_dict()["departure_calendar_only"] is True
    assert AutoSettings.from_stored(chosen.as_dict()).departure_calendar_only is True
    assert decode_settings(body(departure_calendar_only=True)).departure_calendar_only is True
    assert encode_settings(AutoSettings())["departure_calendar_only"] is False
    with pytest.raises(AutoSettingsError):
        decode_settings(body(departure_calendar_only="yes"))
    left_out = {key: value for key, value in body().items() if key != "departure_calendar_only"}
    kept = replacement_mutator(decode_settings(left_out), keep_departure_calendar_only=True)(chosen)
    assert kept.departure_calendar_only is True
    assert "departure_calendar_only" in APP_UNREAD_SETTINGS


def test_the_app_reads_neither_the_field_nor_the_block_until_it_asks() -> None:
    assert "departure_calendar" in APP_UNREAD_SETTINGS
    assert APP_DASHBOARD_BLOCK_READS["departure_calendar"] == "departure_calendar"
    dashboard = {"settings": {"departure_calendar": CALENDAR, "amps": 10}, "departure_calendar": {"calendars": []}}
    older = _for_app(dashboard, {})
    assert "departure_calendar" not in older and "departure_calendar" not in older["settings"]
    newer = _for_app(dashboard, {"reads": ["departure_calendar"]})
    assert newer["departure_calendar"] == {"calendars": []}
    assert newer["settings"]["departure_calendar"] == CALENDAR


# ------------------------------------------------------------------------------- planning


class Calendar:
    """`calendar.get_events` for one calendar, answering what the test puts in `events`."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.events: list[dict[str, Any]] = []
        self.calls = 0
        self.fail = False
        hass.states.async_set(CALENDAR, "off", {"friendly_name": "Trips"})
        hass.services.async_register(
            "calendar", "get_events", self._answer, supports_response=SupportsResponse.ONLY
        )

    async def _answer(self, call: ServiceCall) -> dict[str, Any]:
        self.calls += 1
        if self.fail:
            from homeassistant.exceptions import HomeAssistantError

            raise HomeAssistantError("the calendar's server did not answer")
        asked = call.data["entity_id"]
        ids = [asked] if isinstance(asked, str) else list(asked)
        return {entity_id: {"events": list(self.events)} for entity_id in ids}

    def add(self, start: str, summary: str = "Trip", description: str = "") -> None:
        self.events.append({"start": start, "end": start, "summary": summary, "description": description})


@pytest.fixture
def calendar(hass: HomeAssistant) -> Calendar:
    return Calendar(hass)


def _publish(harness: Harness) -> None:
    """Thursday and Friday published at a flat price, with a flat history."""
    transport = harness.transport
    today = date(2026, 10, 1)
    days = (today, today + timedelta(days=1))
    serve(transport, days=days)
    for day in days:
        transport.serve(transport.day_path(SE4, day), 200, flat_day(SE4, day, price=0.10))
    transport.serve("/v1/index.json", 200, index_listing(SE4, [day.isoformat() for day in days]))
    hold_history(harness.repository, to=today.isoformat(), cheap={})


@pytest.fixture(autouse=True)
def thursday_evening(clock: Clock) -> None:
    clock.now = THURSDAY


def _departure(controller: Any) -> datetime | None:
    return controller.snapshot().departure_at


async def test_an_event_plans_as_a_departure_dated_its_day_and_time(harness: Harness, calendar: Calendar) -> None:
    """Friday's event at 10:00 replaces Friday's daily 08:00: the same plan as a departure dated Friday 10:00."""
    _publish(harness)
    calendar.add("2026-10-02T10:00:00+02:00")
    by_calendar = await harness.auto(departure=time(8, 0), requested_kwh=10.0, departure_calendar=CALENDAR)
    by_date = await harness.auto("entry-b", departure=time(10, 0), requested_kwh=10.0, departure_date=FRIDAY)

    assert _departure(by_calendar) == _departure(by_date) == datetime(2026, 10, 2, 8, 0, tzinfo=timezone.utc)
    assert by_calendar.snapshot().proposal.periods == by_date.snapshot().proposal.periods
    status = by_calendar.calendar_status()
    assert status is not None and status["state"] == "used" and status["summary"] == "Trip"
    assert status["departure"] == "2026-10-02T08:00:00+00:00"
    # The stored record is the person's own, never the calendar's.
    assert harness.store.settings("entry-a").departure == time(8, 0)
    assert harness.store.settings("entry-a").departure_date is None


async def test_without_an_event_the_daily_departure_stands(harness: Harness, calendar: Calendar) -> None:
    _publish(harness)
    with_calendar = await harness.auto(departure=time(8, 0), requested_kwh=10.0, departure_calendar=CALENDAR)
    without = await harness.auto("entry-b", departure=time(8, 0), requested_kwh=10.0)
    assert _departure(with_calendar) == _departure(without)
    assert with_calendar.snapshot().proposal.periods == without.snapshot().proposal.periods
    assert with_calendar.calendar_status()["state"] == "daily"


async def test_a_date_the_person_chose_wins_over_the_calendar(harness: Harness, calendar: Calendar) -> None:
    _publish(harness)
    calendar.add("2026-10-02T10:00:00+02:00")
    chosen = await harness.auto(
        departure=time(8, 0), requested_kwh=10.0, departure_date=SATURDAY, departure_calendar=CALENDAR
    )
    assert _departure(chosen) == datetime(2026, 10, 3, 6, 0, tzinfo=timezone.utc)
    assert chosen.calendar_status()["state"] == "date"


async def test_a_calendar_that_cannot_be_read_leaves_the_daily_departure(harness: Harness, calendar: Calendar) -> None:
    _publish(harness)
    calendar.add("2026-10-02T10:00:00+02:00")
    harness.hass.states.async_set(CALENDAR, "unavailable")
    unread = await harness.auto(departure=time(8, 0), requested_kwh=10.0, departure_calendar=CALENDAR)
    assert _departure(unread) == datetime(2026, 10, 2, 6, 0, tzinfo=timezone.utc)
    assert unread.calendar_status()["state"] == "unavailable"
    assert calendar.calls == 0, "an unavailable calendar is not asked"


async def test_a_calendar_that_stops_answering_keeps_its_last_answer_for_a_while(
    harness: Harness, calendar: Calendar, clock: Clock
) -> None:
    _publish(harness)
    calendar.add("2026-10-02T10:00:00+02:00")
    controller = await harness.auto(departure=time(8, 0), requested_kwh=10.0, departure_calendar=CALENDAR)
    assert controller.calendar_status()["state"] == "used"
    calendar.fail = True
    clock.now = THURSDAY + timedelta(seconds=REFRESH_S)
    await controller.async_recalculate()
    assert controller.calendar_status()["state"] == "used", "one missed answer keeps the last one"
    clock.now = THURSDAY + timedelta(seconds=KEEP_LAST_S + REFRESH_S)
    await controller.async_recalculate()
    assert controller.calendar_status()["state"] == "unavailable"


async def test_the_calendar_is_read_at_most_once_per_refresh(harness: Harness, calendar: Calendar) -> None:
    _publish(harness)
    controller = await harness.auto(departure=time(8, 0), requested_kwh=10.0, departure_calendar=CALENDAR)
    calls = calendar.calls
    await controller.async_recalculate()
    await controller.async_recalculate()
    assert calendar.calls == calls


async def test_a_guest_car_and_no_departure_read_no_calendar(harness: Harness, calendar: Calendar) -> None:
    _publish(harness)
    calendar.add("2026-10-02T10:00:00+02:00")
    guest = TargetSocIntent(vehicle_id=GUEST_VEHICLE_ID, target_percent=None)
    as_guest = await harness.auto(
        departure=time(8, 0), requested_kwh=10.0, driver=DRIVER_MANUAL_KWH, target=guest, departure_calendar=CALENDAR
    )
    assert _departure(as_guest) == datetime(2026, 10, 2, 6, 0, tzinfo=timezone.utc)
    assert as_guest.calendar_status()["state"] == "off"
    no_departure = await harness.auto("entry-b", requested_kwh=10.0, departure_calendar=CALENDAR)
    assert _departure(no_departure) is None
    assert no_departure.calendar_status()["state"] == "off"
    assert calendar.calls == 0


async def test_an_events_amount_is_the_plans_for_a_charger_planning_an_amount(
    harness: Harness, calendar: Calendar
) -> None:
    _publish(harness)
    calendar.add("2026-10-02T10:00:00+02:00", summary="Malmö", description="6 kWh")
    controller = await harness.auto(departure=time(8, 0), requested_kwh=10.0, departure_calendar=CALENDAR)
    proposal = controller.snapshot().proposal
    assert proposal is not None and proposal.requested_kwh == pytest.approx(6.0)
    status = controller.calendar_status()
    assert status["target_kwh"] == 6.0 and status["target_used"] == "kwh"
    assert harness.store.settings("entry-a").requested_kwh == 10.0


async def test_an_events_level_is_not_an_amount(harness: Harness, calendar: Calendar) -> None:
    """A level in the event at a charger that plans an amount is read, said, and not used."""
    _publish(harness)
    calendar.add("2026-10-02T10:00:00+02:00", summary="Malmö 90%")
    controller = await harness.auto(departure=time(8, 0), requested_kwh=10.0, departure_calendar=CALENDAR)
    assert controller.snapshot().proposal.requested_kwh == pytest.approx(10.0)
    status = controller.calendar_status()
    assert status["target_percent"] == 90.0 and status["target_used"] is None


async def test_a_new_event_is_planned_for_at_the_next_read(
    harness: Harness, calendar: Calendar, clock: Clock
) -> None:
    _publish(harness)
    controller = await harness.auto(departure=time(8, 0), requested_kwh=10.0, departure_calendar=CALENDAR)
    assert controller.calendar_status()["state"] == "daily"
    calendar.add("2026-10-02T10:00:00+02:00")
    clock.now = THURSDAY + timedelta(seconds=REFRESH_S)
    await controller._async_calendar_due(controller._generation)
    await harness.hass.async_block_till_done()
    assert controller.calendar_status()["state"] == "used"
    assert _departure(controller) == datetime(2026, 10, 2, 8, 0, tzinfo=timezone.utc)


async def test_an_event_on_a_day_outside_the_weekdays_is_the_departure(harness: Harness, calendar: Calendar) -> None:
    """Only Monday is a daily departure; Saturday's all-day event is a departure at the daily time."""
    _publish(harness)
    calendar.events.append({"start": "2026-10-03", "end": "2026-10-04", "summary": "Göteborg"})
    controller = await harness.auto(
        departure=time(8, 0), requested_kwh=10.0, departure_weekdays=(MON,), departure_calendar=CALENDAR
    )
    assert _departure(controller) == datetime(2026, 10, 3, 6, 0, tzinfo=timezone.utc)
    assert controller.calendar_status()["all_day"] is True


# ------------------------------------------------------------------------------- the calendar alone decides


async def test_alone_the_calendar_skips_a_day_without_an_event(harness: Harness, calendar: Calendar) -> None:
    """Friday has no event: no daily departure Friday; Saturday's event is the plan's."""
    _publish(harness)
    calendar.add("2026-10-03T09:00:00+02:00")
    only = await harness.auto(
        departure=time(8, 0), requested_kwh=10.0, departure_calendar=CALENDAR, departure_calendar_only=True
    )
    assert _departure(only) == datetime(2026, 10, 3, 7, 0, tzinfo=timezone.utc)
    assert only.calendar_status()["state"] == "used"


async def test_alone_without_an_event_ahead_there_is_no_departure(harness: Harness, calendar: Calendar) -> None:
    _publish(harness)
    only = await harness.auto(
        departure=time(8, 0), requested_kwh=10.0, departure_calendar=CALENDAR, departure_calendar_only=True
    )
    assert _departure(only) is None
    assert only.calendar_status()["state"] == "no_event"
    # Stored as chosen: only the plan goes without a departure.
    assert harness.store.settings("entry-a").departure_enabled is True


async def test_alone_a_chosen_date_still_wins(harness: Harness, calendar: Calendar) -> None:
    _publish(harness)
    chosen = await harness.auto(
        departure=time(8, 0),
        requested_kwh=10.0,
        departure_date=SATURDAY,
        departure_calendar=CALENDAR,
        departure_calendar_only=True,
    )
    assert _departure(chosen) == datetime(2026, 10, 3, 6, 0, tzinfo=timezone.utc)
    assert chosen.calendar_status()["state"] == "date"


async def test_alone_a_calendar_that_cannot_be_read_falls_back_to_the_daily_departure(
    harness: Harness, calendar: Calendar
) -> None:
    _publish(harness)
    harness.hass.states.async_set(CALENDAR, "unavailable")
    unread = await harness.auto(
        departure=time(8, 0), requested_kwh=10.0, departure_calendar=CALENDAR, departure_calendar_only=True
    )
    assert _departure(unread) == datetime(2026, 10, 2, 6, 0, tzinfo=timezone.utc)
    assert unread.calendar_status()["state"] == "unavailable"


# ------------------------------------------------------------------------------- the dashboard block


async def test_the_block_lists_the_calendars_and_what_the_plan_made_of_the_chosen_one(
    harness: Harness, calendar: Calendar
) -> None:
    _publish(harness)
    harness.hass.states.async_set("calendar.holidays", "off", {"friendly_name": "Holidays"})
    calendar.add("2026-10-02T10:00:00+02:00", description="mål: 80")
    await harness.auto(departure=time(8, 0), requested_kwh=10.0, departure_calendar=CALENDAR)
    block = departure_calendar_block(harness.hass, "entry-a", harness.store.settings("entry-a"))
    assert block is not None
    assert block["calendars"] == [
        {"entity_id": "calendar.holidays", "name": "Holidays"},
        {"entity_id": CALENDAR, "name": "Trips"},
    ]
    assert block["entity_id"] == CALENDAR and block["state"] == "used"
    assert block["target_percent"] == 80.0 and block["target_used"] is None
    assert set(block) == {
        "calendars", "entity_id", "state", "departure", "summary", "all_day", "target_percent", "target_kwh",
        "target_used",
    }


def test_no_block_without_a_calendar(hass: HomeAssistant) -> None:
    assert departure_calendar_block(hass, "entry-a", AutoSettings()) is None
