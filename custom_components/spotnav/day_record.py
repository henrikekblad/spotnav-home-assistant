"""One site's local day as SpotNav decided and measured it, in one versioned record (SpotNav 3.0).

Built by `day_record` from what the site holds in memory, on the event loop and without I/O (a day of rows is a
few thousand small values); the `export_day` service writes it to `<config>/spotnav_archive/<site_id>/<date>.json`
(`day_export.py`), and the debug bundle carries the last `BUNDLE_DAYS` days. A later decision engine can re-run the
day from it (`tools/replay_day.py`), and `tools/spotnav_archive_to_influx.py` writes it to InfluxDB.

What it holds (`SCHEMA` 1; every instant in UTC as ISO 8601, the day's zone in `tz`; units in `units`):

* `forecasts`: per kind of the site's forecast log (`site/forecast_log.py`) each hour of the day as
  `[hour, value, made_at, outcome]` (the first forecast made for it, when, and what came), and the day's review.
* `prices`: each price interval of the day with its spot price, and the buy and sell prices with the fees as they
  are set now (the fees of the day itself are not kept: `fees_as_of` says so); the quarter hours (below) carry the
  buy and sell prices the battery plan really planned against.
* `quarters`: the battery plan's quarter hours (`battery/plan_shadow.Bucket`, its own row shape under `columns`):
  the meters (import, export, car, battery in kWh), the SoC, the cap, and the plan in force per mode when each began.
* `battery_review`: the night's review of the day with its baselines; `plans_held`: the plans per mode held now,
  cut to the day (only a day still running or to come has any).
* `house_hours`: per hour the net import, the house's share, the cars' share, `grid - car - battery`, the house's
  whole load, the heat pump's metered use and the outdoor temperature, where present.
* `settings`: the site's fuse, margin, import target and cap, the battery as the plan sees it, the mode and the fees,
  as they are at the export (the record says so).
* `switch_load`, `export_curtail`: the shadow logs of the day and the day's tally; `chargers`: a summary of each
  member charger's plug-ins that touched the day.

Each part is kept for its own time (`KEPT_DAYS`): a day older than a part's keeps has none of it, and `missing` names
each part that has nothing and why. No location, no secret, no entity id beyond what the bundle already carries: the
switched load's entity is left out of its entries, the solar statistics' ids out of the plan's inputs.
"""

from __future__ import annotations

import copy
from collections.abc import Iterable, Mapping
from dataclasses import fields
from datetime import date, datetime, timedelta, timezone
import math
from typing import Any, Final

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .core.parse import aware_instant
from . import time_zones
from .const import CONF_CHARGER_ENTRY_IDS, CONF_MAIN_FUSE_A
from .site.import_target import stored_import_target_kw

#: The record's shape; a reader checks it.
SCHEMA: Final = 1
#: What the debug bundle carries: the last completed days.
BUNDLE_DAYS: Final = 3
#: The quarter hours' columns (`plan_shadow.Bucket.as_row`).
QUARTER_COLUMNS: Final = (
    "start",
    "buy",
    "sell",
    "cap_kw",
    "import_kwh",
    "export_kwh",
    "car_kwh",
    "battery_kwh",
    "covered_s",
    "soc_start",
    "soc_end",
    "first_action",
    "first_soc",
    "ran_action",
    "ran_kw",
    "ran_soc",
    "ran_modes",
    "battery_ac_kwh",
)
FORECAST_COLUMNS: Final = ("hour", "value", "made_at", "outcome")
PRICE_COLUMNS: Final = ("start", "end", "spot", "buy", "sell")
HOUSE_COLUMNS: Final = (
    "start",
    "import_kw",
    "house_kw",
    "car_kw",
    "ex_battery_kw",
    "whole_load_kw",
    "heat_kw",
    "outdoor_c",
    "incomplete",
    "imported",
)
PLAN_COLUMNS: Final = ("start", "end", "action", "battery_kw", "soc_end_pct")
UNITS: Final = {
    "price": "minor currency unit per kWh (öre, cent)",
    "money": "minor currency unit",
    "power": "kW (battery + charging, grid + import)",
    "energy": "kWh",
    "soc": "%",
    "temperature": "°C",
    "time": "UTC instants, ISO 8601; the local day in tz",
    "forecast": "per kind: house_* / import_kw / heat_kw in kW, sun_* in kWh in the hour, battery in % SoC",
}
#: How long each part is kept, in days (the house hours: the running and the previous month).
KEPT_DAYS: Final = {
    "forecasts": 14,
    "quarters": 3,
    "battery_review": 30,
    "house_hours": 28,
    "prices": 400,
    "switch_load": 7,
    "export_curtail": 7,
}

