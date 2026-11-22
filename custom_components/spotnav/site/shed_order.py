"""One shed order under the ceiling (plan `plans/v3_step3c_shed_order.md`, step 3c of
`plans/v3_flexible_loads_design.md`, the owner's decisions of 2026-11-22). Pure: no Home Assistant.

When the running period (the hour, or the quarter hour where the tariff counts quarters) is heading over the ceiling
the import target's hold keeps (`site/import_target.py`: the import target or the power level, lifted to the month's
free level, less its margin), the loads give way in one fixed order and come back in the reverse one:

    battery -> water heater -> car -> heat        (put back: heat -> car -> water heater -> battery)

**The projection** is the hold's own, never a second one: its period budget (`HourBudget`) says what the rest of the
period may still import on average (`rate_w`) and what the house draws now without the cars (`house_w`); the hold's
members say what the cars want (`cars_held_w`, the ones the hold may lower or pause) and what it may not touch
(`cars_unheld_w`: a person's Start, the car's minimum level). The period is over when `house + cars > rate`
(`Projection.excess_w`), and over even with every holdable car paused when `house + unheld cars > rate`
(`Projection.beyond_cars_w`).

**Each step** (`ShedOrder.update`, once per hold pass, from `ImportTargetHold.hold`):

1. **Battery.** Nothing new: its help is already in the projection. A battery covering the house lowers the grid's
   reading (`house_w` is the grid less the cars; under `battery_first` its discharge counts as import, as the hold
   counts it). The steps below only go on while the period stays over after that, so the battery's help always
   counts first; its ramp is what the enter delay (`ENTER_S`) waits out.
2. **Water heater** (`heat/water_heater_control.py`, a contactor behind its opt-in): paused for the rest of the period
   while the period is over (`excess_w > 0`) for `ENTER_S` in a row. It gives what its meter reads it drawing now
   (`Offer.give_w`, never a learnt figure); one that gives less than `MIN_GIVE_W` (off, its thermostat satisfied, an
   element without a trusted meter) is no step. Only in a cheap window whose time left after the pause still covers
   what the heater needs in it (`Offer.until_max`): its hard rules win (`water_heater.Rules.want`: full heat every
   20 h, never off 8 h, on without a plan or a reading, the tank low, the day's switchings), and a hard rule ending
   the pause ends the step at once. **Counted ahead for the car**: once the step is taken, while the heater still
   draws and for a short tail after its switch reads off (`CREDIT_TAIL_S`, until the house's reading has fallen by
   half of it), at most `CREDIT_S + CREDIT_TAIL_S` from the step and once per period, the hold counts the heater's
   draw as already gone (`credit_w`, added to what the cars may draw). Never in the period's last minutes
   (`raise_lock_s`: the hour's last 10, a quarter's last 2.5): there the car is held as before, and what the credit
   let through the hold's budget takes back from the cars on its own (it is in the period's import so far). During
   the enter delay nothing is counted: the car is held as before for that minute; but where the hold would pause a
   car for the rest of the period (`Projection.car_pause_due`: the heater's give would keep it above its minimum,
   not a slow or switch-only charger, seen for `import_target_hold.PAUSE_DUE_S` in a row, the car held at its minimum
   meanwhile once a period), the heater's step is taken at once, without the delay: a few minutes' pause of the
   heater is cheaper than a car paused for the hour.
3. **Car**: the hold as before (lower, pause; `share_budget`), unchanged. With the water heater's give counted it
   takes only what remains. In the projection a car counts at what its plan needs now (the plan's own rate in its
   running window, where it has one), never more than the fuse would give it: a car charging faster than its plan
   needs is no reason to pause the water heater.
4. **Heat**: one step lower (the relay or SG Ready contact, the offset, and the controlled zones 1 °C) while the
   period is over even with the cars paused (`beyond_cars_w > 0`) for `ENTER_S`, after the water heater (paused, or
   with nothing to give). Through the heat control's own rules (`heat/relay_control.decide`, `zone_control.decide`):
   at least `MIN_LOWER`, at most 3 h in a row and 6 h a day, the rest between lowerings, the day's writes, never in
   a cold room or below -15 °C outdoors; a safety reason ends it at once. The step lasts at least `MIN_LOWER` and
   `START_MARGIN_S` from when it is taken, so a lowering it asks for is long enough to start. Counted as giving the learnt share of the
   pump's heat (`relay_effect.Effect.lower_share`) and the zones' measured power; no credit (it acts slowly and the
   car is already paused by then).

**Put back in reverse**: heat while `beyond_cars_w + its give` stays under by `RELEASE_MARGIN_W` for `RELEASE_S`; the
car is the hold's own (it raises as the budget allows); then the water heater while `excess_w + its give` does. One
step at a time, heat before the water heater. A step released (or ended by its own rules) is not taken again in the
same period, nor within `REST_S` of its end: a contactor never flaps, and the water heater's planned switchings a day
still count every pause.

**A step must be taken**: a live step whose load's control does not hold it (`Offer.held`) within `TAKE_WITHIN_S`
(`TAKE_PENDING_S` while the control says it is still taking it, `Offer.pending`) ends without a word: it is not logged, not shown (the plan view, the card and the states show only what the controls
hold) and holds no other step back.

**Watched loads** (a water heater without its opt-in, heat control off) take the same steps as `would` only: shown
and logged, never written, and nothing counted (the next step goes on as if they were not there).

**Not acting**: without the hold's budget for good (no import target or power level, outside the level's hours,
load balancing off: `clear`) every step is put back at once and nothing is counted; for a moment (no reading, a gap,
the house or the battery unknown: `update` without a projection) the steps hold, nothing new is taken or counted, and
they are put back only after `TURN_GRACE_S` without a budget. After a start nothing is taken for `REST_S` (what ended
before the restart is not known). The fuse is never this
module's: the hold only ever lowers what the fuse allows, and the credit only gives the cars back part of the
target's own budget, never above the fuse's ceiling. The power tariff's soft ceiling is shadow only
(`site/demand_ceiling.py`) and sheds nothing.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final

from ..heat.relay_control import MIN_LOWER
from .import_target import raise_lock_s

LOAD_BATTERY: Final = "home_battery"
LOAD_WATER_HEATER: Final = "water_heater"
LOAD_CAR: Final = "car"
LOAD_HEAT: Final = "heat"
#: The shed order (fixed in 3.0; a setting may come later).
ORDER: Final = (LOAD_BATTERY, LOAD_WATER_HEATER, LOAD_CAR, LOAD_HEAT)
#: The loads this module steps (the battery and the car are counted, not stepped).
STEPPED: Final = (LOAD_WATER_HEATER, LOAD_HEAT)

#: Why a load is paused or lowered by this module (the plan view, the card, the diagnostics).
REASON_CEILING: Final = "ceiling"

#: How long the period must stay over before a step is taken (two of the heat controls' 30 s looks).
ENTER_S: Final = 60.0
#: How long the period must stay under by the margin, with the step's give put back, before it is put back (ten looks).
RELEASE_S: Final = 300.0
#: How far under (W, the rest of the period's mean) the period must stay with the step's give put back.
RELEASE_MARGIN_W: Final = 500.0
#: How long after a water heater's step its give is still counted for the cars while it reads on: the heater's look
#: (30 s) and its write's read-back (90 s).
CREDIT_S: Final = 120.0
#: How long the credit goes on after the heater's switch reads off, while the house's reading has not yet fallen by
#: half its give (a slow meter) (s).
CREDIT_TAIL_S: Final = 60.0
#: At the period's turn a step holds this much longer while no pass has looked at the new period yet (s); and as long
#: without a budget for a passing reason.
TURN_GRACE_S: Final = 120.0
#: A live step its load's control does not hold within this long is dropped (two and a half of the 30 s looks) (s),
#: and one the control says it is still taking (`Offer.pending`: a slow path) within this long.
TAKE_WITHIN_S: Final = 75.0
TAKE_PENDING_S: Final = 600.0
#: A heat step lasts at least `MIN_LOWER` and this from when it is taken (s): its control's look comes a little later.
START_MARGIN_S: Final = 300.0
#: After a step ends, the same load is not shed again for this long (s), across a period's turn too: a contactor is
#: never switched back and forth within minutes.
REST_S: Final = 900.0
#: A step that gives less than this (W) is not worth a switching.
MIN_GIVE_W: Final = 300.0

STATE_PAUSED: Final = "paused"
STATE_WOULD: Final = "would"

END_UNDER: Final = "under"
END_RULE: Final = "own_rule"
END_OFF: Final = "not_held"

LOG_SIZE: Final = 100


@dataclass(frozen=True, slots=True)
class Offer:
    """What one load can give the shed now, as its own control last saw it.

    `live`: SpotNav steers it (its opt-in); else it is watched and a step is `would` only. `ready`: its own rules let
    it be shed now, or stay shed (a water heater in a cheap window or already paused for the ceiling, heat with no
    safety reason that the rules would lower). `give_w`: what the step gives (W). `drawing`: the load still draws its
    give now (the water heater's switch reads on), what the credit for the cars needs. `why`: the rule that keeps it
    from being shed, where one does."""

    live: bool
    ready: bool
    give_w: float
    drawing: bool = True
    why: str | None = None
    #: The load's control holds it for the ceiling now (the water heater off, the heat lowered).
    held: bool = False
    #: The latest a pause may last and still leave the load its need (the water heater's cheap window), `None`: any.
    until_max: datetime | None = None
    #: The control is taking the step but does not hold it yet (a write awaiting its read-back, SG Ready's other
    #: contact to open first, an offset's write spacing): the step waits `TAKE_PENDING_S` for it instead.
    pending: bool = False

    @property
    def useful(self) -> bool:
        return self.ready and self.give_w >= MIN_GIVE_W


@dataclass(frozen=True, slots=True)
class Projection:
    """The hold's period as the shed reads it (see the module)."""

    period_start: datetime
    period_end: datetime
    period_s: float
    remaining_s: float
    #: What the rest of the period may still import on average (W): the hold's `HourBudget.rate_w`.
    rate_w: float
    #: The house now without the cars (W): the hold's `HourBudget.house_w`.
    house_w: float
    #: What the cars the hold may lower or pause want (W), and what the ones it may not touch draw.
    cars_held_w: float = 0.0
    cars_unheld_w: float = 0.0
    #: The hold would pause a car for the rest of the period this pass (its share fell below its minimum): the water
    #: heater, the earlier step, is then taken without the enter delay.
    car_pause_due: bool = False

    @property
    def excess_w(self) -> float:
        """How far the period is heading over with every car at what it wants (W)."""
        return self.house_w + self.cars_held_w + self.cars_unheld_w - self.rate_w

    @property
    def beyond_cars_w(self) -> float:
        """How far the period is heading over with every car the hold may pause paused (W)."""
        return self.house_w + self.cars_unheld_w - self.rate_w


@dataclass(slots=True)
class Step:
    """A step: the load, live or `would`, since when, until when it holds, what it gives, and whether its control has
    taken it (`taken`: holds it; a `would` step at once)."""

    load: str
    live: bool
    since: datetime
    until: datetime
    give_w: float
    period_start: datetime
    taken: bool = False
    #: The house's reading when it was taken, and when the water heater's switch was first seen off (the credit's tail).
    house_w: float = 0.0
    off_seen_at: datetime | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "state": STATE_PAUSED if self.live else STATE_WOULD,
            "reason": REASON_CEILING,
            "taken": self.taken,
            "since": self.since.isoformat(),
            "until": self.until.isoformat(),
            "give_kw": round(self.give_w / 1000.0, 2),
        }


