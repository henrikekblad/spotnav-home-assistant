"""The home battery's shell: reads the site's battery from Home Assistant and runs the shadow parts on it.

One per site, owned by its `SiteCapacityController`. Every `TICK_S` it reads the battery's facts (`facts.py`), the
site's chargers charging from the grid (`execution/grid_charge.py`), the brand's control roles (`commands.py`, as the
write path reads them) and the site's own measurement, steps each part (`hold.py`, whose would-be writes are the
executor's own via `executor.preview`) and logs the decisions in the shadow book (`shadow.py`), which is kept in the
site's store. The
confirmed-write ledger and the control-failure monitor run beside the hold as dry runs (`dry_run.py` says what they
can and cannot tell in shadow); their state is kept in the book's sections. The house battery plan runs beside them
as a shadow and a benchmark (`plan_runtime.py`), and so does the export-limit rule (`export_curtail.py`: no export
while the sell price is below zero), which logs what the executor's export control would write and counts what it
would have saved, with or without a battery. The outside-control watch (`outside_control.py`) looks at the same
readings: a battery another system drives is marked `externally_controlled` for every part (the plan stays a
benchmark). Each occurrence is logged once at INFO when it starts and once when it ends, and kept in the diagnostics
and in what SpotNav detected; nothing is raised in Repairs (nothing there is for the person to do). The Repairs
notice an earlier version raised is deleted at start.

Nothing here writes to a battery, an inverter or any other device: no service is called. The results are shown in
the site's diagnostics (and so in the debug bundle) only. The write path (`control.py`), behind the site's gate, takes
the hold's decision as a request (`hold_requested`) and runs it through the executor.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Final

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr, entity_registry as er, issue_registry as ir
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .. import time_zones
from ..core.electric import TN_VOLTS_BETWEEN_PHASES
from ..const import (
    BATTERY_OUTSIDE_CONTROL_ISSUE,
    CONF_BATTERY_AGGREGATE_POWER_ENTITY,
    CONF_CHARGER_ENTRY_IDS,
    CONF_GRID_POWER_SOURCE,
    DOMAIN,
)
from ..planning.grid_voltage import stored_voltage_between_phases_v
from ..site.measurement_problem import UNHEALTHY_STATES
from .commands import ACTION_CURTAIL_EXPORT, EXPORT_CONTROLS, BrandControl, control_for, export_control_for, export_table
from .dry_run import SECTION_LEDGER, SECTION_MONITOR, HoldDryRun
from .executor import ExecInputs, RoleRead, preview, role_reads
from .export_curtail import CurtailLook, CurtailShadow, curtails
from .adapters import Match
from .facts import BatteryFacts, EntityFact, battery_facts, find_entity, with_outside_control
from .hold import HoldDecision, HoldInputs, HoldMemory, requested_action, restart, step
from .ledger import Ledger
from .monitor import Monitor
from .outside_control import Observation, OutsideControlWatch
from .plan_model import ACTION_HOLD
from .plan_runtime import BatteryPlanShadow
from .shadow import PART_EXPORT_CURTAIL, PART_HOLD_FOR_CAR, ShadowBook, ShadowEntry

if TYPE_CHECKING:
    from ..site.site_capacity_controller import SiteCapacityController

_LOGGER = logging.getLogger(__name__)

TICK_S: Final = 10.0
#: The book's section that keeps the outside-control watch.
SECTION_OUTSIDE: Final = "outside_control"
#: The book's section that keeps the export-limit rule's shadow (`export_curtail.CurtailShadow`).
SECTION_CURTAIL: Final = "export_curtail"
#: The issue-id prefix of the Repairs notice an earlier version raised (retired: deleted at start).
OUTSIDE_CONTROL_ISSUE: Final = BATTERY_OUTSIDE_CONTROL_ISSUE
STORE_VERSION: Final = 1
_SAVE_DELAY_S: Final = 30.0


def _without_count_time(memory: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in memory.items() if key != "counted_at"}


def store_key(entry_id: str) -> str:
    return f"{DOMAIN}.battery_shadow.{entry_id}"


def outside_control_issue_id(entry_id: str) -> str:
    return f"{OUTSIDE_CONTROL_ISSUE}_{entry_id}"


def _entity_fact(entry: er.RegistryEntry, hass: HomeAssistant) -> EntityFact:
    state = hass.states.get(entry.entity_id)
    return EntityFact(
        entity_id=entry.entity_id,
        platform=entry.platform,
        unique_id=str(entry.unique_id or ""),
        device_id=entry.device_id,
        config_entry_id=entry.config_entry_id,
        disabled=entry.disabled_by is not None,
        device_class=(state.attributes.get("device_class") if state is not None else None)
        or entry.device_class
        or entry.original_device_class,
        state=None if state is None else state.state,
        unit=None if state is None else state.attributes.get("unit_of_measurement"),
        step=_step(None if state is None else state.attributes.get("step")),
        attributes={} if state is None else state.attributes,
    )


def _step(value: Any) -> float | None:
    """A number entity's `step` attribute, when it is a positive number."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) and value > 0 else None


