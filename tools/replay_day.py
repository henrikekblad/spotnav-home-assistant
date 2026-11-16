#!/usr/bin/env python3
"""Re-run the battery plan for an exported day (the `spotnav.export_day` action's file) with today's planner.

    .venv/bin/python tools/replay_day.py /config/spotnav_archive/<site_id>/<YYYY-MM-DD>.json [--json] [--once]
        [--hindsight] [--measured-soc]

The day's quarter hours are planned again from what the record holds, and the new actions are reviewed the way the
night's review does it (`battery/plan_shadow.review_day`: each mode's actions run against the measured load and sun
from the measured SoC, against plain self-consumption). The report puts the recorded saving per mode beside the
replayed one, so a change to the planner shows what it would have earned on a real day.

The inputs, per quarter hour: the buy and sell prices the plan really planned against (the record's quarter hours;
the price table, with the fees as at the export, where a quarter hour has none), the house as the plan counts on it
(the logged first forecast of `house_kw_median`, else `house_kw`), the sun as forecast (`sun_kwh_corrected`, else
`sun_kwh`), the cars as measured (their plans are not kept) and the import cap of the quarter hour. By default the
plan is made again at each local hour over the rest of the day, from the SoC the replayed actions left (the real plan
saw to the end of the published prices: the horizon here is shorter, so its last hours differ); `--measured-soc`
plans from the SoC the battery really read, as the day's plans did (on a battery that ran its own way that SoC is
not the plan's, so the review's figure is the plan's on someone else's battery); `--once` plans once at midnight; `--hindsight` plans on the measured house and sun instead of the forecasts (what a perfect forecast would
have earned). The battery is the record's settings (as at the export) and its floor and round trip.

Only the pure planner modules are imported, never Home Assistant's runtime; the Python of the test environment
(`.venv`, with the `homeassistant` package installed) runs it. Exit status: 0 done, 2 the record cannot be replayed.
"""

from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import date, datetime, timezone
import importlib
import json
from pathlib import Path
import sys
import types
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
SCHEMA = 1
HOUSE_KINDS = ("house_kw_median", "house_kw")
SUN_KINDS = ("sun_kwh_corrected", "sun_kwh")


def _modules() -> tuple[Any, Any, Any]:
    """`plan_shadow`, `plan` and `plan_model`, without running the integration's `__init__`."""
    for name, path in (
        ("custom_components", ROOT / "custom_components"),
        ("custom_components.spotnav", ROOT / "custom_components" / "spotnav"),
    ):
        if name not in sys.modules:
            package = types.ModuleType(name)
            package.__path__ = [str(path)]
            sys.modules[name] = package
    base = "custom_components.spotnav.battery."
    return (
        importlib.import_module(base + "plan_shadow"),
        importlib.import_module(base + "plan"),
        importlib.import_module(base + "plan_model"),
    )


class ReplayError(ValueError):
    """The record cannot be replayed (said why)."""


def _instant(text: str) -> datetime:
    return datetime.fromisoformat(text).astimezone(timezone.utc)


def _by_hour(record: dict[str, Any], kinds: tuple[str, ...]) -> dict[datetime, float]:
    """The first logged forecast per hour of the first of [kinds] that has any."""
    logged = ((record.get("forecasts") or {}).get("kinds")) or {}
    for kind in kinds:
        rows = logged.get(kind)
        if rows:
            return {_instant(row[0]): float(row[1]) for row in rows if row[1] is not None}
    return {}


def _price_table(record: dict[str, Any]) -> list[tuple[datetime, datetime, float | None, float | None]]:
    prices = record.get("prices") or {}
    columns = prices.get("columns") or ["start", "end", "spot", "buy", "sell"]
    out = []
    for row in prices.get("rows") or []:
        item = dict(zip(columns, row))
        out.append((_instant(item["start"]), _instant(item["end"]), item.get("buy"), item.get("sell")))
    return out


def battery_of(record: dict[str, Any], model: Any) -> Any:
    battery = (record.get("settings") or {}).get("battery")
    if not isinstance(battery, dict):
        raise ReplayError("the record has no battery the plan could see")
    return model.BatteryParams(
        usable_kwh=float(battery["usable_kwh"]),
        max_charge_kw=float(battery["max_charge_kw"]),
        max_discharge_kw=float(battery["max_discharge_kw"]),
        min_soc_pct=float(battery["min_soc_pct"]),
        max_soc_pct=float(battery.get("max_soc_pct", 100.0)),
        round_trip_efficiency=float(battery["round_trip_efficiency"]),
    )


def _hour_of(moment: datetime, zone: Any) -> datetime:
    """The local hour's start `moment` falls in, in UTC."""
    local = moment.astimezone(zone)
    return local.replace(minute=0, second=0, microsecond=0).astimezone(timezone.utc)