# Why a part has nothing.
MISSING_NOT_KEPT: Final = "not_kept"
MISSING_NONE: Final = "none_recorded"
MISSING_NOT_REVIEWED: Final = "not_reviewed_yet"
MISSING_NO_BATTERY: Final = "no_battery"
MISSING_NO_PRICES: Final = "no_prices"
MISSING_NO_LOAD: Final = "no_switched_load"


def day_bounds(day: date, tz: str) -> tuple[datetime, datetime]:
    """The local day's first and next day's first instant, in UTC."""
    zone = time_zones.require_zone(tz)
    following = day + timedelta(days=1)
    return (
        datetime(day.year, day.month, day.day, tzinfo=zone).astimezone(timezone.utc),
        datetime(following.year, following.month, following.day, tzinfo=zone).astimezone(timezone.utc),
    )


def _in(moment: datetime, start: datetime, end: datetime) -> bool:
    return start <= moment < end


def _round(value: Any, digits: int = 3) -> Any:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return round(float(value), digits)


def _missing(missing: dict[str, str], part: str, day: date, today: date, empty_reason: str = MISSING_NONE) -> None:
    """Name [part] as missing: not kept any more when the day is older than its keeps, else [empty_reason]."""
    kept = KEPT_DAYS.get(part)
    missing[part] = MISSING_NOT_KEPT if kept is not None and (today - day).days > kept else empty_reason


# ------------------------------------------------------------------------------------------------ the parts


def forecasts_part(log: Any, start: datetime, end: datetime, day: date) -> dict[str, Any]:
    """Each kind's rows of the day and its review of the day (`ForecastLog`)."""
    kinds: dict[str, list[list[Any]]] = {}
    reviews: dict[str, Any] = {}
    for kind, body in sorted(log.kinds.items()):
        rows = [hour.as_row(hour_start) for hour_start, hour in sorted(body.hours.items()) if _in(hour_start, start, end)]
        if rows:
            kinds[kind] = rows
        review = next((item for item in body.reviews if item.day == day), None)
        if review is not None:
            reviews[kind] = review.as_dict()
    return {"columns": list(FORECAST_COLUMNS), "kinds": kinds, "reviews": reviews}


def quarters_part(buckets: Iterable[Any], start: datetime, end: datetime) -> list[list[Any]]:
    return [bucket.as_row() for bucket in sorted(buckets, key=lambda b: b.start) if _in(bucket.start, start, end)]


def plans_part(plan: Any, start: datetime, end: datetime) -> dict[str, list[list[Any]]]:
    """The plans held now, per mode (the site's in force under its mode), cut to the day."""
    held = {plan.mode: plan.plan, **{mode: slots for mode, slots in plan.mode_plans.items() if mode != plan.mode}}
    out: dict[str, list[list[Any]]] = {}
    for mode, slots in sorted(held.items()):
        rows = [slot.as_row() for slot in slots if slot.end > start and slot.start < end]
        if rows:
            out[mode] = rows
    return out


def review_part(review: Any) -> dict[str, Any]:
    """The day's battery review with its derived figures (`plan_shadow.DayReview`)."""
    out = review.as_dict()
    out["battery_saving"] = _round(review.battery_saving, 2)
    out["sun_value"] = _round(review.sun_value, 2)
    out["against_nothing"] = _round(review.against_nothing, 2)
    return out