def external_planner(hass: HomeAssistant) -> str | None:
    """A battery planner that runs here: Predbat (its `predbat.*` states) or EMHASS (its battery forecast)."""
    if hass.states.async_entity_ids("predbat"):
        return "predbat"
    if hass.states.get("sensor.p_batt_forecast") is not None:
        return "emhass"
    return None


def read_battery_facts(
    hass: HomeAssistant, config: dict[str, Any], power_kw: float | None, found: Mapping[str, str] | None = None
) -> BatteryFacts:
    """The facts of the site's battery entity, from its integration's entities in the registry. `found`: the states
    SpotNav's control found before it first wrote, by entity id (`Executor.found_states`)."""
    entity_id = config.get(CONF_BATTERY_AGGREGATE_POWER_ENTITY)
    registry = er.async_get(hass)
    battery_entry = registry.async_get(entity_id) if isinstance(entity_id, str) and entity_id else None
    if battery_entry is None:
        return battery_facts(None, [], power_kw=power_kw)
    if battery_entry.config_entry_id is not None:
        entries = er.async_entries_for_config_entry(registry, battery_entry.config_entry_id)
    elif battery_entry.device_id is not None:
        entries = er.async_entries_for_device(registry, battery_entry.device_id, include_disabled_entities=True)
    else:
        entries = [battery_entry]
    entities = [_entity_fact(entry, hass) for entry in entries if entry.platform == battery_entry.platform]
    device = None if battery_entry.device_id is None else dr.async_get(hass).async_get(battery_entry.device_id)
    return battery_facts(
        _entity_fact(battery_entry, hass),
        entities,
        manufacturer=None if device is None else device.manufacturer,
        power_kw=power_kw,
        external_planner=external_planner(hass),
        found_states=found,
    )


def read_role_reads(hass: HomeAssistant, config: dict[str, Any], control: BrandControl | None) -> dict[str, RoleRead]:
    """Each of the brand's control roles (`commands.py`) as found in the battery's config entry; none without one."""
    if control is None:
        return {}
    registry = er.async_get(hass)
    entity_id = config.get(CONF_BATTERY_AGGREGATE_POWER_ENTITY)
    battery_entry = registry.async_get(entity_id) if isinstance(entity_id, str) and entity_id else None
    if battery_entry is None or battery_entry.config_entry_id is None:
        return {}
    entries = er.async_entries_for_config_entry(registry, battery_entry.config_entry_id)
    entities = [_entity_fact(entry, hass) for entry in entries if entry.platform == battery_entry.platform]
    return role_reads(control, entities, _entity_fact(battery_entry, hass))


def read_plant_entity(hass: HomeAssistant, config: dict[str, Any], match: Match | None) -> str | None:
    """The state of the battery integration's entity `match` names (`facts.find_entity`), `None` when there is none
    or it reads nothing."""
    if match is None:
        return None
    registry = er.async_get(hass)
    entity_id = config.get(CONF_BATTERY_AGGREGATE_POWER_ENTITY)
    battery_entry = registry.async_get(entity_id) if isinstance(entity_id, str) and entity_id else None
    if battery_entry is None or battery_entry.config_entry_id is None:
        return None
    entries = er.async_entries_for_config_entry(registry, battery_entry.config_entry_id)
    entities = [_entity_fact(entry, hass) for entry in entries if entry.platform == battery_entry.platform]
    found = find_entity(match, entities, _entity_fact(battery_entry, hass))
    if found is None or found.disabled or found.state in (None, "", "unavailable", "unknown"):
        return None
    return found.state


