"""Sigenergy's DC charger (EVDC): a charger given a power in kW, not a current per phase.

The TypQxQ "Sigenergy ESS" integration (2.0.3, platform `sigen`) gives the DC charger its own device with a Start
and a Stop button (register 41000), a charging power limit in kW (41002, 0 to 100 within the charger's rating) and
sensors for its output power, its rating, the car's state of charge and its running state.

**Power, not current.** Everything else in SpotNav reckons a charge in amperes per phase: the plan, load balancing,
the sun. A DC charger draws through the inverter, which takes its share of the grid on all three phases, so SpotNav's
current per phase is turned into the power three phases carry at the site's voltage between phases
(`kw_per_amp`: `sqrt(3) x V x I`, 0.693 kW per ampere at 400 V, as `planning/planner.power_kw` counts it) and written
to the limit. The plan's power is therefore the same number the limit gets. The charger's whole rating is one
current (`max_a`, rounded down), and a request at that current writes the full rating, not the rounded-down power.

**What is left out for a DC charger.** The 6 A floor stays SpotNav's lowest step (4.2 kW at 400 V), above the lowest
power the charger is assumed to hold (`EVDC_MIN_POWER_KW`, to be confirmed), so the floor never asks for less than
it can hold. Phase switching, the car's onboard phases and a per-phase cut do not exist for a DC charger: the power
is spread over three phases whatever car is plugged in (`planning/phases.py`).

**Conservative until the tester has answered** (`docs/setup.md`, Sigenergy DC charger): a pause presses Stop (the profile's
`zero_pauses`; whether a 0 kW limit pauses it is not known), no limit is written faster than every 10 s (the
policy), a raise waits `EVDC_SETTLE_S` after the last write so the charger has followed it, and every limit write,
Start and Stop is kept with the output power read after it (`PowerTiming`), which the diagnostics show.
"""

from __future__ import annotations

import math
from collections import deque
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any, Final

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import callback, HassJob, HomeAssistant, State
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_call_later
from homeassistant.util import dt as dt_util

from ...const import CURRENT_CONTROL_POWER, DEFAULT_MIN_CURRENT_A, DEFAULT_VOLTAGE_BETWEEN_PHASES_V
from ..charger_profiles import profile_for, WritePolicy
from .base import (
    _entity_ready,
    _finite,
    ASSIGN_ASSIGNED,
    ASSIGN_BELOW_MINIMUM,
    ASSIGN_RATE_LIMITED,
    ASSIGN_TARGET_UNAVAILABLE,
    ASSIGN_UNCHANGED,
    ASSIGN_UNCONFIRMED,
    ASSIGN_UNIT_UNKNOWN,
    ASSIGN_WRITE_FAILED,
    call_service,
    current_description,
    CurrentPath,
    device_entities,
    MODULATION_WRITES,
    WriteRateLimiter,
)
from .generic import ButtonPath
from .registry import ChargerContext, register_current

#: The lowest power the charger is taken to hold, in kW. TO BE CONFIRMED by the tester: a limit below it is never
#: written. SpotNav's 6 A floor (4.2 kW at 400 V) is above it, so today it only guards.
EVDC_MIN_POWER_KW: Final = 1.5
#: How long after a limit write a raise waits, so the charger has followed the last write before it is given more.
#: A lowering only waits for the policy's 10 s. TO BE CONFIRMED with how fast the charger follows a new limit.
EVDC_SETTLE_S: Final = 30.0
#: How many commands the timing log keeps, and when after each the output power is read again.
TIMING_LOG_SIZE: Final = 12
TIMING_SAMPLES_S: Final = (5.0, 15.0, 30.0, 60.0)
#: A limit is taken as followed when the output is within this share of it (or `FOLLOW_TOLERANCE_KW`).
FOLLOW_SHARE: Final = 0.1
FOLLOW_TOLERANCE_KW: Final = 0.3

_POWER_UNITS: Final = {"kw": 1.0, "w": 0.001, "mw": 1000.0}
#: What a limit read back may lie under the current it was written from (the write is rounded down to its step).
READ_BACK_SLACK_A: Final = 0.01


def kw_per_amp(voltage_between_phases_v: float) -> float:
    """The power one ampere per phase carries on three phases at this voltage between phases (kW)."""
    return math.sqrt(3.0) * voltage_between_phases_v / 1000.0


