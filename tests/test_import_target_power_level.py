"""The power level as a hard cap (plan v3_power_tariff §2): the import target's hold keeps the hour under the smaller
of the target and the level in the level's hours, the month's free level never lifts it, the level alone holds
without a target (in its own hours only), and the plans count on the same caps hour by hour."""

from __future__ import annotations

from datetime import timedelta
from zoneinfo import ZoneInfo

import pytest
from freezegun import freeze_time
from homeassistant.core import HomeAssistant

from custom_components.spotnav.planning.auto_settings import DemandComponent, PowerLevel
from custom_components.spotnav.planning.demand_charge import HourMean
from custom_components.spotnav.planning.planner import HighLoadWindow
from custom_components.spotnav.site.import_target import effective_target
from custom_components.spotnav.site.site_capacity_controller import (
    LIMIT_CAUSE_IMPORT_TARGET,
    TARGET_NOT_APPLIED_OUTSIDE_LEVEL,
)

from .test_import_target_hold import _charging, _HOUR, _world


def _level(monkeypatch, level: PowerLevel | None, *components: DemandComponent) -> None:
    monkeypatch.setattr(
        "custom_components.spotnav.pricing.price_source.site_power_level", lambda _hass, _entry_id: level
    )
    monkeypatch.setattr(
        "custom_components.spotnav.pricing.price_source.site_demand_components",
        lambda _hass, _entry_id: tuple(components),
    )


def _margined(kw: float) -> float:
    return kw - max(0.1, 0.02 * kw)


def test_the_level_caps_the_target_and_the_months_peak_never_lifts_it() -> None:
    assert effective_target(6.0, (), 5.0).kw == 5.0
    capped = effective_target(6.0, [9.0], 5.0)
    assert (capped.kw, capped.free_kw, capped.level_kw, capped.capped, capped.lifted) == (5.0, 9.0, 5.0, True, False)
    # A level above the lifted target changes nothing.
    assert effective_target(6.0, [7.0], 10.0).kw == 7.0
    assert effective_target(6.0, [7.0]).level_kw is None


async def test_in_the_levels_hours_the_hold_keeps_the_hour_under_the_lower_level(
    hass: HomeAssistant, monkeypatch
) -> None:
    # A 5.5 kW level in all hours under a 6 kW target, and a 9 kW peak this month that would lift the target.
    _level(monkeypatch, PowerLevel(5.5), DemandComponent(top_n=1))
    with freeze_time(_HOUR + timedelta(seconds=5)) as frozen:
        world = await _world(hass, monkeypatch, frozen, "levelcap")
        await world.step()
        world.halo.controller.demand.tracker.hours.append(HourMean(start=_HOUR - timedelta(hours=3), kw=9.0))
        await _charging(world)
        await world.run(20)
        controller = world.halo.controller
        charger = world.halo.charger.entry_id
        # 5.5 kW less its margin (5.39), less the house's 1 kW: 6 A on three phases.
        assert world.car_a == 6.0
        assert controller.load_balancing_limit(charger) == (6.0, LIMIT_CAUSE_IMPORT_TARGET)
        effective = controller.import_target_snapshot()["effective"]
        assert (effective["kw"], effective["level_kw"]) == (5.5, 5.5)
        # The plan counts on the level less the house in every hour it holds.
        caps = controller.import_target_plan_kw_by_hour(charger)
        assert caps and all(cap <= _margined(5.5) for cap in caps.values())
        for hour in world.complete_hours():
            assert world.hour_kw(hour) <= 5.5


async def test_without_a_target_the_level_alone_holds_in_its_hours(hass: HomeAssistant, monkeypatch) -> None:
    _level(monkeypatch, PowerLevel(6.0))
    with freeze_time(_HOUR + timedelta(seconds=5)) as frozen:
        world = await _world(hass, monkeypatch, frozen, "levelonly", target_kw=None)
        await world.step()
        await _charging(world)
        await world.run(20)
        controller = world.halo.controller
        charger = world.halo.charger.entry_id
        assert world.car_a == 7.0
        assert controller.load_balancing_limit(charger) == (7.0, LIMIT_CAUSE_IMPORT_TARGET)
        assert controller.import_target_snapshot()["target_kw"] is None
        # No flat cap without a target: the level's hours are capped one by one.
        assert controller.import_target_plan_kw(charger) is None
        assert controller.import_target_plan_kw_by_hour(charger)
        # A person may set it aside as the target.
        assert controller.async_release_import_target(charger) is None


async def test_outside_the_levels_hours_nothing_is_held_without_a_target(hass: HomeAssistant, monkeypatch) -> None:
    with freeze_time(_HOUR + timedelta(seconds=5)) as frozen:
        world = await _world(hass, monkeypatch, frozen, "leveloutside", target_kw=None)
        controller = world.halo.controller
        tz = controller.demand.tracker.tz
        # A level only in one hour of the day, six hours from now on the site's clock.
        other = (_HOUR.astimezone(ZoneInfo(tz)).hour + 6) % 24
        window = HighLoadWindow(days="all", from_month=1, to_month=12, from_hour=other, to_hour=other + 1)
        _level(monkeypatch, PowerLevel(6.0, window))
        await world.step()
        await _charging(world)
        await world.run(5)
        assert world.car_a == 16.0
        assert controller.import_target_snapshot()["not_applied"] == TARGET_NOT_APPLIED_OUTSIDE_LEVEL
        # The plan is capped only in the level's own hour.
        caps = controller.import_target_plan_kw_by_hour(world.halo.charger.entry_id) or {}
        assert all(PowerLevel(6.0, window).counts(hour, tz) for hour in caps)


async def test_neither_a_target_nor_a_level_refuses_a_release(hass: HomeAssistant, monkeypatch) -> None:
    _level(monkeypatch, None)
    with freeze_time(_HOUR + timedelta(seconds=5)) as frozen:
        world = await _world(hass, monkeypatch, frozen, "nolevel", target_kw=None)
        await world.step()
        assert world.halo.controller.async_release_import_target(world.halo.charger.entry_id) == (
            "spotnav_no_import_target"
        )


@pytest.mark.parametrize("target", [None, 6.0])
def test_the_plan_hold_for_an_hour_is_the_lower_of_the_two_where_the_level_holds(target: float | None) -> None:
    from custom_components.spotnav.site.site_capacity_controller import SiteCapacityController

    class _Site:
        class demand:  # noqa: N801 - a stand-in for the site's tracker
            class tracker:  # noqa: N801
                tz = "Europe/Stockholm"

    window = HighLoadWindow(days="weekdays", from_month=11, to_month=3, from_hour=7, to_hour=20)
    level = PowerLevel(5.0, window, outside=True)
    from datetime import datetime, timezone

    inside = datetime(2026, 11, 16, 8, tzinfo=timezone.utc)  # Monday 09:00: the window's, not the level's
    night = datetime(2026, 11, 16, 21, tzinfo=timezone.utc)
    hold = SiteCapacityController.plan_hold_kw_at
    assert hold(_Site(), night, target, level) == 5.0
    assert hold(_Site(), inside, target, level) == target
