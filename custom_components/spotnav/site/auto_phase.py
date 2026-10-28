"""Which phase an "Automatic" single-phase charger draws on, followed from its own measured current.

A single-phase charger whose phase is not chosen in the site's wiring is "Automatic": something other than
the wiring decides the phase, such as an Easee Equalizer that moves a one-phase charge to the phase with
the most room, a different one each session. Pure and Home-Assistant-independent; the site's controller
keeps one `AutoPhaseTracker` per such charger.

* The charger is on a phase only while its own measured current shows it plainly: every phase reads,
  fresh, and exactly one at or above `AUTO_PHASE_DRAW_A` (`drawn_phase`). The site's meter must agree:
  that phase of the site carries at least the charger's draw, less `AUTO_PHASE_SITE_TOLERANCE_A` (a
  terminal mapped to the wrong phase, or a one-phase charger whose single terminal is read as L1, never
  passes for a measured phase where the site shows no such load).
* A new phase counts once it has held for `AUTO_PHASE_SETTLE_S`; until then the charger is reckoned on the
  worst phase. A reading in transition (two phases drawing) keeps the phase it had for at most that long.
* While the charger's phase switch settles (a charger the sun forced to one phase, `execution/phase_switching.py`)
  no new phase is taken: what it was reckoned on stays, and the reading in the switch's gap decides nothing.
* Anything else (idle, before a start, a source that is not set, a stale or missing reading, a reading the
  site does not bear out) is no phase: the caller reckons the charger on the phase with the least headroom
  (`site_capacity.resolve_worst_case_phases`), never on a guess.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, Literal

from .site_capacity import DirectPhaseMeasurement, PhaseName, PHASES, PhaseValue

#: A phase drawing at least this much is the phase the charger charges on; anything below is noise.
AUTO_PHASE_DRAW_A: Final = 1.0
#: How long a new phase must hold before it counts, and the longest a two-phase reading keeps the old one.
AUTO_PHASE_SETTLE_S: Final = 10.0
#: How much less than the charger's draw the site's reading of that phase may show (the two are read at
#: different moments, so a ramp shows on one before the other).
AUTO_PHASE_SITE_TOLERANCE_A: Final = 2.0

#: More than one phase draws: in transition, or not a one-phase charge at all.
AMBIGUOUS: Final = "ambiguous"

DrawnPhase = PhaseName | Literal["ambiguous"] | None


def _fresh(value: PhaseValue, max_age_s: float) -> bool:
    return (
        value.value is not None
        and value.problem is None
        and value.age_s is not None
        and value.age_s <= max_age_s
    )


def drawn_phase(
    measured: DirectPhaseMeasurement | None,
    max_age_s: float,
    site_current_a: Mapping[PhaseName, float | None] | None,
) -> DrawnPhase:
    """The one phase `measured` shows the charger drawing on, `AMBIGUOUS` when more than one draws, or
    `None` when it shows none: idle, no source, any phase missing or stale (a source that reads one phase
    only cannot tell the phases apart), or a draw the site's reading of that phase does not bear out
    (`site_current_a` `None` or short of the draw by more than `AUTO_PHASE_SITE_TOLERANCE_A`)."""
    if measured is None:
        return None
    values = {phase: measured.get(phase) for phase in PHASES}
    if not all(_fresh(value, max_age_s) for value in values.values()):
        return None
    drawing = [phase for phase, value in values.items() if (value.value or 0.0) >= AUTO_PHASE_DRAW_A]
    if not drawing:
        return None
    if len(drawing) > 1:
        return AMBIGUOUS
    phase = drawing[0]
    site = None if site_current_a is None else site_current_a.get(phase)
    draw = values[phase].value or 0.0
    if site is None or site < draw - AUTO_PHASE_SITE_TOLERANCE_A:
        return None
    return phase


@dataclass(slots=True)
class AutoPhaseTracker:
    """One "Automatic" charger's followed phase, debounced. `observe` may be called any number of times
    with one reading: only the time decides when a new phase counts."""

    settle_s: float = AUTO_PHASE_SETTLE_S
    #: The phase it was last followed to; kept through a short transition.
    phase: PhaseName | None = None
    #: What `observe` last answered.
    current: PhaseName | None = None
    _candidate: PhaseName | None = None
    _candidate_since: float = 0.0
    _ambiguous_since: float | None = None

    def observe(self, drawn: DrawnPhase, now: float, *, settling: bool = False) -> PhaseName | None:
        """The phase to reckon the charger on, or `None` for the worst phase. `settling`: a phase switch settles, and
        the reading decides nothing (no new phase, none forgotten)."""
        if settling:
            self._candidate = None
            return self.current
        self.current = self._decide(drawn, now)
        return self.current

    def _decide(self, drawn: DrawnPhase, now: float) -> PhaseName | None:
        if drawn is None:
            self.phase = None
            self._candidate = None
            self._ambiguous_since = None
            return None
        if drawn == AMBIGUOUS:
            self._candidate = None
            if self._ambiguous_since is None:
                self._ambiguous_since = now
            if self.phase is not None and now - self._ambiguous_since < self.settle_s:
                return self.phase
            self.phase = None
            return None
        self._ambiguous_since = None
        if drawn == self.phase:
            self._candidate = None
            return self.phase
        if self._candidate != drawn:
            self._candidate = drawn
            self._candidate_since = now
        if now - self._candidate_since >= self.settle_s:
            self.phase = drawn
            self._candidate = None
            return self.phase
        # A new phase still settling: the old one no longer draws, so neither is trusted yet.
        return None
