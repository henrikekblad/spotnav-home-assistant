"""The site's heat in shadow (plan `plans/v3_heating_plan.md` steps 2 and 3): the heat pump's relay known to the
plan, SpotNav's own relay plan made jointly with the cars, the battery and the import cap, logged, kept per hour and
reviewed per day beside what the relay really did. One per site, owned by its `SiteCapacityController`.

Nothing here writes to a heating device: no service is called, no automation is touched. The relay is read only;
SpotNav's own switching of it, behind the person's opt-in, is the heat control (`heat/control.py`), which carries out
this plan and tells what it will close ahead (`control.closings_ahead`).

* **The relay** (the heat settings' control entity on the relay or SG Ready path; relay A for SG Ready): its closed
  intervals from the recorder (the last `RELAY_DAYS`, read again every hour) and its state now; whoever closes it.
  Its closings ahead are the SpotNav blueprint's where an enabled one drives it (`heat/relay_schedule.py`, from the
  blueprint's inputs and the price's expensive intervals), else a closing now lasts at most three hours.
* **The forecast's change** (`forecast_delta`, read by the house forecast's plan figures): the lowered heat and the
  rebound (`heat/relay_effect.py`) of the relay's past closings whose rebound is still to come, the closing now and
  the closings ahead. The heat per hour is the house forecast's heat part where the meter has made one, else the
  meter's mean over its last day.
* **Learning**: each closing is measured on the meter's 5-minute rows once its rebound window has passed (within the
  recorder's short-term days), kept (`relay_effect.KEEP_BLOCKS`), and the effect learned again.
* **SpotNav's plan** (`heat/shadow_rule.band`, in the executor): today and tomorrow, every hour and when the prices
  change, with the joint plan's tight hours (the house forecast without the relay's change, the cars' installed plans
  and the battery plan's grid charge, against the import cap), the outdoor forecast and the coldest counted room now.
  What it would do now is logged at each change (`log`), and each hour's action is kept for the day as it was planned
  when the hour began (`days`).
* **The review** (`heat/review.py`): each day kept with a plan, once the three hours after its end have passed and
  while the recorder still holds its 5-minute rows: the relay's real closings and SpotNav's plan, money and comfort.
* **Notes**: the enabled automations that reference the relay or a zone (`heat/automations.py`), read every look.

Kept in the site's store (`spotnav.heat_shadow.<entry>`): the measured blocks and `DAYS_KEPT` days of plans and
reviews. Shown in the heat card (`api/heat.py`) and the diagnostics.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime, timedelta, timezone
from functools import partial
from typing import TYPE_CHECKING, Any, Final

from homeassistant.core import HomeAssistant, State, callback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .. import time_zones
from ..const import CONF_HEAT, DOMAIN
from .automations import SteeringAutomation, steering_automations
from .config import BAND_C, PATH_RELAY, PATH_SG_READY, HeatConfig, read_config
from .meter_split import hot_water_runs, levels
from .relay_effect import (
    AFTER,
    BEFORE,
    HOUR,
    KEEP_BLOCKS,
    BlockResult,
    Effect,
    closed_intervals,
    forecast_delta,
    hour_of,
    hour_shares,
    learn,
    measure_block,
)
from .relay_schedule import expected_closings, too_cold, unknown_closing
from .review import review_day
from .rooms import SensorReading, indoor
from .shadow_rule import ACTION_LOWER, BandHour, band, day_hours, would_do

if TYPE_CHECKING:
    from ..site.site_capacity_controller import SiteCapacityController
    from .control import HeatControl

_LOGGER = logging.getLogger(__name__)

TICK_S: Final = 60.0
STORE_VERSION: Final = 1
SAVE_DELAY_S: Final = 60.0
#: The relay's closings read back this far (a block's rebound window and the day before).
RELAY_DAYS: Final = 2
#: Days of plans and reviews kept.
DAYS_KEPT: Final = 35
#: A closing not measurable this long after its rebound window ends is given up.
GIVE_UP: Final = timedelta(hours=6)
LOG_SIZE: Final = 96
#: The relay's state values that read as closed.
CLOSED: Final = frozenset({"on"})

#: A sensor that has not reported within this long is frozen: read as none (outdoors, the rooms, the meter).
STALE_AFTER: Final = timedelta(hours=3)

SOURCE_BLUEPRINT: Final = "blueprint"
SOURCE_SPOTNAV: Final = "spotnav"
SOURCE_UNKNOWN: Final = "unknown"


def store_key(entry_id: str) -> str:
    return f"{DOMAIN}.heat_shadow.{entry_id}"


class ShadowLog:
    """What SpotNav's plan would do now, each change once, newest last, bounded: the review reads it later."""

    def __init__(self) -> None:
        self.entries: deque[dict[str, Any]] = deque(maxlen=LOG_SIZE)

    def record(self, now: datetime, would: Mapping[str, Any] | None) -> None:
        key = None if would is None else (would.get("action"), would.get("target_c"), would.get("until"))
        last = self.entries[-1] if self.entries else None
        if last is not None and last.get("key") == key:
            return
        entry = {"at": now.isoformat(), "key": key, **({} if would is None else dict(would))}
        self.entries.append(entry)
        _LOGGER.debug("SpotNav heat (shadow): would %s", "keep the pump's own curve" if would is None else would)

    def as_list(self) -> list[dict[str, Any]]:
        return [{k: v for k, v in entry.items() if k != "key"} for entry in self.entries]


