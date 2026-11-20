"""The person hold for one charging controller: a charge the charger begins by itself while a person's Stop pauses
Auto is stopped (C7, the core's `hold_stop_*`).

`PersonHold.observe` is the one gate of these stops, fed by a report (`ChargingController._observe_person_hold`) or
as a timer (`PersonHold.observe_timer`: its retry, a restore, a stop's retry); `PersonHold.reset` starts them afresh.
It keeps no state of its own: the retry's timer, whether a stop's task runs and when the last one ended are kept on the
controller it belongs to, which reads the fact `hold_stop_overdue` and cancels the retry at its shutdown.
"""

from __future__ import annotations

from datetime import datetime

from homeassistant.core import callback
from homeassistant.helpers.event import async_call_later
from homeassistant.util import dt as dt_util

from ..core import events as core_events
from ..core.ownership import PERSON_HOLD_MAX_STOPS, PERSON_HOLD_STOP_GAP_S, PERSON_HOLD_WINDOW_S
from .controller import _LOGGER, AUTOMATIC_STOP, ChargingController


class PersonHold:
    """The stops under a person's Stop for one charging controller (`controller`), whose state it reads and writes."""

    def __init__(self, controller: ChargingController) -> None:
        self._controller = controller

    def observe(self) -> None:
        """While a person's Stop pauses Auto, a charge the charger begins by itself (a free-charging OCPP
        charger, an Easee without authorization, at plug-in or at any time) is stopped at once, whatever the
        plan's windows say. Never a Start on its way: the person's own Start replaces their Stop, decided
        again under the boundary's lock.

        At most one stop per `PERSON_HOLD_STOP_GAP_S` (a charger whose status lags says `on` at every report
        after a stop it took), and at most `PERSON_HOLD_MAX_STOPS` in any `PERSON_HOLD_WINDOW_S`, whether the
        charger took them or not (one that takes each stop and begins again is cycled no more than that): then
        no more, and the status says the charger does not take SpotNav's stop until the person acts or the car
        is unplugged. The one gate for these stops, the stop's own retry included (`_async_retry_stop`)."""
        if not self._controller._held_off_by_person():
            self.reset()
            return
        # The core decided C7 for the report or the timer being fed (`_core_hold_verdict`): the give-up, a stop, or a
        # look again after the gap. No verdict (it could not decide): nothing is sent, the next report decides again.
        verdict = self._controller._core_hold_verdict
        if verdict is None:
            return
        if ("notify", "charger_ignores_stop") in verdict:
            _LOGGER.warning(
                "SpotNav charger %s: the charger began charging again after %s stops within %s minutes under a "
                "person's Stop; no more are sent until the person acts or the car is unplugged",
                self._controller.entry_id,
                PERSON_HOLD_MAX_STOPS,
                int(PERSON_HOLD_WINDOW_S / 60),
            )
            self._controller._notify()
            return
        if ("stop", "person_hold") in verdict:
            self._spawn_stop()
            return
        tried_at = self._controller._c7.tried_at
        if (
            tried_at is None
            or self._controller._c7.stop_pending
            or self._controller._c7.gave_up
            or self._controller._control_observation is not True
            or self._controller.start_pending
        ):
            return
        wait_s = PERSON_HOLD_STOP_GAP_S - (dt_util.utcnow() - tried_at).total_seconds()
        if wait_s > 0 and self._controller._person_hold_retry_cancel is None:
            # Looked at again when the gap is over, even if the charger reports nothing new by then.
            self._controller._person_hold_retry_cancel = async_call_later(
                self._controller.hass, wait_s, self._retry_callback
            )

    @callback
    def _retry_callback(self, _now: datetime) -> None:
        self._controller._person_hold_retry_cancel = None
        self.observe_timer()

    def cancel_retry(self) -> None:
        cancel = self._controller._person_hold_retry_cancel
        self._controller._person_hold_retry_cancel = None
        if cancel is not None:
            cancel()

    def reset(self) -> None:
        """A new plug-in session, the person acting, or no person's Stop any more: the stops start afresh."""
        gave_up = self._controller._c7.gave_up
        self.cancel_retry()
        if gave_up:
            self._controller._notify()

    def _spawn_stop(self) -> None:
        self._controller._person_hold_task_running = True
        self._controller._async_spawn(self._async_stop(), "the stop under a person's Stop")

    async def _async_stop(self) -> None:
        """The stop under a person's Stop a report or a timer decided, decided again under the boundary's lock
        (`_recheck`): a person's Start may have replaced their Stop meanwhile."""
        try:
            async with self._controller._automatic(AUTOMATIC_STOP):
                await self._controller._recheck(core_events.RECHECK_PERSON_HOLD, self._stop_locked)
        finally:
            self._controller._person_hold_task_running = False
            self._controller._person_hold_task_ended_at = dt_util.utcnow()
            if (
                self._controller._c7.stop_pending
                and not self._controller._shut_down
                and self._controller._person_hold_retry_cancel is None
            ):
                # The core still awaits this stop (its re-check could not be fed): looked at again once it is
                # overdue, so the stops under the person's Stop go on even if the charger reports nothing new.
                self._controller._person_hold_retry_cancel = async_call_later(
                    self._controller.hass, PERSON_HOLD_STOP_GAP_S + 1, self._retry_callback
                )

    def hold_stop_overdue(self) -> bool:
        """The fact `hold_stop_overdue`: a stop under a person's Stop is still awaited, but no task of it runs and
        the last ended at least `PERSON_HOLD_STOP_GAP_S` ago (its decision never came back)."""
        ended = self._controller._person_hold_task_ended_at
        return (
            self._controller._c7.stop_pending
            and not self._controller._person_hold_task_running
            and ended is not None
            and (dt_util.utcnow() - ended).total_seconds() >= PERSON_HOLD_STOP_GAP_S
        )

    async def _stop_locked(self) -> bool:
        """One stop of a charge a person's Stop holds off, counted (`observe`). Runs with the operation lock held."""
        _LOGGER.info(
            "SpotNav charger %s: a charge began while a person's Stop pauses Auto; stopping it",
            self._controller.entry_id,
        )
        # Every attempt counts, taken or not, but one that sent nothing (a stop of ours still settles): the core counts
        # it from the stop's result.
        return await self._controller._automatic_stop_locked("the stop under a person's Stop")

    def observe_timer(self) -> None:
        """`observe` outside a report (its retry, a restore, a stop's retry), fed as a timer."""
        token = self._controller._runner.begin()
        facts = self._controller._core_facts(
            lambda: {
                "control_on": self._controller._control_observation is True,
                "start_pending": self._controller._core_start_pending(),
                "hold_stop_overdue": self.hold_stop_overdue(),
            },
            core_events.Timer.kind,
            token=token,
        )
        if facts is not None:
            self._controller._core_hold_verdict = self._controller._runner.verdict(core_events.Timer(**facts), token)
        try:
            self.observe()
        finally:
            self._controller._core_hold_verdict = None
            self._controller._core_end_event(token, facts, lambda: core_events.Timer(**facts))
