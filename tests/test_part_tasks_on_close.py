"""The battery plan's, the house forecast's and the sun review's tasks end with the site, and an app wake with its
charger: a task in flight is cancelled at the unload,
and a removed site's battery plan record is not written again by a late change."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.spotnav.battery import plan_runtime
from custom_components.spotnav.notifications.push import ChargerPush
from custom_components.spotnav.runtime import charger_data
from custom_components.spotnav.site.house_forecast import SiteHouseForecast
from custom_components.spotnav.site.sun_review import SiteSunReview

from tests.test_battery_plan_runtime import _planned
from tests.test_push import REF
from tests.world import setup_charger

pytestmark = pytest.mark.usefixtures("offline_relay")


async def _hangs(*_args: Any, **_kwargs: Any) -> None:
    await asyncio.Event().wait()


async def test_a_plan_in_flight_is_cancelled_at_the_unload(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    site, controller, _now = await _planned(hass, monkeypatch)
    plan = controller.battery_shadow.plan
    monkeypatch.setattr(plan_runtime.BatteryPlanShadow, "async_plan", _hangs)
    plan._start(plan.async_plan(dt_util.now(), "test"), "spotnav_battery_plan")
    [task] = plan._tasks

    assert await hass.config_entries.async_unload(site.entry_id)
    await hass.async_block_till_done()

    assert task.cancelled()


async def test_a_house_forecast_refresh_in_flight_is_cancelled_at_the_unload(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    site, controller, _now = await _planned(hass, monkeypatch)
    forecast = controller.house_forecast
    monkeypatch.setattr(SiteHouseForecast, "async_refresh", _hangs)
    forecast._for_hour = None
    forecast._running = False
    forecast.tick()
    task = forecast._task
    assert task is not None and not task.done()

    assert await hass.config_entries.async_unload(site.entry_id)
    await hass.async_block_till_done()

    assert task.cancelled()


async def test_a_sun_review_in_flight_is_cancelled_at_the_unload(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    site, controller, _now = await _planned(hass, monkeypatch)
    review = controller.sun_review
    monkeypatch.setattr(SiteSunReview, "async_review", _hangs)
    review._done_for = None
    review._running = False
    monkeypatch.setattr(review._demand, "now", lambda: dt_util.now().replace(hour=12))
    review.tick()
    task = review._task
    assert task is not None and not task.done()

    assert await hass.config_entries.async_unload(site.entry_id)
    await hass.async_block_till_done()

    assert task.cancelled()


async def test_an_app_wake_in_flight_is_a_background_task_cancelled_at_the_unload(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry = await setup_charger(hass, title="Garage")
    push = charger_data(hass, entry.entry_id).push
    monkeypatch.setattr(ChargerPush, "_async_send", _hangs)
    push._start_wake((REF,), dt_util.utcnow())
    [task] = [task for task in entry._background_tasks if task.get_name() == "spotnav app wake"]
    assert not task.done()
    # Never held up by a start or a test's wait for the loop.
    await hass.async_block_till_done()
    assert not task.done()

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert task.cancelled()


async def test_a_removed_sites_plan_record_is_not_written_again(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    site, controller, _now = await _planned(hass, monkeypatch)
    plan = controller.battery_shadow.plan
    assert await hass.config_entries.async_remove(site.entry_id)
    await hass.async_block_till_done()
    scheduled: list[Any] = []
    monkeypatch.setattr(plan._store, "async_delay_save", lambda *args, **kwargs: scheduled.append(args))

    plan._save()

    assert scheduled == []
