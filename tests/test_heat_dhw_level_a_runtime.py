"""Level A on the pilot's site (`heat/dhw_runtime.py`, `heat/runtime.py`): a relay that blocks the compressor, so
hot water too, no boost route (the owner's NIBE). The heat plan's hours before a peak are cut in shadow and shown,
taken out of the plan the control steers by once level A is live (the opt-in and its own gate), each day's cold risks
reviewed, three in a week close the gate again, and the pump's runs the blocks will hold are in the house forecast."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Any

import pytest
from homeassistant.core import HomeAssistant

from custom_components.spotnav.battery.plan_shadow import PriceSlot
from custom_components.spotnav.heat.dhw_peaks import Peak
from custom_components.spotnav.heat.meter_split import HotWaterRun

from .test_heat_api import read
from .test_heat_control import H, MIN, _passed, _site

pytestmark = pytest.mark.usefixtures("offline_relay")

#: Local hour prices: a cheap night, a dear morning and a dear evening (as `test_heat_dhw_level_a.PRICES`).
PRICES = [80, 78, 76, 75, 77, 85, 120, 190, 210, 160, 120, 110, 105, 100, 105, 130, 180, 240, 260, 220, 150, 110, 95, 85]
#: A shower by 06:00 and one by 18:00, the pump reheating after each.
PEAKS = [Peak(time(6), time(8), 0.9, True, time(7, 10)), Peak(time(18), time(21), 0.8, True, time(19, 30))]


def _slots(zone: Any, day: date) -> list[PriceSlot]:
    start = datetime(day.year, day.month, day.day, tzinfo=zone).astimezone(timezone.utc)
    out = []
    for index in range(48):
        hour = start + timedelta(hours=index)
        out.append(PriceSlot(hour, hour + H, float(PRICES[hour.astimezone(zone).hour]), 30.0))
    return out


async def _villa(hass: HomeAssistant, freezer: Any, monkeypatch: pytest.MonkeyPatch, **heat: Any) -> tuple[Any, Any, Any, Any, datetime]:
    site, controller, relay = await _site(hass, freezer, **heat)
    shadow = controller.heat_shadow
    hot_water = shadow.hot_water
    hot_water.peaks = list(PEAKS)
    monkeypatch.setattr(type(hot_water), "_pump", lambda _self: None)
    now = controller.demand.now()
    zone = hot_water.zone
    slots = _slots(zone, now.astimezone(zone).date())
    monkeypatch.setattr(controller.battery_shadow.plan, "buy_slots", lambda _now: slots)
    return site, controller, relay, hot_water, now


def _local(hot_water: Any, now: datetime, hour: int, days: int = 0) -> datetime:
    day = now.astimezone(hot_water.zone).date() + timedelta(days=days)
    return datetime(day.year, day.month, day.day, hour, tzinfo=hot_water.zone).astimezone(timezone.utc)


def _open_a_gate(hot_water: Any, today: date) -> None:
    for index in range(1, 15):
        day = today - timedelta(days=index)
        hot_water.days[day.isoformat()] = {
            "peaks": [], "picks": {}, "live": [],
            "review": {"date": day.isoformat(), "mode": "shadow", "hits": 2, "peaks": 2, "saved": 0.0, "misses": 0},
        }


def _lowered(plan: list[Any]) -> set[datetime]:
    return {hour.start for hour in plan if hour.action == "lower"}


async def test_in_shadow_the_cut_is_shown_and_the_plan_steered_by_keeps_its_block(hass: HomeAssistant, freezer, monkeypatch) -> None:
    site, controller, _relay, hot_water, now = await _villa(hass, freezer, monkeypatch)
    shadow = controller.heat_shadow
    await shadow._async_plan(now)  # noqa: SLF001
    evening = _local(hot_water, now, 17, 1)
    # Tomorrow's 17:00 is the hour before the 18:00 shower: shadow shows it cut, the plan the control steers by still
    # lowers it.
    assert evening in _lowered(shadow.plan)
    tomorrow = [cut for cut in hot_water.cuts if cut.start.astimezone(hot_water.zone).date() == evening.astimezone(hot_water.zone).date()]
    assert [(cut.start, cut.kind, cut.ready) for cut in tomorrow] == [(evening, "peak", _local(hot_water, now, 18, 1))]
    assert hot_water.cuts_live is False
    answer = (await read(hass, api_version=1, site_id=site.entry_id))["result"]
    blocking = answer["hot_water"]["blocking"]
    assert blocking["mode"] == "shadow" and blocking["tank"] is False
    assert blocking["gate"]["level"] == "A" and blocking["gate"]["days_needed"] == 14 and not blocking["gate"]["open"]
    shown = next(cut for cut in blocking["cuts"] if cut["start"] == evening.isoformat())
    assert shown == {"start": evening.isoformat(), "end": (evening + H).isoformat(), "reason": "peak", "peak": "18:00", "live": False}
    assert answer["hot_water"]["level"] == "A"
    # In shadow the plan steered by keeps all five of the day's lowered hours.
    assert len(_lowered(shadow.plan) & {evening + index * H for index in range(-10, 3)}) == 5


async def test_live_behind_the_opt_in_and_level_as_own_gate(hass: HomeAssistant, freezer, monkeypatch) -> None:
    _site_, controller, relay, hot_water, now = await _villa(hass, freezer, monkeypatch)
    shadow = controller.heat_shadow
    config = shadow.config()
    _open_a_gate(hot_water, now.astimezone(hot_water.zone).date())
    # The boost's gate is not level A's: a week of shadow opens neither, two weeks open A only.
    assert hot_water.a_gate_state()["open"] and hot_water.a_mode(config) == "live"
    await shadow._async_plan(now)  # noqa: SLF001
    evening = _local(hot_water, now, 17, 1)
    assert evening not in _lowered(shadow.plan) and evening + H in _lowered(shadow.plan) and hot_water.cuts_live is True
    # Four lowered hours that day (without a tank sensor four at most): 07, 08, 18 and 19.
    assert sorted(_lowered(shadow.plan) & {evening + index * H for index in range(-10, 3)}) == [
        evening - 10 * H, evening - 9 * H, evening + H, evening + 2 * H
    ]
    # The control steers by it: no block at 17:00, the block from 18:00.
    control = controller.heat_control
    _passed(control)
    made = shadow.plan
    monkeypatch.setattr(hot_water, "making_now", lambda: False)
    shadow.made_at = evening - 11 * MIN
    await control.async_look(evening - 11 * MIN)
    shadow.plan, shadow.made_at = made, evening
    await control.async_look(evening + 5 * MIN)
    assert relay.calls == [] and control.status()["reason"] == "normal"
    shadow.made_at = evening + H
    await control.async_look(evening + H + 5 * MIN)
    assert relay.calls == ["on"]
    # Without the opt-in level A is shadow.
    from dataclasses import replace

    assert hot_water.a_mode(replace(config, control_enabled=False)) == "shadow"


async def test_a_day_s_cold_risk_is_reviewed_and_three_in_a_week_close_the_gate(hass: HomeAssistant, freezer, monkeypatch) -> None:
    _site_, controller, _relay, hot_water, now = await _villa(hass, freezer, monkeypatch)
    shadow = controller.heat_shadow
    zone = hot_water.zone
    today = now.astimezone(zone).date()
    yesterday = today - timedelta(days=1)
    # Yesterday: a block 04:00-06:10 held the reheat to 06:15, after the 06:00 shower's ready time.
    hot_water._keep(_local(hot_water, now, 1, -1))  # noqa: SLF001 - yesterday's peaks kept
    record = hot_water.days[yesterday.isoformat()]
    record["cuts"] = {_local(hot_water, now, 17, -1).isoformat(): ["peak", None, False]}
    hot_water.seen.add(yesterday.isoformat())
    start = _local(hot_water, now, 6, -1) + 15 * MIN
    hot_water.runs[start.isoformat()] = HotWaterRun(start, start + 30 * MIN, 0.9)
    block = (_local(hot_water, now, 4, -1), _local(hot_water, now, 6, -1) + 10 * MIN)
    monkeypatch.setattr(shadow, "relay_intervals", lambda _now: ([block], []))
    monkeypatch.setattr("custom_components.spotnav.api.house_history.period_price_slots", lambda *_args: [])
    await hot_water._async_review(_local(hot_water, now, 4))  # noqa: SLF001
    review = record["review"]
    assert (review["cold_risks"], review["blocks_cut"], review["a_mode"]) == (1, 1, "shadow")
    assert review["per_peak"][0]["cold_risk"] is True and review["per_peak"][1]["cold_risk"] is False


async def _backed_off(hass: HomeAssistant, freezer: Any, monkeypatch: pytest.MonkeyPatch, *, beta: bool = False) -> tuple[Any, Any, Any, datetime]:
    """Level A steering, then three live days with a cold risk each in the last week."""
    from custom_components.spotnav.heat import dhw_runtime

    if beta:
        monkeypatch.setattr(dhw_runtime, "beta_gate_open", lambda *_args: True)
    _site_, controller, _relay, hot_water, now = await _villa(hass, freezer, monkeypatch)
    today = now.astimezone(hot_water.zone).date()
    monkeypatch.setattr("custom_components.spotnav.api.house_history.period_price_slots", lambda *_args: [])
    if not beta:
        _open_a_gate(hot_water, today)
        hot_water.a_opened = (today - timedelta(days=5)).isoformat()
    for index in (1, 2, 3):
        day = (today - timedelta(days=index)).isoformat()
        record = hot_water.days.setdefault(day, {"peaks": [], "picks": {}, "live": []})
        record["review"] = {"date": day, "mode": "shadow", "hits": 2, "peaks": 2, "saved": 0.0, "misses": 0, "a_mode": "live", "cold_risks": 1}
    await hot_water._async_review(_local(hot_water, now, 5))  # noqa: SLF001
    return controller, hot_water, today, now


@pytest.mark.parametrize("beta", [False, True])
async def test_a_back_off_keeps_level_a_and_makes_it_stricter(hass: HomeAssistant, freezer, monkeypatch, beta: bool) -> None:
    """Review S3 (decided): three cold risks in a week never send the relay back to the plan without level A, which
    protects less. Level A stays and widens: three hours before each peak, a day lowering one hour less; its gate
    counts no day until seven clean ones. A beta site backs off as well."""
    controller, hot_water, today, now = await _backed_off(hass, freezer, monkeypatch, beta=beta)
    config = controller.heat_shadow.config()
    assert hot_water.a_strict() and hot_water.a_backed_off == today.isoformat()
    assert hot_water.a_mode(config) == "live"
    tomorrow = today + timedelta(days=1)
    kept, cap = hot_water.level_a_rules(now, config, [tomorrow])
    ready = _local(hot_water, now, 18, 1)
    assert [hour for hour, why in sorted(kept.items()) if why == ("peak", ready)] == [ready - 3 * H, ready - 2 * H, ready - H]
    assert cap == 3
    # The plan steered by keeps level A's cuts.
    await controller.heat_shadow._async_plan(now)  # noqa: SLF001
    assert hot_water.cuts_live is True and ready - H not in _lowered(controller.heat_shadow.plan)
    # A second look the same week does not back off again.
    await hot_water._async_review(_local(hot_water, now, 6))  # noqa: SLF001
    assert hot_water.a_backed_off == today.isoformat()
    # Seven clean days after it: back to the usual rules.
    for index in range(1, 8):
        day = (today + timedelta(days=index)).isoformat()
        hot_water.days[day] = {"peaks": [], "picks": {}, "live": [], "review": {"date": day, "mode": "shadow", "hits": 2, "peaks": 2, "saved": 0.0, "misses": 0, "a_mode": "live", "cold_risks": 0}}
    later = now + timedelta(days=8)
    await hot_water._async_review(later)  # noqa: SLF001
    assert not hot_water.a_strict()
    kept, cap = hot_water.level_a_rules(later, config, [(later + timedelta(days=1)).astimezone(hot_water.zone).date()])
    assert cap == 4


async def test_strict_days_do_not_count_toward_the_gate(hass: HomeAssistant, freezer, monkeypatch) -> None:
    _controller, hot_water, today, _now = await _backed_off(hass, freezer, monkeypatch)
    for index in range(1, 4):
        day = (today + timedelta(days=index)).isoformat()
        hot_water.days[day] = {"review": {"date": day, "mode": "shadow", "hits": 2, "peaks": 2, "a_mode": "shadow", "cold_risks": 0}}
    assert [review["date"] for review in hot_water.a_gate_reviews()] == []


async def test_the_runs_a_block_will_hold_move_in_the_house_forecast(hass: HomeAssistant, freezer, monkeypatch) -> None:
    _site_, controller, _relay, hot_water, now = await _villa(hass, freezer, monkeypatch)
    shadow = controller.heat_shadow
    hot_water.run_len = 30 * MIN
    control = controller.heat_control
    reheat = _local(hot_water, now, 19, 1)
    # A closing 19:00-21:00 ahead holds tomorrow evening's reheat (19:30) to 21:00.
    monkeypatch.setattr(control, "closings_ahead", lambda _now: [(reheat, reheat + 2 * H)])
    delta = shadow.forecast_delta(now)
    # Review S5: the relay's own change already takes `lower_share` of the hour's heat out, hot water included; only
    # the rest of the run is taken out here, the whole run added at the block's end.
    share = shadow.effect.lower_share
    assert 0 < share < 1
    assert delta[reheat] == pytest.approx(-(1 - share) * 0.8)
    assert delta[reheat + 2 * H] == pytest.approx(0.8)
    # No closings ahead (control not running): nothing moves.
    monkeypatch.setattr(control, "closings_ahead", lambda _now: None)
    hot_water._block_key = None  # noqa: SLF001
    assert hot_water.block_delta(now) == {}


async def test_the_kept_cuts_and_level_as_state_survive_a_restart(hass: HomeAssistant, freezer, monkeypatch) -> None:
    import json

    from custom_components.spotnav.heat import dhw_runtime

    _site_, controller, _relay, hot_water, now = await _villa(hass, freezer, monkeypatch)
    await controller.heat_shadow._async_plan(now)  # noqa: SLF001
    hot_water.a_opened = "2026-09-20"
    hot_water._keep(_local(hot_water, now, 17, 1) + 5 * MIN)  # noqa: SLF001
    today = (now.astimezone(hot_water.zone).date() + timedelta(days=1)).isoformat()
    assert list(hot_water.days[today]["cuts"].values())[0][0] == "peak"
    again = dhw_runtime.HotWater(hass, controller.heat_shadow)
    again.restore(json.loads(json.dumps(hot_water.stored())))
    assert again.days[today]["cuts"] == hot_water.days[today]["cuts"] and again.a_opened == "2026-09-20"


async def test_a_path_that_leaves_hot_water_alone_has_no_level_a(hass: HomeAssistant, freezer, monkeypatch) -> None:
    _site_, controller, _relay, hot_water, now = await _villa(
        hass, freezer, monkeypatch, control_path="offset", control_entity="number.nibe_offset"
    )
    config = controller.heat_shadow.config()
    assert hot_water.level_a_rules(now, config, [now.date()]) is None
    answer_cuts = hot_water._blocking_view(now, config, None)  # noqa: SLF001
    assert answer_cuts is None


async def test_a_planned_boost_drops_the_guard_only_once_written_or_filled(hass: HomeAssistant, freezer, monkeypatch) -> None:
    """Review S1: a pick that was skipped (the fuse) or never written (an ended day) keeps the two hours before its
    peak free of blocks; only a boost read back, or a peak already filled, takes the guard away."""
    from types import SimpleNamespace

    from custom_components.spotnav.heat.dhw_plan import Pick

    _site_, controller, _relay, hot_water, now = await _villa(hass, freezer, monkeypatch)
    config = controller.heat_shadow.config()
    monkeypatch.setattr(hot_water, "route", lambda *_args: SimpleNamespace(verified=True))
    monkeypatch.setattr(hot_water, "mode", lambda *_args: "live")
    ready = _local(hot_water, now, 18, 1)
    start = ready - 5 * H
    hot_water.picks = [Pick(start, start + 30 * MIN, 0.8, 100.0, "cheap", ready, ready + 30 * MIN, 150.0, 50.0)]
    tomorrow = ready.astimezone(hot_water.zone).date()

    def guarded() -> bool:
        kept, _cap = hot_water.level_a_rules(now, config, [tomorrow])
        return kept.get(ready - H) == ("peak", ready)

    # Planned (also frozen) but not written: the guard stays.
    assert guarded()
    hot_water.ended_day = tomorrow.isoformat()
    assert guarded()
    # Written and read back: the boost fills the peak.
    hot_water._record(tomorrow.isoformat())["live"] = [ready.isoformat()]  # noqa: SLF001
    assert not guarded()
    # A boost already running when the pick came due fills it too.
    hot_water._record(tomorrow.isoformat())["live"] = []  # noqa: SLF001
    hot_water._record(tomorrow.isoformat())["filled"] = [ready.isoformat()]  # noqa: SLF001
    assert not guarded()


async def test_a_fourteen_day_periodic_increase_is_told_past_the_peaks_window(hass: HomeAssistant, freezer, monkeypatch) -> None:
    """Review S2: the runs for the peaks are kept 22 days, too short to see a 14-day increase three times; the night's
    long runs are kept longer (`dhw_level_a.HYGIENE_KEEP`), also across a restart."""
    import json

    from custom_components.spotnav.heat import dhw_runtime

    _site_, controller, _relay, hot_water, now = await _villa(hass, freezer, monkeypatch)
    config = controller.heat_shadow.config()
    today = now.astimezone(hot_water.zone).date()
    for back in range(50, 0, -1):
        day = today - timedelta(days=back)
        for hour, minutes, kwh in ((6, 30, 0.9), (19, 25, 0.8)):
            start = datetime(day.year, day.month, day.day, hour, tzinfo=hot_water.zone).astimezone(timezone.utc)
            hot_water._keep_run(HotWaterRun(start, start + minutes * MIN, kwh))  # noqa: SLF001
        if back in (43, 29, 15, 1):
            start = datetime(day.year, day.month, day.day, 2, tzinfo=hot_water.zone).astimezone(timezone.utc)
            hot_water._keep_run(HotWaterRun(start, start + 90 * MIN, 3.5))  # noqa: SLF001
    hot_water._trim(now)  # noqa: SLF001
    assert min(run.start for run in hot_water.runs.values()) > now - timedelta(days=23)
    again = dhw_runtime.HotWater(hass, controller.heat_shadow)
    again.restore(json.loads(json.dumps(hot_water.stored())))
    for water in (hot_water, again):
        kept, _cap = water.level_a_rules(now, config, [today])
        increase = datetime(today.year, today.month, today.day, 2, tzinfo=hot_water.zone).astimezone(timezone.utc) + timedelta(days=13)
        assert kept.get(increase) == ("hygiene", None)
