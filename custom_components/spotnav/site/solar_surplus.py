"""Solar surplus mode: how much a charger should want to draw from solar power.

Pure Python (no Home Assistant import, no I/O, no clock; time arrives via
`SolarObservation.now`). Per charger it decides only whether surplus justifies
starting, stopping or re-requesting a current. Start and stop are rate-limited
by minimum on/off times; modulation is only a new *requested* current for site
capacity's damped write path. It must never call a charger's `async_start` on
every tick.

Surplus is the energy balance at the house bus:

    net_grid_w = house_load_w + car_w + battery_w - pv_w
    =>  pv_w - house_load_w = car_w + battery_w - net_grid_w

    car_first:      available_w = car_w + battery_w - net_grid_w
    battery_first:  available_w = car_w + min(0, battery_w) - net_grid_w

`export_w + car_w` would be wrong: `export_w` clamps at zero, so once the site
imports, the car's own draw is reported back as enough surplus. The identity has
no clamp, so grid- or battery-financed energy is subtracted out. `export_w` is
exposed on verdicts for diagnostics only. `battery_first` does not credit the car
with power going into the battery; a discharge passes through. Priority is a
setting because a battery regulating the grid to zero leaves export ~0.

`battery_configured=False` treats `battery_w is None` as 0.0 and reports
`priority_effective = "battery_first"`. With `battery_configured=True` it means
the battery is unreadable, which is no basis (0.0 would hide a discharge).

With the meter's total power instead of per-phase power (`SolarObservation.phase_cap_a`), the total is
split evenly over the phases the car uses and each phase's figure is capped by its fuse headroom.

Freshness: a `None` where a reading is needed means no basis this tick. From
`off` or `arming` that means never start; a running charge is kept for
`stale_grace_s`. Stale ticks neither advance nor reset the timers.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal, Mapping

from .site_capacity import PhaseName, PHASES


# Charger priority between car and battery; also the type of
# `SolarVerdict.priority_effective`.
SolarPriority = Literal["car_first", "battery_first"]

# State of one charger's controller.
SolarState = Literal["off", "arming", "on", "disarming"]

# What a verdict tells the caller to do. `requested_a` is set only for
# `"start"` and `"set_current"`.
SolarAction = Literal["start", "stop", "set_current", "hold"]

# One reason per branch of `observe`, so a log line says which rule decided.
SolarReason = Literal[
    # No basis this tick while `off` or `arming` (no grace).
    "no_basis_off",
    "no_basis_grace",
    "no_basis_stopped",
    "off_no_surplus",
    "arming_delay",
    "arming_min_off_wait",
    # Dipped below `start_a` while arming: back to `off`, delay restarts.
    "arming_dip",
    "start_after_delay",
    "on_steady",
    "on_modulate",
    # Surplus below `stop_a` but `stop_delay_s` not yet elapsed (a passing cloud).
    "disarming_delay",
    "disarming_min_on_wait",
    # Recovered above `stop_a`: back to `on`.
    "disarming_recover",
    "stop_after_delay",
]


@dataclass(frozen=True, slots=True, kw_only=True)
class SolarConfig:
    """Tunables, charger limits and `priority`.

    `start_a`/`stop_a` are `None` only as sentinels: `__post_init__` resolves
    them to `min_current_a` and `start_a - 1.0`.
    """

    priority: SolarPriority = "car_first"
    min_current_a: float = 6.0
    max_current_a: float = 16.0
    start_a: float | None = None
    stop_a: float | None = None
    start_delay_s: float = 120.0
    stop_delay_s: float = 300.0
    min_on_s: float = 600.0
    min_off_s: float = 300.0
    stale_grace_s: float = 120.0

    def __post_init__(self) -> None:
        start_a = self.min_current_a if self.start_a is None else self.start_a
        object.__setattr__(self, "start_a", start_a)
        if self.stop_a is None:
            object.__setattr__(self, "stop_a", start_a - 1.0)


@dataclass(frozen=True, slots=True)
class SolarObservation:
    """One snapshot of readings, keyed by phase; a missing or `None` entry on a
    needed phase means no basis. `battery_w` is positive when charging; `now` is
    seconds from the caller's clock.
    """

    now: float
    signed_grid_w: Mapping[PhaseName, float | None]
    voltage_v: Mapping[PhaseName, float | None]
    car_delivered_a: Mapping[PhaseName, float | None]
    battery_w: float | None
    car_phases: tuple[PhaseName, ...]
    # Whether a battery entity exists (wiring fact from the caller, not inferred from `battery_w`).
    battery_configured: bool = False
    # The most current this charger may draw on each phase, `None` for no cap. A site that reports only
    # the meter's total power (no per-phase power) has the total split evenly over the phases the car
    # uses, which says nothing about one phase being loaded: the cap is that phase's fuse headroom on
    # top of the car's own draw. Given, it covers every phase of `car_phases`, and a `None` entry there
    # is no basis.
    phase_cap_a: Mapping[PhaseName, float | None] | None = None


@dataclass(frozen=True, slots=True)
class SolarVerdict:
    """What to do about one charger and why. The surplus breakdown is the last
    known basis on stale ticks, `None` before the first usable observation.
    """

    action: SolarAction
    requested_a: float | None
    reason: SolarReason
    state: SolarState
    net_grid_w: float | None
    export_w: float | None
    car_w: float | None
    battery_w: float | None
    available_w: float | None
    available_a: float | None
    priority_effective: SolarPriority | None


class SolarController:
    """One instance per charger: the solar state machine, fed by `observe`."""

    def __init__(self, config: SolarConfig) -> None:
        self._config = config

        self._state: SolarState = "off"

        # Absolute timer anchors, set on fresh entry into a state. `_on_since`
        # survives `disarming_recover`; `_last_stop_at` is `None` until the first stop.
        self._arming_since: float | None = None
        self._on_since: float | None = None
        self._disarming_since: float | None = None
        self._last_stop_at: float | None = None

        self._last_requested_a: float | None = None

        self._stale_since: float | None = None

        self._net_grid_w: float | None = None
        self._export_w: float | None = None
        self._car_w: float | None = None
        self._battery_w: float | None = None
        self._available_w: float | None = None
        self._available_a: float | None = None
        self._priority_effective: SolarPriority | None = None

    def observe(self, observation: SolarObservation) -> SolarVerdict:
        """Update state from one observation and return its verdict."""
        now = observation.now

        available_a = self._refresh_breakdown(observation)
        if available_a is None:
            return self._handle_no_basis(now)

        self._stale_since = None
        if self._state in ("off", "arming"):
            return self._handle_off_or_arming(now, available_a)
        return self._handle_on_or_disarming(now, available_a)

    def _refresh_breakdown(self, observation: SolarObservation) -> float | None:
        """Compute this tick's surplus breakdown and store it for `_verdict`.

        Returns `available_a`, or `None` if a needed reading is unusable; the
        stored breakdown is then left as it was.
        """
        cfg = self._config
        grid = observation.signed_grid_w
        voltage = observation.voltage_v
        delivered = observation.car_delivered_a
        car_phases = observation.car_phases

        if not car_phases:
            return None
        if any(grid.get(phase) is None for phase in PHASES):
            return None
        if any(voltage.get(phase) is None for phase in car_phases):
            return None
        if any(delivered.get(phase) is None for phase in car_phases):
            return None
        if observation.battery_configured and observation.battery_w is None:
            # A configured but unreadable battery is no basis, never 0.0.
            return None

        net_grid_w = sum(grid[phase] for phase in PHASES)  # type: ignore[misc]
        export_w = max(0.0, -net_grid_w)
        car_w = sum(delivered[phase] * voltage[phase] for phase in car_phases)  # type: ignore[misc]

        battery_w = observation.battery_w
        battery_w_for_formula = 0.0 if battery_w is None else battery_w
        priority_effective: SolarPriority
        if cfg.priority == "car_first" and battery_w is not None:
            # The energy-balance identity (module docstring).
            available_w = car_w + battery_w_for_formula - net_grid_w
            priority_effective = "car_first"
        else:
            available_w = car_w + min(0.0, battery_w_for_formula) - net_grid_w
            priority_effective = "battery_first"

        mean_voltage = sum(voltage[phase] for phase in car_phases) / len(car_phases)  # type: ignore[misc]
        if mean_voltage <= 0:
            return None
        available_a = available_w / (len(car_phases) * mean_voltage)
        if observation.phase_cap_a is not None:
            caps = [observation.phase_cap_a.get(phase) for phase in car_phases]
            if any(cap is None for cap in caps):
                return None
            cap_a = min(caps)  # type: ignore[type-var]
            if available_a > cap_a:
                available_a = cap_a
                available_w = cap_a * len(car_phases) * mean_voltage

        self._net_grid_w = net_grid_w
        self._export_w = export_w
        self._car_w = car_w
        self._battery_w = battery_w
        self._available_w = available_w
        self._available_a = available_a
        self._priority_effective = priority_effective
        return available_a

    def _handle_no_basis(self, now: float) -> SolarVerdict:
        if self._stale_since is None:
            self._stale_since = now

        if self._state in ("off", "arming"):
            self._state = "off"
            self._arming_since = None
            return self._verdict("hold", None, "no_basis_off")

        # Kept for `stale_grace_s`, anchors untouched so a returning reading resumes.
        if now - self._stale_since >= self._config.stale_grace_s:
            self._state = "off"
            self._last_stop_at = now
            self._on_since = None
            self._disarming_since = None
            self._last_requested_a = None
            return self._verdict("stop", None, "no_basis_stopped")
        return self._verdict("hold", None, "no_basis_grace")

    def _handle_off_or_arming(self, now: float, available_a: float) -> SolarVerdict:
        cfg = self._config
        was_arming = self._state == "arming"

        if available_a >= cfg.start_a:
            if not was_arming:
                self._arming_since = now
            self._state = "arming"
            assert self._arming_since is not None
            elapsed = now - self._arming_since
            min_off_ok = (
                self._last_stop_at is None or (now - self._last_stop_at) >= cfg.min_off_s
            )
            if elapsed >= cfg.start_delay_s and min_off_ok:
                self._state = "on"
                self._on_since = now
                self._arming_since = None
                requested = self._clamp_request(available_a)
                self._last_requested_a = requested
                return self._verdict("start", requested, "start_after_delay")
            reason: SolarReason = "arming_delay" if elapsed < cfg.start_delay_s else "arming_min_off_wait"
            return self._verdict("hold", None, reason)

        self._state = "off"
        self._arming_since = None
        return self._verdict("hold", None, "arming_dip" if was_arming else "off_no_surplus")

    def _handle_on_or_disarming(self, now: float, available_a: float) -> SolarVerdict:
        cfg = self._config
        was_on = self._state == "on"

        if available_a < cfg.stop_a:
            if was_on:
                self._disarming_since = now
            self._state = "disarming"
            assert self._disarming_since is not None
            assert self._on_since is not None
            elapsed = now - self._disarming_since
            min_on_ok = (now - self._on_since) >= cfg.min_on_s
            if elapsed >= cfg.stop_delay_s and min_on_ok:
                self._state = "off"
                self._last_stop_at = now
                self._on_since = None
                self._disarming_since = None
                self._last_requested_a = None
                return self._verdict("stop", None, "stop_after_delay")
            reason: SolarReason = (
                "disarming_delay" if elapsed < cfg.stop_delay_s else "disarming_min_on_wait"
            )
            return self._verdict("hold", None, reason)

        was_disarming = self._state == "disarming"
        self._state = "on"
        self._disarming_since = None
        requested = self._clamp_request(available_a)
        changed = requested != self._last_requested_a
        self._last_requested_a = requested
        if was_disarming:
            return self._verdict(
                "set_current" if changed else "hold",
                requested if changed else None,
                "disarming_recover",
            )
        if changed:
            return self._verdict("set_current", requested, "on_modulate")
        return self._verdict("hold", None, "on_steady")

    def _clamp_request(self, available_a: float) -> float:
        """Floor to whole amps, then clamp to the charger limits, in that order."""
        cfg = self._config
        floored = float(math.floor(available_a))
        return max(cfg.min_current_a, min(cfg.max_current_a, floored))

    def _verdict(
        self,
        action: SolarAction,
        requested_a: float | None,
        reason: SolarReason,
    ) -> SolarVerdict:
        return SolarVerdict(
            action=action,
            requested_a=requested_a,
            reason=reason,
            state=self._state,
            net_grid_w=self._net_grid_w,
            export_w=self._export_w,
            car_w=self._car_w,
            battery_w=self._battery_w,
            available_w=self._available_w,
            available_a=self._available_a,
            priority_effective=self._priority_effective,
        )
