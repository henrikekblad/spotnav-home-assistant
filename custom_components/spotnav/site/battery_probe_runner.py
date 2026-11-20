"""Running the battery probe for the site controller (`site/battery_probe.py` judges, this starts and drives it).

`BatteryProbeRunner.async_maybe_start` resumes a charge load balancing paused at the car's minimum current as a probe
of a grid-charging battery that holds the grid at the fuse, and `BatteryProbeRunner.async_pass` judges a running probe
on each pass and stops the car when it fails, for the site controller it belongs to
(`SiteCapacityController._async_maybe_start_battery_probe`, `_async_battery_probe_pass`). It keeps no state of its own:
the probes, the one timer that re-checks them and the writes it notes are kept on the site controller, which the
diagnostics, the status and the resume read.
"""

from __future__ import annotations

import math
from typing import Any, cast

from homeassistant.util import dt as dt_util

from ..const import CONF_MAIN_FUSE_A, CONF_PHASE_WIRING, DEFAULT_MIN_CURRENT_A, REGULATOR_DWELL_S
from ..execution.charger_connection import CHARGING as CONNECTION_CHARGING, UNKNOWN as CONNECTION_UNKNOWN
from ..execution.controller import REGULATED_FLOORED, REGULATED_STOPPED
from .battery_probe import BatteryProbe, PROBE_IDLE_BELOW_A, PROBE_MAX_FUSE_FACTOR, within_held_band
from .regulator import RegulatorDecision
from .regulator_damping import RegulatorDamper
from .site_capacity import PhaseName, PHASES, SiteCapacityResult
from .site_capacity_controller import _held_still_on, _LOGGER, SiteCapacityController


