"""A need that leaves only a few minutes' worth for its last quarter-hour: that quarter-hour is bought and charged.

Another charge planner dropped such a last slot, or skipped a period that short when it came, so the target was
missed. Here the need is rounded up to whole quarter-hours (`planner.slots_needed`) whatever *Charge periods* says,
and a plan window of a single quarter-hour is started and stopped like any other: execution has no shortest run.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.spotnav.execution.controller import ChargingPlan
from custom_components.spotnav.planning.planner import AUTO_PERIODS, calculate_plan, energy_per_slot_kwh

from .harness import Session
from .planning import build_request, parse_documents
from .relay import Clock, FakeScheduler, flat_day, serve, SE4, StubTransport, TODAY, TOMORROW

pytestmark = pytest.mark.usefixtures("offline_relay")

#: 10 A on one phase: 0.575 kWh a quarter-hour.
PER_SLOT_KWH = energy_per_slot_kwh(10, 1)
#: Three quarter-hours and three minutes' worth of a fourth.
NEED_KWH = PER_SLOT_KWH * (3 + 3 / 15)
#: 09:58 in Stockholm on the served day.
NOW = datetime(2026, 9, 22, 7, 58, tzinfo=timezone.utc)
#: 10:00, 10:15 and 10:30 are cheap, 12:30 is the cheapest quarter-hour of all, on its own; the rest is dear.
CHEAP_BLOCK = (40, 41, 42)
CHEAP_ALONE = 50


def _prices() -> list[float]:
    prices = [1.0] * 96
    for index in CHEAP_BLOCK:
        prices[index] = 0.1
    prices[CHEAP_ALONE] = 0.05
    return prices


def _local(index: int) -> datetime:
    """The served day's quarter-hour `index`, in UTC."""
    return datetime(2026, 9, 21, 22, 0, tzinfo=timezone.utc) + timedelta(minutes=15 * index)


def _request(max_periods: int | None, *, departure: str | None = None) -> Any:
    document = json.loads(flat_day(SE4, TODAY))
    document["prices"] = _prices()
    tomorrow = json.loads(flat_day(SE4, TOMORROW, price=1.0))
    return build_request(
        {
            "area_id": SE4,
            "timezone": "Europe/Stockholm",
            "currency": "EUR",
            "major_unit": "€",
            "minor_unit": "cent",
            "now": NOW.isoformat(),
            "phases": 1,
            "amps": 10,
            "requested_kwh": NEED_KWH,
            "consumption_kwh_per_10km": 2.0,
            "max_periods": max_periods,
            "departure": departure,
        },
        parse_documents([document, tomorrow]),
    )


@pytest.mark.parametrize(
    ("max_periods", "expected"),
    [
        (1, (40, 41, 42, 43)),
        (2, (40, 41, 42, CHEAP_ALONE)),
        (AUTO_PERIODS, None),
    ],
    ids=["one-period", "two-periods", "automatic"],
)
def test_the_last_few_minutes_cost_a_whole_quarter_hour_whatever_the_periods(
    max_periods: int | None, expected: tuple[int, ...] | None
) -> None:
    result = calculate_plan(_request(max_periods))
    assert result.reason is None
    assert result.slots_needed == 4 and len(result.slots) == 4, "three and a bit quarter-hours are four"
    assert result.delivered_kwh >= NEED_KWH
    assert all(slot.end - slot.start == timedelta(minutes=15) for slot in result.slots)
    if expected is not None:
        assert tuple(slot.start for slot in result.slots) == tuple(_local(index) for index in expected)
    else:
        # Automatic periods keep every run at least half an hour long: the lone cheap quarter-hour alone is no run,
        # but the fourth quarter-hour is never dropped for that.
        assert all(end - start >= timedelta(minutes=30) for start, end in result.periods)


def test_a_departure_that_leaves_exactly_the_rounded_up_need_still_plans_all_of_it() -> None:
    """10:00 to 11:00 holds four quarter-hours: the fourth, needed for three minutes, is bought, not cut."""
    result = calculate_plan(_request(1, departure="11:00"))
    assert result.reason is None and not result.short_of_deadline
    assert result.periods == ((_local(40), _local(44)),)


# ------------------------------------------------------------------ execution: the lone quarter-hour is charged


async def _until(session: Session, clock: Clock, when: datetime) -> None:
    """Time passes to `when`: every charger timer due by then fires, at its own moment."""
    while True:
        due = sorted(
            (timer for timer in session.timers.pending if timer.when <= when), key=lambda timer: timer.when
        )
        if not due:
            break
        clock.now = max(clock.now, due[0].when)
        due[0].fire()
        await session.hass.async_block_till_done()
    clock.now = when
    await session.hass.async_block_till_done()


@pytest.fixture
def clock() -> Clock:
    return Clock(NOW)


@pytest.fixture(autouse=True)
def frozen_real_clock(monkeypatch: pytest.MonkeyPatch, clock: Clock) -> None:
    """The controller's own plan rules read `dt_util.utcnow()`: pinned to the injected clock."""
    monkeypatch.setattr(dt_util, "utcnow", lambda *_args, **_kwargs: clock())


async def test_a_plan_whose_last_period_is_one_quarter_hour_starts_and_stops_it(
    hass: HomeAssistant,
    transport: StubTransport,
    clock: Clock,
    timers: FakeScheduler,
    install_spy: list[ChargingPlan],
) -> None:
    session = Session(hass, transport, clock, timers)
    await session.start()
    serve(transport)
    transport.serve(transport.day_path(SE4, TOMORROW), 200, flat_day(SE4, TOMORROW, price=1.0))
    document = json.loads(flat_day(SE4, TODAY))
    document["prices"] = _prices()
    transport.serve(transport.day_path(SE4, TODAY), 200, json.dumps(document))

    snapshot = await session.set_auto(amps=10, phases=1, requested_kwh=round(NEED_KWH, 3), max_periods=2)

    assert snapshot.proposal is not None and snapshot.proposal.slots_needed == 4
    plan = install_spy[-1]
    lone = (_local(CHEAP_ALONE), _local(CHEAP_ALONE + 1))
    assert plan.windows == [(_local(40), _local(43)), lone]

    await _until(session, clock, _local(40) + timedelta(minutes=1))
    assert len(session.on_calls) == 1, "the first period starts"
    hass.states.async_set(session.charge_control, "on")
    await _until(session, clock, _local(43) + timedelta(minutes=1))
    assert len(session.off_calls) == 1, "and ends"
    hass.states.async_set(session.charge_control, "off")

    await _until(session, clock, lone[0] + timedelta(seconds=1))
    assert len(session.on_calls) == 2, "the quarter-hour needed for three minutes is started"
    hass.states.async_set(session.charge_control, "on")
    await _until(session, clock, lone[0] + timedelta(minutes=14))
    assert len(session.off_calls) == 1, "and runs its whole quarter-hour"
    await _until(session, clock, lone[1] + timedelta(seconds=1))
    assert len(session.off_calls) == 2, "then stops at its end"
