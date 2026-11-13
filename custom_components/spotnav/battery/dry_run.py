"""The ledger and the control-failure monitor run dry beside the shadow hold: what they can tell without a write.

A shadow hold writes nothing, so neither can do its real job. What each still tells, and what it cannot:

* The ledger (`ledger.py`) adopts the value the hold control reads when the hold would be written, as if written and
  read back. It tells whether the control could be read back at all (`read_back` in the decision: a control that
  reads nothing, or a service call with no entity, could never confirm a write), and whether that value changes
  while the would-be hold lasts although nothing writes it: another system drives the control, so a real hold would
  have been given up after `MIN_DIVERGENCE_S` and its restore skipped (`changed_outside` in the log). It cannot tell
  whether the battery would have taken the write.
* The monitor (`monitor.py`) watches the would-be hold with the same grace and readings as a real one and logs once
  (`would_judge`) either the three readings it would have judged, with what the battery did without the hold
  (`baseline_no_effect`: it kept discharging, as expected with nothing written), or why it could not have judged.
  It cannot tell whether a hold would have taken; it never counts a failure in shadow.

Pure: no Home Assistant import.
"""

from __future__ import annotations

import math
from dataclasses import replace
from datetime import datetime
from typing import Final

from .facts import BatteryFacts
from .hold import HoldDecision
from .ledger import Ledger
from .monitor import Capability, Command, Monitor, MonitorEvent, Reading
from .shadow import PART_HOLD_FOR_CAR, ShadowEntry

SECTION_LEDGER: Final = "ledger"
SECTION_MONITOR: Final = "monitor"


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


class HoldDryRun:
    """The hold part's ledger and monitor, fed the would-be writes and the real readings."""

    def __init__(self, ledger: Ledger, monitor: Monitor) -> None:
        self.ledger = ledger
        self.monitor = monitor

    def decided(self, decision: HoldDecision, facts: BatteryFacts, now: datetime) -> tuple[HoldDecision, list[ShadowEntry]]:
        """The hold decided: the decision with what the dry runs add to it, and their own log entries."""
        control = facts.hold
        entity_id = None if control is None else control.entity_id
        if decision.action == "would_write":
            if control is None or entity_id is None:
                read_back = "not_possible:no_entity"
            elif self.ledger.adopt(entity_id, control.current, now, step=control.step):
                read_back = "possible"
            else:
                read_back = "not_possible:no_reading"
            event = self.monitor.start(Command("hold", now, 0.0, before_kw=facts.power_kw, written=False))
            return replace(decision, detail=decision.detail | {"read_back": read_back}), _judged(event)
        if decision.action == "would_restore":
            entries = _judged(self.monitor.stop(now))
            if entity_id is not None:
                if self.ledger.given_up(entity_id):
                    decision = replace(decision, detail=decision.detail | {"restore": "skipped:changed_outside_spotnav"})
                self.ledger.release(entity_id)
            return decision, entries
        return decision, []

    def observe(
        self, facts: BatteryFacts, held: bool, now: datetime, *, age_s: float | None, headroom_a: float | None,
        volts_between_phases: float,
    ) -> list[ShadowEntry]:
        """The real readings of one look."""
        control = facts.hold
        entries: list[ShadowEntry] = []
        if control is not None and control.entity_id is not None:
            if not held:
                # Nothing is (would be) held: no claim outlives the hold, whatever storage held.
                self.ledger.release(control.entity_id)
            event = self.ledger.observe(control.entity_id, control.current, now)
            if event is not None and event.kind == "external":
                entries.append(
                    ShadowEntry(
                        at=now,
                        part=PART_HOLD_FOR_CAR,
                        action="changed_outside",
                        reason="hold_control_changed_outside_spotnav",
                        entity_id=control.entity_id,
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
