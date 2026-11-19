"""The site's hot water (plan `plans/v3_dhw_plan.md`): the peaks learned, the boosts planned in shadow, each day
reviewed against what the pump did, the gate to live, the live boosts behind the opt-in, and the Boost button. One
per site, owned by its heat shadow (`heat/runtime.py`), kept in the heat shadow's store under `hot_water`.

* **Each hour** (with the heat shadow's plan): the heat pump meter's 5-minute rows of the last day are split into
  hot-water runs (`meter_split`), merged into the runs kept (`dhw_peaks.PEAK_DAYS`); the peaks are learned on them
  (`dhw_peaks.learn_peaks`, SpotNav's and the button's runs left out, the last peaks held on days with a planned
  boost); the boosts are planned (`dhw_plan.plan`) on the prices, the joint load per quarter (the house forecast
  with the heat's change, the cars' installed plans, the battery plan's grid charge) against the import cap, the
  battery plan's export as sun, the heat plan's lowered hours on a path that blocks hot water and its raise hours as
  blocked, the outdoor forecast at the heat's cold limit and the heat control's safety reason as unsafe. Only with
  a boost route (`dhw_boost.boost_route`): without one nothing is planned.
* **The day kept**: the day's peaks and each peak's pick as it stood when its run began; reviewed once the day is
  over plus `REVIEW_AFTER` (`dhw_review.review_day`) on the runs kept and the day's prices.
* **Mode**: `watch` while the pump's own price control or another optimiser steers it (nothing planned is
  written), `live` with the heat opt-in ("Styr värmen"), a boost route, the gate open (`dhw_review.gate`, level B)
  and no back-off, else `shadow`. A live day whose reviews reach the misses' limit closes the gate again: its shadow
  days count from the next day.
* **Live** (each look): a pick whose start has come (within `START_LATE`) is written once through the route, unless
  the heat control's safety reason is `too_cold`, `room_cold` or `stale`, the meter has not reported within
  `DATA_FRESH`, the day's budget is used, or the day ended. A write not read back ends planned hot water for the day;
  a local route is tried once more after `RETRY` first, a cloud's never. SpotNav's own run is never stopped by it.
* **The Boost button** (`async_press`): a person's own start or stop through the same route, outside the plan's
  budget (a press on a cloud route leaves the day at most one planned action), one start per `dhw_boost.RATE`,
  read back; a stop only of the household's own boost.

Nothing here is written to the pump but the live boosts (behind the opt-in and the gate) and the button's.
"""

from __future__ import annotations

import asyncio
import logging
import statistics
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Final

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .. import time_zones
from .config import HeatConfig
from .dhw_boost import (
    PULSE,
    RATE,
    READ_BACK,
    READ_BACK_CLOUD,
    BoostReading,
    Route,
    boost_route,
    fits_fuse,
    over_fuse,
    boost_state,
    start_call,
    stop_call,
)
from .dhw_ledger import SOURCE_BUTTON, SOURCE_PLAN, Entry, Ledger
from .dhw_peaks import PEAK_DAYS, Peak, draws_from_runs, learn_peaks, peak_instant, standby, standby_kwh
from .dhw_plan import (
    BUDGETS,
    DEFAULT_BOOST_KW,
    DEFAULT_RUN,
    QUARTER,
    ROUTE_CLOUD,
    ROUTE_ONE_SHOT,
    Pick,
    PlanInputs,
    hourly_to_quarters,
    min_gain,
    plan,
    quarter_of,
    quarters_of,
)
from .dhw_review import LEVEL_A, LEVEL_B, MODE_LIVE, MODE_SHADOW, DayPeak, back_off, gate, review_day
from .meter_split import STATE_HOT_WATER, HotWaterRun, hot_water_runs, levels, state_now

if TYPE_CHECKING:
    from .runtime import HeatShadow

_LOGGER = logging.getLogger(__name__)

MODE_WATCH: Final = "watch"
REVIEW_AFTER: Final = timedelta(hours=3)
DAYS_KEPT: Final = 35
START_LATE: Final = timedelta(minutes=10)
RETRY: Final = timedelta(minutes=5)
DATA_FRESH: Final = timedelta(minutes=10)
POLL_S: Final = 5.0
#: The heat control's safety reasons under which no planned boost starts (the compressor goes to the house).
UNSAFE: Final = frozenset({"too_cold", "room_cold", "stale"})
LOG_SIZE: Final = 48
#: Looks in a row over the fuse before SpotNav's own running boost is shed.
SHED_LOOKS: Final = 2


def beta_gate_open(hass: HomeAssistant, site: Any) -> bool:
    """Whether the site's beta testing skips the learning gate (`dhw_review.GATES`).

    MERGE: the single call site for the beta switch. Once v3-beta is merged, return its
    `learning_gate_open(hass, site)` here (or its beta flag accessor); until then no site is a beta site."""
    del hass, site
    return False