def _export_anchor(hass: HomeAssistant, config: dict[str, Any]) -> er.RegistryEntry | None:
    """The site entity the plant's export limit is found from: the battery's, else the grid meter's, when it belongs to
    an integration with an export control (`commands.EXPORT_CONTROLS`). A site without a battery is in scope."""
    registry = er.async_get(hass)
    candidates: list[Any] = [config.get(CONF_BATTERY_AGGREGATE_POWER_ENTITY)]
    grid = config.get(CONF_GRID_POWER_SOURCE)
    if isinstance(grid, dict):
        candidates += [grid.get("power"), grid.get("power_export")]
    platforms = {platform for control in EXPORT_CONTROLS for platform in control.adapter.platforms}
    for entity_id in candidates:
        entry = registry.async_get(entity_id) if isinstance(entity_id, str) and entity_id else None
        if entry is not None and entry.config_entry_id is not None and entry.platform in platforms:
            return entry
    return None


def read_export_facts(
    hass: HomeAssistant, config: dict[str, Any]
) -> tuple[BatteryFacts, dict[str, RoleRead], BrandControl | None]:
    """The plant's facts (its management above all), its export control and that control's role read, as found from
    `_export_anchor`; empty facts and no control when the site has none."""
    anchor = _export_anchor(hass, config)
    if anchor is None or anchor.config_entry_id is None:
        return BatteryFacts(), {}, None
    registry = er.async_get(hass)
    entries = er.async_entries_for_config_entry(registry, anchor.config_entry_id)
    entities = [_entity_fact(entry, hass) for entry in entries if entry.platform == anchor.platform]
    device = None if anchor.device_id is None else dr.async_get(hass).async_get(anchor.device_id)
    anchor_fact = _entity_fact(anchor, hass)
    facts = battery_facts(
        anchor_fact, entities, manufacturer=None if device is None else device.manufacturer,
        external_planner=external_planner(hass),
    )
    control = export_control_for(facts.brand)
    return facts, ({} if control is None else role_reads(control, entities, anchor_fact)), control


