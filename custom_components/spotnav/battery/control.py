"""The shell of the battery's write path (`executor.py`): reads the brand's control entities, makes the writes the
executor returns, persists its state. **Unreachable in this release.**

Nothing constructs `BatteryControl` in production: no service, no setting and no other module refers to it (a test
keeps it so), and every write goes through `_async_write`, which refuses while `shadow.ON_REACHABLE` is off. Tests
drive it against fake entities with `ON_REACHABLE` patched on. A later, explicit opt-in will own one per site and
take its actions from the battery plan and from the hold for the car (`hold.requested_action`), the one route since
wave 7. The shadow shell (`runtime.py`) shares the pure parts only: the role reads (`runtime.read_role_reads`) and the
executor's `preview`; it never imports this module.

The order on every path:

* **Start:** load the stored state; if it holds a snapshot (SpotNav wrote and HA stopped before handing back),
  wait up to `STARTUP_WAIT_S` for its controls' entities and services to load, then restore it before anything else
  (`async_start` runs the hand-back to its end or its deadline).
* **Each look** (`async_tick`): read the battery's facts and the brand's control entities, the site's measurement,
  headroom, import and load balancing, and the plan's running slot; step the executor; **persist the state before
  the writes when it took a new snapshot**; make the writes (a raised service call is reported back as not taken);
  store the state.
* **Stop / unload** (`async_stop`): the executor hands back with that reason; the shell keeps stepping until it is
  done or `HAND_BACK_DEADLINE_S` passes (the state is stored either way, so a restart finishes the restore).
* **Verification** (`async_verify_step`, `verify.py`): the same looks with the routine choosing the action.
* **The export limit** (`export_curtail.py`): a second executor on the brand's export control
  (`commands.EXPORT_CONTROLS`), stepped in the same look: `curtail_export` while the sell price now is below zero
  (`sell_source`, by default the plan shadow's `sell_at`), plain hand-back otherwise. Its plant is found from the
  battery or the grid meter (`runtime.read_export_facts`), so a site without a battery has it too. Its state is
  stored beside the battery's (`export`), restored first after a restart, handed back on stop; the two share the
  day's write cap. While the battery's executor runs the same plant (its Remote EMS on, also while it hands back)
  the export instance is told so (`sibling_active`) and still takes and keeps the limit; Remote EMS on otherwise is
  another controller's and hands the limit back; it is judged on the site's grid power (`grid_import_kw`).
"""

from __future__ import annotations

import asyncio
import logging
import math
from collections import deque
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime
from typing import TYPE_CHECKING, Any, Final

from homeassistant.const import ATTR_RESTORED
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from ..const import (
    CONF_CHARGER_ENTRY_IDS,
    CONF_SOLAR_PRIORITY,
    DEFAULT_SOLAR_PRIORITY,
    DOMAIN,
    SOLAR_PRIORITY_CAR_FIRST,
)
from ..planning.grid_voltage import stored_voltage_between_phases_v
from ..site.measurement_problem import UNHEALTHY_STATES
from . import shadow
from .commands import (
    ACTION_CURTAIL_EXPORT,
    WATCHDOG_S,
    BrandControl,
    control_for,
    export_control_for,
    service_call,
)
from .executor import ExecInputs, ExecState, Executor, Op, RoleRead, StepResult
from .export_curtail import curtails
from .facts import BatteryFacts
from .ledger import Ledger
from .monitor import Capability, Monitor
from .plan_model import ACTION_SELF_CONSUME
from .plan_shadow import slot_at
from .runtime import read_battery_facts, read_export_facts, read_role_reads
from .verify import Sample, Verification, keep_report

if TYPE_CHECKING:
    from ..site.site_capacity_controller import SiteCapacityController

_LOGGER = logging.getLogger(__name__)

