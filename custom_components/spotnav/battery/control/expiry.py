"""The plant's own expiry of a command as the shell reads it: whether it lifts the HA-down cap (balanced and per phase),
which expiry the routine still has to time, and what the executor's events of a timed expiry are logged as."""

from __future__ import annotations

import logging
from typing import Any

from .. import gate
from ..commands import BrandControl, Expiry, control_for
from ..executor import Executor
from ..plan_model import ACTION_GRID_CHARGE, ACTION_HOLD
from .helpers import _grid_charge_expiry

_LOGGER = logging.getLogger(__package__)  # the shell's one logger, whichever of its modules logs


class ExpiryCheck:
    """The expiry and its test. A mixin of `BatteryControl`, on its state."""

    def _expiry_lifts(self, control: BrandControl | None, *, phased: bool) -> bool:
        """Whether the grid charge's own expiry stands in for its HA-down cap (`gate.expiry_lifts`): the phase spec's
        for a charge per phase (`phased`: on all three phases, `_phase_expiry_lifts`), else the recipe's own for the
        grid charge (`expiry_by_action`, then `expiry`: never the phase spec's, which has nothing to end a balanced
        command). Measured only on the firmware read now (`gate.expiry_measured`). `control` is the brand's control as
        `control_for` gives it."""
        if phased:
            return all(self._phase_expiry_lifts(control))
        expiry = _grid_charge_expiry(control, phased=False)
        if control is None or expiry is None:
            return False
        base = control.with_phases(False)
        return gate.expiry_lifts(
            expiry, measured=self._expiry_measured(expiry), slow=base.slow, fixed_charge=base.fixed_charge
        )

    def _phase_expiry_lifts(self, control: BrandControl | None) -> tuple[bool, bool, bool]:
        """Per phase (L1, L2, L3): whether the phase spec's expiry stands in for that phase's HA-down cap, measured on
        that very phase on the firmware read now (owner decision 2026-11-23: an unproven phase keeps its cap)."""
        expiry = _grid_charge_expiry(control, phased=True)
        if control is None or expiry is None:
            return (False, False, False)
        base = control.with_phases(False)
        if not gate.expiry_lifts(expiry, measured=True, slow=base.slow, fixed_charge=base.fixed_charge):
            return (False, False, False)
        key = self.firmware_key() if self.firmware_known() else None
        return gate.expiry_phases_measured(expiry, key, self.report())

    def _expiry_measured(self, expiry: Expiry) -> bool:
        """Whether the expiry was seen to act on the firmware read now (`gate.expiry_measured`): recorded in the
        recipe, or the routine's mode `expiry` obeyed for it in this firmware's report (never the hardware test's)."""
        key = self.firmware_key() if self.firmware_known() else None
        return gate.expiry_measured(expiry, key, self.report())

    def _expiry_to_test(self, report: dict[str, Any] | None = None) -> tuple[Expiry, int | None] | None:
        """The expiry the verification's mode `expiry` may try on this plant (`gate.expiry_testable`: one whose pass
        would lift a cap), still undecided on this firmware: the hold's own where its renewed roles are all in the
        hold's writes (phase `None`), else the phase spec's where a charge per phase can run here, on the first
        phase (0-2) not yet proven or decided (one a run; `report`, by default the report now). `None` for every
        other plant (Sigenergy: no expiry)."""
        control = control_for(self.facts.control_brand)
        if control is None:
            return None
        report = self.report() if report is None else report
        key = self.firmware_key() if self.firmware_known() else None
        testable = {"slow": control.slow, "fixed_charge": control.fixed_charge}
        hold = control.states.get(ACTION_HOLD)
        expiry = control.expiry
        if (
            hold is not None and gate.expiry_testable(expiry, **testable)
            and not gate.expiry_measured(expiry, key, report) and gate.expiry_due(report, expiry)
        ):
            written = {want.role for want in hold(0.0, int(expiry.window_s or 0))}
            if set(expiry.renewed_roles) <= written:
                return expiry, None
        spec = control.phase_control
        if spec is not None and gate.expiry_testable(spec.expiry, **testable) and self._phase_missing() is None:
            for index, phase in enumerate(gate.PHASE_NAMES):
                if not gate.expiry_measured(spec.expiry, key, report, phase) and gate.expiry_due(
                    report, spec.expiry, phase
                ):
                    return spec.expiry, index
        return None

    def _expiry_due(self, report: dict[str, Any] | None) -> bool:
        """Whether a run should try the mode `expiry`: there is one to try (`_expiry_to_test`), still undecided for
        it on this firmware, control has passed here, the routine has runs left, and for the phase spec's the mode
        `per_phase` passed or is still to be tried."""
        found = self._expiry_to_test(report)
        if found is None or gate.report_status(report) != "passed":
            return False
        attempts = (report or {}).get("attempts")
        if isinstance(attempts, int) and attempts >= gate.MAX_ATTEMPTS:
            return False
        return found[1] is None or gate.per_phase(report) == "verified" or self._per_phase_due(report)

    def _expiry_block(self) -> dict[str, Any] | None:
        """The grid charge's own expiry, balanced and per phase, for the diagnostics only (the card does not know
        these keys): what it is, whether it is measured here and lifts the HA-down cap, its line
        (`gate.expiry_note`) and the kWh a charge at the battery's rated power may still take after Home Assistant
        dies (`ha_down_energy_kwh`, `gate.expiry_energy_kwh`; `None`: unbounded). `None` for a brand SpotNav does not
        write to."""
        try:
            control = control_for(self.facts.control_brand)
            if control is None:
                return None
            rated = self.facts.max_charge_kw
            block: dict[str, Any] = {}
            for name, phased in (("balanced", False), ("per_phase", True)):
                expiry = _grid_charge_expiry(control, phased=phased)
                if expiry is None:
                    block[name] = None
                    continue
                key = self.firmware_key() if self.firmware_known() else None
                # Per phase for the phase spec's (each phase proven on its own).
                measured: bool | list[bool] = (
                    list(gate.expiry_phases_measured(expiry, key, self.report())) if phased
                    else self._expiry_measured(expiry)
                )
                energy = None if rated is None else gate.expiry_energy_kwh(expiry, rated)
                block[name] = {
                    "expiry_id": expiry.expiry_id, "kind": expiry.kind, "renewer": expiry.renewer,
                    "window_s": expiry.window_s, "evidence": expiry.evidence, "measured": measured,
                    "lifts": list(self._phase_expiry_lifts(control)) if phased
                    else self._expiry_lifts(control, phased=False),
                    "note": gate.expiry_note(expiry, measured=all(measured) if isinstance(measured, list) else measured),
                    "rate_kw": rated, "ha_down_energy_kwh": None if energy is None else round(energy, 3),
                }
            return block
        except Exception:  # noqa: BLE001 - diagnostics must never fail on one block
            _LOGGER.debug("expiry_block: ignored failure (diagnostics must never fail on one block)", exc_info=True)
            return None

    def _log_expiry(self, executor: Executor, events: list[dict[str, Any]]) -> None:
        """A grid charge entered or changed under an expiry: the kWh it may still take after Home Assistant dies
        (`ha_down_energy_kwh`, `None`: unbounded) in the control log. Nothing for a control without one (Sigenergy's
        log stays as it was)."""
        for event in events:
            if event.get("kind") not in ("enter", "change") or event.get("reason") != ACTION_GRID_CHARGE:
                continue
            phases = event.get("phase_kw")
            phased = executor.control.phased and isinstance(phases, list)
            expiry = _grid_charge_expiry(executor.control, phased=phased)
            if expiry is None or expiry.kind == "none":
                continue
            rate = sum(float(value) for value in phases) if phased else event.get("rate_kw")  # type: ignore[union-attr]
            energy = gate.expiry_energy_kwh(expiry, rate) if isinstance(rate, (int, float)) else None
            self._log([{
                "at": event.get("at"), "kind": "ha_down_energy", "reason": expiry.kind,
                "ha_down_energy_kwh": None if energy is None else round(energy, 3),
                "note": gate.expiry_note(expiry, measured=all(gate.expiry_phases_measured(
                    expiry, self.firmware_key() if self.firmware_known() else None, self.report()
                )) if phased else self._expiry_measured(expiry)),
            }])
