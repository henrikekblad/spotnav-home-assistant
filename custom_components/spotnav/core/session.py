"""One charger's charge session as one frozen record: who owns the charge and which person intent holds.

Pure: no Home Assistant imports. Today the same facts live in about fifteen fields across
`ChargingController`, `WindowHold` and the Auto settings' pause (`plans/research_integrations/
charger_state_machine_2026-10-04.md`, section 1); this record names each once:

* `plugged`: the last connection the charger stated in so many words (`None` until one is known, as after a
  restart: a status that says nothing is never a plug-in or an unplug).
* `owner`: who owns the charge that runs (or would run): nobody, a plan window, the sun, a person (their
  Start under the boundary), the charger by itself, a top-off past the plan's last window, or a Charge-now
  start that came in through the controller with no boundary (a button with no Auto, a webhook start).
* `manual`: a person's Start or Stop pauses Auto for the plug-in session (`action`, and `scope`: the plug-in
  the car is in, or the next one for a Stop given with no car).
* `span_pause`: a pause a person picked for a span (next period, until tomorrow, until resumed). One pause is
  stored at a time, so `manual` and `span_pause` are never both set.
* `held`/`overridden`: the hold of a charge that began by itself outside a window (`window_hold.py`), and a
  person's override of it.
* `car_ended_at`: the car ended a person's charge by itself in this plug-in (R3).
* `balancing_paused`/`paused_origin`: load balancing holds a charge back, and whose it was.
* `held_for_safety`/`safety_stopped_at`: the hold was a safety stop (not the below-floor pause): resumed no
  sooner than its gap.
* `hold_stop_*`: the stops sent under a person's Stop of a charge the charger began by itself (C7, R5): when
  they went out, when the last was tried, whether SpotNav gave up, and whether one is on its way.
* `pending`: the commands the core asked for whose results have not come back yet (`ownership.CommandResult`).

Serialised as JSON with a version field (`to_dict`/`from_dict`); a record of another version is refused.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields, replace
from datetime import datetime
import json
from typing import Any, Final

#: The version of the serialised record (`to_dict`).
SESSION_VERSION: Final = 1

OWNER_NONE: Final = "none"
OWNER_PLAN: Final = "plan"
OWNER_SOLAR: Final = "solar"
OWNER_PERSON: Final = "person"
OWNER_CHARGER_SELF: Final = "charger_self"
OWNER_TOP_OFF: Final = "top_off"
OWNER_CHARGE_NOW: Final = "charge_now"
OWNERS: Final = (
    OWNER_NONE,
    OWNER_PLAN,
    OWNER_SOLAR,
    OWNER_PERSON,
    OWNER_CHARGER_SELF,
    OWNER_TOP_OFF,
    OWNER_CHARGE_NOW,
)
#: Owners whose charge SpotNav started (the hold leaves such a charge alone).
SPOTNAV_OWNERS: Final = frozenset({OWNER_PLAN, OWNER_SOLAR, OWNER_PERSON, OWNER_TOP_OFF, OWNER_CHARGE_NOW})

MANUAL_START: Final = "start"
MANUAL_STOP: Final = "stop"
MANUAL_ACTIONS: Final = (MANUAL_START, MANUAL_STOP)
SCOPE_PLUG_IN: Final = "plug_in"
SCOPE_NEXT_PLUG_IN: Final = "next_plug_in"
MANUAL_SCOPES: Final = (SCOPE_PLUG_IN, SCOPE_NEXT_PLUG_IN)

SPAN_NEXT_PERIOD: Final = "next_period"
SPAN_UNTIL_TOMORROW: Final = "until_tomorrow"
SPAN_UNTIL_RESUMED: Final = "until_resumed"
SPAN_CHOICES: Final = (SPAN_NEXT_PERIOD, SPAN_UNTIL_TOMORROW, SPAN_UNTIL_RESUMED)


class SessionError(ValueError):
    """A serialised session that cannot be read: another version, or a field of the wrong shape."""


@dataclass(frozen=True)
class ManualPause:
    """A person's Start or Stop pausing Auto for a plug-in session."""

    action: str
    scope: str

    def __post_init__(self) -> None:
        if self.action not in MANUAL_ACTIONS or self.scope not in MANUAL_SCOPES:
            raise SessionError(f"not a manual pause: {self.action!r} {self.scope!r}")