def combine(parts: list[Offer | None]) -> Offer | None:
    """One offer for a load made of parts (heat: the pump's path and each zone). Where any part is SpotNav's to steer,
    those parts decide (a step is live; the watched parts add nothing); else the watched parts say what would be."""
    found = [part for part in parts if part is not None]
    if not found:
        return None
    live = [part for part in found if part.live]
    chosen = live or found
    ready = [part for part in chosen if part.ready]
    held = any(part.held for part in chosen)
    pending = any(part.pending for part in chosen)
    if not ready:
        why = next((part.why for part in chosen if part.why), None)
        return Offer(live=bool(live), ready=False, give_w=0.0, drawing=False, why=why, held=held, pending=pending)
    return Offer(
        live=bool(live),
        ready=True,
        give_w=sum(part.give_w for part in ready),
        drawing=any(part.drawing for part in ready),
        held=held,
        pending=pending,
    )


def _until(load: str, since: datetime, projection: Projection) -> datetime:
    """Until when a step holds: the period's end; heat at least its least lowering (`relay_control.MIN_LOWER`) and
    `START_MARGIN_S` from when it was taken, so the lowering it asks for can start at its control's next look."""
    if load == LOAD_HEAT:
        return max(projection.period_end, since + MIN_LOWER + timedelta(seconds=START_MARGIN_S))
    return projection.period_end


