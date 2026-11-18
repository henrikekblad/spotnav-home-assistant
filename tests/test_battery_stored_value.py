"""The stored energy's running value and each day's money split (`battery/stored_value.py`)."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from custom_components.spotnav.battery.plan_shadow import Bucket
from custom_components.spotnav.battery.stored_value import StoredValue

TZ = "Europe/Stockholm"
USABLE = 40.0
#: 2026-11-13 00:00 local (CET).
MIDNIGHT = datetime(2026, 11, 12, 23, 0, tzinfo=timezone.utc)


def quarter(
    start: datetime,
    *,
    buy: float = 100.0,
    sell: float = 50.0,
    imported: float = 0.0,
    exported: float = 0.0,
    battery: float = 0.0,
    soc_start: float | None = None,
    soc_end: float | None = None,
) -> Bucket:
    return Bucket(
        start=start,
        buy=buy,
        sell=sell,
        import_kwh=imported,
        export_kwh=exported,
        battery_kwh=battery,
        covered_s=900.0,
        soc_start=soc_start,
        soc_end=soc_end,
    )


def at(hours: float) -> datetime:
    return MIDNIGHT + timedelta(hours=hours)


def test_the_start_is_an_estimate_at_the_first_days_mean_buy_price_until_given_back() -> None:
    stored = StoredValue()
    stored.step(quarter(at(0), soc_start=50.0), TZ, USABLE, start_price=120.0)
    assert stored.kwh == pytest.approx(20.0) and stored.average == pytest.approx(120.0) and stored.estimated
    # Giving back less than the start held keeps the estimate; giving back all of it ends it.
    stored.step(quarter(at(0.25), battery=-10.0, buy=200.0), TZ, None)
    assert stored.estimated
    stored.step(quarter(at(0.5), battery=-10.0, buy=200.0), TZ, None)
    assert not stored.estimated


def test_the_sun_charges_at_the_sell_price_and_the_grid_at_the_buy_price() -> None:
    stored = StoredValue(kwh=0.0)
    # 2 kWh into the battery with 1.5 kWh imported: 1.5 from the grid at 100, 0.5 from the sun at 50.
    stored.step(quarter(at(1), imported=1.5, battery=2.0), TZ, None)
    assert stored.kwh == pytest.approx(2.0) and stored.cost == pytest.approx(1.5 * 100 + 0.5 * 50)
    # The sun alone.
    stored.step(quarter(at(1.25), battery=2.0, sell=30.0), TZ, None)
    assert stored.cost == pytest.approx(175.0 + 60.0) and stored.average == pytest.approx(235.0 / 4)


def test_a_discharge_earns_its_value_over_the_average_and_an_export_from_the_battery_is_arbitrage() -> None:
    stored = StoredValue(kwh=4.0, cost=400.0)
    # 3 kWh out: 1 kWh exported (at 80), 2 kWh to the house (at 300). The site's export counts as the battery's
    # first, up to what it gave.
    stored.step(quarter(at(17), buy=300.0, sell=80.0, exported=1.0, battery=-3.0), TZ, None)
    money = stored.days[date(2026, 11, 13)]
    assert money.arbitrage == pytest.approx(2 * (300 - 100) + 1 * (80 - 100))
    assert money.battery_export_kwh == pytest.approx(1.0) and money.sold == 0.0
    assert stored.kwh == pytest.approx(1.0) and stored.average == pytest.approx(100.0)
    # An export beyond what the battery gave is the sun's surplus: "sold".
    stored.step(quarter(at(17.25), buy=300.0, sell=80.0, exported=1.5, battery=-0.5), TZ, None)
    assert money.sold == pytest.approx(1.0 * 80) and money.battery_export_kwh == pytest.approx(1.5)


def test_a_loss_raises_the_average_cost_of_what_is_left() -> None:
    stored = StoredValue(kwh=0.0)
    # 10 kWh in from the grid at 100, but the SoC says 9.5 kWh are stored.
    stored.step(quarter(at(2), imported=10.0, battery=10.0, soc_end=9.5 / USABLE * 100), TZ, USABLE)
    assert stored.kwh == pytest.approx(9.5) and stored.cost == pytest.approx(1000.0)
    assert stored.average == pytest.approx(1000.0 / 9.5)


def test_the_average_carries_over_midnight_and_each_day_has_its_own_money() -> None:
    stored = StoredValue()
    stored.run(
        [
            quarter(at(-2), imported=5.0, battery=5.0, buy=80.0, soc_start=0.0),  # 22:00 the evening before
            quarter(at(1), buy=300.0, battery=-2.0),  # 01:00 the next day
        ],
        TZ,
        None,
    )
    yesterday, today = stored.days[date(2026, 11, 12)], stored.days[date(2026, 11, 13)]
    assert yesterday.bought == pytest.approx(400.0) and yesterday.arbitrage == 0.0
    assert today.arbitrage == pytest.approx(2 * (300 - 80))
    assert stored.kwh == pytest.approx(3.0) and stored.average == pytest.approx(80.0)


def test_each_quarter_counts_once_and_unpriced_or_unmeasured_ones_not_at_all() -> None:
    stored = StoredValue(kwh=0.0)
    first = quarter(at(3), imported=1.0, battery=1.0)
    assert stored.run([first, first], TZ, None) == 1
    assert stored.run([first], TZ, None) == 0
    unpriced = Bucket(start=at(3.25), import_kwh=1.0, battery_kwh=1.0, covered_s=900.0)
    thin = quarter(at(3.5), imported=1.0, battery=1.0)
    thin.covered_s = 60.0
    assert stored.run([unpriced, thin], TZ, None) == 0
    assert stored.kwh == pytest.approx(1.0)


def test_a_restart_keeps_the_stock_its_cost_and_the_days() -> None:
    stored = StoredValue()
    stored.run([quarter(at(0), imported=2.0, battery=2.0, soc_start=25.0)], TZ, USABLE, start_price=90.0)
    again = StoredValue.from_dict(stored.as_dict())
    assert again == stored
    # The same quarter is not counted again after the restart, the next one is.
    assert again.run([quarter(at(0), imported=2.0, battery=2.0), quarter(at(0.25), battery=-1.0, buy=250.0)], TZ, None) == 1
    assert StoredValue.from_dict({"kwh": "x"}) == StoredValue()
    assert StoredValue.from_dict(None) == StoredValue()


def test_each_hour_keeps_its_own_arbitrage_summing_to_the_days_and_survives_a_restart() -> None:
    stored = StoredValue(kwh=10.0, cost=1000.0)
    # 17:00 local: two quarters giving the house 1 kWh each at 300 over an average of 100; 18:00: one at 200.
    stored.step(quarter(at(17), battery=-1.0, buy=300.0), TZ, None)
    stored.step(quarter(at(17.25), battery=-1.0, buy=300.0), TZ, None)
    stored.step(quarter(at(18), battery=-1.0, buy=200.0), TZ, None)
    money = stored.days[date(2026, 11, 13)]
    assert money.hours == {at(17): pytest.approx(400.0), at(18): pytest.approx(100.0)}
    assert sum(money.hours.values()) == pytest.approx(money.arbitrage)
    again = StoredValue.from_dict(stored.as_dict())
    assert again.days[date(2026, 11, 13)].hours == {at(17): pytest.approx(400.0), at(18): pytest.approx(100.0)}
    # A store from before the hours reads with none.
    raw = stored.as_dict()
    del raw["days"]["2026-11-13"]["hours"]
    assert StoredValue.from_dict(raw).days[date(2026, 11, 13)].hours == {}


def test_a_days_total_is_kept_a_year_and_its_hours_forty_days() -> None:
    stored = StoredValue(kwh=10.0, cost=1000.0)
    stored.step(quarter(at(17), battery=-1.0, buy=300.0), TZ, None)
    stored.run([quarter(at(17 + 24 * 50), battery=-1.0, buy=300.0)], TZ, None)
    first = stored.days[date(2026, 11, 13)]
    assert first.arbitrage > 0 and first.hours == {}
    stored.run([quarter(at(17 + 24 * 420), battery=-1.0, buy=300.0)], TZ, None)
    assert date(2026, 11, 13) not in stored.days
