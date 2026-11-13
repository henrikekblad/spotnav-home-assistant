"""The house battery plan's shell: reads its inputs from Home Assistant, plans in the executor, keeps what the battery
really did and reviews each day the night after. The policy is pure (`plan_shadow.py`, `plan.py`).

Owned by the site's `BatteryShadow` (`runtime.py`), which calls `tick` every `runtime.TICK_S` with the battery's facts.

* **Each tick**: the quarter hour's meters (the site's net grid, the chargers' draw, the battery's power and SoC) go
  into its row (`PlanActuals`). When a quarter hour turns, completed hours become the plan's own house rows and the
  SoC read at their end goes into the site's forecast log (kind `battery`, beside the SoC the first plan expected);
  days that ended are reviewed.
* **Every `CHECK_S`**: whether to plan again (`plan_shadow.replan_trigger`). A plan reads the prices, the solar
  forecast and the cars' plans here, then builds and solves the plan in the executor. At most one runs at a time; a
  trigger while one runs is skipped (`skipped_busy`) and seen again at the next check.
* **Storage**: the quarter hours, the first planned actions, the house rows, the reviews and the plan in force (so a
  restart keeps the running slot's action stable) in `spotnav.battery_plan.<entry>`. The forecast log is the site's
  (`site/house_forecast.py`), saved with its hourly refresh.

Nothing here writes to a battery, an inverter or any other device: the plan is shown in the site's diagnostics and
the debug bundle only (`battery.plan`).
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import replace
from collections.abc import Sequence
from datetime import datetime, timedelta
from functools import partial
from typing import TYPE_CHECKING, Any, Final

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .. import time_zones
from ..const import (
    CONF_CHARGER_ENTRY_IDS,
    CONF_MAIN_FUSE_A,
    CONF_SAFETY_MARGIN_A,
    CONF_SOLAR_FORECAST_ENTRIES,
    DOMAIN,
)
from ..planning.demand_charge import HourMean, hour_start
from ..planning.grid_voltage import stored_voltage_between_phases_v
from ..site.load_forecast import forecast_house_load
from .facts import BatteryFacts
from .plan import plan_battery, self_consumption
from .plan_model import BatteryParams, PlanInputError, PlanRequest, PlanSlot
from .plan_shadow import (
    EST_HOUSE_COUNTS_BATTERY,
    EST_SOLAR_IN_HOUSE,
    FAILED,
    KIND_BATTERY,
    MIN_OWN_HOURS,
    NO_FEES,
    NO_HOUSE_FORECAST,
    NO_PRICES,
    REVIEW_DAYS,
    TRIGGER_SOC,
    DayReview,
    PlanActuals,
    PlannedSlot,
    PriceSlot,
    TriggerMemory,
    battery_params,
    bucket_start,
    build_slots,
    days_to_review,
    expected_soc_at,
    import_cap_kw,
    keep_min_gain,
    plan_view,
    planned_slots,
    price_slots,
    replan_trigger,
    review_day,
    review_totals,
    slot_at,
    soc_by_hour_end,
)

if TYPE_CHECKING:
    from ..site.site_capacity_controller import SiteCapacityController

_LOGGER = logging.getLogger(__name__)

STORE_VERSION: Final = 1
_SAVE_DELAY_S: Final = 60.0
#: How often the triggers are looked at (s).
CHECK_S: Final = 60.0
#: The quarter hours shown in the diagnostics beside the plan (the last day).
SHOWN_QUARTERS: Final = 96
#: Days of prices read: today and the next (the horizon ends with the last published one).
PRICE_DAYS: Final = 2
#: The variant that may sell from the battery runs only when the first plan took at most this long (s).
EXPORT_BUDGET_S: Final = 5.0
#: The hook for a credit per exported kWh (minor units, the grid operator's "nätnytta"): read from the site's entry
#: under this key, none by default. No setting offers it yet.
SELL_CREDIT_KEY: Final = "sell_credit_minor_per_kwh"

STATUS_WAITING: Final = "waiting"
STATUS_PLANNED: Final = "planned"
STATUS_NO_PLAN: Final = "no_plan"
HOUSE_OWN: Final = "own_rows"
HOUSE_SITE: Final = "site_forecast"


def store_key(entry_id: str) -> str:
    return f"{DOMAIN}.battery_plan.{entry_id}"


class _NoPlan(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _solve(
    now: datetime,
    tz: str,
    prices: Sequence[PriceSlot],
    house: dict[datetime, float] | None,
    own: tuple[list[HourMean], float | None, dict[datetime, float], frozenset[Any] | None] | None,
    solar_wh: dict[datetime, float] | None,
    car_windows: list[tuple[datetime, datetime, float]],
    cap_kw: float | None,
    params: BatteryParams,
    soc_pct: float,
    keep: tuple[str, float] | None,
    keep_export: tuple[str, float] | None,
) -> tuple[Any, Any, tuple[PlanSlot, ...], tuple[str, ...]]:
    """The executor's part: the plan's own house forecast (when it has its rows), the slots, the plan, and the same
    plan allowed to sell from the battery (`None` when the first took longer than `EXPORT_BUDGET_S`)."""
    started = time.monotonic()
    if own is not None:
        rows, flat_kw, temperatures, holidays = own
        forecast = forecast_house_load(rows, now, tz, flat_kw=flat_kw, temperatures=temperatures, holidays=holidays)
        house = forecast.by_hour()
    if not house:
        raise _NoPlan(NO_HOUSE_FORECAST)
    slots, estimates = build_slots(now, prices, house, solar_wh, car_windows, cap_kw, tz)
    if not slots:
        raise _NoPlan(NO_HOUSE_FORECAST if prices else NO_PRICES)
    request = PlanRequest(slots=slots, battery=params, soc_now_pct=min(100.0, max(0.0, soc_pct)))
    hours = sum(slot.hours for slot in slots)
    threshold = keep_min_gain(self_consumption(request).cost, hours)
    plan = plan_battery(request, keep=keep, keep_min_gain=threshold)
    export = None
    if time.monotonic() - started <= EXPORT_BUDGET_S:
        export = plan_battery(
            replace(request, allow_export_discharge=True), keep=keep_export, keep_min_gain=threshold
        )
    return plan, export, slots, estimates


class BatteryPlanShadow:
    """The site's battery plan in shadow (see the module)."""

    def __init__(self, hass: HomeAssistant, site: SiteCapacityController) -> None:
        self.hass = hass
        self.site = site
        self._store: Store[dict[str, Any]] = Store(hass, STORE_VERSION, store_key(site.entry_id))
        self.actuals = PlanActuals()
        self.reviews: list[DayReview] = []
        self.memory = TriggerMemory()
        #: The plan in force: its slots and the SoC it started from.
        self.plan: tuple[PlannedSlot, ...] = ()
        self.soc_at_plan: float | None = None
        self.view: dict[str, Any] | None = None
        #: The same for the variant that may sell from the battery (`None` while it did not run).
        self.plan_export: tuple[PlannedSlot, ...] = ()
        self.view_export: dict[str, Any] | None = None
        self.status = STATUS_WAITING
        self.reason: str | None = None
        self.trigger: str | None = None
        self.made_at: datetime | None = None
        self.inputs: dict[str, Any] = {}
        self.runs = 0
        self.skipped_busy = 0
        self.last_duration_s: float | None = None
        self._prices: list[PriceSlot] = []
        self._cap_kw: float | None = None
        self._facts = BatteryFacts()
        self._quarter: datetime | None = None
        self._checked_at: datetime | None = None
        self._running = False
        self._loaded = False
        self._closed = False

    # ---- life

    async def async_start(self, now: datetime) -> None:
        try:
            stored = await self._store.async_load()
        except Exception:  # noqa: BLE001 - a broken store starts again
            _LOGGER.warning("The battery plan's record could not be read; starting a new one")
            stored = None
        self._restore(stored, now)
        self._loaded = True

    async def async_shutdown(self) -> None:
        self._closed = True
        if self._loaded:
            await self._store.async_save(self.as_dict())

    @staticmethod
    async def async_remove_stored(hass: HomeAssistant, entry_id: str) -> None:
        await Store(hass, STORE_VERSION, store_key(entry_id)).async_remove()

    def as_dict(self) -> dict[str, Any]:
        return {
            "actuals": self.actuals.as_dict(),
            "reviews": [review.as_dict() for review in self.reviews],
            "plan": {
                "soc_at_plan": self.soc_at_plan,
                "slots": [slot.as_row() for slot in self.plan],
                "export_slots": [slot.as_row() for slot in self.plan_export],
            },
        }

    def _restore(self, raw: Any, now: datetime) -> None:
        if not isinstance(raw, dict):
            return
        self.actuals = PlanActuals.from_dict(raw.get("actuals"), now)
        reviews = [DayReview.from_dict(item) for item in raw.get("reviews") or []]
        self.reviews = sorted((r for r in reviews if r is not None), key=lambda r: r.money.day)[-REVIEW_DAYS:]
        plan = raw.get("plan") if isinstance(raw.get("plan"), dict) else {}
        slots = [PlannedSlot.from_row(row) for row in plan.get("slots") or []]
        exported = [PlannedSlot.from_row(row) for row in plan.get("export_slots") or []]
        if all(slot is not None for slot in exported):
            self.plan_export = tuple(slot for slot in exported if slot is not None)
        soc = plan.get("soc_at_plan")
        if all(slot is not None for slot in slots) and isinstance(soc, (int, float)) and not isinstance(soc, bool):
            # Kept for the running slot's stability only; a new plan is made at the first check.
            self.plan = tuple(slot for slot in slots if slot is not None)
            self.soc_at_plan = float(soc)

    def _save(self) -> None:
        self._store.async_delay_save(self.as_dict, _SAVE_DELAY_S)

    # ---- the tick

    @property
    def _tz(self) -> str:
        return self.site.demand.tracker.tz

    def tick(self, now: datetime, facts: BatteryFacts) -> None:
        """One look (see the module). Never raises into the battery's tick."""
        if not self._loaded or self._closed:
            return
        self._facts = facts
        if facts.battery_entity_id is None:
            self.status, self.reason = STATUS_NO_PLAN, "no_battery"
            return
        try:
            self._observe(now, facts)
            if self._checked_at is None or (now - self._checked_at).total_seconds() >= CHECK_S:
                self._checked_at = now
                self._check(now, facts)
        except Exception:  # noqa: BLE001 - the plan must never disturb the site
            _LOGGER.debug("SpotNav battery plan: the tick failed", exc_info=True)

    def _observe(self, now: datetime, facts: BatteryFacts) -> None:
        from ..execution.solar_execution import site_car_w, site_net_grid_w  # noqa: PLC0415 - it imports the site

        quarter = bucket_start(now)
        if quarter != self._quarter:
            self._quarter = quarter
            self._quarter_turned(now)
            price = next((p for p in self._prices if p.start <= quarter < p.end), None)
            self.actuals.open(
                quarter,
                price=price,
                cap_kw=self._cap_kw,
                ran=slot_at(self.plan, quarter),
                ran_export=slot_at(self.plan_export, quarter),
            )
        grid_w = site_net_grid_w(self.site)
        car_w = None if grid_w is None else site_car_w(self.hass, self.site)
        self.actuals.observe(
            now,
            None if grid_w is None else grid_w / 1000.0,
            None if car_w is None else car_w / 1000.0,
            facts.power_kw,
            facts.soc_pct,
        )

    def _quarter_turned(self, now: datetime) -> None:
        """Completed hours to house rows and the forecast log; days that ended reviewed; old rows dropped."""
        tz = self._tz
        log = self.site.house_forecast.log
        for hour in self.actuals.close_hours(now, tz):
            log.record_actual(KIND_BATTERY, hour, self.actuals.soc_at_hour_end(hour, tz))
        params, _reason, _estimates = battery_params(self._facts)
        if params is not None:
            reviewed = [review.money.day for review in self.reviews]
            for day in days_to_review(self.actuals.buckets.values(), reviewed, now, tz):
                review = review_day(day, self.actuals.buckets.values(), params, tz, now)
                if review is not None:
                    self.reviews.append(review)
                    _LOGGER.debug("SpotNav battery plan: %s reviewed, saving %.0f", day, review.saving)
            self.reviews = sorted(self.reviews, key=lambda r: r.money.day)[-REVIEW_DAYS:]
        self.actuals.prune(now)
        self._save()

    # ---- the triggers

    def _price_source(self) -> Any:
        from ..pricing.price_source import site_price_source  # noqa: PLC0415 - it imports the planner

        return site_price_source(self.hass, self.site.entry_id)

    def _price_days(self, source: Any, now: datetime) -> list[Any]:
        """The documents of today and the next day in the area's zone, as held now (nothing is fetched)."""
        from ..runtime import domain_data  # noqa: PLC0415

        repository = domain_data(self.hass).price_repository
        if repository is None or source is None or source.area is None:
            return []
        today = now.astimezone(time_zones.require_zone(source.area.tz)).date()
        documents = []
        for offset in range(PRICE_DAYS):
            document = repository.day_snapshot(source.area_id, today + timedelta(days=offset)).document
            if document is not None:
                documents.append(document)
        return documents

    def _prices_key(self, now: datetime) -> Any:
        """What changes when the prices or the fees do: the source's area and fees, and the documents held."""
        source = self._price_source()
        documents = self._price_days(source, now)
        return (
            None if source is None else (source.area_id, source.revision),
            tuple((doc.day, doc.interval_count, doc.retrieved, doc.published) for doc in documents),
        )

    def _read_prices(self, now: datetime) -> tuple[str, list[PriceSlot]]:
        """The area and its price slots with the fees, as a charger's plan prices them; `_NoPlan` without them."""
        from ..planning.planner import PlannerInputError  # noqa: PLC0415
        from ..pricing.price_points import price_points  # noqa: PLC0415 - it imports the planner

        source = self._price_source()
        if source is None or source.area is None:
            raise _NoPlan(NO_PRICES)
        fiscal = source.fiscal
        if fiscal is None:
            raise _NoPlan(NO_FEES)
        try:
            points = price_points(self._price_days(source, now), currency=source.area.currency, fiscal=fiscal)
        except PlannerInputError as err:
            raise _NoPlan(NO_PRICES) from err
        prices = price_slots(((p.start, p.end, p.spot) for p in points), fiscal, self._sell_credit())
        if not any(price.end > now for price in prices):
            raise _NoPlan(NO_PRICES)
        return source.area_id, prices

    def _sell_credit(self) -> float:
        """The credit per exported kWh (minor units): the hook `SELL_CREDIT_KEY`, none by default."""
        entry = self.hass.config_entries.async_get_entry(self.site.entry_id)
        value = (self.site.config if entry is None else entry.data).get(SELL_CREDIT_KEY)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            return 0.0
        return float(value)

    def _car_windows(self) -> list[tuple[datetime, datetime, float]]:
        """The site's chargers' installed plans: `(start, end, kW)` per window."""
        from ..runtime import controller_for  # noqa: PLC0415 - the charger side imports the site

        windows: list[tuple[datetime, datetime, float]] = []
        for charger_entry_id in self.site.config.get(CONF_CHARGER_ENTRY_IDS) or []:
            charger = controller_for(self.hass, charger_entry_id)
            plan = None if charger is None else getattr(charger, "plan", None)
            if plan is None:
                continue
            kw = plan.power_kw if plan.power_kw else plan.amps * plan.phases * 230.0 / 1000.0
            try:
                windows.extend((start, end, float(kw)) for start, end in plan.windows)
            except (TypeError, ValueError):
                continue
        return windows

    def _cap(self) -> float | None:
        from ..api.entity_fields import stored_import_target_kw  # noqa: PLC0415 - the API imports the site

        entry = self.hass.config_entries.async_get_entry(self.site.entry_id)
        config = self.site.config
        return import_cap_kw(
            config.get(CONF_MAIN_FUSE_A),
            config.get(CONF_SAFETY_MARGIN_A),
            stored_voltage_between_phases_v(config),
            stored_import_target_kw(config if entry is None else entry.data),
        )

    def _keys(self, now: datetime, facts: BatteryFacts) -> dict[str, Any]:
        windows = self._car_windows()
        return {
            "prices_key": self._prices_key(now),
            "forecast_key": self.site.house_forecast.refreshes,
            "hour": hour_start(now, self._tz),
            "inputs_key": (
                facts.soc_pct is not None,
                facts.capacity_kwh,
                facts.rated_charge_kw,
                facts.rated_discharge_kw,
                tuple(windows),
                self._cap(),
            ),
        }

    def _check(self, now: datetime, facts: BatteryFacts) -> None:
        keys = self._keys(now, facts)
        expected = None if self.soc_at_plan is None else expected_soc_at(self.plan, self.soc_at_plan, now)
        trigger = replan_trigger(self.memory, now, soc_now=facts.soc_pct, expected_soc=expected, **keys)
        if trigger is None:
            return
        if self._running:
            self.skipped_busy += 1
            return
        memory = self.memory
        memory.planned_at = now
        memory.prices_key, memory.forecast_key = keys["prices_key"], keys["forecast_key"]
        memory.hour, memory.inputs_key = keys["hour"], keys["inputs_key"]
        if trigger == TRIGGER_SOC:
            memory.soc_replan_at = now
        self._running = True
        self.hass.async_create_task(self.async_plan(now, trigger), "spotnav_battery_plan", eager_start=False)

    # ---- the plan

    async def async_plan(self, now: datetime, trigger: str) -> None:
        """Gather the inputs, solve in the executor, keep the plan. Never raises."""
        self._running = True
        started = time.monotonic()
        self.trigger = trigger
        try:
            await self._async_plan(now)
        except _NoPlan as no_plan:
            self.status, self.reason = STATUS_NO_PLAN, no_plan.reason
            _LOGGER.debug("SpotNav battery plan: no plan (%s)", no_plan.reason)
        except PlanInputError as err:
            self.status, self.reason = STATUS_NO_PLAN, f"invalid_input:{err.code}"
            _LOGGER.debug("SpotNav battery plan: the inputs were refused (%s)", err)
        except Exception:  # noqa: BLE001 - the plan must never disturb the site
            self.status, self.reason = STATUS_NO_PLAN, FAILED
            _LOGGER.debug("SpotNav battery plan: the plan failed", exc_info=True)
        finally:
            self._running = False
            self.runs += 1
            self.last_duration_s = round(time.monotonic() - started, 3)

    async def _async_plan(self, now: datetime) -> None:
        from ..planning.hybrid_forecast import async_read_forecast_wh  # noqa: PLC0415 - it imports the runtime

        facts = self._facts
        tz = self._tz
        params, reason, estimates = battery_params(facts)
        if params is None or facts.soc_pct is None:
            raise _NoPlan(reason or "no_soc")
        area_id, prices = self._read_prices(now)
        self._prices = prices
        cap = self._cap_kw = self._cap()

        entries = list(self.site.config.get(CONF_SOLAR_FORECAST_ENTRIES) or [])
        solar = None
        solar_sources: tuple[str, ...] = ()
        if entries:
            read = await async_read_forecast_wh(self.hass, entries, now=now)
            solar, solar_sources = read.wh_hours, read.sources_read

        own_rows = sorted(self.actuals.rows.items())
        house_source = HOUSE_OWN if len(own_rows) >= MIN_OWN_HOURS else HOUSE_SITE
        house: dict[datetime, float] | None = None
        own = None
        site_forecast = self.site.house_forecast.forecast
        if house_source == HOUSE_OWN:
            tracker = self.site.demand.tracker
            outdoor = {hour.start: hour.outdoor_c for hour in (*tracker.previous, *tracker.hours)}
            rows = [
                HourMean(start=start, kw=kw, house_kw=max(0.0, kw), outdoor_c=outdoor.get(start))
                for start, kw in own_rows
            ]
            recent = [kw for _start, kw in own_rows[-24:]]
            temperatures = {}
            if site_forecast is not None:
                temperatures = {h.start: h.outdoor_c for h in site_forecast.hours if h.outdoor_c is not None}
            own = (rows, max(0.0, sum(recent) / len(recent)), temperatures, self.site.house_forecast.holidays)
        else:
            estimates = (*estimates, EST_HOUSE_COUNTS_BATTERY)
            house = None if site_forecast is None else site_forecast.by_hour()
        if solar is not None:
            estimates = (*estimates, EST_SOLAR_IN_HOUSE)

        running = slot_at(self.plan, now)
        keep = None if running is None else (running.action, running.battery_kw)
        running_export = slot_at(self.plan_export, now)
        keep_export = None if running_export is None else (running_export.action, running_export.battery_kw)
        windows = self._car_windows()
        result, export, slots, more = await self.hass.async_add_executor_job(
            partial(_solve, now, tz, prices, house, own, solar, windows, cap, params, facts.soc_pct, keep, keep_export)
        )
        if self._closed:
            return
        decisions = result.plan.decisions
        self.plan = planned_slots(decisions)
        self.soc_at_plan = float(facts.soc_pct)
        self.actuals.note_first(self.plan, self.soc_at_plan)
        self.site.house_forecast.log.record_forecast(KIND_BATTERY, soc_by_hour_end(decisions, tz), now)
        self.view = plan_view(result, slots)
        self.plan_export = () if export is None else planned_slots(export.plan.decisions)
        self.view_export = None if export is None else plan_view(export, slots)
        self.status, self.reason, self.made_at = STATUS_PLANNED, None, now
        self.inputs = {
            "soc_pct": facts.soc_pct,
            "battery": {
                "usable_kwh": params.usable_kwh,
                "max_charge_kw": params.max_charge_kw,
                "max_discharge_kw": params.max_discharge_kw,
                "min_soc_pct": params.min_soc_pct,
                "round_trip_efficiency": params.round_trip_efficiency,
            },
            "price_area": area_id,
            "import_cap_kw": None if cap is None else round(cap, 3),
            "house": house_source,
            "own_hours": len(own_rows),
            "solar_sources": list(solar_sources),
            "car_windows": len(windows),
            "slots": len(slots),
            "sell_credit": self._sell_credit(),
            "horizon_end": slots[-1].end.isoformat(),
            "estimates": sorted(set(estimates) | set(more)),
        }
        self._save()

    # ---- what is shown

    def snapshot(self) -> dict[str, Any]:
        """`battery.plan` in the diagnostics: the plan, its inputs, the quarter hours beside it and the reviews."""
        management = self._facts.management.state
        quarters = sorted(self.actuals.buckets.values(), key=lambda bucket: bucket.start)[-SHOWN_QUARTERS:]
        return {
            "state": "shadow",
            # A battery that runs its own way is planned all the same, as a benchmark: nothing is written either way.
            "benchmark": management != "free",
            "management": management,
            "status": self.status,
            "reason": self.reason,
            "trigger": self.trigger,
            "made_at": None if self.made_at is None else self.made_at.isoformat(),
            "runs": self.runs,
            "skipped_busy": self.skipped_busy,
            "last_duration_s": self.last_duration_s,
            "inputs": dict(self.inputs),
            "plan": self.view,
            # The same plan allowed to sell from the battery (export discharge), on the same inputs.
            "plan_export": self.view_export,
            "quarters": [bucket.view() for bucket in quarters],
            # Per day, side by side: what the battery really did, the plan and its export variant on the same
            # measured load and sun, and self-consumption; and the days kept summed.
            "reviews": [review.as_dict() for review in self.reviews],
            "review_totals": review_totals(self.reviews),
        }
