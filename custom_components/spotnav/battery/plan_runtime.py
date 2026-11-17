"""The house battery plan's shell: reads its inputs from Home Assistant, plans in the executor, keeps what the battery
really did and reviews each day the night after. The policy is pure (`plan_shadow.py`, `plan.py`).

Owned by the site's `BatteryShadow` (`runtime.py`), which calls `tick` every `runtime.TICK_S` with the battery's facts.

* **Each tick**: the quarter hour's meters (the site's net grid, the chargers' draw, the battery's power and SoC) go
  into its row (`PlanActuals`). When a quarter hour turns, the SoC read at the end of each completed hour goes into
  the site's forecast log (kind `battery`, beside the SoC the first plan expected); days that ended are reviewed.
* **Every `CHECK_S`**: whether to plan again (`plan_shadow.replan_trigger`). A plan reads the prices, the solar
  forecast and the cars' plans here, then builds and solves the plan in the executor. At most one runs at a time; a
  trigger while one runs is skipped (`skipped_busy`) and seen again at the next check.
* **Modes**: the site's mode (entry key `MODE_KEY`, Balanced when unset; no setting yet) is planned first and is the
  plan in force; the other modes are planned on the same inputs while the solve stays within `MODES_BUDGET_S`, and
  are reviewed beside it each night. The wear is the area currency's constant (`plan_model.WEAR_MINOR_PER_KWH`).
* **Battery limits** (`limited_params`): the plant's maximum output where its adapter reads one and it is below the
  battery's rating (the owner's Sigenergy: 13.2 kW beside 20.6/23.04 kW), and the battery's own reserve as the floor
  where one can be read; otherwise the assumed floor, said to be (`min_soc_assumed`).
* **The house**: the site's own forecast (`site/house_forecast.py`), whose house figure on a battery site is the
  house's whole load (`grid - car - battery` plus the sun's production from the Energy dashboard's statistics), so
  neither the battery nor the sun is counted twice; the plan takes its median without the in-day adjustment
  (`battery_house_by_hour`, `house_forecast.MEDIAN_QUANTILE`). Until the site has enough hours of the whole load the
  figure is the import less the chargers, which counts the battery, and the plan says so (`house_counts_battery`);
  a whole load without the sun's production (no solar statistics) beside a solar forecast says so too
  (`solar_in_house_history`). The plan kept its own rows of `grid - car - battery` before the site did: they are
  handed to the site once (`SiteHouseForecast.async_take_rows`) and are no forecast of their own any more.
* **The sun's correction**: the plan counts on the solar forecast as the site's correction corrects it
  (`site/solar_correction_shadow.py`) once that has learnt; the raw forecast while it learns or is off
  (`corrected_solar_wh`, shown as `inputs.solar`). The plan is itself shadow, so no charger reads it.
* **The error band and the defensive variant** (`site/forecast_band.py`, `defensive.py`): at each plan the house's
  and the sun's bands are learnt from the site's forecast log (the misses of the median the plan counts on,
  `house_kw_median`, and of the sun as the plan counts on it, corrected or raw), shown as p10/median/p90 per hour
  (`band`), and the site's mode is planned once more on the pessimistic side (the house's p90 in the dear hours, the
  sun's p10) as `defensive`: kept per quarter hour and reviewed each night beside the modes, within the same CPU
  budget, never in force. Nothing changes for the car.
* **The perfect plan** (`hindsight.py`): each reviewed day, while its quarter hours are kept, is planned again in
  hindsight in the executor (`_async_facit`, one run at a time): the perfect plan, each way's share of it, the
  battery's work and, where the battery reports it, the cells' temperature over the day (the recorder's statistics).
  The energy left at midnight is valued at the next day's mean buy price once it is held (published at about 13:00
  on the day itself, so usually by the night's review): until then on the day's own end value, said to be (`basis`),
  and worked out again when it is. A day under 90 % measured gets its figures and no share. A failed try waits
  `FACIT_RETRY_S`.
* **Storage**: the quarter hours, the first planned actions, the house rows, the reviews and the plan in force (so a
  restart keeps the running slot's action stable) in `spotnav.battery_plan.<entry>`. The forecast log is the site's
  (`site/house_forecast.py`), saved with its hourly refresh.

Nothing here writes to a battery, an inverter or any other device: the plan is shown in the site's diagnostics and
the debug bundle only (`battery.plan`).
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections.abc import Coroutine, Sequence
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from functools import partial
from typing import TYPE_CHECKING, Any, Final, NamedTuple

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
from ..planning.demand_charge import hour_start
from ..planning.grid_voltage import stored_voltage_between_phases_v
from ..site.forecast_band import FORM_ADDITIVE, FORM_RATIO, BandView, ErrorBand, learn, pairs_from_log, view
from ..site.forecast_log import KIND_HOUSE_MEDIAN, KIND_SUN, KIND_SUN_CORRECTED
from ..site.house_forecast import BASIS_GROSS, MEDIAN_QUANTILE
from ..site.solar_correction import STATUS_SHADOW as CORRECTION_LEARNT, SolarCorrection
from .defensive import NO_BAND, DefensiveInputs, defensive_inputs
from .facts import BatteryFacts
from .hindsight import BASIS_DAY, BASIS_NEXT_DAY, CellTemperature, Facit, end_value_next_day, facit_day
from .plan import plan_mode, self_consumption
from .plan_model import (
    DEFAULT_CYCLE_COST,
    DEFAULT_MODE,
    MODE_DEFS,
    MODES,
    BatteryParams,
    PlanInputError,
    PlanRequest,
    PlanSlot,
    mode_named,
    wear_minor_per_kwh,
)
from .plan_shadow import (
    EST_HOUSE_COUNTS_BATTERY,
    EST_MIN_SOC,
    EST_SOLAR_IN_HOUSE,
    FAILED,
    KIND_BATTERY,
    NO_FEES,
    NO_HOUSE_FORECAST,
    NO_PRICES,
    REVIEW_DAYS,
    REVIEWED,
    TRIGGER_SOC,
    VARIANT_DEFENSIVE,
    DayReview,
    PlanActuals,
    PlannedSlot,
    PriceSlot,
    TriggerMemory,
    battery_params,
    bucket_start,
    build_slots,
    day_quarters,
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
    usable_buckets,
)
from .review_baselines import no_battery_cost, sun_by_quarter
from . import gate
from .learned import LearnedBattery, with_learned
from .stored_value import StoredValue

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
#: A day whose perfect plan could not be worked out is tried again after this long (s).
FACIT_RETRY_S: Final = 3600.0
#: The modes beside the site's own are planned only while the solve has taken at most this long (s); the ones left
#: are skipped and named (`modes_skipped`).
MODES_BUDGET_S: Final = 5.0
# The clock the modes budget is read from: wall-clock in production, replaced by tests so they do not depend on load.
budget_clock = time.monotonic
#: The old hidden hook for a credit per exported kWh (minor units), once read from the site's entry under this key.
#: What a sold kWh earns beyond spot is now the electricity price's own figure (`sell_adjustment`, set in the card);
#: a value still under this key is moved there once (`migrate_sell_credit`) and the key removed.
SELL_CREDIT_KEY: Final = "sell_credit_minor_per_kwh"
#: The site's battery mode (`plan_model.MODES`), read from the site's entry under this key; Balanced when unset or
#: unknown. No setting offers it yet.
MODE_KEY: Final = gate.CONF_MODE
#: Estimate markers of the modes: the mode key held something unknown; the currency has no wear figure.
EST_MODE_UNKNOWN: Final = "battery_mode_unknown"
EST_WEAR_CURRENCY: Final = "wear_currency_unknown"

STATUS_WAITING: Final = "waiting"
STATUS_PLANNED: Final = "planned"
STATUS_NO_PLAN: Final = "no_plan"
#: The defensive variant left out for the CPU budget.
DEFENSIVE_SKIPPED: Final = "skipped"
#: The plan's house: the site's forecast of the house's whole load, or (until the site has enough of its hours) of the
#: import less the chargers, which counts the battery.
HOUSE_GROSS: Final = "site_gross"
HOUSE_SITE: Final = "site_forecast"
#: What the whole load's hours are: with the sun's production added back, or net of the sun (no solar statistics).
ROWS_GROSS: Final = "gross"
ROWS_NET: Final = "net_of_sun"
#: What the plan's sun is: the forecast as the site's solar correction corrects it, or the forecast as read (the
#: correction still learning, off on a curtailed site, or no forecast).
SOLAR_CORRECTED: Final = "corrected"
SOLAR_RAW: Final = "raw"
#: The plan's house forecast: the profile's median without the in-day adjustment, made by the site
#: (`site/house_forecast.py`, where the backtest behind it is told).
HOUSE_QUANTILE: Final = MEDIAN_QUANTILE


def store_key(entry_id: str) -> str:
    return f"{DOMAIN}.battery_plan.{entry_id}"


def corrected_solar_wh(
    raw_wh: dict[datetime, float],
    correction: SolarCorrection,
    *,
    observed_max_kwh: float | None,
    array_kwp: float | None,
) -> tuple[dict[datetime, float], str]:
    """The solar forecast the plan counts on (Wh per UTC hour): [raw_wh] corrected by the site's correction
    (`site/solar_correction.py`) once it has learnt (`STATUS_SHADOW`), with its caps; as read while it learns or is
    off on a curtailed site. With which of the two it is.

    The backtest (`scripts/forecast_gap_backtest.py`, 2026-11-13): on the owner's site the raw Solcast p50 was
    optimistic, and the corrected forecast brought the plan within a few kronor a month of perfect sun."""
    if correction.status != CORRECTION_LEARNT:
        return raw_wh, SOLAR_RAW
    kwh = correction.correct(
        {start: wh / 1000.0 for start, wh in raw_wh.items()}, observed_max_kwh=observed_max_kwh, array_kwp=array_kwp
    )
    return {start: value * 1000.0 for start, value in kwh.items()}, SOLAR_CORRECTED


def limited_params(
    params: BatteryParams | None, reason: str | None, estimates: tuple[str, ...], facts: BatteryFacts
) -> tuple[BatteryParams | None, str | None, tuple[str, ...]]:
    """`plan_shadow.battery_params`' answer with what the battery's adapter reads of its limits: the plant's
    maximum when it is below the rating (`BatteryFacts.max_charge_kw`), and the battery's own reserve as the floor in
    place of the assumed one (which is then no estimate). A reserve at or above the ceiling is left out."""
    if params is None:
        return params, reason, estimates
    estimates = (*estimates, *(code for code in facts.estimates if code not in estimates))
    charge, discharge = facts.max_charge_kw, facts.max_discharge_kw
    params = replace(
        params,
        max_charge_kw=params.max_charge_kw if charge is None else max(0.0, min(params.max_charge_kw, charge)),
        max_discharge_kw=(
            params.max_discharge_kw if discharge is None else max(0.0, min(params.max_discharge_kw, discharge))
        ),
    )
    reserve = facts.reserve_soc_pct
    if reserve is not None and 0.0 <= reserve < params.max_soc_pct:
        params = replace(params, min_soc_pct=reserve)
        estimates = tuple(code for code in estimates if code != EST_MIN_SOC)
    return params, reason, estimates


def site_price_source_of(hass: HomeAssistant, site: SiteCapacityController) -> Any:
    """The site's electricity price entry (`pricing/price_source.site_price_source`), or `None`."""
    from ..pricing.price_source import site_price_source  # noqa: PLC0415 - it imports the planner

    return site_price_source(hass, site.entry_id)