class BatteryProbeRunner:
    """The battery probe's start and passes for one site controller (`site`), whose state it reads and writes."""

    def __init__(self, site: SiteCapacityController) -> None:
        self._site = site

    async def async_maybe_start(
        self,
        charger_entry_id: str,
        charger_controller: Any,
        decision: RegulatorDecision,
        fresh: SiteCapacityResult,
        damper: RegulatorDamper,
        previous_setpoint: float | None,
    ) -> bool:
        """Resume a charge that load balancing paused, at the car's minimum current, as a probe;
        `True` when a probe was started (the pass is then this probe's).

        Every condition must hold, each a refusal when unknown: `car_first` with yield stepping on;
        the charger is stopped because balancing paused it and nothing has touched it since (so a
        person's Stop, a window's end or a target stop is never undone) and the charger's own
        scheduler does not hold it; a vehicle is plugged in; the plan's charge is not one the car ended by
        itself (`car_ended_holds_charge`, a wait for its level among them); the request on record is at least the
        minimum; the car is not drawing; the grid has no headroom for the minimum but no phase is
        over the band (never into a real overload: a decision to lower for an overload within the band is the
        battery's own regulation, which a stopped car cannot lower anyway); the battery charges at least as
        hard as the car's minimum would draw; the car's minimum would not take any phase past
        `PROBE_MAX_FUSE_FACTOR` times the fuse; and no back-off is running. A start the charger
        refuses counts as a failure.
        """
        if not self._site._battery_rules_apply():
            return False
        wiring: dict[str, Any] = (self._site.config.get(CONF_PHASE_WIRING) or {}).get(
            charger_entry_id
        ) or {}
        min_current_a = float(wiring.get("min_current_a", DEFAULT_MIN_CURRENT_A))
        phases = tuple(decision.basis.keys())
        main_fuse_a = self._site.config.get(CONF_MAIN_FUSE_A)
        requested = charger_controller.requested_current_a
        if (
            not phases
            or main_fuse_a is None
            or requested is None
            or requested < min_current_a
            or (previous_setpoint is not None and previous_setpoint >= min_current_a)
            or fresh.state != "observing"
            or not charger_controller.paused_by_balancing
            or not self._site._charge_still_wanted(charger_entry_id, charger_controller)
            or charger_controller.held_by_charger
            or charger_controller.adapter.vehicle_connected() is not True
            or charger_controller.car_ended_holds_charge
            or _held_still_on(fresh, phases)
        ):
            return False
        target_cap = self._site._target_start_cap(charger_entry_id)
        if target_cap is not None and target_cap < min_current_a:
            return False  # the import target holds it back: a probe at the minimum would break the hour
        delivered = self._site._stopped_charger_reading(charger_entry_id, wiring)
        if delivered is None:
            return False
        delivered_a = [delivered.get(phase).value for phase in phases]
        if any(value is None or value >= PROBE_IDLE_BELOW_A for value in delivered_a):
            return False
        margins = fresh.measured_margin_a
        if not within_held_band(margins, phases, self._site._held_bands_a(fresh)):
            return False
        if min(margins[phase] for phase in phases if margins[phase] is not None) >= min_current_a:
            return False  # there is headroom for the minimum: the ordinary path restarts the car
        if not self._site._battery_covers_car(fresh, phases, min_current_a):
            return False
        probe_a = float(math.ceil(min_current_a))
        currents = fresh.measured_phase_current_a
        if any(currents.get(phase) is None for phase in PHASES) or any(
            currents[phase] + probe_a > PROBE_MAX_FUSE_FACTOR * float(main_fuse_a)
            for phase in PHASES
        ):
            return False
        now = self._site._yield_now()
        probe = self._probe_for(charger_entry_id)
        if not probe.may_start(now):
            return False
        # The status before the start goes out: one that turns to charging while the start is on its way
        # is the car starting, not a status left over from before.
        charging_before = self._charger_reports_charging(charger_controller)
        started = await charger_controller.async_battery_probe_start(int(probe_a))
        if not started:
            probe.refused(now, "start_refused")
            self._site._record_probe_event(
                charger_entry_id,
                decision,
                outcome="probe_failed",
                detail="start_refused",
                setpoint=None,
                previous_setpoint=previous_setpoint,
            )
            return True
        damper.record_write(probe_a)
        self._site._written_at[charger_entry_id] = dt_util.utcnow().timestamp()
        probe.start(
            now,
            probe_a,
            {phase: float(currents[phase]) for phase in PHASES},
            phases,
            dict(zip(phases, delivered_a)),
            charger_charging=charging_before,
            plug_in=charger_controller.plug_in_generation,
        )
        self._site._schedule_probe_check(probe.window_s)
        self._site._record_probe_event(
            charger_entry_id,
            decision,
            outcome="probe_started",
            detail="battery_charging_at_the_fuse",
            setpoint=int(probe_a),
            previous_setpoint=previous_setpoint,
        )
        return True

    async def async_pass(
        self,
        charger_entry_id: str,
        charger_controller: Any,
        decision: RegulatorDecision,
        fresh: SiteCapacityResult,
        probe: BatteryProbe,
    ) -> None:
        """One pass of a running probe: judge it on the fresh readings, and stop the car if it
        failed (retrying the stop each pass until it is confirmed)."""
        now = self._site._yield_now()
        damper = self._site._damper_for(charger_entry_id)
        if probe.state == "probing":
            wiring: dict[str, Any] = (self._site.config.get(CONF_PHASE_WIRING) or {}).get(
                charger_entry_id
            ) or {}
            # A probe of a stopped charge: a reading the charger has not sent since the probe's start is no sign.
            delivered = self._site._stopped_charger_reading(charger_entry_id, wiring)
            observing = fresh.state == "observing"
            site_current_a = (
                dict(fresh.measured_phase_current_a)
                if observing
                else {phase: None for phase in PHASES}
            )
            limit_a: dict[PhaseName, float | None] = {}
            # No more than the import target leaves the car, as for a yield step (`_consult_yield_stepper`).
            target_a = self._site._target_ceiling_a(charger_entry_id)
            for phase in PHASES:
                measured = fresh.measured_phase_current_a.get(phase)
                margin = fresh.measured_margin_a.get(phase)
                limit_a[phase] = (
                    None if not observing or measured is None or margin is None else measured + margin
                )
                car_a = None if delivered is None else delivered.get(phase).value
                if target_a is not None and limit_a[phase] is not None and measured is not None and car_a is not None:
                    limit_a[phase] = min(cast(float, limit_a[phase]), measured - car_a + target_a)
            main_fuse_a = self._site.config.get(CONF_MAIN_FUSE_A)
            verdict = probe.evaluate(
                now,
                site_current_a=site_current_a,
                limit_a=limit_a,
                delivered_a={
                    phase: None if delivered is None else delivered.get(phase).value
                    for phase in PHASES
                },
                main_fuse_a=float(main_fuse_a) if main_fuse_a is not None else 0.0,
                charger_charging=self._charger_reports_charging(charger_controller),
                connector_status=self._connector_status(charger_controller),
                voltage_v=(
                    self._site._held_band_voltages_v(fresh) if observing else {phase: None for phase in PHASES}
                ),
            )
            if verdict.state == "verifying":
                if verdict.reason != "verifying":
                    # Waiting for a car that has started: judged again by the end of the longest wait,
                    # whatever else triggers a pass.
                    remaining = probe.extension_remaining_s(now)
                    if remaining is not None:
                        self._site._schedule_probe_check(remaining)
                if verdict.extended:
                    self._site._record_probe_event(
                        charger_entry_id,
                        decision,
                        outcome="probe_extended",
                        detail=verdict.reason,
                        setpoint=None,
                        previous_setpoint=damper.last_written_a,
                    )
                return
            if verdict.state == "succeeded":
                probe.succeeded(now)
                # The car runs at its minimum on a grid the battery holds at the limit; the stepper
                # starts from nothing and climbs it as it always does, on a battery now verified to give way
                # (the yield ceiling holds, `YieldConfig.unverified_ceiling_a`).
                self._site._yield_steppers.pop(charger_entry_id, None)
                self._site._yield_stepper_for(charger_entry_id).note_battery_yielded()
                self._site._record_probe_event(
                    charger_entry_id,
                    decision,
                    outcome="probe_succeeded",
                    detail=verdict.reason,
                    setpoint=None,
                    previous_setpoint=damper.last_written_a,
                )
                return
            probe.failed(now, verdict.reason)
            self._site._record_probe_event(
                charger_entry_id,
                decision,
                outcome="probe_failed",
                detail=verdict.reason,
                setpoint=None,
                previous_setpoint=damper.last_written_a,
            )
        # Stopping: the failed probe's stop, hard (a refusal that the fuse needs is a safety stop).
        write = await charger_controller.async_apply_regulated_current(0, must_lower=True)
        if write.written or write.outcome == REGULATED_STOPPED:
            damper.record_write(0.0)
            probe.stopped()
            if probe.car_refused:
                # The car refused the probes in a row with the charger's own reports agreeing: it has ended the
                # charge by itself. Told once, after the stop went out, so the plan's car-ended path sees the charge
                # load balancing holds back, as it is.
                probe.forget_refusals()
                self._site._record_probe_event(
                    charger_entry_id,
                    decision,
                    outcome="probe_car_ended",
                    detail="car_refused_probes",
                    setpoint=None,
                    previous_setpoint=damper.last_written_a,
                )
                charger_controller.note_car_refused_probe()
            return
        if write.outcome == REGULATED_FLOORED:
            # Not paused: the charge runs on at the floor, which is what the charger draws until the stop is taken.
            damper.record_write(float(DEFAULT_MIN_CURRENT_A))
        _LOGGER.warning(
            "SpotNav site %s battery probe: charger %s could not be stopped (%s); trying again",
            self._site.entry_id,
            charger_entry_id,
            write.code,
        )

    def _probe_for(self, charger_entry_id: str) -> BatteryProbe:
        probe = self._site._battery_probes.get(charger_entry_id)
        if probe is None:
            probe = BatteryProbe(
                window_s=REGULATOR_DWELL_S
            )
            self._site._battery_probes[charger_entry_id] = probe
        return probe

    def _charger_reports_charging(self, charger_controller: Any) -> bool | None:
        """Whether the charger's own status says it charges, or `None` when no status says anything (none
        configured, unavailable, unknown or unmapped). Never inferred from the start we sent: a charger read
        through its switch alone has no status to say it."""
        connection, source = charger_controller.connection()
        if source is None or connection == CONNECTION_UNKNOWN:
            return None
        return connection == CONNECTION_CHARGING

    @staticmethod
    def _connector_status(charger_controller: Any) -> str | None:
        """The charger's connector status in the progress check's vocabulary (`SuspendedEV`, `Charging`, the raw
        text), or `None` when none can be read."""
        try:
            return charger_controller.charge_progress_facts().connector_status
        except Exception:  # noqa: BLE001 - an unreadable status says nothing
            _LOGGER.debug("connector_status: ignored failure (an unreadable status says nothing)", exc_info=True)
            return None
