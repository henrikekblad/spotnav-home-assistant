"""Which phases make a site's measurement unusable, and why.

The site result says only that the measurement is missing, invalid or stale. This names the phases and
the cause, so the card can say "L2 and L3 have no value (sensor.x_l2, sensor.x_l3)" or "L1 is older
than 120 s". Pure: the result and the phase-to-entity map go in, facts come out.

When the phases with no value read `unavailable` or `unknown` together (every phase at once, or the
sensors of a known inverter integration, which typically go unavailable when the inverter goes to standby
at night), the problem names them as the meter's sensors being unavailable (`unavailable_entities`,
`inverter`) rather than as a generic missing value. The safety behaviour is the same either way.

When phases with no value read a negative current while the site is not set to read a signed current,
the problem names them (`negative_phases`): the meter reports export as a negative current, and turning
on "Grid current is signed" is the fix. The measurement stays unusable until then.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
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


#: Integrations of inverters that read the grid with their own meter and whose sensors go unavailable
#: while the inverter is in standby (at night, typically).
INVERTER_PLATFORMS: Final = frozenset(
    {
        "foxess",
        "foxess_modbus",
        "goodwe",
        "growatt_server",
        "huawei_solar",
        "sigen",
        "sma",
        "pysmaplus",
        "solarman",
        "solaredge",
        "solaredge_modbus",
        "solaredge_modbus_multi",
        "solax",
        "solax_modbus",
        "solis",
        "solis_modbus",
        "sungrow",
        "fronius",
    }
)


@dataclass(frozen=True, slots=True)
class MeasurementProblem:
    phases: tuple[PhaseProblem, ...]
    max_age_s: float
    #: The meter's sensors that read `unavailable`/`unknown` together; empty unless that is the whole
    #: problem (see the module docstring).
    unavailable_entities: tuple[str, ...] = ()
    #: They are a known inverter integration's (`INVERTER_PLATFORMS`): it may be in standby.
    inverter: bool = False
    #: The phases with no value whose reading is a negative current on a site not set to read a signed
    #: current (see the module docstring); empty otherwise.
    negative_phases: tuple[PhaseName, ...] = ()

    def of(self, cause: PhaseCause) -> tuple[PhaseProblem, ...]:
        return tuple(problem for problem in self.phases if problem.cause == cause)


def measurement_problem(
    result: SiteCapacityResult,
    entities: Mapping[PhaseName, str | None],
    *,
    unavailable: Collection[str] = (),
    platforms: Mapping[str, str] | None = None,
    negative: Collection[PhaseName] = (),
) -> MeasurementProblem | None:
    """The phases behind an unhealthy site measurement, or `None` while it is healthy or unconfigured.

    `unavailable` are the entities whose state is `unavailable` or `unknown`; `platforms` maps an entity
    to the integration that provides it; `negative` are the phases that read a negative current on a
    site not set to read a signed one.
    """
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
    if not problems:
        return None
    gone, inverter = _unavailable_together(problems, unavailable, platforms or {})
    negative_phases = tuple(
        problem.phase for problem in problems if problem.cause == "no_value" and problem.phase in negative
    )
    return MeasurementProblem(tuple(problems), float(result.max_age_s), gone, inverter, negative_phases)


def _unavailable_together(
    problems: list[PhaseProblem], unavailable: Collection[str], platforms: Mapping[str, str]
) -> tuple[tuple[str, ...], bool]:
    """The entities of a problem that is only the meter's sensors being unavailable, and whether they are
    an inverter's: every problem phase has no value from a named entity that reads `unavailable`/`unknown`,
    and either every phase is affected or all of them are a known inverter integration's."""
    if any(problem.cause != "no_value" or problem.entity_id is None for problem in problems):
        return (), False
    entity_ids = [problem.entity_id for problem in problems if problem.entity_id is not None]
    if not all(entity_id in unavailable for entity_id in entity_ids):
        return (), False
    inverter = all(platforms.get(entity_id) in INVERTER_PLATFORMS for entity_id in entity_ids)
    if len(problems) < len(PHASES) and not inverter:
        return (), False
    return tuple(dict.fromkeys(entity_ids)), inverter