class ShedOrder:
    """One site's shed order (see the module). Kept in memory only: a restart starts with nothing shed, and the
    loads' own exits (the water heater on, the heat back) hold either way. `started_at`: when the site started;
    nothing is taken for `REST_S` after it (`None`: no such wait, for a plain test)."""

    def __init__(self, started_at: datetime | None = None) -> None:
        self.steps: dict[str, Step] = {}
        self._started_at = started_at
        #: Since when the period has been over (`excess_w > 0`), and over beyond the cars (`beyond_cars_w > 0`).
        self._over_since: datetime | None = None
        self._beyond_since: datetime | None = None
        #: Per step, since when it could be put back.
        self._under_since: dict[str, datetime] = {}
        #: Per load, the period in which its step ended (not taken again in it) and when (`REST_S`).
        self._ended: dict[str, datetime] = {}
        self._ended_at: dict[str, datetime] = {}
        #: The last pass with a budget (the steps hold `TURN_GRACE_S` without one).
        self._budget_at: datetime | None = None
        #: Per load, the rule that kept it from a step that was due (`blocked`), as of the last pass.
        self.blocked: dict[str, str] = {}
        self.credit_w = 0.0
        self.projection: Projection | None = None
        self.log: deque[dict[str, Any]] = deque(maxlen=LOG_SIZE)
        #: How many entries were ever logged (the site forwards the new ones to its change log).
        self.logged = 0

    # ---- each pass

    def update(self, now: datetime, projection: Projection | None, offers: dict[str, Offer | None]) -> float:
        """One pass on the hold's projection and the loads' offers. The water heater's give the hold counts ahead for
        the cars (W, see the module). `projection` `None`: no budget for a passing reason; the steps hold for
        `TURN_GRACE_S`, nothing new is taken or counted (`clear` puts everything back for good)."""
        self.blocked = {}
        self.credit_w = 0.0
        if projection is None:
            self.projection = None
            if self._budget_at is None or (now - self._budget_at).total_seconds() >= TURN_GRACE_S:
                self.clear(now, END_OFF)
            return 0.0
        self.projection = projection
        self._budget_at = now
        for step in self.steps.values():
            step.until = _until(step.load, step.since, projection)
        self._end_by_rules(now, offers)
        self._check_taken(now, offers)
        heater = offers.get(LOAD_WATER_HEATER)
        heater_step = self.steps.get(LOAD_WATER_HEATER)
        # The water heater's give still in the house's reading (its pause not taken yet) is not over beyond the cars.
        still_in = heater_step.give_w if heater_step is not None and heater_step.live and heater is not None and heater.drawing else 0.0
        excess = projection.excess_w
        beyond = projection.beyond_cars_w - still_in
        self._over_since = (self._over_since or now) if excess > 0 else None
        self._beyond_since = (self._beyond_since or now) if beyond > 0 else None
        self._release(now, excess, beyond)
        self._enter(now, projection, offers)
        self.credit_w = self._credit(now, projection, heater)
        return self.credit_w

    def _end_by_rules(self, now: datetime, offers: dict[str, Offer | None]) -> None:
        """A step whose load's own rules no longer let it stay shed (a hard rule, a safety reason, the opt-in, the
        water heater's need in its window) ends at once: the rules win."""
        for load in list(self.steps):
            offer = offers.get(load)
            step = self.steps[load]
            short = offer is not None and offer.until_max is not None and offer.until_max < step.until
            if offer is None or not offer.ready or offer.live != step.live or short:
                self._end(now, load, END_RULE, rule=None if offer is None else ("need" if short and offer.ready else offer.why))

    def _check_taken(self, now: datetime, offers: dict[str, Offer | None]) -> None:
        """A live step is taken once its control holds it (logged then); one not held within `TAKE_WITHIN_S` is
        dropped without a word."""
        for load in list(self.steps):
            step = self.steps[load]
            if step.taken:
                continue
            offer = offers.get(load)
            # Only the control saying it holds the step counts (a switch that merely does not read is no pause).
            if offer is not None and offer.held:
                step.taken = True
                self._log(now, load, "shed", give_kw=round(step.give_w / 1000.0, 2))
                continue
            wait = TAKE_PENDING_S if offer is not None and offer.pending else TAKE_WITHIN_S
            if (now - step.since).total_seconds() >= wait:
                self._end(now, load, END_RULE)

    def _release(self, now: datetime, excess: float, beyond: float) -> None:
        """The last step taken put back once the period stays under with its give back for `RELEASE_S` (see the
        module): heat before the water heater, one at a time. A step its control has not taken yet holds none back."""
        for load, measure in ((LOAD_HEAT, beyond), (LOAD_WATER_HEATER, excess)):
            step = self.steps.get(load)
            if step is None or not step.taken:
                self._under_since.pop(load, None)
                continue
            back = measure + (step.give_w if step.live else 0.0)
            if back < -RELEASE_MARGIN_W:
                since = self._under_since.setdefault(load, now)
                if (now - since).total_seconds() >= RELEASE_S:
                    self._end(now, load, END_UNDER)
            else:
                self._under_since.pop(load, None)
            # Only the last step taken is looked at: heat goes back before the water heater.
            return

    def _enter(self, now: datetime, projection: Projection, offers: dict[str, Offer | None]) -> None:
        """The next step in the order, where the period has stayed over long enough (see the module)."""
        if self._over_since is None:
            return
        heater = offers.get(LOAD_WATER_HEATER)
        if LOAD_WATER_HEATER not in self.steps and self._may_take(now, LOAD_WATER_HEATER, heater, projection):
            if (now - self._over_since).total_seconds() >= ENTER_S or projection.car_pause_due:
                self._take(now, LOAD_WATER_HEATER, heater, projection)  # type: ignore[arg-type]
            # The heater is the step due: heat waits for it, and its own delay starts after it.
            self._beyond_since = None
            return
        if heater is not None and heater.give_w >= MIN_GIVE_W and not heater.ready and heater.why is not None:
            self.blocked[LOAD_WATER_HEATER] = heater.why
        if self._beyond_since is None or LOAD_HEAT in self.steps:
            return
        heat = offers.get(LOAD_HEAT)
        if not self._may_take(now, LOAD_HEAT, heat, projection):
            if heat is not None and not heat.ready and heat.why is not None:
                self.blocked[LOAD_HEAT] = heat.why
            return
        if (now - self._beyond_since).total_seconds() >= ENTER_S:
            self._take(now, LOAD_HEAT, heat, projection)  # type: ignore[arg-type]

    def _may_take(self, now: datetime, load: str, offer: Offer | None, projection: Projection) -> bool:
        ended_at = self._ended_at.get(load)
        return (
            offer is not None
            and offer.useful
            and (offer.until_max is None or offer.until_max >= _until(load, now, projection))
            and self._ended.get(load) != projection.period_start
            and (ended_at is None or (now - ended_at).total_seconds() >= REST_S)
            and (self._started_at is None or (now - self._started_at).total_seconds() >= REST_S)
        )

    def _take(self, now: datetime, load: str, offer: Offer, projection: Projection) -> None:
        self.steps[load] = Step(
            load=load,
            live=offer.live,
            since=now,
            until=_until(load, now, projection),
            give_w=offer.give_w,
            period_start=projection.period_start,
            taken=not offer.live,
            house_w=projection.house_w,
        )
        self._under_since.pop(load, None)
        if not offer.live:
            self._log(now, load, "would_shed", give_kw=round(offer.give_w / 1000.0, 2))

    def _end(self, now: datetime, load: str, why: str, **extra: Any) -> None:
        step = self.steps.pop(load, None)
        self._under_since.pop(load, None)
        if step is None:
            return
        # Not taken again in the period it ended in (nor, ended by a period it no longer knows, in the step's own).
        period = self.projection.period_start if self.projection is not None else step.period_start
        self._ended[load] = period
        self._ended_at[load] = now
        if step.taken:
            self._log(now, load, "put_back" if step.live else "would_put_back", why=why, **extra)

    def _credit(self, now: datetime, projection: Projection, heater: Offer | None) -> float:
        """The water heater's give the hold counts as gone already (see the module): only for a live step, from when
        it is taken while the heater still draws (or for the tail after its switch reads off), at most `CREDIT_S`
        and `CREDIT_TAIL_S` from the step, which is once per period; never in the period's last minutes."""
        step = self.steps.get(LOAD_WATER_HEATER)
        if step is None or not step.live or heater is None or not heater.live or not heater.ready:
            return 0.0
        if projection.remaining_s <= raise_lock_s(0.0, projection.period_s):
            return 0.0
        elapsed = (now - step.since).total_seconds()
        if elapsed > CREDIT_S + CREDIT_TAIL_S:
            return 0.0
        if heater.drawing:
            # Only for a draw its meter reads now: never for power it may not be drawing.
            step.off_seen_at = None
            return step.give_w if elapsed <= CREDIT_S and heater.give_w >= MIN_GIVE_W else 0.0
        # Read off: a slow meter may not show it yet; the tail ends once the house has fallen by half the give.
        step.off_seen_at = step.off_seen_at or now
        if (now - step.off_seen_at).total_seconds() > CREDIT_TAIL_S:
            return 0.0
        if projection.house_w <= step.house_w - step.give_w / 2:
            return 0.0
        return step.give_w

    def clear(self, now: datetime, why: str) -> None:
        """Every step put back at once (nothing held for good, a failure): the loads' own exits do the rest."""
        for load in list(self.steps):
            self._end(now, load, why)
        self._over_since = self._beyond_since = None
        self._under_since = {}
        self._budget_at = None
        self.credit_w = 0.0

    # ---- what the loads and the views read

    def until(self, load: str, now: datetime) -> datetime | None:
        """Until when SpotNav sheds this load for the ceiling now (a live step only), `None` when it does not. A step
        is ended by this module, never by its own clock: at the period's turn, before the next pass has looked at the
        new period, it holds a little longer (`TURN_GRACE_S`) rather than switch on and off again; a step no pass has
        looked at for that long is over."""
        step = self.steps.get(load)
        if step is None or not step.live:
            return None
        if now < step.until:
            return step.until
        if (now - step.until).total_seconds() < TURN_GRACE_S:
            return now + timedelta(seconds=TURN_GRACE_S)
        return None

    def step(self, load: str) -> Step | None:
        return self.steps.get(load)

    def snapshot(self) -> dict[str, Any]:
        """The site's diagnostics block (`shed_order`)."""
        projection = self.projection
        return {
            "order": list(ORDER),
            "steps": {load: step.as_dict() for load, step in self.steps.items()},
            "blocked": dict(self.blocked),
            "credit_kw": round(self.credit_w / 1000.0, 2),
            "projection": None
            if projection is None
            else {
                "period_start": projection.period_start.isoformat(),
                "period_end": projection.period_end.isoformat(),
                "rate_kw": round(projection.rate_w / 1000.0, 2),
                "house_kw": round(projection.house_w / 1000.0, 2),
                "cars_held_kw": round(projection.cars_held_w / 1000.0, 2),
                "cars_unheld_kw": round(projection.cars_unheld_w / 1000.0, 2),
                "excess_kw": round(projection.excess_w / 1000.0, 2),
                "beyond_cars_kw": round(projection.beyond_cars_w / 1000.0, 2),
            },
            "log": list(self.log),
        }

    def _log(self, now: datetime, load: str, what: str, **extra: Any) -> None:
        self.log.append({"at": now.isoformat(), "load": load, "what": what, **extra})
        self.logged += 1
