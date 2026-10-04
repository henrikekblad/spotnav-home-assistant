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

A charger whose own measured current is missing (not configured, or unreadable) while the grid and the
battery read fine runs blind, at its minimum current only: what the car draws is unknown, so no surplus
can be sized beyond the minimum. It starts (at the start minimum, then held at the minimum current) only
when the caller allows it (`SolarObservation.unmeasured_start_allowed`: no other charger on the site is
about to start or has just started, so two chargers never claim the same export) and the spare power
without the car (`_spare_w`: export, plus a charging battery under `car_first`) covers the start minimum
for `start_delay_s`, after `min_off_s`, and within the fuse caps. A running charge is never stopped
merely for the missing reading (a stop would be followed by whatever resumes the charger, round and
round); it stays at the minimum current while the spare power is not below `-missing_import_tolerance_w`
per phase the car uses (a battery discharging into the car counts as import), and import beyond that for
`stop_delay_s` and `min_on_s` stops it as a normal solar stop. Solar never charges from the grid on the
strength of a fault, and never asks for more than the minimum current without the car's own reading.

Several chargers on one site share the surplus in the site's charger order (priority First, Normal,
Last; ties by the order they joined, as capacity allocation serves them): `share_surplus` offers the
whole surplus to the first charger, and a later one gets only what the earlier ones cannot use (too
little for their minimum, or more than they take). Each charger's controller still runs on its own;
the split reaches it as `SolarObservation.share_adjust_w`, added to its own reckoning.

A start is made at the start minimum (`start_a`), never at what the surplus seems to allow, and held
there for `verify_s` while the car's draw shows up in the readings; only then does modulation step it up.
A start that counted a charging battery under `car_first` is checked in that time: a battery that only
took the sun's power until the car drew, and then turns to feed the car, leaves the surplus below `stop_a`
with the grid near zero. That credit was false: the charge is stopped at once (`battery_credit_false`)
and a charging battery is not counted again for `credit_backoff_s`, doubling with each false credit up to
`credit_backoff_max_s`, and back to `credit_backoff_s` after a credit that held. A discharging battery is
never surplus under either priority.

The charger's own state is the caller's to watch: a charge solar runs that ended without solar (the
charge control went off, or the car drew nothing for a while) is handed in through `charge_ended`. A car
that ended it by itself (`vehicle_full`, `car_stopped`) is not started again at once: a stopped car is
tried again after `ended_retry_s`, doubling with each charge it ends up to `ended_retry_max_s`, and a car at
its own limit not at all, until the caller clears it (`clear_ended`: a re-plug, a lower state of charge, a
higher limit or target). Anything else that ended it (`charger_stopped`) is an ordinary stop.

Freshness: a `None` where a reading is needed means no basis this tick. From
`off` or `arming` that means never start; a running charge is kept for
`stale_grace_s`. Stale ticks neither advance nor reset the timers.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal, Mapping, Sequence

from .site_capacity import PhaseName, PHASES, charger_order_key


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
    # The charger's own measured current is unusable while the grid and battery readings are fine:
    # nothing to size the surplus against, so no charge starts, and a running one is held at the minimum
    # current while the grid shows no real import and stopped (once) when it does.
    "charger_measurement_missing",
    # The same, while a blind start at the minimum current is armed (spare power covers the start
    # minimum) or carried out.
    "unmeasured_arming",
    "unmeasured_start",
    "off_no_surplus",
    "arming_delay",
    "arming_min_off_wait",
    # Dipped below `start_a` while arming: back to `off`, delay restarts.
    "arming_dip",
    "start_after_delay",
    # Just started at the start minimum: held there while the car's draw shows the surplus is real.
    "start_verifying",
    # The surplus a charging battery was credited with vanished once the car drew: stopped at once.
    "battery_credit_false",
    "on_steady",
    "on_modulate",
    # Surplus below `stop_a` but `stop_delay_s` not yet elapsed (a passing cloud).
    "disarming_delay",
    "disarming_min_on_wait",
    # Recovered above `stop_a`: back to `on`.
    "disarming_recover",
    "stop_after_delay",
    # The charge ended without solar (`SolarController.charge_ended`): the car stopped drawing by itself
    # at its own limit, or short of it (tried again after the back-off), or something else ended it.
    "vehicle_full",
    "car_stopped",
    "charger_stopped",
]

