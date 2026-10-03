"""What happened at a charger, as discrete events for Home Assistant automations.

A pure tracker: `ChargerEventTracker.observe` is given the charger's facts and answers with the
events that the change since the last call makes. It performs no I/O and has no clock. The event
entity (`event.py`) feeds it from the controller's listeners.

The events, `CHARGER_EVENT_TYPES`:

* `plugged_in` / `unplugged`: the vehicle's connection changed, for a charger that can say whether a
  vehicle is connected (an unknown reading is never a change);
* `charge_started` / `charge_finished`: the charger began / stopped charging. `finished` says whether
  the vehicle was unplugged then, and how much energy the charger's register counted since the start;
* `plan_installed`: a charging plan that is not the one before it was installed (by Auto or by hand);
* `plan_at_risk`: the departure cannot be met with the energy that remains. Once per time it becomes so.

The first observation only sets the baseline: restarting Home Assistant is not an event.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Final

EVENT_CHARGE_STARTED: Final = "charge_started"
EVENT_CHARGE_FINISHED: Final = "charge_finished"
EVENT_PLUGGED_IN: Final = "plugged_in"
EVENT_UNPLUGGED: Final = "unplugged"
EVENT_PLAN_INSTALLED: Final = "plan_installed"
EVENT_PLAN_AT_RISK: Final = "plan_at_risk"
CHARGER_EVENT_TYPES: Final = (
    EVENT_CHARGE_STARTED,
    EVENT_CHARGE_FINISHED,
    EVENT_PLUGGED_IN,
    EVENT_UNPLUGGED,
    EVENT_PLAN_INSTALLED,
    EVENT_PLAN_AT_RISK,
)

type Event = tuple[str, dict[str, Any]]


@dataclass(frozen=True, slots=True)
class ChargerFacts:
    """One observation of a charger."""

    charging: bool
    #: Whether a vehicle is plugged in, `None` when the charger cannot say.
    connected: bool | None
    #: What identifies the installed plan (`None` without one); a change of it is a new plan.
    plan_key: str | None = None
    #: What an automation may want to know about that plan.
    plan: dict[str, Any] = field(default_factory=dict)
    #: Whether the departure cannot be met; `at_risk` carries what to tell about it.
    at_risk: bool = False
    at_risk_info: dict[str, Any] = field(default_factory=dict)
    #: The charger's cumulative energy register in kWh, when it has one.
    register_kwh: float | None = None


class ChargerEventTracker:
    """Turns successive `ChargerFacts` into events."""

    def __init__(self) -> None:
        self._last: ChargerFacts | None = None
        self._register_at_start: float | None = None

    def observe(self, facts: ChargerFacts) -> list[Event]:
        before = self._last
        self._last = facts
        if before is None:
            if facts.charging:
                self._register_at_start = facts.register_kwh
            return []
        events: list[Event] = []
        if before.connected is not None and facts.connected is not None and before.connected != facts.connected:
            events.append((EVENT_PLUGGED_IN if facts.connected else EVENT_UNPLUGGED, {}))
        if not before.charging and facts.charging:
            self._register_at_start = facts.register_kwh
            events.append((EVENT_CHARGE_STARTED, {"energy_register_kwh": facts.register_kwh}))
        elif before.charging and not facts.charging:
            attributes: dict[str, Any] = {"unplugged": facts.connected is False}
            if self._register_at_start is not None and facts.register_kwh is not None:
                attributes["energy_kwh"] = round(max(facts.register_kwh - self._register_at_start, 0.0), 3)
            self._register_at_start = None
            events.append((EVENT_CHARGE_FINISHED, attributes))
        if facts.plan_key is not None and facts.plan_key != before.plan_key:
            events.append((EVENT_PLAN_INSTALLED, dict(facts.plan)))
        if facts.at_risk and not before.at_risk:
            events.append((EVENT_PLAN_AT_RISK, dict(facts.at_risk_info)))
        return events
