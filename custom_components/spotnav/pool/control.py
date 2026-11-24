"""SpotNav setting the pool's circulation pump and heat pump, the spa and the sauna (spec `plans/v3_pool_spa_spec.md`
stages 9 to 14, the owner's decisions of 2026-11-23: low-risk loads, only the hard rules that protect the hardware and
the water). Owned by the site controller beside the pool and spa shadow (`pool/runtime.py`), whose plan it follows;
kept in its own store (`spotnav.pool_spa_control.<entry>`).

**Behind a per-load opt-in** (`pool.control_circulation`, `pool.control_heat_pump`, `spa.control`, `sauna.control`,
administrator, one tap): without it nothing is written and the card says what SpotNav would do (`would`).

**The pool's hard rules** (`PoolPart`, pure constants in `pool/rules.py`):

1. **Circulation before the heat pump.** The heat pump is turned on only after the circulation has read on for
   `CIRC_LEAD_S` and, where a flow sensor is on the pump's device, it reads flow. The heat pump goes off first; the
   circulation goes off only once the heat pump has read off for `RUN_ON_MIN`. The circulation reading off (or no
   flow, or the pump's problem sensor on) while SpotNav runs the heat pump turns the heat pump off at once; a fault
   keeps it off for `FAULT_HOLD`. With only the heat pump's opt-in, the person's circulation decides when it may run.
2. **Season closed: handed back, then no writes at all.** What SpotNav still holds is handed back first, as at an
   opt-out (the heat pump before the circulation, the run-on respected, only where the device still reads SpotNav's
   value); a heat that would wait for the water to move is left off. After that nothing is written, and the person
   owns the pool until it is reopened.
3. **Minimum times and budgets**: the heat pump `HEAT_PUMP_MIN_ON_MIN`/`MIN_OFF`, at most `HEAT_PUMP_STARTS_DAY`
   starts in a day; the circulation `CIRCULATION_MIN_ON_MIN`/`MIN_OFF`, `CIRCULATION_STARTS_HOUR` and
   `CIRCULATION_STARTS_DAY`; the setpoint at most `SETPOINT_WRITES_DAY` writes a day, on/off `ON_OFF_WRITES_DAY`. A
   write that a hard rule needs (the interlock's off, the run-on's on) is never held back by them.
4. Only `heat` and `off` are written to a climate pump (and the dial's setpoint, once per change); a pump found in
   another mode (cool, auto) is watched until it reads `heat` or `off` again.

**Common to every load** (`loads/hold_part.py`): the person's values are the snapshot taken while SpotNav holds
nothing, written back at the opt-out, a pause, Home Assistant's stop and at the next start after a stop that could
not (only where SpotNav's value still reads); a change SpotNav did not make hands the load back to the next local
day; every write is read back within 90 s (300 s through a cloud) and a failure is counted, shown and pauses the load
to the next day; an enabled automation that steers an entity keeps SpotNav off it. A cloud route (core Tuya,
Aquatemp, WarmLink, Fairland, iAquaLink, SmartTub, MSpa, Bestway, Huum, Harvia) acts on the plan only: it is never a
live step of the shed order (`offers`).

**The shed order** (`site/shed_order.py`): a live step of the pool's heat pump, the circulation beyond the day's need
(taking the heat pump with it), the spa's idle heat, a booked bath or a sauna session turns that load off for the step;
each offers what it gives at each look (`offers`), the hard rules first (the spa's floor, the circulation's run-on).
"""

from __future__ import annotations

import asyncio
import copy
import logging
from collections import deque
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Final

from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from ..const import DOMAIN
from ..core.parse import finite_number, number_from_state
from ..heat.automations import steering_automations
from ..heat.config import HeatConfig
from ..loads.hold import KIND_SETTING, KIND_SWITCH, OBSERVED_FOREIGN, READ_BACK_CLOUD, READ_BACK_LOCAL, Value, same
from ..loads.hold_part import (
    REASON_AUTOMATION,
    REASON_CLOUD,
    REASON_FAULT,
    REASON_FOREIGN_CHANGE,
    REASON_FOREIGN_MODE,
    REASON_STARTS_CAP,
    REASON_UNAVAILABLE,
    REASON_WRITE_CAP,
    STATE_CONTROLLED,
    STATE_HANDED_BACK,
    STATE_OFF,
    STATE_PAUSED,
    STATE_SEASON_CLOSED,
    HoldPart,
)
from .config import SHAPE_CLIMATE, PoolConfig
from .detect import CLOUD_DOMAINS
from .plan import DEFAULT_CIRCULATION_KW, DEFAULT_HEAT_PUMP_KW
from .rules import (
    CIRC_LEAD_S,
    CIRCULATION_MIN_OFF_MIN,
    CIRCULATION_MIN_ON_MIN,
    CIRCULATION_STARTS_DAY,
    CIRCULATION_STARTS_HOUR,
    HEAT_PUMP_MIN_OFF_MIN,
    HEAT_PUMP_MIN_ON_MIN,
    HEAT_PUMP_STARTS_DAY,
    LOAD_POOL_CIRCULATION,
    LOAD_POOL_HEAT,
    ON_OFF_WRITES_DAY,
    RUN_ON_MIN,
    SETPOINT_WRITES_DAY,
)

if TYPE_CHECKING:
    from ..site.shed_order import Offer
    from .runtime import PoolSpaShadow

_LOGGER = logging.getLogger(__name__)