def fresh_state(hass: HomeAssistant, entity_id: str) -> State | None:
    """The entity's state when it reads and has reported within `STALE_AFTER`, else `None`."""
    state = hass.states.get(entity_id)
    if state is None or state.state in ("unavailable", "unknown"):
        return None
    reported = getattr(state, "last_reported", None) or state.last_updated
    return state if dt_util.utcnow() - reported <= STALE_AFTER else None


def temperature_c(hass: HomeAssistant, entity_id: str) -> float | None:
    """A temperature sensor's or a weather entity's temperature now (°C, °F converted), `None` when it does not read
    or is frozen."""
    from ..site.house_forecast import to_celsius  # noqa: PLC0415 - the site imports this module

    state = fresh_state(hass, entity_id)
    if state is None:
        return None
    if entity_id.startswith("weather."):
        return to_celsius(state.attributes.get("temperature"), state.attributes.get("temperature_unit"))
    return to_celsius(state.state, state.attributes.get("unit_of_measurement"))


def relay_entity(config: HeatConfig) -> str | None:
    """The relay the heat settings name (relay A for SG Ready), `None` on another path."""
    if config.control_path in (PATH_RELAY, PATH_SG_READY):
        return config.control_entity
    return None


def merged(intervals: Sequence[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    """The intervals with those that touch or overlap joined (a closing now: its past part and the rest ahead are one
    block, with one rebound after its real end)."""
    out: list[tuple[datetime, datetime]] = []
    for start, end in sorted(intervals):
        if out and start <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], end))
        else:
            out.append((start, end))
    return out


def battery_charge_by_hour(slots: Sequence[Any]) -> dict[datetime, float]:
    """The battery plan's grid charge per UTC hour (kW, the hour's mean) from its planned slots."""
    out: dict[datetime, float] = {}
    for slot in slots:
        if getattr(slot, "action", None) != "grid_charge":
            continue
        kw = abs(float(getattr(slot, "battery_kw", 0.0) or 0.0))
        for hour, share in hour_shares(slot.start, slot.end):
            out[hour] = out.get(hour, 0.0) + kw * share
    return out


def tight_hours(
    cap_kw: float | None,
    house_kw: Mapping[datetime, float],
    car_kw: Callable[[datetime], float],
    battery_kw: Mapping[datetime, float],
    heat_kw: Callable[[datetime], float | None],
    effect: Effect,
) -> frozenset[datetime]:
    """The hours whose joint load (the house, the cars' plans, the battery's grid charge) reaches the import cap
    within what lowering the heat frees there (`effect.lower_share` x the hour's heat)."""
    if cap_kw is None:
        return frozenset()
    out = set()
    for hour, house in house_kw.items():
        freed = effect.lower_share * (heat_kw(hour) or 0.0)
        if freed <= 0:
            continue
        if house + car_kw(hour) + battery_kw.get(hour, 0.0) >= cap_kw - freed:
            out.add(hour)
    return frozenset(out)


