"""The decision history's recorder for one site (plan `plans/v3_decision_history.md`): the shell around the ledger
(`site/decision_ledger.py`).

Once a minute (the site history's sample, `SiteCapacityController._async_sample_history`) it reads what the site's
controllers already hold, with no I/O (`observe`): the grid's import, each member charger's draw and what the import
target, a person's Stop or the car did to it, the home battery's power, whether the heat control holds the pump
lowered or raised, each floor-heating zone lowered, the water heater on or paused, and the shed order's step for the
ceiling. At the first sample of a clock hour it closes the hour before into rows and reads the plan for the new one,
once, from the plan view's own answer (`api/plan.async_plan_answer`, read-only; a read that fails is tried again
every `RETRY_S` and the row then says how late it was read). A plan read later never replaces it.

Kept in its own store (`spotnav.decision_ledger.<site>`), written off the event loop (`debug_day.DayStore`,
`off_loop`) at most every `SAVE_DELAY_S` and at Home Assistant's stop; the running hour is kept with it, so a restart
in the hour goes on with the plan read at its start and the samples so far. An hour Home Assistant did not run is
never written: it reads as missing. Never raises into the site.
"""

from __future__ import annotations

from datetime import datetime, timedelta
import logging
from typing import TYPE_CHECKING, Any, Final

from homeassistant.core import HomeAssistant, callback
from homeassistant.util import dt as dt_util

from .. import time_zones
from ..const import CONF_CHARGER_ENTRY_IDS, DOMAIN
from ..debug_day import DayStore
from .decision_ledger import (
    HINT_CAR_FULL,
    HINT_CAR_LIMITED,
    HINT_NOT_PLUGGED,
    REASON_ACCEPT_PEAK,
    REASON_CEILING,
    REASON_IMPORT_TARGET,
    REASON_PERSON_STOP,
    Ledger,
    LoadSample,
    OpenHour,
    Sample,
    close,
    hour_number,
    hour_of,
    snapshot,
)

if TYPE_CHECKING:
    from .site_capacity_controller import SiteCapacityController

_LOGGER = logging.getLogger(__name__)

#: How long a change waits for its write: the running hour's plan must survive a restart soon after it is read.
SAVE_DELAY_S: Final = 60.0
#: A plan read that failed is tried again after this.
RETRY_S: Final = 300.0
#: A charger drawing more than this (kW) charges.
CHARGING_KW: Final = 0.1
#: The battery's power beyond this (kW) is charging or covering.
BATTERY_KW: Final = 0.05


def store_key(entry_id: str) -> str:
    return f"{DOMAIN}.decision_ledger.{entry_id}"


def _kw(watts: float | None) -> float | None:
    return None if watts is None else watts / 1000.0


def _cars(site: SiteCapacityController, sample: Sample) -> float | None:
    """Each member charger into `sample`; their draw in all (kW), `None` when one cannot be read."""
    from ..execution.solar_execution import charger_draw_w  # noqa: PLC0415 - it imports the site
    from ..runtime import controller_for  # noqa: PLC0415

    hass = site.hass
    total: float | None = 0.0
    for charger_entry_id in site.config.get(CONF_CHARGER_ENTRY_IDS) or []:
        charger = controller_for(hass, charger_entry_id)
        if charger is None:
            continue
        kw = _kw(charger_draw_w(hass, site, charger_entry_id, charger, idle_unread_as_zero=True))
        total = None if total is None or kw is None else total + kw
        action = site.import_target_charger_row(charger_entry_id)[0]
        reason = None
        if action in ("held", "paused"):
            reason = REASON_IMPORT_TARGET
        elif action == "released":
            reason = REASON_ACCEPT_PEAK
        held_by_person = getattr(charger, "_held_off_by_person", None)
        if callable(held_by_person) and held_by_person():
            reason = REASON_PERSON_STOP
        charging = kw is not None and kw > CHARGING_KW
        connection = charger.connection()[0]
        hint = {"disconnected": HINT_NOT_PLUGGED, "finished": HINT_CAR_FULL}.get(connection)
        if hint is None and charging:
            hint = HINT_CAR_LIMITED
        entry = hass.config_entries.async_get_entry(charger_entry_id)
        sample.loads[charger_entry_id] = LoadSample(
            kind="car",
            name=None if entry is None else entry.title,
            kw=kw,
            what="charge" if charging else ("paused" if action == "paused" or reason == REASON_PERSON_STOP else None),
            reason=reason,
            hint=hint,
            shadow=not bool(charger.is_commandable or charger.is_switch_only),
        )
    return total


