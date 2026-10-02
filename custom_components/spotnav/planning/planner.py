"""The Auto-price charging planner: pure arithmetic, no Home Assistant in it.

Prices are normalized onto a 15-minute grid of absolute instants, converted to the
area's major unit, taxed (energy tax and transfer fee in the minor unit, then VAT over
the sum), and exactly the needed whole slots are chosen, in at most the configured
number of contiguous runs, for the lowest total cost. Every failure is a named
[PlanResult.reason]. A slot nobody has published is never filled from another day: the
window ends in `insufficient_price_horizon` and `planning/price_wait.py` decides what next.

Pure: no `hass`, entity, service, timer, storage or network.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Final, Literal

from homeassistant.util import dt as dt_util

from ..pricing.relay_contract import PriceDocument


#: Contract version of the inputs, result and JSON fixtures. Bump when a field changes meaning.
CONTRACT_VERSION: Final = 1

STEP_MINUTES: Final = 15
SLOT_HOURS: Final = STEP_MINUTES / 60

HORIZON_HOURS: Final = 24

#: The farthest a dated departure may lie ahead, in local calendar days (the relay publishes one day
#: ahead, so a longer wait gains nothing). Enforced where a date is written; the planner trusts it.
MAX_DEPARTURE_DAYS_AHEAD: Final = 7

#: Bounds on the number of contiguous runs.
MIN_PERIODS: Final = 1
MAX_PERIODS: Final = 8

#: Target-SoC slider bounds and its starting value.
DEFAULT_TARGET_SOC_PERCENT: Final = 80
ENERGY_SLIDER_MIN_KWH: Final = 1
ENERGY_SLIDER_MAX_KWH: Final = 100

EUR_RATE: Final = 1.0

#: Why no plan was produced, as a fact about the data. `unsupported_resolution` is not
#: here: `parse_day` refuses it upstream, so it is bad caller input and raises.
PlannerReason = Literal[
    "no_published_prices",
    "insufficient_price_horizon",
    "deadline_too_short",
    "missing_fx_rate",
]

TargetEnergyReason = Literal["ok", "unknown_capacity", "already_at_target"]

PlannerCode = Literal[
    "invalid_phases",
    "invalid_amps",
    "invalid_energy",
    "invalid_consumption",
    "invalid_periods",
    "invalid_departure",
    "invalid_timestamp",
    "invalid_number",
    "invalid_fiscal",
    "area_mismatch",
    "timezone_mismatch",
    "no_documents",
    "invalid_target_soc",
] | PlannerReason


class PlannerInputError(ValueError):
    """A caller's input that cannot be planned against.

    [code] is the program-facing fact; the message is for the log. This boundary does
    not repair a bad caller, because every value here changes what gets charged.
    """

    def __init__(self, code: PlannerCode, message: str) -> None:
        super().__init__(message)
        self.code: PlannerCode = code


def _refuse(code: PlannerCode, message: str) -> None:
    raise PlannerInputError(code, message)


def _finite(value: Any, code: PlannerCode, what: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _refuse(code, f"{what} must be a number")
    number = float(value)
    if not math.isfinite(number):
        _refuse("invalid_number", f"{what} must be finite")
    return number


def _positive(value: Any, code: PlannerCode, what: str) -> float:
    number = _finite(value, code, what)
    if number <= 0:
        _refuse(code, f"{what} must be positive")
    return number


def _aware(moment: Any, what: str) -> datetime:
    if not isinstance(moment, datetime):
        _refuse("invalid_timestamp", f"{what} must be a datetime")
    if moment.tzinfo is None:
        _refuse("invalid_timestamp", f"{what} must be timezone-aware")
    return moment


@dataclass(frozen=True, slots=True)
class FiscalChoice:
    """The fiscal controls, as the caller set them.

    `tax_enabled` with no `tax_minor_per_kwh` is invalid rather than zero: "no tax" and
    "unknown tax" differ and only one is plannable. VAT works the same way.
    """

    tax_enabled: bool = False
    tax_minor_per_kwh: float | None = None
    transfer_enabled: bool = False
    transfer_minor_per_kwh: float | None = None
    vat_enabled: bool = False
    vat_percent: float | None = None

    def validated(self) -> FiscalChoice:
        """The same choice, once every enabled component has a usable value."""
        tax = _component("tax", self.tax_enabled, self.tax_minor_per_kwh)
        transfer = _component("transfer", self.transfer_enabled, self.transfer_minor_per_kwh)
        # A zero VAT rate is legitimate (NO4); an absent one is not while VAT is on.
        vat: float | None = None
        if self.vat_enabled:
            if self.vat_percent is None:
                _refuse("invalid_fiscal", "VAT is enabled but no rate was given")
            vat = _finite(self.vat_percent, "invalid_fiscal", "vat_percent")
            if vat < 0:
                _refuse("invalid_fiscal", "vat_percent must not be negative")
        return FiscalChoice(
            tax_enabled=self.tax_enabled,
            tax_minor_per_kwh=tax,
            transfer_enabled=self.transfer_enabled,
            transfer_minor_per_kwh=transfer,
            vat_enabled=self.vat_enabled,
            vat_percent=vat,
        )


def _component(name: str, enabled: bool, value: float | None) -> float | None:
    """A fee's explicit value, or `None` when the component is switched off.

    A value given while the component is off is validated and then ignored, so a
    component can be switched off without clearing its field.
    """
    if value is None:
        if enabled:
            _refuse("invalid_fiscal", f"{name} is enabled but no value was given")
        return None
    number = _finite(value, "invalid_fiscal", f"{name}_minor_per_kwh")
    if number < 0:
        _refuse("invalid_fiscal", f"{name}_minor_per_kwh must not be negative")
    return number if enabled else None


def effective_minor_per_kwh(local_major_per_kwh: float, fiscal: FiscalChoice) -> float:
    """The price a slot is planned against, in the area's minor unit.

    The order is not commutative, which is why it lives in one function:
    local major x 100, + energy tax, + transfer fee, x (1 + VAT / 100).
    VAT is last because it applies to the tax and the fee too.
    """
    minor = local_major_per_kwh * 100
    if fiscal.tax_enabled and fiscal.tax_minor_per_kwh is not None:
        minor += fiscal.tax_minor_per_kwh
    if fiscal.transfer_enabled and fiscal.transfer_minor_per_kwh is not None:
        minor += fiscal.transfer_minor_per_kwh
    if fiscal.vat_enabled and fiscal.vat_percent is not None:
        minor *= 1 + fiscal.vat_percent / 100
    return minor


def power_kw(amps: int, phases: int) -> float:
    """The power a connector draws: 230 V single phase, or 400 V three-phase.

    Three-phase uses `sqrt(3)` times the line current, not a "230 x 3" shortcut.
    """
    if phases == 1:
        return 230.0 * amps / 1000.0
    return math.sqrt(3.0) * 400.0 * amps / 1000.0


def energy_per_slot_kwh(amps: int, phases: int) -> float:
    """What one whole 15-minute slot delivers at that power."""
    return power_kw(amps, phases) * SLOT_HOURS


def slots_needed(requested_kwh: float, amps: int, phases: int) -> int:
    """How many whole slots the request needs, and never fewer than one.

    Delivered energy may exceed the request by up to one slot; rounding down would
    deliver less than asked for.
    """
    return max(1, math.ceil(requested_kwh / energy_per_slot_kwh(amps, phases)))


@dataclass(frozen=True, slots=True)
class PlanningSlot:
    """One 15-minute slot of published price, on the absolute grid.

    `local_major_per_kwh` is the relay's EUR price times the day document's own rate.
    `source` names the document it came from, for diagnostics only, never arithmetic.
    """

    start: datetime
    local_major_per_kwh: float
    source: str

    @property
    def end(self) -> datetime:
        return self.start + timedelta(minutes=STEP_MINUTES)


@dataclass(frozen=True, slots=True)
class SelectedSlot:
    """One slot the plan chose, with both prices and where the price came from."""

    start: datetime
    end: datetime
    local_major_per_kwh: float
    effective_minor_per_kwh: float
    unpriced: bool
    source: str


def planning_slots(
    documents: tuple[PriceDocument, ...],
    *,
    currency: str,
    timezone: str,
) -> tuple[PlanningSlot, ...]:
    """Every document's prices on one absolute 15-minute grid, deduplicated and time-sorted.

    A 60-minute row becomes four equal slots; an unrepresentable resolution is refused.
    EUR has rate 1, a non-EUR area needs that day's own rate (else refused). When two
    documents cover an instant, the first in the caller's order wins. Sorting is by absolute
    time so DST nights stay correct.
    """
    if not documents:
        _refuse("no_documents", "the planner needs at least one price document")

    by_instant: dict[datetime, PlanningSlot] = {}
    for document in documents:
        resolution = document.resolution_minutes
        if STEP_MINUTES > resolution or resolution % STEP_MINUTES != 0:
            # Unreachable from `parse_day`; a forged document is bad input, not a data state.
            _refuse(
                "unsupported_resolution",
                f"a {resolution}-minute document cannot be represented on a {STEP_MINUTES}-minute grid",
            )
        per_row = resolution // STEP_MINUTES
        rate = _rate_for(document, currency)
        for index, euro in enumerate(document.prices):
            row_start = document.start_instant + timedelta(minutes=resolution) * index
            for step in range(per_row):
                start = row_start + timedelta(minutes=STEP_MINUTES) * step
                if start in by_instant:
                    continue
                by_instant[start] = PlanningSlot(
                    start=start,
                    local_major_per_kwh=euro * rate,
                    source=document.day.isoformat(),
                )
    return tuple(by_instant[start] for start in sorted(by_instant))


def _rate_for(document: PriceDocument, currency: str) -> float:
    """The day's own rate for the area's currency, or 1 for a euro area.

    A missing or unusable rate is a `missing_fx_rate` refusal, not a `KeyError` and
    not another day's number.
    """
    if currency.upper() == "EUR":
        return EUR_RATE
    rate = document.fx_rate(currency)
    if rate is None:
        _refuse(
            "missing_fx_rate",
            f"the {document.day.isoformat()} document has no {currency} rate",
        )
    if not math.isfinite(rate) or rate <= 0:
        _refuse("missing_fx_rate", f"the {document.day.isoformat()} {currency} rate is not usable")
    return rate


@dataclass(frozen=True, slots=True)
class PlanRequest:
    """Everything the planner needs, validated, with nothing read from anywhere.

    The identity fields (`area_id`, `timezone`, `currency`, units) are the
    catalogue's, passed in rather than looked up.
    """

    area_id: str
    timezone: str
    currency: str
    major_unit: str
    minor_unit: str
    documents: tuple[PriceDocument, ...]
    now: datetime
    phases: int
    amps: int
    requested_kwh: float
    consumption_kwh_per_10km: float
    fiscal: FiscalChoice = FiscalChoice()
    max_periods: int = MIN_PERIODS
    departure: time | None = None
    #: The local date the departure falls on, or `None` for the next occurrence of `departure`. With a
    #: date the deadline is that wall time on that date and the horizon reaches it (up to 7 days), so
    #: intervals after the last published one are unknown, not absent.
    departure_date: date | None = None
    #: Latest instant a selected slot may end at, tighter than the departure or horizon.
    #: Lets `planning/price_wait.py` plan inside the priced part of the window.
    window_end: datetime | None = None

    def validated(self) -> PlanRequest:
        """The same request, once every field is known to be plannable."""
        if self.phases not in (1, 3):
            _refuse("invalid_phases", "phases must be 1 or 3")
        if isinstance(self.amps, bool) or not isinstance(self.amps, int) or self.amps <= 0:
            _refuse("invalid_amps", "amps must be a positive whole number")
        if not MIN_PERIODS <= _as_int(self.max_periods) <= MAX_PERIODS:
            _refuse("invalid_periods", f"max_periods must be between {MIN_PERIODS} and {MAX_PERIODS}")
        _positive(self.requested_kwh, "invalid_energy", "requested_kwh")
        _positive(self.consumption_kwh_per_10km, "invalid_consumption", "consumption_kwh_per_10km")
        _aware(self.now, "now")
        if self.window_end is not None:
            _aware(self.window_end, "window_end")
        if self.departure is not None:
            if not isinstance(self.departure, time):
                _refuse("invalid_departure", "departure must be a wall time")
            if self.departure.tzinfo is not None:
                _refuse("invalid_departure", "departure must be an area-local wall time, not an instant")
            if not (0 <= self.departure.hour <= 23 and 0 <= self.departure.minute <= 59):
                _refuse("invalid_departure", "departure must be a valid time of day")
        if self.departure_date is not None:
            if self.departure is None:
                _refuse("invalid_departure", "a departure date needs a departure time")
            if not isinstance(self.departure_date, date) or isinstance(self.departure_date, datetime):
                _refuse("invalid_departure", "departure_date must be a calendar date")
        if dt_util.get_time_zone(self.timezone) is None:
            _refuse("timezone_mismatch", f"{self.timezone!r} is not a known timezone")
        for document in self.documents:
            if document.area_id != self.area_id:
                _refuse(
                    "area_mismatch",
                    f"a {document.area_id!r} document was given to a {self.area_id!r} plan",
                )
            if document.tz != self.timezone:
                _refuse(
                    "timezone_mismatch",
                    f"a document in {document.tz!r} was given to a plan in {self.timezone!r}",
                )
            if document.unit != "EUR/kWh":
                _refuse("invalid_number", f"a document priced in {document.unit!r} cannot be planned")
        return PlanRequest(
            area_id=self.area_id,
            timezone=self.timezone,
            currency=self.currency,
            major_unit=self.major_unit,
            minor_unit=self.minor_unit,
            documents=self.documents,
            now=self.now,
            phases=self.phases,
            amps=self.amps,
            requested_kwh=float(self.requested_kwh),
            consumption_kwh_per_10km=float(self.consumption_kwh_per_10km),
            fiscal=self.fiscal.validated(),
            max_periods=_as_int(self.max_periods),
            departure=self.departure,
            departure_date=self.departure_date,
            window_end=self.window_end,
        )


def _as_int(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        _refuse("invalid_periods", "max_periods must be a whole number")
    return value


@dataclass(frozen=True, slots=True)
class PlanResult:
    """One plan, or the reason there is none; never `None` and never an exception.

    Every number is in the unit its name says: `estimated_cost` in the major unit,
    `effective_minor_per_kwh` in the minor unit, and `local_major_per_kwh` as the
    relay's price converted with the day's own rate and nothing added.
    """

    version: int
    area_id: str
    timezone: str
    currency: str
    major_unit: str
    minor_unit: str
    now: datetime
    reason: PlannerReason | None
    power_kw: float
    requested_kwh: float
    delivered_kwh: float
    distance_mil: float
    estimated_cost: float
    periods: tuple[tuple[datetime, datetime], ...]
    slots: tuple[SelectedSlot, ...]
    unpriced_slots: int
    priced_slots: int
    unpriced: bool
    slots_needed: int
    #: The candidate range the plan was chosen from; empty only for a refusal.
    horizon: tuple[HorizonInterval, ...] = ()

    @property
    def has_plan(self) -> bool:
        return self.reason is None


def first_start_for(documents: tuple[PriceDocument, ...], *, now: datetime, tz: str) -> datetime:
    """The first usable planning instant for `now`, on the grid `calculate_plan` uses.

    Mirrors the start of `calculate_plan` (kept in step by hand). Slots are built with
    `currency="EUR"` because only `start` is read and a real currency could make
    `planning_slots` refuse a document lacking its rate. Requires at least one document.
    """
    slots = planning_slots(documents, currency="EUR", timezone=tz)
    zone = dt_util.get_time_zone(tz)
    day_offset = slots[0].start.astimezone(zone).utcoffset() or timedelta(0)
    day_zone = timezone(day_offset)
    local_now = now.astimezone(day_zone)
    floored = local_now - timedelta(
        minutes=local_now.minute % STEP_MINUTES, seconds=local_now.second, microseconds=local_now.microsecond
    )
    return floored if floored >= local_now else floored + timedelta(minutes=STEP_MINUTES)


def departure_for(
    documents: tuple[PriceDocument, ...], *, now: datetime, tz: str, departure: time
) -> datetime:
    """`departure` resolved to the instant `calculate_plan` would use for this `now`.

    Composes `first_start_for` with `resolve_departure`; adds no policy of its own.
    """
    first_start = first_start_for(documents, now=now, tz=tz)
    return resolve_departure(now, tz, departure, first_start)


def resolve_departure(now: datetime, tz: str, departure: time, first_start: datetime) -> datetime:
    """The user's departure wall time as an absolute instant, in the area's own zone.

    A real area-local wall time, not a fixed offset (Stockholm's 03:30 on 2026-03-29 is
    01:30Z). It is built on the local date of `now`, or the next local date if not strictly
    after the first usable slot. An ambiguous time takes the earlier occurrence (cannot
    arrive late); a nonexistent one advances to the first valid instant after the gap.
    Compared as UTC instants, never wall datetimes.
    """
    zone = dt_util.get_time_zone(tz)
    local_now = now.astimezone(zone)
    for day in (local_now.date(), local_now.date() + timedelta(days=1)):
        moment = local_instant(day, departure, zone)
        if _as_utc(moment) > _as_utc(first_start):
            return moment
    # Unreachable for a real time of day; keeps a pathological zone answering.
    return local_instant(local_now.date() + timedelta(days=1), departure, zone)


def _as_utc(moment: datetime) -> datetime:
    return moment.astimezone(timezone.utc)


def local_instant(day: date, wall: time, zone) -> datetime:
    """One wall time on one local calendar date, resolved to a single instant.

    Real times are told from gap times by a round trip through UTC: if it does not
    return the same wall clock, the time does not exist and the first valid instant
    after the gap is used.
    """
    naive = datetime(day.year, day.month, day.day, wall.hour, wall.minute)
    candidate = naive.replace(tzinfo=zone)
    if candidate.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None) == naive:
        # Real; `fold=0` picks the earlier occurrence of an ambiguous time.
        return candidate
    for minutes in range(1, 24 * 60):
        shifted = naive + timedelta(minutes=minutes)
        probe = shifted.replace(tzinfo=zone)
        if probe.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None) == shifted:
            return probe
    return candidate


@dataclass(frozen=True, slots=True)
class HorizonInterval:
    """One candidate instant of a calculation, as the calculation saw it.

    The same normalized candidates `calculate_plan` chooses from, so a chart drawn
    from these rows and its plan cannot disagree.
    """

    start: datetime
    end: datetime
    utc_start: datetime
    utc_end: datetime
    day: date
    published_major_per_kwh: float
    effective_minor_per_kwh: float
    in_proposal: bool


def _candidates(
    slots: tuple[PlanningSlot, ...],
    *,
    first_start: datetime,
    required_end: datetime,
) -> tuple[list[PlanningSlot], PlannerReason | None]:
    """Every slot the calculation may choose from, always a published one.

    The single place the candidate range is built. A gap ends with
    `insufficient_price_horizon`; never a shorter list or another day's price.
    """
    candidates: list[PlanningSlot] = []
    published = {slot.start: slot for slot in slots}
    time_cursor = first_start
    while time_cursor < required_end:
        actual = published.get(time_cursor)
        if actual is None:
            return ([], "insufficient_price_horizon")
        candidates.append(actual)
        time_cursor += timedelta(minutes=STEP_MINUTES)
    return (candidates, None)


def _horizon(
    candidates: list[PlanningSlot],
    selected: tuple[PlanningSlot, ...],
    *,
    zone: Any,
    fiscal: FiscalChoice,
) -> tuple[HorizonInterval, ...]:
    """The candidate range as an immutable projection of what the calculation saw.

    `in_proposal` marks the instants the plan selected, which is what a chart shades.
    """
    chosen = {slot.start for slot in selected}
    rows: list[HorizonInterval] = []
    for slot in candidates:
        # Build bounds from the instant: wall-clock +15 min is not 15 elapsed minutes across
        # an offset transition, and an autumn repeated hour would fold two rows together.
        utc_start = slot.start.astimezone(timezone.utc)
        utc_end = utc_start + timedelta(minutes=STEP_MINUTES)
        local = utc_start.astimezone(zone)
        rows.append(
            HorizonInterval(
                start=local,
                end=utc_end.astimezone(zone),
                utc_start=utc_start,
                utc_end=utc_end,
                day=local.date(),
                published_major_per_kwh=slot.local_major_per_kwh,
                effective_minor_per_kwh=effective_minor_per_kwh(slot.local_major_per_kwh, fiscal),
                in_proposal=slot.start in chosen,
            )
        )
    return tuple(rows)


@dataclass(frozen=True, slots=True)
class _Window:
    """The geometry of one calculation: where it starts, where it must end, how much it needs."""

    slots: tuple[PlanningSlot, ...]
    zone: Any
    first_start: datetime
    horizon: datetime
    #: Instant the charge must finish by (departure or tighter `window_end`); `None` if neither.
    deadline: datetime | None
    required_end: datetime
    needed: int
    per_slot: float

    @property
    def end(self) -> datetime:
        """The last instant a selected slot may end at."""
        return self.deadline if self.deadline is not None else self.horizon


def _window(request: PlanRequest) -> _Window | PlannerReason:
    """Normalize the prices and lay out the window, or name why there is none.

    Shared by `calculate_plan` and `price_gap` so both agree on which prices a request needs.
    """
    zone = dt_util.get_time_zone(request.timezone)
    try:
        slots = planning_slots(request.documents, currency=request.currency, timezone=request.timezone)
    except PlannerInputError as err:
        # A missing rate is a data fact and becomes a reason; everything else is bad input.
        if err.code == "missing_fx_rate":
            return "missing_fx_rate"
        raise
    if not slots:
        return "no_published_prices"

    # Two zones: `zone` is the area's own, for calendar and wall-clock questions.
    # `day_zone` is the fixed offset of the day's first published point, used only to
    # find the next quarter-hour boundary (elapsed-time arithmetic). The departure is
    # resolved separately in `zone` by [resolve_departure].
    day_offset = slots[0].start.astimezone(zone).utcoffset() or timedelta(0)
    day_zone = timezone(day_offset)
    now = request.now.astimezone(day_zone)
    floored = now - timedelta(
        minutes=now.minute % STEP_MINUTES, seconds=now.second, microseconds=now.microsecond
    )
    # A `now` exactly on a boundary is usable.
    first_start = floored if floored >= now else floored + timedelta(minutes=STEP_MINUTES)
    horizon = first_start + timedelta(hours=HORIZON_HOURS)

    per_slot = energy_per_slot_kwh(request.amps, request.phases)
    needed = slots_needed(request.requested_kwh, request.amps, request.phases)
    duration = timedelta(minutes=STEP_MINUTES * needed)

    deadline: datetime | None = None
    if request.departure is not None:
        if request.departure_date is not None:
            deadline = local_instant(request.departure_date, request.departure, zone)
            # A dated departure reaches as far as its deadline, past the 24 hours of a daily one.
            horizon = max(horizon, deadline)
        else:
            deadline = resolve_departure(request.now, request.timezone, request.departure, first_start)
    if request.window_end is not None:
        deadline = request.window_end if deadline is None else min(deadline, request.window_end)

    if deadline is None:
        # No deadline: the window ends exactly at the horizon.
        required_end = horizon
        latest_start_exclusive = horizon - duration + timedelta(minutes=STEP_MINUTES)
        if latest_start_exclusive <= first_start:
            # The request cannot fit in the window at all, whatever the prices say.
            return "insufficient_price_horizon"
    else:
        # Deadline: the bound is the latest start, so the last slot may end at the deadline.
        latest_start_exclusive = min(horizon, deadline - duration + timedelta(minutes=STEP_MINUTES))
        if latest_start_exclusive <= first_start:
            return "deadline_too_short"
        required_end = latest_start_exclusive + duration
        if request.window_end is not None and deadline == request.window_end:
            # A caller's own window (the published part, or what must be bought before a publication) ends
            # where it says: no slot can be chosen past it, so no price past it is required either.
            required_end = min(required_end, deadline)
    return _Window(
        slots=slots,
        zone=zone,
        first_start=first_start,
        horizon=horizon,
        deadline=deadline,
        required_end=required_end,
        needed=needed,
        per_slot=per_slot,
    )


@dataclass(frozen=True, slots=True)
class PriceGap:
    """Where a request's window runs out of published prices.

    `known` is the contiguous published slots from the first usable one up to the gap;
    `missing_from` is the first unpublished instant; `deadline` is when the charge must
    finish (departure or 24-hour horizon).
    """

    first_start: datetime
    missing_from: datetime
    deadline: datetime
    known: tuple[PlanningSlot, ...]


def price_gap(request: PlanRequest) -> PriceGap | None:
    """The gap in a request's published prices, or `None` when it is fully priced.

    Also `None` when the window cannot be laid out (`calculate_plan` reports why).
    """
    request = request.validated()
    window = _window(request)
    if isinstance(window, str):
        return None
    published = {slot.start: slot for slot in window.slots}
    known: list[PlanningSlot] = []
    cursor = window.first_start
    while cursor < window.required_end:
        slot = published.get(cursor)
        if slot is None:
            return PriceGap(
                first_start=window.first_start,
                missing_from=cursor,
                deadline=window.end,
                known=tuple(known),
            )
        known.append(slot)
        cursor += timedelta(minutes=STEP_MINUTES)
    return None


def plan_unpriced(request: PlanRequest) -> PlanResult:
    """Charge at once from the first usable slot, at unknown prices.

    Used when prices are unpublished and waiting would miss the deadline (`price_wait`).
    One run of consecutive slots, each flagged unpriced; cost is reported as zero.
    """
    request = request.validated()
    window = _window(request)
    if isinstance(window, str):
        return _no_plan(request, window)
    step = timedelta(minutes=STEP_MINUTES)
    count = 0
    while count < window.needed and window.first_start + step * (count + 1) <= window.end:
        count += 1
    if count == 0:
        return _no_plan(request, "deadline_too_short")
    starts = [window.first_start + step * index for index in range(count)]
    delivered = count * window.per_slot
    return PlanResult(
        version=CONTRACT_VERSION,
        area_id=request.area_id,
        timezone=request.timezone,
        currency=request.currency,
        major_unit=request.major_unit,
        minor_unit=request.minor_unit,
        now=request.now,
        reason=None,
        power_kw=power_kw(request.amps, request.phases),
        requested_kwh=request.requested_kwh,
        delivered_kwh=delivered,
        distance_mil=delivered / request.consumption_kwh_per_10km,
        estimated_cost=0.0,
        periods=((starts[0], starts[-1] + step),),
        slots=tuple(
            SelectedSlot(
                start=start,
                end=start + step,
                local_major_per_kwh=0.0,
                effective_minor_per_kwh=0.0,
                unpriced=True,
                source=UNPRICED_SOURCE,
            )
            for start in starts
        ),
        unpriced_slots=count,
        priced_slots=0,
        unpriced=True,
        slots_needed=count,
    )


#: `SelectedSlot.source` of a slot charged without any published price (`plan_unpriced`).
UNPRICED_SOURCE: Final = "unpriced"


def calculate_plan(request: PlanRequest) -> PlanResult:
    """The whole calculation: normalize, choose, and report, or say why not.

    Each step can end with a named reason rather than a partial answer. What to do about
    `insufficient_price_horizon` is `price_wait`'s decision, not this function's.
    """
    request = request.validated()
    window = _window(request)
    if isinstance(window, str):
        return _no_plan(request, window)
    zone = window.zone
    slots = window.slots
    first_start = window.first_start
    horizon = window.horizon
    deadline = window.deadline
    needed = window.needed
    per_slot = window.per_slot

    candidates, refusal = _candidates(slots, first_start=first_start, required_end=window.required_end)
    if refusal is not None:
        return _no_plan(request, refusal)
    if len(candidates) < needed:
        return _no_plan(request, "insufficient_price_horizon")

    chosen = _choose(
        candidates,
        needed,
        per_slot,
        request.max_periods,
        request.fiscal,
        latest_end_inclusive=deadline if deadline is not None else horizon,
    )
    if chosen is None:
        return _no_plan(request, "insufficient_price_horizon")

    selected = tuple(candidates[index] for index in chosen)
    periods = _periods(selected)
    delivered = needed * per_slot
    cost_minor = sum(
        per_slot * effective_minor_per_kwh(slot.local_major_per_kwh, request.fiscal)
        for slot in selected
    )
    return PlanResult(
        version=CONTRACT_VERSION,
        area_id=request.area_id,
        timezone=request.timezone,
        currency=request.currency,
        major_unit=request.major_unit,
        minor_unit=request.minor_unit,
        now=request.now,
        reason=None,
        power_kw=power_kw(request.amps, request.phases),
        requested_kwh=request.requested_kwh,
        delivered_kwh=delivered,
        distance_mil=delivered / request.consumption_kwh_per_10km,
        estimated_cost=cost_minor / 100,
        periods=periods,
        slots=tuple(
            SelectedSlot(
                start=slot.start,
                end=slot.end,
                local_major_per_kwh=slot.local_major_per_kwh,
                effective_minor_per_kwh=effective_minor_per_kwh(slot.local_major_per_kwh, request.fiscal),
                unpriced=False,
                source=slot.source,
            )
            for slot in selected
        ),
        unpriced_slots=0,
        priced_slots=needed,
        unpriced=False,
        slots_needed=needed,
        horizon=_horizon(candidates, selected, zone=zone, fiscal=request.fiscal),
    )


def _no_plan(request: PlanRequest, reason: PlannerReason) -> PlanResult:
    """A result that carries the reason and no plan, keeping the identity intact."""
    return PlanResult(
        version=CONTRACT_VERSION,
        area_id=request.area_id,
        timezone=request.timezone,
        currency=request.currency,
        major_unit=request.major_unit,
        minor_unit=request.minor_unit,
        now=request.now,
        reason=reason,
        power_kw=power_kw(request.amps, request.phases),
        requested_kwh=request.requested_kwh,
        delivered_kwh=0.0,
        distance_mil=0.0,
        estimated_cost=0.0,
        periods=(),
        slots=(),
        unpriced_slots=0,
        priced_slots=0,
        unpriced=False,
        slots_needed=0,
    )


@dataclass(frozen=True, slots=True)
class _State:
    """One state of the search: what it has chosen, and what that has cost."""

    selected: int
    runs: int
    active: bool
    cost: float
    slots: tuple[int, ...]


def _choose(
    candidates: list[PlanningSlot],
    needed: int,
    per_slot: float,
    period_cap: int,
    fiscal: FiscalChoice,
    latest_end_inclusive: datetime | None,
) -> tuple[int, ...] | None:
    """Which slots to charge in: exactly `needed` of them, for the lowest total cost.

    A dynamic program with state (chosen count, runs, previous slot chosen) and transitions
    skip / start a run / continue a run, so plans may be non-contiguous and the period cap
    binds properly (a greedy merge is not equivalent). [latest_end_inclusive] bounds the end
    of any chosen slot; `None` means unbounded. Ties: states compare by `(cost, indices)` over
    chronological candidates, so equal costs pick the earliest slots; no epsilon.
    """
    initial = _State(selected=0, runs=0, active=False, cost=0.0, slots=())
    states: dict[tuple[int, int, bool], _State] = {(0, 0, False): initial}

    for index, slot in enumerate(candidates):
        next_states: dict[tuple[int, int, bool], _State] = {}
        slot_cost = per_slot * effective_minor_per_kwh(slot.local_major_per_kwh, fiscal)

        def keep(state: _State) -> None:
            key = (state.selected, state.runs, state.active)
            existing = next_states.get(key)  # noqa: B023 - only called within this iteration
            if existing is None or (state.cost, state.slots) < (existing.cost, existing.slots):
                next_states[key] = state  # noqa: B023

        for state in states.values():
            # Skipping is always allowed and ends a run.
            keep(_State(state.selected, state.runs, False, state.cost, state.slots))
            runs = state.runs + (0 if state.active else 1)
            ends_inside_the_window = latest_end_inclusive is None or slot.end <= latest_end_inclusive
            if ends_inside_the_window and state.selected < needed and runs <= period_cap:
                keep(
                    _State(
                        selected=state.selected + 1,
                        runs=runs,
                        active=True,
                        cost=state.cost + slot_cost,
                        slots=state.slots + (index,),
                    )
                )
        states = next_states

    best: _State | None = None
    for state in states.values():
        if state.selected != needed:
            continue
        if best is None or (state.cost, state.slots) < (best.cost, best.slots):
            best = state
    return None if best is None else best.slots


def cheapest_slots(
    candidates: list[PlanningSlot],
    needed: int,
    per_slot: float,
    period_cap: int,
    fiscal: FiscalChoice,
    latest_end_inclusive: datetime | None,
) -> tuple[PlanningSlot, ...] | None:
    """The cheapest `needed` of these slots in at most `period_cap` runs, or `None` when none fit.

    The planner's own search over candidates the caller supplies, for readers that must price a
    hypothetical window the same way a real one is priced (see `planning/history_wait.py`).
    """
    chosen = _choose(candidates, needed, per_slot, period_cap, fiscal, latest_end_inclusive)
    return None if chosen is None else tuple(candidates[index] for index in chosen)


def _periods(selected: tuple[PlanningSlot, ...]) -> tuple[tuple[datetime, datetime], ...]:
    """Contiguous chosen slots merged into half-open `[start, end)` periods.

    Merging is by instant, not local clock label, so clock changes neither join nor split runs.
    """
    periods: list[list[datetime]] = []
    for slot in selected:
        if periods and periods[-1][1] == slot.start:
            periods[-1][1] = slot.end
        else:
            periods.append([slot.start, slot.end])
    return tuple((start, end) for start, end in periods)


@dataclass(frozen=True, slots=True)
class TargetEnergyRequest:
    """What a target state of charge needs, with every source made explicit.

    `capacity_kwh` is resolved by the caller; `None` means unknown, never zero.
    `apply_default_target` is a request, not a default.
    """

    soc_percent: float
    capacity_kwh: float | None
    stored_target_percent: int | None = None
    vehicle_max_percent: float | None = None
    apply_default_target: bool = False
    whole_kwh_input: bool = False


def _target_or_none(request: TargetEnergyRequest) -> int | None:
    """The effective target when it can be worked out, else `None`.

    Used on the unknown-capacity path, where an unresolvable target must not turn the answer into an exception.
    """
    try:
        return effective_target_soc_percent(
            request.stored_target_percent,
            request.vehicle_max_percent,
            apply_default_target=request.apply_default_target,
        )
    except PlannerInputError:
        return None


@dataclass(frozen=True, slots=True)
class TargetEnergyResolution:
    """The energy a target implies: a number, a reason, or both, but never a guess."""

    reason: TargetEnergyReason
    kwh: float | None
    target_percent: int | None
    effective_capacity_kwh: float | None

def effective_target_soc_percent(
    stored_target_percent: int | None,
    vehicle_max_percent: float | None,
    *,
    apply_default_target: bool,
) -> int:
    """The target to work with: the stored one, else the asked-for default, capped.

    A stored value above the vehicle's own limit is clamped here, not rewritten in
    storage, so the choice survives pointing the settings at another car. A vehicle
    with no usable maximum caps nothing.
    """
    ceiling = 100
    if vehicle_max_percent is not None:
        maximum = _finite(vehicle_max_percent, "invalid_target_soc", "vehicle_max_percent")
        ceiling = min(100, max(0, math.floor(maximum)))
    if stored_target_percent is None:
        if not apply_default_target:
            _refuse(
                "invalid_target_soc",
                "no stored target was given and the Android-compatible default was not requested",
            )
        chosen = DEFAULT_TARGET_SOC_PERCENT
    else:
        chosen = _as_int(stored_target_percent)
        if not 0 <= chosen <= 100:
            _refuse("invalid_target_soc", "the stored target must be a percentage")
    return min(chosen, ceiling)


def resolve_target_energy(request: TargetEnergyRequest) -> TargetEnergyResolution:
    """How much energy a target state of charge needs, or why that cannot be said.

    * `unknown_capacity`: `kwh` is `None`, not zero.
    * `already_at_target`: `kwh` is `0.0` and must not become a plan.
    * `ok`: a positive figure; with `whole_kwh_input` rounded up into the 1..100 kWh slider range.
    """
    soc = _finite(request.soc_percent, "invalid_target_soc", "soc_percent")
    # Capacity first: an unknown battery size is a more useful answer than a target complaint.
    capacity = request.capacity_kwh
    usable: float | None = None
    if capacity is not None:
        candidate = _finite(capacity, "invalid_target_soc", "capacity_kwh")
        usable = candidate if candidate > 0 else None
    if usable is None:
        return TargetEnergyResolution(
            "unknown_capacity",
            None,
            _target_or_none(request),
            None,
        )

    target = effective_target_soc_percent(
        request.stored_target_percent,
        request.vehicle_max_percent,
        apply_default_target=request.apply_default_target,
    )

    needed = max(0.0, (target - soc) / 100.0 * usable)
    if needed == 0.0:
        return TargetEnergyResolution("already_at_target", 0.0, target, usable)
    if request.whole_kwh_input:
        needed = float(min(ENERGY_SLIDER_MAX_KWH, max(ENERGY_SLIDER_MIN_KWH, math.ceil(needed))))
    return TargetEnergyResolution("ok", needed, target, usable)


@dataclass(frozen=True, slots=True)
class ChartInterval:
    """One row a chart can draw: a published price, with the two figures beside it.

    `raw_minor_per_kwh` is the converted relay price; `effective_minor_per_kwh` adds the fiscal choice.
    """

    start: datetime
    end: datetime
    utc_start: datetime
    utc_end: datetime
    day: date
    raw_minor_per_kwh: float
    effective_minor_per_kwh: float


def document_rate(document: PriceDocument, currency: str) -> float:
    """The day's own rate for the area's currency, for readers that show a price.

    The same call as `_rate_for`, so the rule has one implementation.
    """
    return _rate_for(document, currency)


def chart_intervals(
    documents: tuple[PriceDocument, ...],
    *,
    currency: str,
    fiscal: FiscalChoice,
) -> tuple[ChartInterval, ...]:
    """Every published interval of these documents, ready to draw, in instant order.

    Rows are the relay's own resolution (including 23- and 25-hour days), never
    re-gridded, and nothing is invented. Two documents pricing the same instant give
    one row, the caller's order winning, as in `planning_slots`.
    """
    rows: dict[datetime, ChartInterval] = {}
    for document in documents:
        rate = document_rate(document, currency)
        for interval in document.intervals:
            if interval.utc_start in rows:
                continue
            local_major = interval.eur_per_kwh * rate
            rows[interval.utc_start] = ChartInterval(
                start=interval.start,
                end=interval.end,
                utc_start=interval.utc_start,
                utc_end=interval.utc_end,
                day=document.day,
                raw_minor_per_kwh=local_major * 100,
                effective_minor_per_kwh=effective_minor_per_kwh(local_major, fiscal),
            )
    return tuple(rows[start] for start in sorted(rows))