LOOK_S: Final = 30.0
STORE_VERSION: Final = 1
_SAVE_DELAY_S: Final = 60.0
SHUTDOWN_WAIT_S: Final = 5.0
LOG_SIZE: Final = 100
#: A fault on the heat pump's device keeps it off this long.
FAULT_HOLD: Final = timedelta(hours=1)
#: Platforms of a switch read through a cloud (the circulation's relay): slow planned actions only.
CLOUD_PLATFORMS: Final = frozenset({*CLOUD_DOMAINS, "smartthings"})

#: Why the heat pump waits or is held off by the interlock (the card's line).
REASON_INTERLOCK: Final = "interlock"
#: A climate that cannot be set to `heat` and `off`.
REASON_UNSUPPORTED: Final = "unsupported"
REASON_RUN_ON: Final = "run_on"
#: The heat pump is not SpotNav's and does not say whether it runs.
REASON_PUMP_NOT_OURS: Final = "pump_not_ours"
#: The person's heat is written back once the circulation runs (an exit with the water still).
REASON_RESTORE_WAITS: Final = "restore_waits"
#: What the pool would do is older than this: the control holds what is there.
STALE_PLAN: Final = timedelta(minutes=5)

KEY_CIRC: Final = "circulation"
KEY_HP: Final = "heat_pump"
KEY_HP_SET: Final = "heat_pump_setpoint"

_UNREADABLE: Final = frozenset({"unavailable", "unknown"})


def store_key(entry_id: str) -> str:
    return f"{DOMAIN}.pool_spa_control.{entry_id}"


def to_unit(celsius: float, unit: Any) -> float:
    """A °C figure in the entity's unit (°F where it says so)."""
    if unit == UnitOfTemperature.FAHRENHEIT:
        return round(celsius * 9.0 / 5.0 + 32.0)
    return celsius


def platform_of(hass: HomeAssistant, entity_id: str | None) -> str | None:
    from homeassistant.helpers import entity_registry as er  # noqa: PLC0415

    if entity_id is None:
        return None
    entry = er.async_get(hass).async_get(entity_id)
    return None if entry is None else entry.platform


def device_binary_sensors(hass: HomeAssistant, entity_id: str | None) -> tuple[list[str], list[str]]:
    """`(flow, fault)`: the flow sensors (on: water flows) and the problem sensors (on: a fault, or no flow) on the
    entity's device."""
    from homeassistant.helpers import entity_registry as er  # noqa: PLC0415

    if entity_id is None:
        return [], []
    registry = er.async_get(hass)
    entry = registry.async_get(entity_id)
    if entry is None or entry.device_id is None:
        return [], []
    flow: list[str] = []
    fault: list[str] = []
    for item in er.async_entries_for_device(registry, entry.device_id):
        if item.domain != "binary_sensor" or item.disabled_by is not None:
            continue
        device_class = item.device_class or item.original_device_class
        words = f"{item.translation_key or ''} {item.original_name or ''} {item.name or ''} {item.unique_id}".casefold()
        if device_class == "problem":
            fault.append(item.entity_id)
        elif "flow" in words or "flöde" in words:
            flow.append(item.entity_id)
    return flow, fault


