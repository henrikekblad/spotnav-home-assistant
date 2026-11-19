"""The hot-water boost's route and state (plan `plans/v3_dhw_plan.md` sections 8 and 11). Pure.

**The route** (`boost_route`): the pump's own self-ending boost first (`adapters.Capability`'s recipe: NIBE's
temporary lux "one time", myUplink's temporary lux, CTC's extra hot water for an hour, HeishaMon's Force DHW),
enabled and readable; else the heat settings' `hot_water_entity`, a pulse on the pump's hot-water input (a switch on
then off, or a button pressed): the pump ends the run itself. A boost that does not end by itself (NIBE S 40698) is
no route: the button is not shown.

**The writes** (`start_call`, `stop_call`): a switch is turned on and off, a select set to the option its recipe's
words name, a number set to its recipe's value; a pulse's switch is turned on, and off again by the shell after
`PULSE`. No brand is named here: the recipe says what to write.

**The state** (`boost_state`): `starting` while a start waits for its read-back, `failed` for a while after one that
was not seen, `running` while the boost reads on (or, on a pulse, while the meter shows the run the press started)
or the pump makes hot water by itself, else `idle`. `source`: `user` (the button's or the household's own boost),
`plan` (SpotNav's planned boost), `pump` (the pump's own run). The remaining time is exact on a timer, estimated
from the learned run length on a one-time run, unknown otherwise.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final

from .dhw_ledger import SOURCE_BUTTON, SOURCE_PLAN
from .dhw_plan import ROUTE_CLOUD, ROUTE_ONE_SHOT, ROUTE_PULSE, ROUTE_VOLATILE

#: How long a pulse's switch is held on.
PULSE: Final = timedelta(seconds=2)
#: A start's read-back: a local entity, and a cloud's (two polls).
READ_BACK: Final = timedelta(seconds=30)
READ_BACK_CLOUD: Final = timedelta(minutes=3)
#: The Boost button's starts: one per this long per site.
RATE: Final = timedelta(minutes=10)
#: A failed start shows this long (or until the next press).
FAILED_FOR: Final = timedelta(minutes=2)
#: A pulse's run must show on the meter this soon after the press, or the press is taken as ended.
PULSE_RUN_WITHIN: Final = timedelta(minutes=10)

#: The phase voltage a boost's current is reckoned at.
VOLTAGE_V: Final = 230.0

STATE_IDLE: Final = "idle"
STATE_STARTING: Final = "starting"
STATE_RUNNING: Final = "running"
STATE_FAILED: Final = "failed"

BY_USER: Final = "user"
BY_PLAN: Final = "plan"
BY_PUMP: Final = "pump"

_SWITCH: Final = ("switch", "input_boolean")
_BUTTON: Final = ("button", "input_button")


@dataclass(frozen=True, slots=True)
class Route:
    entity_id: str
    #: `one_shot`, `volatile`, `cloud` (the plan's budget, `dhw_plan.BUDGETS`) or `pulse`.
    kind: str
    start: str | None = None
    stop: str | None = None
    duration: timedelta | None = None
    #: That a start ends by itself is confirmed: only then may SpotNav plan boosts on it (the button may always).
    verified: bool = True

    @property
    def domain(self) -> str:
        return self.entity_id.split(".", 1)[0]

    @property
    def cloud(self) -> bool:
        return self.kind == ROUTE_CLOUD

    @property
    def stoppable(self) -> bool:
        if self.kind == ROUTE_PULSE:
            return False
        return self.domain in _SWITCH or self.stop is not None

    def as_dict(self) -> dict[str, Any]:
        return {
            "entity_id": self.entity_id,
            "kind": self.kind,
            "start": self.start,
            "stop": self.stop,
            "duration_s": None if self.duration is None else self.duration.total_seconds(),
            "verified": self.verified,
        }


def boost_route(pump: Any, hot_water_entity: str | None) -> Route | None:
    """The route a boost is started through (see the module), `None` without one. `pump`: `facts.HeatPumpFacts`."""
    if pump is not None:
        for capability in pump.capabilities:
            if capability.kind != "hot_water_boost" or not capability.self_ending:
                continue
            if not capability.enabled or not capability.available:
                continue
            kind = ROUTE_CLOUD if pump.cloud else ROUTE_VOLATILE if capability.volatile else ROUTE_ONE_SHOT
            duration = None if capability.duration_s is None else timedelta(seconds=capability.duration_s)
            verified = getattr(capability, "verified", True) is not False
            return Route(capability.entity_id, kind, capability.start, capability.stop, duration, verified)
    if hot_water_entity is not None and hot_water_entity.split(".", 1)[0] in (*_SWITCH, *_BUTTON):
        return Route(hot_water_entity, ROUTE_PULSE)
    return None


def _option(options: Sequence[str], words: str | None) -> str | None:
    if words is None:
        return None
    wanted = words.casefold()
    return next((option for option in options if wanted in str(option).casefold()), None)


def start_call(route: Route, options: Sequence[str] = ()) -> tuple[str, str, dict[str, Any]] | None:
    """`(domain, service, data)` that starts the boost, `None` when the recipe cannot be written (a select without
    its option)."""
    target = {"entity_id": route.entity_id}
    domain = route.domain
    if domain in _SWITCH:
        return domain, "turn_on", target
    if domain in _BUTTON:
        return domain, "press", target
    if domain in ("select", "input_select"):
        option = _option(options, route.start)
        return None if option is None else (domain, "select_option", {**target, "option": option})
    if domain in ("number", "input_number") and route.start is not None:
        return domain, "set_value", {**target, "value": float(route.start)}
    return None


def stop_call(route: Route, options: Sequence[str] = ()) -> tuple[str, str, dict[str, Any]] | None:
    """`(domain, service, data)` that ends the boost early, `None` without a way."""
    if not route.stoppable:
        return None
    target = {"entity_id": route.entity_id}
    domain = route.domain
    if domain in _SWITCH:
        return domain, "turn_off", target
    if domain in ("select", "input_select"):
        option = _option(options, route.stop)
        return None if option is None else (domain, "select_option", {**target, "option": option})
    if domain in ("number", "input_number") and route.stop is not None:
        return domain, "set_value", {**target, "value": float(route.stop)}
    return None


def fits_fuse(margins: Mapping[str, float | None], boost_kw: float, phases: int = 1) -> bool | None:
    """Whether a boost of `boost_kw` fits the fuse now: the site's measured margin per phase (A, the fuse less its
    margin less what flows) holds its current on the tightest phase. A boost on one phase (the worst case, taken
    until an adapter says the pump heats water on three) needs all of it there. `None` without phase readings."""
    known = [value for value in margins.values() if value is not None]
    if not known:
        return None
    need_a = boost_kw * 1000.0 / (VOLTAGE_V * max(1, phases))
    return min(known) >= need_a


def over_fuse(margins: Mapping[str, float | None]) -> bool | None:
    """Whether a phase draws more than the fuse less its margin now; `None` without phase readings."""
    known = [value for value in margins.values() if value is not None]
    return None if not known else min(known) < 0.0


@dataclass(frozen=True, slots=True)
class BoostReading:
    """What the shell read now."""

    #: The boost entity reads on (`paths.boost_on`), and since when; a pulse's entity never counts.
    on: bool = False
    on_since: datetime | None = None
    #: The ledger's source of the boost reading on (`Ledger.source_of_on`), `None`: the household's own.
    ledger_source: str | None = None
    #: The pump makes hot water now (the meter's state or the pump's own priority).
    making: bool = False
    #: The last button start on a pulse route and whether the meter has shown its run.
    pulse_at: datetime | None = None
    pending: bool = False
    failed_at: datetime | None = None


def boost_state(route: Route | None, reading: BoostReading, now: datetime, run_len: timedelta) -> dict[str, Any] | None:
    """`hot_water.boost` (see the module), `None` without a route."""
    if route is None:
        return None

    def answer(state: str, source: str | None = None, started: datetime | None = None, exact: bool = False) -> dict[str, Any]:
        ends = None
        estimated = False
        if started is not None and state == STATE_RUNNING:
            if exact and route.duration is not None:
                ends = started + route.duration
            elif source in (BY_USER, BY_PLAN):
                ends = max(now, started + run_len)
                estimated = True
        return {
            "state": state,
            "source": source,
            "started": None if started is None else started.isoformat(),
            "ends": None if ends is None else ends.isoformat(),
            "ends_estimated": estimated,
            "stoppable": state == STATE_RUNNING and source == BY_USER and route.stoppable,
        }

    if reading.pending:
        return answer(STATE_STARTING)
    if route.kind != ROUTE_PULSE and reading.on:
        source = BY_PLAN if reading.ledger_source == SOURCE_PLAN else BY_USER
        return answer(STATE_RUNNING, source, reading.on_since, exact=True)
    if route.kind == ROUTE_PULSE and reading.pulse_at is not None:
        since = now - reading.pulse_at
        if since <= PULSE_RUN_WITHIN or (reading.making and since <= PULSE_RUN_WITHIN + 2 * run_len):
            return answer(STATE_RUNNING, BY_PLAN if reading.ledger_source == SOURCE_PLAN else BY_USER, reading.pulse_at)
    if reading.failed_at is not None and now - reading.failed_at <= FAILED_FOR:
        return answer(STATE_FAILED)
    if reading.making:
        return answer(STATE_RUNNING, BY_PUMP)
    return answer(STATE_IDLE)


__all__ = [
    "BY_PLAN",
    "BY_PUMP",
    "BY_USER",
    "PULSE",
    "RATE",
    "READ_BACK",
    "READ_BACK_CLOUD",
    "STATE_FAILED",
    "STATE_IDLE",
    "STATE_RUNNING",
    "STATE_STARTING",
    "BoostReading",
    "Route",
    "boost_route",
    "boost_state",
    "PULSE_RUN_WITHIN",
    "fits_fuse",
    "over_fuse",
    "start_call",
    "stop_call",
    "SOURCE_BUTTON",
]