def plan_days(
    config: HeatConfig,
    days: Sequence[list[datetime]],
    prices: Mapping[datetime, float],
    now: datetime,
    zone: Any,
    *,
    outdoor_c: float | None,
    coldest_c: float | None,
    tight: frozenset[datetime],
    outdoor_by_hour: Mapping[datetime, float],
) -> tuple[list[BandHour], str | None]:
    """SpotNav's plan for the days given (the executor's part): each day's band, and today's guard."""
    hours: list[BandHour] = []
    guard: str | None = None
    for index, day in enumerate(days):
        banded, day_guard = band(
            config,
            day,
            prices,
            now,
            zone,
            outdoor_c=outdoor_c,
            coldest_c=coldest_c,
            tight=tight,
            outdoor_by_hour=outdoor_by_hour,
        )
        if index == 0:
            guard = day_guard
        hours.extend(banded)
    return hours, guard


def coldest_by_period(
    rows: Mapping[str, Sequence[Mapping[str, Any]]], offsets: Mapping[str, float] | None = None
) -> dict[datetime, float]:
    """The lowest of the rooms' 5-minute means less each room's offset per period (°C by UTC period start): the
    figure the house's comfort band is compared with."""
    out: dict[datetime, float] = {}
    for entity_id, series in rows.items():
        offset = (offsets or {}).get(entity_id, 0.0)
        for row in series:
            start, mean = row.get("start"), row.get("mean")
            if isinstance(start, (int, float)) and not isinstance(start, bool):
                start = datetime.fromtimestamp(float(start), timezone.utc)
            if not isinstance(start, datetime) or not isinstance(mean, (int, float)):
                continue
            out[start] = min(out.get(start, float(mean) - offset), float(mean) - offset)
    return out