class PoolPart:
    """The pool's circulation and heat pump under control (see the module)."""

    def __init__(self, control: PoolSpaControl) -> None:
        self.control = control
        self.hass = control.hass
        self.circ = HoldPart(control.hass, "pool circulation", control.tz, control.log)
        self.hp = HoldPart(control.hass, "pool heat pump", control.tz, control.log)
        # Seen.
        self.circ_on_since: datetime | None = None
        self.circ_off_since: datetime | None = None
        self.hp_on_since: datetime | None = None
        #: Since when the heat pump reads off (from the first look reading it off: its past is not known), and when it
        #: was last seen to stop (the minimum off time counts from a stop seen).
        self.hp_off_since: datetime | None = None
        self.hp_stopped_at: datetime | None = None
        self.circ_stopped_at: datetime | None = None
        self.fault_until: datetime | None = None
        self.circ_read: str | None = None
        self.hp_read: str | None = None
        self.has_pump = False
        #: Whether the pump's state holds the circulation on (`None`: unreadable), when it last went idle, and a pump
        #: SpotNav does not control that does not say whether it runs.
        self.hp_holds: bool | None = None
        self.hp_idle_at: datetime | None = None
        self.pump_not_ours = False
        # Kept: the dial last written to the pump (°C).
        self.dial_written: float | None = None

    def stored(self) -> dict[str, Any]:
        return {"circulation": self.circ.stored(), "heat_pump": self.hp.stored(), "dial_written": self.dial_written}

    def restore(self, raw: Any) -> None:
        if not isinstance(raw, dict):
            return
        self.circ.restore(raw.get("circulation"))
        self.hp.restore(raw.get("heat_pump"))
        self.dial_written = finite_number(raw.get("dial_written"))

    # ---- reads

    def _state(self, entity_id: str | None) -> Any:
        return None if entity_id is None else self.hass.states.get(entity_id)

    def _switch(self, entity_id: str | None) -> str | None:
        state = self._state(entity_id)
        if state is None or state.state in _UNREADABLE:
            return None
        return state.state

    def _hp_values(self, pool: PoolConfig) -> tuple[str | None, float | None, Any, tuple[float | None, float | None]]:
        """The heat pump's on/off reading (a climate's mode, the switch), its setpoint as read, the setpoint's unit and
        its range."""
        pump = pool.heat_pump
        if pump is None:
            return None, None, None, (None, None)
        if pump.shape == SHAPE_CLIMATE:
            state = self._state(pump.entity_id)
            if state is None or state.state in _UNREADABLE:
                return None, None, None, (None, None)
            attributes = state.attributes
            unit = self.hass.config.units.temperature_unit
            return (
                state.state,
                finite_number(attributes.get("temperature")),
                unit,
                (finite_number(attributes.get("min_temp")), finite_number(attributes.get("max_temp"))),
            )
        on = self._switch(pump.switch_id)
        number = self._state(pump.number_id)
        if number is None or number.state in _UNREADABLE:
            return on, None, None, (None, None)
        attributes = number.attributes
        return (
            on,
            number_from_state(number.state),
            attributes.get("unit_of_measurement"),
            (finite_number(attributes.get("min")), finite_number(attributes.get("max"))),
        )

    def _on_value(self, pool: PoolConfig) -> str:
        pump = pool.heat_pump
        return "heat" if pump is not None and pump.shape == SHAPE_CLIMATE else "on"

    def _hp_call(self, pool: PoolConfig, value: Value) -> tuple[str, str, dict[str, Any]] | None:
        pump = pool.heat_pump
        if pump is None:
            return None
        if pump.shape == SHAPE_CLIMATE:
            if value not in ("heat", "off"):
                # Cool, auto, heat_cool: never written.
                return None
            return ("climate", "set_hvac_mode", {"entity_id": pump.entity_id, "hvac_mode": value})
        if pump.switch_id is None or value not in ("on", "off"):
            return None
        return ("switch", "turn_on" if value == "on" else "turn_off", {"entity_id": pump.switch_id})

    def _set_call(self, pool: PoolConfig, value: Value) -> tuple[str, str, dict[str, Any]] | None:
        pump = pool.heat_pump
        if pump is None or isinstance(value, str):
            return None
        if pump.shape == SHAPE_CLIMATE:
            return ("climate", "set_temperature", {"entity_id": pump.entity_id, "temperature": value})
        if pump.number_id is None:
            return None
        return ("number", "set_value", {"entity_id": pump.number_id, "value": value})

    def _circ_call(self, pool: PoolConfig, value: Value) -> tuple[str, str, dict[str, Any]] | None:
        entity_id = pool.circulation_id
        if entity_id is None or value not in ("on", "off"):
            return None
        return (entity_id.split(".", 1)[0], "turn_on" if value == "on" else "turn_off", {"entity_id": entity_id})

    def hp_cloud(self, pool: PoolConfig) -> bool:
        pump = pool.heat_pump
        if pump is None:
            return False
        return platform_of(self.hass, pump.switch_id or pump.entity_id) in CLOUD_DOMAINS

    def circ_cloud(self, pool: PoolConfig) -> bool:
        return platform_of(self.hass, pool.circulation_id) in CLOUD_PLATFORMS

    def hp_running(self, pool: PoolConfig) -> bool:
        """Whether the heat pump may run: any mode but off (another mode may run the compressor too)."""
        return self.hp_read is not None and self.hp_read != "off"

    def _run_on(self, now: datetime, pool: PoolConfig | None = None) -> bool:
        """The circulation must run: the heat pump runs (or a write to start it is in flight), or stopped less than
        `RUN_ON_MIN` ago. A heat pump SpotNav controls runs while its mode is not off; one it does not control runs
        while its `hvac_action` says so, and only a stop seen starts its run-on (review S2). An unreadable pump counts
        as running unless its last reading was off for the run-on (review B4)."""
        held = self.hp.helds.get(KEY_HP)
        starting = held is not None and held.pending is not None and held.pending.value in ("heat", "on")
        if starting or self.hp_holds is True:
            return True
        run_on = timedelta(minutes=RUN_ON_MIN)
        if self.hp_holds is None and self.has_pump:
            return not (self.hp_off_since is not None and now - self.hp_off_since >= run_on)
        controlled = pool is not None and pool.control_heat_pump
        if controlled:
            return self.hp_off_since is not None and now - self.hp_off_since < run_on
        stops = [moment for moment in (self.hp_stopped_at, self.hp_idle_at) if moment is not None]
        return bool(stops) and now - max(stops) < run_on

    # ---- each look

    def _see(self, pool: PoolConfig, now: datetime) -> None:
        self.circ_read = self._switch(pool.circulation_id)
        if self.circ_read == "on":
            self.circ_on_since = self.circ_on_since or now
            self.circ_off_since = None
        elif self.circ_read is not None:
            if self.circ_on_since is not None:
                self.circ_stopped_at = now
            self.circ_on_since = None
            self.circ_off_since = self.circ_off_since or now
        hp_read, _set, _unit, _range = self._hp_values(pool)
        self.hp_read = hp_read
        pump = pool.heat_pump
        self.has_pump = pump is not None
        if hp_read is not None and hp_read != "off":
            self.hp_on_since = self.hp_on_since or now
        elif hp_read is not None:
            if self.hp_on_since is not None:
                self.hp_stopped_at = now
            self.hp_on_since = None
        # Whether the pump's state holds the circulation on (`_run_on`).
        action = None
        if pump is not None and pump.shape == SHAPE_CLIMATE and (state := self._state(pump.entity_id)) is not None:
            action = state.attributes.get("hvac_action")
        self.pump_not_ours = False
        if hp_read is None:
            holds: bool | None = None
        elif pool.control_heat_pump or action is None and hp_read == "off":
            holds = hp_read != "off"
        elif action is not None:
            holds = str(action) not in ("idle", "off")
        else:
            # A pump SpotNav does not control and that does not say whether it runs: its own flow switch protects it.
            holds = False
            self.pump_not_ours = True
        if holds is True:
            self.hp_off_since = None
        elif holds is False:
            if self.hp_holds is True:
                self.hp_idle_at = now
            self.hp_off_since = self.hp_off_since or now
        self.hp_holds = holds

    def _faulted(self, pool: PoolConfig, now: datetime) -> bool:
        pump = pool.heat_pump
        _flow, faults = device_binary_sensors(self.hass, None if pump is None else (pump.switch_id or pump.entity_id))
        if any((state := self.hass.states.get(entity_id)) is not None and state.state == "on" for entity_id in faults):
            if self.fault_until is None or self.fault_until <= now:
                self.control.log(now, "pool heat pump", "fault")
            self.fault_until = now + FAULT_HOLD
        return self.fault_until is not None and self.fault_until > now

    def _flow_ok(self, pool: PoolConfig) -> bool:
        """No flow sensor, or every one reads flow."""
        pump = pool.heat_pump
        flows, _faults = device_binary_sensors(self.hass, None if pump is None else (pump.switch_id or pump.entity_id))
        return all((state := self.hass.states.get(entity_id)) is not None and state.state == "on" for entity_id in flows)

    async def async_look(self, now: datetime, pool: PoolConfig | None, action: str | None) -> None:
        """One look (see the module). `action` is what the plan does now (`heat`, `filter`, `off`)."""
        if pool is None:
            for part in (self.hp, self.circ):
                part.state, part.reason = STATE_OFF, None
            await self._async_exit(now, None)
            return
        self._see(pool, now)
        hp_set_read = self._hp_values(pool)[1]
        read_back_hp = READ_BACK_CLOUD if self.hp_cloud(pool) else READ_BACK_LOCAL
        read_back_circ = READ_BACK_CLOUD if self.circ_cloud(pool) else READ_BACK_LOCAL
        self.circ.observe(KEY_CIRC, self.circ_read, now, read_back_circ)
        self.hp.observe(KEY_HP, self.hp_read, now, read_back_hp)
        if self.hp.observe(KEY_HP_SET, hp_set_read, now, read_back_hp) == OBSERVED_FOREIGN:
            # The person's setpoint at the panel is theirs: only a change of the dial writes again (review S1).
            self.dial_written = pool.target_c
        if not pool.season_open:
            # What SpotNav still holds is handed back first, as at an opt-out (the heat pump before the circulation,
            # the run-on respected); once nothing of SpotNav's is left, nothing is written at all.
            for part in (self.hp, self.circ):
                part.state, part.reason = STATE_SEASON_CLOSED, None
                part.restart_restore = False
            if self.hp.holds() or self.circ.holds():
                done = await self._async_exit(now, pool)
                if not done and self.hp.reason == REASON_RESTORE_WAITS:
                    # The person's heat would wait for the water to move; with the season closed the heat pump is
                    # left off (SpotNav's last value) rather than heated again.
                    held = self.hp.helds.get(KEY_HP)
                    if held is not None:
                        held.drop(self.hp_read)
                    self.hp.state, self.hp.reason = STATE_SEASON_CLOSED, None
                    done = not self.hp.holds() and not self.circ.holds()
                if done:
                    self.dial_written = None
                    for part in (self.hp, self.circ):
                        self.control.log(now, part.name, "let_go", why="season_closed")
            return
        for part in (self.hp, self.circ):
            part.trim(now)
        if self.hp.restart_restore or self.circ.restart_restore:
            await self._async_exit(now, pool)
            self.hp.restart_restore = self.hp.holds()
            self.circ.restart_restore = self.circ.holds()
            if self.hp.restart_restore or self.circ.restart_restore:
                return
        order = self.control.shed_until
        shed_heat = order(LOAD_POOL_HEAT, now) is not None
        shed_circ = order(LOAD_POOL_CIRCULATION, now) is not None
        if action is None:
            # No plan, or one gone stale (a look that failed): what is there is held, never read as off (review S5).
            hp_held, circ_held = self.hp.helds.get(KEY_HP), self.circ.helds.get(KEY_CIRC)
            keeping_hp = (self.hp_read if hp_held is None else hp_held.target(self.hp_read)) not in (None, "off")
            keeping_circ = (self.circ_read if circ_held is None else circ_held.target(self.circ_read)) == "on"
            want_hp = keeping_hp and not shed_heat and not shed_circ
            want_circ = keeping_circ and not shed_circ
        else:
            want_hp = action == "heat" and not shed_heat and not shed_circ
            want_circ = action in ("heat", "filter") and not shed_circ
        await self._async_heat_pump(now, pool, want_hp, hp_set_read)
        await self._async_circulation(now, pool, want_circ)

    def _blocked(self, part: HoldPart, entities: list[str | None], now: datetime) -> bool:
        """Kept off by a pause, an automation that steers the load, or an unreadable entity: `True` (the state set)."""
        part.automation = next(iter(steering_automations(self.hass, [e for e in entities if e])), None)
        if part.automation is not None:
            part.state, part.reason = STATE_PAUSED, REASON_AUTOMATION
            return True
        if part.paused(now):
            handed = part.paused_reason == REASON_FOREIGN_CHANGE
            part.state, part.reason = (STATE_HANDED_BACK if handed else STATE_PAUSED), part.paused_reason
            return True
        return False

    async def _async_heat_pump(self, now: datetime, pool: PoolConfig, want: bool, set_read: float | None) -> None:
        part = self.hp
        pump = pool.heat_pump
        if pump is None or not pool.control_heat_pump:
            part.state, part.reason = STATE_OFF, None
            await self._async_exit_hp(now, pool)
            return
        if self._blocked(part, [pump.entity_id, pump.switch_id, pump.number_id], now):
            await self._async_exit_hp(now, pool)
            return
        if self.hp_read is None:
            part.state, part.reason = STATE_PAUSED, REASON_UNAVAILABLE
            return
        on_value = self._on_value(pool)
        if pump.shape == SHAPE_CLIMATE:
            state = self._state(pump.entity_id)
            modes = None if state is None else state.attributes.get("hvac_modes")
            if isinstance(modes, (list, tuple)) and not {"heat", "off"} <= {str(mode) for mode in modes}:
                # A climate that cannot be set to heat and off (a mapping of localtuya's, a panel's heater): never written.
                part.state, part.reason = STATE_PAUSED, REASON_UNSUPPORTED
                return
        if self.hp_read not in (on_value, "off"):
            # Another mode the person chose: watched until it reads heat or off again.
            part.state, part.reason = STATE_PAUSED, REASON_FOREIGN_MODE
            return
        part.state, part.reason = STATE_CONTROLLED, REASON_CLOUD if self.hp_cloud(pool) else None
        held = part.held(KEY_HP)
        heading_on = held.target(self.hp_read) == on_value
        faulted = self._faulted(pool, now)
        circulating = self.circ_read == "on"
        flow = self._flow_ok(pool)
        if heading_on and (faulted or not circulating or not flow):
            # The interlock (or a fault): off at once, whatever the budget or the minimum on time.
            if held.pending is None or held.pending.value != "off":
                await part.async_write(KEY_HP, self._hp_call(pool, "off"), "off", now, kind=KIND_SWITCH, limit=None)  # type: ignore[arg-type]
            part.reason = REASON_FAULT if faulted else REASON_INTERLOCK
            return
        if want and not heading_on:
            if faulted:
                part.reason = REASON_FAULT
            elif self.circ_on_since is None or now - self.circ_on_since < timedelta(seconds=CIRC_LEAD_S) or not flow:
                part.reason = REASON_INTERLOCK
            elif self.hp_stopped_at is not None and now - self.hp_stopped_at < timedelta(minutes=HEAT_PUMP_MIN_OFF_MIN):
                pass
            elif part.budget.started(KEY_HP, now - timedelta(days=1)) >= HEAT_PUMP_STARTS_DAY:
                part.reason = REASON_STARTS_CAP
            elif not await part.async_write(
                KEY_HP, self._hp_call(pool, on_value), on_value, now, kind=KIND_SWITCH, limit=ON_OFF_WRITES_DAY  # type: ignore[arg-type]
            ):
                part.reason = REASON_WRITE_CAP
            else:
                part.budget.note_start(KEY_HP, now)
        elif not want and heading_on and held.pending is None:
            if self.hp_on_since is None or now - self.hp_on_since >= timedelta(minutes=HEAT_PUMP_MIN_ON_MIN):
                # Off is never held back by the budget: the start counted the cycle.
                await part.async_write(KEY_HP, self._hp_call(pool, "off"), "off", now, kind=KIND_SWITCH, limit=None)  # type: ignore[arg-type]
        await self._async_dial(now, pool, set_read)

    async def _async_dial(self, now: datetime, pool: PoolConfig, set_read: float | None) -> None:
        """The dial's water temperature written to the pump once per change, within its range."""
        part = self.hp
        if set_read is None or same(self.dial_written, pool.target_c) or part.state != STATE_CONTROLLED:
            return
        _on, _set, unit, (low, high) = self._hp_values(pool)
        value = to_unit(pool.target_c, unit)
        if (low is not None and value < low) or (high is not None and value > high):
            self.dial_written = pool.target_c
            return
        held = part.held(KEY_HP_SET)
        if held.pending is not None:
            return
        if same(set_read, value):
            self.dial_written = pool.target_c
            return
        call = self._set_call(pool, value)
        if call is None:
            return
        if await part.async_write(KEY_HP_SET, call, value, now, kind=KIND_SETTING, limit=SETPOINT_WRITES_DAY):
            self.dial_written = pool.target_c
        else:
            part.reason = REASON_WRITE_CAP

    async def _async_circulation(self, now: datetime, pool: PoolConfig, want: bool) -> None:
        part = self.circ
        if pool.circulation_id is None or not pool.control_circulation:
            part.state, part.reason = STATE_OFF, None
            await self._async_exit_circ(now, pool)
            return
        if self._blocked(part, [pool.circulation_id], now):
            await self._async_exit_circ(now, pool)
            return
        if self.circ_read is None:
            part.state, part.reason = STATE_PAUSED, REASON_UNAVAILABLE
            return
        part.state, part.reason = STATE_CONTROLLED, REASON_CLOUD if self.circ_cloud(pool) else None
        if self.pump_not_ours:
            # The heat pump is the person's and does not say when it runs: the circulation follows the plan only, the
            # pump's own flow switch protects it (turn on the heat pump's control too).
            part.reason = REASON_PUMP_NOT_OURS
        held = part.held(KEY_CIRC)
        heading_on = held.target(self.circ_read) == "on"
        run_on = self._run_on(now, pool)
        if (want or run_on) and not heading_on:
            if run_on:
                # The heat pump needs the water moving: on at once.
                await part.async_write(KEY_CIRC, self._circ_call(pool, "on"), "on", now, kind=KIND_SWITCH, limit=None)  # type: ignore[arg-type]
                part.budget.note_start(KEY_CIRC, now)
                return
            if self.circ_stopped_at is not None and now - self.circ_stopped_at < timedelta(minutes=CIRCULATION_MIN_OFF_MIN):
                return
            if (
                part.budget.started(KEY_CIRC, now - timedelta(days=1)) >= CIRCULATION_STARTS_DAY
                or part.budget.started(KEY_CIRC, now - timedelta(hours=1)) >= CIRCULATION_STARTS_HOUR
            ):
                part.reason = REASON_STARTS_CAP
                return
            if await part.async_write(KEY_CIRC, self._circ_call(pool, "on"), "on", now, kind=KIND_SWITCH, limit=ON_OFF_WRITES_DAY):  # type: ignore[arg-type]
                part.budget.note_start(KEY_CIRC, now)
            else:
                part.reason = REASON_WRITE_CAP
        elif not want and heading_on and held.pending is None:
            if run_on:
                part.reason = REASON_RUN_ON
                return
            if self.circ_on_since is not None and now - self.circ_on_since < timedelta(minutes=CIRCULATION_MIN_ON_MIN):
                return
            await part.async_write(KEY_CIRC, self._circ_call(pool, "off"), "off", now, kind=KIND_SWITCH, limit=None)  # type: ignore[arg-type]

    # ---- exits

    async def _async_exit_hp(self, now: datetime, pool: PoolConfig | None) -> bool:
        """The person's mode and setpoint back where the pump still reads SpotNav's; their `heat` only while the
        circulation reads on (the interlock holds at an exit too). `True` when nothing of SpotNav's is left."""
        if pool is None or pool.heat_pump is None:
            for held in self.hp.helds.values():
                held.drop(None)
            return True
        circulating = self.circ_read == "on"
        held = self.hp.helds.get(KEY_HP)
        if (
            held is not None
            and held.pending is None
            and held.ours(self.hp_read)
            and self.hp_read not in (None, "off")
            and held.person != "off"
            and not circulating
        ):
            # The person's value is on, but the water is not moving: SpotNav's on is turned off first (the interlock
            # holds at an exit too); the person's on is then not written back.
            await self.hp.async_write(KEY_HP, self._hp_call(pool, "off"), "off", now, kind=KIND_SWITCH, limit=None)  # type: ignore[arg-type]
            return False
        if (
            held is not None
            and held.pending is None
            and held.ours(self.hp_read)
            and held.person not in (None, "off")
            and not circulating
        ):
            # The person's heat waits for the water to move: kept, shown, and written back at the first look the
            # circulation reads on (never dropped; review S3).
            self.hp.state, self.hp.reason = STATE_PAUSED, REASON_RESTORE_WAITS
            return False

        def mode_call(value: Value) -> tuple[str, str, dict[str, Any]] | None:
            if value != "off" and not circulating:
                return None
            return self._hp_call(pool, value)

        done = await self.hp.async_give_back(KEY_HP, self.hp_read, mode_call, now)
        done = await self.hp.async_give_back(KEY_HP_SET, self._hp_values(pool)[1], lambda v: self._set_call(pool, v), now) and done
        if done and not pool.control_heat_pump:
            self.dial_written = None
        return done

    async def _async_exit_circ(self, now: datetime, pool: PoolConfig | None) -> bool:
        """The person's circulation back where it still reads SpotNav's; an off only once the heat pump's run-on is
        over."""
        if pool is None or pool.circulation_id is None:
            for held in self.circ.helds.values():
                held.drop(None)
            return True
        held = self.circ.helds.get(KEY_CIRC)
        if held is not None and held.person == "off" and held.ours(self.circ_read) and self._run_on(now, pool):
            return False
        return await self.circ.async_give_back(KEY_CIRC, self.circ_read, lambda v: self._circ_call(pool, v), now)

    async def _async_exit(self, now: datetime, pool: PoolConfig | None) -> bool:
        """Both handed back, the heat pump first."""
        if pool is not None:
            self._see(pool, now)
        hp = await self._async_exit_hp(now, pool)
        circ = await self._async_exit_circ(now, pool)
        return hp and circ

    # ---- the shed order

    def offers(self, now: datetime, pool: PoolConfig, *, beyond: bool, until_max: datetime | None) -> dict[str, Offer]:
        """The live offers of the loads SpotNav controls locally (cloud routes and watched loads: none here)."""
        from ..site.shed_order import Offer  # noqa: PLC0415

        out: dict[str, Offer] = {}
        order = self.control.shed_until
        running = self.hp_running(pool)
        hp_give = (pool.heat_pump_kw or DEFAULT_HEAT_PUMP_KW) * 1000.0
        if pool.season_open and pool.control_heat_pump and self.hp.state == STATE_CONTROLLED and not self.hp_cloud(pool):
            if order(LOAD_POOL_HEAT, now) is not None:
                out[LOAD_POOL_HEAT] = Offer(live=True, ready=True, give_w=hp_give, drawing=running, held=not running, pending=running)
            else:
                settled = self.hp_on_since is not None and now - self.hp_on_since >= timedelta(minutes=HEAT_PUMP_MIN_ON_MIN)
                ready = running and settled
                out[LOAD_POOL_HEAT] = Offer(
                    live=True, ready=ready, give_w=hp_give if running else 0.0, drawing=running,
                    why=None if ready else ("min_on" if running else "off"),
                )
        # A live circulation step would turn a cloud heat pump off with it: never (review S4).
        cloud_pump_on = pool.control_heat_pump and self.hp_cloud(pool) and running
        if (
            pool.season_open
            and pool.control_circulation
            and self.circ.state == STATE_CONTROLLED
            and not self.circ_cloud(pool)
            and not cloud_pump_on
        ):
            on = self.circ_read == "on"
            give = (pool.circulation_kw or DEFAULT_CIRCULATION_KW) * 1000.0 + (hp_give if running else 0.0)
            if order(LOAD_POOL_CIRCULATION, now) is not None:
                # Held once it reads off; the heat pump's run-on comes first (a slow step).
                out[LOAD_POOL_CIRCULATION] = Offer(
                    live=True, ready=True, give_w=give, drawing=on, held=not on, pending=on, until_max=until_max
                )
            else:
                settled = self.circ_on_since is not None and now - self.circ_on_since >= timedelta(minutes=CIRCULATION_MIN_ON_MIN)
                fits = beyond or (until_max is not None and until_max > now)
                ready = on and settled and fits
                why = None if ready else ("off" if not on else "min_on" if not settled else "need")
                out[LOAD_POOL_CIRCULATION] = Offer(
                    live=True, ready=ready, give_w=give if on else 0.0, drawing=on, why=why,
                    until_max=None if beyond else until_max,
                )
        return out

    def status(self, now: datetime, pool: PoolConfig) -> dict[str, dict[str, Any]]:
        return {
            "heat_pump": self.hp.status(now, enabled=pool.control_heat_pump, live=not self.hp_cloud(pool)),
            "circulation": self.circ.status(now, enabled=pool.control_circulation, live=not self.circ_cloud(pool)),
        }


