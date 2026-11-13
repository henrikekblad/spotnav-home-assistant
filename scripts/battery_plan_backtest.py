"""Replay the house battery plan (`battery/plan.py`) on a site's history and compare it with what happened.

Not part of the integration: a research tool, run by hand with the repository's virtual environment
(`.venv/bin/python scripts/battery_plan_backtest.py --cache DIR`). It reads, never writes, anything outside DIR.

Data
* Minute means from InfluxDB 3 (database `ha`, written by `influx3_writer`), forward-filled within up-time (no grid
  sample for 10 min is down-time, as in the load-forecast backtest), then quarter-hour means.
* The day-ahead spot price from ENTSO-E (EUR/MWh) at the ECB's EUR rate of the day before delivery.
* Tokens are read from files (`~/tokens/influx-ha-token`, `~/tokens/entsoe-security-token`) and never printed: an
  HTTP error is reported by its status alone.

Replay
* Each day at 13:00 local (when the next day's prices are out) a plan is made to the end of the next day and run for
  24 h against what really happened; the battery's energy carries from one day to the next. `--every 4` plans again
  every hour instead, each time to the end of the last published day, as a plan running on a site would.
* `perfect`: the plan knows the house's load and the sun exactly.
* `forecast`: the house load is the p75 weekday/weekend x hour profile of the 6 weeks before (half-life 2 weeks, car
  hours left out), the sun is the same quarter hour of the day before. The car is a fixed load SpotNav plans, so both
  replays know it.
* The planned action is run against the real load: self-consumption, hold and discharge-to-house follow the real
  load; a grid charge keeps its planned power. The fuse and export caps are enforced in every slot.
* `self-consumption`: the same battery left to itself. `actual`: the grid meter as it was, under the site's own
  battery control (Sigenergy AI mode on the owner's site).
* Costs are compared with the energy each ends with valued at the period's median buy price x the round trip.
"""

from __future__ import annotations

import argparse
import bisect
import gzip
import json
import math
import os
import statistics
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ElementTree
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from custom_components.spotnav.battery.plan import action_power, plan_battery, run_slot  # noqa: E402
from custom_components.spotnav.battery.plan_model import (  # noqa: E402
    ACTION_SELF_CONSUME,
    BatteryParams,
    PlanRequest,
    PlanSlot,
)

TZ = ZoneInfo("Europe/Stockholm")
INFLUX_URL = "http://influxdb3.internal:8181/api/v3/query_sql"
ENTSOE_URL = "https://web-api.tp.entsoe.eu/api"
ECB_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist-90d.xml"
SE4 = "10Y1001A1001A47J"
SLOT = timedelta(minutes=15)

#: (table, entity) per series of the owner's site. Power in kW: grid + import, battery + charge.
SERIES = {
    "grid_kw": ("kW", "sensor.sigen_plant_grid_active_power"),
    "pv_kw": ("kW", "sensor.sigen_plant_pv_power"),
    "batt_kw": ("kW", "sensor.sigen_plant_battery_power"),
    "load_kw": ("kW", "sensor.sigen_plant_total_load_power"),
    "soc": ("%", "sensor.sigen_plant_battery_state_of_charge"),
    "car_w": ("W", "sensor.billaddning_effekt"),
}
#: Sigenergy's total load reads 0.23 kW below grid + pv - battery (load-forecast backtest); used before it existed.
DERIVED_BIAS_KW = 0.23


def _token(name: str) -> str:
    return Path(os.path.expanduser(f"~/tokens/{name}")).read_text().strip()


def _get(request: urllib.request.Request, what: str) -> bytes:
    try:
        with urllib.request.urlopen(request, timeout=900) as response:
            return response.read()
    except urllib.error.HTTPError as err:
        raise SystemExit(f"{what}: HTTP {err.code}") from None
    except urllib.error.URLError as err:
        raise SystemExit(f"{what}: {type(err.reason).__name__}") from None


def _cached(path: Path, fetch: Callable[[], object]) -> object:
    if path.exists():
        with gzip.open(path, "rt") as handle:
            return json.load(handle)
    data = fetch()
    with gzip.open(path, "wt") as handle:
        json.dump(data, handle)
    return data


# ----------------------------------------------------------------------------- data