# Why a charge solar ran ended without solar (`SolarController.charge_ended`).
SolarEndCause = Literal["vehicle_full", "car_stopped", "charger_stopped"]


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
    # While the charger's own current is missing: the import, per phase the car uses, that still counts
    # as the sun covering the minimum-current charge.
    missing_import_tolerance_w: float = 100.0
    # After a start: held at `start_a` this long, while the car's draw shows whether the surplus is real.
    verify_s: float = 120.0
    # A false battery credit: how long a charging battery is not counted, doubling up to the maximum.
    credit_backoff_s: float = 600.0
    credit_backoff_max_s: float = 14400.0
    # A car that stopped drawing by itself short of its limit: how long until it is tried again, doubling
    # with each charge it ends, up to the maximum.
    ended_retry_s: float = 1800.0
    ended_retry_max_s: float = 14400.0

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
    # What the site's priority order moves to or from this charger, in watts, on top of its own
    # reckoning (`priority_adjust_w`); 0.0 for a charger alone on its site.
    share_adjust_w: float = 0.0
    # Whether a charger with no reading of its own current may start blind, at the minimum current: the
    # caller's judgement that no other charger on the site is about to start or has only just started.
    unmeasured_start_allowed: bool = False


@dataclass(frozen=True, slots=True)
class SurplusBreakdown:
    """One observation's energy balance (module docstring), before the priority split and any fuse
    cap. `available_w` already counts the charger's own draw `car_w`."""

    net_grid_w: float
    export_w: float
    car_w: float
    battery_w: float | None
    available_w: float
    mean_voltage_v: float
    priority_effective: SolarPriority