STORE_VERSION: Final = 1
#: How long a stop or unload keeps stepping the hand-back before it leaves the rest to the next start (s).
HAND_BACK_DEADLINE_S: Final = 30.0
#: How long the shell waits for an entity to change after a write, between looks during a hand-back (s).
HAND_BACK_LOOK_S: Final = 2.0
LOG_MAX: Final = 200
#: After a restart, how long the restore of a stored snapshot waits for its controls' integration to load (s), and
#: how often it looks.
STARTUP_WAIT_S: Final = 120.0
STARTUP_LOOK_S: Final = 5.0
#: The service each role's method writes with (a service role names its own, `Role.services`).
_METHOD_SERVICES: Final = {
    "number": ("set_value",), "select": ("select_option",), "switch": ("turn_on", "turn_off"), "button": ("press",),
}

#: `(action, battery kW magnitude, slot end)` of the running slot, or `None`.
PlanSource = Callable[[datetime], tuple[str, float, datetime | None] | None]
#: What an exported kWh earns now (minor per kWh), `None` when unknown.
SellSource = Callable[[datetime], float | None]


def store_key(entry_id: str) -> str:
    return f"{DOMAIN}.battery_control.{entry_id}"


class ControlNotReachable(RuntimeError):
    """A look or a write was asked for while `shadow.ON_REACHABLE` is off."""


def _reachable() -> None:
    """Refuse before anything is read, decided or written while no part may write (`shadow.ON_REACHABLE`)."""
    if not shadow.ON_REACHABLE:
        raise ControlNotReachable("no battery control can write in this release")


