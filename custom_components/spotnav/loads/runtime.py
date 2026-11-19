"""The switched load's shell: reads the site, plans the load's hours and logs, in shadow, when it would switch it.

One per site, owned by its `SiteCapacityController`; idle unless the site's options name a load (`CONF_SWITCH_LOAD`).
Every `TICK_S` it reads the switch, the temperature sensor and the site's headroom, plans again when the quarter hour
turns or the prices change (`switch_load.plan_switch`, in the executor: a long runtime takes up to a second), decides on or off (`switch_load.decide`)
and logs each change in its shadow book (`battery/shadow.py`, part `switch_load`). The time it would have run is its
own history for the rolling runtime (any 28 hours, `switch_load.WINDOW`); beside it each day keeps what the switch really did, priced the same way, so the
two can be compared.

* **Prices**: the site's electricity price entry (`pricing/price_source.site_price_source`), today and tomorrow, with
  its fees; on the spot price alone while the fees are unknown (`fees_unknown`).
* **Room**: the site's import cap (the main fuse less its margin, or the import target's held level), less the house's
  expected load per hour (`site/house_forecast.py`'s `plan_kw_by_hour`, which has the load's own past draw in it, so
  it errs on the safe side) and the cars' installed plans. An hour the house forecast does not know is not blocked
  (`room_unknown_hours`).

Nothing here writes to the switch or any other device: no service is called. `ShadowBook.writes` is off while
`battery/shadow.ON_REACHABLE` is off, and there is no write path. Results are shown in the site's diagnostics (and so
in the debug bundle) only.
"""

from __future__ import annotations

import asyncio
import copy
import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from functools import partial
from typing import TYPE_CHECKING, Any, Final

from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from ..battery.plan_runtime import price_days, site_car_windows, site_import_cap_kw, site_price_source_of
from ..battery.plan_shadow import car_kw_in
from ..battery.shadow import PART_SWITCH_LOAD, ShadowBook, ShadowEntry
from ..const import DOMAIN
from ..planning.demand_charge import hour_start
from ..planning.grid_voltage import stored_voltage_between_phases_v
from ..planning.planner import FiscalChoice, PlannerInputError, PlanningSlot, effective_minor_per_kwh, planning_slots
from ..site.measurement_problem import UNHEALTHY_STATES
from .switch_load import (
    CONF_SWITCH_LOAD,
    WINDOW,
    DayTally,
    RunHistory,
    SwitchDecision,
    SwitchInputs,
    SwitchLoadConfig,
    SwitchPlan,
    decide,
    plan_switch,
    quarter_start,
)

if TYPE_CHECKING:
    from ..site.site_capacity_controller import SiteCapacityController

_LOGGER = logging.getLogger(__name__)

TICK_S: Final = 10.0
STORE_VERSION: Final = 1
_SAVE_DELAY_S: Final = 300.0
#: A gap between two looks longer than this (a restart, a stalled loop) is not counted as run or not run.
MAX_GAP: Final = timedelta(minutes=2)
#: Days of runtime and cost kept.
DAYS_KEPT: Final = 14
SECTION_HISTORY: Final = "history"
SECTION_DAYS: Final = "days"

STATUS_PLANNED: Final = "planned"
STATUS_NO_PRICES: Final = "no_prices"
FLAG_FEES_UNKNOWN: Final = "fees_unknown"
FLAG_NO_CAP: Final = "import_cap_unknown"

_UNREADABLE: Final = frozenset({"unavailable", "unknown"})


def store_key(entry_id: str) -> str:
    return f"{DOMAIN}.switch_load.{entry_id}"


def _float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and abs(number) != float("inf") else None


