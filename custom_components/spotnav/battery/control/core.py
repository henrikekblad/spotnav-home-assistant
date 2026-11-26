"""The shell itself: one site's battery control, its state and what it is made of. What it does lives in the mixins
beside it, each on this one state."""

from __future__ import annotations

import asyncio
import logging
import math
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import datetime
from typing import TYPE_CHECKING, Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from ...const import (
    CONF_BATTERY_AGGREGATE_POWER_ENTITY,
    CONF_CHARGER_ENTRY_IDS,
    CONF_CURRENT_CONTROL,
    CONF_MAIN_FUSE_A,
    CURRENT_CONTROL_POWER,
    SOLAR_PRIORITY_CAR_FIRST,
)
from ...site.priority import solar_priority
from ...core.electric import phase_volts, stored_safety_margin_a
from ...planning.grid_voltage import stored_voltage_between_phases_v
from ...site.energy_statistics import async_energy_prefs, energy_statistic_ids
from ...site.measurement_problem import UNHEALTHY_STATES
from ...site.phase_peaks import MIN_READY_DAYS, Learned
from .. import gate, optin
from ..preheat import PreheatWatch
from ..commands import (
    ACTION_CURTAIL_EXPORT,
    FOLLOWS_MAX,
    NORMAL,
    plant_phases,
    BrandControl,
    Phases,
    control_for,
)
from ..executor import (
    restore_order,
    HEADROOM_MARGIN_A,
    ExecInputs,
    Executor,
    RoleRead,
    taken_over,
)
from ..export_curtail import curtails
from ..export_shell import ExportShell
from ..facts import BatteryFacts
from ..mirrored import NEED_MIRRORED_READ_ONLY, READ_ONLY_MIRRORED
from ..outside_control import RESERVE_MARGIN_PCT
from ..plan_model import (
    ACTION_EXPORT_DISCHARGE,
    ACTION_GRID_CHARGE,
    ACTION_HOLD,
    ACTION_SELF_CONSUME,
    DEFAULT_CYCLE_COST,
    sell_margin_minor_per_kwh,
)
from ..runtime import plant_entity_id, read_battery_facts, read_export_facts, read_plant_entity, read_role_reads
from ..verify import (
    Verification,
)
from .const import (
    ACTING_GRACE_S,
    FIRMWARE_STEPS,
    HA_DOWN_OFFER_GAIN_KW,
    HEAT_RAISE_KW,
    IMPORT_LIMIT_READ_BACK_S,
    IMPORT_LIMIT_READ_BACK_STEP_S,
    LOG_MAX,
    NEED_HARDWARE_TEST_FIRST,
    NEED_RECIPE_BLOCKED,
    NEED_RESTORE_BY_HAND,
    NEED_TESTER_SIGNUP,
    ORPHANED,
    PHASE_PATH_STEPS,
    RULE_CHARGE_POWER_ENTITY,
    RULE_NO_GRID_CHARGE,
    SOURCES_REFRESH_S,
    STORE_VERSION,
    _UNSET,
    PlanSource,
    SellSource,
)
from .helpers import _accept_applies, _card_refusal, _float, _state_of
from .io import stored_reads
from .stored import store_key
from .trial import Trial
from .life import Life
from .gate_record import GateRecord
from .report import Report
from .plan_proof import PlanProof
from .look import Look
from .per_phase import PerPhase
from .write_path import WritePath
from .verify_driver import VerifyDriver
from .hardware_run import HardwareRun
from .trial_run import TrialRun
from .expiry import ExpiryCheck

if TYPE_CHECKING:
    from ...site.site_capacity_controller import SiteCapacityController

_LOGGER = logging.getLogger(__package__)  # the shell's one logger, whichever of its modules logs