def _influx_minutes(table: str, entity: str, start: datetime, end: datetime) -> list[list]:
    token = _token("influx-ha-token")
    rows: list[list] = []
    month = start
    while month < end:
        upto = min(end, month + timedelta(days=31))
        sql = (
            f"SELECT date_bin(interval '1 minute', time) AS t, AVG(value) AS v FROM \"{table}\" "
            f"WHERE entity_id = '{entity}' AND time >= '{month:%Y-%m-%dT%H:%M:%S}' "
            f"AND time < '{upto:%Y-%m-%dT%H:%M:%S}' GROUP BY t ORDER BY t"
        )
        body = json.dumps({"db": "ha", "format": "json", "q": sql}).encode()
        request = urllib.request.Request(
            INFLUX_URL, data=body, headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        )
        rows += [[r["t"], r["v"]] for r in json.loads(_get(request, f"influx {entity}") or b"[]")]
        month = upto
    return rows


def _entsoe_prices(start: datetime, end: datetime) -> dict[str, float]:
    """EUR/MWh per quarter hour (UTC ISO), A03 curves carried forward, PT60M spread over its quarters."""
    params = {
        "securityToken": _token("entsoe-security-token"),
        "documentType": "A44",
        "in_Domain": SE4,
        "out_Domain": SE4,
        "periodStart": f"{start:%Y%m%d%H%M}",
        "periodEnd": f"{end:%Y%m%d%H%M}",
    }
    body = _get(urllib.request.Request(f"{ENTSOE_URL}?{urllib.parse.urlencode(params)}"), "entsoe")
    root = ElementTree.fromstring(body)
    ns = {"n": root.tag.split("}")[0].strip("{")}
    out: dict[str, float] = {}
    for period in root.iterfind(".//n:Period", ns):
        first = datetime.fromisoformat(period.find("n:timeInterval/n:start", ns).text.replace("Z", "+00:00"))
        last = datetime.fromisoformat(period.find("n:timeInterval/n:end", ns).text.replace("Z", "+00:00"))
        step = timedelta(minutes=15 if period.find("n:resolution", ns).text == "PT15M" else 60)
        points = {
            int(p.find("n:position", ns).text): float(p.find("n:price.amount", ns).text)
            for p in period.iterfind("n:Point", ns)
        }
        price = None
        position, at = 1, first
        while at < last:
            price = points.get(position, price)
            for quarter in range(int(step / SLOT)):
                out[(at + SLOT * quarter).isoformat()] = price
            position, at = position + 1, at + step
    return out


def _ecb_sek() -> dict[str, float]:
    root = ElementTree.fromstring(_get(urllib.request.Request(ECB_URL), "ecb"))
    out: dict[str, float] = {}
    for cube in root.iter():
        if "time" in cube.attrib:
            for rate in cube:
                if rate.attrib.get("currency") == "SEK":
                    out[cube.attrib["time"]] = float(rate.attrib["rate"])
    return out


