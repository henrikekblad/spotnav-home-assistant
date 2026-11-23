"""One deadline heat load in shadow, the spa's or the sauna's (`DeadlineShadow`), run by the site's pool and spa shadow
(`pool/runtime.py`): reads it, learns its rate from its real heating runs, keeps the household's "now" press and its
answers to a session that does not fit, plans the next session (`spa/plan.py`) and says what it would do. Nothing is
written to the device.

**The sauna's safety** (the owner's decision of 2026-11-23): its plan only ever heats for a booked session or a "now"
press; a lock that reads (its door sensor open, Tylö's door fault) turns a heat-up into `blocked` (`lock`); a relay
whose heater's own safety measure is not declared turns it into `start_by_hand` (`no_safety`): SpotNav only says when.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final

from ..sauna.config import CLOUD as SAUNA_CLOUD, FAMILY_RELAY, SaunaConfig
from .config import CLOUD as SPA_CLOUD, SpaConfig
from .plan import (
    ACTION_HEAT,
    HOLD,
    KIND_SAUNA,
    QUARTER,
    SAUNA,
    SOURCE_NOW,
    SPA,
    DeadlineInputs,
    DeadlinePlan,
    HeatHour,
    Profile,
    learnt_rate,
    plan_deadline,
    sessions_ahead,
)

#: A "now" press at most this often (spec section 10.2).
NOW_SPACING: Final = timedelta(minutes=10)
LEARN_KEPT: Final = 20
#: What the heater draws where nothing says (only to cost and judge the hours: placeholders).
DEFAULT_KW: Final[dict[str, float]] = {"spa": 3.0, "sauna": 9.0}

ACTION_BLOCKED: Final = "blocked"
ACTION_BY_HAND: Final = "start_by_hand"
REASON_LOCK: Final = "lock"
REASON_NO_SAFETY: Final = "no_safety"
STATE_WATCHED: Final = "watched"
STATE_WATCH_ONLY: Final = "watch_only"
REASON_CLOUD: Final = "cloud_slow"

ERROR_RATE_LIMITED: Final = "spotnav_rate_limited"
ERROR_NO_LATER: Final = "spotnav_no_later_bath"


def _aware(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else None


@dataclass(frozen=True, slots=True)
class Reading:
    """The load as it is now."""

    current_c: float | None
    heating: bool | None
    #: The lock reads locked (a door open, a door fault); `None` where nothing reads.
    locked: bool | None = None
    #: The device's own longest run, where it says (Harvia's on time).
    max_on: timedelta | None = None


class DeadlineShadow:
    """One spa or sauna in shadow (see the module)."""

    def __init__(self, profile: Profile) -> None:
        self.profile = profile
        self.runs: list[tuple[datetime, datetime, float, float]] = []
        self.now_until: datetime | None = None
        self.now_pressed: datetime | None = None
        self.took_what_fits: set[datetime] = set()
        self.accept_peak: set[datetime] = set()
        self.later: dict[datetime, datetime] = {}
        self.plan: DeadlinePlan | None = None
        self.reading = Reading(None, None)
        self._run_start: tuple[datetime, float] | None = None

    # ---- the record

    def as_dict(self) -> dict[str, Any]:
        return {
            "runs": [[a.isoformat(), b.isoformat(), low, high] for a, b, low, high in self.runs],
            "now_until": None if self.now_until is None else self.now_until.isoformat(),
            "now_pressed": None if self.now_pressed is None else self.now_pressed.isoformat(),
            "took_what_fits": sorted(moment.isoformat() for moment in self.took_what_fits),
            "accept_peak": sorted(moment.isoformat() for moment in self.accept_peak),
            "later": {a.isoformat(): b.isoformat() for a, b in self.later.items()},
        }

    def restore(self, raw: Any) -> None:
        if not isinstance(raw, Mapping):
            return
        for row in raw.get("runs") or ():
            if isinstance(row, list) and len(row) == 4:
                start, end = _aware(row[0]), _aware(row[1])
                if start and end and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in row[2:]):
                    self.runs.append((start, end, float(row[2]), float(row[3])))
        self.now_until = _aware(raw.get("now_until"))
        self.now_pressed = _aware(raw.get("now_pressed"))
        self.took_what_fits = {m for m in (_aware(v) for v in raw.get("took_what_fits") or ()) if m is not None}
        self.accept_peak = {m for m in (_aware(v) for v in raw.get("accept_peak") or ()) if m is not None}
        later = raw.get("later")
        if isinstance(later, Mapping):
            self.later = {a: b for a, b in ((_aware(k), _aware(v)) for k, v in later.items()) if a is not None and b is not None}

    def _prune(self, now: datetime) -> None:
        """Answers about sessions gone by are dropped."""
        old = now - HOLD - timedelta(days=1)
        self.took_what_fits = {m for m in self.took_what_fits if m > old}
        self.accept_peak = {m for m in self.accept_peak if m > old}
        self.later = {a: b for a, b in self.later.items() if b > old}
        if self.now_until is not None and self.now_until <= now:
            self.now_until = None

    # ---- each look

    def look(
        self,
        now: datetime,
        tz: str,
        config: SpaConfig | SaunaConfig,
        reading: Reading,
        hours: list[HeatHour],
    ) -> DeadlinePlan:
        """Learn from the reading and plan again."""
        self.reading = reading
        self._prune(now)
        current = reading.current_c
        if reading.heating and self._run_start is None and current is not None:
            self._run_start = (now, current)
        elif not reading.heating and self._run_start is not None:
            start, low = self._run_start
            self._run_start = None
            if current is not None:
                self.runs = [*self.runs, (start, now, low, current)][-LEARN_KEPT:]
        rate, _learnt = learnt_rate(self.runs, self.profile.seed_rate_c_h)
        if isinstance(config, SpaConfig):
            target, weekly, once, hold, floor = config.bath_c, config.baths, config.bath_once_at(), config.hold_c, config.floor_c
            kw = config.heater_kw
        else:
            target, weekly, once, hold, floor = config.target_c, config.sessions, config.session_once_at(), None, None
            kw = config.heater_kw
        self.plan = plan_deadline(
            DeadlineInputs(
                now=now,
                tz=tz,
                profile=self.profile,
                target_c=target,
                current_c=current,
                rate_c_h=rate,
                kw=kw or DEFAULT_KW[self.profile.kind],
                sessions=sessions_ahead(weekly, once, now, tz),
                hours=hours,
                now_until=self.now_until,
                hold_c=hold,
                floor_c=floor,
                took_what_fits=frozenset(self.took_what_fits),
                accept_peak=frozenset(self.accept_peak),
                later=dict(self.later),
                max_on=reading.max_on,
            )
        )
        return self.plan

    # ---- the household's presses

    def press_now(self, now: datetime, target_c: float, current_c: float | None) -> str | None:
        """Heat at once, any price (still under the fuse); held `HOLD` after the target is reached. Once per
        `NOW_SPACING`."""
        if self.now_pressed is not None and now - self.now_pressed < NOW_SPACING:
            return ERROR_RATE_LIMITED
        rate, _learnt = learnt_rate(self.runs, self.profile.seed_rate_c_h)
        need = timedelta(hours=max(0.0, target_c - (current_c if current_c is not None else 20.0)) / max(0.1, rate))
        self.now_pressed = now
        self.now_until = now + need + HOLD
        return None

    def cancel_now(self) -> None:
        self.now_until = None

    def take_what_fits(self, at: datetime) -> None:
        self.took_what_fits.add(at)

    def allow_peak(self, at: datetime) -> None:
        self.accept_peak.add(at)

    def move_later(self) -> tuple[datetime | None, str | None]:
        """Move the next session that does not fit to the earliest later whole hour where it fits (the plan's), or
        refuse when there is none."""
        plan = self.plan
        not_ready = None if plan is None else plan.not_ready
        if not_ready is None or not_ready.later is None:
            return None, ERROR_NO_LATER
        original = next((first for first, moved in self.later.items() if moved == not_ready.at), not_ready.at)
        self.later[original] = not_ready.later
        return not_ready.later, None

    # ---- what the card, the plan and the descriptions read

    def would(self, config: SpaConfig | SaunaConfig) -> dict[str, Any] | None:
        plan = self.plan
        if plan is None:
            return None
        action, reason = plan.action, plan.reason
        if isinstance(config, SaunaConfig) and action == ACTION_HEAT:
            if self.reading.locked:
                action, reason = ACTION_BLOCKED, REASON_LOCK
            elif config.family == FAMILY_RELAY and not config.safety_declared:
                action, reason = ACTION_BY_HAND, REASON_NO_SAFETY
        return {"action": action, "until": None if plan.until is None else plan.until.isoformat(), "reason": reason}

    def control(self, config: SpaConfig | SaunaConfig) -> dict[str, Any]:
        cloud = SPA_CLOUD if isinstance(config, SpaConfig) else SAUNA_CLOUD
        reason = REASON_CLOUD if config.family in cloud else None
        if isinstance(config, SaunaConfig) and config.family == FAMILY_RELAY and not config.safety_declared:
            reason = REASON_NO_SAFETY
        return {
            "state": STATE_WATCHED if config.controllable else STATE_WATCH_ONLY,
            "reason": reason,
            "paused_until": None,
        }

    def common(self, config: SpaConfig | SaunaConfig, now: datetime) -> dict[str, Any]:
        """The keys the spa's and the sauna's status share."""
        plan = self.plan
        rate, learnt = learnt_rate(self.runs, self.profile.seed_rate_c_h)
        nxt = None if plan is None else plan.next_session
        return {
            "family": config.family,
            "heating": self.reading.heating,
            "kw": config.heater_kw,
            "rate_c_h": round(rate, 2),
            "rate_learned": learnt,
            "state": None if plan is None else plan.state,
            "ready_at": None if plan is None or plan.ready_at is None else plan.ready_at.isoformat(),
            "live": False,
            "control": self.control(config),
            "would": self.would(config),
            "not_ready": None if plan is None or plan.not_ready is None else plan.not_ready.as_dict(),
            "next": None if nxt is None else {"at": nxt[0].isoformat(), "source": nxt[1]},
            "now": None if self.now_until is None or self.now_until <= now else {"until": self.now_until.isoformat()},
            "notes": [],
        }

    def lanes(self, config: SpaConfig | SaunaConfig) -> dict[str, Any] | None:
        """What the Plan tab draws (`site/plan_view.DeadlinePlanView`)."""
        plan = self.plan
        if plan is None:
            return None
        hold: tuple[datetime, datetime] | None = None
        if plan.quarters and plan.hold_until is not None:
            hold = (max(plan.quarters) + QUARTER, plan.hold_until)
        elif plan.next_session is not None and plan.next_session[1] == SOURCE_NOW and plan.hold_until is not None:
            hold = (plan.made_at, plan.hold_until)
        return {
            "kind": self.profile.kind,
            "quarters": dict(plan.quarters),
            "hold": hold,
            "kw": config.heater_kw,
            "target_c": config.bath_c if isinstance(config, SpaConfig) else config.target_c,
            "ready_at": plan.ready_at,
            "next": None if plan.next_session is None else plan.next_session[0],
            "entity_id": config.entity_id,
        }

    def facts(self, config: SpaConfig | SaunaConfig) -> dict[str, Any]:
        plan = self.plan
        nxt = None if plan is None or plan.next_session is None else plan.next_session[0]
        out: dict[str, Any] = {"kw": config.heater_kw}
        out["next_session" if self.profile.kind == KIND_SAUNA else "next_bath"] = nxt
        if self.reading.max_on is not None:
            out["max_on_min"] = self.reading.max_on.total_seconds() / 60.0
        return out


def new_spa() -> DeadlineShadow:
    return DeadlineShadow(SPA)


def new_sauna() -> DeadlineShadow:
    return DeadlineShadow(SAUNA)