def build_slots(
    record: dict[str, Any], buckets: dict[datetime, Any], shadow: Any, model: Any, *, hindsight: bool
) -> list[Any]:
    """Every quarter hour of the day as a plan slot (see the module); stops at the first one without a price."""
    zone = shadow.time_zones.require_zone(record["tz"])
    start, end = _instant(record["start"]), _instant(record["end"])
    house, sun = _by_hour(record, HOUSE_KINDS), _by_hour(record, SUN_KINDS)
    table = _price_table(record)
    cap_default = (record.get("settings") or {}).get("import_cap_kw")
    slots = []
    moment = start
    while moment < end:
        bucket = buckets.get(moment)
        buy = None if bucket is None else bucket.buy
        sell = None if bucket is None else bucket.sell
        if buy is None or sell is None:
            row = next((item for item in table if item[0] <= moment < item[1]), None)
            buy, sell = (None, None) if row is None else (row[2], row[3])
        if buy is None or sell is None:
            break
        measured = None if bucket is None else bucket.net_house_kw
        hour = _hour_of(moment, zone)
        if hindsight and measured is not None:
            house_kw, solar_kw = max(0.0, measured), max(0.0, -measured)
        else:
            house_kw = house.get(hour, max(0.0, measured or 0.0))
            solar_kw = sun.get(hour, 0.0)
        car_kw = 0.0
        if bucket is not None and bucket.covered:
            car_kw = bucket.scaled(bucket.car_kwh) / (shadow.BUCKET.total_seconds() / 3600.0)
        cap = cap_default if bucket is None or bucket.cap_kw is None else bucket.cap_kw
        slots.append(
            model.PlanSlot(
                start=moment,
                end=moment + shadow.BUCKET,
                buy=float(buy),
                sell=float(sell),
                house_kw=max(0.0, float(house_kw)),
                solar_kw=max(0.0, float(solar_kw)),
                car_kw=max(0.0, car_kw),
                import_cap_kw=cap,
            )
        )
        moment += shadow.BUCKET
    return slots


def _advance(soc: float, decisions: Any, buckets: dict[datetime, Any], battery: Any, shadow: Any, plan: Any) -> float:
    """The replayed battery's SoC after [decisions] run against the measured quarter hours, as the review runs them
    (`plan_shadow.review_day`: a quarter hour the meters lost leaves the battery as it was)."""
    kwh = min(battery.max_kwh, max(battery.min_kwh, battery.usable_kwh * soc / 100))
    for decision in decisions:
        bucket = buckets.get(decision.start)
        if bucket is None or not bucket.covered or bucket.buy is None or bucket.sell is None:
            continue
        slot = shadow._actual_slot(bucket)  # noqa: SLF001 - the review's own measured slot
        power = plan.action_power(slot, battery, kwh, decision.action, decision.battery_kw)
        kwh = plan.run_slot(slot, battery, kwh, decision.action, power).soc_end_pct / 100 * battery.usable_kwh
    return kwh / battery.usable_kwh * 100


def replan(
    slots: list[Any], buckets: dict[datetime, Any], battery: Any, start_soc: float, modes: list[str], wear: float,
    shadow: Any, plan: Any, model: Any, zone: Any, *, once: bool, measured_soc: bool,
) -> dict[datetime, dict[str, tuple[str, float]]]:
    """Each quarter hour's action and kW per mode: planned again each local hour (or once) from the replayed
    battery's SoC (with `measured_soc`, the SoC the real battery read then, as the plans of the day did)."""
    actions: dict[datetime, dict[str, tuple[str, float]]] = {}
    for mode in modes:
        definition = model.MODE_DEFS[mode]
        soc = start_soc
        running: tuple[str, float] | None = None
        index = 0
        while index < len(slots):
            first = slots[index]
            if index and measured_soc:
                bucket = buckets.get(first.start)
                if bucket is not None and bucket.soc_start is not None:
                    soc = float(bucket.soc_start)
            request = model.PlanRequest(slots=tuple(slots[index:]), battery=battery, soc_now_pct=min(100.0, max(0.0, soc)))
            baseline = plan.self_consumption(request)
            gain = shadow.keep_min_gain(baseline.cost, sum(slot.hours for slot in request.slots))
            made = plan.plan_mode(request, definition, wear, keep=running, keep_min_gain=gain, baseline=baseline)
            decisions = made.plan.decisions
            hour = _hour_of(first.start, zone)
            taken = len(decisions) if once else sum(1 for d in decisions if _hour_of(d.start, zone) == hour)
            for decision in decisions[:taken]:
                actions.setdefault(decision.start, {})[mode] = (decision.action, decision.battery_kw)
            soc = _advance(soc, decisions[:taken], buckets, battery, shadow, plan)
            last = decisions[taken - 1]
            running = (last.action, last.battery_kw)
            index += taken
    return actions