@dataclass(frozen=True)
class PendingCommand:
    """A command the core asked for, awaiting its result: what it was and what the result needs to know."""

    command: str
    reason: str
    owner_before: str = OWNER_NONE
    #: For a start: who owns the charge once it went out.
    owner_after: str | None = None
    #: For a balancing resume: whose charge it gives back (`None`: nobody's).
    paused_origin: str | None = None
    #: For a balancing stop: whether the charge was on; for a re-arm stop: whether it was charging.
    was_on: bool = False
    #: For a balancing stop: `pause` (below the floor) or a safety stop's code.
    code: str | None = None
    #: For a stop: whether it clears the plan too.
    clear_schedule: bool = False


@dataclass(frozen=True)
class ChargeSession:
    """One charger's ownership and person intent. Frozen: `ownership.decide` returns a new one."""

    plugged: bool | None = None
    owner: str = OWNER_NONE
    manual: ManualPause | None = None
    span_pause: str | None = None
    held: bool = False
    overridden: bool = False
    car_ended_at: datetime | None = None
    balancing_paused: bool = False
    paused_origin: str | None = None
    held_for_safety: bool = False
    safety_stopped_at: datetime | None = None
    hold_stop_times: tuple[datetime, ...] = field(default=())
    hold_tried_at: datetime | None = None
    hold_gave_up: bool = False
    hold_stop_pending: bool = False
    pending: tuple[PendingCommand, ...] = field(default=())

    def __post_init__(self) -> None:
        if self.owner not in OWNERS:
            raise SessionError(f"not an owner: {self.owner!r}")
        if self.span_pause is not None and self.span_pause not in SPAN_CHOICES:
            raise SessionError(f"not a pause choice: {self.span_pause!r}")
        if self.manual is not None and self.span_pause is not None:
            raise SessionError("one pause at a time: a manual pause and a span pause")
        if self.paused_origin is not None and self.paused_origin not in OWNERS:
            raise SessionError(f"not an owner: {self.paused_origin!r}")

    # ------------------------------------------------------------------ queries

    @property
    def paused(self) -> bool:
        """Whether a pause holds Auto's execution (a person's Start or Stop, or a span they picked)."""
        return self.manual is not None or self.span_pause is not None

    @property
    def held_off_by_person(self) -> bool:
        """Whether a person's Stop pauses Auto: any charge the charger begins is stopped (C7)."""
        return self.manual is not None and self.manual.action == MANUAL_STOP

    @property
    def start_pending(self) -> bool:
        """Whether a start the core asked for has not come back: the charge that runs is that start's."""
        return any(command.command == "start" for command in self.pending)

    @property
    def person_started(self) -> bool:
        """Whether a person's Start pauses Auto: nothing automatic stops their charge."""
        return self.manual is not None and self.manual.action == MANUAL_START

    def with_changes(self, **changes: Any) -> ChargeSession:
        return replace(self, **changes)

    # ------------------------------------------------------------------ serialisation

    def to_dict(self) -> dict[str, Any]:
        """Plain JSON values, with the version. Every field is written, so a reader needs no defaults."""
        return {
            "version": SESSION_VERSION,
            "plugged": self.plugged,
            "owner": self.owner,
            "manual": None
            if self.manual is None
            else {"action": self.manual.action, "scope": self.manual.scope},
            "span_pause": self.span_pause,
            "held": self.held,
            "overridden": self.overridden,
            "car_ended_at": _iso(self.car_ended_at),
            "balancing_paused": self.balancing_paused,
            "paused_origin": self.paused_origin,
            "held_for_safety": self.held_for_safety,
            "safety_stopped_at": _iso(self.safety_stopped_at),
            "hold_stop_times": [_iso(moment) for moment in self.hold_stop_times],
            "hold_tried_at": _iso(self.hold_tried_at),
            "hold_gave_up": self.hold_gave_up,
            "hold_stop_pending": self.hold_stop_pending,
            "pending": [
                {
                    "command": command.command,
                    "reason": command.reason,
                    "owner_before": command.owner_before,
                    "owner_after": command.owner_after,
                    "paused_origin": command.paused_origin,
                    "was_on": command.was_on,
                    "code": command.code,
                    "clear_schedule": command.clear_schedule,
                }
                for command in self.pending
            ],
        }

    @classmethod
    def from_dict(cls, raw: Any) -> ChargeSession:
        """A session from `to_dict`'s output, or `SessionError`: every field checked, none coerced."""
        if not isinstance(raw, dict):
            raise SessionError("a session is an object")
        if raw.get("version") != SESSION_VERSION:
            raise SessionError(f"session version {raw.get('version')!r} is not {SESSION_VERSION}")
        expected = {"version"} | {item.name for item in fields(cls)}
        if set(raw) != expected:
            raise SessionError(f"session fields differ: {sorted(set(raw) ^ expected)}")
        manual = raw["manual"]
        try:
            return cls(
                plugged=_optional_bool(raw["plugged"], "plugged"),
                owner=_text(raw["owner"], "owner"),
                manual=None
                if manual is None
                else ManualPause(_text(_member(manual, "action"), "action"), _text(_member(manual, "scope"), "scope")),
                span_pause=_optional_text(raw["span_pause"], "span_pause"),
                held=_bool(raw["held"], "held"),
                overridden=_bool(raw["overridden"], "overridden"),
                car_ended_at=_instant(raw["car_ended_at"], "car_ended_at"),
                balancing_paused=_bool(raw["balancing_paused"], "balancing_paused"),
                paused_origin=_optional_text(raw["paused_origin"], "paused_origin"),
                held_for_safety=_bool(raw["held_for_safety"], "held_for_safety"),
                safety_stopped_at=_instant(raw["safety_stopped_at"], "safety_stopped_at"),
                hold_stop_times=tuple(_required_instant(value, "hold_stop_times") for value in _list(raw["hold_stop_times"])),
                hold_tried_at=_instant(raw["hold_tried_at"], "hold_tried_at"),
                hold_gave_up=_bool(raw["hold_gave_up"], "hold_gave_up"),
                hold_stop_pending=_bool(raw["hold_stop_pending"], "hold_stop_pending"),
                pending=tuple(
                    PendingCommand(
                        command=_text(_member(pending, "command"), "command"),
                        reason=_text(_member(pending, "reason"), "reason"),
                        owner_before=_text(_member(pending, "owner_before"), "owner_before"),
                        owner_after=_optional_text(_member(pending, "owner_after"), "owner_after"),
                        paused_origin=_optional_text(_member(pending, "paused_origin"), "paused_origin"),
                        was_on=_bool(_member(pending, "was_on"), "was_on"),
                        code=_optional_text(_member(pending, "code"), "code"),
                        clear_schedule=_bool(_member(pending, "clear_schedule"), "clear_schedule"),
                    )
                    for pending in _list(raw["pending"])
                ),
            )
        except SessionError:
            raise
        except (TypeError, ValueError) as err:
            raise SessionError(str(err)) from err

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_json(cls, text: str) -> ChargeSession:
        try:
            raw = json.loads(text)
        except ValueError as err:
            raise SessionError("not JSON") from err
        return cls.from_dict(raw)


