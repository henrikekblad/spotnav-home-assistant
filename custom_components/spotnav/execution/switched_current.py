"""The current a charger draws whose current SpotNav cannot set, only switch on and off (`SwitchedCurrent`).

Such a charger (a smart plug, Monta, zappi, Ohme and others without a current setting) charges at a current of its
own, so load balancing and the sun reckon it as a switchable load of that size. Its "switched current" is learned,
from the first source that has anything to say:

1. **Its own measurement.** The charger's own current sensors (the highest phase), or a smart plug's power over
   `phases x NOMINAL_VOLTAGE_V`, read while it charges and the car draws (`DRAWING_FROM_A`). Smoothed: a rise is
   taken quickly (`RISE_WEIGHT`), a fall slowly (`FALL_WEIGHT`), so a car tapering off at the end of a charge does
   not make the next charge look small.
2. **The site meter's step**, only for a charger with no measurement of its own: when SpotNav switches it on or off,
   the change in the site's measured phase currents on the phases it is wired to (net of what the other chargers'
   own readings changed by), from the reading just before the command to the first stable reading after it
   (`STEP_SETTLE_S`, `STEP_STABLE_A`, within `STEP_WINDOW_S`). A step counts only when it is plausible: the same on
   every phase it is wired to (`STEP_PHASE_SPREAD_A`), quiet on the phases it is not (`STEP_QUIET_A`), at least
   `STEP_MIN_A` and not above the charger's configured maximum (`STEP_OVER_MAX_A`). It is trusted only once a
   second step agrees with it (`STEP_AGREE_A`): an on and an off edge, or the same edge again.
3. **The configured charging current** ("Laddström": the current Auto plans with, else the plan's or the last one
   asked for), before anything is learned; `DEFAULT_SWITCHED_CURRENT_A` when even that is not set. It is the
   current the person told SpotNav the charger uses, and what the plan already reckons with; the charger's wiring
   holds no maximum to use instead.

Pure and clock-free: the caller passes the readings and the time.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Mapping, Sequence

PhaseKey = Literal["L1", "L2", "L3"]
PHASE_KEYS: tuple[PhaseKey, ...] = ("L1", "L2", "L3")

SwitchedSource = Literal["measured", "site_step", "charging_current", "default"]

NOMINAL_VOLTAGE_V = 230.0
# A reading below this is a car not drawing (yet), not a charge current.
DRAWING_FROM_A = 2.0
RISE_WEIGHT = 0.5
FALL_WEIGHT = 0.05
# A change smaller than this is not worth a save.
SAVE_STEP_A = 0.5
DEFAULT_SWITCHED_CURRENT_A = 16.0

STEP_SETTLE_S = 5.0
STEP_WINDOW_S = 120.0
STEP_STABLE_A = 0.5
STEP_PHASE_SPREAD_A = 1.5
STEP_QUIET_A = 1.0
STEP_MIN_A = DRAWING_FROM_A
STEP_OVER_MAX_A = 1.0
STEP_AGREE_A = 1.5

Edge = Literal["on", "off"]


@dataclass(frozen=True, slots=True)
class SwitchedCurrentValue:
    """The current to reckon the charger with, and where it came from."""

    amps: float
    source: SwitchedSource


@dataclass(slots=True)
class _OpenEdge:
    kind: Edge
    at: float
    phases: tuple[PhaseKey, ...]
    baseline: dict[PhaseKey, float]
    maximum_a: float
    last: dict[PhaseKey, float] | None = None


def measured_amps(
    *, current_a: float | None, power_w: float | None, phases: int
) -> tuple[float | None, str | None]:
    """The charger's own draw in amps and which reading it came from: its current sensors' highest phase, else a
    smart plug's power over `phases x NOMINAL_VOLTAGE_V`; `(None, None)` with neither."""
    if current_a is not None:
        return max(0.0, float(current_a)), "current"
    if power_w is not None and phases > 0:
        return max(0.0, float(power_w)) / (phases * NOMINAL_VOLTAGE_V), "power"
    return None, None