def house_part(
    tracked: Iterable[Any],
    history: Any,
    heat_by_hour: Mapping[datetime, float],
    start: datetime,
    end: datetime,
) -> list[list[Any]]:
    """Per hour of the day (see the module), from the demand tracker's hours, the house history and the heat meter."""
    by_start = {hour.start.astimezone(timezone.utc): hour for hour in tracked if _in(hour.start, start, end)}
    starts = set(by_start)
    starts.update(moment for moment in history.rows if _in(moment, start, end))
    starts.update(moment for moment in history.gross if _in(moment, start, end))
    starts.update(moment for moment in heat_by_hour if _in(moment, start, end))
    rows: list[list[Any]] = []
    for moment in sorted(starts):
        hour = by_start.get(moment)
        kept = history.rows.get(moment)
        gross = history.gross.get(moment)
        house = hour.house_kw if hour is not None else (None if kept is None else kept[0])
        net = None if hour is None else hour.kw
        outdoor = hour.outdoor_c if hour is not None and hour.outdoor_c is not None else (None if kept is None else kept[1])
        rows.append(
            [
                moment.isoformat(),
                _round(net),
                _round(house),
                _round(net - house) if net is not None and house is not None else None,
                None if hour is None else _round(hour.ex_battery_kw),
                None if gross is None else _round(gross[0]),
                _round(heat_by_hour.get(moment)),
                _round(outdoor, 1),
                None if hour is None else bool(hour.incomplete),
                None if hour is None else bool(hour.imported),
            ]
        )
    return rows


def fees_part(fiscal: Any) -> dict[str, Any] | None:
    """The fees as numbers and switches (`planner.FiscalChoice`); the high-load window as text."""
    if fiscal is None:
        return None
    out: dict[str, Any] = {}
    for item in fields(fiscal):
        value = getattr(fiscal, item.name)
        out[item.name] = value if value is None or isinstance(value, (bool, int, float, str)) else str(value)
    return out


def prices_part(hass: HomeAssistant, source: Any, start: datetime, end: datetime, day: date) -> dict[str, Any] | None:
    """The day's price intervals (see the module) from the documents held (nothing is fetched); `None` without any."""
    from .battery.plan import buy_prices, sell_prices  # noqa: PLC0415 - the planner imports the runtime
    from .planning.planner import FiscalChoice, PlannerInputError  # noqa: PLC0415
    from .pricing.price_points import price_points  # noqa: PLC0415
    from .runtime import domain_data  # noqa: PLC0415

    repository = domain_data(hass).price_repository
    if repository is None or source is None or source.area is None:
        return None
    documents = []
    for offset in (-1, 0, 1):
        document = repository.day_snapshot(source.area_id, day + timedelta(days=offset)).document
        if document is not None:
            documents.append(document)
    fiscal = source.fiscal
    try:
        # Without the fees (not resolved now) the spot prices are still read; buy and sell are then unknown.
        points = price_points(documents, currency=source.area.currency, fiscal=fiscal or FiscalChoice())
    except PlannerInputError:
        points = ()
    points = sorted((point for point in points if _in(point.start, start, end)), key=lambda point: point.start)
    if not points:
        return None
    spots = [point.spot for point in points]
    buys: list[float | None] = (
        list(buy_prices(spots, [point.start for point in points], fiscal)) if fiscal else [None] * len(points)
    )
    sells: list[float | None] = (
        list(sell_prices(spots, fiscal.sell_adjustment)) if fiscal else [None] * len(points)
    )
    return {
        "area": source.area_id,
        "currency": source.area.currency,
        "fees_as_of": "export",
        "fees": fees_part(fiscal),
        "markup": None if fiscal is None else fiscal.markup_minor_per_kwh,
        "sell_adjustment": None if fiscal is None else fiscal.sell_adjustment,
        "columns": list(PRICE_COLUMNS),
        "rows": [
            [point.start.isoformat(), point.end.isoformat(), _round(spot * 100, 4), _round(buy, 4), _round(sell, 4)]
            for point, spot, buy, sell in zip(points, spots, buys, sells)
        ],
    }


def _entry_without_entity(entry: Any) -> dict[str, Any]:
    out = entry.as_dict()
    out.pop("entity_id", None)
    return out


def shadow_part(entries: Iterable[Any], part: str, start: datetime, end: datetime) -> list[dict[str, Any]]:
    """A shadow book's entries of [part] in the day, without the entity id."""
    return [_entry_without_entity(entry) for entry in entries if entry.part == part and _in(entry.at, start, end)]


