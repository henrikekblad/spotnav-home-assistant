"""`spotnav/get_plan`'s past days (plan `plans/v3_decision_history.md`): the Plan tab stepped back, in the live
answer's shape (`site/plan_view.py`), from the decision history's rows (`site/decision_ledger.py`). Pure.

A past local day has every hour of it (23 or 25 on the clock's change), `past` true. Today has its hours gone by from
the history and the running hour on from the live answer, to midnight. An hour gone by:

* `hours[]`: `price` and `sell` as the plan had them at the hour's start, `house_kw` the house measured (the grid less
  the cars and the battery, so less the sun: `house.sun_in_house` is true and `sun_kw` null) less the pool's, the
  spa's and the sauna's measured power (layers of their own, `in_house` false, as live), `net_kw` the grid's
  measured import, `ceiling_kw`/`ceiling` the ceiling that applied at the hour's start, `past` true, `recorded`
  `full`, `partial` (Home Assistant ran only part of it) or `missing` (nothing recorded: every figure null, never
  filled in), `planned_net_kw` the net the plan counted on at the hour's start.
* `loads[]`: `kw` what was measured (`level` what the heat did: -1 lowered, +1 pre-heated), `planned` per hour
  `{what, kw, reason}` (the plan at the hour's start, `null` where there was none), `ran` per hour `{what, kw}` and
  `reason` per hour: what decided it (`ceiling`, `import_target`, `accept_peak`, `person_stop`), else the plan's own
  reason where what ran agrees with the plan, else a hint why it did not (`car_full`, `not_plugged`,
  `car_limited`), else `null`. `runs` are what ran in words with that reason; for a load SpotNav only watched
  (`state` `would`) what the plan said it would do, as live. The running hour on, these three are `null`.
* `summary`: the cost of the measured import (less the export) and the highest measured hour; today's adds the plan's
  hours to midnight.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta, timezone
from typing import Any

from .. import time_zones
from ..planning.flexible_load import (
    KIND_CAR,
    KIND_HEAT,
    KIND_HEAT_ZONE,
    KIND_HOME_BATTERY,
    KIND_POOL_CIRCULATION,
    KIND_POOL_HEAT,
    KIND_SAUNA,
    KIND_SPA,
    KIND_WATER_HEATER,
)
from .decision_ledger import RECORDED_MISSING, HourRow, LoadRow, hour_number
from .plan_view import (
    HEAT_LEVELS,
    HOUR,
    PLAN_API_VERSION,
    QUIET_KW,
    STATE_LIVE,
    STATE_WOULD,
    WHAT_GRID_CHARGE,
    _r,
    _runs,
    history_block,
)

_IN_HOUSE = (KIND_HEAT, KIND_HEAT_ZONE, KIND_WATER_HEATER)
#: Drawn as layers of their own on top of the house (`site/plan_view.py`): in an hour gone by the house measured holds
#: them, so their measured power is taken off its `house_kw` (the stack still sums to what was measured).
_OWN_LAYER = (KIND_POOL_HEAT, KIND_POOL_CIRCULATION, KIND_SPA, KIND_SAUNA)
#: The pool's and the spa's lanes keep the live answer's summary (their figures are the shadow's plan).
_LIVE_SUMMARY = (KIND_POOL_HEAT, KIND_POOL_CIRCULATION, KIND_SPA, KIND_SAUNA)


def day_hours(day: date, tz: str) -> list[datetime]:
    """The local day's hour starts (UTC), 23 or 25 on the clock's change."""
    zone = time_zones.require_zone(tz)
    following = day + timedelta(days=1)
    start = datetime(day.year, day.month, day.day, tzinfo=zone).astimezone(timezone.utc)
    end = datetime(following.year, following.month, following.day, tzinfo=zone).astimezone(timezone.utc)
    return [start + HOUR * index for index in range(int((end - start) / HOUR))]


def _level(kind: str, what: str | None) -> int:
    if kind not in (KIND_HEAT, KIND_HEAT_ZONE) or what is None:
        return 0
    return HEAT_LEVELS.get(what, 0)


def _summary(kind: str, kw: Sequence[float | None], what: Sequence[str | None], level: Sequence[int] | None, live: Mapping[str, Any] | None) -> dict[str, Any]:
    """A load's summary over the day, in the live answer's keys."""
    live_summary = {} if live is None else dict(live.get("summary") or {})
    if kind == KIND_CAR:
        return {
            "kwh": _r(sum(value for value in kw if value is not None and value > QUIET_KW), 2),
            "departure": live_summary.get("departure"),
        }
    if kind == KIND_HOME_BATTERY:
        charged = sum(value for value in kw if value is not None and value > 0)
        grid = sum(value for value, item in zip(kw, what, strict=True) if item == WHAT_GRID_CHARGE and value is not None and value > 0)
        covered = -sum(value for value in kw if value is not None and value < 0)
        return {"charge_kwh": _r(charged, 2), "grid_charge_kwh": _r(grid, 2), "cover_kwh": _r(covered, 2)}
    if kind in _LIVE_SUMMARY:
        return live_summary
    steps = list(level or ())
    if kind == KIND_HEAT:
        return {"lowered_hours": sum(1 for step in steps if step < 0), "raised_hours": sum(1 for step in steps if step > 0)}
    if kind == KIND_HEAT_ZONE:
        return {"lowered_hours": sum(1 for step in steps if step < 0)}
    on = sum(1 for item in what if item == "on")
    known_kw = [value for value in kw if value is not None and value > QUIET_KW]
    return {"on_hours": _r(float(on), 2), "kw": live_summary.get("kw", _r(max(known_kw), 2) if known_kw else None)}


def _hour_row(start: datetime, row: HourRow | None) -> dict[str, Any]:
    if row is None:
        return {
            "start": start.isoformat(),
            "price": None,
            "sell": None,
            "house_kw": None,
            "sun_kw": None,
            "net_kw": None,
            "ceiling_kw": None,
            "ceiling": None,
            "past": True,
            "recorded": RECORDED_MISSING,
            "planned_net_kw": None,
        }
    return {
        "start": start.isoformat(),
        "price": _r(row.price),
        "sell": _r(row.sell),
        "house_kw": _r(row.house_kw),
        "sun_kw": None,
        "net_kw": _r(row.import_kw),
        "ceiling_kw": _r(row.ceiling_kw),
        "ceiling": row.ceiling,
        "past": True,
        "recorded": row.recorded,
        "planned_net_kw": _r(row.planned_net_kw),
    }


def _live_index(live: Mapping[str, Any] | None, hours: Sequence[datetime]) -> dict[datetime, int]:
    """The live answer's hour index for each of `hours` it has."""
    if live is None:
        return {}
    wanted = set(hours)
    out: dict[datetime, int] = {}
    for index, row in enumerate(live.get("hours") or ()):
        moment = datetime.fromisoformat(row["start"]).astimezone(timezone.utc)
        if moment in wanted:
            out[moment] = index
    return out


