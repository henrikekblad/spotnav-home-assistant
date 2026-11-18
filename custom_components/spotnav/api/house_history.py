"""`spotnav/get_house_history` (API v1): the house card's history, one day, month or year of a site (read-only).

`{api_version: 1, site_id | charger_id, period: "day" | "month" | "year", date?: "YYYY-MM-DD"}` (any day inside the
period; absent: today). Any user who can see the card may read. The answer is `site/house_period.py:house_history`
(its docstring has the shape). A command of its own rather than a `period` on `spotnav/get_house`: that answer is the
card's front (now, the forecasts ahead, the plan), read every five minutes; the history is a period's sums from the
long-term statistics with a cache of its own, and neither answer has to carry the other's keys.

What is read, all off the event loop and kept:

* A day: its energy flow as `spotnav/get_house` reads a day (`api/house.async_cached_day_flow`: by the quarter hour
  within the recorder's short-term statistics, its finished hours kept, else by the hour).
* A month or a year: the Energy dashboard's meters, the chargers' registers, the heat pump's meter and the heat zones' sensors by the hour
  from the recorder's long-term statistics (kept for good), in the recorder's executor, up to the last completed hour.
* The prices: the area's held day documents (`PriceRepository.day_snapshot`, in memory) with the fees as set now.
* What SpotNav saved per day (`api/saving.py`): the car's sessions with their hours from the recorder, the battery's
  day money and the heat's reviews; for a day's hourly bars, the car's figure in the hour each charge started.
* The battery's reviewed days: the battery plan's own (30 days), and for an older day of the period its file in the
  day archive (`<config>/spotnav_archive/<site>/<date>.json`, `day_export.py`) where one was exported, read in the
  executor.

The answer is kept per site and period until the next hour (a period with today in it: the next 5-minute row) or,
for a finished period, until the next day (a night's review may add to it). Refusals as `spotnav/get_house`, and
`spotnav_invalid_date` for a period that is not one (an unknown kind, not a date, after today, more than
`YEARS_BACK` years back).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Final

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import callback, HomeAssistant

from ..battery.plan_shadow import DayReview, PriceSlot, price_slots
from ..day_export import ARCHIVE_DIR
from ..planning.demand_charge import hour_start
from ..pricing.price_source import site_price_source
from ..runtime import domain_data, site_controller_for
from ..site.energy_flow import EnergyHour
from ..site.energy_statistics import async_energy_prefs, energy_flow_ids
from ..site.history_period import PERIOD_DAY, Period, local_date, requested_period, utc_bounds
from ..site.house_period import HOUSE_HISTORY_API_VERSION, HouseHistoryInputs, house_history
from .common import send_unsupported_version
from .house import _car_register_ids, _hourly_periods, async_cached_day_flow, five_slot
from .saving import async_day_savings_by_hour
from .power_month import ERROR_NO_SITE, PowerMonthRefusal, _site_of_request

_LOGGER = logging.getLogger(__name__)

ERROR_INVALID_DATE: Final = "spotnav_invalid_date"
_CACHE: Final = "spotnav_house_history"
#: The answers kept (all sites together); the oldest goes first.
CACHE_SIZE: Final = 24


def _arbitrage(controller: Any, has_battery: bool) -> dict[str, Any]:
    """The battery's arbitrage per day and per hour as its stored value keeps it (`battery/stored_value.py`); none
    without a battery or its tally."""
    stored = getattr(getattr(controller.battery_shadow, "plan", None), "stored", None)
    if not has_battery or controller.battery_shadow.facts.battery_entity_id is None or stored is None:
        return {}
    days = {day: money.arbitrage for day, money in stored.days.items()}
    hours = {hour: value for money in stored.days.values() for hour, value in money.hours.items()}
    return {
        "arbitrage_days": days,
        "arbitrage_hours": hours,
        "flow_days": {day: money.flow for day, money in stored.days.items() if money.flow is not None},
        "flow_hours": {hour: flow for money in stored.days.values() for hour, flow in money.hour_flows.items()},
    }


def period_price_slots(hass: HomeAssistant, site_entry_id: str, period: Period) -> list[PriceSlot]:
    """The period's price slots with the fees as set now, from the day documents held (nothing is fetched); none
    without a price source, its area or its fees. A day either side is read too (an area on another clock)."""
    from ..pricing.price_points import price_points  # noqa: PLC0415 - it imports the planner
    from ..planning.planner import PlannerInputError  # noqa: PLC0415

    source = site_price_source(hass, site_entry_id)
    repository = domain_data(hass).price_repository
    if source is None or source.area is None or repository is None:
        return []
    fiscal = source.fiscal
    if fiscal is None:
        return []
    documents = []
    day = period.first - timedelta(days=1)
    while day <= period.end:
        document = repository.day_snapshot(source.area_id, day).document
        if document is not None:
            documents.append(document)
        day += timedelta(days=1)
    try:
        points = price_points(documents, currency=source.area.currency, fiscal=fiscal)
    except PlannerInputError:
        return []
    return price_slots(((point.start, point.end, point.spot) for point in points), fiscal)


def read_archived_reviews(folder: Path, days: Sequence[date]) -> dict[date, DayReview]:
    """The battery reviews of `days` from the day archive's files in `folder` (executor job); a missing, unreadable
    or malformed file, or one without a review, is skipped."""
    out: dict[date, DayReview] = {}
    if not folder.is_dir():
        return out
    for day in days:
        path = folder / f"{day.isoformat()}.json"
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        review = DayReview.from_dict(record.get("battery_review")) if isinstance(record, dict) else None
        if review is not None and review.day == day:
            out[day] = review
    return out


def forecast_maes(log: Any) -> dict[str, dict[date, float]]:
    """Each forecast kind's daily mean absolute errors as the forecast log reviewed them."""
    return {kind: {review.day: review.all.mae for review in body.reviews} for kind, body in log.kinds.items()}


