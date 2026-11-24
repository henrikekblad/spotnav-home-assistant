"""The pool's and the spa's shadow (`PoolSpaShadow`, spec section 5.3): reads them, plans their hours and logs, in
shadow, what SpotNav would do. One per site, owned by its `SiteCapacityController`; idle unless the heat settings name
a pool or a spa (`heat/config.py`).

Every `TICK_S` it reads the pool's water (the chosen sensor, else the heat pump's own reading once it has run
`PUMP_READING_AFTER`), the heat pump and the circulation as they are, keeps each day's highest and lowest water reading
(the season hint), learns the heat pump's rise per hour from its real runs and the water's fall overnight, counts the
hours it would have run the circulation (its own rolling need, as the switched load's), plans again when the hour turns,
the prices or the settings change or a household control is pressed (`pool/plan.py`), and logs each change of what it
would do in its shadow book (`battery/shadow.py`, part `pool`). The sun's surplus over the house is read every
`SUN_REFRESH` (as the Plan tab reads it, `api/plan.py`).

It also counts each load's energy per hour as it really draws (the circulation and the heat pump while on, the spa
or the sauna while it heats, at the entered powers or typical ones) for the heat card's Use tab (`energy_hours`,
`DAYS_KEPT` days).

Nothing here writes to a device: no service is called. The settings are the heat settings'; the household's extra
filtering is kept here (`extra`). Stored in `spotnav.pool_spa.<entry>`, the days' figures for `DAYS_KEPT` days.
"""

from __future__ import annotations

import asyncio
import copy
import logging
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final

from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .. import time_zones
from ..battery.shadow import PART_POOL, ShadowBook, ShadowEntry
from ..const import DOMAIN
from ..core.parse import number_from_state
from ..heat.config import HeatConfig
from ..loads.switch_load import RunHistory
from ..planning.demand_charge import hour_start
from ..sauna.config import FAMILY_HARVIA, FAMILY_RELAY, FAMILY_TYLO, SaunaConfig
from ..spa.adapters import HARVIA_ON_TIME_KEY
from ..spa.config import SpaConfig
from ..spa.plan import HeatHour
from ..spa.runtime import DEFAULT_KW, Reading, new_sauna, new_spa
from .config import SHAPE_CLIMATE, PoolConfig
from .plan import (
    DEFAULT_CIRCULATION_KW,
    DEFAULT_HEAT_PUMP_KW,
    EXTRA_HOURS,
    EXTRA_SPAN,
    HOUR,
    MAX_HOURS,
    REASON_EXTRA,
    Extra,
    PoolInputs,
    PoolPlan,
    PriceHour,
    learnt_fall,
    learnt_rise,
    plan_pool,
    season_hint,
)

if TYPE_CHECKING:
    from ..site.site_capacity_controller import SiteCapacityController

_LOGGER = logging.getLogger(__name__)

TICK_S: Final = 30.0
STORE_VERSION: Final = 1
_SAVE_DELAY_S: Final = 300.0
MAX_GAP: Final = timedelta(minutes=2)
DAYS_KEPT: Final = 14
#: The heat pump's own water reading is valid once it has run this long (the water at the sensor has turned over).
PUMP_READING_AFTER: Final = timedelta(minutes=10)
SUN_REFRESH: Final = timedelta(minutes=15)
#: The overnight window the water's fall is learnt in (local hours), with no heat between.
NIGHT_FROM_H: Final = 22
NIGHT_TO_H: Final = 6
#: Runs and samples kept for the learning.
LEARN_KEPT: Final = 20

#: What the pool would do now (`would.action`).
ACTION_HEAT: Final = "heat"
ACTION_FILTER: Final = "filter"
ACTION_OFF: Final = "off"
REASON_WAITING: Final = "waiting"
REASON_MET: Final = "need_met"
REASON_SEASON: Final = "season_closed"
REASON_NO_PRICES: Final = "no_prices"
#: The controls' state while SpotNav only watches (the controls come later).
STATE_WATCHED: Final = "watched"
STATE_SEASON_CLOSED: Final = "season_closed"
NOTE_SEASON_CLOSE: Final = "pool_season_close"
#: The loads whose energy is counted per hour (the heat card's Use tab).
ENERGY_KINDS: Final = ("pool", "spa", "sauna")

ERROR_NO_POOL: Final = "spotnav_not_supported"

_UNREADABLE: Final = frozenset({"unavailable", "unknown"})


def store_key(entry_id: str) -> str:
    return f"{DOMAIN}.pool_spa.{entry_id}"


