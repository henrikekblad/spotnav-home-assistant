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
* **Level A** (`heat/dhw_level_a.py`, a path that blocks the compressor, so hot water too): each hour the heat plan
  asks for the time kept free before each peak and the day's cap (`level_a_rules`); the hours they take out of the
  plan are its cuts, shown in shadow ("would") and applied to the plan the heat control steers by once level A is
  live: "Styr värmen" and its own gate (`dhw_review.GATES`, two weeks), closed again by three cold risks in a week.
  The rules that only end or hold off a block (a run under way, the tank rule) are the heat control's and always
  apply. Each day's review counts the peaks a block made cold and the hours cut. The pump's own runs the planned
  blocks will hold go into the house forecast at the block's end (`forecast_delta`).
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
from ..battery.plan_model import ACTION_GRID_CHARGE, ACTION_HOLD
from ..pricing.period_prices import period_price_slots
from ..site.history_period import PERIOD_DAY, midnight_utc, period_of
from .config import HeatConfig
from .dhw_boost import (
    PULSE,
    PULSE_RUN_WITHIN,
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
from .dhw_level_a import CLEAN_DAYS, HYGIENE_KEEP, PEAK_GUARD, PEAK_GUARD_STRICT, Cut, TankGuard, guards, is_night, kept_hours, max_day_hours, next_hygiene
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
#: A tank sensor that has not reported for this long is no tank sensor.
TANK_FRESH: Final = timedelta(minutes=30)
POLL_S: Final = 5.0
#: The heat control's safety reasons under which no planned boost starts (the compressor goes to the house).
UNSAFE: Final = frozenset({"too_cold", "room_cold", "stale"})
LOG_SIZE: Final = 48
#: A boost not verified to end by itself is stopped this long after its expected run.
BACKSTOP: Final = timedelta(minutes=15)
#: Looks in a row over the fuse before SpotNav's own running boost is shed.
SHED_LOOKS: Final = 2
#: How long one write's service call may take before it is given up on as a write not taken (s): Home Assistant has
#: no timeout of its own, and a hung pump integration must not hold a stop task, so a later shed or backstop runs.
#: Long enough for a cloud route.
CALL_TIMEOUT_S: Final = 30.0
#: A pulse switch SpotNav wrote within `PULSE_WATCH` that reads on for longer than `PULSE_LEFT_ON` is opened.
PULSE_LEFT_ON: Final = timedelta(minutes=1)
PULSE_WATCH: Final = timedelta(hours=2)


def learned_live_share(days: Mapping[str, Mapping[str, Any]], today: date, key: str = "live") -> float:
    """The share of the house profile's weight (`site/load_forecast.py`: `WINDOW_DAYS`, halved every
    `HALF_LIFE_DAYS`) that lies on days with a live boost (`key`: `blocked` for days with a block of SpotNav's): the
    profile already holds that much of the boosts and of the reheats they moved, so the forecast's change is scaled
    by what is left."""
    from ..site.load_forecast import HALF_LIFE_DAYS, WINDOW_DAYS  # noqa: PLC0415 - the site imports this module

    total = live = 0.0
    for age in range(1, WINDOW_DAYS + 1):
        weight = 0.5 ** (age / HALF_LIFE_DAYS)
        total += weight
        record = days.get((today - timedelta(days=age)).isoformat())
        if record is not None and record.get(key):
            live += weight
    return 0.0 if total <= 0 else live / total


def beta_gate_open(hass: HomeAssistant, site: Any) -> bool:
    """Whether the site's beta testing skips the learning gate (`dhw_review.GATES`): `beta.learning_gate_open`, the
    one place the beta switch is read. It opens the learning gate only; every safety check still applies."""
    from ..beta import learning_gate_open  # noqa: PLC0415 - imported here so the heat package loads without it

    return learning_gate_open(hass, site)


async def _pause(seconds: float) -> None:
    await asyncio.sleep(seconds)


async def _async_service(hass: HomeAssistant, domain: str, service: str, data: dict[str, Any]) -> None:
    """One write's blocking service call, given up on after `CALL_TIMEOUT_S` (`TimeoutError`, a write not taken)."""
    async with asyncio.timeout(CALL_TIMEOUT_S):
        await hass.services.async_call(domain, service, data, blocking=True)


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
        #: The night's runs, kept longer (`dhw_level_a.HYGIENE_KEEP`) to tell the pump's periodic increase.
        self.night_runs: dict[str, HotWaterRun] = {}
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
        #: The day the gate opened: it stays open until misses close it (`back_off`).
        self.opened: str | None = None
        self.ended_day: str | None = None
        #: The site's standby cutoff (kWh, `dhw_peaks.standby_kwh` on the runs kept).
        self.standby: float = standby_kwh(())
        self.run_len: timedelta = DEFAULT_RUN
        self.boost_kw: float = DEFAULT_BOOST_KW
        self.made_at: datetime | None = None
        self.log: list[dict[str, Any]] = []
        self._pending: asyncio.Task[None] | None = None
        #: A stop SpotNav writes of its own (the backstop, a shed), apart from a start's write and read-back.
        self._side: asyncio.Task[None] | None = None
        self._failed_at: datetime | None = None
        self._retry_at: dict[str, datetime] = {}
        self._over_looks = 0
        #: The local day SpotNav shed its own boost for the fuse (not restarted that day).
        self.shed_day: str | None = None
        self._delta_key: Any = None
        self._delta: dict[datetime, float] = {}
        self._block_key: Any = None
        self._block: dict[datetime, float] = {}
        #: The meter has shown the run a pulse asked for (its end then ends the pulse's boost).
        self._pulse_ran = False
        #: A planned pulse waiting for its read-back: its write's time (ISO) to its peak (ISO).
        self._pulse_peaks: dict[str, str] = {}
        #: The tank rule (level A): stepped at each of the heat control's looks.
        self.tank = TankGuard()
        #: Level A's gate, as the boost's: its shadow days count after `a_since`, it stays open from `a_opened` until
        #: cold risks close it (`a_backed_off`).
        self.a_since: str | None = None
        self.a_opened: str | None = None
        self.a_backed_off: str | None = None
        #: Since a back-off: level A steers with its stricter rules until `CLEAN_DAYS` clean days.
        self.a_strict_since: str | None = None
        #: The heat plan's hours level A takes out (`dhw_level_a.cuts`) and whether the plan steered by has them out.
        self.cuts: list[Cut] = []
        self.cuts_live = False

    # ---- store

    def stored(self) -> dict[str, Any]:
        return {
            "ledger": self.ledger.stored(),
            "runs": [run.as_dict() for run in sorted(self.runs.values(), key=lambda run: run.start)],
            "night_runs": [run.as_dict() for run in sorted(self.night_runs.values(), key=lambda run: run.start)],
            "seen": sorted(self.seen),
            "peaks": [peak.stored() for peak in self.peaks],
            "days": self.days,
            "gate_since": self.gate_since,
            "backed_off": self.backed_off,
            "opened": self.opened,
            "ended_day": self.ended_day,
            "picks": [pick.stored() for pick in self.picks],
            "a_since": self.a_since,
            "a_opened": self.a_opened,
            "a_backed_off": self.a_backed_off,
            "a_strict_since": self.a_strict_since,
        }

    def restore(self, raw: Any) -> None:
        if not isinstance(raw, dict):
            return
        self.ledger = Ledger.from_stored(raw.get("ledger"))
        runs = [run for item in raw.get("runs") or [] if (run := _run_of(item)) is not None]
        self.runs = {run.start.isoformat(): run for run in runs}
        night = [run for item in raw.get("night_runs") or [] if (run := _run_of(item)) is not None]
        self.night_runs = {run.start.isoformat(): run for run in night}
        self.seen = {day for day in raw.get("seen") or [] if isinstance(day, str)}
        peaks = [peak for item in raw.get("peaks") or [] if (peak := Peak.from_stored(item)) is not None]
        if peaks:
            self.peaks = peaks
        days = raw.get("days")
        if isinstance(days, dict):
            self.days = {key: value for key, value in days.items() if isinstance(key, str) and isinstance(value, dict)}
        for name in ("gate_since", "backed_off", "opened", "ended_day", "a_since", "a_opened", "a_backed_off", "a_strict_since"):
            value = raw.get(name)
            setattr(self, name, value if isinstance(value, str) else None)
        self.picks = [pick for item in raw.get("picks") or [] if (pick := Pick.from_stored(item)) is not None]
        self.run_len, self.boost_kw = learned_run(list(self.runs.values()))
        self.standby = standby_kwh(self.runs.values())

    async def async_start(self) -> None:
        """At the start: a pulse switch SpotNav has pulsed that reads on was left on (Home Assistant stopped within
        the pulse): opened, so the pump's hot-water input is never held."""
        try:
            route = self.route()
            if route is None or route.kind != "pulse" or route.domain not in ("switch", "input_boolean"):
                return
            state = self.hass.states.get(route.entity_id)
            if state is None or state.state != "on":
                return
            if not any(entry.entity_id == route.entity_id for entry in self.ledger.entries):
                return
            _LOGGER.info("SpotNav hot water: %s was left on after a pulse; turning it off", route.entity_id)
            await _async_service(self.hass, route.domain, "turn_off", {"entity_id": route.entity_id})
        except Exception:  # noqa: BLE001 - tried again at the next start
            _LOGGER.debug("SpotNav hot water: the pulse switch could not be opened", exc_info=True)

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
            # A boost not verified to end by itself is the button's only: nothing is planned on it.
            return LEVEL_B if route.verified else None
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
        if self.opened is not None:
            state["open"] = True
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

    # ---- level A

    def a_applies(self, config: HeatConfig) -> bool:
        """Whether the settings' path lowers by blocking the compressor, so hot water too (`paths.blocks_hot_water`)."""
        from .paths import blocks_hot_water  # noqa: PLC0415

        return blocks_hot_water(config) and config.control_entity is not None

    def a_gate_reviews(self) -> list[dict[str, Any]]:
        out = []
        for key in sorted(self.days):
            review = self.days[key].get("review")
            # Only a review made with level A's figures counts: one kept before them would open it unseen.
            if isinstance(review, dict) and "a_mode" in review and (self.a_since is None or key > self.a_since):
                out.append(review)
        # While stricter after a back-off, no day counts toward the gate.
        return [] if self.a_strict() else out

    def a_strict(self) -> bool:
        """Whether level A steers with its stricter rules after a back-off."""
        return self.a_strict_since is not None

    def a_gate_state(self) -> dict[str, Any]:
        state = gate(self.a_gate_reviews(), LEVEL_A)
        if self.a_opened is not None:
            state["open"] = True
        if not state["open"] and beta_gate_open(self.hass, self.shadow.site):
            state["open"] = True
        return {**state, "backed_off": self.a_backed_off}

    def a_mode(self, config: HeatConfig, pump: Any = None) -> str:
        """Level A's mode: `watch` as the boost's, `live` with "Styr värmen" and its gate open, else `shadow`."""
        if self.watched(self._pump() if pump is None else pump):
            return MODE_WATCH
        if config.control_enabled and (self.a_gate_state()["open"] or self.a_strict()):
            # After a back-off level A keeps steering, stricter: never back to the plan without its rules.
            return MODE_LIVE
        return MODE_SHADOW

    def level_a_rules(
        self, now: datetime, config: HeatConfig, days: Sequence[date]
    ) -> tuple[dict[datetime, tuple[str, datetime | None]], int | None] | None:
        """Level A's rules for the heat plan of the local days given (`dhw_level_a`): the hours kept free (before each
        peak, the pump's periodic increase where the meter shows it) and the day's cap; `None` where the path does not
        block hot water."""
        if not self.a_applies(config):
            return None
        pump = self._pump()
        runs = list(self.runs.values())
        strict = self.a_strict()
        guard = PEAK_GUARD_STRICT if strict else PEAK_GUARD
        found = guards(self.peaks, days, self.zone, runs, now, self.filled_peaks(), guard, self.standby)
        hygiene = next_hygiene(runs, self.zone, now, list(self.night_runs.values()))
        return kept_hours(found, hygiene), max_day_hours(self.tank_c(pump) is not None, strict)

    def filled_peaks(self) -> set[datetime]:
        """The peaks a boost fills for certain: SpotNav's written and read back (`live`), or one already running when
        the pick came due (`filled`). A pick only planned, skipped or never written fills nothing."""
        out: set[datetime] = set()
        for record in self.days.values():
            for key in [*(record.get("live") or []), *(record.get("filled") or [])]:
                try:
                    out.add(datetime.fromisoformat(key))
                except (TypeError, ValueError):
                    continue
        return out

    def set_cuts(self, cuts: Sequence[Cut], live: bool) -> None:
        self.cuts, self.cuts_live = list(cuts), live

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

    def tank_c(self, pump: Any = None) -> float | None:
        """The tank sensor's reading (°C), `None` without one, and also where its entity does not read or has not
        reported within `TANK_FRESH`: a stale tank is no tank sensor (the day's cap applies, the 40 °C rule is not
        trusted to allow a block)."""
        pump = self._pump() if pump is None else pump
        value = None if pump is None else getattr(pump, "hot_water_temp_c", None)
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return None
        source = (getattr(pump, "sources", None) or {}).get("hot_water_temp")
        if isinstance(source, str) and source:
            now = dt_util.utcnow()
            for entity_id in source.split(" + "):
                state = self.hass.states.get(entity_id.strip())
                if state is None or state.state in ("unavailable", "unknown"):
                    return None
                reported = getattr(state, "last_reported", None) or state.last_updated
                if now - reported > TANK_FRESH:
                    return None
        return float(value)

    def tank_low(self) -> bool:
        """The tank rule (`dhw_level_a.TankGuard`) stepped on the tank's reading now: whether no block may hold the
        hot water. Without a tank sensor never."""
        return self.tank.step(self.tank_c())

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
                self._keep_run(run)

    def _keep_run(self, run: HotWaterRun) -> None:
        self.runs[run.start.isoformat()] = run
        if is_night(run.start, self.zone):
            self.night_runs[run.start.isoformat()] = run

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
        own = self.boost_delta(now)
        load = hourly_to_quarters(
            {hour: max(0.0, kw - own.get(hour, 0.0)) for hour, kw in forecast.plan_kw_by_hour(now, heat=True).items()}
        )
        try:
            windows = site_car_windows(self.hass, site)
        except Exception:  # noqa: BLE001 - no car plan: none counted
            _LOGGER.debug("inputs: ignored failure (no car plan: none counted)", exc_info=True)
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
        if route is None or not route.verified:
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
        # Level A: each cut hour as it stood when it began; whether level A steered that day; whether SpotNav blocked.
        for cut in self.cuts:
            if cut.start <= now:
                record = self._record(cut.start.astimezone(zone).date().isoformat())
                record.setdefault("cuts", {}).setdefault(cut.start.isoformat(), [*cut.stored(), self.cuts_live])
        today_record = self._record(today.isoformat())
        if self.cuts_live:
            today_record["a_live"] = True
        control = self.shadow.control
        if control is not None and any(item[0] is not None and (item[1] is None or item[1] > now - timedelta(hours=1)) for item in control.lowerings):
            today_record["blocked"] = True
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
            blocks = [(s, e) for s, e in self.shadow.relay_intervals(now)[0] if e > start - timedelta(hours=3) and s < end + timedelta(hours=3)]
            record["review"] = review_day(
                day,
                self.zone,
                peaks,
                self.day_picks(key),
                self.pump_runs(start - timedelta(hours=12), end + timedelta(hours=3)),
                prices.get,
                mode=MODE_LIVE if live else MODE_SHADOW,
                live_peaks=live,
                cutoff=self.standby,
                blocks=blocks,
                previous_until=self._previous_until(key),
                a_mode=MODE_LIVE if record.get("a_live") else MODE_SHADOW,
                blocks_cut=len(record.get("cuts") or {}),
            )
            _LOGGER.info("SpotNav hot water: %s reviewed: %s", key, {k: v for k, v in record["review"].items() if k != "per_peak"})
        today = now.astimezone(self.zone).date()
        reviews = [r for d in self.days.values() if isinstance(r := d.get("review"), dict)]
        if self.gate_state()["open"] and back_off(reviews, today) and self.backed_off != today.isoformat():
            # Only misses close the gate: its shadow days count again from tomorrow.
            self.gate_since = today.isoformat()
            self.backed_off = today.isoformat()
            self.opened = None
            _LOGGER.info("SpotNav hot water: too many misses in a week; back to shadow")
        elif self.opened is None and gate(self.gate_reviews(), LEVEL_B)["open"]:
            # Opened once, the gate stays open while steering: the shadow days that opened it age out of the kept
            # days, and live days are no shadow days.
            self.opened = today.isoformat()
            _LOGGER.info("SpotNav hot water: the shadow gate opened")
        if self.a_strict():
            self._a_strict_end(reviews)
        elif self.a_gate_state()["open"] and back_off(reviews, today, LEVEL_A):
            # Also on a beta site: this is a safety net, not a learning gate. Level A stays and widens.
            self.a_since = self.a_strict_since = self.a_backed_off = today.isoformat()
            _LOGGER.info("SpotNav hot water: blocks held the pump's run into a peak too often in a week; level A stricter")
        elif self.a_opened is None and gate(self.a_gate_reviews(), LEVEL_A)["open"]:
            self.a_opened = today.isoformat()
            _LOGGER.info("SpotNav hot water: level A's shadow gate opened")

    def _a_strict_end(self, reviews: Sequence[Mapping[str, Any]]) -> None:
        """`CLEAN_DAYS` reviewed days in a row after the back-off without a cold risk end the stricter rules; the gate
        counts its days from then."""
        since = self.a_strict_since or ""
        after = sorted((review for review in reviews if str(review.get("date", "")) > since), key=lambda review: review["date"])
        clean = after[-CLEAN_DAYS:]
        if len(clean) == CLEAN_DAYS and all(int(review.get("cold_risks", 0)) == 0 for review in clean):
            self.a_since = str(clean[-1]["date"])
            self.a_strict_since = None
            _LOGGER.info("SpotNav hot water: a week without a late reheat; level A back to its usual rules")

    def _previous_until(self, key: str) -> datetime | None:
        """The end of the last peak the day before `key` kept."""
        previous = self.days.get((date.fromisoformat(key) - timedelta(days=1)).isoformat()) or {}
        ends = []
        for item in previous.get("peaks") or []:
            try:
                ends.append(datetime.fromisoformat(item[1]))
            except (TypeError, ValueError, IndexError):
                continue
        return max(ends) if ends else None

    def _trim(self, now: datetime) -> None:
        oldest = (now - timedelta(days=DAYS_KEPT)).astimezone(self.zone).date().isoformat()
        for key in [key for key in self.days if key < oldest]:
            del self.days[key]
        first = now - timedelta(days=PEAK_DAYS + 1)
        self.runs = {key: run for key, run in self.runs.items() if run.start >= first}
        self.night_runs = {key: run for key, run in self.night_runs.items() if run.start >= now - HYGIENE_KEEP}
        self.seen = {day for day in self.seen if day >= first.astimezone(self.zone).date().isoformat()}
        self.ledger.trim(now)

    # ---- each look: live boosts

    def tick(self, now: datetime) -> None:
        """Shed SpotNav's own running boost under fuse pressure; start a live pick whose time has come (see the
        module)."""
        config = self.shadow.config()
        self._observe(now, config)
        if self._side is None or self._side.done():
            self._pulse_left_on(now, config)
        if self._side is None or self._side.done():
            # A stop (the backstop, a shed) is its own task: a start's read-back never holds it back.
            self._backstop(now, config)
            self._shed_check(now, config)
        if self._pending is not None and not self._pending.done():
            return
        if not config.control_enabled or not self.picks:
            # Nothing planned is written without the opt-in: no need to read the pump each look.
            return
        pump = self._pump()
        route = self.route(config, pump)
        if route is None or not route.verified or self.mode(config, route, pump) != MODE_LIVE:
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

    def _observe(self, now: datetime, config: HeatConfig) -> None:
        """A boost entity read off ends SpotNav's and the button's writes to it in the ledger (their ownership); a
        pulse's boost ends when the meter has shown its run and the run is over, or when no run came in time."""
        if not any(entry.ended is None and entry.read_back is not False for entry in self.ledger.entries):
            return
        route = self.route(config)
        if route is None:
            return
        if route.kind != "pulse":
            if not self._on(route)[0]:
                self.ledger.close(route.entity_id, now)
            return
        self._confirm_pulse(now, route)
        entry = self._pulse_entry(route, now)
        if entry is None:
            self._pulse_ran = False
            return
        making = self.making_now()
        if making:
            self._pulse_ran = True
        elif self._pulse_ran or now - entry.at > PULSE_RUN_WITHIN:
            self.ledger.close(route.entity_id, now)
            self._pulse_ran = False

    def _confirm_pulse(self, now: datetime, route: Route) -> None:
        """A pulse waiting for its read-back: the run showing reads it back, none within `PULSE_RUN_WITHIN` does not
        (a press then shows failed; a planned one is tried once more on this local route, then ends the day)."""
        entry = next((item for item in reversed(self.ledger.entries) if item.entity_id == route.entity_id), None)
        if entry is None or entry.read_back is not None:
            return
        if self.making_now():
            seen = True
        elif now - entry.at > PULSE_RUN_WITHIN:
            seen = False
        else:
            return
        self.ledger.update(entry, read_back=seen)
        if seen:
            self._pulse_ran = True
        if entry.source == SOURCE_BUTTON and not seen:
            self._failed_at = now
        peak = self._pulse_peaks.pop(entry.at.isoformat(), None)
        if entry.source == SOURCE_PLAN and peak is not None:
            self._planned_outcome(now, route, peak, seen)
        self.shadow.save()

    def _pulse_entry(self, route: Route, now: datetime) -> Entry | None:
        """The last pulse SpotNav or the button sent on the route whose run may still be coming or going."""
        last = next((entry for entry in reversed(self.ledger.entries) if entry.entity_id == route.entity_id), None)
        if last is None or last.read_back is False or last.ended is not None:
            return None
        if now - last.at > PULSE_RUN_WITHIN + 2 * self.run_len:
            return None
        return last

    def pulse_owner(self, now: datetime | None = None) -> str | None:
        """Whose pulse run may be going now on a pulse route (`plan` or `button`), `None` for none: a pulse is on
        for two seconds, so the boost it asked for is known from the ledger, not from the switch."""
        now = dt_util.utcnow() if now is None else now
        route = self.route()
        if route is None or route.kind != "pulse":
            return None
        entry = self._pulse_entry(route, now)
        return None if entry is None else entry.source

    def _backstop(self, now: datetime, config: HeatConfig) -> None:
        """A boost not verified to end by itself that SpotNav or the button started and that still reads on once
        its run should be over (`run_len` and `BACKSTOP`): one stop write, so the pump is never left forcing hot
        water."""
        route = self.route(config)
        if route is None or route.verified or route.kind == "pulse" or not self._on(route)[0]:
            return
        if self.ledger.owner(route.entity_id, now) is None:
            return
        last = next((entry for entry in reversed(self.ledger.entries) if entry.entity_id == route.entity_id), None)
        if last is None or now - last.at < (route.duration or self.run_len) + BACKSTOP:
            return
        call = stop_call(route, self._options(route))
        if call is None:
            return
        _LOGGER.info("SpotNav hot water: %s still on after its run; stopping it", route.entity_id)

        async def stop() -> None:
            if await self._async_call_quietly(call):
                self.ledger.close(route.entity_id, now)

        self._side = self.hass.async_create_background_task(stop(), f"spotnav hot water backstop {self.shadow.site.entry_id}")

    def _pulse_left_on(self, now: datetime, config: HeatConfig) -> None:
        """A pulse switch SpotNav wrote within `PULSE_WATCH` that reads on for longer than `PULSE_LEFT_ON` (a turn-on
        that landed after it was given up on, an off that failed): opened, at each look until it reads off, so the
        pump's hot-water input is never held (`async_start` does the same at the start)."""
        route = self.route(config)
        if route is None or route.kind != "pulse" or route.domain not in ("switch", "input_boolean"):
            return
        state = self.hass.states.get(route.entity_id)
        if state is None or state.state != "on" or now - state.last_changed < PULSE_LEFT_ON:
            return
        last = next((entry for entry in reversed(self.ledger.entries) if entry.entity_id == route.entity_id), None)
        if last is None or now - last.at > PULSE_WATCH:
            return
        _LOGGER.info("SpotNav hot water: %s still on after a pulse; turning it off", route.entity_id)
        call = (route.domain, "turn_off", {"entity_id": route.entity_id})
        self._side = self.hass.async_create_background_task(
            self._async_call_quietly(call), f"spotnav hot water pulse off {self.shadow.site.entry_id}"
        )

    async def _async_call_quietly(self, call: tuple[str, str, dict[str, Any]]) -> bool:
        """One service call, its failure logged (the next look tries again where it still applies)."""
        try:
            await _async_service(self.hass, call[0], call[1], call[2])
        except Exception:  # noqa: BLE001 - logged (a call given up on too)
            _LOGGER.info("SpotNav hot water: the stop through %s failed", call[2].get("entity_id"), exc_info=True)
            return False
        return True

    async def async_shutdown(self) -> None:
        """The site unloads: a pending write and its read-back end with it (a pulse's switch is opened on the way
        out, `_async_write`)."""
        for task in (self._pending, self._side):
            if task is not None and not task.done():
                task.cancel()

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
        if not self._on(route)[0] or self.ledger.owner(route.entity_id, now) != SOURCE_PLAN:
            return
        self._over_looks += 1
        if self._over_looks < SHED_LOOKS:
            return
        call = stop_call(route, self._options(route))
        if call is None:
            return
        self._over_looks = 0
        day = now.astimezone(self.zone).date().isoformat()

        async def shed() -> None:
            if await self._async_call_quietly(call):
                self.shed_day = day
                _LOGGER.info("SpotNav hot water: its own boost ended for the fuse; not started again today")

        self._side = self.hass.async_create_background_task(shed(), f"spotnav hot water shed {self.shadow.site.entry_id}")

    def forecast_delta(self, now: datetime) -> dict[datetime, float]:
        """The change the hot water brings to the house's load per hour (kW), what the cars' and the battery's plans
        count on (`HeatShadow.forecast_delta`): SpotNav's own boosts (`boost_delta`) and the pump's own runs its
        planned blocks hold (`block_delta`)."""
        boosts = self.boost_delta(now)
        try:
            held = self.block_delta(now)
        except Exception:  # noqa: BLE001 - the boosts' change stands without it
            _LOGGER.debug("SpotNav hot water: the blocks' change to the forecast could not be made", exc_info=True)
            held = {}
        if not held:
            return boosts
        out = dict(boosts)
        for hour, kw in held.items():
            out[hour] = out.get(hour, 0.0) + kw
        return out

    def block_delta(self, now: datetime) -> dict[datetime, float]:
        """The pump's own runs the heat control's closings ahead will hold (on a path that blocks hot water): each
        peak's expected reheat (`Peak.run_at`, its learned kWh over the run's length) that falls in a closing moves to
        the closing's end; where it was, only the part the relay's own change (`lower_share` of the heat, hot water
        included) does not already take out. A peak a live boost fills is the boost's (`boost_delta`). Scaled by what the house profile
        does not hold of it yet (days with a block of SpotNav's, `learned_live_share`)."""
        control = self.shadow.control
        closings = None if control is None else control.closings_ahead(now)
        if not closings:
            return {}
        key = (int(now.timestamp() // 300), tuple(closings), tuple(self.peaks), self.run_len, len(self.days))
        if key == self._block_key:
            return self._block
        self._block_key, self._block = key, self._held_runs(now, closings)
        return self._block

    def _held_runs(self, now: datetime, closings: Sequence[tuple[datetime, datetime]]) -> dict[datetime, float]:
        from .relay_effect import hour_shares  # noqa: PLC0415

        zone = self.zone
        today = now.astimezone(zone).date()
        # A peak a boost fills is the boost's (`boost_delta`): one written, or one it still counts on.
        boosted = self.filled_peaks()
        if self.boost_delta(now):
            boosted |= {pick.peak for pick in self.picks if now < pick.start + START_LATE}
        run_h = max(self.run_len.total_seconds() / 3600.0, 0.25)
        scale = 1.0 - learned_live_share(self.days, today, key="blocked")
        # The relay's own change takes `lower_share` of the block hour's heat out, the pump's hot water included
        # (`HeatShadow.heat_kw` is the meter's whole forecast): only the rest of the held run is taken out here.
        kept = 1.0 - min(1.0, max(0.0, float(self.shadow.effect.lower_share)))
        out: dict[datetime, float] = {}
        for day in (today, today + timedelta(days=1)):
            for peak in self.peaks:
                ready, _until, run_at = peak_instant(peak, day, zone)
                if run_at < now or ready in boosted:
                    continue
                held = next((end for start, end in closings if start <= run_at < end), None)
                if held is None:
                    continue
                kw = peak.kwh / run_h * scale
                for hour, share in hour_shares(run_at, run_at + self.run_len):
                    out[hour] = out.get(hour, 0.0) - kw * kept * share
                for hour, share in hour_shares(held, held + self.run_len):
                    out[hour] = out.get(hour, 0.0) + kw * share
        return {hour: kw for hour, kw in out.items() if abs(kw) > 1e-9}

    def boost_delta(self, now: datetime) -> dict[datetime, float]:
        """The change SpotNav's own boosts bring to the house's load per hour (kW): each boost that will be written
        (live) adds its power over its run, and the pump's own reheat it replaces is taken out at its expected time.
        Nothing in shadow: there the pump runs as it always has. Kept per 5 minutes."""
        filled_count = sum(len(record.get("filled") or []) + len(record.get("live") or []) for record in self.days.values())
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
                live = {key for record in self.days.values() for key in record.get("live") or []}
                # The profile learns the live boosts from the meter: what it holds of them already is not added again.
                scale = 1.0 - learned_live_share(self.days, now.astimezone(self.zone).date())
                for pick in self.picks:
                    key = pick.peak.isoformat()
                    if pick.end <= now or pick.peak <= now or key in filled:
                        continue
                    if key not in live and now >= pick.start + START_LATE:
                        # Its start passed without a write (the fuse, a safety reason): the pump reheats as always.
                        continue
                    for hour, share in hour_shares(max(pick.start, now), pick.end):
                        out[hour] = out.get(hour, 0.0) + self.boost_kw * share * scale
                    for hour, share in hour_shares(pick.own_at, pick.own_at + self.run_len):
                        out[hour] = out.get(hour, 0.0) - pick.kwh / run_h * share * scale
        self._delta_key, self._delta = key, out
        return out

    def _live_blocker(self, now: datetime, route: Route) -> str | None:
        # The safety reasons as they are now, on every path: never the heat control's last look, which runs only
        # while it steers a contact.
        control = self.shadow.control
        safety = "stale" if control is None else control.safety_now(self.shadow.config(), now)
        if safety in UNSAFE:
            return str(safety)
        if not self._data_fresh(now):
            # No meter or pump state read within `DATA_FRESH`: the pump runs as normal.
            return "no_data"
        budget = BUDGETS.get(route.kind, BUDGETS[ROUTE_ONE_SHOT])
        if self.ledger.used_on(now.astimezone(self.zone).date(), self.zone, SOURCE_PLAN) >= budget:
            return "budget"
        if self.shed_day == now.astimezone(self.zone).date().isoformat():
            return "shed"
        return self._fuse_blocker()

    def _fuse_blocker(self) -> str | None:
        """Why the boost does not fit the site now, `None` when it does. Fails closed: the site's measurements must
        be healthy and every phase known (SpotNav's own battery charge or hold counts as what it would yield); without phase
        sensors, the site's total import against its cap; and the import target's level for the running hour."""
        state = self._site_state()
        if state == "observing":
            margins = self._margins(start=True)
            if not margins or any(value is None for value in margins.values()):
                return "no_phase_data"
            if not fits_fuse(margins, self.boost_kw):
                # A car started since the boost was placed, or the house draws more: no room on the fuse now.
                return "fuse"
        elif state == "not_configured":
            from ..battery.plan_runtime import site_import_cap_kw  # noqa: PLC0415 - it imports the site

            cap = site_import_cap_kw(self.hass, self.shadow.site)
            grid = self._grid_kw()
            if cap is None or grid is None:
                return "no_fuse_data"
            if grid + self.boost_kw > cap:
                return "fuse"
        else:
            return "measurements"
        site = self.shadow.site
        set_kw = site._import_target_kw()  # noqa: SLF001 - the site's own read of its stored target
        if set_kw is not None:
            from ..site.import_target import held_level_kw  # noqa: PLC0415

            level = site.effective_import_target_kw(set_kw)
            grid = self._grid_kw()
            if level is None or grid is None:
                return "import_target"
            if grid + self.boost_kw > held_level_kw(level):
                return "import_target"
        return None

    def _site_result(self) -> Any:
        """The site's last capacity result (`site/site_capacity.SiteCapacityResult`)."""
        return getattr(self.shadow.site, "result", None)

    def _site_state(self) -> str | None:
        return getattr(self._site_result(), "state", None)

    def _margins(self, *, start: bool = False) -> dict[str, float | None]:
        """Each phase's room now (A): the measured margin. For a start only, what the battery would yield where
        that is known, and only while SpotNav's own executor runs a grid charge or a hold (`_own_battery_yields`):
        that one hands back per phase under 1 A of headroom. A battery in its own mode does not yield (or only on
        its total), so its charge is no room. The shed and the button's refusal read the measured margin alone: an
        own charge has yielded within two looks, so a phase still over then is over."""
        result = self._site_result()
        measured = dict(getattr(result, "measured_margin_a", None) or {})
        if not start or not self._own_battery_yields():
            return measured
        estimated = dict(getattr(result, "estimated_headroom_if_battery_yields_a", None) or {})
        return {phase: estimated.get(phase) if estimated.get(phase) is not None else value for phase, value in measured.items()}

    def _own_battery_yields(self) -> bool:
        """SpotNav's own battery executor is active in a grid charge or a hold: it hands back for the fuse."""
        control = getattr(self.shadow.site, "battery_control", None)
        state = getattr(getattr(control, "executor", None), "state", None)
        return getattr(state, "phase", None) == "active" and getattr(state, "action", None) in (ACTION_GRID_CHARGE, ACTION_HOLD)

    def _grid_kw(self) -> float | None:
        from ..execution.solar_execution import site_net_grid_w  # noqa: PLC0415 - it imports the site

        try:
            watts = site_net_grid_w(self.shadow.site)
        except Exception:  # noqa: BLE001 - not known: the checks fail closed
            _LOGGER.debug("grid_kw: ignored failure (not known: the checks fail closed)", exc_info=True)
            return None
        return None if watts is None else watts / 1000.0

    def _meter_found(self) -> bool:
        meter = self.shadow.site.house_forecast.heat.meter
        return bool(meter.found and meter.entity_id is not None)

    def _data_fresh(self, now: datetime) -> bool:
        """The pump's hot water can be told now: the heat pump meter reported within `DATA_FRESH`, or, without a
        meter, the pump's own state reads."""
        if self._meter_found():
            return self._meter_fresh(now)
        pump = self._pump()
        return pump is not None and pump.priority is not None

    async def _async_live_start(self, now: datetime, route: Route, pick: Pick) -> None:
        try:
            seen = await self._async_write(route, SOURCE_PLAN, now)
            key = pick.peak.isoformat()
            if seen is None:
                self._pulse_peaks[now.isoformat()] = key
                self.shadow.save()
                return
            self._planned_outcome(now, route, key, seen)
            self.shadow.save()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - never into the site
            _LOGGER.debug("SpotNav hot water: the planned boost failed", exc_info=True)

    def _planned_outcome(self, now: datetime, route: Route, key: str, seen: bool) -> None:
        """A planned boost read back (the peak is live) or not (tried once more on a local route, else the day ends)."""
        try:
            record = self._record(datetime.fromisoformat(key).astimezone(self.zone).date().isoformat())
            if seen:
                record.setdefault("live", []).append(key)
                if (control := self.shadow.control) is not None:
                    control.request_look()
                self._retry_at.pop(key, None)
                _LOGGER.info("SpotNav hot water: the planned boost for %s started", datetime.fromisoformat(key).astimezone(self.zone).strftime("%H:%M"))
            elif route.cloud or key in self._retry_at:
                self.ended_day = now.astimezone(self.zone).date().isoformat()
                _LOGGER.info("SpotNav hot water: the planned boost was not read back; no more planned boosts today")
            else:
                self._retry_at[key] = now + RETRY
        except ValueError:
            return

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

    async def _async_write(self, route: Route, source: str, now: datetime) -> bool | None:
        """Start the boost through the route, kept in the ledger first; whether it was read back (`None`: a pulse,
        read back later by the meter)."""
        call = start_call(route, self._options(route))
        entry = self.ledger.add(Entry(route.entity_id, "start" if call is None else str(call[2].get("option", call[2].get("value", call[1]))), now, source))
        if call is None:
            self.ledger.update(entry, read_back=False)
            return False
        domain, service, data = call
        pulse = route.kind == "pulse" and domain in ("switch", "input_boolean")
        try:
            try:
                await _async_service(self.hass, domain, service, data)
            except TimeoutError:
                if route.kind == "pulse":
                    # Given up on, the switch may still close late: a pulse never stays on (`_pulse_left_on` opens
                    # it at a later look should this fail too). A pulse is read back by its run, not here.
                    if pulse:
                        await self.hass.services.async_call(domain, "turn_off", data, blocking=False)
                    raise
                # Given up on, the write may still land: it is read back as one that answered, so a boost it
                # started is SpotNav's and the backstop and the fuse shed still stop it.
                _LOGGER.info("SpotNav hot water: starting the boost through %s did not answer in time; reading it back",
                             route.entity_id)
            if pulse:
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
            # A pulse is read back by the run it asks for: the meter (or the pump's state) must show it within
            # `PULSE_RUN_WITHIN` (`_confirm_pulse`); until then the entry waits.
            return None
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
        pulse = self._pulse_entry(route, now) if route.kind == "pulse" else None
        pulse_at = None if pulse is None or pulse.read_back is not True else pulse.at
        reading = BoostReading(
            on=on,
            on_since=since,
            ledger_source=self.ledger.owner(route.entity_id, now) if on else (None if pulse is None else pulse.source),
            making=self.making_now(),
            pulse_at=pulse_at,
            pending=(self._pending is not None and not self._pending.done()) or (pulse is not None and pulse.read_back is None),
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
                await _async_service(self.hass, call[0], call[1], call[2])
            except Exception:  # noqa: BLE001 - the answer says so (a call given up on too)
                _LOGGER.info("SpotNav hot water: stopping the boost failed", exc_info=True)
                return "spotnav_write_failed", self.boost(now)
            return None, self.boost(now)
        # A start that was not taken or not read back is no start: the next press is not held for it.
        last = next((entry for entry in reversed(self.ledger.entries) if entry.source == SOURCE_BUTTON and entry.read_back is not False), None)
        if last is not None and now - last.at < RATE:
            return "spotnav_rate_limited", state
        if state.get("state") == "running" and state.get("source") in ("user", "plan"):
            # Already on: nothing more to write.
            return None, state
        if over_fuse(self._margins()) is True:
            # A phase is already over the fuse less its margin: something must go off first. A tight margin is the
            # household's own to use, as at the pump's display; unknown margins never refuse.
            return "spotnav_no_headroom", state
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
            if seen is False:
                self._failed_at = dt_util.utcnow()
            elif (control := self.shadow.control) is not None:
                # A block held now ends at once: the heat control looks without waiting for its next look.
                control.request_look()
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
                # A review kept before level A's figures has none of them: none were counted.
                review = {
                    "cold_risks": 0,
                    "blocks_cut": 0,
                    "a_mode": MODE_SHADOW,
                    **candidate,
                    "per_peak": [{"cold_risk": False, **item} for item in candidate.get("per_peak") or []],
                }
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
            "blocking": self._blocking_view(now, config, pump),
        }

    def _blocking_view(self, now: datetime, config: HeatConfig, pump: Any) -> dict[str, Any] | None:
        """`hot_water.blocking`: level A where the path blocks hot water (see `heat/view.py`)."""
        if not self.a_applies(config):
            return None
        zone = self.zone
        shown: dict[str, dict[str, Any]] = {}
        today = now.astimezone(zone).date().isoformat()
        for key, value in (self.days.get(today, {}).get("cuts") or {}).items():
            try:
                start = datetime.fromisoformat(key)
                ready = None if value[1] is None else datetime.fromisoformat(value[1])
                shown[key] = Cut(start, str(value[0]), ready).as_dict(zone, bool(value[2]))
            except (TypeError, ValueError, IndexError):
                continue
        for cut in self.cuts:
            shown.setdefault(cut.start.isoformat(), cut.as_dict(zone, self.cuts_live))
        return {
            "mode": self.a_mode(config, pump),
            "gate": self.a_gate_state(),
            "cuts": [shown[key] for key in sorted(shown)],
            "tank": self.tank_c(pump) is not None,
            "strict": self.a_strict(),
        }

    def diagnostics(self) -> dict[str, Any]:
        config = self.shadow.config()
        pump = self._pump()
        route = self.route(config, pump)
        return {
            "route": None if route is None else route.as_dict(),
            "mode": self.mode(config, route, pump),
            "gate": self.gate_state(),
            "level_a": None
            if not self.a_applies(config)
            else {"mode": self.a_mode(config, pump), "gate": self.a_gate_state(), "cuts": [cut.as_dict(self.zone, self.cuts_live) for cut in self.cuts]},
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