class HeatShadow:
    """The site's heat in shadow (see the module). Never raises into the site."""

    def __init__(self, hass: HomeAssistant, site: SiteCapacityController) -> None:
        self.hass = hass
        self.site = site
        self.log = ShadowLog()
        self._store: Store[dict[str, Any]] = Store(hass, STORE_VERSION, store_key(site.entry_id))
        self.blocks: list[BlockResult] = []
        self.effect = Effect()
        #: Per local day (ISO): `plan` {hour start ISO: [action, reason]} as planned when the hour began, `review`.
        self.days: dict[str, dict[str, Any]] = {}
        self.relay_id: str | None = None
        #: The relay's states `(since, closed)` from the recorder and as seen since.
        self.relay_states: list[tuple[datetime, bool]] = []
        self.automations: list[SteeringAutomation] = []
        self.plan: list[BandHour] = []
        self.guard: str | None = None
        self.tight: frozenset[datetime] = frozenset()
        self.made_at: datetime | None = None
        self.status = "idle"
        self._given_up: set[datetime] = set()
        self._plan_key: Any = None
        self._for_hour: datetime | None = None
        self._running = False
        self._closed = False
        self._loaded = False
        self._task: asyncio.Task[None] | None = None
        self._timer_cancel: Callable[[], None] | None = None
        self._delta_key: Any = None
        self._delta: dict[datetime, float] = {}
        #: SpotNav's own switching of the relay (`heat/control.py`), set by the site.
        self.control: HeatControl | None = None

    # ---- life cycle

    async def async_start(self) -> None:
        try:
            raw = await self._store.async_load()
            self._restore(raw)
        except Exception:  # noqa: BLE001 - a store that cannot be read starts again
            _LOGGER.warning("SpotNav heat: the kept heat shadow could not be read; starting a new one", exc_info=True)
        self._loaded = True
        self._timer_cancel = async_track_time_interval(self.hass, self._async_tick, timedelta(seconds=TICK_S))
        self.tick(dt_util.utcnow())

    def _restore(self, raw: Any) -> None:
        if not isinstance(raw, dict):
            return
        self.relay_id = raw.get("relay") if isinstance(raw.get("relay"), str) else None
        blocks = [BlockResult.from_dict(item) for item in raw.get("blocks") or []]
        self.blocks = [block for block in blocks if block is not None][-KEEP_BLOCKS:]
        self.effect = learn(self.blocks)
        days = raw.get("days")
        if isinstance(days, dict):
            self.days = {day: value for day, value in days.items() if isinstance(day, str) and isinstance(value, dict)}

    async def async_shutdown(self) -> None:
        self._closed = True
        if self._timer_cancel is not None:
            self._timer_cancel()
            self._timer_cancel = None
        if self._task is not None and not self._task.done():
            self._task.cancel()
        if self._loaded:
            await self._store.async_save(self._data())

    @staticmethod
    async def async_remove_stored(hass: HomeAssistant, entry_id: str) -> None:
        await Store(hass, STORE_VERSION, store_key(entry_id)).async_remove()

    def _data(self) -> dict[str, Any]:
        return {"relay": self.relay_id, "blocks": [block.as_dict() for block in self.blocks], "days": self.days}

    def _save(self) -> None:
        self._store.async_delay_save(self._data, SAVE_DELAY_S)

    # ---- reads

    def config(self) -> HeatConfig:
        entry = self.hass.config_entries.async_get_entry(self.site.entry_id)
        return read_config(None if entry is None else entry.data.get(CONF_HEAT))

    @property
    def tz(self) -> str:
        return self.site.demand.tracker.tz

    def _relay_now(self) -> tuple[bool | None, datetime | None]:
        """Whether the relay is closed now and since when; `None` when it cannot be read."""
        if self.relay_id is None:
            return None, None
        state = self.hass.states.get(self.relay_id)
        if state is None or state.state in ("unavailable", "unknown"):
            return None, None
        return state.state in CLOSED, state.last_changed

    def heat_kw(self) -> Callable[[datetime], float | None]:
        """The heat per hour (kW): the house forecast's heat part, else the meter's mean over its last day."""
        forecast = self.site.house_forecast
        split = forecast.heat_split
        known = {} if split is None else split.heat.known_by_hour()
        recent = sorted(forecast.heat.by_hour.items())[-24:]
        fallback = sum(kwh for _start, kwh in recent) / len(recent) if recent else None

        def at(hour: datetime) -> float | None:
            return known.get(hour, fallback)

        return at

    def _expensive_intervals(self, entity_id: str | None) -> list[tuple[datetime, datetime]]:
        """The expensive intervals of the SpotNav price whose Expensive now sensor is `entity_id`."""
        if entity_id is None:
            return []
        from homeassistant.helpers import entity_registry as er  # noqa: PLC0415

        from ..pricing.price_points import EXPENSIVE  # noqa: PLC0415 - it imports the planner

        entry = er.async_get(self.hass).async_get(entity_id)
        price_entry = None if entry is None or entry.config_entry_id is None else self.hass.config_entries.async_get_entry(entry.config_entry_id)
        data = None if price_entry is None else getattr(price_entry, "runtime_data", None)
        device = getattr(data, "device", None)
        if device is None:
            return []
        chosen = device.ranking(EXPENSIVE).chosen
        return [(point.start, point.end) for point in device.points if point.start in chosen]

    def relay_source(self) -> tuple[str | None, SteeringAutomation | None]:
        """Who is expected to drive the relay: SpotNav's heat control while it runs, the SpotNav blueprint (with its
        automation) or someone unknown."""
        if self.relay_id is None:
            return None, None
        if self.control is not None and self.control.closings_ahead(dt_util.utcnow()) is not None:
            return SOURCE_SPOTNAV, None
        for automation in self.automations:
            if automation.rule is not None and automation.rule.relay == self.relay_id:
                return SOURCE_BLUEPRINT, automation
        return SOURCE_UNKNOWN, next((item for item in self.automations if self.relay_id in item.targets), None)

    def relay_intervals(self, now: datetime) -> tuple[list[tuple[datetime, datetime]], list[tuple[datetime, datetime]]]:
        """The relay's closed intervals: as it really was (up to now) and as expected from now on."""
        closed_now, since = self._relay_now()
        states = list(self.relay_states)
        if closed_now is not None and since is not None and (not states or states[-1][1] != closed_now):
            states.append((since, closed_now))
        past = [(start, min(end, now)) for start, end in closed_intervals(states, now) if start < now]
        if closed_now is None:
            return past, []
        source, automation = self.relay_source()
        controlled = None if self.control is None else self.control.closings_ahead(now)
        if controlled is not None:
            ahead = controlled
        elif source == SOURCE_BLUEPRINT and automation is not None and automation.rule is not None:
            rule = automation.rule
            outdoor = None
            if rule.outdoor_sensor is not None:
                state = self.hass.states.get(rule.outdoor_sensor)
                try:
                    outdoor = None if state is None else float(state.state)
                except ValueError:
                    outdoor = None
            ahead = expected_closings(
                now,
                rule,
                closed_now=closed_now,
                changed_at=since,
                expensive=self._expensive_intervals(rule.expensive_now),
                cold=too_cold(rule, outdoor),
            )
        else:
            ahead = unknown_closing(now, closed_now=closed_now, changed_at=since)
        return past, [(max(start, now), end) for start, end in ahead if end > now]

    def forecast_delta(self, now: datetime) -> dict[datetime, float]:
        """The change to the house's expected load per hour (`relay_effect.forecast_delta`), from the relay's closings
        whose rebound is not over; empty without a relay. Kept per 5 minutes."""
        if self.relay_id is None or not self._loaded:
            return {}
        controlled = None if self.control is None else self.control.closings_ahead(now)
        key = (int(now.timestamp() // 300), self._relay_now(), len(self.relay_states), self.effect, None if controlled is None else tuple(controlled))
        if key == self._delta_key:
            return self._delta
        past, ahead = self.relay_intervals(now)
        recent = [(start, end) for start, end in past if end + AFTER > now]
        self._delta = forecast_delta(merged([*recent, *ahead]), self.heat_kw(), self.effect, hour_of(now))
        self._delta_key = key
        return self._delta

    # ---- each look

    @callback
    def _async_tick(self, _now: datetime) -> None:
        try:
            self.tick(dt_util.utcnow())
        except Exception:  # noqa: BLE001 - the heat shadow must never disturb the site
            _LOGGER.debug("SpotNav heat: the look failed", exc_info=True)

    def tick(self, now: datetime) -> None:
        if self._closed or not self._loaded:
            return
        config = self.config()
        relay = relay_entity(config)
        if relay != self.relay_id:
            self.relay_id, self.relay_states, self._for_hour = relay, [], None
            self._given_up.clear()
        closed_now, since = self._relay_now()
        if closed_now is not None and since is not None and (not self.relay_states or self.relay_states[-1][1] != closed_now):
            self.relay_states.append((since, closed_now))
        watched = [entity for entity in (relay, config.control_entity_b, *(zone.entity_id for zone in config.zones)) if entity]
        self.automations = steering_automations(self.hass, watched)
        self._log_would(now, config)
        key = self._prices_key(now)
        if self._running or (hour_of(now) == self._for_hour and key == self._plan_key):
            return
        self._running = True
        self._for_hour = hour_of(now)
        self._plan_key = key
        self._task = self.hass.async_create_background_task(self._async_hourly(now), f"{DOMAIN} heat shadow {self.site.entry_id}")

    def _prices_key(self, now: datetime) -> Any:
        slots = self.site.battery_shadow.plan.buy_slots(now)
        return (len(slots), slots[-1].end if slots else None, repr(self.config()))

    def _log_would(self, now: datetime, config: HeatConfig) -> None:
        if not self.plan:
            return
        would = would_do(self.plan, now, config, time_zones.require_zone(self.tz))
        self.log.record(now, None if would is None else would.as_dict())

    async def _async_hourly(self, now: datetime) -> None:
        """Read the relay's past, measure its closings, plan, keep the hour and review the days due. Never raises."""
        try:
            await self._async_read_relay(now)
            await self._async_measure(now)
            await self._async_plan(now)
            self._keep_hour(now)
            await self._async_review(now)
            self._trim(now)
            self._save()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - tried again at the next hour
            _LOGGER.debug("SpotNav heat: the hourly shadow failed", exc_info=True)
            self.status = "failed"
        finally:
            self._running = False

    async def _async_read_relay(self, now: datetime) -> None:
        if self.relay_id is None:
            self.relay_states = []
            return
        start = now - timedelta(days=RELAY_DAYS)
        states = await self._async_states(self.relay_id, start, now)
        if states is not None:
            self.relay_states = states

    async def _async_states(self, entity_id: str, start: datetime, end: datetime) -> list[tuple[datetime, bool]] | None:
        """The entity's states in `[start, end]` with the one in force at `start` (closed or not), from the recorder;
        `None` when it cannot be read."""
        try:
            from homeassistant.components.recorder import get_instance, history  # noqa: PLC0415 - only when loaded
        except ImportError:  # pragma: no cover
            return None

        def read() -> dict[str, list[Any]]:
            return history.get_significant_states(
                self.hass, start, end, [entity_id], significant_changes_only=False, include_start_time_state=True
            )

        try:
            rows = await get_instance(self.hass).async_add_executor_job(read)
        except Exception:  # noqa: BLE001 - no recorder: the states seen live only
            _LOGGER.debug("SpotNav heat: the relay's history could not be read", exc_info=True)
            return None
        out: list[tuple[datetime, bool]] = []
        for state in rows.get(entity_id, ()):
            changed = getattr(state, "last_changed", None)
            if isinstance(changed, datetime):
                out.append((max(changed, start), getattr(state, "state", None) in CLOSED))
        return out

    async def _async_measure(self, now: datetime) -> None:
        """Measure the relay's closings whose rebound window has passed and which are not measured yet."""
        meter = self.site.house_forecast.heat.meter
        if self.relay_id is None or not meter.found:
            return
        from ..site.heat_meter import async_heat_hours  # noqa: PLC0415 - the site imports this module

        intervals = closed_intervals(self.relay_states, now)
        known = {block.start for block in self.blocks}
        due = [
            (index, interval)
            for index, interval in enumerate(intervals)
            if interval[1] + AFTER <= now and interval[0] not in known and interval[0] not in self._given_up
        ]
        if not due:
            return
        first = min(interval[0] for _index, interval in due) - BEFORE - timedelta(hours=24)
        five = await async_heat_hours(self.hass, meter, first, now, "5minute")
        periods = {start: kwh * 12.0 for start, kwh in five.items()}
        found = levels(periods)
        runs = [] if found is None else [(run.start, run.end) for run in hot_water_runs(periods, found)]
        rows = getattr(self.site.house_forecast.history, "rows", {}) or {}
        outdoor = {start: values[1] for start, values in rows.items() if values[1] is not None}
        changed = False
        for index, interval in due:
            following = intervals[index + 1][0] if index + 1 < len(intervals) else None
            result = measure_block(interval, periods, runs, next_start=following, outdoor=outdoor)
            if result is not None:
                self.blocks.append(result)
                changed = True
                _LOGGER.debug("SpotNav heat: a lowered block measured: %s", result.as_dict())
            elif interval[1] + AFTER + GIVE_UP <= now:
                self._given_up.add(interval[0])
        if changed:
            self.blocks = sorted(self.blocks, key=lambda block: block.start)[-KEEP_BLOCKS:]
            self.effect = learn(self.blocks)

    def coldest_now(self, config: HeatConfig) -> float | None:
        """The coldest counted room now less its offset (what the comfort band is compared with), `None` without a
        readable indoor sensor (a frozen one does not read)."""
        readings = []
        for entity_id in config.indoor_sensors:
            state = fresh_state(self.hass, entity_id)
            try:
                value = None if state is None else float(state.state)
            except ValueError:
                value = None
            readings.append(SensorReading(entity_id, entity_id, value, (), None, config.offset_of(entity_id)))
        return indoor(readings, dt_util.utcnow(), frozenset()).relative_c if readings else None

    def joint_inputs(self, now: datetime) -> tuple[frozenset[datetime], dict[datetime, float]]:
        """The joint plan's tight hours and the outdoor forecast (see the module)."""
        from ..battery.plan_runtime import site_car_windows, site_import_cap_kw  # noqa: PLC0415 - it imports the site
        from ..battery.plan_shadow import car_kw_in  # noqa: PLC0415

        forecast = self.site.house_forecast
        try:
            windows = site_car_windows(self.hass, self.site)
        except Exception:  # noqa: BLE001 - no car plan: none counted
            windows = []
        plan = getattr(self.site.battery_shadow, "plan", None)
        slots = getattr(plan, "plan", ()) or ()
        tight = tight_hours(
            site_import_cap_kw(self.hass, self.site),
            forecast.plan_kw_by_hour(now, heat=False),
            lambda hour: car_kw_in(hour, hour + HOUR, windows),
            battery_charge_by_hour(slots),
            self.heat_kw(),
            self.effect,
        )
        return tight, forecast.temperatures()

    async def _async_plan(self, now: datetime) -> None:
        from .view import hour_prices_of  # noqa: PLC0415 - kept beside its use

        config = self.config()
        zone = time_zones.require_zone(self.tz)
        slots = self.site.battery_shadow.plan.buy_slots(now)
        prices = hour_prices_of(tuple((slot.start, slot.end, slot.buy) for slot in slots))
        today = day_hours(now, zone)
        tomorrow = day_hours(today[-1] + HOUR, zone)
        days = [today] + ([tomorrow] if any(hour in prices for hour in tomorrow) else [])
        tight, outdoor_by_hour = self.joint_inputs(now)
        outdoor = self.site.house_forecast.outdoor_c()
        if config.outdoor_sensor is not None:
            state = self.hass.states.get(config.outdoor_sensor)
            try:
                outdoor = None if state is None else float(state.state)
            except ValueError:
                outdoor = None
        planned, guard = await self.hass.async_add_executor_job(
            partial(
                plan_days,
                config,
                days,
                prices,
                now,
                zone,
                outdoor_c=outdoor,
                coldest_c=self.coldest_now(config),
                tight=tight,
                outdoor_by_hour=outdoor_by_hour,
            )
        )
        if self._closed:
            return
        self.plan, self.guard, self.tight, self.made_at = planned, guard, tight, now
        self.status = "planned"
        self._log_would(now, config)

    def _keep_hour(self, now: datetime) -> None:
        """Keep the running hour's planned action for the day, once (as planned when the hour began)."""
        current = hour_of(now)
        hour = next((item for item in self.plan if item.start == current), None)
        if hour is None:
            return
        day = current.astimezone(time_zones.require_zone(self.tz)).date().isoformat()
        plan = self.days.setdefault(day, {"plan": {}, "review": None}).setdefault("plan", {})
        plan.setdefault(current.isoformat(), [hour.action, hour.reason])

    def kept_actions(self, day: date) -> dict[datetime, tuple[str, str]]:
        """A day's kept hours: `{hour start: (action, reason)}`."""
        out: dict[datetime, tuple[str, str]] = {}
        for key, value in (self.days.get(day.isoformat(), {}).get("plan") or {}).items():
            try:
                out[datetime.fromisoformat(key)] = (str(value[0]), str(value[1]))
            except (TypeError, ValueError, IndexError):
                continue
        return out

    def review_of(self, day: date) -> dict[str, Any] | None:
        review = self.days.get(day.isoformat(), {}).get("review")
        return review if isinstance(review, dict) else None

    async def _async_review(self, now: datetime) -> None:
        """Review the kept days that are due (see the module)."""
        from ..api.house import short_term_days  # noqa: PLC0415 - the API imports the site
        from ..api.house_history import period_price_slots  # noqa: PLC0415
        from ..site.heat_meter import async_heat_hours  # noqa: PLC0415
        from ..site.history_period import PERIOD_DAY, midnight_utc, period_of  # noqa: PLC0415
        from .history import hour_prices  # noqa: PLC0415

        keep = short_term_days(self.hass)
        zone = time_zones.require_zone(self.tz)
        config = self.config()
        meter = self.site.house_forecast.heat.meter
        for key in sorted(self.days):
            record = self.days[key]
            if record.get("review") is not None or not record.get("plan"):
                continue
            day = date.fromisoformat(key)
            start = midnight_utc(day, self.tz)
            end = midnight_utc(day + timedelta(days=1), self.tz)
            if now < end + AFTER or keep is None or now - start > timedelta(days=keep):
                continue
            heat = await async_heat_hours(self.hass, meter, start, end) if meter.found else {}
            states = await self._async_states(self.relay_id, start, end) if self.relay_id else []
            relay = [] if not states else [(a, b) for a, b in closed_intervals(states, end) if a >= start]
            coldest = await self._async_coldest(config, start, end + AFTER)
            prices = hour_prices(
                [(slot.start, slot.end, slot.buy) for slot in period_price_slots(self.hass, self.site.entry_id, period_of(PERIOD_DAY, day))]
            )
            kept = self.kept_actions(day)
            record["review"] = review_day(
                day,
                day_hours(start, zone),
                prices=prices,
                heat_kwh=heat,
                relay=relay,
                plan_lowered=[hour for hour, (action, _reason) in kept.items() if action == ACTION_LOWER],
                measured=[block for block in self.blocks if start <= block.start < end],
                effect=self.effect,
                coldest=coldest,
                low_c=config.target_c - BAND_C,
            )
            _LOGGER.info("SpotNav heat (shadow): %s reviewed: %s", key, record["review"])

    async def _async_coldest(self, config: HeatConfig, start: datetime, end: datetime) -> dict[datetime, float]:
        if not config.indoor_sensors:
            return {}
        try:
            from homeassistant.components.recorder import get_instance  # noqa: PLC0415 - only when loaded
            from homeassistant.components.recorder.statistics import statistics_during_period  # noqa: PLC0415
        except ImportError:  # pragma: no cover
            return {}
        ids = set(config.indoor_sensors)

        def read() -> dict[str, list[dict[str, Any]]]:
            return dict(statistics_during_period(self.hass, start, end, ids, "5minute", {"temperature": "°C"}, {"mean"}))

        try:
            rows = await get_instance(self.hass).async_add_executor_job(read)
        except Exception:  # noqa: BLE001 - no rooms: the review has no comfort
            _LOGGER.debug("SpotNav heat: the rooms' 5-minute rows could not be read", exc_info=True)
            return {}
        return coldest_by_period(rows, {entity_id: config.offset_of(entity_id) for entity_id in ids})

    def _trim(self, now: datetime) -> None:
        oldest = (now - timedelta(days=DAYS_KEPT)).date().isoformat()
        for key in [key for key in self.days if key < oldest]:
            del self.days[key]

    # ---- what is shown

    def relay_minutes(self, hours: Sequence[datetime], now: datetime) -> dict[datetime, tuple[int, bool]]:
        """Per hour: the minutes the relay was (past) or is expected to be (ahead) closed, and whether expected."""
        past, ahead = self.relay_intervals(now)
        out: dict[datetime, tuple[int, bool]] = {}
        for hour in hours:
            done = sum((min(end, hour + HOUR) - max(start, hour)).total_seconds() for start, end in past if start < hour + HOUR and end > hour)
            coming = sum((min(end, hour + HOUR) - max(start, hour)).total_seconds() for start, end in ahead if start < hour + HOUR and end > hour)
            out[hour] = (round((done + coming) / 60), coming > 0)
        return out

    def diagnostics(self) -> dict[str, Any]:
        now = dt_util.utcnow()
        source, automation = self.relay_source()
        past, ahead = self.relay_intervals(now) if self.relay_id else ([], [])
        return {
            "status": self.status,
            "relay": self.relay_id,
            "relay_source": source,
            "relay_automation": None if automation is None else automation.as_dict(),
            "closed": [[start.isoformat(), end.isoformat()] for start, end in past[-8:]],
            "expected": [[start.isoformat(), end.isoformat()] for start, end in ahead],
            "effect": self.effect.as_dict(),
            "blocks": [block.as_dict() for block in self.blocks[-10:]],
            "forecast_delta": {start.isoformat(): round(kw, 3) for start, kw in sorted(self.forecast_delta(now).items())},
            "automations": [item.as_dict() for item in self.automations],
            "made_at": None if self.made_at is None else self.made_at.isoformat(),
            "guard": self.guard,
            "tight": sorted(hour.isoformat() for hour in self.tight),
            "plan": [hour.as_dict() for hour in self.plan],
            "days": self.days,
            "log": self.log.as_list(),
        }
