"""The shed order under the ceiling (`site/shed_order.py`, plan `plans/v3_step3c_shed_order.md`): pure."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from custom_components.spotnav.site.shed_order import (
    combine,
    CREDIT_S,
    CREDIT_TAIL_S,
    ENTER_S,
    LOAD_HEAT,
    LOAD_WATER_HEATER,
    Offer,
    Projection,
    RELEASE_MARGIN_W,
    RELEASE_S,
    REST_S,
    ShedOrder,
    Step,
    TAKE_WITHIN_S,
    TURN_GRACE_S,
)

HOUR = datetime(2026, 11, 22, 17, 0, tzinfo=timezone.utc)
HEATER = Offer(live=True, ready=True, give_w=3000.0)
HEAT = Offer(live=True, ready=True, give_w=1500.0)
HELD_HEAT = Offer(live=True, ready=True, give_w=1500.0, held=True)


def _projection(
    now: datetime, *, house: float, held: float = 0.0, unheld: float = 0.0, rate: float = 8000.0, quarter: bool = False
) -> Projection:
    start = now.replace(minute=(now.minute // 15) * 15 if quarter else 0, second=0, microsecond=0)
    period = 900.0 if quarter else 3600.0
    end = start + timedelta(seconds=period)
    return Projection(
        period_start=start,
        period_end=end,
        period_s=period,
        remaining_s=(end - now).total_seconds(),
        rate_w=rate,
        house_w=house,
        cars_held_w=held,
        cars_unheld_w=unheld,
    )


def following(order: ShedOrder) -> dict[str, Offer | None]:
    """The loads as their controls follow the order: a live step is held (the heater off, the heat lowered)."""
    heater = order.steps.get(LOAD_WATER_HEATER)
    heat = order.steps.get(LOAD_HEAT)
    return {
        LOAD_WATER_HEATER: HEATER if heater is None or not heater.live else Offer(True, True, 0.0, drawing=False, held=True),
        LOAD_HEAT: HEAT if heat is None or not heat.live else Offer(True, True, 1500.0, held=True),
    }


def _run(order: ShedOrder, start: datetime, seconds: float, step_s: float = 5.0, **kwargs: object) -> tuple[datetime, float]:
    """Passes every `step_s` for `seconds`; `kwargs` are `_projection`'s and `offers` (fixed; else the loads follow
    the order, `following`)."""
    fixed = kwargs.pop("offers", None)
    now, credit = start, 0.0
    elapsed = 0.0
    while elapsed <= seconds:
        offers = following(order) if fixed is None else fixed
        credit = order.update(now, _projection(now, **kwargs), offers)  # type: ignore[arg-type]
        now += timedelta(seconds=step_s)
        elapsed += step_s
    return now, credit


def test_nothing_is_shed_while_the_period_fits() -> None:
    order = ShedOrder()
    _run(order, HOUR + timedelta(minutes=5), 600, house=2000, held=5000)
    assert order.steps == {}
    assert order.credit_w == 0.0


def test_the_water_heater_goes_first_and_is_counted_ahead_for_the_car() -> None:
    order = ShedOrder()
    now = HOUR + timedelta(minutes=5)
    # House 4 kW (the heater's 3 kW in it) + a car wanting 6 kW against 8 kW: over by 2 kW.
    credit = order.update(now, _projection(now, house=4000, held=6000), {LOAD_WATER_HEATER: HEATER, LOAD_HEAT: HEAT})
    # Not shed yet (the enter delay), and nothing counted before the step.
    assert order.steps == {}
    assert credit == 0.0
    now, credit = _run(order, now, ENTER_S, house=4000, held=6000, offers={LOAD_WATER_HEATER: HEATER, LOAD_HEAT: HEAT})
    assert set(order.steps) == {LOAD_WATER_HEATER}
    step = order.steps[LOAD_WATER_HEATER]
    assert step.live and step.give_w == 3000.0
    assert step.until == HOUR + timedelta(hours=1)
    assert order.until(LOAD_WATER_HEATER, now) == HOUR + timedelta(hours=1)
    # Heat is not touched: the car covers the rest.
    assert LOAD_HEAT not in order.steps
    assert credit == 3000.0


def test_the_credit_ends_once_the_pause_reads_back_or_after_its_time() -> None:
    order = ShedOrder()
    now, _ = _run(order, HOUR + timedelta(minutes=5), ENTER_S, house=4000, held=6000)
    off = Offer(live=True, ready=True, give_w=0.0, drawing=False)
    assert order.update(now, _projection(now, house=1000, held=6000), {LOAD_WATER_HEATER: off, LOAD_HEAT: HEAT}) == 0.0
    # A pause that does not read back is counted no longer than `CREDIT_S`.
    order = ShedOrder()
    now, _ = _run(order, HOUR + timedelta(minutes=5), ENTER_S, house=4000, held=6000)
    now, credit = _run(order, now, CREDIT_S + 10, house=4000, held=6000)
    assert credit == 0.0


def test_no_credit_in_the_period_s_last_minutes() -> None:
    order = ShedOrder()
    now = HOUR + timedelta(minutes=52)
    assert order.update(now, _projection(now, house=4000, held=6000), {LOAD_WATER_HEATER: HEATER}) == 0.0
    # A quarter's last 2.5 minutes too.
    order = ShedOrder()
    now = HOUR + timedelta(minutes=13)
    assert order.update(now, _projection(now, house=4000, held=6000, quarter=True), {LOAD_WATER_HEATER: HEATER}) == 0.0


def test_heat_only_when_even_the_paused_car_cannot_cover() -> None:
    order = ShedOrder()
    now = HOUR + timedelta(minutes=5)
    # Over with the car; the heater is paused: the house alone (1 kW after the heater) fits.
    now, _ = _run(order, now, ENTER_S, house=4000, held=6000)
    now, _ = _run(order, now, 600, house=1000, held=8000, offers={LOAD_WATER_HEATER: Offer(True, True, 0.0, False), LOAD_HEAT: HEAT})
    assert set(order.steps) == {LOAD_WATER_HEATER}
    # The house alone goes over the rate: heat goes down one step.
    now, _ = _run(order, now, ENTER_S, house=9000, held=6000, offers={LOAD_WATER_HEATER: Offer(True, True, 0.0, False), LOAD_HEAT: HEAT})
    assert set(order.steps) == {LOAD_WATER_HEATER, LOAD_HEAT}
    assert order.steps[LOAD_HEAT].until >= order.steps[LOAD_HEAT].since + timedelta(minutes=45)


def test_heat_waits_for_the_water_heater() -> None:
    order = ShedOrder()
    now = HOUR + timedelta(minutes=5)
    # Over beyond the cars from the start: the water heater still goes first.
    order.update(now, _projection(now, house=12000), {LOAD_WATER_HEATER: HEATER, LOAD_HEAT: HEAT})
    order.update(now + timedelta(seconds=ENTER_S), _projection(now, house=12000), {LOAD_WATER_HEATER: HEATER, LOAD_HEAT: HEAT})
    assert set(order.steps) == {LOAD_WATER_HEATER}
    # Heat only once the beyond-the-cars measure, the heater's give still in the reading taken off, stays over.
    later = now + timedelta(seconds=ENTER_S + 5)
    order.update(later, _projection(later, house=12000), {LOAD_WATER_HEATER: HEATER, LOAD_HEAT: HEAT})
    assert LOAD_HEAT not in order.steps
    _run(order, later, ENTER_S, house=12000)
    assert LOAD_HEAT in order.steps


def test_without_a_water_heater_to_give_heat_follows_the_car() -> None:
    order = ShedOrder()
    nothing = Offer(live=True, ready=False, give_w=3000.0, why="full_heat")
    _run(order, HOUR + timedelta(minutes=5), ENTER_S, house=9000, held=6000, offers={LOAD_WATER_HEATER: nothing, LOAD_HEAT: HEAT})
    assert set(order.steps) == {LOAD_HEAT}
    assert order.blocked.get(LOAD_WATER_HEATER) == "full_heat"


def test_put_back_in_reverse_order_with_a_margin_and_a_delay() -> None:
    order = ShedOrder()
    off = Offer(True, True, 0.0, False)
    offers = {LOAD_WATER_HEATER: off, LOAD_HEAT: HELD_HEAT}
    now = HOUR + timedelta(minutes=5)
    now, _ = _run(order, now, ENTER_S, house=4000, held=6000)
    now, _ = _run(order, now, ENTER_S, house=9000, held=6000, offers=offers)
    assert set(order.steps) == {LOAD_WATER_HEATER, LOAD_HEAT}
    # Under, but not by the heat's give plus the margin: nothing goes back.
    under = 8000 - 1500 - RELEASE_MARGIN_W + 100
    now, _ = _run(order, now, RELEASE_S + 60, house=under, held=6000, offers=offers)
    assert set(order.steps) == {LOAD_WATER_HEATER, LOAD_HEAT}
    # Room for the heat back with the margin: heat goes back first, after `RELEASE_S`, the heater stays.
    room = 8000 - 1500 - RELEASE_MARGIN_W - 200
    now, _ = _run(order, now, RELEASE_S - 30, house=room, held=6000, offers=offers)
    assert LOAD_HEAT in order.steps
    now, _ = _run(order, now, 60, house=room, held=6000, offers=offers)
    assert set(order.steps) == {LOAD_WATER_HEATER}
    # The heater back needs the cars and its own give to fit with the margin.
    now, _ = _run(order, now, RELEASE_S + 60, house=1000, held=6000, offers=offers)
    assert set(order.steps) == {LOAD_WATER_HEATER}
    now, _ = _run(order, now, RELEASE_S + 10, house=1000, held=3000, offers=offers)
    assert order.steps == {}
    events = [(item["load"], item["what"]) for item in order.log]
    assert events == [
        (LOAD_WATER_HEATER, "shed"),
        (LOAD_HEAT, "shed"),
        (LOAD_HEAT, "put_back"),
        (LOAD_WATER_HEATER, "put_back"),
    ]


def test_a_hard_rule_ends_the_step_at_once_and_it_is_not_taken_again_in_the_period() -> None:
    order = ShedOrder()
    now, _ = _run(order, HOUR + timedelta(minutes=5), ENTER_S, house=4000, held=6000)
    assert LOAD_WATER_HEATER in order.steps
    forced = Offer(live=True, ready=False, give_w=3000.0, why="max_off")
    order.update(now, _projection(now, house=4000, held=6000), {LOAD_WATER_HEATER: forced, LOAD_HEAT: HEAT})
    assert LOAD_WATER_HEATER not in order.steps
    assert order.until(LOAD_WATER_HEATER, now) is None
    # Ready again later in the same period: no second pause (no flapping).
    now, credit = _run(order, now, 600, house=4000, held=6000)
    assert LOAD_WATER_HEATER not in order.steps
    assert credit == 0.0
    # The next period may pause it again.
    _run(order, HOUR + timedelta(hours=1, minutes=1), ENTER_S, house=4000, held=6000)
    assert LOAD_WATER_HEATER in order.steps


def test_a_cold_room_ends_the_heat_step_at_once() -> None:
    order = ShedOrder()
    nothing = Offer(live=True, ready=False, give_w=0.0)
    now, _ = _run(order, HOUR + timedelta(minutes=5), ENTER_S, house=9000, offers={LOAD_WATER_HEATER: nothing, LOAD_HEAT: HEAT})
    assert LOAD_HEAT in order.steps
    now, _ = _run(order, now, 10, house=9000, offers={LOAD_WATER_HEATER: nothing, LOAD_HEAT: HELD_HEAT})
    assert order.steps[LOAD_HEAT].taken
    cold = Offer(live=True, ready=False, give_w=0.0, why="room_cold")
    order.update(now, _projection(now, house=9000), {LOAD_WATER_HEATER: nothing, LOAD_HEAT: cold})
    assert order.steps == {}
    assert order.log[-1]["rule"] == "room_cold"


def test_watched_loads_only_would() -> None:
    order = ShedOrder()
    watched = Offer(live=False, ready=True, give_w=3000.0)
    now, credit = _run(order, HOUR + timedelta(minutes=5), ENTER_S, house=4000, held=6000, offers={LOAD_WATER_HEATER: watched})
    step = order.steps[LOAD_WATER_HEATER]
    assert not step.live
    assert step.as_dict()["state"] == "would"
    # Nothing written, nothing counted.
    assert credit == 0.0
    assert order.until(LOAD_WATER_HEATER, now) is None
    assert order.log[-1]["what"] == "would_shed"


def test_a_watched_water_heater_does_not_hold_back_live_heat() -> None:
    order = ShedOrder()
    watched = Offer(live=False, ready=True, give_w=3000.0)
    _run(order, HOUR + timedelta(minutes=5), ENTER_S * 2 + 10, house=12000, offers={LOAD_WATER_HEATER: watched, LOAD_HEAT: HEAT})
    assert not order.steps[LOAD_WATER_HEATER].live
    assert order.steps[LOAD_HEAT].live


def test_nothing_is_shed_without_the_hold() -> None:
    """The fuse-only case: no import target, no power level (or load balancing off): every step is put back at once
    (`clear`), and nothing is taken while it lasts."""
    order = ShedOrder()
    now, _ = _run(order, HOUR + timedelta(minutes=5), ENTER_S + 10, house=4000, held=6000)
    assert order.steps
    order.clear(now, "not_held")
    assert order.steps == {}
    for _ in range(30):
        now += timedelta(seconds=10)
        assert order.update(now, None, {LOAD_WATER_HEATER: HEATER}) == 0.0
    assert order.steps == {}


def test_a_passing_loss_of_the_budget_holds_the_steps_for_a_while() -> None:
    """No reading, a gap, the house or the battery unknown for a moment: the steps hold `TURN_GRACE_S`, nothing is
    counted meanwhile, and only a longer loss puts them back."""
    order = ShedOrder()
    now, _ = _run(order, HOUR + timedelta(minutes=5), ENTER_S + 10, house=4000, held=6000)
    assert order.steps[LOAD_WATER_HEATER].taken
    assert order.update(now, None, {}) == 0.0
    assert LOAD_WATER_HEATER in order.steps
    assert order.until(LOAD_WATER_HEATER, now) == HOUR + timedelta(hours=1)
    now, _ = _run(order, now + timedelta(seconds=30), 30, house=4000, held=6000)
    assert LOAD_WATER_HEATER in order.steps
    # A long loss: put back.
    for second in range(0, int(TURN_GRACE_S) + 10, 10):
        order.update(now + timedelta(seconds=second), None, {})
    assert order.steps == {}


def test_a_kettle_does_not_shed() -> None:
    order = ShedOrder()
    now = HOUR + timedelta(minutes=5)
    for second in range(0, 600, 5):
        # Over for 30 s every two minutes.
        house = 9000 if second % 120 < 30 else 2000
        order.update(now + timedelta(seconds=second), _projection(now, house=house), {LOAD_WATER_HEATER: HEATER, LOAD_HEAT: HEAT})
    assert order.steps == {}


def test_no_flapping_at_the_edge() -> None:
    """A load hovering at the ceiling: one pause, one return, never a switching per look."""
    order = ShedOrder()
    now = HOUR + timedelta(minutes=1)
    switchings = 0
    live = False
    for second in range(0, 3300, 5):
        moment = now + timedelta(seconds=second)
        paused = LOAD_WATER_HEATER in order.steps
        # The heater's 3 kW out of the house while paused; the rest wobbles around the rate.
        wobble = 300 if (second // 20) % 2 else -300
        house = 5000 + wobble - (3000 if paused else 0)
        offer = Offer(True, True, 0.0 if paused else 3000.0, not paused)
        order.update(moment, _projection(moment, house=house, held=3000), {LOAD_WATER_HEATER: offer})
        if (LOAD_WATER_HEATER in order.steps) != live:
            live = not live
            switchings += 1
    assert switchings <= 2


def test_the_period_s_turn_holds_the_step_until_a_pass_looks() -> None:
    order = ShedOrder()
    now, _ = _run(order, HOUR + timedelta(minutes=50), ENTER_S, house=4000, held=6000)
    end = HOUR + timedelta(hours=1)
    assert order.until(LOAD_WATER_HEATER, end + timedelta(seconds=5)) == end + timedelta(seconds=5 + TURN_GRACE_S)
    assert order.until(LOAD_WATER_HEATER, end + timedelta(seconds=TURN_GRACE_S + 1)) is None
    # A pass in the new period moves it to the new period's end.
    order.update(end + timedelta(seconds=3), _projection(end + timedelta(seconds=3), house=4000, held=6000), {LOAD_WATER_HEATER: Offer(True, True, 0.0, False)})
    assert order.until(LOAD_WATER_HEATER, end + timedelta(seconds=4)) == end + timedelta(hours=1)


def test_combine_heat_parts() -> None:
    pump_watched = Offer(live=False, ready=True, give_w=1000.0)
    zone = Offer(live=True, ready=True, give_w=400.0)
    cold_zone = Offer(live=True, ready=False, give_w=600.0, why="zone_cold")
    assert combine([None]) is None
    found = combine([pump_watched, zone, cold_zone])
    assert found is not None and found.live and found.ready and found.give_w == 400.0
    found = combine([pump_watched, cold_zone])
    assert found is not None and found.live and not found.ready and found.why == "zone_cold"
    found = combine([pump_watched, None])
    assert found is not None and not found.live and found.ready and found.give_w == 1000.0


def test_a_load_rests_after_its_step_across_the_period_s_turn() -> None:
    order = ShedOrder()
    off = Offer(True, True, 0.0, False)
    now, _ = _run(order, HOUR + timedelta(minutes=48), ENTER_S, house=4000, held=6000)
    assert LOAD_WATER_HEATER in order.steps
    # Put back just before the hour's end.
    now, _ = _run(order, now, RELEASE_S + 5, house=1000, held=1000, offers={LOAD_WATER_HEATER: off})
    assert order.steps == {}
    ended = now
    # Over again right after the turn: not shed (nor counted ahead) until `REST_S` after it was put back.
    turn = HOUR + timedelta(hours=1, minutes=1)
    moment, credit = _run(order, turn, 120, house=4000, held=6000)
    assert order.steps == {} and credit == 0.0
    _run(order, ended + timedelta(seconds=REST_S), ENTER_S + 5, house=4000, held=6000)
    assert LOAD_WATER_HEATER in order.steps


# -- the safety review's points


def test_a_heat_step_starts_a_lowering_late_in_the_hour_and_in_a_quarter() -> None:
    """The heat control's own rules start the lowering the step asks for at its next look: after the hour's minute 15
    and within a quarter too (at least `MIN_LOWER` left)."""
    from custom_components.spotnav.heat.relay_control import Situation, decide

    for start, quarter in ((HOUR + timedelta(minutes=30), False), (HOUR + timedelta(minutes=50), False), (HOUR + timedelta(minutes=3), True)):
        order = ShedOrder()
        nothing = Offer(live=True, ready=False, give_w=0.0)
        now, _ = _run(order, start, ENTER_S, house=12000, quarter=quarter, offers={LOAD_WATER_HEATER: nothing, LOAD_HEAT: HEAT})
        look = now + timedelta(seconds=30)
        until = order.until(LOAD_HEAT, look)
        decision = decide(
            Situation(now=look, plan_until=until, held_since=None, last_end=None, lowered_24h=timedelta(0), writes_today=0)
        )
        assert decision.closed, (start, quarter, decision)


def test_a_step_its_control_does_not_take_ends_without_a_word() -> None:
    order = ShedOrder()
    nothing = Offer(live=True, ready=False, give_w=0.0)
    # The heat control never lowers (its look found a reason not to): the step is dropped, never logged.
    now, _ = _run(order, HOUR + timedelta(minutes=5), ENTER_S + TAKE_WITHIN_S + 5, house=12000, offers={LOAD_WATER_HEATER: nothing, LOAD_HEAT: HEAT})
    assert LOAD_HEAT not in order.steps
    assert list(order.log) == []


def test_an_untaken_heat_step_does_not_hold_back_the_water_heater_s_return() -> None:
    order = ShedOrder()
    now, _ = _run(order, HOUR + timedelta(minutes=5), ENTER_S + 10, house=4000, held=6000)
    assert order.steps[LOAD_WATER_HEATER].taken
    # A heat step not yet held, and room for the heater back: the heater goes back all the same.
    order.steps[LOAD_HEAT] = Step(LOAD_HEAT, True, now, now + timedelta(hours=1), 1500.0, HOUR)
    paused = Offer(True, True, 0.0, drawing=False, held=True)
    for second in range(0, int(RELEASE_S) + 10, 5):
        moment = now + timedelta(seconds=second)
        if LOAD_HEAT in order.steps:
            order.steps[LOAD_HEAT].since = moment  # still waiting for its control, never dropped here
        order.update(moment, _projection(moment, house=1000, held=1000), {LOAD_WATER_HEATER: paused, LOAD_HEAT: HEAT})
    assert LOAD_WATER_HEATER not in order.steps


def test_the_credit_is_once_per_period_and_bounded_whatever_the_excess_does() -> None:
    order = ShedOrder()
    credited = 0
    for second in range(0, 3000, 10):
        moment = HOUR + timedelta(seconds=second)
        over = (second // 20) % 2 == 0
        offers = following(order)
        offers[LOAD_WATER_HEATER] = HEATER  # its switch never reads off
        if order.update(moment, _projection(moment, house=2000, held=7000 if over else 5000), offers) > 0:
            credited += 10
    assert credited <= CREDIT_S
    # Steady over, a heater that never takes the pause: counted at most `CREDIT_S`, then the step is dropped.
    order = ShedOrder()
    credited = 0
    for second in range(0, 3000, 10):
        moment = HOUR + timedelta(seconds=second)
        if order.update(moment, _projection(moment, house=4000, held=6000), {LOAD_WATER_HEATER: HEATER}) > 0:
            credited += 10
    assert 0 < credited <= CREDIT_S
    assert LOAD_WATER_HEATER not in order.steps


def test_the_credit_keeps_a_short_tail_for_a_slow_meter() -> None:
    order = ShedOrder()
    now, _ = _run(order, HOUR + timedelta(minutes=5), ENTER_S, house=4000, held=6000, offers={LOAD_WATER_HEATER: HEATER})
    off = Offer(True, True, 0.0, drawing=False, held=True)
    # Read off, the meter not down yet: still counted.
    assert order.update(now, _projection(now, house=4000, held=6000), {LOAD_WATER_HEATER: off}) == 3000.0
    # The meter shows the heater gone: no longer.
    assert order.update(now + timedelta(seconds=10), _projection(now, house=1200, held=6000), {LOAD_WATER_HEATER: off}) == 0.0
    # A meter that never shows it: the tail ends after `CREDIT_TAIL_S`.
    order = ShedOrder()
    now, _ = _run(order, HOUR + timedelta(minutes=5), ENTER_S, house=4000, held=6000, offers={LOAD_WATER_HEATER: HEATER})
    assert order.update(now, _projection(now, house=4000, held=6000), {LOAD_WATER_HEATER: off}) > 0
    later = now + timedelta(seconds=CREDIT_TAIL_S + 5)
    assert order.update(later, _projection(later, house=4000, held=6000), {LOAD_WATER_HEATER: off}) == 0.0


def test_the_heater_is_shed_only_where_its_window_still_covers_its_need() -> None:
    order = ShedOrder()
    end = HOUR + timedelta(hours=1)
    short = Offer(True, True, 3000.0, until_max=end - timedelta(minutes=10))
    _run(order, HOUR + timedelta(minutes=5), ENTER_S + 10, house=4000, held=6000, offers={LOAD_WATER_HEATER: short})
    assert order.steps == {}
    # Taken where it covers, ended once it no longer does (it drew less than planned meanwhile, say).
    enough = Offer(True, True, 3000.0, until_max=end + timedelta(hours=2))
    now, _ = _run(order, HOUR + timedelta(minutes=10), ENTER_S, house=4000, held=6000, offers={LOAD_WATER_HEATER: enough})
    assert LOAD_WATER_HEATER in order.steps
    tight = Offer(True, True, 0.0, drawing=False, held=True, until_max=end - timedelta(minutes=1))
    order.update(now, _projection(now, house=1000, held=6000), {LOAD_WATER_HEATER: tight})
    assert LOAD_WATER_HEATER not in order.steps


def test_nothing_is_taken_for_a_while_after_a_start() -> None:
    start = HOUR + timedelta(minutes=5)
    order = ShedOrder(started_at=start)
    _run(order, start, ENTER_S + 60, house=4000, held=6000)
    assert order.steps == {}
    _run(order, start + timedelta(seconds=REST_S), ENTER_S + 10, house=4000, held=6000)
    assert LOAD_WATER_HEATER in order.steps


def test_no_credit_without_a_draw_the_meter_reads_now() -> None:
    order = ShedOrder()
    now, _ = _run(order, HOUR + timedelta(minutes=5), ENTER_S, house=4000, held=6000, offers={LOAD_WATER_HEATER: HEATER})
    assert LOAD_WATER_HEATER in order.steps
    # Its switch on, its meter lost: nothing counted.
    lost = Offer(True, True, 0.0, drawing=True)
    assert order.update(now, _projection(now, house=4000, held=6000), {LOAD_WATER_HEATER: lost}) == 0.0
