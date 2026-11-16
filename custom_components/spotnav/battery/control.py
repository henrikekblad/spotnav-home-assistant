"""The shell of the battery's write path (`executor.py`): reads the brand's control entities, makes the writes the
executor returns, persists its state. One per site, owned by its `SiteCapacityController`.

**The gate** (`gate.py`): nothing is written unless the site's options say so: `battery_control_enabled` (the
person's opt-in, off by default) and `battery_control_shadow_only` off (on by default until the owner's on-site
tests are done). With both, enabling starts the verification routine (`verify.py`); control runs the plan only once
the routine passed for the plant's current firmware and integration version (`gate.firmware_key`), and only the
actions the report allows (`gate.allowed_actions`). While shadow only, the shell logs what it would do
(`would_do`, `executor.preview`) and writes nothing. One kind of write is never refused: handing back what SpotNav
itself wrote (a restore), on every path, so turning anything off can always put the plant back.

The order on every path:

* **Start** (`async_launch`, a background task, never awaited in the site's setup: the restore may wait up to
  `STARTUP_WAIT_S` for the controls' integration): load the stored state; a stored snapshot (SpotNav wrote and HA
  stopped before handing back) is restored before anything else, after waiting for its controls' entities and
  services to load. Only then does the look timer (`TICK_S`) start. A start that raises is tried again
  (`START_RETRY_S`, doubling); an unload while it has not finished still hands back what its executors hold.
* **Each look** (`async_look`, never raises): the owner's test action (`async_trial`) if one runs; else the
  verification routine if one runs; else the plan's running slot merged with the hold for the car
  (`gate.merged_action`), restricted to the allowed actions and the grid charge cap (`gate.restricted`), when
  control is on; else a hand-back of anything SpotNav holds. The export limit beside it: on its live check whenever
  the options allow writing (no routine of its own, owner 2026-11-16), handed back otherwise. Each executor step:
  read the battery's facts and the brand's control entities, the site's measurement, headroom, import and load
  balancing; step the executor; **persist the state before the writes when it took a new snapshot**; make the writes
  (a raised service call is reported back as not taken); store the state.
* **Stop / unload** (`async_stop`, `async_shutdown`): the executor hands back with that reason; the shell keeps
  stepping until it is done or `HAND_BACK_DEADLINE_S` passes (the state is stored either way, so a restart finishes
  the restore). The site shuts this down first, before its other parts.
* **Verification**: started at the first suitable moment after enabling (`gate.suitable`: a discharging battery
  between 30 and 80 %, no car charging, the plant SpotNav's to take), a mode that could not be judged is tried again
  at a later one; after a firmware or integration change the plant is handed back and checked again at the next
  suitable evening. The report is kept per firmware key.
* **Sigenergy's AI mode** (owner 2026-11-16): taken over only with `battery_control_ai_takeover_ack`; whenever
  control stops running after such a takeover (turned off, back to shadow only, a failed check, a firmware change,
  the acknowledgement withdrawn) a note asks the person to set AI mode again in the app (the hand-back leaves the
  plant in Maximum Self Consumption); seeing AI mode again ends the note and the takeover. Without the
  acknowledgement a plant in AI mode is only watched.
* **The export limit** (`export_curtail.py`): a second executor on the brand's export control
  (`commands.EXPORT_CONTROLS`): `curtail_export` while the sell price now is below zero (`sell_source`, by default
  the plan shadow's `sell_at`), plain hand-back otherwise. Its plant is found from the battery or the grid meter
  (`runtime.read_export_facts`), so a site without a battery has it too. Its state is stored beside the battery's
  (`export`), restored after the battery's after a restart and handed back after it on stop; the two share the day's write cap. While the
  battery's executor runs the same plant (its Remote EMS on, also while it hands back) the export instance is told so
  (`sibling_active`) and still takes and keeps the limit; Remote EMS on otherwise is another controller's and hands
  the limit back; it is judged on the site's grid power (`grid_import_kw`).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Final

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_RESTORED
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.loader import async_get_loaded_integration
from homeassistant.util import dt as dt_util

from ..const import (
    CONF_CHARGER_ENTRY_IDS,
    CONF_MAIN_FUSE_A,
    CONF_SOLAR_PRIORITY,
    DEFAULT_SOLAR_PRIORITY,
    DOMAIN,
    SOLAR_PRIORITY_CAR_FIRST,
)
from ..planning.grid_voltage import stored_voltage_between_phases_v
from ..site.measurement_problem import UNHEALTHY_STATES
from ..site.phase_peaks import Learned
from . import gate
from .commands import (
    ACTION_CURTAIL_EXPORT,
    OUTPUT_TYPE_PHASES,
    WATCHDOG_S,
    BrandControl,
    control_for,
    export_control_for,
    service_call,
)
from .executor import (
    HEADROOM_MARGIN_A,
    ExecInputs,
    ExecState,
    Executor,
    Op,
    RoleRead,
    StepResult,
    preview,
    taken_over,
)
from .export_curtail import curtails
from .facts import BatteryFacts
from .ledger import Ledger
from .monitor import Capability, Monitor
from .plan_model import (
    ACTION_DISCHARGE_HOUSE,
    ACTION_EXPORT_DISCHARGE,
    ACTION_GRID_CHARGE,
    ACTION_HOLD,
    ACTION_SELF_CONSUME,
)
from .plan_shadow import slot_at
from .runtime import TICK_S, read_battery_facts, read_export_facts, read_plant_entity, read_role_reads
from .verify import TEST_CHARGE_KW, Sample, Verification

if TYPE_CHECKING:
    from ..site.site_capacity_controller import SiteCapacityController

_LOGGER = logging.getLogger(__name__)

STORE_VERSION: Final = 1
#: How long a stop or unload keeps stepping the hand-back before it leaves the rest to the next start (s).
HAND_BACK_DEADLINE_S: Final = 30.0
#: How long the shell waits for an entity to change after a write, between looks during a hand-back (s).
HAND_BACK_LOOK_S: Final = 2.0
#: How long an unload waits for a look in progress before it cancels it (s).
LOOK_WAIT_S: Final = 10.0
LOG_MAX: Final = 200
#: Reports kept, one per firmware key.
MAX_REPORTS: Final = 10
#: After a restart, how long the restore of a stored snapshot waits for its controls' integration to load (s), and
#: how often it looks.
STARTUP_WAIT_S: Final = 120.0
STARTUP_LOOK_S: Final = 5.0
#: A start that raised is tried again after this long, doubled each time up to the most (s).
START_RETRY_S: Final = 30.0
START_RETRY_MAX_S: Final = 600.0
#: How long after a hand-back the plant's changes still count as SpotNav's own (s): the outside-control watch
#: (`acting`) must not take a sensor that shows the hand-back late for someone else's change.
ACTING_GRACE_S: Final = 300.0
#: The longest test action (min).
TRIAL_MAX_MIN: Final = 120
#: Looks in a row that may fail while SpotNav holds the plant before it hands back on the fewest reads.
FAILED_LOOKS_HAND_BACK: Final = 3
#: The service each role's method writes with (a service role names its own, `Role.services`).
_METHOD_SERVICES: Final = {
    "number": ("set_value",), "select": ("select_option",), "switch": ("turn_on", "turn_off"), "button": ("press",),
}
#: Every action control may run when the report allows it; selling only with `battery_control_allow_sell`.
_PLAN_ACTIONS: Final = frozenset({ACTION_HOLD, ACTION_GRID_CHARGE, ACTION_DISCHARGE_HOUSE})
#: The note shown after control is turned off on a plant taken over from AI mode.
NOTE_AI_MODE_BACK: Final = "ai_mode_back"

#: `(action, battery kW magnitude, slot end)` of the running slot, or `None`.
PlanSource = Callable[[datetime], tuple[str, float, datetime | None] | None]
#: What an exported kWh earns now (minor per kWh), `None` when unknown.
SellSource = Callable[[datetime], float | None]

_UNSET: Final = object()


def store_key(entry_id: str) -> str:
    return f"{DOMAIN}.battery_control.{entry_id}"


def _float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def stored_reads(hass: HomeAssistant, executor: Executor) -> dict[str, RoleRead]:
    """Each control the executor wrote, read by its stored entity (no registry lookup)."""
    reads: dict[str, RoleRead] = {}
    for name, entity_id in executor.state.entities.items():
        state = hass.states.get(entity_id)
        reads[name] = RoleRead(entity_id=entity_id, state=None if state is None else state.state,
                               found=state is not None, enabled=state is not None, device_id=executor.state.device_id)
    return reads


async def _async_call(hass: HomeAssistant, op: Op, executor: Executor, watchdog_s: int) -> bool:
    """One write as a service call; `False` when it raised (the executor counts it as not taken)."""
    domain, service, data = service_call(
        op.method, op.entity_id, op.value, device_id=op.device_id,  # type: ignore[arg-type]
        services=executor.control.role(op.role).services, watchdog_s=watchdog_s,
    )
    try:
        await hass.services.async_call(domain, service, data, blocking=True)
    except Exception as err:  # noqa: BLE001 - any error (a Modbus library's too) is a write not taken
        _LOGGER.warning("Battery control: %s.%s for %s failed: %s", domain, service, op.role, err)
        return False
    return True


async def async_hand_back_stored(
    hass: HomeAssistant, config: dict[str, Any], data: dict[str, Any], *, deadline_s: float | None = None
) -> bool:
    """Hand back the snapshots a stored state holds (the battery's, then the export limit's) without a site: only
    restores, on the plant's own reads. `data` is updated in place; answers whether anything is still held."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + (HAND_BACK_DEADLINE_S if deadline_s is None else deadline_s)
    held = False
    for key in ("battery", "export"):
        part = data if key == "battery" else data.get("export")
        if not isinstance(part, dict):
            continue
        state = ExecState.from_dict(part.get("state"))
        control = (control_for if key == "battery" else export_control_for)(state.brand)
        if control is None or not state.holds_snapshot:
            continue
        executor = Executor(control, Ledger.from_dict(part.get("ledger")), Monitor.from_dict(part.get("monitor")), state)
        executor.restart(dt_util.now())
        failed: tuple[str, ...] = ()
        while executor.state.phase == "restoring" and loop.time() < deadline:
            if key == "battery":
                facts = read_battery_facts(hass, config, None)
                reads = {**stored_reads(hass, executor), **read_role_reads(hass, config, control)}
            else:
                facts, reads, _control = read_export_facts(hass, config)
                reads = {**stored_reads(hass, executor), **reads}
            result = executor.step(ExecInputs(
                now=dt_util.now(), facts=facts, reads=reads, measurement_ok=False, stopping="removed", failed=failed,
            ))
            failed = tuple([
                op.role for op in result.ops if op.restore and not await _async_call(hass, op, executor, WATCHDOG_S)
            ])
            if executor.state.phase != "restoring":
                break
            await asyncio.sleep(0 if result.ops else HAND_BACK_LOOK_S)
        part |= {"state": executor.state.as_dict(), "ledger": executor.ledger.as_dict(),
                 "monitor": executor.monitor.as_dict()}
        held = held or executor.state.holds_snapshot
    return held