def migrate_sell_credit(hass: HomeAssistant, site_entry_id: str) -> bool:
    """Move a value under the old hook `SELL_CREDIT_KEY` in the site's entry to its electricity price's
    `sell_adjustment`, on with that figure, and remove the key. A price whose figure is already set keeps its own; a
    value that is no finite number is dropped. Nothing happens while the site has no electricity price entry (the key
    waits for one). Whether the site's entry was changed."""
    from ..planning.auto_settings import FiscalOverride, GridTariff  # noqa: PLC0415
    from ..pricing.price_source import async_write_price_entry, site_price_source  # noqa: PLC0415

    site = hass.config_entries.async_get_entry(site_entry_id)
    if site is None or SELL_CREDIT_KEY not in site.data:
        return False
    source = site_price_source(hass, site_entry_id)
    price_entry = None if source is None or source.entry_id is None else hass.config_entries.async_get_entry(
        source.entry_id
    )
    if source is None or price_entry is None:
        return False
    value = site.data[SELL_CREDIT_KEY]
    usable = not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value) and value != 0
    tariff = source.overrides.grid_tariff or GridTariff()
    if usable and tariff.sell_adjustment == FiscalOverride():
        moved = replace(tariff, sell_adjustment=FiscalOverride(enabled=True, value=float(value)))
        async_write_price_entry(hass, price_entry, replace(source.overrides, grid_tariff=moved))
        _LOGGER.info("SpotNav moved the site's credit per sold kWh (%s) to its electricity price", value)
    hass.config_entries.async_update_entry(
        site, data={key: item for key, item in site.data.items() if key != SELL_CREDIT_KEY}
    )
    return True