class BatteryControl(Life, GateRecord, Report, PlanProof, Look, PerPhase, WritePath, VerifyDriver, HardwareRun, TrialRun, ExpiryCheck):
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
        #: The export limit's own switch, gates and status (`export_shell.py`).
        self.export = ExportShell(self)
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
        #: The firmware keys a person's hardware test passed on (hold and the hand-back passed, nothing failed): a
        #: recipe that writes only after one (`commands.BrandControl.hardware_test_first`) stays shadow only elsewhere.
        #: The brands a person's hardware test passed with on this plant (`commands.BrandControl.hardware_test_first`):
        #: the first pass needs the person; after it, the routine checks each firmware, integration version and recipe
        #: revision by itself, as for every brand.
        self.hardware_brands: list[str] = []
        #: The keys (`firmware_key`) a later hardware test failed on, after the first pass: shadow only there until
        #: one passes again.
        self.hardware_failed: list[str] = []
        #: When the plant last failed a check that leaves no failed report behind (ISO): a failed check the person
        #: had forgotten by turning control on again (`_transitions`), a person's failed hardware test
        #: (`_keep_hardware_verdict`). No report passed before it is carried onto another key (`control_report`).
        self.carry_after: str | None = None
        #: When each firmware key was first read here (ISO, by key; the newest `MAX_REPORTS`): a command of the
        #: plan's counts for a key only when written after that, and what the plan has not shown
        #: `gate.PLAN_PROOF_DEADLINE_S` later is checked ahead of it (`_plan_aside`).
        self.key_since: dict[str, str] = {}
        #: The key the plan last wrote something on that it still holds (`_credit_plan`); not kept over a restart.
        self._plan_wrote: str | None = None
        #: The key read at the last look, and since when it has been read without a break in this run
        #: (`_note_key`): a key read again after another (a downgrade) or after a restart counts no command written
        #: before that, though it was first read long ago.
        self._key_run: tuple[str, datetime] | None = None
        #: Until when the plan stands aside for a check past its deadline, and when it last began to (`_plan_aside`).
        self._aside_until: datetime | None = None
        self._aside_at: datetime | None = None
        #: The brands gated after running (`commands.GATED_AFTER_RUNNING`) this plant was looked at for
        #: grandfathering (`_grandfather_running`), granted or not: each is decided once and never again.
        self.grandfather_checked: list[str] = []
        #: The person's sign-up as a tester for the plant's brand (`battery/tester.py`: `brand`, `asked_at`, `ref`, and
        #: once a test's result went `result_ref`, `result_at`), `None` before one; never the e-mail address.
        self.tester: dict[str, Any] | None = None
        #: The control codes entered per brand (`battery/codes.Code.as_stored` with `entered_at` and the firmware key
        #: then): with a passed hardware test (`hardware_brands`) they lift a gated recipe's shadow only.
        self.control_codes: dict[str, dict[str, Any]] = {}
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
        #: Stored snapshots whose brand has no recipe in this release (`_set_aside`, the store's `ORPHANED`).
        self._orphaned: list[dict[str, Any]] = []
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
        #: The charge power entity's bound as read in the look `(look id, brand)` (`_charge_entity_max_kw`).
        self._charge_bound: tuple[tuple[int, str | None], float | None] | None = None
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
        control = control_for(self.facts.control_brand)
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
            own_import_a=self._own_import_a(control),
        )

    def _own_import_a(self, control: BrandControl | None, *, live: bool = False) -> float:
        """What the plant may import by itself on each phase it spreads that over (A, `commands.AutonomousImport`):
        read from its entity where it has one, else the recipe's figure; neither known: infinite (no grid charge, no
        hold start). For the HA-down caps whatever the trigger (the SoC drifts while Home Assistant is down); `live`
        (the executor's room now): only while the trigger may fire (`below_reserve`: the SoC within
        `outside_control.RESERVE_MARGIN_PCT` of the reserve, or not read; so too for the HA-down caps where the recipe says
        its top-up can only start that low, `AutonomousImport.near_reserve_only`). 0 for a recipe that declares none."""
        spec = None if control is None else control.autonomous_import
        if control is None or spec is None:
            return 0.0
        if (live or spec.near_reserve_only) and spec.trigger == "below_reserve":
            soc = self.facts.soc_pct
            reserve = _float(read_plant_entity(self.hass, self.site.config, spec.reserve)) if spec.reserve else None
            reserve = reserve if reserve is not None else self.facts.reserve_soc_pct
            if soc is not None and reserve is not None and soc > reserve + RESERVE_MARGIN_PCT:
                return 0.0
        kw: float | None = None
        if spec.entity is not None:
            read = _float(read_plant_entity(self.hass, self.site.config, spec.entity))
            kw = None if read is None else read * spec.scale
        if kw is None:
            kw = spec.kw
        if kw is None or not math.isfinite(kw):
            return math.inf
        phases = min(max(int(spec.phases or self._plant_phases(control)), 1), 3)
        return max(kw, 0.0) * 1000.0 / (phases * phase_volts(stored_voltage_between_phases_v(self.site.config)))

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

    def _import_limit_refusal(self, *, dc_charger: bool = True, action: str = ACTION_GRID_CHARGE) -> str | None:
        """Why the plant's own import limit cannot be the fuse's guard on this site whatever it reads, as a stable
        code; `None` when it can. The rule (`gate.import_limit_max_kw`) rests on the plant seeing every source and
        spreading its charge evenly over three phases, so it is refused where the recipe does not trust the limit here
        (`_import_limit_distrust`: `not_trusted`, not shown to act in that state; `firmware_not_tested`, another
        firmware than the one it was tested on; `firmware_unread`, the firmware never read), for a
        plant that is not three-phase
        (`plant_not_three_phase`: a single-phase source), a sun source that is not the plant's (`foreign_pv`: it
        lowers the total the plant sees while one phase carries more), sources not read yet (`sources_unknown`), and
        a charger run by power through the plant (`dc_charger`: Sigenergy's DC charger, which can also put out, V2G;
        `dc_charger` false leaves that one out, for that charger's own fuse cap)."""
        control = control_for(self.facts.control_brand)
        if control is None or control.import_limit is None:
            return "not_supported"
        distrust = self._import_limit_distrust(control, action)
        if distrust is not None:
            return distrust
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

    def _import_limit_distrust(self, control: BrandControl, action: str = ACTION_GRID_CHARGE) -> str | None:
        """Why the recipe does not trust the plant's own import limit here (`commands.ImportLimitTrust`), `None` when it
        does: `not_trusted` (marked untrusted, or not shown to act in the state the caller relies on, `action`: the
        recipe's grid charge for the battery's two rules, the plant's own mode for a charger's lift),
        `firmware_unread` (the recipe names its firmwares and the plant's was never read: fail closed) or
        `firmware_not_tested` (read, and not one it was tested on: the field test is to be run on it). The last two
        are steps on the per-phase path (`PHASE_PATH_STEPS`, owner decision 7): no HA-down acceptance is offered
        instead."""
        trust = control.import_limit_trust
        if trust is None or trust.untrusted or action not in trust.acts_in:
            return "not_trusted"
        if trust.firmware is None:
            return None
        if not self.firmware_known():
            return "firmware_unread"
        return None if self.firmware_key().split(" | ", 1)[0] in trust.firmware else "firmware_not_tested"

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
        control = control_for(self.facts.control_brand)
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
        control = control_for(self.facts.control_brand)
        if control is None or control.import_limit is None or not self._phase_path_open():
            return None
        if self._import_limit_phase_refusal(assume_acts=True) in FIRMWARE_STEPS:
            return None  # not trusted on this firmware yet: the field test first, nothing to write
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
        control = control_for(self.facts.control_brand)
        refusal = self._import_limit_phase_refusal(assume_acts=True)
        if control is None or control.import_limit is None or refusal not in PHASE_PATH_STEPS or refusal in FIRMWARE_STEPS:
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
            # The charger's lift runs while the plant is in its own mode, not in the recipe's grid charge: the limit
            # must have been shown to act there (`commands.ImportLimitTrust.acts_in`).
            if not options.import_limit_acts or self._import_limit_refusal(
                dc_charger=False, action=ACTION_SELF_CONSUME
            ) is not None:
                return False
            value = self._import_limit_kw(control_for(self.facts.control_brand))
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
        control = control_for(self.facts.control_brand)
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
        if options.accept_ha_down and _accept_applies(control_for(self.facts.control_brand)):
            return "accepted"
        usable = learned is not None and learned.phase_peaks() is not None
        return "conservative" if not usable else "learned" if learned is not None and learned.ready else "learning"

    def plan_grid_charge_cap(self, now: datetime) -> tuple[float | None, str]:
        """The grid charge cap the battery plan plans under, `(cap_kw, rule)`: the one control obeys right now
        (`_charge_limits`, per phase where it runs; read as the executor is, never changing it), so the plan and the
        command can never disagree. `cap_kw` `None` with the rule `import_limit` is no cap of this kind (the plant's
        own import limit guards the fuse); a cap that cannot be worked out is 0 with the rule `error`, never no cap."""
        try:
            control = control_for(self.facts.control_brand)
            if control is not None and not control.supports(ACTION_GRID_CHARGE):
                return 0.0, RULE_NO_GRID_CHARGE  # a limits-only recipe: control never charges from the grid
            options = self.options()
            executor = self.executor
            phased = executor is not None and executor.control.phased
            cap, caps = self._charge_limits(now, options, phased=phased)
            rule = self._cap_rule(options, cap, False, self._learned(now))
            # Never more than the plant's own charge power entity takes, as `_control_slot`: its own rule where it
            # is what holds the charge.
            bounded = self._within_charge_power(cap)
            if bounded is not None and (cap is None or bounded < cap):
                cap, rule = bounded, RULE_CHARGE_POWER_ENTITY
            # Where control runs no grid charge at all, the plan plans none: as `_control_slot` and `gate.restricted`.
            if caps is None and gate.charge_by_phases(self.control_report()):
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
            control = control_for(self.facts.control_brand)
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
                own_import_a=self._own_import_a(control),
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
        control = control_for(self.facts.control_brand)
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
        from ...execution.controller import ASSUMED_CURRENT_A  # noqa: PLC0415 - the charger side imports the site

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
        """The phases the plant spreads a grid charge over: its Output Type (Solis: the inverter's own type) when
        it shows one, else the brand's (`commands.plant_phases`)."""
        if control is None:
            return 1
        state = read_plant_entity(self.hass, self.site.config, control.output_type)
        return plant_phases(control, state)

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
        from ...runtime import controller_for, preview_for  # noqa: PLC0415 - the charger side imports the site
        from ...site.import_target import member_w, plan_remaining_kwh  # noqa: PLC0415

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
            control = control_for(self.facts.control_brand)
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
            grid_import_kw=self.export.grid_kw(),
            sibling_active=sibling_active,
            takeover=self.options().ai_takeover_ack,
            excused_import_kw=self.export.excused_kw(),
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
        hold_cap = 0.0
        if slot is not None and slot[0] == ACTION_HOLD and control.hold_surplus is not None:
            # A hold that follows the sun leaves a charge limit standing, a grid charge once the sun goes: the
            # balanced grid charge's HA-down cap bounds it (none where the plant's import limit guards the fuse).
            cap, _caps = self._charge_limits(now, options, phased=False)
            hold_cap = math.inf if cap is None else cap
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
            hold_charge_cap_kw=hold_cap,
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
            output_balanced=control.balanced_output,
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
            own_import_a=self._own_import_a(control_for(self.facts.control_brand), live=True),
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
        car_first = solar_priority(config) == SOLAR_PRIORITY_CAR_FIRST
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
                     "notes": [dict(note) for note in self.notes], "hardware_brands": list(self.hardware_brands),
                     "hardware_failed": list(self.hardware_failed), "carry_after": self.carry_after,
                     "key_since": dict(self.key_since),
                     "grandfather_checked": list(self.grandfather_checked),
                     "tester": None if self.tester is None else dict(self.tester),
                     "control_codes": {brand: dict(code) for brand, code in self.control_codes.items()}},
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
        if self._started:
            # The export limit's own gate (the migration, the day's stretches, the checks), executor or not.
            data["export"] = {**(data.get("export") or {}), "gate": self.export.gate.as_dict()}
        if self._orphaned:
            data[ORPHANED] = [dict(orphan) for orphan in self._orphaned]
        return data

    def _needs(self) -> list[str]:
        """What the person can do before control can run, one code each (the card's checklist)."""
        needs: list[str] = []
        facts = self.facts
        control = control_for(facts.control_brand)
        if control is None:
            if facts.battery_entity_id is None:
                return []
            # Mirrored from another Home Assistant (`mirrored.py`): read only, whatever the brand's recipe.
            return [NEED_MIRRORED_READ_ONLY] if facts.read_only == READ_ONLY_MIRRORED else ["no_battery_control"]
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
        restore = self._restore_by_hand()
        if restore is not None:
            needs.append(restore)
        if self.recipe_blocked():
            needs.append(NEED_RECIPE_BLOCKED)
        else:
            due = self.hardware_test_due()
            if due == NEED_HARDWARE_TEST_FIRST and self.tester_signup_needed():
                # The first test waits for the person's sign-up as a tester: that is the step to take.
                due = NEED_TESTER_SIGNUP
            if due is not None:
                needs.append(due)
        return needs

    def _restore_by_hand(self) -> str | None:
        """The checklist's line where a hand-back could not write some roles back (`restore_failed`): each entity and
        the value to set by hand, in the hand-back's order (`restore_by_hand:<entity_id>=<value>|...`). It clears by
        itself once they read so (the executor tries them again, `executor.BLOCKED_RESTORE_S`)."""
        executor = self.executor
        if executor is None or executor.state.phase != "blocked" or not executor.state.unrestored:
            return None
        state = executor.state
        normal = NORMAL.get(executor.control.brand, {})
        order = [name for name in restore_order(executor.control, state.action) if name in state.unrestored]
        order += [name for name in state.unrestored if name not in order]
        rows = []
        for name in order:
            entity_id = state.entities.get(name)
            value = state.restore_values.get(name, normal.get(name))
            if entity_id is not None and value == FOLLOWS_MAX:
                # Found following its own maximum (`Role.follows_max`): the number to set is that maximum, as the
                # hand-back writes it (`resolve_follows_max`); the recipe's word stays only where none is shown.
                shown = self.hass.states.get(entity_id)
                maximum = None if shown is None else _float(shown.attributes.get("max"))
                value = value if maximum is None else maximum
            if entity_id is not None and value is not None:
                text = ("on" if value else "off") if isinstance(value, bool) else (
                    f"{value:g}" if isinstance(value, float) else str(value)
                )
                rows.append(f"{entity_id}={text}")
        return f"{NEED_RESTORE_BY_HAND}:{'|'.join(rows)}" if rows else None

    def _settings_device(self) -> str | None:
        """The device that holds the first control the person must enable (`control_disabled`), so the card can open
        its page; `None` when none is disabled."""
        control = control_for(self.facts.control_brand)
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
        return optin.describe(self.hass, self.site.config, self.facts.control_brand)

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
        return await optin.async_enable(self.hass, self.site.config, self.facts.control_brand)

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
            return f"waiting:{self.waiting_for or 'moment'}"
        if state == "on" and executor is not None and executor.state.phase == "blocked":
            return f"blocked:{executor.state.blocked}"
        if state == "on" and "ai_takeover_ack" in self._needs():
            return "watching:ai_mode"
        if state == "on" and "strategy_ack" in self._needs():
            return "watching:strategy"
        if state == "on" and self._reverifying() and self._check_due(self.report()):
            # Control runs on what passed before the update; the check of the new key waits for its moment.
            return f"waiting_after_update:{self.waiting_for or 'moment'}"
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
            # Set when SpotNav may only read it (`facts.BatteryFacts.read_only`, `remote_homeassistant` for one mirrored
            # from another Home Assistant), `null` otherwise (additive).
            "battery_read_only": self.facts.read_only,
            # As stored: a recipe still waiting for its hardware test runs as shadow only whatever the switch says,
            # and the checklist says why (`hardware_test_first`).
            "options": self._stored_options().as_dict(),
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
            # The tester stage for a recipe that takes a control code (`tester_view`), `null` for every other.
            "tester": self.tester_view(),
        }

    def wear_default(self) -> float:
        """The area currency's own wear per kWh each way (the SEK figure while no area or currency is known): what
        bounds a written wear (`gate.valid_change`)."""
        try:
            from ..plan_runtime import site_price_source_of  # noqa: PLC0415 - it imports this module

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
            from ..plan_runtime import site_price_source_of  # noqa: PLC0415 - it imports this module

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
            "export_gate": self.export.snapshot(),
            "verification": None if self.verification is None else self.verification.report(),
            "reports": dict(self.reports),
            # The steps control runs on an earlier key's answer while the routine checks the key now, each with the
            # key it came from (`control_report`).
            "carried_from": self._carried_block(),
            # When the plant last failed a check that left no failed report: nothing older is carried.
            "carry_after": self.carry_after,
            # What the plan's own commands showed on the key now, and its deadline (`_credit_plan`, `_plan_aside`).
            "plan_proof": self._plan_proof_block(),
            "log": list(self.log),
            "preheat": self.preheat.as_dict(),
            "write_budget": self._write_budget_block(),
            "phase_backfill": self._backfill_status(),
            "expiry": self._expiry_block(),
            # The precise import-limit refusals (the card is sent `not_supported` for `not_trusted`).
            "import_limit_refusal": self._import_limit_refusals(),
            # Snapshots kept because this version has no recipe for their brand (`_set_aside`).
            "orphaned": [
                {"key": orphan["key"], **{name: (orphan.get("state") or {}).get(name)
                                          for name in ("brand", "entities", "restore_values")}}
                for orphan in self._orphaned
            ],
        }

    def _carried_block(self) -> dict[str, str] | None:
        try:
            return self.carried_from()
        except Exception:  # noqa: BLE001 - diagnostics must never fail on one block
            _LOGGER.debug("carried_block: ignored failure (diagnostics must never fail on one block)", exc_info=True)
            return None

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
