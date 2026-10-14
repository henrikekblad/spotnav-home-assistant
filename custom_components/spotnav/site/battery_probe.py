"""A grid-charging home battery that holds the grid at the fuse: the band it is held in and the probe
that finds out whether it gives way to the car.

Some batteries charge from the grid at whatever the fuse allows and hold the grid current at the
setpoint. The regulator then sees no headroom and keeps the car at 0 A, though the battery would give
up the amps the car takes. Two things follow, both only under `car_first` with yield stepping on:

* The *band*: while the battery charges at least as hard as the car's minimum power and every phase is
  within `HELD_BAND_A` above the limit (fuse less margin), the grid sitting at the limit is the
  battery's own regulation, not an overload, and the car is not stepped down for it. A phase above the
  band is a real excess and is dealt with exactly as before.
* The *probe*: with the car held at 0 A while the plan wants it charging, the car is started at its
  minimum current and the grid is watched for a short window. If every phase is back within the band
  by the end of it the battery gave way; otherwise the car is stopped again and the probe is not
  repeated for a back-off of ten minutes, then a quarter of an hour each time, for as long as the charge
  is wanted (a plan window open, a person's Start): a battery whose own grid charge ends while the
  window is still open gives the car its turn within that quarter, and a charger is paused no more than
  three times in ten minutes (Peblar's `PAUSE_WINDOW_S`). A car that has visibly started but not yet
  reached its minimum by the end of the window (it ramps slowly, or the charger's reading lags) is
  waited for up to `PROBE_EXTENDED_WINDOW_S`, only while every phase stays within the band.
* The *car's refusal*: a probe can also show that the car itself takes nothing. One that ends as
  `car_not_drawing` while the charger's own reports agreed all along (its measured current at most
  `PROBE_IDLE_BELOW_A` on every phase the car uses at every look and never rising by `PROBE_RISE_A`, its status
  never charging, and its connector at `SuspendedEV`, the car asking for no current, at the end) is the car's
  refusal; two of them in a row
  (`PROBE_CAR_ENDED_STREAK`, in one plug-in, at most `PROBE_CAR_ENDED_GAP_S` apart) are the car ending the charge
  by itself (`car_ended`): the plan then waits for the car's level, as after the plan's own watch
  (`execution/plan_car_ended.py`), and no probe starts the car again. A probe that fails for the grid (the
  battery did not yield, an overload, a measurement) is no such sign, and neither is a reading that is missing.

This module is pure (no Home Assistant import, no I/O, no clock of its own): time arrives as seconds
from the caller's clock. It decides nothing about writing; `SiteCapacityController` does that.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal

from .site_capacity import PhaseName

#: How far above the limit (amperes per phase) a battery-held grid may read before it is a real
#: excess. A battery regulating the grid to its setpoint reads a few hundredths over.
HELD_BAND_A: Final = 0.5

#: The longest a probe waits for the battery to give way; the regulator's dwell shortens it.
PROBE_WINDOW_S: Final = 30.0
#: The shortest window, however short the dwell: a battery needs seconds to react.
PROBE_MIN_WINDOW_S: Final = 5.0

#: Back-off after a failed probe, doubling on each consecutive failure up to the cap: the probe goes on being
#: tried at least every quarter of an hour while the charge is wanted (the night of 2026-10-09: an hour's cap would
#: have left a 75-minute window with two tries).
PROBE_BACKOFF_INITIAL_S: Final = 600.0
PROBE_BACKOFF_MAX_S: Final = 900.0

#: While probing, the car's minimum current is on top of a grid already at the limit. A phase above
#: this multiple of the main fuse, or more than `PROBE_EXCESS_TOLERANCE_A` above what the car can
#: explain (its probe current on the phase's reading at the start), ends the probe at once. A probe
#: is also never started where the car's minimum would take a phase above the first.
PROBE_MAX_FUSE_FACTOR: Final = 1.4
PROBE_EXCESS_TOLERANCE_A: Final = 1.0

#: The car counts as not drawing, so a probe may start, below this current (A) on every phase.
PROBE_IDLE_BELOW_A: Final = 1.0

#: How long (seconds from the start) a probe may wait for a car that has visibly started but does not
#: yet deliver `PROBE_FOLLOW_RATIO` of the probe current when the window ends: a car ramps up over tens of
#: seconds after a start and an OCPP charger's reading lags behind the draw. The same bound as a start's
#: credit (`site_capacity_controller.START_CREDIT_S`), which covers the same lag on the regulator's side.
#: The wait is granted pass by pass, only while every phase is within `HELD_BAND_A` of its limit (what a
#: probe that succeeded may hold indefinitely), so it never keeps the site above the band past the window.
PROBE_EXTENDED_WINDOW_S: Final = 90.0

#: How many probes in a row the car must refuse (`car_refused`) before it has ended the charge by itself. One probe
#: looks at the car for half a minute (at most `PROBE_EXTENDED_WINDOW_S`), which a car waking from sleep may need
#: whole; two, ten to fifteen minutes apart, are as much as the plan's own watch asks (five minutes at SuspendedEV).
PROBE_CAR_ENDED_STREAK: Final = 2
#: The longest (seconds) between two refusals that count as in a row: twice the longest back-off, so a probe that
#: was not tried in between (the site had room, the window had not opened) breaks the row.
PROBE_CAR_ENDED_GAP_S: Final = 2.0 * PROBE_BACKOFF_MAX_S

#: The connector status of a car that asks for no current (OCPP's spelling, `plan_car_ended.SUSPENDED_EV`).
SUSPENDED_EV: Final = "SuspendedEV"
#: The status a charger that charges reports (`charge_progress`'s vocabulary).
CHARGING_STATUS: Final = "Charging"

#: A rise (A) in the charger's own reading on a phase the car uses, since the probe started, that shows the
#: car has begun to draw; below it the reading is an idle charger's flicker.
PROBE_RISE_A: Final = 0.2

#: The car counts as drawing when it delivers at least this fraction of the probe current on every
#: phase it uses (a car delivers about 89 to 98 % of what it is given).
PROBE_FOLLOW_RATIO: Final = 0.8

ProbeState = Literal["idle", "probing", "stopping", "backoff"]
ProbeOutcome = Literal["succeeded", "failed"]


def car_minimum_power_w(
    min_current_a: float, voltages_v: Sequence[float | None]
) -> float | None:
    """The power the car draws at its minimum current over the given phase voltages, or `None` if
    one is unknown (nothing is then judged on a guess)."""
    total = 0.0
    for voltage in voltages_v:
        if voltage is None or voltage <= 0.0:
            return None
        total += min_current_a * voltage
    return total if voltages_v else None


def within_held_band(
    margins_a: Mapping[PhaseName, float | None], required: Sequence[PhaseName]
) -> bool:
    """Whether every phase the car uses has a margin and no phase with one is more than
    `HELD_BAND_A` over its limit (margin = limit less measured, so over the limit is negative).

    A phase with an unknown margin that the car does not use is ignored; one the car uses refuses.
    """
    if not required:
        return False
    for phase in required:
        if margins_a.get(phase) is None:
            return False
    return all(margin >= -HELD_BAND_A for margin in margins_a.values() if margin is not None)


@dataclass(frozen=True, slots=True)
class ProbeVerdict:
    """One evaluation of a running probe: still verifying, or over, with the reason."""

    state: Literal["verifying", "succeeded", "failed"]
    reason: str
    #: This verdict is the first to wait past the window for a car that has started (`reason` says why).
    extended: bool = False


class BatteryProbe:
    """One instance per charger: the probe's state, back-off and last outcome."""

    def __init__(self, *, window_s: float = PROBE_WINDOW_S) -> None:
        self._window_s = max(PROBE_MIN_WINDOW_S, min(PROBE_WINDOW_S, window_s))
        self.state: ProbeState = "idle"
        self._started_at: float | None = None
        self._probe_a: float = 0.0
        self._baseline: dict[PhaseName, float] = {}
        self._delivered_baseline: dict[PhaseName, float] = {}
        self._charging_at_start = False
        self._status_left_charging = False
        self._extended = False
        self._car_phases: tuple[PhaseName, ...] = ()
        self._backoff_until: float | None = None
        self._next_backoff_s = PROBE_BACKOFF_INITIAL_S
        self.last_outcome: ProbeOutcome | None = None
        self.last_reason: str | None = None
        self.last_at: float | None = None
        self.count = 0
        # The car's refusal (`car_refused`): whether the running probe's charger reports agreed so far, and the
        # last verdict's connector status; the refusals in a row, when the last was, and the plug-in it was in.
        self._took_nothing = False
        self._last_connector: str | None = None
        self._plug_in: object | None = None
        self._refusals = 0
        self._last_refusal_at: float | None = None
        self._refusal_plug_in: object | None = None
        #: The last probe that ended was the car's refusal (`car_refused`).
        self.last_refused = False

    @property
    def window_s(self) -> float:
        return self._window_s

    @property
    def probing(self) -> bool:
        return self.state in ("probing", "stopping")

    def may_start(self, now: float) -> bool:
        """Whether a probe may start now: none running and any back-off over."""
        if self.probing:
            return False
        return self._backoff_until is None or now >= self._backoff_until

    def start(
        self,
        now: float,
        probe_a: float,
        baseline_a: Mapping[PhaseName, float],
        car_phases: Sequence[PhaseName],
        delivered_a: Mapping[PhaseName, float | None] | None = None,
        *,
        charger_charging: bool | None = None,
        plug_in: object | None = None,
    ) -> None:
        """A probe was started: the car, on `car_phases`, was given `probe_a` while the grid read
        `baseline_a` and the charger itself `delivered_a`; `charger_charging` is what the charger's
        status said just before the start was sent (`None` when it said nothing readable). `plug_in`
        names the plug-in the car is in: refusals in another one are not in a row with this one's."""
        self.state = "probing"
        self._started_at = now
        self._probe_a = probe_a
        self._baseline = dict(baseline_a)
        self._delivered_baseline = {
            phase: value for phase, value in (delivered_a or {}).items() if value is not None
        }
        self._charging_at_start = charger_charging is True
        self._status_left_charging = False
        self._extended = False
        self._car_phases = tuple(car_phases)
        self._took_nothing = True
        self._last_connector = None
        self._plug_in = plug_in
        self.count += 1

    def refused(self, now: float, reason: str) -> None:
        """The start write was refused, so nothing ran: a failure all the same, and backed off."""
        self._finish(now, "failed", reason)

    def evaluate(
        self,
        now: float,
        *,
        site_current_a: Mapping[PhaseName, float | None],
        limit_a: Mapping[PhaseName, float | None],
        delivered_a: Mapping[PhaseName, float | None],
        main_fuse_a: float,
        charger_charging: bool | None = None,
        connector_status: str | None = None,
    ) -> ProbeVerdict:
        """Judge a running probe on fresh readings. Any doubt ends it as a failure: the window is a
        bounded exception to the overload rules, never a reason to look away from a bad reading.

        `charger_charging` is the charger's own status saying it charges (never inferred from the start
        we sent): `True` or `False` when it says, `None` when it is unreadable or unknown. With it and the charger's reading, a car that has started but not yet reached its
        minimum when the window ends is waited for (`PROBE_EXTENDED_WINDOW_S`), every check above still
        running on every pass.

        `connector_status`: the charger's connector status in `charge_progress`'s vocabulary (`None`: none
        readable). With the charger's reading it decides whether a `car_not_drawing` is the car's refusal
        (`car_refused`), never whether the probe goes on.
        """
        if self._started_at is None:
            return ProbeVerdict("failed", "not_started")
        self._observe_car(delivered_a, charger_charging, connector_status)
        if charger_charging is False:
            # Only a status that definitely says something else has left charging; an unreadable one
            # (a reconnect) says nothing, and a stale Charging after it is still stale.
            self._status_left_charging = True
        phases = tuple(site_current_a)
        if not phases or any(site_current_a.get(phase) is None for phase in phases):
            return ProbeVerdict("failed", "measurement_unusable")
        if any(
            site_current_a[phase] > PROBE_MAX_FUSE_FACTOR * main_fuse_a  # type: ignore[operator]
            for phase in phases
        ):
            return ProbeVerdict("failed", "over_fuse_cap")
        for phase in phases:
            baseline = self._baseline.get(phase)
            if baseline is None:
                return ProbeVerdict("failed", "measurement_unusable")
            if site_current_a[phase] > baseline + self._probe_a + PROBE_EXCESS_TOLERANCE_A:  # type: ignore[operator]
                return ProbeVerdict("failed", "excess_beyond_car")
        elapsed = now - self._started_at
        if elapsed < self._window_s:
            return ProbeVerdict("verifying", "verifying")
        if any(delivered_a.get(phase) is None for phase in self._car_phases):
            return ProbeVerdict("failed", "car_not_drawing")
        drawing = all(
            delivered_a[phase] >= PROBE_FOLLOW_RATIO * self._probe_a  # type: ignore[operator]
            for phase in self._car_phases
        )
        evidence = None
        if not drawing:
            evidence = self._car_starting(delivered_a, charger_charging)
            if evidence is None or elapsed >= PROBE_EXTENDED_WINDOW_S:
                return ProbeVerdict("failed", "car_not_drawing")
        # Drawing, or waited for: either way past the window every phase must be within the band now.
        for phase in phases:
            limit = limit_a.get(phase)
            if limit is None:
                return ProbeVerdict("failed", "measurement_unusable")
            if site_current_a[phase] > limit + HELD_BAND_A:  # type: ignore[operator]
                return ProbeVerdict("failed", "battery_did_not_yield")
        if evidence is not None:
            first = not self._extended
            self._extended = True
            return ProbeVerdict("verifying", evidence, extended=first)
        return ProbeVerdict("succeeded", "battery_yielded")

    def _car_starting(
        self, delivered_a: Mapping[PhaseName, float | None], charger_charging: bool | None
    ) -> str | None:
        """Why a car not yet at its minimum is still worth waiting for, or `None`: the charger's status
        has turned to charging since the start (a status that already said so then, and has not left it,
        is no sign), or its reading has risen on a phase the car uses.

        A reading not reported since the start is no sign on its own: a charger that reports only on a
        change says nothing while an idle car draws nothing."""
        if charger_charging is True and (not self._charging_at_start or self._status_left_charging):
            return "charger_reports_charging"
        for phase in self._car_phases:
            value = delivered_a[phase]
            baseline = self._delivered_baseline.get(phase)
            if value is not None and (
                value >= PROBE_IDLE_BELOW_A
                or (baseline is not None and value - baseline >= PROBE_RISE_A)
            ):
                return "car_current_rising"
        return None

    def _observe_car(
        self,
        delivered_a: Mapping[PhaseName, float | None],
        charger_charging: bool | None,
        connector_status: str | None,
    ) -> None:
        """One look at what the charger itself says about the car: the probe stays the car's refusal only while
        every phase the car uses reads at most `PROBE_IDLE_BELOW_A`, none has risen by `PROBE_RISE_A` since the
        start (a car that has begun to draw), and nothing says the charger charges."""
        self._last_connector = connector_status
        if not self._car_phases or charger_charging is True or connector_status == CHARGING_STATUS:
            self._took_nothing = False
            return
        for phase in self._car_phases:
            value = delivered_a.get(phase)
            baseline = self._delivered_baseline.get(phase)
            if (
                value is None
                or value > PROBE_IDLE_BELOW_A
                or (baseline is not None and value - baseline >= PROBE_RISE_A)
            ):
                self._took_nothing = False
                return

    @property
    def car_refused(self) -> bool:
        """Whether the car has refused `PROBE_CAR_ENDED_STREAK` probes in a row, the last just now: it has ended the
        charge by itself. Cleared by `forget_refusals` (once acted on) and by any other outcome."""
        return self.last_refused and self._refusals >= PROBE_CAR_ENDED_STREAK

    def forget_refusals(self) -> None:
        """The car's refusals were acted on (or no longer matter): the next conclusion needs a new row."""
        self._refusals = 0
        self._last_refusal_at = None
        self._refusal_plug_in = None
        self.last_refused = False

    def _count_refusal(self, now: float, outcome: ProbeOutcome, reason: str) -> None:
        """Count the probe that just ended towards the car's refusals, or break the row."""
        refused = (
            outcome == "failed"
            and reason == "car_not_drawing"
            and self._took_nothing
            and self._last_connector == SUSPENDED_EV
        )
        self.last_refused = refused
        if not refused:
            self.forget_refusals()
            return
        in_row = (
            self._last_refusal_at is not None
            and now - self._last_refusal_at <= PROBE_CAR_ENDED_GAP_S
            and self._refusal_plug_in == self._plug_in
        )
        self._refusals = self._refusals + 1 if in_row else 1
        self._last_refusal_at = now
        self._refusal_plug_in = self._plug_in
        self.last_refused = True

    def extension_remaining_s(self, now: float) -> float | None:
        """Seconds left of the longest wait for a starting car, or `None` with no probe running."""
        if self._started_at is None:
            return None
        return max(0.0, self._started_at + PROBE_EXTENDED_WINDOW_S - now)

    def succeeded(self, now: float) -> None:
        self._finish(now, "succeeded", "battery_yielded")

    def failed(self, now: float, reason: str) -> None:
        """The probe failed: the car must be stopped (state `stopping` until that is confirmed)."""
        self._finish(now, "failed", reason)
        self.state = "stopping"

    def stopped(self) -> None:
        """The failed probe's stop went out: back-off runs."""
        if self.state == "stopping":
            self.state = "backoff"

    def _finish(self, now: float, outcome: ProbeOutcome, reason: str) -> None:
        self._count_refusal(now, outcome, reason)
        self._took_nothing = False
        self.last_outcome = outcome
        self.last_reason = reason
        self.last_at = now
        self._started_at = None
        if outcome == "succeeded":
            self.state = "idle"
            self._backoff_until = None
            self._next_backoff_s = PROBE_BACKOFF_INITIAL_S
        else:
            duration = self._next_backoff_s
            self._backoff_until = now + duration
            self._next_backoff_s = min(duration * 2.0, PROBE_BACKOFF_MAX_S)
            self.state = "backoff"

    def snapshot(self, now: float) -> dict[str, Any]:
        """Plain values for the diagnostics: state, last outcome and why, probes so far and the
        seconds of back-off left."""
        if self.state in ("backoff", "idle") and self._backoff_until is not None:
            remaining = max(0.0, self._backoff_until - now)
        else:
            remaining = None
        return {
            "state": self.state if remaining is None or remaining > 0.0 or self.probing else "idle",
            "last_outcome": self.last_outcome,
            "last_reason": self.last_reason,
            "last_age_s": None if self.last_at is None else round(now - self.last_at, 1),
            "backoff_remaining_s": None if not remaining else round(remaining, 1),
            "probes": self.count,
            "car_refusals": self._refusals,
        }
