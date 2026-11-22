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
   while the period is over (`excess_w > 0`) for `ENTER_S` in a row. It gives what it draws now (measured, else its
   learnt kW, `Offer.give_w`); one that gives less than `MIN_GIVE_W` (off, its thermostat satisfied, an unmetered
   element whose kW is unknown) is no step. Only in a cheap window: its hard rules win (`water_heater.Rules.want`:
   full heat every 20 h, never off 8 h, on without a plan or a reading, the tank low, the day's switchings), and a
   hard rule ending the pause ends the step at once. **Counted ahead for the car**: from the first look over until
   the pause has read back (at most `ENTER_S + CREDIT_S`), the hold counts the heater's draw as already gone
   (`credit_w`, added to what the cars may draw), so the car is not cut, or paused for the hour, for what the heater
   is about to give. Never in the period's last minutes (`raise_lock_s`: the hour's last 10, a quarter's last 2.5):
   there the car is held as before, and what the credit let through earlier in the period the hold's budget takes
   back from the cars on its own (it is in the period's import so far).
3. **Car**: the hold as before (lower, pause; `share_budget`), unchanged. With the water heater's give counted it
   takes only what remains.
4. **Heat**: one step lower (the relay or SG Ready contact, the offset, and the controlled zones 1 °C) while the
   period is over even with the cars paused (`beyond_cars_w > 0`) for `ENTER_S`, after the water heater (paused, or
   with nothing to give). Through the heat control's own rules (`heat/relay_control.decide`, `zone_control.decide`):
   at least `MIN_LOWER`, at most 3 h in a row and 6 h a day, the rest between lowerings, the day's writes, never in
   a cold room or below -15 °C outdoors; a safety reason ends it at once. Counted as giving the learnt share of the
   pump's heat (`relay_effect.Effect.lower_share`) and the zones' measured power; no credit (it acts slowly and the
   car is already paused by then).

**Put back in reverse**: heat while `beyond_cars_w + its give` stays under by `RELEASE_MARGIN_W` for `RELEASE_S`; the
car is the hold's own (it raises as the budget allows); then the water heater while `excess_w + its give` does. One
step at a time, heat before the water heater. A step released (or ended by its own rules) is not taken again in the
same period, nor within `REST_S` of its end: a contactor never flaps, and the water heater's planned switchings a day
still count every pause.

**Watched loads** (a water heater without its opt-in, heat control off) take the same steps as `would` only: shown
and logged, never written, and nothing counted (the next step goes on as if they were not there).

**Not acting**: without the hold's budget (no import target or power level, outside the level's hours, load balancing
off, the period not measurable) every step is put back at once and nothing is counted. The fuse is never this
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
#: At the period's turn a step holds this much longer while no pass has looked at the new period yet (s).
TURN_GRACE_S: Final = 120.0
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
    """A step taken: the load, live or `would`, since when, until when it is shown to hold, and what it gives."""

    load: str
    live: bool
    since: datetime
    until: datetime
    give_w: float
    period_start: datetime

    def as_dict(self) -> dict[str, Any]:
        return {
            "state": STATE_PAUSED if self.live else STATE_WOULD,
            "reason": REASON_CEILING,
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
    if not ready:
        return Offer(live=bool(live), ready=False, give_w=0.0, drawing=False, why=next((part.why for part in chosen if part.why), None))
    return Offer(live=bool(live), ready=True, give_w=sum(part.give_w for part in ready), drawing=any(part.drawing for part in ready))


def _until(load: str, since: datetime, projection: Projection) -> datetime:
    """Until when a step holds: the period's end; heat at least its least lowering (`relay_control.MIN_LOWER`), which
    its own rules keep anyway."""
    if load == LOAD_HEAT:
        return max(projection.period_end, since + MIN_LOWER)
    return projection.period_end


class ShedOrder:
    """One site's shed order (see the module). Kept in memory only: a restart starts with nothing shed, and the
    loads' own exits (the water heater on, the heat back) hold either way."""

    def __init__(self) -> None:
        self.steps: dict[str, Step] = {}
        #: Since when the period has been over (`excess_w > 0`), and over beyond the cars (`beyond_cars_w > 0`).
        self._over_since: datetime | None = None
        self._beyond_since: datetime | None = None
        #: Per step, since when it could be put back.
        self._under_since: dict[str, datetime] = {}
        #: Per load, the period in which its step ended (not taken again in it) and when (`REST_S`).
        self._ended: dict[str, datetime] = {}
        self._ended_at: dict[str, datetime] = {}
        #: Per load, the rule that kept it from a step that was due (`blocked`), as of the last pass.
        self.blocked: dict[str, str] = {}
        self.credit_w = 0.0
        self.projection: Projection | None = None
        self.log: deque[dict[str, Any]] = deque(maxlen=LOG_SIZE)

    # ---- each pass

    def update(self, now: datetime, projection: Projection | None, offers: dict[str, Offer | None]) -> float:
        """One pass on the hold's projection (`None`: nothing held, everything put back) and the loads' offers. The
        water heater's give the hold counts ahead for the cars (W, see the module)."""
        self.projection = projection
        self.blocked = {}
        if projection is None:
            self.clear(now, END_OFF)
            return 0.0
        for step in self.steps.values():
            step.until = _until(step.load, step.since, projection)
        self._end_by_rules(now, offers)
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
        """A step whose load's own rules no longer let it stay shed (a hard rule, a safety reason, the opt-in) ends
        at once: the rules win."""
        for load in list(self.steps):
            offer = offers.get(load)
            step = self.steps[load]
            if offer is None or not offer.ready or offer.live != step.live:
                self._end(now, load, END_RULE, rule=None if offer is None else offer.why)

    def _release(self, now: datetime, excess: float, beyond: float) -> None:
        """The last step taken put back once the period stays under with its give back for `RELEASE_S` (see the
        module): heat before the water heater, one at a time."""
        for load, measure in ((LOAD_HEAT, beyond), (LOAD_WATER_HEATER, excess)):
            step = self.steps.get(load)
            if step is None:
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
            if (now - self._over_since).total_seconds() >= ENTER_S:
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
            and self._ended.get(load) != projection.period_start
            and (ended_at is None or (now - ended_at).total_seconds() >= REST_S)
        )

    def _take(self, now: datetime, load: str, offer: Offer, projection: Projection) -> None:
        self.steps[load] = Step(
            load=load,
            live=offer.live,
            since=now,
            until=_until(load, now, projection),
            give_w=offer.give_w,
            period_start=projection.period_start,
        )
        self._under_since.pop(load, None)
        self._log(now, load, "shed" if offer.live else "would_shed", give_kw=round(offer.give_w / 1000.0, 2))

    def _end(self, now: datetime, load: str, why: str, **extra: Any) -> None:
        step = self.steps.pop(load, None)
        self._under_since.pop(load, None)
        if step is None:
            return
        # Not taken again in the period it ended in (nor, ended by a period it no longer knows, in the step's own).
        period = self.projection.period_start if self.projection is not None else step.period_start
        self._ended[load] = period
        self._ended_at[load] = now
        self._log(now, load, "put_back" if step.live else "would_put_back", why=why, **extra)

    def _credit(self, now: datetime, projection: Projection, heater: Offer | None) -> float:
        """The water heater's give the hold counts as gone already (see the module): from the first look over until
        its pause has read back, never in the period's last minutes, never for a watched heater."""
        if heater is None or not heater.live or not heater.ready or not heater.drawing:
            return 0.0
        if projection.remaining_s <= raise_lock_s(0.0, projection.period_s):
            return 0.0
        step = self.steps.get(LOAD_WATER_HEATER)
        if step is not None:
            if step.live and (now - step.since).total_seconds() <= CREDIT_S:
                return step.give_w
            return 0.0
        if (
            self._over_since is not None
            and self._may_take(now, LOAD_WATER_HEATER, heater, projection)
            and (now - self._over_since).total_seconds() <= ENTER_S
        ):
            return heater.give_w
        return 0.0

    def clear(self, now: datetime, why: str) -> None:
        """Every step put back at once (nothing held, a failure): the loads' own exits do the rest."""
        for load in list(self.steps):
            self._end(now, load, why)
        self._over_since = self._beyond_since = None
        self._under_since = {}
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