def surplus_breakdown(observation: SolarObservation, priority: SolarPriority) -> SurplusBreakdown | None:
    """The surplus one observation shows this charger, or `None` if a needed reading is unusable."""
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
    car_w = sum(delivered[phase] * voltage[phase] for phase in car_phases)  # type: ignore[misc]

    battery_w = observation.battery_w
    battery_w_for_formula = 0.0 if battery_w is None else battery_w
    priority_effective: SolarPriority
    if priority == "car_first" and battery_w is not None:
        # The energy-balance identity (module docstring).
        available_w = car_w + battery_w_for_formula - net_grid_w
        priority_effective = "car_first"
    else:
        available_w = car_w + min(0.0, battery_w_for_formula) - net_grid_w
        priority_effective = "battery_first"

    mean_voltage = sum(voltage[phase] for phase in car_phases) / len(car_phases)  # type: ignore[misc]
    if mean_voltage <= 0:
        return None
    return SurplusBreakdown(
        net_grid_w=net_grid_w,
        export_w=max(0.0, -net_grid_w),
        car_w=car_w,
        battery_w=battery_w,
        available_w=available_w,
        mean_voltage_v=mean_voltage,
        priority_effective=priority_effective,
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class SolarShareMember:
    """One charger taking part in its site's surplus split this tick: on `solar` (or `hybrid` outside
    a plan window and short of its target), not paused, and not known to be unplugged. A charger the
    split leaves out is house load to the others, as before the split existed.
    """

    charger_entry_id: str
    # "first", "normal" or "last" (`const.CHARGER_PRIORITIES`) and the place in the site's charger list.
    priority: str
    order: int
    # What the charger draws now, and the watts one amp is to it (its phases x their mean voltage).
    car_w: float
    watts_per_a: float
    # Its solar controller has it charging (`on` or `disarming`).
    running: bool
    start_a: float
    stop_a: float
    min_current_a: float
    max_current_a: float
    # Running, but the car takes clearly less than it was asked for (full, held back by the car, or a
    # charger whose current is not written): it uses what it draws, and the rest goes on down.
    takes_less: bool = False

    def uses_w(self, offered_w: float) -> float:
        """How much of `offered_w` this charger uses, leaving the rest to the chargers after it."""
        if self.takes_less:
            return self.car_w
        if self.running:
            if offered_w < self.stop_a * self.watts_per_a:
                # On its way to a stop, it keeps drawing what it draws until it gets there.
                return self.car_w
            return min(max(offered_w, self.min_current_a * self.watts_per_a), self.max_current_a * self.watts_per_a)
        if offered_w < self.start_a * self.watts_per_a:
            # Too little to start on: it cannot use it.
            return 0.0
        return min(offered_w, self.max_current_a * self.watts_per_a)


def share_surplus(pool_w: float, members: Sequence[SolarShareMember]) -> dict[str, float]:
    """The surplus each member is offered, in watts: the whole `pool_w` to the first in the site's
    order (`charger_order_key`), and to each later one what is left after the earlier ones took what
    they use (`SolarShareMember.uses_w`).
    """
    remaining = pool_w
    offered: dict[str, float] = {}
    for member in sorted(
        members, key=lambda m: charger_order_key(m.priority, m.order, m.charger_entry_id)
    ):
        offered[member.charger_entry_id] = remaining
        remaining -= member.uses_w(remaining)
    return offered


def priority_adjust_w(
    charger_entry_id: str, available_w: float, members: Sequence[SolarShareMember]
) -> float:
    """`SolarObservation.share_adjust_w` for one member: what the split offers it minus what it
    reckons alone (`available_w`, from `surplus_breakdown`, which already counts its own draw and sees
    every other charger's draw as house load). The site's whole surplus is that plus what the other
    members draw. Zero when it is the only member, or not a member at all.
    """
    if not any(member.charger_entry_id == charger_entry_id for member in members):
        return 0.0
    pool_w = available_w + sum(
        member.car_w for member in members if member.charger_entry_id != charger_entry_id
    )
    return share_surplus(pool_w, members)[charger_entry_id] - available_w


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
        self._import_since: float | None = None

        # A start's verification (module docstring): until when it is held at the start minimum, and
        # whether it counted a charging battery. Then the battery-credit back-off: until when a charging
        # battery is not counted, and how long the next back-off is.
        self._verify_until: float | None = None
        self._credited_start = False
        self._credit_backoff_until: float | None = None
        self._next_backoff_s: float = config.credit_backoff_s
        # A charge the car ended by itself (module docstring): why, until when no new one starts (`None`
        # for a car at its own limit: until cleared), and how long the next wait is.
        self._ended: SolarEndCause | None = None
        self._retry_at: float | None = None
        self._next_retry_s: float = config.ended_retry_s

        self._net_grid_w: float | None = None
        self._export_w: float | None = None
        self._car_w: float | None = None
        self._battery_w: float | None = None
        self._available_w: float | None = None
        self._available_a: float | None = None
        self._priority_effective: SolarPriority | None = None

    @property
    def config(self) -> SolarConfig:
        return self._config

    @property
    def running(self) -> bool:
        """Whether this controller has the charger charging (`on` or `disarming`)."""
        return self._state in ("on", "disarming")

    @property
    def on_since(self) -> float | None:
        return self._on_since

    @property
    def last_requested_a(self) -> float | None:
        return self._last_requested_a

    @property
    def ended(self) -> SolarEndCause | None:
        """Why the car ended the last charge by itself (`vehicle_full`, `car_stopped`), until cleared."""
        return self._ended

    def retry_in(self, now: float) -> float | None:
        """The seconds until a stopped car may be started again; `None` when nothing waits for a retry."""
        if self._ended != "car_stopped" or self._retry_at is None or self._retry_at <= now:
            return None
        return self._retry_at - now

    def charge_ended(self, now: float, cause: SolarEndCause) -> SolarVerdict:
        """The charge solar ran ended without solar: `off` from `now`, with a stop the caller carries out
        (the charge control may still be on, and a stop also forgets who started it). A car that ended it
        by itself waits for its retry (module docstring); `charger_stopped` is an ordinary stop."""
        cfg = self._config
        self._state = "off"
        self._last_stop_at = now
        self._on_since = None
        self._arming_since = None
        self._disarming_since = None
        self._last_requested_a = None
        self._verify_until = None
        self._credited_start = False
        self._import_since = None
        if cause == "charger_stopped":
            self._ended = None
            self._retry_at = None
        else:
            self._ended = cause
            if cause == "vehicle_full":
                self._retry_at = None
            else:
                self._retry_at = now + self._next_retry_s
                self._next_retry_s = min(self._next_retry_s * 2.0, cfg.ended_retry_max_s)
        return self._verdict("stop", None, cause)

    def car_drew(self) -> None:
        """The car took a charge again: the next one it ends waits the shortest retry again."""
        self._ended = None
        self._retry_at = None
        self._next_retry_s = self._config.ended_retry_s

    def clear_ended(self) -> None:
        """Start afresh after a charge the car ended (a re-plug, a lower state of charge, a higher limit or
        target): no wait, and the shortest retry next time."""
        self.car_drew()

    def _ended_blocks(self, now: float) -> bool:
        """Whether a charge the car ended still keeps a new one from starting."""
        if self._ended is None or self._state not in ("off", "arming"):
            return False
        return self._retry_at is None or now < self._retry_at

    def priority_now(self, now: float) -> SolarPriority:
        """The priority the surplus is reckoned with now: `battery_first` while a false battery credit
        keeps a charging battery from counting (module docstring), else the configured one."""
        if self._credit_backoff_until is not None and now < self._credit_backoff_until:
            return "battery_first"
        return self._config.priority

    def credit_backoff(self, now: float) -> tuple[float | None, float]:
        """The battery-credit back-off as the caller keeps it: the seconds it still runs (`None` when it
        does not) and the next back-off's length."""
        until = self._credit_backoff_until
        remaining = None if until is None or until <= now else until - now
        return remaining, self._next_backoff_s

    def seed_credit_backoff(self, now: float, remaining_s: float | None, next_s: float | None) -> None:
        """Restore a back-off the caller kept (a restart, a rebuilt controller): `remaining_s` more seconds
        of it, and the next one's length (`None` for the configured start)."""
        self._credit_backoff_until = None if remaining_s is None or remaining_s <= 0 else now + remaining_s
        if next_s is not None and next_s > 0:
            self._next_backoff_s = min(next_s, self._config.credit_backoff_max_s)

    def ended_backoff(self, now: float) -> tuple[SolarEndCause | None, float | None, float]:
        """A charge the car ended as the caller keeps it: why (`None` when nothing waits), the seconds until
        a stopped car is tried again (`None` when no retry is pending) and the next retry's length."""
        return self._ended, self.retry_in(now), self._next_retry_s

    def seed_ended_backoff(
        self, now: float, cause: SolarEndCause | None, remaining_s: float | None, next_s: float | None
    ) -> None:
        """Restore a charge the car ended that the caller kept (a restart, a rebuilt controller): why,
        `remaining_s` more seconds before a stopped car is tried again (`None` or past: it may be now), and
        the next retry's length (`None` for the configured start). `charger_stopped` is no wait."""
        if next_s is not None and next_s > 0:
            self._next_retry_s = min(next_s, self._config.ended_retry_max_s)
        if cause not in ("vehicle_full", "car_stopped"):
            self._ended = None
            self._retry_at = None
            return
        self._ended = cause
        if cause == "vehicle_full":
            self._retry_at = None
        else:
            self._retry_at = now + remaining_s if remaining_s is not None and remaining_s > 0 else now

    def adopt(self, now: float, *, requested_a: float | None = None, on_since: float | None = None) -> None:
        """Take a charge that is already running as this controller's own: `on` from `now`, with no
        start verification (it was not started on a credited surplus). `on_since` dates the charge for
        `min_on_s` (a charge found mid-way, at a restart, counts from now; one that began by itself need
        not wait out a minimum it never had). `requested_a` is the current it is asked for now, if any.
        The last stop is kept, so `min_off_s` still holds after it."""
        self._state = "on"
        self._on_since = now if on_since is None else on_since
        self._arming_since = None
        self._disarming_since = None
        self._last_requested_a = requested_a
        self._stale_since = None
        self._verify_until = None
        self._credited_start = False

    def observe(self, observation: SolarObservation) -> SolarVerdict:
        """Update state from one observation and return its verdict."""
        now = observation.now

        if self._ended_blocks(now):
            # The car ended the last charge by itself: nothing starts until its retry, or until cleared.
            # The surplus is still reckoned for the diagnostics.
            self._refresh_breakdown(observation)
            self._state = "off"
            self._arming_since = None
            self._stale_since = None
            assert self._ended is not None
            return self._verdict("hold", None, self._ended)

        if self._charger_measurement_missing(observation):
            return self._handle_charger_measurement_missing(observation)

        available_a = self._refresh_breakdown(observation)
        if available_a is None:
            return self._handle_no_basis(now)

        self._stale_since = None
        if self._state in ("off", "arming"):
            return self._handle_off_or_arming(now, available_a)
        return self._handle_on_or_disarming(now, available_a)

    @staticmethod
    def _charger_measurement_missing(observation: SolarObservation) -> bool:
        """Only the charger's own current is unusable: the grid, the voltage and a configured battery
        all read. Anything else missing is an ordinary gap (`_handle_no_basis`)."""
        car_phases = observation.car_phases
        if not car_phases:
            return False
        if all(observation.car_delivered_a.get(phase) is not None for phase in car_phases):
            return False
        if any(observation.signed_grid_w.get(phase) is None for phase in PHASES):
            return False
        if any(observation.voltage_v.get(phase) is None for phase in car_phases):
            return False
        return not (observation.battery_configured and observation.battery_w is None)

    def _spare_w(self, observation: SolarObservation) -> float:
        """The power the site has to spare with the car's own draw not counted back in: export, plus a
        battery that is charging under `car_first`; a discharging battery counts against it under either
        priority. A running car's draw is already in the grid reading."""
        net_grid_w = sum(observation.signed_grid_w[phase] for phase in PHASES)  # type: ignore[misc]
        battery_w = observation.battery_w
        if battery_w is None:
            return -net_grid_w
        if self._config.priority == "car_first":
            return battery_w - net_grid_w
        return min(0.0, battery_w) - net_grid_w

    def _handle_charger_measurement_missing(self, observation: SolarObservation) -> SolarVerdict:
        """Blind, at the minimum current (module docstring): a start only when allowed and covered by
        spare power; a running charge held at the minimum while there is no real import, and stopped
        when import lasts."""
        now = observation.now
        self._stale_since = None
        cfg = self._config
        net_grid_w = sum(observation.signed_grid_w[phase] for phase in PHASES)  # type: ignore[misc]
        spare_w = self._spare_w(observation)
        self._net_grid_w = net_grid_w
        self._export_w = max(0.0, -net_grid_w)
        self._battery_w = observation.battery_w
        # Without the car's draw there is no surplus to state.
        self._car_w = None
        self._available_w = None
        self._available_a = None
        self._priority_effective = (
            "car_first" if cfg.priority == "car_first" and observation.battery_w is not None else "battery_first"
        )
        if self._state in ("off", "arming"):
            return self._unmeasured_off_or_arming(observation, spare_w)
        importing = spare_w < -cfg.missing_import_tolerance_w * len(observation.car_phases)
        if not importing:
            self._import_since = None
        else:
            if self._import_since is None:
                self._import_since = now
            on_since = self._on_since if self._on_since is not None else now
            if now - self._import_since >= cfg.stop_delay_s and now - on_since >= cfg.min_on_s:
                self._state = "off"
                self._last_stop_at = now
                self._on_since = None
                self._disarming_since = None
                self._import_since = None
                self._last_requested_a = None
                return self._verdict("stop", None, "charger_measurement_missing")
        # `disarming` is a countdown that this branch replaces with its own.
        self._state = "on"
        self._disarming_since = None
        minimum = cfg.min_current_a
        if self._last_requested_a != minimum:
            self._last_requested_a = minimum
            return self._verdict("set_current", minimum, "charger_measurement_missing")
        return self._verdict("hold", None, "charger_measurement_missing")

    def _unmeasured_off_or_arming(self, observation: SolarObservation, spare_w: float) -> SolarVerdict:
        """A blind start at the start minimum, once the spare power has covered it for `start_delay_s`
        (after `min_off_s`) and every fuse cap allows it; anything short of that is back to `off`."""
        now = observation.now
        cfg = self._config
        assert cfg.start_a is not None
        car_phases = observation.car_phases
        mean_voltage = sum(observation.voltage_v[phase] for phase in car_phases) / len(car_phases)  # type: ignore[misc]
        caps_allow = True
        if observation.phase_cap_a is not None:
            caps = [observation.phase_cap_a.get(phase) for phase in car_phases]
            caps_allow = all(cap is not None and cap >= cfg.start_a for cap in caps)
        covered = (
            observation.unmeasured_start_allowed
            and caps_allow
            and mean_voltage > 0
            and spare_w >= cfg.start_a * len(car_phases) * mean_voltage
        )
        self._import_since = None
        if not covered:
            self._state = "off"
            self._arming_since = None
            return self._verdict("hold", None, "charger_measurement_missing")
        if self._state != "arming":
            self._arming_since = now
        self._state = "arming"
        assert self._arming_since is not None
        min_off_ok = self._last_stop_at is None or (now - self._last_stop_at) >= cfg.min_off_s
        if now - self._arming_since >= cfg.start_delay_s and min_off_ok:
            self._state = "on"
            self._on_since = now
            self._arming_since = None
            self._last_requested_a = cfg.start_a
            return self._verdict("start", cfg.start_a, "unmeasured_start")
        return self._verdict("hold", None, "unmeasured_arming")

    def _refresh_breakdown(self, observation: SolarObservation) -> float | None:
        """Compute this tick's surplus breakdown and store it for `_verdict`.

        Returns `available_a`, or `None` if a needed reading is unusable; the
        stored breakdown is then left as it was.
        """
        breakdown = surplus_breakdown(observation, self.priority_now(observation.now))
        if breakdown is None:
            return None
        car_phases = observation.car_phases
        mean_voltage = breakdown.mean_voltage_v
        available_w = breakdown.available_w + observation.share_adjust_w
        available_a = available_w / (len(car_phases) * mean_voltage)
        if observation.phase_cap_a is not None:
            caps = [observation.phase_cap_a.get(phase) for phase in car_phases]
            if any(cap is None for cap in caps):
                return None
            cap_a = min(caps)  # type: ignore[type-var]
            if available_a > cap_a:
                available_a = cap_a
                available_w = cap_a * len(car_phases) * mean_voltage

        self._net_grid_w = breakdown.net_grid_w
        self._export_w = breakdown.export_w
        self._car_w = breakdown.car_w
        self._battery_w = breakdown.battery_w
        self._available_w = available_w
        self._available_a = available_a
        self._priority_effective = breakdown.priority_effective
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
                # At the start minimum, never at what the surplus seems to allow: the car's draw shows
                # within `verify_s` whether it is real (module docstring).
                assert cfg.start_a is not None
                requested = self._clamp_request(cfg.start_a)
                self._last_requested_a = requested
                self._verify_until = now + cfg.verify_s
                self._credited_start = self._battery_credited()
                return self._verdict("start", requested, "start_after_delay")
            reason: SolarReason = "arming_delay" if elapsed < cfg.start_delay_s else "arming_min_off_wait"
            return self._verdict("hold", None, reason)

        self._state = "off"
        self._arming_since = None
        return self._verdict("hold", None, "arming_dip" if was_arming else "off_no_surplus")

    def _battery_credited(self) -> bool:
        """Whether the surplus just reckoned needed a charging battery under `car_first` to reach the
        start minimum: without that battery's charge it would not have started."""
        cfg = self._config
        battery_w = self._battery_w
        if self._priority_effective != "car_first" or battery_w is None or battery_w <= 0:
            return False
        if self._available_w is None or self._available_a is None or self._available_w <= 0:
            return False
        assert cfg.start_a is not None
        watts_per_a = self._available_w / self._available_a
        return (self._available_w - battery_w) / watts_per_a < cfg.start_a

    def _false_credit(self, now: float) -> SolarVerdict:
        """The surplus a charging battery was credited with vanished once the car drew: stop now, and
        count no charging battery for the back-off, which doubles for the next false credit."""
        cfg = self._config
        self._credit_backoff_until = now + self._next_backoff_s
        self._next_backoff_s = min(self._next_backoff_s * 2.0, cfg.credit_backoff_max_s)
        self._state = "off"
        self._last_stop_at = now
        self._on_since = None
        self._disarming_since = None
        self._last_requested_a = None
        self._verify_until = None
        self._credited_start = False
        return self._verdict("stop", None, "battery_credit_false")

    def _handle_on_or_disarming(self, now: float, available_a: float) -> SolarVerdict:
        cfg = self._config
        if self._verify_until is not None:
            if now < self._verify_until:
                if available_a < cfg.stop_a and self._credited_start:  # type: ignore[operator]
                    return self._false_credit(now)
                if available_a >= cfg.stop_a:  # type: ignore[operator]
                    # Held at the start minimum while the car's draw shows the surplus is real.
                    self._state = "on"
                    self._disarming_since = None
                    return self._verdict("hold", None, "start_verifying")
                # Short of the stop level without a battery credit: the ordinary stop rules decide.
            else:
                if self._credited_start and available_a >= cfg.stop_a:  # type: ignore[operator]
                    # The credit held: the next false one backs off from the start again.
                    self._next_backoff_s = cfg.credit_backoff_s
                self._verify_until = None
                self._credited_start = False
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
