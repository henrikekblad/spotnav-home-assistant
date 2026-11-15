"""A stored battery record of any shape never stops the site: the battery plan's record (`battery/plan_runtime.py`)
and the battery shadow's book (`battery/runtime.py`) are read defensively, and a record that still cannot be read
starts a new one. Each malformed shape is saved, the site is loaded again, and it loads."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from custom_components.spotnav.battery import plan_runtime, runtime
from custom_components.spotnav.battery.executor import ExecState
from custom_components.spotnav.battery.learned import LearnedBattery
from custom_components.spotnav.battery.monitor import Command
from custom_components.spotnav.battery.plan_shadow import DayReview, PlanActuals
from custom_components.spotnav.runtime import site_controller_for

from tests.test_battery_plan_runtime import _planned

pytestmark = pytest.mark.usefixtures("offline_relay")

NOW = datetime(2026, 11, 15, 12, tzinfo=timezone.utc)

_REVIEW_WITH_LIST_MODES: dict[str, Any] = {
    "date": "2026-11-13", "reviewed_at": "2026-11-14T00:00:00+00:00", "quarters": 96, "covered": 96,
    "unplanned": 0, "start_soc_pct": 50, "end_value": 1, "usable_kwh": 10, "actual": {"cost": 1, "end_soc_pct": 1},
    "plan": {"cost": 1, "end_soc_pct": 1}, "self_consumption": {"cost": 1, "end_soc_pct": 1}, "modes": ["balanced"],
}


async def _reload_with(hass: HomeAssistant, site: Any, key: str, record: Any) -> Any:
    """Unload the site, store `record` under `key`, load the site again; the controller after."""
    assert await hass.config_entries.async_unload(site.entry_id)
    await Store(hass, 1, key).async_save(record)
    await hass.config_entries.async_setup(site.entry_id)
    await hass.async_block_till_done()
    assert site.state is ConfigEntryState.LOADED
    controller = site_controller_for(hass, site.entry_id)
    assert controller is not None
    return controller


@pytest.mark.parametrize(
    "bad",
    [
        {"reviews": 5},
        {"reviews": [_REVIEW_WITH_LIST_MODES]},
        {"learned": {"samples": 5}},
        {"plan": {"slots": 3}},
        {"actuals": {"buckets": 1}},
        {"actuals": {"firsts": 1, "rows": True}},
        {"plan": {"mode_slots": {"balanced": [[["x"], "y", "z", 1, 2]]}}},
    ],
)
async def test_a_malformed_battery_plan_record_starts_a_new_one(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, bad: dict[str, Any]
) -> None:
    site, controller, _now = await _planned(hass, monkeypatch)
    record = controller.battery_shadow.plan.as_dict() | bad

    controller = await _reload_with(hass, site, plan_runtime.store_key(site.entry_id), record)

    assert controller.battery_shadow.plan._loaded


async def test_a_battery_plan_record_that_raises_is_dropped(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    site, controller, _now = await _planned(hass, monkeypatch)
    record = controller.battery_shadow.plan.as_dict()

    def broken(*_args: Any, **_kwargs: Any) -> PlanActuals:
        raise RuntimeError("unreadable")

    monkeypatch.setattr(PlanActuals, "from_dict", broken)
    controller = await _reload_with(hass, site, plan_runtime.store_key(site.entry_id), record)

    plan = controller.battery_shadow.plan
    assert plan._loaded and plan.reviews == [] and plan.plan == ()


@pytest.mark.parametrize(
    "sections",
    [
        {"monitor": {"watch": {"command": {"kind": ["x"], "at": "2026-11-14T10:00:00+00:00", "rate_kw": 1}}}},
        {"monitor": 5, "ledger": [1], "outside_control": "x", "export_curtail": [[1, 2]]},
    ],
)
async def test_a_malformed_battery_shadow_book_still_loads_the_site(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, sections: dict[str, Any]
) -> None:
    site, controller, _now = await _planned(hass, monkeypatch)
    book = controller.battery_shadow.book.as_dict()
    book["sections"] = (book.get("sections") or {}) | sections

    controller = await _reload_with(hass, site, runtime.store_key(site.entry_id), book)

    assert controller.battery_shadow._loaded and controller.battery_shadow._timer_cancel is not None


async def test_a_battery_shadow_start_that_raises_still_loads_the_site(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    site, _controller, _now = await _planned(hass, monkeypatch)
    assert await hass.config_entries.async_unload(site.entry_id)

    def broken(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("no facts")

    monkeypatch.setattr(runtime.BatteryShadow, "_read_facts", broken)
    monkeypatch.setattr(plan_runtime.BatteryPlanShadow, "async_start", broken)
    await hass.config_entries.async_setup(site.entry_id)
    await hass.async_block_till_done()

    assert site.state is ConfigEntryState.LOADED
    controller = site_controller_for(hass, site.entry_id)
    assert controller is not None and controller.battery_shadow._timer_cancel is not None


@pytest.mark.parametrize("raw", [{"buckets": 1}, {"firsts": "x"}, {"rows": 2.5}, {"buckets": [1, [2]]}])
def test_plan_actuals_skip_what_is_not_a_list(raw: dict[str, Any]) -> None:
    assert PlanActuals.from_dict(raw, NOW).buckets == {}


def test_a_review_with_modes_not_a_mapping_is_dropped() -> None:
    assert DayReview.from_dict(_REVIEW_WITH_LIST_MODES) is None


@pytest.mark.parametrize("samples", [5, "x", {"a": 1}, True])
def test_learned_samples_not_a_list_are_none(samples: Any) -> None:
    assert LearnedBattery.from_dict({"samples": samples}).samples == []


def test_a_command_kind_that_cannot_be_hashed_is_none() -> None:
    assert Command.from_dict({"kind": ["x"], "at": "2026-11-14T10:00:00+00:00", "rate_kw": 1}) is None


def test_an_exec_state_phase_that_cannot_be_hashed_is_idle() -> None:
    assert ExecState.from_dict({"phase": ["x"]}).phase == "idle"
