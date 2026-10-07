"""The site's solar surplus (`site/solar_surplus.py`'s `site_surplus`): what a car could take from the sun
at the site now, on the basis solar charges on, whatever the chargers' strategies and with no charger at all.
"""

from __future__ import annotations

import pytest

from custom_components.spotnav.site.solar_surplus import (
    SolarObservation,
    site_surplus,
    surplus_breakdown,
)


def test_car_first_counts_export_a_charging_battery_and_the_cars_draw() -> None:
    surplus = site_surplus(
        net_grid_w=-1500.0, car_w=2000.0, battery_w=800.0, battery_configured=True, priority="car_first"
    )

    assert surplus is not None
    assert surplus.surplus_w == 4300.0
    assert (surplus.export_w, surplus.battery_w, surplus.car_w) == (1500.0, 800.0, 2000.0)
    assert surplus.priority_effective == "car_first"


def test_battery_first_leaves_a_charging_battery_out() -> None:
    surplus = site_surplus(
        net_grid_w=-1500.0, car_w=2000.0, battery_w=800.0, battery_configured=True, priority="battery_first"
    )

    assert surplus is not None
    assert (surplus.surplus_w, surplus.battery_w) == (3500.0, 0.0)
    assert surplus.priority_effective == "battery_first"


@pytest.mark.parametrize("priority", ["car_first", "battery_first"])
def test_a_discharging_battery_is_never_surplus(priority) -> None:
    # The battery feeds the car 1000 W of the 2000 W it draws: only the other 1000 W are the sun's.
    surplus = site_surplus(
        net_grid_w=0.0, car_w=2000.0, battery_w=-1000.0, battery_configured=True, priority=priority
    )

    assert surplus is not None
    assert (surplus.surplus_w, surplus.battery_w) == (1000.0, -1000.0)


def test_import_beyond_what_the_car_draws_shows_no_surplus_never_a_negative_one() -> None:
    surplus = site_surplus(
        net_grid_w=2500.0, car_w=2000.0, battery_w=None, battery_configured=False, priority="car_first"
    )

    assert surplus is not None
    assert surplus.available_w == -500.0
    assert surplus.surplus_w == 0.0
    assert surplus.priority_effective == "battery_first"


def test_no_grid_reading_is_no_value() -> None:
    assert (
        site_surplus(net_grid_w=None, car_w=0.0, battery_w=500.0, battery_configured=True, priority="car_first")
        is None
    )


def test_a_configured_battery_that_cannot_be_read_is_no_value() -> None:
    assert (
        site_surplus(net_grid_w=-1000.0, car_w=0.0, battery_w=None, battery_configured=True, priority="car_first")
        is None
    )


def test_a_site_without_chargers_shows_its_export_and_its_charging_battery() -> None:
    surplus = site_surplus(
        net_grid_w=-1200.0, car_w=0.0, battery_w=3000.0, battery_configured=True, priority="car_first"
    )

    assert surplus is not None
    assert (surplus.surplus_w, surplus.export_w, surplus.battery_w, surplus.car_w) == (4200.0, 1200.0, 3000.0, 0.0)


@pytest.mark.parametrize("priority", ["car_first", "battery_first"])
def test_one_charger_alone_reckons_what_the_site_does(priority) -> None:
    observation = SolarObservation(
        now=0.0,
        signed_grid_w={"L1": -400.0, "L2": -300.0, "L3": 100.0},
        voltage_v={"L1": 230.0, "L2": 230.0, "L3": 230.0},
        car_delivered_a={"L1": 6.0, "L2": 6.0, "L3": 6.0},
        battery_w=700.0,
        car_phases=("L1", "L2", "L3"),
        battery_configured=True,
    )

    breakdown = surplus_breakdown(observation, priority)
    site = site_surplus(
        net_grid_w=-600.0, car_w=3 * 6.0 * 230.0, battery_w=700.0, battery_configured=True, priority=priority
    )

    assert breakdown is not None and site is not None
    assert breakdown.available_w == site.available_w
    assert breakdown.priority_effective == site.priority_effective