class SwitchLoadShadow:
    """The site's switched load in shadow."""

    def __init__(self, hass: HomeAssistant, site: SiteCapacityController) -> None:
        self.hass = hass
        self.site = site
        self.config = SwitchLoadConfig.from_data(site.config.get(CONF_SWITCH_LOAD))
        self._store: Store[dict[str, Any]] = Store(hass, STORE_VERSION, store_key(site.entry_id))
        self.book = ShadowBook()
        self.history = RunHistory()
        self.days: dict[str, DayTally] = {}
        self.plan: SwitchPlan | None = None
        self.status = STATUS_NO_PRICES
        self.flags: tuple[str, ...] = ()
        self.room_unknown_hours = 0
        self.import_cap_kw: float | None = None
        self.decision: SwitchDecision | None = None
        self._prices: dict[datetime, float] = {}
        self._plan_key: Any = None
        self._last_tick: datetime | None = None
        self._last_on = False
        self._last_actual = False
        self._timer_cancel: Callable[[], None] | None = None
        self._loaded = False
        #: Whether a write of the book waits (`_save`): the ticks go on, and must not keep putting it off.
        self._save_pending = False
        #: The book as last written (or handed to the store), so an unchanged book is not written again.
        self._saved: dict[str, Any] | None = None
        self._look_failed_logged = False
        self._planning = False
        self._closed = False
        self._plan_task: asyncio.Task[None] | None = None

    # ---- life cycle

    async def async_start(self) -> None:
        """Load the book and the history, then look every `TICK_S`; nothing at all without a load."""
        if self.config is None:
            return
        now = dt_util.now()
        try:
            stored = await self._store.async_load()
        except Exception:  # noqa: BLE001 - a broken store starts an empty book
            _LOGGER.warning("The switched load's shadow log could not be read; starting a new one")
            stored = None
        try:
            self._restore(stored, now)
        except Exception:  # noqa: BLE001 - a record SpotNav cannot read must not stop the site; it starts again
            _LOGGER.warning("The switched load's shadow log could not be read; starting a new one", exc_info=True)
            self.book, self.history, self.days = ShadowBook(), RunHistory(), {}
        self._loaded = True
        if self.history.tracked_since is None:
            self.history.tracked_since = now
        try:
            await self.async_replan(now)
            self.tick(now)
        except Exception:  # noqa: BLE001 - the first look is tried again at the next one
            self._look_failed()
        self._timer_cancel = async_track_time_interval(self.hass, self._async_tick, timedelta(seconds=TICK_S))

    def _restore(self, stored: Any, now: datetime) -> None:
        config = self.config
        assert config is not None
        self.book = ShadowBook.from_dict(stored, now)
        memory = self.book.memory(PART_SWITCH_LOAD)
        # Another switch (or none before): its history says nothing about this one.
        if memory.get("entity_id") == config.entity_id:
            self.history = RunHistory.from_dict(self.book.section(SECTION_HISTORY))
            self.days = {
                day: DayTally.from_dict(row)
                for day, row in self.book.section(SECTION_DAYS).items()
                if isinstance(day, str)
            }

    def _look_failed(self) -> None:
        """A failed look, logged with its traceback the first time and quietly after."""
        if self._look_failed_logged:
            _LOGGER.debug("SpotNav switched load: the look failed again", exc_info=True)
            return
        self._look_failed_logged = True
        _LOGGER.warning("SpotNav switched load: the look failed; it is tried again at each look", exc_info=True)

    @callback
    def halt(self) -> None:
        """The site shuts down: no look or plan from here (the book is saved by `async_shutdown`)."""
        self._closed = True
        if self._plan_task is not None and not self._plan_task.done():
            self._plan_task.cancel()
        if self._timer_cancel is not None:
            self._timer_cancel()
            self._timer_cancel = None

    async def async_shutdown(self) -> None:
        self.halt()
        if self._loaded:
            data = self._book_data()
            if data != self._saved:
                await self._store.async_save(data)
                self._saved = copy.deepcopy(data)

    @staticmethod
    async def async_remove_stored(hass: HomeAssistant, entry_id: str) -> None:
        await Store(hass, STORE_VERSION, store_key(entry_id)).async_remove()

    def _save(self) -> None:
        """Write the book within `_SAVE_DELAY_S`: a write already waiting is not put off by the next look."""
        if self._save_pending:
            return
        if self._book_data() == self._saved:
            return
        self._save_pending = True
        self._store.async_delay_save(self._data_to_save, _SAVE_DELAY_S)

    def _data_to_save(self) -> dict[str, Any]:
        self._save_pending = False
        data = self._book_data()
        self._saved = copy.deepcopy(data)
        return data

    def _book_data(self) -> dict[str, Any]:
        self.book.set_section(SECTION_HISTORY, self.history.as_dict())
        self.book.set_section(SECTION_DAYS, {day: tally.as_dict() for day, tally in self.days.items()})
        return self.book.as_dict()

    # ---- reads

    def _tz(self) -> str:
        source = site_price_source_of(self.hass, self.site)
        if source is not None and source.area is not None:
            return source.area.tz
        return str(self.hass.config.time_zone)

    def _temperature_c(self) -> float | None:
        config = self.config
        if config is None or config.temperature_entity is None:
            return None
        state = self.hass.states.get(config.temperature_entity)
        value = None if state is None else _float(state.state)
        if value is None or state is None:
            return None
        if state.attributes.get("unit_of_measurement") == UnitOfTemperature.FAHRENHEIT:
            return (value - 32.0) * 5.0 / 9.0
        return value

    def _headroom_a(self) -> float | None:
        result = self.site.result
        if result.state in UNHEALTHY_STATES or result.state == "not_configured":
            return None
        values = [value for value in result.phase_headroom_a.values() if value is not None]
        return min(values) if values else None

    def _read_prices(
        self, now: datetime
    ) -> tuple[Any, Callable[[], tuple[list[PlanningSlot], FiscalChoice, tuple[str, ...]]]]:
        """`(key, read)`: what changes when the prices or the fees do, and how to read the site's published quarter
        hours of today and tomorrow with the fees and their flags (only when the key changed)."""
        source = site_price_source_of(self.hass, self.site)
        documents = price_days(self.hass, source, now)
        key = (
            None if source is None else (source.area_id, source.revision),
            tuple((doc.day, doc.interval_count, doc.retrieved, doc.published) for doc in documents),
        )

        def read() -> tuple[list[PlanningSlot], FiscalChoice, tuple[str, ...]]:
            if source is None or source.area is None or not documents:
                return [], FiscalChoice(), ()
            fiscal = source.fiscal
            flags: tuple[str, ...] = ()
            if fiscal is None:
                fiscal, flags = FiscalChoice(), (FLAG_FEES_UNKNOWN,)
            try:
                slots = list(planning_slots(tuple(documents), currency=source.area.currency, timezone=source.area.tz))
            except PlannerInputError:
                return [], fiscal, flags
            return slots, fiscal, flags

        return key, read

    def _room_kw_by_hour(self, now: datetime, hours: set[datetime]) -> dict[datetime, float]:
        """The import cap less the house's expected load and the cars' plans, per clock hour the house forecast
        knows; also counts the hours it does not know (`room_unknown_hours`)."""
        cap = self.import_cap_kw = site_import_cap_kw(self.hass, self.site)
        self.room_unknown_hours = 0
        if cap is None:
            return {}
        house = self.site.house_forecast.plan_kw_by_hour(now)
        windows = site_car_windows(self.hass, self.site)
        room: dict[datetime, float] = {}
        for start in hours:
            if start not in house:
                self.room_unknown_hours += 1
                continue
            room[start] = cap - house[start] - car_kw_in(start, start + timedelta(hours=1), windows)
        return room

    # ---- the plan

    def _plan_job(self, now: datetime) -> Callable[[], SwitchPlan] | None:
        """The plan to make, its inputs read here on the loop, or `None` when nothing it depends on changed since the
        last one (the prices, the quarter hour, the settings)."""
        config = self.config
        if config is None:
            return None
        key, read = self._read_prices(now)
        plan_key = (key, quarter_start(now), config)
        if plan_key == self._plan_key:
            return None
        self._plan_key = plan_key
        slots, fiscal, flags = read()
        tz = self._tz()

        def hour_of(start: datetime) -> datetime:
            return hour_start(start, tz)

        room = self._room_kw_by_hour(now, {hour_of(slot.start) for slot in slots if slot.end > now})
        self.flags = flags + (() if self.import_cap_kw is not None else (FLAG_NO_CAP,))
        self._prices = {
            slot.start: effective_minor_per_kwh(slot.local_major_per_kwh, fiscal, slot.start) for slot in slots
        }
        # A copy: the history goes on growing on the loop while the plan is made.
        history = RunHistory(self.history.tracked_since, list(self.history.runs))
        return partial(
            plan_switch, now=now, config=config, history=history, slots=slots, fiscal=fiscal,
            room_kw_by_hour=room, hour_of=hour_of,
        )

    async def async_replan(self, now: datetime) -> None:
        """Plan again now, when something changed and no plan is being made."""
        if self._planning:
            return
        job = self._plan_job(now)
        if job is not None:
            self._planning = True
            await self._async_run(job)

    async def _async_run(self, job: Callable[[], SwitchPlan]) -> None:
        """Make the plan in the executor (a long runtime takes up to a second); a failure leaves the last one and is
        tried again at the next look."""
        try:
            plan = await self.hass.async_add_executor_job(job)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("The switched load's plan failed")
            self._plan_key = None
            return
        finally:
            self._planning = False
        self.plan = plan
        self.status = STATUS_PLANNED if plan.priced_until is not None else STATUS_NO_PRICES

    def _schedule_replan(self, now: datetime) -> None:
        """At most one plan at a time: a change seen while one is made is planned at the next look."""
        if self._planning or self._closed:
            return
        job = self._plan_job(now)
        if job is None:
            return
        self._planning = True
        self._plan_task = self.hass.async_create_background_task(
            self._async_run(job), name=f"{DOMAIN} switched load plan {self.site.entry_id}"
        )

    # ---- each look

    @callback
    def _async_tick(self, _now: datetime) -> None:
        try:
            self.tick(dt_util.now())
        except Exception:  # noqa: BLE001 - the switched load's shadow must never disturb the site
            self._look_failed()

    def _account(self, now: datetime) -> None:
        """The time since the last look, at the last decision: the shadow's run history and the day's tallies."""
        last, self._last_tick = self._last_tick, now
        config = self.config
        if last is None or config is None or now <= last or now - last > MAX_GAP:
            return
        if self._last_on:
            self.history.note(last, now)
        if self._last_on or self._last_actual:
            day = dt_util.as_local(last).date().isoformat()
            tally = self.days.setdefault(day, DayTally())
            tally.add(
                (now - last).total_seconds(), self._prices.get(quarter_start(last)), config.rated_kw,
                shadow=self._last_on, actual=self._last_actual,
            )
            for old in sorted(self.days)[:-DAYS_KEPT]:
                del self.days[old]

    def tick(self, now: datetime) -> None:
        config = self.config
        if config is None:
            return
        state = self.hass.states.get(config.entity_id)
        readable = state is not None and state.state not in _UNREADABLE
        actual_on = readable and state is not None and state.state == "on"
        self._account(now)
        self._last_actual = actual_on
        self.history.prune(now)
        if self.history.tracked_since is None:
            self.history.tracked_since = now
        self._schedule_replan(now)
        memory = self.book.memory(PART_SWITCH_LOAD)
        decision = self.decision = decide(
            SwitchInputs(
                now=now,
                config=config,
                plan=self.plan,
                history=self.history,
                temperature_c=self._temperature_c(),
                floor_active=memory.get("floor_active") is True,
                headroom_a=self._headroom_a(),
                switch_on=actual_on,
                volts_between_phases=stored_voltage_between_phases_v(self.site.config),
            )
        )
        self._last_on = decision.on
        self._log(now, decision, state.state if state is not None else None, readable, memory)
        self._save()

    def _log(
        self, now: datetime, decision: SwitchDecision, read: str | None, readable: bool, memory: dict[str, Any]
    ) -> None:
        """One entry each time what the load would do (or whether it could) changes; nothing is written."""
        config = self.config
        assert config is not None
        value = "on" if decision.on else "off"
        action = "would_write" if readable else "not_possible"
        reason = decision.reason if readable else "switch_unavailable"
        changed = (memory.get("value"), memory.get("reason"), memory.get("action")) != (value, reason, action)
        self.book.set_memory(
            PART_SWITCH_LOAD,
            {
                "entity_id": config.entity_id,
                "value": value,
                "reason": reason,
                "action": action,
                "floor_active": decision.floor_active,
            },
        )
        if not changed:
            return
        # Shadow only: the decision is logged, never written (`ShadowBook.writes` is off in this release).
        entry = ShadowEntry(
            at=now, part=PART_SWITCH_LOAD, action=action, reason=reason, entity_id=config.entity_id, value=value,
            detail=dict(decision.detail), read=read,
        )
        self.book.record(entry)
        _LOGGER.debug("Switched load in shadow: would be %s (%s)", value, reason)

    # ---- diagnostics

    def snapshot(self) -> dict[str, Any] | None:
        """The load's settings, the plan, what it would do now and why, the days' runtime and cost, and the log;
        `None` without a load."""
        config = self.config
        if config is None:
            return None
        now = dt_util.now()
        decision = self.decision
        state = self.hass.states.get(config.entity_id)
        return {
            "config": {
                **config.as_dict(),
                "need_kwh": round(config.need_kwh, 3),
                "window_h": WINDOW.total_seconds() / 3600.0,
                "assumed_phases": config.phases,
            },
            "part": self.book.state(PART_SWITCH_LOAD),
            "writes": self.book.writes(PART_SWITCH_LOAD),
            "status": self.status,
            "flags": list(self.flags),
            "switch_state": None if state is None else state.state,
            "temperature_c": self._temperature_c(),
            "now": None
            if decision is None
            else {
                "on": decision.on,
                "reason": decision.reason,
                "floor_active": decision.floor_active,
                "detail": dict(decision.detail),
            },
            "plan": None if self.plan is None else self.plan.as_dict(config.rated_kw),
            "import_cap_kw": None if self.import_cap_kw is None else round(self.import_cap_kw, 3),
            "room_unknown_hours": self.room_unknown_hours,
            "history": {
                "tracked_since": None
                if self.history.tracked_since is None
                else self.history.tracked_since.isoformat(),
                "ran_last_window_h": round(self.history.on_seconds(now - WINDOW, now) / 3600.0, 3),
                "runs": self.history.as_dict()["runs"],
            },
            "days": {day: tally.as_dict() for day, tally in sorted(self.days.items())},
            "log": [entry.as_dict() for entry in self.book.entries],
        }