def _quarter_means(rows: list[list], up: list[bool], t0: datetime, minutes: int) -> list[float | None]:
    """Forward-filled minute values, blanked in down-time, as quarter-hour means."""
    filled: list[float | None] = [None] * minutes
    index = {(datetime.fromisoformat(t).replace(tzinfo=timezone.utc) - t0) // timedelta(minutes=1): v for t, v in rows}
    value = None
    for minute in range(minutes):
        if minute in index and index[minute] is not None:
            value = index[minute]
        filled[minute] = value if up[minute] else None
    out: list[float | None] = []
    for q in range(minutes // 15):
        chunk = [v for v in filled[q * 15:(q + 1) * 15] if v is not None]
        out.append(sum(chunk) / len(chunk) if len(chunk) >= 10 else None)
    return out


@dataclass
class History:
    t0: datetime
    grid: list[float | None]
    pv: list[float | None]
    batt: list[float | None]
    load: list[float | None]
    soc: list[float | None]
    car: list[float]
    spot: list[float | None] = field(default_factory=list)  # SEK/kWh

    def at(self, when: datetime) -> int:
        return (when - self.t0) // SLOT

    def house(self, i: int) -> float | None:
        """The house without the car: Sigenergy's total load where it exists, else grid + pv - battery."""
        total = self.load[i]
        if total is None and None not in (self.grid[i], self.pv[i], self.batt[i]):
            total = self.grid[i] + self.pv[i] - self.batt[i] - DERIVED_BIAS_KW
        if total is None:
            return None
        return max(0.0, total - self.car[i])


def load_history(start: datetime, end: datetime, cache: Path) -> History:
    minutes = int((end - start) / timedelta(minutes=1))
    tag = f"{start:%Y%m%d}_{end:%Y%m%d}"
    raw = {
        name: _cached(cache / f"{name}_{tag}.json.gz", lambda t=table, e=entity: _influx_minutes(t, e, start, end))
        for name, (table, entity) in SERIES.items()
    }
    seen = [False] * minutes
    for t, _v in raw["grid_kw"]:
        m = (datetime.fromisoformat(t).replace(tzinfo=timezone.utc) - start) // timedelta(minutes=1)
        if 0 <= m < minutes:
            seen[m] = True
    up = [any(seen[max(0, m - 9):m + 1]) for m in range(minutes)]
    q = {name: _quarter_means(rows, up, start, minutes) for name, rows in raw.items()}
    car = [(v or 0.0) / 1000 for v in q["car_w"]]
    history = History(start, q["grid_kw"], q["pv_kw"], q["batt_kw"], q["load_kw"], q["soc"], car)

    prices = _cached(cache / f"entsoe_{tag}.json.gz", lambda: _entsoe_prices(start - timedelta(days=1), end))
    rates = _cached(cache / f"ecb_{tag}.json.gz", _ecb_sek)
    days = sorted(rates)
    for i in range(len(car)):
        at = start + SLOT * i
        eur = prices.get(at.isoformat())
        before = (at.astimezone(TZ).date() - timedelta(days=1)).isoformat()
        k = bisect.bisect_right(days, before) - 1
        history.spot.append(None if eur is None or k < 0 else eur * rates[days[k]] / 1000)
    return history


# ----------------------------------------------------------------------------- forecasts


def p75_house(history: History, at: datetime, n: int) -> list[float]:
    """The weighted p75 of the house per weekday/weekend x local hour, from the 6 weeks before [at]."""
    cells: dict[tuple[bool, int], list[tuple[float, float]]] = {}
    first = max(0, history.at(at - timedelta(weeks=6)))
    for i in range(first, history.at(at)):
        value = history.house(i)
        if value is None or history.car[i] > 0.5:
            continue
        local = (history.t0 + SLOT * i).astimezone(TZ)
        age_days = (at - (history.t0 + SLOT * i)).total_seconds() / 86400
        cells.setdefault((local.weekday() >= 5, local.hour), []).append((value, 0.5 ** (age_days / 14)))

    def quantile(pairs: list[tuple[float, float]], q: float) -> float:
        pairs = sorted(pairs)
        total = sum(w for _, w in pairs)
        running = 0.0
        for value, weight in pairs:
            running += weight
            if running >= q * total:
                return value
        return pairs[-1][0]

    fallback = quantile([p for c in cells.values() for p in c], 0.75)
    out = []
    for k in range(n):
        local = (at + SLOT * k).astimezone(TZ)
        pairs = cells.get((local.weekday() >= 5, local.hour))
        out.append(quantile(pairs, 0.75) if pairs and len(pairs) >= 8 else fallback)
    return out


def persistence_sun(history: History, start: int, n: int) -> list[float]:
    """The sun of the same quarter hour one day before (two for the plan's second day)."""
    out = []
    for k in range(n):
        back = 96 * (1 + k // 96)
        value = history.pv[start + k - back] if start + k - back >= 0 else None
        out.append(max(0.0, value or 0.0))
    return out


# ----------------------------------------------------------------------------- replay


@dataclass(frozen=True)
class Site:
    battery: BatteryParams
    import_cap_kw: float
    export_cap_kw: float
    fee_minor: float  # energy tax + grid fee per kWh, before VAT
    fee_percent: float  # the grid fee's part that is a share of the spot price, before VAT
    vat_percent: float
    sell_credit_minor: float
    cycle_cost: float

    def buy(self, spot_sek: float) -> float:
        spot = spot_sek * 100
        return (spot + spot * self.fee_percent / 100 + self.fee_minor) * (1 + self.vat_percent / 100)

    def sell(self, spot_sek: float) -> float:
        return spot_sek * 100 + self.sell_credit_minor


def _slots(history: History, site: Site, start: int, n: int, house: Sequence[float], sun: Sequence[float]) -> tuple:
    return tuple(
        PlanSlot(
            start=history.t0 + SLOT * (start + k),
            end=history.t0 + SLOT * (start + k + 1),
            buy=site.buy(history.spot[start + k]),
            sell=site.sell(history.spot[start + k]),
            house_kw=max(0.0, house[k]),
            solar_kw=max(0.0, sun[k]),
            car_kw=history.car[start + k],
            import_cap_kw=site.import_cap_kw,
            export_cap_kw=site.export_cap_kw,
        )
        for k in range(n)
    )


def _real(history: History, i: int) -> tuple[float, float]:
    house = history.house(i)
    return (house if house is not None else 0.0), max(0.0, history.pv[i] or 0.0)


@dataclass
class Tally:
    cost: float = 0.0
    import_kwh: float = 0.0
    export_kwh: float = 0.0
    grid_charge_kwh: float = 0.0


def replay(
    history: History,
    site: Site,
    first: datetime,
    last: datetime,
    mode: str,
    end_value: float,
    export: bool = False,
    every: int = 96,
) -> tuple:
    """Day by day from 13:00 local on [first] to 13:00 on [last]; returns (tally, start kWh, end kWh)."""
    battery = site.battery
    i = history.at(first)
    soc_start = history.soc[i] if history.soc[i] is not None else 50.0
    soc_kwh = battery.usable_kwh * soc_start / 100
    start_kwh = soc_kwh
    tally = Tally()
    at = first
    while at < last:
        # Prices reach the end of tomorrow from 13:00, else the end of today.
        local = at.astimezone(TZ)
        days = 2 if local.hour >= 13 else 1
        local_end = datetime.combine(local.date() + timedelta(days=days), datetime.min.time(), TZ)
        n = min(history.at(local_end) - i, len(history.spot) - i)
        while n > 0 and history.spot[i + n - 1] is None:
            n -= 1
        run = min(every, n, history.at(last) - i)
        if mode == "self":
            actions = None
        else:
            if mode == "perfect":
                pairs = [_real(history, i + k) for k in range(n)]
                house, sun = [p[0] for p in pairs], [p[1] for p in pairs]
            else:
                house, sun = p75_house(history, at, n), persistence_sun(history, i, n)
            request = PlanRequest(
                slots=_slots(history, site, i, n, house, sun),
                battery=battery,
                soc_now_pct=min(100.0, max(0.0, soc_kwh / battery.usable_kwh * 100)),
                end_value=end_value,
                cycle_cost=site.cycle_cost,
                allow_export_discharge=export,
            )
            actions = plan_battery(request).plan.decisions
        real_house_sun = [_real(history, i + k) for k in range(run)]
        slots = _slots(
            history, site, i, run, [h for h, _ in real_house_sun], [s for _, s in real_house_sun]
        )
        for k, slot in enumerate(slots):
            action = ACTION_SELF_CONSUME if actions is None else actions[k].action
            planned_kw = 0.0 if actions is None else actions[k].battery_kw
            power = action_power(slot, battery, soc_kwh, action, planned_kw)
            outcome = run_slot(slot, battery, soc_kwh, action, power, site.cycle_cost)
            soc_kwh = battery.usable_kwh * outcome.soc_end_pct / 100
            if history.grid[i + k] is None:
                continue  # no meter reading: left out of every run, as of the actual one
            tally.cost += outcome.cost
            hours = slot.hours
            tally.import_kwh += max(0.0, outcome.grid_kw) * hours
            tally.export_kwh += max(0.0, -outcome.grid_kw) * hours
            if outcome.battery_kw > 0 and outcome.grid_kw > 0:
                tally.grid_charge_kwh += min(outcome.battery_kw, outcome.grid_kw) * hours
        i += run
        at += SLOT * run
    return tally, start_kwh, soc_kwh


def actual(history: History, site: Site, first: datetime, last: datetime) -> tuple:
    tally = Tally()
    i0, i1 = history.at(first), history.at(last)
    for i in range(i0, i1):
        grid, spot = history.grid[i], history.spot[i]
        if grid is None or spot is None:
            continue
        hours = 0.25
        if grid >= 0:
            tally.cost += grid * hours * site.buy(spot)
            tally.import_kwh += grid * hours
        else:
            tally.cost += grid * hours * site.sell(spot)
            tally.export_kwh += -grid * hours
    usable = site.battery.usable_kwh
    soc0 = next(history.soc[i] for i in range(i0, i1) if history.soc[i] is not None)
    soc1 = next(history.soc[i] for i in range(i1 - 1, i0, -1) if history.soc[i] is not None)
    return tally, usable * soc0 / 100, usable * soc1 / 100


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--cache", type=Path, default=Path("."))
    parser.add_argument("--first", default="2026-10-13", help="first local day (replay starts 13:00)")
    parser.add_argument("--last", default="2026-11-12", help="last local day (replay ends 13:00)")
    parser.add_argument("--usable-kwh", type=float, default=40.3)
    parser.add_argument("--max-kw", type=float, default=13.2, help="the plant's max active power, both ways")
    parser.add_argument("--min-soc", type=float, default=0.0)
    parser.add_argument("--efficiency", type=float, default=0.94, help="measured over the default period")
    parser.add_argument("--import-cap", type=float, default=13.8, help="20 A x 3 x 230 V")
    parser.add_argument("--export-cap", type=float, default=13.2)
    parser.add_argument("--fee", type=float, default=61.9, help="energy tax + grid fee, öre/kWh before VAT")
    parser.add_argument("--fee-percent", type=float, default=2.8, help="grid fee as %% of spot, before VAT")
    parser.add_argument("--vat", type=float, default=25.0)
    parser.add_argument("--sell-credit", type=float, default=0.0, help="öre/kWh on top of spot for export")
    parser.add_argument("--cycle-cost", type=float, default=5.0)
    parser.add_argument("--every", type=int, default=96, help="quarter hours between plans (96: day by day)")
    parser.add_argument("--json", type=Path, help="also write the table's numbers here")
    args = parser.parse_args()

    first = datetime.combine(date.fromisoformat(args.first), datetime.min.time(), TZ).replace(hour=13)
    last = datetime.combine(date.fromisoformat(args.last), datetime.min.time(), TZ).replace(hour=13)
    history_start = (first - timedelta(weeks=7)).astimezone(timezone.utc).replace(hour=0, minute=0)
    history_end = (last + timedelta(days=2)).astimezone(timezone.utc).replace(hour=0, minute=0)
    args.cache.mkdir(parents=True, exist_ok=True)
    history = load_history(history_start, history_end, args.cache)

    site = Site(
        battery=BatteryParams(
            usable_kwh=args.usable_kwh,
            max_charge_kw=args.max_kw,
            max_discharge_kw=args.max_kw,
            min_soc_pct=args.min_soc,
            max_soc_pct=100.0,
            round_trip_efficiency=args.efficiency,
        ),
        import_cap_kw=args.import_cap,
        export_cap_kw=args.export_cap,
        fee_minor=args.fee,
        fee_percent=args.fee_percent,
        vat_percent=args.vat,
        sell_credit_minor=args.sell_credit,
        cycle_cost=args.cycle_cost,
    )
    i0, i1 = history.at(first), history.at(last)
    buys = [site.buy(history.spot[i]) for i in range(i0, i1) if history.spot[i] is not None]
    end_value = statistics.median(buys) * args.efficiency

    rows = {"actual": actual(history, site, first, last)}
    for mode in ("self", "perfect", "forecast"):
        rows[mode] = replay(history, site, first, last, mode, end_value, every=args.every)
    for mode in ("perfect", "forecast"):
        rows[f"{mode}_export"] = replay(history, site, first, last, mode, end_value, export=True, every=args.every)

    labels = {
        "actual": "Actual (Sigenergy AI mode)",
        "self": "Self-consumption (simulated)",
        "perfect": "Plan, perfect foresight",
        "forecast": "Plan, p75 load + persistence sun",
        "perfect_export": "Plan with export discharge, perfect foresight",
        "forecast_export": "Plan with export discharge, p75 load + persistence sun",
    }
    out = {}
    print(f"Period {first:%Y-%m-%d %H:%M} to {last:%Y-%m-%d %H:%M} local, end value {end_value:.1f} öre/kWh")
    print("| Run | Cost kr | Start kWh | End kWh | Cost kr, end energy valued | vs self-consumption kr | "
          "Import kWh | Export kWh | Grid charge kWh |")
    print("|---|---|---|---|---|---|---|---|---|")
    base = None
    for mode, (tally, start_kwh, end_kwh) in rows.items():
        adjusted = (tally.cost - (end_kwh - start_kwh) * end_value) / 100
        if mode == "self":
            base = adjusted
        out[mode] = {"cost": tally.cost / 100, "adjusted": adjusted, "start_kwh": start_kwh, "end_kwh": end_kwh,
                     "import_kwh": tally.import_kwh, "export_kwh": tally.export_kwh,
                     "grid_charge_kwh": tally.grid_charge_kwh}
    for mode, row in out.items():
        delta = "" if base is None else f"{row['adjusted'] - base:+.0f}"
        grid_charge = "" if mode == "actual" else f"{row['grid_charge_kwh']:.0f}"
        print(f"| {labels[mode]} | {row['cost']:.0f} | {row['start_kwh']:.1f} | {row['end_kwh']:.1f} | "
              f"{row['adjusted']:.0f} | {delta} | {row['import_kwh']:.0f} | {row['export_kwh']:.0f} | {grid_charge} |")
    if args.json:
        args.json.write_text(json.dumps({"end_value": end_value, "rows": out}, indent=2))
    if any(math.isnan(r["cost"]) for r in out.values()):
        raise SystemExit("a cost came out NaN")


if __name__ == "__main__":
    main()