def power_kw(state: State | None) -> float | None:
    """A power entity's state in kW (from kW, W or MW), or `None` when it is unreadable or has no power unit."""
    if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN, ""):
        return None
    factor = _POWER_UNITS.get(str(state.attributes.get("unit_of_measurement") or "").strip().lower())
    value = _finite(state.state)
    if factor is None or value is None:
        return None
    return value * factor


def _native_factor(state: State | None) -> float | None:
    """How many of the number's own units make one kW (1 for kW, 1000 for W), or `None`."""
    if state is None:
        return None
    factor = _POWER_UNITS.get(str(state.attributes.get("unit_of_measurement") or "").strip().lower())
    return None if factor is None else 1.0 / factor


def charger_voltage_v(hass: HomeAssistant, charge_control: str | None) -> float:
    """The voltage between phases of the SpotNav charger whose charge control this is: its site's, else its own."""
    from ...const import CONF_CHARGE_CONTROL, DOMAIN  # noqa: PLC0415 - kept beside its only use
    from ...planning.grid_voltage import voltage_between_phases_v  # noqa: PLC0415 - the planning imports execution

    if charge_control:
        for entry in hass.config_entries.async_entries(DOMAIN):
            if entry.data.get(CONF_CHARGE_CONTROL) == charge_control:
                return voltage_between_phases_v(hass, entry.entry_id)
    return float(DEFAULT_VOLTAGE_BETWEEN_PHASES_V)


def charger_sensor(hass: HomeAssistant, number_entity_id: str | None, keys_of: str) -> str | None:
    """The sensor of the charger's own device that the profile names under `keys_of` (`output_power_keys` or
    `rated_power_keys`), found from the power number's device; `None` without one."""
    if not number_entity_id:
        return None
    entry = er.async_get(hass).async_get(number_entity_id)
    profile = profile_for(entry.platform if entry is not None else None)
    keys = getattr(profile, keys_of, ()) if profile is not None else ()
    if entry is None or not keys:
        return None
    found = device_entities(hass, entry.platform, "sensor", keys, number_entity_id)
    return found[0] if found else None


class PowerTiming:
    """What the charger did after each limit write, Start and Stop: the output power read at `TIMING_SAMPLES_S`.

    Kept in memory for the diagnostics, so a tester's debug report answers how fast the charger follows a new limit,
    the lowest power it holds and what a Stop does. Nothing reads it to decide anything.
    """

    def __init__(self, hass: HomeAssistant, output: Callable[[], str | None], status: Callable[[], str | None],
                 now: Callable[[], datetime] = dt_util.utcnow) -> None:
        self.hass = hass
        self._output = output
        self._status = status
        self._now = now
        self._records: deque[dict[str, Any]] = deque(maxlen=TIMING_LOG_SIZE)
        self._cancels: set[Callable[[], None]] = set()

    def output_kw(self) -> float | None:
        entity_id = self._output()
        return None if entity_id is None else power_kw(self.hass.states.get(entity_id))

    def note(self, kind: str, limit_kw: float | None = None) -> None:
        """Keep one command and read the output after it (scheduled, never awaited)."""
        at = self._now()
        record: dict[str, Any] = {
            "at": at.isoformat(),
            "kind": kind,
            "limit_kw": None if limit_kw is None else round(limit_kw, 3),
            "output_kw_before": self._rounded(self.output_kw()),
            "status_before": self._status(),
            "samples": [],
            "followed_after_s": None,
        }
        self._records.append(record)
        for after_s in TIMING_SAMPLES_S:
            self._schedule(record, after_s)

    @staticmethod
    def _rounded(value: float | None) -> float | None:
        return None if value is None else round(value, 3)

    def _schedule(self, record: dict[str, Any], after_s: float) -> None:
        handles: list[Callable[[], None]] = []

        @callback
        def sample(_now: datetime) -> None:
            self._cancels.discard(handles[0])
            output = self.output_kw()
            record["samples"].append(
                {"after_s": after_s, "output_kw": self._rounded(output), "status": self._status()}
            )
            limit = record["limit_kw"]
            if (
                record["followed_after_s"] is None
                and limit is not None
                and output is not None
                and abs(output - limit) <= max(FOLLOW_TOLERANCE_KW, FOLLOW_SHARE * limit)
            ):
                record["followed_after_s"] = after_s

        handles.append(
            async_call_later(self.hass, timedelta(seconds=after_s), HassJob(sample, cancel_on_shutdown=True))
        )
        self._cancels.add(handles[0])

    def cancel(self) -> None:
        for cancel in tuple(self._cancels):
            cancel()
        self._cancels.clear()

    def records(self) -> list[dict[str, Any]]:
        return [{**record, "samples": [dict(sample) for sample in record["samples"]]} for record in self._records]


