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
* **A grid charge per phase** (`commands.PhaseControl`, no brand named here): where the brand has targets per
  phase, they and what they require are found and enabled, the plant's own power per phase is read
  (`_phase_missing`) and the verification's mode `per_phase` passed on this firmware (`gate.per_phase`), the
  executor runs the brand's phased control (`_sync_phases`, changed only while it holds nothing) and a grid charge
  is given each phase's HA-down cap (`gate.grid_charge_phase_caps_kw`) when those allow at least
  `PER_PHASE_GAIN_KW` more in all than the balanced cap (`_charge_limits`); the slot is then capped at their sum.
  Anything else is the balanced charge under the balanced cap, as before: the targets not there, not verified, a
  reading per phase missing, the plant's own import limit guarding the fuse, a balanced house. A running charge
  per phase that the plant does not follow withdraws the capability (`gate.per_phase_failed`). Where the
  verification's `charge` could only be tried per phase (the balanced cap leaves no room: `_async_verify`,
  `verify.phase_charge_step`), a grid charge runs only per phase (`gate.charge_by_phases`, `_control_slot`).
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
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Final

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_RESTORED
from homeassistant.core import HomeAssistant, State, callback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.loader import async_get_loaded_integration
from homeassistant.util import dt as dt_util

from ..beta import learning_gate_open
from ..const import (
    CONF_BATTERY_AGGREGATE_POWER_ENTITY,
    CONF_CHARGER_ENTRY_IDS,
    CONF_CURRENT_CONTROL,
    CONF_MAIN_FUSE_A,
    CONF_SOLAR_PRIORITY,
    CURRENT_CONTROL_POWER,
    DEFAULT_SOLAR_PRIORITY,
    DOMAIN,
    SOLAR_PRIORITY_CAR_FIRST,
)
from ..core.electric import phase_volts, stored_safety_margin_a
from ..planning.grid_voltage import stored_voltage_between_phases_v
from ..site.energy_statistics import async_energy_prefs, energy_statistic_ids
from ..site.measurement_problem import UNHEALTHY_STATES
from ..site.phase_peaks import MIN_READY_DAYS, Learned, plant_phase_w
from . import gate, optin
from .preheat import PreheatWatch
from .commands import (
    ACTION_CURTAIL_EXPORT,
    OUTPUT_TYPE_PHASES,
    BrandControl,
    Expiry,
    Phases,
    answered,
    control_for,
    export_control_for,
    field_tested as recipe_field_tested,
    fixed_rate_kw,
    reading,
    service_call,
)
from .executor import (
    BATTERY_MAX_AGE_S,
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
    DEFAULT_CYCLE_COST,
    sell_margin_minor_per_kwh,
)
from .plan_shadow import slot_at
from .runtime import TICK_S, plant_entity_id, read_battery_facts, read_export_facts, read_plant_entity, read_role_reads
from .verify import (
    MAX_REPORTS,
    TEST_CHARGE_KW,
    TEST_CHARGE_SOC_MAX_PCT,
    Sample,
    Timing,
    Verification,
    phase_charge_step,
)

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
#: What a heat raise held drives the compressor above anything the learned house peak saw (kW, one phase): taken
#: off each phase's HA-down cap while the raise stands (`_heat_standing_a`).
HEAT_RAISE_KW: Final = 1.0
#: The longest test action (min).
TRIAL_MAX_MIN: Final = 120
#: Looks in a row that may fail while SpotNav holds the plant before it hands back on the fewest reads.
FAILED_LOOKS_HAND_BACK: Final = 3
#: How long one write's service call may take before it is given up on as a write not taken (s): Home Assistant
#: has no timeout of its own, and an integration that never answers must not hold a look, the lock or a hand-back.
#: As long as a charger command's (`execution.chargers.base.DEFAULT_CALL_TIMEOUT_S`), so a slow cloud still answers.
CALL_TIMEOUT_S: Final = 30.0
#: The service each role's method writes with (a service role names its own, `Role.services`).
_METHOD_SERVICES: Final = {
    "number": ("set_value",), "select": ("select_option",), "switch": ("turn_on", "turn_off"), "button": ("press",),
}
#: Every action control may run when the report allows it; selling only in a mode that sells (`gate.Options.allow_sell`).
_PLAN_ACTIONS: Final = frozenset({ACTION_HOLD, ACTION_GRID_CHARGE, ACTION_DISCHARGE_HOUSE})
#: How often the site's energy sources are read again for the import-limit rule (s).
SOURCES_REFRESH_S: Final = 3600.0
#: A grid charge runs per phase only when the phases' caps allow at least this much more in all than the balanced
#: cap (kW): a balanced house keeps the brand's own charge.
PER_PHASE_GAIN_KW: Final = 0.5
#: The person is offered to accept the HA-down risk (`battery_control_accept_ha_down`) only where that would let a
#: grid charge run at least this much faster (kW): the HA-down cap with an empty house of cars, against what the fuse
#: allows with an empty house (and at most the battery's rated charge).
HA_DOWN_OFFER_GAIN_KW: Final = 1.0
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


def _state_of(control: BrandControl, name: str, state: State | None) -> str | None:
    """A stored control's state as its role reads it (`commands.answered`); `None` when the entity is gone."""
    if state is None:
        return None
    try:
        role = control.role(name)
    except KeyError:
        return state.state
    return answered(role, state.state, state.attributes)


def stored_reads(hass: HomeAssistant, executor: Executor) -> dict[str, RoleRead]:
    """Each control the executor wrote, read by its stored entity (no registry lookup)."""
    reads: dict[str, RoleRead] = {}
    for name, entity_id in executor.state.entities.items():
        state = hass.states.get(entity_id)
        reads[name] = RoleRead(entity_id=entity_id, state=_state_of(executor.control, name, state),
                               found=state is not None, enabled=state is not None, device_id=executor.state.device_id)
    return reads


async def _async_call(hass: HomeAssistant, op: Op, executor: Executor) -> str | None:
    """One write as a service call; why it was not taken (`_failure`), `None` when it was."""
    role = executor.control.role(op.role)
    domain, service, data = service_call(
        op.method, op.entity_id, op.value, device_id=op.device_id,  # type: ignore[arg-type]
        services=role.services, watchdog_s=executor.watchdog_s, device_field=role.device_field, duration=role.lease,
    )
    try:
        await _async_service(hass, domain, service, data)
    except Exception as err:  # noqa: BLE001 - any error (a Modbus library's too) is a write not taken
        _LOGGER.warning("Battery control: %s.%s for %s failed: %s", domain, service, op.role, _error_text(err))
        return _failure(err)
    return None


async def _async_service(hass: HomeAssistant, domain: str, service: str, data: dict[str, Any]) -> None:
    """One write's service call, given up on after `CALL_TIMEOUT_S` (`TimeoutError`, a write not taken)."""
    async with asyncio.timeout(CALL_TIMEOUT_S):
        await hass.services.async_call(domain, service, data, blocking=True)


def _failure(err: Exception) -> str:
    """Why a write's call was not taken: `call_timeout` when it was given up on (a slow integration, which the
    executor and a verification take as the moment's), else `service_failed`."""
    return "call_timeout" if isinstance(err, TimeoutError) else "service_failed"


def _error_text(err: Exception) -> str:
    """A failed write's error as logged; a call given up on says so (its `TimeoutError` has no text)."""
    if isinstance(err, TimeoutError):
        return f"no answer within {CALL_TIMEOUT_S:.0f} s"
    return str(err)


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
        timed_out: tuple[str, ...] = ()
        while executor.state.phase == "restoring" and loop.time() < deadline:
            if key == "battery":
                facts = read_battery_facts(hass, config, None)
                reads = {**stored_reads(hass, executor), **read_role_reads(hass, config, control)}
            else:
                facts, reads, _control = read_export_facts(hass, config)
                reads = {**stored_reads(hass, executor), **reads}
            result = executor.step(ExecInputs(
                now=dt_util.now(), facts=facts, reads=reads, measurement_ok=False, stopping="removed", failed=failed,
                timed_out=timed_out,
            ))
            whys = {op.role: await _async_call(hass, op, executor) for op in result.ops if op.restore}
            failed = tuple(role for role, why in whys.items() if why is not None)
            timed_out = tuple(role for role, why in whys.items() if why == "call_timeout")
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



def _card_refusal(refusal: str | None) -> str | None:
    """An import-limit refusal as the card is sent it: `not_trusted` (`commands.ImportLimitTrust`) as `not_supported`,
    a code the card knows; the precise code is in the diagnostics (`snapshot`)."""
    return "not_supported" if refusal == "not_trusted" else refusal


def _grid_charge_expiry(control: BrandControl | None, *, phased: bool) -> Expiry | None:
    """The expiry of a grid charge on this control: the phase spec's for a charge per phase (`phased`; `None` for a
    brand without one), else the recipe's own for the grid charge (`expiry_by_action`, then `expiry`: never the
    phase spec's, which has nothing to end a balanced command)."""
    if control is None:
        return None
    base = control.with_phases(False)
    if phased:
        return None if base.phase_control is None else base.phase_control.expiry
    return base.expiry_by_action.get(ACTION_GRID_CHARGE, base.expiry)


def _accept_applies(control: BrandControl | None) -> bool:
    """Whether the person's acceptance of the HA-down risk (`battery_control_accept_ha_down`) may lift the cap on this
    recipe: never for a slow control or one whose charge SpotNav cannot set (`BrandControl.slow`, `fixed_charge`:
    Tesla, a cloud in 30-minute blocks), since the live clamp cannot lower its charge within seconds. A hold that may
    itself charge from the grid there (`hold_charges_without`) stays under the HA-down cap with it."""
    return control is not None and not (control.slow or control.fixed_charge)

#: The plan's cap rule where control runs no grid charge at all (`BatteryControl.plan_grid_charge_cap`): a cap of 0.
RULE_NO_GRID_CHARGE: Final = "no_grid_charge"

#: Refusals of the per-phase import-limit rule that leave it a way for the person (`BatteryControl._phase_path_open`):
#: none, a step they can take (enable the Output Type or the grid sensor status, turn the phase switch on), or the
#: site's sources not read yet (a moment after a start: the HA-down row must not flash up meanwhile).
PHASE_PATH_STEPS: Final = frozenset(
    {None, "sources_unknown", "output_type_unread", "phase_control_off", "grid_sensor_offline"}
)
#: How long the person's import-limit write is read back for, and how often (s).
IMPORT_LIMIT_READ_BACK_S: Final = 15.0
IMPORT_LIMIT_READ_BACK_STEP_S: Final = 0.5