def _iso(moment: datetime | None) -> str | None:
    return None if moment is None else moment.isoformat()


def _member(raw: Any, key: str) -> Any:
    if not isinstance(raw, dict) or key not in raw:
        raise SessionError(f"missing {key!r}")
    return raw[key]


def _bool(value: Any, what: str) -> bool:
    if not isinstance(value, bool):
        raise SessionError(f"{what} is not a boolean")
    return value


def _optional_bool(value: Any, what: str) -> bool | None:
    return None if value is None else _bool(value, what)


def _text(value: Any, what: str) -> str:
    if not isinstance(value, str):
        raise SessionError(f"{what} is not text")
    return value


def _optional_text(value: Any, what: str) -> str | None:
    return None if value is None else _text(value, what)


def _list(value: Any) -> list[Any]:
    if not isinstance(value, list):
        raise SessionError("not a list")
    return value


def _instant(value: Any, what: str) -> datetime | None:
    if value is None:
        return None
    return _required_instant(value, what)


def _required_instant(value: Any, what: str) -> datetime:
    if not isinstance(value, str):
        raise SessionError(f"{what} is not an instant")
    try:
        moment = datetime.fromisoformat(value)
    except ValueError as err:
        raise SessionError(f"{what} is not an instant") from err
    if moment.tzinfo is None:
        raise SessionError(f"{what} must be timezone-aware")
    return moment
