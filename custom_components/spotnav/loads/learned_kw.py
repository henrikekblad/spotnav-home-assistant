"""Each watched or controlled load's real power, learned (the pool's heat pump and circulation, the spa's heater, the
sauna's heater). Pure: the shell (`pool/runtime.py`) reads the load's on/off, its device's power sensor and the site's
house figure at each look and hands them over; nothing is read or written here.

**Which kW counts** (`effective_kw`): the entered kW (the person's override), else the learned one once trusted, else
`None` (the caller's typical figure stands in until something is learned). `source` says which: `entered`,
`learned` or `typical`.

**The device's power sensor first** (`pick_power_sensor` on the load's device, as the water heater reads its meter,
`heat/water_heater_control.py`): trusted only once it followed one of the load's own switchings seen at a look
(`TRUST_WITHIN`): an on that raises it to at least `DRAW_MIN_W`, or an off that takes it under `CUT_W`. A meter on a
contactor's coil, another channel of the device or one frozen at a figure never follows, so it never counts; a later
off it does not follow takes the trust back. A watched load is switched by its own thermostat or the household, a
controlled one by SpotNav: each switching seen counts the same. While trusted, each look the load reads on and the
sensor at least `DRAW_MIN_W` is a reading; the learned kW is their median (`DEVICE_SAMPLES` kept, from
`DEVICE_TRUST` on).

**Else the site's meter step**: the house figure (the grid less the cars and the battery's charge, plus its
discharge) at the look before a switching against the one `STEP_AFTER` or more after it (at most `STEP_WITHIN`), its
rise at an on, its fall at an off. A step is no sample where another of the loads switched in between, the load
switched back, either reading is missing, or it lies outside the load's bounds (`BOUNDS_KW`). The learned kW is the
median of the samples that agree (`STEP_SPREAD` of their median, at least `STEP_TRUST` of them, of the last
`STEP_SAMPLES`): the sun or a kettle moving in the same half minute makes one sample wrong, not the figure.

**Stored** (`as_dict`/`restore`) in the pool and spa shadow's book; read as untrusted.
"""

from __future__ import annotations

import statistics
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Final

LOAD_POOL_HEAT: Final = "pool_heat"
LOAD_POOL_CIRCULATION: Final = "pool_circulation"
LOAD_SPA: Final = "spa"
LOAD_SAUNA: Final = "sauna"
LOADS: Final = (LOAD_POOL_HEAT, LOAD_POOL_CIRCULATION, LOAD_SPA, LOAD_SAUNA)

SOURCE_ENTERED: Final = "entered"
SOURCE_LEARNED: Final = "learned"
SOURCE_TYPICAL: Final = "typical"

#: What a load may plausibly draw (kW): a step outside is no sample (the settings' own ranges, a little wider).
BOUNDS_KW: Final[dict[str, tuple[float, float]]] = {
    LOAD_POOL_HEAT: (0.2, 16.0),
    LOAD_POOL_CIRCULATION: (0.03, 3.5),
    LOAD_SPA: (0.4, 13.0),
    LOAD_SAUNA: (1.5, 16.0),
}
#: The device's sensor: what counts as drawing, and as cut, and how soon after a switching it must follow.
DRAW_MIN_W: Final = 30.0
CUT_W: Final = 15.0
TRUST_WITHIN: Final = timedelta(minutes=2)
DEVICE_SAMPLES: Final = 60
DEVICE_TRUST: Final = 5
#: The site's meter step.
STEP_AFTER: Final = timedelta(seconds=20)
STEP_WITHIN: Final = timedelta(seconds=150)
STEP_SAMPLES: Final = 12
STEP_TRUST: Final = 3
STEP_SPREAD: Final = 0.2


