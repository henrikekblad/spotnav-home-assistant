"""Public holidays in the high-load windows (`HighLoadWindow.holidays`) and where they come from (`site/holidays.py`).

Göteborg Energi's tidsindelad tariff counts weekdays 07-20, November to March, without public holidays, and counts
Christmas Eve as one. 2026-12-24 is a Thursday, 2027-01-06 (Epiphany) a Wednesday. Stockholm's clock (CET).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.spotnav.planning.auto_settings import DemandComponent
from custom_components.spotnav.planning.demand_charge import component_month, counted_day, HourMean
from custom_components.spotnav.planning.planner import effective_minor_per_kwh, FiscalChoice, HighLoadWindow
from custom_components.spotnav.site.holidays import (
    calendar_holidays,
    HolidaySource,
    SOURCE_CALENDAR,
    SOURCE_NONE,
    SOURCE_WORKDAY,
    span,
    work_weekdays,
    workday_answer,
    workday_holidays,
)

TZ = "Europe/Stockholm"
STOCKHOLM = ZoneInfo(TZ)
UTC = timezone.utc
CHRISTMAS_EVE = date(2026, 12, 24)
EPIPHANY = date(2027, 1, 6)
HOLIDAYS = frozenset({CHRISTMAS_EVE, date(2026, 12, 25), EPIPHANY})
#: Göteborg Energi tidsindelad: weekdays 07-20, November to March.
GBG = HighLoadWindow(days="weekdays", from_month=11, to_month=3, from_hour=7, to_hour=20, tz=TZ)
NIGHT = HighLoadWindow(days="weekdays", from_month=1, to_month=12, from_hour=22, to_hour=6, tz=TZ)


def _local(*args: int) -> datetime:
    return datetime(*args, tzinfo=STOCKHOLM)


# ------------------------------------------------------------------------------------------------ the window


@pytest.mark.parametrize("holiday", [CHRISTMAS_EVE, EPIPHANY])
def test_a_holiday_on_a_weekday_is_not_in_a_weekday_window(holiday: date) -> None:
    window = HighLoadWindow(**{**_fields(GBG), "holidays": HOLIDAYS})
    noon = _local(holiday.year, holiday.month, holiday.day, 12)
    assert window.window_day(noon) is None
    assert not window.contains(noon)
    # The day before is a weekday that counts, and without holidays the holiday counts as its weekday.
    assert window.window_day(noon - timedelta(days=1)) == holiday - timedelta(days=1)
    assert GBG.window_day(noon) == holiday


def test_a_mon_sat_window_leaves_a_holiday_out_and_an_all_days_window_keeps_it() -> None:
    noon = _local(2026, 12, 24, 12)
    for days, expected in (("mon_sat", None), ("all", CHRISTMAS_EVE)):
        window = HighLoadWindow(days=days, from_month=11, to_month=3, from_hour=7, to_hour=20, tz=TZ, holidays=HOLIDAYS)
        assert window.window_day(noon) == expected


def test_a_night_window_belongs_to_the_day_it_starts_on_with_holidays() -> None:
    window = HighLoadWindow(**{**_fields(NIGHT), "holidays": HOLIDAYS})
    # Wednesday night into Christmas Eve: started on a weekday, so its morning hours count under the 23rd.
    assert window.window_day(_local(2026, 12, 23, 23)) == date(2026, 12, 23)
    assert window.window_day(_local(2026, 12, 24, 3)) == date(2026, 12, 23)
    # Christmas Eve's own night (into Christmas Day) is left out, morning hours too.
    assert window.window_day(_local(2026, 12, 24, 22)) is None
    assert window.window_day(_local(2026, 12, 25, 3)) is None
    # Without holidays nothing changed.
    assert NIGHT.window_day(_local(2026, 12, 24, 3)) == date(2026, 12, 23)
    assert NIGHT.window_day(_local(2026, 12, 24, 22)) == CHRISTMAS_EVE
    assert NIGHT.window_day(_local(2026, 12, 25, 3)) == CHRISTMAS_EVE
    assert NIGHT.window_day(_local(2026, 12, 25, 12)) is None


def test_the_time_of_use_fee_charges_the_low_rate_on_a_holiday() -> None:
    fiscal = FiscalChoice(
        transfer_enabled=True,
        transfer_minor_per_kwh=10.0,
        transfer_high_enabled=True,
        transfer_high_minor_per_kwh=50.0,
        high_load_window=HighLoadWindow(**{**_fields(GBG), "holidays": HOLIDAYS}),
    ).validated()
    assert effective_minor_per_kwh(1.0, fiscal, _local(2026, 12, 24, 12)) == pytest.approx(100 + 10)
    assert effective_minor_per_kwh(1.0, fiscal, _local(2026, 12, 23, 12)) == pytest.approx(100 + 50)


def test_the_validated_window_keeps_its_holidays() -> None:
    window = HighLoadWindow(**{**_fields(GBG), "holidays": HOLIDAYS})
    assert window.validated().holidays == HOLIDAYS


def test_a_demand_charge_window_part_does_not_count_a_holiday_hour() -> None:
    part = DemandComponent(top_n=3, window=HighLoadWindow(**{**_fields(GBG), "tz": None, "holidays": HOLIDAYS}))
    plain = DemandComponent(top_n=3, window=HighLoadWindow(**{**_fields(GBG), "tz": None}))
    eve = _local(2026, 12, 24, 17).astimezone(UTC)
    assert counted_day(part, eve, TZ) is None
    assert counted_day(plain, eve, TZ) == CHRISTMAS_EVE
    hours = [
        HourMean(start=_local(2026, 12, 22, 17).astimezone(UTC), kw=3.0),
        HourMean(start=_local(2026, 12, 23, 17).astimezone(UTC), kw=4.0),
        HourMean(start=eve, kw=9.0),
    ]
    now = _local(2026, 12, 28, 12).astimezone(UTC)
    assert component_month(part, hours, TZ, now).bar_kw == pytest.approx(3.5)
    assert component_month(plain, hours, TZ, now).bar_kw == pytest.approx((3 + 4 + 9) / 3)


def _fields(window: HighLoadWindow) -> dict[str, Any]:
    return {name: getattr(window, name) for name in ("days", "from_month", "to_month", "from_hour", "to_hour", "tz")}


# ------------------------------------------------------------------------------------------------ the source, pure


def test_workday_holidays_are_its_work_days_it_calls_no_workday() -> None:
    answers = {
        date(2026, 12, 23): True,
        CHRISTMAS_EVE: False,
        date(2026, 12, 25): False,
        date(2026, 12, 26): False,  # a Saturday: not a work day, so the window's own days decide
    }
    assert workday_holidays(answers, work_weekdays(["mon", "tue", "wed", "thu", "fri"])) == {
        CHRISTMAS_EVE,
        date(2026, 12, 25),
    }
    assert work_weekdays(None) == frozenset({1, 2, 3, 4, 5})
    assert work_weekdays(["mon", "sat", "holiday"]) == frozenset({1, 6})
    assert workday_answer({"binary_sensor.workday": {"workday": False}}, "binary_sensor.workday") is False
    assert workday_answer({"binary_sensor.other": {"workday": False}}, "binary_sensor.workday") is None


def test_the_calendar_fallback_adds_christmas_and_new_years_eve_for_sweden_only() -> None:
    first, last = span(date(2026, 12, 1))
    listed = {date(2026, 12, 25), date(2026, 12, 26)}
    assert calendar_holidays(listed, "SE", first, last) == {CHRISTMAS_EVE, date(2026, 12, 31), *listed}
    assert calendar_holidays(listed, "DK", first, last) == listed
    # Outside the span read, nothing is added.
    first, last = span(date(2026, 6, 1))
    assert calendar_holidays((), "SE", first, last) == frozenset()


# ------------------------------------------------------------------------------------------------ the source, HA


def _workday(hass: HomeAssistant, holidays: set[date], calls: list[date]) -> None:
    config = MockConfigEntry(domain="workday", options={"country": "SE"})
    config.add_to_hass(hass)
    er.async_get(hass).async_get_or_create(
        "binary_sensor", "workday", "se_workday", suggested_object_id="workday_sensor", config_entry=config
    )
    hass.states.async_set("binary_sensor.workday_sensor", "on", {"workdays": ["mon", "tue", "wed", "thu", "fri"]})

    async def check_date(call: ServiceCall) -> dict[str, Any]:
        day = call.data["check_date"]
        calls.append(day)
        return {"binary_sensor.workday_sensor": {"workday": day.isoweekday() <= 5 and day not in holidays}}

    hass.services.async_register("workday", "check_date", check_date, supports_response=SupportsResponse.ONLY)


def _calendar(hass: HomeAssistant, days: set[date]) -> None:
    config = MockConfigEntry(domain="holiday", data={"country": "SE"})
    config.add_to_hass(hass)
    er.async_get(hass).async_get_or_create(
        "calendar", "holiday", "se_holidays", suggested_object_id="sverige", config_entry=config
    )

    async def get_events(call: ServiceCall) -> dict[str, Any]:
        return {"calendar.sverige": {"events": [{"start": d.isoformat(), "end": d.isoformat()} for d in days]}}

    hass.services.async_register("calendar", "get_events", get_events, supports_response=SupportsResponse.ONLY)


def _source(hass: HomeAssistant) -> HolidaySource:
    return HolidaySource(hass, now=lambda: datetime(2026, 12, 10, 12, tzinfo=STOCKHOLM))


async def test_a_workday_sensor_is_the_source_first(hass: HomeAssistant) -> None:
    calls: list[date] = []
    # Workday configured with the de facto holidays: Christmas Eve is no workday.
    _workday(hass, {CHRISTMAS_EVE, date(2026, 12, 25), EPIPHANY}, calls)
    _calendar(hass, {date(2026, 12, 25)})
    source = _source(hass)
    await source.async_refresh()
    assert (source.source, source.entity_id) == (SOURCE_WORKDAY, "binary_sensor.workday_sensor")
    assert source.dates == {CHRISTMAS_EVE, date(2026, 12, 25), EPIPHANY}
    # Only its work days are asked about, once a day.
    assert calls and all(day.isoweekday() <= 5 for day in calls)
    asked = len(calls)
    await source.async_refresh()
    assert len(calls) == asked
    window = HighLoadWindow(**{**_fields(GBG), "holidays": source.dates})
    assert window.window_day(_local(2026, 12, 24, 12)) is None
    assert window.window_day(_local(2027, 1, 6, 12)) is None


async def test_the_holiday_calendar_is_the_fallback_with_christmas_and_new_years_eve(hass: HomeAssistant) -> None:
    _calendar(hass, {date(2026, 12, 25), date(2026, 12, 26), EPIPHANY})
    source = _source(hass)
    await source.async_refresh()
    assert (source.source, source.entity_id) == (SOURCE_CALENDAR, "calendar.sverige")
    assert source.dates == {CHRISTMAS_EVE, date(2026, 12, 25), date(2026, 12, 26), date(2026, 12, 31), EPIPHANY}
    window = HighLoadWindow(**{**_fields(GBG), "holidays": source.dates})
    assert window.window_day(_local(2026, 12, 24, 12)) is None
    assert window.window_day(_local(2027, 1, 6, 12)) is None
    assert window.window_day(_local(2026, 12, 23, 12)) == date(2026, 12, 23)


async def test_without_a_source_nothing_changes(hass: HomeAssistant) -> None:
    source = _source(hass)
    await source.async_refresh()
    assert (source.source, source.dates) == (SOURCE_NONE, frozenset())
    window = HighLoadWindow(**{**_fields(GBG), "holidays": source.dates})
    assert window.window_day(_local(2026, 12, 24, 12)) == CHRISTMAS_EVE
    assert source.diagnostics() == {"source": SOURCE_NONE, "entity_id": None, "dates": []}


async def test_a_failed_read_keeps_the_last_dates_and_is_tried_again(hass: HomeAssistant) -> None:
    _calendar(hass, {date(2026, 12, 25)})
    source = _source(hass)
    await source.async_refresh()
    kept = source.dates

    async def broken(call: ServiceCall) -> dict[str, Any]:
        raise ValueError("down")

    hass.services.async_register("calendar", "get_events", broken, supports_response=SupportsResponse.ONLY)
    source._read_day = None  # the next day
    await source.async_refresh()
    assert source.dates == kept
    assert source._read_day is None


# ------------------------------------------------------------------------------------------------ the wiring


@pytest.mark.usefixtures("offline_relay")
async def test_the_sites_window_parts_and_a_price_source_carry_the_holidays(hass: HomeAssistant, freezer) -> None:
    from custom_components.spotnav.pricing.price_source import site_demand_components, site_price_source
    from custom_components.spotnav.runtime import domain_data

    from .test_demand_site import pilot_site

    entry = await pilot_site(hass, freezer)
    source = domain_data(hass).holidays
    assert source is not None
    source.dates = HOLIDAYS
    parts = site_demand_components(hass, entry.entry_id)
    assert parts and all(part.window is not None and part.window.holidays == HOLIDAYS for part in parts)
    price = site_price_source(hass, entry.entry_id)
    assert price is not None and price.holidays == HOLIDAYS
