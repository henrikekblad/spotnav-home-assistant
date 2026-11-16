"""The ledger and the control-failure monitor run dry beside the shadow hold: what they can tell without a write.

A shadow hold writes nothing, so neither can do its real job. They are the same ledger and monitor the executor
uses (`executor.py`); since wave 7 the dry run follows the executor's own hold (`hold.would_hold`, every role it would
write: on Sigenergy the discharge limit, the Remote EMS switch and its control mode), not one discharge-limit entity
of the part's own. What each still tells, and what it cannot:

* The ledger (`ledger.py`) adopts, per role the hold would write, the value it reads when the hold would be written,
  as if written and read back. It tells whether each role could be read back at all (`read_back` in the decision,
  per role in `read_back_roles`: a role that reads nothing, or one with no entity, could never confirm a write), and
  whether a value changes while the would-be hold lasts although nothing writes it: another system drives the
  control, so a real hold would have been given up after `MIN_DIVERGENCE_S` and its restore skipped
  (`changed_outside` in the log). It cannot tell whether the battery would have taken the write.
  A Sigenergy limit that reads `unknown` (2.0.1's "never set") is no reading: it is not adopted, as the executor
  would not count it as a read-back either, and the role says `unset` (its restore would write the rating). A
  button (SolaX's trigger) has no read-back by design: `monitor_only`. A role that is not in use until an earlier
  write turns it on (Sigenergy's control mode, unavailable while Remote EMS is off) is `idle`: it can only be read
  back once a real write has turned it on.
* The monitor (`monitor.py`) watches the would-be hold with the same grace and readings as a real one and logs once
  (`would_judge`) either the three readings it would have judged, with what the battery did without the hold
  (`baseline_no_effect`: it kept discharging, as expected with nothing written), or why it could not have judged.
  It cannot tell whether a hold would have taken; it never counts a failure in shadow.

Pure: no Home Assistant import.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime
from typing import Any, Final

from .commands import control_for
from .executor import RoleRead
from .facts import BatteryFacts
from .hold import HoldDecision
from .ledger import Ledger
from .monitor import Capability, Command, Monitor, MonitorEvent, Reading
from .shadow import PART_HOLD_FOR_CAR, ShadowEntry

SECTION_LEDGER: Final = "ledger"
SECTION_MONITOR: Final = "monitor"
#: Per-role read-back verdicts that do not stop a write from being confirmed or judged.
_FINE: Final = frozenset({"possible", "unset", "monitor_only", "idle"})


def site_cap_kw(headroom_a: float | None, volts_between_phases: float) -> float | None:
    """The fuse headroom (A per phase, three phases) as kW."""
    if headroom_a is None or volts_between_phases <= 0:
        return None
    return max(headroom_a, 0.0) * math.sqrt(3.0) * volts_between_phases / 1000.0


def _judged(event: MonitorEvent | None) -> list[ShadowEntry]:
    if event is None:
        return []
    return [
        ShadowEntry(
            at=event.at,
            part=PART_HOLD_FOR_CAR,
            action="would_judge",
            reason=event.outcome,
            detail={"dry_run": True} | event.as_dict(),
        )
    ]


def _states(reads: Mapping[str, RoleRead]) -> dict[str, str | None]:
    return {read.entity_id: read.state for read in reads.values() if read.entity_id is not None}


class HoldDryRun:
    """The hold part's ledger and monitor, fed the would-be writes and the real readings."""

    def __init__(self, ledger: Ledger, monitor: Monitor) -> None:
        self.ledger = ledger
        self.monitor = monitor

    def _adopt(self, facts: BatteryFacts, write: Mapping[str, Any], now: datetime) -> str:
        control = control_for(facts.brand)
        role = None if control is None else control.role(str(write.get("role")))
        entity_id, read = write.get("entity_id"), write.get("read")
        if role is not None and role.method == "button":
            return "monitor_only"
        if not isinstance(entity_id, str):
            return "not_possible:no_entity"
        if role is not None and isinstance(read, str) and read in role.idle_when:
            # Not in use until an earlier write turns it on (Sigenergy's mode select while Remote EMS is off).
            return "idle"
        if role is not None and role.unknown_is_unset and isinstance(read, str) and read.strip().lower() == "unknown":
            return "unset"
        step = None if role is None else role.step
        if self.ledger.adopt(entity_id, read if isinstance(read, str) else None, now, step=step):
            return "possible"
        return "not_possible:no_reading"

    def decided(
        self, decision: HoldDecision, facts: BatteryFacts, now: datetime, variant: str | None = None
    ) -> tuple[HoldDecision, list[ShadowEntry]]:
        """The hold decided: the decision with what the dry runs add to it, and their own log entries (`variant`: the
        hold variant control would run)."""
        if decision.action == "would_write":
            writes = decision.detail.get("writes") or []
            roles = {str(write.get("role")): self._adopt(facts, write, now) for write in writes if isinstance(write, Mapping)}
            failing = [f"{verdict}:{role}" for role, verdict in roles.items() if verdict not in _FINE]
            read_back = failing[0] if failing else "possible" if roles else "not_possible:no_entity"
            control = control_for(facts.brand, variant)
            idle = control is not None and control.hold_idle()
            event = self.monitor.start(Command("hold", now, 0.0, before_kw=facts.power_kw, written=False, idle=idle))
            detail = decision.detail | {"read_back": read_back, "read_back_roles": roles}
            return replace(decision, detail=detail), _judged(event)
        if decision.action == "would_restore":
            entries = _judged(self.monitor.stop(now))
            given_up = [entity_id for entity_id in self.ledger.entity_ids() if self.ledger.given_up(entity_id)]
            if given_up:
                decision = replace(
                    decision,
                    detail=decision.detail | {"restore": "skipped:changed_outside_spotnav", "skipped": given_up},
                )
            for entity_id in self.ledger.entity_ids():
                self.ledger.release(entity_id)
            return decision, entries
        return decision, []

    def observe(
        self, facts: BatteryFacts, reads: Mapping[str, RoleRead], held: bool, now: datetime, *, age_s: float | None,
        headroom_a: float | None, volts_between_phases: float,
    ) -> list[ShadowEntry]:
        """The real readings of one look."""
        entries: list[ShadowEntry] = []
        states = _states(reads)
        for entity_id in self.ledger.entity_ids():
            if not held:
                # Nothing is (would be) held: no claim outlives the hold, whatever storage held.
                self.ledger.release(entity_id)
                continue
            # A role no longer found reads nothing: no evidence either way.
            event = self.ledger.observe(entity_id, states.get(entity_id), now)
            if event is not None and event.kind == "external":
                entries.append(
                    ShadowEntry(
                        at=now,
                        part=PART_HOLD_FOR_CAR,
                        action="changed_outside",
                        reason="hold_control_changed_outside_spotnav",
                        entity_id=entity_id,
                        value=event.read,
                        detail={"dry_run": True} | event.as_dict(),
                        read=event.read,
                    )
                )
        if not held and self.monitor.watch is not None:
            entries += _judged(self.monitor.stop(now))
        capability = Capability(
            rated_charge_kw=facts.rated_charge_kw,
            rated_discharge_kw=facts.rated_discharge_kw,
            site_cap_kw=site_cap_kw(headroom_a, volts_between_phases),
        )
        entries += _judged(self.monitor.observe(Reading(facts.power_kw, age_s, facts.soc_pct), capability, now))
        return entries