def _finite(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if number == number and abs(number) != float("inf") else None


@dataclass(frozen=True, slots=True)
class Look:
    """One load at one look: whether it reads on, its device's power sensor and reading (W), and the site's house
    figure (W, `None` unread)."""

    on: bool | None
    sensor: str | None = None
    device_w: float | None = None
    house_w: float | None = None


@dataclass
class _Watch:
    """A switching seen, waiting for the device sensor to follow it and for the meter's reading after it."""

    on: bool
    at: datetime
    house_before: float | None
    sensor: str | None


@dataclass
class LoadPower:
    """What is learned of one load (see the module)."""

    device_draws: list[float] = field(default_factory=list)
    #: The device sensor trusted as the load's (one that followed a switching).
    trusted_sensor: str | None = None
    steps: list[float] = field(default_factory=list)
    last_on: bool | None = None
    last_house_w: float | None = None
    watch: _Watch | None = None

    def device_kw(self) -> float | None:
        if self.trusted_sensor is None or len(self.device_draws) < DEVICE_TRUST:
            return None
        return statistics.median(self.device_draws) / 1000.0

    def step_kw(self) -> float | None:
        if len(self.steps) < STEP_TRUST:
            return None
        middle = statistics.median(self.steps)
        agreeing = [value for value in self.steps if abs(value - middle) <= STEP_SPREAD * middle]
        return statistics.median(agreeing) if len(agreeing) >= STEP_TRUST else None

    def learned(self) -> tuple[float | None, str | None]:
        """The learned kW and where from (`device` or `meter`), `(None, None)` before it is trusted."""
        device = self.device_kw()
        if device is not None:
            return device, "device"
        step = self.step_kw()
        return (step, "meter") if step is not None else (None, None)


class PowerLearner:
    """Every load's learned power (see the module)."""

    def __init__(self) -> None:
        self.loads: dict[str, LoadPower] = {}

    def part(self, load: str) -> LoadPower:
        return self.loads.setdefault(load, LoadPower())

    def observe(self, now: datetime, looks: Mapping[str, Look]) -> None:
        """One look at every load the site has: learn from the switchings seen (see the module)."""
        switched = {
            load
            for load, look in looks.items()
            if look.on is not None and (part := self.loads.get(load)) is not None and part.last_on is not None and part.last_on != look.on
        }
        for load, look in looks.items():
            self._observe(now, load, look, others=bool(switched - {load}))
        for load in [load for load in self.loads if load not in looks]:
            # A load no longer set up forgets its switching in progress (its learned power is kept).
            self.loads[load].watch = None

    def _observe(self, now: datetime, load: str, look: Look, *, others: bool) -> None:
        part = self.part(load)
        low, high = BOUNDS_KW.get(load, (0.0, float("inf")))
        watch = part.watch
        if watch is not None:
            self._judge(now, load, part, watch, look, others=others, low=low, high=high)
        if look.on is not None and part.last_on is not None and look.on != part.last_on:
            part.watch = _Watch(look.on, now, part.last_house_w if not others else None, look.sensor)
        if look.sensor != part.trusted_sensor and part.trusted_sensor is not None:
            # Another sensor on the device: it has to follow a switching itself.
            part.trusted_sensor = None
            part.device_draws = []
        if (
            look.on
            and part.trusted_sensor is not None
            and look.sensor == part.trusted_sensor
            and look.device_w is not None
            and look.device_w >= DRAW_MIN_W
            and low * 1000.0 <= look.device_w <= high * 1000.0
        ):
            part.device_draws = [*part.device_draws, look.device_w][-DEVICE_SAMPLES:]
        if look.on is not None:
            part.last_on = look.on
        part.last_house_w = look.house_w

    @staticmethod
    def _judge(
        now: datetime, load: str, part: LoadPower, watch: _Watch, look: Look, *, others: bool, low: float, high: float
    ) -> None:
        """A switching in progress: does the device sensor follow it, and what step did the meter see?"""
        timed_out = now - watch.at > max(TRUST_WITHIN, STEP_WITHIN)
        if timed_out or (look.on is not None and look.on != watch.on):
            # Too late, or switched back: nothing more from it. A trusted sensor that never followed an off the load
            # stayed in loses its trust.
            if timed_out and not watch.on and part.trusted_sensor is not None and watch.sensor == part.trusted_sensor:
                part.trusted_sensor = None
                part.device_draws = []
            part.watch = None
            return
        if watch.sensor is not None and look.sensor == watch.sensor and look.device_w is not None and now - watch.at <= TRUST_WITHIN:
            followed = look.device_w >= DRAW_MIN_W if watch.on else look.device_w < CUT_W
            if followed:
                part.trusted_sensor = watch.sensor
                watch.sensor = None
        if watch.house_before is not None and now - watch.at >= STEP_AFTER:
            if not others and look.house_w is not None and now - watch.at <= STEP_WITHIN:
                step = (look.house_w - watch.house_before) / 1000.0
                kw = step if watch.on else -step
                if low <= kw <= high:
                    part.steps = [*part.steps, kw][-STEP_SAMPLES:]
            watch.house_before = None
        if others:
            watch.house_before = None
        if watch.sensor is None and watch.house_before is None:
            part.watch = None

    def learned(self, load: str) -> tuple[float | None, str | None]:
        part = self.loads.get(load)
        return (None, None) if part is None else part.learned()

    def effective_kw(self, load: str, entered: float | None) -> tuple[float | None, str]:
        """The kW that counts and where from (see the module): `(None, "typical")` until something is learned."""
        if entered is not None and entered > 0:
            return entered, SOURCE_ENTERED
        kw, _how = self.learned(load)
        if kw is not None:
            return round(kw, 2), SOURCE_LEARNED
        return None, SOURCE_TYPICAL

    def as_dict(self) -> dict[str, Any]:
        return {
            load: {
                "device_draws": [round(value, 1) for value in part.device_draws],
                "trusted_sensor": part.trusted_sensor,
                "steps": [round(value, 3) for value in part.steps],
            }
            for load, part in self.loads.items()
            if part.device_draws or part.trusted_sensor or part.steps
        }

    def restore(self, raw: Any) -> None:
        if not isinstance(raw, Mapping):
            return
        for load, row in raw.items():
            if load not in LOADS or not isinstance(row, Mapping):
                continue
            low, high = BOUNDS_KW[load]
            draws = [v for v in (_finite(x) for x in row.get("device_draws") or ()) if v is not None and v >= DRAW_MIN_W]
            steps = [v for v in (_finite(x) for x in row.get("steps") or ()) if v is not None and low <= v <= high]
            sensor = row.get("trusted_sensor")
            self.loads[load] = LoadPower(
                device_draws=draws[-DEVICE_SAMPLES:],
                trusted_sensor=sensor if isinstance(sensor, str) and sensor.startswith("sensor.") else None,
                steps=steps[-STEP_SAMPLES:],
            )

    def diagnostics(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for load, part in self.loads.items():
            kw, how = part.learned()
            out[load] = {
                "kw": None if kw is None else round(kw, 3),
                "from": how,
                "trusted_sensor": part.trusted_sensor,
                "device_draws": len(part.device_draws),
                "steps": [round(value, 3) for value in part.steps],
            }
        return out