def _battery(site: SiteCapacityController, sample: Sample) -> float | None:
    """The home battery into `sample`; its power (kW, + charging), 0 without one, `None` unread."""
    facts = site.battery_shadow.facts
    configured, watts = site.battery_live_w()
    if facts.battery_entity_id is None:
        return 0.0 if not configured else _kw(watts)
    kw = _kw(watts)
    control = getattr(site, "battery_control", None)
    try:
        state = None if control is None else control.state()
    except Exception:  # noqa: BLE001 - read as watched
        state = None
    what = None
    if kw is not None and kw > BATTERY_KW:
        what = "charge"
    elif kw is not None and kw < -BATTERY_KW:
        what = "cover"
    sample.loads[facts.battery_entity_id] = LoadSample(
        kind="home_battery", name=facts.label, kw=kw, what=what, shadow=state != "on"
    )
    return kw if configured else 0.0


def _shed_reason(site: SiteCapacityController, load: str, held: bool) -> str | None:
    """`ceiling` while the shed order's step holds the load (a watched one's step as it would)."""
    order = getattr(site, "shed_order", None)
    step = None if order is None else order.step(load)
    if step is None:
        return None
    if step.live and not (step.taken and held):
        return None
    return REASON_CEILING


def _heat(site: SiteCapacityController, sample: Sample) -> None:
    """The heat pump, the floor-heating zones and the water heater into `sample`."""
    from ..heat.zones_control import STATE_RUNNING  # noqa: PLC0415 - the heat side imports the site
    from .shed_order import LOAD_HEAT, LOAD_WATER_HEATER  # noqa: PLC0415

    shadow = getattr(site, "heat_shadow", None)
    if shadow is None:
        return
    config = shadow.config()
    control = getattr(site, "heat_control", None)
    pump_id = config.control_entity or shadow.relay_id
    if pump_id and shadow.plan:
        status = None if control is None else control.status()
        live = bool(status and status.get("enabled") and status.get("state") == "running")
        held = bool(control is not None and control.held)
        raised = bool(control is not None and getattr(control, "raised", False))
        sample.loads[pump_id] = LoadSample(
            kind="heat",
            name=None,
            what="lower" if live and held else ("preheat" if live and raised else None),
            reason=_shed_reason(site, LOAD_HEAT, held),
            shadow=not live,
        )
    zones_control = getattr(site, "zones_control", None)
    for zone in config.zones:
        state = None if zones_control is None else zones_control.zones.get(zone.entity_id)
        running = bool(zone.control and state is not None and state.state == STATE_RUNNING)
        kw = None if zones_control is None else zones_control.kw.get(zone.entity_id)
        sample.loads[zone.entity_id] = LoadSample(
            kind="heat_zone",
            name=None,
            kw=kw if isinstance(kw, (int, float)) else None,
            what="lower" if running and state is not None and state.held_lower() else None,
            shadow=not running,
        )
    heater_control = getattr(site, "water_heater_control", None)
    status = None if heater_control is None else heater_control.status(config)
    if status is not None:
        held_off = bool(getattr(heater_control, "held_off", False))
        reason = _shed_reason(site, LOAD_WATER_HEATER, held_off)
        on = status.get("on")
        kw = status.get("kw")
        sample.loads[status["entity_id"]] = LoadSample(
            kind="water_heater",
            name=status.get("name"),
            kw=(float(kw) if on is True and isinstance(kw, (int, float)) else (0.0 if on is False else None)),
            what="paused" if reason is not None and held_off else ("on" if on is True else None),
            reason=reason,
            shadow=not bool(status.get("live")),
        )


def observe(site: SiteCapacityController) -> Sample:
    """One sample of the site now (see the module); a part that cannot be read is left out."""
    from ..execution.solar_execution import site_net_grid_w  # noqa: PLC0415 - it imports the site

    grid = _kw(site_net_grid_w(site))
    sample = Sample(grid_kw=grid, house_kw=None)
    cars: float | None = None
    battery: float | None = None
    for name, part in (("cars", _cars), ("battery", _battery), ("heat", _heat)):
        try:
            value = part(site, sample)
        except Exception:  # noqa: BLE001 - one part must not cost the sample
            _LOGGER.debug("SpotNav decision history: the %s could not be read", name, exc_info=True)
            continue
        if name == "cars":
            cars = value
        elif name == "battery":
            battery = value
    if grid is not None and cars is not None and battery is not None:
        sample.house_kw = grid - cars - battery
    return sample