class ControlNotReachable(RuntimeError):
    """A look or a write was asked for that the site's gate does not allow now."""


class TrialRefused(ValueError):
    """The owner's test action cannot run: `code` says why (a stable code for the service's error)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class Trial:
    """The owner's test action (`spotnav.battery_control_test`): one action through the executor until `until`."""

    action: str
    rate_kw: float
    started_at: datetime
    until: datetime
    force: bool

    @property
    def export(self) -> bool:
        return self.action == ACTION_CURTAIL_EXPORT

    def as_dict(self) -> dict[str, Any]:
        return {"action": self.action, "rate_kw": self.rate_kw, "started_at": self.started_at.isoformat(),
                "until": self.until.isoformat(), "force_test": self.force}


class BatteryControl:
    """One site's battery and export-limit write path (see the module)."""

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
        #: The verification reports by firmware key (`gate.merge_report`).
        self.reports: dict[str, Any] = {}
        self.verification: Verification | None = None
        #: The key and attempt the running verification counts for.
        self._verify_key: str | None = None
        self._verify_attempt = 0
        #: The last firmware key read (kept while the firmware reads nothing), whether the opt-in was on at the last
        #: look, whether SpotNav took the plant over from its own optimiser since, and the notes for the card.
        self._key: str | None = None
        self._enabled_seen = False
        self.took_from_ai = False
        self.notes: list[dict[str, Any]] = []
        #: Whether control ran (on, verifying or a test action) at the end of the last look (`_note_stopped`).
        self._running = False
        self.trial: Trial | None = None
        #: Why the routine did not start at the last look (`gate.suitable`), and the last would-do logged.
        self.waiting_for: str | None = None
        self._would_do: tuple[Any, ...] | None = None
        self._restricted: str | None = None
        self._failed: tuple[str, ...] = ()
        self._stored: dict[str, Any] = {}
        #: What was stored last (`_async_save` writes only a change).
        self._saved: dict[str, Any] | None = None
        self._lock = asyncio.Lock()
        self._started = False
        self._closed = False
        self._shut_down = False
        self._start_task: asyncio.Task[None] | None = None
        self._look_task: asyncio.Task[None] | None = None
        self._timer_cancel: Callable[[], None] | None = None
        self._look_failures: set[str] = set()
        #: Looks that failed in a row (`FAILED_LOOKS_HAND_BACK`).
        self._failed_looks = 0
        #: The house's p90 for the running hour (`_house_peak_kw`), read once an hour.
        self._peak: tuple[datetime, float | None] | None = None
        battery_shadow = getattr(site, "battery_shadow", None)
        if battery_shadow is not None:
            # The outside-control watch does not count SpotNav's own writes as someone else's, and the shadow hold
            # judges a plant in AI mode as control would.
            battery_shadow.spotnav_acting = self.acting
            battery_shadow.takeover = lambda: self.options().ai_takeover_ack

    # ---- life

    @callback
    def async_launch(self, entry: ConfigEntry | None) -> None:
        """Start in the background: the restore first, then the look timer. Never awaited by the site's setup."""
        name = f"spotnav_battery_control_start_{self.site.entry_id}"
        coro = self._async_start_then_look()
        self._start_task = (
            self.hass.async_create_background_task(coro, name) if entry is None
            else entry.async_create_background_task(self.hass, coro, name)
        )

    async def _async_start_then_look(self) -> None:
        delay = START_RETRY_S
        while True:
            try:
                await self.async_start()
                break
            except Exception:  # noqa: BLE001 - the site must run on; the start is tried again (a stored snapshot
                # must be handed back)
                self._look_failed("start")
            if self._closed:
                return
            await asyncio.sleep(delay)
            delay = min(delay * 2, START_RETRY_MAX_S)
            if self._closed:
                return
        if self._closed:
            return
        self._timer_cancel = async_track_time_interval(self.hass, self._on_timer, timedelta(seconds=TICK_S))

    @callback
    def _on_timer(self, _now: datetime) -> None:
        if self._closed or (self._look_task is not None and not self._look_task.done()):
            return  # a look still runs (a slow write): this one is skipped, the next timer comes
        self._look_task = self.hass.async_create_background_task(
            self.async_look(), f"spotnav_battery_control_look_{self.site.entry_id}"
        )

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
        self._load_gate(self._stored.get("gate"))
        state = ExecState.from_dict(self._stored.get("state"))
        self.facts = self._read_facts()
        # The battery first (a grid charge left standing adds import), then the export limit.
        control = control_for(state.brand) or control_for(self.facts.brand)
        if control is not None:
            self.executor = Executor(
                control, Ledger.from_dict(self._stored.get("ledger")), Monitor.from_dict(self._stored.get("monitor")),
                state, watchdog_s=self._watchdog_s,
            )
            if state.holds_snapshot:
                await self._async_wait_for_controls(self.executor)
            self._log(self.executor.restart(dt_util.now()))
            if self.executor.state.phase == "restoring":
                await self._async_until_handed_back("restart")
        await self._async_start_export()
        self._started = True

    def _load_gate(self, raw: Any) -> None:
        data = raw if isinstance(raw, dict) else {}
        key = data.get("key")
        self._key = key if isinstance(key, str) else None
        self._enabled_seen = data.get("enabled_seen") is True
        self.took_from_ai = data.get("took_from_ai") is True
        notes = data.get("notes")
        self.notes = [dict(note) for note in notes if isinstance(note, dict)] if isinstance(notes, list) else []

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
        """Hand the battery and the export limit back now (control turned off, the entry unloads). Only restores are
        written, whatever the gate says."""
        async with self._lock:
            await self._async_stop(reason)

    async def _async_stop(self, reason: str) -> None:
        self.trial = None
        self._end_verification(dt_util.now())
        # The battery first (a grid charge adds import; a lower export limit never does), then the export limit.
        if self.executor is not None:
            await self._async_until_handed_back(reason)
        if self.export_executor is not None:
            await self._async_export_until_handed_back(reason)

    async def async_shutdown(self) -> None:
        """The site unloads or Home Assistant stops: before the rest of the site, the timer ends, a start still
        waiting is cancelled (its snapshot stays stored for the next start), a look in progress ends, then the
        hand-back runs, bounded. Once only: a second call (the site's own shutdown after Home Assistant's stop handed
        every battery back) returns at once."""
        if self._shut_down:
            return
        self._shut_down = True
        self._closed = True
        if self._timer_cancel is not None:
            self._timer_cancel()
            self._timer_cancel = None
        start = self._start_task
        if start is not None and not start.done():
            start.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await start
        look = self._look_task
        if look is not None and not look.done():
            _done, pending = await asyncio.wait({look}, timeout=LOOK_WAIT_S)
            for task in pending:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        if self._started or self._holds_anything():
            # Also when the start did not finish: an executor it made from the store may hold a snapshot.
            try:
                async with asyncio.timeout(HAND_BACK_DEADLINE_S * 2 + 5.0):
                    await self.async_stop("unload")
            except TimeoutError:
                _LOGGER.warning("SpotNav battery control: the hand-back on unload did not finish; it is finished at the next start")
            except Exception:  # noqa: BLE001 - the rest of the site must still shut down; the state is stored
                _LOGGER.warning("SpotNav battery control: the hand-back on unload failed", exc_info=True)
        with contextlib.suppress(Exception):
            await self._async_save()

    @staticmethod
    async def async_remove_stored(hass: HomeAssistant, entry_id: str, config: dict[str, Any] | None = None) -> None:
        """The site entry is deleted: a snapshot still stored (its unload's hand-back did not finish) is handed back
        first, on the site's `config`; the store goes only once nothing is held, else it is kept and a warning
        logged (its restore values are not thrown away)."""
        store: Store[dict[str, Any]] = Store(hass, STORE_VERSION, store_key(entry_id))
        try:
            stored = await store.async_load()
        except Exception:  # noqa: BLE001 - a broken store holds nothing that can be handed back
            stored = None
        data = dict(stored) if isinstance(stored, dict) else {}
        try:
            held = await async_hand_back_stored(hass, config or {}, data)
        except Exception:  # noqa: BLE001
            _LOGGER.warning("SpotNav battery control: the hand-back of a removed site failed", exc_info=True)
            held = True
        if held:
            _LOGGER.warning(
                "SpotNav battery control: the removed site's home battery could not be handed back; its stored"
                " state (%s) is kept", store_key(entry_id),
            )
            await store.async_save(data)
            return
        await store.async_remove()

    # ---- the gate

    def options(self) -> gate.Options:
        """The site's options, from its entry as stored now (a settings write needs no reload)."""
        entry = self.hass.config_entries.async_get_entry(self.site.entry_id)
        return gate.Options.from_config(entry.data if entry is not None else self.site.config)

    def _integration_version(self, domain: str | None) -> str | None:
        if not domain:
            return None
        try:
            version = async_get_loaded_integration(self.hass, domain).version
        except Exception:  # noqa: BLE001 - not loaded (yet): the version is unknown
            return None
        return None if version is None else str(version)

    def firmware_key(self) -> str:
        """The plant's firmware and integration version as a report key; the last one read while the firmware reads
        nothing (a reading that blinks must not hand the plant back)."""
        facts = self.facts
        version = self._integration_version(facts.integration)
        if facts.firmware and (
            version is not None or self._key is None or not self._key.startswith(f"{facts.firmware} |")
        ):
            # An integration not loaded (yet) is no change of version: the key read before is kept.
            self._key = gate.firmware_key(facts.firmware, facts.integration, version)
        return self._key or gate.firmware_key(None, facts.integration, version)

    def firmware_known(self) -> bool:
        """Whether the plant's firmware was ever read (the last reading is kept while it blinks): without it a later
        firmware change could not be seen, so no check runs and no report counts."""
        return gate.firmware_known(self.firmware_key())

    def report(self) -> dict[str, Any] | None:
        if not self.firmware_known():
            return None
        report = self.reports.get(self.firmware_key())
        return report if isinstance(report, dict) else None

    def state(self, options: gate.Options | None = None) -> str:
        """`off`, `shadow_only`, `verifying`, `waiting` (for the routine), `on` or `failed`."""
        options = options or self.options()
        if not options.enabled:
            return "off"
        if options.shadow_only:
            return "shadow_only"
        if self.verification is not None and not self.verification.done:
            return "verifying"
        status = gate.report_status(self.report())
        return {"passed": "on", "failed": "failed"}.get(status, "waiting")

    def allowed(self, options: gate.Options | None = None) -> frozenset[str]:
        options = options or self.options()
        if self.state(options) != "on":
            return frozenset()
        return gate.allowed_actions(self.report(), options)

    def _require(self, ok: bool, what: str) -> None:
        if not ok:
            raise ControlNotReachable(f"{what} is not allowed by the site's battery control settings now")

    # ---- one look

    async def async_look(self, now: datetime | None = None) -> None:
        """One look (see the module). Never raises into the site."""
        if self._closed or not self._started:
            return
        async with self._lock:
            now = now or dt_util.now()
            try:
                await self._async_look(now)
            except Exception:  # noqa: BLE001 - the battery control must never disturb the site; it looks again
                self._look_failed("look")
                self._failed_looks += 1
                if self._failed_looks >= FAILED_LOOKS_HAND_BACK:
                    await self._async_hand_back_after_failures(now)
            else:
                self._failed_looks = 0

    async def _async_hand_back_after_failures(self, now: datetime) -> None:
        """Looks keep failing while SpotNav may hold the plant: hand back what it holds on the fewest reads (one
        step per look), so a part that keeps raising cannot leave a command standing."""
        if not self._holds_anything():
            return
        self.trial = None
        with contextlib.suppress(Exception):
            self._end_verification(now)
        try:
            if self.executor is not None and self._holding(self.executor):
                await self._async_step(
                    self.executor, self._minimal_inputs(now, self.executor, "look_failed"), allowed=False
                )
            if self.export_executor is not None and self._holding(self.export_executor):
                await self._async_export_raw(now, self.export_executor, None, stopping="look_failed", allowed=False)
        except Exception:  # noqa: BLE001 - tried again at the next failed look
            self._look_failed("hand_back_after_failures")

    @staticmethod
    def _holding(executor: Executor) -> bool:
        return executor.state.phase in ("active", "restoring") or executor.state.holds_snapshot

    def _holds_anything(self) -> bool:
        return any(executor is not None and self._holding(executor) for executor in (self.executor, self.export_executor))

    def _look_failed(self, kind: str) -> None:
        if kind in self._look_failures:
            _LOGGER.debug("SpotNav battery control: the %s failed again", kind, exc_info=True)
            return
        self._look_failures.add(kind)
        _LOGGER.warning("SpotNav battery control: the %s failed; it is tried again at each look", kind, exc_info=True)

    async def _async_look(self, now: datetime) -> None:
        options = self.options()
        self.facts = self._read_facts()
        self._transitions(options, now)
        trial = self.trial
        withdrawn = self._takeover_withdrawn(options)
        if trial is not None and (
            now >= trial.until or not self._trial_allowed(trial, options) or (withdrawn and not trial.export)
        ):
            self._log([{"at": now.isoformat(), "kind": "trial_ended", "reason": trial.action}])
            self.trial = trial = None
        # The battery.
        if withdrawn:
            # The person withdrew the acknowledgement after SpotNav took the plant over from AI mode: handed back,
            # and nothing more until AI mode is seen again or the acknowledgement is given again.
            if self.verification is not None and not self.verification.done:
                self._end_verification(now)
            await self._async_release(now, "ai_takeover_withdrawn")
        elif trial is not None and not trial.export:
            await self._async_battery_step(now, self._trial_slot(now, trial, options), allowed=True)
        elif self.verification is not None and not self.verification.done:
            if options.writes:
                await self._async_verify(now)
            else:
                self._end_verification(now)
                await self._async_release(now, "disabled")
        else:
            state = self.state(options)
            if state == "on":
                await self._async_battery_step(now, self._control_slot(now, options), allowed=True)
            else:
                await self._async_release(now, "shadow_only" if state == "shadow_only" else f"control_{state}")
            if options.writes and trial is None:
                await self._async_maybe_verify(now, options)
        # The export limit.
        if trial is not None and trial.export:
            await self._async_export_step(now, ACTION_CURTAIL_EXPORT, allowed=True)
        elif options.writes:
            await self._async_export_step(now, None, allowed=True)
        else:
            await self._async_export_release(now, "shadow_only" if options.enabled else "control_off")
        if options.enabled and options.shadow_only:
            self._shadow_look(now, options)
        self._note_stopped(now)
        await self._async_save()

    def _takeover_withdrawn(self, options: gate.Options) -> bool:
        return self.took_from_ai and not options.ai_takeover_ack

    def _note_stopped(self, now: datetime) -> None:
        """Control stopped running (out of on or verifying, a test action ended) on a plant SpotNav took over from its
        own optimiser: the reminder to set AI mode again, whatever the path (turned off, back to shadow only, a failed
        check, a firmware change, the acknowledgement withdrawn)."""
        options = self.options()
        running = not self._takeover_withdrawn(options) and (
            self.trial is not None or self.state(options) in ("on", "verifying")
        )
        if self._running and not running and self.took_from_ai:
            self._add_note(NOTE_AI_MODE_BACK, now)
        self._running = running

    def _transitions(self, options: gate.Options, now: datetime) -> None:
        """The opt-in turned on (blocks lifted, a failed check forgotten so it runs again, the reminder dropped) or
        off; the plant seen in AI mode again (the reminder done, the takeover forgotten)."""
        if options.enabled and not self._enabled_seen:
            for executor in (self.executor, self.export_executor):
                if executor is not None:
                    executor.reset()
            key = self.firmware_key()
            if gate.report_status(self.reports.get(key)) == "failed":
                self.reports.pop(key, None)
            self.notes = [note for note in self.notes if note.get("code") != NOTE_AI_MODE_BACK]
            self._log([{"at": now.isoformat(), "kind": "enabled", "reason": "opt_in"}])
        elif not options.enabled and self._enabled_seen:
            self._log([{"at": now.isoformat(), "kind": "disabled", "reason": "opt_out"}])
        self._enabled_seen = options.enabled
        control = control_for(self.facts.brand)
        executor = self.executor
        handed_back = executor is None or (executor.state.phase in ("idle", "blocked") and not executor.state.holds_snapshot)
        if control is not None and handed_back and taken_over(self.facts, control):
            # The person set AI mode again (not a takeover still being written): the reminder is done, and the plant
            # is no longer SpotNav's takeover.
            self.notes = [note for note in self.notes if note.get("code") != NOTE_AI_MODE_BACK]
            self.took_from_ai = False

    def _add_note(self, code: str, now: datetime) -> None:
        if not any(note.get("code") == code for note in self.notes):
            self.notes.append({"code": code, "at": now.isoformat()})

    async def _async_release(self, now: datetime, reason: str) -> None:
        """Hand back whatever the battery's executor holds (only restores are written)."""
        executor = self.executor
        if executor is None or (executor.state.phase in ("idle", "blocked") and not executor.state.holds_snapshot):
            return
        await self._async_step(executor, self._hand_back_inputs(now, executor, reason), allowed=False)

    async def _async_export_release(self, now: datetime, reason: str) -> None:
        executor = self.export_executor
        if executor is None or (executor.state.phase in ("idle", "blocked") and not executor.state.holds_snapshot):
            return
        await self._async_export_raw(now, executor, None, stopping=reason, allowed=False)

    async def _async_battery_step(
        self, now: datetime, slot: tuple[str, float, datetime | None] | None, *, allowed: bool
    ) -> StepResult | None:
        executor = self._executor()
        if executor is None:
            return None
        return await self._async_step(executor, self.inputs(now, executor.control, slot=slot), allowed=allowed)

    async def async_tick(self, now: datetime | None = None, *, stopping: str | None = None) -> StepResult | None:
        """One look of control as it runs the plan: refused unless control is on (the routine passed)."""
        options = self.options()
        self._require(self.state(options) == "on", "Battery control")
        now = now or dt_util.now()
        await self.async_export_tick(now, stopping=stopping)
        executor = self._executor()
        if executor is None:
            return None
        slot = None if stopping is not None else self._control_slot(now, options)
        return await self._async_step(
            executor, self.inputs(now, executor.control, stopping=stopping, slot=slot), allowed=True
        )

    async def async_export_tick(self, now: datetime | None = None, *, stopping: str | None = None) -> StepResult | None:
        """One look of the export limit: `curtail_export` while the sell price is below zero, else hand back.
        Refused unless the options allow writing."""
        self._require(self.options().writes, "The export limit")
        return await self._async_export_step(now or dt_util.now(), None, stopping=stopping, allowed=True)

    async def _async_export_step(
        self, now: datetime, action: str | None, *, stopping: str | None = None, allowed: bool
    ) -> StepResult | None:
        executor = self._export_executor()
        if executor is None:
            return None
        return await self._async_export_raw(now, executor, action, stopping=stopping, allowed=allowed)

    async def _async_export_raw(
        self, now: datetime, executor: Executor, action: str | None, *, stopping: str | None, allowed: bool
    ) -> StepResult:
        if stopping is not None or not allowed:
            inputs = self._export_hand_back_inputs(now, executor, stopping)
        else:
            inputs = self.export_inputs(now, executor.control, stopping=stopping, action=action)
        inputs, allowed = self._gated(inputs, allowed)
        result = executor.step(inputs)
        self._log(result.events)
        if result.persist_first:
            await self._async_save()
        failed: list[str] = []
        for op in result.ops:
            if not await self._async_write(op, executor, allowed=allowed):
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
            result = await self._async_export_raw(dt_util.now(), executor, None, stopping=stopping, allowed=False)
            stopping = None if executor.state.phase == "restoring" else stopping
            if executor.state.phase in ("idle", "blocked") or loop.time() >= deadline:
                return
            await asyncio.sleep(0 if result.ops else HAND_BACK_LOOK_S)

    # ---- the verification routine

    async def async_verify_step(self, now: datetime | None = None) -> StepResult | None:
        """One look of the verification routine; it starts a run when none runs. Refused unless the options allow
        writing."""
        self._require(self.options().writes, "The verification routine")
        now = now or dt_util.now()
        if self.verification is None or self.verification.done:
            if self._begin_verification(now) is None:
                return None
        return await self._async_verify(now)

    def _begin_verification(self, now: datetime) -> Verification | None:
        executor = self._executor()
        if executor is None:
            return None
        key = self.firmware_key()
        previous = self.reports.get(key) if isinstance(self.reports.get(key), dict) else None
        attempt = (previous or {}).get("attempts")
        self._verify_attempt = (attempt if isinstance(attempt, int) else 0) + 1
        self._verify_key = key
        self.verification = Verification(
            executor, now, firmware=self.facts.firmware, modes=None if previous is None else gate.modes_to_run(previous)
        )
        # The attempt counts from its start: a restart in the middle neither forgets it nor runs again at once.
        self._keep_report(key, gate.merge_report(
            previous, {"brand": executor.control.brand, "firmware": self.facts.firmware, "started_at": now.isoformat()},
            key=key, attempts=self._verify_attempt, now=now,
        ))
        self._log([{"at": now.isoformat(), "kind": "verify_started", "reason": key,
                    "modes": [result.mode for result in self.verification.results]}])
        _LOGGER.info("SpotNav battery control: checking the battery (%s)", key)
        return self.verification

    def _keep_report(self, key: str, report: dict[str, Any]) -> None:
        self.reports = gate.keep_reports(self.reports, key, report, most=MAX_REPORTS)

    def _end_verification(self, now: datetime) -> None:
        """Keep what a run found so far (a run ended by a stop or by turning off: its open modes stay pending)."""
        verification = self.verification
        if verification is None or self._verify_key is None:
            self.verification = None
            return
        report = gate.merge_report(
            self.reports.get(self._verify_key), verification.report(), key=self._verify_key,
            attempts=self._verify_attempt, now=now,
        )
        self._keep_report(self._verify_key, report)
        self._log([{"at": now.isoformat(), "kind": "verify_done" if verification.done else "verify_stopped",
                    "reason": gate.report_status(report), "modes": {
                        name: row.get("outcome") for name, row in report["modes"].items()}}])
        self.verification = None
        self._verify_key = None

    async def _async_verify(self, now: datetime) -> StepResult | None:
        verification = self.verification
        executor = self.executor
        if verification is None or executor is None:
            return None
        inputs = self.inputs(now, executor.control, slot=None)
        sample = Sample(inputs.facts.power_kw, inputs.battery_age_s, inputs.facts.soc_pct, inputs.house_deficit_kw)
        action, rate = verification.command(now, sample, inputs.facts.max_charge_kw)
        if action == ACTION_GRID_CHARGE:
            cap = self._grid_charge_cap_kw(now, self.options())
            if cap is not None and cap < gate.MIN_RATE_KW:
                # No grid charge fits under the HA-down cap now: tried again at a later run.
                verification.not_judgeable(now, "grid_charge_cap")
                action, rate = verification.command(now, sample, inputs.facts.max_charge_kw)
            rate = rate if cap is None else min(rate, cap)
        inputs = replace(inputs, action=action, rate_kw=rate, slot_end=None)
        result = await self._async_step(executor, inputs, allowed=True)
        capability = Capability(
            rated_charge_kw=inputs.facts.max_charge_kw,
            rated_discharge_kw=inputs.facts.max_discharge_kw,
            soc_min_pct=inputs.facts.reserve_soc_pct or 0.0,
        )
        verification.after(now, result, sample, capability)
        if verification.done:
            self._end_verification(now)
            await self._async_save()
        return result

    async def _async_maybe_verify(self, now: datetime, options: gate.Options) -> None:
        """Start a run at a suitable moment when the report for this firmware still needs one."""
        report = self.report()
        state = self.state(options)
        if state not in ("waiting", "on") or not gate.needs_run(report):
            self.waiting_for = None
            return
        if not self.firmware_known():
            self.waiting_for = "firmware_unknown"
            return
        last = gate.parse_time((report or {}).get("last_run_at"))
        if last is not None and (now - last).total_seconds() < gate.RETRY_GAP_S:
            self.waiting_for = "retry_gap"
            return
        executor = self._executor()
        if executor is None:
            self.waiting_for = "no_battery_control"
            return
        why = gate.suitable(self._moment(now, executor), evening=self._reverifying())
        self.waiting_for = why
        if why is None:
            self._begin_verification(now)
            await self._async_verify(now)

    def _reverifying(self) -> bool:
        """Whether this is a check after a firmware or integration change (another key passed before)."""
        key = self.firmware_key()
        return any(name != key and gate.report_status(report) == "passed" for name, report in self.reports.items())

    def _moment(self, now: datetime, executor: Executor) -> gate.Moment:
        inputs = self.inputs(now, executor.control, slot=(ACTION_HOLD, 0.0, None))
        seen = preview(executor.control, inputs)
        chargers, _car_kw = self.site.battery_shadow.grid_charging()
        return gate.Moment(
            local_hour=dt_util.as_local(now).hour,
            soc_pct=inputs.facts.soc_pct,
            power_kw=inputs.facts.power_kw,
            fresh=inputs.battery_age_s is not None and inputs.battery_age_s <= 120.0,
            measurement_ok=inputs.measurement_ok,
            headroom_a=inputs.headroom_a,
            car_charging=bool(chargers),
            blocker=seen.blocker,
            executor_idle=executor.state.phase == "idle" and not executor.state.holds_snapshot,
        )

    # ---- the settings

    async def async_update_options(self, changes: dict[str, bool]) -> dict[str, Any]:
        """Write some of the site's options (`gate.DEFAULTS`' keys, booleans) to its entry, without a reload, and act
        on them at once: turned off or back to shadow only, the plant is handed back before this returns; turned on,
        the next look starts the routine. Answers the status."""
        unknown = [key for key, value in changes.items() if key not in gate.DEFAULTS or not isinstance(value, bool)]
        if unknown:
            raise ValueError(f"not a battery control option: {', '.join(sorted(unknown))}")
        entry = self.hass.config_entries.async_get_entry(self.site.entry_id)
        if entry is None:
            raise ValueError("the site is not loaded")
        # Under the lock: a look in progress finishes with the options it read before this write changes them.
        async with self._lock:
            before = self.options()
            self.hass.config_entries.async_update_entry(entry, data={**entry.data, **changes})
            after = self.options()
            _LOGGER.info("SpotNav battery control: options %s", after.as_dict())
            # The entry is written: nothing below may raise into the caller (the plant is handed back at the next
            # look if this hand-back fails).
            if self._started and not self._closed:
                trial = self.trial
                try:
                    if (before.writes and not after.writes) or (
                        trial is not None and not self._trial_allowed(trial, after)
                    ):
                        # A test action ends with the options that no longer allow it, forced or not.
                        await self._async_stop("disabled" if not after.enabled else "shadow_only")
                except Exception:  # noqa: BLE001
                    self._look_failed("options_stop")
                try:
                    await self._async_look(dt_util.now())
                except Exception:  # noqa: BLE001
                    self._look_failed("look")
        try:
            return self.status()
        except Exception:  # noqa: BLE001
            self._look_failed("status")
            return {"options": after.as_dict(), "state": self.state(after)}

    # ---- the owner's test action

    async def async_trial(self, action: str, power_kw: float | None, minutes: float, *, force: bool) -> dict[str, Any]:
        """Run one action through the executor, with every rule, for `minutes` (`stop` ends a running one). Refused
        unless the opt-in is on; while shadow only, and before hold and normal passed the verification, unless `force`
        (the owner's on-site tests); while the routine runs; and for selling unless it is allowed. Answers what it
        started."""
        if not self._started or self._closed:
            raise TrialRefused("starting")
        now = dt_util.now()
        options = self.options()
        if action == "stop":
            async with self._lock:
                trial, self.trial = self.trial, None
                if trial is not None and trial.export and not options.writes and self.export_executor is not None:
                    await self._async_export_until_handed_back("trial_stopped")
                elif trial is not None and not trial.export and self.state(options) != "on" and self.executor is not None:
                    await self._async_until_handed_back("trial_stopped")
                await self._async_look(now)
            return {"stopped": trial is not None}
        if not options.enabled:
            raise TrialRefused("not_enabled")
        if options.shadow_only and not force:
            raise TrialRefused("shadow_only")
        if not force and gate.report_status(self.report()) != "passed":
            raise TrialRefused("not_verified")
        why = gate.trial_refusal(action, options)
        if why is not None:
            raise TrialRefused(why)
        if self.verification is not None and not self.verification.done:
            raise TrialRefused("verifying")
        if action == ACTION_CURTAIL_EXPORT:
            if self._export_executor() is None:
                raise TrialRefused("no_export_control")
            rate = 0.0
        else:
            if self._executor() is None:
                raise TrialRefused("no_battery_control")
            self.facts = self._read_facts()
            rate = 0.0
            if action in (ACTION_GRID_CHARGE, ACTION_EXPORT_DISCHARGE):
                rate = self._bounded_rate(action, abs(power_kw) if power_kw is not None else TEST_CHARGE_KW)
            if action == ACTION_GRID_CHARGE:
                cap = self._grid_charge_cap_kw(now, options)
                if cap is not None and cap < gate.MIN_RATE_KW:
                    raise TrialRefused("grid_charge_cap")
                rate = rate if cap is None else min(rate, cap)
        minutes = max(1.0, min(float(minutes), float(TRIAL_MAX_MIN)))
        async with self._lock:
            self.trial = Trial(action, round(rate, 3), now, now + timedelta(minutes=minutes), force)
            self._log([{"at": now.isoformat(), "kind": "trial", "reason": action, "rate_kw": round(rate, 3),
                        "minutes": minutes, "force_test": force}])
            _LOGGER.warning("SpotNav battery control: test action %s for %s min (%s kW)", action, minutes, rate)
            await self._async_look(now)
        executor = self.export_executor if action == ACTION_CURTAIL_EXPORT else self.executor
        return {
            "trial": None if self.trial is None else self.trial.as_dict(),
            "phase": None if executor is None else executor.state.phase,
            "events": list(self.log)[-6:],
        }

    def _bounded_rate(self, action: str, rate_kw: float) -> float:
        """A test action's power within the battery's own rating (`max_charge_kw`, `max_discharge_kw`) when known."""
        rating = (
            self.facts.max_charge_kw if action == ACTION_GRID_CHARGE
            else self.facts.max_discharge_kw if action == ACTION_EXPORT_DISCHARGE else None
        )
        return rate_kw if rating is None or rating <= 0 else min(rate_kw, rating)

    def _trial_slot(self, now: datetime, trial: Trial, options: gate.Options) -> tuple[str, float, datetime | None]:
        """The test action for this look: its power within the rating and, for a grid charge, the HA-down cap as the
        site reads now (the house's peak changes by the hour, the chargers' currents at any time); plain
        self-consumption while the cap leaves no grid charge."""
        rate = self._bounded_rate(trial.action, trial.rate_kw)
        if trial.action == ACTION_GRID_CHARGE:
            cap = self._grid_charge_cap_kw(now, options)
            if cap is not None and cap < gate.MIN_RATE_KW:
                return (ACTION_SELF_CONSUME, 0.0, trial.until)
            rate = rate if cap is None else min(rate, cap)
        elif trial.action == ACTION_EXPORT_DISCHARGE:
            cap = self._export_cap_kw()
            if cap < gate.MIN_RATE_KW:
                return (ACTION_SELF_CONSUME, 0.0, trial.until)
            rate = min(rate, cap)
        return (trial.action, round(rate, 3), trial.until)

    @staticmethod
    def _trial_allowed(trial: Trial, options: gate.Options) -> bool:
        """Whether a running test action may go on under these options: the opt-in on, and shadow only off unless
        the action was forced (`force_test` overrides shadow only and the verification, nothing else)."""
        return options.enabled and (not options.shadow_only or trial.force)

    # ---- shadow only

    def _shadow_look(self, now: datetime, options: gate.Options) -> None:
        """While shadow only: what control would do now (`executor.preview`), logged when it changes."""
        control = control_for(self.facts.brand)
        if control is None:
            return
        allowed = _PLAN_ACTIONS | ({ACTION_EXPORT_DISCHARGE} if options.allow_sell else set())
        slot, why = gate.restricted(
            self._merged_slot(now), allowed, self._grid_charge_cap_kw(now, options), self._export_cap_kw()
        )
        action = None if slot is None else slot[0]
        if action in (None, ACTION_SELF_CONSUME):
            seen = None
            signature: tuple[Any, ...] = (action, None, why)
        else:
            assert slot is not None
            seen = preview(control, self.inputs(now, control, slot=slot))
            signature = (action, seen.blocker, why, tuple((op.role, op.value) for op in seen.writes))
        if signature == self._would_do:
            return
        self._would_do = signature
        self._log([{
            "at": now.isoformat(), "kind": "would_do", "reason": action or "nothing", "why": why,
            "blocker": None if seen is None else seen.blocker,
            "rate_kw": None if slot is None else slot[1],
            "writes": [] if seen is None else [op.as_dict() for op in seen.writes],
        }])

    # ---- what control runs

    def _merged_slot(self, now: datetime) -> tuple[str, float, datetime | None] | None:
        """The plan's running slot with the hold for the car (`gate.merged_action`)."""
        return gate.merged_action(self._plan_now(now), self.site.battery_shadow.hold_requested())

    def _control_slot(self, now: datetime, options: gate.Options) -> tuple[str, float, datetime | None] | None:
        slot, why = gate.restricted(
            self._merged_slot(now), gate.allowed_actions(self.report(), options), self._grid_charge_cap_kw(now, options),
            self._export_cap_kw(),
        )
        if why != self._restricted:
            # Logged when it changes: an action the report does not allow, or the grid charge cap.
            self._restricted = why
            if why is not None:
                self._log([{"at": now.isoformat(), "kind": "restricted", "reason": why,
                            "rate_kw": None if slot is None else slot[1]}])
        return slot

    def _main_fuse_a(self) -> float | None:
        fuse_a = self.site.config.get(CONF_MAIN_FUSE_A)
        return float(fuse_a) if isinstance(fuse_a, (int, float)) and not isinstance(fuse_a, bool) else None

    def _grid_charge_cap_kw(self, now: datetime, options: gate.Options) -> float | None:
        """The HA-down grid charge cap (`gate.grid_charge_cap_kw`) on the site's readings now."""
        control = control_for(self.facts.brand)
        import_limit_kw = None
        if options.import_limit_acts and control is not None:
            import_limit_kw = _float(read_plant_entity(self.hass, self.site.config, control.import_limit))
        return gate.grid_charge_cap_kw(
            self._main_fuse_a(), stored_voltage_between_phases_v(self.site.config), margin_a=HEADROOM_MARGIN_A,
            cars_a=self._cars_allowed_a(), house_peak_kw=self._house_peak_kw(now),
            plant_phases=self._plant_phases(control), import_limit_kw=import_limit_kw,
            import_limit_acts=options.import_limit_acts, house_phase_peaks_a=self._phase_peaks(now),
        )

    def _learned(self, now: datetime) -> Learned | None:
        """The site's learned per-phase house peaks (`site/phase_peaks.py`), `None` when it keeps none."""
        peaks = getattr(self.site, "phase_peaks", None)
        if peaks is None:
            return None
        try:
            return peaks.learned(now.timestamp())
        except Exception:  # noqa: BLE001 - without them the conservative rule counts
            self._look_failed("phase_peaks")
            return None

    def _phase_peaks(self, now: datetime) -> dict[str, float] | None:
        """The per-phase peaks the grid charge cap uses once they are learned, else `None` (the conservative rule)."""
        learned = self._learned(now)
        return None if learned is None else learned.phase_peaks()

    def _cap_status(self, now: datetime, options: gate.Options) -> dict[str, Any]:
        """The grid charge cap and what it rests on, for the status and the diagnostics."""
        learned = self._learned(now)
        try:
            cap: float | None = self._grid_charge_cap_kw(now, options)
            failed = False
        except Exception:  # noqa: BLE001 - the status must never fail on it
            cap, failed = None, True
        return {
            "cap_kw": None if cap is None else round(cap, 2),
            "rule": "error" if failed else "import_limit" if cap is None else (
                "learned" if learned is not None and learned.phase_peaks() is not None else "conservative"
            ),
            "main_fuse_a": self._main_fuse_a(),
            "learned": None if learned is None else learned.as_dict(),
        }

    def _export_cap_kw(self) -> float:
        """The fuse rule on the export side (`gate.export_discharge_cap_kw`) on the site's phases now."""
        volts = stored_voltage_between_phases_v(self.site.config)
        exports = self._phase_export_a(volts)
        control = control_for(self.facts.brand)
        phases = self._plant_phases(control)
        executor = self.executor
        share_a = 0.0
        battery_kw = self.facts.power_kw
        if (
            executor is not None and executor.state.phase == "active"
            and executor.state.action == ACTION_EXPORT_DISCHARGE and battery_kw is not None and battery_kw < 0
        ):
            # What the battery measurably discharges, at most what it was told: never more of the export than its own.
            share_a = min(executor.state.rate_kw, -battery_kw) * 1000.0 / (phases * gate.phase_volts(volts))
        return gate.export_discharge_cap_kw(
            self._main_fuse_a(), volts, margin_a=HEADROOM_MARGIN_A, phase_export_a=exports, battery_share_a=share_a,
            plant_phases=phases,
        )

    def _phase_export_a(self, volts_between_phases: float) -> list[float] | None:
        """Each phase's export now (A, 0 where it imports), `None` when a phase is not known."""
        signed = list(self.site.result.phase_signed_active_power_w.values())
        if not signed or not all(value is not None and math.isfinite(value) for value in signed):
            return None
        volts = gate.phase_volts(volts_between_phases)
        return [max(-float(value), 0.0) / volts for value in signed]  # type: ignore[arg-type]

    def _cars_allowed_a(self) -> float:
        """What the site's chargers may draw per phase if nothing writes them again (A, summed): each one's last
        current (`charger_allowed_a`), `ASSUMED_CURRENT_A` when it is not known; infinite (no grid charge) when the
        chargers cannot be read."""
        from ..execution.controller import ASSUMED_CURRENT_A  # noqa: PLC0415 - the charger side imports the site

        try:
            total = 0.0
            for charger_entry_id in self.site.config.get(CONF_CHARGER_ENTRY_IDS) or []:
                amps = self.site.charger_allowed_a(charger_entry_id)
                total += float(ASSUMED_CURRENT_A) if amps is None else max(amps, 0.0)
        except Exception:  # noqa: BLE001 - unknown: no grid charge on the HA-down rule
            self._look_failed("cars_allowed")
            return math.inf
        return total

    def _plant_phases(self, control: BrandControl | None) -> int:
        """The phases the plant spreads a grid charge over: its Output Type when it shows one, else the brand's."""
        if control is None:
            return 1
        state = read_plant_entity(self.hass, self.site.config, control.output_type)
        return OUTPUT_TYPE_PHASES.get(str(state).lower(), control.grid_charge_phases) if state else control.grid_charge_phases

    def _house_peak_kw(self, now: datetime) -> float | None:
        """The house forecast's p90 for the running hour (kW), read once an hour; `None` when it has none."""
        hour = now.astimezone(dt_util.UTC).replace(minute=0, second=0, microsecond=0)
        if self._peak is not None and self._peak[0] == hour:
            return self._peak[1]
        value: float | None = None
        try:
            view = self.site.house_forecast.band_view(now)
            for row in view.get("hours") or []:
                if row[0] == hour.isoformat() and isinstance(row[3], (int, float)) and math.isfinite(row[3]):
                    value = float(row[3])
                    break
        except Exception:  # noqa: BLE001 - without a band the fixed margin counts
            _LOGGER.debug("SpotNav battery control: no house p90 for the hour", exc_info=True)
        self._peak = (hour, value)
        return value

    def _held_cars_have_room(self, now: datetime, held: list[str]) -> bool:
        """Whether every charger load balancing holds back has a departure with room (`gate.departure_has_room`): its
        remaining energy need against the time left at its planned power. The need is what its plan still expects
        before the departure plus what the running window is already behind (`import_target_shortfall_kwh`), as the
        site's import target reckons a departure (`SiteCapacityController._target_shortfall`)."""
        if not held:
            return False
        from ..runtime import controller_for, preview_for  # noqa: PLC0415 - the charger side imports the site
        from ..site.import_target import member_w, plan_remaining_kwh  # noqa: PLC0415

        for charger_entry_id in held:
            charger = controller_for(self.hass, charger_entry_id)
            plan = None if charger is None else getattr(charger, "plan", None)
            if plan is None or not plan.departure or not getattr(plan, "auto_owned", False):
                return False
            departure = dt_util.parse_datetime(plan.departure)
            if departure is None or departure.tzinfo is None:
                return False
            windows = plan.windows
            window_h = sum(max(0.0, (end - start).total_seconds()) for start, end in windows) / 3600.0
            if plan.energy_kwh and window_h > 0:
                power_kw = plan.energy_kwh / window_h
            else:
                power_kw = plan.power_kw if plan.power_kw else member_w(plan.phases, plan.amps) / 1000.0
            need = plan_remaining_kwh(windows, power_kw, now, departure)
            preview = preview_for(self.hass, charger_entry_id)
            if preview is not None:
                need += max(preview.import_target_shortfall_kwh(), 0.0)
            if not gate.departure_has_room(departure, now, need, power_kw):
                return False
        return True

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
        """Whether this path acts on the battery or the export limit now (its writes are no one else's), and for
        `ACTING_GRACE_S` after a hand-back: a plant's sensors can show SpotNav's own last writes a poll or two late."""
        now = dt_util.now()
        for executor in (self.executor, self.export_executor):
            if executor is None:
                continue
            state = executor.state
            if state.phase in ("active", "restoring"):
                return True
            if state.released_at is not None and 0 <= (now - state.released_at).total_seconds() < ACTING_GRACE_S:
                return True
        return False

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

    def export_inputs(
        self, now: datetime, control: BrandControl, *, stopping: str | None = None, action: str | None = None
    ) -> ExecInputs:
        """The export limit's look: the plant's facts and control, the site's measurement, the action (`action`, or
        from the sell price now), and the battery path's writes today (the cap is shared)."""
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
        if action is None:
            action = ACTION_CURTAIL_EXPORT if curtails(self._sell_now(now)) else ACTION_SELF_CONSUME
        return ExecInputs(
            now=now,
            facts=self._with_outside(facts),
            reads=reads,
            action=action,
            measurement_ok=result.state not in UNHEALTHY_STATES and result.state != "not_configured",
            headroom_a=min(headrooms) if headrooms else None,
            volts_between_phases=stored_voltage_between_phases_v(self.site.config),
            stopping=stopping,
            failed=self._export_failed,
            other_writes_today=0 if battery is None or battery.state.day != now.date().isoformat()
            else battery.state.writes_today,
            grid_import_kw=self._grid_import_kw(),
            sibling_active=sibling_active,
            takeover=self.options().ai_takeover_ack,
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

    def inputs(
        self, now: datetime, control: BrandControl, *, stopping: str | None = None, slot: Any = _UNSET,
    ) -> ExecInputs:
        """The battery's look with `slot` as the action (by default what control runs now)."""
        power_kw, age_s = self._battery_power()
        self.facts = facts = self._with_outside(read_battery_facts(self.hass, self.site.config, power_kw))
        result = self.site.result
        headrooms = [value for value in result.phase_headroom_a.values() if value is not None]
        try:
            grid_import_kw = self._grid_import_kw()
        except Exception:  # noqa: BLE001 - unknown: the grid charge then counts none of its own import back
            self._look_failed("grid_import")
            grid_import_kw = None
        # What the house needs beyond the sun now (kW): the grid's import less what the battery takes from it, for
        # judging a discharge to the house.
        house_deficit_kw = (
            None if grid_import_kw is None or facts.power_kw is None else max(grid_import_kw - facts.power_kw, 0.0)
        )
        config = self.site.config
        options = self.options()
        if slot is _UNSET:
            slot = self._control_slot(now, options)
        car_first, car_held = self._shedding(now, config)
        return ExecInputs(
            now=now,
            facts=facts,
            reads=self._reads(control),
            action=None if slot is None else slot[0],
            rate_kw=0.0 if slot is None else slot[1],
            slot_end=None if slot is None else slot[2],
            battery_age_s=age_s,
            measurement_ok=result.state not in UNHEALTHY_STATES and result.state != "not_configured",
            headroom_a=min(headrooms) if headrooms else None,
            volts_between_phases=stored_voltage_between_phases_v(config),
            grid_import_kw=grid_import_kw,
            # The fast shedding order (`gate.battery_yields`): the battery's grid charge before the car, the car
            # first when its departure has room; "battery first" backs the car off first.
            car_first=car_first,
            car_held=car_held,
            house_deficit_kw=house_deficit_kw,
            stopping=stopping,
            failed=self._failed,
            other_writes_today=self._export_writes_today(now),
            takeover=options.ai_takeover_ack,
        )

    def _shedding(self, now: datetime, config: dict[str, Any]) -> tuple[bool, bool]:
        """`(car_first, car_held)` for the executor: whether the battery's grid charge yields to a held car, and
        whether load balancing holds one back now. When the chargers cannot be read: the battery yields and no car
        counts as held (the cautious side: the grid charge never takes the car's room)."""
        try:
            held = [
                charger for charger in config.get(CONF_CHARGER_ENTRY_IDS) or []
                if self.site.load_balancing_limit(charger)[0] is not None
            ]
        except Exception:  # noqa: BLE001 - a part of the site that raises must not stop the battery's look
            self._look_failed("held_cars")
            return True, False
        car_first = config.get(CONF_SOLAR_PRIORITY, DEFAULT_SOLAR_PRIORITY) == SOLAR_PRIORITY_CAR_FIRST
        room = False
        if held and car_first:
            try:
                room = self._held_cars_have_room(now, held)
            except Exception:  # noqa: BLE001 - no room known: the car is not shed first
                self._look_failed("held_cars_room")
        return gate.battery_yields(car_first, room), bool(held)

    def _hand_back_inputs(self, now: datetime, executor: Executor, stopping: str | None) -> ExecInputs:
        """The battery's look for a hand-back: the full one, or the fewest reads when a part of it raises (a hand-back
        must never wait for the rest of the site)."""
        try:
            return self.inputs(now, executor.control, stopping=stopping, slot=None)
        except Exception:  # noqa: BLE001 - handed back on what can still be read
            self._look_failed("hand_back_inputs")
            return self._minimal_inputs(now, executor, stopping)

    def _minimal_inputs(self, now: datetime, executor: Executor, stopping: str | None) -> ExecInputs:
        """The fewest reads a hand-back needs: the battery's facts (the last ones when they cannot be read), the
        controls' states and the reason; the site's measurement counts as unknown."""
        try:
            power_kw, age_s = self._battery_power()
        except Exception:  # noqa: BLE001
            power_kw, age_s = None, None
        try:
            self.facts = facts = self._with_outside(read_battery_facts(self.hass, self.site.config, power_kw))
        except Exception:  # noqa: BLE001
            facts = self.facts
        try:
            reads = self._reads(executor.control)
        except Exception:  # noqa: BLE001
            reads = stored_reads(self.hass, executor)
        try:
            takeover = self.options().ai_takeover_ack
        except Exception:  # noqa: BLE001
            takeover = False
        return ExecInputs(
            now=now, facts=facts, reads=reads, battery_age_s=age_s, measurement_ok=False, stopping=stopping,
            failed=self._failed, takeover=takeover,
        )

    def _export_hand_back_inputs(self, now: datetime, executor: Executor, stopping: str | None) -> ExecInputs:
        """The export limit's look for a hand-back (no sell price needed), the fewest reads when a part raises."""
        try:
            return self.export_inputs(now, executor.control, stopping=stopping, action=ACTION_SELF_CONSUME)
        except Exception:  # noqa: BLE001 - handed back on what can still be read
            self._look_failed("export_hand_back_inputs")
        try:
            facts, reads, _control = read_export_facts(self.hass, self.site.config)
            facts = self._with_outside(facts)
        except Exception:  # noqa: BLE001
            facts, reads = BatteryFacts(brand=executor.state.brand), {}
        reads = {**stored_reads(self.hass, executor), **reads}
        return ExecInputs(now=now, facts=facts, reads=reads, measurement_ok=False, stopping=stopping,
                          failed=self._export_failed)

    def _export_writes_today(self, now: datetime) -> int:
        executor = self.export_executor
        if executor is None or executor.state.day != now.date().isoformat():
            return 0
        return executor.state.writes_today

    # ---- writes

    def _writes_allowed_now(self) -> bool:
        """Whether the options as stored now still allow a write that is not a hand-back (read again before every
        step: the entry can change while a look waits on a write)."""
        options = self.options()
        trial = self.trial
        return options.writes or (trial is not None and self._trial_allowed(trial, options))

    def _gated(self, inputs: ExecInputs, allowed: bool) -> tuple[ExecInputs, bool]:
        """A step the options no longer allow becomes a hand-back."""
        if not allowed or self._writes_allowed_now():
            return inputs, allowed
        return replace(
            inputs, stopping=inputs.stopping or "options_changed", action=None, rate_kw=0.0, slot_end=None
        ), False

    async def _async_step(self, executor: Executor, inputs: ExecInputs, *, allowed: bool) -> StepResult:
        inputs, allowed = self._gated(inputs, allowed)
        result = executor.step(inputs)
        self._failed = ()
        self._log(result.events)
        if any(event["kind"] == "enter" for event in result.events) and taken_over(inputs.facts, executor.control):
            # Taken over from the plant's own optimiser: turning control off reminds the person to set it again.
            self.took_from_ai = True
        if result.persist_first:
            # The snapshot is on disk before the first write, so a crash in between still restores it.
            await self._async_save()
        failed: list[str] = []
        for op in result.ops:
            if not await self._async_write(op, executor, allowed=allowed):
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
            result = await self._async_step(executor, self._hand_back_inputs(now, executor, stopping), allowed=False)
            stopping = None if executor.state.phase == "restoring" else stopping
            if executor.state.phase in ("idle", "blocked") or loop.time() >= deadline:
                return
            await asyncio.sleep(0 if result.ops else HAND_BACK_LOOK_S)

    async def _async_write(self, op: Op, executor: Executor, *, allowed: bool) -> bool:
        """One write; `False` when it was refused or the service call raised (the executor counts it as not taken).
        A write that is not a restore is refused unless the caller's gate allows it: a hand-back never is."""
        if not op.restore and not allowed:
            _LOGGER.warning("SpotNav battery control: a write of %s was refused by the gate", op.role)
            self._log([{"at": dt_util.now().isoformat(), "kind": "refused", "reason": op.role}])
            return False
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
        """Store the state when it differs from what was stored last (a look that changed nothing writes nothing)."""
        data = self._data()
        if data == self._saved:
            return
        await self._store.async_save(data)
        self._saved = data

    def _data(self) -> dict[str, Any]:
        executor = self.executor
        data: dict[str, Any] = {
            "reports": dict(self.reports),
            "gate": {"key": self._key, "enabled_seen": self._enabled_seen, "took_from_ai": self.took_from_ai,
                     "notes": [dict(note) for note in self.notes]},
        }
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
        return data

    def _needs(self) -> list[str]:
        """What the person can do before control can run, one code each (the card's checklist)."""
        needs: list[str] = []
        facts = self.facts
        control = control_for(facts.brand)
        if control is None:
            return ["no_battery_control"] if facts.battery_entity_id is not None else []
        reads = read_role_reads(self.hass, self.site.config, control)
        if not self.firmware_known():
            needs.append("firmware_unknown")
        for role in control.roles:
            read = reads.get(role.name)
            if read is None or not read.found:
                needs.append(f"control_not_found:{role.name}")
            elif role.method != "service" and not read.enabled:
                needs.append(f"control_disabled:{role.name}")
        management = facts.management
        if management.state == "externally_controlled":
            needs.append(f"outside_control:{management.reason}")
        elif taken_over(facts, control) or self._takeover_withdrawn(self.options()):
            if not self.options().ai_takeover_ack:
                needs.append("ai_takeover_ack")
        elif management.state == "self_managed" and not (self.acting()):
            needs.append(f"self_managed:{management.reason}")
        return needs

    def _line(self, options: gate.Options) -> str:
        """The one line under the switch, as a stable code (the card words it)."""
        state = self.state(options)
        report = self.report()
        executor = self.executor
        if state == "verifying":
            return "checking_after_update" if self._reverifying() else "checking"
        if state == "failed":
            mode = gate.failed_mode(report)
            outcome = None if mode is None else (report or {}).get("modes", {}).get(mode, {}).get("outcome")
            return f"failed:{mode or 'not_judgeable'}:{outcome or 'attempts'}"
        if state == "waiting":
            return f"{'waiting_after_update' if self._reverifying() else 'waiting'}:{self.waiting_for or 'moment'}"
        if state == "on" and executor is not None and executor.state.phase == "blocked":
            return f"blocked:{executor.state.blocked}"
        if state == "on" and "ai_takeover_ack" in self._needs():
            return "watching:ai_mode"
        return state

    def status(self) -> dict[str, Any]:
        """What the card's battery section shows (once it has one) and diagnostics: the options, the state and its
        line, the report for the plant's firmware, the actions allowed, the routine's progress, what is missing and
        the notes."""
        options = self.options()
        verification = self.verification
        current = None if verification is None else verification.current
        return {
            "options": options.as_dict(),
            "state": self.state(options),
            "line": self._line(options),
            "key": self.firmware_key(),
            "report": self.report(),
            "allowed_actions": sorted(self.allowed(options)),
            "verification": None if verification is None else {
                "started_at": verification.started_at.isoformat(),
                "mode": None if current is None else current.mode,
                "stage": verification.stage,
                "modes": [result.mode for result in verification.results],
                "done": [result.mode for result in verification.results if result.outcome is not None],
            },
            "waiting_for": self.waiting_for,
            "needs": self._needs(),
            "notes": [dict(note) for note in self.notes],
            "trial": None if self.trial is None else self.trial.as_dict(),
            "grid_charge_cap": self._cap_status(dt_util.now(), options),
        }

    def snapshot(self) -> dict[str, Any]:
        executor = self.executor
        try:
            status: dict[str, Any] | None = self.status()
        except Exception:  # noqa: BLE001 - diagnostics must never fail on one block
            status = None
        return {
            "status": status,
            "brand": None if executor is None else executor.control.brand,
            "executor": None if executor is None else executor.as_dict(),
            "export": None if self.export_executor is None else self.export_executor.as_dict(),
            "verification": None if self.verification is None else self.verification.report(),
            "reports": dict(self.reports),
            "log": list(self.log),
        }
