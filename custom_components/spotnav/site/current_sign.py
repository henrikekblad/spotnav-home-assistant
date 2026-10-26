"""Confirm a direct site's per-phase current sign against the meter's total grid power.

A source flagged `site_current_signed` may report negative currents, but the flag only says the
magnitude is |I|: not every integration counts export as negative (SolarEdge's meter counts it
positive), and a single reversed phase would look the same. A phase's sign is therefore trusted as
its import/export direction only after it has agreed with the meter's total grid power (signed,
positive = import, `SiteCapacityController.grid_total_power`) on `min_agreements` readings in a row
in which the answer is clear:

- the total is outside the zero band, every phase reads, and each phase outside the band (`I x V`
  with the nominal voltage) has the total's sign, and
- the phases add up to the total within `zero_margin_w` plus `tolerance_fraction` of their summed
  magnitude (|I| x V overstates active power by the power factor; a reversed phase does not fit).

A clear reading that does not fit withdraws it: every phase outside the band against the total
withdraws every phase (the convention itself is in doubt); one phase against a total the other
phases outside the band agree with withdraws that phase; a reading that agrees in sign but not in
sum restarts the run of the phases outside the band. A reading with phases outside the band on both
sides of the total (normal on a solar site) neither confirms nor withdraws, and a phase inside the
band is left as it is. The state lives in memory
only: after a restart every phase is unconfirmed again.

Errors run in the safe direction: a lagging total or a reactive load can withdraw a phase that was
right, which only stops the regulator deciding on it until it is confirmed again.
"""

from __future__ import annotations

from collections.abc import Mapping

from .site_capacity import DIRECTION_NOMINAL_PHASE_VOLTAGE_V, PHASES, PhaseName

# Share of the phases' summed |I| x V that the total may differ from their signed sum.
DEFAULT_SIGN_TOLERANCE_FRACTION = 0.25


class CurrentSignCheck:
    """Which phases' signed current has been confirmed as their direction (module docstring)."""

    def __init__(
        self,
        *,
        zero_margin_w: float,
        min_agreements: int,
        tolerance_fraction: float = DEFAULT_SIGN_TOLERANCE_FRACTION,
        voltage_v: float = DIRECTION_NOMINAL_PHASE_VOLTAGE_V,
    ) -> None:
        self._zero_margin_w = zero_margin_w
        self._min_agreements = max(1, min_agreements)
        self._tolerance_fraction = tolerance_fraction
        self._voltage_v = voltage_v
        self._agreements: dict[PhaseName, int] = {phase: 0 for phase in PHASES}
        self._confirmed: set[PhaseName] = set()

    @property
    def confirmed(self) -> frozenset[PhaseName]:
        return frozenset(self._confirmed)

    def reset(self) -> None:
        self._agreements = {phase: 0 for phase in PHASES}
        self._confirmed.clear()

    def _withdraw(self, phase: PhaseName) -> None:
        self._agreements[phase] = 0
        self._confirmed.discard(phase)

    def observe(
        self, signed_current_a: Mapping[PhaseName, float | None], total_w: float | None
    ) -> None:
        """Take one new reading: each phase's signed current (fresh, else `None`) and the meter's
        total grid power (fresh, else `None`)."""
        if total_w is None or abs(total_w) <= self._zero_margin_w:
            return
        powers: dict[PhaseName, float] = {}
        for phase in PHASES:
            current = signed_current_a.get(phase)
            if current is None:
                return
            powers[phase] = current * self._voltage_v
        clear = [phase for phase in PHASES if abs(powers[phase]) > self._zero_margin_w]
        if not clear:
            return
        import_total = total_w > 0
        agree = [phase for phase in clear if (powers[phase] > 0) == import_total]
        oppose = [phase for phase in clear if phase not in agree]

        if not agree:
            self.reset()
            return
        if oppose:
            if len(oppose) == 1:
                self._withdraw(oppose[0])
            return
        summed = sum(powers.values())
        tolerance = self._zero_margin_w + self._tolerance_fraction * sum(abs(value) for value in powers.values())
        if abs(summed - total_w) > tolerance:
            # A lagging reading or a reversed phase: no run counts across it, but a confirmed phase stays.
            for phase in clear:
                self._agreements[phase] = 0
            return
        for phase in clear:
            self._agreements[phase] = min(self._agreements[phase] + 1, self._min_agreements)
            if self._agreements[phase] >= self._min_agreements:
                self._confirmed.add(phase)
