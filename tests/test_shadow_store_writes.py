"""The battery shadow's and the switched load's books are written within their delay while the looks go on every ten
seconds: a write already waiting is not put off by the next look, and the hold's count time alone is no change."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.spotnav.battery import runtime
from custom_components.spotnav.loads import runtime as loads_runtime
from custom_components.spotnav.loads.switch_load import quarter_start

from tests.test_battery_plan_runtime import _planned
from tests.test_switch_load_runtime import _prices
from tests.test_switch_load_runtime import _site as _load_site

pytestmark = pytest.mark.usefixtures("offline_relay")


async def _looks(hass: HomeAssistant, look: Any, seconds: int) -> None:
    """A look every ten seconds for `seconds`, the clock moved on with them."""
    start = dt_util.utcnow()
    for at in range(10, seconds + 1, 10):
        look(start + timedelta(seconds=at))
        async_fire_time_changed(hass, start + timedelta(seconds=at))
        await hass.async_block_till_done()


async def test_the_battery_book_is_written_within_its_delay_while_changes_go_on(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    site, controller, _now = await _planned(hass, monkeypatch)
    shadow = controller.battery_shadow
    key = runtime.store_key(site.entry_id)
    hass_storage.pop(key, None)

    await _looks(hass, lambda _at: shadow._save(), int(runtime._SAVE_DELAY_S) + 10)

    assert key in hass_storage


async def test_the_hold_count_time_alone_asks_for_no_write(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    _site_entry, controller, now = await _planned(hass, monkeypatch)
    shadow = controller.battery_shadow
    shadow.tick(now + timedelta(seconds=10))
    calls: list[float] = []
    real = shadow._store.async_delay_save
    monkeypatch.setattr(shadow, "_save_pending", False)

    def spy(data: Any, delay: float = 0) -> None:
        calls.append(delay)
        real(data, delay)

    monkeypatch.setattr(shadow._store, "async_delay_save", spy)
    for seconds in range(20, 320, 10):
        shadow.tick(now + timedelta(seconds=seconds))

    assert len(calls) <= 1


async def test_the_switched_load_book_is_written_within_its_delay_while_it_ticks(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    _prices(monkeypatch, quarter_start(dt_util.now()))
    site, controller = await _load_site(hass, switch_state="on")
    load = controller.switch_load
    key = loads_runtime.store_key(site.entry_id)
    hass_storage.pop(key, None)

    await _looks(hass, load.tick, int(loads_runtime._SAVE_DELAY_S) + 10)

    assert key in hass_storage
