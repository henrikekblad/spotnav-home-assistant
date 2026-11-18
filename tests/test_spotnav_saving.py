"""What SpotNav saved: the car against charging at once on plug-in, the battery only where SpotNav controlled it, the
heat only from SpotNav's own lowerings, and the sums; the charger history and the house total agree."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from custom_components.spotnav.api.sessions import sessions_payload
from custom_components.spotnav.battery.plan_shadow import Bucket, PriceSlot
from custom_components.spotnav.battery.stored_value import DayMoney, StoredValue
from custom_components.spotnav.sessions.costing import Slice
from custom_components.spotnav.sessions.model import ChargeSession, SOURCE_REGISTER
from custom_components.spotnav.site.energy_flow import EnergyHour
from custom_components.spotnav.site.history_period import PERIOD_MONTH, PERIOD_YEAR, period_of
from custom_components.spotnav.site.house_period import HouseHistoryInputs, house_history
from custom_components.spotnav.site.spotnav_saving import (
    DaySaving,
    GridShares,
    Prices,
    battery_part,
    car_days,
    car_hours,
    day_savings,
    default_share,
    heat_part,
    own_control,
    period_sums,
    saving_block,
    session_savings,
)

ZONE = ZoneInfo("Europe/Stockholm")
UTC = timezone.utc
HOUR = timedelta(hours=1)
#: Local 2026-11-16 16:00 (CET).
PLUG = datetime(2026, 11, 16, 15, tzinfo=UTC)
NOW = datetime(2026, 11, 17, 12, tzinfo=UTC)


def slots() -> list[PriceSlot]:
    """Two days of hourly prices: 200 öre/kWh in the evening (16-20 local), 50 at night, 100 otherwise."""
    out = []
    start = datetime(2026, 11, 15, 23, tzinfo=UTC)
    for index in range(48):
        moment = start + index * HOUR
        local = moment.astimezone(ZONE).hour
        buy = 200.0 if 16 <= local < 20 else 50.0 if local < 6 else 100.0
        out.append(PriceSlot(moment, moment + HOUR, buy, buy / 2))
    return out


def charge(
    start: datetime,
    kwh_per_hour: list[float],
    *,
    started_by: str = "plan_window",
    plugged: datetime | None = PLUG,
    charger: str = "c1",
) -> ChargeSession:
    intervals = tuple(
        Slice(start + index * HOUR, start + (index + 1) * HOUR, kwh, 0.5, 1.0, None, 0.5)
        for index, kwh in enumerate(kwh_per_hour)
    )
    return ChargeSession(
        id=f"{charger}-{int(start.timestamp())}", charger_id=charger, start=start, end=start + len(kwh_per_hour) * HOUR,
        energy_kwh=sum(kwh_per_hour), energy_source=SOURCE_REGISTER, priced_kwh=0.0, cost_minor=None,
        reference_cost_minor=None, area_id=None, currency="SEK", major_unit="kr", minor_unit="öre",
        started_by=started_by, strategy=None, vehicle_id=None, vehicle_name=None, solar_kwh=0.0,
        solar_known_kwh=0.0, last_register_kwh=None, last_sample_at=None, intervals=intervals, plugged_in_at=plugged,
    )


def all_grid() -> GridShares:
    return GridShares([], default_share(has_battery=False, has_sun=False))


# ------------------------------------------------------------------------------------------------ the car


def test_a_charge_moved_from_the_evening_to_the_night_saves_the_price_difference() -> None:
    """Plugged in at 16:00, charged 02:00-04:00 at 11 kW: at once it would have charged 16:00-18:00 at 200 öre."""
    session = charge(datetime(2026, 11, 17, 1, tzinfo=UTC), [11.0, 11.0])
    savings = session_savings([session], Prices(slots()), all_grid(), NOW)
    assert savings[session.id] == pytest.approx(22.0 * (200.0 - 50.0))
    days, since = car_days([session], savings, ZONE)
    # Counted on the day the charge started, as the charger history counts the session.
    assert days == {date(2026, 11, 17): pytest.approx(3300.0)}
    assert since == date(2026, 11, 16)


def test_a_days_hourly_bars_put_a_charge_in_the_hour_it_started() -> None:
    """Two charges of one evening, the second over midnight: each whole figure in its start hour (UTC), a session
    without a figure in none."""
    first = charge(datetime(2026, 11, 16, 20, 40, tzinfo=UTC), [5.0])
    second = charge(datetime(2026, 11, 16, 22, 10, tzinfo=UTC), [11.0, 11.0])
    unknown = charge(datetime(2026, 11, 16, 20, 5, tzinfo=UTC), [3.0], plugged=None, charger="c2")
    hours = car_hours([first, second, unknown], {first.id: 120.0, second.id: -40.0, unknown.id: None})
    assert hours == {datetime(2026, 11, 16, 20, tzinfo=UTC): 120.0, datetime(2026, 11, 16, 22, tzinfo=UTC): -40.0}


def test_a_charge_started_at_plug_in_saves_nothing() -> None:
    session = charge(PLUG, [11.0, 11.0])
    assert session_savings([session], Prices(slots()), all_grid(), NOW)[session.id] == pytest.approx(0.0)


def test_a_session_a_person_started_saves_zero_and_one_without_a_plug_in_time_has_no_figure() -> None:
    person = charge(datetime(2026, 11, 17, 1, tzinfo=UTC), [11.0], started_by="manual")
    unknown = charge(datetime(2026, 11, 17, 1, tzinfo=UTC), [11.0], plugged=None, charger="c2")
    savings = session_savings([person, unknown], Prices(slots()), all_grid(), NOW)
    assert savings == {person.id: 0.0, unknown.id: None}
    days, since = car_days([unknown], savings, ZONE)
    assert days == {} and since is None


def test_a_person_start_in_a_plug_in_counts_in_the_at_once_schedule_but_saves_nothing_itself() -> None:
    """A person started 11 kWh at 16:00; SpotNav charged the rest at 02:00. At once, all 22 kWh go 16:00-18:00."""
    person = charge(PLUG, [11.0], started_by="manual")
    planned = charge(datetime(2026, 11, 17, 1, tzinfo=UTC), [11.0])
    savings = session_savings([person, planned], Prices(slots()), all_grid(), NOW)
    assert savings[person.id] == 0.0
    assert savings[planned.id] == pytest.approx(11.0 * (200.0 - 50.0))


def test_the_battery_s_kwh_to_the_car_are_left_out_of_the_car_s_saving() -> None:
    """At 02:00 the battery gives the car half its energy: those kWh are the battery's arbitrage, not the car's."""
    session = charge(datetime(2026, 11, 17, 1, tzinfo=UTC), [11.0, 11.0])
    periods = [
        EnergyHour(datetime(2026, 11, 17, 1, tzinfo=UTC) + index * HOUR, grid_in=6.5, grid_out=0.0, solar=0.0,
                   battery_in=0.0, battery_out=6.5, car=11.0, heat=0.0)
        for index in range(2)
    ]
    # The house takes 2 kWh of the battery's 6.5 first; the car gets 4.5 from the battery and 6.5 from the grid.
    shares = GridShares(periods, default_share(has_battery=True, has_sun=False))
    savings = session_savings([session], Prices(slots()), shares, NOW)
    assert savings[session.id] == pytest.approx(2 * 6.5 * 150.0)
    # An hour with no flow figure on a battery site counts none of the car's kWh.
    none = session_savings([session], Prices(slots()), GridShares([], default_share(True, False)), NOW)
    assert none[session.id] == 0.0


def test_without_the_plug_in_s_prices_there_is_no_figure() -> None:
    session = charge(datetime(2026, 11, 17, 1, tzinfo=UTC), [11.0], plugged=datetime(2026, 11, 10, tzinfo=UTC))
    assert session_savings([session], Prices(slots()), all_grid(), NOW)[session.id] is None


def test_one_session_has_the_same_saving_in_the_charger_history_and_the_house_total() -> None:
    session = charge(datetime(2026, 11, 17, 1, tzinfo=UTC), [11.0, 11.0])
    savings = session_savings([session], Prices(slots()), all_grid(), NOW)
    days, since = car_days([session], savings, ZONE)
    house = day_savings([date(2026, 11, 17)], battery_money={}, heat_days={}, car=days, car_since=since,
                        today=date(2026, 11, 17))
    charger = sessions_payload((session,), None, charger_id="c1", zone=ZONE, now=NOW, limit=5, savings=savings)
    day_row = next(row for row in charger["days"] if row["period"] == "2026-11-17")
    assert charger["sessions"][0]["plug_in_saving"] == pytest.approx(house[date(2026, 11, 17)].car / 100)
    assert day_row["plug_in_saving"] == pytest.approx(house[date(2026, 11, 17)].car / 100)
    # The webhook's (the app's) answer has no such key.
    assert "plug_in_saving" not in sessions_payload((session,), None, charger_id="c1", zone=ZONE, now=NOW, limit=5)["days"][0]


def test_a_session_round_trips_its_plug_in_time() -> None:
    session = charge(datetime(2026, 11, 17, 1, tzinfo=UTC), [11.0])
    assert ChargeSession.from_dict(session.as_dict()).plugged_in_at == PLUG
    older = session.as_dict()
    del older["plugged_in_at"]
    assert ChargeSession.from_dict(older).plugged_in_at is None


# ------------------------------------------------------------------------------------------------ battery and heat


def test_the_battery_counts_only_where_spotnav_controlled_it() -> None:
    # In shadow (Sigenergy's own AI): what the battery earned is its own, not SpotNav's.
    assert battery_part(DayMoney(arbitrage=3500.0, quarters=96)) == (None, 0.0)
    assert battery_part(DayMoney(arbitrage=3500.0, quarters=96, controlled=96)) == (3500.0, 1.0)
    assert battery_part(DayMoney(arbitrage=3500.0, quarters=96, controlled=48)) == (1750.0, 0.5)
    assert battery_part(None) == (None, None)
    day = date(2026, 11, 16)
    shadow = day_savings([day], battery_money={day: DayMoney(arbitrage=3500.0, quarters=96)}, heat_days={}, car={},
                         car_since=None, today=day)[day]
    assert shadow.total is None
    assert saving_block(day, shadow, None) == {
        "date": "2026-11-16", "total": None, "battery": None, "car": None, "heat": None, "battery_share": 0.0,
        "car_since": None,
    }


def test_own_control_says_what_the_battery_earned_and_what_the_plan_would_have() -> None:
    class Reviewed:
        saving, actual_saving = 150.0, 50.0

    assert own_control(3500.0, 0.0) == (3500.0, None)
    assert own_control(3500.0, 0.0, Reviewed()) == (3500.0, 3600.0)
    assert own_control(3500.0, 0.5, Reviewed()) == (1750.0, 1800.0)
    # Controlled all day, or a day without the battery's money: nothing to compare.
    assert own_control(3500.0, 1.0, Reviewed()) == (None, None)
    assert own_control(None, 0.0) == (None, None) and own_control(3500.0, None) == (None, None)


def test_the_stored_value_counts_the_quarters_spotnav_controlled() -> None:
    stored = StoredValue()
    start = datetime(2026, 11, 16, 12, tzinfo=UTC)
    buckets = [
        Bucket(start + index * timedelta(minutes=15), buy=100.0, sell=50.0, import_kwh=0.5, covered_s=900.0,
               soc_start=50.0, soc_end=50.0)
        for index in range(4)
    ]
    stored.run(buckets[:2], "Europe/Stockholm", 10.0)
    stored.run(buckets[2:], "Europe/Stockholm", 10.0, controlled=True)
    money = stored.days[date(2026, 11, 16)]
    assert (money.quarters, money.controlled) == (4, 2)
    again = StoredValue.from_dict(stored.as_dict()).days[date(2026, 11, 16)]
    assert again.controlled == 2


def test_the_heat_counts_spotnav_s_own_lowerings_never_the_shadow_plan() -> None:
    review = {"relay": {"hours": 3.0, "value": 158.0}, "plan": {"hours": 4.0, "value": 400.0}}
    assert heat_part({"review": review, "spotnav_min": 180}) == pytest.approx(158.0)
    # SpotNav held the relay a third of its closed time: a third of the relay's figure.
    assert heat_part({"review": review, "spotnav_min": 60}) == pytest.approx(158.0 / 3)
    # Nothing SpotNav did (only the shadow plan, or someone else's closings): no heat part.
    assert heat_part({"review": review, "spotnav_min": 0}) is None
    assert heat_part({"review": review}) is None
    assert heat_part({"review": None, "spotnav_min": 120}) is None


# ------------------------------------------------------------------------------------------------ sums


def _days() -> dict[date, DaySaving]:
    return {
        date(2026, 10, 30): DaySaving(battery=1000.0, car=200.0, battery_share=1.0),
        date(2026, 11, 1): DaySaving(car=300.0, heat=50.0, battery_share=0.0),
        date(2026, 11, 2): DaySaving(car=0.0, battery_share=0.0),
    }


def test_a_period_sums_each_part_over_the_days_that_have_it() -> None:
    november = {day: item for day, item in _days().items() if day.month == 11}
    assert period_sums(november) == {
        "total": 350.0, "battery": None, "battery_days": 0, "car": 300.0, "car_days": 2, "heat": 50.0,
        "heat_days": 1, "days": 2,
    }
    assert period_sums({}) is None


@pytest.mark.parametrize(("kind", "keys"), [(PERIOD_MONTH, ("2026-11-01", "2026-11-02")), (PERIOD_YEAR, ("2026-10", "2026-11"))])
def test_the_history_buckets_carry_the_sums(kind: str, keys: tuple[str, str]) -> None:
    period = period_of(kind, date(2026, 11, 1) if kind == PERIOD_MONTH else date(2026, 1, 1))
    answer = house_history(
        HouseHistoryInputs(site_id="s", now=NOW, tz="Europe/Stockholm", period=period, periods=[], savings=_days())
    )
    rows = {row["key"]: row for row in answer["buckets"]}
    first, second = rows[keys[0]], rows[keys[1]]
    if kind == PERIOD_MONTH:
        assert (first["saving"], first["saving_car"], first["saving_heat"], first["saving_battery"]) == (350.0, 300.0, 50.0, None)
        assert (second["saving"], second["saving_car"]) == (0.0, 0.0)
        assert answer["saving"]["total"] == 350.0
    else:
        assert (first["saving"], first["saving_battery"], first["saving_car"]) == (1200.0, 1000.0, 200.0)
        assert (second["saving"], second["saving_car"], second["saving_heat"]) == (350.0, 300.0, 50.0)
        assert answer["saving"] == {
            "total": 1550.0, "battery": 1000.0, "battery_days": 1, "car": 500.0, "car_days": 3, "heat": 50.0,
            "heat_days": 1, "days": 3,
        }
