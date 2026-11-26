"""Who manages the battery as the executor reads it: whether the person's own strategy was taken over, and why a
management state stops a start."""

from __future__ import annotations

from ..commands import BrandControl
from ..facts import BatteryFacts


def taken_over(facts: BatteryFacts, control: BrandControl) -> bool:
    """Whether the battery runs only its own optimiser in a mode SpotNav may take it over from
    (`BrandControl.takeover`: Sigenergy's AI mode), with every other mode setting free."""
    management = facts.management
    if management.state != "self_managed" or not control.takeover:
        return False
    seen = False
    for rule in control.adapter.modes:
        if rule.label not in management.settings:
            continue
        state = management.settings[rule.label]
        if state in rule.free:
            continue
        if state is not None and state in control.takeover.get(rule.label, frozenset()):
            seen = True
            continue
        return False
    return seen


def management_problem(
    facts: BatteryFacts, control: BrandControl, active: bool, *, takeover: bool = False
) -> str | None:
    """Why the battery is not SpotNav's to run now; `unknown` when a setting cannot be read. `takeover`: the person
    acknowledged that SpotNav may take the plant over from its own optimiser (`taken_over`), and for a plant whose
    own strategy cannot be read (`BrandControl.strategy_unread`) that SpotNav overrides whatever strategy runs:
    without it such a plant is never SpotNav's to run (`strategy_unread`)."""
    if control.strategy_unread and not takeover:
        return "strategy_unread"
    management = facts.management
    if management.state == "free":
        return None
    if takeover and taken_over(facts, control):
        return None
    if management.state == "externally_controlled" or management.reason.startswith("planner:") or not active:
        # Another system drives it (`outside_control.py`): SpotNav only watches, whatever the modes read.
        return f"{management.state}:{management.reason}"
    for rule in control.adapter.modes:
        if rule.label not in management.settings:
            continue
        state = management.settings[rule.label]
        if state is None or state.strip().lower() in ("unavailable", "unknown", ""):
            return "unknown"
        if state in rule.free or state in control.own_states.get(rule.label, frozenset()):
            continue
        return f"self_managed:{rule.label}:{state}"
    return None if management.settings else f"{management.state}:{management.reason}"
