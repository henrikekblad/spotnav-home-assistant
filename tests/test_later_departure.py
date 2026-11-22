""""Later departure" for one departure that does not fit under the ceiling (plan
`plans/v3_does_not_fit_followups.md`, step 3): the earliest whole hour where it all fits
(`import_target.later_departure`), the person's choice on the site (`SiteCapacityController.async_later_departure`,
`spotnav/later_departure`), the phone's button, and the charger's plan made for it (`AutoPlannerController`) while the
departure's settings stay the person's."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from freezegun import freeze_time
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.spotnav.runtime import controller_for, domain_data
from custom_components.spotnav.site.import_target import departure_shortfall_kwh, later_departure, RISK_LEAVE_KWH
from custom_components.spotnav.site.site_capacity_controller import (
    ERROR_NO_LATER_DEPARTURE,
    ERROR_STALE_DEPARTURE,
    SiteCapacityController,
)

from .test_import_target_hold import _HOUR, _plan, _tight_night, _told

STOCKHOLM = ZoneInfo("Europe/Stockholm")
#: 2026-11-02 13:00 in Stockholm (UTC+1).
NOW = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)


# -- the earliest whole hour that fits (pure)


def _later(need: float, departure: datetime, until: datetime | None, **changes: Any) -> datetime | None:
    figures = {
        "need_kwh": need,
        "now": NOW,
        "departure": departure,
        "hour_end": NOW + timedelta(hours=1),
        "held_kw": 4.0,
        "later_kw": 4.0,
        "later_kw_by_hour": None,
        "until": until,
        "tz": STOCKHOLM,
    }
    return later_departure(**{**figures, **changes})


def test_the_first_whole_hour_that_fits_with_a_margin_to_spare() -> None:
    # 14 kWh at 4 kW: 3.5 h, and half a kWh to spare: 17:00 local is the first whole hour (4 h, 16 kWh).
    departure = NOW + timedelta(hours=2, minutes=30)  # 15:30 local
    found = _later(14.0, departure, NOW + timedelta(hours=35))
    assert found == datetime(2026, 11, 2, 16, 0, tzinfo=timezone.utc)
    shortfall = departure_shortfall_kwh(
        need_kwh=14.0, now=NOW, departure=found, hour_end=NOW + timedelta(hours=1), held_kw=4.0, later_kw=4.0
    )
    assert shortfall is not None and shortfall <= RISK_LEAVE_KWH


def test_the_hours_later_ones_know_are_counted_at_their_own_figure() -> None:
    # The evening's hours leave the car nothing (the house's own peak): the first hour that fits moves on.
    departure = NOW + timedelta(hours=2, minutes=30)
    evening = {NOW + timedelta(hours=h): 0.0 for h in (3, 4)}
    assert _later(14.0, departure, NOW + timedelta(hours=35), later_kw_by_hour=evening) == NOW + timedelta(hours=6)


def test_nothing_past_the_prices_held_and_nothing_without_them() -> None:
    departure = NOW + timedelta(hours=2, minutes=30)
    assert _later(14.0, departure, NOW + timedelta(hours=3)) is None
    assert _later(14.0, departure, None) is None
    # Nothing later fits at all (no power): none, after a bounded search.
    assert _later(14.0, departure, NOW + timedelta(hours=35), held_kw=0.0, later_kw=0.0) is None


# -- the person's choice on the site, through the tight night of `tests/test_import_target_hold.py`


def _prices_held(monkeypatch, hours: float = 35.0) -> None:
    monkeypatch.setattr(SiteCapacityController, "prices_end", lambda self, now: _HOUR + timedelta(hours=hours))


def _replans(monkeypatch, hass: HomeAssistant, charger: str) -> list[str]:
    """The charger's plan made again: recorded, not run (the world's plan is set by hand)."""
    from custom_components.spotnav.runtime import preview_for

    calls: list[str] = []
    preview = preview_for(hass, charger)
    if preview is not None:

        async def recalculate() -> None:
            calls.append("recalculate")

        monkeypatch.setattr(preview, "async_recalculate", recalculate)
    return calls


async def test_later_departure_moves_this_departure_only_and_ends_with_another(hass: HomeAssistant, monkeypatch) -> None:
    from custom_components.spotnav.api.import_target import later_departure as ask_later

    _prices_held(monkeypatch)
    with freeze_time(_HOUR + timedelta(seconds=5)) as frozen:
        world = await _tight_night(hass, monkeypatch, frozen, "laterok")
        charger = world.halo.charger.entry_id
        controller = world.halo.controller
        replans = _replans(monkeypatch, hass, charger)
        await world.run(2)
        risk = controller.import_target_status(charger).at_risk
        assert risk is not None and risk.later_departure is not None
        later = dt_util.parse_datetime(risk.later_departure)
        assert later > world.departure and later.minute == 0
        settings = domain_data(hass).auto_store.settings(charger)
        assert ask_later(hass, world.halo.charger) is None
        await hass.async_block_till_done()
        assert replans == ["recalculate"], "the plan is made again for it"
        assert controller.later_departure_for(charger) == (world.departure, later)
        assert controller.import_target_snapshot()["later_departure"] == {
            charger: {"from": world.departure.isoformat(), "to": later.isoformat()}
        }
        assert domain_data(hass).auto_store.settings(charger) == settings, "the departure's settings are untouched"
        # The plan made again for the later departure: it is kept, and fits there.
        now = dt_util.utcnow()
        world.halo.cc.plan = _plan(now, later, [(now, later)])
        await world.run(2)
        assert controller.later_departure_for(charger) == (world.departure, later)
        # Another departure set: it ends.
        world.halo.cc.plan = _plan(now, later + timedelta(days=1), [(now, later)])
        await world.run(2)
        assert controller.later_departure_for(charger) is None
        assert controller.import_target_snapshot()["later_departure"] == {}


async def test_later_departure_survives_a_restart_and_ends_at_the_unplug_and_at_the_departure(
    hass: HomeAssistant, monkeypatch
) -> None:
    _prices_held(monkeypatch)
    with freeze_time(_HOUR + timedelta(seconds=5)) as frozen:
        world = await _tight_night(hass, monkeypatch, frozen, "laterkeep")
        charger = world.halo.charger.entry_id
        controller = world.halo.controller
        _replans(monkeypatch, hass, charger)
        await world.run(2)
        assert controller.async_later_departure(charger) is None
        moved = controller.later_departure_for(charger)
        assert moved is not None
        record = controller._released_record()  # noqa: SLF001
        assert record["later"] == {charger: {"from": moved[0].isoformat(), "to": moved[1].isoformat()}}
        await controller._target_release_store.async_save(record)  # noqa: SLF001
        fresh = SiteCapacityController(hass, controller.entry_id, dict(controller.config))
        await fresh._async_load_released()  # noqa: SLF001
        assert fresh.later_departure_for(charger) == moved
        # The later departure passes: it ends.
        fresh._prune_released(moved[1] + timedelta(seconds=1))  # noqa: SLF001
        assert fresh.later_departure_for(charger) is None
        # The car is unplugged: it ends.
        monkeypatch.setattr(controller_for(hass, charger).adapter, "vehicle_connected", lambda: False)
        controller._prune_released(dt_util.utcnow())  # noqa: SLF001
        assert controller.later_departure_for(charger) is None


async def test_later_departure_is_refused_without_a_later_hour_or_for_another_departure(
    hass: HomeAssistant, monkeypatch
) -> None:
    with freeze_time(_HOUR + timedelta(seconds=5)) as frozen:
        world = await _tight_night(hass, monkeypatch, frozen, "laternone")
        charger = world.halo.charger.entry_id
        controller = world.halo.controller
        await world.run(2)
        # No prices held past the departure: nothing later is proposed, nor done.
        risk = controller.import_target_status(charger).at_risk
        assert risk is not None and risk.later_departure is None
        assert controller.async_later_departure(charger) == ERROR_NO_LATER_DEPARTURE
        stale = int(world.departure.timestamp()) - 3600
        assert controller.async_later_departure(charger, for_departure=stale, to=stale + 7200) == ERROR_STALE_DEPARTURE
        assert controller.later_departure_for(charger) is None
        # Nor is the notice's button offered.
        assert [action["action"].split("_")[1] for action in _told(world)[0]["data"]["actions"]] == ["TAKE", "IMPORT"]


async def test_the_notice_offers_the_later_departure_and_its_press_moves_it(hass: HomeAssistant, monkeypatch) -> None:
    _prices_held(monkeypatch)
    with freeze_time(_HOUR + timedelta(seconds=5)) as frozen:
        world = await _tight_night(hass, monkeypatch, frozen, "laterphone")
        charger = world.halo.charger.entry_id
        controller = world.halo.controller
        _replans(monkeypatch, hass, charger)
        await world.run(2)
        later = dt_util.parse_datetime(controller.import_target_status(charger).at_risk.later_departure)
        told = _told(world)
        assert len(told) == 1
        actions = told[0]["data"]["actions"]
        stamp = int(world.departure.timestamp())
        assert [action["action"] for action in actions] == [
            f"SPOTNAV_TAKE_WHAT_FITS_{charger}_{stamp}",
            f"SPOTNAV_LATER_DEPARTURE_{charger}_{stamp}_{int(later.timestamp())}",
            f"SPOTNAV_IMPORT_TARGET_RELEASE_{charger}_{stamp}",
        ]
        assert actions[1]["title"] == f"Later departure ({dt_util.as_local(later):%H:%M})"
        # A press for another departure is refused.
        hass.bus.async_fire(
            "mobile_app_notification_action",
            {"action": f"SPOTNAV_LATER_DEPARTURE_{charger}_{stamp - 3600}_{int(later.timestamp())}"},
        )
        await hass.async_block_till_done()
        assert controller.later_departure_for(charger) is None
        # The pre-release review (2026-11-22): a time other than the one the risk holds, or one that cannot be read
        # (it overflowed), is not taken.
        for to in (int(later.timestamp()) + 3600, int(later.timestamp()) + 86400 * 365, 10**30):
            hass.bus.async_fire(
                "mobile_app_notification_action", {"action": f"SPOTNAV_LATER_DEPARTURE_{charger}_{stamp}_{to}"}
            )
            await hass.async_block_till_done()
            assert controller.later_departure_for(charger) is None, to
        assert controller.async_later_departure(charger, for_departure=stamp, to=10**30) == ERROR_NO_LATER_DEPARTURE
        # The press moves this departure to the time told.
        hass.bus.async_fire("mobile_app_notification_action", {"action": actions[1]["action"]})
        await hass.async_block_till_done()
        assert controller.later_departure_for(charger) == (world.departure, later)


def test_the_buttons_word_in_swedish_with_avresa() -> None:
    from custom_components.spotnav.notifications.messages import does_not_fit_choice_text

    assert does_not_fit_choice_text("sv", "later", time="14:00") == "Senare avresa (14:00)"
    assert does_not_fit_choice_text("en", "later", time="14:00") == "Later departure (14:00)"


async def test_a_plan_made_again_in_the_background_that_fails_is_logged(hass: HomeAssistant, caplog) -> None:
    """The pre-release review (2026-11-22): `_replan`'s recalculation was fire-and-forget, its failure lost."""
    from types import SimpleNamespace

    import pytest

    from custom_components.spotnav.site import site_capacity_controller as module

    async def fails() -> None:
        raise RuntimeError("no prices")

    recomputes: list[bool] = []
    site = SimpleNamespace(hass=hass, entry_id="site_1", _recompute=lambda: recomputes.append(True))
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(module, "preview_for", lambda _hass, _id: SimpleNamespace(async_recalculate=fails))
        SiteCapacityController._replan(site, "charger_1")  # type: ignore[arg-type]
        await hass.async_block_till_done(wait_background_tasks=True)
    assert recomputes == [True]
    failed = [record for record in caplog.records if "could not be made again" in record.getMessage()]
    assert len(failed) == 1 and failed[0].exc_info is not None and "charger_1" in failed[0].getMessage()