def _live_what(load: Mapping[str, Any], start: datetime) -> tuple[str | None, str | None]:
    """The live run covering the hour: its `what` and `reason`."""
    for run in load.get("runs") or ():
        if datetime.fromisoformat(run["start"]) <= start < datetime.fromisoformat(run["end"]):
            return run.get("what"), run.get("reason")
    return None, None


def past_view(
    *,
    site_id: str,
    tz: str,
    now: datetime,
    day: date,
    rows: Sequence[tuple[HourRow, Sequence[LoadRow]]],
    history_first: datetime | None,
    live: Mapping[str, Any] | None = None,
    money: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The answer for the local day `day` (see the module); `live` the live answer for today, else `None`."""
    hours = day_hours(day, tz)
    running = hour_number(now)
    by_hour = {row.hour: (row, loads) for row, loads in rows}
    live_at = _live_index(live, hours) if live is not None else {}
    past_hours = [start for start in hours if hour_number(start) < running or live is None]
    future_hours = [start for start in hours if start not in past_hours and start in live_at]
    shown = past_hours + future_hours
    count = len(shown)

    hour_rows: list[dict[str, Any]] = []
    for start in past_hours:
        found = by_hour.get(hour_number(start))
        hour_rows.append(_hour_row(start, None if found is None else found[0]))
    for start in future_hours:
        hour_rows.append(dict(live["hours"][live_at[start]]))  # type: ignore[index]

    # The loads: the live answer's order, then any only the history has, in the order first seen.
    order: list[tuple[str, str]] = []
    names: dict[tuple[str, str], str | None] = {}
    shadow: dict[tuple[str, str], bool] = {}
    live_loads: dict[tuple[str, str], Mapping[str, Any]] = {}
    for load in (live or {}).get("loads") or ():
        key = (str(load["id"]), str(load["kind"]))
        live_loads[key] = load
        order.append(key)
        names[key] = load.get("name")
    for start in past_hours:
        found = by_hour.get(hour_number(start))
        for load_row in () if found is None else found[1]:
            key = (load_row.load_id, load_row.kind)
            if key not in names:
                order.append(key)
                names[key] = load_row.name
            elif names[key] is None:
                names[key] = load_row.name
            shadow[key] = load_row.shadow

    loads: list[dict[str, Any]] = []
    for key in order:
        load_id, kind = key
        live_load = live_loads.get(key)
        by_load = {
            hour: load_row
            for hour, (_row, items) in by_hour.items()
            for load_row in items
            if (load_row.load_id, load_row.kind) == key
        }
        kw: list[float | None] = []
        what: list[str | None] = []
        level: list[int] = []
        planned: list[dict[str, Any] | None] = []
        ran: list[dict[str, Any] | None] = []
        reason: list[str | None] = []
        run_what: list[str | None] = []
        run_reason: list[str | None] = []
        run_kw: list[float | None] = []
        would = shadow.get(key, False) if live_load is None else live_load.get("state") == STATE_WOULD
        for start in past_hours:
            item = by_load.get(hour_number(start))
            if item is None:
                kw.append(None)
                what.append(None)
                level.append(0)
                planned.append(None)
                ran.append(None)
                reason.append(None)
                run_what.append(None)
                run_reason.append(None)
                run_kw.append(None)
                continue
            plan = item.planned
            planned.append(None if plan is None else {"what": plan.what, "kw": _r(plan.kw), "reason": plan.reason})
            ran.append({"what": item.ran_what, "kw": _r(item.ran_kw)})
            reason.append(item.reason)
            if item.shadow:
                # Only watched: what SpotNav would have done, as live; the measured power is what the load drew.
                shown_what = None if plan is None else plan.what
                kw.append(None if kind == KIND_HEAT_ZONE else _r(item.ran_kw))
                level.append(
                    (plan.level if plan.level is not None else _level(kind, plan.what)) if plan is not None else 0
                )
                run_what.append(shown_what)
                run_reason.append(None if plan is None else plan.reason)
                run_kw.append(None if plan is None else plan.kw)
            else:
                shown_what = item.ran_what
                kw.append(None if kind == KIND_HEAT_ZONE else _r(item.ran_kw))
                level.append(_level(kind, item.ran_what))
                run_what.append(item.ran_what)
                run_reason.append(item.reason if item.ran_what is not None else None)
                run_kw.append(item.ran_kw)
            what.append(shown_what)
        runs = _runs(past_hours, run_what, run_kw, run_reason)
        future_what: list[str | None] = []
        future_reason: list[str | None] = []
        future_kw: list[float | None] = []
        for start in future_hours:
            index = live_at[start]
            value = None if live_load is None else live_load["kw"][index]
            step = None if live_load is None or live_load.get("level") is None else live_load["level"][index]
            live_what, live_reason = (None, None) if live_load is None else _live_what(live_load, start)
            kw.append(value)
            level.append(step if isinstance(step, int) else 0)
            what.append(live_what)
            future_what.append(live_what)
            future_reason.append(live_reason)
            future_kw.append(value)
            planned.append(None)
            ran.append(None)
            reason.append(None)
        runs.extend(_runs(future_hours, future_what, future_kw, future_reason))
        heat_like = kind in (KIND_HEAT, KIND_HEAT_ZONE)
        loads.append(
            {
                "id": load_id,
                "kind": kind,
                "name": names.get(key),
                "state": STATE_WOULD if would else STATE_LIVE,
                "in_house": kind in _IN_HOUSE,
                "kw": kw,
                "level": level if heat_like else None,
                "runs": runs,
                "summary": _summary(kind, kw, what, level if heat_like else None, live_load),
                "does_not_fit": None if live_load is None else live_load.get("does_not_fit"),
                "planned": planned,
                "ran": ran,
                "reason": reason,
            }
        )
    for index in range(len(past_hours)):
        row = hour_rows[index]
        if row["house_kw"] is not None:
            own = sum(max(0.0, load["kw"][index] or 0.0) for load in loads if load["kind"] in _OWN_LAYER)
            if own > 0:
                row["house_kw"] = _r(max(0.0, row["house_kw"] - own))

    cost = 0.0
    priced = False
    peak: tuple[float, str, float | None, str | None] | None = None
    for row in hour_rows:
        net, price, sell = row["net_kw"], row["price"], row["sell"]
        if net is not None and price is not None:
            priced = True
            cost += net * price if net >= 0 else net * (sell if sell is not None else 0.0)
        if net is not None and (peak is None or net > peak[0]):
            peak = (net, row["start"], row["ceiling_kw"], row["ceiling"])
    end = hours[-1] + HOUR
    return {
        "api_version": PLAN_API_VERSION,
        "site_id": site_id,
        "time_zone": tz,
        "generated_at": now.isoformat(),
        "money": live.get("money") if live is not None else (None if money is None else dict(money)),
        "window": {
            "start": hours[0].isoformat(),
            "end": end.isoformat() if count == len(hours) else (shown[-1] + HOUR).isoformat(),
            "hours": count,
            "prices_end": None if live is None else live["window"].get("prices_end"),
        },
        "house": {"basis": "net", "sun_in_house": True}
        if live is None
        else dict(live["house"]),
        "power_tariff": False if live is None else bool(live.get("power_tariff")),
        "would": any(load["state"] == STATE_WOULD and load["runs"] for load in loads),
        "month_peak": None if live is None else live.get("month_peak"),
        "summary": {
            "cost": round(cost, 2) if priced else None,
            "peak_kw": None if peak is None else round(peak[0], 2),
            "peak_start": None if peak is None else peak[1],
            "peak_ceiling_kw": None if peak is None else _r(peak[2], 2),
            "peak_ceiling": None if peak is None else peak[3],
            "over": peak is not None and peak[2] is not None and peak[0] > peak[2] + 0.01,
        },
        "hours": hour_rows,
        "loads": loads,
        "past": True,
        "date": day.isoformat(),
        "history": history_block(history_first),
        "not_ready": None if live is None else live.get("not_ready"),
    }