class DecisionRecorder:
    """The site's decision history (see the module)."""

    def __init__(self, hass: HomeAssistant | None, entry_id: str) -> None:
        self._hass = hass
        self._entry_id = entry_id
        self.ledger = Ledger()
        self._store = (
            None
            if hass is None
            else DayStore(hass, store_key(entry_id), self._to_store, save_delay_s=SAVE_DELAY_S, off_loop=True)
        )
        self._reading: bool = False
        self._last_try: datetime | None = None

    def _to_store(self) -> dict[str, Any]:
        return self.ledger.as_store()

    async def async_load(self) -> None:
        if self._store is None:
            return
        stored = await self._store.async_load()
        try:
            ledger = Ledger.from_store(stored)
        except Exception:  # noqa: BLE001 - a history that cannot be read starts again
            _LOGGER.debug("SpotNav decision history: the kept hours could not be read", exc_info=True)
            return
        # Samples taken before the load (none: the load runs before the first sample) would go after it.
        self.ledger = ledger

    async def async_close(self) -> None:
        if self._store is not None:
            await self._store.async_close()

    @staticmethod
    async def async_remove_stored(hass: HomeAssistant, entry_id: str) -> None:
        await DayStore.async_remove(hass, store_key(entry_id))

    @property
    def first_hour(self) -> datetime | None:
        first = self.ledger.first_hour
        return None if first is None else hour_of(first)

    def _changed(self) -> None:
        if self._store is not None:
            self._store.changed()

    @callback
    def sample(self, site: SiteCapacityController, now: datetime | None = None) -> None:
        """Take one sample (see the module). Never raises."""
        try:
            self._sample(site, now or dt_util.utcnow())
        except Exception:  # noqa: BLE001 - the history must never disturb the site
            _LOGGER.debug("SpotNav decision history: could not take a sample", exc_info=True)

    def _sample(self, site: SiteCapacityController, now: datetime) -> None:
        number = hour_number(now)
        opened = self.ledger.open
        if opened is not None and opened.hour > number:
            # The clock went back: that hour is not this one.
            opened = self.ledger.open = None
        if opened is None or opened.hour != number:
            if opened is not None:
                self.close_hour(site, opened)
            opened = self.ledger.open = OpenHour(number)
            self._last_try = None
            tz = site.demand.tracker.tz
            zone = time_zones.require_zone(tz)
            self.ledger.trim(now.astimezone(zone).date(), zone)
        if opened.planned_at is None:
            self._read_plan(site, now)
        opened.add(observe(site))
        self._changed()

    def close_hour(self, site: SiteCapacityController, opened: OpenHour) -> None:
        """The ended hour into rows (the heat pump's power from the heat meter's hour, where it has it)."""
        extra: dict[str, float | None] = {}
        heat = getattr(site.house_forecast, "heat", None)
        if heat is not None and heat.meter.found:
            for load_id, load in opened.loads.items():
                if load.kind == "heat":
                    extra[load_id] = heat.by_hour.get(opened.start)
        hour_row, rows = close(opened, extra)
        self.ledger.keep(hour_row, rows)

    def _read_plan(self, site: SiteCapacityController, now: datetime) -> None:
        if self._hass is None or self._reading:
            return
        if self._last_try is not None and now - self._last_try < timedelta(seconds=RETRY_S):
            return
        self._last_try = now
        self._reading = True
        self._hass.async_create_background_task(
            self._async_read_plan(site, self.ledger.open.hour),  # type: ignore[union-attr]
            f"spotnav decision history plan {self._entry_id}",
        )

    async def _async_read_plan(self, site: SiteCapacityController, number: int) -> None:
        from ..api.plan import async_plan_answer  # noqa: PLC0415 - the API imports the site

        try:
            entry = None if self._hass is None else self._hass.config_entries.async_get_entry(self._entry_id)
            if entry is None:
                return
            answer = await async_plan_answer(self._hass, entry)  # type: ignore[arg-type]
            self.take_plan(answer, number, dt_util.utcnow())
        except Exception:  # noqa: BLE001 - tried again at the next sample after RETRY_S
            _LOGGER.debug("SpotNav decision history: the plan could not be read", exc_info=True)
        finally:
            self._reading = False

    def take_plan(self, answer: dict[str, Any], number: int, at: datetime) -> bool:
        """Keep the plan view's lanes for the hour `number` while it is still the running hour (once)."""
        opened = self.ledger.open
        if opened is None or opened.hour != number:
            return False
        found = snapshot(answer, hour_of(number))
        if found is None:
            return False
        site, loads = found
        kept = opened.plan(at, site, loads)
        if kept:
            self._changed()
        return kept