def charger_part(hass: HomeAssistant, entry_ids: Iterable[str], start: datetime, end: datetime) -> list[dict[str, Any]]:
    """Each member charger's plug-ins that touched the day: when, the strategy and its changes, how many of each log."""
    from .runtime import charger_data  # noqa: PLC0415

    out: list[dict[str, Any]] = []
    for entry_id in entry_ids:
        data = charger_data(hass, entry_id)
        day_log = None if data is None else data.debug_day
        if day_log is None:
            continue
        sessions = []
        for session in day_log.sessions():
            plugged = aware_instant(session.get("plugged_in_at"))
            unplugged = aware_instant(session.get("unplugged_at"))
            if (unplugged is not None and unplugged < start) or (plugged is not None and plugged >= end):
                continue
            sessions.append(
                {
                    "plugged_in_at": session.get("plugged_in_at"),
                    "unplugged_at": session.get("unplugged_at"),
                    "strategy": session.get("strategy"),
                    "strategy_changes": session.get("strategy_changes") or [],
                    "entries": {
                        name: len(items)
                        for name, items in session.items()
                        if isinstance(items, list) and name != "strategy_changes"
                    },
                }
            )
        entry = hass.config_entries.async_get_entry(entry_id)
        out.append({"entry_id": entry_id, "title": None if entry is None else entry.title, "sessions": sessions})
    return out


def settings_part(hass: HomeAssistant, entry: ConfigEntry, controller: Any, source: Any) -> dict[str, Any]:
    """The site's settings the day's decisions read, as they are now."""
    from .battery.plan_runtime import limited_params, site_import_cap_kw  # noqa: PLC0415
    from .battery.learned import with_learned  # noqa: PLC0415
    from .battery.plan_shadow import battery_params  # noqa: PLC0415
    from .core.electric import stored_safety_margin_a  # noqa: PLC0415
    from .planning.grid_voltage import stored_voltage_between_phases_v  # noqa: PLC0415

    config = controller.config
    shadow = controller.battery_shadow
    plan = shadow.plan
    facts = shadow.facts
    if facts.battery_entity_id is not None:
        facts = with_learned(facts, plan.learned)
    params, reason, estimates = limited_params(*battery_params(facts), facts)
    inputs = copy.deepcopy(plan.inputs)
    inputs.pop("solar_statistics", None)
    return {
        "as_of": "export",
        "main_fuse_a": config.get(CONF_MAIN_FUSE_A),
        "safety_margin_a": stored_safety_margin_a(config),
        "volts_between_phases": stored_voltage_between_phases_v(config),
        "import_target_kw": stored_import_target_kw(entry.data),
        "import_cap_kw": _round(site_import_cap_kw(hass, controller)),
        "battery_mode": plan.mode,
        "battery": None
        if params is None
        else {
            "usable_kwh": params.usable_kwh,
            "max_charge_kw": params.max_charge_kw,
            "max_discharge_kw": params.max_discharge_kw,
            "min_soc_pct": params.min_soc_pct,
            "max_soc_pct": params.max_soc_pct,
            "round_trip_efficiency": params.round_trip_efficiency,
            "sun_round_trip_efficiency": params.sun_round_trip_efficiency,
            "estimates": list(estimates),
        },
        "battery_reason": reason,
        "battery_brand": facts.brand,
        "price_area": None if source is None else source.area_id,
        "currency": None if source is None or source.area is None else source.area.currency,
        "fees": fees_part(None if source is None else source.fiscal),
        "last_plan_inputs": inputs,
    }


# ------------------------------------------------------------------------------------------------ the record