class SwitchedCurrent:
    """One charger's learned switched current (module docstring)."""

    def __init__(self) -> None:
        self._measured_a: float | None = None
        self._step_a: float | None = None
        # A plausible step not yet confirmed by a second one: (edge, amps).
        self._candidate: tuple[Edge, float] | None = None
        self._edge: _OpenEdge | None = None
        self._last_step: dict[str, Any] | None = None

    # -- source 1: the charger's own measurement

    def observe_draw(self, amps: float | None) -> None:
        """One reading of the charger's own draw while it charges (`None`: nothing read). Below `DRAWING_FROM_A` the
        car is not drawing, and nothing is learned."""
        if amps is None or amps < DRAWING_FROM_A:
            return
        if self._measured_a is None:
            self._measured_a = float(amps)
            return
        weight = RISE_WEIGHT if amps > self._measured_a else FALL_WEIGHT
        self._measured_a += weight * (amps - self._measured_a)

    # -- source 2: the site meter's step

    def begin_edge(
        self,
        kind: Edge,
        *,
        now: float,
        phases: Sequence[PhaseKey],
        baseline: Mapping[PhaseKey, float | None],
        maximum_a: float,
    ) -> None:
        """SpotNav switched the charger on (`on`) or off (`off`); `baseline` is the site's phase currents (net of the
        other chargers' own readings) just before. A baseline missing a phase starts nothing."""
        values: dict[PhaseKey, float] = {}
        for phase in PHASE_KEYS:
            value = baseline.get(phase)
            if value is None:
                self._edge = None
                return
            values[phase] = float(value)
        if not phases:
            self._edge = None
            return
        self._edge = _OpenEdge(kind, now, tuple(phases), values, float(maximum_a))

    def observe_site(self, readings: Mapping[PhaseKey, float | None], *, now: float) -> None:
        """A site reading (net as for `begin_edge`) after an edge: the first one that holds still against the one
        before it, past the settle time, is the step's other end."""
        edge = self._edge
        if edge is None:
            return
        if now - edge.at > STEP_WINDOW_S:
            self._end_edge(edge, None, "no_stable_reading")
            return
        values: dict[PhaseKey, float] = {}
        for phase in PHASE_KEYS:
            value = readings.get(phase)
            if value is None:
                return
            values[phase] = float(value)
        if now - edge.at < STEP_SETTLE_S:
            edge.last = values
            return
        previous = edge.last
        edge.last = values
        if previous is None or any(abs(values[p] - previous[p]) > STEP_STABLE_A for p in PHASE_KEYS):
            return
        sign = 1.0 if edge.kind == "on" else -1.0
        steps = {phase: sign * (values[phase] - edge.baseline[phase]) for phase in PHASE_KEYS}
        wired = [steps[phase] for phase in edge.phases]
        quiet = [abs(steps[phase]) for phase in PHASE_KEYS if phase not in edge.phases]
        amps = sum(wired) / len(wired)
        if max(wired) - min(wired) > STEP_PHASE_SPREAD_A or any(q > STEP_QUIET_A for q in quiet):
            self._end_edge(edge, amps, "noisy")
        elif amps < STEP_MIN_A:
            self._end_edge(edge, amps, "too_small")
        elif amps > edge.maximum_a + STEP_OVER_MAX_A:
            self._end_edge(edge, amps, "above_maximum")
        else:
            self._end_edge(edge, amps, "plausible")
            self._take_step(edge.kind, amps)

    def _end_edge(self, edge: _OpenEdge, amps: float | None, outcome: str) -> None:
        self._edge = None
        self._last_step = {"edge": edge.kind, "amps": None if amps is None else round(amps, 2), "outcome": outcome}

    def _take_step(self, kind: Edge, amps: float) -> None:
        candidate = self._candidate
        if self._step_a is not None and abs(amps - self._step_a) <= STEP_AGREE_A:
            # Agrees with what is trusted: refines it.
            self._step_a = (self._step_a + amps) / 2.0
            self._candidate = None
            return
        if candidate is not None and abs(amps - candidate[1]) <= STEP_AGREE_A:
            self._step_a = (candidate[1] + amps) / 2.0
            self._candidate = None
            return
        self._candidate = (kind, amps)

    # -- the answer

    def value(self, configured_a: float | None) -> SwitchedCurrentValue:
        """The current to reckon with: measured, else the trusted site step, else the configured charging current,
        else `DEFAULT_SWITCHED_CURRENT_A`."""
        if self._measured_a is not None:
            return SwitchedCurrentValue(round(self._measured_a, 2), "measured")
        if self._step_a is not None:
            return SwitchedCurrentValue(round(self._step_a, 2), "site_step")
        if configured_a is not None and configured_a > 0:
            return SwitchedCurrentValue(float(configured_a), "charging_current")
        return SwitchedCurrentValue(DEFAULT_SWITCHED_CURRENT_A, "default")

    @property
    def learned(self) -> bool:
        return self._measured_a is not None or self._step_a is not None

    def snapshot(self, configured_a: float | None) -> dict[str, Any]:
        """For the diagnostics."""
        value = self.value(configured_a)
        return {
            "amps": value.amps,
            "source": value.source,
            "measured_a": None if self._measured_a is None else round(self._measured_a, 2),
            "site_step_a": None if self._step_a is None else round(self._step_a, 2),
            "unconfirmed_step_a": None if self._candidate is None else round(self._candidate[1], 2),
            "edge_open": None if self._edge is None else self._edge.kind,
            "last_step": None if self._last_step is None else dict(self._last_step),
            "configured_a": configured_a,
        }

    # -- storage

    def to_store(self) -> dict[str, Any] | None:
        if self._measured_a is None and self._step_a is None:
            return None
        return {
            "measured_a": None if self._measured_a is None else round(self._measured_a, 2),
            "site_step_a": None if self._step_a is None else round(self._step_a, 2),
        }

    def signature(self) -> tuple[float | None, float | None]:
        """What is worth saving, coarsely (`SAVE_STEP_A`): a smoothed value moving by tenths is not saved each time."""

        def coarse(value: float | None) -> float | None:
            return None if value is None else round(value / SAVE_STEP_A) * SAVE_STEP_A

        return coarse(self._measured_a), coarse(self._step_a)

    @classmethod
    def from_store(cls, raw: Any) -> SwitchedCurrent:
        learner = cls()
        if isinstance(raw, dict):
            for key, attr in (("measured_a", "_measured_a"), ("site_step_a", "_step_a")):
                value = raw.get(key)
                if isinstance(value, (int, float)) and not isinstance(value, bool) and 0 < value <= 80:
                    setattr(learner, attr, float(value))
        return learner