class BatteryShadow:
    """The site's battery parts in shadow."""

    def __init__(self, hass: HomeAssistant, site: SiteCapacityController) -> None:
        self.hass = hass
        self.site = site
        self._store: Store[dict[str, Any]] = Store(hass, STORE_VERSION, store_key(site.entry_id))
        self.book = ShadowBook()
        self.facts: BatteryFacts = BatteryFacts()
        self.dry_run = HoldDryRun(Ledger(), Monitor())
        self.outside = OutsideControlWatch()
        #: Whether SpotNav's own write path (`control.py`) acts on the battery now, and whether the person let it take
        #: the plant over from its own optimiser (`battery_control_ai_takeover_ack`); set by that path.
        self.spotnav_acting: Callable[[], bool] = lambda: False
        #: The states SpotNav's control found before it first wrote, by entity id (`Executor.found_states`): a floor
        #: it set for a hold is read as the one found, so the plan never takes SpotNav's own floor for the battery's.
        self.found_states: Callable[[], Mapping[str, str]] = dict
        self.takeover: Callable[[], bool] = lambda: False
        #: The hold variant the write path would run (`gate.hold_variant_to_run`), set by that path.
        self.hold_variant: Callable[[], str | None] = lambda: None
        self._timer_cancel: Callable[[], None] | None = None
        self._loaded = False
        #: Whether a write of the book waits (`_save`): the ticks go on, and must not keep putting it off.
        self._save_pending = False
        #: The kinds of look that failed since the start (`_look_failed`), each logged once.
        self._failed_looks: set[str] = set()
        self.plan = BatteryPlanShadow(hass, site)
        self.curtail = CurtailShadow()
        #: The sell price now (minor per kWh) and the export control found, as at the last look (shown only).
        self.curtail_sell: float | None = None
        self.curtail_brand: str | None = None

    @property
    def _hold(self) -> HoldMemory:
        return HoldMemory.from_dict(self.book.memory(PART_HOLD_FOR_CAR))

    def hold_requested(self) -> bool:
        """Whether the hold for the car asks for the executor's `hold` now (`hold.requested_action`), for the write
        path to merge with the plan's slot."""
        return requested_action(self._hold) == ACTION_HOLD

    async def async_start(self) -> None:
        """Load the book, restore a hold a restart found (in shadow: log it), then look every `TICK_S`."""
        now = dt_util.now()
        try:
            stored = await self._store.async_load()
        except Exception:  # noqa: BLE001 - a broken store starts an empty book
            _LOGGER.warning("The battery shadow log could not be read; starting a new one")
            stored = None
        try:
            self._restore(stored, now)
        except Exception:  # noqa: BLE001 - a record SpotNav cannot read must not stop the site; it starts again
            _LOGGER.warning("The battery shadow log could not be read; starting a new one", exc_info=True)
            try:
                self._restore(None, now)
            except Exception:  # noqa: BLE001 - the battery's facts are read again at each look
                _LOGGER.debug("SpotNav battery shadow: the start's first look failed", exc_info=True)
        self._loaded = True
        # An earlier version raised a Repairs notice for an occurrence: it is retired, so a left-over one goes.
        ir.async_delete_issue(self.hass, DOMAIN, outside_control_issue_id(self.site.entry_id))
        try:
            await self.plan.async_start(now)
        except Exception:  # noqa: BLE001 - the plan must never stop the site; it starts without its record
            _LOGGER.warning("SpotNav battery plan: the start failed", exc_info=True)
        self._timer_cancel = async_track_time_interval(self.hass, self._async_tick, timedelta(seconds=TICK_S))

    def _restore(self, stored: Any, now: datetime) -> None:
        """The book and its parts from the stored record (`None`: a new one), then the restart's look at the hold."""
        self.book = ShadowBook.from_dict(stored, now)
        self.dry_run = HoldDryRun(
            Ledger.from_dict(self.book.section(SECTION_LEDGER)), Monitor.from_dict(self.book.section(SECTION_MONITOR))
        )
        self.outside = OutsideControlWatch.from_dict(self.book.section(SECTION_OUTSIDE))
        self.curtail = CurtailShadow.from_dict(self.book.section(SECTION_CURTAIL))
        self.facts = self._read_facts()
        memory, decision = restart(self._hold, self.facts, now)
        self._apply(PART_HOLD_FOR_CAR, memory, decision, now)

    async def async_shutdown(self) -> None:
        if self._timer_cancel is not None:
            self._timer_cancel()
            self._timer_cancel = None
        if self._loaded:
            await self._store.async_save(self.book.as_dict())
        await self.plan.async_shutdown()

    @staticmethod
    async def async_remove_stored(hass: HomeAssistant, entry_id: str) -> None:
        await Store(hass, STORE_VERSION, store_key(entry_id)).async_remove()
        await BatteryPlanShadow.async_remove_stored(hass, entry_id)
        ir.async_delete_issue(hass, DOMAIN, outside_control_issue_id(entry_id))

    def _battery_power(self) -> tuple[float | None, float | None]:
        """The site's signed battery reading (kW, + charging) and how old the last word on it is (s)."""
        reading = self.site.battery_aggregate_power()
        if reading is None or reading.value is None:
            return None, None
        ages = [age for age in (reading.age_s, reading.report_age_s) if age is not None]
        return reading.value / 1000.0, min(ages) if ages else None

    def _read_facts(self) -> BatteryFacts:
        power_kw, _age = self._battery_power()
        return self.with_outside(read_battery_facts(self.hass, self.site.config, power_kw, self._found()))

    def _found(self) -> Mapping[str, str]:
        try:
            return self.found_states()
        except Exception:  # noqa: BLE001 - without the write path's answer the entities are read as they are
            return {}

    def with_outside(self, facts: BatteryFacts) -> BatteryFacts:
        """The facts with the outside-control verdict applied (`facts.with_outside_control`)."""
        verdict = self.outside.verdict
        return with_outside_control(facts, verdict.active, verdict.reason)

    def clear_outside_by_person(self, now: datetime) -> bool:
        """An administrator's "it was me" (`OutsideControlWatch.clear_by_person`): the verdict from the battery's
        settings or behaviour ends now, logged and kept; `False` when there is none a person may clear."""
        event = self.outside.clear_by_person(now)
        if event is None:
            return False
        _LOGGER.info(
            "SpotNav battery: an administrator cleared the outside-control verdict (%s); SpotNav may control the "
            "battery again", event.get("reason"),
        )
        self.book.set_section(SECTION_OUTSIDE, self.outside.as_dict())
        self._save()
        self.facts = self._read_facts()
        return True

    def _grid_kw(self) -> float | None:
        """The site's grid power (kW, + import) from its signed phase powers, `None` when one is not known."""
        signed = list(self.site.result.phase_signed_active_power_w.values())
        if not signed or not all(value is not None and math.isfinite(value) for value in signed):
            return None
        return sum(signed) / 1000.0  # type: ignore[arg-type]

    def _observe_outside(self, inputs: HoldInputs) -> HoldInputs:
        """One look of the outside-control watch on the raw facts; the inputs with its verdict applied."""
        facts = inputs.facts
        controls = {
            control.entity_id: control.current
            for control in (facts.hold, facts.grid_charge)
            if control is not None and control.entity_id is not None
        }
        events = self.outside.observe(
            Observation(
                now=inputs.now,
                management=facts.management.state,
                modes=dict(facts.management.settings),
                controls=controls,
                power_kw=facts.power_kw,
                grid_kw=self._grid_kw(),
                soc_pct=facts.soc_pct,
                reserve_soc_pct=facts.reserve_soc_pct,
                battery_age_s=inputs.battery_age_s,
                on_grid=facts.on_grid,
                measurement_ok=inputs.measurement_ok,
                integrations=frozenset(self.hass.config_entries.async_domains()),
                planner=external_planner(self.hass),
                spotnav_acting=self.spotnav_acting(),
            )
        )
        for event in events:
            _LOGGER.info(
                "SpotNav battery: something outside SpotNav %s the battery (%s%s); SpotNav only watches it",
                "drives" if event["kind"] == "started" else "no longer drives",
                event.get("reason"),
                "" if event.get("source") is None else f", likely {event['source']}",
            )
        return replace(inputs, facts=self.with_outside(facts))

    def grid_charging(self) -> tuple[tuple[str, ...], float | None]:
        """The site's chargers charging from the grid, and what they draw together (kW, `None` when unknown)."""
        from ..execution.grid_charge import grid_charge  # noqa: PLC0415 - the charger side imports the site
        from ..runtime import controller_for  # noqa: PLC0415

        active: list[str] = []
        total_w = 0.0
        known = True
        for charger_entry_id in self.site.config.get(CONF_CHARGER_ENTRY_IDS) or []:
            charger = controller_for(self.hass, charger_entry_id)
            if charger is None:
                continue
            reading = grid_charge(self.hass, charger_entry_id, charger)
            if not reading.on:
                continue
            active.append(charger_entry_id)
            if reading.power_w is None:
                known = False
            else:
                total_w += reading.power_w
        return tuple(active), (total_w / 1000.0 if known and active else None)

    def inputs(self, now: datetime) -> HoldInputs:
        power_kw, age_s = self._battery_power()
        facts = read_battery_facts(self.hass, self.site.config, power_kw, self._found())
        chargers, car_kw = self.grid_charging()
        result = self.site.result
        headrooms = [value for value in result.phase_headroom_a.values() if value is not None]
        return HoldInputs(
            now=now,
            grid_charging=bool(chargers),
            facts=facts,
            battery_age_s=age_s,
            measurement_ok=result.state not in UNHEALTHY_STATES and result.state != "not_configured",
            headroom_a=min(headrooms) if headrooms else None,
            volts_between_phases=stored_voltage_between_phases_v(self.site.config),
            charger_ids=chargers,
            car_kw=car_kw,
            reads=read_role_reads(self.hass, self.site.config, control_for(facts.brand)),
            takeover=self._takeover(),
            variant=self._hold_variant(),
        )

    def _hold_variant(self) -> str | None:
        try:
            return self.hold_variant()
        except Exception:  # noqa: BLE001 - without the write path's answer the brand's first variant is shown
            return None

    def _takeover(self) -> bool:
        try:
            return bool(self.takeover())
        except Exception:  # noqa: BLE001 - without the site's answer a plant in its own optimiser is only watched
            return False

    @callback
    def _async_tick(self, _now: datetime) -> None:
        self.tick(dt_util.now())

    def tick(self, now: datetime) -> None:
        """One look: the hold, the plan and the export limit, each on its own. Never raises into the site."""
        inputs: HoldInputs | None = None
        try:
            inputs = self._observe_outside(self.inputs(now))
            self.facts = inputs.facts
            memory, decision = step(self._hold, inputs)
            self._apply(
                PART_HOLD_FOR_CAR, memory, decision, now, reads=inputs.reads,
                age_s=inputs.battery_age_s, headroom_a=inputs.headroom_a, volts=inputs.volts_between_phases,
            )
        except Exception:  # noqa: BLE001 - the battery's shadow must never disturb the site
            self._look_failed("hold")
        try:
            self.plan.tick(now, self.facts)
        except Exception:  # noqa: BLE001
            self._look_failed("plan")
        if inputs is None:
            return
        try:
            self._curtail_tick(now, inputs)
        except Exception:  # noqa: BLE001 - the rule's shadow must never disturb the site
            self._look_failed("export limit")

    def _look_failed(self, kind: str) -> None:
        """A failed look, logged with its traceback the first time for each kind and quietly after."""
        if kind in self._failed_looks:
            _LOGGER.debug("SpotNav battery shadow: the %s look failed again", kind, exc_info=True)
            return
        self._failed_looks.add(kind)
        _LOGGER.warning("SpotNav battery shadow: the %s look failed; it is tried again at each look", kind, exc_info=True)

    def _curtail_tick(self, now: datetime, inputs: HoldInputs) -> None:
        """One look of the export-limit rule in shadow (`export_curtail.py`): in a curtail slot, what the executor's
        export control would write (`executor.preview`) or why not; the export counted either way."""
        from ..execution.solar_execution import site_net_grid_w  # noqa: PLC0415 - it imports the site

        sell = self.curtail_sell = self.plan.sell_at(now)
        shown = None
        blocker: str | None = None
        if curtails(sell):
            facts, reads, control = read_export_facts(self.hass, self.site.config)
            self.curtail_brand = None if control is None else control.brand
            if control is None:
                blocker = "no_export_control"
            else:
                shown = preview(
                    control,
                    ExecInputs(
                        now=now, facts=self.with_outside(facts), reads=reads, action=ACTION_CURTAIL_EXPORT,
                        measurement_ok=inputs.measurement_ok, headroom_a=inputs.headroom_a,
                        volts_between_phases=inputs.volts_between_phases,
                    ),
                )
                blocker = shown.blocker
        grid_w = site_net_grid_w(self.site)
        day = now.astimezone(time_zones.require_zone(self.plan._tz)).date().isoformat()
        restore = dict(self.curtail.restore)
        before = self.curtail.as_dict()
        edge = self.curtail.step(CurtailLook(now, day, sell, None if grid_w is None else grid_w / 1000.0, blocker))
        entry: ShadowEntry | None = None
        detail: dict[str, Any] = {"sell": None if sell is None else round(sell, 2)}
        if edge == "enter" and shown is not None and shown.blocker is None:
            self.curtail.restore = {op.role: op.value for op in shown.restores}
            write = shown.writes[0] if shown.writes else None
            entry = ShadowEntry(
                at=now, part=PART_EXPORT_CURTAIL, action="would_write" if write is not None else "left_alone",
                reason="sell_below_zero" if write is not None else "already_at_zero",
                entity_id=None if write is None else write.entity_id, value=None if write is None else write.value,
                detail=detail | {"writes": [op.as_dict() for op in shown.writes],
                                 "restores": [op.as_dict() for op in shown.restores], "snapshot": dict(shown.snapshot)},
                read=next(iter(shown.snapshot.values()), None),
            )
        elif edge == "enter":
            entry = ShadowEntry(at=now, part=PART_EXPORT_CURTAIL, action="not_possible", reason=blocker or "", detail=detail)
        elif edge == "leave":
            entry = ShadowEntry(
                at=now, part=PART_EXPORT_CURTAIL, action="would_restore" if restore else "left_alone",
                reason="sell_not_below_zero", detail=detail | {"restore": restore},
            )
        if entry is not None:
            self.book.record(entry)
            _LOGGER.debug("SpotNav export limit in shadow: %s (%s)", entry.action, entry.reason)
        after = self.curtail.as_dict()
        if entry is not None or after != before:
            self.book.set_section(SECTION_CURTAIL, after)
            self._save()

    def _apply(
        self, part: str, memory: HoldMemory, decision: HoldDecision | None, now: datetime, *,
        reads: Mapping[str, RoleRead] | None = None, age_s: float | None = None, headroom_a: float | None = None,
        volts: float = TN_VOLTS_BETWEEN_PHASES,
    ) -> None:
        # When the hold last counted changes at every look: alone it is no change worth a write.
        changed = _without_count_time(memory.as_dict()) != _without_count_time(self.book.memory(part))
        self.book.set_memory(part, memory.as_dict())
        entries: list[ShadowEntry] = []
        if decision is not None:
            decision, judged = self.dry_run.decided(decision, self.facts, now, self._hold_variant())
            # Shadow only: the decision is logged, never written (`ShadowBook.writes` is off in this release).
            entries += [
                ShadowEntry(
                    at=now,
                    part=part,
                    action=decision.action,
                    reason=decision.reason,
                    entity_id=decision.entity_id,
                    value=decision.value,
                    detail=decision.detail,
                    read=decision.read,
                ),
                *judged,
            ]
        entries += self.dry_run.observe(
            self.facts, reads or {}, memory.held, now, age_s=age_s, headroom_a=headroom_a, volts_between_phases=volts
        )
        for entry in entries:
            self.book.record(entry)
            _LOGGER.debug("Battery %s in shadow: %s (%s)", entry.part, entry.action, entry.reason)
        sections = {
            SECTION_LEDGER: self.dry_run.ledger.as_dict(),
            SECTION_MONITOR: self.dry_run.monitor.as_dict(),
            SECTION_OUTSIDE: self.outside.as_dict(),
        }
        for name, data in sections.items():
            if data != self.book.section(name):
                self.book.set_section(name, data)
                changed = True
        if changed or entries:
            self._save()

    def _save(self) -> None:
        """Write the book within `_SAVE_DELAY_S`: a write already waiting is not put off by a later change."""
        if self._save_pending:
            return
        self._save_pending = True
        self._store.async_delay_save(self._data_to_save, _SAVE_DELAY_S)

    def _data_to_save(self) -> dict[str, Any]:
        self._save_pending = False
        return self.book.as_dict()

    def _dry_run_snapshot(self) -> dict[str, Any]:
        """The ledger's and the monitor's state and today's counters; in shadow nothing is written, so their real
        counters stay at zero and only the dry-run ones move."""
        today = dt_util.now().date().isoformat()
        ledger, monitor = self.dry_run.ledger, self.dry_run.monitor
        return {
            "ledger": {"claims": ledger.as_dict()["claims"], "today": ledger.counters(today)},
            "monitor": {
                "watch": None if monitor.watch is None else monitor.watch.as_dict(),
                "today": monitor.counters(today),
            },
        }

    def snapshot(self) -> dict[str, Any]:
        """The battery's facts, the parts' states, the hold's memory, the kWh it gave the car and the log."""
        hold = self._hold
        return {
            "facts": self.facts.as_dict(),
            "parts": {part: self.book.state(part) for part in (PART_HOLD_FOR_CAR, PART_EXPORT_CURTAIL)},
            "hold_for_car": {
                "held_in_shadow": hold.held,
                "held_since": None if hold.held_since is None else hold.held_since.isoformat(),
                # What the executor's hold would hand back, per role (it writes nothing in shadow).
                "snapshot": dict(hold.snapshot),
                "restore": dict(hold.restore),
                "writes_today": hold.writes_today,
                "battery_gave_car_kwh": {day: round(kwh, 3) for day, kwh in sorted(hold.gave_car_kwh.items())},
            },
            "dry_run": self._dry_run_snapshot(),
            # Another system driving the battery (`outside_control.py`): the verdict now, the signals counted, the
            # setting changes within the day and the last occurrences.
            "outside_control": self.outside.as_dict(),
            "log": [entry.as_dict() for entry in self.book.entries],
            "plan": self.plan.snapshot(),
            # No export while the sell price is below zero (`export_curtail.py`), in shadow: the slot now, the export
            # control found, and per day the kWh exported in curtail slots and what they cost (the would-be saving;
            # `blocked_*` the part the control could not have stopped).
            "export_curtail": {
                "sell_now": None if self.curtail_sell is None else round(self.curtail_sell, 2),
                "control": self.curtail_brand,
                **self.curtail.as_dict(),
                "totals": self.curtail.totals(),
                "commands": export_table(),
            },
        }
