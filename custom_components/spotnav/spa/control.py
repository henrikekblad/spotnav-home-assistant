"""SpotNav setting the spa or the sauna (spec `plans/v3_pool_spa_spec.md` stages 12 and 13, the sauna decision of
2026-11-23), one `DeadlinePart` each, run by the pool and spa control (`pool/control.py`) on the plan of their shadow
(`spa/runtime.py`). How each family is set is `spa/adapters.py`; what every load shares (the snapshot and its
restore, the hand-back, the read-back, the budgets) is `loads/hold_part.py`.

**The spa** (behind `spa.control`):

* heats for a bath (the plan's `heat` and `hold`), holds its idle temperature between baths, and is never let below
  its floor: water at or below `DEFAULT_FLOOR_C` (10 °C) turns the heat on whatever the plan, the shed order or the day's budget
  (only a hand-back to the person comes first). No setpoint is ever written below `max(idle_c, DEFAULT_FLOOR_C)`.
* Balboa is set by its presets, Ready to heat and Rest between (its hvac `off` is Rest, never "off"); the bath
  temperature is written only when the dial changes. MSpa is watched only while its own heat schedule is set. Gecko
  is set through its water heater only, never the water-care presets.

**The sauna** (behind `sauna.control`, a relay only with its declared safety measure):

* started only for a session the person booked or a "now" press (the plan heats only then), never while its lock
  reads locked (a door open, Tylö's door fault: an on is turned off), never past the device's own longest run
  (Harvia's on time, Saunum's session length, else `SAUNA_MAX_ON`), and never turned on by a restore: an exit only ever
  turns a sauna off.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Final

from ..core.parse import aware_instant, finite_number, number_from_state
from ..heat.automations import steering_automations
from ..loads.hold import KIND_SETTING, KIND_SWITCH, OBSERVED_FOREIGN, READ_BACK_CLOUD, READ_BACK_LOCAL, Value, same
from ..loads.hold_part import (
    REASON_AUTOMATION,
    REASON_CLOUD,
    REASON_FOREIGN_CHANGE,
    REASON_FOREIGN_MODE,
    REASON_UNAVAILABLE,
    REASON_WRITE_CAP,
    STATE_CONTROLLED,
    STATE_HANDED_BACK,
    STATE_OFF,
    STATE_PAUSED,
    HoldPart,
)
from ..pool.rules import LOAD_SAUNA, LOAD_SPA, ON_OFF_WRITES_DAY, SETPOINT_WRITES_DAY
from ..sauna.config import FAMILY_SAUNUM, SaunaConfig
from .adapters import (
    HEAT_PRESET,
    MSPA_SCHEDULE_ID,
    SAUNUM_DURATION_KEY,
    SETPOINT_DIAL,
    SETPOINT_HOLD,
    Route,
    heat_call,
    heat_read,
    route_of,
    setpoint_call,
)
from .config import DEFAULT_FLOOR_C, FAMILY_MSPA, SpaConfig
from .plan import ACTION_FLOOR, ACTION_HEAT, ACTION_HOLD, ACTION_IDLE, SOURCE_NOW

if TYPE_CHECKING:
    from ..pool.control import PoolSpaControl
    from ..site.shed_order import Offer

#: The spa's idle heat comes on this far under its idle temperature (on/off families: SpotNav is the thermostat).
IDLE_BAND_C: Final = 1.0
#: The writes a bath day keeps for the bath's heat-up (two blocks: on, off, on, and off after the bath).
BATH_WRITES: Final = 4
#: The floor's setpoint margin for the families steered by setpoint.
FLOOR_MARGIN_C: Final = 2.0
#: A sauna never runs longer than this where the device says nothing of its own (placeholder).
SAUNA_MAX_ON: Final = timedelta(hours=3)
#: The sauna's on time starts over once it has read off this long (a new run).
RUN_RESET: Final = timedelta(minutes=10)
#: A spa's or a sauna's plan older than this is not followed: the spa is left as it is, the sauna turned off.
STALE_PLAN: Final = timedelta(minutes=5)
#: A gap between two looks counted as on time at most this long (a restart's gap is not counted).
LOOK_GAP: Final = timedelta(minutes=2)
LOAD_SPA_IDLE: Final = "spa_idle"

REASON_OWN_SCHEDULER: Final = "own_scheduler"
REASON_LOCK: Final = "lock"
#: The lock chosen cannot be read: it counts as locked.
REASON_LOCK_UNREADABLE: Final = "lock_unreadable"
REASON_MAX_ON: Final = "max_on"
REASON_FLOOR: Final = "floor"
REASON_STALE: Final = "stale_plan"
REASON_UNSUPPORTED: Final = "unsupported"

KEY_HEAT: Final = "heat"
KEY_SET: Final = "setpoint"

_UNREADABLE: Final = frozenset({"unavailable", "unknown"})


class DeadlinePart:
    """The spa or the sauna under control (see the module)."""

    def __init__(self, control: PoolSpaControl, kind: str) -> None:
        self.control = control
        self.hass = control.hass
        self.kind = kind
        self.part = HoldPart(control.hass, kind, control.tz, control.log)
        self.route: Route | None = None
        self.heat_read: str | None = None
        self.set_read: float | None = None
        self.readable = False
        #: The spa's idle heat is on (on/off families: SpotNav keeps the idle temperature within `IDLE_BAND_C`).
        self.idle_heating = False
        #: The heater was last turned on for a bath (not an idle cycle).
        self.on_for_bath = False
        # The sauna: since when SpotNav has it on, the run (its session, or the "now" press) and its on time so far,
        # and the run its longest run ended (kept).
        self.on_since: datetime | None = None
        self.last_on_look: datetime | None = None
        self.off_since: datetime | None = None
        self.run_key: datetime | None = None
        self.on_seconds = 0.0
        self.max_on_for: datetime | None = None
        # Kept: the dial last written (°C).
        self.dial_written: float | None = None

    def stored(self) -> dict[str, Any]:
        return {
            **self.part.stored(),
            "dial_written": self.dial_written,
            "run_key": None if self.run_key is None else self.run_key.isoformat(),
            "off_since": None if self.off_since is None else self.off_since.isoformat(),
            "on_seconds": self.on_seconds,
            "max_on_for": None if self.max_on_for is None else self.max_on_for.isoformat(),
        }

    def restore(self, raw: Any) -> None:
        if isinstance(raw, dict):
            self.part.restore(raw)
            self.dial_written = finite_number(raw.get("dial_written"))
            self.run_key = aware_instant(raw.get("run_key"))
            self.off_since = aware_instant(raw.get("off_since"))
            self.on_seconds = finite_number(raw.get("on_seconds")) or 0.0
            self.max_on_for = aware_instant(raw.get("max_on_for"))

    @property
    def shadow(self) -> Any:
        return getattr(self.control.shadow, self.kind)

    @property
    def load(self) -> str:
        return LOAD_SPA if self.kind == "spa" else LOAD_SAUNA

    # ---- reads

    def _read(self, config: SpaConfig | SaunaConfig) -> None:
        route = self.route
        state = self.hass.states.get(config.entity_id)
        if route is None or state is None or state.state in _UNREADABLE:
            self.heat_read, self.set_read, self.readable = None, None, False
            return
        self.readable = True
        self.heat_read = heat_read(route, state.state, dict(state.attributes))
        self.set_read = finite_number(state.attributes.get("temperature"))

    def _cloud(self) -> bool:
        return self.route is not None and self.route.cloud

    def observe(self, now: datetime, config: SpaConfig | SaunaConfig) -> None:
        self.route = route_of(config.family, config.entity_id)
        self._read(config)
        read_back = READ_BACK_CLOUD if self._cloud() else READ_BACK_LOCAL
        self.part.observe(KEY_HEAT, self.heat_read, now, read_back)
        if self.part.observe(KEY_SET, self.set_read, now, read_back) == OBSERVED_FOREIGN:
            # The person's setpoint at the panel is theirs: only a change of the dial writes again (review S1).
            self.dial_written = config.bath_c if isinstance(config, SpaConfig) else config.target_c

    def _own_scheduler(self, config: SpaConfig | SaunaConfig) -> bool:
        """MSpa's own heat schedule is set (its "Scheduled for" on the spa's device reads a time)."""
        if config.family != FAMILY_MSPA:
            return False
        from homeassistant.helpers import entity_registry as er  # noqa: PLC0415

        registry = er.async_get(self.hass)
        entry = registry.async_get(config.entity_id)
        if entry is None or entry.device_id is None:
            return False
        for item in er.async_entries_for_device(registry, entry.device_id):
            if item.domain == "datetime" and str(item.unique_id).startswith(MSPA_SCHEDULE_ID):
                state = self.hass.states.get(item.entity_id)
                if state is not None and state.state not in _UNREADABLE:
                    return True
        return False

    def _device_max_on(self, config: SaunaConfig) -> timedelta:
        """The sauna's own longest run: Harvia's on time (read by the shadow), Saunum's session length, else
        `SAUNA_MAX_ON`."""
        reading = getattr(self.shadow, "reading", None)
        found = None if reading is None else reading.max_on
        if found is None and config.family == FAMILY_SAUNUM:
            from homeassistant.helpers import entity_registry as er  # noqa: PLC0415

            registry = er.async_get(self.hass)
            entry = registry.async_get(config.entity_id)
            if entry is not None and entry.device_id is not None:
                for item in er.async_entries_for_device(registry, entry.device_id):
                    if item.domain == "number" and item.translation_key == SAUNUM_DURATION_KEY:
                        state = self.hass.states.get(item.entity_id)
                        minutes = None if state is None else number_from_state(state.state)
                        if minutes is not None and minutes > 0:
                            found = timedelta(minutes=minutes)
        return min(found, SAUNA_MAX_ON) if found is not None else SAUNA_MAX_ON

    # ---- each look

    async def async_look(self, now: datetime, config: SpaConfig | SaunaConfig | None) -> None:
        part = self.part
        if config is None:
            part.state, part.reason = STATE_OFF, None
            for held in part.helds.values():
                held.drop(None)
            return
        self.observe(now, config)
        part.trim(now)
        if part.restart_restore:
            await self.async_exit(now, config)
            part.restart_restore = part.holds()
            if part.restart_restore:
                return
        route = self.route
        if not config.control:
            part.state, part.reason = STATE_OFF, None
            if await self.async_exit(now, config):
                self.dial_written = None
            return
        if route is None:
            part.state, part.reason = STATE_PAUSED, REASON_UNSUPPORTED
            await self.async_exit(now, config)
            return
        part.automation = next(iter(steering_automations(self.hass, [config.entity_id])), None)
        if part.automation is not None:
            part.state, part.reason = STATE_PAUSED, REASON_AUTOMATION
            await self.async_exit(now, config)
            return
        if self._own_scheduler(config):
            part.state, part.reason = STATE_PAUSED, REASON_OWN_SCHEDULER
            await self.async_exit(now, config)
            return
        handed = part.paused(now) and part.paused_reason == REASON_FOREIGN_CHANGE
        if handed:
            part.state, part.reason = STATE_HANDED_BACK, REASON_FOREIGN_CHANGE
            return
        if not self.readable or (self.heat_read is None and route.heat is not None):
            part.state, part.reason = STATE_PAUSED, REASON_UNAVAILABLE
            return
        if route.heat is not None and self.heat_read not in (route.on, route.off) and route.heat != HEAT_PRESET:
            # A mode SpotNav never writes (Tylö's standby, a person's auto): watched until it reads on or off.
            part.state, part.reason = STATE_PAUSED, REASON_FOREIGN_MODE
            return
        if isinstance(config, SpaConfig):
            await self._async_spa(now, config, route)
        else:
            await self._async_sauna(now, config, route)

    async def _async_spa(self, now: datetime, config: SpaConfig, route: Route) -> None:
        part = self.part
        plan = self.shadow.plan
        current = self.shadow.reading.current_c
        floor = current is not None and current <= DEFAULT_FLOOR_C
        paused = part.paused(now)
        if paused and not floor:
            part.state, part.reason = STATE_PAUSED, part.paused_reason
            await self.async_exit(now, config)
            return
        part.state, part.reason = STATE_CONTROLLED, REASON_CLOUD if route.cloud else None
        if self._stale(now) and not floor:
            # The shadow's plan stopped: the spa is left as it is (its own thermostat holds it).
            part.reason = REASON_STALE
            return
        action = None if plan is None else plan.action
        bath = action in (ACTION_HEAT, ACTION_HOLD)
        shed_bath = bath and self.control.shed_until(LOAD_SPA, now) is not None
        shed_idle = action == ACTION_IDLE and self.control.shed_until(LOAD_SPA_IDLE, now) is not None
        hold = config.hold_c
        if action == ACTION_IDLE and current is not None and not shed_idle:
            if current <= hold - IDLE_BAND_C:
                self.idle_heating = True
            elif current >= hold:
                self.idle_heating = False
        elif action != ACTION_IDLE:
            self.idle_heating = False
        heat = floor or action == ACTION_FLOOR or (bath and not shed_bath) or (action == ACTION_IDLE and self.idle_heating and not shed_idle)
        floor_rule = floor or action == ACTION_FLOOR
        if floor_rule:
            part.reason = REASON_FLOOR
        # The floor's write and a shed step's are never held back by the day's budget (review, should-fix).
        exempt = floor_rule or shed_bath or shed_idle
        if route.heat is not None:
            # An idle cycle is SpotNav's own thermostat between baths; waiting between two blocks of a bath's heat-up
            # (the latched session) is the bath's. On a bath day the idle cycles leave the bath its writes.
            latched = getattr(self.shadow, "latch", None) is not None
            idle_cycle = action == ACTION_IDLE and not floor_rule and not latched
            reserve = BATH_WRITES if idle_cycle and self._bath_today(now, plan) else 0
            value = route.on if heat else route.off
            # A heater a bath turned on is held there when the day's last write would turn it off (review S7); one
            # an idle cycle turned on is always turned off (its Ready was only started where its Rest fits).
            idle_write = idle_cycle and (value == route.on or not self.on_for_bath)
            await self._async_set_heat(now, config, route, value, exempt=exempt, idle=idle_write, reserve=reserve)
            if value == route.on and self.heat_read != route.on:
                self.on_for_bath = not idle_cycle
        if route.setpoint == SETPOINT_HOLD:
            # The setpoint is the handle: the bath, or the idle temperature (never below the floor).
            value = config.bath_c if (bath and not shed_bath) else hold
            if floor_rule:
                value = max(value, DEFAULT_FLOOR_C + FLOOR_MARGIN_C)
            await self._async_setpoint(now, config, route, max(value, hold), exempt=exempt)
        elif route.setpoint == SETPOINT_DIAL:
            await self._async_dial(now, config, route, config.bath_c)

    async def _async_sauna(self, now: datetime, config: SaunaConfig, route: Route) -> None:
        part = self.part
        if part.paused(now):
            part.state, part.reason = STATE_PAUSED, part.paused_reason
            await self.async_exit(now, config)
            return
        part.state, part.reason = STATE_CONTROLLED, REASON_CLOUD if route.cloud else None
        shadow = self.shadow
        if self._stale(now):
            # The shadow's plan stopped: SpotNav's own start is turned off, a person's is left (review, should-fix).
            part.reason = REASON_STALE
            if part.held(KEY_HEAT).holds() and self.heat_read == route.on:
                await self._async_set_heat(now, config, route, route.off, exempt=True)
            return
        plan = shadow.plan
        would = shadow.would(config)
        action = None if would is None else would["action"]
        nxt = None if plan is None else plan.next_session
        session = None if nxt is None else nxt[0]
        # The on time of the continuous run counts against the longest run, whatever session or press it serves
        # (review R2 of the sauna's cap): it starts over only once the sauna has read off for `RUN_RESET`. The run a
        # cap ended (its session, or the "now" press) is not started again; another one may start once the sauna has
        # been off `RUN_RESET`, or at once for a new press.
        key = None
        if nxt is not None:
            pressed = getattr(shadow, "now_pressed", None)
            key = pressed if nxt[1] == SOURCE_NOW and pressed is not None else nxt[0]
        on = self.heat_read == route.on
        held = part.held(KEY_HEAT)
        if on and held.ours(self.heat_read):
            self.on_since = self.on_since or now
            self.off_since = None
            if self.last_on_look is not None and timedelta(0) < now - self.last_on_look <= LOOK_GAP:
                self.on_seconds += (now - self.last_on_look).total_seconds()
            self.last_on_look = now
        else:
            self.on_since = None
            self.last_on_look = None
            if not on:
                self.off_since = self.off_since or now
        long_off = self.off_since is not None and now - self.off_since >= RUN_RESET
        if long_off and self.max_on_for is None:
            self.on_seconds = 0.0
        new_press = nxt is not None and nxt[1] == SOURCE_NOW and key != self.run_key
        if self.max_on_for is not None and key is not None and key != self.max_on_for and not on and (long_off or new_press):
            self.max_on_for, self.on_seconds = None, 0.0
        if key is not None:
            self.run_key = key
        want = action in (ACTION_HEAT, ACTION_HOLD) and session is not None
        if shadow.reading.locked or (config.lock_id is not None and shadow.reading.locked is None):
            want = False
            part.reason = REASON_LOCK_UNREADABLE if shadow.reading.lock_unreadable or shadow.reading.locked is None else REASON_LOCK
        if self.run_key is not None and self.max_on_for is None and self.on_seconds >= self._device_max_on(config).total_seconds():
            self.max_on_for = self.run_key
        if self.max_on_for is not None:
            want = False
            part.reason = REASON_MAX_ON
        if self.control.shed_until(LOAD_SAUNA, now) is not None:
            want = False
        if not want and not held.holds():
            # Only what SpotNav turned on is turned off: a session the person started by hand is theirs.
            return
        await self._async_set_heat(now, config, route, route.on if want else route.off, exempt=not want)
        if route.setpoint == SETPOINT_DIAL and want:
            await self._async_dial(now, config, route, config.target_c)

    async def _async_set_heat(
        self,
        now: datetime,
        config: SpaConfig | SaunaConfig,
        route: Route,
        value: str,
        *,
        exempt: bool,
        idle: bool = False,
        reserve: int = 0,
    ) -> None:
        part = self.part
        held = part.held(KEY_HEAT)
        if held.pending is not None or same(held.target(self.heat_read), value):
            return
        call = heat_call(route, config.entity_id, value)
        if call is None:
            return
        kind = KIND_SETTING if route.heat == HEAT_PRESET or route.cloud else KIND_SWITCH
        limit = None if exempt else (SETPOINT_WRITES_DAY if kind == KIND_SETTING else ON_OFF_WRITES_DAY)
        written = part.budget.written(part.day(now), KEY_HEAT, kind)
        if limit is not None and idle and value == route.on and written + 2 + reserve > limit:
            # An idle cycle is only started where its end fits the day's budget too: the heater's own thermostat
            # stands at the bath temperature, so a Ready that could not be ended would heat to it with no bath
            # booked (final check W1). The floor is exempt and still heats.
            part.reason = REASON_WRITE_CAP
            return
        if limit is not None and not idle and value == route.off and written + 2 > limit:
            # For a bath the day's last write is never spent on holding the heat off: the heater is left heating
            # (its own thermostat holds the bath temperature), never parked off below the plan (review S7).
            part.reason = REASON_WRITE_CAP
            return
        if not await part.async_write(KEY_HEAT, call, value, now, kind=kind, limit=limit):
            part.reason = REASON_WRITE_CAP

    async def _async_setpoint(self, now: datetime, config: SpaConfig, route: Route, value: float, *, exempt: bool) -> None:
        part = self.part
        held = part.held(KEY_SET)
        if held.pending is not None or same(held.target(self.set_read), value):
            return
        state = self.hass.states.get(config.entity_id)
        attributes = {} if state is None else state.attributes
        low, high = finite_number(attributes.get("min_temp")), finite_number(attributes.get("max_temp"))
        if high is not None:
            value = min(value, high)
        if low is not None and value < low:
            return
        call = setpoint_call(route, config.entity_id, value)
        if call is None:
            return
        if not await part.async_write(KEY_SET, call, value, now, kind=KIND_SETTING, limit=None if exempt else SETPOINT_WRITES_DAY):
            part.reason = REASON_WRITE_CAP

    async def _async_dial(self, now: datetime, config: SpaConfig | SaunaConfig, route: Route, target_c: float) -> None:
        """The household's temperature written once per change of the dial, within the device's range."""
        part = self.part
        held = part.held(KEY_SET)
        if self.set_read is None or same(self.dial_written, target_c) or held.pending is not None:
            return
        from ..pool.control import to_unit  # noqa: PLC0415

        state = self.hass.states.get(config.entity_id)
        attributes = {} if state is None else state.attributes
        value = to_unit(target_c, self.hass.config.units.temperature_unit)
        low, high = finite_number(attributes.get("min_temp")), finite_number(attributes.get("max_temp"))
        if same(self.set_read, value) or (low is not None and value < low) or (high is not None and value > high):
            self.dial_written = target_c
            return
        call = setpoint_call(route, config.entity_id, value)
        if call is None:
            return
        if await part.async_write(KEY_SET, call, value, now, kind=KIND_SETTING, limit=SETPOINT_WRITES_DAY):
            self.dial_written = target_c
        else:
            part.reason = REASON_WRITE_CAP

    def _bath_today(self, now: datetime, plan: Any) -> bool:
        """A booked bath (not a "now" press) still ahead within this local day."""
        nxt = None if plan is None else getattr(plan, "next_session", None)
        if nxt is None or nxt[1] == SOURCE_NOW or nxt[0] <= now:
            return False
        return self.part.day(nxt[0]) == self.part.day(now)

    def _stale(self, now: datetime) -> bool:
        plan = getattr(self.shadow, "plan", None)
        made = getattr(plan, "made_at", None)
        return isinstance(made, datetime) and now - made > STALE_PLAN

    # ---- exits

    async def async_exit(self, now: datetime, config: SpaConfig | SaunaConfig | None) -> bool:
        """The person's values back where SpotNav's still read; a sauna is never turned on by it."""
        part = self.part
        route = None if config is None else route_of(config.family, config.entity_id)
        if config is None or route is None:
            for held in part.helds.values():
                held.drop(None)
            return True
        self.route = route
        self._read(config)
        sauna = isinstance(config, SaunaConfig)

        def heat(value: Value) -> tuple[str, str, dict[str, Any]] | None:
            if sauna and value != route.off:
                return None
            return heat_call(route, config.entity_id, str(value))

        def setpoint(value: Value) -> tuple[str, str, dict[str, Any]] | None:
            if isinstance(value, str):
                return None
            if isinstance(config, SpaConfig) and value < DEFAULT_FLOOR_C:
                return None
            return setpoint_call(route, config.entity_id, float(value))

        done = await part.async_give_back(KEY_HEAT, self.heat_read, heat, now)
        done = await part.async_give_back(KEY_SET, self.set_read, setpoint, now) and done
        if done:
            self.on_since = self.last_on_look = None
        return done

    # ---- the shed order and the card

    def offers(self, now: datetime, config: SpaConfig | SaunaConfig | None) -> dict[str, Offer]:
        """The live offers of a spa or a sauna SpotNav controls locally (a cloud family never offers one)."""
        from ..site.shed_order import Offer  # noqa: PLC0415

        part = self.part
        route = self.route
        if config is None or not config.control or route is None or route.cloud or part.state != STATE_CONTROLLED:
            return {}
        give = (config.heater_kw or 0.0) * 1000.0
        on = self.heat_read == route.on if route.heat is not None else False
        if route.heat is None:
            # Steered by setpoint only (Gecko): the idle hold is never lowered below itself, so nothing to give.
            return {}
        if isinstance(config, SaunaConfig):
            if self.control.shed_until(LOAD_SAUNA, now) is not None:
                return {LOAD_SAUNA: Offer(live=True, ready=True, give_w=give, drawing=on, held=not on, pending=on and self.part.reason != REASON_WRITE_CAP)}
            return {LOAD_SAUNA: Offer(live=True, ready=on, give_w=give if on else 0.0, drawing=on, why=None if on else "off")}
        plan = self.shadow.plan
        current = self.shadow.reading.current_c
        action = None if plan is None else plan.action
        warm = current is not None and current > DEFAULT_FLOOR_C + 1.0
        out: dict[str, Offer] = {}
        if action in (ACTION_HEAT, ACTION_HOLD):
            if self.control.shed_until(LOAD_SPA, now) is not None:
                out[LOAD_SPA] = Offer(live=True, ready=warm, give_w=give, drawing=on, held=not on, pending=on and self.part.reason != REASON_WRITE_CAP, why=None if warm else "floor")
            else:
                out[LOAD_SPA] = Offer(live=True, ready=on and warm, give_w=give if on else 0.0, drawing=on, why=None if on and warm else ("floor" if on else "off"))
        elif action == ACTION_IDLE:
            if self.control.shed_until(LOAD_SPA_IDLE, now) is not None:
                out[LOAD_SPA_IDLE] = Offer(live=True, ready=warm, give_w=give, drawing=on, held=not on, pending=on and self.part.reason != REASON_WRITE_CAP, why=None if warm else "floor")
            else:
                out[LOAD_SPA_IDLE] = Offer(live=True, ready=on and warm, give_w=give if on else 0.0, drawing=on, why=None if on and warm else ("floor" if on else "off"))
        return out

    def status(self, now: datetime, config: SpaConfig | SaunaConfig) -> dict[str, Any]:
        route = route_of(config.family, config.entity_id)
        found = self.part.status(now, enabled=config.control, live=route is not None and not route.cloud)
        return found

    def diagnostics(self) -> dict[str, Any]:
        return {
            "kept": self.stored(),
            "state": self.part.state,
            "reason": self.part.reason,
            "heat_read": self.heat_read,
            "set_read": self.set_read,
            "on_since": None if self.on_since is None else self.on_since.isoformat(),
        }
