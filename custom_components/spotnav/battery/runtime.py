"""The home battery's shell: reads the site's battery from Home Assistant and runs the shadow parts on it.

One per site, owned by its `SiteCapacityController`. Every `TICK_S` it reads the battery's facts (`facts.py`), the
site's chargers charging from the grid (`execution/grid_charge.py`) and the site's own measurement, steps each part
(`hold.py`) and logs the decisions in the shadow book (`shadow.py`), which is kept in the site's store. The
confirmed-write ledger and the control-failure monitor run beside the hold as dry runs (`dry_run.py` says what they
can and cannot tell in shadow); their state is kept in the book's sections.

Nothing here writes to a battery, an inverter or any other device: no service is called. The results are shown in
the site's diagnostics (and so in the debug bundle) only.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Final

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from ..const import CONF_BATTERY_AGGREGATE_POWER_ENTITY, CONF_CHARGER_ENTRY_IDS, DOMAIN
from ..planning.grid_voltage import stored_voltage_between_phases_v
from ..site.measurement_problem import UNHEALTHY_STATES
from .dry_run import SECTION_LEDGER, SECTION_MONITOR, HoldDryRun
from .facts import BatteryFacts, EntityFact, battery_facts
from .hold import HoldDecision, HoldInputs, HoldMemory, restart, step
from .ledger import Ledger
from .monitor import Monitor
from .shadow import PART_HOLD_FOR_CAR, ShadowBook, ShadowEntry

if TYPE_CHECKING:
    from ..site.site_capacity_controller import SiteCapacityController

_LOGGER = logging.getLogger(__name__)

TICK_S: Final = 10.0
STORE_VERSION: Final = 1
_SAVE_DELAY_S: Final = 30.0


def store_key(entry_id: str) -> str:
    return f"{DOMAIN}.battery_shadow.{entry_id}"


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


def read_battery_facts(hass: HomeAssistant, config: dict[str, Any], power_kw: float | None) -> BatteryFacts:
    """The facts of the site's battery entity, from its integration's entities in the registry."""
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
    )


class BatteryShadow:
    """The site's battery parts in shadow."""

    def __init__(self, hass: HomeAssistant, site: SiteCapacityController) -> None:
        self.hass = hass
        self.site = site
        self._store: Store[dict[str, Any]] = Store(hass, STORE_VERSION, store_key(site.entry_id))
        self.book = ShadowBook()
        self.facts: BatteryFacts = BatteryFacts()
        self.dry_run = HoldDryRun(Ledger(), Monitor())
        self._timer_cancel: Callable[[], None] | None = None
        self._loaded = False

    @property
    def _hold(self) -> HoldMemory:
        return HoldMemory.from_dict(self.book.memory(PART_HOLD_FOR_CAR))

    async def async_start(self) -> None:
        """Load the book, restore a hold a restart found (in shadow: log it), then look every `TICK_S`."""
        now = dt_util.now()
        try:
            stored = await self._store.async_load()
        except Exception:  # noqa: BLE001 - a broken store starts an empty book
            _LOGGER.warning("The battery shadow log could not be read; starting a new one")
            stored = None
        self.book = ShadowBook.from_dict(stored, now)
        self.dry_run = HoldDryRun(
            Ledger.from_dict(self.book.section(SECTION_LEDGER)), Monitor.from_dict(self.book.section(SECTION_MONITOR))
        )
        self._loaded = True
        self.facts = self._read_facts()
        memory, decision = restart(self._hold, self.facts, now)
        self._apply(PART_HOLD_FOR_CAR, memory, decision, now)
        self._timer_cancel = async_track_time_interval(self.hass, self._async_tick, timedelta(seconds=TICK_S))

    async def async_shutdown(self) -> None:
        if self._timer_cancel is not None:
            self._timer_cancel()
            self._timer_cancel = None
        if self._loaded:
            await self._store.async_save(self.book.as_dict())

    @staticmethod
    async def async_remove_stored(hass: HomeAssistant, entry_id: str) -> None:
        await Store(hass, STORE_VERSION, store_key(entry_id)).async_remove()

    def _battery_power(self) -> tuple[float | None, float | None]:
        """The site's signed battery reading (kW, + charging) and how old the last word on it is (s)."""
        reading = self.site.battery_aggregate_power()
        if reading is None or reading.value is None:
            return None, None
        ages = [age for age in (reading.age_s, reading.report_age_s) if age is not None]
        return reading.value / 1000.0, min(ages) if ages else None

    def _read_facts(self) -> BatteryFacts:
        power_kw, _age = self._battery_power()
        return read_battery_facts(self.hass, self.site.config, power_kw)

    def _grid_charging(self) -> tuple[tuple[str, ...], float | None]:
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
        facts = read_battery_facts(self.hass, self.site.config, power_kw)
        chargers, car_kw = self._grid_charging()
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
        )

    @callback
    def _async_tick(self, _now: datetime) -> None:
        self.tick(dt_util.now())

    def tick(self, now: datetime) -> None:
        inputs = self.inputs(now)
        self.facts = inputs.facts
        memory, decision = step(self._hold, inputs)
        self._apply(
            PART_HOLD_FOR_CAR, memory, decision, now,
            age_s=inputs.battery_age_s, headroom_a=inputs.headroom_a, volts=inputs.volts_between_phases,
        )

    def _apply(
        self, part: str, memory: HoldMemory, decision: HoldDecision | None, now: datetime, *,
        age_s: float | None = None, headroom_a: float | None = None, volts: float = 400.0,
    ) -> None:
        changed = memory.as_dict() != self.book.memory(part)
        self.book.set_memory(part, memory.as_dict())
        entries: list[ShadowEntry] = []
        if decision is not None:
            decision, judged = self.dry_run.decided(decision, self.facts, now)
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
            self.facts, memory.held, now, age_s=age_s, headroom_a=headroom_a, volts_between_phases=volts
        )
        for entry in entries:
            self.book.record(entry)
            _LOGGER.debug("Battery %s in shadow: %s (%s)", entry.part, entry.action, entry.reason)
        sections = {SECTION_LEDGER: self.dry_run.ledger.as_dict(), SECTION_MONITOR: self.dry_run.monitor.as_dict()}
        for name, data in sections.items():
            if data != self.book.section(name):
                self.book.set_section(name, data)
                changed = True
        if changed or entries:
            self._store.async_delay_save(self.book.as_dict, _SAVE_DELAY_S)

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
            "parts": {part: self.book.state(part) for part in (PART_HOLD_FOR_CAR,)},
            "hold_for_car": {
                "held_in_shadow": hold.held,
                "held_since": None if hold.held_since is None else hold.held_since.isoformat(),
                "snapshot": hold.snapshot,
                "snapshot_unset": hold.snapshot_unset,
                "writes_today": hold.writes_today,
                "battery_gave_car_kwh": {day: round(kwh, 3) for day, kwh in sorted(hold.gave_car_kwh.items())},
            },
            "dry_run": self._dry_run_snapshot(),
            "log": [entry.as_dict() for entry in self.book.entries],
        }
