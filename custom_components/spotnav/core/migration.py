"""A charge session read from a charger's record as the logic before the core kept it, once.

Pure: no Home Assistant imports. A charger that never stored the core's own record (`charge_session`), one whose record
could not be read, or one an earlier build said was older than today's keys, is migrated once from today's ownership
keys (`plan_charge`, `charge_origin`, `top_off_until`, `balancing_pause`, `hold`, `car_ended_*`) and the person intent
the execution boundary keeps (`PauseIntent`, or `person_stopped`, which a release before the boundary's pause stored).
`migrate_from_keys` reads the stored dict only. The controller still writes those keys, derived from the session, for
one release more, so a rollback reads a correct state; this reader stays until no release line needs it.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
import math
from typing import Any

from .ownership import ORIGIN_OWNER
from .session import (
    CarEnded,
    CarEndedWait,
    ChargeSession,
    ManualPause,
    MANUAL_STOP,
    OWNER_CHARGER_SELF,
    OWNER_NONE,
    OWNER_PLAN,
    OWNER_TOP_OFF,
    SCOPE_PLUG_IN,
)


def legacy_owner(
    *,
    origin: str | None,
    top_off: bool,
    charging: bool = False,
    start_pending: bool = False,
    stop_recent: bool = False,
) -> str:
    """The owner the keys name, in the core's words: from `charge_origin`, a top-off past the last window, and, with no
    origin, a charge the charger began by itself (charging, no start or stop of ours on its way)."""
    owner = ORIGIN_OWNER.get(origin) if origin is not None else None
    if owner == OWNER_PLAN and top_off:
        return OWNER_TOP_OFF
    if owner is not None:
        return owner
    if charging and not start_pending and not stop_recent:
        return OWNER_CHARGER_SELF
    return OWNER_NONE


def legacy_intent(pause: Any, legacy_person_stopped: bool) -> tuple[ManualPause | None, str | None]:
    """The person intent from the execution boundary's stored pause (`PauseIntent`), or a person's Stop an older
    release stored that the boundary has not taken over yet."""
    if pause is not None and getattr(pause, "manual", False):
        try:
            return ManualPause(pause.action, pause.scope), None
        except ValueError:
            return None, None
    if pause is not None and getattr(pause, "admitted", False):
        return None, pause.choice
    if legacy_person_stopped:
        return ManualPause(MANUAL_STOP, SCOPE_PLUG_IN), None
    return None, None


def _instant(value: Any) -> datetime | None:
    """A stored instant with its time zone, else `None` (storage is untrusted)."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if math.isfinite(parsed) else None


def stored_car_ended_wait(raw: Any) -> CarEndedWait | None:
    """A stored `car_ended_wait`, `{"target", "level", "estimated"}`, or `None` when it is not one."""
    if not isinstance(raw, dict):
        return None
    target = _number(raw.get("target"))
    level = raw.get("level")
    level = None if level is None or isinstance(level, bool) else _number(level)
    estimated = raw.get("estimated")
    if target is None or not 0.0 < target <= 100.0 or not isinstance(estimated, bool):
        return None
    if raw.get("level") is not None and level is None:
        return None
    return CarEndedWait(target=target, level=level, estimated=estimated)


def car_ended_from_keys(saved: Mapping[str, Any]) -> CarEnded | None:
    """The car-ended record today's `car_ended_*` keys hold (`None`: nothing ended)."""
    at = _instant(saved.get("car_ended_at"))
    if at is None:
        return None
    soc = saved.get("car_ended_soc")
    vehicle = saved.get("car_ended_vehicle")
    return CarEnded(
        at=at,
        soc=float(soc) if isinstance(soc, (int, float)) and not isinstance(soc, bool) else None,
        vehicle=vehicle if isinstance(vehicle, str) else None,
        wait=stored_car_ended_wait(saved.get("car_ended_wait")),
    )


def migrate_from_keys(saved: Mapping[str, Any], pause: Any = None) -> ChargeSession:
    """The session today's keys in `saved` describe, with the person intent from the boundary's `pause` (or a
    `person_stopped` an older release stored). Whether the charger charges by itself, the connection and what is
    transient are seen again after a restart, not read here."""
    top_off = _instant(saved.get("top_off_until")) is not None and bool(saved.get("plan"))
    origin = saved.get("charge_origin")
    owner = legacy_owner(origin=origin if isinstance(origin, str) else None, top_off=top_off)
    balancing = saved.get("balancing_pause")
    paused_origin: str | None = None
    if isinstance(balancing, dict):
        paused = balancing.get("origin")
        named = legacy_owner(origin=paused if isinstance(paused, str) else None, top_off=top_off)
        paused_origin = None if named == OWNER_NONE else named
    hold = saved.get("hold")
    held = isinstance(hold, dict) and hold.get("held") is True
    overridden = isinstance(hold, dict) and hold.get("overridden") is True
    manual, span = legacy_intent(pause, saved.get("person_stopped") is True)
    return ChargeSession(
        owner=owner,
        manual=manual,
        span_pause=span,
        held=held,
        overridden=overridden,
        car_ended=car_ended_from_keys(saved),
        balancing_paused=isinstance(balancing, dict),
        paused_origin=paused_origin,
    )
