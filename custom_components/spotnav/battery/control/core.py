"""The shell itself: one site's battery control, its state and what it is made of. What it does lives in the mixins
beside it, each on this one state."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING, Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from ..preheat import PreheatWatch
from ..commands import Phases
from ..executor import Executor
from ..export_shell import ExportShell
from ..facts import BatteryFacts
from ..verify import Verification
from .const import LOG_MAX, STORE_VERSION, PlanSource, SellSource
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
from .import_limit import ImportLimit
from .caps import Caps
from .exec_inputs import Inputs
from .status import Status

if TYPE_CHECKING:
    from ...site.site_capacity_controller import SiteCapacityController


class BatteryControl(
    Life, GateRecord, Report, PlanProof, Look, PerPhase, WritePath, VerifyDriver,
    HardwareRun, TrialRun, ExpiryCheck, ImportLimit, Caps, Inputs, Status,
):
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
