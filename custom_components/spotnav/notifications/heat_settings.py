"""Which phones heard about the heat card's loads (the pool and the spa) before the per-phone settings: the heat
settings' `heat_notifications` field (`heat/config.py`), and its codec. Pure. Since SpotNav 3.0 the phones decide
(`phones.py`): the field is read by the one-time migration, answered from the phones and a write applied to them.

    {targets: [notify services] (up to 10, none by default), events: ["plan_at_risk", "pool_season"] (both by default)}

`plan_at_risk` is a spa bath or a sauna session that will not be ready in time (the site's combined "Blir inte klart i
tid" notice, `site_notice.py`); `pool_season` the once-a-season hint to close the pool. The targets are services of Home
Assistant's `notify` domain, as a charger's (`notifications/settings.py`); a target that no longer exists is kept and
skipped when sending.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

from .settings import _SERVICE, EVENT_PLAN_AT_RISK, MAX_TARGETS

F_HEAT_NOTIFICATIONS: Final = "heat_notifications"
#: The pool's season hint: the water has stayed cold for days, time to close the season.
EVENT_POOL_SEASON: Final = "pool_season"
#: The events the heat card's phones may hear, in the order a client lists them; both on by default.
HEAT_EVENTS: Final = (EVENT_PLAN_AT_RISK, EVENT_POOL_SEASON)

ERR_INVALID: Final = "invalid_value"
ERR_TOO_MANY: Final = "too_many"
ERR_DUPLICATE: Final = "duplicate"


@dataclass(frozen=True, slots=True)
class HeatNotifications:
    targets: tuple[str, ...] = ()
    events: tuple[str, ...] = HEAT_EVENTS

    def wants(self, event: str) -> bool:
        return bool(self.targets) and event in self.events

    def as_dict(self) -> dict[str, Any]:
        return {"targets": list(self.targets), "events": list(self.events)}


def _targets(raw: Any) -> tuple[tuple[str, ...] | None, str | None]:
    if not isinstance(raw, list) or any(not isinstance(item, str) or not _SERVICE.match(item) for item in raw):
        return None, ERR_INVALID
    if len(raw) > MAX_TARGETS:
        return None, ERR_TOO_MANY
    if len(set(raw)) != len(raw):
        return None, ERR_DUPLICATE
    return tuple(raw), None


def _events(raw: Any) -> tuple[tuple[str, ...] | None, str | None]:
    if not isinstance(raw, list) or any(item not in HEAT_EVENTS for item in raw):
        return None, ERR_INVALID
    if len(set(raw)) != len(raw):
        return None, ERR_DUPLICATE
    return tuple(event for event in HEAT_EVENTS if event in raw), None


def read_heat_notifications(raw: Any) -> HeatNotifications:
    """The stored field; anything broken reads as its default (an event this release does not know is read past)."""
    if not isinstance(raw, Mapping):
        return HeatNotifications()
    targets, _code = _targets(raw.get("targets"))
    events = raw.get("events")
    known = tuple(event for event in HEAT_EVENTS if isinstance(events, list) and event in events)
    return HeatNotifications(targets=targets or (), events=known if isinstance(events, list) else HEAT_EVENTS)


def judge_heat_notifications(current: HeatNotifications, raw: Any) -> tuple[HeatNotifications, str | None]:
    """`current` with the field `raw` applied (each key named changes, `null` the defaults), or the error code."""
    if raw is None:
        return HeatNotifications(), None
    if not isinstance(raw, Mapping) or set(raw) - {"targets", "events"}:
        return current, ERR_INVALID
    targets, events = current.targets, current.events
    if "targets" in raw:
        found, code = _targets(raw["targets"])
        if code is not None or found is None:
            return current, code or ERR_INVALID
        targets = found
    if "events" in raw:
        found, code = _events(raw["events"])
        if code is not None or found is None:
            return current, code or ERR_INVALID
        events = found
    return HeatNotifications(targets, events), None
