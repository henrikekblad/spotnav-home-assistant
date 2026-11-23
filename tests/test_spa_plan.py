"""The deadline heat loads' planner, the spa and the sauna (`spa/plan.py`, spec section 5.2 and the owner's decision of
2026-11-23 on the sauna)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from custom_components.spotnav.spa.config import Bath
from custom_components.spotnav.spa.plan import (
    SAUNA,
    SPA,
    DeadlineInputs,
    HeatHour,
    learnt_rate,
    plan_deadline,
    sessions_ahead,
)

TZ = "Europe/Stockholm"
# Friday 27 November 2026, 10:00 local (09:00 UTC).
NOW = datetime(2026, 11, 27, 9, 0, tzinfo=timezone.utc)
BATH = datetime(2026, 11, 27, 18, 0, tzinfo=timezone.utc)  # 19:00 local


def hours(prices: dict[int, float] | None = None, *, room: float | None = None, ceiling: str | None = None) -> list[HeatHour]:
    prices = prices or {}
    return [
        HeatHour(NOW + timedelta(hours=i), prices.get(i, 100.0 + i), 40.0, None, room, ceiling) for i in range(36)
    ]


def spa(**overrides) -> DeadlineInputs:
    base = dict(
        now=NOW, tz=TZ, profile=SPA, target_c=38.0, current_c=35.0, rate_c_h=1.5, kw=3.0,
        sessions=[(BATH, "weekly")], hours=hours(), hold_c=30.0, floor_c=10.0,
    )
    base.update(overrides)
    return DeadlineInputs(**base)  # type: ignore[arg-type]


def test_weekly_and_one_off_sessions_ahead() -> None:
    weekly = (Bath((4, 5), "19:00"),)
    once = datetime(2026, 11, 28, 8, 0, tzinfo=timezone.utc)
    found = sessions_ahead(weekly, once, NOW, TZ)
    assert found == [(BATH, "weekly"), (once, "once"), (BATH + timedelta(days=1), "weekly")]
    # Beyond 36 hours: not yet.
    assert sessions_ahead((Bath((0,), "19:00"),), None, NOW, TZ) == []


def test_the_rate_is_learnt_without_the_first_half_degree() -> None:
    start = NOW
    runs = [
        (start, start + timedelta(hours=2), 30.0, 34.5),  # (4.5 - 0.5) / 2 = 2.0
        (start, start + timedelta(hours=1), 30.0, 30.6),  # under a degree: not counted
        (start, start + timedelta(hours=1), 30.0, 32.5),  # 2.0
    ]
    assert learnt_rate(runs, 1.5) == (2.0, True)
    assert learnt_rate([], 1.5) == (1.5, False)


def test_the_spa_heats_in_the_cheapest_quarters_of_a_bounded_window_and_is_ready_by_the_bath() -> None:
    # 3 °C at 1.5 °C/h: 2 h; the window opens 5 h before the bath (2·need + 1 h): 14:00 local, not the cheaper morning.
    plan = plan_deadline(spa(hours=hours({1: 1.0, 2: 1.0, 4: 10.0, 5: 12.0, 7: 5.0})))
    assert plan.need_h == 2.0 and len(plan.quarters) == 8
    chosen = {moment.replace(minute=0) for moment in plan.quarters}
    assert chosen == {NOW + timedelta(hours=h) for h in (4, 7)}
    assert all(moment < BATH for moment in plan.quarters)
    assert plan.ready_at == NOW + timedelta(hours=8)
    assert plan.not_ready is None and plan.action == "idle" and plan.reason == "waiting"
    assert plan.hold_until == BATH + timedelta(hours=2)


def test_the_floor_heats_at_once_whatever_the_plan() -> None:
    plan = plan_deadline(spa(current_c=9.0, sessions=[]))
    assert (plan.action, plan.reason, plan.state) == ("floor", "floor", "heating")


def test_bath_now_heats_at_once_and_holds() -> None:
    plan = plan_deadline(spa(now_until=NOW + timedelta(hours=4), sessions=[]))
    assert plan.next_session == (NOW, "now")
    assert plan.action == "heat" and plan.reason == "now" and plan.state == "heating"
    assert plan.ready_at == NOW + timedelta(hours=2)


def test_not_ready_for_too_little_time_or_too_little_room_and_the_later_bath() -> None:
    short = plan_deadline(spa(current_c=30.0, sessions=[(NOW + timedelta(hours=2), "once")]))
    assert short.not_ready is not None and short.not_ready.cause == "rate"
    assert short.not_ready.reach_c == 33.0 and short.not_ready.short_c == 5.0
    assert short.not_ready.later is not None and short.not_ready.later >= NOW + timedelta(hours=6)
    # The import target leaves no room before the bath, room again from 20:00 local.
    tight = [
        HeatHour(h.start, h.buy, h.sell, None, 1.0 if h.start < BATH + timedelta(hours=1) else 10.0, "import_target")
        for h in hours()
    ]
    full = plan_deadline(spa(hours=tight))
    assert full.not_ready is not None and full.not_ready.cause == "import_target"
    assert full.not_ready.later is not None and full.not_ready.later > BATH
    # Allowing the peak lifts the import target, never the fuse; taking what fits is not reported again.
    assert plan_deadline(spa(hours=tight, accept_peak=frozenset({BATH}))).not_ready is None
    assert plan_deadline(spa(hours=tight, took_what_fits=frozenset({BATH}))).not_ready is None
    fuse = [HeatHour(h.start, h.buy, h.sell, None, 1.0, "fuse") for h in hours()]
    blocked = plan_deadline(spa(hours=fuse, accept_peak=frozenset({BATH})))
    assert blocked.not_ready is not None and blocked.not_ready.cause == "fuse" and blocked.quarters == {}
    # A bath moved later is planned at its new time.
    moved = plan_deadline(spa(later={BATH: BATH + timedelta(hours=3)}))
    assert moved.next_session == (BATH + timedelta(hours=3), "weekly")


def test_the_sauna_heats_in_one_block_ending_at_the_session_and_is_off_between() -> None:
    session = NOW + timedelta(hours=8)
    plan = plan_deadline(
        DeadlineInputs(
            now=NOW, tz=TZ, profile=SAUNA, target_c=80.0, current_c=20.0, rate_c_h=SAUNA.seed_rate_c_h, kw=9.0,
            sessions=[(session, "weekly")], hours=hours({1: 1.0, 2: 1.0}),
        )
    )
    # 60 °C at 80 °C/h: 45 minutes, right before the session, not in the cheap hours.
    assert sorted(plan.quarters) == [session - timedelta(minutes=45), session - timedelta(minutes=30), session - timedelta(minutes=15)]
    assert plan.action == "off" and plan.reason == "waiting"
    # Nothing booked: nothing at all.
    idle = plan_deadline(
        DeadlineInputs(
            now=NOW, tz=TZ, profile=SAUNA, target_c=80.0, current_c=20.0, rate_c_h=80.0, kw=9.0, sessions=[], hours=hours()
        )
    )
    assert idle.quarters == {} and (idle.action, idle.reason) == ("off", "no_session")
    # The device's own longest run ends the hold.
    capped = plan_deadline(
        DeadlineInputs(
            now=NOW, tz=TZ, profile=SAUNA, target_c=80.0, current_c=20.0, rate_c_h=80.0, kw=9.0,
            sessions=[(session, "weekly")], hours=hours(), max_on=timedelta(hours=1),
        )
    )
    assert capped.hold_until == session - timedelta(minutes=45) + timedelta(hours=1)
