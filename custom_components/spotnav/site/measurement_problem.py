"""Which phases make a site's measurement unusable, and why.

The site result says only that the measurement is missing, invalid or stale. This names the phases and
the cause, so the card can say "L2 and L3 have no value (sensor.x_l2, sensor.x_l3)" or "L1 is older
than 120 s". Pure: the result and the phase-to-entity map go in, facts come out.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, Literal

from .site_capacity import PHASES, PhaseName, SiteCapacityResult

#: The result states that stop a measurement being used.
UNHEALTHY_STATES: Final = frozenset({"missing_measurements", "invalid_measurements", "stale_measurements"})

#: `no_value`: nothing usable was read for the phase; `stale`: a value, but older than the maximum age.
PhaseCause = Literal["no_value", "stale"]


@dataclass(frozen=True, slots=True)
class PhaseProblem:
    phase: PhaseName
    cause: PhaseCause
    #: The entity the phase is read from, when one entity answers for it.
    entity_id: str | None
    #: Seconds since the value last changed (stale phases).
    age_s: float | None


@dataclass(frozen=True, slots=True)
class MeasurementProblem:
    phases: tuple[PhaseProblem, ...]
    max_age_s: float

    def of(self, cause: PhaseCause) -> tuple[PhaseProblem, ...]:
        return tuple(problem for problem in self.phases if problem.cause == cause)


def measurement_problem(
    result: SiteCapacityResult, entities: Mapping[PhaseName, str | None]
) -> MeasurementProblem | None:
    """The phases behind an unhealthy site measurement, or `None` while it is healthy or unconfigured."""
    if result.state not in UNHEALTHY_STATES:
        return None
    problems: list[PhaseProblem] = []
    for phase in PHASES:
        value = result.measured_phase_current_a.get(phase)
        liveness = result.phase_liveness.get(phase)
        if value is None:
            problems.append(PhaseProblem(phase, "no_value", entities.get(phase), None))
        elif liveness in ("unconfirmed_stale", "no_recent_report"):
            problems.append(PhaseProblem(phase, "stale", entities.get(phase), result.phase_age_s.get(phase)))
    return MeasurementProblem(tuple(problems), float(result.max_age_s)) if problems else None
