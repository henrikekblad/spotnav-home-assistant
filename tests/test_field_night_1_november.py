"""A plan charge and a home battery that charges from the grid and holds it at the fuse: the field night of 2026-11-01.

At 05:15 the next window's charge was paused, the charger reported its charge once more before it answered, the window
claimed that report as the plan's charge, and the claim forgot that load balancing held it back: no resume for two
hours, and a notice that the charge "did not start as planned".
"""

from __future__ import annotations

from homeassistant.util import dt as dt_util

from custom_components.spotnav.core.events import BalancingPause, CommandResult, Recheck
from custom_components.spotnav.core.ownership import decide
from custom_components.spotnav.core.session import ChargeSession


# -- the core's claim


def test_a_claim_keeps_the_charge_load_balancing_holds_back() -> None:
    """A charger that reports its charge once more before it answers a balancing pause, inside a window: the claim
    makes the charge the plan's and sends nothing, and load balancing still holds it back (as today's claim leaves
    `_paused_by_balancing`)."""
    now = dt_util.utcnow()
    session = ChargeSession(plugged=True, owner="plan")
    session, _ = decide(session, BalancingPause(code="pause", was_on=True), now)
    session, _ = decide(session, CommandResult(command="stop", reason="balancing", executed=True), now)
    assert session.balancing_paused and session.paused_origin == "plan" and session.owner == "none"
    session, commands = decide(
        session,
        Recheck(what="claim", control_on=True, window_open=True, in_window=True, plan_present=True),
        now,
    )
    assert [(command.kind, command.reason) for command in commands] == [("start", "claim")]
    session, _ = decide(session, CommandResult(command="start", reason="claim", executed=True), now)
    assert session.owner == "plan"
    assert session.balancing_paused and session.paused_origin == "plan"
