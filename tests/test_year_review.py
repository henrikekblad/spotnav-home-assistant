"""The year's review worked out (`year_review/compute.py`): the year rule, every page's figures and omission rules on
six site shapes, and the contract fixtures the card reads (`tests/fixtures/year_review/v1/`,
`SPOTNAV_WRITE_FIXTURES=1` rewrites them)."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
import json
import os
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from custom_components.spotnav.battery.plan_shadow import PriceSlot
from custom_components.spotnav.battery.stored_value import DayMoney, Flow
from custom_components.spotnav.sessions.costing import Slice
from custom_components.spotnav.sessions.model import SOURCE_IMPORTED_HOURLY, SOURCE_REGISTER, ChargeSession
from custom_components.spotnav.site.energy_flow import EnergyHour
from custom_components.spotnav.site.spotnav_saving import DaySaving
from custom_components.spotnav.year_review.compute import (
    ANSWER_KEYS,
    PAGE_KEYS,
    PAGE_ORDER,
    REVIEW_KEYS,
    STATE_NONE,
    STATE_READY,
    DayParts,
    YearInputs,
    cheap_slots,
    compute_review,
    price_days_of,
    day_parts_of,
    estimate_days,
    in_run_window,
    in_show_window,
    review_answer,
    review_year,
    year_bounds,
)

TZ = "Europe/Stockholm"
ZONE = ZoneInfo(TZ)
UTC = timezone.utc
HOUR = timedelta(hours=1)
FIXTURES = Path(__file__).parent / "fixtures" / "year_review" / "v1"
MONEY = ("SEK", "kr", "öre")
#: Mid-December 2026: the year so far is 1 January to 14 December.
TODAY = date(2026, 12, 15)
#: SpotNav 3.0 on the site from here: the car's plug-in, the battery's own count.
UPGRADE = date(2026, 10, 15)
#: The most expensive hour of the year (local).
PEAK = datetime(2026, 1, 28, 8, tzinfo=ZONE)


def _fixture(name: str, payload: dict[str, Any]) -> None:
    path = FIXTURES / name
    payload = json.loads(json.dumps(payload))
    if os.environ.get("SPOTNAV_WRITE_FIXTURES") == "1":
        FIXTURES.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    assert json.loads(path.read_text(encoding="utf-8")) == payload, f"{name} drifted from the backend"


def local_hours(first: date, end: date) -> list[datetime]:
    """Every local clock hour from `first` to before `end` (UTC starts; the DST days have 23 and 25)."""
    out = []
    moment = datetime(first.year, first.month, first.day, tzinfo=ZONE).astimezone(UTC)
    stop = datetime(end.year, end.month, end.day, tzinfo=ZONE).astimezone(UTC)
    while moment < stop:
        out.append(moment)
        moment += HOUR
    return out


def buy_at(moment: datetime) -> float:
    local = moment.astimezone(ZONE)
    if local == PEAK:
        return 793.0
    if local.hour < 6:
        return 50.0
    if 17 <= local.hour < 20:
        return 250.0
    return 120.0


def price_slots(first: date, end: date, *, sell: float | None = None) -> list[PriceSlot]:
    return [
        PriceSlot(hour, hour + HOUR, buy_at(hour), buy_at(hour) / 2 if sell is None else sell)
        for hour in local_hours(first, end)
    ]


def energy_hours(
    first: date, end: date, *, solar: bool = True, battery: bool = True, export: bool = True
) -> list[EnergyHour]:
    out = []
    for hour in local_hours(first, end):
        local = hour.astimezone(ZONE)
        sunny = 10 <= local.hour < 15
        produced = (2.0 if 4 <= local.month <= 9 else 0.5) if sunny else 0.0
        out.append(
            EnergyHour(
                start=hour,
                grid_in=0.3 if local == PEAK else (1.0 if local.hour < 6 else 0.4),
                grid_out=(0.5 if sunny and export and solar else 0.0),
                solar=produced if solar else None,
                battery_in=(1.5 if 2 <= local.hour < 4 else 0.0) if battery else None,
                battery_out=(1.0 if 17 <= local.hour < 20 else 0.0) if battery else None,
            )
        )
    return out


def session(
    start_local: datetime, kwh: list[float], *, plugged: bool, imported: bool = False, charger: str = "c1"
) -> ChargeSession:
    start = start_local.astimezone(UTC)
    intervals = tuple(
        Slice(start + index * HOUR, start + (index + 1) * HOUR, value, 0.5, 1.0, None, 0.5)
        for index, value in enumerate(kwh)
    )
    cost = sum(value * buy_at(start + index * HOUR) for index, value in enumerate(kwh))
    energy = sum(kwh)
    return ChargeSession(
        id=f"{charger}-{int(start.timestamp())}", charger_id=charger, start=start, end=start + len(kwh) * HOUR,
        energy_kwh=energy, energy_source=SOURCE_REGISTER, priced_kwh=energy, cost_minor=cost,
        reference_cost_minor=energy * 130.0, area_id="SE4", currency="SEK", major_unit="kr", minor_unit="öre",
        started_by="unknown" if imported else "plan_window", strategy=None, vehicle_id=None, vehicle_name=None,
        solar_kwh=0.0, solar_known_kwh=0.0, last_register_kwh=None, last_sample_at=None, intervals=intervals,
        source=SOURCE_IMPORTED_HOURLY if imported else None, plugged_in_at=start if plugged else None,
    )


def year_sessions(first: date, end: date, *, live_from: date, imported_before: date | None = None) -> list[ChargeSession]:
    """A night charge every other day (01-04 local, 12 kWh) and an evening one every tenth (17-19, 10 kWh); live from
    `live_from` (plugged in from `UPGRADE`), imported before `imported_before`."""
    out = []
    day = first
    index = 0
    while day < end:
        imported = imported_before is not None and day < imported_before
        if imported or day >= live_from:
            plugged = day >= UPGRADE
            if index % 2 == 0:
                out.append(session(datetime(day.year, day.month, day.day, 1, tzinfo=ZONE), [4.0, 4.0, 4.0], plugged=plugged, imported=imported))
            if index % 10 == 5:
                out.append(session(datetime(day.year, day.month, day.day, 17, tzinfo=ZONE), [5.0, 5.0], plugged=plugged, imported=imported))
        day += timedelta(days=1)
        index += 1
    return out


def savings_from(first: date, end: date, *, battery: bool, car: bool, heat_from: date | None) -> dict[date, DaySaving]:
    out = {}
    day = first
    while day < end:
        out[day] = DaySaving(
            battery=500.0 if battery else None,
            car=200.0 if car else None,
            heat=50.0 if heat_from is not None and day >= heat_from else None,
        )
        day += timedelta(days=1)
    return out


def battery_days(first: date, end: date) -> dict[date, DayMoney]:
    out = {}
    day = first
    while day < end:
        out[day] = DayMoney(
            arbitrage=600.0,
            quarters=96,
            controlled=96,
            flow=Flow(discharged=8.0, worth=1600.0, cost=1000.0, charged=10.0, charge_cost=620.0),
        )
        day += timedelta(days=1)
    return out


def with_prices(parts: dict[date, DayParts], slots: list[PriceSlot]) -> dict[date, DayParts]:
    """The day parts with each day's price part, as the nightly run lays them in (`compute.price_days_of`)."""
    out = dict(parts)
    for day, price in price_days_of(slots, ZONE).items():
        out[day] = replace(out.get(day, DayParts()), price=price)
    return out