def day_record(
    hass: HomeAssistant,
    entry: ConfigEntry,
    controller: Any,
    day: date,
    now: datetime,
    *,
    version: str | None = None,
    chargers: bool = True,
) -> dict[str, Any]:
    """The site's record of [day] (see the module), from what it holds now. Runs on the event loop; no I/O."""
    from .battery.plan_runtime import site_price_source_of  # noqa: PLC0415
    from .battery.plan_shadow import day_quarters  # noqa: PLC0415
    from .battery.shadow import PART_EXPORT_CURTAIL, PART_SWITCH_LOAD  # noqa: PLC0415

    tz = controller.demand.tracker.tz
    start, end = day_bounds(day, tz)
    today = now.astimezone(time_zones.require_zone(tz)).date()
    missing: dict[str, str] = {}
    house = controller.house_forecast
    shadow = controller.battery_shadow
    plan = shadow.plan
    has_battery = shadow.facts.battery_entity_id is not None or bool(plan.actuals.buckets)
    source = site_price_source_of(hass, controller)

    forecasts = forecasts_part(house.log, start, end, day)
    if not forecasts["kinds"]:
        _missing(missing, "forecasts", day, today)

    prices = prices_part(hass, source, start, end, day)
    if prices is None:
        _missing(missing, "prices", day, today, MISSING_NO_PRICES)

    quarters = quarters_part(plan.actuals.buckets.values(), start, end)
    expected = day_quarters(day, tz)
    if not quarters:
        _missing(missing, "quarters", day, today, MISSING_NONE if has_battery else MISSING_NO_BATTERY)
    review = next((item for item in plan.reviews if item.day == day), None)
    if review is None:
        reason = MISSING_NOT_REVIEWED if day >= today - timedelta(days=1) else MISSING_NONE
        _missing(missing, "battery_review", day, today, reason if has_battery else MISSING_NO_BATTERY)

    tracker = controller.demand.tracker
    heat = house.heat.by_hour if house.heat.meter.found else {}
    house_hours = house_part((*tracker.previous, *tracker.hours), house.history, heat, start, end)
    if not house_hours:
        _missing(missing, "house_hours", day, today)

    load = controller.switch_load
    switch_load: dict[str, Any] | None = None
    if load.config is None:
        missing["switch_load"] = MISSING_NO_LOAD
    else:
        runs = [
            [first.isoformat(), last.isoformat()]
            for first, last in load.history.runs
            if last > start and first < end
        ]
        tally = load.days.get(day.isoformat())
        switch_load = {
            "log": shadow_part(load.book.entries, PART_SWITCH_LOAD, start, end),
            "runs": runs,
            "day": None if tally is None else tally.as_dict(),
            "rated_kw": load.config.rated_kw,
        }
    curtail_count = shadow.curtail.days.get(day.isoformat())
    export_curtail = {
        "log": shadow_part(shadow.book.entries, PART_EXPORT_CURTAIL, start, end),
        "day": None if curtail_count is None else curtail_count.as_dict(),
    }

    record: dict[str, Any] = {
        "schema": SCHEMA,
        "kind": "spotnav_day",
        "site_id": entry.entry_id,
        "site": entry.title,
        "date": day.isoformat(),
        "tz": tz,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "complete": day < today,
        "exported_at": now.astimezone(timezone.utc).isoformat(),
        "spotnav_version": version,
        "units": dict(UNITS),
        "kept_days": dict(KEPT_DAYS),
        "missing": missing,
        "counts": {
            "forecast_rows": sum(len(rows) for rows in forecasts["kinds"].values()),
            "forecast_kinds": len(forecasts["kinds"]),
            "price_intervals": 0 if prices is None else len(prices["rows"]),
            "quarters": len(quarters),
            "quarters_in_day": expected,
            "house_hours": len(house_hours),
            "battery_review": review is not None,
        },
        "settings": settings_part(hass, entry, controller, source),
        "forecasts": forecasts,
        "prices": prices,
        "quarters": {"columns": list(QUARTER_COLUMNS), "rows": quarters},
        "plans_held": {"columns": list(PLAN_COLUMNS), "modes": plans_part(plan, start, end)},
        "battery_review": None if review is None else review_part(review),
        "house_hours": {"columns": list(HOUSE_COLUMNS), "rows": house_hours},
        "switch_load": switch_load,
        "export_curtail": export_curtail,
    }
    if chargers:
        record["chargers"] = charger_part(hass, controller.config.get(CONF_CHARGER_ENTRY_IDS) or [], start, end)
    return record


def bundle_days(
    hass: HomeAssistant, entry: ConfigEntry, controller: Any, now: datetime, *, version: str | None = None
) -> list[dict[str, Any]]:
    """The last `BUNDLE_DAYS` completed days' records for the debug bundle, newest first (the chargers' plug-ins are
    in the bundle already, so left out)."""
    today = now.astimezone(time_zones.require_zone(controller.demand.tracker.tz)).date()
    return [
        day_record(hass, entry, controller, today - timedelta(days=back), now, chargers=False, version=version)
        for back in range(1, BUNDLE_DAYS + 1)
    ]