def _aware(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else None


def _float(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value:
        return None
    return float(value)


class PoolSpaShadow:
    """The site's pool and spa in shadow (see the module)."""

    def __init__(self, hass: HomeAssistant, site: SiteCapacityController) -> None:
        self.hass = hass
        self.site = site
        self._store: Store[dict[str, Any]] = Store(hass, STORE_VERSION, store_key(site.entry_id))
        self.book = ShadowBook()
        # ---- the pool
        self.history = RunHistory()
        self.plan: PoolPlan | None = None
        self.extra: Extra | None = None
        self.season_hinted: int | None = None
        #: Each local day's lowest and highest valid water reading.
        self.water_days: dict[str, list[float]] = {}
        self.heat_runs: list[tuple[datetime, datetime, float, float]] = []
        self.fall_samples: list[tuple[float, float, float]] = []
        self.water_c: float | None = None
        self.water_source: str | None = None
        self.would: dict[str, Any] | None = None
        #: When `would` was last decided (the control holds what is there once it is older than a few minutes).
        self.would_at: datetime | None = None
        # ---- the spa and the sauna
        self.spa = new_spa()
        self.sauna = new_sauna()
        self._pump_on_since: datetime | None = None
        self._run_start: tuple[datetime, float | None] | None = None
        self._night_start: tuple[datetime, float] | None = None
        self._night_heated = False
        self._last_tick: datetime | None = None
        #: Each load's energy per UTC hour (`energy_hours`): `{"pool" | "spa" | "sauna": {hour start ISO: kWh}}`.
        self.energy: dict[str, dict[str, float]] = {}
        self._energy_tick: datetime | None = None
        self._last_filter = False
        self._plan_key: Any = None
        self._surplus: dict[datetime, float] = {}
        self._sun_read: datetime | None = None
        self._sun_task: asyncio.Task[None] | None = None
        self._timer_cancel: Callable[[], None] | None = None
        self._loaded = False
        self._closed = False
        self._save_pending = False
        self._saved: dict[str, Any] | None = None
        self._look_failed_logged = False
        #: The sessions not ready last told to the site (`_tell_not_ready`).
        self._not_ready_seen: tuple[Any, ...] = ()

    # ---- life cycle

    async def async_start(self) -> None:
        """Load the book, then look every `TICK_S` (a look without a pool or a spa does nothing)."""
        now = dt_util.now()
        try:
            stored = await self._store.async_load()
            self._restore(stored, now)
        except Exception:  # noqa: BLE001 - a record SpotNav cannot read must not stop the site
            _LOGGER.warning("The pool and spa shadow log could not be read; starting a new one", exc_info=True)
            self.book = ShadowBook()
        self._loaded = True
        try:
            self.tick(now)
        except Exception:  # noqa: BLE001 - tried again at the next look
            self._look_failed()
        self._timer_cancel = async_track_time_interval(self.hass, self._async_tick, timedelta(seconds=TICK_S))

    def _restore(self, stored: Any, now: datetime) -> None:
        self.book = ShadowBook.from_dict(stored, now)
        pool = self.book.section("pool")
        self.history = RunHistory.from_dict(pool.get("history"))
        extra = pool.get("extra")
        if isinstance(extra, dict) and extra.get("hours") in EXTRA_HOURS and (until := _aware(extra.get("until"))) is not None:
            self.extra = Extra(int(extra["hours"]), until)
        hinted = pool.get("season_hinted")
        self.season_hinted = hinted if isinstance(hinted, int) and not isinstance(hinted, bool) else None
        days = pool.get("water_days")
        if isinstance(days, dict):
            self.water_days = {
                day: [float(row[0]), float(row[1])]
                for day, row in days.items()
                if isinstance(day, str) and isinstance(row, list) and len(row) == 2 and all(_float(v) is not None for v in row)
            }
        for row in pool.get("heat_runs") or ():
            if isinstance(row, list) and len(row) == 4:
                start, end, low, high = _aware(row[0]), _aware(row[1]), _float(row[2]), _float(row[3])
                if start and end and low is not None and high is not None:
                    self.heat_runs.append((start, end, low, high))
        for row in pool.get("fall_samples") or ():
            if isinstance(row, list) and len(row) == 3 and all(_float(v) is not None for v in row):
                self.fall_samples.append((float(row[0]), float(row[1]), float(row[2])))
        self.spa.restore(self.book.section("spa"))
        self.sauna.restore(self.book.section("sauna"))
        energy = self.book.section("energy")
        for kind in ENERGY_KINDS:
            rows = energy.get(kind)
            if isinstance(rows, dict):
                self.energy[kind] = {
                    key: float(value)
                    for key, value in rows.items()
                    if isinstance(key, str) and _aware(key) is not None and _float(value) is not None
                }

    def _look_failed(self) -> None:
        if self._look_failed_logged:
            _LOGGER.debug("SpotNav pool and spa: the look failed again", exc_info=True)
            return
        self._look_failed_logged = True
        _LOGGER.warning("SpotNav pool and spa: the look failed; it is tried again at each look", exc_info=True)

    @callback
    def halt(self) -> None:
        self._closed = True
        if self._sun_task is not None and not self._sun_task.done():
            self._sun_task.cancel()
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
        if self._save_pending or self._book_data() == self._saved:
            return
        self._save_pending = True
        self._store.async_delay_save(self._data_to_save, _SAVE_DELAY_S)

    def _data_to_save(self) -> dict[str, Any]:
        self._save_pending = False
        data = self._book_data()
        self._saved = copy.deepcopy(data)
        return data

    def _book_data(self) -> dict[str, Any]:
        self.book.set_section(
            "pool",
            {
                "history": self.history.as_dict(),
                "extra": None if self.extra is None else self.extra.as_dict(),
                "season_hinted": self.season_hinted,
                "water_days": {day: list(row) for day, row in self.water_days.items()},
                "heat_runs": [[a.isoformat(), b.isoformat(), low, high] for a, b, low, high in self.heat_runs],
                "fall_samples": [list(row) for row in self.fall_samples],
            },
        )
        self.book.set_section("spa", self.spa.as_dict())
        self.book.set_section("sauna", self.sauna.as_dict())
        self.book.set_section("energy", {kind: {key: round(kwh, 4) for key, kwh in rows.items()} for kind, rows in self.energy.items()})
        return self.book.as_dict()

    # ---- reads

    def config(self) -> HeatConfig:
        return self.site.heat_shadow.config()

    @property
    def tz(self) -> str:
        return self.site.demand.tracker.tz

    def _local(self, moment: datetime) -> datetime:
        return moment.astimezone(time_zones.require_zone(self.tz))

    def _celsius(self, value: float | None, unit: Any) -> float | None:
        if value is None:
            return None
        if unit == UnitOfTemperature.FAHRENHEIT:
            return (value - 32.0) * 5.0 / 9.0
        return value

    def _pump_state(self, pool: PoolConfig) -> tuple[bool | None, bool | None, float | None]:
        """The heat pump as it is: `(on, heating, its own water reading in °C)`; `None` where it cannot be read."""
        pump = pool.heat_pump
        if pump is None:
            return None, None, None
        system = self.hass.config.units.temperature_unit
        if pump.shape == SHAPE_CLIMATE:
            state = self.hass.states.get(pump.entity_id)
            if state is None or state.state in _UNREADABLE:
                return None, None, None
            reading = self._celsius(_float(state.attributes.get("current_temperature")), system)
            action = state.attributes.get("hvac_action")
            on = state.state != "off"
            return on, (action == "heating") if action is not None else on, reading
        switch = None if pump.switch_id is None else self.hass.states.get(pump.switch_id)
        on = None if switch is None or switch.state in _UNREADABLE else switch.state == "on"
        reading = None
        if pump.entity_id.startswith("sensor."):
            sensor = self.hass.states.get(pump.entity_id)
            if sensor is not None:
                reading = self._celsius(number_from_state(sensor.state), sensor.attributes.get("unit_of_measurement"))
        return on, on, reading

    def _switch_on(self, entity_id: str | None) -> bool | None:
        state = None if entity_id is None else self.hass.states.get(entity_id)
        if state is None or state.state in _UNREADABLE:
            return None
        return state.state == "on"

    def _water(self, pool: PoolConfig, now: datetime, pump_reading: float | None) -> tuple[float | None, str | None]:
        """The water now and where it is read: the chosen sensor, else the heat pump's own once it has run long
        enough; else `None`."""
        if pool.water_sensor is not None:
            state = self.hass.states.get(pool.water_sensor)
            if state is None:
                return None, None
            value = self._celsius(number_from_state(state.state), state.attributes.get("unit_of_measurement"))
            return value, None if value is None else "sensor"
        if pump_reading is not None and self._pump_on_since is not None and now - self._pump_on_since >= PUMP_READING_AFTER:
            return pump_reading, "heat_pump"
        return None, None

    def _prices(self, now: datetime) -> list[PriceHour]:
        """The window's whole hours with their mean buy and sell price, from the running hour on."""
        slots = self.site.battery_shadow.plan.buy_slots(now)
        first = hour_start(now, self.tz)
        sums: dict[datetime, list[float]] = {}
        for slot in slots:
            if slot.end <= first:
                continue
            start = hour_start(slot.start, self.tz)
            seconds = (slot.end - slot.start).total_seconds()
            row = sums.setdefault(start, [0.0, 0.0, 0.0])
            row[0] += slot.buy * seconds
            row[1] += slot.sell * seconds
            row[2] += seconds
        out: list[PriceHour] = []
        moment = first
        while moment in sums and sums[moment][2] >= 3599.0 and len(out) < MAX_HOURS:
            buy, sell, seconds = sums[moment]
            out.append(PriceHour(moment, buy / seconds, sell / seconds))
            moment += HOUR
        return out

    def _room(self, now: datetime, hours: list[datetime]) -> dict[datetime, float]:
        from ..battery.plan_runtime import site_car_windows, site_import_cap_kw  # noqa: PLC0415 - it imports the site
        from ..battery.plan_shadow import car_kw_in  # noqa: PLC0415

        cap = site_import_cap_kw(self.hass, self.site)
        if cap is None:
            return {}
        house = self.site.house_forecast.plan_kw_by_hour(now)
        windows = site_car_windows(self.hass, self.site)
        return {start: cap - house[start] - car_kw_in(start, start + HOUR, windows) for start in hours if start in house}

    async def _async_read_sun(self, now: datetime) -> None:
        """The sun's surplus over the house per hour (kW), as the Plan tab counts on both."""
        from ..api.plan import _house, _sun  # noqa: PLC0415 - the API imports the site

        try:
            sun = await _sun(self.hass, self.site, now)
            if sun is None:
                self._surplus = {}
            else:
                house, _basis, net_of_sun = _house(self.site, now)
                self._surplus = {
                    start: (max(0.0, -house[start]) if net_of_sun else kw - house[start])
                    for start, kw in sun.items()
                    if start in house
                }
            self._plan_key = None
        except Exception:  # noqa: BLE001 - without the sun the pool plans on the prices alone
            _LOGGER.debug("SpotNav pool: the sun could not be read", exc_info=True)

    # ---- each look

    @callback
    def _async_tick(self, _now: datetime) -> None:
        try:
            self.tick(dt_util.now())
        except Exception:  # noqa: BLE001 - the pool's shadow must never disturb the site
            self._look_failed()

    def request_look(self) -> None:
        """Plan again at once (a settings or household write)."""
        self._plan_key = None
        if self._loaded and not self._closed:
            try:
                self.tick(dt_util.now())
            except Exception:  # noqa: BLE001
                self._look_failed()

    def tick(self, now: datetime) -> None:
        config = self.config()
        if config.pool is None and config.spa is None and config.sauna is None:
            return
        if not self._closed and (self._sun_read is None or now - self._sun_read >= SUN_REFRESH):
            self._sun_read = now
            self._sun_task = self.hass.async_create_background_task(
                self._async_read_sun(now), name=f"{DOMAIN} pool sun {self.site.entry_id}"
            )
        # One look each, on its own: one that fails leaves the others' plans fresh (a stale one is not followed).
        failed = False
        if config.pool is not None:
            try:
                self._pool_tick(now, config.pool)
            except Exception:  # noqa: BLE001 - the spa and the sauna look all the same
                failed = True
        if config.spa is not None or config.sauna is not None:
            hours = self._heat_hours(now)
            for part, block in ((self.spa, config.spa), (self.sauna, config.sauna)):
                if block is None:
                    continue
                try:
                    part.look(now, self.tz, block, self._read_deadline(block), hours)
                except Exception:  # noqa: BLE001 - the other looks go on
                    failed = True
        if failed:
            self._look_failed()
        self._tell_not_ready(config)
        self._account_energy(now, config)
        self._save()

    def _tell_not_ready(self, config: HeatConfig) -> None:
        """A bath or a session that will not be ready (or no longer) changes what the site tells: its combined notice
        is told again (`SiteCapacityController._tell_target_risk`) when the sessions not ready change."""
        found = []
        for kind, part, block in (("spa", self.spa, config.spa), ("sauna", self.sauna, config.sauna)):
            not_ready = None if block is None or part.plan is None else part.plan.not_ready
            if not_ready is not None:
                found.append((kind, not_ready.at, not_ready.cause, not_ready.later))
        signature = tuple(found)
        if signature == self._not_ready_seen:
            return
        self._not_ready_seen = signature
        tell = getattr(self.site, "_tell_target_risk", None)
        if tell is None:
            return
        try:
            tell()
        except Exception:  # noqa: BLE001 - the shadow goes on; the next change tells again
            _LOGGER.debug("SpotNav site %s: the combined notice could not be told", self.site.entry_id, exc_info=True)

    def _draws(self, config: HeatConfig) -> dict[str, float]:
        """What each load draws now (kW), as read: the pool's circulation and heat pump while on, the spa or the
        sauna while it heats, each at its entered power or a typical one."""
        out: dict[str, float] = {}
        pool = config.pool
        if pool is not None:
            kw = 0.0
            if self._switch_on(pool.circulation_id):
                kw += pool.circulation_kw or DEFAULT_CIRCULATION_KW
            if self._pump_state(pool)[1]:
                kw += pool.heat_pump_kw or DEFAULT_HEAT_PUMP_KW
            out["pool"] = kw
        for kind, part, block in (("spa", self.spa, config.spa), ("sauna", self.sauna, config.sauna)):
            if block is not None and part.reading.heating:
                out[kind] = block.heater_kw or DEFAULT_KW[kind]
        return out

    def _account_energy(self, now: datetime, config: HeatConfig) -> None:
        """Count each load's energy since the last look into its hours (a gap over `MAX_GAP` is not counted), and
        forget the hours older than `DAYS_KEPT` days."""
        last, self._energy_tick = self._energy_tick, now
        if last is None or not last < now or now - last > MAX_GAP:
            return
        for kind, kw in self._draws(config).items():
            if kw <= 0:
                continue
            rows = self.energy.setdefault(kind, {})
            moment = last
            while moment < now:
                hour = moment.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
                end = min(now, hour + HOUR)
                key = hour.isoformat()
                rows[key] = rows.get(key, 0.0) + kw * (end - moment).total_seconds() / 3600.0
                moment = end
        oldest = (now - timedelta(days=DAYS_KEPT)).astimezone(UTC).isoformat()
        for rows in self.energy.values():
            for key in [key for key in rows if key < oldest]:
                del rows[key]

    def energy_hours(self, kind: str) -> dict[datetime, float]:
        """A load's energy per UTC hour start (kWh) over the days kept (`get_heat_history`'s `pool`, `spa`, `sauna`)."""
        out: dict[datetime, float] = {}
        for key, kwh in (self.energy.get(kind) or {}).items():
            moment = _aware(key)
            if moment is not None:
                out[moment] = kwh
        return out

    def _heat_hours(self, now: datetime) -> list[HeatHour]:
        """The priced hours with the sun's surplus, the room under the ceiling and which ceiling it is."""
        from ..site.flexible_loads import has_power_level  # noqa: PLC0415 - it imports the site
        from ..site.import_target import stored_import_target_kw  # noqa: PLC0415

        prices = self._prices(now)
        try:
            room = self._room(now, [hour.start for hour in prices])
        except Exception:  # noqa: BLE001 - without the room nothing is kept out
            _LOGGER.debug("SpotNav spa: the room could not be read", exc_info=True)
            room = {}
        try:
            ceiling = (
                "import_target"
                if stored_import_target_kw(self.site.config) is not None
                else "power_level" if has_power_level(self.site) else "fuse"
            )
        except Exception:  # noqa: BLE001
            ceiling = "fuse"
        return [
            HeatHour(hour.start, hour.buy, hour.sell, self._surplus.get(hour.start), room.get(hour.start), ceiling)
            for hour in prices
        ]

    def _read_deadline(self, config: SpaConfig | SaunaConfig) -> Reading:
        """The spa or the sauna as it is: its temperature, whether it heats, its lock and its own longest run."""
        state = self.hass.states.get(config.entity_id)
        system = self.hass.config.units.temperature_unit
        if state is None or state.state in _UNREADABLE:
            return Reading(None, None)
        if state.domain in ("switch", "input_boolean"):
            current, heating = None, state.state == "on"
        else:
            current = self._celsius(_float(state.attributes.get("current_temperature")), system)
            action = state.attributes.get("hvac_action")
            heating = action == "heating" if action is not None else state.state not in ("off", "idle")
        locked: bool | None = None
        unreadable = False
        max_on: timedelta | None = None
        if isinstance(config, SaunaConfig):
            if config.lock_id is not None:
                # Never bypassed: anything but a readable "off" (closed, not locked) keeps the sauna from starting.
                lock = self.hass.states.get(config.lock_id)
                unreadable = lock is None or lock.state not in ("on", "off")
                locked = unreadable or lock.state == "on"  # type: ignore[union-attr]
            if config.family == FAMILY_TYLO and state.attributes.get("door_fault_pending") is True:
                locked = True
            if config.family == FAMILY_HARVIA:
                max_on = self._harvia_on_time(config.entity_id)
        return Reading(current, heating, locked, max_on, unreadable)

    def _harvia_on_time(self, entity_id: str) -> timedelta | None:
        """Harvia's own on time (its `on_time` number on the sauna's device, minutes), where it reads."""
        from homeassistant.helpers import entity_registry as er  # noqa: PLC0415

        registry = er.async_get(self.hass)
        entry = registry.async_get(entity_id)
        if entry is None or entry.device_id is None:
            return None
        for item in er.async_entries_for_device(registry, entry.device_id):
            if item.domain == "number" and item.translation_key == HARVIA_ON_TIME_KEY:
                state = self.hass.states.get(item.entity_id)
                minutes = None if state is None else number_from_state(state.state)
                if minutes is not None and minutes > 0:
                    return timedelta(minutes=minutes)
        return None

    def _pool_tick(self, now: datetime, pool: PoolConfig) -> None:
        on, heating, reading = self._pump_state(pool)
        if heating:
            self._pump_on_since = self._pump_on_since or now
        else:
            self._pump_on_since = None
        water, source = self._water(pool, now, reading)
        if water is not None:
            self.water_c, self.water_source = water, source
            day = self._local(now).date().isoformat()
            row = self.water_days.setdefault(day, [water, water])
            row[0], row[1] = min(row[0], water), max(row[1], water)
            for old in sorted(self.water_days)[:-DAYS_KEPT]:
                del self.water_days[old]
        self._learn(now, bool(heating), water)
        self._account(now)
        if self.extra is not None and self.extra.until <= now:
            self.extra = None
        self._replan(now, pool)
        self._decide(now, pool)
        self._tell_season(now, pool)

    def _tell_season(self, now: datetime, pool: PoolConfig) -> None:
        """The season hint on the heat card's phones, once a season (`season_hinted`, the year told): only once it went
        to a phone, so one chosen later still hears it this season."""
        local = self._local(now)
        days = {date.fromisoformat(day): row[1] for day, row in self.water_days.items()}
        if not season_hint(days, local.date(), season_open=pool.season_open, hinted_year=self.season_hinted):
            return
        notice = getattr(self.site, "site_notice", None)
        if notice is not None and notice.pool_season(now):
            self.season_hinted = local.year

    def _learn(self, now: datetime, heating: bool, water: float | None) -> None:
        """The heat pump's real runs (their rise) and the water's overnight fall without heat."""
        if heating and self._run_start is None:
            self._run_start = (now, water)
        elif not heating and self._run_start is not None:
            start, low = self._run_start
            self._run_start = None
            if low is not None and water is not None:
                self.heat_runs = [*self.heat_runs, (start, now, low, water)][-LEARN_KEPT:]
        local = self._local(now)
        if heating:
            self._night_heated = True
        if local.hour == NIGHT_FROM_H and self._night_start is None and water is not None:
            self._night_start, self._night_heated = (now, water), heating
        elif local.hour == NIGHT_TO_H and self._night_start is not None:
            start, low = self._night_start
            self._night_start = None
            if water is not None and not self._night_heated:
                hours = (now - start).total_seconds() / 3600.0
                self.fall_samples = [*self.fall_samples, (low, water, hours)][-LEARN_KEPT:]

    def _account(self, now: datetime) -> None:
        last, self._last_tick = self._last_tick, now
        if last is not None and self._last_filter and last < now and now - last <= MAX_GAP:
            self.history.note(last, now)
        self.history.prune(now)
        if self.history.tracked_since is None:
            self.history.tracked_since = now

    def _day_water(self, now: datetime) -> float | None:
        today = self._local(now).date()
        for day in (today, today - timedelta(days=1)):
            row = self.water_days.get(day.isoformat())
            if row is not None:
                return row[1]
        return None

    def _replan(self, now: datetime, pool: PoolConfig) -> None:
        prices = self._prices(now)
        key = (pool, hour_start(now, self.tz), tuple(prices), self.extra, round(self._day_water(now) or -99.0, 1), round(self.water_c or -99.0, 1))
        if key == self._plan_key and self.plan is not None:
            return
        self._plan_key = key
        rise, _learnt = learnt_rise(self.heat_runs)
        fall, _learnt = learnt_fall(self.fall_samples)
        hours = [hour.start for hour in prices]
        try:
            room = self._room(now, hours)
        except Exception:  # noqa: BLE001 - without the room nothing is blocked
            _LOGGER.debug("SpotNav pool: the room could not be read", exc_info=True)
            room = {}
        local = self._local(now)
        day_end = (local.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)).astimezone(now.tzinfo)
        self.plan = plan_pool(
            PoolInputs(
                now=now,
                config=pool,
                prices=prices,
                history=RunHistory(self.history.tracked_since, list(self.history.runs)),
                water_c=self.water_c,
                day_water_c=self._day_water(now),
                rise_c_h=rise,
                fall_c_day=fall,
                surplus_kw=self._surplus,
                room_kw=room,
                extra=self.extra,
                day_end=day_end,
            )
        )

    def _decide(self, now: datetime, pool: PoolConfig) -> None:
        """What the pool would do now, logged on each change; nothing is written."""
        plan = self.plan
        hour = hour_start(now, self.tz)
        if not pool.season_open:
            would = None
            self._last_filter = False
        elif plan is None or (not plan.circulation_hours and not plan.heat_hours and not self._prices(now)):
            # No prices: the day's filtering need is still run, in any hours (moved, never dropped; review S5).
            from .plan import filter_need_h  # noqa: PLC0415

            need_h = plan.need_h if plan is not None else filter_need_h(pool, self._day_water(now))[0]
            day_start = self._local(now).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(now.tzinfo)
            short = pool.circulation_id is not None and self.history.on_seconds(day_start, now) / 3600.0 < need_h
            if short:
                would = {"action": ACTION_FILTER, "until": None, "reason": "need"}
            else:
                would = {"action": ACTION_OFF, "until": None, "reason": REASON_NO_PRICES}
            self._last_filter = short
        else:
            if hour in plan.heat_hours:
                action, reason, runs = ACTION_HEAT, plan.heat_hours[hour], plan.heat
            elif hour in plan.circulation_hours:
                action, reason, runs = ACTION_FILTER, plan.circulation_hours[hour], plan.circulation
            else:
                later = any(start > hour for start in plan.circulation_hours)
                action, reason, runs = ACTION_OFF, REASON_WAITING if later else REASON_MET, ()
            until = next((end for start, end, _why in runs if start <= hour < end), None)
            if action == ACTION_OFF:
                until = min((start for start in plan.circulation_hours if start > hour), default=None)
            would = {"action": action, "until": None if until is None else until.isoformat(), "reason": reason}
            self._last_filter = action in (ACTION_HEAT, ACTION_FILTER) and pool.circulation_id is not None
        changed = (self.would or {}).get("action"), (self.would or {}).get("reason")
        self.would = would
        self.would_at = now
        if would is not None and changed != (would["action"], would["reason"]):
            self.book.record(
                ShadowEntry(
                    at=now,
                    part=PART_POOL,
                    action="would_write",
                    reason=str(would["reason"]),
                    entity_id=pool.circulation_id or (None if pool.heat_pump is None else pool.heat_pump.entity_id),
                    value=str(would["action"]),
                )
            )
            _LOGGER.debug("Pool in shadow: would %s (%s)", would["action"], would["reason"])

    # ---- the household's controls

    def set_extra(self, hours: int | None, now: datetime) -> Extra | None:
        """Start (or replace) an extra filtering of `hours`, or end it (`None`)."""
        self.extra = None if hours is None else Extra(hours, now + EXTRA_SPAN)
        self.request_look()
        return self.extra

    # ---- what the card, the plan and the descriptions read

    def _hours_in(self, runs: dict[datetime, str], first: datetime, end: datetime) -> float:
        return float(sum(1 for start in runs if first <= start < end))

    def load_facts(self) -> dict[str, Any]:
        """The circulation's need, its extra and its next deadline, for the flexible-load descriptions."""
        plan = self.plan
        if plan is None:
            return {}
        from ..loads.switch_load import deadlines  # noqa: PLC0415

        steps = deadlines(self.history, dt_util.now(), plan.need_h)
        return {
            "need_h": plan.need_h,
            "extra_h": float(plan.extra.hours) if plan.extra is not None else 0.0,
            "by": steps[-1][0] if steps else None,
        }

    def spa_facts(self) -> dict[str, Any]:
        config = self.config()
        return {} if config.spa is None else self.spa.facts(config.spa)

    def sauna_facts(self) -> dict[str, Any]:
        config = self.config()
        return {} if config.sauna is None else self.sauna.facts(config.sauna)

    def spa_status(self, config: HeatConfig, now: datetime) -> dict[str, Any] | None:
        """`get_heat`'s `spa` (`heat/view.py`), `None` without a spa."""
        spa = config.spa
        if spa is None:
            return None
        common = self.spa.common(spa, now)
        self._controlled(common, "spa", now, spa)
        current = self.spa.reading.current_c
        return {
            "family": common["family"],
            "water_c": None if current is None else round(current, 1),
            "bath_c": spa.bath_c,
            "idle_c": spa.idle_c,
            "floor_c": spa.floor_c,
            "heating": common["heating"],
            "kw": common["kw"],
            "rate_c_h": common["rate_c_h"],
            "rate_learned": common["rate_learned"],
            "state": common["state"],
            "ready_at": common["ready_at"],
            "next_bath": common["next"],
            "baths": [bath.as_dict() for bath in spa.baths],
            "bath_once": spa.bath_once,
            "bath_now": common["now"],
            "live": common["live"],
            "control": common["control"],
            "would": common["would"],
            "not_ready": common["not_ready"],
            "planned": common["planned"],
            "notes": common["notes"],
        }

    def sauna_status(self, config: HeatConfig, now: datetime) -> dict[str, Any] | None:
        """`get_heat`'s `sauna` (`heat/view.py`), `None` without a sauna."""
        sauna = config.sauna
        if sauna is None:
            return None
        common = self.sauna.common(sauna, now)
        self._controlled(common, "sauna", now, sauna)
        current = self.sauna.reading.current_c
        return {
            "family": common["family"],
            "temp_c": None if current is None else round(current, 1),
            "target_c": sauna.target_c,
            "heating": common["heating"],
            "kw": common["kw"],
            "rate_c_h": common["rate_c_h"],
            "rate_learned": common["rate_learned"],
            "state": common["state"],
            "ready_at": common["ready_at"],
            "next_session": common["next"],
            "sessions": [session.as_dict() for session in sauna.sessions],
            "session_once": sauna.session_once,
            "session_now": common["now"],
            "locked": self.sauna.reading.locked,
            "lock_id": sauna.lock_id,
            "safety_declared": sauna.safety_declared if sauna.family == FAMILY_RELAY else None,
            "live": common["live"],
            "control": common["control"],
            "would": common["would"],
            "not_ready": common["not_ready"],
            "planned": common["planned"],
            "notes": common["notes"],
        }

    def _controlled(self, common: dict[str, Any], kind: str, now: datetime, block: Any) -> None:
        """The spa's or the sauna's control block from its control where the opt-in is on (or SpotNav still holds
        something on it), else the shadow's watched one; `failures` counted either way."""
        found = self._control_part(kind, now, block.control, block)
        if found is None:
            common["control"] = {**common["control"], "failures": 0}
            return
        common["control"] = {
            "state": found["state"], "reason": found["reason"], "paused_until": found["paused_until"], "failures": found["failures"],
        }
        common["live"] = found["live"]

    def deadline_lanes(self, config: HeatConfig) -> list[dict[str, Any]]:
        """What the Plan tab draws of the spa and the sauna."""
        out = []
        for part, block in ((self.spa, config.spa), (self.sauna, config.sauna)):
            if block is not None and (lanes := part.lanes(block)) is not None:
                out.append(lanes)
        return out

    def shed_offers(self, now: datetime) -> dict[str, Any]:
        """The pool's, the spa's and the sauna's offers to the site's shed order (`site/shed_order.Offer`): the pool's
        heat pump while it heats (its entered kW, else a typical one: a learnt figure is allowed for this low-risk
        load); the circulation beyond the day's filtering need, or while the need still fits after the period
        (`until_max`), with the heat pump it carries; the spa heating towards a booked bath (`spa`) or holding its
        idle temperature (`spa_idle`, never below its floor); the sauna heating for a session. A load SpotNav controls
        locally offers live (`pool/control.py`); a watched or cloud one is `would` only. A load not running now offers
        nothing to give."""
        from ..site.shed_order import LOAD_POOL_CIRCULATION, LOAD_POOL_HEAT, LOAD_SAUNA, LOAD_SPA, LOAD_SPA_IDLE, Offer  # noqa: PLC0415
        from ..spa.plan import STATE_HEATING, STATE_LATE  # noqa: PLC0415

        out: dict[str, Any] = {}
        config = self.config()
        pool = config.pool
        control = getattr(self.site, "pool_spa_control", None)
        if pool is not None and pool.season_open:
            on, heating, _reading = self._pump_state(pool)
            pump_w = (pool.heat_pump_kw or DEFAULT_HEAT_PUMP_KW) * 1000.0 if heating else 0.0
            out[LOAD_POOL_HEAT] = Offer(live=False, ready=bool(heating), give_w=pump_w, drawing=bool(heating))
            day_start = self._local(now).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(now.tzinfo)
            done_h = self.history.on_seconds(day_start, now) / 3600.0
            need_h = None if self.plan is None else self.plan.need_h
            beyond = need_h is not None and done_h >= need_h
            until_max = None
            if need_h is not None and not beyond:
                # The latest a pause may end and still leave the day's need before its deadline (moved, never dropped).
                by = self.load_facts().get("by") or (day_start + timedelta(days=1))
                until_max = by - timedelta(hours=need_h - done_h)
            circulating = self._switch_on(pool.circulation_id)
            if circulating:
                give = (pool.circulation_kw or DEFAULT_CIRCULATION_KW) * 1000.0 + pump_w
                fits = beyond or (until_max is not None and until_max > now)
                out[LOAD_POOL_CIRCULATION] = Offer(
                    live=False, ready=fits, give_w=give, why=None if fits else "need", until_max=None if beyond else until_max
                )
            if control is not None:
                out.update(control.pool.offers(now, pool, beyond=beyond, until_max=until_max))
        for part, block, booked_load in ((self.spa, config.spa, LOAD_SPA), (self.sauna, config.sauna, LOAD_SAUNA)):
            if block is None:
                continue
            if part.reading.heating:
                give = (block.heater_kw or 0.0) * 1000.0
                plan = part.plan
                booked = plan is not None and plan.state in (STATE_HEATING, STATE_LATE)
                if booked or isinstance(block, SaunaConfig):
                    out[booked_load] = Offer(live=False, ready=True, give_w=give)
                else:
                    current = part.reading.current_c
                    warm = current is not None and current > block.floor_c + 1.0
                    out[LOAD_SPA_IDLE] = Offer(live=False, ready=warm, give_w=give, why=None if warm else "floor")
            if control is not None:
                out.update(getattr(control, booked_load).offers(now, block))
        return out

    def _control_part(self, name: str, now: datetime, enabled: bool, block: Any) -> dict[str, Any] | None:
        """A part's control as the card shows it (`pool/control.py`), `None` while the opt-in is off and SpotNav holds
        nothing on it."""
        control = getattr(self.site, "pool_spa_control", None)
        if control is None:
            return None
        if name in ("heat_pump", "circulation"):
            found = control.pool.status(now, block)[name]
            holds = (control.pool.hp if name == "heat_pump" else control.pool.circ).holds()
        else:
            part = getattr(control, name)
            found = part.status(now, block)
            holds = part.part.holds()
        return found if enabled or holds else None

    def pool_status(self, config: HeatConfig, now: datetime) -> dict[str, Any] | None:
        """`get_heat`'s `pool` (`heat/view.py`), `None` without a pool."""
        pool = config.pool
        if pool is None:
            return None
        plan = self.plan
        on, heating, _reading = self._pump_state(pool)
        local = self._local(now)
        day_start = local.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = (day_start + timedelta(days=1)).astimezone(now.tzinfo)
        day_start_utc = day_start.astimezone(now.tzinfo)
        hour = hour_start(now, self.tz)
        state = STATE_WATCHED if pool.season_open else STATE_SEASON_CLOSED
        would = self.would
        action = None if would is None else would["action"]
        done_h = round(self.history.on_seconds(day_start_utc, now) / 3600.0, 2)
        planned_h = 0.0 if plan is None else self._hours_in(dict(plan.circulation_hours), hour, day_end)
        heat_h = 0.0 if plan is None else self._hours_in(dict(plan.heat_hours), hour, day_end)
        circ_kw, pump_kw = pool.circulation_kw, pool.heat_pump_kw
        kwh = None
        if circ_kw is not None and (pump_kw is not None or pool.heat_pump is None):
            kwh = round((done_h + planned_h) * circ_kw + heat_h * (pump_kw or 0.0), 2)
        planned: list[dict[str, Any]] = []
        if plan is not None:
            for start, end, why in plan.heat:
                if end > now:
                    planned.append({"start": start.isoformat(), "end": end.isoformat(), "what": "heat", "reason": why})
            heat_hours = set(plan.heat_hours)
            for start, end, why in plan.circulation:
                moment = start
                # The circulation's own hours only: its hours under the heat pump are the heat pump's run.
                while moment < end:
                    if moment not in heat_hours and moment + HOUR > now:
                        what = "extra" if why == REASON_EXTRA else "filter"
                        if planned and planned[-1]["what"] == what and planned[-1]["reason"] == why and planned[-1]["end"] == moment.isoformat():
                            planned[-1]["end"] = (moment + HOUR).isoformat()
                        else:
                            planned.append({"start": moment.isoformat(), "end": (moment + HOUR).isoformat(), "what": what, "reason": why})
                    moment += HOUR
            planned.sort(key=lambda item: item["start"])
        days = {date.fromisoformat(day): row[1] for day, row in self.water_days.items()}
        hint = season_hint(days, local.date(), season_open=pool.season_open, hinted_year=None)
        reason_now = None if would is None else would["reason"]
        hp_control = None if pool.heat_pump is None else self._control_part("heat_pump", now, pool.control_heat_pump, pool)
        circ_control = None if pool.circulation_id is None else self._control_part("circulation", now, pool.control_circulation, pool)

        def part_control(found: dict[str, Any] | None) -> dict[str, Any]:
            if found is None or not pool.season_open:
                return {"live": False, "state": state, "paused_until": None, "failures": 0 if found is None else found["failures"]}
            return {"live": found["live"], "state": found["state"], "paused_until": found["paused_until"], "failures": found["failures"]}

        return {
            "season_open": pool.season_open,
            "water_c": None if self.water_c is None else round(self.water_c, 1),
            "water_source": self.water_source,
            "target_c": pool.target_c,
            "heat_pump": None
            if pool.heat_pump is None
            else {
                "on": on,
                "heating": heating,
                "kw": pump_kw,
                **part_control(hp_control),
                "reason": None
                if not pool.season_open
                else hp_control["reason"]
                if hp_control is not None and hp_control["state"] != "controlled"
                else (reason_now if action == ACTION_HEAT else REASON_WAITING if heat_h else REASON_MET),
            },
            "circulation": None
            if pool.circulation_id is None
            else {
                "on": self._switch_on(pool.circulation_id),
                "kw": circ_kw,
                **part_control(circ_control),
                "reason": None
                if not pool.season_open
                else circ_control["reason"]
                if circ_control is not None and circ_control["state"] != "controlled"
                else reason_now,
            },
            "today": {
                "need_h": None if plan is None else plan.need_h,
                "need_source": None if plan is None else plan.need_source,
                "done_h": done_h,
                "planned_h": planned_h,
                "heat_h": None if plan is None else plan.heat_h,
                "kwh": kwh,
            },
            "planned": planned,
            "extra": None if self.extra is None or not pool.season_open else self.extra.as_dict(),
            "season_hint": hint,
            "would": would,
            "notes": [{"code": NOTE_SEASON_CLOSE, "params": {}}] if hint else [],
        }

    def pool_lanes(self, config: HeatConfig) -> dict[str, Any] | None:
        """What the Plan tab draws of the pool (`site/plan_view.PoolPlanView`), `None` without a pool or a plan."""
        pool = config.pool
        plan = self.plan
        if pool is None or plan is None or not pool.season_open:
            return None
        return {
            "circulation": None if pool.circulation_id is None else dict(plan.circulation_hours),
            "heat": None if pool.heat_pump is None else dict(plan.heat_hours),
            "circulation_kw": pool.circulation_kw,
            "heat_pump_kw": pool.heat_pump_kw,
            "need_h": plan.need_h,
            "heat_h": plan.heat_h,
            "circulation_name": None if pool.circulation_id is None else pool.circulation_id,
            "heat_pump_name": None if pool.heat_pump is None else pool.heat_pump.entity_id,
        }

    def diagnostics(self) -> dict[str, Any]:
        rise = learnt_rise(self.heat_runs)
        fall = learnt_fall(self.fall_samples)
        return {
            "plan": None if self.plan is None else self.plan.as_dict(),
            "would": self.would,
            "water_c": self.water_c,
            "water_source": self.water_source,
            "water_days": dict(self.water_days),
            "rise_c_h": {"value": rise[0], "learnt": rise[1]},
            "fall_c_day": {"value": fall[0], "learnt": fall[1]},
            "extra": None if self.extra is None else self.extra.as_dict(),
            "history": self.history.as_dict(),
            "surplus_hours": len(self._surplus),
            "spa": None if self.spa.plan is None else self.spa.plan.as_dict(),
            "sauna": None if self.sauna.plan is None else self.sauna.plan.as_dict(),
            "log": [entry.as_dict() for entry in self.book.entries],
            "control": None if getattr(self.site, "pool_spa_control", None) is None else self.site.pool_spa_control.diagnostics(),
        }