def priced(inputs: YearInputs, slots: list[PriceSlot]) -> YearInputs:
    """The site's inputs with these prices in place of the ones it had."""
    parts = {day: replace(held, price=None) for day, held in inputs.day_parts.items()}
    return replace(inputs, day_parts=with_prices(parts, slots))


def with_fuse(parts: dict[date, DayParts], days: list[date]) -> dict[date, DayParts]:
    out = dict(parts)
    for day in days:
        held = out.get(day, DayParts())
        out[day] = replace(held, fuse=held.fuse + 1)
    return out


YEAR_START = date(2026, 1, 1)


def full_site(today: date = TODAY) -> YearInputs:
    """A villa on SpotNav 2.x all year (relay prices kept), upgraded to 3.0 on 15 October: sun, battery, a car."""
    sessions = year_sessions(YEAR_START, today, live_from=YEAR_START)
    savings = savings_from(UPGRADE, today, battery=True, car=True, heat_from=date(2026, 11, 1))
    parts = day_parts_of(savings, estimate_days(sessions, ZONE))
    fuse_days = [date(2026, 3, 1) + timedelta(days=6 * index) for index in range(47)]
    return YearInputs(
        year=2026,
        today=today,
        tz=TZ,
        money=MONEY,
        record_start=date(2025, 3, 2),
        hours=energy_hours(YEAR_START, today),
        has_solar=True,
        has_export=True,
        has_battery=True,
        sessions=sessions,
        day_parts=with_prices(with_fuse(parts, fuse_days), price_slots(YEAR_START, today)),
        battery_days=battery_days(UPGRADE, today),
        capacity_kwh=10.0,
        fuse_since=date(2026, 2, 1),
    )