def replay(
    record: dict[str, Any], *, once: bool = False, hindsight: bool = False, measured_soc: bool = False
) -> dict[str, Any]:
    """The recorded review and the replayed one (see the module)."""
    if not isinstance(record, dict) or record.get("kind") != "spotnav_day":
        raise ReplayError("not a SpotNav day record")
    if record.get("schema") != SCHEMA:
        raise ReplayError(f"schema {record.get('schema')} is not {SCHEMA}")
    shadow, plan, model = _modules()
    battery = battery_of(record, model)
    rows = (record.get("quarters") or {}).get("rows") or []
    buckets = {bucket.start: bucket for bucket in (shadow.Bucket.from_row(row) for row in rows) if bucket is not None}
    if not buckets:
        raise ReplayError("the record has no quarter hours (kept three days): nothing measured to replay against")
    recorded = shadow.DayReview.from_dict(record.get("battery_review"))
    day = date.fromisoformat(record["date"])
    tz = record["tz"]
    zone = shadow.time_zones.require_zone(tz)
    socs = [b.soc_start for _start, b in sorted(buckets.items()) if b.soc_start is not None]
    if recorded is not None:
        start_soc = recorded.start_soc_pct
    elif socs:
        start_soc = float(socs[0])
    else:
        raise ReplayError("no SoC was measured that day")
    site_mode = (recorded.mode if recorded is not None else None) or (record.get("settings") or {}).get("battery_mode")
    planning = [name for name in model.MODES if model.MODE_DEFS[name].plans]
    mode = site_mode if site_mode in planning else model.DEFAULT_MODE
    currency = (record.get("prices") or {}).get("currency") or (record.get("settings") or {}).get("currency")
    wear = model.wear_minor_per_kwh(currency) or model.DEFAULT_CYCLE_COST

    slots = build_slots(record, buckets, shadow, model, hindsight=hindsight)
    if not slots:
        raise ReplayError("the day's first quarter hour has no price")
    actions = replan(
        slots, buckets, battery, start_soc, planning, wear, shadow, plan, model, zone, once=once, measured_soc=measured_soc
    )
    replayed_buckets = []
    for start, bucket in buckets.items():
        chosen = actions.get(start)
        if chosen is None:
            replayed_buckets.append(replace(bucket, ran_action=None, ran_kw=None, ran_soc=None, ran_modes={}))
            continue
        action, kw = chosen[mode]
        replayed_buckets.append(replace(bucket, ran_action=action, ran_kw=kw, ran_soc=None, ran_modes=dict(chosen)))
    now = datetime.now(timezone.utc)
    replayed = shadow.review_day(day, replayed_buckets, battery, tz, now, planning, mode)
    if replayed is None:
        raise ReplayError("no quarter hour of the day was measured with its prices")

    def figures(review: Any) -> dict[str, Any] | None:
        if review is None:
            return None
        return {
            "saving": round(review.saving, 2),
            "mode_savings": {name: round(value, 2) for name, value in sorted(review.mode_savings.items())},
            "actual_saving": round(review.actual_saving, 2),
            "self_consumption_net": round(review.net(review.self_consumption), 2),
            "plan_end_soc_pct": round(review.plan.end_soc_pct, 1),
            "covered": review.covered,
            "unplanned": review.unplanned,
        }

    before, after = figures(recorded), figures(replayed)
    return {
        "date": record["date"],
        "site_id": record.get("site_id"),
        "mode": mode,
        "replanned": "once" if once else "hourly",
        "soc": "measured" if measured_soc else "replayed",
        "inputs": "measured" if hindsight else "forecast",
        "slots": len(slots),
        "recorded": before,
        "replayed": after,
        "difference": None if before is None else round(after["saving"] - before["saving"], 2),
        "currency": currency,
    }


def render(result: dict[str, Any]) -> str:
    unit = f"minor {result['currency'] or 'currency'} units"
    lines = [
        f"{result['date']} (site {result['site_id']}), mode {result['mode']}: replanned {result['replanned']} from the "
        f"{result['soc']} SoC on the {result['inputs']} house and sun, {result['slots']} quarter hours; savings "
        f"against self-consumption in {unit}",
    ]
    recorded, replayed = result["recorded"], result["replayed"]
    if recorded is None:
        lines.append("  recorded: no review in the record (the night's review had not run)")
    else:
        lines.append(f"  recorded: saving {recorded['saving']}, per mode {recorded['mode_savings']}")
    lines.append(f"  replayed: saving {replayed['saving']}, per mode {replayed['mode_savings']}")
    if result["difference"] is not None:
        lines.append(f"  difference (replayed - recorded): {result['difference']}")
    lines.append(f"  the battery's own way earned {replayed['actual_saving']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Re-run the battery plan for an exported SpotNav day.")
    parser.add_argument("record", help="the day record JSON file")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--once", action="store_true", help="plan once at midnight, not again each hour")
    parser.add_argument("--hindsight", action="store_true", help="plan on the measured house and sun")
    parser.add_argument(
        "--measured-soc", action="store_true", help="plan each hour from the SoC the battery read, not the replayed one"
    )
    args = parser.parse_args(argv)
    try:
        with open(args.record, encoding="utf-8") as handle:
            record = json.load(handle)
        result = replay(record, once=args.once, hindsight=args.hindsight, measured_soc=args.measured_soc)
    except (OSError, ValueError) as err:
        print(f"Cannot replay {args.record}: {err}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2) if args.json else render(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