async def _read_periods(
    hass: HomeAssistant, site_entry_id: str, controller: Any, period: Period, now: datetime
) -> tuple[list[EnergyHour] | None, int, Any]:
    """The period's energy periods, their length (minutes) and the meters read (`FlowIds`); `None` without the
    Energy dashboard's grid meter."""
    tz = controller.demand.tracker.tz
    ids = energy_flow_ids(await async_energy_prefs(hass))
    if not ids.grid_in:
        return None, 60, ids
    if period.kind == PERIOD_DAY:
        today = local_date(now, tz)
        read = await async_cached_day_flow(hass, site_entry_id, controller, None if period.first == today else period.first)
        if read is None:
            return [], 60, ids
        step = 60 if read.flow is None else int(read.flow.get("step_min") or 60)
        return list(read.periods), step, ids
    start, end = utc_bounds(period, tz)
    end = min(end, hour_start(now, tz))
    if end <= start:
        return [], 60, ids
    hours = []
    moment = start
    while moment < end:
        hours.append(moment)
        moment += timedelta(hours=1)
    car_ids = _car_register_ids(hass, controller)
    heat = controller.house_forecast.heat
    return await _hourly_periods(hass, controller, hours, end, ids, car_ids, heat.meter, await heat.async_current_zones()), 60, ids


async def history_answer(hass: HomeAssistant, site: Any, raw_period: Any, raw_date: Any) -> dict[str, Any]:
    """The answer for one site and period, or a `PowerMonthRefusal`."""
    return (await history_with_periods(hass, site, raw_period, raw_date))[0]