class BatteryControl:
    """One site's battery and export-limit write path (see the module)."""

    def __init__(
        self, hass: HomeAssistant, site: SiteCapacityController, *, plan_source: PlanSource | None = None,
        sell_source: SellSource | None = None, watchdog_s: int | None = None,
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
        self._export_timed_out: tuple[str, ...] = ()
        self.facts = BatteryFacts()
        self.log: deque[dict[str, Any]] = deque(maxlen=LOG_MAX)
        #: The verification reports by firmware key (`gate.merge_report`).
        self.reports: dict[str, Any] = {}
        self.verification: Verification | None = None
        #: Whether the running verification is a person's hardware test (`async_hardware_test_start`), and the last
        #: one's report (never merged into `reports`).
        self._hw_run = False
        self.hardware_report: dict[str, Any] | None = None
        #: The key and attempt the running verification counts for.
        self._verify_key: str | None = None
        self._verify_attempt = 0
        #: The last firmware key read (kept while the firmware reads nothing), whether the opt-in was on at the last
        #: look, whether SpotNav took the plant over from its own optimiser since, and the notes for the card.
        self._key: str | None = None
        self._enabled_seen = False
        self.took_from_ai = False
        self.notes: list[dict[str, Any]] = []
        #: The battery section's line about a cold battery's preheating (`preheat.py`), not kept over a restart.
        self.preheat = PreheatWatch()
        #: Whether control ran (on, verifying or a test action) at the end of the last look (`_note_stopped`).
        self._running = False
        self.trial: Trial | None = None
        #: Why the routine did not start at the last look (`gate.suitable`), and the last would-do logged.
        self.waiting_for: str | None = None
        self._would_do: tuple[Any, ...] | None = None
        self._restricted: str | None = None
        self._failed: tuple[str, ...] = ()
        self._timed_out: tuple[str, ...] = ()
        self._stored: dict[str, Any] = {}
        #: What was stored last (`_async_save` writes only a change).
        self._saved: dict[str, Any] | None = None
        self._lock = asyncio.Lock()
        self._started = False
        self._closed = False
        self._shut_down = False
        self._start_task: asyncio.Task[None] | None = None
        # The site shuts down (`halt`): no look starts from the timer any more; a hardware test still hands back.
        self._halted = False
        # The store was read (`async_start`): before that, a save would write over a snapshot it holds.
        self._loaded = False
        self._look_task: asyncio.Task[None] | None = None
        self._timer_cancel: Callable[[], None] | None = None
        self._look_failures: set[str] = set()
        #: Looks that failed in a row (`FAILED_LOOKS_HAND_BACK`).
        self._failed_looks = 0
        #: The house's p90 for the running hour (`_house_peak_kw`), read once an hour.
        self._peak: tuple[datetime, float | None] | None = None
        #: When the site's energy sources were last read and why they refuse the import-limit rule (`None`: they do
        #: not); not read yet: the rule is refused (`_import_limit_refusal`).
        self._sources: tuple[datetime, str | None] | None = None
        #: The look (or status read) being answered, and `_phase_missing` as read in it.
        self._look_id = 0
        self._missing: tuple[int, str | None] | None = None
        #: For a recipe whose targets per phase are the grid's: the load behind each point and the power at it, read
        #: once a look (`_grid_phases`).
        self._grid_read: tuple[int, tuple[Phases, Phases] | None] | None = None
        battery_shadow = getattr(site, "battery_shadow", None)
        if battery_shadow is not None:
            # The outside-control watch does not count SpotNav's own writes as someone else's, and the shadow hold
            # judges a plant in AI mode as control would.
            battery_shadow.spotnav_acting = self.acting
            battery_shadow.found_states = self.found_states
            battery_shadow.takeover = lambda: self.options().ai_takeover_ack
            battery_shadow.hold_variant = self.hold_variant

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
        if self._closed or self._halted:
            return
        self._timer_cancel = async_track_time_interval(self.hass, self._on_timer, timedelta(seconds=TICK_S))

    @callback
    def halt(self) -> None:
        """The site shuts down: the look timer ends (a look running ends in `async_shutdown`). Not closed yet: a
        running hardware test is still handed back through this control first."""
        self._halted = True
        if self._timer_cancel is not None:
            self._timer_cancel()
            self._timer_cancel = None

    def holds_anything(self) -> bool:
        """Whether the battery or the export limit is still held (a snapshot not handed back)."""
        return self._holds_anything()

    async def async_save(self) -> None:
        """Store the state as it stands (a hand-back cut short at Home Assistant's stop: the next start finishes it).
        Nothing before the store was read."""
        if self._loaded:
            await self._async_save()

    @callback
    def _on_timer(self, _now: datetime) -> None:
        if self._closed or self._halted or (self._look_task is not None and not self._look_task.done()):
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
        self._loaded = True
        self._migrate_mode()
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
        if self._loaded:
            # Never before the store was read (a start cancelled early): its snapshot stays for the next start.
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
            _LOGGER.debug("async_remove_stored: ignored failure (a broken store holds nothing that can be handed back)", exc_info=True)
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
        """The site's options, from its entry as stored now (a settings write needs no reload). The acceptance of
        the HA-down risk counts only for the setup it was given for (`gate.ha_down_setup_state`)."""
        entry = self.hass.config_entries.async_get_entry(self.site.entry_id)
        data = entry.data if entry is not None else self.site.config
        options = gate.Options.from_config(data, shadow_default=self.shadow_default())
        if options.accept_ha_down and self._ha_down_setup_state(data) != "same":
            options = replace(options, accept_ha_down=False)
        return options

    def _ha_down_setup_state(self, data: Mapping[str, Any]) -> str:
        return gate.ha_down_setup_state(data.get(gate.CONF_ACCEPT_HA_DOWN_SETUP), self._main_fuse_a(), self.facts.brand)

    def _ha_down_setup_look(self) -> None:
        """At each look: an acceptance of the HA-down risk given before the brand was read is completed with it; one
        whose setup changed (another main fuse, another brand) is cleared, and logged."""
        entry = self.hass.config_entries.async_get_entry(self.site.entry_id)
        if entry is None or entry.data.get(gate.CONF_ACCEPT_HA_DOWN) is not True:
            return
        stored = entry.data.get(gate.CONF_ACCEPT_HA_DOWN_SETUP)
        state = self._ha_down_setup_state(entry.data)
        if state == "unknown" and self.facts.brand is not None and isinstance(stored, Mapping):
            data = {**entry.data, gate.CONF_ACCEPT_HA_DOWN_SETUP: gate.ha_down_setup(
                self._main_fuse_a(), self.facts.brand)}
            self.hass.config_entries.async_update_entry(entry, data=data)
        elif state == "changed":
            data = {key: value for key, value in entry.data.items() if key != gate.CONF_ACCEPT_HA_DOWN_SETUP}
            data[gate.CONF_ACCEPT_HA_DOWN] = False
            self.hass.config_entries.async_update_entry(entry, data=data)
            _LOGGER.warning(
                "SpotNav battery control: the acceptance of the Home Assistant down risk was cleared: the main fuse "
                "or the battery changed since it was given"
            )
            self._log([{"at": dt_util.now().isoformat(), "kind": "ha_down_cleared", "reason": "setup_changed"}])

    def shadow_default(self) -> bool:
        """Shadow only's default here (`gate.shadow_default`): the plant's recipe as last read (none read yet: on),
        and whether the site is a beta tester."""
        return gate.shadow_default(
            field_tested=recipe_field_tested(self.facts.brand),
            beta=learning_gate_open(self.hass, self.site.entry_id),
        )

    def _migrate_mode(self) -> None:
        """Once, at load: a site that allowed selling (`battery_control_allow_sell`, an early 3.0 build's option) and
        never chose a mode gets Max profit stored, and the legacy option is removed from the entry."""
        entry = self.hass.config_entries.async_get_entry(self.site.entry_id)
        if entry is None or gate.CONF_ALLOW_SELL not in entry.data:
            return
        data = {key: value for key, value in entry.data.items() if key != gate.CONF_ALLOW_SELL}
        mode = gate.legacy_mode(entry.data)
        if mode is not None:
            data[gate.CONF_MODE] = mode
        self.hass.config_entries.async_update_entry(entry, data=data)

    def _integration_version(self, domain: str | None) -> str | None:
        if not domain:
            return None
        try:
            version = async_get_loaded_integration(self.hass, domain).version
        except Exception:  # noqa: BLE001 - not loaded (yet): the version is unknown
            _LOGGER.debug("integration_version: ignored failure (not loaded (yet): the version is unknown)", exc_info=True)
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

    def _phases_mapped(self) -> bool:
        """Whether the plant's phases A, B and C are shown to be the meter's L1, L2 and L3: only the verification's
        mode `per_phase`, passed on the firmware last read, shows it (`gate.per_phase`). Reads the key as it stands
        (`firmware_key` would set it from the facts of this look)."""
        key = self._key
        if key is None or not gate.firmware_known(key):
            return False
        report = self.reports.get(key)
        return isinstance(report, dict) and gate.per_phase(report) == "verified"

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
        return gate.allowed_actions(self.report(), options, export_needs_charge=self._export_needs_charge())

    def _export_needs_charge(self) -> bool:
        """Whether this brand's discharge to the grid needs the charge step passed (`gate.allowed_actions`)."""
        control = control_for(self.facts.brand)
        return control is not None and control.export_signed_by_charge()

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
        self._look_id += 1
        # The facts first: the brand read decides shadow only's default (`shadow_default`).
        self.facts = self._read_facts()
        self._ha_down_setup_look()
        options = self.options()
        await self._async_read_sources(now)
        self._transitions(options, now)
        self._preheat_look(now, options)
        trial = self.trial
        withdrawn = self._takeover_withdrawn(options)
        if trial is not None and (
            now >= trial.until or not self._trial_allowed(trial, options) or (withdrawn and not trial.export)
        ):
            self._log([{"at": now.isoformat(), "kind": "trial_ended", "reason": trial.action}])
            self.trial = trial = None
        if self._start_wait():
            await self._async_start_wait_look(now, options)
            return
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
            if options.writes or (self._hw_run and options.enabled):
                # A person's hardware test runs under shadow only too (as a forced test action does); never without
                # the opt-in.
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

    def _start_wait(self) -> bool:
        """Whether the start-up wait runs (`runtime.START_GRACE`): right after a start a battery planner may not show
        its states yet, so neither the write path nor the verification starts; only hand-backs are written. Without
        the site's battery shadow (never in a running site) there is none to wait for."""
        battery_shadow = getattr(self.site, "battery_shadow", None)
        start_wait = getattr(battery_shadow, "start_wait", None)
        return bool(start_wait()) if callable(start_wait) else False

    async def _async_start_wait_look(self, now: datetime, options: gate.Options) -> None:
        """A look during the start-up wait: whatever is held is handed back, nothing else is written."""
        if self.verification is not None and not self.verification.done:
            self._end_verification(now)
        await self._async_release(now, "start_wait")
        await self._async_export_release(now, "start_wait")
        self.waiting_for = "start_wait" if self.state(options) in ("waiting", "on") else None
        if options.enabled and options.shadow_only:
            self._shadow_look(now, options)
        self._note_stopped(now)
        await self._async_save()

    def _preheat_look(self, now: datetime, options: gate.Options) -> None:
        """The preheating line (`preheat.PreheatWatch`) while SpotNav runs the battery; logged when it shows or
        clears. Read only: nothing is written to the heater."""
        controlling = self.state(options) in ("on", "verifying") or self.trial is not None
        change = self.preheat.look(now, self.facts, controlling)
        if change is not None:
            self._log([{"at": now.isoformat(), "kind": f"preheat_{change[0]}", "reason": change[1],
                        "cell_c": self.facts.cell_temperature_c}])

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
        executor = self._control_executor()
        if executor is None:
            return None
        return await self._async_step(executor, self.inputs(now, executor.control, slot=slot), allowed=allowed)

    def hold_variant(self) -> str | None:
        """The hold variant control runs on this firmware (`gate.hold_variant_to_run`); `None` for a brand with one
        hold. The shadow and its previews show the same."""
        control = control_for(self.facts.brand)
        names = () if control is None else control.variant_names()
        return (gate.hold_variant_to_run(self.report(), names) or names[0]) if names else None

    def _control_executor(self) -> Executor | None:
        """The executor for control and trials, running the hold variant the verification passed, else the first not
        recorded as failed, else the brand's first (once it holds nothing; `Executor.use_variant`). A test hold is
        refused when every variant failed (`async_trial`)."""
        executor = self._executor()
        if executor is not None and executor.control.hold_variants:
            names = executor.control.variant_names()
            executor.use_variant(gate.hold_variant_to_run(self.report(), names) or names[0])
        self._sync_phases()
        return executor

    # ---- per phase

    def _sync_phases(self) -> bool:
        """Have the executor run the brand's targets per phase when they are there and verified, its plain control
        otherwise (changed only while it holds nothing). Whether it runs them now."""
        executor = self.executor
        if executor is None:
            return False
        executor.use_phases(self._phased_wanted())
        return executor.control.phased

    def _phased_wanted(self) -> bool:
        return gate.per_phase(self.report()) == "verified" and self._phase_missing() is None

    def _phase_missing(self) -> str | None:
        """Why a grid charge per phase cannot run on this plant now, as a stable code; `None` when everything it
        needs is there: the brand's targets per phase and what they require, found, enabled and reading so; a
        three-phase plant; the plant's own power per phase; the site's reading of each phase."""
        cached = self._missing
        if cached is not None and cached[0] == self._look_id:
            return cached[1]
        missing = self._phase_missing_now()
        self._missing = (self._look_id, missing)
        return missing

    def _phase_missing_now(self) -> str | None:
        """`_phase_missing`, read afresh (it walks the registry: once a look, `_look_id`)."""
        control = control_for(self.facts.brand)
        spec = None if control is None else control.phase_control
        if control is None or spec is None:
            return "not_supported"
        if not spec.three_phase_by_targets and self._plant_phases(control) != 3:
            # Where the three targets exist only on a three-phase plant, finding them (below) is what shows it.
            return "not_three_phase"
        reads = read_role_reads(self.hass, self.site.config, control.with_phases(True))
        for role in spec.owned():
            read = reads.get(role.name)
            if read is None or not read.found:
                return f"control_not_found:{role.name}"
            if not read.enabled:
                return f"control_disabled:{role.name}"
        for role, states, why in spec.requires:
            if reading(role, reads[role.name].state) not in states:
                return why
        if spec.behind is not None and spec.point is not None:
            # The recipe's own sensors at its point give the plant's power per phase (`_grid_phases`).
            if self._grid_phases() is None:
                return "plant_phase_power"
        elif self.facts.plant_phase_entities is None:
            return "plant_phase_power"
        if self._phase_headroom_a() is None or self._grid_phase_kw() is None:
            return "site_phases"
        return None

    def _grid_phases(self) -> tuple[Phases, Phases] | None:
        """For a brand whose targets per phase are the grid's, with sensors at their point
        (`commands.PhaseControl.grid_phases`): the load behind each phase's point and the power at the point now
        (kW); `None` for any other brand, or when a sensor is missing, disabled or reads nothing. Read once a look
        (it walks the registry), whichever control the executor runs now."""
        cached = self._grid_read
        if cached is not None and cached[0] == self._look_id:
            return cached[1]
        control = control_for(self.facts.brand)
        spec = None if control is None else control.phase_control
        found: tuple[Phases, Phases] | None = None
        if control is not None and spec is not None and spec.behind is not None and spec.point is not None:
            try:
                reads = read_role_reads(self.hass, self.site.config, control.with_phases(True))
                found = spec.grid_phases({
                    name: read.state for name, read in reads.items() if read.found and read.enabled
                })
            except Exception:  # noqa: BLE001 - unknown: no charge per phase runs without them
                self._look_failed("grid_phases")
        self._grid_read = (self._look_id, found)
        return found

    def _phase_headroom_a(self) -> Phases | None:
        """Each phase's headroom now (A), `None` when one is unknown."""
        headroom = self.site.result.phase_headroom_a
        values = [headroom.get(phase) for phase in gate.PHASE_NAMES]  # type: ignore[call-overload]
        if not all(value is not None and math.isfinite(value) for value in values):
            return None
        return (float(values[0]), float(values[1]), float(values[2]))

    def _grid_phase_kw(self) -> Phases | None:
        """The site meter's power per phase (kW, + import), `None` when one is unknown."""
        signed = self.site.result.phase_signed_active_power_w
        values = [signed.get(phase) for phase in gate.PHASE_NAMES]  # type: ignore[call-overload]
        if not all(value is not None and math.isfinite(value) for value in values):
            return None
        return (values[0] / 1000.0, values[1] / 1000.0, values[2] / 1000.0)

    def _plant_phase_kw(self, now: datetime) -> Phases | None:
        """The plant's own power per phase (kW, + output; the adapter's `plant_phase_power`), `None` when it is not
        read or older than a battery reading may be."""
        try:
            watts = plant_phase_w(self.site, BATTERY_MAX_AGE_S, now.timestamp())
        except Exception:  # noqa: BLE001 - unknown: a charge per phase then counts none of its own draw back
            self._look_failed("plant_phase_power")
            return None
        return None if watts is None else (watts[0] / 1000.0, watts[1] / 1000.0, watts[2] / 1000.0)

    def _phase_caps_kw(
        self, now: datetime, *, held_off: bool = False, accept: bool | None = None, no_cars: bool = False,
    ) -> Phases:
        """The HA-down cap of a grid charge on each phase (`gate.grid_charge_phase_caps_kw`) on the site's readings
        now, by the same house peaks, chargers and margin as the balanced cap. `held_off`: a person's hardware test
        runs, and the chargers its hold keeps stopped count as it says (`_cars_allowed_a`). `accept`: whether the
        person accepted the HA-down risk (`battery_control_accept_ha_down`), by default as stored now. `no_cars`:
        the chargers and SpotNav's own heat counted as drawing nothing, for the status's offer only."""
        control = control_for(self.facts.brand)
        return gate.grid_charge_phase_caps_kw(
            self._main_fuse_a(), stored_voltage_between_phases_v(self.site.config), margin_a=HEADROOM_MARGIN_A,
            cars_a=0.0 if no_cars else self._cars_allowed_a(held_off=held_off) + self._heat_standing_a(),
            house_peak_kw=self._house_peak_kw(now),
            house_phase_peaks_a=self._phase_peaks(now) or None,
            # A plant that stops following its targets by itself when the writes stop (the phase spec's own measured
            # expiry).
            expiry_lifts=self._expiry_lifts(control, phased=True),
            accept_ha_down=(self.options().accept_ha_down if accept is None else accept) and _accept_applies(control),
            site_margin_a=self._site_margin_a(),
        )

    def _charge_limits(
        self, now: datetime, options: gate.Options, *, phased: bool | None = None, no_cars: bool = False,
    ) -> tuple[float | None, Phases | None]:
        """The HA-down cap a grid charge runs under now (kW, `None`: the plant's own import limit guards the fuse)
        and, when it runs per phase, each phase's cap (the first is then their sum). Per phase only on a control
        that runs the targets (`phased`, by default the executor's as synced now), verified and with everything
        there, and only when the phases allow at least `PER_PHASE_GAIN_KW` more than the balanced cap (always, once the
        site accepted the HA-down risk: both are then the empty house's fuse)."""
        cap = self._grid_charge_cap_kw(now, options, no_cars=no_cars)
        if phased is None:
            phased = self._sync_phases()
        if cap is None or not phased or not self._phased_wanted():
            return cap, None
        caps = self._phase_caps_kw(now, accept=options.accept_ha_down, no_cars=no_cars)
        total = sum(caps)
        if options.accept_ha_down and _accept_applies(control_for(self.facts.brand)):
            # Accepted: the phases' caps and the balanced one are both the empty house's fuse, so there is no gain to
            # weigh; the charge per phase keeps running where it is verified (and where `charge` was shown only per
            # phase, it is the one grid charge there is).
            return total, caps
        if total < cap + PER_PHASE_GAIN_KW:
            return cap, None
        return total, caps

    def _per_phase_status(self, now: datetime, options: gate.Options) -> dict[str, Any] | None:
        """The capability for the status: `state` (`verified`, `pending`, `failed`, or `unavailable` with `why`,
        `_phase_missing`), `used` (a grid charge now would run per phase), `caps_kw` (each phase's HA-down cap) and
        `charging_kw` (what a running charge per phase is commanded at). `None` for a brand without targets per
        phase."""
        control = control_for(self.facts.brand)
        if control is None or control.phase_control is None:
            return None

        def per_phase(values: Phases) -> dict[str, float]:
            return {phase: round(value, 2) for phase, value in zip(gate.PHASE_NAMES, values, strict=True)}

        missing = self._phase_missing()
        executor = self.executor
        state = None if executor is None else executor.state
        probing = self.verification is not None and not self.verification.done
        running = (
            state.phase_kw if state is not None and state.phase == "active" and state.action == ACTION_GRID_CHARGE
            and not probing else None
        )
        phased = executor is not None and executor.control.phased
        return {
            "state": "unavailable" if missing is not None else gate.per_phase(self.report()),
            "why": missing,
            "used": self._charge_limits(now, options, phased=phased)[1] is not None,
            "caps_kw": per_phase(self._phase_caps_kw(now)),
            "charging_kw": None if running is None else per_phase(running),
        }

    def _curtailing(self) -> bool:
        """Whether SpotNav holds the site's export limit at 0 now (or is handing it back)."""
        export = self.export_executor
        return export is not None and export.state.phase in ("active", "restoring")

    def write_budget(self) -> dict[str, dict[str, int]]:
        """The day's writes per class (`commands.WriteClass`) for the diagnostics: the brand's cap, what is used
        (the settings' count holds the export limit's writes too) and what is left."""
        executor = self.executor
        if executor is None:
            return {}
        today = dt_util.now().date().isoformat()
        state = executor.state
        fresh = state.day == today
        used = {
            "setting": (state.writes_today if fresh else 0) + self._export_writes_today(dt_util.now()),
            "setpoint": state.setpoint_writes_today if fresh else 0,
        }
        return {
            name: {"cap": executor.control.write_cap(name), "used": count,
                   "left": max(executor.control.write_cap(name) - count, 0)}
            for name, count in used.items()
        }

    def _phase_charge_possible(self) -> bool:
        """Whether the verification's `charge` may be tried per phase: the brand's targets per phase and everything
        they need are there (`_phase_missing`), and the capability has not failed on this firmware."""
        return self._phase_missing() is None and gate.per_phase(self.report()) != "failed"

    def _per_phase_due(self, report: dict[str, Any] | None) -> bool:
        """Whether a run should try the mode `per_phase`: everything it needs is there, it is still undecided on
        this firmware, and the routine has runs left and has not failed."""
        attempts = (report or {}).get("attempts")
        return (
            gate.per_phase(report) == "pending" and gate.report_status(report) != "failed"
            and not (isinstance(attempts, int) and attempts >= gate.MAX_ATTEMPTS)
            and self._phase_missing() is None
        )

    async def async_tick(self, now: datetime | None = None, *, stopping: str | None = None) -> StepResult | None:
        """One look of control as it runs the plan: refused unless control is on (the routine passed)."""
        self._look_id += 1
        options = self.options()
        self._require(self.state(options) == "on", "Battery control")
        now = now or dt_util.now()
        await self.async_export_tick(now, stopping=stopping)
        executor = self._control_executor()
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
        self._export_failed, self._export_timed_out = await self._async_writes(result, executor, allowed=allowed)
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
        self._look_id += 1
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
        modes = gate.BATTERY_MODES if previous is None else gate.modes_to_run(previous)
        if self._per_phase_due(previous):
            modes = (*modes, gate.MODE_PER_PHASE)
        expiry = self._expiry_to_test() if self._expiry_due(previous) else None
        if expiry is not None:
            # Tried only after a hold that passed in this same run.
            modes = (*modes, *(() if "hold" in modes else ("hold",)), gate.MODE_EXPIRY)
        self.verification = Verification(
            executor, now, firmware=self.facts.firmware, modes=modes, tried=gate.variants_tried(previous),
            expiry=None if expiry is None else expiry[0], expiry_phased=expiry is not None and expiry[1],
            phase_mapped=gate.per_phase(previous) == "verified",
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
        if verification is not None and self._hw_run:
            # The hardware test's report is its own; the routine's per firmware is untouched.
            self._hw_run = False
            self.hardware_report = {**verification.report(), "completed": verification.done}
            self._log([{"at": now.isoformat(), "kind": "hardware_test_done" if verification.done else
                        "hardware_test_stopped", "modes": {
                            result.mode: result.outcome for result in verification.results}}])
            self.verification = None
            return
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
        sample = Sample(
            inputs.facts.power_kw, inputs.battery_age_s, inputs.facts.soc_pct, inputs.house_deficit_kw,
            inputs.grid_import_kw if inputs.measurement_ok else None,
            self._grid_phase_kw() if inputs.measurement_ok else None, inputs.plant_phase_kw,
            curtailing=self._curtailing(), soc_min_pct=inputs.facts.reserve_soc_pct or 0.0,
            headroom_a=inputs.headroom_a,
        )
        action, rate = verification.command(now, sample, inputs.facts.max_charge_kw)
        probe = verification.probe()
        if action == ACTION_GRID_CHARGE and probe is None:
            # The mode `charge` proves the brand's own balanced charge, under the balanced cap.
            cap = self._grid_charge_cap_kw(now, self.options(), held_off=self._hw_run)
            if cap is not None and cap < gate.MIN_RATE_KW:
                if self._phase_charge_possible():
                    # An unbalanced house leaves the balanced charge no room: a charge on the phase with the most
                    # room instead, within that phase's own HA-down cap (`verify.phase_charge_step`), after a
                    # discharge on it has shown its sign and mapping unless `per_phase` has on this firmware.
                    step = phase_charge_step(
                        self._phase_caps_kw(now, held_off=self._hw_run),
                        stored_voltage_between_phases_v(self.site.config),
                    )
                    verification.charge_per_phase(
                        now, sample, step, mapped=gate.per_phase(self.report()) == "verified"
                    )
                else:
                    # No grid charge fits under the HA-down cap now: tried again at a later run.
                    verification.not_judgeable(now, "grid_charge_cap")
                action, rate = verification.command(now, sample, inputs.facts.max_charge_kw)
                probe = verification.probe()
            rate = rate if cap is None or probe is not None else min(rate, cap)
        # The probe of `per_phase` is a discharge on one phase (`Verification.probe`), which needs no cap; `charge`
        # tried per phase is a charge on one phase, under the phases' HA-down caps and the executor's live room per
        # phase as any charge per phase. The reads are the control's as it is now (a phased mode has just made it
        # the phased one).
        charging = probe is not None and any(value > 0 for value in probe)
        inputs = replace(
            inputs, action=action, rate_kw=rate, slot_end=None,
            phase_caps_kw=self._phase_caps_kw(now, held_off=self._hw_run) if charging else None, phase_probe_kw=probe,
            reads=self._reads(executor.control),
            # The mode `expiry`'s silence: nothing renews the command (`verify.Verification.expiry_probe`).
            expiry_probe=verification.expiry_probe(),
        )
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
        if state not in ("waiting", "on") or not (
            gate.needs_run(report) or self._per_phase_due(report) or self._expiry_due(report)
        ):
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

    # ---- a person's hardware test

    def hardware_test_refusal(self) -> str | None:
        """Why the battery part of a hardware test cannot start now, `None` when it can: the opt-in, everything the
        person still has to do (`_needs`: an acknowledgement, a control to enable), a known firmware, nothing else
        running on the plant."""
        if not self._started or self._closed or self._start_wait():
            return "starting"
        if control_for(self.facts.brand) is None:
            return "no_battery_control"
        if not self.options().enabled:
            return "not_enabled"
        needs = self._needs()
        if needs:
            return f"needs:{needs[0]}"
        if self.trial is not None:
            return "trial"
        if self.verification is not None and not self.verification.done:
            return "verifying"
        return None

    async def async_hardware_test_start(self) -> str | None:
        """Start the battery part of a person's hardware test (`verify.Timing.quick`, `stop_on_fail`): hold, a small
        grid charge under the HA-down cap, a discharge for the house and the hand-back, through the executor with
        every rule. Answers the refusal code, `None` once it started."""
        why = self.hardware_test_refusal()
        if why is not None:
            return why
        async with self._lock:
            now = dt_util.now()
            executor = self._executor()
            if executor is None:
                return "no_battery_control"
            self.verification = Verification(
                executor, now, firmware=self.facts.firmware, modes=gate.BATTERY_MODES,
                tried=gate.variants_tried(self.report()), timing=Timing.quick(), stop_on_fail=True,
                charge_soc_max_pct=TEST_CHARGE_SOC_MAX_PCT,
            )
            self._hw_run = True
            self._verify_key = None
            self.hardware_report = None
            self._log([{"at": now.isoformat(), "kind": "hardware_test_started", "reason": self.firmware_key()}])
            _LOGGER.info("SpotNav battery control: a hardware test started (%s)", self.firmware_key())
            await self._async_look(now)
        return None

    async def async_hardware_test_stop(self, reason: str) -> None:
        """End a running hardware test at once and hand the plant back (its deadline, an anomaly elsewhere, an
        unload). Nothing when none runs."""
        async with self._lock:
            if not self._hw_run:
                return
            self._end_verification(dt_util.now())
            if self.executor is not None and self._holding(self.executor):
                await self._async_until_handed_back(reason)
            await self._async_save()

    def hardware_test_progress(self) -> dict[str, Any]:
        """The battery part as the test runner reads it: running or not, the mode and stage now, and the report so
        far (the finished one once done)."""
        verification = self.verification if self._hw_run else None
        current = None if verification is None else verification.current
        executor = self.executor
        return {
            "running": verification is not None,
            "mode": None if current is None else current.mode,
            "stage": None if verification is None else verification.stage,
            "report": verification.report() if verification is not None else self.hardware_report,
            "holding": executor is not None and self._holding(executor),
            "firmware_key": self.firmware_key(),
        }

    # ---- the settings

    async def async_update_options(self, changes: dict[str, Any]) -> dict[str, Any]:
        """Write some of the site's options (`gate.WRITABLE`: booleans, the battery mode and the wear) to its entry,
        without a reload (a wear of `None` removes the key: the currency's default); a changed mode or wear re-plans
        at once (the comparison's \"in force\" row moves with it); act
        on them at once: turned off or back to shadow only, the plant is handed back before this returns; turned on,
        the next look starts the routine. Answers the status."""
        wear_default = self.wear_default()
        unknown = [
            key for key, value in changes.items() if not gate.valid_change(key, value, wear_default=wear_default)
        ]
        if unknown:
            raise ValueError(f"not a battery control option: {', '.join(sorted(unknown))}")
        entry = self.hass.config_entries.async_get_entry(self.site.entry_id)
        if entry is None:
            raise ValueError("the site is not loaded")
        # Under the lock: a look in progress finishes with the options it read before this write changes them.
        async with self._lock:
            before = self.options()
            stamped = dict(changes)
            if changes.get(gate.CONF_ACCEPT_HA_DOWN) is True:
                # Given for this setup: another main fuse or another brand clears it (`_ha_down_setup_look`).
                stamped[gate.CONF_ACCEPT_HA_DOWN_SETUP] = gate.ha_down_setup(self._main_fuse_a(), self.facts.brand)
            data = {**entry.data, **stamped}
            if gate.CONF_WEAR in stamped and stamped[gate.CONF_WEAR] is None:
                data.pop(gate.CONF_WEAR)
            self.hass.config_entries.async_update_entry(entry, data=data)
            after = self.options()
            _LOGGER.info("SpotNav battery control: options %s", after.as_dict())
            if before.mode != after.mode or before.wear_minor_per_kwh != after.wear_minor_per_kwh:
                self.site.battery_shadow.plan.mode_changed(dt_util.now())
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
        self._look_id += 1
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
        if self._start_wait():
            # The start-up wait (`runtime.START_GRACE`): a planner may not be back yet.
            raise TrialRefused("starting")
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
            executor = self._executor()
            if executor is None:
                raise TrialRefused("no_battery_control")
            names = executor.control.variant_names()
            if action == ACTION_HOLD and names and gate.hold_variant_to_run(self.report(), names) is None:
                # Every way to hold failed on this firmware: not even a forced test runs one again.
                raise TrialRefused("hold_variants_failed")
            self.facts = self._read_facts()
            rate = 0.0
            if action in (ACTION_GRID_CHARGE, ACTION_EXPORT_DISCHARGE):
                rate = self._bounded_rate(action, abs(power_kw) if power_kw is not None else TEST_CHARGE_KW)
            if action == ACTION_GRID_CHARGE:
                cap = self._charge_limits(now, options)[0]
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
            cap = self._charge_limits(now, options)[0]
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
        control = control_for(self.facts.brand, self.hold_variant())
        if control is None:
            return
        # As control would run it: the brand's targets per phase where they are there and verified.
        control = control.with_phases(self._phased_wanted())
        allowed = _PLAN_ACTIONS | ({ACTION_EXPORT_DISCHARGE} if options.allow_sell else set())
        slot, why = gate.restricted(
            self._merged_slot(now), allowed, self._charge_limits(now, options, phased=control.phased)[0],
            self._export_cap_kw(), self._fixed_charge_kw(), hold_charges=self._hold_charges(),
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
        control = control_for(self.facts.brand)
        # A slow control (a cloud) runs planned actions only: no hold for a car, which comes and goes in minutes.
        hold_requested = self.site.battery_shadow.hold_requested() and not (control is not None and control.slow)
        return gate.merged_action(self._plan_now(now), hold_requested)

    def _fixed_charge_kw(self) -> float | None:
        """The rate a plant charges at when the recipe cannot set it (`commands.fixed_rate_kw`), `None` otherwise."""
        control = control_for(self.facts.brand)
        return None if control is None else fixed_rate_kw(control, self.facts)

    def _hold_charges(self) -> bool:
        """Whether a hold on this plant may itself charge from the grid at its fixed rate: a role whose absence lets it
        (`BrandControl.hold_charges_without`) is not found."""
        control = control_for(self.facts.brand)
        if control is None or not control.hold_charges_without:
            return False
        reads = read_role_reads(self.hass, self.site.config, control)
        return any((read := reads.get(name)) is None or not read.found for name in control.hold_charges_without)

    def _control_slot(self, now: datetime, options: gate.Options) -> tuple[str, float, datetime | None] | None:
        report = self.report()
        allowed = gate.allowed_actions(report, options, export_needs_charge=self._export_needs_charge())
        cap, caps = self._charge_limits(now, options)
        if caps is None and gate.charge_by_phases(report):
            # The charge was shown only per phase: the balanced one is never run on that.
            allowed = allowed - {ACTION_GRID_CHARGE}
        slot, why = gate.restricted(
            self._merged_slot(now), allowed, cap, self._export_cap_kw(), self._fixed_charge_kw(),
            hold_charges=self._hold_charges(),
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

    def _grid_charge_cap_kw(
        self, now: datetime, options: gate.Options, *, peaks: Mapping[str, float] | None | bool = True,
        held_off: bool = False, no_cars: bool = False,
    ) -> float | None:
        """The HA-down grid charge cap (`gate.grid_charge_cap_kw`) on the site's readings now. `peaks`: the house's
        per-phase peaks to reckon with instead of the ones in use now (`_phase_peaks`), `None` for the conservative
        rule; for the status, which says what the cap would be either way. `held_off`: a person's hardware test
        runs, and the chargers its hold keeps stopped count as it says (`_cars_allowed_a`). `no_cars`: the chargers
        and SpotNav's own heat counted as drawing nothing, for the status's offer only (`_ha_down_status`). The
        person's acceptance of the HA-down risk is the options' (`accept_ha_down`)."""
        if peaks is True:
            peaks = self._phase_peaks(now)
        control = control_for(self.facts.brand)
        guards = options.import_limit_acts and self._import_limit_refusal() is None
        # Held per phase: every reading the recipe's fact needs, read again now (each start and each look).
        per_phase = self._import_limit_phase_refusal() is None
        import_limit_kw = self._import_limit_kw(control) if guards or per_phase else None
        return gate.grid_charge_cap_kw(
            self._main_fuse_a(), stored_voltage_between_phases_v(self.site.config), margin_a=HEADROOM_MARGIN_A,
            cars_a=0.0 if no_cars else self._cars_allowed_a(held_off=held_off) + self._heat_standing_a(),
            house_peak_kw=self._house_peak_kw(now), plant_phases=self._plant_phases(control), import_limit_kw=import_limit_kw,
            import_limit_acts=options.import_limit_acts, house_phase_peaks_a=peaks or None,
            import_limit_per_phase=per_phase,
            # The per-phase rule and the accepted HA-down risk: the site's own safety margin as the person set it.
            phase_margin_a=self._site_margin_a(),
            accept_ha_down=options.accept_ha_down and _accept_applies(control),
            expiry_lifts=self._expiry_lifts(control, phased=False),
        )

    def _expiry_lifts(self, control: BrandControl | None, *, phased: bool) -> bool:
        """Whether the grid charge's own expiry stands in for its HA-down cap (`gate.expiry_lifts`): the phase spec's
        for a charge per phase (`phased`), else the recipe's own for the grid charge (`expiry_by_action`, then
        `expiry`: never the phase spec's, which has nothing to end a balanced command). Measured only on the
        firmware read now (`gate.expiry_measured`). `control` is the brand's control as `control_for` gives it."""
        expiry = _grid_charge_expiry(control, phased=phased)
        if control is None or expiry is None:
            return False
        base = control.with_phases(False)
        return gate.expiry_lifts(
            expiry, measured=self._expiry_measured(expiry), slow=base.slow, fixed_charge=base.fixed_charge
        )

    def _expiry_measured(self, expiry: Expiry) -> bool:
        """Whether the expiry was seen to act on the firmware read now (`gate.expiry_measured`): recorded in the
        recipe, or the routine's mode `expiry` obeyed for it in this firmware's report (never the hardware test's)."""
        key = self.firmware_key() if self.firmware_known() else None
        return gate.expiry_measured(expiry, key, self.report())

    def _expiry_to_test(self) -> tuple[Expiry, bool] | None:
        """The expiry the verification's mode `expiry` may try on this plant, and whether it is the phase spec's
        (`gate.expiry_testable`: one whose pass would lift a cap), not yet measured on this firmware: the hold's own
        where its renewed roles are all in the hold's writes, else the phase spec's where a charge per phase can run
        here. `None` for every other plant (Sigenergy: no expiry)."""
        control = control_for(self.facts.brand)
        if control is None:
            return None
        testable = {"slow": control.slow, "fixed_charge": control.fixed_charge}
        hold = control.states.get(ACTION_HOLD)
        expiry = control.expiry
        if hold is not None and gate.expiry_testable(expiry, **testable) and not self._expiry_measured(expiry):
            written = {want.role for want in hold(0.0, int(expiry.window_s or 0))}
            if set(expiry.renewed_roles) <= written:
                return expiry, False
        spec = control.phase_control
        if spec is not None and gate.expiry_testable(spec.expiry, **testable) and not self._expiry_measured(
            spec.expiry
        ) and self._phase_missing() is None:
            return spec.expiry, True
        return None

    def _expiry_due(self, report: dict[str, Any] | None) -> bool:
        """Whether a run should try the mode `expiry`: there is one to try (`_expiry_to_test`), still undecided for
        it on this firmware, control has passed here, the routine has runs left, and for the phase spec's the mode
        `per_phase` passed or is still to be tried."""
        found = self._expiry_to_test()
        if found is None or gate.report_status(report) != "passed":
            return False
        attempts = (report or {}).get("attempts")
        if isinstance(attempts, int) and attempts >= gate.MAX_ATTEMPTS:
            return False
        expiry, phased = found
        if not gate.expiry_due(report, expiry):
            return False
        return not phased or gate.per_phase(report) == "verified" or self._per_phase_due(report)

    def _expiry_block(self) -> dict[str, Any] | None:
        """The grid charge's own expiry, balanced and per phase, for the diagnostics only (the card does not know
        these keys): what it is, whether it is measured here and lifts the HA-down cap, its line
        (`gate.expiry_note`) and the kWh a charge at the battery's rated power may still take after Home Assistant
        dies (`ha_down_energy_kwh`, `gate.expiry_energy_kwh`; `None`: unbounded). `None` for a brand SpotNav does not
        write to."""
        try:
            control = control_for(self.facts.brand)
            if control is None:
                return None
            rated = self.facts.max_charge_kw
            block: dict[str, Any] = {}
            for name, phased in (("balanced", False), ("per_phase", True)):
                expiry = _grid_charge_expiry(control, phased=phased)
                if expiry is None:
                    block[name] = None
                    continue
                measured = self._expiry_measured(expiry)
                energy = None if rated is None else gate.expiry_energy_kwh(expiry, rated)
                block[name] = {
                    "expiry_id": expiry.expiry_id, "kind": expiry.kind, "renewer": expiry.renewer,
                    "window_s": expiry.window_s, "evidence": expiry.evidence, "measured": measured,
                    "lifts": self._expiry_lifts(control, phased=phased),
                    "note": gate.expiry_note(expiry, measured=measured),
                    "rate_kw": rated, "ha_down_energy_kwh": None if energy is None else round(energy, 3),
                }
            return block
        except Exception:  # noqa: BLE001 - diagnostics must never fail on one block
            _LOGGER.debug("expiry_block: ignored failure (diagnostics must never fail on one block)", exc_info=True)
            return None

    def _log_expiry(self, executor: Executor, events: list[dict[str, Any]]) -> None:
        """A grid charge entered or changed under an expiry: the kWh it may still take after Home Assistant dies
        (`ha_down_energy_kwh`, `None`: unbounded) in the control log. Nothing for a control without one (Sigenergy's
        log stays as it was)."""
        for event in events:
            if event.get("kind") not in ("enter", "change") or event.get("reason") != ACTION_GRID_CHARGE:
                continue
            phases = event.get("phase_kw")
            phased = executor.control.phased and isinstance(phases, list)
            expiry = _grid_charge_expiry(executor.control, phased=phased)
            if expiry is None or expiry.kind == "none":
                continue
            rate = sum(float(value) for value in phases) if phased else event.get("rate_kw")  # type: ignore[union-attr]
            energy = gate.expiry_energy_kwh(expiry, rate) if isinstance(rate, (int, float)) else None
            self._log([{
                "at": event.get("at"), "kind": "ha_down_energy", "reason": expiry.kind,
                "ha_down_energy_kwh": None if energy is None else round(energy, 3),
                "note": gate.expiry_note(expiry, measured=self._expiry_measured(expiry)),
            }])

    def _import_limit_kw(self, control: BrandControl | None) -> float | None:
        """What the plant's own grid import limit reads now (kW), `None` when the brand has none, it is disabled or
        it was never set. Written only by an administrator's tap (`async_set_import_limit`), lowered, never raised."""
        if control is None:
            return None
        return _float(read_plant_entity(self.hass, self.site.config, control.import_limit))

    async def _async_read_sources(self, now: datetime) -> None:
        """Read, at most every `SOURCES_REFRESH_S`, whether the site has a sun source that is not the plant's own
        (the Energy dashboard's solar statistics against the battery's integration entry). Never raises: a read that
        fails refuses the import-limit rule until the next one."""
        if self._sources is not None and 0 <= (now - self._sources[0]).total_seconds() < SOURCES_REFRESH_S:
            return
        try:
            prefs = await async_energy_prefs(self.hass)
            solar = () if prefs is None else energy_statistic_ids(prefs)[0]
            reason = self._foreign_sun(solar)
        except Exception:  # noqa: BLE001 - unknown sources guard nothing
            self._look_failed("energy_sources")
            reason = "sources_unknown"
        self._sources = (now, reason)

    def _foreign_sun(self, statistic_ids: tuple[str, ...]) -> str | None:
        """`foreign_pv` when one of the Energy dashboard's solar statistics is not an entity of the battery's own
        integration entry (another inverter, a statistic that is no entity), else `None`."""
        if not statistic_ids:
            return None
        from homeassistant.helpers import entity_registry as er  # noqa: PLC0415 - kept beside its only use

        registry = er.async_get(self.hass)
        entity_id = self.site.config.get(CONF_BATTERY_AGGREGATE_POWER_ENTITY)
        battery = registry.async_get(entity_id) if isinstance(entity_id, str) and entity_id else None
        own = None if battery is None else battery.config_entry_id
        for statistic_id in statistic_ids:
            entry = registry.async_get(statistic_id)
            if own is None or entry is None or entry.config_entry_id != own:
                return "foreign_pv"
        return None

    def _import_limit_refusal(self, *, dc_charger: bool = True) -> str | None:
        """Why the plant's own import limit cannot be the fuse's guard on this site whatever it reads, as a stable
        code; `None` when it can. The rule (`gate.import_limit_max_kw`) rests on the plant seeing every source and
        spreading its charge evenly over three phases, so it is refused where the recipe does not trust the limit here
        (`not_trusted`, `_import_limit_trusted`: not shown to act in its grid charge, or on another firmware), for a
        plant that is not three-phase
        (`plant_not_three_phase`: a single-phase source), a sun source that is not the plant's (`foreign_pv`: it
        lowers the total the plant sees while one phase carries more), sources not read yet (`sources_unknown`), and
        a charger run by power through the plant (`dc_charger`: Sigenergy's DC charger, which can also put out, V2G;
        `dc_charger` false leaves that one out, for that charger's own fuse cap)."""
        control = control_for(self.facts.brand)
        if control is None or control.import_limit is None:
            return "not_supported"
        if not self._import_limit_trusted(control):
            return "not_trusted"
        if self._plant_phases(control) != 3:
            return "plant_not_three_phase"
        if self._sources is None:
            return "sources_unknown"
        if self._sources[1] is not None:
            return self._sources[1]
        if dc_charger:
            for charger_entry_id in self.site.config.get(CONF_CHARGER_ENTRY_IDS) or []:
                entry = self.hass.config_entries.async_get_entry(charger_entry_id)
                if entry is not None and entry.data.get(CONF_CURRENT_CONTROL) == CURRENT_CONTROL_POWER:
                    return "dc_charger"
        return None

    def _import_limit_trusted(self, control: BrandControl) -> bool:
        """Whether the recipe trusts the plant's own import limit here (`commands.ImportLimitTrust`): not marked
        untrusted, shown to act in the recipe's grid charge, and on the plant's firmware as read (the last reading,
        `firmware_key`; one never read is not trusted where the recipe names its firmwares)."""
        trust = control.import_limit_trust
        if trust is None or trust.untrusted or ACTION_GRID_CHARGE not in trust.acts_in:
            return False
        if trust.firmware is None:
            return True
        if not self.firmware_known():
            return False
        return self.firmware_key().split(" | ", 1)[0] in trust.firmware

    def _site_margin_a(self) -> float:
        """The site's own safety margin (A per phase) as stored now, for the per-phase import-limit rule only."""
        entry = self.hass.config_entries.async_get_entry(self.site.entry_id)
        return stored_safety_margin_a(entry.data if entry is not None else self.site.config)

    def _import_limit_phase_refusal(self, *, assume_acts: bool = False) -> str | None:
        """Why the plant's own import limit cannot be taken as held **per phase** now (`commands.ImportLimitPhases`),
        as a stable code; `None` when it can. Fails closed: anything unread refuses. `not_supported` (the recipe states
        no such hold), `not_field_tested` (the owner's on-site test is not recorded in the recipe), every reason of the
        total rule (`_import_limit_refusal`: a plant not three-phase, another inverter's sun, the sources unread, a
        charger run through the plant), `output_type_unread` (the plant's Output Type reads nothing: the brand's
        default is not taken for it), `output_type_not_supported` (it reads other than the one the switch acts with,
        `ImportLimitPhases.output_type`: Sigenergy's L1/L2/L3/N, not L1/L2/L3), `phase_control_off` (the switch
        that shares the plant's power per phase reads anything but on, or nothing) and `grid_sensor_offline` (the
        plant's grid sensor, which the limit regulates on, reads anything but connected, or nothing), and last
        `not_said_to_act` (the site's own `battery_control_import_limit_acts` is off: the recipe's fact is one plant's
        test, so each site still says that its plant's limit acts; `assume_acts` leaves it out)."""
        control = control_for(self.facts.brand)
        spec = None if control is None else control.import_limit_phases
        if control is None or spec is None or control.import_limit is None:
            return "not_supported"
        if not spec.field_tested:
            return "not_field_tested"
        refusal = self._import_limit_refusal()
        if refusal is not None:
            return refusal
        output = read_plant_entity(self.hass, self.site.config, control.output_type)
        if output is None:
            return "output_type_unread"
        if str(output).lower() != spec.output_type:
            return "output_type_not_supported"
        if read_plant_entity(self.hass, self.site.config, spec.switch) != "on":
            return "phase_control_off"
        if read_plant_entity(self.hass, self.site.config, spec.grid_sensor) != spec.sensor_online:
            return "grid_sensor_offline"
        # Last, so a step the site still needs (or a reason that rules the hold out) is named before the person's word.
        if not assume_acts and not self.options().import_limit_acts:
            return "not_said_to_act"
        return None

    def _phase_path_open(self) -> bool:
        """Whether the plant's own per-phase hold is a way the person can take here: the recipe has it, field-tested,
        and nothing about the site rules it out whatever the person does (another inverter's sun, a plant that is not
        three-phase, a charger run through the plant, an Output Type it does not act with). What is left for the
        person (saying it acts, setting the limit, enabling an entity, turning the phase switch on) keeps it open. Where
        it is open the HA-down acceptance is not offered: the plant can guard the fuse without accepting a risk."""
        return self._import_limit_phase_refusal(assume_acts=True) in PHASE_PATH_STEPS

    def import_limit_target_kw(self) -> float | None:
        """The value the person's tap sets the plant's import limit to (`async_set_import_limit`): the most that keeps
        every phase under the fuse less the site's own safety margin when the plant holds each phase at a third of it
        (`gate.import_limit_phase_max_kw`), rounded down to 0.1 kW; `None` when no value guards or it is under the
        least rate a grid charge runs at."""
        most = gate.import_limit_phase_max_kw(
            self._main_fuse_a(), stored_voltage_between_phases_v(self.site.config),
            margin_a=self._site_margin_a(), cars_a=0.0,
        )
        if most is None:
            return None
        target = math.floor(most * 10.0 + 1e-9) / 10.0
        return target if target >= gate.MIN_RATE_KW else None

    def import_limit_write_kw(self) -> float | None:
        """What the administrator's tap would write to the plant's import limit now: `import_limit_target_kw` when the
        limit reads more than keeps every phase under the fuse (`gate.import_limit_phase_max_kw`, unrounded);
        `None` when it already guards (only saying it acts is left), reads nothing (never written over), no value guards
        or the per-phase hold is no way here (`_phase_path_open`). The card shows this
        figure, so what it says is what is written."""
        control = control_for(self.facts.brand)
        if control is None or control.import_limit is None or not self._phase_path_open():
            return None
        target = self.import_limit_target_kw()
        most = gate.import_limit_phase_max_kw(
            self._main_fuse_a(), stored_voltage_between_phases_v(self.site.config),
            margin_a=self._site_margin_a(), cars_a=0.0,
        )
        current = self._import_limit_kw(control)
        # A limit that reads nothing is not written over: it may hide a lower value set on purpose (a power tariff)
        # until the integration has polled it.
        if target is None or most is None or current is None or 0.0 <= current <= most + 1e-6:
            return None
        return target

    async def async_set_import_limit(self) -> str | None:
        """The administrator's one tap on the per-phase import-limit line: lower the plant's own grid import limit to
        `import_limit_target_kw` when it reads more than guards (`import_limit_write_kw`; never raised: a lower limit only makes the plant more
        careful), read it back, and only then say that it acts (`battery_control_import_limit_acts`). A limit already
        at or under the target is left as it is. Answers a refusal code, `None` when done: `not_enabled` (battery
        control is off: nothing is written to the plant), `not_read` (the limit reads nothing yet: never written over),
        `shadow_only` (a write is needed while control only watches: nothing is written to the plant; saying the limit
        acts alone is allowed, it is SpotNav's own option), `not_supported` (no
        field-tested per-phase hold here), the site's own refusals of the rule (`_import_limit_refusal`,
        `output_type_not_supported`), `no_value` (no value guards the fuse), `entity_missing` (the limit's entity is
        not there or disabled), `unit_unknown` (it is not in kW), `out_of_range` (the target is outside the entity's
        own range), `write_failed` (the integration refused) and `not_read_back` (it did not read the target back
        within `IMPORT_LIMIT_READ_BACK_S`). Every write is logged."""
        if not self.options().enabled:
            return "not_enabled"  # nothing is written to the plant while battery control is off
        control = control_for(self.facts.brand)
        refusal = self._import_limit_phase_refusal(assume_acts=True)
        if control is None or control.import_limit is None or refusal not in PHASE_PATH_STEPS:
            return refusal or "not_supported"
        target = self.import_limit_target_kw()
        if target is None:
            return "no_value"
        current = self._import_limit_kw(control)
        if current is None:
            return "not_read"
        if self.import_limit_write_kw() is not None:
            if not self.options().writes:
                # Shadow only: nothing is written to the plant (the limit also steers the plant's own modes).
                return "shadow_only"
            entity_id = plant_entity_id(self.hass, self.site.config, control.import_limit)
            state = None if entity_id is None else self.hass.states.get(entity_id)
            if entity_id is None or state is None:
                return "entity_missing"
            if state.attributes.get("unit_of_measurement") != "kW":
                return "unit_unknown"
            low, high = _float(state.attributes.get("min")), _float(state.attributes.get("max"))
            if (low is not None and target < low) or (high is not None and target > high):
                return "out_of_range"
            now = dt_util.now()
            try:
                await self.hass.services.async_call(
                    "number", "set_value", {"entity_id": entity_id, "value": target}, blocking=True
                )
            except Exception:  # noqa: BLE001 - the answer says so; nothing else changes
                _LOGGER.warning("SpotNav battery: setting the plant's import limit to %s kW failed", target, exc_info=True)
                self._log([{"at": now.isoformat(), "kind": "import_limit_write_failed", "reason": str(target)}])
                return "write_failed"
            read = None
            for _ in range(max(1, int(IMPORT_LIMIT_READ_BACK_S / IMPORT_LIMIT_READ_BACK_STEP_S))):
                read = self._import_limit_kw(control)
                if read is not None and read <= target + 1e-6:
                    break
                await asyncio.sleep(IMPORT_LIMIT_READ_BACK_STEP_S)
            read = self._import_limit_kw(control)
            self._log([{"at": now.isoformat(), "kind": "import_limit_set",
                        "reason": f"{current}->{target} read {read}"}])
            _LOGGER.info("SpotNav battery: the plant's import limit set from %s to %s kW (reads %s)", current, target, read)
            if read is not None and current is not None and read > current + 1e-6:
                _LOGGER.warning("SpotNav battery: the plant's import limit reads %s kW after setting %s, more than before (%s)",
                                read, target, current)
            if read is None or read > target + 1e-6:
                return "not_read_back"
        if not self.options().import_limit_acts:
            await self.async_update_options({gate.CONF_IMPORT_LIMIT_ACTS: True})
        return None

    def import_limit_guards(self, *, without_charger: str | None = None) -> bool:
        """Whether the plant's own import limit guards the fuse **now** (`gate.import_limit_holds`), for a charger
        that runs through the plant (`execution/chargers/sigenergy.py`): the person said it acts, it reads a value,
        the site's sources allow the rule, and that value keeps the worst phase under the fuse by the same fuse,
        margin and learned peaks as the battery's cap, with the **other** chargers' last currents (`without_charger`
        is left out: its own lifted current must not undo the lift) and what SpotNav itself leaves standing in the
        house (`_heat_standing_a`). Anything that cannot be read: `False`."""
        try:
            now = dt_util.now()
            options = self.options()
            if not options.import_limit_acts or self._import_limit_refusal(dc_charger=False) is not None:
                return False
            value = self._import_limit_kw(control_for(self.facts.brand))
            peaks = getattr(self.site, "phase_peaks", None)
            if value is None or peaks is None:
                return False
            learned = peaks.learned(now.timestamp())  # raises: not guarded (never the cautious rule by default)
            return gate.import_limit_holds(
                value, self._main_fuse_a(), stored_voltage_between_phases_v(self.site.config),
                margin_a=HEADROOM_MARGIN_A,
                cars_a=self._cars_allowed_a(without=without_charger) + self._heat_standing_a(),
                house_peak_kw=self._house_peak_kw(now), house_phase_peaks_a=learned.phase_peaks(),
            )
        except Exception:  # noqa: BLE001 - fail closed: the fuse cap stays
            self._look_failed("import_limit_guards")
            return False

    def _import_limit_status(self, now: datetime, options: gate.Options) -> dict[str, Any] | None:
        """The plant's own grid import limit against what keeps the worst phase under the fuse
        (`gate.import_limit_max_kw`), for the status: `value_kw` (what it reads), `max_kw` (the most that guards the
        fuse with the chargers' last currents as they are now), `suggested_kw` (the same with no car charging, rounded
        down to 0.1 kW: what the person may set by the total rule), `holds` (shown to act and at most
        `max_kw`: the rule in force) and `why` (why the site's sources refuse the rule whatever the limit reads,
        `_import_limit_refusal`). For a brand whose limit can be held per phase (`commands.ImportLimitPhases`) also
        `per_phase`: `max_kw` (`gate.import_limit_phase_max_kw`, `None` where no value guards), `set_kw` (what the
        administrator's tap writes, `import_limit_write_kw`; `None` when the limit already guards), `holds`, `why`
        (`_import_limit_phase_refusal`) and `entity_id` (the plant's entity a refusal names: the Output Type, the
        phase switch or the grid sensor status, disabled or not; `None` for any other); `holds` above is then true when either rule is in force. `None` for a brand
        without such a limit."""
        control = control_for(self.facts.brand)
        if control is None or control.import_limit is None:
            return None
        value = self._import_limit_kw(control)
        rule: dict[str, Any] = {
            "margin_a": HEADROOM_MARGIN_A, "house_peak_kw": self._house_peak_kw(now),
            "house_phase_peaks_a": self._phase_peaks(now),
        }
        fuse_a, volts = self._main_fuse_a(), stored_voltage_between_phases_v(self.site.config)
        # What SpotNav itself leaves standing in the house (a boost, a raise) counts as the chargers' currents do.
        cars_a = self._cars_allowed_a() + self._heat_standing_a()
        refusal = self._import_limit_refusal()
        most = gate.import_limit_max_kw(fuse_a, volts, cars_a=cars_a, **rule)
        free = gate.import_limit_max_kw(fuse_a, volts, cars_a=0.0, **rule)
        suggested = None if free is None else math.floor(free * 10.0 + 1e-9) / 10.0
        block: dict[str, Any] = {
            "value_kw": None if value is None else round(value, 3),
            "max_kw": None if most is None else round(most, 2),
            "suggested_kw": None if suggested is None or suggested < gate.MIN_RATE_KW else suggested,
            "holds": options.import_limit_acts and refusal is None
            and gate.import_limit_holds(value, fuse_a, volts, cars_a=cars_a, **rule),
            "why": _card_refusal(refusal),
        }
        spec = control.import_limit_phases
        if spec is None:
            return block
        phase_refusal = self._import_limit_phase_refusal()
        phase_margin_a = self._site_margin_a()
        phase_most = gate.import_limit_phase_max_kw(fuse_a, volts, margin_a=phase_margin_a, cars_a=cars_a)
        phase_holds = phase_refusal is None and gate.import_limit_phase_holds(
            value, fuse_a, volts, margin_a=phase_margin_a, cars_a=cars_a
        )
        # `holds`: the plant's limit guards the fuse now by either rule (the cap is lifted); `per_phase` says which.
        block["holds"] = block["holds"] or phase_holds
        # The plant's entity the person can fix for a refusal that names one (enable it, or turn the switch on).
        match = {"output_type_unread": control.output_type, "phase_control_off": spec.switch,
                 "grid_sensor_offline": spec.grid_sensor}.get(phase_refusal or "")
        block["per_phase"] = {
            "max_kw": None if phase_most is None else round(phase_most, 2), "holds": phase_holds,
            "why": _card_refusal(phase_refusal),
            # What the administrator's tap sets the limit to (`import_limit_target_kw`), the card's figure too.
            "set_kw": self.import_limit_write_kw(),
            "entity_id": None if match is None else plant_entity_id(self.hass, self.site.config, match),
        }
        return block

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
        """The per-phase peaks the grid charge cap uses, with the further margin of the days counted so far
        (`Learned.phase_peaks`); `None` while too few days count (the conservative rule)."""
        learned = self._learned(now)
        return None if learned is None else learned.phase_peaks()

    def _cap_status(self, now: datetime, options: gate.Options) -> dict[str, Any]:
        """The grid charge cap and what it rests on, for the status and the diagnostics. `rule`: `learned` (the
        house's per-phase peaks, the learning done), `learning` (those peaks with the further margin of the days
        counted so far), `conservative` (too few days: 4 kW on one phase), `import_limit` (no cap of this kind),
        `accepted` (the person accepted the HA-down risk: the fuse with an empty house, `gate.ha_down_accepted_cap_kw`)
        or `error`. `cap_conservative_kw` and `cap_learned_kw` say what the HA-down cap would be by the conservative
        rule and by the learned peaks without the further margin, the acceptance left out (`None` where there is no
        cap of this kind, or no peaks). `import_limit`: the plant's own import limit against the most that guards the
        fuse (`_import_limit_status`). `ha_down`: the acceptance's offer (`_ha_down_status`)."""
        learned = self._learned(now)
        without: float | None = None
        done: float | None = None
        import_limit: dict[str, Any] | None = None
        # What the HA-down rule itself gives, whether or not the person accepted the risk.
        plain = replace(options, accept_ha_down=False)
        try:
            import_limit = self._import_limit_status(now, options)
            cap: float | None = self._grid_charge_cap_kw(now, options)
            if cap is not None:
                without = self._grid_charge_cap_kw(now, plain, peaks=None)
                if learned is not None and learned.peaks_a is not None:
                    done = self._grid_charge_cap_kw(
                        now, plain, peaks=dict(zip(gate.PHASE_NAMES, learned.peaks_a, strict=True))
                    )
            failed = False
        except Exception:  # noqa: BLE001 - the status must never fail on it
            _LOGGER.debug("cap_status: ignored failure (the status must never fail on it)", exc_info=True)
            cap, failed = None, True
        return {
            "cap_kw": None if cap is None else round(cap, 2),
            "rule": self._cap_rule(options, cap, failed, learned),
            "main_fuse_a": self._main_fuse_a(),
            "cap_conservative_kw": None if without is None else round(without, 2),
            "cap_learned_kw": None if done is None else round(done, 2),
            "learned": None if learned is None else learned.as_dict(),
            "import_limit": import_limit,
            "ha_down": self._ha_down_status(now, options),
        }

    def _cap_rule(self, options: gate.Options, cap: float | None, failed: bool, learned: Any) -> str:
        """Which rule a grid charge cap rests on (`_cap_status`'s `rule`)."""
        if failed:
            return "error"
        if cap is None:
            return "import_limit"
        if options.accept_ha_down and _accept_applies(control_for(self.facts.brand)):
            return "accepted"
        usable = learned is not None and learned.phase_peaks() is not None
        return "conservative" if not usable else "learned" if learned is not None and learned.ready else "learning"

    def plan_grid_charge_cap(self, now: datetime) -> tuple[float | None, str]:
        """The grid charge cap the battery plan plans under, `(cap_kw, rule)`: the one control obeys right now
        (`_charge_limits`, per phase where it runs; read as the executor is, never changing it), so the plan and the
        command can never disagree. `cap_kw` `None` with the rule `import_limit` is no cap of this kind (the plant's
        own import limit guards the fuse); a cap that cannot be worked out is 0 with the rule `error`, never no cap."""
        try:
            options = self.options()
            executor = self.executor
            phased = executor is not None and executor.control.phased
            cap, caps = self._charge_limits(now, options, phased=phased)
            rule = self._cap_rule(options, cap, False, self._learned(now))
            # Where control runs no grid charge at all, the plan plans none: as `_control_slot` and `gate.restricted`.
            if caps is None and gate.charge_by_phases(self.report()):
                return 0.0, RULE_NO_GRID_CHARGE  # the charge was shown only per phase: the balanced one is never run
            fixed = self._fixed_charge_kw()
            if fixed is not None and (not math.isfinite(fixed) or (cap is not None and fixed > cap)):
                return 0.0, RULE_NO_GRID_CHARGE  # a plant that charges at its own rate: as planned or not at all
            if cap is not None and cap < gate.MIN_RATE_KW:
                return 0.0, RULE_NO_GRID_CHARGE  # under the least rate control commands it does self-consumption
            return cap, rule
        except Exception:  # noqa: BLE001 - an unknown cap allows no grid charge
            _LOGGER.debug("plan_grid_charge_cap: ignored failure (no grid charge is planned)", exc_info=True)
            return 0.0, "error"

    def _ha_down_status(self, now: datetime, options: gate.Options) -> dict[str, Any]:
        """Whether accepting the HA-down risk (`battery_control_accept_ha_down`) would change anything here, for the
        card's administrator-only line: `offered` (battery control is on, the plant's recipe can charge from the
        grid, neither import-limit rule holds, no recipe's own dead-man stands in, and the HA-down cap with no car or
        heat counted, per phase where a charge per phase runs, leaves at least `HA_DOWN_OFFER_GAIN_KW` less than the
        fuse allows with an empty house, at most the battery's rated charge), `held_kw` (that HA-down cap) and
        `empty_house_kw` (what the fuse allows with an empty house). Read the same whether or not it is accepted, so
        the line stays while it changes something; never raises (`offered` false)."""
        block: dict[str, Any] = {"offered": False, "held_kw": None, "empty_house_kw": None}
        try:
            control = control_for(self.facts.brand)
            if control is None or not control.supports(ACTION_GRID_CHARGE) or not _accept_applies(control):
                return block
            # A command's own measured expiry stands in for the cap: nothing to accept.
            if self._expiry_lifts(control, phased=True) or self._expiry_lifts(control, phased=False):
                return block
            plain = replace(options, accept_ha_down=False)
            phase_path = self._phase_path_open()
            executor = self.executor
            held = self._charge_limits(
                now, plain, phased=executor is not None and executor.control.phased, no_cars=True
            )[0]
            if held is None:
                return block
            empty = gate.ha_down_accepted_cap_kw(
                self._main_fuse_a(), stored_voltage_between_phases_v(self.site.config),
                margin_a=self._site_margin_a(), plant_phases=self._plant_phases(control),
            )
            if executor is not None and executor.control.phased and self._phased_wanted():
                # A charge per phase may use each phase's fuse less the margin.
                empty = max(empty, sum(self._phase_caps_kw(now, accept=True, no_cars=True)))
            rated = self.facts.max_charge_kw
            most = empty if rated is None or not math.isfinite(rated) or rated <= 0 else min(empty, rated)
            block.update(held_kw=round(held, 2), empty_house_kw=round(empty, 2))
            # Where the plant's own per-phase hold can guard the fuse, that is the way: no risk is offered instead.
            block["offered"] = options.enabled and not phase_path and most - held >= HA_DOWN_OFFER_GAIN_KW
        except Exception:  # noqa: BLE001 - the status must never fail on it
            self._look_failed("ha_down_status")
            block["offered"] = False
        return block

    def learning_hold(self, now: datetime) -> dict[str, Any] | None:
        """The learning of the house's per-phase peaks while it holds the grid charge back, for the house card's status
        line: `{what: "phase_peaks", days_left, cap_kw}` (`cap_kw` 0 when no grid charge can run), `None` when it
        holds nothing back: grid charging not allowed, the learning done, the plant's import limit guarding the
        fuse, the cap of the days counted so far (the conservative rule, or the learned peaks with their further
        margin) still at least the battery's own rated charge, or that further margin costing nothing (the cap the
        same without it: the house itself is what leaves no more). `days_left`: to the next step, the first usable
        days while the conservative rule counts, the end of the learning after that."""
        self._look_id += 1
        options = self.options()
        if ACTION_GRID_CHARGE not in self.allowed(options):
            return None
        learned = self._learned(now)
        status = self._cap_status(now, options)
        cap = status["cap_kw"]
        if learned is None or learned.ready or status["rule"] not in ("conservative", "learning") or cap is None:
            return None
        # A charge per phase runs under the phases' own caps, which may leave more than the balanced one. Read as the
        # executor's control is now: a status read changes nothing.
        executor = self.executor
        effective = self._charge_limits(now, options, phased=executor is not None and executor.control.phased)[0]
        cap = cap if effective is None else max(cap, round(effective, 2))
        rated = self.facts.max_charge_kw
        if rated is not None and cap >= rated:
            return None
        days_left = learned.days_left
        if status["rule"] == "conservative":
            days_left = max(MIN_READY_DAYS - learned.days_spread, 1)
        elif status["cap_learned_kw"] is not None and cap >= status["cap_learned_kw"]:
            return None
        return {
            "what": "phase_peaks",
            "days_left": days_left,
            "cap_kw": 0.0 if cap < gate.MIN_RATE_KW else cap,
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
            share_a = min(executor.state.rate_kw, -battery_kw) * 1000.0 / (phases * phase_volts(volts))
        return gate.export_discharge_cap_kw(
            self._main_fuse_a(), volts, margin_a=HEADROOM_MARGIN_A, phase_export_a=exports, battery_share_a=share_a,
            plant_phases=phases,
        )

    def _phase_export_a(self, volts_between_phases: float) -> list[float] | None:
        """Each phase's export now (A, 0 where it imports), `None` when a phase is not known."""
        signed = list(self.site.result.phase_signed_active_power_w.values())
        if not signed or not all(value is not None and math.isfinite(value) for value in signed):
            return None
        volts = phase_volts(volts_between_phases)
        return [max(-float(value), 0.0) / volts for value in signed]  # type: ignore[arg-type]

    def _cars_allowed_a(self, *, without: str | None = None, held_off: bool = False) -> float:
        """What the site's chargers may draw per phase if nothing writes them again (A, summed): each one's last
        current (`charger_allowed_a`), `ASSUMED_CURRENT_A` when it is not known; infinite (no grid charge) when the
        chargers cannot be read. `without`: a charger left out (`import_limit_guards`).

        `held_off` (only while a person's hardware test runs, `_async_verify`): a charger the test's hold keeps
        stopped counts 0 A where it cannot draw with Home Assistant gone (`ChargerTestHold.counts_nothing`: the hold
        on, and either an OCPP charger out of service by its own report and not charging, or the charger read
        stopped with its stop a stored setting that holds every later session off). Every other charger keeps its
        current, an Easee and one on buttons among them: their stop ends the session only, and the next plug-in may
        charge with nobody there to stop it."""
        from ..execution.controller import ASSUMED_CURRENT_A  # noqa: PLC0415 - the charger side imports the site

        hold = getattr(self.site, "charger_test_hold", None) if held_off and self._hw_run else None
        try:
            total = 0.0
            for charger_entry_id in self.site.config.get(CONF_CHARGER_ENTRY_IDS) or []:
                if charger_entry_id == without:
                    continue
                if hold is not None and hold.counts_nothing(charger_entry_id):
                    continue
                amps = self.site.charger_allowed_a(charger_entry_id)
                total += float(ASSUMED_CURRENT_A) if amps is None else max(amps, 0.0)
        except Exception:  # noqa: BLE001 - unknown: no grid charge on the HA-down rule
            self._look_failed("cars_allowed")
            return math.inf
        return total

    def _heat_standing_a(self) -> float:
        """What SpotNav itself adds to the house and leaves standing if Home Assistant stops (A, on each phase): a
        hot-water boost SpotNav or the button started that may run (`HotWater.standing_boost_kw`, its learned power,
        all on one phase as its worst case) and a raise held (`HEAT_RAISE_KW`). The learned peak holds them only
        as far as the window saw them; after some weeks of live days they are counted twice, the safe side.
        Infinite (no grid charge) when it cannot be read."""
        try:
            kw = 0.0
            shadow = getattr(self.site, "heat_shadow", None)
            hot_water = None if shadow is None else getattr(shadow, "hot_water", None)
            if hot_water is not None:
                kw += hot_water.standing_boost_kw()
            heat = getattr(self.site, "heat_control", None)
            if heat is not None and heat.raised:
                kw += HEAT_RAISE_KW
        except Exception:  # noqa: BLE001 - unknown: no grid charge on the HA-down rule
            self._look_failed("heat_standing")
            return math.inf
        if kw <= 0:
            return 0.0
        return kw * 1000.0 / phase_volts(stored_voltage_between_phases_v(self.site.config))

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
        site's import target reckons a departure (`ImportTargetHold._target_shortfall`)."""
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
        return self._with_outside(read_battery_facts(self.hass, self.site.config, power_kw, self.found_states()))

    def _with_outside(self, facts: BatteryFacts) -> BatteryFacts:
        """A battery the site's outside-control watch marks is another system's: every look refuses it."""
        battery_shadow = getattr(self.site, "battery_shadow", None)
        return facts if battery_shadow is None else battery_shadow.with_outside(facts)

    def found_states(self) -> dict[str, str]:
        """The states the battery's executor found before it first wrote, by entity id (`Executor.found_states`)."""
        executor = self.executor
        return {} if executor is None else executor.found_states()

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
                    reads[name] = RoleRead(entity_id=entity_id, state=_state_of(executor.control, name, state),
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
            timed_out=self._export_timed_out,
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
                    reads[name] = RoleRead(entity_id=entity_id, state=_state_of(executor.control, name, state),
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
        self.facts = facts = self._with_outside(read_battery_facts(self.hass, self.site.config, power_kw, self.found_states()))
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
        phase_caps: Phases | None = None
        if slot is not None and slot[0] == ACTION_GRID_CHARGE:
            # The cap of the control that will run it, again: a slot capped for a charge per phase is never run as
            # the balanced one above the balanced cap (the control changes only while it holds nothing).
            cap, phase_caps = self._charge_limits(now, options, phased=control.phased)
            if cap is not None and slot[1] > cap:
                slot = (slot[0], cap, slot[2]) if cap >= gate.MIN_RATE_KW else (ACTION_SELF_CONSUME, 0.0, slot[2])
                phase_caps = phase_caps if cap >= gate.MIN_RATE_KW else None
        # A recipe whose targets per phase are the grid's reads the plant's power per phase at its own point: the
        # load behind the point less the power at it.
        grid_phases = self._grid_phases()
        behind, point = grid_phases if grid_phases is not None else (None, None)
        spec = control.phase_control
        if spec is not None and spec.behind is not None and spec.point is not None:
            plant_phase_kw = None if behind is None or point is None else (
                behind[0] - point[0], behind[1] - point[1], behind[2] - point[2]
            )
        else:
            plant_phase_kw = self._plant_phase_kw(now)
        return ExecInputs(
            now=now,
            facts=facts,
            reads=self._reads(control),
            phase_caps_kw=phase_caps,
            phase_headroom_a=self._phase_headroom_a(),
            plant_phase_kw=plant_phase_kw,
            plant_phases_mapped=self._phases_mapped(),
            phase_behind_kw=behind,
            phase_point_kw=point,
            grid_phase_kw=self._grid_phase_kw() if result.state not in UNHEALTHY_STATES else None,
            action=None if slot is None else slot[0],
            rate_kw=0.0 if slot is None else slot[1],
            slot_end=None if slot is None else slot[2],
            battery_age_s=age_s,
            measurement_ok=result.state not in UNHEALTHY_STATES and result.state != "not_configured",
            headroom_a=min(headrooms) if headrooms else None,
            charge_phases=self._plant_phases(control),
            volts_between_phases=stored_voltage_between_phases_v(config),
            grid_import_kw=grid_import_kw,
            # The fast shedding order (`gate.battery_yields`): the battery's grid charge before the car, the car
            # first when its departure has room; "battery first" backs the car off first.
            car_first=car_first,
            car_held=car_held,
            house_deficit_kw=house_deficit_kw,
            stopping=stopping,
            failed=self._failed,
            timed_out=self._timed_out,
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
            _LOGGER.debug("minimal_inputs: ignored failure", exc_info=True)
            power_kw, age_s = None, None
        try:
            self.facts = facts = self._with_outside(read_battery_facts(self.hass, self.site.config, power_kw, self.found_states()))
        except Exception:  # noqa: BLE001
            _LOGGER.debug("minimal_inputs: ignored failure", exc_info=True)
            facts = self.facts
        try:
            reads = self._reads(executor.control)
        except Exception:  # noqa: BLE001
            _LOGGER.debug("minimal_inputs: ignored failure", exc_info=True)
            reads = stored_reads(self.hass, executor)
        try:
            takeover = self.options().ai_takeover_ack
        except Exception:  # noqa: BLE001
            _LOGGER.debug("minimal_inputs: ignored failure", exc_info=True)
            takeover = False
        return ExecInputs(
            now=now, facts=facts, reads=reads, battery_age_s=age_s, measurement_ok=False, stopping=stopping,
            failed=self._failed, timed_out=self._timed_out, takeover=takeover,
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
            _LOGGER.debug("export_hand_back_inputs: ignored failure", exc_info=True)
            facts, reads = BatteryFacts(brand=executor.state.brand), {}
        reads = {**stored_reads(self.hass, executor), **reads}
        return ExecInputs(now=now, facts=facts, reads=reads, measurement_ok=False, stopping=stopping,
                          failed=self._export_failed, timed_out=self._export_timed_out)

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
        return (
            options.writes or (trial is not None and self._trial_allowed(trial, options))
            or (self._hw_run and options.enabled)
        )

    def _gated(self, inputs: ExecInputs, allowed: bool) -> tuple[ExecInputs, bool]:
        """A step the options no longer allow, or one during the start-up wait, becomes a hand-back."""
        if allowed and self._start_wait():
            return replace(
                inputs, stopping=inputs.stopping or "start_wait", action=None, rate_kw=0.0, slot_end=None
            ), False
        if not allowed or self._writes_allowed_now():
            return inputs, allowed
        return replace(
            inputs, stopping=inputs.stopping or "options_changed", action=None, rate_kw=0.0, slot_end=None
        ), False

    async def _async_step(self, executor: Executor, inputs: ExecInputs, *, allowed: bool) -> StepResult:
        inputs, allowed = self._gated(inputs, allowed)
        result = executor.step(inputs)
        self._failed = self._timed_out = ()
        self._log(result.events)
        self._log_expiry(executor, result.events)
        if any(event["kind"] == "enter" for event in result.events) and taken_over(inputs.facts, executor.control):
            # Taken over from the plant's own optimiser: turning control off reminds the person to set it again.
            self.took_from_ai = True
        self._withdraw_per_phase(result, inputs.now)
        if result.persist_first:
            # The snapshot is on disk before the first write, so a crash in between still restores it.
            await self._async_save()
        self._failed, self._timed_out = await self._async_writes(result, executor, allowed=allowed)
        await self._async_save()
        return result

    def _withdraw_per_phase(self, result: StepResult, now: datetime) -> None:
        """A running charge per phase that the plant did not follow (`executor._judge_phases` handed it back): the
        capability is withdrawn for this firmware, so the next grid charge is the balanced one. Not while the
        verification (or a hardware test) runs: its own modes record what a runaway on their targets showed."""
        if self.verification is not None:
            return
        judged = next((event for event in result.events
                       if event["kind"] == "monitor" and str(event["reason"]) in ("phase_over", "phase_no_effect")), None)
        report = None if judged is None else self.report()
        if judged is None or report is None:
            return
        outcome = "partial" if judged["reason"] == "phase_over" else "ignored"
        self._keep_report(self.firmware_key(), gate.per_phase_failed(report, outcome, now))
        self._log([{"at": now.isoformat(), "kind": "per_phase_withdrawn", "reason": judged["reason"]}])

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

    async def _async_writes(
        self, result: StepResult, executor: Executor, *, allowed: bool
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """A step's writes in order: the roles not taken (the next look's `failed`), and those of them whose call was
        given up on (its `timed_out`)."""
        failed: list[str] = []
        timed_out: list[str] = []
        for op in result.ops:
            why = await self._async_write(op, executor, allowed=allowed)
            if why is not None:
                failed.append(op.role)
                if why == "call_timeout":
                    timed_out.append(op.role)
        return tuple(failed), tuple(timed_out)

    async def _async_write(self, op: Op, executor: Executor, *, allowed: bool) -> str | None:
        """One write; why it was not taken (refused, or the service call raised or was given up on: the executor
        counts it as not taken), `None` when it was. A write that is not a restore is refused unless the caller's
        gate allows it: a hand-back never is."""
        if not op.restore and not allowed:
            _LOGGER.warning("SpotNav battery control: a write of %s was refused by the gate", op.role)
            self._log([{"at": dt_util.now().isoformat(), "kind": "refused", "reason": op.role}])
            return "refused"
        role = executor.control.role(op.role)
        domain, service, data = service_call(
            op.method, op.entity_id, op.value, device_id=op.device_id,  # type: ignore[arg-type]
            services=role.services, watchdog_s=executor.watchdog_s, device_field=role.device_field,
            duration=role.lease,
        )
        try:
            await _async_service(self.hass, domain, service, data)
        except Exception as err:  # noqa: BLE001 - any error (a Modbus library's too, a call given up on) is a write
            # not taken; the state must still be stored and a stop or unload must still hand back.
            _LOGGER.warning("Battery control: %s.%s for %s failed: %s", domain, service, op.role, _error_text(err))
            self._log([{"at": dt_util.now().isoformat(), "kind": "service_failed", "reason": op.role,
                        "error": _error_text(err)}])
            return _failure(err)
        return None

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
        elif not self._started:
            # The start has not made the executor yet (it failed and waits to retry, or was cut short): what the
            # store holds stays for the next start, or a snapshot not handed back would be written away.
            data |= {name: self._stored[name] for name in ("state", "ledger", "monitor") if name in self._stored}
        export = self.export_executor
        if export is not None:
            data["export"] = {
                "state": export.state.as_dict(), "ledger": export.ledger.as_dict(), "monitor": export.monitor.as_dict(),
            }
        elif not self._started and "export" in self._stored:
            data["export"] = self._stored["export"]
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
                if role.name in control.hacs_roles:
                    # Core's integration lacks it: one note the person can act on, not one line per control.
                    need = f"needs_hacs_integration:{control.hacs_repo}"
                    if need not in needs:
                        needs.append(need)
                elif not role.optional:
                    needs.append(f"control_not_found:{role.name}")
            elif role.method != "service" and not read.enabled:
                needs.append(f"control_disabled:{role.name}")
        # A setting of the person's on the plant that keeps an action from ever running ("Max inverter power" 0 on a
        # Victron GX: the battery never discharges). Read only: SpotNav never writes it.
        executor = self.executor
        if executor is None or executor.control.brand != control.brand:
            executor = Executor(control)
        needs.extend(executor.told_problems(reads))
        management = facts.management
        if management.state == "externally_controlled":
            needs.append(f"outside_control:{management.reason}")
        elif taken_over(facts, control) or self._takeover_withdrawn(self.options()):
            if not self.options().ai_takeover_ack:
                needs.append("ai_takeover_ack")
        elif control.strategy_unread and not self.options().ai_takeover_ack:
            needs.append("strategy_ack")
        elif management.state == "self_managed" and not (self.acting()):
            needs.append(f"self_managed:{management.reason}")
        return needs

    def _settings_device(self) -> str | None:
        """The device that holds the first control the person must enable (`control_disabled`), so the card can open
        its page; `None` when none is disabled."""
        control = control_for(self.facts.brand)
        if control is None:
            return None
        reads = read_role_reads(self.hass, self.site.config, control)
        for role in control.roles:
            read = reads.get(role.name)
            if read is not None and read.found and role.method != "service" and not read.enabled:
                return read.device_id
        return None

    def enable_entities(self) -> dict[str, Any] | None:
        """The brand's control entities that are disabled in Home Assistant, for the card's opt-in dialog (`optin`);
        `None` when none is."""
        return optin.describe(self.hass, self.site.config, self.facts.brand)

    def enable_facts(self) -> dict[str, Any] | None:
        """The disabled entities that hold a fact the battery plan needs and lacks (its size, its floor), for the
        card's one line (`optin.describe_facts`), shown with control off as on: the plan needs them either way. `None`
        when none is."""
        return optin.describe_facts(self.hass, self.site.config, self.facts)

    def clear_outside_control(self) -> bool:
        """An administrator's "it was me" for the outside-control verdict (`BatteryShadow.clear_outside_by_person`):
        `False` when none a person may clear stands (none, or an integration's or a planner's)."""
        battery_shadow = getattr(self.site, "battery_shadow", None)
        if battery_shadow is None or not battery_shadow.clear_outside_by_person(dt_util.now()):
            return False
        self.facts = self._read_facts()
        return True

    async def async_enable_entities(self) -> list[str]:
        """Enable the control entities `enable_entities` lists and reload their integration once; only ever called
        on the person's confirm (an administrator, never over the webhook). Answers the entity ids enabled."""
        return await optin.async_enable(self.hass, self.site.config, self.facts.brand)

    async def async_enable_facts(self) -> list[str]:
        """Enable the fact entities `enable_facts` lists (never a control) and reload their integration once; only
        ever called on the person's confirm (an administrator, never over the webhook)."""
        enabled = await optin.async_enable_facts(self.hass, self.site.config, self.facts)
        if enabled:
            self.facts = self._read_facts()
        return enabled

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
        if state == "on" and "strategy_ack" in self._needs():
            return "watching:strategy"
        return state

    def status(self) -> dict[str, Any]:
        """What the card's battery section shows (once it has one) and diagnostics: the options, the state and its
        line, the report for the plant's firmware, the actions allowed, the routine's progress, what is missing and
        the notes."""
        self._look_id += 1
        options = self.options()
        verification = self.verification
        current = None if verification is None else verification.current
        return {
            # Whether the site's battery facts name a battery: without one a client hides its battery card (additive).
            "battery_found": bool(self.facts.battery_entity_id),
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
            "device_id": self._settings_device(),
            "enable_entities": self.enable_entities(),
            "enable_facts": self.enable_facts(),
            "notes": [dict(note) for note in self.notes],
            "preheat": self.preheat.status(self.facts),
            "trial": None if self.trial is None else self.trial.as_dict(),
            "grid_charge_cap": self._cap_status(dt_util.now(), options),
            "per_phase": self._per_phase_block(dt_util.now(), options),
            "plan_costs": self._plan_costs(),
        }

    def wear_default(self) -> float:
        """The area currency's own wear per kWh each way (the SEK figure while no area or currency is known): what
        bounds a written wear (`gate.valid_change`)."""
        try:
            from .plan_runtime import site_price_source_of  # noqa: PLC0415 - it imports this module

            source = site_price_source_of(self.hass, self.site)
            area = None if source is None else source.area
            return gate.wear_default(None if area is None else area.currency)[0]
        except Exception:  # noqa: BLE001 - a bound must never fail the write
            self._look_failed("wear_default")
            return DEFAULT_CYCLE_COST

    def _plan_costs(self) -> dict[str, Any] | None:
        """The plan's two figures in the site's currency for the card and the app: the wear per kWh each way (the
        site's own, `gate.CONF_WEAR`, else the currency's default, also sent as `wear_default_minor_per_kwh`) and
        Balanced's selling margin per kWh, in the minor unit; `None` while no price area is known."""
        try:
            from .plan_runtime import site_price_source_of  # noqa: PLC0415 - it imports this module

            source = site_price_source_of(self.hass, self.site)
            area = None if source is None else source.area
            if area is None or not area.major_unit or not area.minor_unit:
                return None
            entry = self.hass.config_entries.async_get_entry(self.site.entry_id)
            wear, _unknown = gate.site_wear(self.site.config if entry is None else entry.data, area.currency)
            default, _unknown = gate.wear_default(area.currency)
            return {
                "wear_minor_per_kwh": wear,
                "wear_default_minor_per_kwh": default,
                "balanced_margin_minor_per_kwh": sell_margin_minor_per_kwh(area.currency),
                "major_unit": area.major_unit,
                "minor_unit": area.minor_unit,
            }
        except Exception:  # noqa: BLE001 - the status must never fail on it
            self._look_failed("plan_costs")
            return None

    def _per_phase_block(self, now: datetime, options: gate.Options) -> dict[str, Any] | None:
        try:
            return self._per_phase_status(now, options)
        except Exception:  # noqa: BLE001 - the status must never fail on it
            self._look_failed("per_phase_status")
            return None

    def snapshot(self) -> dict[str, Any]:
        executor = self.executor
        try:
            status: dict[str, Any] | None = self.status()
        except Exception:  # noqa: BLE001 - diagnostics must never fail on one block
            _LOGGER.debug("snapshot: ignored failure (diagnostics must never fail on one block)", exc_info=True)
            status = None
        return {
            "status": status,
            "brand": None if executor is None else executor.control.brand,
            "executor": None if executor is None else executor.as_dict(),
            "export": None if self.export_executor is None else self.export_executor.as_dict(),
            "verification": None if self.verification is None else self.verification.report(),
            "reports": dict(self.reports),
            "log": list(self.log),
            "preheat": self.preheat.as_dict(),
            "write_budget": self._write_budget_block(),
            "phase_backfill": self._backfill_status(),
            "expiry": self._expiry_block(),
            # The precise import-limit refusals (the card is sent `not_supported` for `not_trusted`).
            "import_limit_refusal": self._import_limit_refusals(),
        }

    def _import_limit_refusals(self) -> dict[str, str | None] | None:
        try:
            return {"total": self._import_limit_refusal(), "per_phase": self._import_limit_phase_refusal()}
        except Exception:  # noqa: BLE001 - diagnostics must never fail on one block
            _LOGGER.debug("import_limit_refusals: ignored failure (diagnostics must never fail on one block)", exc_info=True)
            return None

    def _write_budget_block(self) -> dict[str, Any] | None:
        try:
            return self.write_budget()
        except Exception:  # noqa: BLE001 - diagnostics must never fail on one block
            _LOGGER.debug("write_budget_block: ignored failure (diagnostics must never fail on one block)", exc_info=True)
            return None

    def _backfill_status(self) -> dict[str, Any] | None:
        """What reading the house's per-phase peaks back from the recorder gave (`site/phase_backfill.py`)."""
        try:
            return self.site.phase_peaks.backfill_status()
        except Exception:  # noqa: BLE001 - diagnostics must never fail on one block
            _LOGGER.debug("backfill_status: ignored failure (diagnostics must never fail on one block)", exc_info=True)
            return None
