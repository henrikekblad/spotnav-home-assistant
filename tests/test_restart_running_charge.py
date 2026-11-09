"""A charge running across a restart is never left hanging (2.5.1).

The field case of 2026-11-08 23:51: the plan's charge ran on an OCPP charger when Home Assistant restarted. The first
calculation ran before the charger's record was read back, so its executor saw no plan, installed a fresh one (with no
window open now) and saved it over the record: the charge's owner and its requested current were gone before the
restore read them. The restore then read that save, the charge became the charger's own, and load balancing had no
request to work from (`requested_current_unknown`), so the charge ran unregulated. The stop the new plan asked for went
out once the charger was ready, but the OCPP integration did not know the transaction yet and took the stop as done
with nothing sent; nothing looked at the charger again.

Now:
* the record is read back before the first calculation, which reconciles with the restored plan (the same plan is
  kept, its open window's charge goes on as the plan's, with its request);
* a charge outside the plan's windows is stopped once the charger can be commanded, and a stop the charger did not
  take (still charging `STOP_CHECK_S` later, never seen off) is decided again;
* a running charge with no current on record is reckoned by load balancing at a safe current, never unknown
  (`test_restart_record_and_request`).

The Session harness of `test_stale_plan_on_wait`: real prices, settings, preview, boundary and controller, restarted in
`async_setup_entry`'s order.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.spotnav.core.session import OWNER_PLAN
from custom_components.spotnav.execution.controller import STOP_CHECK_S, STOP_CHECK_TRIES

from .harness import Session
from .test_stale_plan_on_wait import (  # noqa: F401 - the module's clock fixture, used here too
    _charging_window,
    _expects_nothing,
    _restart,
    both_restarts,
    thursday_evening,
)

pytestmark = pytest.mark.usefixtures("offline_relay")


def _kinds(session: Session) -> list[str]:
    return [record["event"]["kind"] for record in session.controller.session_runner.events]


async def _after_check(session: Session) -> None:
    """`STOP_CHECK_S` pass, on the harness clock and on Home Assistant's own timers (the stop's check is not a window
    timer)."""
    session.clock.now += timedelta(seconds=STOP_CHECK_S + 1)
    async_fire_time_changed(session.hass, datetime.now(timezone.utc) + timedelta(seconds=STOP_CHECK_S + 1))
    await session.hass.async_block_till_done()


async def _report(session: Session, state: str) -> None:
    session.hass.states.async_set(session.charge_control, state)
    await session.hass.async_block_till_done()


# --------------------------------------------------------------------------------- inside the plan's window


@pytest.mark.usefixtures("both_restarts")
@pytest.mark.parametrize("charger", ["on", "unavailable"], ids=["ready", "late"])
async def test_a_restart_inside_the_window_keeps_the_plans_charge_with_its_request(
    session: Session, both_restarts: str, charger: str
) -> None:
    plan = await _charging_window(session)
    assert session.controller.requested_current_a == 10

    async def down() -> None:
        session.clock.now = plan.windows[0][0] + timedelta(minutes=20)
        await _report(session, charger)

    restarted = await _restart(session, both_restarts, meanwhile=down)
    if charger != "on":
        await _report(restarted, "on")

    kinds = _kinds(restarted)
    assert kinds[0] == "restart", "the record is read back before anything else is decided"
    assert "plan_installed" not in kinds, "the first calculation keeps the restored plan"
    controller = restarted.controller
    assert controller.plan is not None and controller.plan.auto_identity == plan.auto_identity
    assert controller.session_runner.session.owner == OWNER_PLAN
    assert controller.charge_origin == "plan_window"
    assert controller.requested_current_a == 10, "the request survives the restart"
    assert restarted.off_calls == []


# -------------------------------------------------------------------------------- outside the plan's windows


async def _running_between_windows(session: Session, both_restarts: str, charger: str) -> Session:
    """The plan's first window charged; Home Assistant was down at its end, so the charge ran on past it, where it
    restarts. The first calculation now plans the rest of the need: its plan has no window open now."""
    plan = await _charging_window(session, two_windows=True)

    async def down() -> None:
        session.clock.now = plan.windows[0][1] + timedelta(minutes=20)
        await _report(session, charger)

    restarted = await _restart(session, both_restarts, meanwhile=down)
    controller = restarted.controller
    assert controller.plan is not None
    assert not any(start <= restarted.clock() < end for start, end in controller.plan.windows)
    assert controller.requested_current_a == 10
    return restarted


@pytest.mark.usefixtures("both_restarts")
async def test_a_restart_outside_the_windows_stops_the_charge_with_the_charger_ready(
    session: Session, both_restarts: str
) -> None:
    restarted = await _running_between_windows(session, both_restarts, "on")

    assert len(restarted.off_calls) == 1, "stopped at the restart: no window of the plan is open"
    await _report(restarted, "off")
    await _after_check(restarted)
    assert len(restarted.off_calls) == 1, "seen off: nothing more"
    _expects_nothing(restarted)


@pytest.mark.usefixtures("both_restarts")
async def test_a_restart_outside_the_windows_stops_the_charge_once_the_charger_is_ready(
    session: Session, both_restarts: str
) -> None:
    """The charger's integration loads after SpotNav: nothing can be sent at the restart, and the stop goes out once
    the charger reports."""
    restarted = await _running_between_windows(session, both_restarts, "unavailable")
    assert restarted.off_calls == []

    await _report(restarted, "on")

    assert len(restarted.off_calls) == 1
    await _report(restarted, "off")
    _expects_nothing(restarted)


@pytest.mark.usefixtures("both_restarts")
async def test_a_stop_the_charger_did_not_take_is_decided_again(session: Session, both_restarts: str) -> None:
    """The OCPP case: the stop went out, but the integration did not know the transaction yet and sent nothing, so the
    charger charges on, never seen off. `STOP_CHECK_S` later the stop is decided again, and goes out; at most
    `STOP_CHECK_TRIES` times."""
    restarted = await _running_between_windows(session, both_restarts, "on")
    assert len(restarted.off_calls) == 1

    await _after_check(restarted)
    assert len(restarted.off_calls) == 2, "the charge runs on: stopped again"

    for _ in range(STOP_CHECK_TRIES + 2):
        await _after_check(restarted)
    assert len(restarted.off_calls) == 1 + STOP_CHECK_TRIES, "then left to the next report or plan"

    await _report(restarted, "off")
    assert restarted.controller.charging is False