class PowerCurrent(CurrentPath):
    """`number.set_value` on a power limit (kW or W), given a current per phase. See the module docstring.

    Refuses, in this order: below the 6 A floor or `EVDC_MIN_POWER_KW`, a number that is gone or has no power unit,
    a value it already carries, a raise within `EVDC_SETTLE_S` of the last write (the regulator's and the sun's only),
    and the policy's interval not yet elapsed.
    """

    kind = CURRENT_CONTROL_POWER
    can_set = True

    def __init__(
        self,
        hass: HomeAssistant,
        entity_id: str,
        policy: WritePolicy,
        *,
        limiter: WriteRateLimiter | None = None,
        voltage_v: Callable[[], float] = lambda: float(DEFAULT_VOLTAGE_BETWEEN_PHASES_V),
        rated_entity_id: Callable[[], str | None] = lambda: None,
        timing: PowerTiming | None = None,
        now: Callable[[], datetime] = dt_util.utcnow,
    ) -> None:
        super().__init__(policy)
        self.hass = hass
        self.entity_id = entity_id
        self._limiter = limiter or WriteRateLimiter(policy, now)
        self._voltage_v = voltage_v
        self._rated_entity_id = rated_entity_id
        self.timing = timing
        self._now = now

    # -- the conversion

    def kw_per_a(self) -> float:
        return kw_per_amp(self._voltage_v())

    def rated_kw(self) -> float | None:
        """The charger's rating (kW): its rated-power sensor, else the number's `max`; `None` when neither says."""
        rated_id = self._rated_entity_id()
        rated = power_kw(self.hass.states.get(rated_id)) if rated_id else None
        if rated is not None and rated > 0:
            return rated
        state = self.hass.states.get(self.entity_id)
        factor = _native_factor(state)
        maximum = _finite(state.attributes.get("max")) if state is not None else None
        if factor is None or maximum is None or maximum <= 0:
            return None
        return maximum / factor

    def max_a(self) -> int | None:
        """The current per phase the rating is, rounded down; `None` without a rating."""
        rated = self.rated_kw()
        if rated is None:
            return None
        return int(math.floor(rated / self.kw_per_a() + 1e-6))

    def kw_for(self, amps: int) -> float:
        """The power `amps` per phase is written as: the rating at its current or above, else the three-phase power."""
        rated = self.rated_kw()
        kw = amps * self.kw_per_a()
        maximum = self.max_a()
        if rated is not None and (kw > rated or (maximum is not None and amps >= maximum)):
            return rated
        return kw

    def _amps_of(self, kw: float) -> int:
        """The current per phase a power limit is: rounded down, after allowing for the limit's own step (a write is
        rounded down to it, so 6 A written as 4.156 kW reads back as 6 A, not 5)."""
        maximum = self.max_a()
        amps = int(math.floor(kw / self.kw_per_a() + READ_BACK_SLACK_A))
        rated = self.rated_kw()
        if maximum is not None and rated is not None and kw >= rated - 1e-6:
            return maximum
        return amps

    def limit_kw(self) -> float | None:
        state = self.hass.states.get(self.entity_id)
        factor = _native_factor(state)
        value = _finite(state.state) if state is not None else None
        if factor is None or value is None:
            return None
        return value / factor

    # -- `CurrentPath`

    def setpoint_a(self) -> int | None:
        limit = self.limit_kw()
        return self.last_written_a if limit is None else self._amps_of(limit)

    def read_back_a(self) -> int | None:
        limit = self.limit_kw()
        return None if limit is None else self._amps_of(limit)

    def ready(self) -> bool:
        return _entity_ready(self.hass, self.entity_id)

    def wait_s(self) -> float:
        return self._limiter.wait_s()

    async def async_set(self, amps: int, *, reason: str, verify: bool = False) -> str:
        if amps < DEFAULT_MIN_CURRENT_A:
            return ASSIGN_BELOW_MINIMUM
        state = self.hass.states.get(self.entity_id)
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return ASSIGN_TARGET_UNAVAILABLE
        factor = _native_factor(state)
        if factor is None:
            return ASSIGN_UNIT_UNKNOWN
        kw = self.kw_for(amps)
        if kw < EVDC_MIN_POWER_KW:
            return ASSIGN_BELOW_MINIMUM
        native = kw * factor
        step = _finite(state.attributes.get("step"))
        if step is not None and step > 0:
            native = math.floor(native / step + 1e-9) * step
            native = round(native, max(0, -int(math.floor(math.log10(step)))) + 1 if step < 1 else 0)
        minimum = _finite(state.attributes.get("min"))
        maximum = _finite(state.attributes.get("max"))
        if minimum is not None and native < minimum:
            return ASSIGN_BELOW_MINIMUM
        if maximum is not None and native > maximum:
            native = maximum
        present = _finite(state.state)
        if present is not None and abs(present - native) < max(1e-9, (step or 0.0) / 2):
            return ASSIGN_UNCHANGED
        last = self._limiter.last_write_at
        if (
            reason in MODULATION_WRITES
            and present is not None
            and native > present
            and last is not None
            and (self._now() - last).total_seconds() < EVDC_SETTLE_S
        ):
            # A raise waits until the charger has followed the last write.
            return ASSIGN_RATE_LIMITED
        if self._limiter.wait_s() > 0:
            return ASSIGN_RATE_LIMITED
        value: float | int = int(native) if float(native).is_integer() else native
        self._limiter.record()
        try:
            await call_service(
                self.hass, "number", "set_value", {"entity_id": self.entity_id, "value": value}, blocking=True
            )
        except Exception:  # noqa: BLE001 - the outcome code says it; the regulator decides what that means
            return ASSIGN_WRITE_FAILED
        self.last_written_a = amps
        if self.timing is not None:
            self.timing.note("limit", value / factor)
        if verify:
            after = _finite((self.hass.states.get(self.entity_id) or state).state)
            if after is None or abs(after - native) > 1e-6:
                return ASSIGN_UNCONFIRMED
        return ASSIGN_ASSIGNED

    def describe(self) -> dict[str, Any]:
        return current_description(self.kind, entity_id=self.entity_id, service="number.set_value")

    def describe_state(self) -> dict[str, Any]:
        rated = self.rated_kw()
        limit = self.limit_kw()
        return {
            "limit_kw": None if limit is None else round(limit, 3),
            "rated_kw": None if rated is None else round(rated, 3),
            "kw_per_a": round(self.kw_per_a(), 4),
            "max_a": self.max_a(),
            "min_power_kw": EVDC_MIN_POWER_KW,
            "settle_s": EVDC_SETTLE_S,
            "output_kw": None if self.timing is None else PowerTiming._rounded(self.timing.output_kw()),
            "timing": [] if self.timing is None else self.timing.records(),
        }


def _power_current(context: ChargerContext) -> CurrentPath | None:
    number = context.current_limit
    if not number:
        return None
    hass = context.hass

    def output() -> str | None:
        return charger_sensor(hass, number, "output_power_keys")

    timing = PowerTiming(hass, output, lambda: _status_text(context), context.now)
    path = context.path
    if isinstance(path, ButtonPath):
        # Start and Stop are kept in the same log, with what the output did after them.
        path.pressed = lambda kind: timing.note(kind)
    return PowerCurrent(
        hass,
        number,
        context.policy,
        limiter=context.limiter,
        voltage_v=lambda: charger_voltage_v(hass, context.charge_control),
        rated_entity_id=lambda: charger_sensor(hass, number, "rated_power_keys"),
        timing=timing,
        now=context.now,
    )


def _status_text(context: ChargerContext) -> str | None:
    try:
        return context.status()
    except KeyError:  # the adapter is not built yet
        return None


register_current(CURRENT_CONTROL_POWER, _power_current)