async def _pause(seconds: float) -> None:
    await asyncio.sleep(seconds)


def _run_of(raw: Any) -> HotWaterRun | None:
    try:
        return HotWaterRun(datetime.fromisoformat(raw["start"]), datetime.fromisoformat(raw["end"]), float(raw["kwh"]))
    except (KeyError, TypeError, ValueError):
        return None


def learned_run(runs: Sequence[HotWaterRun]) -> tuple[timedelta, float]:
    """The run's length (rounded up to the quarter, at least one) and the boost's power (kW) from the runs that are
    no standby loss; the defaults without any."""
    cutoff = standby_kwh(runs)
    real = [run for run in runs if not standby(run, cutoff) and run.end > run.start]
    if not real:
        return DEFAULT_RUN, DEFAULT_BOOST_KW
    minutes = statistics.median((run.end - run.start).total_seconds() / 60 for run in real)
    quarters = max(1, -(-int(minutes) // 15))
    kw = statistics.median(run.kwh / ((run.end - run.start).total_seconds() / 3600) for run in real)
    return QUARTER * quarters, kw


class HotWater:
    """The site's hot water (see the module). Never raises into the heat shadow."""

    def __init__(self, hass: HomeAssistant, shadow: HeatShadow) -> None:
        self.hass = hass
        self.shadow = shadow
        self.ledger = Ledger()
        self.runs: dict[str, HotWaterRun] = {}
        #: Local days the meter had rows for (the peaks' warm start).
        self.seen: set[str] = set()
        self.peaks: list[Peak] = learn_peaks([], timezone.utc, 0)
        self.picks: list[Pick] = []
        #: Per local day: `peaks` [[ready, until]], `picks` {peak: pick}, `live` [peak] (a planned boost written),
        #: `filled` [peak] (a boost already running when the pick came due), `review`.
        self.days: dict[str, dict[str, Any]] = {}
        #: The gate counts shadow days after this day (it closed then).
        self.gate_since: str | None = None
        self.backed_off: str | None = None
        self.ended_day: str | None = None
        #: The site's standby cutoff (kWh, `dhw_peaks.standby_kwh` on the runs kept).
        self.standby: float = standby_kwh(())
        self.run_len: timedelta = DEFAULT_RUN
        self.boost_kw: float = DEFAULT_BOOST_KW
        self.made_at: datetime | None = None
        self.log: list[dict[str, Any]] = []
        self._pending: asyncio.Task[None] | None = None
        self._failed_at: datetime | None = None
        self._retry_at: dict[str, datetime] = {}
        self._over_looks = 0
        #: The local day SpotNav shed its own boost for the fuse (not restarted that day).
        self.shed_day: str | None = None
        self._delta_key: Any = None
        self._delta: dict[datetime, float] = {}

    # ---- store

    def stored(self) -> dict[str, Any]:
        return {
            "ledger": self.ledger.stored(),
            "runs": [run.as_dict() for run in sorted(self.runs.values(), key=lambda run: run.start)],
            "seen": sorted(self.seen),
            "peaks": [peak.stored() for peak in self.peaks],
            "days": self.days,
            "gate_since": self.gate_since,
            "backed_off": self.backed_off,
            "ended_day": self.ended_day,
        }

    def restore(self, raw: Any) -> None:
        if not isinstance(raw, dict):
            return
        self.ledger = Ledger.from_stored(raw.get("ledger"))
        runs = [run for item in raw.get("runs") or [] if (run := _run_of(item)) is not None]
        self.runs = {run.start.isoformat(): run for run in runs}
        self.seen = {day for day in raw.get("seen") or [] if isinstance(day, str)}
        peaks = [peak for item in raw.get("peaks") or [] if (peak := Peak.from_stored(item)) is not None]
        if peaks:
            self.peaks = peaks
        days = raw.get("days")
        if isinstance(days, dict):
            self.days = {key: value for key, value in days.items() if isinstance(key, str) and isinstance(value, dict)}
        for name in ("gate_since", "backed_off", "ended_day"):
            value = raw.get(name)
            setattr(self, name, value if isinstance(value, str) else None)
        self.run_len, self.boost_kw = learned_run(list(self.runs.values()))
        self.standby = standby_kwh(self.runs.values())

    # ---- reads

    @property
    def zone(self) -> Any:
        return time_zones.require_zone(self.shadow.tz)

    def _pump(self) -> Any:
        from .detect import read_heat_pumps  # noqa: PLC0415 - read only when it matters

        pumps = read_heat_pumps(self.hass)
        return pumps[0] if pumps else None

    def route(self, config: HeatConfig | None = None, pump: Any = None) -> Route | None:
        config = self.shadow.config() if config is None else config
        return boost_route(self._pump() if pump is None else pump, config.hot_water_entity)

    def level(self, config: HeatConfig, route: Route | None) -> str | None:
        from .paths import blocks_hot_water  # noqa: PLC0415

        if route is not None:
            return LEVEL_B
        return LEVEL_A if blocks_hot_water(config) and config.control_entity else None

    def watched(self, pump: Any) -> bool:
        """The pump's own price control or another optimiser steers it: nothing planned is written."""
        from .detect import other_controllers  # noqa: PLC0415

        return (pump is not None and pump.price_control.state == "active") or bool(other_controllers(self.hass))

    def gate_reviews(self) -> list[dict[str, Any]]:
        out = []
        for key in sorted(self.days):
            review = self.days[key].get("review")
            if isinstance(review, dict) and (self.gate_since is None or key > self.gate_since):
                out.append(review)
        return out

    def gate_state(self) -> dict[str, Any]:
        state = gate(self.gate_reviews(), LEVEL_B)
        if not state["open"] and beta_gate_open(self.hass, self.shadow.site):
            # A beta tester skips the shadow gate: the figures stay as they are, the gate opens.
            state["open"] = True
        return {**state, "backed_off": self.backed_off}

    def mode(self, config: HeatConfig, route: Route | None, pump: Any) -> str:
        if self.watched(pump):
            return MODE_WATCH
        if config.control_enabled and route is not None and self.gate_state()["open"]:
            return MODE_LIVE
        return MODE_SHADOW

    def making_now(self) -> bool:
        """Whether the pump makes hot water now: its own priority, else the meter's power against its levels."""
        pump = self._pump()
        if pump is not None and pump.priority is not None:
            return pump.priority == "hot_water"
        control = self.shadow.control
        levels_ = None if control is None else getattr(control, "_levels", None)
        meter = self.shadow.site.house_forecast.heat.meter
        if levels_ is None or not meter.found or meter.entity_id is None:
            return False
        state = self.hass.states.get(meter.entity_id)
        try:
            kw = None if state is None else float(state.state)
        except ValueError:
            kw = None
        if kw is None or state is None:
            return False
        unit = state.attributes.get("unit_of_measurement")
        kw = kw / 1000.0 if unit == "W" else kw
        return state_now(kw, levels_, kw) == STATE_HOT_WATER

    def _meter_fresh(self, now: datetime) -> bool:
        meter = self.shadow.site.house_forecast.heat.meter
        if not meter.found or meter.entity_id is None:
            return False
        state = self.hass.states.get(meter.entity_id)
        if state is None or state.state in ("unavailable", "unknown"):
            return False
        reported = getattr(state, "last_reported", None) or state.last_updated
        return now - reported <= DATA_FRESH

    # ---- each hour

    async def async_hourly(self, now: datetime) -> None:
        """Read the runs, learn the peaks, plan, keep the day and review the days due."""
        await self._async_read_runs(now)
        self._learn(now)
        self._plan(now)
        self._keep(now)
        await self._async_review(now)
        self._trim(now)

    async def _async_read_runs(self, now: datetime) -> None:
        meter = self.shadow.site.house_forecast.heat.meter
        if not meter.found:
            return
        from ..site.heat_meter import async_heat_hours  # noqa: PLC0415 - the site imports this module

        rows = await async_heat_hours(self.hass, meter, now - timedelta(hours=26), now, "5minute")
        periods = {start: kwh * 12.0 for start, kwh in rows.items()}
        found = levels(periods)
        for start in periods:
            self.seen.add(start.astimezone(self.zone).date().isoformat())
        if found is None:
            return
        for run in hot_water_runs(periods, found):
            # A run still going at the window's end is taken again, whole, next hour.
            if run.end <= now - timedelta(minutes=5):
                self.runs[run.start.isoformat()] = run

    def _learn(self, now: datetime) -> None:
        first = now - timedelta(days=PEAK_DAYS)
        window = [run for run in self.runs.values() if run.start >= first]
        own = self.ledger.starts()
        days = sorted(day for day in self.seen if day >= first.astimezone(self.zone).date().isoformat())
        hold = any(entry.source == SOURCE_PLAN and entry.at >= first for entry in self.ledger.entries)
        self.standby = standby_kwh(window)
        self.peaks = learn_peaks(draws_from_runs(window, own, self.standby), self.zone, len(days), previous=self.peaks, hold=hold)
        self.run_len, self.boost_kw = learned_run(window)

    def inputs(self, now: datetime, config: HeatConfig, route: Route) -> PlanInputs:
        """The planner's inputs (see the module)."""
        from ..battery.plan_runtime import site_car_windows, site_import_cap_kw  # noqa: PLC0415 - it imports the site
        from ..battery.plan_shadow import car_kw_in  # noqa: PLC0415
        from .control import raise_entity  # noqa: PLC0415
        from .paths import blocks_hot_water  # noqa: PLC0415
        from .relay_control import NO_LOWER_AT_C  # noqa: PLC0415
        from .runtime import battery_charge_by_hour  # noqa: PLC0415
        from .shadow_rule import ACTION_LOWER, ACTION_PREHEAT  # noqa: PLC0415

        site = self.shadow.site
        slots = site.battery_shadow.plan.buy_slots(now)
        buy = quarters_of([(slot.start, slot.end, slot.buy) for slot in slots])
        sell = quarters_of([(slot.start, slot.end, slot.sell) for slot in slots])
        forecast = site.house_forecast
        # The house with the heat's change, less the boosts' own (they are placed here again, not counted twice).
        own = self.forecast_delta(now)
        load = hourly_to_quarters(
            {hour: max(0.0, kw - own.get(hour, 0.0)) for hour, kw in forecast.plan_kw_by_hour(now, heat=True).items()}
        )
        try:
            windows = site_car_windows(self.hass, site)
        except Exception:  # noqa: BLE001 - no car plan: none counted
            windows = []
        battery_plan = getattr(site.battery_shadow, "plan", None)
        planned = getattr(battery_plan, "plan", ()) or ()
        charge = hourly_to_quarters(battery_charge_by_hour(planned))
        surplus: dict[datetime, float] = {}
        for slot in planned:
            grid = float(getattr(slot, "grid_kw", 0.0) or 0.0)
            if grid < 0:
                moment = quarter_of(slot.start)
                while moment < slot.end:
                    surplus[moment] = -grid
                    moment += QUARTER
        for q in set(load) | set(charge) | {q for q in buy if q >= quarter_of(now)}:
            load[q] = load.get(q, 0.0) + charge.get(q, 0.0) + car_kw_in(q, q + QUARTER, windows)
        blocked: set[datetime] = set()
        for hour in self.shadow.plan:
            if (hour.action == ACTION_LOWER and blocks_hot_water(config)) or (
                hour.action == ACTION_PREHEAT and raise_entity(config) is not None
            ):
                blocked.update(hour.start + QUARTER * index for index in range(4))
        unsafe = {
            hour + QUARTER * index
            for hour, value in forecast.temperatures().items()
            if value is not None and value <= NO_LOWER_AT_C
            for index in range(4)
        }
        control = self.shadow.control
        if control is not None and control.safety_now(config, now) in UNSAFE:
            start = quarter_of(now)
            unsafe.update(start + QUARTER * index for index in range(4))
        budget = BUDGETS.get(route.kind, BUDGETS[ROUTE_ONE_SHOT])
        used: dict[date, int] = {}
        today = now.astimezone(self.zone).date()
        for day in (today, today + timedelta(days=1)):
            count = self.ledger.used_on(day, self.zone, SOURCE_PLAN)
            if route.kind == ROUTE_CLOUD and self.ledger.used_on(day, self.zone, SOURCE_BUTTON):
                count = max(count, budget - 1)
            if self.ended_day == day.isoformat():
                count = budget
            used[day] = count
        return PlanInputs(
            now=now,
            zone=self.zone,
            peaks=self.peaks,
            buy=buy,
            sell=sell,
            surplus_kw=surplus,
            load_kw=load,
            cap_kw=site_import_cap_kw(self.hass, site),
            blocked=frozenset(blocked),
            unsafe=frozenset(unsafe),
            budget=budget,
            used=used,
            run_len=self.run_len,
            boost_kw=self.boost_kw,
            previous=self.picks,
            min_gain=min_gain(self._currency()),
        )

    def _currency(self) -> str | None:
        from ..pricing.price_source import site_price_source  # noqa: PLC0415 - it imports the planner

        source = site_price_source(self.hass, self.shadow.site.entry_id)
        area = None if source is None else source.area
        return None if area is None else area.currency

    def _plan(self, now: datetime) -> None:
        config = self.shadow.config()
        route = self.route(config)
        if route is None:
            self.picks = []
            return
        before = {pick.peak: pick.start for pick in self.picks}
        self.picks = plan(self.inputs(now, config, route))
        self.made_at = now
        after = {pick.peak: pick.start for pick in self.picks}
        if after != before:
            entry = {"at": now.isoformat(), "picks": [pick.start.isoformat() for pick in self.picks]}
            self.log = [*self.log, entry][-LOG_SIZE:]
            _LOGGER.debug("SpotNav hot water (shadow): would boost at %s", entry["picks"])

    def _record(self, day: str) -> dict[str, Any]:
        return self.days.setdefault(day, {"peaks": [], "picks": {}, "live": [], "review": None})

    def _keep(self, now: datetime) -> None:
        """Keep each day's peaks and each peak's pick as it stood when its run began."""
        zone = self.zone
        today = now.astimezone(zone).date()
        for day in (today, today + timedelta(days=1)):
            record = self._record(day.isoformat())
            if record.get("review") is None:
                record["peaks"] = [
                    [instant[0].astimezone(timezone.utc).isoformat(), instant[1].astimezone(timezone.utc).isoformat()]
                    for peak in self.peaks
                    for instant in (peak_instant(peak, day, zone),)
                ]
        for pick in self.picks:
            record = self._record(pick.peak.astimezone(zone).date().isoformat())
            picks = record.setdefault("picks", {})
            kept = Pick.from_stored(picks.get(pick.peak.isoformat()))
            if kept is None or kept.start > now:
                picks[pick.peak.isoformat()] = pick.stored()
        # A pick no longer planned whose run has not begun is dropped from its day.
        planned = {pick.peak.isoformat() for pick in self.picks}
        for record in self.days.values():
            picks = record.get("picks") or {}
            for key in [key for key in picks if key not in planned]:
                kept = Pick.from_stored(picks[key])
                if kept is None or (kept.start > now and kept.peak > now):
                    del picks[key]

    def day_picks(self, day: str) -> list[Pick]:
        return [pick for raw in (self.days.get(day, {}).get("picks") or {}).values() if (pick := Pick.from_stored(raw)) is not None]

    def pump_runs(self, start: datetime, end: datetime) -> list[HotWaterRun]:
        """The pump's own runs in `[start, end)`: SpotNav's and the button's left out."""
        return [
            run
            for run in sorted(self.runs.values(), key=lambda run: run.start)
            if start <= run.start < end and self.ledger.run_source(run.start) is None
        ]

    async def _async_review(self, now: datetime) -> None:
        from ..api.house_history import period_price_slots  # noqa: PLC0415 - the API imports the site
        from ..site.history_period import PERIOD_DAY, midnight_utc, period_of  # noqa: PLC0415

        tz = self.shadow.tz
        for key in sorted(self.days):
            record = self.days[key]
            if record.get("review") is not None or not record.get("peaks"):
                continue
            day = date.fromisoformat(key)
            start = midnight_utc(day, tz)
            end = midnight_utc(day + timedelta(days=1), tz)
            if now < end + REVIEW_AFTER:
                continue
            if key not in self.seen:
                # No meter rows that day: nothing to compare.
                record["review"] = None
                continue
            slots = period_price_slots(self.hass, self.shadow.site.entry_id, period_of(PERIOD_DAY, day))
            prices = quarters_of([(slot.start, slot.end, slot.buy) for slot in slots])
            peaks = []
            for item in record.get("peaks") or []:
                try:
                    peaks.append(DayPeak(datetime.fromisoformat(item[0]), datetime.fromisoformat(item[1])))
                except (TypeError, ValueError, IndexError):
                    continue
            live = frozenset(datetime.fromisoformat(item) for item in record.get("live") or [])
            record["review"] = review_day(
                day,
                self.zone,
                peaks,
                self.day_picks(key),
                self.pump_runs(start, end + timedelta(hours=3)),
                prices.get,
                mode=MODE_LIVE if live else MODE_SHADOW,
                live_peaks=live,
                cutoff=self.standby,
            )
            _LOGGER.info("SpotNav hot water: %s reviewed: %s", key, {k: v for k, v in record["review"].items() if k != "per_peak"})
        today = now.astimezone(self.zone).date()
        if back_off([r for d in self.days.values() if isinstance(r := d.get("review"), dict)], today) and self.gate_state()["open"]:
            self.gate_since = today.isoformat()
            self.backed_off = today.isoformat()
            _LOGGER.info("SpotNav hot water: too many misses in a week; back to shadow")

    def _trim(self, now: datetime) -> None:
        oldest = (now - timedelta(days=DAYS_KEPT)).astimezone(self.zone).date().isoformat()
        for key in [key for key in self.days if key < oldest]:
            del self.days[key]
        first = now - timedelta(days=PEAK_DAYS + 1)
        self.runs = {key: run for key, run in self.runs.items() if run.start >= first}
        self.seen = {day for day in self.seen if day >= first.astimezone(self.zone).date().isoformat()}
        self.ledger.trim(now)

    # ---- each look: live boosts

    def tick(self, now: datetime) -> None:
        """Shed SpotNav's own running boost under fuse pressure; start a live pick whose time has come (see the
        module)."""
        if self._pending is not None and not self._pending.done():
            return
        config = self.shadow.config()
        self._shed_check(now, config)
        if not config.control_enabled or not self.picks:
            # Nothing planned is written without the opt-in: no need to read the pump each look.
            return
        pump = self._pump()
        route = self.route(config, pump)
        if route is None or self.mode(config, route, pump) != MODE_LIVE:
            return
        today = now.astimezone(self.zone).date()
        if self.ended_day == today.isoformat():
            return
        for pick in self.picks:
            if not pick.start <= now < pick.start + START_LATE:
                continue
            record = self._record(pick.peak.astimezone(self.zone).date().isoformat())
            key = pick.peak.isoformat()
            if key in (record.get("live") or []) or key in (record.get("filled") or []):
                continue
            if self._on(route)[0] or self.making_now():
                # A boost already runs (the household's, the button's, the pump's own run): it fills the peak.
                # SpotNav never writes over it, never takes it as its own and spends nothing on it.
                record.setdefault("filled", []).append(key)
                _LOGGER.debug("SpotNav hot water: the peak at %s is filled by a boost already running", pick.peak)
                continue
            retry = self._retry_at.get(pick.peak.isoformat())
            if retry is not None and now < retry:
                continue
            reason = self._live_blocker(now, route)
            if reason is not None:
                _LOGGER.debug("SpotNav hot water: the planned boost at %s waits: %s", pick.start, reason)
                continue
            self._pending = self.hass.async_create_background_task(
                self._async_live_start(now, route, pick), f"spotnav hot water boost {self.shadow.site.entry_id}"
            )
            return

    def _shed_check(self, now: datetime, config: HeatConfig) -> None:
        """SpotNav's own planned boost running while a phase is over the fuse for `SHED_LOOKS` looks: ended, and not
        started again that day. A household's or the button's boost is never shed."""
        last = self.ledger.last(SOURCE_PLAN)
        if last is None or last.read_back is not True or now - last.at > timedelta(hours=2):
            self._over_looks = 0
            return
        if not over_fuse(self._margins()):
            self._over_looks = 0
            return
        route = self.route(config)
        if route is None or route.entity_id != last.entity_id:
            return
        on, since = self._on(route)
        if not on or self.ledger.source_of_on(route.entity_id, since) != SOURCE_PLAN:
            return
        self._over_looks += 1
        if self._over_looks < SHED_LOOKS:
            return
        call = stop_call(route, self._options(route))
        if call is None:
            return
        self._over_looks = 0
        self.shed_day = now.astimezone(self.zone).date().isoformat()
        _LOGGER.info("SpotNav hot water: its own boost ended for the fuse; not started again today")
        self._pending = self.hass.async_create_background_task(
            self.hass.services.async_call(call[0], call[1], call[2], blocking=True), f"spotnav hot water shed {self.shadow.site.entry_id}"
        )

    def forecast_delta(self, now: datetime) -> dict[datetime, float]:
        """The change SpotNav's own boosts bring to the house's load per hour (kW), what the cars' and the battery's
        plans count on (`HeatShadow.forecast_delta`): each boost that will be written (live) adds its power over its
        run, and the pump's own reheat it replaces is taken out at its expected time. Nothing in shadow: there the
        pump runs as it always has. Kept per 5 minutes."""
        filled_count = sum(len(record.get("filled") or []) for record in self.days.values())
        key = (int(now.timestamp() // 300), tuple(pick.start for pick in self.picks), self.shed_day, self.ended_day, filled_count)
        if key == self._delta_key:
            return self._delta
        out: dict[datetime, float] = {}
        if self.picks:
            config = self.shadow.config()
            pump = self._pump() if config.control_enabled else None
            route = None if pump is None and not config.hot_water_entity else self.route(config, pump)
            today = now.astimezone(self.zone).date().isoformat()
            if route is not None and self.mode(config, route, pump) == MODE_LIVE and today not in (self.shed_day, self.ended_day):
                from .relay_effect import hour_shares  # noqa: PLC0415

                run_h = max(self.run_len.total_seconds() / 3600.0, 0.25)
                filled = {key for record in self.days.values() for key in record.get("filled") or []}
                for pick in self.picks:
                    if pick.end <= now or pick.peak <= now or pick.peak.isoformat() in filled:
                        continue
                    for hour, share in hour_shares(max(pick.start, now), pick.end):
                        out[hour] = out.get(hour, 0.0) + self.boost_kw * share
                    for hour, share in hour_shares(pick.own_at, pick.own_at + self.run_len):
                        out[hour] = out.get(hour, 0.0) - pick.kwh / run_h * share
        self._delta_key, self._delta = key, out
        return out

    def _live_blocker(self, now: datetime, route: Route) -> str | None:
        # The safety reasons as they are now, on every path: never the heat control's last look, which runs only
        # while it steers a contact.
        control = self.shadow.control
        safety = "stale" if control is None else control.safety_now(self.shadow.config(), now)
        if safety in UNSAFE:
            return str(safety)
        if not self._meter_fresh(now) and self._pump() is None:
            return "no_data"
        budget = BUDGETS.get(route.kind, BUDGETS[ROUTE_ONE_SHOT])
        if self.ledger.used_on(now.astimezone(self.zone).date(), self.zone, SOURCE_PLAN) >= budget:
            return "budget"
        if self.shed_day == now.astimezone(self.zone).date().isoformat():
            return "shed"
        if fits_fuse(self._margins(), self.boost_kw) is False:
            # A car started since the boost was placed, or the house draws more: no room on the fuse now.
            return "fuse"
        return None

    def _margins(self) -> dict[str, float | None]:
        result = getattr(self.shadow.site, "result", None)
        return dict(getattr(result, "measured_margin_a", None) or {})

    async def _async_live_start(self, now: datetime, route: Route, pick: Pick) -> None:
        try:
            seen = await self._async_write(route, SOURCE_PLAN, now)
            record = self._record(pick.peak.astimezone(self.zone).date().isoformat())
            key = pick.peak.isoformat()
            if seen:
                record.setdefault("live", []).append(key)
                self._retry_at.pop(key, None)
                _LOGGER.info("SpotNav hot water: the planned boost for %s started", pick.peak.astimezone(self.zone).strftime("%H:%M"))
            elif route.cloud or key in self._retry_at:
                self.ended_day = now.astimezone(self.zone).date().isoformat()
                _LOGGER.info("SpotNav hot water: the planned boost was not read back; no more planned boosts today")
            else:
                self._retry_at[key] = now + RETRY
            self.shadow.save()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - never into the site
            _LOGGER.debug("SpotNav hot water: the planned boost failed", exc_info=True)

    # ---- writes

    def _options(self, route: Route) -> Sequence[str]:
        state = self.hass.states.get(route.entity_id)
        return () if state is None else tuple(state.attributes.get("options") or ())

    def _on(self, route: Route) -> tuple[bool, datetime | None]:
        from .paths import boost_on  # noqa: PLC0415

        state = self.hass.states.get(route.entity_id)
        if state is None or route.kind == "pulse":
            return False, None
        return boost_on(route.entity_id, state.state), state.last_changed

    async def _async_write(self, route: Route, source: str, now: datetime) -> bool:
        """Start the boost through the route, kept in the ledger first; whether it was read back."""
        call = start_call(route, self._options(route))
        entry = self.ledger.add(Entry(route.entity_id, "start" if call is None else str(call[2].get("option", call[2].get("value", call[1]))), now, source))
        if call is None:
            self.ledger.update(entry, read_back=False)
            return False
        domain, service, data = call
        try:
            await self.hass.services.async_call(domain, service, data, blocking=True)
            if route.kind == "pulse" and domain in ("switch", "input_boolean"):
                try:
                    await _pause(PULSE.total_seconds())
                finally:
                    # A pulse never stays on, not even when the wait is cut short.
                    await self.hass.services.async_call(domain, "turn_off", data, blocking=False)
        except Exception:  # noqa: BLE001 - the write was not taken
            _LOGGER.info("SpotNav hot water: starting the boost through %s failed", route.entity_id, exc_info=True)
            self.ledger.update(entry, read_back=False)
            return False
        if route.kind == "pulse":
            self.ledger.update(entry, read_back=True)
            return True
        limit = READ_BACK_CLOUD if route.cloud else READ_BACK
        waited = 0.0
        while True:
            if self._on(route)[0]:
                self.ledger.update(entry, read_back=True)
                return True
            if waited >= limit.total_seconds():
                break
            await _pause(POLL_S)
            waited += POLL_S
        self.ledger.update(entry, read_back=False)
        return False

    # ---- the Boost button

    def boost(self, now: datetime | None = None) -> dict[str, Any] | None:
        """`hot_water.boost` (`dhw_boost.boost_state`), `None` without a route."""
        now = dt_util.utcnow() if now is None else now
        route = self.route()
        if route is None:
            return None
        on, since = self._on(route)
        last = self.ledger.last(SOURCE_BUTTON)
        pulse_at = last.at if route.kind == "pulse" and last is not None and last.read_back else None
        reading = BoostReading(
            on=on,
            on_since=since,
            ledger_source=self.ledger.source_of_on(route.entity_id, since) if on else None,
            making=self.making_now(),
            pulse_at=pulse_at,
            pending=self._pending is not None and not self._pending.done(),
            failed_at=self._failed_at,
        )
        return boost_state(route, reading, now, self.run_len)

    async def async_press(self, action: str, now: datetime | None = None) -> tuple[str | None, dict[str, Any] | None]:
        """The Boost button: `(error, boost)` (see `api/heat.py`'s `spotnav/heat_boost`)."""
        now = dt_util.utcnow() if now is None else now
        route = self.route()
        if route is None:
            return "spotnav_not_supported", None
        if self._pending is not None and not self._pending.done():
            return "spotnav_busy", self.boost(now)
        state = self.boost(now) or {}
        if action == "stop":
            if state.get("state") != "running":
                return None, state
            if state.get("source") != "user":
                return "spotnav_not_own", state
            call = stop_call(route, self._options(route))
            if call is None:
                return "spotnav_not_supported", state
            try:
                await self.hass.services.async_call(call[0], call[1], call[2], blocking=True)
            except Exception:  # noqa: BLE001 - the answer says so
                _LOGGER.info("SpotNav hot water: stopping the boost failed", exc_info=True)
                return "spotnav_write_failed", self.boost(now)
            return None, self.boost(now)
        last = self.ledger.last(SOURCE_BUTTON)
        if last is not None and now - last.at < RATE:
            return "spotnav_rate_limited", state
        if state.get("state") == "running" and state.get("source") in ("user", "plan"):
            # Already on: nothing more to write.
            return None, state
        self._failed_at = None
        if start_call(route, self._options(route)) is None:
            return "spotnav_write_failed", state
        self._pending = self.hass.async_create_background_task(self._async_button_start(route, now), f"spotnav hot water button {self.shadow.site.entry_id}")
        # Let the write itself go out before answering: a refused call is answered as such.
        await asyncio.sleep(0)
        return None, self.boost(now)

    async def _async_button_start(self, route: Route, now: datetime) -> None:
        try:
            seen = await self._async_write(route, SOURCE_BUTTON, now)
            if not seen:
                self._failed_at = dt_util.utcnow()
            self.shadow.save()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - never into the site
            self._failed_at = dt_util.utcnow()
            _LOGGER.debug("SpotNav hot water: the button's start failed", exc_info=True)

    # ---- what is shown

    def view(self, now: datetime, config: HeatConfig) -> dict[str, Any]:
        """`get_heat`'s `hot_water` (see `heat/view.py`)."""
        zone = self.zone
        pump = self._pump()
        route = self.route(config, pump)
        today = now.astimezone(zone).date()
        start = datetime.combine(today, datetime.min.time(), tzinfo=zone).astimezone(timezone.utc)
        end = datetime.combine(today + timedelta(days=1), datetime.min.time(), tzinfo=zone).astimezone(timezone.utc)
        mode = self.mode(config, route, pump)
        record = self.days.get(today.isoformat(), {})
        live = set(record.get("live") or [])
        own_runs = self.pump_runs(start - timedelta(hours=12), end + timedelta(hours=3))
        shown: dict[str, Pick] = {pick.peak.isoformat(): pick for pick in self.day_picks(today.isoformat())}
        for pick in self.picks:
            shown.setdefault(pick.peak.isoformat(), pick)
        planned = []
        for key, pick in sorted(shown.items(), key=lambda item: item[1].start):
            until = next(
                (datetime.fromisoformat(item[1]) for item in record.get("peaks") or [] if item[0] == key), pick.peak + timedelta(hours=2)
            )
            pump_run = next((run for run in own_runs if not standby(run, self.standby) and pick.start <= run.start <= until + timedelta(minutes=90)), None)
            planned.append(
                {
                    "start": pick.start.isoformat(),
                    "end": pick.end.isoformat(),
                    "kwh": round(pick.kwh, 2),
                    "price": round(pick.price, 2),
                    "reason": pick.reason,
                    "peak": pick.peak.astimezone(zone).strftime("%H:%M"),
                    "live": key in live,
                    "pump_run": None if pump_run is None or pump_run.start > now else pump_run.start.isoformat(),
                }
            )
        runs_today = []
        for run in sorted(self.runs.values(), key=lambda run: run.start):
            if start <= run.start < end:
                source = self.ledger.run_source(run.start)
                runs_today.append(
                    {
                        "start": run.start.isoformat(),
                        "end": run.end.isoformat(),
                        "kwh": round(run.kwh, 3),
                        "source": "pump" if source is None else source,
                    }
                )
        review = None
        for key in sorted(self.days, reverse=True):
            candidate = self.days[key].get("review")
            if key < today.isoformat() and isinstance(candidate, dict):
                review = candidate
                break
        return {
            "boost": self.boost(now),
            "peaks": [peak.as_dict() for peak in self.peaks],
            "planned": planned,
            "runs_today": runs_today,
            "review": review,
            "gate": self.gate_state(),
            "level": self.level(config, route),
            "mode": mode,
        }

    def diagnostics(self) -> dict[str, Any]:
        config = self.shadow.config()
        pump = self._pump()
        route = self.route(config, pump)
        return {
            "route": None if route is None else route.as_dict(),
            "mode": self.mode(config, route, pump),
            "gate": self.gate_state(),
            "ended_day": self.ended_day,
            "shed_day": self.shed_day,
            "standby_kwh": round(self.standby, 3),
            "run_len_min": self.run_len.total_seconds() / 60,
            "boost_kw": round(self.boost_kw, 2),
            "peaks": [peak.stored() for peak in self.peaks],
            "picks": [pick.stored() for pick in self.picks],
            "made_at": None if self.made_at is None else self.made_at.isoformat(),
            "seen_days": len(self.seen),
            "runs": len(self.runs),
            "ledger": self.ledger.stored()[-10:],
            "days": {key: {k: v for k, v in value.items() if k != "peaks"} for key, value in sorted(self.days.items())[-7:]},
            "log": self.log[-10:],
        }


def picks_by_hour(picks: Sequence[Mapping[str, Any]]) -> set[str]:
    """The planned boosts' hour starts (ISO, UTC), for the band's `hot_water` mark."""
    out = set()
    for item in picks:
        start = datetime.fromisoformat(item["start"]).astimezone(timezone.utc)
        out.add(start.replace(minute=0, second=0, microsecond=0).isoformat())
    return out


__all__ = ["MODE_WATCH", "HotWater", "learned_run", "picks_by_hour"]