def price_days(hass: HomeAssistant, source: Any, now: datetime, days: int = PRICE_DAYS) -> list[Any]:
    """The price documents of today and the next `days - 1` days in the source's area zone, as held now (nothing is
    fetched); none without a source or an area."""
    from ..runtime import domain_data  # noqa: PLC0415

    repository = domain_data(hass).price_repository
    if repository is None or source is None or source.area is None:
        return []
    today = now.astimezone(time_zones.require_zone(source.area.tz)).date()
    documents = []
    for offset in range(days):
        document = repository.day_snapshot(source.area_id, today + timedelta(days=offset)).document
        if document is not None:
            documents.append(document)
    return documents


def site_car_windows(hass: HomeAssistant, site: SiteCapacityController) -> list[tuple[datetime, datetime, float]]:
    """The site's chargers' installed plans: `(start, end, kW)` per window."""
    from ..runtime import controller_for  # noqa: PLC0415 - the charger side imports the site

    windows: list[tuple[datetime, datetime, float]] = []
    for charger_entry_id in site.config.get(CONF_CHARGER_ENTRY_IDS) or []:
        charger = controller_for(hass, charger_entry_id)
        plan = None if charger is None else getattr(charger, "plan", None)
        if plan is None:
            continue
        kw = plan.power_kw if plan.power_kw else plan.amps * plan.phases * 230.0 / 1000.0
        try:
            windows.extend((start, end, float(kw)) for start, end in plan.windows)
        except (TypeError, ValueError):
            continue
    return windows


def site_import_cap_kw(hass: HomeAssistant, site: SiteCapacityController) -> float | None:
    """The most the site may import (kW, `plan_shadow.import_cap_kw`): the main fuse less its margin, and the import
    target's held level where one is set."""
    from ..api.entity_fields import stored_import_target_kw  # noqa: PLC0415 - the API imports the site

    entry = hass.config_entries.async_get_entry(site.entry_id)
    config = site.config
    return import_cap_kw(
        config.get(CONF_MAIN_FUSE_A),
        config.get(CONF_SAFETY_MARGIN_A),
        stored_voltage_between_phases_v(config),
        stored_import_target_kw(config if entry is None else entry.data),
    )