async def history_with_periods(
    hass: HomeAssistant, site: Any, raw_period: Any, raw_date: Any
) -> tuple[dict[str, Any], list[EnergyHour] | None]:
    """`history_answer` and the energy periods it was made from (`None` when they could not be read), kept with it,
    so the house pie (`api/house_loads.py`) judges its meters' hours without reading the period again."""
    controller = site_controller_for(hass, site.entry_id)
    if site.state is not ConfigEntryState.LOADED or controller is None:
        raise PowerMonthRefusal(ERROR_NO_SITE, "The site is not loaded")
    now = controller.demand.now()
    tz = controller.demand.tracker.tz
    today = local_date(now, tz)
    try:
        period = requested_period(raw_period, raw_date, today)
    except ValueError as err:
        raise PowerMonthRefusal(ERROR_INVALID_DATE, "The period is not one the house card can show") from err
    complete = period.end <= today
    mark: Any = today if complete else (five_slot(now) if period.kind == PERIOD_DAY else hour_start(now, tz))
    cache: dict[tuple[Any, ...], tuple[Any, dict[str, Any], list[EnergyHour] | None]] = hass.data.setdefault(_CACHE, {})
    key = (site.entry_id, period.kind, period.first)
    held = cache.get(key)
    if held is not None and held[0] == mark:
        return held[1], held[2]

    try:
        periods, step, ids = await _read_periods(hass, site.entry_id, controller, period, now)
    except Exception:  # noqa: BLE001 - without the recorder the rest is still shown
        _LOGGER.debug("SpotNav house history: the energy could not be read", exc_info=True)
        periods, step, ids = None, 60, None
    battery = controller.battery_shadow
    has_battery = battery.facts.battery_entity_id is not None
    reviews: list[DayReview] | None = None
    archived: frozenset[date] = frozenset()
    if has_battery:
        held_reviews = {review.day: review for review in battery.plan.reviews if period.contains(review.day)}
        wanted = [day for day in period.days() if day < today and day not in held_reviews]
        found: dict[date, DayReview] = {}
        if wanted:
            folder = Path(hass.config.config_dir) / ARCHIVE_DIR / site.entry_id
            try:
                found = await hass.async_add_executor_job(read_archived_reviews, folder, wanted)
            except Exception:  # noqa: BLE001 - the archive is a bonus
                _LOGGER.debug("SpotNav house history: the day archive could not be read", exc_info=True)
        reviews = [*held_reviews.values(), *found.values()]
        archived = frozenset(found)
    source = site_price_source(hass, site.entry_id)
    area = None if source is None else source.area
    heat = controller.house_forecast.heat
    zones = await heat.async_current_zones()
    try:
        savings, _since, car_hours = await async_day_savings_by_hour(
            hass, site.entry_id, controller, [day for day in period.days() if day <= today]
        )
    except Exception:  # noqa: BLE001 - the rest is still shown
        _LOGGER.debug("SpotNav house history: what SpotNav saved could not be worked out", exc_info=True)
        savings, car_hours = {}, {}
    heat_sources = (1 if heat.meter.found else 0) + len(zones.sources)
    answer = house_history(
        HouseHistoryInputs(
            site_id=site.entry_id,
            now=now,
            tz=tz,
            period=period,
            periods=periods,
            has_solar=bool(ids is not None and ids.solar),
            has_battery=bool(ids is not None and ids.battery_in),
            has_heat=heat_sources > 0,
            heat_sources=heat_sources,
            step_min=step,
            prices=period_price_slots(hass, site.entry_id, period) if periods else (),
            money=None if area is None else (area.currency, area.major_unit, area.minor_unit),
            reviews=reviews,
            archived=archived,
            forecast_mae=forecast_maes(controller.house_forecast.log),
            savings=savings,
            car_hours=car_hours,
            **_arbitrage(controller, bool(ids is not None and ids.battery_in)),
        )
    )
    cache.pop(key, None)
    cache[key] = (mark, answer, periods)
    while len(cache) > CACHE_SIZE:
        del cache[next(iter(cache))]
    return answer, periods


@callback
def forget_cached_history(hass: HomeAssistant, site_entry_id: str) -> None:
    """Drop a site's kept answers (its unload)."""
    cache: dict[tuple[Any, ...], Any] | None = hass.data.get(_CACHE)
    if cache:
        for key in [key for key in cache if key[0] == site_entry_id]:
            del cache[key]


@websocket_api.websocket_command(
    {
        vol.Required("type"): "spotnav/get_house_history",
        vol.Optional("api_version"): object,
        vol.Optional("charger_id"): object,
        vol.Optional("site_id"): object,
        vol.Optional("period"): object,
        vol.Optional("date"): object,
    }
)
@websocket_api.async_response
async def websocket_get_house_history(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """The house card's history of one site and period."""
    version = msg.get("api_version")
    if type(version) is not int or version != HOUSE_HISTORY_API_VERSION:
        send_unsupported_version(connection, msg, HOUSE_HISTORY_API_VERSION)
        return
    try:
        site = _site_of_request(hass, msg)
        answer = await history_answer(hass, site, msg.get("period"), msg.get("date"))
    except PowerMonthRefusal as refusal:
        connection.send_error(msg["id"], refusal.code, refusal.message)
        return
    connection.send_result(msg["id"], answer)


@callback
def async_setup_house_history_api(hass: HomeAssistant) -> None:
    """Register the command once for the domain."""
    websocket_api.async_register_command(hass, websocket_get_house_history)