class BatteryControl:
    """One site's battery write path (see the module). Never constructed in production."""

    def __init__(
        self, hass: HomeAssistant, site: SiteCapacityController, *, plan_source: PlanSource | None = None,
        sell_source: SellSource | None = None, watchdog_s: int = WATCHDOG_S,
    ) -> None:
        self.hass = hass
        self.site = site
        self._store: Store[dict[str, Any]] = Store(hass, STORE_VERSION, store_key(site.entry_id))
        self._plan_source = plan_source
        self._sell_source = sell_source
        self._watchdog_s = watchdog_s
        self.executor: Executor | None = None
        #: The export limit's executor (`commands.EXPORT_CONTROLS`), `None` when the site has none.
        self.export_executor: Executor | None = None
        self._export_failed: tuple[str, ...] = ()
        self.facts = BatteryFacts()
        self.log: deque[dict[str, Any]] = deque(maxlen=LOG_MAX)
        self.reports: dict[str, Any] = {}
        self.verification: Verification | None = None
        self._failed: tuple[str, ...] = ()
        self._stored: dict[str, Any] = {}
        battery_shadow = getattr(site, "battery_shadow", None)
        if battery_shadow is not None:
            # The outside-control watch does not count SpotNav's own writes as someone else's.
            battery_shadow.spotnav_acting = self.acting

    # ---- life

    async def async_start(self) -> None:
        """Load the state; a stored snapshot is handed back before anything else."""
        try:
            stored = await self._store.async_load()
        except Exception:  # noqa: BLE001 - a broken store is an empty one; nothing was written by it
            _LOGGER.warning("The battery control state could not be read")
            stored = None
        self._stored = dict(stored) if isinstance(stored, dict) else {}
        reports = self._stored.get("reports")
        self.reports = dict(reports) if isinstance(reports, dict) else {}
        state = ExecState.from_dict(self._stored.get("state"))
        self.facts = self._read_facts()
        await self._async_start_export()
        control = control_for(state.brand) or control_for(self.facts.brand)
        if control is None:
            return
        self.executor = Executor(
            control, Ledger.from_dict(self._stored.get("ledger")), Monitor.from_dict(self._stored.get("monitor")),
            state, watchdog_s=self._watchdog_s,
        )
        if not shadow.ON_REACHABLE:
            # Nothing writes in such a release; a snapshot here came from a release that did, and is kept for it.
            if state.holds_snapshot:
                _LOGGER.warning("A battery control snapshot is stored but this release cannot write it back")
            return
        if state.holds_snapshot:
            await self._async_wait_for_controls(self.executor)
        now = dt_util.now()
        self._log(self.executor.restart(now))
        if self.executor.state.phase == "restoring":
            await self._async_until_handed_back("restart")

    async def _async_start_export(self) -> None:
        """The export limit's executor from the store; a stored snapshot is handed back first."""
        stored = self._stored.get("export") if isinstance(self._stored.get("export"), dict) else {}
        state = ExecState.from_dict(stored.get("state"))
        control = export_control_for(state.brand) or read_export_facts(self.hass, self.site.config)[2]
        if control is None:
            return
        self.export_executor = Executor(
            control, Ledger.from_dict(stored.get("ledger")), Monitor.from_dict(stored.get("monitor")), state=state,
            watchdog_s=self._watchdog_s,
        )
        if not shadow.ON_REACHABLE:
            if state.holds_snapshot:
                _LOGGER.warning("An export limit snapshot is stored but this release cannot write it back")
            return
        if state.holds_snapshot:
            await self._async_wait_for_controls(self.export_executor)
        self._log(self.export_executor.restart(dt_util.now()))
        if self.export_executor.state.phase == "restoring":
            await self._async_export_until_handed_back("restart")

    def _controls_ready(self, executor: Executor) -> bool:
        """Whether every stored control's entity is loaded (not the registry's restored placeholder) and the service
        that writes it is registered."""
        for name, entity_id in executor.state.entities.items():
            state = self.hass.states.get(entity_id)
            if state is None or state.attributes.get(ATTR_RESTORED):
                return False
            try:
                role = executor.control.role(name)
            except KeyError:
                continue
            services = (
                [tuple(full.split(".", 1)) for full in role.services.values()] if role.method == "service"
                else [(role.method, service) for service in _METHOD_SERVICES.get(role.method, ())]
            )
            if not all(self.hass.services.has_service(domain, service) for domain, service in services):
                return False
        return True

    async def _async_wait_for_controls(self, executor: Executor) -> None:
        """Before a stored snapshot is restored: wait, up to `STARTUP_WAIT_S`, for its controls to load, so an
        integration that loads after SpotNav does not spend the restore's retries."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + STARTUP_WAIT_S
        while not self._controls_ready(executor):
            if loop.time() >= deadline:
                _LOGGER.info("Battery control: restoring before every control has loaded")
                return
            await asyncio.sleep(STARTUP_LOOK_S)

    async def async_stop(self, reason: str = "stop") -> None:
        """Hand the battery and the export limit back now (the part is stopped, the entry unloads)."""
        if not shadow.ON_REACHABLE:
            return
        if self.export_executor is not None:
            await self._async_export_until_handed_back(reason)
        if self.executor is None:
            return
        await self._async_until_handed_back(reason)

    async def async_shutdown(self) -> None:
        await self.async_stop("unload")
        await self._async_save()

    @staticmethod
    async def async_remove_stored(hass: HomeAssistant, entry_id: str) -> None:
        await Store(hass, STORE_VERSION, store_key(entry_id)).async_remove()

    # ---- one look

    async def async_tick(self, now: datetime | None = None, *, stopping: str | None = None) -> StepResult | None:
        _reachable()
        now = now or dt_util.now()
        await self.async_export_tick(now, stopping=stopping)
        executor = self._executor()
        if executor is None:
            return None
        inputs = self.inputs(now, executor.control, stopping=stopping)
        return await self._async_step(executor, inputs)

    async def async_export_tick(self, now: datetime | None = None, *, stopping: str | None = None) -> StepResult | None:
        """One look of the export limit: `curtail_export` while the sell price is below zero, else hand back."""
        _reachable()
        now = now or dt_util.now()
        executor = self._export_executor()
        if executor is None:
            return None
        result = executor.step(self.export_inputs(now, executor.control, stopping=stopping))
        self._log(result.events)
        if result.persist_first:
            await self._async_save()
        failed: list[str] = []
        for op in result.ops:
            if not await self._async_write(op, executor):
                failed.append(op.role)
        self._export_failed = tuple(failed)
        await self._async_save()
        return result

    async def _async_export_until_handed_back(self, reason: str) -> None:
        executor = self.export_executor
        assert executor is not None
        loop = asyncio.get_running_loop()
        deadline = loop.time() + HAND_BACK_DEADLINE_S
        stopping: str | None = reason
        while True:
            result = await self.async_export_tick(stopping=stopping)
            stopping = None if executor.state.phase == "restoring" else stopping
            if result is None or executor.state.phase in ("idle", "blocked") or loop.time() >= deadline:
                return
            await asyncio.sleep(0 if result.ops else HAND_BACK_LOOK_S)

    async def async_verify_step(self, now: datetime | None = None) -> StepResult | None:
        """One look of the verification routine; it starts one when none runs. Built, not callable from HA."""
        _reachable()
        now = now or dt_util.now()
        executor = self._executor()
        if executor is None:
            return None
        if self.verification is None or self.verification.done:
            self.verification = Verification(executor, now, firmware=self.facts.firmware)
        verification = self.verification
        inputs = self.inputs(now, executor.control)
        sample = Sample(
            inputs.facts.power_kw, inputs.battery_age_s, inputs.facts.soc_pct, inputs.house_deficit_kw
        )
        action, rate = verification.command(now, sample, inputs.facts.max_charge_kw)
        inputs = replace(inputs, action=action, rate_kw=rate, slot_end=None)
        result = await self._async_step(executor, inputs)
        capability = Capability(
            rated_charge_kw=inputs.facts.max_charge_kw,
            rated_discharge_kw=inputs.facts.max_discharge_kw,
            soc_min_pct=inputs.facts.reserve_soc_pct or 0.0,
        )
        verification.after(now, result, sample, capability)
        if verification.done:
            self.reports = keep_report(self.reports, verification.report())
            await self._async_save()
        return result

    async def _async_step(self, executor: Executor, inputs: ExecInputs) -> StepResult:
        result = executor.step(inputs)
        self._failed = ()
        self._log(result.events)
        if result.persist_first:
            # The snapshot is on disk before the first write, so a crash in between still restores it.
            await self._async_save()
        failed: list[str] = []
        for op in result.ops:
            if not await self._async_write(op, executor):
                failed.append(op.role)
        self._failed = tuple(failed)
        await self._async_save()
        return result

    async def _async_until_handed_back(self, reason: str) -> None:
        executor = self.executor
        assert executor is not None
        loop = asyncio.get_running_loop()
        deadline = loop.time() + HAND_BACK_DEADLINE_S
        stopping: str | None = reason
        while True:
            now = dt_util.now()
            battery = self.executor
            result = None if battery is None else await self._async_step(
                battery, self.inputs(now, battery.control, stopping=stopping)
            )
            stopping = None if executor.state.phase == "restoring" else stopping
            if result is None or executor.state.phase in ("idle", "blocked") or loop.time() >= deadline:
                return
            await asyncio.sleep(0 if result.ops else HAND_BACK_LOOK_S)

    # ---- reads

    def _executor(self) -> Executor | None:
        if self.executor is None:
            self.facts = self._read_facts()
            control = control_for(self.facts.brand)
            if control is None:
                return None
            self.executor = Executor(control, watchdog_s=self._watchdog_s)
        return self.executor

    def _battery_power(self) -> tuple[float | None, float | None]:
        reading = self.site.battery_aggregate_power()
        if reading is None or reading.value is None:
            return None, None
        ages = [age for age in (reading.age_s, reading.report_age_s) if age is not None]
        return reading.value / 1000.0, min(ages) if ages else None

    def _read_facts(self) -> BatteryFacts:
        power_kw, _age = self._battery_power()
        return self._with_outside(read_battery_facts(self.hass, self.site.config, power_kw))

    def _with_outside(self, facts: BatteryFacts) -> BatteryFacts:
        """A battery the site's outside-control watch marks is another system's: every look refuses it."""
        battery_shadow = getattr(self.site, "battery_shadow", None)
        return facts if battery_shadow is None else battery_shadow.with_outside(facts)

    def acting(self) -> bool:
        """Whether this path acts on the battery or the export limit now (its writes are no one else's)."""
        return any(
            executor is not None and executor.state.phase in ("active", "restoring")
            for executor in (self.executor, self.export_executor)
        )

    def _export_executor(self) -> Executor | None:
        if self.export_executor is None:
            control = read_export_facts(self.hass, self.site.config)[2]
            if control is None:
                return None
            self.export_executor = Executor(control, watchdog_s=self._watchdog_s)
        return self.export_executor

    def _sell_now(self, now: datetime) -> float | None:
        if self._sell_source is not None:
            return self._sell_source(now)
        return self.site.battery_shadow.plan.sell_at(now)

    def export_inputs(self, now: datetime, control: BrandControl, *, stopping: str | None = None) -> ExecInputs:
        """The export limit's look: the plant's facts and control, the site's measurement, the action from the sell
        price now, and the battery path's writes today (the cap is shared)."""
        facts, reads, _control = read_export_facts(self.hass, self.site.config)
        executor = self.export_executor
        if executor is not None:
            for name, entity_id in executor.state.entities.items():
                if name not in reads:
                    state = self.hass.states.get(entity_id)
                    reads[name] = RoleRead(entity_id=entity_id, state=None if state is None else state.state,
                                           found=state is not None, enabled=state is not None)
        result = self.site.result
        headrooms = [value for value in result.phase_headroom_a.values() if value is not None]
        battery = self.executor
        # SpotNav's own battery control on the same plant: its Remote EMS is not the plant's own management, also
        # while it hands back (Remote EMS goes off last) or keeps a snapshot it could not restore yet.
        sibling_active = battery is not None and battery.state.brand == control.brand and (
            battery.state.phase in ("active", "restoring") or battery.state.holds_snapshot
        )
        return ExecInputs(
            now=now,
            facts=self._with_outside(facts),
            reads=reads,
            action=ACTION_CURTAIL_EXPORT if curtails(self._sell_now(now)) else ACTION_SELF_CONSUME,
            measurement_ok=result.state not in UNHEALTHY_STATES and result.state != "not_configured",
            headroom_a=min(headrooms) if headrooms else None,
            volts_between_phases=stored_voltage_between_phases_v(self.site.config),
            stopping=stopping,
            failed=self._export_failed,
            other_writes_today=0 if battery is None or battery.state.day != now.date().isoformat()
            else battery.state.writes_today,
            grid_import_kw=self._grid_import_kw(),
            sibling_active=sibling_active,
        )

    def _grid_import_kw(self) -> float | None:
        """The site's grid power (kW, + import) from its signed phase powers, `None` when one is not known."""
        signed = list(self.site.result.phase_signed_active_power_w.values())
        if not signed or not all(value is not None and math.isfinite(value) for value in signed):
            return None
        return sum(signed) / 1000.0  # type: ignore[arg-type]

    def _reads(self, control: BrandControl) -> dict[str, RoleRead]:
        reads = read_role_reads(self.hass, self.site.config, control)
        executor = self.executor
        if executor is not None:
            # After a restart the stored entities are read even if the registry lookup fails.
            for name, entity_id in executor.state.entities.items():
                if name not in reads:
                    state = self.hass.states.get(entity_id)
                    reads[name] = RoleRead(entity_id=entity_id, state=None if state is None else state.state,
                                           found=state is not None, enabled=state is not None,
                                           device_id=executor.state.device_id)
        return reads

    def _plan_now(self, now: datetime) -> tuple[str, float, datetime | None] | None:
        if self._plan_source is not None:
            return self._plan_source(now)
        plan = self.site.battery_shadow.plan
        slot = slot_at(plan.plan, now)
        return None if slot is None else (slot.action, abs(slot.battery_kw), slot.end)

    def inputs(self, now: datetime, control: BrandControl, *, stopping: str | None = None) -> ExecInputs:
        power_kw, age_s = self._battery_power()
        self.facts = facts = self._with_outside(read_battery_facts(self.hass, self.site.config, power_kw))
        result = self.site.result
        headrooms = [value for value in result.phase_headroom_a.values() if value is not None]
        grid_import_kw = self._grid_import_kw()
        config = self.site.config
        car_held = any(
            self.site.load_balancing_limit(charger)[0] is not None for charger in config.get(CONF_CHARGER_ENTRY_IDS) or []
        )
        plan = self._plan_now(now)
        return ExecInputs(
            now=now,
            facts=facts,
            reads=self._reads(control),
            action=None if plan is None else plan[0],
            rate_kw=0.0 if plan is None else plan[1],
            slot_end=None if plan is None else plan[2],
            battery_age_s=age_s,
            measurement_ok=result.state not in UNHEALTHY_STATES and result.state != "not_configured",
            headroom_a=min(headrooms) if headrooms else None,
            volts_between_phases=stored_voltage_between_phases_v(config),
            grid_import_kw=grid_import_kw,
            car_first=config.get(CONF_SOLAR_PRIORITY, DEFAULT_SOLAR_PRIORITY) == SOLAR_PRIORITY_CAR_FIRST,
            car_held=car_held,
            stopping=stopping,
            failed=self._failed,
            other_writes_today=self._export_writes_today(now),
        )

    def _export_writes_today(self, now: datetime) -> int:
        executor = self.export_executor
        if executor is None or executor.state.day != now.date().isoformat():
            return 0
        return executor.state.writes_today

    # ---- writes

    async def _async_write(self, op: Op, executor: Executor) -> bool:
        """One write; `False` when the service call raised (the executor counts it as not taken)."""
        _reachable()
        domain, service, data = service_call(
            op.method, op.entity_id, op.value, device_id=op.device_id,  # type: ignore[arg-type]
            services=executor.control.role(op.role).services, watchdog_s=self._watchdog_s,
        )
        try:
            await self.hass.services.async_call(domain, service, data, blocking=True)
        except Exception as err:  # noqa: BLE001 - any error (a Modbus library's too) is a write not taken; the state
            # must still be stored and a stop or unload must still hand back.
            _LOGGER.warning("Battery control: %s.%s for %s failed: %s", domain, service, op.role, err)
            self._log([{"at": dt_util.now().isoformat(), "kind": "service_failed", "reason": op.role,
                        "error": str(err)}])
            return False
        return True

    # ---- storage and what it shows

    def _log(self, events: Any) -> None:
        for event in events:
            self.log.append(dict(event))
            _LOGGER.debug("Battery control: %s %s", event.get("kind"), event.get("reason"))

    async def _async_save(self) -> None:
        executor = self.executor
        data: dict[str, Any] = {"reports": dict(self.reports)}
        if executor is not None:
            data |= {
                "state": executor.state.as_dict(),
                "ledger": executor.ledger.as_dict(),
                "monitor": executor.monitor.as_dict(),
            }
        export = self.export_executor
        if export is not None:
            data["export"] = {
                "state": export.state.as_dict(), "ledger": export.ledger.as_dict(), "monitor": export.monitor.as_dict(),
            }
        await self._store.async_save(data)

    def snapshot(self) -> dict[str, Any]:
        executor = self.executor
        return {
            "reachable": shadow.ON_REACHABLE,
            "brand": None if executor is None else executor.control.brand,
            "executor": None if executor is None else executor.as_dict(),
            "export": None if self.export_executor is None else self.export_executor.as_dict(),
            "verification": None if self.verification is None else self.verification.report(),
            "reports": dict(self.reports),
            "log": list(self.log),
        }