class _NoPlan(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def learn_bands(
    now: datetime,
    tz: str,
    house_pairs: list[tuple[datetime, float, float]],
    sun_pairs: list[tuple[datetime, float, float]],
    holidays: frozenset[date] | None,
) -> tuple[ErrorBand | None, ErrorBand | None]:
    """The house's and the sun's error bands from their logged misses (`site/forecast_band.py`)."""
    return (
        learn(house_pairs, now, tz, FORM_ADDITIVE, holidays=holidays),
        learn(sun_pairs, now, tz, FORM_RATIO),
    )


class Solved(NamedTuple):
    """The executor's answer: each mode's plan, the modes skipped, the slots, what was assumed, the bands and the
    defensive variant (its inputs, slots and plan; `None` when not planned)."""

    plans: dict[str, Any]
    skipped: tuple[str, ...]
    slots: tuple[PlanSlot, ...]
    estimates: tuple[str, ...]
    house_band: ErrorBand | None = None
    sun_band: ErrorBand | None = None
    defensive_inputs: DefensiveInputs | None = None
    defensive_slots: tuple[PlanSlot, ...] = ()
    defensive: Any = None


def _solve(
    now: datetime,
    tz: str,
    prices: Sequence[PriceSlot],
    house: dict[datetime, float],
    solar_wh: dict[datetime, float] | None,
    car_windows: list[tuple[datetime, datetime, float]],
    cap_kw: float | None,
    params: BatteryParams,
    soc_pct: float,
    mode: str,
    wear: float,
    keeps: dict[str, tuple[str, float] | None],
    misses: tuple[list[tuple[datetime, float, float]], list[tuple[datetime, float, float]], frozenset[date] | None]
    | None = None,
) -> Solved:
    """The executor's part: the slots, and each mode's plan, the site's [mode] first; then, from the logged
    [misses] (house, sun, holidays), the error bands and the defensive variant. What is left when the solve took
    longer than `MODES_BUDGET_S` is skipped and named."""
    started = budget_clock()
    if not house:
        raise _NoPlan(NO_HOUSE_FORECAST)
    slots, estimates = build_slots(now, prices, house, solar_wh, car_windows, cap_kw, tz)
    if not slots:
        raise _NoPlan(NO_HOUSE_FORECAST if prices else NO_PRICES)
    request = PlanRequest(slots=slots, battery=params, soc_now_pct=min(100.0, max(0.0, soc_pct)))
    hours = sum(slot.hours for slot in slots)
    baseline = self_consumption(request)
    threshold = keep_min_gain(baseline.cost, hours)
    plans: dict[str, Any] = {}
    skipped: list[str] = []
    for name in (mode, *(other for other in MODES if other != mode)):
        # Self-consumption costs nothing (it is the baseline): never skipped.
        if plans and MODE_DEFS[name].plans and budget_clock() - started > MODES_BUDGET_S:
            skipped.append(name)
            continue
        plans[name] = plan_mode(
            request, MODE_DEFS[name], wear, keep=keeps.get(name), keep_min_gain=threshold, baseline=baseline
        )
    if misses is None:
        return Solved(plans, tuple(skipped), slots, estimates)
    house_band, sun_band = learn_bands(now, tz, *misses)
    inputs = defensive_inputs(house, solar_wh, house_band, sun_band, prices, now, tz)
    if inputs is None:
        return Solved(plans, tuple(skipped), slots, estimates, house_band, sun_band)
    if budget_clock() - started > MODES_BUDGET_S:
        return Solved(plans, (*skipped, VARIANT_DEFENSIVE), slots, estimates, house_band, sun_band, inputs)
    guarded, _more = build_slots(now, prices, inputs.house_by_hour, inputs.solar_wh_by_hour, car_windows, cap_kw, tz)
    if not guarded:
        return Solved(plans, tuple(skipped), slots, estimates, house_band, sun_band, inputs)
    request = PlanRequest(slots=guarded, battery=params, soc_now_pct=min(100.0, max(0.0, soc_pct)))
    baseline = self_consumption(request)
    defensive = plan_mode(
        request,
        MODE_DEFS[mode],
        wear,
        keep=keeps.get(VARIANT_DEFENSIVE),
        keep_min_gain=keep_min_gain(baseline.cost, sum(slot.hours for slot in guarded)),
        baseline=baseline,
    )
    return Solved(plans, tuple(skipped), slots, estimates, house_band, sun_band, inputs, guarded, defensive)


def _mode_view(plan: Any, slots: Sequence[PlanSlot]) -> dict[str, Any]:
    """A mode beside the site's in the diagnostics: the whole view for a mode that plans, the totals alone for
    Self-consumption (its slots are every view's baseline already)."""
    if plan.plan is not plan.baseline:
        return plan_view(plan, slots)
    return {
        "cost": round(plan.baseline.cost, 2),
        "end_soc_pct": round(plan.baseline.end_soc_pct, 1),
        "savings": 0.0,
        "baseline": True,
    }


def work_facit(
    usable: Sequence[Any],
    battery: BatteryParams,
    review: DayReview,
    prices: Sequence[PriceSlot],
    end: datetime,
    next_end: datetime,
    day_quarters: int,
    wear: float,
    now: datetime,
) -> Facit | None:
    """The executor's part of a day's perfect plan (`hindsight.py`): the energy left at midnight valued at the next
    day's mean buy price where held, else at the review's own end value; then the plans and the shares."""
    value = end_value_next_day(prices, end, next_end, battery)
    return facit_day(
        usable,
        battery,
        start_soc_pct=review.start_soc_pct,
        actual_cost=review.actual.cost,
        actual_end_soc_pct=review.actual.end_soc_pct,
        mode=review.mode,
        wear=wear,
        end_value=review.end_value if value is None else value,
        basis=BASIS_DAY if value is None else BASIS_NEXT_DAY,
        no_battery_cost=no_battery_cost(usable),
        day_quarters=day_quarters,
        now=now,
    )


class BatteryPlanShadow:
    """The site's battery plan in shadow (see the module)."""

    def __init__(self, hass: HomeAssistant, site: SiteCapacityController) -> None:
        self.hass = hass
        self.site = site
        self._store: Store[dict[str, Any]] = Store(hass, STORE_VERSION, store_key(site.entry_id))
        self.actuals = PlanActuals()
        self.reviews: list[DayReview] = []
        #: The value of the energy in the battery and each day's money split (`stored_value.py`).
        self.stored = StoredValue()
        #: The battery's size and rated power learnt from the quarter hours (`learned.py`), for a battery that does
        #: not say them; persisted.
        self.learned = LearnedBattery()
        self.memory = TriggerMemory()
        #: The plan in force: its slots and the SoC it started from.
        self.plan: tuple[PlannedSlot, ...] = ()
        self.soc_at_plan: float | None = None
        self.view: dict[str, Any] | None = None
        #: The site's mode (the plan in force), and every mode's plan and view as last planned (a mode skipped for
        #: the CPU budget has none).
        self.mode: str = DEFAULT_MODE
        self.mode_plans: dict[str, tuple[PlannedSlot, ...]] = {}
        self.mode_views: dict[str, dict[str, Any]] = {}
        self.modes_skipped: tuple[str, ...] = ()
        #: The learnt error bands over the plan's hours, and the defensive variant's inputs and view.
        self.bands: dict[str, BandView] = {}
        self.defensive_inputs: DefensiveInputs | None = None
        self.defensive_view: dict[str, Any] | None = None
        self.status = STATUS_WAITING
        self.reason: str | None = None
        self.trigger: str | None = None
        self.made_at: datetime | None = None
        self.inputs: dict[str, Any] = {}
        self.runs = 0
        self.skipped_busy = 0
        self.last_duration_s: float | None = None
        self._prices: list[PriceSlot] = []
        #: The price slots read for the export-limit rule (`sell_at`), once a quarter hour, with or without a plan.
        self._sell_prices: list[PriceSlot] = []
        self._sell_quarter: datetime | None = None
        self._cap_kw: float | None = None
        self._facts = BatteryFacts()
        self._quarter: datetime | None = None
        self._checked_at: datetime | None = None
        self._running = False
        #: Whether the sun's production for the reviews is being read (`_async_sun`).
        self._sun_running = False
        #: Whether a day's perfect plan is being worked out (`_async_facit`), and when each day was last tried.
        self._facit_running = False
        self._facit_tried: dict[date, datetime] = {}
        self._loaded = False
        self._closed = False
        #: The plan's and the sun's tasks in flight, cancelled at the shutdown.
        self._tasks: set[asyncio.Task[None]] = set()

    # ---- life

    async def async_start(self, now: datetime) -> None:
        try:
            stored = await self._store.async_load()
        except Exception:  # noqa: BLE001 - a broken store starts again
            _LOGGER.warning("The battery plan's record could not be read; starting a new one")
            stored = None
        try:
            self._restore(stored, now)
        except Exception:  # noqa: BLE001 - a record SpotNav cannot read must not stop the site; it starts again
            _LOGGER.warning("The battery plan's record could not be read; starting a new one", exc_info=True)
            self._reset_record()
        self._loaded = True
        try:
            migrate_sell_credit(self.hass, self.site.entry_id)
        except Exception:  # noqa: BLE001 - a hook value left in place prices nothing; it is tried again next start
            _LOGGER.warning("SpotNav could not move the site's credit per sold kWh to its electricity price")

    async def async_shutdown(self) -> None:
        self._closed = True
        for task in list(self._tasks):
            task.cancel()
        if self._loaded:
            await self._store.async_save(self.as_dict())

    @staticmethod
    async def async_remove_stored(hass: HomeAssistant, entry_id: str) -> None:
        await Store(hass, STORE_VERSION, store_key(entry_id)).async_remove()

    def as_dict(self) -> dict[str, Any]:
        return {
            "actuals": self.actuals.as_dict(),
            "reviews": [review.as_dict() for review in self.reviews],
            "stored_value": self.stored.as_dict(),
            "learned": self.learned.as_dict(),
            "plan": {
                "soc_at_plan": self.soc_at_plan,
                "slots": [slot.as_row() for slot in self.plan],
                "mode": self.mode,
                "mode_slots": {mode: [slot.as_row() for slot in slots] for mode, slots in self.mode_plans.items()},
            },
        }

    def _restore(self, raw: Any, now: datetime) -> None:
        if not isinstance(raw, dict):
            return
        self.actuals = PlanActuals.from_dict(raw.get("actuals"), now)
        self.stored = StoredValue.from_dict(raw.get("stored_value"))
        self.learned = LearnedBattery.from_dict(raw.get("learned"))
        stored_reviews = raw.get("reviews")
        reviews = [DayReview.from_dict(item) for item in stored_reviews] if isinstance(stored_reviews, list) else []
        self.reviews = sorted((r for r in reviews if r is not None), key=lambda r: r.day)[-REVIEW_DAYS:]
        plan = raw.get("plan") if isinstance(raw.get("plan"), dict) else {}
        stored_slots = plan.get("slots")
        slots = [PlannedSlot.from_row(row) for row in stored_slots] if isinstance(stored_slots, list) else []
        # A record from before the modes kept an export variant (`export_slots`): not a mode, dropped.
        mode_slots = plan.get("mode_slots") if isinstance(plan.get("mode_slots"), dict) else {}
        for mode, rows in mode_slots.items():
            kept = [PlannedSlot.from_row(row) for row in rows or []] if isinstance(rows, list) else [None]
            if mode in REVIEWED and all(slot is not None for slot in kept):
                self.mode_plans[mode] = tuple(slot for slot in kept if slot is not None)
        if mode_named(plan.get("mode")) is not None:
            self.mode = plan["mode"]
        soc = plan.get("soc_at_plan")
        if all(slot is not None for slot in slots) and isinstance(soc, (int, float)) and not isinstance(soc, bool):
            # Kept for the running slot's stability only; a new plan is made at the first check.
            self.plan = tuple(slot for slot in slots if slot is not None)
            self.soc_at_plan = float(soc)

    def _reset_record(self) -> None:
        """The restored parts set back to a new record's (after a record that could not be read)."""
        self.actuals = PlanActuals()
        self.reviews = []
        self.stored = StoredValue()
        self.learned = LearnedBattery()
        self.plan = ()
        self.soc_at_plan = None
        self.mode = DEFAULT_MODE
        self.mode_plans = {}

    def _save(self) -> None:
        if self._closed:
            # Closed (unloaded or removed): the shutdown wrote the record, and a removed site's must not come back.
            return
        self._store.async_delay_save(self.as_dict, _SAVE_DELAY_S)

    def _start(self, job: Coroutine[Any, Any, None], name: str) -> None:
        """A task kept until it ends, so the shutdown can cancel it."""
        task = self.hass.async_create_task(job, name, eager_start=False)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # ---- the tick

    @property
    def _tz(self) -> str:
        return self.site.demand.tracker.tz

    def tick(self, now: datetime, facts: BatteryFacts) -> None:
        """One look (see the module). Never raises into the battery's tick."""
        if not self._loaded or self._closed:
            return
        if facts.battery_entity_id is not None:
            facts = with_learned(facts, self.learned)
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
                ran_modes={mode: slot_at(slots, quarter) for mode, slots in self.mode_plans.items()},
            )
        grid_w = site_net_grid_w(self.site)
        car_w = None if grid_w is None else site_car_w(self.hass, self.site, idle_unread_as_zero=True)
        self.actuals.observe(
            now,
            None if grid_w is None else grid_w / 1000.0,
            None if car_w is None else car_w / 1000.0,
            facts.power_kw,
            facts.soc_pct,
        )

    def _controlled(self) -> bool:
        """Whether SpotNav controls the battery now (`BatteryControl.state` `on`); false where it cannot be read."""
        control = getattr(self.site, "battery_control", None)
        if control is None:
            return False
        try:
            return control.state() == "on"
        except Exception:  # noqa: BLE001 - a state that cannot be read is no control
            return False

    def _quarter_turned(self, now: datetime) -> None:
        """Completed hours to house rows and the forecast log; days that ended reviewed; old rows dropped."""
        tz = self._tz
        log = self.site.house_forecast.log
        for hour in self.actuals.close_hours(now, tz):
            log.record_actual(KIND_BATTERY, hour, self.actuals.soc_at_hour_end(hour, tz))
        if self._facts.battery_entity_id is not None and self.learned.learn(self.actuals.buckets.values(), now):
            _LOGGER.debug("SpotNav battery plan: learnt %s", self.learned.view())
        params, _reason, _estimates = limited_params(*battery_params(self._facts), self._facts)
        # The stored energy's value and each day's money (`stored_value.py`), from the quarter hours completed.
        usable = params.usable_kwh if params is not None else self._facts.capacity_kwh
        today = now.astimezone(time_zones.require_zone(tz)).date()
        buys = [p.buy for p in self._prices if p.start.astimezone(time_zones.require_zone(tz)).date() == today]
        self.stored.run(
            [bucket for bucket in self.actuals.buckets.values() if bucket.end <= now],
            tz,
            usable,
            sum(buys) / len(buys) if buys else None,
            controlled=self._controlled(),
        )
        if params is not None:
            reviewed = [review.day for review in self.reviews]
            planning = [*(name for name in MODES if MODE_DEFS[name].plans), VARIANT_DEFENSIVE]
            for day in days_to_review(self.actuals.buckets.values(), reviewed, now, tz):
                review = review_day(day, self.actuals.buckets.values(), params, tz, now, planning, self.mode)
                if review is not None:
                    self.reviews.append(review)
                    _LOGGER.debug("SpotNav battery plan: %s reviewed, saving %.0f", day, review.saving)
            self.reviews = sorted(self.reviews, key=lambda r: r.day)[-REVIEW_DAYS:]
            self._ask_sun(now)
            self._ask_facit(now)
        self.actuals.prune(now)
        self._save()

    def _ask_sun(self, now: datetime) -> None:
        """Read what the sun gave for the reviews that wait for its production (one read at a time)."""
        waiting = [review.day for review in self.reviews if review.sun_quarters is None]
        if not waiting or self._sun_running or self._closed:
            return
        self._sun_running = True
        self._start(self._async_sun(waiting), "spotnav_battery_review_sun")

    async def _async_read_sun(self, first: datetime, end: datetime) -> dict[datetime, float] | None:
        """The sun's production per UTC quarter hour from the Energy dashboard's statistics (five minutes where the
        recorder has them, else the hour); `None` without solar statistics. Raises when they cannot be read."""
        from ..site.energy_statistics import async_energy_ids, async_statistics, summed  # noqa: PLC0415

        ids, _export = await async_energy_ids(self.hass)
        if not ids:
            return None
        hourly = summed(await async_statistics(self.hass, ids, first, end, "hour"), ids)
        five = summed(await async_statistics(self.hass, ids, first, end, "5minute"), ids)
        return sun_by_quarter(five, hourly)

    async def _async_sun(self, days: list[date]) -> None:
        """Fill the waiting reviews' sun figures (`review_baselines.py`); a failed read waits for the next quarter
        hour, the quarter hours being kept only a few days."""
        try:
            tz = self._tz
            zone = time_zones.require_zone(tz)
            first = datetime(days[0].year, days[0].month, days[0].day, tzinfo=zone).astimezone(timezone.utc)
            last = days[-1] + timedelta(days=1)
            end = datetime(last.year, last.month, last.day, tzinfo=zone).astimezone(timezone.utc)
            sun = await self._async_read_sun(first, end)
            filled: dict[date, DayReview] = {}
            for review in self.reviews:
                if review.day not in days or review.sun_quarters is not None:
                    continue
                usable = usable_buckets(self.actuals.buckets.values(), review.day, tz)
                filled[review.day] = review.with_sun(usable, sun or {}) if usable else replace(review, sun_quarters=0)
            if filled:
                self.reviews = [filled.get(review.day, review) for review in self.reviews]
                self._save()
        except Exception:  # noqa: BLE001 - the sun's figure waits for the next try
            _LOGGER.debug("SpotNav battery plan: the sun's production could not be read", exc_info=True)
        finally:
            self._sun_running = False

    # ---- the perfect plan

    def _facit_due(self, review: DayReview, now: datetime) -> bool:
        """Whether [review]'s day wants its perfect plan worked out (again) now."""
        if review.facit is not None and review.facit.basis == BASIS_NEXT_DAY:
            return False
        tried = self._facit_tried.get(review.day)
        if tried is not None and (now - tried).total_seconds() < FACIT_RETRY_S:
            return False
        if not usable_buckets(self.actuals.buckets.values(), review.day, self._tz):
            return False  # its quarter hours are gone (kept three days): as it is
        # A day worked out on its own end value waits for the next day's prices.
        return review.facit is None or self._prices_after_held(review.day)

    def _ask_facit(self, now: datetime) -> None:
        """Work out the perfect plan of the first reviewed day that wants one (one run at a time)."""
        if self._facit_running or self._closed:
            return
        review = next((item for item in self.reviews if self._facit_due(item, now)), None)
        if review is None:
            return
        self._facit_tried[review.day] = now
        self._facit_running = True
        self._start(self._async_facit(review.day, now), "spotnav_battery_facit")

    def _day_bounds(self, day: date) -> tuple[datetime, datetime]:
        zone = time_zones.require_zone(self._tz)
        following = day + timedelta(days=1)
        return (
            datetime(day.year, day.month, day.day, tzinfo=zone).astimezone(timezone.utc),
            datetime(following.year, following.month, following.day, tzinfo=zone).astimezone(timezone.utc),
        )

    def _prices_after(self, day: date) -> list[PriceSlot]:
        """The buy prices with the fees of the day after [day], as held (nothing is fetched); empty without them."""
        from ..pricing.price_points import price_points  # noqa: PLC0415 - it imports the planner
        from ..runtime import domain_data  # noqa: PLC0415

        source = self._price_source()
        repository = domain_data(self.hass).price_repository
        if source is None or source.area is None or source.fiscal is None or repository is None:
            return []
        documents = []
        for offset in (1,):
            document = repository.day_snapshot(source.area_id, day + timedelta(days=offset)).document
            if document is None:
                return []
            documents.append(document)
        try:
            points = price_points(documents, currency=source.area.currency, fiscal=source.fiscal)
        except Exception:  # noqa: BLE001 - prices that cannot be read are no prices
            return []
        return price_slots(((p.start, p.end, p.spot) for p in points), source.fiscal)

    def _prices_after_held(self, day: date) -> bool:
        _start, next_end = self._day_bounds(day + timedelta(days=1))
        prices = self._prices_after(day)
        return bool(prices) and max(slot.end for slot in prices) >= next_end - timedelta(minutes=1)

    async def _async_cell_temperature(self, day: date) -> CellTemperature | None:
        """The day's cell temperature from the recorder's statistics, where the battery reports one."""
        from ..site.energy_statistics import async_mean_max_statistics  # noqa: PLC0415
        from .hindsight import cell_temperature  # noqa: PLC0415

        entity_id = self._facts.cell_temperature_entity
        if entity_id is None:
            return None
        start, end = self._day_bounds(day)
        rows, step_min = await async_mean_max_statistics(self.hass, entity_id, start, end)
        return cell_temperature(rows, step_min)

    async def _async_facit(self, day: date, now: datetime) -> None:
        """[day]'s perfect plan, worked out in the executor, kept beside its review. Never raises."""
        try:
            review = next((item for item in self.reviews if item.day == day), None)
            params, _reason, _estimates = limited_params(*battery_params(self._facts), self._facts)
            usable = usable_buckets(self.actuals.buckets.values(), day, self._tz)
            if review is None or params is None or not usable:
                return
            _start, end = self._day_bounds(day)
            _end, next_end = self._day_bounds(day + timedelta(days=1))
            prices = self._prices_after(day)
            wear, _unknown = self._wear()
            try:
                temperature = await self._async_cell_temperature(day)
            except Exception:  # noqa: BLE001 - the temperature is a bonus
                _LOGGER.debug("SpotNav battery plan: the cell temperature could not be read", exc_info=True)
                temperature = review.cell_temperature
            made: Facit | None = await self.hass.async_add_executor_job(
                partial(work_facit, usable, params, review, prices, end, next_end, day_quarters(day, self._tz), wear, now)
            )
            if made is None:
                return
            self._facit_tried.pop(day, None)  # worked out: a later try waits only for the prices
            self.reviews = [
                replace(item, facit=made, cell_temperature=temperature or item.cell_temperature)
                if item.day == day
                else item
                for item in self.reviews
            ]
            _LOGGER.debug("SpotNav battery plan: %s against its perfect plan (%s): %s", day, made.basis, made.saving)
            self._save()
        except Exception:  # noqa: BLE001 - the perfect plan waits for the next try
            _LOGGER.debug("SpotNav battery plan: the perfect plan could not be worked out", exc_info=True)
        finally:
            self._facit_running = False

    # ---- the triggers

    def _price_source(self) -> Any:
        return site_price_source_of(self.hass, self.site)

    def _price_days(self, source: Any, now: datetime) -> list[Any]:
        """The documents of today and the next day in the area's zone, as held now (nothing is fetched)."""
        return price_days(self.hass, source, now)

    def _prices_key(self, now: datetime) -> Any:
        """What changes when the prices or the fees do: the source's area and fees, and the documents held."""
        source = self._price_source()
        documents = self._price_days(source, now)
        return (
            None if source is None else (source.area_id, source.revision),
            tuple((doc.day, doc.interval_count, doc.retrieved, doc.published) for doc in documents),
        )

    def buy_slots(self, now: datetime) -> list[PriceSlot]:
        """The price slots with every fee as the plan reads them, from the prices held (nothing is fetched); empty
        without prices or fees. The heat card's shadow rule and cost read them."""
        try:
            return self._read_prices(now)[1]
        except _NoPlan:
            return []

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
        prices = price_slots(((p.start, p.end, p.spot) for p in points), fiscal)
        if not any(price.end > now for price in prices):
            raise _NoPlan(NO_PRICES)
        return source.area_id, prices

    def sell_at(self, now: datetime) -> float | None:
        """What an exported kWh earns now (minor per kWh, the credit included), `None` when it is not known. Read once
        a quarter hour from the prices held (nothing is fetched), so the export-limit rule works without a battery."""
        quarter = bucket_start(now)
        if quarter != self._sell_quarter:
            self._sell_quarter = quarter
            try:
                _area, self._sell_prices = self._read_prices(now)
            except _NoPlan:
                self._sell_prices = []
            except Exception:  # noqa: BLE001 - the rule must never disturb the site
                _LOGGER.debug("SpotNav export limit: the prices could not be read", exc_info=True)
                self._sell_prices = []
        return next((price.sell for price in self._sell_prices if price.start <= now < price.end), None)

    def _sell_credit(self) -> float:
        """What a sold kWh earns beyond spot (minor units, no VAT): the electricity price's `sell_adjustment`, none
        without one or while the fees cannot be resolved."""
        source = self._price_source()
        fiscal = None if source is None else source.fiscal
        return 0.0 if fiscal is None else fiscal.sell_adjustment

    def _site_mode(self) -> tuple[str, bool]:
        """The site's mode (`MODE_KEY`) and whether the key held something unknown (then Balanced)."""
        entry = self.hass.config_entries.async_get_entry(self.site.entry_id)
        value = (self.site.config if entry is None else entry.data).get(MODE_KEY)
        if value is None:
            return DEFAULT_MODE, False
        mode = mode_named(value)
        return (DEFAULT_MODE, True) if mode is None else (mode.name, False)

    def _wear(self) -> tuple[float, bool]:
        """The wear per kWh each way in the area's currency, and whether the currency has none (then the default)."""
        source = self._price_source()
        currency = None if source is None or source.area is None else source.area.currency
        wear = wear_minor_per_kwh(currency)
        return (DEFAULT_CYCLE_COST, True) if wear is None else (wear, False)

    def _car_windows(self) -> list[tuple[datetime, datetime, float]]:
        return site_car_windows(self.hass, self.site)

    def _cap(self) -> float | None:
        return site_import_cap_kw(self.hass, self.site)

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
                self._site_mode(),
            ),
        }

    def mode_changed(self, now: datetime) -> None:
        """The site's mode was written: plan again at once, not at the next check."""
        if not self._loaded or self._closed or self._facts.battery_entity_id is None:
            return
        try:
            self._checked_at = now
            self._check(now, self._facts)
        except Exception:  # noqa: BLE001 - the plan must never disturb the site
            _LOGGER.debug("SpotNav battery plan: the mode change could not start a plan", exc_info=True)

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
        self._start(self.async_plan(now, trigger), "spotnav_battery_plan")

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
        params, reason, estimates = limited_params(*battery_params(facts), facts)
        if params is None or facts.soc_pct is None:
            raise _NoPlan(reason or "no_soc")
        area_id, prices = self._read_prices(now)
        self._prices = prices
        cap = self._cap_kw = self._cap()

        entries = list(self.site.config.get(CONF_SOLAR_FORECAST_ENTRIES) or [])
        solar = None
        solar_sources: tuple[str, ...] = ()
        solar_basis: str | None = None
        if entries:
            read = await async_read_forecast_wh(self.hass, entries, now=now)
            solar, solar_sources = read.wh_hours, read.sources_read
        if solar is not None:
            sun = self.site.solar_correction
            solar, solar_basis = corrected_solar_wh(
                solar, sun.state, observed_max_kwh=sun.observed_max_kwh, array_kwp=sun.array_kwp
            )

        site_house = self.site.house_forecast
        if self.actuals.rows and site_house.history.gross_migrated_at is None:
            # The plan's own rows of before, handed to the site once: its whole load starts with them.
            await site_house.async_take_rows(sorted(self.actuals.rows.items()), now)
        house, basis, with_sun = site_house.battery_house_by_hour()
        house_source = HOUSE_GROSS if basis == BASIS_GROSS else HOUSE_SITE
        rows_basis = (ROWS_GROSS if with_sun else ROWS_NET) if house_source == HOUSE_GROSS else None
        if house_source == HOUSE_SITE:
            estimates = (*estimates, EST_HOUSE_COUNTS_BATTERY)
        if solar is not None and rows_basis != ROWS_GROSS:
            estimates = (*estimates, EST_SOLAR_IN_HOUSE)

        mode, mode_unknown = self._site_mode()
        wear, wear_unknown = self._wear()
        if mode_unknown:
            estimates = (*estimates, EST_MODE_UNKNOWN)
        if wear_unknown:
            estimates = (*estimates, EST_WEAR_CURRENCY)
        keeps: dict[str, tuple[str, float] | None] = {}
        for name in REVIEWED:
            running = slot_at(self.plan if name == self.mode else self.mode_plans.get(name, ()), now)
            keeps[name] = None if running is None else (running.action, running.battery_kw)
        windows = self._car_windows()
        log = site_house.log
        sun_kind = KIND_SUN_CORRECTED if solar_basis == SOLAR_CORRECTED else KIND_SUN
        misses = (
            pairs_from_log(log.hours(KIND_HOUSE_MEDIAN)),
            pairs_from_log(log.hours(sun_kind)) if solar is not None else [],
            site_house.holidays,
        )
        solved = await self.hass.async_add_executor_job(
            partial(
                _solve, now, tz, prices, house, solar, windows, cap, params, facts.soc_pct, mode, wear, keeps, misses
            )
        )
        if self._closed:
            return
        plans, skipped, slots, more = solved.plans, solved.skipped, solved.slots, solved.estimates
        result = plans[mode]
        decisions = result.plan.decisions
        self.mode = mode
        self.plan = planned_slots(decisions)
        self.soc_at_plan = float(facts.soc_pct)
        self.actuals.note_first(self.plan, self.soc_at_plan)
        self.site.house_forecast.log.record_forecast(KIND_BATTERY, soc_by_hour_end(decisions, tz), now)
        self.view = plan_view(result, slots)
        self.mode_views = {name: _mode_view(item, slots) for name, item in plans.items() if name != mode}
        self.mode_views[mode] = self.view
        # Self-consumption does not plan: its "plan" is the baseline, which needs no slot of its own in the actuals.
        self.mode_plans = {
            name: planned_slots(item.plan.decisions) for name, item in plans.items() if MODE_DEFS[name].plans
        }
        current = now.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
        self.bands = {
            "house": view(solved.house_band, {start: kw for start, kw in house.items() if start >= current}),
            "sun": view(solved.sun_band, {start: wh / 1000.0 for start, wh in (solar or {}).items() if start >= current}),
        }
        self.defensive_inputs = solved.defensive_inputs
        self.defensive_view = None
        if solved.defensive is not None:
            self.mode_plans[VARIANT_DEFENSIVE] = planned_slots(solved.defensive.plan.decisions)
            self.defensive_view = plan_view(solved.defensive, solved.defensive_slots)
        self.modes_skipped = skipped
        self.status, self.reason, self.made_at = STATUS_PLANNED, None, now
        self.inputs = {
            "soc_pct": facts.soc_pct,
            "battery": {
                "plant_max_kw": facts.plant_max_kw,
                "reserve_soc_pct": facts.reserve_soc_pct,
                "usable_kwh": params.usable_kwh,
                "max_charge_kw": params.max_charge_kw,
                "max_discharge_kw": params.max_discharge_kw,
                "min_soc_pct": params.min_soc_pct,
                "round_trip_efficiency": params.round_trip_efficiency,
            },
            "price_area": area_id,
            "import_cap_kw": None if cap is None else round(cap, 3),
            "house": house_source,
            "gross_hours": site_house.history.gross_in_window(now),
            "house_rows": rows_basis,
            "solar_statistics": list(site_house.solar_statistics),
            "solar_sources": list(solar_sources),
            "solar": solar_basis,
            "car_windows": len(windows),
            "slots": len(slots),
            "sell_credit": self._sell_credit(),
            "mode": mode,
            "wear": wear,
            "horizon_end": slots[-1].end.isoformat(),
            "estimates": sorted(set(estimates) | set(more)),
        }
        self._save()

    # ---- what is shown

    def _defensive_status(self) -> str:
        if self.defensive_view is not None:
            return STATUS_PLANNED
        if VARIANT_DEFENSIVE in self.modes_skipped:
            return DEFENSIVE_SKIPPED
        return NO_BAND if self.defensive_inputs is None else STATUS_NO_PLAN

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
            # The size and rated power learnt where the integration reads none, and the samples behind them.
            "learned": self.learned.view(),
            # The plan in force: the site's mode's.
            "mode": self.mode,
            "plan": self.view,
            # Every mode planned on the same inputs (the site's among them); those the CPU budget left out are named.
            "modes": {name: self.mode_views.get(name) for name in MODES},
            "modes_skipped": list(self.modes_skipped),
            # The learnt error bands: per hour [p10, the median the plan counts on, p90] (house kW, sun kWh).
            "band": {name: band.as_dict() for name, band in self.bands.items()},
            # The site's mode planned on the band's pessimistic side (the house's p90 in the dear hours, the sun's
            # p10): a shadow beside the modes, reviewed with them, never in force.
            "defensive": {
                "status": self._defensive_status(),
                "inputs": None if self.defensive_inputs is None else self.defensive_inputs.as_dict(),
                "plan": self.defensive_view,
            },
            "quarters": [bucket.view() for bucket in quarters],
            # Per day, side by side: what the battery really did, each mode's plan on the same measured load and
            # sun, and self-consumption; and the days kept summed.
            "reviews": [review.as_dict() for review in self.reviews],
            "review_totals": review_totals(self.reviews),
        }