def fresh_site() -> YearInputs:
    """Installed on 20 November on Nord Pool (prices since install), charges imported 24 months back."""
    install = date(2026, 11, 20)
    sessions = year_sessions(YEAR_START, TODAY, live_from=install, imported_before=install)
    savings = savings_from(install, TODAY, battery=True, car=True, heat_from=None)
    return YearInputs(
        year=2026,
        today=TODAY,
        tz=TZ,
        money=MONEY,
        record_start=install,
        hours=energy_hours(YEAR_START, TODAY),
        has_solar=True,
        has_export=True,
        has_battery=True,
        sessions=sessions,
        day_parts=with_prices(day_parts_of(savings, estimate_days(sessions, ZONE)), price_slots(install, TODAY)),
        battery_days=battery_days(install, TODAY),
        capacity_kwh=10.0,
        fuse_since=install,
    )


def car_only_site() -> YearInputs:
    sessions = year_sessions(YEAR_START, TODAY, live_from=YEAR_START)
    savings = savings_from(UPGRADE, TODAY, battery=False, car=True, heat_from=None)
    return YearInputs(
        year=2026,
        today=TODAY,
        tz=TZ,
        money=MONEY,
        record_start=date(2025, 6, 1),
        hours=energy_hours(YEAR_START, TODAY, solar=False, battery=False, export=False),
        sessions=sessions,
        day_parts=with_prices(day_parts_of(savings, estimate_days(sessions, ZONE)), price_slots(YEAR_START, TODAY)),
        fuse_since=date(2026, 12, 1),
    )


def sun_battery_site() -> YearInputs:
    savings = savings_from(UPGRADE, TODAY, battery=True, car=False, heat_from=None)
    return YearInputs(
        year=2026,
        today=TODAY,
        tz=TZ,
        money=MONEY,
        record_start=date(2025, 4, 1),
        hours=energy_hours(YEAR_START, TODAY),
        has_solar=True,
        has_export=True,
        has_battery=True,
        day_parts=with_prices(day_parts_of(savings, {}), price_slots(YEAR_START, TODAY)),
        battery_days=battery_days(UPGRADE, TODAY),
        capacity_kwh=12.0,
    )


def nord_pool_site() -> YearInputs:
    """A year-old site whose provider keeps 55 days of prices."""
    return priced(full_site(), price_slots(TODAY - timedelta(days=55), TODAY))


