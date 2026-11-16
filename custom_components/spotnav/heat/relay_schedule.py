"""When the heat pump's relay is expected to be closed (plan `plans/v3_heating_plan.md` step 2): SpotNav knows the
relay it was told about (the heat settings' control entity), and whoever closes it, the forecast counts the lowered
hours. Pure.

* **The SpotNav blueprint** (`relay_by_price.yaml`, mode "closed while expensive") is read from its automation's
  configuration with the inputs filled in (`blueprint_rule`): the relay, the price's Expensive now sensor, the
  longest closed time, the shortest rest, the outdoor sensor and its lowest temperature. Its closings ahead are
  simulated the way the automation decides (`expected_closings`): closed while the price's expensive intervals run,
  opened after the longest closed time, closed again only after the shortest rest, never while too cold (the outdoor
  reading now below the lowest, or no reading from a chosen sensor; the forecast's outdoor ahead is not counted, as
  the automation does not count it either).
* **Anyone else** (another automation, a person, the alarm system): only the closing now is known, and it is
  expected to last at most `UNKNOWN_CLOSED` from its start (the heating plan's longest lowered run), so a relay left
  closed is not counted as lowered for the rest of the day (`unknown_closing`).

Nothing here switches the relay: SpotNav only watches it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final

#: The SpotNav blueprint, as an automation references it.
BLUEPRINT_PATH: Final = "spotnav/relay_by_price.yaml"
MODE_EXPENSIVE_CLOSED: Final = "expensive_closed"
MODE_SG_READY: Final = "sg_ready"
UNKNOWN_CLOSED: Final = timedelta(hours=3)
STEP: Final = timedelta(minutes=5)
#: How far ahead the closings are simulated (the prices rarely reach further).
HORIZON: Final = timedelta(hours=48)


@dataclass(frozen=True, slots=True)
class BlueprintRule:
    """The blueprint automation's inputs that decide when relay A is closed."""

    relay: str
    mode: str
    expensive_now: str | None
    max_on: timedelta
    min_rest: timedelta
    outdoor_sensor: str | None
    min_outdoor_c: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "relay": self.relay,
            "mode": self.mode,
            "expensive_now": self.expensive_now,
            "max_on_h": self.max_on.total_seconds() / 3600.0,
            "min_rest_h": self.min_rest.total_seconds() / 3600.0,
            "outdoor_sensor": self.outdoor_sensor,
            "min_outdoor_c": self.min_outdoor_c,
        }


def _hours(value: Any, default: float) -> timedelta:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = default
    return timedelta(hours=max(0.0, number))


def _entity(value: Any) -> str | None:
    return value if isinstance(value, str) and "." in value else None


def blueprint_rule(raw_config: Mapping[str, Any] | None) -> BlueprintRule | None:
    """The rule from a SpotNav blueprint automation's configuration with its inputs filled in (its `variables`, which
    the blueprint sets from its inputs), `None` when it is not one that lowers (pulse mode) or cannot be read."""
    if not isinstance(raw_config, Mapping):
        return None
    variables = raw_config.get("variables")
    if not isinstance(variables, Mapping):
        return None
    mode = variables.get("relay_mode")
    relay = _entity(variables.get("relay"))
    if relay is None or mode not in (MODE_EXPENSIVE_CLOSED, MODE_SG_READY):
        return None
    try:
        min_outdoor = float(variables.get("min_outdoor", -15))
    except (TypeError, ValueError):
        min_outdoor = -15.0
    return BlueprintRule(
        relay=relay,
        mode=str(mode),
        expensive_now=_entity(variables.get("expensive_now")),
        max_on=_hours(variables.get("max_on_hours"), 3.0),
        min_rest=_hours(variables.get("min_rest_hours"), 1.0),
        outdoor_sensor=_entity(variables.get("outdoor_sensor")),
        min_outdoor_c=min_outdoor,
    )


def too_cold(rule: BlueprintRule, outdoor_c: float | None) -> bool:
    """As the blueprint: with an outdoor sensor chosen, no reading or one below the lowest is too cold."""
    if rule.outdoor_sensor is None:
        return False
    return outdoor_c is None or outdoor_c < rule.min_outdoor_c


def _expensive_at(moment: datetime, expensive: Sequence[tuple[datetime, datetime]]) -> bool:
    return any(start <= moment < end for start, end in expensive)


def _next_step(moment: datetime) -> datetime:
    """The next 5-minute boundary after `moment`."""
    base = moment.replace(second=0, microsecond=0)
    return base + timedelta(minutes=5 - base.minute % 5)


def expected_closings(
    now: datetime,
    rule: BlueprintRule,
    *,
    closed_now: bool,
    changed_at: datetime | None,
    expensive: Sequence[tuple[datetime, datetime]],
    cold: bool,
    horizon_end: datetime | None = None,
) -> list[tuple[datetime, datetime]]:
    """The relay's closed intervals from `now` on as the blueprint would switch it (see the module); a closing running
    now starts at `changed_at`. `expensive` are the price's expensive intervals; `cold` whether it is too cold now."""
    end = horizon_end or now + HORIZON
    last_expensive = max((stop for _start, stop in expensive), default=now)
    end = min(end, max(now, last_expensive) + rule.max_on)
    on = closed_now
    since = changed_at or now
    out: list[tuple[datetime, datetime]] = []
    # Never switched as far as is known: no rest is owed before the first closing (as the blueprint).
    rest_owed = changed_at is not None
    moment = now
    while moment < end:
        expensive_now = _expensive_at(moment, expensive)
        if on:
            if not expensive_now or cold or moment - since >= rule.max_on:
                out.append((since, moment))
                on, since, rest_owed = False, moment, True
        elif expensive_now and not cold and (not rest_owed or moment - since >= rule.min_rest):
            on, since = True, moment
        following = _next_step(moment)
        if on and since + rule.max_on < following:
            following = max(moment + timedelta(seconds=1), since + rule.max_on)
        moment = following
    if on:
        out.append((since, max(end, since)))
    return [(start, stop) for start, stop in out if stop > start]


def unknown_closing(now: datetime, *, closed_now: bool, changed_at: datetime | None) -> list[tuple[datetime, datetime]]:
    """A closing by someone SpotNav cannot read: from its start to at most `UNKNOWN_CLOSED` after it (see the
    module); nothing when the relay is open."""
    if not closed_now:
        return []
    start = changed_at or now
    return [(start, max(now, start + UNKNOWN_CLOSED))]
