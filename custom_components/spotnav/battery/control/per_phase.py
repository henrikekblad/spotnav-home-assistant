"""The executor's recipe as the plant needs it: the hold variant the report names and, where the brand has targets per
phase, the phased control with what it reads per phase (the grid's, the plant's own), each phase's cap and the
capability's withdrawal when the plant does not follow."""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any


from ...planning.grid_voltage import stored_voltage_between_phases_v
from ...site.phase_peaks import plant_phase_w
from .. import gate
from ..commands import (
    Phases,
    control_for,
    reading,
)
from ..executor import (
    BATTERY_MAX_AGE_S,
    HEADROOM_MARGIN_A,
    Executor,
    StepResult,
)
from ..plan_model import (
    ACTION_GRID_CHARGE,
)
from ..runtime import read_role_reads
from .const import (
    PER_PHASE_GAIN_KW,
)
from .helpers import _accept_applies


class PerPhase:
    """The hold variant and the charge per phase. A mixin of `BatteryControl`, on its state."""

    def hold_variant(self) -> str | None:
        """The hold variant control runs on this firmware (`gate.hold_variant_to_run`); `None` for a brand with one
        hold. The shadow and its previews show the same."""
        control = control_for(self.facts.control_brand)
        names = () if control is None else control.variant_names()
        return (gate.hold_variant_to_run(self.control_report(), names) or names[0]) if names else None

    def _control_executor(self) -> Executor | None:
        """The executor for control and trials, running the hold variant the verification passed, else the first not
        recorded as failed, else the brand's first (once it holds nothing; `Executor.use_variant`). A test hold is
        refused when every variant failed (`async_trial`)."""
        executor = self._executor()
        if executor is not None and executor.control.hold_variants:
            names = executor.control.variant_names()
            executor.use_variant(gate.hold_variant_to_run(self.control_report(), names) or names[0])
        self._sync_phases()
        return executor

    # ---- per phase

    def _sync_phases(self) -> bool:
        """Have the executor run the brand's targets per phase when they are there and verified, its plain control
        otherwise (changed only while it holds nothing). Whether it runs them now."""
        executor = self.executor
        if executor is None:
            return False
        executor.use_phases(self._phased_wanted())
        return executor.control.phased

    def _phased_wanted(self) -> bool:
        return gate.per_phase(self.control_report()) == "verified" and self._phase_missing() is None

    def _phase_missing(self) -> str | None:
        """Why a grid charge per phase cannot run on this plant now, as a stable code; `None` when everything it
        needs is there: the brand's targets per phase and what they require, found, enabled and reading so; a
        three-phase plant; the plant's own power per phase; the site's reading of each phase."""
        cached = self._missing
        if cached is not None and cached[0] == self._look_id:
            return cached[1]
        missing = self._phase_missing_now()
        self._missing = (self._look_id, missing)
        return missing

    def _phase_missing_now(self) -> str | None:
        """`_phase_missing`, read afresh (it walks the registry: once a look, `_look_id`)."""
        control = control_for(self.facts.control_brand)
        spec = None if control is None else control.phase_control
        if control is None or spec is None:
            return "not_supported"
        if not spec.three_phase_by_targets and self._plant_phases(control) != 3:
            # Where the three targets exist only on a three-phase plant, finding them (below) is what shows it.
            return "not_three_phase"
        reads = read_role_reads(self.hass, self.site.config, control.with_phases(True))
        for role in spec.owned():
            read = reads.get(role.name)
            if read is None or not read.found:
                return f"control_not_found:{role.name}"
            if not read.enabled:
                return f"control_disabled:{role.name}"
        for role, states, why in spec.requires:
            if reading(role, reads[role.name].state) not in states:
                return why
        if spec.behind is not None and spec.point is not None:
            # The recipe's own sensors at its point give the plant's power per phase (`_grid_phases`).
            if self._grid_phases() is None:
                return "plant_phase_power"
        elif self.facts.plant_phase_entities is None:
            return "plant_phase_power"
        if self._phase_headroom_a() is None or self._grid_phase_kw() is None:
            return "site_phases"
        return None

    def _grid_phases(self) -> tuple[Phases, Phases] | None:
        """For a brand whose targets per phase are the grid's, with sensors at their point
        (`commands.PhaseControl.grid_phases`): the load behind each phase's point and the power at the point now
        (kW); `None` for any other brand, or when a sensor is missing, disabled or reads nothing. Read once a look
        (it walks the registry), whichever control the executor runs now."""
        cached = self._grid_read
        if cached is not None and cached[0] == self._look_id:
            return cached[1]
        control = control_for(self.facts.control_brand)
        spec = None if control is None else control.phase_control
        found: tuple[Phases, Phases] | None = None
        if control is not None and spec is not None and spec.behind is not None and spec.point is not None:
            try:
                reads = read_role_reads(self.hass, self.site.config, control.with_phases(True))
                found = spec.grid_phases({
                    name: read.state for name, read in reads.items() if read.found and read.enabled
                })
            except Exception:  # noqa: BLE001 - unknown: no charge per phase runs without them
                self._look_failed("grid_phases")
        self._grid_read = (self._look_id, found)
        return found

    def _phase_headroom_a(self) -> Phases | None:
        """Each phase's headroom now (A), `None` when one is unknown."""
        headroom = self.site.result.phase_headroom_a
        values = [headroom.get(phase) for phase in gate.PHASE_NAMES]  # type: ignore[call-overload]
        if not all(value is not None and math.isfinite(value) for value in values):
            return None
        return (float(values[0]), float(values[1]), float(values[2]))

    def _grid_phase_kw(self) -> Phases | None:
        """The site meter's power per phase (kW, + import), `None` when one is unknown."""
        signed = self.site.result.phase_signed_active_power_w
        values = [signed.get(phase) for phase in gate.PHASE_NAMES]  # type: ignore[call-overload]
        if not all(value is not None and math.isfinite(value) for value in values):
            return None
        return (values[0] / 1000.0, values[1] / 1000.0, values[2] / 1000.0)

    def _plant_phase_kw(self, now: datetime) -> Phases | None:
        """The plant's own power per phase (kW, + output; the adapter's `plant_phase_power`), `None` when it is not
        read or older than a battery reading may be."""
        try:
            watts = plant_phase_w(self.site, BATTERY_MAX_AGE_S, now.timestamp())
        except Exception:  # noqa: BLE001 - unknown: a charge per phase then counts none of its own draw back
            self._look_failed("plant_phase_power")
            return None
        return None if watts is None else (watts[0] / 1000.0, watts[1] / 1000.0, watts[2] / 1000.0)

    def _phase_caps_kw(
        self, now: datetime, *, held_off: bool = False, accept: bool | None = None, no_cars: bool = False,
    ) -> Phases:
        """The HA-down cap of a grid charge on each phase (`gate.grid_charge_phase_caps_kw`) on the site's readings
        now, by the same house peaks, chargers and margin as the balanced cap. `held_off`: a person's hardware test
        runs, and the chargers its hold keeps stopped count as it says (`_cars_allowed_a`). `accept`: whether the
        person accepted the HA-down risk (`battery_control_accept_ha_down`), by default as stored now. `no_cars`:
        the chargers and SpotNav's own heat counted as drawing nothing, for the status's offer only."""
        control = control_for(self.facts.control_brand)
        return gate.grid_charge_phase_caps_kw(
            self._main_fuse_a(), stored_voltage_between_phases_v(self.site.config), margin_a=HEADROOM_MARGIN_A,
            cars_a=0.0 if no_cars else self._cars_allowed_a(held_off=held_off) + self._heat_standing_a(),
            house_peak_kw=self._house_peak_kw(now),
            house_phase_peaks_a=self._phase_peaks(now) or None,
            # A plant that stops following its targets by itself when the writes stop (the phase spec's own measured
            # expiry).
            expiry_lifts=self._phase_expiry_lifts(control),
            own_import_a=self._own_import_a(control),
            accept_ha_down=(self.options().accept_ha_down if accept is None else accept) and _accept_applies(control),
            site_margin_a=self._site_margin_a(),
        )

    def _charge_limits(
        self, now: datetime, options: gate.Options, *, phased: bool | None = None, no_cars: bool = False,
    ) -> tuple[float | None, Phases | None]:
        """The HA-down cap a grid charge runs under now (kW, `None`: the plant's own import limit guards the fuse)
        and, when it runs per phase, each phase's cap (the first is then their sum). Per phase only on a control
        that runs the targets (`phased`, by default the executor's as synced now), verified and with everything
        there, and only when the phases allow at least `PER_PHASE_GAIN_KW` more than the balanced cap (always, once the
        site accepted the HA-down risk: both are then the empty house's fuse)."""
        cap = self._grid_charge_cap_kw(now, options, no_cars=no_cars)
        if phased is None:
            phased = self._sync_phases()
        if cap is None or not phased or not self._phased_wanted():
            return cap, None
        caps = self._phase_caps_kw(now, accept=options.accept_ha_down, no_cars=no_cars)
        total = sum(caps)
        if options.accept_ha_down and _accept_applies(control_for(self.facts.control_brand)):
            # Accepted: the phases' caps and the balanced one are both the empty house's fuse, so there is no gain to
            # weigh; the charge per phase keeps running where it is verified (and where `charge` was shown only per
            # phase, it is the one grid charge there is).
            return total, caps
        if total < cap + PER_PHASE_GAIN_KW:
            return cap, None
        return total, caps

    def _per_phase_status(self, now: datetime, options: gate.Options) -> dict[str, Any] | None:
        """The capability for the status: `state` (`verified`, `pending`, `failed`, or `unavailable` with `why`,
        `_phase_missing`), `used` (a grid charge now would run per phase), `caps_kw` (each phase's HA-down cap) and
        `charging_kw` (what a running charge per phase is commanded at). `None` for a brand without targets per
        phase."""
        control = control_for(self.facts.control_brand)
        if control is None or control.phase_control is None:
            return None

        def per_phase(values: Phases) -> dict[str, float]:
            return {phase: round(value, 2) for phase, value in zip(gate.PHASE_NAMES, values, strict=True)}

        missing = self._phase_missing()
        executor = self.executor
        state = None if executor is None else executor.state
        probing = self.verification is not None and not self.verification.done
        running = (
            state.phase_kw if state is not None and state.phase == "active" and state.action == ACTION_GRID_CHARGE
            and not probing else None
        )
        phased = executor is not None and executor.control.phased
        return {
            "state": "unavailable" if missing is not None else gate.per_phase(self.control_report()),
            "why": missing,
            "used": self._charge_limits(now, options, phased=phased)[1] is not None,
            "caps_kw": per_phase(self._phase_caps_kw(now)),
            "charging_kw": None if running is None else per_phase(running),
        }

    def _phase_charge_possible(self) -> bool:
        """Whether the verification's `charge` may be tried per phase: the brand's targets per phase and everything
        they need are there (`_phase_missing`), and the capability has not failed on this firmware."""
        return self._phase_missing() is None and gate.per_phase(self.report()) != "failed"

    def _per_phase_due(self, report: dict[str, Any] | None) -> bool:
        """Whether a run should try the mode `per_phase`: everything it needs is there, it is still undecided on
        this firmware, and the routine has runs left and has not failed."""
        attempts = (report or {}).get("attempts")
        return (
            gate.per_phase(report) == "pending" and gate.report_status(report) != "failed"
            and not (isinstance(attempts, int) and attempts >= gate.MAX_ATTEMPTS)
            and self._phase_missing() is None
        )

    def _withdraw_per_phase(self, result: StepResult, now: datetime) -> None:
        """A running charge per phase that the plant did not follow (`executor._judge_phases` handed it back): the
        capability is withdrawn for this firmware, so the next grid charge is the balanced one. Not while the
        verification (or a hardware test) runs: its own modes record what a runaway on their targets showed."""
        if self.verification is not None:
            return
        judged = next((event for event in result.events
                       if event["kind"] == "monitor" and str(event["reason"]) in ("phase_over", "phase_no_effect")), None)
        if judged is None or self.control_report() is None:
            return
        outcome = "partial" if judged["reason"] == "phase_over" else "ignored"
        key = self.firmware_key()
        # On the key's own report (started here when control ran on a report carried over an update): decided
        # there, the capability is no longer carried.
        control = control_for(self.facts.control_brand)
        own = self.report() or {"key": key, "brand": None if control is None else control.brand,
                                "firmware": self.facts.firmware, "attempts": 0, "modes": {}}
        self._keep_report(key, gate.per_phase_failed(own, outcome, now))
        self._log([{"at": now.isoformat(), "kind": "per_phase_withdrawn", "reason": judged["reason"]}])