def pages_of(review: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {page["id"]: page for page in review["pages"]}


def check_shape(review: dict[str, Any]) -> None:
    assert tuple(review) == REVIEW_KEYS
    ids = [page["id"] for page in review["pages"]]
    assert ids == [page for page in PAGE_ORDER if page in ids]
    assert ids[0] == "cover" and ids[-1] == "thanks"
    for page in review["pages"]:
        assert tuple(page) == PAGE_KEYS[page["id"]]


# ------------------------------------------------------------------------------------------------ the year rule


@pytest.mark.parametrize(
    ("today", "runs", "shows", "year"),
    [
        (date(2026, 12, 9), False, False, 2026),
        (date(2026, 12, 10), True, True, 2026),
        (date(2026, 12, 31), True, True, 2026),
        (date(2027, 1, 1), True, True, 2026),
        (date(2027, 1, 2), False, True, 2026),
        (date(2027, 1, 31), False, True, 2026),
        (date(2027, 2, 1), False, False, 2027),
        (date(2026, 7, 4), False, False, 2026),
    ],
)
def test_december_runs_and_shows_the_year_so_far_and_january_the_year_before(
    today: date, runs: bool, shows: bool, year: int
) -> None:
    assert in_run_window(today) is runs
    assert in_show_window(today) is shows
    assert review_year(today) == year


def test_the_year_ends_yesterday_in_december_and_on_new_year_s_eve_in_january() -> None:
    assert year_bounds(2026, date(2026, 12, 15)) == (date(2026, 1, 1), date(2026, 12, 14))
    assert year_bounds(2026, date(2027, 1, 1)) == (date(2026, 1, 1), date(2026, 12, 31))
    assert year_bounds(2027, date(2027, 1, 1)) is None
    assert year_bounds(2028, date(2027, 6, 1)) is None


def test_january_s_review_is_the_whole_year_before() -> None:
    review = compute_review(full_site(today=date(2027, 1, 1)))
    assert review is not None
    assert review["year"] == 2026 and review["through"] == "2026-12-31"


# ------------------------------------------------------------------------------------------------ the pages


def test_a_year_old_site_has_every_page_with_the_figures_of_its_year() -> None:
    review = compute_review(full_site())
    assert review is not None
    check_shape(review)
    assert review["since"] is None and review["partial"] is False
    assert review["money"] == {"currency": "SEK", "major_unit": "kr", "minor_unit": "öre"}
    pages = pages_of(review)
    assert list(pages) == list(PAGE_ORDER)

    saved = pages["saved"]
    # 61 days of battery and car from the upgrade, 44 of heat, and the estimate of the 2.x charges before it.
    assert saved["battery"] == 61 * 500 and saved["heat"] == 44 * 50
    assert saved["car"] > 61 * 200 and saved["estimated"] is True
    assert saved["total"] == saved["battery"] + saved["car"] + saved["heat"]
    # The car's estimate counts from January, the battery and the heat from SpotNav's own count.
    assert saved["starts"] == {"battery": "2026-10-15", "car": None, "heat": "2026-11-01"}

    car = pages["car"]
    # 12 of every 22 kWh in the night's cheap hours.
    assert car["cheap_share_pct"] == round(100 * 12 * 174 / (12 * 174 + 10 * 35))
    assert car["charges"] == 174 + 35
    assert car["since"] is None and car["estimated"] is False
    assert car["cheapest"] == {"price": 50.0, "date": "2026-01-01"}

    sun = pages["sun"]
    assert sun["home_pct"] is not None and 0 < sun["home_pct"] < 100
    assert sun["export_paid"] is True and sun["since"] is None

    battery = pages["battery"]
    assert battery["earned"] == 61 * 600 and battery["earned_since"] == "2026-10-15"
    assert battery["buy_price"] == 62.0 and battery["use_price"] == 200.0
    assert battery["kwh_out"] == 348 * 3 and battery["cycles"] == round(348 * 3 / 10)

    assert pages["fuse"] == {"id": "fuse", "count": 47, "since": "2026-02-01"}

    expensive = pages["expensive"]
    assert expensive["price"] == 793.0
    assert expensive["start"] == "2026-01-28T08:00:00+01:00"
    assert expensive["bought_kwh"] == 0.3 and expensive["since"] is None


def test_a_fresh_install_marks_its_year_partial_and_omits_what_has_too_little_behind_it() -> None:
    review = compute_review(fresh_site())
    assert review is not None
    check_shape(review)
    assert review["since"] == "2026-11-20" and review["partial"] is True
    pages = pages_of(review)
    # 25 days: enough for the saving and the battery's kronor, too little for the fuse and the expensive hour.
    assert "fuse" not in pages and "expensive" not in pages
    assert pages["saved"]["starts"] == {"battery": "2026-11-20", "car": "2026-11-20", "heat": None}
    assert pages["saved"]["estimated"] is False
    car = pages["car"]
    # The imported charges count, marked; the cheap share only over the priced kWh, from the first priced charge.
    assert car["estimated"] is True and car["since"] == "2026-11-21"
    battery = pages["battery"]
    assert battery["earned"] == 25 * 600 and battery["earned_since"] == "2026-11-20"
    # The meters reach the whole year: the kWh do.
    assert battery["kwh_out"] == 348 * 3


def test_a_car_alone_has_no_sun_battery_or_fuse_page() -> None:
    review = compute_review(car_only_site())
    assert review is not None
    check_shape(review)
    assert [page["id"] for page in review["pages"]] == ["cover", "saved", "car", "expensive", "thanks"]
    assert pages_of(review)["saved"]["battery"] is None


def test_sun_and_battery_without_a_car_has_no_car_page() -> None:
    review = compute_review(sun_battery_site())
    assert review is not None
    check_shape(review)
    pages = pages_of(review)
    assert "car" not in pages and "fuse" not in pages
    assert pages["saved"]["car"] is None and pages["saved"]["estimated"] is False


def test_fifty_five_days_of_prices_say_since_when_on_the_car_and_the_expensive_hour() -> None:
    review = compute_review(nord_pool_site())
    assert review is not None
    pages = pages_of(review)
    assert pages["expensive"]["since"] == "2026-10-21"
    assert pages["expensive"]["price"] == 250.0
    # The first priced charge (a night one, every other day).
    assert pages["car"]["since"] == "2026-10-22"


def test_the_saving_is_left_out_under_a_week_or_a_krona() -> None:
    parts = {date(2026, 12, 1) + timedelta(days=index): DayParts(battery=500.0) for index in range(6)}
    inputs = replace(sun_battery_site(), day_parts=parts)
    assert "saved" not in pages_of(compute_review(inputs))
    parts = {date(2026, 12, 1) + timedelta(days=index): DayParts(battery=10.0) for index in range(9)}
    assert "saved" not in pages_of(compute_review(replace(inputs, day_parts=parts)))


def test_the_battery_s_kronor_wait_for_two_weeks_of_its_own_count() -> None:
    inputs = replace(sun_battery_site(), battery_days=battery_days(date(2026, 12, 2), TODAY))
    battery = pages_of(compute_review(inputs))["battery"]
    assert battery["earned"] is None and battery["buy_price"] is None and battery["use_price"] is None
    assert battery["kwh_in"] is not None and battery["cycles"] is not None


def test_without_an_export_meter_the_sun_s_home_share_is_not_guessed() -> None:
    inputs = replace(sun_battery_site(), has_export=False)
    sun = pages_of(compute_review(inputs))["sun"]
    assert sun["home_pct"] is None and sun["export_paid"] is None
    assert sun["kwh"] > 0


def test_a_zero_export_site_s_sun_page_says_nothing_was_sold() -> None:
    inputs = priced(sun_battery_site(), price_slots(YEAR_START, TODAY, sell=0.0))
    assert pages_of(compute_review(inputs))["sun"]["export_paid"] is False


def test_the_fuse_page_needs_four_weeks_counted_and_three_episodes() -> None:
    two = with_fuse({}, [date(2026, 11, 2), date(2026, 11, 9)])
    inputs = replace(sun_battery_site(), day_parts=two, fuse_since=date(2026, 11, 1))
    assert "fuse" not in pages_of(compute_review(inputs))
    three = with_fuse(two, [date(2026, 11, 20)])
    assert pages_of(compute_review(replace(inputs, day_parts=three)))["fuse"] == {
        "id": "fuse",
        "count": 3,
        "since": "2026-11-01",
    }
    assert "fuse" not in pages_of(compute_review(replace(inputs, day_parts=three, fuse_since=date(2026, 11, 20))))


def test_without_the_grid_meter_the_pages_from_the_hours_are_left_out() -> None:
    inputs = replace(car_only_site(), hours=None)
    pages = pages_of(compute_review(inputs))
    assert "sun" not in pages and "battery" not in pages
    assert pages["expensive"]["bought_kwh"] is None


def test_without_prices_the_car_has_charges_and_kwh_but_no_share_or_expensive_hour() -> None:
    inputs = priced(car_only_site(), [])
    pages = pages_of(compute_review(inputs))
    assert pages["car"]["cheap_share_pct"] is None and pages["car"]["since"] is None
    assert pages["car"]["charges"] > 0
    assert "expensive" not in pages


def test_only_spotnav_started_charges_before_the_plug_in_count_at_the_day_average() -> None:
    start = datetime(2026, 3, 3, 1, tzinfo=ZONE)
    planned = session(start, [4.0], plugged=False)
    person = replace(session(start + HOUR, [4.0], plugged=False), id="p", started_by="manual")
    plugged = replace(session(start + 2 * HOUR, [4.0], plugged=True), id="q")
    imported = replace(session(start + 3 * HOUR, [4.0], plugged=False, imported=True), id="r")
    assert estimate_days([planned, person, plugged, imported], ZONE) == {date(2026, 3, 3): 4 * 130.0 - 4 * 50.0}


def test_the_cheapest_third_is_counted_per_local_day() -> None:
    slots = price_slots(date(2026, 3, 3), date(2026, 3, 4))
    cheap = cheap_slots(slots, ZONE)
    assert len(cheap) == 8
    # The six night hours, then the earliest two of the day's 120 öre hours.
    assert sorted(buy_at(start) for start in cheap) == [50.0] * 6 + [120.0] * 2
    assert sorted(start.astimezone(ZONE).hour for start in cheap) == [0, 1, 2, 3, 4, 5, 6, 7]


def test_day_parts_keep_what_a_newer_merge_lacks() -> None:
    old = DayParts(battery=10.0, heat=3.0, fuse=2)
    merged = old.merged(DayParts(battery=12.0, car=4.0))
    assert merged == DayParts(battery=12.0, car=4.0, heat=3.0, fuse=2)
    assert DayParts.from_dict(merged.as_dict()) == merged
    assert DayParts.from_dict({"battery": "x", "fuse": True}) == DayParts()


# ------------------------------------------------------------------------------------------------ the fixtures


def _answer(review: dict[str, Any] | None, *, state: str = STATE_READY) -> dict[str, Any]:
    answer = review_answer(site_id="site_villa", year=2026, state=state, banner=True, row=True, review=review)
    assert tuple(answer) == ANSWER_KEYS
    return answer


@pytest.mark.parametrize(
    ("name", "inputs"),
    [
        ("full_year.json", full_site),
        ("fresh_install.json", fresh_site),
        ("car_only.json", car_only_site),
        ("sun_battery_no_car.json", sun_battery_site),
        ("nord_pool_55_days.json", nord_pool_site),
    ],
)
def test_the_contract_fixtures_are_the_backend_s_answers(name: str, inputs: Any) -> None:
    review = compute_review(inputs())
    assert review is not None
    _fixture(name, _answer(review))


def test_the_contract_fixture_of_no_review() -> None:
    answer = _answer(None, state=STATE_NONE)
    assert answer["banner"] is False and answer["row"] is False and answer["review"] is None
    _fixture("state_none.json", answer)