class PoolSpaControl:
    """The site's pool, spa and sauna control (see the module). Never raises into the site."""

    def __init__(self, hass: HomeAssistant, site: Any) -> None:
        from ..spa.control import DeadlinePart  # noqa: PLC0415 - the spa's part imports this module's helpers

        self.hass = hass
        self.site = site
        self._store: Store[dict[str, Any]] = Store(hass, STORE_VERSION, store_key(site.entry_id))
        self.log_lines: deque[dict[str, Any]] = deque(maxlen=LOG_SIZE)
        self.pool = PoolPart(self)
        self.spa = DeadlinePart(self, "spa")
        self.sauna = DeadlinePart(self, "sauna")
        self._closed = False
        self._started = False
        self._looking = False
        self._again = False
        self._task: asyncio.Task[None] | None = None
        self._timer_cancel: Callable[[], None] | None = None
        self._saved: dict[str, Any] | None = None
        self._save_pending = False

    @property
    def shadow(self) -> PoolSpaShadow:
        return self.site.pool_spa

    def tz(self) -> str:
        return self.shadow.tz

    def config(self) -> HeatConfig:
        return self.shadow.config()

    def log(self, now: datetime, part: str, what: str, **extra: Any) -> None:
        self.log_lines.append({"at": now.isoformat(), "part": part, "what": what, **extra})

    def shed_until(self, load: str, now: datetime) -> datetime | None:
        order = getattr(self.site, "shed_order", None)
        return None if order is None else order.until(load, now)

    # ---- kept

    def _data(self) -> dict[str, Any]:
        return {"pool": self.pool.stored(), "spa": self.spa.stored(), "sauna": self.sauna.stored()}

    def _save(self) -> None:
        if self._save_pending or self._data() == self._saved:
            return
        self._save_pending = True
        self._store.async_delay_save(self._data_to_save, _SAVE_DELAY_S)

    def _data_to_save(self) -> dict[str, Any]:
        self._save_pending = False
        data = self._data()
        self._saved = copy.deepcopy(data)
        return data

    async def async_save(self) -> None:
        data = self._data()
        if data != self._saved:
            await self._store.async_save(data)
            self._saved = copy.deepcopy(data)

    @staticmethod
    async def async_remove_stored(hass: HomeAssistant, entry_id: str) -> None:
        await Store(hass, STORE_VERSION, store_key(entry_id)).async_remove()

    # ---- life cycle

    async def async_start(self) -> None:
        try:
            raw = await self._store.async_load()
        except Exception:  # noqa: BLE001 - a record SpotNav cannot read: nothing held is known
            _LOGGER.warning("SpotNav pool and spa control: its record could not be read", exc_info=True)
            raw = None
        if isinstance(raw, dict):
            self.pool.restore(raw.get("pool"))
            self.spa.restore(raw.get("spa"))
            self.sauna.restore(raw.get("sauna"))
            self._saved = copy.deepcopy(self._data())
        self._started = True
        self._timer_cancel = async_track_time_interval(self.hass, self._async_tick, timedelta(seconds=LOOK_S))
        self.request_look()

    @callback
    def halt(self) -> None:
        self._closed = True
        if self._timer_cancel is not None:
            self._timer_cancel()
            self._timer_cancel = None

    async def async_shutdown(self) -> None:
        """Hand everything back (bounded): the heat pump before the circulation, the circulation only once the heat
        pump's run-on is over (else kept, and handed back at the next start); the spa and the sauna side by side."""
        self.halt()
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                async with asyncio.timeout(SHUTDOWN_WAIT_S):
                    await asyncio.wait({self._task})
            except TimeoutError:
                _LOGGER.warning("SpotNav pool and spa control: a look did not end at the stop")
        if not self._started:
            return
        now = dt_util.utcnow()
        try:
            config = self.config()
        except Exception:  # noqa: BLE001 - nothing known to hand back by: kept for the next start
            config = None
        jobs = []
        if config is not None:
            if (self.pool.hp.holds() or self.pool.circ.holds()) and config.pool is not None:
                jobs.append(self._async_settle(lambda: self.pool._async_exit(now, config.pool)))
            for part, block in ((self.spa, config.spa), (self.sauna, config.sauna)):
                if part.part.holds():
                    jobs.append(self._async_settle(lambda part=part, block=block: part.async_exit(now, block)))
        if jobs:
            await asyncio.gather(*jobs, return_exceptions=True)
        try:
            await self.async_save()
        except Exception:  # noqa: BLE001 - what was stored before stands
            _LOGGER.warning("SpotNav pool and spa control: its record could not be saved", exc_info=True)

    async def _async_settle(self, exit_once: Callable[[], Any]) -> None:
        """One exit at the stop, then its writes read back for up to `SHUTDOWN_WAIT_S`; what does not show stays kept
        and is handed back at the next start."""
        try:
            async with asyncio.timeout(SHUTDOWN_WAIT_S):
                await exit_once()
                for _try in range(int(SHUTDOWN_WAIT_S / 0.2)):
                    await asyncio.sleep(0.2)
                    if await self._async_confirm_all(dt_util.utcnow()):
                        break
        except TimeoutError:
            _LOGGER.warning("SpotNav pool and spa control: a hand-back did not show by the stop")
        except Exception:  # noqa: BLE001 - kept for the next start
            _LOGGER.warning("SpotNav pool and spa control: a hand-back at the stop failed", exc_info=True)

    async def _async_confirm_all(self, now: datetime) -> bool:
        """Every pending write of the person's values read; `True` once none is left."""
        config = self.config()
        left = False
        if config.pool is not None:
            self.pool._see(config.pool, now)
            self.pool.circ.observe(KEY_CIRC, self.pool.circ_read, now, READ_BACK_CLOUD)
            self.pool.hp.observe(KEY_HP, self.pool.hp_read, now, READ_BACK_CLOUD)
            self.pool.hp.observe(KEY_HP_SET, self.pool._hp_values(config.pool)[1], now, READ_BACK_CLOUD)
        for part, block in ((self.spa, config.spa), (self.sauna, config.sauna)):
            if block is not None:
                part.observe(now, block)
        for part in (self.pool.hp, self.pool.circ, self.spa.part, self.sauna.part):
            left = left or any(held.pending is not None for held in part.helds.values())
        return not left

    # ---- looks

    @callback
    def _async_tick(self, _now: datetime) -> None:
        self.request_look()

    @callback
    def request_look(self) -> None:
        if self._closed or not self._started:
            return
        if self._looking:
            self._again = True
            return
        self._looking = True
        self._task = self.hass.async_create_background_task(self._async_looks(), f"{DOMAIN} pool control {self.site.entry_id}")

    async def _async_looks(self) -> None:
        try:
            while True:
                self._again = False
                await self.async_look(dt_util.utcnow())
                if not self._again or self._closed:
                    break
        finally:
            self._looking = False

    async def async_look(self, now: datetime) -> None:
        """One look over the pool, the spa and the sauna. Never raises."""
        try:
            config = self.config()
        except Exception:  # noqa: BLE001 - tried again at the next look
            _LOGGER.debug("SpotNav pool and spa control: the settings could not be read", exc_info=True)
            return
        shadow = self.shadow
        would = getattr(shadow, "would", None)
        would_at = getattr(shadow, "would_at", None)
        stale = would_at is not None and now - would_at > STALE_PLAN
        action = None if would is None or stale else would.get("action")
        looks = (
            ("pool", lambda: self.pool.async_look(now, config.pool, action)),
            ("spa", lambda: self.spa.async_look(now, config.spa)),
            ("sauna", lambda: self.sauna.async_look(now, config.sauna)),
        )
        for name, look in looks:
            try:
                await look()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - the next look tries again
                _LOGGER.warning("SpotNav pool and spa control: a look at the %s failed", name, exc_info=True)
        self._save()

    # ---- what the shadow, the shed order and the card read

    def holds_any(self) -> bool:
        return any(part.holds() for part in (self.pool.hp, self.pool.circ, self.spa.part, self.sauna.part))

    def diagnostics(self) -> dict[str, Any]:
        return {
            "pool": {
                "kept": self.pool.stored(),
                "circulation": {"state": self.pool.circ.state, "reason": self.pool.circ.reason, "read": self.pool.circ_read},
                "heat_pump": {"state": self.pool.hp.state, "reason": self.pool.hp.reason, "read": self.pool.hp_read},
                "fault_until": None if self.pool.fault_until is None else self.pool.fault_until.isoformat(),
            },
            "spa": self.spa.diagnostics(),
            "sauna": self.sauna.diagnostics(),
            "log": list(self.log_lines),
        }
