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

**The lowest step is the charger's, not the 6 A pilot floor.** An AC charger's 6 A floor (4.2 kW at 400 V) is the
car's pilot minimum, which a DC charger does not have: its lowest step is the lowest power it holds
(`EVDC_MIN_POWER_KW`) as a current per phase (`PowerCurrent.min_a`, 1.5 kW / 0.6928 kW per ampere = 2.17 A at 400 V,
rounded up to the hundredth so the power written is never below it). That current is the charger's floor wherever
SpotNav reckons one: the sun's start, hold and stop, load balancing's lowest step before it pauses, the import
target's hold (`ChargingController.min_current_a`, `SiteCapacityController.charger_min_current_a`). It is the one
current that need not be a whole ampere: above it the steps stay whole amperes (`util.amps_at_floor`). Phase
switching, the car's onboard phases and a per-phase cut do not exist for a DC charger: the power is spread over three
phases whatever car is plugged in (`planning/phases.py`).

**Three phases only.** `sqrt(3) x V x I` is the power of a three-phase draw. On one phase the same limit would draw
three times the current the plan counts, so a charger wired as one phase is never written (`PowerCurrent.async_set`
refuses it, and the site's wiring step does not take one phase for a DC charger).

**What the limit may be.** The charger's rating, from its rated-power sensor when that reads a plausible power
(`RATED_KW_RANGE`), else the last plausible one it read (kept across restarts, `RatingMemory`), else nothing above
the limit the charger already carries. On a site, never above the fuse either (`evdc_fuse_cap_a`).

**Conservative until the tester has answered** (`docs/setup.md`, Sigenergy DC charger): a pause presses Stop (the
controller's pause; whether a 0 kW limit pauses it is not known), no limit is written faster than every 10 s (the
policy), a raise waits `EVDC_SETTLE_S` after the last write so the charger has followed it, and every limit write,
Start and Stop is kept with the output power read after it (`PowerTiming`), which the diagnostics show.
"""

from __future__ import annotations

import logging
import math
from collections import deque
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any, Final

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import callback, HassJob, HomeAssistant, State
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from ...const import CURRENT_CONTROL_POWER, DEFAULT_VOLTAGE_BETWEEN_PHASES_V
from ...util import amps_at_floor
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
    ASSIGN_UNSUPPORTED,
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

_LOGGER = logging.getLogger(__name__)

#: The lowest power the charger is taken to hold, in kW (Sigenergy's own surplus mode charges down to about it): a
#: limit below it is never written, and it is the charger's floor as a current per phase (`PowerCurrent.min_a`). TO BE
#: CONFIRMED by the tester: it is load-bearing (the sun starts and load balancing holds a charge at it), but what the
#: charger really holds, and what the grid draws at it, has not been measured on site.
EVDC_MIN_POWER_KW: Final = 1.5
#: What a start capped by the site's allowance must leave above the floor, in amperes per phase: a start at exactly the
#: allowance would overshoot it if the real grid draw at a 1.5 kW limit is a little higher than the limit (the
#: inverter's losses, not yet measured). Only for a floor below 6 A (`ChargingController._start_command_locked`).
EVDC_START_MARGIN_A: Final = 0.5
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
#: What a limit read back may lie under the power it was written from (the write is rounded down to the number's
#: step): at least 0.01 kW, the number's own step when larger, never as much as one ampere.
READ_BACK_SLACK_KW: Final = 0.01
#: A rating outside this range (kW) is not the charger's (a sensor in W labelled kW, a 0 before it has read).
RATED_KW_RANGE: Final = (1.0, 400.0)
#: How long after a write the limit SpotNav wrote stands for the number's state, when the integration has not yet
#: refreshed it (Sigenergy ESS reads the register back on its next poll, not during the write).
PENDING_WRITE_S: Final = 15.0
#: Where the last plausible rating of each DC charger is kept, by its power number.
RATING_STORE_KEY: Final = "spotnav.evdc_ratings"
RATING_STORE_VERSION: Final = 1
#: Whether a site whose plant's own import limit guards the fuse now (`BatteryControl.import_limit_guards`) lifts the
#: fuse cap (`evdc_fuse_cap_a`). Off until an on-site test shows that the limit (40040) curtails the DC charger when
#: the battery reaches its floor: seen so far is only that it cuts the battery's own grid charge.
EVDC_CAP_LIFTED_BY_IMPORT_LIMIT: Final = False


def evdc_fuse_cap_a(hass: HomeAssistant, charger_entry_id: str | None) -> int | None:
    """The most a DC charger on a site is given, in amperes per phase: the main fuse less the safety margin
    (`site_limit_a`), or `None` (no cap) for a charger in no site or on a site whose battery import limit is confirmed.

    Load balancing measures the room net of the battery and the sun, so on a site with a home battery it can let a
    DC charger's limit sit far above the fuse (25 kW on a 20 A fuse) while the battery carries the car. When the
    battery reaches its floor all of that lands on the grid at once, faster than the regulator lowers it. Capped
    at the fuse, the grid carries at most what the fuse does. The cap could be lifted where the plant's own grid
    import limit guards the fuse (`EVDC_CAP_LIFTED_BY_IMPORT_LIMIT`, off until that is shown on site for this
    charger): then only while that limit holds **now** by the battery gate's own rule (`gate.import_limit_holds`
    through `BatteryControl.import_limit_guards`: the person said it acts, it reads at most what keeps the worst
    phase under the fuse by the same fuse, margin and learned peaks, counting the other chargers' last currents and
    never this charger's own lifted one), never on the person's word alone, and not when any of it cannot be read.
    """
    from ...battery.gate import Options  # noqa: PLC0415 - the battery package imports the execution
    from ...planning.first_run import site_for_charger, site_limit_a  # noqa: PLC0415 - the planning imports execution

    if not charger_entry_id:
        return None
    site = site_for_charger(hass, charger_entry_id)
    if site is None:
        return None
    if EVDC_CAP_LIFTED_BY_IMPORT_LIMIT and Options.from_config(site.data).import_limit_acts:
        from ...runtime import site_controller_for  # noqa: PLC0415 - the runtime imports the execution

        controller = site_controller_for(hass, site.entry_id)
        control = None if controller is None else getattr(controller, "battery_control", None)
        try:
            if control is not None and control.import_limit_guards(without_charger=charger_entry_id):
                return None
        except Exception:  # noqa: BLE001 - fail closed: the cap stays
            _LOGGER.debug("evdc_fuse_cap_a: ignored failure (fail closed: the cap stays)", exc_info=True)
            pass
    limit = site_limit_a(site)
    return None if limit is None else max(0, limit)


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


_LOOKUPS: Final = "spotnav_evdc_lookups"


def _lookups(hass: HomeAssistant) -> dict[tuple[str, str], str]:
    return hass.data.setdefault(_LOOKUPS, {})


def charger_entry_id(hass: HomeAssistant, charge_control: str | None) -> str | None:
    """The SpotNav charger entry whose charge control this is, or `None` (kept once found, checked on each use)."""
    from ...const import CONF_CHARGE_CONTROL, DOMAIN  # noqa: PLC0415 - kept beside its only use

    if not charge_control:
        return None
    cache = _lookups(hass)
    cached = cache.get(("entry", charge_control))
    if cached is not None:
        entry = hass.config_entries.async_get_entry(cached)
        if entry is not None and entry.data.get(CONF_CHARGE_CONTROL) == charge_control:
            return cached
        cache.pop(("entry", charge_control), None)
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.data.get(CONF_CHARGE_CONTROL) == charge_control:
            cache[("entry", charge_control)] = entry.entry_id
            return entry.entry_id
    return None


def charger_voltage_v(hass: HomeAssistant, charge_control: str | None) -> float:
    """The voltage between phases of the SpotNav charger whose charge control this is: its site's, else its own."""
    from ...planning.grid_voltage import voltage_between_phases_v  # noqa: PLC0415 - the planning imports execution

    entry_id = charger_entry_id(hass, charge_control)
    return float(DEFAULT_VOLTAGE_BETWEEN_PHASES_V) if entry_id is None else voltage_between_phases_v(hass, entry_id)


def charger_sensor(hass: HomeAssistant, number_entity_id: str | None, keys_of: str) -> str | None:
    """The sensor of the charger's own device that the profile names under `keys_of` (`output_power_keys` or
    `rated_power_keys`), found from the power number's device; `None` without one. A sensor found is kept and only
    looked up again once it is gone from the registry or disabled."""
    if not number_entity_id:
        return None
    registry = er.async_get(hass)
    cache = _lookups(hass)
    key = (number_entity_id, keys_of)
    cached = cache.get(key)
    if cached is not None:
        cached_entry = registry.async_get(cached)
        if cached_entry is not None and cached_entry.disabled_by is None:
            return cached
        cache.pop(key, None)
    entry = registry.async_get(number_entity_id)
    profile = profile_for(entry.platform if entry is not None else None)
    keys = getattr(profile, keys_of, ()) if profile is not None else ()
    if entry is None or not keys:
        return None
    found = device_entities(hass, entry.platform, "sensor", keys, number_entity_id)
    if not found:
        return None
    cache[key] = found[0]
    return found[0]


class RatingMemory:
    """The last plausible rating (kW) read from each DC charger's rated-power sensor, by its power number, kept across
    restarts: when the sensor is unavailable, reads 0 or is disabled, the charger is still never given more."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._store: Store[dict[str, Any]] = Store(hass, RATING_STORE_VERSION, RATING_STORE_KEY)
        self._ratings: dict[str, float] = {}
        self._loading = False

    def ensure_loaded(self, hass: HomeAssistant) -> None:
        if self._loading:
            return
        self._loading = True
        hass.async_create_task(self._async_load(), "spotnav evdc ratings", eager_start=True)

    async def _async_load(self) -> None:
        data = await self._store.async_load()
        ratings = data.get("ratings") if isinstance(data, dict) else None
        for entity_id, value in (ratings or {}).items() if isinstance(ratings, dict) else ():
            kw = _finite(value)
            if isinstance(entity_id, str) and kw is not None and _plausible_rating(kw):
                # A rating read since the start is newer than the stored one.
                self._ratings.setdefault(entity_id, kw)

    def get(self, entity_id: str) -> float | None:
        return self._ratings.get(entity_id)

    def remember(self, entity_id: str, kw: float) -> None:
        known = self._ratings.get(entity_id)
        if known is not None and abs(known - kw) < 1e-3:
            return
        self._ratings[entity_id] = kw
        self._store.async_delay_save(lambda: {"ratings": dict(self._ratings)}, 5.0)


def rating_memory(hass: HomeAssistant) -> RatingMemory:
    memory = hass.data.get(RATING_STORE_KEY)
    if not isinstance(memory, RatingMemory):
        memory = hass.data[RATING_STORE_KEY] = RatingMemory(hass)
    memory.ensure_loaded(hass)
    return memory


def _plausible_rating(kw: float) -> bool:
    return RATED_KW_RANGE[0] <= kw <= RATED_KW_RANGE[1]


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

    Refuses, in this order: below its floor (`min_a`), a charger not wired as three phases, a number that is gone or has
    no power unit, no ceiling known (`ceiling_kw`), below `EVDC_MIN_POWER_KW` (also once rounded down to the number's
    step or held to its `max`), a value it already carries, a raise within
    `EVDC_SETTLE_S` of the last write (the regulator's and the sun's only), and the policy's interval not yet elapsed.

    The number's state lags a write (the integration reads the register back on its next poll), so for
    `PENDING_WRITE_S` after a write, until the state shows it, the value written stands for what the charger carries
    (`_carried_native`): a lowering right after a raise is never taken for "unchanged".
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
        three_phase: Callable[[], bool] = lambda: True,
        fuse_cap_a: Callable[[], int | None] = lambda: None,
        ratings: RatingMemory | None = None,
        timing: PowerTiming | None = None,
        now: Callable[[], datetime] = dt_util.utcnow,
    ) -> None:
        super().__init__(policy)
        self.hass = hass
        self.entity_id = entity_id
        self._limiter = limiter or WriteRateLimiter(policy, now)
        self._voltage_v = voltage_v
        self._rated_entity_id = rated_entity_id
        self._three_phase = three_phase
        self._fuse_cap_a = fuse_cap_a
        self._ratings = ratings
        self.timing = timing
        self._now = now
        #: The native value last written and when, until the number's state shows it (`_carried_native`).
        self._pending: tuple[float, datetime] | None = None
        #: How many must-lowers the write interval refused, so the charge was stopped instead (the controller counts).
        self.interval_stops = 0
        self._single_phase_logged = False

    # -- the conversion

    def kw_per_a(self) -> float:
        return kw_per_amp(self._voltage_v())

    def min_a(self) -> float:
        """The charger's floor as a current per phase: `EVDC_MIN_POWER_KW` at `kw_per_a`, rounded up to the hundredth
        of an ampere so the power it is written as is never below the lowest power (2.17 A, 1.503 kW, at 400 V)."""
        return math.ceil(EVDC_MIN_POWER_KW / self.kw_per_a() * 100.0 - 1e-6) / 100.0

    def sensor_rated_kw(self) -> float | None:
        """The rated-power sensor's reading (kW) when it is a plausible rating, else `None`."""
        rated_id = self._rated_entity_id()
        rated = power_kw(self.hass.states.get(rated_id)) if rated_id else None
        return rated if rated is not None and _plausible_rating(rated) else None

    def rated_kw(self) -> float | None:
        """The charger's rating (kW): its rated-power sensor when plausible (then remembered), else the last plausible
        one remembered; `None` when neither. The number's own `max` (100 kW) is never taken for it."""
        rated = self.sensor_rated_kw()
        if rated is not None:
            if self._ratings is not None:
                self._ratings.remember(self.entity_id, rated)
            return rated
        return None if self._ratings is None else self._ratings.get(self.entity_id)

    def fuse_cap_kw(self) -> float | None:
        cap_a = self._fuse_cap_a()
        return None if cap_a is None else cap_a * self.kw_per_a()

    def ceiling_kw(self) -> float | None:
        """The most the limit may be (kW): the rating, else the limit the charger carries now (never raised without a
        rating), and on a site the fuse cap below either. `None` when nothing says."""
        rated = self.rated_kw()
        bound = rated if rated is not None else self.limit_kw()
        cap = self.fuse_cap_kw()
        values = [value for value in (bound, cap) if value is not None]
        return min(values) if values else None

    def max_a(self) -> int | None:
        """The current per phase the ceiling is, rounded down; `None` without one."""
        ceiling = self.ceiling_kw()
        if ceiling is None:
            return None
        return int(math.floor(ceiling / self.kw_per_a() + 1e-6))

    def kw_for(self, amps: float) -> float | None:
        """The power `amps` per phase is written as: the ceiling at its current or above, else the three-phase power;
        `None` when there is no ceiling."""
        ceiling = self.ceiling_kw()
        if ceiling is None:
            return None
        kw = amps * self.kw_per_a()
        maximum = self.max_a()
        if kw > ceiling or (maximum is not None and amps >= maximum):
            return ceiling
        return kw

    def _slack_kw(self, state: State | None) -> float:
        """What a limit read back may lie under the power written (the number's step, at least 0.01 kW, under one
        ampere's worth)."""
        factor = _native_factor(state)
        step = _finite(state.attributes.get("step")) if state is not None else None
        step_kw = step / factor if step is not None and step > 0 and factor else 0.0
        return min(max(READ_BACK_SLACK_KW, step_kw) + 1e-6, 0.9 * self.kw_per_a())

    def _amps_of(self, kw: float) -> float:
        """The current per phase a power limit is: rounded down to a whole ampere, after allowing for the limit's own
        step (a write is rounded down to it, so 6 A written as 4.15 kW reads back as 6 A, not 5), or the floor
        (`min_a`) for a limit between the floor and the next whole ampere (the floor written as 1.50 kW reads back
        as 2.17 A, not 2)."""
        maximum = self.max_a()
        ceiling = self.ceiling_kw()
        if maximum is not None and ceiling is not None and kw >= ceiling - 1e-6:
            return maximum
        amps = (kw + self._slack_kw(self.hass.states.get(self.entity_id))) / self.kw_per_a()
        return amps_at_floor(amps, self.min_a())

    def limit_kw(self) -> float | None:
        """The limit the number's state shows (kW)."""
        state = self.hass.states.get(self.entity_id)
        factor = _native_factor(state)
        value = _finite(state.state) if state is not None else None
        if factor is None or value is None:
            return None
        return value / factor

    def _carried_native(self, state: State | None) -> float | None:
        """What the charger carries, in the number's units: the value last written while the state has not shown it
        yet (within `PENDING_WRITE_S`), else the state."""
        present = _finite(state.state) if state is not None else None
        pending = self._pending
        if pending is None:
            return present
        native, at = pending
        step = _finite(state.attributes.get("step")) if state is not None else None
        if present is not None and abs(present - native) < max(1e-6, (step or 0.0) / 2):
            self._pending = None
            return present
        if (self._now() - at).total_seconds() < PENDING_WRITE_S:
            return native
        self._pending = None
        return present

    # -- `CurrentPath`

    def setpoint_a(self) -> float | None:
        state = self.hass.states.get(self.entity_id)
        factor = _native_factor(state)
        carried = self._carried_native(state)
        if factor is None or carried is None:
            return self.last_written_a
        return self._amps_of(carried / factor)

    def read_back_a(self) -> float | None:
        limit = self.limit_kw()
        return None if limit is None else self._amps_of(limit)

    def ready(self) -> bool:
        return _entity_ready(self.hass, self.entity_id)

    def wait_s(self) -> float:
        return self._limiter.wait_s()

    def note_interval_stop(self) -> None:
        self.interval_stops += 1

    async def async_set(self, amps: float, *, reason: str, verify: bool = False) -> str:
        if amps < self.min_a() - 1e-9:
            return ASSIGN_BELOW_MINIMUM
        if not self._three_phase():
            # sqrt(3) x V x I on one phase is three times the current the plan counts: never written.
            if not self._single_phase_logged:
                self._single_phase_logged = True
                _LOGGER.warning(
                    "SpotNav: the DC charger's power limit %s is not written: the charger is wired as one phase, and "
                    "a DC charger is driven only on three",
                    self.entity_id,
                )
            return ASSIGN_UNSUPPORTED
        self._single_phase_logged = False
        state = self.hass.states.get(self.entity_id)
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return ASSIGN_TARGET_UNAVAILABLE
        factor = _native_factor(state)
        if factor is None:
            return ASSIGN_UNIT_UNKNOWN
        kw = self.kw_for(amps)
        if kw is None:
            return ASSIGN_TARGET_UNAVAILABLE
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
        if native / factor < EVDC_MIN_POWER_KW - 1e-9:
            # Rounded down to the number's step, or held to its `max`, it would be below the lowest power: never
            # written.
            return ASSIGN_BELOW_MINIMUM
        present = self._carried_native(state)
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
            _LOGGER.debug("async_set: ignored failure (the outcome code says it; the regulator decides what that means)", exc_info=True)
            return ASSIGN_WRITE_FAILED
        self.last_written_a = amps
        self._pending = (float(native), self._now())
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
        cap = self._fuse_cap_a()
        return {
            "limit_kw": None if limit is None else round(limit, 3),
            "rated_kw": None if rated is None else round(rated, 3),
            "rated_from_sensor": self.sensor_rated_kw() is not None,
            "fuse_cap_a": cap,
            "three_phase": self._three_phase(),
            "kw_per_a": round(self.kw_per_a(), 4),
            "max_a": self.max_a(),
            "min_power_kw": EVDC_MIN_POWER_KW,
            "min_a": self.min_a(),
            "settle_s": EVDC_SETTLE_S,
            "interval_stops": self.interval_stops,
            "output_kw": None if self.timing is None else PowerTiming._rounded(self.timing.output_kw()),
            "timing": [] if self.timing is None else self.timing.records(),
        }


def _power_current(context: ChargerContext) -> CurrentPath | None:
    from ...planning.phases import charger_wiring  # noqa: PLC0415 - the planning imports execution

    number = context.current_limit
    if not number:
        return None
    hass = context.hass

    def output() -> str | None:
        return charger_sensor(hass, number, "output_power_keys")

    def entry_id() -> str | None:
        return charger_entry_id(hass, context.charge_control)

    def three_phase() -> bool:
        # Guard: a charger wired as one phase is never given a power (`PowerCurrent.async_set`). No entry or no
        # wiring stated is three phases, as everywhere else.
        charger = entry_id()
        return charger is None or (charger_wiring(hass, charger) or 3) == 3

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
        three_phase=three_phase,
        fuse_cap_a=lambda: evdc_fuse_cap_a(hass, entry_id()),
        ratings=rating_memory(hass),
        timing=timing,
        now=context.now,
    )


def _status_text(context: ChargerContext) -> str | None:
    try:
        return context.status()
    except KeyError:  # the adapter is not built yet
        return None


register_current(CURRENT_CONTROL_POWER, _power_current)
